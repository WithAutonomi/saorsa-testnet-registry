import os
import socket
import time

import requests

DO_API_URL = "https://api.digitalocean.com/v2"
BUILD_REGION = "lon1"
BUILD_SIZE = "c-16"
BUILD_IMAGE = "ubuntu-24-04-x64"
SSH_KEY_IDS = [
    36971688,
    30643816,
    30113222,
    42022675,
    30878672,
    31216015,
    34183228,
    38596814,
    54385801,
]


def _get_headers():
    token = os.environ.get("DO_TOKEN")
    if not token:
        raise RuntimeError("DO_TOKEN environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _find_droplet_by_name(droplet_name, headers):
    """Find an existing droplet by exact name. Returns the droplet dict or None."""
    resp = requests.get(f"{DO_API_URL}/droplets", headers=headers, params={"name": droplet_name})
    resp.raise_for_status()
    for droplet in resp.json()["droplets"]:
        if droplet["name"] == droplet_name:
            return droplet
    return None


def _get_public_ip(droplet):
    """Extract the public IP address from a droplet dict."""
    ip_address = droplet["networks"]["v4"][0]["ip_address"]
    for net in droplet["networks"]["v4"]:
        if net["type"] == "public":
            ip_address = net["ip_address"]
            break
    return ip_address


def _wait_for_droplet_active(droplet_id, headers, timeout=300):
    """Poll until the droplet status is 'active'."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        resp = requests.get(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
        resp.raise_for_status()
        status = resp.json()["droplet"]["status"]
        if status == "active":
            return resp.json()["droplet"]
        time.sleep(5)
    raise TimeoutError(f"Droplet {droplet_id} did not become active within {timeout}s")


def wait_for_ssh(ip: str, timeout: int = 300) -> None:
    """Poll SSH port 22 until the host accepts connections."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            sock = socket.create_connection((ip, 22), timeout=5)
            sock.close()
            return
        except (ConnectionRefusedError, TimeoutError, OSError):
            time.sleep(5)
    raise TimeoutError(f"SSH on {ip} not available within {timeout}s")


def create_build_vm(repo_owner: str, branch_name: str) -> dict:
    """Create an ephemeral DO droplet for building saorsa-node.

    If a droplet with the same name already exists (from a failed previous run),
    it is destroyed first.

    Returns a dict with keys: droplet_id, droplet_name, ip_address.
    """
    headers = _get_headers()
    droplet_name = f"saorsa-build-{repo_owner}-{branch_name}"

    existing = _find_droplet_by_name(droplet_name, headers)
    if existing:
        ip_address = _get_public_ip(existing)
        return {
            "droplet_id": existing["id"],
            "droplet_name": droplet_name,
            "ip_address": ip_address,
            "reused": True,
        }

    resp = requests.post(
        f"{DO_API_URL}/droplets",
        headers=headers,
        json={
            "name": droplet_name,
            "region": BUILD_REGION,
            "size": BUILD_SIZE,
            "image": BUILD_IMAGE,
            "ssh_keys": SSH_KEY_IDS,
        },
    )
    resp.raise_for_status()
    droplet_id = resp.json()["droplet"]["id"]
    droplet = _wait_for_droplet_active(droplet_id, headers)
    ip_address = _get_public_ip(droplet)

    return {
        "droplet_id": droplet_id,
        "droplet_name": droplet_name,
        "ip_address": ip_address,
        "reused": False,
    }


def destroy_build_vm(droplet_id: int) -> None:
    """Destroy the build droplet."""
    headers = _get_headers()
    requests.delete(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
