import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.generate_inventory import (
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_USER,
    DEVNET_DATA_DIR,
    DEVNET_EVM_NETWORK,
    FIREWALL_PLACEHOLDER,
    INVENTORY_SCHEMA_VERSION,
    build_devnet_manifest,
    build_inventory,
)
from saorsa_deploy.evm import get_evm_network_info


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
        "bootstrap_port": 30000,
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
    def test_top_level_keys_match_schema(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert set(inv.keys()) == {
            "schema_version",
            "testnet_id",
            "created_at",
            "binary",
            "firewalls",
            "bootstrap_vms",
            "node_vms",
            "client_vms",
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
            "nat_simulated",
            "nodes",
            "services",
        }
        assert first["ssh_user"] == DEFAULT_SSH_USER
        assert first["ssh_key"] == DEFAULT_SSH_KEY
        assert first["nat_simulated"] is False

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
            {"service_name": "ant-node-1", "nat_simulated": False},
            {"service_name": "ant-node-2", "nat_simulated": False},
            {"service_name": "ant-node-3", "nat_simulated": False},
        ]

    def test_explicit_node_count_overrides_state(self):
        inv = build_inventory(
            _state(node_count=3), node_count=15, created_at="2026-04-16T18:00:00Z"
        )
        vm = inv["node_vms"][0]
        assert len(vm["services"]) == 15

    def test_nat_simulated_flag_on_matching_ips(self):
        state = _state(nat_vm_ips=["10.0.0.1", "10.2.0.1"])
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        by_ip = {vm["ip"]: vm for vm in inv["node_vms"]}
        assert by_ip["10.0.0.1"]["nat_simulated"] is True
        assert by_ip["10.0.0.2"]["nat_simulated"] is False
        assert by_ip["10.1.0.1"]["nat_simulated"] is False
        assert by_ip["10.2.0.1"]["nat_simulated"] is True

    def test_nat_simulated_propagates_into_nodes_list(self):
        state = _state(nat_vm_ips=["10.0.0.1"])
        inv = build_inventory(state, created_at="2026-04-16T18:00:00Z")
        by_ip = {vm["ip"]: vm for vm in inv["node_vms"]}
        nat_vm = by_ip["10.0.0.1"]
        assert all(n["nat_simulated"] is True for n in nat_vm["nodes"])
        plain_vm = by_ip["10.0.0.2"]
        assert all(n["nat_simulated"] is False for n in plain_vm["nodes"])

    def test_nat_simulated_defaults_false_when_state_missing_key(self):
        # Legacy deployments pre-dating NAT support have no `nat_vm_ips` key.
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert all(vm["nat_simulated"] is False for vm in inv["node_vms"])

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


class TestClientVmsField:
    def _clients(self):
        return [
            {
                "region": "lon1",
                "file_size_mb": 100,
                "vm_size": "s-2vcpu-4gb",
                "client_index": 2,
                "ip": "5.5.5.5",
            },
            {
                "region": "nyc1",
                "file_size_mb": 250,
                "vm_size": "s-4vcpu-8gb",
                "client_index": 1,
                "ip": "6.6.6.6",
            },
        ]

    def test_empty_when_clients_omitted(self):
        inv = build_inventory(_state(), created_at="2026-04-16T18:00:00Z")
        assert inv["client_vms"] == []

    def test_empty_when_clients_is_empty_list(self):
        inv = build_inventory(_state(), clients=[], created_at="2026-04-16T18:00:00Z")
        assert inv["client_vms"] == []

    def test_one_entry_per_client(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        assert len(inv["client_vms"]) == 2

    def test_entries_sorted_by_client_index(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        assert [c["client_index"] for c in inv["client_vms"]] == [1, 2]

    def test_entry_shape(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        entry = inv["client_vms"][0]
        assert set(entry.keys()) == {
            "name",
            "provider",
            "provider_id",
            "region",
            "ip",
            "role",
            "client_index",
            "file_size_mb",
            "vm_size",
            "bootstrap_cache",
            "ssh_user",
            "ssh_key",
            "nodes",
            "services",
        }
        assert entry["provider"] == "digitalocean"
        assert entry["ssh_user"] == DEFAULT_SSH_USER
        assert entry["ssh_key"] == DEFAULT_SSH_KEY

    def test_name_uses_testnet_region_and_index(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        names = [c["name"] for c in inv["client_vms"]]
        assert names == [
            "DEV-02-ant-client-upload-nyc1-1",
            "DEV-02-ant-client-upload-lon1-2",
        ]

    def test_bootstrap_cache_passed_through(self):
        clients = self._clients()
        clients[0]["bootstrap_cache"] = False
        clients[1]["bootstrap_cache"] = True
        inv = build_inventory(
            _state(), clients=clients, created_at="2026-04-16T18:00:00Z"
        )
        # entries are sorted by client_index: [1 (nyc1), 2 (lon1)]
        assert [c["bootstrap_cache"] for c in inv["client_vms"]] == [True, False]

    def test_bootstrap_cache_none_for_legacy_state(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        assert [c["bootstrap_cache"] for c in inv["client_vms"]] == [None, None]

    def test_services_match_the_vm_role(self):
        clients = self._clients()
        clients[0]["role"] = "download"
        inv = build_inventory(
            _state(), clients=clients, created_at="2026-04-16T18:00:00Z"
        )
        # sorted by client_index: [1 (nyc1, upload default), 2 (lon1, download)]
        upload_entry, download_entry = inv["client_vms"]
        assert upload_entry["services"] == ["ant-uploader"]
        assert upload_entry["nodes"] == [{"service_name": "ant-uploader"}]
        assert download_entry["services"] == ["ant-downloader"]
        assert download_entry["nodes"] == [{"service_name": "ant-downloader"}]

    def test_same_region_and_index_across_roles_do_not_collide(self):
        # client_index is assigned per role (uploaders 1..N, downloaders 1..N),
        # so an uploader and downloader sharing a region+index must still get
        # distinct inventory names. Regression for the DEV-01 duplicate
        # "ant-client-lon1-1" pair.
        clients = [
            {
                "role": "upload",
                "region": "lon1",
                "file_size_mb": 10,
                "vm_size": "s-2vcpu-4gb",
                "client_index": 1,
                "ip": "1.1.1.1",
            },
            {
                "role": "download",
                "region": "lon1",
                "vm_size": "s-2vcpu-4gb",
                "client_index": 1,
                "ip": "2.2.2.2",
            },
        ]
        inv = build_inventory(
            _state(), clients=clients, created_at="2026-04-16T18:00:00Z"
        )
        names = [c["name"] for c in inv["client_vms"]]
        assert names == [
            "DEV-02-ant-client-upload-lon1-1",
            "DEV-02-ant-client-download-lon1-1",
        ]
        assert len(set(names)) == 2

    def test_provider_passed_through_from_client_state(self):
        clients = self._clients()
        clients[0]["provider"] = "vultr"
        inv = build_inventory(
            _state(), clients=clients, created_at="2026-04-16T18:00:00Z"
        )
        # sorted by client_index: [1 (nyc1, default digitalocean), 2 (lon1, vultr)]
        assert [c["provider"] for c in inv["client_vms"]] == [
            "digitalocean",
            "vultr",
        ]

    def test_file_size_and_vm_size_propagate(self):
        inv = build_inventory(
            _state(), clients=self._clients(), created_at="2026-04-16T18:00:00Z"
        )
        by_index = {c["client_index"]: c for c in inv["client_vms"]}
        assert by_index[1]["file_size_mb"] == 250
        assert by_index[1]["vm_size"] == "s-4vcpu-8gb"
        assert by_index[2]["file_size_mb"] == 100
        assert by_index[2]["vm_size"] == "s-2vcpu-4gb"

    def test_firewalls_include_digitalocean_when_only_clients(self):
        state = _state(vm_ips={}, vm_ids={}, bootstrap_nodes=[])
        inv = build_inventory(
            state,
            clients=self._clients(),
            node_count=3,
            created_at="2026-04-16T18:00:00Z",
        )
        assert inv["firewalls"] == {"digitalocean": FIREWALL_PLACEHOLDER}


class TestCmdGenerateInventory:
    def _args(self, tmp_path, name="DEV-02"):
        args = MagicMock()
        args.name = name
        args.output = str(tmp_path / f"{name}.json")
        args.ssh_key_path = "~/.ssh/id_rsa"
        args.node_count = None
        args.wallet_secret_key = "0xdeadbeef"
        return args

    def test_writes_file_to_output_path(self, tmp_path):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        args = self._args(tmp_path)

        with patch(
            "saorsa_deploy.cmd.generate_inventory.load_deployment_state",
            return_value=_state(),
        ), patch(
            "saorsa_deploy.cmd.generate_inventory.load_client_state",
            side_effect=RuntimeError("no client state"),
        ):
            cmd_generate_inventory(args)

        data = json.loads(Path(args.output).read_text())
        assert data["testnet_id"] == "DEV-02"
        assert data["schema_version"] == INVENTORY_SCHEMA_VERSION
        assert len(data["node_vms"]) == 4
        assert data["client_vms"] == []

    def test_includes_client_vms_when_client_state_exists(self, tmp_path):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        args = self._args(tmp_path)

        client_state = {
            "name": "DEV-02",
            "clients": [
                {
                    "region": "lon1",
                    "file_size_mb": 100,
                    "vm_size": "s-2vcpu-4gb",
                    "client_index": 1,
                    "ip": "7.7.7.7",
                },
            ],
        }
        with patch(
            "saorsa_deploy.cmd.generate_inventory.load_deployment_state",
            return_value=_state(),
        ), patch(
            "saorsa_deploy.cmd.generate_inventory.load_client_state",
            return_value=client_state,
        ):
            cmd_generate_inventory(args)

        data = json.loads(Path(args.output).read_text())
        assert len(data["client_vms"]) == 1
        assert data["client_vms"][0]["ip"] == "7.7.7.7"
        assert data["client_vms"][0]["name"] == "DEV-02-ant-client-upload-lon1-1"

    def test_writes_devnet_manifest_alongside_inventory(self, tmp_path):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        args = self._args(tmp_path)

        with patch(
            "saorsa_deploy.cmd.generate_inventory.load_deployment_state",
            return_value=_state(),
        ), patch(
            "saorsa_deploy.cmd.generate_inventory.load_client_state",
            side_effect=RuntimeError("no client state"),
        ):
            cmd_generate_inventory(args)

        manifest_path = Path(args.output).parent / "devnet-manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["base_port"] == 30000
        assert manifest["node_count"] == 12  # 3 per VM × 4 node VMs
        assert manifest["bootstrap"] == ["/ip4/143.198.0.1/udp/30000/quic"]
        assert manifest["data_dir"] == DEVNET_DATA_DIR
        assert manifest["evm"]["wallet_private_key"] == "0xdeadbeef"

    def test_wallet_key_falls_back_to_env_var(self, tmp_path, monkeypatch):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        args = self._args(tmp_path)
        args.wallet_secret_key = None
        monkeypatch.setenv("ANT_DEPLOY_SECRET_KEY", "0xfromenv")

        with patch(
            "saorsa_deploy.cmd.generate_inventory.load_deployment_state",
            return_value=_state(),
        ), patch(
            "saorsa_deploy.cmd.generate_inventory.load_client_state",
            side_effect=RuntimeError("no client state"),
        ):
            cmd_generate_inventory(args)

        manifest_path = Path(args.output).parent / "devnet-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["evm"]["wallet_private_key"] == "0xfromenv"

    def test_exits_when_no_wallet_key_available(self, tmp_path, monkeypatch):
        from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

        args = self._args(tmp_path)
        args.wallet_secret_key = None
        monkeypatch.delenv("ANT_DEPLOY_SECRET_KEY", raising=False)

        with pytest.raises(SystemExit) as exc:
            cmd_generate_inventory(args)
        assert exc.value.code == 1


class TestBuildDevnetManifest:
    def test_top_level_shape(self):
        manifest = build_devnet_manifest(
            _state(),
            wallet_private_key="0xabc",
            created_at="2026-04-22T14:00:00.000000+00:00",
        )
        assert set(manifest.keys()) == {
            "base_port",
            "node_count",
            "bootstrap",
            "data_dir",
            "created_at",
            "evm",
        }

    def test_base_port_from_bootstrap_port(self):
        manifest = build_devnet_manifest(
            _state(bootstrap_port=40001), wallet_private_key="k"
        )
        assert manifest["base_port"] == 40001

    def test_node_count_is_total_across_node_vms(self):
        # 3 per VM × (2 + 1 + 1) VMs = 12
        manifest = build_devnet_manifest(_state(), wallet_private_key="k")
        assert manifest["node_count"] == 12

    def test_node_count_override_multiplies_by_vm_count(self):
        # 5 per VM × 4 VMs = 20
        manifest = build_devnet_manifest(
            _state(), wallet_private_key="k", node_count=5
        )
        assert manifest["node_count"] == 20

    def test_bootstrap_multiaddrs_use_state_port_by_default(self):
        state = _state(
            bootstrap_nodes=[
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            ],
            bootstrap_port=31337,
        )
        manifest = build_devnet_manifest(state, wallet_private_key="k")
        assert manifest["bootstrap"] == [
            "/ip4/1.1.1.1/udp/31337/quic",
            "/ip4/2.2.2.2/udp/31337/quic",
        ]

    def test_bootstrap_multiaddrs_use_per_node_port_when_set(self):
        # `bootstraps provision` stamps `port` on each entry; should take precedence.
        state = _state(
            bootstrap_nodes=[
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1, "port": 22000},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2, "port": 22000},
            ],
            bootstrap_port=30000,
        )
        manifest = build_devnet_manifest(state, wallet_private_key="k")
        assert manifest["bootstrap"] == [
            "/ip4/1.1.1.1/udp/22000/quic",
            "/ip4/2.2.2.2/udp/22000/quic",
        ]

    def test_bootstrap_multiaddrs_sorted_by_index(self):
        state = _state(
            bootstrap_nodes=[
                {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            ],
        )
        manifest = build_devnet_manifest(state, wallet_private_key="k")
        ips_in_order = [m.split("/")[2] for m in manifest["bootstrap"]]
        assert ips_in_order == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]

    def test_data_dir_is_mount_point(self):
        manifest = build_devnet_manifest(_state(), wallet_private_key="k")
        assert manifest["data_dir"] == DEVNET_DATA_DIR
        assert manifest["data_dir"].startswith("/mnt/")

    def test_created_at_defaults_to_iso_with_utc_offset(self):
        manifest = build_devnet_manifest(_state(), wallet_private_key="k")
        # datetime.isoformat() with UTC timezone yields '...+00:00' (RFC3339-compatible)
        assert "T" in manifest["created_at"]
        assert manifest["created_at"].endswith("+00:00")

    def test_created_at_override_preserved(self):
        manifest = build_devnet_manifest(
            _state(),
            wallet_private_key="k",
            created_at="2026-04-22T14:00:00.000000+00:00",
        )
        assert manifest["created_at"] == "2026-04-22T14:00:00.000000+00:00"

    def test_evm_block_uses_sepolia_constants_by_default(self):
        manifest = build_devnet_manifest(_state(), wallet_private_key="0xabc")
        sepolia = get_evm_network_info(DEVNET_EVM_NETWORK)
        assert manifest["evm"] == {
            "rpc_url": sepolia["rpc_url"],
            "wallet_private_key": "0xabc",
            "payment_token_address": sepolia["payment_token_address"],
            "payment_vault_address": sepolia["payment_vault_address"],
        }

    def test_raises_when_bootstrap_port_missing(self):
        state = _state()
        del state["bootstrap_port"]
        with pytest.raises(ValueError, match="bootstrap_port is missing"):
            build_devnet_manifest(state, wallet_private_key="k")

    def test_raises_when_node_count_missing(self):
        state = _state()
        del state["node_count"]
        with pytest.raises(ValueError, match="node_count is missing"):
            build_devnet_manifest(state, wallet_private_key="k")

    def test_raises_for_unknown_evm_network(self):
        with pytest.raises(ValueError, match="Unknown EVM network"):
            build_devnet_manifest(
                _state(), wallet_private_key="k", evm_network="bogus"
            )

    def test_round_trips_through_json(self):
        manifest = build_devnet_manifest(
            _state(),
            wallet_private_key="k",
            created_at="2026-04-22T14:00:00.000000+00:00",
        )
        assert json.loads(json.dumps(manifest)) == manifest
