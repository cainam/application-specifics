import requests

serviceaccount = "/var/run/secrets/kubernetes.io/serviceaccount"
cacert = serviceaccount+"/ca.crt"
apiserver = "https://kubernetes.default.svc"
token = open(serviceaccount+'/token', 'r').read()
headers = {'Accept': '*/*', 'Authorization': 'Bearer '+token}

def vs_info():
  # list all namespaces
  content = []
  namespaces = requests.get(apiserver+"/api/v1/namespaces/", verify=cacert, headers=headers).json()
  for n in namespaces["items"]:
    namespace = n["metadata"]["name"]
    vss = requests.get(apiserver+"/apis/networking.istio.io/v1beta1/namespaces/"+namespace+"/virtualservices/", verify=cacert, headers=headers).json()
    for vs in vss["items"]:
      #print(vs["metadata"]["name"])
      #print(vs["spec"]["hosts"])
      for s in vs["spec"]["http"]:
        if "name" in s and "uri" in s["match"][0]:
          for host in vs["spec"]["hosts"]:
            content.append( [namespace, host, vs["metadata"]["name"], s["name"], s["match"][0]['uri']['prefix']] )
  return content
  
def node_info():
  # list all nodes
  content=[]
  nodes = requests.get(apiserver+"/api/v1/nodes/", verify=cacert, headers=headers).json()
  for n in nodes["items"]:
    info = "name: "+n["metadata"]["name"]+"\n kernelVersion:"+n["status"]["nodeInfo"]["kernelVersion"]+"\n containerRuntimeVersion:"+n["status"]["nodeInfo"]["containerRuntimeVersion"]
    cond=""
    for c in n["status"]["conditions"]:
        cond += c["type"]+": status:"+c["status"]+" reason:"+c["reason"]+"\n"

    content.append([ info, cond  ] )
  return content

def software(name):
  import yaml
  import re
  from packaging.specifiers import SpecifierSet

  software_file = "software"
  with open(software_file, 'r') as file:
    sw = yaml.safe_load(file)
  if name is None:
    return "",list(sw["software"].keys())
  else:
    raw = "" #str(sw)
    item = name
    raw+="\n"+item+"\n  current version: "+str(sw["software"][item]["version"])

    installed=["unknn"]
    if True:
      installed=[]
      installed_type = "k8s"
      installed_pattern = item+":"
      if "max_entries" in sw["software"][item]:
        max = sw["software"][item]["max_entries"]
      else:
        max = 5
      if "installed" in sw["software"][item]:
        if "type" in sw["software"][item]["installed"]:
          installed_type = sw["software"][item]["installed"]["type"]
        if "pattern" in sw["software"][item]["installed"]:
          installed_pattern = sw["software"][item]["installed"]["pattern"]
      pods = requests.get(apiserver+"/api/v1/pods/", verify=cacert, headers=headers).json()
      for pod in pods["items"]:
        for x in pod["spec"]["containers"]:
          if re.search(installed_pattern, x["image"]) and x["image"] not in installed:
            installed.append(x["image"]);
    content_item=[item,installed,sw["software"][item]["version"]]

    if not 'latest' in sw["software"][item]:
      return "no latest configured",""
    
    versions = []
    if sw["software"][item]["latest"]["type"] == "github":
      try:
        vers = requests.get("https://api.github.com/repos/"+sw["software"][item]["latest"]["params"]["repo"]+"/releases/latest") # | jq .tag_name
        vers.raise_for_status()
        raw += "\n  github version: "+str(vers.json()["tag_name"] )
        versions = [vers.json()["tag_name"]]
      except requests.exceptions.HTTPError as exc:
        versions = ["error while fetching information: "+str(exc.response.status_code)]
        raw += "\n error while fetching information: "+str(exc.response.status_code)
    elif sw["software"][item]["latest"]["type"] == "github-tags":
      try:
        vers = requests.get("https://api.github.com/repos/"+sw["software"][item]["latest"]["params"]["repo"]+"/tags") # | jq .tag_name
        vers.raise_for_status()
        raw += "\n  github version: "+str([item["name"] for item in vers.json()])
        versions = [item["name"] for item in vers.json()]
      except requests.exceptions.HTTPError as exc:
        versions = ["error while fetching information: "+str(exc.response.status_code)]
        raw += "\n error while fetching information: "+str(exc.response.status_code)
      except TypeError as e:
        raise TypeError("vers: "+str(vers.json()))

    elif sw["software"][item]["latest"]["type"] == "github-branches":
      try:
        vers = requests.get("https://api.github.com/repos/"+sw["software"][item]["latest"]["params"]["repo"]+"/branches") # | jq .tag_name
        vers.raise_for_status()
        raw += "\n  github branch version: "+str(vers.json()[-1]['name'] )
        versions = [vers.json()[-1]['name']]
      except requests.exceptions.HTTPError as exc:
        versions = ["error while fetching information: "+str(exc.response.status_code)]
        raw += "\n error while fetching information: "+str(exc.response.status_code)

    elif sw["software"][item]["latest"]["type"] == "dockerhub":
      dockerhub_base_url = "https://hub.docker.com/v2/repositories/"
      digest = requests.get(dockerhub_base_url+sw["software"][item]["latest"]["params"]["repo"]+"/tags/latest")
      for image in digest.json()["images"]:
        if image["architecture"] == sw["software"][item]["latest"]["params"]["arch"]:
          digest = image["digest"]
          break
      raw += "\n  latest digest: "+digest
      all_tags = requests.get(dockerhub_base_url+sw["software"][item]["latest"]["params"]["repo"]+"/tags/?page_size=500")
      for tag in all_tags.json()["results"]:
        if tag["name"] == "latest": continue
        for image in tag["images"]:
          if not "digest" in image:
            raw += "\n  no digest for image of tag named "+tag["name"]
            continue
          if image["digest"] == digest:
            versions.append(tag["name"])

    elif sw["software"][item]["latest"]["type"] == "quay":
      base_url = "https://quay.io/api/v1/repository/"
      results = requests.get(base_url+sw["software"][item]["latest"]["params"]["repo"]+"/tag/?onlyActiveTags=true")
      vspec = SpecifierSet(">="+sw["software"][item]["version"])
      for v in results.json()["tags"]:
        name = v["name"]
        if name[0:6] != "latest" and name.split("-")[0] in vspec:
          versions.append(name)

    elif sw["software"][item]["latest"]["type"] == "gentoo":
      try:
        base_url = "https://api.github.com/repos/gentoo/gentoo/contents/"
        results = requests.get(base_url+sw["software"][item]["latest"]["params"]["repo"])
        results.raise_for_status()
        for f in results.json():
          if f['name'] != "files" and f['name'] != "Manifest" and f['name'] != "metadata.xml" and not "9999" in f['name']:
            n=re.sub(item+'-','',f['name'])
            n=re.sub('.ebuild','',n)
            versions.append(n)
        versions.reverse()
      except requests.exceptions.HTTPError as exc:
        versions = ["error while fetching information: "+str(exc.response.status_code)]
        raw += "\n error while fetching information: "+str(exc.response.status_code)

    else:
      versions = ["unknown type"]

    content_item.append(versions[:max])
    return raw, content_item

