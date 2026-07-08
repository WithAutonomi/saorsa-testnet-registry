"""Tests for the DigitalOcean branch of `migration destroy-source`.

The DO API is mocked end-to-end (via the module's `requests`); these verify
that destroy-source deletes the droplet AND its attached volume, falls back to
name-based volume lookup when the droplet is already gone, and refuses to mark
a VM destroyed if a volume can't be removed (no silent orphans).
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.cmd import migration_destroy_source as mod


class FakeResp:
    def __init__(self, status_code, json_data=None, reason="", text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.reason = reason
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._json


class FakeRepo:
    """Minimal in-memory migration repository for the command under test."""

    def __init__(self, migration, vms):
        self.migration = migration
        self.vms = vms
        self.status_updates = []

    def get_migration_by_name(self, name):
        return self.migration if name == self.migration.name else None

    def list_vms(self, migration_id, status=None):
        return [v for v in self.vms if status is None or v.status == status]

    def update_vm(self, vm_id, status=None, source_destroyed_at=None, **_kw):
        for v in self.vms:
            if v.id == vm_id and status is not None:
                v.status = status

    def update_migration_status(self, migration_id, status):
        self.status_updates.append(status)
        self.migration.status = status


def _vm(vm_id, name, *, provider="digitalocean", pid="100", region="ams3"):
    return SimpleNamespace(
        id=vm_id,
        source_name=name,
        source_provider=provider,
        source_provider_id=pid,
        source_region=region,
        status=MigrationVmStatus.SOURCE_DRAINED,
    )


def _args():
    return SimpleNamespace(name="mig", force=False)


def _run(repo, get_fn, delete_fn, post_fn=None):
    if post_fn is None:
        def post_fn(*_a, **_k):
            return FakeResp(204)
    with (
        patch.object(mod, "get_migration_repository", return_value=repo),
        patch.dict(
            os.environ,
            {"DO_TOKEN": "test-token", "VULTR_API_KEY": "test-vultr"},
        ),
        patch.object(mod.requests, "get", side_effect=get_fn),
        patch.object(mod.requests, "delete", side_effect=delete_fn),
        patch.object(mod.requests, "post", side_effect=post_fn),
        patch.object(mod.time, "sleep"),
    ):
        mod.cmd_migration_destroy_source(_args())


def _migration():
    return SimpleNamespace(
        id=1, name="mig", status=MigrationVmStatus.SOURCE_DRAINED
    )


def test_digitalocean_destroys_droplet_and_volume():
    vm = _vm(10, "ant-prod-01-do-ams3-node-1", pid="564994701")
    repo = FakeRepo(_migration(), [vm])

    deleted = []

    def get(url, **_):
        assert "/droplets/564994701" in url
        return FakeResp(200, {"droplet": {"volume_ids": ["vol-1"]}})

    def delete(url, **_):
        deleted.append(url)
        return FakeResp(204)

    _run(repo, get, delete)

    assert vm.status == MigrationVmStatus.SOURCE_DESTROYED
    # Both the droplet and its volume were deleted.
    assert any("/droplets/564994701" in u for u in deleted)
    assert any("/volumes/vol-1" in u for u in deleted)
    # Status went DESTROYING then COMPLETE (all VMs destroyed).
    assert repo.status_updates == [
        MigrationStatus.DESTROYING,
        MigrationStatus.COMPLETE,
    ]


def test_falls_back_to_name_lookup_when_droplet_already_gone():
    vm = _vm(11, "ant-prod-01-do-lon1-node-2", pid="999", region="lon1")
    repo = FakeRepo(_migration(), [vm])
    deleted = []

    def get(url, params=None, **_):
        if "/droplets/999" in url:
            return FakeResp(404, reason="Not Found")
        # name-based volume lookup
        assert "/volumes" in url
        assert params == {"name": "ant-prod-01-do-lon1-node-2-data", "region": "lon1"}
        return FakeResp(200, {"volumes": [{"id": "vol-orphan"}]})

    def delete(url, **_):
        deleted.append(url)
        return FakeResp(204)

    _run(repo, get, delete)

    assert vm.status == MigrationVmStatus.SOURCE_DESTROYED
    assert any("/volumes/vol-orphan" in u for u in deleted)


def test_volume_delete_failure_keeps_vm_undestroyed():
    vm = _vm(12, "ant-prod-01-do-fra1-node-1", pid="555", region="fra1")
    repo = FakeRepo(_migration(), [vm])

    def get(url, **_):
        return FakeResp(200, {"droplet": {"volume_ids": ["vol-stuck"]}})

    def delete(url, **_):
        if "/droplets/" in url:
            return FakeResp(204)
        # Volume delete keeps failing (e.g. still-attached) -> retries exhaust.
        return FakeResp(422, reason="Unprocessable", text="volume still attached")

    with pytest.raises(SystemExit):
        _run(repo, get, delete)

    # Droplet gone, but the volume couldn't be removed -> NOT marked destroyed
    # so the migration never reports complete and the orphan is surfaced.
    assert vm.status == MigrationVmStatus.SOURCE_DRAINED
    assert MigrationStatus.COMPLETE not in repo.status_updates


def test_unsupported_source_provider_rejected():
    vm = _vm(13, "node-x", provider="linode")
    repo = FakeRepo(_migration(), [vm])

    def get(url, **_):
        raise AssertionError("should not hit the API for an unsupported provider")

    def delete(url, **_):
        raise AssertionError("should not delete anything")

    with pytest.raises(SystemExit):
        _run(repo, get, delete)

    assert vm.status == MigrationVmStatus.SOURCE_DRAINED
    # Rejected before any DESTROYING transition.
    assert MigrationStatus.DESTROYING not in repo.status_updates


def test_vultr_destroys_instance_and_block():
    vm = _vm(20, "ant-prod-01-vl-lax-1", provider="vultr", pid="inst-1", region="lax")
    repo = FakeRepo(_migration(), [vm])

    deleted = []

    def get(url, **_):
        assert "/blocks" in url
        return FakeResp(
            200,
            {
                "blocks": [
                    {
                        "id": "blk-1",
                        "attached_to_instance": "inst-1",
                        "label": "ant-prod-01-vl-lax-1-data",
                    }
                ]
            },
        )

    def delete(url, **_):
        deleted.append(url)
        return FakeResp(204)

    _run(repo, get, delete)

    assert vm.status == MigrationVmStatus.SOURCE_DESTROYED
    # Both the instance and its block were deleted.
    assert any("/instances/inst-1" in u for u in deleted)
    assert any("/blocks/blk-1" in u for u in deleted)
    assert repo.status_updates == [
        MigrationStatus.DESTROYING,
        MigrationStatus.COMPLETE,
    ]


def test_vultr_falls_back_to_label_when_instance_gone():
    vm = _vm(21, "ant-prod-01-vl-lax-2", provider="vultr", pid="gone", region="lax")
    repo = FakeRepo(_migration(), [vm])
    deleted = []

    def get(url, **_):
        # Instance already gone -> block is detached; find it by label.
        return FakeResp(
            200,
            {
                "blocks": [
                    {
                        "id": "blk-orphan",
                        "attached_to_instance": "",
                        "label": "ant-prod-01-vl-lax-2-data",
                    }
                ]
            },
        )

    def delete(url, **_):
        deleted.append(url)
        return FakeResp(404 if "/instances/gone" in url else 204)

    _run(repo, get, delete)

    assert vm.status == MigrationVmStatus.SOURCE_DESTROYED
    assert any("/blocks/blk-orphan" in u for u in deleted)


def test_vultr_block_delete_failure_keeps_vm_undestroyed():
    vm = _vm(22, "ant-prod-01-vl-lax-3", provider="vultr", pid="inst-3", region="lax")
    repo = FakeRepo(_migration(), [vm])

    def get(url, **_):
        return FakeResp(
            200,
            {"blocks": [{"id": "blk-stuck", "attached_to_instance": "inst-3"}]},
        )

    def delete(url, **_):
        if "/instances/" in url:
            return FakeResp(204)
        # Block delete keeps failing (e.g. still-attached) -> retries exhaust.
        return FakeResp(400, reason="Bad Request", text="block must be detached")

    with pytest.raises(SystemExit):
        _run(repo, get, delete)

    # Instance gone, but the block couldn't be removed -> NOT marked destroyed
    # so the migration never reports complete and the orphan is surfaced.
    assert vm.status == MigrationVmStatus.SOURCE_DRAINED
    assert MigrationStatus.COMPLETE not in repo.status_updates
