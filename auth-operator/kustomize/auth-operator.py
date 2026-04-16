import kopf
import asyncio
import json
import kubernetes
import os
import base64
import yaml
import requests

# env vars: namespace oauth2-proxy, hydra, secret, client_name, redirect_url, etc.
redirect_url = os.environ.get('REDIRECT_URL')
oauth2_config_secret = os.environ.get('OAUTH2_CONFIG_SECRET')
oauth2_secret_file = os.environ.get('OAUTH2_SECRET_FILE')
hydra_service = os.environ.get('HYDRA_SERVICE')
hydra_service_port = os.environ.get('HYDRA_SERVICE_PORT') # "4445"
hydra_search_label_key = os.environ.get('HYDRA_SEARCH_LABEL_KEY')
hydra_search_label_value = os.environ.get('HYDRA_SEARCH_LABEL_VALUE')

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # Disable cluster-wide namespace scanning
    settings.scanning.disabled = True

# extend with decorator for hydra
@kopf.on.create('pods', labels={'app.kubernetes.io/instance': 'hydra'} )
async def fun2(namespace, spec, body, logger, **kwargs):
  d = dict(body)
  logger.debug(json.dumps(d, indent=4))

  s = body.get("status")
  logger.debug("status, type:"+str(type(s))+" content:"+json.dumps(s, indent=4)  )
  logger.info("status phase:"+s["phase"] )

  if s["phase"] != "Running":
    raise kopf.TemporaryError(f"Pod is not ready yet", delay=10)

  api = kubernetes.client.CoreV1Api()
  # oauth2-proxy client_id and secret are mandatory at startup, get client_id and client_secret from oauth2_config
  oauth2_proxy_configmap = api.read_namespaced_secret(oauth2_config_secret, namespace).data
  logger.debug("secret: "+str(oauth2_proxy_configmap))
  secret_decoded = base64.b64decode(oauth2_proxy_configmap[oauth2_secret_file]).decode()
  logger.debug("secret decoded: "+str(secret_decoded))
  conf = yaml.safe_load(secret_decoded)["providers"][0] # assuming only one provider is configured
  client_id = conf['clientID']
  client_secret = conf['clientSecret']
  logger.debug("clientID:"+client_id+" client_secret:"+client_secret)

  # is a client_id set on hydra with same - client_secret should be checked too implementation left as TODO
  url = 'http://'+hydra_service+'.'+namespace+':'+hydra_service_port+'/clients'
  response = requests.get(url)
  client_missing = True
  for x in response.json():
    if x['client_id'] == client_id:
      client_missing = False

  logger.info("is the client_id missing? "+str(client_missing))
  # if not, create it
  if client_missing:
    client_request = {
      'client_id': client_id,
      'client_name': 'test',
      'client_secret': client_secret,
      'grant_types': ["authorization_code","refresh_token"],
      'redirect_uris': redirect_url.split(),
      'response_types': ["code", "id_token"],
      'scope': 'offline openid users.write users.read users.edit users.delete email',
      'token_endpoint_auth_method': 'client_secret_post'
    }
    logger.info("request for new client"+str(client_request))
    response = requests.post(url, json=client_request)
    logger.info("HTTP status of client creation request:"+str(response))

#kopf.run()

