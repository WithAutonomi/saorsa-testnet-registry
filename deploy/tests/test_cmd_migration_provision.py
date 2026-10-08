"""Tests for `migration provision` — focused on the hostname-normalisation step.

Migrated VMs inherit the cloud provider's default (often lowercased) hostname;
`migration provision` must set each VM's hostname to its fleet-convention
``target_name`` so host-keyed telemetry (the InfluxDB host tag) matches the rest
of the fleet. Heavy dependencies (the DB repo, bootstrap fetch, binary resolve,
and the pyinfra provisioner) are mocked; we assert `set_hostnames` is called with
the right ip→name mapping.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from saorsa_db.models import MigrationVmStatus

from saorsa_deploy.cmd import migration_provision as mod


def _vm(vm_id, ip, name):
    return SimpleNamespace(
        id=vm_id,
        target_ip=ip,
        target_name=name,
        status=MigrationVmStatus.INFRA_PROVISIONED,
    )


def _args():
    return SimpleNamespace(
        name="mig",
        ssh_key_path="~/.ssh/ant_deploy",
        rewards_address="0xabc",
        bootstrap_peers_file="/tmp/bp.toml",
        port=10000,
        log_level=None,
        node_version=None,
        branch_name=None,
        repo_owner=None,
    )


def _run_provision(migration, vms):
    repo = MagicMock()
    repo.get_migration_by_name.return_value = migration
    repo.list_vms.return_value = vms
    with (
        patch.object(mod, "get_migration_repository", return_value=repo),
        patch.object(mod, "load_prod_bootstraps_from_file", return_value=["1.2.3.4:10000"]),
        patch.object(mod, "_resolve_binary_source", return_value=(None, True)),
        patch.object(mod, "_wait_for_ssh_ready", return_value=[]),
        patch.object(mod, "clear_known_hosts"),
        patch.object(mod, "AntNodeProvisioner") as prov,
        patch.object(mod, "set_hostnames"),
    ):
        prov.return_value.execute.return_value = None
        mod.cmd_migration_provision(_args())
    return prov


def test_provision_contabo_uses_root_disk_ips():
    vms = [
        _vm(1, "10.0.0.1", "ant-prod-01-cntb-US-central-1"),
        _vm(2, "10.0.0.2", "ant-prod-01-cntb-US-central-2"),
    ]
    migration = SimpleNamespace(id=9, name="mig", node_count_per_vm=15, target_provider="contabo")
    prov = _run_provision(migration, vms)
    # Every Contabo target VM is a root-disk host (no attached volume).
    assert prov.call_args.kwargs["root_disk_ips"] == {"10.0.0.1", "10.0.0.2"}


def test_provision_volume_provider_has_no_root_disk_ips():
    vms = [_vm(1, "10.0.0.1", "ant-prod-01-hz-ash-1")]
    migration = SimpleNamespace(id=9, name="mig", node_count_per_vm=15, target_provider="hetzner")
    prov = _run_provision(migration, vms)
    assert prov.call_args.kwargs["root_disk_ips"] is None


def test_provision_sets_fleet_convention_hostnames():
    vms = [
        _vm(1, "51.255.147.100", "ant-prod-01-ovh-SBG5-1"),
        _vm(2, "51.255.147.77", "ant-prod-01-ovh-SBG5-2"),
        _vm(3, "148.113.231.246", "ant-prod-01-ovh-BHS5-10"),
    ]
    migration = SimpleNamespace(id=7, name="mig", node_count_per_vm=15, target_provider="ovh")

    repo = MagicMock()
    repo.get_migration_by_name.return_value = migration
    repo.list_vms.return_value = vms

    with (
        patch.object(mod, "get_migration_repository", return_value=repo),
        patch.object(mod, "load_prod_bootstraps_from_file", return_value=["1.2.3.4:10000"]),
        patch.object(mod, "_resolve_binary_source", return_value=(None, True)),
        patch.object(mod, "_wait_for_ssh_ready", return_value=[]),
        patch.object(mod, "clear_known_hosts"),
        patch.object(mod, "AntNodeProvisioner") as prov,
        patch.object(mod, "set_hostnames") as set_hn,
    ):
        prov.return_value.execute.return_value = None
        mod.cmd_migration_provision(_args())

    # set_hostnames was called once, before the provisioner ran, with one entry
    # per VM mapping its target_ip to its (uppercase) target_name.
    set_hn.assert_called_once()
    entries = set_hn.call_args.args[0]
    assert [(e["ip"], e["name"]) for e in entries] == [
        ("51.255.147.100", "ant-prod-01-ovh-SBG5-1"),
        ("51.255.147.77", "ant-prod-01-ovh-SBG5-2"),
        ("148.113.231.246", "ant-prod-01-ovh-BHS5-10"),
    ]
    assert all(e["ssh_user"] == "root" for e in entries)
    assert all(e["ssh_key"] == "~/.ssh/ant_deploy" for e in entries)
    # And the node provisioner still ran afterwards.
    prov.return_value.execute.assert_called_once()
