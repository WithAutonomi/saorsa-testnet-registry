from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.extend import cmd_extend


def _args(**overrides):
    defaults = {"name": "DEV-02", "source": "DEV-01", "force": False}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch(
            "saorsa_deploy.cmd.extend.record_deployment_created"
        ) as p_record_create,
        patch("saorsa_deploy.cmd.extend.record_event") as p_record_event,
        patch(
            "saorsa_deploy.cmd.extend.load_deployment_state"
        ) as p_load_state,
        patch(
            "saorsa_deploy.cmd.extend.save_deployment_state"
        ) as p_save_state,
        patch(
            "saorsa_deploy.cmd.extend.update_deployment_state"
        ) as p_update_state,
    ):
        yield SimpleNamespace(
            record_create=p_record_create,
            record_event=p_record_event,
            load_state=p_load_state,
            save_state=p_save_state,
            update_state=p_update_state,
        )


def _source_state(**overrides):
    state = {
        "name": "DEV-01",
        "bootstrap_nodes": [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1, "port": 10000},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2, "port": 10000},
        ],
        "bootstrap_ip": "1.1.1.1",
        "bootstrap_port": 10000,
        "regions": [["digitalocean", "lon1"]],
        "vm_ips": {"digitalocean/lon1": ["3.3.3.3"]},
        "terraform_variables": {"name": "DEV-01"},
    }
    state.update(overrides)
    return state


def _load_states(source=None, target=None):
    def _side(name):
        if name == "DEV-01":
            if source is None:
                raise RuntimeError("No deployment state")
            return source
        if name == "DEV-02":
            if target is None:
                raise RuntimeError("No deployment state")
            return target
        raise RuntimeError(f"Unexpected name: {name}")
    return _side


class TestHappyPath:
    def test_fresh_extend_saves_state_with_shared_bootstrap_from(self, patches):
        patches.load_state.side_effect = _load_states(source=_source_state())

        cmd_extend(_args())

        patches.save_state.assert_called_once()
        save_kwargs = patches.save_state.call_args.kwargs
        assert save_kwargs["regions"] == []
        assert save_kwargs["vm_ips"] == {}
        assert save_kwargs["vm_ids"] == {}
        # bootstrap_nodes should be copied, not shared
        assert len(save_kwargs["bootstrap_nodes"]) == 2
        assert save_kwargs["bootstrap_nodes"][0]["ip"] == "1.1.1.1"

        # shared_bootstrap_from + bootstrap_port applied via follow-up update
        patches.update_state.assert_called_once()
        followup = patches.update_state.call_args.args[1]
        assert followup["shared_bootstrap_from"] == "DEV-01"
        assert followup["bootstrap_port"] == 10000

        patches.record_create.assert_called_once()
        create_args = patches.record_create.call_args.args
        assert create_args[0] == "DEV-02"
        assert create_args[1]["shared_bootstrap_from"] == "DEV-01"

    def test_extend_without_source_port(self, patches):
        source = _source_state()
        source.pop("bootstrap_port")
        patches.load_state.side_effect = _load_states(source=source)

        cmd_extend(_args())

        patches.update_state.assert_called_once()
        followup = patches.update_state.call_args.args[1]
        assert "bootstrap_port" not in followup
        assert followup["shared_bootstrap_from"] == "DEV-01"


class TestSourceValidation:
    def test_rejects_same_name_as_source(self, patches):
        with pytest.raises(SystemExit):
            cmd_extend(_args(name="DEV-01", source="DEV-01"))

        patches.save_state.assert_not_called()

    def test_rejects_missing_source(self, patches):
        patches.load_state.side_effect = _load_states(source=None)

        with pytest.raises(SystemExit):
            cmd_extend(_args())

        patches.save_state.assert_not_called()

    def test_rejects_source_with_no_bootstraps(self, patches):
        patches.load_state.side_effect = _load_states(
            source=_source_state(bootstrap_nodes=[])
        )

        with pytest.raises(SystemExit):
            cmd_extend(_args())

        patches.save_state.assert_not_called()


class TestIdempotency:
    def test_refuses_to_overwrite_regular_deployment(self, patches):
        target = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "9.9.9.9", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }
        patches.load_state.side_effect = _load_states(
            source=_source_state(), target=target
        )

        with pytest.raises(SystemExit):
            cmd_extend(_args())

        patches.save_state.assert_not_called()
        patches.update_state.assert_not_called()

    def test_refreshes_when_already_extends_same_source(self, patches):
        target = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-01",
            "bootstrap_nodes": [
                {"ip": "old.ip", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [],
        }
        # New source state — say port has changed after redeploy
        source = _source_state(bootstrap_port=10001)
        patches.load_state.side_effect = _load_states(source=source, target=target)

        cmd_extend(_args())

        # Should NOT create a new DB record; just refresh state
        patches.record_create.assert_not_called()
        patches.update_state.assert_called_once()
        updates = patches.update_state.call_args.args[1]
        assert updates["shared_bootstrap_from"] == "DEV-01"
        assert updates["bootstrap_port"] == 10001
        # Should not re-call save_deployment_state (which would wipe regions/vms)
        patches.save_state.assert_not_called()

    def test_rejects_repoint_to_different_source_without_force(self, patches):
        target = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-X",
            "regions": [],
        }
        patches.load_state.side_effect = _load_states(
            source=_source_state(), target=target
        )

        with pytest.raises(SystemExit):
            cmd_extend(_args())

        patches.update_state.assert_not_called()

    def test_allows_repoint_with_force(self, patches):
        target = {
            "name": "DEV-02",
            "shared_bootstrap_from": "DEV-X",
            "regions": [],
        }
        patches.load_state.side_effect = _load_states(
            source=_source_state(), target=target
        )

        cmd_extend(_args(force=True))

        patches.update_state.assert_called_once()
        updates = patches.update_state.call_args.args[1]
        assert updates["shared_bootstrap_from"] == "DEV-01"
