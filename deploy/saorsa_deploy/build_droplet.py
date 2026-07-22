import hashlib
import os
import socket
import time

import requests

DO_API_URL = "https://api.digitalocean.com/v2"
BUILD_REGIONS = ["lon1", "ams3", "nyc1", "syd1"]
BUILD_SIZE = "c-32"
BUILD_IMAGE = "ubuntu-24-04-x64"
MAX_DROPLET_NAME_LENGTH = 63
BRANCH_HASH_LENGTH = 10
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


def _build_droplet_name(kind: str, name: str, repo_owner: str, branch_name: str) -> str:
    """Build a DigitalOcean-safe droplet name.

    DO caps droplet names at 63 characters, but branch names are unbounded
    (e.g. `chris/disable-outbound-audits-0.14.4` alone pushed a real name over
    the limit), so the branch is condensed to a short deterministic hash
    rather than embedded verbatim. Hashing (not a random id) keeps the name
    stable across retries of the same build, so `_find_droplet_by_name` can
    still reuse a droplet left over from a failed previous run instead of
    orphaning it.
    """
    branch_hash = hashlib.sha256(branch_name.encode()).hexdigest()[:BRANCH_HASH_LENGTH]
    droplet_name = f"{kind}-build-{name}-{repo_owner}-{branch_hash}"
    return droplet_name[:MAX_DROPLET_NAME_LENGTH]


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


def create_build_vm(
    kind: str,
    name: str,
    repo_owner: str,
    branch_name: str,
    build_vm_size: str | None = None,
) -> dict:
    """Create an ephemeral DO droplet for building a binary.

    `kind` is the binary name (e.g. `ant`, `ant-node`) and becomes the droplet
    name prefix, so each command that uses a distinct `kind` gets its own VM
    and the two can run in parallel.

    If a droplet with the same name already exists (from a failed previous run),
    it is reused.

    Returns a dict with keys: droplet_id, droplet_name, ip_address, reused.
    """
    size = build_vm_size or BUILD_SIZE
    headers = _get_headers()
    droplet_name = _build_droplet_name(kind, name, repo_owner, branch_name)

    existing = _find_droplet_by_name(droplet_name, headers)
    if existing:
        ip_address = _get_public_ip(existing)
        return {
            "droplet_id": existing["id"],
            "droplet_name": droplet_name,
            "ip_address": ip_address,
            "reused": True,
        }

    errors = []
    for region in BUILD_REGIONS:
        resp = requests.post(
            f"{DO_API_URL}/droplets",
            headers=headers,
            json={
                "name": droplet_name,
                "region": region,
                "size": size,
                "image": BUILD_IMAGE,
                "ssh_keys": SSH_KEY_IDS,
            },
        )
        if resp.status_code == 422:
            error_msg = resp.json().get("message", resp.text)
            print(f"Region {region} unavailable: {error_msg}. Trying next region...")
            errors.append(f"{region}: {error_msg}")
            continue
        resp.raise_for_status()
        droplet_id = resp.json()["droplet"]["id"]

        try:
            droplet = _wait_for_droplet_active(droplet_id, headers)
        except Exception as e:
            print(f"Region {region}: droplet {droplet_id} created but failed to become active: {e}")
            print(f"Destroying failed droplet {droplet_id} and trying next region...")
            errors.append(f"{region}: {e}")
            try:
                requests.delete(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
            except Exception:
                pass
            continue

        ip_address = _get_public_ip(droplet)
        return {
            "droplet_id": droplet_id,
            "droplet_name": droplet_name,
            "ip_address": ip_address,
            "reused": False,
        }

    raise RuntimeError("Failed to create build droplet in any region: " + "; ".join(errors))


def destroy_build_vm(droplet_id: int, wait: bool = True, timeout: int = 120) -> None:
    """Destroy the build droplet, confirming the deletion actually took effect.

    DigitalOcean's delete is asynchronous and returns 204 immediately. Previously
    the response was never checked, so a rejected delete (or one still in flight)
    silently left the droplet listed — where the next run's name lookup would
    reuse it, half-built. We therefore check the response status and, by default,
    poll until the droplet no longer exists, so a leak surfaces as an error the
    caller can warn about rather than passing silently.
    """
    headers = _get_headers()
    resp = requests.delete(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
    # 204 = accepted, 404 = already gone; anything else is a real failure.
    if resp.status_code not in (204, 404):
        resp.raise_for_status()

    if not wait:
        return

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        check = requests.get(f"{DO_API_URL}/droplets/{droplet_id}", headers=headers)
        if check.status_code == 404:
            return
        check.raise_for_status()
        time.sleep(5)
    raise TimeoutError(
        f"Droplet {droplet_id} still exists {timeout}s after delete request"
    )
