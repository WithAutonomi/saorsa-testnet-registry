"""Tests for the parallel (multi-host) bootstrap provisioner.

`AntBootstrapNodesProvisioner` provisions all additional bootstrap nodes
(index 2+) in a single pyinfra session — mirroring `AntNodeProvisioner` — so
pyinfra runs them concurrently and a live per-host progress table is shown.

These tests mock the pyinfra surface (Inventory/State/connect_all/add_op/
run_ops/disconnect_all) and the progress-handler factory, then assert the
op layout: host-agnostic ops added once for the fleet, host-specific ops
targeted with `host=[...]` and the correct `ant-bootstrap-{index}` identity.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyinfra.operations import server, systemd

from saorsa_deploy.provisioning.bootstrap import AntBootstrapNodesProvisioner


@pytest.fixture(autouse=True)
def _mock_operator_authorized_keys_add_op():
    """Neutralize the operator-SSH-keys op's internal ``add_op``.

    ``execute()`` calls ``add_operator_authorized_keys_op`` (added in
    ``feat: install operator SSH keys on every VM``), which invokes the real
    pyinfra ``add_op`` from ``provisioning.authorized_keys`` — a module these
    tests don't patch — making pyinfra raise "Operation called within another
    operation" against the mocked State. Patch that helper's ``add_op`` the same
    way these tests already patch the bootstrap module's ``add_op``.
    """
    with patch("saorsa_deploy.provisioning.authorized_keys.add_op"):
        yield


def _fake_host(ip):
    return SimpleNamespace(name=ip)


def _patched_execute(provisioner, *, active_ips, failed_ips=None):
    """Run ``execute`` with the pyinfra surface mocked.

    Returns the patched ``add_op`` mock and the ``Inventory`` mock for
    assertions. ``active_ips`` become the hosts pyinfra reports connected;
    ``failed_ips`` (names only) populate ``state.failed_hosts``.
    """
    failed_ips = failed_ips or []
    with (
        patch("saorsa_deploy.provisioning.bootstrap.Inventory") as mock_inventory,
        patch("saorsa_deploy.provisioning.bootstrap.State") as mock_state_cls,
        patch("saorsa_deploy.provisioning.bootstrap.connect_all"),
        patch("saorsa_deploy.provisioning.bootstrap.add_op") as mock_add_op,
        patch("saorsa_deploy.provisioning.bootstrap.run_ops"),
        patch("saorsa_deploy.provisioning.bootstrap.disconnect_all"),
        patch("saorsa_deploy.provisioning.bootstrap.get_release_url") as mock_release,
        patch(
            "saorsa_deploy.provisioning.bootstrap.create_progress_handler"
        ) as mock_handler,
    ):
        mock_release.return_value = "https://example/ant-node.tar.gz"
        # A plain MagicMock is not a RichLiveProgressHandler, so the Live
        # start/stop/mark_all_done branches are skipped.
        mock_handler.return_value = MagicMock()
        state = mock_state_cls.return_value
        state.inventory.get_active_hosts.return_value = [
            _fake_host(ip) for ip in active_ips
        ]
        state.failed_hosts = [_fake_host(ip) for ip in failed_ips]
        provisioner.execute()
        return mock_add_op, mock_inventory


def _ops_by_name(mock_add_op):
    """Map op `name` -> the call object, for every add_op call."""
    out = {}
    for call in mock_add_op.call_args_list:
        out[call.kwargs.get("name")] = call
    return out


class TestAntBootstrapNodesProvisioner:
    def _provisioner(self, **overrides):
        kwargs = dict(
            nodes=[
                {"ip": "10.0.0.2", "bootstrap_index": 2, "region": "lon1"},
                {"ip": "10.0.0.3", "bootstrap_index": 3, "region": "ams3"},
            ],
            bootstrap_peers=["10.0.0.1:10000"],
            testnet_name="DEV-01",
            port=10000,
            testnet=True,
        )
        kwargs.update(overrides)
        return AntBootstrapNodesProvisioner(**kwargs)

    def test_single_inventory_holds_all_hosts(self):
        prov = self._provisioner()
        _, mock_inventory = _patched_execute(
            prov, active_ips=["10.0.0.2", "10.0.0.3"]
        )
        # Exactly one Inventory built (one pyinfra session, not one per host).
        assert mock_inventory.call_count == 1
        hosts_data = mock_inventory.call_args[0][0][0]
        ips = [entry[0] for entry in hosts_data]
        assert ips == ["10.0.0.2", "10.0.0.3"]

    def test_shared_ops_added_once_without_host_targeting(self):
        prov = self._provisioner()
        mock_add_op, _ = _patched_execute(
            prov, active_ips=["10.0.0.2", "10.0.0.3"]
        )
        by_name = _ops_by_name(mock_add_op)
        for shared in (
            "Disable Ubuntu unattended-upgrades",
            "Mount attached storage volume",
            "Download and install ant-node binary",
            "Reload systemd daemon",
        ):
            assert shared in by_name, f"missing shared op {shared!r}"
            # Fleet-wide ops do not pin a host subset.
            assert "host" not in by_name[shared].kwargs

    def test_per_host_config_op_targets_each_host(self):
        prov = self._provisioner()
        mock_add_op, _ = _patched_execute(
            prov, active_ips=["10.0.0.2", "10.0.0.3"]
        )
        by_name = _ops_by_name(mock_add_op)
        for ip, idx in (("10.0.0.2", 2), ("10.0.0.3", 3)):
            name = f"Configure ant-bootstrap-{idx}"
            assert name in by_name
            call = by_name[name]
            assert call[0][1] is server.shell
            # Targeted at exactly this host.
            hosts = call.kwargs["host"]
            assert [h.name for h in hosts] == [ip]
            # Writes that host's own unit file + log dir.
            joined = "\n".join(call.kwargs["commands"])
            assert f"ant-bootstrap-{idx}.service" in joined
            assert f"ant-bootstrap-{idx}" in joined

    def test_per_host_service_op_enables_and_starts_correct_service(self):
        prov = self._provisioner()
        mock_add_op, _ = _patched_execute(
            prov, active_ips=["10.0.0.2", "10.0.0.3"]
        )
        by_name = _ops_by_name(mock_add_op)
        for ip, idx in (("10.0.0.2", 2), ("10.0.0.3", 3)):
            name = f"Enable and start ant-bootstrap-{idx}"
            assert name in by_name
            call = by_name[name]
            assert call[0][1] is systemd.service
            assert call.kwargs["service"] == f"ant-bootstrap-{idx}"
            assert call.kwargs["running"] is True
            assert call.kwargs["enabled"] is True
            assert [h.name for h in call.kwargs["host"]] == [ip]

    def test_unit_file_embeds_bootstrap_peer(self):
        prov = self._provisioner()
        mock_add_op, _ = _patched_execute(prov, active_ips=["10.0.0.2"])
        by_name = _ops_by_name(mock_add_op)
        joined = "\n".join(by_name["Configure ant-bootstrap-2"].kwargs["commands"])
        assert "--bootstrap 10.0.0.1:10000" in joined

    def test_op_order_daemon_reload_between_config_and_service(self):
        prov = self._provisioner()
        mock_add_op, _ = _patched_execute(
            prov, active_ips=["10.0.0.2", "10.0.0.3"]
        )
        names = [c.kwargs.get("name") for c in mock_add_op.call_args_list]
        last_config = max(
            i for i, n in enumerate(names) if n.startswith("Configure ")
        )
        reload_idx = names.index("Reload systemd daemon")
        first_service = min(
            i for i, n in enumerate(names) if n.startswith("Enable and start ")
        )
        assert last_config < reload_idx < first_service

    def test_binary_url_skips_release_fetch(self):
        prov = self._provisioner(binary_url="https://custom/ant-node")
        with (
            patch("saorsa_deploy.provisioning.bootstrap.Inventory"),
            patch("saorsa_deploy.provisioning.bootstrap.State") as mock_state_cls,
            patch("saorsa_deploy.provisioning.bootstrap.connect_all"),
            patch("saorsa_deploy.provisioning.bootstrap.add_op") as mock_add_op,
            patch("saorsa_deploy.provisioning.bootstrap.run_ops"),
            patch("saorsa_deploy.provisioning.bootstrap.disconnect_all"),
            patch(
                "saorsa_deploy.provisioning.bootstrap.get_release_url"
            ) as mock_release,
            patch(
                "saorsa_deploy.provisioning.bootstrap.create_progress_handler"
            ) as mock_handler,
        ):
            mock_handler.return_value = MagicMock()
            state = mock_state_cls.return_value
            state.inventory.get_active_hosts.return_value = [_fake_host("10.0.0.2")]
            state.failed_hosts = []
            prov.execute()
            mock_release.assert_not_called()
        by_name = _ops_by_name(mock_add_op)
        install = by_name["Download and install ant-node binary"]
        assert "https://custom/ant-node" in install.kwargs["commands"][0]

    def test_failed_host_raises_runtime_error(self):
        prov = self._provisioner()
        with pytest.raises(RuntimeError, match="1 bootstrap host"):
            _patched_execute(
                prov,
                active_ips=["10.0.0.2", "10.0.0.3"],
                failed_ips=["10.0.0.3"],
            )

    def test_all_succeed_no_raise(self):
        prov = self._provisioner()
        # Should not raise when failed_hosts is empty.
        _patched_execute(prov, active_ips=["10.0.0.2", "10.0.0.3"], failed_ips=[])
