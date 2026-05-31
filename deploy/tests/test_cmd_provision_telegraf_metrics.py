from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd import provision_telegraf_metrics as ptm


def _args(**overrides):
    defaults = {
        "name": "PROD-DL-01",
        "influxdb_url": "http://mon:8086",
        "influxdb_token": "tok",
        "influxdb_org": "ant",
        "influxdb_bucket": "testnet-metrics",
        "inventory_path": None,
        "host_ips_file": None,
        "host_ips": None,
        "ssh_key_path": "~/.ssh/id_rsa",
        "ssh_user": "root",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _provisioner_calls(mock_prov):
    """All kwargs the TelegrafMetricsProvisioner was constructed with."""
    return [call.kwargs for call in mock_prov.call_args_list]


class TestNameBranchClientOnly:
    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_deployment_state")
    def test_client_only_deployment_provisions_client_role(
        self, mock_dep, mock_client, mock_prov, _ckh
    ):
        # No testnet deployment state (client-only deployment)...
        mock_dep.side_effect = RuntimeError("no deployment state")
        # ...but a download client VM exists.
        mock_client.return_value = {
            "clients": [{"ip": "139.59.190.165", "role": "download"}]
        }

        ptm.cmd_provision_telegraf_metrics(_args())

        kwargs_list = _provisioner_calls(mock_prov)
        assert len(kwargs_list) == 1
        kw = kwargs_list[0]
        assert kw["host_ips"] == ["139.59.190.165"]
        assert kw["host_role"] == "CLIENT"
        assert kw["node_count"] == 0
        # Client host metrics go to the dedicated client bucket, not the
        # node bucket passed in via --influxdb-bucket.
        assert kw["influxdb_bucket"] == "client-metrics"

    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_deployment_state")
    def test_no_node_or_client_state_exits(
        self, mock_dep, mock_client, mock_prov, _ckh
    ):
        mock_dep.side_effect = RuntimeError("no deployment state")
        mock_client.side_effect = RuntimeError("no client state")

        with pytest.raises(SystemExit):
            ptm.cmd_provision_telegraf_metrics(_args())

        mock_prov.assert_not_called()

    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_deployment_state")
    def test_node_deployment_still_provisions_nodes(
        self, mock_dep, mock_client, mock_prov, _ckh
    ):
        mock_dep.return_value = {
            "node_count": 15,
            "vm_ips": {"lon1": ["10.0.0.1", "10.0.0.2"]},
        }
        mock_client.side_effect = RuntimeError("no client state")

        ptm.cmd_provision_telegraf_metrics(_args(name="DEV-01"))

        calls = _provisioner_calls(mock_prov)
        roles = [kw["host_role"] for kw in calls]
        assert "WAN_NODE" in roles
        assert "CLIENT" not in roles
        # Node host metrics stay in the node bucket.
        for kw in calls:
            assert kw["influxdb_bucket"] == "testnet-metrics"

    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_deployment_state")
    def test_node_failure_still_provisions_clients_then_exits(
        self, mock_dep, mock_client, mock_prov, _ckh
    ):
        # A flaky node must NOT prevent client host-metrics from being
        # provisioned; the command still exits non-zero overall.
        mock_dep.return_value = {
            "node_count": 15,
            "vm_ips": {"lon1": ["10.0.0.1"]},
        }
        mock_client.return_value = {"clients": [{"ip": "1.2.3.4"}]}
        # First execute() (node WAN_NODE) raises; second (client) succeeds.
        mock_prov.return_value.execute.side_effect = [
            RuntimeError("node telegraf boom"),
            None,
        ]

        with pytest.raises(SystemExit):
            ptm.cmd_provision_telegraf_metrics(_args(name="DEV-02"))

        roles = [kw["host_role"] for kw in _provisioner_calls(mock_prov)]
        assert "WAN_NODE" in roles  # node was attempted
        assert "CLIENT" in roles  # client STILL attempted despite node failure


class TestClientHostnamesSetBeforeTelegraf:
    @patch.object(ptm, "set_hostnames")
    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    def test_sets_canonical_hostnames_before_telegraf(
        self, mock_client, mock_prov, _ckh, mock_set_hostnames
    ):
        # Telegraf captures `$(hostname)` at start, and uploader.sh /
        # downloader.sh tag Influx with it too. Only vultr-client sets the
        # hostname from the VM name in terraform; DO / OVH / Hetzner do not,
        # so the client provisioner must set it explicitly using the inventory
        # name before Telegraf runs.
        mock_client.return_value = {
            "clients": [
                {
                    "ip": "1.1.1.1",
                    "role": "upload",
                    "region": "lon1",
                    "client_index": 1,
                },
                {
                    "ip": "2.2.2.2",
                    "role": "download",
                    "region": "nyc1",
                    "client_index": 2,
                },
            ]
        }
        common = {
            "ssh_key_path": "~/.ssh/k",
            "ssh_user": "root",
            "influxdb_url": "http://x:8086",
            "influxdb_token": "tok",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
            "testnet_name": "DEV-01",
            "console": MagicMock(),
        }

        found, ok = ptm._provision_client_telegraf("DEV-01", common, MagicMock())

        assert (found, ok) == (True, True)
        mock_prov.assert_called_once()
        mock_set_hostnames.assert_called_once()
        entries = mock_set_hostnames.call_args.args[0]
        assert {e["ip"]: e["name"] for e in entries} == {
            "1.1.1.1": "DEV-01-ant-client-upload-lon1-1",
            "2.2.2.2": "DEV-01-ant-client-download-nyc1-2",
        }
        # Each entry carries the SSH details set_hostnames needs.
        for e in entries:
            assert e["ssh_user"] == "root"
            assert e["ssh_key"] == "~/.ssh/k"


class TestProvisionClientTelegrafForDeployment:
    @patch.object(ptm, "clear_known_hosts")
    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_monitoring_state")
    def test_resolves_influx_from_monitoring_state(
        self, mock_mon, mock_client, mock_prov, _ckh
    ):
        mock_mon.return_value = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "secret",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        mock_client.return_value = {"clients": [{"ip": "1.2.3.4"}]}

        result = ptm.provision_client_telegraf_for_deployment(
            "PROD-DL-01", "~/.ssh/id_rsa", console=MagicMock()
        )

        assert result is True
        kw = _provisioner_calls(mock_prov)[0]
        assert kw["influxdb_token"] == "secret"
        assert kw["host_role"] == "CLIENT"
        assert kw["host_ips"] == ["1.2.3.4"]
        # Bucket from monitoring state (testnet-metrics) is overridden to the
        # client bucket for CLIENT-role host metrics.
        assert kw["influxdb_bucket"] == "client-metrics"

    @patch.object(ptm, "TelegrafMetricsProvisioner")
    @patch.object(ptm, "load_client_state")
    @patch.object(ptm, "load_monitoring_state")
    def test_no_clients_returns_false(
        self, mock_mon, mock_client, mock_prov
    ):
        mock_mon.return_value = {"influxdb_url": "u", "influxdb_token": "t"}
        mock_client.return_value = {"clients": []}

        result = ptm.provision_client_telegraf_for_deployment(
            "PROD-DL-01", "~/.ssh/id_rsa", console=MagicMock()
        )

        assert result is False
        mock_prov.assert_not_called()


class TestBestEffortWrapper:
    @patch.object(ptm, "provision_client_telegraf_for_deployment")
    def test_swallows_system_exit(self, mock_inner):
        mock_inner.side_effect = SystemExit(1)
        assert (
            ptm.provision_client_telegraf_best_effort(
                "X", "~/.ssh/id_rsa", console=MagicMock()
            )
            is False
        )

    @patch.object(ptm, "provision_client_telegraf_for_deployment")
    def test_swallows_runtime_error(self, mock_inner):
        # e.g. no monitoring state for the deployment
        mock_inner.side_effect = RuntimeError("no monitoring state")
        assert (
            ptm.provision_client_telegraf_best_effort(
                "X", "~/.ssh/id_rsa", console=MagicMock()
            )
            is False
        )

    @patch.object(ptm, "provision_client_telegraf_for_deployment")
    def test_passes_through_success(self, mock_inner):
        mock_inner.return_value = True
        assert (
            ptm.provision_client_telegraf_best_effort(
                "X", "~/.ssh/id_rsa", console=MagicMock()
            )
            is True
        )
