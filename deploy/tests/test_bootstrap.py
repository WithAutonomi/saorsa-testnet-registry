import pytest

from saorsa_deploy.bootstrap import (
    BOOTSTRAP_STATE_KEY_PREFIX,
    _build_bootstrap_configs,
    _select_bootstrap_regions,
)


class TestSelectBootstrapRegions:
    def test_single_bootstrap_returns_first_region(self):
        regions = _select_bootstrap_regions(1)
        assert regions == ["lon1"]

    def test_three_bootstraps_returns_first_three(self):
        regions = _select_bootstrap_regions(3)
        assert regions == ["lon1", "nyc1", "ams3"]

    def test_seven_bootstraps_returns_seven_regions(self):
        regions = _select_bootstrap_regions(7)
        assert len(regions) == 7
        assert regions == [
            "lon1", "nyc1", "ams3", "sfo3", "sgp1", "blr1", "fra1"
        ]

    def test_raises_when_count_exceeds_available_regions(self):
        with pytest.raises(ValueError, match="only 8"):
            _select_bootstrap_regions(9)

    def test_all_regions_unique(self):
        regions = _select_bootstrap_regions(7)
        assert len(set(regions)) == 7


class TestBuildBootstrapConfigs:
    def test_single_config_for_single_region(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"])
        assert len(configs) == 1
        assert configs[0].provider == "digitalocean"
        assert configs[0].region == "lon1"
        assert configs[0].variables["bootstrap_index"] == "1"
        assert configs[0].variables["name"] == "DEV-01"

    def test_multiple_configs_for_multiple_regions(self):
        configs = _build_bootstrap_configs(
            "DEV-02", ["lon1", "nyc1", "ams3"]
        )
        assert len(configs) == 3
        assert configs[0].region == "lon1"
        assert configs[0].variables["bootstrap_index"] == "1"
        assert configs[1].region == "nyc1"
        assert configs[1].variables["bootstrap_index"] == "2"
        assert configs[2].region == "ams3"
        assert configs[2].variables["bootstrap_index"] == "3"

    def test_state_keys_use_correct_prefix(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1", "nyc1"])
        assert configs[0].state_key == (
            f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-01-lon1.tfstate"
        )
        assert configs[1].state_key == (
            f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-01-nyc1.tfstate"
        )

    def test_workspace_dirs_are_unique_per_region(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1", "nyc1"])
        assert configs[0].workspace_dir != configs[1].workspace_dir
        assert "lon1" in str(configs[0].workspace_dir)
        assert "nyc1" in str(configs[1].workspace_dir)

    def test_volume_size_passed_to_variables(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"], volume_size=50)
        assert configs[0].variables["attached_volume_size"] == "50"

    def test_droplet_size_passed_to_variables(self):
        configs = _build_bootstrap_configs(
            "DEV-01", ["lon1"], droplet_size="s-4vcpu-8gb"
        )
        assert configs[0].variables["droplet_size"] == "s-4vcpu-8gb"

    def test_tf_source_dir_points_to_bootstrap_manifests(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"])
        assert configs[0].tf_source_dir.name == "digitalocean-bootstrap"


class TestMultiBootstrapExecStart:
    """Test that regular nodes get multiple --bootstrap args."""

    def test_single_bootstrap_peer(self):
        from saorsa_deploy.provisioning.node import _build_node_exec_start
        from saorsa_deploy.provisioning.volume import MOUNT_POINT

        root_dir = f"{MOUNT_POINT}/data/ant-node-1"
        result = _build_node_exec_start(["10.0.0.1:5000"], root_dir)
        assert result.count("--bootstrap") == 1
        assert "--bootstrap 10.0.0.1:5000" in result

    def test_multiple_bootstrap_peers(self):
        from saorsa_deploy.provisioning.node import _build_node_exec_start
        from saorsa_deploy.provisioning.volume import MOUNT_POINT

        root_dir = f"{MOUNT_POINT}/data/ant-node-1"
        peers = [
            "10.0.0.1:5000",
            "10.0.0.2:5000",
            "10.0.0.3:5000",
        ]
        result = _build_node_exec_start(peers, root_dir)
        assert result.count("--bootstrap") == 3
        for peer in peers:
            assert f"--bootstrap {peer}" in result

    def test_seven_bootstrap_peers(self):
        from saorsa_deploy.provisioning.node import _build_node_exec_start
        from saorsa_deploy.provisioning.volume import MOUNT_POINT

        root_dir = f"{MOUNT_POINT}/data/ant-node-1"
        peers = [f"10.0.0.{i}:5000" for i in range(1, 8)]
        result = _build_node_exec_start(peers, root_dir)
        assert result.count("--bootstrap") == 7


class TestBootstrapNodeExecStart:
    """Test the bootstrap provisioner exec_start building."""

    def test_genesis_has_no_bootstrap(self):
        from saorsa_deploy.provisioning.bootstrap import _build_exec_start

        result = _build_exec_start(bootstrap_index=1)
        assert "--bootstrap" not in result

    def test_non_genesis_includes_genesis_peer(self):
        from saorsa_deploy.provisioning.bootstrap import _build_exec_start

        result = _build_exec_start(
            bootstrap_index=2,
            bootstrap_peers=["10.0.0.1:5000"],
        )
        assert "--bootstrap 10.0.0.1:5000" in result

    def test_bootstrap_index_in_root_dir(self):
        from saorsa_deploy.provisioning.bootstrap import _build_exec_start

        result = _build_exec_start(bootstrap_index=3)
        assert "ant-bootstrap-3" in result
