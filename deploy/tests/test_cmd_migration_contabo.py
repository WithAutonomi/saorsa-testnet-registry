"""Tests for the Contabo target path of the migration subsystem.

Contabo is provisioned via the REST API (not Terraform) and is root-disk only,
so it takes a bespoke branch in `migration infra` / `destroy-infra` and a
root-disk mode in `migration provision`. Heavy deps (the DB repo and the Contabo
REST client) are mocked; we assert the records written and the API calls made.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from saorsa_db.models import MigrationStatus, MigrationVmStatus

from saorsa_deploy.cmd import migration_destroy_infra as dmod
from saorsa_deploy.cmd import migration_infra as mod
from saorsa_deploy.cmd import migration_plan as pmod


def _planned_vm(vm_id):
    return SimpleNamespace(id=vm_id, status=MigrationVmStatus.PLANNED)


def _infra_args(**over):
    base = dict(
        name="mig-ord",
        locations="US-central=3",
        inventory_path="/nonexistent/inventory.json",
        contabo_image_id="img-uuid",
        contabo_ssh_public_key_path="~/.ssh/ant_deploy.pub",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _contabo_migration(**over):
    base = dict(
        id=42,
        name="mig-ord",
        deployment_name="ant-prod-01",
        target_provider="contabo",
        target_server_type="V99",
        target_volume_size_gb=0,
        node_count_per_vm=15,
        target_locations=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# migration infra — Contabo REST branch
# --------------------------------------------------------------------------- #
def test_infra_contabo_creates_instances_and_records_vms():
    migration = _contabo_migration()
    planned = [_planned_vm(1), _planned_vm(2), _planned_vm(3)]

    repo = MagicMock()
    repo.get_migration_by_name.return_value = migration
    repo.list_vms.return_value = planned

    client = MagicMock()
    client.ensure_ssh_secret.return_value = 555

    def _adopt(_client, _console, *, display_name, product_id, image_id, region, ssh_secret_id):
        idx = int(display_name.rsplit("-", 1)[1])
        return (1000 + idx, f"10.0.0.{idx}")

    with (
        patch.object(mod, "get_migration_repository", return_value=repo),
        patch("saorsa_deploy.contabo.require_contabo_env"),
        patch("saorsa_deploy.contabo_api.ContaboClient", return_value=client),
        patch("saorsa_deploy.cmd.contabo_infra._create_or_adopt", side_effect=_adopt),
        patch.object(mod, "_wait_for_ssh_ready", return_value=[]) as wait,
    ):
        mod.cmd_migration_infra(_infra_args())

    # SSH-readiness was awaited on the created target IPs before completing.
    wait.assert_called_once()
    assert set(wait.call_args.args[0]) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}

    # Target locations recorded, and one create per VM with the right product/region.
    repo.set_target_locations.assert_called_once_with(42, {"US-central": 3})
    client.ensure_ssh_secret.assert_called_once_with(
        "migration-mig-ord-us-central", "~/.ssh/ant_deploy.pub"
    )

    # Each planned VM updated to infra_provisioned with contabo target + no volume.
    assert repo.update_vm.call_count == 3
    first = repo.update_vm.call_args_list[0]
    assert first.args[0] == 1
    assert first.kwargs["target_name"] == "ant-prod-01-cntb-US-central-1"
    assert first.kwargs["target_provider"] == "contabo"
    assert first.kwargs["target_provider_id"] == "1001"
    assert first.kwargs["target_ip"] == "10.0.0.1"
    assert first.kwargs["target_volume_id"] is None
    assert first.kwargs["status"] == MigrationVmStatus.INFRA_PROVISIONED

    repo.update_migration_status.assert_called_with(42, MigrationStatus.INFRA_PROVISIONED)


def test_infra_contabo_locations_count_mismatch_exits():
    migration = _contabo_migration()
    repo = MagicMock()
    repo.get_migration_by_name.return_value = migration
    repo.list_vms.return_value = [_planned_vm(1), _planned_vm(2)]  # only 2 planned

    with (
        patch.object(mod, "get_migration_repository", return_value=repo),
        pytest.raises(SystemExit),
    ):
        mod.cmd_migration_infra(_infra_args(locations="US-central=3"))
    repo.set_target_locations.assert_not_called()


# --------------------------------------------------------------------------- #
# _wait_for_ssh_ready — first-boot SSH settle
# --------------------------------------------------------------------------- #
def test_wait_for_ssh_ready_all_ready_returns_empty():
    with (
        patch.object(mod.socket, "create_connection"),  # always succeeds
        patch.object(mod.time, "sleep"),
    ):
        not_ready = mod._wait_for_ssh_ready(
            ["1.1.1.1", "2.2.2.2"],
            MagicMock(),
            required_consecutive=2,
            interval=0,
            timeout=60,
        )
    assert not_ready == []


def test_wait_for_ssh_ready_reports_unready_host():
    def _connect(addr, timeout=None):
        if addr[0] == "2.2.2.2":
            raise OSError("connection refused")
        return MagicMock()  # usable as a context manager

    # monotonic climbs past the (0 + timeout) deadline after a few iterations.
    clock = iter([0, 5, 10, 15, 20, 999, 999, 999])
    with (
        patch.object(mod.socket, "create_connection", side_effect=_connect),
        patch.object(mod.time, "sleep"),
        patch.object(mod.time, "monotonic", side_effect=lambda: next(clock, 999)),
    ):
        not_ready = mod._wait_for_ssh_ready(
            ["1.1.1.1", "2.2.2.2"],
            MagicMock(),
            required_consecutive=1,
            interval=0,
            timeout=30,
        )
    assert not_ready == ["2.2.2.2"]


def test_wait_for_ssh_ready_empty_noop():
    # No IPs → returns immediately without touching the network.
    with patch.object(mod.socket, "create_connection") as cc:
        assert mod._wait_for_ssh_ready([], MagicMock()) == []
    cc.assert_not_called()


# --------------------------------------------------------------------------- #
# migration destroy-infra — Contabo cancel branch
# --------------------------------------------------------------------------- #
def _target_vm(vm_id, iid, loc, name):
    return SimpleNamespace(
        id=vm_id,
        status=MigrationVmStatus.INFRA_PROVISIONED,
        target_provider="contabo",
        target_provider_id=str(iid),
        target_location=loc,
        target_name=name,
    )


def test_destroy_infra_contabo_cancels_and_resets():
    migration = _contabo_migration()
    vms = [
        _target_vm(1, 1001, "US-central", "ant-prod-01-cntb-US-central-1"),
        _target_vm(2, 1002, "US-central", "ant-prod-01-cntb-US-central-2"),
    ]
    repo = MagicMock()
    repo.get_migration_by_name.return_value = migration
    # Honor the status filter: the force-check queries forbidden states (none
    # of our VMs match), while the destroy branch lists all VMs (status=None).
    repo.list_vms.side_effect = lambda _id, status=None: vms if status is None else []

    client = MagicMock()
    client.find_secret.return_value = 555

    args = SimpleNamespace(name="mig-ord", locations=None, inventory_path=None, force=False)

    with (
        patch.object(dmod, "get_migration_repository", return_value=repo),
        patch("saorsa_deploy.contabo_api.ContaboClient", return_value=client),
    ):
        dmod.cmd_migration_destroy_infra(args)

    assert [c.args[0] for c in client.cancel_instance.call_args_list] == [1001, 1002]
    client.find_secret.assert_called_once_with("migration-mig-ord-us-central")
    client.delete_secret.assert_called_once_with(555)
    assert repo.update_vm.call_count == 2
    assert repo.update_vm.call_args_list[0].kwargs["status"] == MigrationVmStatus.PLANNED
    repo.update_migration_status.assert_called_with(42, MigrationStatus.PLANNING)


# --------------------------------------------------------------------------- #
# migration plan — volume-size validation
# --------------------------------------------------------------------------- #
def _plan_args(**over):
    base = dict(
        name="mig-ord",
        deployment_name="ant-prod-01",
        inventory_path=None,
        source_provider="vultr",
        source_region="ord",
        target_provider="contabo",
        target_server_type="V99",
        target_volume_size=0,
        node_count_per_vm=15,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_plan_contabo_allows_zero_volume():
    repo = MagicMock()
    repo.get_migration_by_name.return_value = None
    repo.create_migration.return_value = SimpleNamespace(id=1)
    inv_vm = SimpleNamespace(
        name="ant-prod-01-vl-ord-1",
        provider="vultr",
        provider_id="v1",
        region="ord",
        ip="1.1.1.1",
        ssh_user="root",
        ssh_key="~/.ssh/ant_deploy",
        services=[1, 2, 3],
    )
    with (
        patch.object(pmod, "get_migration_repository", return_value=repo),
        patch.object(pmod, "load_node_inventory", return_value=[inv_vm]),
    ):
        pmod.cmd_migration_plan(_plan_args(target_provider="contabo", target_volume_size=0))
    repo.create_migration.assert_called_once()


def test_plan_hetzner_requires_positive_volume():
    repo = MagicMock()
    repo.get_migration_by_name.return_value = None
    with (
        patch.object(pmod, "get_migration_repository", return_value=repo),
        pytest.raises(SystemExit),
    ):
        pmod.cmd_migration_plan(_plan_args(target_provider="hetzner", target_volume_size=0))
    repo.create_migration.assert_not_called()
