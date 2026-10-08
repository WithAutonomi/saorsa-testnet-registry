from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.infra_clients import cmd_infra_clients, parse_uploader_spec
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {
        # Deliberately not a real deployment name — these are unit tests and
        # load_client_state is mocked, so this never touches S3.
        "name": "UNITTEST-CLIENTS",
        "uploader": ["digital_ocean:lon1:50", "digital_ocean:nyc3:50"],
        "downloader": [],
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.infra_clients.check_terraform_state_exists") as p_check,
        patch("saorsa_deploy.cmd.infra_clients.delete_terraform_state") as p_delete,
        patch("saorsa_deploy.cmd.infra_clients.save_client_state") as p_save,
        patch("saorsa_deploy.cmd.infra_clients.update_client_state") as p_update,
        patch("saorsa_deploy.cmd.infra_clients.execute_terraform_runs") as p_execute,
        patch("saorsa_deploy.cmd.infra_clients.load_client_state") as p_load,
    ):
        p_check.return_value = False
        # Simulate a fresh deployment with no pre-existing client state, so the
        # idempotent-reuse path is skipped. (Hermetic: never reads real S3.)
        p_load.side_effect = RuntimeError("no client state")
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
            load=p_load,
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
            cmd_infra_clients(_args(uploader=[], downloader=[]))

        patches.execute.assert_not_called()
        patches.save.assert_not_called()


class TestPartialFailureResume:
    """A partial `clients infra` failure must be resumable.

    A provider capacity failure leaves state recording every requested VM but
    with an empty IP for the ones that never came up. Re-running must create
    only those, not refuse — recovering two VMs by destroying a healthy client
    tier churns the whole workload and changes what the run measures.
    """

    def _partial_state(self):
        """State as `clients infra` writes it when the second VM fails.

        Built through the real spec parser rather than hand-written, so it
        carries every field the signature match compares.
        """
        clients = []
        for i, raw in enumerate(_args().uploader, start=1):
            spec = parse_uploader_spec(raw)
            spec["client_index"] = i
            spec["ip"] = "10.0.0.1" if i == 1 else ""
            clients.append(spec)
        return {"clients": clients}

    def test_creates_only_the_missing_vm(self, patches):
        patches.load.side_effect = None
        patches.load.return_value = self._partial_state()
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="nyc3",
                outputs={"vm_ip": "10.0.0.2"},
            ),
        ]

        cmd_infra_clients(_args())

        # Exactly one terraform run, and it is the VM that was missing.
        configs = patches.execute.call_args.args[0]
        assert [c.region for c in configs] == ["nyc3"]

    def test_resume_preserves_the_surviving_ip(self, patches):
        patches.load.side_effect = None
        patches.load.return_value = self._partial_state()
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="nyc3",
                outputs={"vm_ip": "10.0.0.2"},
            ),
        ]

        cmd_infra_clients(_args())

        # The upfront save must not blank the VM that is still running — that
        # state is what `clients destroy` uses to find it.
        upfront = {c["region"]: c["ip"] for c in patches.save.call_args.args[1]}
        assert upfront == {"lon1": "10.0.0.1", "nyc3": ""}

        final = {c["region"]: c["ip"] for c in patches.update.call_args.args[1]["clients"]}
        assert final == {"lon1": "10.0.0.1", "nyc3": "10.0.0.2"}

    def test_resume_does_not_treat_own_state_as_stale(self, patches):
        # The failed apply leaves a Terraform state file for that VM. It is not
        # stale — it is that VM's own state — so the resume must not demand
        # --force to delete it.
        patches.load.side_effect = None
        patches.load.return_value = self._partial_state()
        patches.check.return_value = True
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="nyc3",
                outputs={"vm_ip": "10.0.0.2"},
            ),
        ]

        cmd_infra_clients(_args(force=False))

        patches.delete.assert_not_called()
        patches.execute.assert_called_once()

    def test_full_state_still_short_circuits(self, patches):
        state = self._partial_state()
        state["clients"][1]["ip"] = "10.0.0.2"
        patches.load.side_effect = None
        patches.load.return_value = state

        cmd_infra_clients(_args())

        patches.execute.assert_not_called()
        patches.save.assert_not_called()

    def test_genuine_spec_mismatch_still_refuses(self, patches):
        state = self._partial_state()
        state["clients"].pop()
        patches.load.side_effect = None
        patches.load.return_value = state

        with pytest.raises(SystemExit):
            cmd_infra_clients(_args())

        patches.execute.assert_not_called()
