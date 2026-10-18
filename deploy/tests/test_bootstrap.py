from unittest.mock import patch

import pytest

from saorsa_deploy.bootstrap import (
    BOOTSTRAP_STATE_KEY_PREFIX,
    _build_bootstrap_configs,
    _region_from_bootstrap_state_key,
    _select_bootstrap_regions,
    list_stale_bootstrap_state_keys,
    purge_stale_bootstrap_state,
)
from saorsa_deploy.providers import PROVIDERS


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
        assert regions == ["lon1", "nyc1", "ams3", "sfo3", "sgp1", "blr1", "fra1"]

    def test_raises_when_count_exceeds_available_regions(self):
        with pytest.raises(ValueError, match=f"only {len(PROVIDERS['digitalocean'].regions)}"):
            _select_bootstrap_regions(len(PROVIDERS["digitalocean"].regions) + 1)

    def test_all_regions_unique(self):
        regions = _select_bootstrap_regions(7)
        assert len(set(regions)) == 7

    def test_override_pins_exact_regions_in_order(self):
        regions = _select_bootstrap_regions(1, regions_override=["fra1", "lon1", "syd1"])
        assert regions == ["fra1", "lon1", "syd1"]

    def test_override_ignores_count_argument(self):
        regions = _select_bootstrap_regions(99, regions_override=["lon1"])
        assert regions == ["lon1"]

    def test_override_rejects_unknown_region(self):
        with pytest.raises(ValueError, match="Unknown digitalocean region"):
            _select_bootstrap_regions(2, regions_override=["lon1", "not-a-region"])

    def test_override_lists_known_regions_in_error(self):
        with pytest.raises(ValueError, match="lon1"):
            _select_bootstrap_regions(1, regions_override=["bogus"])


class TestBuildBootstrapConfigs:
    def test_single_config_for_single_region(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"])
        assert len(configs) == 1
        assert configs[0].provider == "digitalocean"
        assert configs[0].region == "lon1"
        assert configs[0].variables["bootstrap_index"] == "1"
        assert configs[0].variables["name"] == "DEV-01"

    def test_multiple_configs_for_multiple_regions(self):
        configs = _build_bootstrap_configs("DEV-02", ["lon1", "nyc1", "ams3"])
        assert len(configs) == 3
        assert configs[0].region == "lon1"
        assert configs[0].variables["bootstrap_index"] == "1"
        assert configs[1].region == "nyc1"
        assert configs[1].variables["bootstrap_index"] == "2"
        assert configs[2].region == "ams3"
        assert configs[2].variables["bootstrap_index"] == "3"

    def test_state_keys_use_correct_prefix(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1", "nyc1"])
        assert configs[0].state_key == (f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-01-lon1.tfstate")
        assert configs[1].state_key == (f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-01-nyc1.tfstate")

    def test_workspace_dirs_are_unique_per_region(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1", "nyc1"])
        assert configs[0].workspace_dir != configs[1].workspace_dir
        assert "lon1" in str(configs[0].workspace_dir)
        assert "nyc1" in str(configs[1].workspace_dir)

    def test_volume_size_passed_to_variables(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"], volume_size=50)
        assert configs[0].variables["attached_volume_size"] == "50"

    def test_droplet_size_passed_to_variables(self):
        configs = _build_bootstrap_configs("DEV-01", ["lon1"], droplet_size="s-4vcpu-8gb")
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


class TestRegionFromBootstrapStateKey:
    def test_extracts_region_from_well_formed_key(self):
        region = _region_from_bootstrap_state_key(
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate", "DEV-02"
        )
        assert region == "lon1"

    def test_handles_hyphenated_region(self):
        region = _region_from_bootstrap_state_key(
            "saorsa-deploy/do-bootstrap-DEV-02-ams3.tfstate", "DEV-02"
        )
        assert region == "ams3"

    def test_raises_on_unexpected_key(self):
        with pytest.raises(ValueError):
            _region_from_bootstrap_state_key("saorsa-deploy/something-else.tfstate", "DEV-02")


class TestListStaleBootstrapStateKeys:
    def test_delegates_to_list_terraform_state_keys_with_prefix(self):
        with patch("saorsa_deploy.bootstrap.list_terraform_state_keys") as mock_list:
            mock_list.return_value = [
                f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-lon1.tfstate",
            ]
            result = list_stale_bootstrap_state_keys("DEV-02")
            mock_list.assert_called_once_with(f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-")
            assert result == [
                f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-lon1.tfstate",
            ]


class TestPurgeStaleBootstrapState:
    def test_no_op_when_no_stale_keys(self):
        with (
            patch("saorsa_deploy.bootstrap.list_stale_bootstrap_state_keys") as mock_list,
            patch("saorsa_deploy.bootstrap.execute_terraform_runs") as mock_exec,
        ):
            mock_list.return_value = []
            purge_stale_bootstrap_state("DEV-02")
            mock_exec.assert_not_called()

    def test_runs_destroy_then_deletes_each_state(self):
        from saorsa_deploy.terraform import TerraformResult

        with (
            patch("saorsa_deploy.bootstrap.list_stale_bootstrap_state_keys") as mock_list,
            patch("saorsa_deploy.bootstrap.execute_terraform_runs") as mock_exec,
            patch("saorsa_deploy.bootstrap.delete_terraform_state") as mock_delete,
            patch(
                "saorsa_deploy.bootstrap.load_terraform_state",
                side_effect=Exception("no tfstate"),
            ),
        ):
            mock_list.return_value = [
                f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-lon1.tfstate",
                f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-nyc1.tfstate",
            ]
            mock_exec.return_value = [
                TerraformResult(success=True, provider="digitalocean", region="lon1"),
                TerraformResult(success=True, provider="digitalocean", region="nyc1"),
            ]

            purge_stale_bootstrap_state("DEV-02")

            assert mock_exec.call_count == 1
            args, kwargs = mock_exec.call_args
            configs = args[0]
            assert kwargs == {"action": "destroy"}
            assert [c.region for c in configs] == ["lon1", "nyc1"]
            assert mock_delete.call_count == 2

    def test_raises_when_destroy_fails(self):
        from saorsa_deploy.terraform import TerraformResult

        with (
            patch("saorsa_deploy.bootstrap.list_stale_bootstrap_state_keys") as mock_list,
            patch("saorsa_deploy.bootstrap.execute_terraform_runs") as mock_exec,
            patch("saorsa_deploy.bootstrap.delete_terraform_state") as mock_delete,
            patch(
                "saorsa_deploy.bootstrap.load_terraform_state",
                side_effect=Exception("no tfstate"),
            ),
        ):
            mock_list.return_value = [
                f"{BOOTSTRAP_STATE_KEY_PREFIX}-DEV-02-lon1.tfstate",
            ]
            mock_exec.return_value = [
                TerraformResult(success=False, provider="digitalocean", region="lon1"),
            ]

            with pytest.raises(RuntimeError, match="Failed to destroy"):
                purge_stale_bootstrap_state("DEV-02")
            mock_delete.assert_not_called()


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
