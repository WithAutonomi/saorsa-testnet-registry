import json
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.generate_inventory import (
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_USER,
    FIREWALL_PLACEHOLDER,
    INVENTORY_SCHEMA_VERSION,
    build_inventory,
)


def _state(**overrides):
    base = {
        "name": "DEV-02",
        "regions": [
            ["digitalocean", "lon1"],
            ["hetzner", "fsn1"],
            ["vultr", "ewr"],
        ],
        "terraform_variables": {"name": "DEV-02"},
        "bootstrap_nodes": [
            {"ip": "143.198.0.1", "region": "lon1", "bootstrap_index": 1},
        ],
        "bootstrap_ip": "143.198.0.1",
        "vm_ips": {
            "digitalocean/lon1": ["10.0.0.1", "10.0.0.2"],
            "hetzner/fsn1": ["10.1.0.1"],
            "vultr/ewr": ["10.2.0.1"],
        },
        "vm_ids": {
            "digitalocean/lon1": ["111", "222"],
            "hetzner/fsn1": ["333"],
            "vultr/ewr": ["uuid-444"],
        },
        "node_count": 3,
        "binary": {
            "url": "s3://ant-node-builds/builds/jacderida/feat/ant-node",
            "is_archive": False,
            "branch": "feat/shard-consumer-instrumentation",
            "repo_owner": "jacderida",
        },
    }
    base.update(overrides)
    return base


class TestBuildInventory:
    def test_top_level_keys_match_schema_v3(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert set(inv.keys()) == {
            "schema_version",
            "testnet_id",
            "created_at",
            "binary",
            "firewalls",
            "bootstrap_vms",
            "node_vms",
        }
        assert inv["schema_version"] == INVENTORY_SCHEMA_VERSION
        assert inv["testnet_id"] == "DEV-02"
        assert inv["created_at"] == "2026-04-16T18:00:00Z"

    def test_created_at_defaults_to_utc_now(self):
        inv = build_inventory(_state())
        assert inv["created_at"].endswith("Z")
        assert "T" in inv["created_at"]

    def test_binary_field_from_branch_build(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert inv["binary"] == {
            "ant_node_version": "branch:jacderida/feat/shard-consumer-instrumentation",
            "ant_node_release_url": (
                "s3://ant-node-builds/builds/jacderida/feat/ant-node"
            ),
        }

    def test_binary_field_from_release_version(self):
        inv = build_inventory(
            _state(
                binary={
                    "url": "https://github.com/WithAutonomi/ant-node/releases/download/v0.10.0/ant-node-cli-linux-x64.tar.gz",
                    "is_archive": True,
                    "version": "0.10.0",
                }
            ),
            created_at="2026-04-16T18:00:00Z",
        )
        assert inv["binary"] == {
            "ant_node_version": "0.10.0",
            "ant_node_release_url": (
                "https://github.com/WithAutonomi/ant-node/releases/"
                "download/v0.10.0/ant-node-cli-linux-x64.tar.gz"
            ),
        }

    def test_binary_field_empty_when_state_lacks_binary(self):
        state = _state()
        del state["binary"]
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        assert inv["binary"] == {
            "ant_node_version": "",
            "ant_node_release_url": "",
        }

    def test_firewalls_placeholder_per_provider(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert inv["firewalls"] == {
            "digitalocean": FIREWALL_PLACEHOLDER,
            "hetzner": FIREWALL_PLACEHOLDER,
            "vultr": FIREWALL_PLACEHOLDER,
        }

    def test_firewalls_includes_digitalocean_when_only_bootstrap(self):
        state = _state(vm_ips={}, vm_ids={})
        inv = build_inventory(
            state, node_count=3, created_at="2026-04-16T18:00:00Z"
        )
        assert inv["firewalls"] == {"digitalocean": FIREWALL_PLACEHOLDER}

    def test_node_vms_shape(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert len(inv["node_vms"]) == 4  # 2 do + 1 hetzner + 1 vultr
        first = inv["node_vms"][0]
        assert set(first.keys()) == {
            "name",
            "provider",
            "provider_id",
            "region",
            "ip",
            "ssh_user",
            "ssh_key",
            "nodes",
            "services",
        }
        assert first["ssh_user"] == DEFAULT_SSH_USER
        assert first["ssh_key"] == DEFAULT_SSH_KEY

    def test_node_vm_name_is_deterministic(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        do_vms = [v for v in inv["node_vms"] if v["provider"] == "digitalocean"]
        assert [v["name"] for v in do_vms] == [
            "DEV-02-ant-node-lon1-1",
            "DEV-02-ant-node-lon1-2",
        ]

    def test_node_vm_provider_id_from_vm_ids(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        do_vms = [v for v in inv["node_vms"] if v["provider"] == "digitalocean"]
        assert [v["provider_id"] for v in do_vms] == ["111", "222"]

        vultr_vms = [v for v in inv["node_vms"] if v["provider"] == "vultr"]
        assert vultr_vms[0]["provider_id"] == "uuid-444"

    def test_node_vm_provider_id_empty_when_vm_ids_missing(self):
        state = _state(vm_ids={})
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        assert all(v["provider_id"] == "" for v in inv["node_vms"])

    def test_services_list_matches_node_count(self):
        inv = build_inventory(
            _state(node_count=3), node_count=3, created_at="2026-04-16T18:00:00Z"
        )
        vm = inv["node_vms"][0]
        assert vm["services"] == ["ant-node-1", "ant-node-2", "ant-node-3"]
        assert vm["nodes"] == [
            {"service_name": "ant-node-1"},
            {"service_name": "ant-node-2"},
            {"service_name": "ant-node-3"},
        ]

    def test_explicit_node_count_overrides_state(self):
        inv = build_inventory(
            _state(node_count=3), node_count=15, created_at="2026-04-16T18:00:00Z"
        )
        vm = inv["node_vms"][0]
        assert len(vm["services"]) == 15

    def test_raises_when_node_count_missing(self):
        state = _state()
        del state["node_count"]
        with pytest.raises(ValueError, match="node_count is missing"):
            build_inventory(state)

    def test_custom_ssh_key_propagates_to_each_vm(self):
        inv = build_inventory(
            _state(),
            ssh_key="~/.ssh/ant_deploy",
            created_at="2026-04-16T18:00:00Z",
        )
        assert all(v["ssh_key"] == "~/.ssh/ant_deploy" for v in inv["node_vms"])

    def test_round_trips_through_json(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        round_tripped = json.loads(json.dumps(inv))
        assert round_tripped == inv


class TestBootstrapVmsField:
    def _multi_bootstrap_state(self):
        return _state(
            bootstrap_nodes=[
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
                {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
            ],
        )

    def test_one_entry_per_bootstrap_node(self):
        inv = build_inventory(
            self._multi_bootstrap_state(), created_at="2026-04-16T18:00:00Z"
        )
        assert len(inv["bootstrap_vms"]) == 3

    def test_entries_are_sorted_by_bootstrap_index(self):
        state = _state(
            bootstrap_nodes=[
                {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            ],
        )
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        indices = [v["bootstrap_index"] for v in inv["bootstrap_vms"]]
        assert indices == [1, 2, 3]

    def test_each_entry_has_required_fields(self):
        inv = build_inventory(
            self._multi_bootstrap_state(), created_at="2026-04-16T18:00:00Z"
        )
        entry = inv["bootstrap_vms"][0]
        assert set(entry.keys()) == {
            "name",
            "provider",
            "provider_id",
            "region",
            "ip",
            "bootstrap_index",
            "ssh_user",
            "ssh_key",
            "nodes",
            "services",
        }

    def test_service_name_follows_ant_bootstrap_index(self):
        inv = build_inventory(
            self._multi_bootstrap_state(), created_at="2026-04-16T18:00:00Z"
        )
        services = [v["services"] for v in inv["bootstrap_vms"]]
        assert services == [
            ["ant-bootstrap-1"],
            ["ant-bootstrap-2"],
            ["ant-bootstrap-3"],
        ]
        for entry in inv["bootstrap_vms"]:
            assert entry["nodes"] == [{"service_name": entry["services"][0]}]

    def test_name_uses_testnet_id_and_index(self):
        inv = build_inventory(
            self._multi_bootstrap_state(), created_at="2026-04-16T18:00:00Z"
        )
        names = [v["name"] for v in inv["bootstrap_vms"]]
        assert names == [
            "DEV-02-ant-bootstrap-1",
            "DEV-02-ant-bootstrap-2",
            "DEV-02-ant-bootstrap-3",
        ]

    def test_provider_is_digitalocean(self):
        inv = build_inventory(
            self._multi_bootstrap_state(), created_at="2026-04-16T18:00:00Z"
        )
        assert all(v["provider"] == "digitalocean" for v in inv["bootstrap_vms"])

    def test_ssh_defaults_and_override(self):
        inv = build_inventory(
            self._multi_bootstrap_state(),
            ssh_key="~/.ssh/ant_deploy",
            created_at="2026-04-16T18:00:00Z",
        )
        for entry in inv["bootstrap_vms"]:
            assert entry["ssh_user"] == DEFAULT_SSH_USER
            assert entry["ssh_key"] == "~/.ssh/ant_deploy"

    def test_empty_when_no_bootstrap_nodes_in_state(self):
        state = _state(bootstrap_nodes=[])
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        assert inv["bootstrap_vms"] == []


class TestCmdGenerateInventory:
    def test_writes_file_to_output_path(self, tmp_path):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        output = tmp_path / "DEV-02.json"
        args = MagicMock()
        args.name = "DEV-02"
        args.output = str(output)
        args.ssh_key_path = "~/.ssh/id_rsa"
        args.node_count = None

        with patch(
            "saorsa_deploy.cmd.generate_inventory.load_deployment_state",
            return_value=_state(),
        ):
            cmd_generate_inventory(args)

        data = json.loads(output.read_text())
        assert data["testnet_id"] == "DEV-02"
        assert data["schema_version"] == INVENTORY_SCHEMA_VERSION
        assert len(data["node_vms"]) == 4
