from unittest.mock import MagicMock, patch

from saorsa_deploy.cmd import telegraf_fold as tf
from saorsa_deploy.provisioning.telegraf import TelegrafProvisioner
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner

_ES = {
    "elasticsearch_url": "http://es:9200",
    "elasticsearch_username": "u",
    "elasticsearch_password": "p",
}
_INFLUX = {"influxdb_url": "http://mon:8086", "influxdb_token": "tok"}


def _build(**kw):
    defaults = dict(
        host_ips=["10.0.0.1"],
        host_role="WAN_NODE",
        ssh_key_path="~/.ssh/id_rsa",
        console=MagicMock(),
    )
    defaults.update(kw)
    return tf.build_node_telegraf_providers("DEV-01", **defaults)


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_both_backends_yield_es_then_metrics(mock_es, mock_mon):
    mock_es.return_value = _ES
    mock_mon.return_value = _INFLUX
    providers = _build()
    assert len(providers) == 2
    assert isinstance(providers[0], TelegrafProvisioner)
    assert isinstance(providers[1], TelegrafMetricsProvisioner)
    assert providers[1].host_role == "WAN_NODE"


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_metrics_only_when_no_elastic(mock_es, mock_mon):
    mock_es.side_effect = RuntimeError("no elastic state")
    mock_mon.return_value = _INFLUX
    providers = _build()
    assert len(providers) == 1
    assert isinstance(providers[0], TelegrafMetricsProvisioner)


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_elastic_only_when_no_monitoring(mock_es, mock_mon):
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring state")
    providers = _build()
    assert len(providers) == 1
    assert isinstance(providers[0], TelegrafProvisioner)


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_neither_warns_and_returns_empty(mock_es, mock_mon):
    mock_es.side_effect = RuntimeError("no elastic state")
    mock_mon.side_effect = RuntimeError("no monitoring state")
    console = MagicMock()
    providers = _build(console=console)
    assert providers == []
    assert console.print.called


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_drop_log_levels_and_buffer_threaded_to_es(mock_es, mock_mon):
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring state")
    providers = _build(drop_log_levels="", buffer_limit=500000)
    es = providers[0]
    # Empty string -> keep every line (no drop levels).
    assert es.drop_log_levels == ()
    assert es.metric_buffer_limit == 500000


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_genesis_provider_flags(mock_es, mock_mon):
    mock_es.side_effect = RuntimeError("no elastic state")
    mock_mon.return_value = _INFLUX
    providers = _build(host_role="GENESIS")
    metrics = providers[0]
    assert metrics.host_role == "GENESIS"
