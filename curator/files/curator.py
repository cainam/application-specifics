import kopf
import kubernetes
import sys

MAX_RETRIES = 3

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # Disable cluster-wide namespace scanning
    settings.scanning.disabled = True
    # Fix duplicate logging: keep only Kopf's handler on root logger
    import logging
    root = logging.getLogger()
    root.handlers = [ h for h in root.handlers if h.__class__.__name__ == "_KopfStreamHandler" ]

@kopf.on.field('v1', 'pods', field='status.containerStatuses')
def handle_pull_failures(logger, old, new, name, namespace, spec, **kwargs):
    logger.info(f"handler called, name: {name}")
    return

    # imagePullPolicy is not allowed to change for Pods, so the code from here is useless
    if not new:
        return

    for status in new:
        state = status.get('state', {})
        waiting = state.get('waiting', {})
        logger.info(f"Pod {name} - state: {state}.")

        # Check if the pod is stuck due to image pull issues
        if waiting.get('reason') in ['ImagePullBackOff', 'ErrImagePull']:
            retries = status.get('restartCount', 0)
            if retries >= MAX_RETRIES:
                logger.info(f"Pod {name} failed {retries} times. Switching to IfNotPresent.")
                patch_pod_policy(name, namespace)

def patch_pod_policy(name, namespace):
    api = kubernetes.client.CoreV1Api()
    # Note: Many Pod fields are immutable. To change imagePullPolicy,
    # you often have to delete and recreate the pod, or ensure the
    # controller manages a Deployment/StatefulSet instead.
    body = {
        "spec": {
            "containers": [{"name": "example", "imagePullPolicy": "IfNotPresent"}]
        }
    }
    api.patch_namespaced_pod(name, namespace, body)
