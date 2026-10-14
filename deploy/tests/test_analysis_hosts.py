"""Inventory resolution and SSH plumbing shared by every `analysis` command.

The property that matters: a command never guesses a host's NAT tier or key.
Both come from the inventory, and an IP outside the inventory is refused rather
than treated as an ad-hoc host — that is how a probe or a restart would land on
the wrong fleet.
"""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis import hosts, remote


def _write_inventory(tmp_path):
    inv = {
        "schema_version": 6,
        "node_vms": [
            {
                "name": "DEV-01-ant-node-DE1-1",
                "provider": "ovh",
                "region": "DE1",
                "ip": "10.0.0.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": False,
                "services": ["ant-node-1", "ant-node-2"],
            },
            {
                "name": "DEV-01-ant-node-ord-1",
                "provider": "vultr",
                "region": "ord",
                "ip": "10.0.0.2",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": True,
                "services": ["ant-node-1"],
            },
        ],
        "bootstrap_vms": [
            {
                "name": "DEV-01-ant-bootstrap-1",
                "ip": "10.0.1.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "services": ["ant-bootstrap-1"],
            },
        ],
        "client_vms": [
            {
                "name": "DEV-01-ant-client-upload-1",
                "ip": "10.0.2.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "services": ["ant-uploader"],
            },
        ],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    return path


def test_missing_inventory_names_the_generate_command(tmp_path):
    with pytest.raises(FileNotFoundError, match="generate-inventory --name DEV-09"):
        hosts.load_vms("DEV-09", str(tmp_path / "nope.json"))


def test_tiers_come_from_nat_simulated(tmp_path):
    inv = _write_inventory(tmp_path)
    assert [v.ip for v in hosts.node_vms("DEV-01", str(inv), "nat")] == ["10.0.0.2"]
    assert [v.ip for v in hosts.node_vms("DEV-01", str(inv), "plain")] == ["10.0.0.1"]
    assert len(hosts.node_vms("DEV-01", str(inv))) == 2
    with pytest.raises(ValueError):
        hosts.node_vms("DEV-01", str(inv), "bogus")


def test_select_vms_preserves_order_and_refuses_unknown_ips(tmp_path):
    inv = _write_inventory(tmp_path)
    picked = hosts.select_vms("DEV-01", ["10.0.0.2", "10.0.0.1"], str(inv))
    assert [v.ip for v in picked] == ["10.0.0.2", "10.0.0.1"]
    assert picked[0].nat and not picked[1].nat
    with pytest.raises(KeyError, match="10.0.9.9"):
        hosts.select_vms("DEV-01", ["10.0.9.9"], str(inv))
    # Bootstraps and clients are not node VMs and are refused by default.
    with pytest.raises(KeyError):
        hosts.select_vms("DEV-01", ["10.0.1.1"], str(inv))
    boot = hosts.select_vms("DEV-01", ["10.0.1.1"], str(inv), roles=("bootstrap",))
    assert boot[0].role == "bootstrap"


def test_select_vms_without_ips_uses_the_tier(tmp_path):
    inv = _write_inventory(tmp_path)
    assert [v.ip for v in hosts.select_vms("DEV-01", None, str(inv), tier="nat")] == ["10.0.0.2"]
    assert len(hosts.select_vms("DEV-01", [], str(inv))) == 2


def test_the_production_inventory_shape_loads(tmp_path):
    """`ant-prod-01`'s inventory is schema 3 and carries no `nat_simulated` key.

    V2-1308 needs `analysis snapshot` to run against production for the V2-834
    reconcile. The prod hosts run their node services in the host namespace, so
    every VM must come back `nat=False` — one wrongly flagged NAT'd would make the
    snapshot read `ant_nat` counters that do not exist there, and the reconcile
    would then divide the ES numerators by zeros.
    """
    inv = tmp_path / "prod.json"
    inv.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "testnet_id": "ant-prod-01",
                "node_vms": [
                    {
                        "name": "ant-prod-01-hz-fsn1-1",
                        "provider": "hetzner",
                        "provider_id": "127000688",
                        "region": "fsn1",
                        "ip": "10.1.0.1",
                        "ssh_user": "root",
                        "ssh_key": "~/.ssh/ant_deploy",
                        "nodes": [{"service_name": "ant-node-2"}],
                        "services": ["ant-node-2", "ant-node-3"],
                    },
                    {
                        "name": "ant-prod-01-ovh-waw1-7",
                        "provider": "ovh",
                        "region": "waw1",
                        "ip": "10.1.0.2",
                        "ssh_user": "root",
                        "ssh_key": "~/.ssh/ant_deploy",
                        "services": ["ant-node-1"],
                    },
                ],
            }
        )
    )
    vms = hosts.node_vms("ant-prod-01", str(inv))
    assert [v.ip for v in vms] == ["10.1.0.1", "10.1.0.2"]
    assert not any(v.nat for v in vms)
    assert vms[0].services == ("ant-node-2", "ant-node-3")
    assert {v.provider for v in vms} == {"hetzner", "ovh"}
    # `analysis snapshot` selects the node and bootstrap roles; a prod inventory
    # has no `bootstrap_vms` key at all, which must not be an error.
    assert len(hosts.select_vms("ant-prod-01", None, str(inv), roles=("node", "bootstrap"))) == 2


def test_ssh_args_expand_the_key_and_pin_batch_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    vm = hosts.select_vms("DEV-01", ["10.0.0.1"], str(_write_inventory(tmp_path)))[0]
    args = vm.ssh_args()
    assert args[:3] == ["ssh", "-i", str(tmp_path / ".ssh" / "ant_deploy")]
    assert "BatchMode=yes" in args
    assert vm.target == "root@10.0.0.1"


def test_run_script_passes_payload_on_stdin_and_quotes_args(tmp_path):
    vm = hosts.select_vms("DEV-01", ["10.0.0.1"], str(_write_inventory(tmp_path)))[0]
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout="STATUS=x\nSTATUS=started NS=1\n", stderr="")

    with patch.object(remote.subprocess, "run", fake_run):
        res = remote.run_script(vm, "echo hi", ["600", "p2 tag", "unit"])
    assert calls["cmd"][-1] == "bash -s -- 600 'p2 tag' unit"
    assert calls["cmd"][-2] == "root@10.0.0.1"
    assert calls["input"] == "echo hi"
    assert res.ok and res.last_line == "STATUS=started NS=1"


def test_run_script_reports_timeout_as_result_not_exception(tmp_path):
    vm = hosts.select_vms("DEV-01", ["10.0.0.1"], str(_write_inventory(tmp_path)))[0]

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    with patch.object(remote.subprocess, "run", fake_run):
        res = remote.run_script(vm, "sleep 999", timeout=1)
    assert not res.ok and res.returncode == 124 and "timed out" in res.stderr


def test_run_parallel_keeps_input_order(tmp_path):
    vms = hosts.select_vms("DEV-01", ["10.0.0.2", "10.0.0.1"], str(_write_inventory(tmp_path)))
    assert remote.run_parallel(vms, lambda v: v.ip) == ["10.0.0.2", "10.0.0.1"]
    assert remote.run_parallel([], lambda v: v.ip) == []


class TestDefaultPathFreshness:
    """A default-path snapshot is checked against the deployment's S3 state.

    V2-1530 found `~/.autonomi/testnets/DEV-02/inventory.json` on disk ten days
    after that DEV-02 was destroyed and ran `df` against its 14 addresses, all
    of which the provider had reassigned. Three commands reading this inventory
    (`restart-nodes`, `isolate`, `rot-chunks`) mutate the hosts they are given,
    so the stale set must be refused, not probed.
    """

    @staticmethod
    def _default(local_inventory_root, name, inv):
        path = local_inventory_root / name / "inventory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(inv))
        return path

    @staticmethod
    def _inventory(name="DEV-02", ips=("10.0.0.1",), bootstrap_ips=("10.0.1.1",)):
        return {
            "schema_version": 7,
            "testnet_id": name,
            "created_at": "2026-10-03T21:13:45Z",
            "node_vms": [
                {
                    "name": f"{name}-ant-node-lon1-{i + 1}",
                    "ip": ip,
                    "services": ["ant-node-1"],
                }
                for i, ip in enumerate(ips)
            ],
            "bootstrap_vms": [
                {"name": f"{name}-ant-bootstrap-{i + 1}", "ip": ip, "services": ["ant-bootstrap-1"]}
                for i, ip in enumerate(bootstrap_ips)
            ],
        }

    @staticmethod
    def _no_state(name):
        raise RuntimeError(f"No deployment state found for '{name}'.")

    def test_loads_when_every_ip_is_in_the_state(self, local_inventory_root, monkeypatch):
        self._default(local_inventory_root, "DEV-02", self._inventory())
        monkeypatch.setattr(
            hosts,
            "_load_deployment_state",
            lambda name: {
                "vm_ips": {"digitalocean/lon1": ["10.0.0.1"]},
                "bootstrap_nodes": [{"ip": "10.0.1.1", "region": "lon1", "bootstrap_index": 1}],
            },
        )

        assert [v.ip for v in hosts.load_vms("DEV-02")] == ["10.0.0.1", "10.0.1.1"]

    def test_refuses_the_v2_1530_shape(self, local_inventory_root, monkeypatch):
        """The regression: testnet_id DEV-02, created 2026-10-03, 14 node VMs,
        no S3 state."""
        ips = tuple(f"10.0.0.{i + 1}" for i in range(14))
        path = self._default(
            local_inventory_root, "DEV-02", self._inventory(ips=ips, bootstrap_ips=())
        )
        monkeypatch.setattr(hosts, "_load_deployment_state", self._no_state)

        with pytest.raises(hosts.StaleInventoryError) as excinfo:
            hosts.load_vms("DEV-02")

        message = str(excinfo.value)
        assert str(path) in message
        assert "2026-10-03" in message
        assert "no S3 state for 'DEV-02'" in message
        assert "generate-inventory --name DEV-02" in message
        assert "--inventory" in message

    def test_refuses_an_ip_the_state_does_not_know(self, local_inventory_root, monkeypatch):
        """A rebuilt fleet reuses the name, so the state exists but the old
        addresses in the snapshot are someone else's now."""
        self._default(local_inventory_root, "DEV-02", self._inventory(ips=("10.0.0.1", "10.0.0.9")))
        monkeypatch.setattr(
            hosts,
            "_load_deployment_state",
            lambda name: {
                "vm_ips": {"digitalocean/lon1": ["10.0.0.1"]},
                "bootstrap_nodes": [{"ip": "10.0.1.1"}],
            },
        )

        with pytest.raises(hosts.StaleInventoryError, match="10.0.0.9"):
            hosts.load_vms("DEV-02")

    def test_refuses_a_bootstrap_ip_the_state_does_not_know(
        self, local_inventory_root, monkeypatch
    ):
        self._default(local_inventory_root, "DEV-02", self._inventory(bootstrap_ips=("10.0.1.9",)))
        monkeypatch.setattr(
            hosts,
            "_load_deployment_state",
            lambda name: {"vm_ips": {"digitalocean/lon1": ["10.0.0.1"]}, "bootstrap_nodes": []},
        )

        with pytest.raises(hosts.StaleInventoryError, match="ant-bootstrap-1 at 10.0.1.9"):
            hosts.load_vms("DEV-02")

    def test_node_vms_and_select_vms_inherit_the_check(self, local_inventory_root, monkeypatch):
        self._default(local_inventory_root, "DEV-02", self._inventory())
        monkeypatch.setattr(hosts, "_load_deployment_state", self._no_state)

        with pytest.raises(hosts.StaleInventoryError):
            hosts.node_vms("DEV-02")
        with pytest.raises(hosts.StaleInventoryError):
            hosts.select_vms("DEV-02", ["10.0.0.1"])

    def test_a_client_only_snapshot_is_checked_against_client_state(
        self, local_inventory_root, monkeypatch
    ):
        """The prod client kinds keep no deployment state at all, so their
        snapshot's liveness is the client state's."""
        inv = {
            "schema_version": 7,
            "testnet_id": "PROD-DL-02",
            "created_at": "2026-10-03T21:13:45Z",
            "node_vms": [],
            "bootstrap_vms": [],
            "client_vms": [{"name": "PROD-DL-02-ant-client-download-lon1-1", "ip": "203.0.113.10"}],
        }
        self._default(local_inventory_root, "PROD-DL-02", inv)
        calls = []
        monkeypatch.setattr(
            hosts,
            "_load_deployment_state",
            lambda name: calls.append(name) or {"vm_ips": {}, "bootstrap_nodes": []},
        )
        monkeypatch.setattr(
            hosts, "_load_client_state", lambda name: {"clients": [{"ip": "203.0.113.10"}]}
        )

        assert [v.ip for v in hosts.load_vms("PROD-DL-02")] == ["203.0.113.10"]
        assert calls == []

        monkeypatch.setattr(
            hosts,
            "_load_client_state",
            lambda name: (_ for _ in ()).throw(RuntimeError("No client state")),
        )
        with pytest.raises(hosts.StaleInventoryError, match="no S3 client state"):
            hosts.load_vms("PROD-DL-02")

    def test_a_client_ip_outside_the_client_state_is_stale(self, local_inventory_root, monkeypatch):
        inv = {
            "schema_version": 7,
            "testnet_id": "PROD-DL-02",
            "created_at": "2026-10-03T21:13:45Z",
            "client_vms": [{"name": "PROD-DL-02-ant-client-download-lon1-1", "ip": "203.0.113.99"}],
        }
        self._default(local_inventory_root, "PROD-DL-02", inv)
        monkeypatch.setattr(
            hosts, "_load_client_state", lambda name: {"clients": [{"ip": "203.0.113.10"}]}
        )

        with pytest.raises(hosts.StaleInventoryError, match="203.0.113.99"):
            hosts.load_vms("PROD-DL-02")

    def test_an_explicit_override_makes_no_s3_call(self, tmp_path, monkeypatch):
        """`--inventory` is the operator saying "I know": the check is skipped
        entirely, which is what keeps every `--inventory-path` caller unchanged."""
        inv = _write_inventory(tmp_path)

        def _never(name):
            raise AssertionError("state must not be loaded for an explicit inventory")

        monkeypatch.setattr(hosts, "_load_deployment_state", _never)
        monkeypatch.setattr(hosts, "_load_client_state", _never)

        assert len(hosts.load_vms("DEV-01", str(inv))) == 4

    def test_a_vm_less_snapshot_needs_no_state(self, local_inventory_root, monkeypatch):
        """A standalone Elasticsearch deployment's inventory has no VM of any
        tier; there is nothing to act on, so there is nothing to refuse."""
        self._default(
            local_inventory_root,
            "BETA-ES-01",
            {"schema_version": 7, "testnet_id": "BETA-ES-01", "node_vms": [], "client_vms": []},
        )

        def _never(name):
            raise AssertionError("no state lookup for a VM-less inventory")

        monkeypatch.setattr(hosts, "_load_deployment_state", _never)
        monkeypatch.setattr(hosts, "_load_client_state", _never)

        assert hosts.load_vms("BETA-ES-01") == []
