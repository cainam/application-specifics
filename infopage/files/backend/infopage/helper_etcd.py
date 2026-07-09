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

def etcd_member_status():
    """
    Get etcd member status efficiently
    Since all etcd members maintain identical cluster state,
    query member/list only once from the primary endpoint
    Returns: list of dicts with merged etcd response data directly in each member info
    """
    member_status_list = []

    if not ETCD_ENDPOINTS:
        print("No etcd endpoints configured")
        return []

    # Use the first endpoint as primary since all etcd members
    # maintain identical cluster state (member list is the same from any member)
    primary_endpoint = ETCD_ENDPOINTS[0]

    try:
        # Remove /v3 prefix if present
        base_url = primary_endpoint.rstrip('/')

        # Get maintenance status from primary endpoint
        response = requests.get(
            f"{base_url}/v3/maintenance/status",
            verify=ETCD_CA_CERT,
            cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
            timeout=10
        )
        response.raise_for_status()
        maintenance_status = response.json()

        # Get cluster member list - GET THIS ONLY ONCE since all members have identical cluster state
        response = requests.get(
            f"{base_url}/v3/cluster/member/list",
            verify=ETCD_CA_CERT,
            cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
            timeout=10
        )
        response.raise_for_status()
        member_list = response.json()

        # Get health status
        response = requests.get(
            f"{base_url}/health",
            verify=ETCD_CA_CERT,
            cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
            timeout=5
        )
        health_status = response.text  # health endpoint returns plain text

        # Create member info dict and merge full etcd responses directly into it
        member_info = {"endpoint": primary_endpoint, "status": "success"}

        # Merge full etcd responses directly into member_info
        member_info.update(maintenance_status)
        member_info.update(member_list)
        member_info["health"] = health_status

    except requests.exceptions.RequestException as e:
        member_info = {"endpoint": primary_endpoint, "error": str(e), "status": "error"}

        # Fallback: try backup endpoint if primary fails
        if len(ETCD_ENDPOINTS) > 1:
            backup_endpoint = ETCD_ENDPOINTS[1]
            try:
                base_url = backup_endpoint.rstrip('/')

                # Get maintenance status from backup
                response = requests.get(
                    f"{base_url}/v3/maintenance/status",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=10
                )
                response.raise_for_status()
                maintenance_status = response.json()

                # Get cluster member list from backup
                response = requests.get(
                    f"{base_url}/v3/cluster/member/list",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=10
                )
                response.raise_for_status()
                member_list = response.json()

                # Get health status from backup
                response = requests.get(
                    f"{base_url}/health",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=5
                )
                health_status = response.text

                member_info = {"endpoint": backup_endpoint, "status": "success"}
                member_info.update(maintenance_status)
                member_info.update(member_list)
                member_info["health"] = health_status

            except Exception as e:
                member_info["error"] = f"Backup endpoint also failed: {str(e)}"
                member_info["status"] = "error"

    except Exception as e:
        member_info = {"endpoint": primary_endpoint, "error": f"Unexpected error: {str(e)}", "status": "error"}

        # Fallback: try backup endpoint if unexpected error occurs
        if len(ETCD_ENDPOINTS) > 1:
            backup_endpoint = ETCD_ENDPOINTS[1]
            try:
                base_url = backup_endpoint.rstrip('/')

                # Get all data from backup
                response = requests.get(
                    f"{base_url}/v3/maintenance/status",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=10
                )
                response.raise_for_status()
                maintenance_status = response.json()

                response = requests.get(
                    f"{base_url}/v3/cluster/member/list",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=10
                )
                response.raise_for_status()
                member_list = response.json()

                response = requests.get(
                    f"{base_url}/health",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=5
                )
                health_status = response.text

                member_info = {"endpoint": backup_endpoint, "status": "success"}
                member_info.update(maintenance_status)
                member_info.update(member_list)
                member_info["health"] = health_status

            except Exception as e:
                member_info["error"] = f"Backup endpoint also failed: {str(e)}"
                member_info["status"] = "error"

    member_status_list.append(member_info)

    # Additional endpoints (if any) would be monitored separately if needed
    # for health checks, but member list would remain the same
    for endpoint in ETCD_ENDPOINTS[1:]:
        if endpoint != primary_endpoint and endpoint != backup_endpoint:  # Only for endpoints not already processed
            try:
                response = requests.get(
                    f"{endpoint.rstrip('/')}/health",
                    verify=ETCD_CA_CERT,
                    cert=(ETCD_CLIENT_CERT, ETCD_CLIENT_KEY),
                    timeout=5
                )
                health_status = response.text
                member_status_list.append({
                    "endpoint": endpoint,
                    "status": "healthy" if response.status_code == 200 else "unhealthy",
                    "health": health_status,
                    "note": "Health check only (member list from primary)"
                })
            except Exception as e:
                member_status_list.append({
                    "endpoint": endpoint,
                    "status": "error",
                    "error": str(e),
                    "note": "Health check failed (member list from primary)"
                })

    return member_status_list
def get_etcd_data():
    """
    Get etcd cluster health from all endpoints
    Returns: aggregated health status across all etcd members
    """
    health_results = {}

    for endpoint in ETCD_ENDPOINTS:
        try:
            response = requests.get(
                f"{'https://'+endpoint.rstrip('/')}/health",
                verify=etcd_ca_path,
                cert=(cert_path, key_path),
                timeout=5
            )
            data = response.json()
            health_results[endpoint] = {
                "status": "healthy" if response.status_code == 200 else "unhealthy",
                "response_code": response.status_code,
                "response_text": response.text
                "show": "healthy" if data["health"] else f"unhealthy: {data['reason']}"
            }
        except Exception as e:
            health_results[endpoint] = {
                "status": "error",
                "response_code": -1,
                "response_text": str(e)
                "show": f"unhealthy: {e}"
            }

    return {'health': health_results}

def get_all_etcd_data():
    """
    Convenience function to get all etcd data in one call
    Returns: dict with member_status and cluster_health
    """
    return {
        "member_status": etcd_member_status(),
        "cluster_health": etcd_cluster_health()
    }
