from unittest.mock import MagicMock, patch

from saorsa_deploy.cmd import provision_telegraf_metrics as ptm


def _provisioner_calls(mock_prov):
    """All kwargs the TelegrafMetricsProvisioner was constructed with."""
    return [call.kwargs for call in mock_prov.call_args_list]


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
