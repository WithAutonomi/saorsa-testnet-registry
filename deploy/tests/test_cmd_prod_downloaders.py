from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.prod_downloaders import (
    cmd_prod_downloaders_deploy,
)


def _deploy_args(**overrides):
    defaults = {
        "name": "PROD-DL-OS-01",
        "spec": "lon1",
        "addresses_path": None,
        "branch_name": None,
        "repo_owner": None,
        "ant_version": None,
        "monitoring_volume_size": 50,
        "ssh_key_path": "~/.ssh/id_rsa",
        "purpose": "test",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def deploy_patches():
    with (
        patch("saorsa_deploy.cmd.prod_downloaders.record_deployment_created") as p_create,
        patch("saorsa_deploy.cmd.prod_downloaders.record_event") as p_event,
        patch("saorsa_deploy.cmd.prod_downloaders.record_status") as p_status,
        patch(
            "saorsa_deploy.cmd.prod_downloaders._monitoring_stack_exists",
            return_value=False,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_downloaders._client_stack_exists",
            return_value=False,
        ) as p_client_exists,
        patch("saorsa_deploy.cmd.monitoring_infra.cmd_monitoring_infra") as p_mon_infra,
        patch("saorsa_deploy.cmd.provision_monitoring.cmd_provision_monitoring") as p_prov_mon,
        patch("saorsa_deploy.cmd.infra_clients.cmd_infra_clients") as p_infra_clients,
        patch("saorsa_deploy.cmd.prod_downloaders._provision_prod_downloader") as p_prov_clients,
        patch("saorsa_deploy.cmd.prod_downloaders._start_prod_downloader") as p_start_clients,
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
    def test_deploy_rejects_addresses_file_with_extra_tokens(self, deploy_patches, tmp_path):
        custom_file = tmp_path / "addrs.txt"
        custom_file.write_text("ant file download " + "a" * 64 + " -o foo.bin\n")

        with pytest.raises(SystemExit):
            cmd_prod_downloaders_deploy(_deploy_args(addresses_path=str(custom_file)))


class TestDeployFailure:
    def test_deploy_records_failed_status_when_underlying_stage_exits(self, deploy_patches):
        deploy_patches.prov_clients.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            cmd_prod_downloaders_deploy(_deploy_args())

        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"
