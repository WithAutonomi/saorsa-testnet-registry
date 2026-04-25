"""Provider-agnostic helpers for inspecting and resizing the single data
volume attached to a node VM in a production inventory.

The four supported providers (AWS, DigitalOcean, Hetzner, Vultr) each have
their own block-storage API; this module hides those differences behind
``get_attached_data_volume`` and ``resize_volume``.

Both functions are idempotent: ``resize_volume`` is a no-op if the current
size is already at or above the target, and otherwise polls the relevant
provider until the resize is reflected in the volume's reported size.
"""

import os
import time
from dataclasses import dataclass

import boto3
import requests

from saorsa_deploy.inventory import NodeVm

DO_API_BASE = "https://api.digitalocean.com/v2"
HETZNER_API_BASE = "https://api.hetzner.cloud/v1"
VULTR_API_BASE = "https://api.vultr.com/v2"

REQUEST_TIMEOUT = 30
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600


def _raise_with_body(resp: requests.Response, context: str) -> None:
    """Raise RuntimeError with the response body when an HTTP call failed.

    ``requests.Response.raise_for_status`` only emits the status line, which
    swallows the structured error message most cloud APIs put in the body
    (Hetzner: ``error.code`` / ``error.message``; DO: ``message``; Vultr:
    ``error``). For a tool that's interacting with four different providers
    we want the actual reason surfaced.
    """
    if resp.ok:
        return
    body = resp.text.strip()
    raise RuntimeError(
        f"{context} → HTTP {resp.status_code} {resp.reason}: "
        f"{body or '<empty body>'}"
    )


@dataclass(frozen=True)
class CloudVolume:
    provider: str
    id: str
    size_gb: int
    region: str


# ---------- public API ----------


def get_attached_data_volume(vm: NodeVm) -> CloudVolume:
    """Return the single non-root volume attached to the given VM.

    Raises ``RuntimeError`` if zero or more than one candidate is found —
    these node VMs are expected to have exactly one attached data volume.
    """
    if vm.provider == "aws":
        return _aws_get_data_volume(vm)
    if vm.provider == "digitalocean":
        return _do_get_data_volume(vm)
    if vm.provider == "hetzner":
        return _hetzner_get_data_volume(vm)
    if vm.provider == "vultr":
        return _vultr_get_data_volume(vm)
    raise ValueError(f"Unknown provider: {vm.provider!r}")


def resize_volume(volume: CloudVolume, target_gb: int) -> CloudVolume:
    """Grow ``volume`` to ``target_gb``. Returns the updated CloudVolume.

    No-op (returns the input unchanged) when ``volume.size_gb >= target_gb``.
    Polls the provider until the new size is observable; raises
    ``TimeoutError`` if that takes longer than ``POLL_TIMEOUT_SECONDS``.
    """
    if volume.size_gb >= target_gb:
        return volume

    if volume.provider == "aws":
        return _aws_resize(volume, target_gb)
    if volume.provider == "digitalocean":
        return _do_resize(volume, target_gb)
    if volume.provider == "hetzner":
        return _hetzner_resize(volume, target_gb)
    if volume.provider == "vultr":
        return _vultr_resize(volume, target_gb)
    raise ValueError(f"Unknown provider: {volume.provider!r}")


# ---------- AWS ----------


def _aws_ec2(region: str):
    # boto3 picks credentials up from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
    # in the environment, matching the rest of saorsa-deploy.
    return boto3.client("ec2", region_name=region)


def _aws_get_data_volume(vm: NodeVm) -> CloudVolume:
    ec2 = _aws_ec2(vm.region)

    instance_resp = ec2.describe_instances(InstanceIds=[vm.provider_id])
    reservations = instance_resp.get("Reservations") or []
    if not reservations or not reservations[0].get("Instances"):
        raise RuntimeError(
            f"AWS instance {vm.provider_id} not found in region {vm.region}"
        )
    instance = reservations[0]["Instances"][0]
    root_device = instance.get("RootDeviceName")

    volumes_resp = ec2.describe_volumes(
        Filters=[
            {"Name": "attachment.instance-id", "Values": [vm.provider_id]}
        ]
    )
    candidates = []
    for vol in volumes_resp.get("Volumes", []):
        attachments = vol.get("Attachments", [])
        if not attachments:
            continue
        device = attachments[0].get("Device")
        if device == root_device:
            continue
        candidates.append(vol)

    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one non-root EBS volume on {vm.name} "
            f"({vm.provider_id}); found {len(candidates)}"
        )
    vol = candidates[0]
    return CloudVolume(
        provider="aws",
        id=vol["VolumeId"],
        size_gb=int(vol["Size"]),
        region=vm.region,
    )


def _aws_resize(volume: CloudVolume, target_gb: int) -> CloudVolume:
    ec2 = _aws_ec2(volume.region)
    ec2.modify_volume(VolumeId=volume.id, Size=target_gb)

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        resp = ec2.describe_volumes_modifications(VolumeIds=[volume.id])
        mods = resp.get("VolumesModifications") or []
        if mods:
            mod = mods[0]
            state = mod.get("ModificationState")
            target = mod.get("TargetSize")
            # 'optimizing' means the new size is already visible to the
            # instance (the EBS background re-stripe is decoupled from the
            # filesystem-visible size). 'completed' is the final state.
            if state in ("optimizing", "completed") and target == target_gb:
                return CloudVolume(
                    provider="aws",
                    id=volume.id,
                    size_gb=target_gb,
                    region=volume.region,
                )
            if state == "failed":
                raise RuntimeError(
                    f"AWS volume modification for {volume.id} failed: "
                    f"{mod.get('StatusMessage')}"
                )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Timed out waiting for AWS volume {volume.id} to reach {target_gb} GiB"
    )


# ---------- DigitalOcean ----------


def _do_token() -> str:
    token = os.environ.get("DO_TOKEN")
    if not token:
        raise RuntimeError(
            "DO_TOKEN env var not set — required for DigitalOcean volume operations"
        )
    return token


def _do_headers() -> dict:
    return {
        "Authorization": f"Bearer {_do_token()}",
        "Content-Type": "application/json",
    }


def _do_get_data_volume(vm: NodeVm) -> CloudVolume:
    droplet_resp = requests.get(
        f"{DO_API_BASE}/droplets/{vm.provider_id}",
        headers=_do_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(droplet_resp, f"DO GET droplets/{vm.provider_id}")
    droplet = droplet_resp.json().get("droplet") or {}
    volume_ids = droplet.get("volume_ids") or []
    if len(volume_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one attached volume on DO droplet {vm.provider_id} "
            f"({vm.name}); found {len(volume_ids)}"
        )
    vid = volume_ids[0]
    vol_resp = requests.get(
        f"{DO_API_BASE}/volumes/{vid}",
        headers=_do_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(vol_resp, f"DO GET volumes/{vid}")
    vol = vol_resp.json().get("volume") or {}
    return CloudVolume(
        provider="digitalocean",
        id=vid,
        size_gb=int(vol["size_gigabytes"]),
        region=vm.region,
    )


def _do_resize(volume: CloudVolume, target_gb: int) -> CloudVolume:
    action_resp = requests.post(
        f"{DO_API_BASE}/volumes/{volume.id}/actions",
        headers=_do_headers(),
        json={
            "type": "resize",
            "size_gigabytes": target_gb,
            "region": volume.region,
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(action_resp, f"DO POST volumes/{volume.id}/actions resize")
    action = action_resp.json().get("action") or {}
    action_id = action.get("id")
    if action_id is None:
        raise RuntimeError(f"DigitalOcean did not return an action id for {volume.id}")

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        poll = requests.get(
            f"{DO_API_BASE}/volumes/{volume.id}/actions/{action_id}",
            headers=_do_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        _raise_with_body(poll, f"DO GET volumes/{volume.id}/actions/{action_id}")
        status = (poll.json().get("action") or {}).get("status")
        if status == "completed":
            return CloudVolume(
                provider="digitalocean",
                id=volume.id,
                size_gb=target_gb,
                region=volume.region,
            )
        if status == "errored":
            raise RuntimeError(
                f"DigitalOcean resize action {action_id} for {volume.id} errored"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Timed out waiting for DO volume {volume.id} resize to complete"
    )


# ---------- Hetzner ----------


def _hetzner_token() -> str:
    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        raise RuntimeError(
            "HCLOUD_TOKEN env var not set — required for Hetzner volume operations"
        )
    return token


def _hetzner_headers() -> dict:
    return {
        "Authorization": f"Bearer {_hetzner_token()}",
        "Content-Type": "application/json",
    }


def _hetzner_get_data_volume(vm: NodeVm) -> CloudVolume:
    server_resp = requests.get(
        f"{HETZNER_API_BASE}/servers/{vm.provider_id}",
        headers=_hetzner_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(server_resp, f"Hetzner GET servers/{vm.provider_id}")
    server = server_resp.json().get("server") or {}
    volume_ids = server.get("volumes") or []
    if len(volume_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one attached volume on Hetzner server {vm.provider_id} "
            f"({vm.name}); found {len(volume_ids)}"
        )
    vid = volume_ids[0]
    vol_resp = requests.get(
        f"{HETZNER_API_BASE}/volumes/{vid}",
        headers=_hetzner_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(vol_resp, f"Hetzner GET volumes/{vid}")
    vol = vol_resp.json().get("volume") or {}
    return CloudVolume(
        provider="hetzner",
        id=str(vid),
        size_gb=int(vol["size"]),
        region=vm.region,
    )


def _hetzner_resize(volume: CloudVolume, target_gb: int) -> CloudVolume:
    action_resp = requests.post(
        f"{HETZNER_API_BASE}/volumes/{volume.id}/actions/resize",
        headers=_hetzner_headers(),
        json={"size": target_gb},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(
        action_resp,
        f"Hetzner POST volumes/{volume.id}/actions/resize size={target_gb}",
    )
    action = action_resp.json().get("action") or {}
    action_id = action.get("id")
    if action_id is None:
        raise RuntimeError(f"Hetzner did not return an action id for {volume.id}")

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        poll = requests.get(
            f"{HETZNER_API_BASE}/volumes/{volume.id}/actions/{action_id}",
            headers=_hetzner_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        _raise_with_body(
            poll,
            f"Hetzner GET volumes/{volume.id}/actions/{action_id}",
        )
        status = (poll.json().get("action") or {}).get("status")
        if status == "success":
            return CloudVolume(
                provider="hetzner",
                id=volume.id,
                size_gb=target_gb,
                region=volume.region,
            )
        if status == "error":
            raise RuntimeError(
                f"Hetzner resize action {action_id} for volume {volume.id} errored"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Timed out waiting for Hetzner volume {volume.id} resize to complete"
    )


# ---------- Vultr ----------


def _vultr_token() -> str:
    token = os.environ.get("VULTR_API_KEY")
    if not token:
        raise RuntimeError(
            "VULTR_API_KEY env var not set — required for Vultr volume operations"
        )
    return token


def _vultr_headers() -> dict:
    return {
        "Authorization": f"Bearer {_vultr_token()}",
        "Content-Type": "application/json",
    }


def _vultr_list_blocks() -> list[dict]:
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"per_page": 500}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{VULTR_API_BASE}/blocks",
            headers=_vultr_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        _raise_with_body(resp, "Vultr GET blocks")
        body = resp.json()
        blocks.extend(body.get("blocks") or [])
        meta = body.get("meta") or {}
        links = meta.get("links") or {}
        cursor = links.get("next")
        if not cursor:
            return blocks


def _vultr_get_data_volume(vm: NodeVm) -> CloudVolume:
    candidates = [
        b for b in _vultr_list_blocks()
        if b.get("attached_to_instance") == vm.provider_id
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one attached block storage on Vultr instance "
            f"{vm.provider_id} ({vm.name}); found {len(candidates)}"
        )
    block = candidates[0]
    return CloudVolume(
        provider="vultr",
        id=block["id"],
        size_gb=int(block["size_gb"]),
        region=vm.region,
    )


def _vultr_resize(volume: CloudVolume, target_gb: int) -> CloudVolume:
    resp = requests.patch(
        f"{VULTR_API_BASE}/blocks/{volume.id}",
        headers=_vultr_headers(),
        json={"size_gb": target_gb},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_with_body(resp, f"Vultr PATCH blocks/{volume.id} size_gb={target_gb}")

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        poll = requests.get(
            f"{VULTR_API_BASE}/blocks/{volume.id}",
            headers=_vultr_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        _raise_with_body(poll, f"Vultr GET blocks/{volume.id}")
        block = poll.json().get("block") or {}
        if int(block.get("size_gb", 0)) >= target_gb and block.get("status") == "active":
            return CloudVolume(
                provider="vultr",
                id=volume.id,
                size_gb=int(block["size_gb"]),
                region=volume.region,
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Timed out waiting for Vultr block {volume.id} resize to complete"
    )
