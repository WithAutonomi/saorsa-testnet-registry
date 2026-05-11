from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.prod_continuous_downloader import (
    cmd_prod_continuous_downloader_deploy,
    cmd_prod_continuous_downloader_destroy,
)


def _deploy_args(**overrides):
    defaults = {
        "name": "PROD-DL-01",
        "spec": "lon1",
        "addresses_file": None,
        "branch_name": None,
        "repo_owner": None,
        "ant_version": None,
        "monitoring_volume_size": 50,
        "ssh_key_path": "~/.ssh/id_rsa",
        "purpose": "test",
        "sleep_interval_secs": 900,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _destroy_args(**overrides):
    defaults = {
        "name": "PROD-DL-01",
        "keep_monitoring": False,
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def deploy_patches():
    """Patch every orchestration dependency the deploy wrapper calls out to.

    The provisioner + service-start path was rewired off ``cmd_provision_clients``
    / ``cmd_start_clients`` onto the prod-specific ``_provision_prod_downloader``
    / ``_start_prod_downloader`` helpers when the dedicated ``prod_downloader.py``
    service was introduced. The fixture now patches those helpers directly so
    the test suite continues to assert orchestration order without touching
    pyinfra / SSH.
    """
    with (
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader.record_deployment_created"
        ) as p_create,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader.record_event"
        ) as p_event,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader.record_status"
        ) as p_status,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._monitoring_stack_exists",
            return_value=False,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._client_stack_exists",
            return_value=False,
        ) as p_client_exists,
        patch(
            "saorsa_deploy.cmd.monitoring_infra.cmd_monitoring_infra"
        ) as p_mon_infra,
        patch(
            "saorsa_deploy.cmd.provision_monitoring.cmd_provision_monitoring"
        ) as p_prov_mon,
        patch(
            "saorsa_deploy.cmd.infra_clients.cmd_infra_clients"
        ) as p_infra_clients,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._provision_prod_downloader"
        ) as p_prov_clients,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._start_prod_downloader"
        ) as p_start_clients,
    ):
        yield SimpleNamespace(
            record_create=p_create,
            record_event=p_event,
            record_status=p_status,
            mon_exists=p_mon_exists,
            client_exists=p_client_exists,
            mon_infra=p_mon_infra,
            prov_mon=p_prov_mon,
            infra_clients=p_infra_clients,
            prov_clients=p_prov_clients,
            start_clients=p_start_clients,
        )


class TestDeployHappyPath:
    def test_full_deploy_invokes_each_stage_in_order(self, deploy_patches):
        cmd_prod_continuous_downloader_deploy(_deploy_args())

        deploy_patches.record_create.assert_called_once()
        deploy_patches.mon_infra.assert_called_once()
        deploy_patches.prov_mon.assert_called_once()
        deploy_patches.infra_clients.assert_called_once()
        deploy_patches.prov_clients.assert_called_once()
        deploy_patches.start_clients.assert_called_once()

    def test_deploy_records_running_status_on_success(self, deploy_patches):
        cmd_prod_continuous_downloader_deploy(_deploy_args())

        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "running"

    def test_deploy_skips_monitoring_when_already_provisioned(self, deploy_patches):
        deploy_patches.mon_exists.return_value = True

        cmd_prod_continuous_downloader_deploy(_deploy_args())

        deploy_patches.mon_infra.assert_not_called()
        deploy_patches.prov_mon.assert_not_called()
        deploy_patches.infra_clients.assert_called_once()
        deploy_patches.prov_clients.assert_called_once()
        deploy_patches.start_clients.assert_called_once()

    def test_deploy_skips_infra_clients_when_client_state_exists(self, deploy_patches):
        deploy_patches.client_exists.return_value = True

        cmd_prod_continuous_downloader_deploy(_deploy_args())

        deploy_patches.infra_clients.assert_not_called()
        deploy_patches.prov_clients.assert_called_once()
        deploy_patches.start_clients.assert_called_once()

    def test_deploy_passes_addresses_file_to_provisioner(
        self, deploy_patches, tmp_path
    ):
        custom_file = tmp_path / "my_addrs.txt"
        custom_file.write_text("abc\n")

        cmd_prod_continuous_downloader_deploy(
            _deploy_args(addresses_file=str(custom_file))
        )

        # _provision_prod_downloader(args, addresses_file, console)
        addresses_file_arg = deploy_patches.prov_clients.call_args.args[1]
        assert addresses_file_arg == str(custom_file)

    def test_deploy_uses_shipped_default_addresses_when_flag_absent(self, deploy_patches):
        cmd_prod_continuous_downloader_deploy(_deploy_args())

        addresses_file_arg = deploy_patches.prov_clients.call_args.args[1]
        assert addresses_file_arg.endswith("prod_addresses.txt")

    def test_deploy_records_kind_in_deploy_args(self, deploy_patches):
        cmd_prod_continuous_downloader_deploy(_deploy_args())

        payload = deploy_patches.record_create.call_args.args[1]
        assert payload["kind"] == "prod-continuous-downloader"
        assert payload["spec"] == "lon1"

    def test_deploy_passes_spec_as_single_downloader_to_infra_clients(
        self, deploy_patches
    ):
        cmd_prod_continuous_downloader_deploy(
            _deploy_args(spec="nyc1:s-4vcpu-8gb")
        )

        infra_args = deploy_patches.infra_clients.call_args.args[0]
        assert infra_args.uploader == []
        assert infra_args.downloader == ["nyc1:s-4vcpu-8gb"]


class TestDeployFailure:
    def test_deploy_records_failed_status_when_underlying_stage_exits(
        self, deploy_patches
    ):
        deploy_patches.prov_clients.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            cmd_prod_continuous_downloader_deploy(_deploy_args())

        deploy_patches.record_status.assert_called()
        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"



@pytest.fixture
def destroy_patches():
    with (
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader.record_event"
        ) as p_event,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader.record_status"
        ) as p_status,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._client_stack_exists",
            return_value=True,
        ) as p_client_exists,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._monitoring_stack_exists",
            return_value=True,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._stop_prod_downloader"
        ) as p_stop,
        patch(
            "saorsa_deploy.cmd.destroy_clients.cmd_destroy_clients"
        ) as p_destroy_clients,
        patch(
            "saorsa_deploy.cmd.destroy_monitoring.cmd_destroy_monitoring"
        ) as p_destroy_mon,
    ):
        yield SimpleNamespace(
            record_event=p_event,
            record_status=p_status,
            client_exists=p_client_exists,
            mon_exists=p_mon_exists,
            stop=p_stop,
            destroy_clients=p_destroy_clients,
            destroy_mon=p_destroy_mon,
        )


class TestDestroy:
    def test_default_destroy_tears_down_monitoring(self, destroy_patches):
        cmd_prod_continuous_downloader_destroy(_destroy_args())

        destroy_patches.stop.assert_called_once()
        destroy_patches.destroy_clients.assert_called_once()
        destroy_patches.destroy_mon.assert_called_once()

        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "destroyed"

    def test_keep_monitoring_flag_preserves_monitoring(self, destroy_patches):
        cmd_prod_continuous_downloader_destroy(
            _destroy_args(keep_monitoring=True)
        )

        destroy_patches.destroy_clients.assert_called_once()
        destroy_patches.destroy_mon.assert_not_called()

    def test_default_destroy_skips_monitoring_when_stack_is_missing(
        self, destroy_patches
    ):
        destroy_patches.mon_exists.return_value = False

        cmd_prod_continuous_downloader_destroy(_destroy_args())

        destroy_patches.destroy_mon.assert_not_called()

    def test_destroy_skips_client_steps_when_no_client_state(self, destroy_patches):
        destroy_patches.client_exists.return_value = False

        cmd_prod_continuous_downloader_destroy(_destroy_args())

        destroy_patches.stop.assert_not_called()
        destroy_patches.destroy_clients.assert_not_called()

    def test_destroy_tolerates_stop_clients_systemexit(self, destroy_patches):
        destroy_patches.stop.side_effect = SystemExit(1)

        cmd_prod_continuous_downloader_destroy(_destroy_args())

        destroy_patches.destroy_clients.assert_called_once()
        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "destroyed"

    def test_destroy_tolerates_stop_clients_runtime_error(self, destroy_patches):
        # Regression: Pyinfra raises RuntimeError("No hosts remaining!") when a
        # half-provisioned VM has no ant-downloader.service yet. That must NOT
        # block `clients destroy` from tearing the Terraform VMs down.
        destroy_patches.stop.side_effect = RuntimeError("No hosts remaining!")

        cmd_prod_continuous_downloader_destroy(_destroy_args())

        destroy_patches.destroy_clients.assert_called_once()
        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "destroyed"

    def test_destroy_records_failed_on_destroy_clients_error(self, destroy_patches):
        destroy_patches.destroy_clients.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            cmd_prod_continuous_downloader_destroy(_destroy_args())

        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"
