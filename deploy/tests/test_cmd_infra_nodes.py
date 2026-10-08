from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.infra_nodes import cmd_infra_nodes
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {
        "name": "DEV-02",
        "attached_volume_size": 20,
        "do_vm_count": 1,
        "do_regions": 1,
        "do_regions_list": None,
        "do_droplet_size": "s-2vcpu-4gb",
        "hetzner_vm_count": 0,
        "hetzner_regions": 0,
        "hetzner_regions_list": None,
        "hetzner_server_type": "cpx22",
        "vultr_vm_count": 0,
        "vultr_regions": 0,
        "vultr_regions_list": None,
        "vultr_plan": "vc2-2c-4gb",
        "ovh_vm_count": 0,
        "ovh_regions": 0,
        "ovh_regions_list": None,
        "ovh_flavor": "b3-16",
        "ovh_3az_vm_count": 0,
        "ovh_3az_regions": 1,
        "ovh_3az_regions_list": None,
        "ovh_3az_flavor": "b3-16",
        "do_attached_volume_size": None,
        "hetzner_attached_volume_size": None,
        "vultr_attached_volume_size": None,
        "ovh_attached_volume_size": None,
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _state_with_bootstraps():
    return {
        "name": "DEV-02",
        "bootstrap_nodes": [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ],
        "regions": [],
        "vm_ips": {},
        "vm_ids": {},
    }


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.infra_nodes.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.infra_nodes.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.infra_nodes.load_deployment_state") as p_load_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.update_deployment_state"
        ) as p_update_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.check_terraform_state_exists"
        ) as p_check_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.delete_terraform_state"
        ) as p_delete_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.execute_terraform_runs"
        ) as p_execute,
    ):
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
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            update_state=p_update_state,
            check_state=p_check_state,
            delete_state=p_delete_state,
            execute=p_execute,
        )


class TestRefusesWithoutBootstraps:
    def test_errors_when_no_deployment_state(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args())

        patches.execute.assert_not_called()

    def test_errors_when_state_has_no_bootstrap_nodes(self, patches):
        patches.load_state.return_value = {"bootstrap_nodes": []}

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args())

        patches.execute.assert_not_called()


class TestRegionsOverride:

    def test_unknown_region_in_override_errors_out(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()

        with pytest.raises(SystemExit):
            cmd_infra_nodes(
                _args(
                    do_vm_count=5,
                    do_regions_list="lon1,bogus-region",
                )
            )

        patches.execute.assert_not_called()


OVH_ENV = {
    "OVH_APPLICATION_KEY": "ak",
    "OVH_APPLICATION_SECRET": "as",
    "OVH_CONSUMER_KEY": "ck",
    "OVH_CLOUD_PROJECT_SERVICE": "project-id",
}


class TestOvhProvider:
    def test_missing_env_vars_error_out_before_terraform(self, patches, monkeypatch):
        patches.load_state.return_value = _state_with_bootstraps()
        for var in OVH_ENV:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit):
            cmd_infra_nodes(
                _args(do_vm_count=0, ovh_vm_count=2, ovh_regions_list="GRA9,UK1")
            )

        patches.execute.assert_not_called()
