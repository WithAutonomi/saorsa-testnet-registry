from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.cmd.destroy_bootstraps import cmd_destroy_bootstraps


def _args(**overrides):
    defaults = {"name": "DEV-02", "force": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.destroy_bootstraps.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.destroy_bootstraps.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.destroy_bootstraps.load_deployment_state") as p_load_state,
        patch("saorsa_deploy.cmd.destroy_bootstraps.update_deployment_state") as p_update_state,
        patch("saorsa_deploy.cmd.destroy_bootstraps.delete_deployment_state") as p_delete_state,
        patch("saorsa_deploy.cmd.destroy_bootstraps.destroy_bootstrap_vms") as p_destroy_boot,
        patch(
            "saorsa_deploy.cmd.destroy_bootstraps.list_stale_bootstrap_state_keys"
        ) as p_list_stale,
        patch("saorsa_deploy.cmd.destroy_bootstraps.purge_stale_bootstrap_state") as p_purge_stale,
        patch("saorsa_deploy.cmd.destroy_bootstraps.list_extensions_of") as p_list_exts,
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


class TestStatusFlip:
    """`bootstraps destroy` is the last step of a full testnet teardown, so it is
    what must flip the row to DESTROYED. Leaving it at DESTROYING makes a clean
    teardown indistinguishable from one that stalled with live infrastructure."""

    @staticmethod
    def _statuses(patches):
        return [c.args[1] for c in patches.record_status.call_args_list]

    def test_records_destroyed_when_whole_state_file_removed(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
            "terraform_variables": {"attached_volume_size": "50"},
        }

        cmd_destroy_bootstraps(_args())

        assert self._statuses(patches) == [
            DeploymentStatus.DESTROYING,
            DeploymentStatus.DESTROYED,
        ]

    def test_does_not_record_destroyed_when_state_deletion_fails(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
            "terraform_variables": {"attached_volume_size": "50"},
        }
        patches.delete_state.side_effect = Exception("s3 unavailable")

        cmd_destroy_bootstraps(_args())

        assert DeploymentStatus.DESTROYED not in self._statuses(patches)


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


class TestClientExtensionGuard:
    """The separate guard for CLIENT extensions (`kind: clients`).

    Distinct from the node-extension guard above: a client extension has no
    deployment state of its own (so `list_extensions_of` cannot see it), and it
    is `list_client_extensions` that reports one. Both refuse, neither is
    overridden by `--force`.

    The scan answers "none" for every test via the `no_client_extensions`
    autouse fixture in conftest, so these two set their own value over it.
    """

    def test_refuses_while_a_client_extension_stands(self, patches, monkeypatch):
        from saorsa_deploy import state as state_mod

        monkeypatch.setattr(state_mod, "list_client_extensions", lambda name: ["browsers"])

        with pytest.raises(SystemExit) as exc:
            cmd_destroy_bootstraps(_args(name="DEV-01"))

        assert exc.value.code == 2
        # Refused before the state load, so the orphan-tfstate path is covered.
        patches.load_state.assert_not_called()
        patches.destroy_boot.assert_not_called()
        patches.delete_state.assert_not_called()

    def test_refuses_when_the_scan_cannot_complete(self, patches, monkeypatch):
        # "Could not look" is not "nothing there", and a teardown cannot be
        # undone. This is the case that was reaching real S3 and failing CI.
        from saorsa_deploy import state as state_mod

        def boom(name):
            raise OSError("Unable to locate credentials")

        monkeypatch.setattr(state_mod, "list_client_extensions", boom)

        with pytest.raises(SystemExit) as exc:
            cmd_destroy_bootstraps(_args(name="DEV-01"))

        assert exc.value.code == 2
        patches.destroy_boot.assert_not_called()


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


class TestRetiresTheLocalSnapshot:
    """`bootstraps destroy` is the last tier of a full teardown, so this is
    where a testnet's local snapshot is usually retired."""

    @staticmethod
    def _snapshot(root, name="DEV-02"):
        path = root / name / "inventory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"testnet_id": "{name}", "created_at": "2026-10-03T21:13:45Z"}}')
        return path

    def test_retires_it_when_the_state_file_goes(self, patches, local_inventory_root):
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}],
            "regions": [],
            "terraform_variables": {"attached_volume_size": "50"},
        }

        cmd_destroy_bootstraps(_args())

        patches.delete_state.assert_called_once()
        assert not path.exists()
        assert any(p.name.startswith("inventory.json.destroyed-") for p in path.parent.iterdir())

    def test_retires_it_when_an_empty_state_is_removed(self, patches, local_inventory_root):
        """No bootstraps recorded and no orphan tfstate: the state file is
        removed outright, which is a full teardown too."""
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [],
            "terraform_variables": {},
        }

        cmd_destroy_bootstraps(_args())

        patches.delete_state.assert_called_once()
        assert not path.exists()

    def test_keeps_it_when_the_state_deletion_fails(self, patches, local_inventory_root):
        """The registry still calls the deployment DESTROYING, so the snapshot
        is not yet evidence of a finished teardown."""
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}],
            "regions": [],
            "terraform_variables": {},
        }
        patches.delete_state.side_effect = Exception("s3 unavailable")

        cmd_destroy_bootstraps(_args())

        assert path.exists()
