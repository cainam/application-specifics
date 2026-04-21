import base64
import json
import logging
import yaml
import sys

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from kubernetes.client import models as k8s

import valmut_helper
from valmut_model import Status, PatchType, AdmissionRequest, AdmissionResponse, AdmissionReview

# 1. Reset and configure the root logger
# force=True clears out any handlers Uvicorn or other libs already attached
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True 
)

# 2. Create your module logger
# It will now inherit the root's stdout handler and format automatically
logger = logging.getLogger(__name__)

#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
#logger = logging.getLogger(__name__)

config_path = '/config/valmut.conf'
try:
    with open(config_path, 'r') as file:
        app_config = yaml.safe_load(file)
    logger.info('app_config loaded:\n'+str(app_config))
except FileNotFoundError:
    logger.error(f"Error: The file '{config_path}' was not found.")
except yaml.YAMLError as e:
    logger.error(f"Error parsing YAML: {e}")

name='valmut-mutate'
config_namespace = valmut_helper.get_current_namespace()
mutating_config = valmut_helper.get_configmap_data(config_namespace, name, logger)
#first_key = next(iter(mutating_config)) # the name of the key doesn't matter, we only need the content
mutating_config = yaml.safe_load(mutating_config['mutate'])
logger.info('found name: '+name+' mutating_config: '+str(mutating_config))

app = FastAPI(
    title="Kubernetes Admission WebHook Server",
    description="A FastAPI application serving as a Kubernetes Mutating and Validating Admission WebHook.",
    version="1.0.0",
)

def get_a_name_from_request(req):
    metadata = req.object.get("metadata", {}) if req.object else {}
    return (req.name or metadata.get("name") or metadata.get("generateName") or "unknown-name")

# one function for validation and mutation as the same items are addressed
def process_pod_object(req_object, mutate, exemptions=None):
    import copy, random, re
    pod_definition = copy.deepcopy(req_object)
    patch_ops = []
    allowed = True
    messages = [f"process_pod_object: provided object: {req_object}"]

    pod_labels = req_object['metadata'].get('labels', {})
    exemption_list = []
    pod2container = {}
    config = {}

    for name, data in exemptions.get('pods', {}).items():
      match = False
      if data.get('identifiedBy') == 'label':
        label_key = data.get('label')
        if label_key and pod_labels.get(label_key) == name: match = True
      elif data.get('identifiedBy') == 'name':
        if req_object['metadata'].get('name') == name: match = True
      if match:
          config = data
          if 'pod2container' in data: pod2container = data['pod2container']
          if 'exemptions' in data:
            exemption_list = exemption_list + data['exemptions']
            if 'privileged' in data['exemptions']:
              exemption_list = exemption_list + ['allowPrivilegeEscalation'] # because privileged=True implies allowPrivilegeEscalation=True, too
          break
    messages.append(f'pod labels: {pod_labels}\nexemptions: {exemption_list}')
        
    # emptyDir: create volumes if some directories need to be writable (e.g. /var/log) but still enforcing readOnlyRootFilesystem 
    for i, ed in enumerate(config.get('emptyDir', [])):
        vol_name = re.sub(r'[^a-z0-9.]+', '-', ed['path'].lower()).strip('-.')
        vol_name = re.sub(r'-+', '-', vol_name).strip('-.')
        vol_name = (vol_name+'-'+ed['container'])[:253]
        vols = req_object['spec'].get('volumes', )
        if vols is None:
          patch_ops.append({"op": "add", "path": "/spec/volumes", "value": [] })
        messages.append(f"- adding emptyDir volume for {ed['path']}")
        patch_ops.append({"op": "add", "path": "/spec/volumes/-", "value": {'name': vol_name, 'emptyDir': {} } })
        config['emptyDir'][i]['name'] = vol_name


    # Host Namespaces (Network, PID, IPC)
    for host_namespace in ['hostNetwork', 'hostPid', 'hostIpc']:
      if req_object['spec'].get(host_namespace):
        if host_namespace in exemption_list: 
          messages.append(f'- forbidden host namespace {host_namespace} violated, creation exempted')
        else:
          allowed = False
          messages.append(f"- forbidden host namespace {host_namespace} violated, creation forbidden")
    
    # HostPath volumes
    if req_object['spec'].get('volumes'):
        violates = []
        for volume in req_object['spec'].get('volumes'):
            if volume.get('hostPath'):
                if not 'hostPath' in exemption_list: allowed = False
                violates.append(volume.get('name'))
        if len(violates) > 0: messages.append(f"- forbidden 'hostPath' for volume(s): "+",".join(violates)+" violated, creation "+('exempted' if 'hostPath' in exemption_list else 'forbidden')+'')

    # /spec/securityContext
    pod_sc = req_object['spec'].get('securityContext')
    messages.append(f'- pod securityContext: {pod_sc}')
    if mutate:
        messages.append('- enforcing /spec/securityContext')
        sc_val = {"runAsNonRoot": True}
        sc_val.update({"seccompProfile": {"type": "RuntimeDefault"}})
        patch_ops.append({"op": "replace", "path": "/spec/securityContext", "value": sc_val })
    else:
        if pod_sc:
            notAsRoot = pod_sc.get('runAsNonRoot')           
            if not notAsRoot:
                if 'runAsNonRoot' not in exemption_list:
                    patch_ops.append({"op": "replace", "path": "/spec/securityContext/runAsNonRoot", "value": True })
            else:
                if notAsRoot is False: # runAsNonRoot
                    if 'runAsNonRoot' not in exemption_list: allowed = False
                    messages.append("- Pod 'spec.securityContext.runAsNonRoot' must be set to true => violated, creation "+('exempted' if 'runAsNonRoot' in exemption_list else 'forbidden')+'')
            scp = pod_sc.get('seccompProfile')
            if scp:
                if scp.get('type') not in ["RuntimeDefault", "Localhost"]: # seccompProfile
                    allowed = False
                    messages.append("- Pod 'seccompProfile.type' must be 'RuntimeDefault' or 'Localhost' => violated, creation forbidden")
            else:
                patch_ops.append({"op": "replace", "path": "/spec/securityContext/seccompProfile", "value": {"type": "RuntimeDefault"} })
        else: # Default is often 'Unconfined', which is not restricted
            messages.append("- Pod 'securityContext' missing, creation forbidden")

    for container_type in ['containers', 'initContainers', 'ephemeralContainers']:
      containers = req_object['spec'].get(container_type, [])
      for i, container in enumerate(containers):
        c_name = container.get('name')
        container_path = f"/spec/{container_type}/{i}"
        
        # add volumeMount for emptyDir if needed
        for ed in config.get('emptyDir', []):
            if ed['container'] == c_name:
              if container.get('volumeMounts') is None:
                patch_ops.append({"op": "add", "path": container_path+"/volumeMounts", 'value': []})
              messages.append(f"- adding emptyDir volumeMount in container {c_name} for {ed['path']}")
              patch_ops.append({"op": "add", "path": container_path+"/volumeMounts/-", "value": {'name': ed['name'], 'mountPath': ed['path'] } })

        if config.get('set') is not None and config.get('set').get(c_name) is not None and config.get('set').get(c_name).get('uid') is not None:
            uid = config['set'][c_name]['uid']
        else:
            uid = random.randint(2**16, 2**31-1)
        if config.get('set') is not None and config.get('set').get(c_name) is not None and config.get('set').get(c_name).get('gid') is not None:
            gid = config['set'][c_name]['gid']
        else:
            gid = random.randint(2**16, 2**31-1)
        set_caps = []
        if config.get('set') is not None and config.get('set').get(c_name) is not None and config.get('set').get(c_name).get('caps') is not None:
            set_caps = config['set'][c_name]['caps']

        # forbid the use of image tag "latest"
        image = container.get('image')
        if ':' not in image or image.split(':')[-1] == 'latest':
          messages.append(f"- Container '{c_name}': image tag 'latest' or no tag is forbidden => violated, creation forbidden")
          allowed = False

        sc = container.get('securityContext')
        logger.info(f"securityContext of {container_type} {i}: {sc}")
        sc_path = f"{container_path}/securityContext"
        if not sc:
            sc = {}
            if mutate:
                patch_ops.append({"op": 'add', 'path': sc_path, 'value': sc })
            else:
                allowed = False
                messages.append(f"- Container '{c_name}' 'securityContext' missing => violated, creation forbidden")

        for restriction_item in ['allowPrivilegeEscalation', 'privileged']:
            if sc.get(restriction_item) is not False:
                messages.append(f"- Container '{c_name}' '{restriction_item}' must be false => violated, creation "+('exempted' if restriction_item in exemption_list else 'forbidden')+'')
                if mutate:
                    if restriction_item not in exemption_list:
                        patch_ops.append({"op": "replace", "path": sc_path+"/"+restriction_item, "value": False})
                else:
                    if restriction_item not in exemption_list: allowed = False        

        # runAsUser, runAsGroup: if set check, if not generate
        if sc.get('runAsGroup'):
          messages.append(f"- Container '{c_name}': 'runAsGroup' is set to {sc.get('runAsGroup')}")
          pass # to be implemented if needed
        elif mutate:
            patch_ops.append({"op": "replace", "path": sc_path+'/runAsGroup', "value": gid})
        if sc.get('runAsUser'):
          messages.append(f"- Container '{c_name}': 'runAsUser' is set to {sc.get('runAsUser')}")
          pass # to be implemented if needed
        elif mutate:
            patch_ops.append({"op": "replace", "path": sc_path+'/runAsUser', "value": uid})
        if mutate:
            if uid == 0 and 'runAsNonRoot' in exemption_list:
                patch_ops.append({"op": "replace", "path": sc_path+'/runAsNonRoot', "value": False })
            else: # enforce runAsNonRoot setting: although normally inherited from Pod, PSA restricted requires this to be set again
                patch_ops.append({"op": "replace", "path": sc_path+'/runAsNonRoot', "value": True })

        # Container-level SeccompProfile - default from Pod Security Context, so can be empty (could also be dropped here)
        scp = sc.get('seccompProfile')
        if not scp or scp.get('type') not in ["RuntimeDefault"]:
            if mutate:
                patch_ops.append({"op": "replace", "path": sc_path+'/seccompProfile', "value": {"type": "RuntimeDefault"} })
            else:
                allowed = False
                messages.append(f"- Container '{c_name}' 'seccompProfile.type' must be 'RuntimeDefault' or 'Localhost' => violated, creation forbidden")
        
        # capabilities
        caps_path = f"{sc_path}/capabilities"
        caps = sc.get('capabilities')  
        messages.append(f"- Container '{c_name}' {caps_path}: {caps}")
        if caps is None:
          caps = {}
          if mutate:
              patch_ops.append({"op": "replace", "path": caps_path, "value": {} })
          else:
              allowed = False
              messages.append(f"- Container '{c_name}' capabilities have to be set => violated, creation forbidden")
        drop = caps.get('drop', [])
        if len(drop) != 1 or (len(drop) == 1 and drop[0] != 'ALL'):
            if mutate:
                patch_ops.append({"op": "replace", "path": caps_path+"/drop", "value": ['ALL'] })
            else:
                allowed = False
                messages.append("- Container '{c_name}' does not drop ALL => violated, creation forbidden")

        add = caps.get('add')                
        if add and len(add) > 0: # if add is missing it is semantically the same as add: []
            if 'caps_add' not in exemption_list:
                if mutate:
                    patch_ops.append({"op": "replace", "path": caps_path+"/add", "value": set_caps })
                elif len(add) != len(set_caps) or sorted(add) != sorted(set_caps): # fail if added capabilities are not set explicitly
                    allowed = False
                    messages.append(f"- Container '{c_name}' adds capabilities => violated, creation forbidden")
            else:
                messages.append(f"- Container '{c_name}' adds capabilities => violated, creation exempted")
        elif len(set_caps) != 0:
        # elif mutate: # enforce add: [] to hmake 
            patch_ops.append({"op": "replace", "path": caps_path+"/add", "value": set_caps })

        # readOnlyRootFilesystem
        readOnlyRootFilesystem = sc.get('readOnlyRootFilesystem')
        if readOnlyRootFilesystem is not True:
            if 'readOnlyRootFilesystem' in exemption_list:
                messages.append(f"- Container '{c_name}' 'readOnlyRootFilesystem' must be true but is "+str(readOnlyRootFilesystem)+" => violated, creation exempted")
            elif mutate:
                messages.append(f'- enforcing readOnlyRootFilesystem=True')
                patch_ops.append({"op": "replace", "path": sc_path+"/readOnlyRootFilesystem", "value": True})                
            else:
                allowed = False
                messages.append(f"- Container '{c_name}' 'readOnlyRootFilesystem' must be true but is "+str(readOnlyRootFilesystem)+" => violated, creation forbidden")

        # apply pod securityContext settings to containers
        for name, containers in pod2container.items():
          messages.append(f'container {c_name}: pod2container for {name} to containers {containers}')
          if c_name in containers and mutate:
            setting = pod_sc.get(name)
            if setting is not None:
                messages.append(f'apply setting {name} from pod securityContext to container {c_name}, value: {setting}')
                patch_ops.append({"op": "replace", "path": sc_path+"/"+name, "value": setting})
                # if it is runAsUser:0 then also runAsNonRoot needs to be set to false
                if name == 'runAsUser' and setting == 0:
                  messages.append(f'container {c_name}: runAsUser:0 is set, so runAsNonRoot:false is required, too')
                  patch_ops.append({"op": "replace", "path": sc_path+"/runAsNonRoot", "value": False})
            else:
              allowed = False
              messages.append(f'setting {name} was requested to be shifted from pod to container, but is not defined{setting}')

    return patch_ops if mutate else allowed, messages
  
def process_controller_object(req_object, mutate, exemptions=None):
    patch_ops = []
    messages = ''
    for name, data in exemptions.get('deployment', {}).items():
        logger.info("deployment: "+name+" data: "+str(data))
        #for name, data in data.get('deployment', {}).items():
        if name == 'standards':
            for path, value in data.items():
                patch_ops.append({"op": "replace", "path": path, "value": value})

    return patch_ops if mutate else allowed, messages


@app.post("/mutate")
async def mutate_webhook(request: Request): # preferred over mutate_webhook(admission_review: AdmissionReview) as the data validation happens inside the function and allows error handling
    """
    Handles mutating admission requests from Kubernetes.
    """
    admission_review = await valmut_helper.parse_request(request)
    if isinstance(admission_review, JSONResponse):
        logger.error(admission_review.body)
        return admission_review
    
    req = admission_review.request
    item_name = get_a_name_from_request(req)
    if req.kind.get("kind") == "Pod":
        logger.info(f"Mutating Pod {item_name} in namespace: {req.namespace}")
        if not req.object or not req.object.get('spec'):
            logger.warning(f"Pod {item_name} has no spec. Skipping mutation.")
            return JSONResponse(content=AdmissionReview(response=AdmissionResponse(uid=req.uid, allowed=True)).model_dump(by_alias=True, exclude_none=True))

        patches, messages = process_pod_object(req.object, True, mutating_config)
        full_message = f"Pod {req.object.get('metadata').get('name')} in namespace {req.object.get('metadata').get('namespace')} mutation: {'\n'.join(messages)}"
        logger.info(full_message)
        
        if "metadata" not in req.object: patches.append({"op": "add", "path": "/metadata", "value": {}})
        if "annotations" not in req.object.get("metadata", {}): patches.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        annotations = req.object.get("metadata", {}).get("annotations", {})
        if "mutated" not in annotations: patches.append({"op": "add", "path": "/metadata/annotations/admission-webhook-example.com~1mutated-by", "value": "yepp"})
    elif req.kind.get("kind") in ["Deployment", "StatefulSet", "CronJob", "DaemonSet"]:
        logger.info(f"Mutating Deployment: {item_name} in namespace: {req.namespace}")
        patches, messages = process_controller_object(req.object, True, mutating_config)
    else: # non-Pod resources, just allow without modification
        logger.info(f"Allowing non-Pod resource of kind: {req.kind.get('kind')} without mutation.")
        return JSONResponse(content=AdmissionReview(response=AdmissionResponse(uid=req.uid, allowed=True)).model_dump(by_alias=True, exclude_none=True))

    response = AdmissionResponse(
        uid=req.uid,
        allowed=True, # always allowed, it is mutating
        patch=base64.b64encode(json.dumps(patches).encode('utf-8')).decode('utf-8'), # Encode the patch operations to base64
        patch_type=PatchType.type  # Important: Specify the patch type
    )
    
    logger.info(f"Generated patch for {item_name}:\n{json.dumps(patches, indent=2)}")
    logger.info(f"json response: "+str(JSONResponse(content=AdmissionReview(response=response).model_dump(by_alias=True, exclude_none=True)) ))

    final_response_content = AdmissionReview(response=response).model_dump(by_alias=True, exclude_none=True)
    logger.info(f"Final AdmissionReview response JSON:\n{json.dumps(final_response_content, indent=2)}")
    return JSONResponse(content=final_response_content)

@app.post("/validate")
async def validate_webhook(request: Request):
    """
    Handles validating admission requests from Kubernetes.
    """
    
    logger.info("/validate called")
    admission_review = await valmut_helper.parse_request(request)
    if isinstance(admission_review, JSONResponse): 
        logger.error(admission_review.body)
        return admission_review

    logger.debug("Received validation request.\n"+str(admission_review))
    req = admission_review.request
    logger.debug("req.object\n"+str(req.object))

    item_name = get_a_name_from_request(req)
    if req.kind.get("kind") == "Pod":
        logger.info(f"Validating Pod: {item_name} in namespace: {req.namespace}")

        pod_dict = valmut_helper.convert_keys_to_snake_case(req.object)
        pod = k8s.V1Pod(**pod_dict)
        logger.debug("pod metadata: "+str(pod.metadata))

        pod_metadata = pod.metadata if pod.metadata else {} # Sicherstellen, dass es ein dict ist
        pod_name = pod_metadata.get('name', "unknown")
        namespace = pod_metadata.get('namespace', "unknown")
        
        allowed, messages = process_pod_object(req.object, False, mutating_config)
        full_message = f"Pod {item_name} in namespace {namespace} violates restricted security policy: {'\n'.join(messages)}"
        if allowed:
            logger.info(f"Pod {item_name} in namespace {namespace} conforms to restricted security policy. Allowing: {'\n'.join(messages)}")
        else:
            logger.warning(full_message)
    else:
        allowed = True

    response = AdmissionResponse(
        uid=req.uid,
        allowed=allowed,
        status=None if allowed else Status(message=full_message, code=403, status="Failure")
    )

    return JSONResponse(content=AdmissionReview(response=response).model_dump(by_alias=True, exclude_none=True))

# --- Health Check Endpoint ---
@app.get("/healthz")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}

