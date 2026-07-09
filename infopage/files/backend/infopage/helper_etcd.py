import requests
import ssl
import json
import os

# Etcd configuration - these should be sourced from proper configuration management
ETCD_ENDPOINTS = [
    "10.10.10.21:2379",
    "10.10.10.22:2379",
    "10.10.10.23:2379"
]
ETCD_CLIENT_CERT = os.environ.get('ETCD_CLIENT_CRT')
ETCD_CLIENT_KEY = os.environ.get('ETCD_CLIENT_KEY')
ETCD_CA_CERT = os.environ.get('ETCD_CA_CRT')

def _memfd_path(content: str, name: str):
    fd = os.memfd_create(name, flags=0)
    os.write(fd, content.encode())
    os.lseek(fd, 0, os.SEEK_SET)
    return fd, f"/proc/self/fd/{fd}"

cert_fd, cert_path = _memfd_path(ETCD_CLIENT_CERT, "etcd-client-cert")
key_fd, key_path = _memfd_path(ETCD_CLIENT_KEY, "etcd-client-key")
etcd_ca_fd, etcd_ca_path = _memfd_path(ETCD_CA_CERT, "etcd-ca-cert")

# Global SSL context for etcd connections
_etcd_ssl_context = None
def get_etcd_ssl_context():
    """Create SSL context for etcd client certificate authentication"""
    global _etcd_ssl_context
    if _etcd_ssl_context is None:
        try:
            _etcd_ssl_context = ssl.create_default_context(cafile=etcd_ca_fd)
            _etcd_ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            print(f"Etcd SSL context created successfully")
        except Exception as e:
            print(f"Error creating etcd SSL context: {e}")
            raise
    return _etcd_ssl_context

def get_etcd_data():
    """
    Get etcd cluster health from all endpoints
    Returns: aggregated health status across all etcd members
    """
    health_results = {}
    member_list_fetched = False
    results = {}
    for endpoint in ETCD_ENDPOINTS:
        try:
            response = requests.get(
                f"{'https://'+endpoint.rstrip('/')}/health",
                verify=etcd_ca_path,
                cert=(cert_path, key_path)
            )
            response.raise_for_status()
            data = response.json()
            health_results[endpoint] = {
                "status": "healthy" if response.status_code == 200 else "unhealthy",
                "response_code": response.status_code,
                "response_text": response.text,
                "show": "healthy" if data["health"] else f"unhealthy: {data['reason']}"
            }
        except Exception as e:
            health_results[endpoint] = {
                "status": "error",
                "response_code": -1,
                "response_text": str(e),
                "show": f"unhealthy: {e}"
            }
        results[endpoint] = {'health': health_results[endpoint]["show"]}
        try:
            response = requests.post(
                f"{'https://'+endpoint.rstrip('/')}/v3/maintenance/status",
                json={},
                verify=etcd_ca_path,
                cert=(cert_path, key_path),
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            print("maintenance: "+response.text)
        except Exception as e:
            data = {'version': 'N/A', 'raftIndex': 'N/A'}

        results[endpoint]['version'] = data["version"]
        results[endpoint]['raftIndex'] = data['raftIndex']
        results[endpoint]['leader'] = 'true' if data['header']['member_id'] == data['leader'] else 'false'
        results[endpoint]['dbSizeInUse'] = round(int(data['dbSizeInUse'])/1024/1024,2)

    return results
