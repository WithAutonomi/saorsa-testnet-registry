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
        "addresses_path": None,
        "branch_name": None,
        "repo_owner": None,
        "ant_version": None,
        "monitoring_volume_size": 50,
        "ssh_key_path": "~/.ssh/id_rsa",
        "purpose": "test",
        "daily_at": "00:00",
        "provider": "digitalocean",
        "monitoring_provider": "digitalocean",
        "monitoring_region": None,
        "monitoring_droplet_size": None,
        "monitoring_volume_type": None,
        "influx_retention_days": 7,
        "log_retention_days": 7,
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
        patch("saorsa_deploy.cmd.prod_continuous_downloader.record_deployment_created") as p_create,
        patch("saorsa_deploy.cmd.prod_continuous_downloader.record_event") as p_event,
        patch("saorsa_deploy.cmd.prod_continuous_downloader.record_status") as p_status,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._monitoring_stack_exists",
            return_value=False,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._monitoring_has_influx",
            return_value=True,
        ) as p_mon_has_influx,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._client_stack_exists",
            return_value=False,
        ) as p_client_exists,
        patch("saorsa_deploy.cmd.monitoring_infra.cmd_monitoring_infra") as p_mon_infra,
        patch("saorsa_deploy.cmd.provision_monitoring.cmd_provision_monitoring") as p_prov_mon,
        patch("saorsa_deploy.cmd.infra_clients.cmd_infra_clients") as p_infra_clients,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._provision_prod_downloader"
        ) as p_prov_clients,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._start_prod_downloader"
        ) as p_start_clients,
        # The deploy's final step is a best-effort client-telegraf provision,
        # which reads monitoring state — a real s3.GetObject. It is swallowed,
        # so these tests passed either way, but they were the last two in the
        # suite making a live AWS call. Patched here, not asserted on: what the
        # tests are about is the recorded cadence.
        patch("saorsa_deploy.cmd.provision_telegraf_metrics.provision_client_telegraf_best_effort"),
    ):
        yield SimpleNamespace(
            record_create=p_create,
            record_event=p_event,
            record_status=p_status,
            mon_exists=p_mon_exists,
            mon_has_influx=p_mon_has_influx,
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
        # User pasted a full `ant file download <addr> -o name` line.
        custom_file.write_text("ant file download " + "a" * 64 + " -o foo.bin\n")

        with pytest.raises(SystemExit):
            cmd_prod_continuous_downloader_deploy(_deploy_args(addresses_path=str(custom_file)))


class TestTheRecordedCadence:
    """What `deploy_args` says the cadence is, which every reader works from."""

    def test_deploy_args_carry_the_anchor_and_not_the_retired_interval(self, deploy_patches):
        cmd_prod_continuous_downloader_deploy(_deploy_args(daily_at="02:00"))
        payload = deploy_patches.record_create.call_args.args[1]
        assert payload["daily_at"] == "02:00"
        assert "sleep_interval_secs" not in payload

    def test_the_anchor_defaults_to_midnight_when_the_flag_is_absent(self, deploy_patches):
        """The one-shot kind reuses this module and never defines the flag."""
        args = _deploy_args()
        del args.daily_at
        cmd_prod_continuous_downloader_deploy(args)
        assert deploy_patches.record_create.call_args.args[1]["daily_at"] == "00:00"


class TestDeployFailure:
    def test_deploy_records_failed_status_when_underlying_stage_exits(self, deploy_patches):
        deploy_patches.prov_clients.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            cmd_prod_continuous_downloader_deploy(_deploy_args())

        deploy_patches.record_status.assert_called()
        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"


@pytest.fixture
def destroy_patches():
    with (
        patch("saorsa_deploy.cmd.prod_continuous_downloader.record_event") as p_event,
        patch("saorsa_deploy.cmd.prod_continuous_downloader.record_status") as p_status,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._client_stack_exists",
            return_value=True,
        ) as p_client_exists,
        patch(
            "saorsa_deploy.cmd.prod_continuous_downloader._monitoring_stack_exists",
            return_value=True,
        ) as p_mon_exists,
        patch("saorsa_deploy.cmd.prod_continuous_downloader._stop_prod_downloader") as p_stop,
        patch("saorsa_deploy.cmd.destroy_clients.cmd_destroy_clients") as p_destroy_clients,
        patch("saorsa_deploy.cmd.destroy_monitoring.cmd_destroy_monitoring") as p_destroy_mon,
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
    def test_destroy_records_failed_on_destroy_clients_error(self, destroy_patches):
        destroy_patches.destroy_clients.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            cmd_prod_continuous_downloader_destroy(_destroy_args())

        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"
