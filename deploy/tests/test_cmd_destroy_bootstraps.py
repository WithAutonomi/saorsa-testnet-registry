from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.destroy_bootstraps import cmd_destroy_bootstraps


def _args(**overrides):
    defaults = {"name": "DEV-02", "force": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.record_event"
        ) as p_record_event,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.record_status"
        ) as p_record_status,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.load_deployment_state"
        ) as p_load_state,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.update_deployment_state"
        ) as p_update_state,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.delete_deployment_state"
        ) as p_delete_state,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.destroy_bootstrap_vms"
        ) as p_destroy_boot,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.list_stale_bootstrap_state_keys"
        ) as p_list_stale,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.purge_stale_bootstrap_state"
        ) as p_purge_stale,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.list_extensions_of"
        ) as p_list_exts,
    ):
        p_list_stale.return_value = []
        p_list_exts.return_value = []
        yield SimpleNamespace(
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            update_state=p_update_state,
            delete_state=p_delete_state,
            destroy_boot=p_destroy_boot,
            list_stale=p_list_stale,
            purge_stale=p_purge_stale,
            list_exts=p_list_exts,
        )


class TestHappyPath:
    def test_destroys_bootstraps_and_clears_state_slice(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            ],
            "regions": [],
            "terraform_variables": {"attached_volume_size": "50"},
        }

        cmd_destroy_bootstraps(_args())

        patches.destroy_boot.assert_called_once()
        call_kwargs = patches.destroy_boot.call_args.kwargs
        assert call_kwargs["regions"] == ["lon1", "nyc1"]
        assert call_kwargs["volume_size"] == 50
        # With no remaining node slice, the state file should be removed outright.
        patches.delete_state.assert_called_once()
        patches.update_state.assert_not_called()


class TestRefusesWhenNodesPresent:
    def test_errors_when_regions_populated(self, patches):
        patches.load_state.return_value = {
            "bootstrap_nodes": [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {},
        }

        with pytest.raises(SystemExit):
            cmd_destroy_bootstraps(_args())

        patches.destroy_boot.assert_not_called()


class TestExtensionGuard:
    def test_refuses_when_extensions_depend_on_these_bootstraps(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
            "terraform_variables": {},
        }
        patches.list_exts.return_value = ["DEV-02", "DEV-03"]

        with pytest.raises(SystemExit):
            cmd_destroy_bootstraps(_args(name="DEV-01"))

        patches.destroy_boot.assert_not_called()
        patches.delete_state.assert_not_called()

    def test_proceeds_when_no_extensions(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
            "terraform_variables": {},
        }
        patches.list_exts.return_value = []

        cmd_destroy_bootstraps(_args(name="DEV-01"))

        patches.destroy_boot.assert_called_once()


class TestMissingStateFallback:
    def test_scans_s3_for_orphan_bootstrap_tfstate(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
        ]

        cmd_destroy_bootstraps(_args())

        patches.purge_stale.assert_called_once()
        assert patches.purge_stale.call_args.args[0] == "DEV-02"

    def test_no_op_when_no_state_and_no_orphans(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = []

        cmd_destroy_bootstraps(_args())

        patches.purge_stale.assert_not_called()
