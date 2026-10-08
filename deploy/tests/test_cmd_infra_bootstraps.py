from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.infra_bootstraps import cmd_infra_bootstraps


def _args(**overrides):
    defaults = {
        "name": "DEV-02",
        "attached_volume_size": 20,
        "bootstrap_count": 3,
        "force": False,
        "do_regions_list": None,
        "purpose": "test",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.infra_bootstraps.record_deployment_created") as p_record_create,
        patch("saorsa_deploy.cmd.infra_bootstraps.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.infra_bootstraps.load_deployment_state") as p_load_state,
        patch("saorsa_deploy.cmd.infra_bootstraps.list_stale_bootstrap_state_keys") as p_list_stale,
        patch("saorsa_deploy.cmd.infra_bootstraps.purge_stale_bootstrap_state") as p_purge_stale,
        patch("saorsa_deploy.cmd.infra_bootstraps.create_bootstrap_vms") as p_create_boot,
        patch("saorsa_deploy.cmd.infra_bootstraps.save_deployment_state") as p_save_state,
        patch("saorsa_deploy.cmd.infra_bootstraps.update_deployment_state") as p_update_state,
    ):
        p_list_stale.return_value = []
        yield SimpleNamespace(
            record_create=p_record_create,
            record_event=p_record_event,
            load_state=p_load_state,
            list_stale=p_list_stale,
            purge_stale=p_purge_stale,
            create_boot=p_create_boot,
            save_state=p_save_state,
            update_state=p_update_state,
        )


class TestFreshRun:
    def test_creates_bootstraps_and_saves_state_with_empty_regions(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]

        cmd_infra_bootstraps(_args())

        patches.create_boot.assert_called_once()
        patches.save_state.assert_called_once()
        call = patches.save_state.call_args
        assert call.kwargs["regions"] == []
        assert call.kwargs["vm_ips"] == {}
        assert call.kwargs["vm_ids"] == {}
        assert call.kwargs["bootstrap_nodes"] == patches.create_boot.return_value

    def test_errors_out_when_stale_state_without_force(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
        ]

        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args())

        patches.purge_stale.assert_not_called()
        patches.create_boot.assert_not_called()

    def test_force_purges_stale_then_creates(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
        ]
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]

        cmd_infra_bootstraps(_args(force=True))

        patches.purge_stale.assert_called_once()
        patches.create_boot.assert_called_once()


class TestRerun:
    def test_reuses_existing_bootstraps_when_count_matches(self, patches):
        nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ]
        patches.load_state.return_value = {"bootstrap_nodes": nodes}

        cmd_infra_bootstraps(_args(bootstrap_count=3))

        patches.create_boot.assert_not_called()
        patches.save_state.assert_not_called()
        patches.update_state.assert_not_called()

    def test_errors_when_existing_count_differs(self, patches):
        nodes = [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}]
        patches.load_state.return_value = {"bootstrap_nodes": nodes}

        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(bootstrap_count=3))

        patches.create_boot.assert_not_called()

    def test_state_exists_without_bootstraps_writes_them_via_update(self, patches):
        """If `nodes infra` ran somehow before `bootstraps infra` (shouldn't happen
        but shouldn't destroy existing state if it does), write via update
        instead of overwriting the whole file."""
        patches.load_state.return_value = {"bootstrap_nodes": [], "regions": []}
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]

        cmd_infra_bootstraps(_args())

        patches.save_state.assert_not_called()
        patches.update_state.assert_called_once()
        updates = patches.update_state.call_args.args[1]
        assert updates["bootstrap_nodes"] == patches.create_boot.return_value
        assert updates["bootstrap_ip"] == "1.2.3.4"


class TestArgValidation:
    def test_rejects_bootstrap_count_zero(self, patches):
        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(bootstrap_count=0))

    def test_rejects_bootstrap_count_above_seven(self, patches):
        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(bootstrap_count=8))


class TestDoRegionsList:
    def test_list_overrides_count_and_is_passed_through(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "fra1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "lon1", "bootstrap_index": 2},
        ]

        cmd_infra_bootstraps(_args(bootstrap_count=7, do_regions_list="fra1, lon1"))

        patches.create_boot.assert_called_once()
        kwargs = patches.create_boot.call_args.kwargs
        assert kwargs["regions_override"] == ["fra1", "lon1"]
        assert kwargs["count"] == 2

    def test_empty_list_string_behaves_like_unset(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.create_boot.return_value = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ]

        cmd_infra_bootstraps(_args(bootstrap_count=3, do_regions_list=""))

        kwargs = patches.create_boot.call_args.kwargs
        assert kwargs["regions_override"] is None
        assert kwargs["count"] == 3

    def test_list_with_more_than_seven_regions_errors(self, patches):
        eight = "lon1,nyc1,ams3,sfo3,sgp1,blr1,fra1,tor1"
        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(do_regions_list=eight))

        patches.create_boot.assert_not_called()

    def test_rerun_with_matching_list_reuses_bootstraps(self, patches):
        nodes = [
            {"ip": "1.1.1.1", "region": "fra1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "lon1", "bootstrap_index": 2},
        ]
        patches.load_state.return_value = {"bootstrap_nodes": nodes}

        cmd_infra_bootstraps(_args(do_regions_list="fra1,lon1"))

        patches.create_boot.assert_not_called()
        patches.save_state.assert_not_called()
        patches.update_state.assert_not_called()

    def test_rerun_with_mismatched_list_errors(self, patches):
        nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
        ]
        patches.load_state.return_value = {"bootstrap_nodes": nodes}

        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(do_regions_list="fra1,lon1"))

        patches.create_boot.assert_not_called()

    def test_rerun_with_mismatched_list_length_errors(self, patches):
        nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ]
        patches.load_state.return_value = {"bootstrap_nodes": nodes}

        with pytest.raises(SystemExit):
            cmd_infra_bootstraps(_args(do_regions_list="lon1,nyc1"))

        patches.create_boot.assert_not_called()
