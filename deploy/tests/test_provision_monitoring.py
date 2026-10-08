from unittest.mock import MagicMock, patch

from saorsa_deploy.provisioning.monitoring import (
    MONITORING_MOUNT_POINT,
    MonitoringProvisioner,
)


def _make_provisioner(**overrides):
    defaults = {
        "influxdb_ip": "10.0.0.1",
        "grafana_ip": "10.0.0.2",
        "ssh_key_path": "~/.ssh/id_rsa",
        "grafana_password": "secret",
    }
    defaults.update(overrides)
    return MonitoringProvisioner(**defaults)


class TestMountOrderingOp:
    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    def test_installs_dropin_requiring_the_data_volume(self, mock_add_op):
        p = _make_provisioner()
        p._add_mount_ordering_op(MagicMock())
        mock_add_op.assert_called_once()
        commands = mock_add_op.call_args.kwargs["commands"]
        assert f"RequiresMountsFor={MONITORING_MOUNT_POINT}" in commands[0]
        assert "/etc/systemd/system/influxdb.service.d" in commands[0]

    @patch("saorsa_deploy.provisioning.monitoring.add_op")
    def test_skipped_on_root_disk_providers(self, mock_add_op):
        # Contabo has no attachable volume: the data dir is on / by design, so
        # there is no mount to wait for and the drop-in would be meaningless.
        p = _make_provisioner(root_disk=True)
        p._add_mount_ordering_op(MagicMock())
        mock_add_op.assert_not_called()


class TestInfluxdbStartOrdering:
    def _adders(self, provisioner) -> list:
        captured = []

        def fake_run_session(host_ip, label, op_adders):
            if label == "InfluxDB":
                captured.extend(op_adders)

        with patch.object(provisioner, "_run_session", side_effect=fake_run_session):
            with patch.object(provisioner, "_retrieve_telegraf_token", return_value="tok"):
                provisioner.execute()
        return [a.__name__ for a in captured]

    def test_dropin_is_installed_before_influxd_ever_starts(self):
        # The regression this guards: influxd's FIRST start is in
        # _add_setup_influxdb_ops, not the later _add_influxdb_start_ops. A
        # drop-in added after it would leave the very first boot unprotected.
        p = _make_provisioner(grafana_ip=None)
        names = self._adders(p)
        assert "_add_mount_ordering_op" in names
        assert names.index("_add_mount_ordering_op") < names.index("_add_setup_influxdb_ops")

    def test_dropin_is_installed_after_the_volume_is_mounted(self):
        # RequiresMountsFor resolves against a real mount unit; installing it
        # before the volume exists would order against nothing.
        p = _make_provisioner(grafana_ip=None)
        names = self._adders(p)
        assert names.index("_add_mount_volume_op") < names.index("_add_mount_ordering_op")
