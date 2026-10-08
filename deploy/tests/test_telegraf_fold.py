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
def test_from_beginning_defaults_true_and_renders_true(mock_es, mock_mon):
    """Every kind except prod-debug-nodes must keep backfill-from-start.

    Asserted on the rendered TOML, not just the attribute, so a change to the
    template can't silently flip what the node fleet actually runs.
    """
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring state")
    es = _build()[0]
    assert es.from_beginning is True
    assert "from_beginning = true" in es._build_telegraf_config()


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_from_beginning_false_is_threaded_to_es(mock_es, mock_mon):
    """prod-debug-nodes restarts the forwarder on every log prune.

    Telegraf keeps no tail offset across restarts, so reading from the
    beginning would re-ingest every remaining log on each prune.
    """
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring state")
    es = _build(from_beginning=False)[0]
    assert es.from_beginning is False
    assert "from_beginning = false" in es._build_telegraf_config()


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_from_beginning_does_not_disturb_other_config(mock_es, mock_mon):
    """Only the one line differs between the two renderings."""
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring state")
    default_cfg = _build()[0]._build_telegraf_config()
    debug_cfg = _build(from_beginning=False)[0]._build_telegraf_config()
    differing = [(a, b) for a, b in zip(default_cfg.splitlines(), debug_cfg.splitlines()) if a != b]
    assert differing == [("  from_beginning = true", "  from_beginning = false")]


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_genesis_provider_flags(mock_es, mock_mon):
    mock_es.side_effect = RuntimeError("no elastic state")
    mock_mon.return_value = _INFLUX
    providers = _build(host_role="GENESIS")
    metrics = providers[0]
    assert metrics.host_role == "GENESIS"


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_es_forwarder_gets_influx_creds_when_monitoring_exists(mock_es, mock_mon):
    """With both stacks, the ES forwarder also writes the traffic summaries to
    Influx -- using exactly the credentials the metrics forwarder gets."""
    mock_es.return_value = _ES
    mock_mon.return_value = _INFLUX
    es, metrics = _build()
    assert es.influx_output_enabled
    assert (es.influxdb_url, es.influxdb_token, es.influxdb_org, es.influxdb_bucket) == (
        metrics.influxdb_url,
        metrics.influxdb_token,
        metrics.influxdb_org,
        metrics.influxdb_bucket,
    )
    assert "[[outputs.influxdb_v2]]" in es._build_telegraf_config()


@patch.object(tf, "load_monitoring_state")
@patch.object(tf, "load_elastic_state")
def test_es_forwarder_without_monitoring_has_no_influx_output(mock_es, mock_mon):
    mock_es.return_value = _ES
    mock_mon.side_effect = RuntimeError("no monitoring")
    (es,) = _build()
    assert not es.influx_output_enabled
    assert "influxdb_v2" not in es._build_telegraf_config()
