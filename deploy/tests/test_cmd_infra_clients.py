from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.infra_clients import cmd_infra_clients
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {
        "name": "DEV-02",
        "client": ["lon1:50", "nyc3:50"],
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch(
            "saorsa_deploy.cmd.infra_clients.check_terraform_state_exists"
        ) as p_check,
        patch(
            "saorsa_deploy.cmd.infra_clients.delete_terraform_state"
        ) as p_delete,
        patch(
            "saorsa_deploy.cmd.infra_clients.save_client_state"
        ) as p_save,
        patch(
            "saorsa_deploy.cmd.infra_clients.update_client_state"
        ) as p_update,
        patch(
            "saorsa_deploy.cmd.infra_clients.execute_terraform_runs"
        ) as p_execute,
    ):
        p_check.return_value = False
        p_execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ip": "10.0.0.1"},
            ),
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="nyc3",
                outputs={"vm_ip": "10.0.0.2"},
            ),
        ]
        yield SimpleNamespace(
            check=p_check,
            delete=p_delete,
            save=p_save,
            update=p_update,
            execute=p_execute,
        )


class TestUpfrontStateSave:
    def test_saves_state_before_terraform_runs(self, patches):
        save_count_when_execute_ran = []

        def _spy_on_execute(*_a, **_kw):
            save_count_when_execute_ran.append(patches.save.call_count)
            return patches.execute.return_value

        patches.execute.side_effect = _spy_on_execute

        cmd_infra_clients(_args())

        assert save_count_when_execute_ran == [1]
        patches.save.assert_called_once()
        # Upfront save uses empty IPs — real values land via update after apply
        upfront_specs = patches.save.call_args.args[1]
        assert all(spec["ip"] == "" for spec in upfront_specs)

    def test_updates_ips_on_partial_failure(self, patches):
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ip": "10.0.0.1"},
            ),
            TerraformResult(
                success=False,
                provider="digitalocean",
                region="nyc3",
                stderr="Size is not available in this region.",
            ),
        ]

        with pytest.raises(SystemExit):
            cmd_infra_clients(_args())

        # Upfront save happened, and an update call merged the IP for lon1
        # (the region that succeeded) before we exited.
        patches.save.assert_called_once()
        patches.update.assert_called_once()
        updated_clients = patches.update.call_args.args[1]["clients"]
        by_region = {c["region"]: c["ip"] for c in updated_clients}
        assert by_region == {"lon1": "10.0.0.1", "nyc3": ""}

    def test_aborts_if_initial_save_fails(self, patches):
        patches.save.side_effect = RuntimeError("S3 unavailable")

        with pytest.raises(SystemExit):
            cmd_infra_clients(_args())

        patches.execute.assert_not_called()


class TestNoClients:
    def test_rejects_empty_client_list(self, patches):
        with pytest.raises(SystemExit):
            cmd_infra_clients(_args(client=None))

        patches.execute.assert_not_called()
        patches.save.assert_not_called()
