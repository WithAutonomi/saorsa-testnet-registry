import json
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.bootstrap_production import (
    BINARY_INSTALL_DIR,
    BINARY_INSTALL_PATH,
    PROVIDER_TF_DIRS,
    STATE_KEY_PREFIX,
    _build_terraform_configs,
    _resolve_hetzner_availability,
)
from saorsa_deploy.provisioning.hardening import (
    SERVICE_USER,
    build_systemd_unit,
)
from saorsa_deploy.provisioning.volume import (
    DEVICE_DISCOVERY,
    MOUNT_POINT,
    STRIPE_LV_NAME,
    STRIPE_VG_NAME,
    build_striped_volume_command,
)

# === Production IPs loading ===


class TestLoadProductionIps:
    def test_loads_nodes_from_file(self, tmp_path):
        ips_file = tmp_path / "production-ips.json"
        ips_file.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "provider": "vultr",
                            "region": "nrt",
                            "ip": "1.2.3.4",
                            "role": "genesis",
                        }
                    ]
                }
            )
        )

        # Test the JSON parsing logic directly
        with open(ips_file) as f:
            data = json.load(f)
        nodes = data["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["ip"] == "1.2.3.4"
        assert nodes[0]["role"] == "genesis"

    def test_production_ips_file_structure(self, tmp_path):
        """Verify the expected structure of production-ips.json."""
        data = {
            "nodes": [
                {
                    "provider": "vultr",
                    "region": "nrt",
                    "ip": "207.148.94.42",
                    "role": "genesis",
                    "resource_id": "c8091c4c-f801-41ce-9795-aed8c47fe7e9",
                },
                {
                    "provider": "hetzner",
                    "region": "fsn1",
                    "ip": "167.235.201.229",
                    "resource_id": "123956850",
                },
            ]
        }
        nodes = data["nodes"]
        assert nodes[0]["provider"] == "vultr"
        assert nodes[1]["provider"] == "hetzner"
        assert nodes[0].get("role") == "genesis"
        assert nodes[1].get("role") is None


# === Terraform config building ===


class TestBuildTerraformConfigs:
    @pytest.fixture
    def sample_nodes(self):
        return [
            {
                "provider": "vultr",
                "region": "nrt",
                "ip": "1.2.3.4",
                "role": "genesis",
                "resource_id": "uuid-genesis",
            },
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "5.6.7.8",
                "resource_id": "12345",
            },
            {
                "provider": "vultr",
                "region": "ewr",
                "ip": "9.10.11.12",
                "resource_id": "uuid-abc",
            },
            {
                "provider": "aws",
                "region": "sa-east-1",
                "ip": "13.14.15.16",
                "resource_id": "eipalloc-123",
            },
        ]

    @pytest.fixture
    def sample_ssh_key_ids(self):
        return {
            "hetzner": "99999",
            "vultr": "vultr-key-id",
            "aws": "ant-production-production",
        }

    def test_builds_config_per_node(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        assert len(configs) == 4

    def test_hetzner_uses_floating_ip_id(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        hz_config = next(c for c in configs if c.provider == "hetzner")
        assert hz_config.variables["floating_ip_id"] == "12345"
        assert hz_config.variables["location"] == "fsn1"

    def test_vultr_uses_reserved_ip_id(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        vl_config = next(c for c in configs if c.provider == "vultr" and c.region == "nrt")
        assert vl_config.variables["reserved_ip_id"] == "uuid-genesis"

    def test_aws_uses_eip_allocation_id(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        aws_config = next(c for c in configs if c.provider == "aws")
        assert aws_config.variables["eip_allocation_id"] == "eipalloc-123"

    def test_state_keys_use_production_prefix(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        for config in configs:
            assert config.state_key.startswith(STATE_KEY_PREFIX)
            assert config.state_key.endswith(".tfstate")

    def test_hetzner_uses_ssh_key_id(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        hz_config = next(c for c in configs if c.provider == "hetzner")
        assert hz_config.variables["ssh_key_id"] == "99999"

    def test_vultr_uses_ssh_key_id(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        vl_config = next(c for c in configs if c.provider == "vultr" and c.region == "nrt")
        assert vl_config.variables["ssh_key_id"] == "vultr-key-id"

    def test_aws_uses_key_pair_name(self, sample_nodes, sample_ssh_key_ids):
        from unittest.mock import MagicMock

        console = MagicMock()
        configs = _build_terraform_configs(
            sample_nodes, "ant-production", sample_ssh_key_ids, console
        )
        aws_config = next(c for c in configs if c.provider == "aws")
        assert aws_config.variables["key_pair_name"] == "ant-production-production"


# === Striped volume ===


class TestBuildStripedVolumeCommand:
    def test_checks_if_already_mounted(self):
        cmd = build_striped_volume_command("vultr")
        assert f"mountpoint -q {MOUNT_POINT}" in cmd

    def test_reports_already_mounted(self):
        cmd = build_striped_volume_command("vultr")
        assert "ANT_STRIPE:ALREADY_MOUNTED" in cmd

    def test_reports_created(self):
        cmd = build_striped_volume_command("vultr")
        assert "ANT_STRIPE:CREATED" in cmd

    def test_reports_not_enough_devices(self):
        cmd = build_striped_volume_command("vultr")
        assert "ANT_STRIPE:NOT_ENOUGH_DEVICES" in cmd

    def test_installs_lvm2(self):
        cmd = build_striped_volume_command("vultr")
        assert "apt-get install -y lvm2" in cmd

    def test_creates_pv(self):
        cmd = build_striped_volume_command("vultr")
        assert "pvcreate" in cmd

    def test_creates_vg(self):
        cmd = build_striped_volume_command("vultr")
        assert f"vgcreate {STRIPE_VG_NAME}" in cmd

    def test_creates_striped_lv(self):
        cmd = build_striped_volume_command("vultr")
        assert "--stripes 2" in cmd
        assert f"-n {STRIPE_LV_NAME}" in cmd

    def test_formats_ext4(self):
        cmd = build_striped_volume_command("vultr")
        assert "mkfs.ext4" in cmd

    def test_adds_fstab_entry(self):
        cmd = build_striped_volume_command("vultr")
        assert "/etc/fstab" in cmd

    def test_uses_noatime_mount_option(self):
        cmd = build_striped_volume_command("vultr")
        assert "noatime" in cmd

    def test_hetzner_device_discovery(self):
        cmd = build_striped_volume_command("hetzner")
        assert "scsi-0HC_Volume" in cmd

    def test_vultr_device_discovery(self):
        cmd = build_striped_volume_command("vultr")
        assert "/dev/vd" in cmd

    def test_aws_device_discovery(self):
        cmd = build_striped_volume_command("aws")
        assert "/dev/xvd" in cmd

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            build_striped_volume_command("unknown_provider")

    def test_custom_mount_point(self):
        cmd = build_striped_volume_command("vultr", mount_point="/mnt/custom")
        assert "/mnt/custom" in cmd
        assert MOUNT_POINT not in cmd

    def test_requires_two_devices(self):
        cmd = build_striped_volume_command("vultr")
        assert '-z "$DEV1"' in cmd or '-z "$DEV2"' in cmd


# === Hardening / systemd unit ===


class TestBuildSystemdUnit:
    def test_contains_exec_start(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert "ExecStart=/usr/local/bin/ant-node" in unit

    def test_contains_port(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data", port=10000)
        assert "--port 10000" in unit

    def test_contains_service_user(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert f"User={SERVICE_USER}" in unit

    def test_contains_resource_limits(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert "LimitNOFILE=65536" in unit
        assert "MemoryMax=3G" in unit
        assert "CPUQuota=180%" in unit

    def test_contains_restart_policy(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert "Restart=always" in unit

    def test_includes_bootstrap_peers(self):
        unit = build_systemd_unit(
            "/usr/local/bin/ant-node",
            "/data",
            bootstrap_peers="/ip4/1.2.3.4/udp/10000/quic-v1",
        )
        assert "--bootstrap /ip4/1.2.3.4/udp/10000/quic-v1" in unit

    def test_no_peer_flag_for_genesis(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data", bootstrap_peers=None)
        assert "--bootstrap" not in unit

    def test_data_dir_in_exec_start(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/my/data")
        assert "--root-dir /my/data" in unit

    def test_includes_enable_logging(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert "--enable-logging" in unit

    def test_includes_stop_on_upgrade(self):
        unit = build_systemd_unit("/usr/local/bin/ant-node", "/data")
        assert "--stop-on-upgrade" in unit

    def test_a_peer_list_becomes_one_flag_each(self):
        # V2-1309: a bootstrap handed a single peer has exactly one way back in
        # if it loses its data directory.
        unit = build_systemd_unit(
            "/opt/ant-node/ant-node",
            "/data",
            bootstrap_peers=["1.1.1.1:10000", "2.2.2.2:10000"],
        )
        assert "--bootstrap 1.1.1.1:10000" in unit
        assert "--bootstrap 2.2.2.2:10000" in unit
        assert unit.count("--bootstrap") == 2

    def test_an_empty_peer_list_writes_no_flag(self):
        unit = build_systemd_unit("/opt/ant-node/ant-node", "/data", bootstrap_peers=[])
        assert "--bootstrap" not in unit


# === Binary install path ===


class TestBinaryInstallPath:
    def test_it_is_where_the_live_tier_runs_from(self):
        # The constant read /usr/local/bin/ant-node until V2-1309, which no
        # production bootstrap has ever run: a re-provision would have
        # installed a second, unused binary and left the service on the old
        # one.
        assert BINARY_INSTALL_PATH == "/opt/ant-node/ant-node"

    def test_the_directory_is_the_path_it_installs_into(self):
        assert BINARY_INSTALL_PATH.startswith(f"{BINARY_INSTALL_DIR}/")


# === Provider Terraform directories ===


class TestProviderTfDirs:
    def test_all_three_providers_mapped(self):
        assert "hetzner" in PROVIDER_TF_DIRS
        assert "vultr" in PROVIDER_TF_DIRS
        assert "aws" in PROVIDER_TF_DIRS

    def test_digitalocean_not_in_providers(self):
        assert "digitalocean" not in PROVIDER_TF_DIRS

    def test_device_discovery_all_providers(self):
        assert "hetzner" in DEVICE_DISCOVERY
        assert "vultr" in DEVICE_DISCOVERY
        assert "aws" in DEVICE_DISCOVERY


# === Production hardening provisioner ===


class TestProductionHardeningProvisioner:
    def test_init_stores_params(self):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1", ssh_key_path="/tmp/key", node_port=12000)
        assert p.ip == "10.0.0.1"
        assert p.ssh_key_path == "/tmp/key"
        assert p.node_port == 12000

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_execute_calls_pyinfra_operations(
        self,
        _mock_inventory,
        _mock_state,
        mock_connect,
        mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        mock_connect.assert_called_once()
        assert mock_add_op.call_count > 1
        mock_run_ops.assert_called_once()
        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_execute_disconnects_on_error(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        _mock_add_op,
        mock_run_ops,
        mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        mock_run_ops.side_effect = RuntimeError("connection failed")

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        with pytest.raises(RuntimeError, match="connection failed"):
            p.execute()

        mock_disconnect.assert_called_once()

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_installs_hardening_packages(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "fail2ban" in all_args
        assert "unattended-upgrades" in all_args

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_creates_service_user(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "useradd" in all_args

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_hardens_ssh(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "PermitRootLogin" in all_args

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_configures_fail2ban(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "jail" in all_args.lower() or "sshd.conf" in all_args

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_applies_sysctl_hardening(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "sysctl" in all_args.lower()

    @patch("saorsa_deploy.provisioning.hardening.disconnect_all")
    @patch("saorsa_deploy.provisioning.hardening.run_ops")
    @patch("saorsa_deploy.provisioning.hardening.add_op")
    @patch("saorsa_deploy.provisioning.hardening.connect_all")
    @patch("saorsa_deploy.provisioning.hardening.State")
    @patch("saorsa_deploy.provisioning.hardening.Inventory")
    def test_applies_rate_limiting(
        self,
        _mock_inventory,
        _mock_state,
        _mock_connect,
        mock_add_op,
        _mock_run_ops,
        _mock_disconnect,
    ):
        from saorsa_deploy.provisioning.hardening import ProductionHardeningProvisioner

        p = ProductionHardeningProvisioner(ip="10.0.0.1")
        p.execute()

        all_args = str(mock_add_op.call_args_list)
        assert "ANT_RATE_LIMIT" in all_args


# === cmd_bootstrap_production behaviour ===


class TestCmdBootstrapProduction:
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_exits_without_hcloud_token(self, _mock_subprocess, monkeypatch):
        monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
        monkeypatch.setenv("VULTR_API_KEY", "fake")

        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        args = SimpleNamespace(
            name="test",
            force=True,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_bootstrap_production(args)
        assert exc_info.value.code == 1

    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_exits_without_vultr_api_key(self, _mock_subprocess, monkeypatch):
        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.delenv("VULTR_API_KEY", raising=False)

        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        args = SimpleNamespace(
            name="test",
            force=True,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )
        with pytest.raises(SystemExit) as exc_info:
            cmd_bootstrap_production(args)
        assert exc_info.value.code == 1

    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_exits_without_ssh_public_key(self, mock_subprocess, monkeypatch, tmp_path):
        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.setenv("VULTR_API_KEY", "fake")
        mock_subprocess.return_value = MagicMock(returncode=0)

        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        nonexistent_key = str(tmp_path / "no-such-key")
        args = SimpleNamespace(
            name="test",
            force=True,
            node_version=None,
            ssh_key_path=nonexistent_key,
        )
        sample_nodes = [
            {
                "provider": "vultr",
                "region": "nrt",
                "ip": "1.2.3.4",
                "role": "genesis",
            },
        ]
        sample_keys = {"vultr": "vultr-key-id"}
        with (
            patch(
                "saorsa_deploy.cmd.bootstrap_production._load_production_config",
                return_value=(sample_nodes, sample_keys),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.get_release_url",
                return_value="https://example.com/binary.tar.gz",
            ),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_bootstrap_production(args)
            assert exc_info.value.code == 1

    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    @patch("saorsa_deploy.cmd.bootstrap_production._confirm_hetzner_token")
    def test_confirms_hetzner_token(self, mock_confirm, mock_subprocess, monkeypatch):
        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.setenv("VULTR_API_KEY", "fake")
        mock_subprocess.return_value = MagicMock(returncode=0)

        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        args = SimpleNamespace(
            name="test",
            force=False,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )
        sample_nodes = [
            {
                "provider": "vultr",
                "region": "nrt",
                "ip": "1.2.3.4",
                "role": "genesis",
            },
        ]
        sample_keys = {"vultr": "vultr-key-id"}
        with (
            patch(
                "saorsa_deploy.cmd.bootstrap_production._load_production_config",
                return_value=(sample_nodes, sample_keys),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.Path",
            ) as mock_path,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.get_release_url",
                return_value="https://example.com/binary.tar.gz",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.check_terraform_state_exists",
                return_value=False,
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.execute_terraform_runs",
                return_value=[MagicMock(success=True)],
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.clear_known_hosts",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.Popen",
            ) as mock_hardener,
            patch(
                "saorsa_deploy.cmd.bootstrap_production._provision_node",
            ),
        ):
            mock_path.return_value.expanduser.return_value.exists.return_value = True
            mock_hardener.return_value.communicate.return_value = (b"", b"")
            mock_hardener.return_value.returncode = 0
            cmd_bootstrap_production(args)

        mock_confirm.assert_called_once()


# === Full success path ===


class TestCmdBootstrapProductionSuccessPath:
    """Behaviour tests for the full success path of cmd_bootstrap_production."""

    SAMPLE_NODES = [
        {
            "provider": "vultr",
            "region": "nrt",
            "ip": "1.2.3.4",
            "role": "genesis",
            "resource_id": "uuid-genesis",
        },
        {
            "provider": "hetzner",
            "region": "fsn1",
            "ip": "5.6.7.8",
            "resource_id": "12345",
        },
        {
            "provider": "vultr",
            "region": "ewr",
            "ip": "9.10.11.12",
            "resource_id": "uuid-abc",
        },
    ]

    SAMPLE_SSH_KEY_IDS = {
        "hetzner": "99999",
        "vultr": "vultr-key-id",
    }

    def _run_success(self, monkeypatch, **patch_overrides):
        """Run cmd_bootstrap_production with all external calls mocked.

        Returns a dict of all mock objects for assertion.
        """
        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.setenv("VULTR_API_KEY", "fake")

        args = SimpleNamespace(
            name="ant-production",
            force=True,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )

        mocks = {}
        tf_results = [MagicMock(success=True) for _ in self.SAMPLE_NODES]

        def _mock_subprocess_run(cmd, **kwargs):
            result = MagicMock(returncode=0)
            # hcloud server ip returns the primary IP
            if cmd and len(cmd) >= 3 and cmd[0] == "hcloud" and cmd[1] == "server":
                result.stdout = "10.20.30.40\n"
            else:
                result.stdout = ""
            return result

        with (
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.run",
                side_effect=_mock_subprocess_run,
            ) as m_subprocess,
            patch(
                "saorsa_deploy.cmd.bootstrap_production._load_production_config",
                return_value=(self.SAMPLE_NODES, self.SAMPLE_SSH_KEY_IDS),
            ) as m_load_config,
            patch(
                "saorsa_deploy.cmd.bootstrap_production._resolve_hetzner_availability",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.Path",
            ) as m_path,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.get_release_url",
                return_value="https://example.com/ant-node.tar.gz",
            ) as m_release_url,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.check_terraform_state_exists",
                return_value=False,
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.execute_terraform_runs",
                return_value=tf_results,
            ) as m_terraform,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.clear_known_hosts",
            ) as m_clear_hosts,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.Popen",
            ) as m_popen,
            patch(
                "saorsa_deploy.cmd.bootstrap_production._provision_node",
            ) as m_provision,
        ):
            m_path.return_value.expanduser.return_value.exists.return_value = True
            m_popen.return_value.communicate.return_value = (b"", b"")
            m_popen.return_value.returncode = 0
            cmd_bootstrap_production(args)

            mocks["subprocess"] = m_subprocess
            mocks["load_config"] = m_load_config
            mocks["release_url"] = m_release_url
            mocks["terraform"] = m_terraform
            mocks["clear_hosts"] = m_clear_hosts
            mocks["popen"] = m_popen
            mocks["provision"] = m_provision

        return mocks

    def test_runs_terraform_for_all_nodes(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        mocks["terraform"].assert_called_once()
        configs = mocks["terraform"].call_args[0][0]
        assert len(configs) == len(self.SAMPLE_NODES)

    def test_clears_known_hosts_for_all_ips(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        mocks["clear_hosts"].assert_called_once()
        cleared_ips = mocks["clear_hosts"].call_args[0][0]
        # Includes floating IPs, node IPs, AND Hetzner primary SSH IPs
        assert {"1.2.3.4", "5.6.7.8", "9.10.11.12"}.issubset(set(cleared_ips))

    def test_hardens_all_nodes(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        # Hardening runs via subprocess.Popen — one per node for hardening,
        # plus more for remaining node provisioning. At minimum, len(SAMPLE_NODES)
        # Popen calls for hardening.
        popen = mocks["popen"]
        assert popen.call_count >= len(self.SAMPLE_NODES)

    def test_provisions_genesis_first_with_the_other_hosts_as_peers(self, monkeypatch):
        # V2-1309: every host, genesis included, lists the others on the node
        # port and never itself. The genesis is still provisioned first, so on
        # a from-scratch deploy it comes up while its peers are unreachable.
        mocks = self._run_success(monkeypatch)
        genesis_call = mocks["provision"].call_args_list[0]
        assert genesis_call.kwargs["ip"] == "1.2.3.4"
        assert genesis_call.kwargs["bootstrap_peers"] == [
            "5.6.7.8:10000",
            "9.10.11.12:10000",
        ]

    def test_provisions_remaining_nodes_with_every_other_host(self, monkeypatch):
        # The remaining nodes are provisioned through subprocess.Popen, so the
        # peer list has to be read back out of the script it is handed.
        mocks = self._run_success(monkeypatch)
        scripts = [
            call.args[0][2]
            for call in mocks["popen"].call_args_list
            if "_provision_node" in call.args[0][2]
        ]
        assert len(scripts) == len(self.SAMPLE_NODES) - 1
        for script, peers in zip(
            scripts,
            [
                ["1.2.3.4:10000", "9.10.11.12:10000"],
                ["1.2.3.4:10000", "5.6.7.8:10000"],
            ],
        ):
            assert f"bootstrap_peers={peers!r}" in script

    def test_provisions_correct_number_of_nodes(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        # Genesis is provisioned directly via _provision_node
        assert mocks["provision"].call_count == 1
        # Remaining nodes are provisioned via subprocess.Popen
        # (Popen is also used for hardening, so total calls = nodes + remaining)
        remaining = len(self.SAMPLE_NODES) - 1
        assert mocks["popen"].call_count == len(self.SAMPLE_NODES) + remaining

    def test_uses_latest_release_when_no_version_specified(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        mocks["release_url"].assert_called_once_with()

    def test_passes_binary_url_to_all_provisions(self, monkeypatch):
        mocks = self._run_success(monkeypatch)
        for call in mocks["provision"].call_args_list:
            assert call.kwargs["binary_url"] == "https://example.com/ant-node.tar.gz"
            assert call.kwargs["binary_is_archive"] is True

    def test_exits_on_terraform_failure(self, monkeypatch):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.setenv("VULTR_API_KEY", "fake")

        args = SimpleNamespace(
            name="ant-production",
            force=True,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )

        failed_result = MagicMock(success=False, provider="hetzner", region="fsn1", stderr="error")

        with (
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production._load_production_config",
                return_value=(self.SAMPLE_NODES, self.SAMPLE_SSH_KEY_IDS),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production._resolve_hetzner_availability",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.Path",
            ) as m_path,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.get_release_url",
                return_value="https://example.com/ant-node.tar.gz",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.check_terraform_state_exists",
                return_value=False,
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.execute_terraform_runs",
                return_value=[MagicMock(success=True), failed_result],
            ),
        ):
            m_path.return_value.expanduser.return_value.exists.return_value = True
            with pytest.raises(SystemExit) as exc_info:
                cmd_bootstrap_production(args)
            assert exc_info.value.code == 1

    def test_exits_on_hardening_failure(self, monkeypatch):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        monkeypatch.setenv("HCLOUD_TOKEN", "fake")
        monkeypatch.setenv("VULTR_API_KEY", "fake")

        args = SimpleNamespace(
            name="ant-production",
            force=True,
            node_version=None,
            ssh_key_path="/tmp/key",
            rewards_address="0x1234567890abcdef",
            evm_network="arbitrum-one",
        )

        with (
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.run",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production._load_production_config",
                return_value=(self.SAMPLE_NODES, self.SAMPLE_SSH_KEY_IDS),
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production._resolve_hetzner_availability",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.Path",
            ) as m_path,
            patch(
                "saorsa_deploy.cmd.bootstrap_production.get_release_url",
                return_value="https://example.com/ant-node.tar.gz",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.check_terraform_state_exists",
                return_value=False,
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.execute_terraform_runs",
                return_value=[MagicMock(success=True) for _ in self.SAMPLE_NODES],
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.clear_known_hosts",
            ),
            patch(
                "saorsa_deploy.cmd.bootstrap_production.subprocess.Popen",
            ) as m_hardener,
        ):
            m_path.return_value.expanduser.return_value.exists.return_value = True
            m_hardener.return_value.communicate.return_value = (b"", b"SSH failed")
            m_hardener.return_value.returncode = 1
            with pytest.raises(SystemExit) as exc_info:
                cmd_bootstrap_production(args)
            assert exc_info.value.code == 1


# === Hetzner availability resolution ===


def _build_dc_response(locations: dict[str, dict[int, bool]]) -> dict:
    """Build a mock Hetzner /v1/datacenters API response.

    Args:
        locations: mapping of location name -> {type_id: is_available}
    """
    datacenters = []
    for loc_name, types in locations.items():
        supported = list(types.keys())
        available = [tid for tid, avail in types.items() if avail]
        datacenters.append(
            {
                "location": {"name": loc_name},
                "server_types": {
                    "supported": supported,
                    "available": available,
                },
            }
        )
    return {"datacenters": datacenters}


class TestResolveHetznerAvailability:
    """Tests for _resolve_hetzner_availability."""

    def _make_console(self):
        return MagicMock()

    def _mock_curl(self, dc_response):
        """Return a MagicMock suitable for subprocess.run return value."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps(dc_response)
        return m

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_no_hetzner_nodes_skips(self, mock_run):
        """No API call when there are no Hetzner nodes."""
        nodes = [{"provider": "vultr", "region": "nrt", "ip": "1.2.3.4"}]
        console = self._make_console()
        _resolve_hetzner_availability(nodes, console)
        mock_run.assert_not_called()

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_current_type_available_no_change(self, mock_run):
        """When the configured type is available, node is not modified."""
        dc_data = _build_dc_response({"fsn1": {109: True, 114: True}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            }
        ]
        console = self._make_console()
        _resolve_hetzner_availability(nodes, console)
        assert nodes[0]["server_type"] == "cpx22"

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_fallback_to_cx23_in_fsn1(self, mock_run):
        """When cpx22 is unavailable in fsn1, falls back to cx23."""
        dc_data = _build_dc_response({"fsn1": {109: False, 114: True}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            }
        ]
        console = self._make_console()
        _resolve_hetzner_availability(nodes, console)
        assert nodes[0]["server_type"] == "cx23"

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_no_fallback_exits(self, mock_run):
        """When no type is available in the location, exits with error."""
        dc_data = _build_dc_response({"fsn1": {109: False, 114: False}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            }
        ]
        console = self._make_console()
        with pytest.raises(SystemExit) as exc_info:
            _resolve_hetzner_availability(nodes, console)
        assert exc_info.value.code == 1

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_ash_cpx21_available(self, mock_run):
        """When cpx21 is available in ash, no change."""
        dc_data = _build_dc_response({"ash": {23: True}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "ash",
                "ip": "1.2.3.4",
                "server_type": "cpx21",
                "resource_id": "456",
            }
        ]
        console = self._make_console()
        _resolve_hetzner_availability(nodes, console)
        assert nodes[0]["server_type"] == "cpx21"

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_ash_no_fallback_exits(self, mock_run):
        """When cpx21 is unavailable in ash and no fallbacks exist, exits."""
        dc_data = _build_dc_response({"ash": {23: False}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "ash",
                "ip": "1.2.3.4",
                "server_type": "cpx21",
                "resource_id": "456",
            }
        ]
        console = self._make_console()
        with pytest.raises(SystemExit) as exc_info:
            _resolve_hetzner_availability(nodes, console)
        assert exc_info.value.code == 1

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_api_failure_exits(self, mock_run):
        """When the Hetzner API call fails, exits with error."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")

        nodes = [
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            }
        ]
        console = self._make_console()
        with pytest.raises(SystemExit) as exc_info:
            _resolve_hetzner_availability(nodes, console)
        assert exc_info.value.code == 1

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_multiple_hetzner_nodes(self, mock_run):
        """Resolves availability for multiple Hetzner nodes independently."""
        dc_data = _build_dc_response(
            {
                "fsn1": {109: False, 114: True},
                "ash": {23: True},
            }
        )
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "fsn1",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            },
            {
                "provider": "hetzner",
                "region": "ash",
                "ip": "5.6.7.8",
                "server_type": "cpx21",
                "resource_id": "456",
            },
            {"provider": "vultr", "region": "nrt", "ip": "9.9.9.9"},
        ]
        console = self._make_console()
        _resolve_hetzner_availability(nodes, console)
        assert nodes[0]["server_type"] == "cx23"  # fell back
        assert nodes[1]["server_type"] == "cpx21"  # stayed the same

    @patch.dict("os.environ", {"HCLOUD_TOKEN": "test-token"})
    @patch("saorsa_deploy.cmd.bootstrap_production.subprocess.run")
    def test_unknown_location_exits(self, mock_run):
        """When a node's location is not in the API response, exits."""
        dc_data = _build_dc_response({"fsn1": {109: True}})
        mock_run.return_value = self._mock_curl(dc_data)

        nodes = [
            {
                "provider": "hetzner",
                "region": "xyz99",
                "ip": "1.2.3.4",
                "server_type": "cpx22",
                "resource_id": "123",
            }
        ]
        console = self._make_console()
        with pytest.raises(SystemExit) as exc_info:
            _resolve_hetzner_availability(nodes, console)
        assert exc_info.value.code == 1


# === The provision pass's binary-directory ownership (V2-1412) ===


class TestBinaryDirectoryIsOwnedByTheServiceUser:
    """The pass that installs the binary also hands its directory over.

    Whichever branch installs it does so as root, and ant-node's own
    auto-upgrader runs as `ant`: it writes `<binary>.backup` and a temp dir
    *inside* this directory, neither of which it can do under root ownership.
    `/opt/ant-node` is `ant:ant` on all seven live hosts only because someone
    chowned it by hand; a rebuilt bootstrap would be un-upgradeable from its
    first day.
    """

    def _ops(self, **overrides):
        from saorsa_deploy.cmd.bootstrap_production import _provision_node

        recorded = []

        def add_op(state, op, **kwargs):
            recorded.append((getattr(op, "__name__", str(op)), kwargs))

        kwargs = {
            "ip": "1.2.3.4",
            "provider": "vultr",
            "ssh_key_path": "~/.ssh/ant-production",
            "binary_url": "https://example/ant-node-cli-linux-x64.tar.gz",
            "binary_is_archive": True,
            "bootstrap_peers": ["1.1.1.1:10000"],
            "rewards_address": "0xc20c19b57629fc22E8e1e50a3b924A20b0fA25AA",
            "evm_network": "arbitrum-one",
            "console": MagicMock(),
        }
        kwargs.update(overrides)
        with (
            patch("pyinfra.api.operation.add_op", add_op),
            patch("pyinfra.api.connect.connect_all", MagicMock()),
            patch("pyinfra.api.connect.disconnect_all", MagicMock()),
            patch("pyinfra.api.operations.run_ops", MagicMock()),
        ):
            _provision_node(**kwargs)
        return recorded

    @staticmethod
    def _index_of_name(ops, name):
        return next(i for i, (_, kwargs) in enumerate(ops) if kwargs.get("name") == name)

    @pytest.mark.parametrize(
        "overrides,install_op",
        [
            ({}, "Download and install ant-node binary"),
            (
                {"binary_is_archive": False},
                "Download and install ant-node binary",
            ),
            (
                {"binary_path": "/tmp/ant-node", "binary_url": None},
                "Upload local ant-node binary",
            ),
        ],
        ids=["archive", "bare-binary", "local-file"],
    )
    def test_the_chown_follows_the_install_and_precedes_the_unit(self, overrides, install_op):
        ops = self._ops(**overrides)
        chown_at = self._index_of_name(ops, "Own the binary directory as the service user")
        assert ops[chown_at][1]["commands"] == [
            f"chown -R {SERVICE_USER}:{SERVICE_USER} {BINARY_INSTALL_DIR}"
        ]
        # After the install, or it would chown a directory the install then
        # writes into as root; before the unit, so the service never starts on
        # a directory its own upgrader cannot use.
        assert chown_at > self._index_of_name(ops, install_op)
        assert chown_at < self._index_of_name(ops, "Write systemd unit file")
