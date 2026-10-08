from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd import provision_telegraf_combined as ptc
from saorsa_deploy.provisioning import telegraf_combined as tc


# --------------------------------------------------------------------------- #
# provision_combined: single SSH pass, install once, both services
# --------------------------------------------------------------------------- #
class TestProvisionCombined:
    @patch("saorsa_deploy.provisioning.telegraf_combined.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_combined.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.State")
    @patch("saorsa_deploy.provisioning.telegraf_combined.Inventory")
    def test_installs_once_and_adds_both_services(
        self, _inv, mock_state, mock_connect, mock_run, mock_disc
    ):
        st = MagicMock()
        st.failed_hosts = set()
        mock_state.return_value = st

        es = MagicMock()
        metrics = MagicMock()
        tc.provision_combined(
            ["10.0.0.1", "10.0.0.2"], "~/.ssh/k", [es, metrics], console=MagicMock()
        )

        # One connect/run/disconnect for the whole group.
        mock_connect.assert_called_once_with(st)
        mock_run.assert_called_once_with(st)
        mock_disc.assert_called_once_with(st)
        # First provisioner installs Telegraf; the second reuses it.
        es.add_ops.assert_called_once_with(st, install=True)
        metrics.add_ops.assert_called_once_with(st, install=False)

    @patch("saorsa_deploy.provisioning.telegraf_combined.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_combined.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.State")
    @patch("saorsa_deploy.provisioning.telegraf_combined.Inventory")
    def test_noop_when_no_active_provisioners(self, _inv, _state, mock_connect, mock_run):
        tc.provision_combined(["10.0.0.1"], "~/.ssh/k", [None], console=MagicMock())
        mock_connect.assert_not_called()
        mock_run.assert_not_called()

    @patch("saorsa_deploy.provisioning.telegraf_combined.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_combined.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.State")
    @patch("saorsa_deploy.provisioning.telegraf_combined.Inventory")
    def test_raises_on_failed_hosts(self, _inv, mock_state, _connect, _run, mock_disc):
        failed = MagicMock()
        failed.name = "10.0.0.9"
        st = MagicMock()
        st.failed_hosts = {failed}
        mock_state.return_value = st

        with pytest.raises(RuntimeError, match="1 host"):
            tc.provision_combined(["10.0.0.9"], "~/.ssh/k", [MagicMock()], console=MagicMock())
        mock_disc.assert_called_once_with(st)

    @patch("saorsa_deploy.provisioning.telegraf_combined.disconnect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.run_ops")
    @patch("saorsa_deploy.provisioning.telegraf_combined.connect_all")
    @patch("saorsa_deploy.provisioning.telegraf_combined.State")
    @patch("saorsa_deploy.provisioning.telegraf_combined.Inventory")
    def test_single_provisioner_installs(self, _inv, mock_state, _connect, _run, _disc):
        st = MagicMock()
        st.failed_hosts = set()
        mock_state.return_value = st
        only = MagicMock()
        tc.provision_combined(["10.0.0.1"], "~/.ssh/k", [None, only], console=MagicMock())
        only.add_ops.assert_called_once_with(st, install=True)


# --------------------------------------------------------------------------- #
# _resolve_backends: which forwarders run
# --------------------------------------------------------------------------- #
def _args(**overrides):
    defaults = {
        "name": "DEV-01",
        "elasticsearch_url": None,
        "elasticsearch_username": None,
        "elasticsearch_password": None,
        "node_version": "1.0",
        "branch_commit": None,
        "repo_owner": "maidsafe",
        "influxdb_url": None,
        "influxdb_token": None,
        "influxdb_org": "ant",
        "influxdb_bucket": "testnet-metrics",
        "inventory_path": None,
        "host_ips_file": None,
        "host_role": None,
        "ssh_key_path": "~/.ssh/id_rsa",
        "ssh_user": "root",
        "ant_log_dir": None,
        "from_beginning": True,
        "drop_log_levels": "DEBUG,TRACE",
        "buffer_limit": None,
        "max_undelivered_lines": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestResolveBackends:
    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_both_present(self, mock_es, mock_mon):
        mock_es.return_value = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
        }
        mock_mon.return_value = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "tok",
        }
        es, influx = ptc._resolve_backends(_args(), MagicMock())
        assert es["elasticsearch_url"] == "http://es:9200"
        assert influx["influxdb_token"] == "tok"

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_metrics_only_when_no_elastic_state(self, mock_es, mock_mon):
        mock_es.side_effect = RuntimeError("no elastic state")
        mock_mon.return_value = {"influxdb_url": "http://mon:8086", "influxdb_token": "t"}
        es, influx = ptc._resolve_backends(_args(), MagicMock())
        assert es is None
        assert influx is not None

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_elastic_only_when_no_monitoring_state(self, mock_es, mock_mon):
        mock_es.return_value = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
        }
        mock_mon.side_effect = RuntimeError("no monitoring state")
        es, influx = ptc._resolve_backends(_args(), MagicMock())
        assert es is not None
        assert influx is None

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_exits_when_neither(self, mock_es, mock_mon):
        mock_es.side_effect = RuntimeError("no elastic state")
        mock_mon.side_effect = RuntimeError("no monitoring state")
        with pytest.raises(SystemExit):
            ptc._resolve_backends(_args(), MagicMock())

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_skip_metrics_suppresses_influx(self, mock_es, mock_mon):
        """--skip-metrics keeps a log-forwarding retrofit off the metrics forwarder.

        --name is required for the ES index name, so without this the monitoring
        state it also resolves would regenerate and restart telegraf-ant-metrics
        on every targeted host.
        """
        mock_es.return_value = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
        }
        mock_mon.return_value = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "tok",
        }
        es, influx = ptc._resolve_backends(_args(skip_metrics=True), MagicMock())
        assert es is not None
        assert influx is None
        # The ES forwarder still gets the Influx credentials: it writes the
        # traffic-accounting summaries to Influx too (V2-1225), so an ES-only
        # retrofit must render that output rather than silently drop it.
        assert es["influx"]["influxdb_url"] == "http://mon:8086"
        assert es["influx"]["influxdb_token"] == "tok"

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_es_provisioner_carries_influx_creds(self, mock_es, mock_mon):
        """Both backends resolved -> the ES provisioner renders the Influx output."""
        mock_es.return_value = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
        }
        mock_mon.return_value = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "tok",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        args = _args()
        es, influx = ptc._resolve_backends(args, MagicMock())
        prov = ptc._make_es_provisioner(
            args,
            es,
            host_ips=["1.1.1.1"],
            host_role="WAN_NODE",
            ssh_key="k",
            ssh_user="root",
            name="DEV-01",
            console=MagicMock(),
        )
        assert prov.influx_output_enabled
        assert prov.influxdb_bucket == "testnet-metrics"
        assert "[[outputs.influxdb_v2]]" in prov._build_telegraf_config()

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_es_provisioner_without_monitoring_has_no_influx_output(self, mock_es, mock_mon):
        mock_es.return_value = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
        }
        mock_mon.side_effect = RuntimeError("no monitoring state")
        args = _args()
        es, influx = ptc._resolve_backends(args, MagicMock())
        assert influx is None
        prov = ptc._make_es_provisioner(
            args,
            es,
            host_ips=["1.1.1.1"],
            host_role="WAN_NODE",
            ssh_key="k",
            ssh_user="root",
            name="DEV-01",
            console=MagicMock(),
        )
        assert not prov.influx_output_enabled
        assert "influxdb_v2" not in prov._build_telegraf_config()

    @patch.object(ptc, "load_monitoring_state")
    @patch.object(ptc, "load_elastic_state")
    def test_skip_metrics_still_exits_when_no_elastic(self, mock_es, mock_mon):
        mock_es.side_effect = RuntimeError("no elastic state")
        mock_mon.return_value = {"influxdb_url": "http://mon:8086", "influxdb_token": "t"}
        with pytest.raises(SystemExit):
            ptc._resolve_backends(_args(skip_metrics=True), MagicMock())


# --------------------------------------------------------------------------- #
# _provision_by_name: node combined pass + client metrics pass
# --------------------------------------------------------------------------- #
class TestProvisionByName:
    @patch.object(ptc, "_provision_client_telegraf", return_value=(False, True))
    @patch.object(ptc, "provision_combined")
    @patch.object(ptc, "clear_known_hosts")
    @patch.object(ptc, "load_deployment_state")
    def test_node_groups_get_combined_pass_with_both(self, mock_dep, _ckh, mock_combined, _client):
        mock_dep.return_value = {
            "node_count": 15,
            "bootstrap_nodes": [
                {"ip": "10.0.0.1", "bootstrap_index": 1},
                {"ip": "10.0.0.2", "bootstrap_index": 2},
            ],
            "vm_ips": {"lon1": ["10.0.1.1", "10.0.1.2"]},
        }
        es = {
            "elasticsearch_url": "http://es:9200",
            "elasticsearch_username": "u",
            "elasticsearch_password": "p",
            "node_version": "1.0",
            "branch_commit": "unknown",
        }
        influx = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "t",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        ptc._provision_by_name(_args(name="DEV-01"), es, influx, MagicMock())

        # One combined pass per node group (GENESIS, BOOTSTRAP, WAN_NODE).
        assert mock_combined.call_count == 3
        for call in mock_combined.call_args_list:
            provisioners = call.args[2]
            # Both an ES and a metrics provisioner are present in every group.
            assert provisioners[0] is not None
            assert provisioners[1] is not None

    @patch.object(ptc, "_provision_client_telegraf", return_value=(True, True))
    @patch.object(ptc, "provision_combined")
    @patch.object(ptc, "clear_known_hosts")
    @patch.object(ptc, "load_deployment_state")
    def test_client_only_metrics_when_no_node_state(
        self, mock_dep, _ckh, mock_combined, mock_client
    ):
        mock_dep.side_effect = RuntimeError("no deployment state")
        influx = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "t",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        ptc._provision_by_name(_args(name="PROD-DL-01"), None, influx, MagicMock())

        mock_combined.assert_not_called()  # no node tier
        mock_client.assert_called_once()  # client metrics still provisioned

    @patch.object(ptc, "_provision_client_telegraf")
    @patch.object(ptc, "provision_combined")
    @patch.object(ptc, "clear_known_hosts")
    @patch.object(ptc, "load_deployment_state")
    def test_no_node_or_client_exits(self, mock_dep, _ckh, mock_combined, mock_client):
        mock_dep.side_effect = RuntimeError("no deployment state")
        influx = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "t",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        mock_client.return_value = (False, True)
        with pytest.raises(SystemExit):
            ptc._provision_by_name(_args(name="EMPTY"), None, influx, MagicMock())

    @patch.object(ptc, "_provision_client_telegraf", return_value=(False, True))
    @patch.object(ptc, "provision_combined")
    @patch.object(ptc, "clear_known_hosts")
    @patch.object(ptc, "load_deployment_state")
    def test_metrics_only_passes_no_es_provisioner(self, mock_dep, _ckh, mock_combined, _client):
        mock_dep.return_value = {"node_count": 5, "vm_ips": {"lon1": ["10.0.1.1"]}}
        influx = {
            "influxdb_url": "http://mon:8086",
            "influxdb_token": "t",
            "influxdb_org": "ant",
            "influxdb_bucket": "testnet-metrics",
        }
        ptc._provision_by_name(_args(name="DEV-01"), None, influx, MagicMock())
        provisioners = mock_combined.call_args.args[2]
        assert provisioners[0] is None  # no ES provisioner
        assert provisioners[1] is not None  # metrics provisioner present
