import os
import time

import requests

DO_API_URL = "https://api.digitalocean.com/v2"
BOOTSTRAP_REGION = "lon1"
BOOTSTRAP_SIZE = "s-2vcpu-4gb"
BOOTSTRAP_IMAGE = "ubuntu-24-04-x64"
BOOTSTRAP_VOLUME_SIZE_GB = 35
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


def _find_volume_by_name(volume_name, headers):
    """Find an existing volume by name and region. Returns the volume dict or None."""
    resp = requests.get(
        f"{DO_API_URL}/volumes",
        headers=headers,
        params={"name": volume_name, "region": BOOTSTRAP_REGION},
    )
    resp.raise_for_status()
    for volume in resp.json()["volumes"]:
        if volume["name"] == volume_name:
            return volume
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


def create_bootstrap_vm(name):
    """Create the bootstrap VM with an attached volume via the DO API.

    Idempotent: skips creation of droplet/volume if they already exist.

    Returns a dict with keys: droplet_id, droplet_name, ip_address, volume_id, created.
    The 'created' key is True if resources were newly created, False if they already existed.
    """
    headers = _get_headers()
    droplet_name = f"{name}-saorsa-bootstrap"
    volume_name = f"{name}-saorsa-bootstrap-storage".lower()

    created = False

    # Check for existing droplet
    existing_droplet = _find_droplet_by_name(droplet_name, headers)
    if existing_droplet:
        droplet_id = existing_droplet["id"]
        ip_address = _get_public_ip(existing_droplet)
    else:
        droplet_resp = requests.post(
            f"{DO_API_URL}/droplets",
            headers=headers,
            json={
                "name": droplet_name,
                "region": BOOTSTRAP_REGION,
                "size": BOOTSTRAP_SIZE,
                "image": BOOTSTRAP_IMAGE,
                "ssh_keys": SSH_KEY_IDS,
            },
        )
        droplet_resp.raise_for_status()
        droplet_id = droplet_resp.json()["droplet"]["id"]
        droplet = _wait_for_droplet_active(droplet_id, headers)
        ip_address = _get_public_ip(droplet)
        created = True

    # Check for existing volume
    existing_volume = _find_volume_by_name(volume_name, headers)
    if existing_volume:
        volume_id = existing_volume["id"]
        # Ensure it's attached to our droplet
        if droplet_id not in existing_volume.get("droplet_ids", []):
            attach_resp = requests.post(
                f"{DO_API_URL}/volumes/{volume_id}/actions",
                headers=headers,
                json={
                    "type": "attach",
                    "droplet_id": droplet_id,
                    "region": BOOTSTRAP_REGION,
                },
            )
            attach_resp.raise_for_status()
    else:
        volume_resp = requests.post(
            f"{DO_API_URL}/volumes",
            headers=headers,
            json={
                "size_gigabytes": BOOTSTRAP_VOLUME_SIZE_GB,
                "name": volume_name,
                "region": BOOTSTRAP_REGION,
                "filesystem_type": "ext4",
            },
        )
        volume_resp.raise_for_status()
        volume_id = volume_resp.json()["volume"]["id"]
        created = True

        # Attach volume to droplet
        attach_resp = requests.post(
            f"{DO_API_URL}/volumes/{volume_id}/actions",
            headers=headers,
            json={
                "type": "attach",
                "droplet_id": droplet_id,
                "region": BOOTSTRAP_REGION,
            },
        )
        attach_resp.raise_for_status()

    return {
        "droplet_id": droplet_id,
        "droplet_name": droplet_name,
        "ip_address": ip_address,
        "volume_id": volume_id,
        "created": created,
    }


def destroy_bootstrap_vm(droplet_id, volume_id):
    """Destroy the bootstrap VM and its volume."""
    headers = _get_headers()

    # Detach volume first
    requests.post(
        f"{DO_API_URL}/volumes/{volume_id}/actions",
        headers=headers,
        json={
            "type": "detach",
            "droplet_id": droplet_id,
            "region": BOOTSTRAP_REGION,
        },
    )
    # Wait for detach
    time.sleep(15)

    # Delete droplet
    requests.delete(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)

    # Delete volume
    requests.delete(f"{DO_API_URL}/volumes/{volume_id}", headers=headers)


def find_and_destroy_bootstrap_vm(name):
    """Find the bootstrap VM and volume by deployment name and destroy them.

    Looks up the droplet and volume by their naming convention, then calls
    destroy_bootstrap_vm(). Returns a dict with keys: found, droplet_name.

    If neither the droplet nor volume is found, returns found=False and does nothing.
    """
    headers = _get_headers()
    droplet_name = f"{name}-saorsa-bootstrap"
    volume_name = f"{name}-saorsa-bootstrap-storage".lower()

    droplet = _find_droplet_by_name(droplet_name, headers)
    volume = _find_volume_by_name(volume_name, headers)

    if not droplet and not volume:
        return {"found": False, "droplet_name": droplet_name}

    droplet_id = droplet["id"] if droplet else None
    volume_id = volume["id"] if volume else None

    if droplet_id and volume_id:
        destroy_bootstrap_vm(droplet_id, volume_id)
    elif droplet_id:
        requests.delete(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
    elif volume_id:
        requests.delete(f"{DO_API_URL}/volumes/{volume_id}", headers=headers)

    return {"found": True, "droplet_name": droplet_name}
