"""Tests for the OVH branch of cloud_volumes (added for prod volume extends).

The OVH API calls are mocked at the ``_ovh_call`` seam so these run offline.
"""

from unittest.mock import patch

import pytest

from saorsa_deploy import cloud_volumes
from saorsa_deploy.cloud_volumes import CloudVolume, get_attached_data_volume, resize_volume
from saorsa_deploy.inventory import NodeVm


def _ovh_vm(provider_id="inst-uuid-1"):
    return NodeVm(
        name="ant-prod-01-ovh-BHS5-1",
        provider="ovh",
        provider_id=provider_id,
        region="BHS5",
        ip="51.161.83.207",
        ssh_user="root",
        ssh_key="~/.ssh/ant_deploy",
        services=("ant-node-1",),
    )


@pytest.fixture(autouse=True)
def _ovh_env(monkeypatch):
    monkeypatch.setenv("OVH_CLOUD_PROJECT_SERVICE", "proj-sn")


def test_ovh_get_attached_data_volume_matches_by_instance_uuid():
    volumes = [
        {"id": "vol-other", "size": 50, "attachedTo": ["someone-else"]},
        {"id": "vol-data", "size": 50, "attachedTo": ["inst-uuid-1"]},
        {"id": "vol-unattached", "size": 50, "attachedTo": []},
    ]
    with patch.object(cloud_volumes, "_ovh_call", return_value=volumes):
        vol = get_attached_data_volume(_ovh_vm())
    assert vol == CloudVolume(provider="ovh", id="vol-data", size_gb=50, region="BHS5")


def test_ovh_get_attached_data_volume_requires_exactly_one():
    volumes = [
        {"id": "a", "size": 50, "attachedTo": ["inst-uuid-1"]},
        {"id": "b", "size": 50, "attachedTo": ["inst-uuid-1"]},
    ]
    with patch.object(cloud_volumes, "_ovh_call", return_value=volumes):
        with pytest.raises(RuntimeError, match="found 2"):
            get_attached_data_volume(_ovh_vm())


def test_ovh_resize_polls_until_in_use_at_target():
    vol = CloudVolume(provider="ovh", id="vol-data", size_gb=50, region="BHS5")
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"status": "extending", "size": 50}
        # first GET still extending, second GET done
        gets = [c for c in calls if c[0] == "GET"]
        if len(gets) < 2:
            return {"status": "extending", "size": 50}
        return {"status": "in-use", "size": 100}

    with (
        patch.object(cloud_volumes, "_ovh_call", side_effect=fake_call),
        patch.object(cloud_volumes.time, "sleep", lambda _s: None),
    ):
        out = resize_volume(vol, 100)

    assert out == CloudVolume(provider="ovh", id="vol-data", size_gb=100, region="BHS5")
    assert calls[0][0] == "POST" and calls[0][1].endswith("/vol-data/upsize")
    assert calls[0][2] == {"size": 100}


def test_resize_volume_is_noop_when_already_at_target():
    vol = CloudVolume(provider="ovh", id="vol-data", size_gb=100, region="BHS5")
    # No _ovh_call patch — a no-op must not touch the API at all.
    assert resize_volume(vol, 100) is vol
    assert resize_volume(vol, 80) is vol
