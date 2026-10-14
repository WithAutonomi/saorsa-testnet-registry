from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.unextend import cmd_unextend


def _args(**overrides):
    defaults = {"name": "DEV-02", "force": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.unextend.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.unextend.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.unextend.load_deployment_state") as p_load_state,
        patch("saorsa_deploy.cmd.unextend.delete_deployment_state") as p_delete_state,
        patch("saorsa_deploy.cmd.unextend.load_client_state") as p_load_client_state,
    ):
        # Most extensions have no client tier; absence is a RuntimeError.
        p_load_client_state.side_effect = RuntimeError("No client state")
        yield SimpleNamespace(
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            delete_state=p_delete_state,
            load_client_state=p_load_client_state,
        )


class TestHappyPath:
    def test_deletes_state_when_extension_has_no_nodes(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }

        cmd_unextend(_args())

        patches.delete_state.assert_called_once_with("DEV-02")


class TestValidation:
    def test_refuses_when_state_missing(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state")

        with pytest.raises(SystemExit):
            cmd_unextend(_args())

        patches.delete_state.assert_not_called()

    def test_refuses_when_not_an_extension(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }

        with pytest.raises(SystemExit):
            cmd_unextend(_args())

        patches.delete_state.assert_not_called()

    def test_refuses_when_nodes_still_present(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [["digitalocean", "lon1"]],
        }

        with pytest.raises(SystemExit):
            cmd_unextend(_args())

        patches.delete_state.assert_not_called()

    def test_refuses_when_clients_still_present(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }
        patches.load_client_state.side_effect = None
        patches.load_client_state.return_value = {
            "clients": [{"role": "upload", "region": "syd", "client_index": 1}],
        }

        with pytest.raises(SystemExit):
            cmd_unextend(_args())

        patches.delete_state.assert_not_called()

    def test_empty_client_state_does_not_block(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }
        patches.load_client_state.side_effect = None
        patches.load_client_state.return_value = {"clients": []}

        cmd_unextend(_args())

        patches.delete_state.assert_called_once_with("DEV-02")


class TestRetiresTheLocalSnapshot:
    """`unextend` is the last step of an extension's teardown — `nodes destroy`
    released its node VMs before it ran, so the snapshot names freed IPs."""

    @staticmethod
    def _snapshot(root, name="DEV-02"):
        path = root / name / "inventory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"testnet_id": "{name}", "created_at": "2026-10-03T21:13:45Z"}}')
        return path

    def _extension_state(self):
        return {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}],
            "regions": [],
        }

    def test_retires_it_after_the_state_is_deleted(self, patches, local_inventory_root):
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = self._extension_state()

        cmd_unextend(_args())

        assert not path.exists()
        assert any(p.name.startswith("inventory.json.destroyed-") for p in path.parent.iterdir())

    def test_never_touches_the_sources_snapshot(self, patches, local_inventory_root):
        source = self._snapshot(local_inventory_root, "DEV-01")
        patches.load_state.return_value = self._extension_state()

        cmd_unextend(_args())

        assert source.exists()

    def test_keeps_it_when_the_state_deletion_fails(self, patches, local_inventory_root):
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = self._extension_state()
        patches.delete_state.side_effect = Exception("s3 unavailable")

        with pytest.raises(SystemExit):
            cmd_unextend(_args())

        assert path.exists()
