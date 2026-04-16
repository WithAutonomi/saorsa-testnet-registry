from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.infra import cmd_infra
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {
        "name": "DEV-02",
        "attached_volume_size": 20,
        "bootstrap_count": 3,
        "do_vm_count": 1,
        "do_regions": 1,
        "do_droplet_size": "s-2vcpu-4gb",
        "hetzner_vm_count": 0,
        "hetzner_regions": 0,
        "hetzner_server_type": "cpx22",
        "vultr_vm_count": 0,
        "vultr_regions": 0,
        "vultr_plan": "vc2-2c-4gb",
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    """Patch every external side-effect cmd_infra reaches for."""
    with (
        patch("saorsa_deploy.cmd.infra.record_deployment_created") as p_record_create,
        patch("saorsa_deploy.cmd.infra.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.infra.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.infra.load_deployment_state") as p_load_state,
        patch(
            "saorsa_deploy.cmd.infra.list_stale_bootstrap_state_keys"
        ) as p_list_stale,
        patch(
            "saorsa_deploy.cmd.infra.purge_stale_bootstrap_state"
        ) as p_purge_stale,
        patch("saorsa_deploy.cmd.infra.create_bootstrap_vms") as p_create_boot,
        patch("saorsa_deploy.cmd.infra.save_deployment_state") as p_save_state,
        patch(
            "saorsa_deploy.cmd.infra.check_terraform_state_exists"
        ) as p_check_state,
        patch("saorsa_deploy.cmd.infra.delete_terraform_state") as p_delete_state,
        patch("saorsa_deploy.cmd.infra.execute_terraform_runs") as p_execute,
    ):
        p_list_stale.return_value = []
        p_check_state.return_value = False
        p_execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ips": ["10.0.0.1"], "vm_ids": [1234]},
            ),
        ]
        yield SimpleNamespace(
            record_create=p_record_create,
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            list_stale=p_list_stale,
            purge_stale=p_purge_stale,
            create_boot=p_create_boot,
            save_state=p_save_state,
            check_state=p_check_state,
            delete_state=p_delete_state,
            execute=p_execute,
        )


class TestCmdInfraFreshRun:
    def test_creates_bootstraps_and_saves_state_before_main_provisioning(
        self, patches
    ):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]

        save_count_when_execute_ran = []
        original_return = patches.execute.return_value

        def _check_save_happened_first(*_args, **_kwargs):
            save_count_when_execute_ran.append(patches.save_state.call_count)
            return original_return

        patches.execute.side_effect = _check_save_happened_first

        cmd_infra(_args())

        patches.create_boot.assert_called_once()
        assert patches.save_state.call_count == 2
        # At the moment main terraform kicked off, bootstrap state was already saved.
        assert save_count_when_execute_ran == [1]

        first_call = patches.save_state.call_args_list[0]
        assert first_call.args[0] == "DEV-02"
        assert first_call.args[1] == []
        assert first_call.args[3] == patches.create_boot.return_value
        assert first_call.kwargs["vm_ips"] == {}
        assert first_call.kwargs["vm_ids"] == {}

    def test_errors_out_when_stale_bootstrap_state_found_without_force(
        self, patches
    ):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
        ]

        with pytest.raises(SystemExit):
            cmd_infra(_args(force=False))

        patches.purge_stale.assert_not_called()
        patches.create_boot.assert_not_called()

    def test_force_purges_stale_bootstrap_state_then_creates(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_stale.return_value = [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
        ]
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]

        cmd_infra(_args(force=True))

        patches.purge_stale.assert_called_once_with(
            "DEV-02", volume_size=20, console=patches.purge_stale.call_args.kwargs["console"]
        )
        patches.create_boot.assert_called_once()


class TestCmdInfraReruns:
    def test_reuses_existing_bootstraps_when_count_matches(self, patches):
        existing_nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ]
        patches.load_state.return_value = {
            "name": "DEV-02",
            "regions": [],
            "terraform_variables": {},
            "bootstrap_nodes": existing_nodes,
            "vm_ips": {},
            "vm_ids": {},
        }

        cmd_infra(_args(bootstrap_count=3))

        patches.create_boot.assert_not_called()
        patches.list_stale.assert_not_called()
        # Initial state save and final state save both use the same nodes
        for call in patches.save_state.call_args_list:
            assert call.args[3] == existing_nodes

    def test_errors_when_existing_bootstrap_count_differs(self, patches):
        existing_nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
        ]
        patches.load_state.return_value = {
            "name": "DEV-02",
            "regions": [],
            "terraform_variables": {},
            "bootstrap_nodes": existing_nodes,
            "vm_ips": {},
            "vm_ids": {},
        }

        with pytest.raises(SystemExit):
            cmd_infra(_args(bootstrap_count=3))

        patches.create_boot.assert_not_called()
        patches.save_state.assert_not_called()

    def test_skips_stale_main_vm_check_when_existing_state_present(
        self, patches
    ):
        existing_nodes = [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ]
        patches.load_state.return_value = {
            "name": "DEV-02",
            "regions": [],
            "terraform_variables": {},
            "bootstrap_nodes": existing_nodes,
            "vm_ips": {},
            "vm_ids": {},
        }

        cmd_infra(_args(bootstrap_count=3))

        patches.check_state.assert_not_called()


class TestCmdInfraAbortsOnStateSaveFailure:
    def test_exits_if_initial_state_save_fails_after_bootstraps(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.create_boot.return_value = [
            {"ip": "1.2.3.4", "region": "lon1", "bootstrap_index": 1},
            {"ip": "5.6.7.8", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "9.10.11.12", "region": "ams3", "bootstrap_index": 3},
        ]
        save_error = RuntimeError("S3 unavailable")
        patches.save_state.side_effect = [save_error, MagicMock()]

        with pytest.raises(SystemExit):
            cmd_infra(_args())

        # Should not proceed to main terraform run when initial save fails
        patches.execute.assert_not_called()
