"""Wiring tests for the node memory-composition collector (V2-972).

Three things are asserted here that nothing else would catch until a live run:
the config append is genuinely opt-in, the composition metrics reach the rollup
but never the bucket table, and `--skip-logs` resolves the backends the way a
metrics-only retrofit needs.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from saorsa_deploy.influx import (
    NODE_MEMORY_METRICS,
    ROLLUP_ONLY_METRICS,
    aggregate_host_metrics,
    bucket_host_metric_samples,
    normalise_host_metric_points,
)
from saorsa_deploy.provisioning.telegraf_metrics import (
    NODE_MEMORY_COLLECTOR_PATH,
    TelegrafMetricsProvisioner,
    build_telegraf_metrics_config,
)

T0 = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class TestConfigAppend:
    def test_default_config_has_no_collector(self):
        """Every deployment that does not ask for it keeps its exact config.

        The same discipline the CLIENT-role append follows: a byte-identical
        config for every other kind is what makes this safe to land.
        """
        config = build_telegraf_metrics_config()
        assert "inputs.exec" not in config
        assert "ant_node_memory" not in config

    def test_enabled_config_appends_both_exec_inputs(self):
        config = build_telegraf_metrics_config(include_node_memory=True)
        assert config.count("[[inputs.exec]]") == 2
        assert "--source status" in config
        assert "--source rollup" in config

    def test_enabled_config_is_a_pure_append(self):
        base = build_telegraf_metrics_config()
        enabled = build_telegraf_metrics_config(include_node_memory=True)
        assert enabled.startswith(base)

    def test_collector_path_placeholder_is_substituted(self):
        config = build_telegraf_metrics_config(include_node_memory=True)
        assert "{{COLLECTOR_PATH}}" not in config
        assert config.count(NODE_MEMORY_COLLECTOR_PATH) == 2

    def test_costly_source_runs_less_often_than_the_cheap_one(self):
        """smaps_rollup is ~90x the cost of status; the intervals must reflect it."""
        config = build_telegraf_metrics_config(include_node_memory=True)
        status_block = config.split("--source status")[1]
        rollup_block = config.split("--source rollup")[1]
        assert 'interval = "30s"' in status_block.split("[[inputs.exec]]")[0]
        assert 'interval = "60s"' in rollup_block

    def test_each_exec_input_is_bounded_by_a_timeout(self):
        config = build_telegraf_metrics_config(include_node_memory=True)
        assert config.count("timeout =") == 2

    def test_client_and_node_memory_appends_are_independent(self):
        both = build_telegraf_metrics_config(
            include_client_services=True, include_node_memory=True
        )
        assert "[[inputs.exec]]" in both
        assert 'metric_source = "ant-client"' in both


class TestProvisionerRoleGate:
    def _provisioner(self, host_role, collect_node_memory):
        return TelegrafMetricsProvisioner(
            host_ips=["10.0.0.1"],
            ssh_key_path="/dev/null",
            influxdb_url="http://influx:8086",
            influxdb_token="tok",
            influxdb_org="ant",
            influxdb_bucket="testnet-metrics",
            host_role=host_role,
            collect_node_memory=collect_node_memory,
        )

    @pytest.mark.parametrize("role", ["GENESIS", "BOOTSTRAP", "WAN_NODE"])
    def test_node_roles_get_the_collector(self, role):
        assert self._provisioner(role, True)._wants_node_memory()

    def test_client_role_never_gets_the_collector(self):
        """A CLIENT host runs no unit the collector's globs match.

        Installing it there would ship a script that can only ever emit
        nothing, so the role gate is belt-and-braces against a caller that
        passes the flag indiscriminately.
        """
        assert not self._provisioner("CLIENT", True)._wants_node_memory()

    def test_flag_off_means_off_on_node_roles(self):
        assert not self._provisioner("WAN_NODE", False)._wants_node_memory()

    def test_client_config_unaffected_by_the_flag(self):
        with_flag = self._provisioner("CLIENT", True)._build_telegraf_config()
        without = self._provisioner("CLIENT", False)._build_telegraf_config()
        assert with_flag == without


def _memory_points(fields, *, host="node-1", service="ant-node-3", count=3):
    return [
        {
            "host": host,
            "node_service": service,
            "_field": field,
            "_value": str(value),
            "_time": T0 + timedelta(seconds=30 * i),
        }
        for field, value in fields.items()
        for i in range(count)
    ]


class TestRollupCapture:
    def test_composition_reaches_the_rollup_per_service(self):
        points = {
            "ant_node_memory": _memory_points(
                {"vm_rss": 4096, "rss_anon": 3072, "rss_file": 1024}
            )
        }
        entries = aggregate_host_metrics(points)
        assert len(entries) == 1
        composition = entries[0]["memory_composition_by_service"]
        assert set(composition) == {"ant-node-3"}
        assert composition["ant-node-3"]["rss_anon"]["avg"] == 3072
        assert composition["ant-node-3"]["vm_rss"]["samples"] == 3

    def test_only_observed_fields_appear(self):
        """A kernel that omits a field must produce no key for it.

        An all-None stat would be indistinguishable from a field that was
        collected and happened to be empty.
        """
        points = {"ant_node_memory": _memory_points({"vm_rss": 1})}
        composition = aggregate_host_metrics(points)[0][
            "memory_composition_by_service"
        ]
        assert set(composition["ant-node-3"]) == {"vm_rss"}

    def test_unknown_field_is_ignored(self):
        """An allow-list, so a newer collector's field is not stored unread."""
        points = {
            "ant_node_memory": _memory_points({"vm_rss": 1, "some_new_field": 9})
        }
        composition = aggregate_host_metrics(points)[0][
            "memory_composition_by_service"
        ]
        assert set(composition["ant-node-3"]) == {"vm_rss"}

    def test_absent_measurement_yields_an_empty_dict(self):
        """The normal case: every deployment that did not opt in."""
        points = {
            "procstat": [
                {
                    "host": "node-1",
                    "node_service": "ant-node-3",
                    "_value": "100",
                    "_time": T0,
                }
            ]
        }
        entry = aggregate_host_metrics(points)[0]
        assert entry["memory_composition_by_service"] == {}
        assert entry["rss_by_service"]["ant-node-3"]["samples"] == 1

    def test_composition_does_not_disturb_rss_by_service(self):
        points = {
            "procstat": [
                {
                    "host": "node-1",
                    "node_service": "ant-node-3",
                    "_value": "100",
                    "_time": T0,
                }
            ],
            "ant_node_memory": _memory_points({"vm_rss": 4096}),
        }
        entry = aggregate_host_metrics(points)[0]
        assert entry["rss_by_service"]["ant-node-3"]["avg"] == 100
        assert entry["memory_composition_by_service"]["ant-node-3"]["vm_rss"][
            "avg"
        ] == 4096


class TestBucketExclusion:
    def test_composition_never_becomes_bucket_rows(self):
        """The load-bearing half of the archival decision (see V2-973).

        One per-service bucket metric costs ~164k rows / ~35 MB for a single
        990-node staging run, against a 10 GB disk already 76% bucket rows.
        Twenty-one of them would be ~5x the entire per-run cost, weekly.
        """
        points = {
            "ant_node_memory": _memory_points(
                {"vm_rss": 4096, "rollup_pss": 2048}
            )
        }
        rows = bucket_host_metric_samples(
            normalise_host_metric_points(points), bucket_secs=300
        )
        assert rows == []

    def test_other_metrics_still_bucket_normally(self):
        points = {
            "procstat": [
                {
                    "host": "node-1",
                    "node_service": "ant-node-3",
                    "_value": "100",
                    "_time": T0,
                }
            ],
            "ant_node_memory": _memory_points({"vm_rss": 4096}),
        }
        normalised = normalise_host_metric_points(points)
        rows = bucket_host_metric_samples(normalised, bucket_secs=300)
        assert [r["metric"] for r in rows] == ["rss_bytes"]

    def test_every_composition_metric_is_excluded(self):
        assert NODE_MEMORY_METRICS <= ROLLUP_ONLY_METRICS

    def test_pid_is_not_a_stored_metric(self):
        """PID is an identifier; averaging one is meaningless."""
        assert "pid" not in NODE_MEMORY_METRICS


class TestSkipLogsBackendResolution:
    """`--skip-logs` is what makes a metrics-only retrofit possible at all.

    Without it the only way to avoid resolving the ES backend is to drop
    `--name` — but `--name` is also what supplies the `testnet_name` global tag,
    so dropping it would blank that tag on every targeted host and sever every
    series from its history.
    """

    def _args(self, **overrides):
        base = dict(
            name="ant-prod-01",
            elasticsearch_url=None,
            elasticsearch_username=None,
            elasticsearch_password=None,
            influxdb_url="http://influx:8086",
            influxdb_token="tok",
            influxdb_org="ant",
            influxdb_bucket="testnet-metrics",
            skip_metrics=False,
            skip_logs=False,
            node_version=None,
            branch_commit=None,
            repo_owner="maidsafe",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_skip_logs_drops_the_es_backend(self, monkeypatch):
        from saorsa_deploy.cmd import provision_telegraf_combined as mod

        monkeypatch.setattr(
            mod, "load_elastic_state",
            lambda name: {
                "elasticsearch_url": "http://es:9200",
                "elasticsearch_username": "u",
                "elasticsearch_password": "p",
            },
        )
        es, influx = mod._resolve_backends(self._args(skip_logs=True), _Console())
        assert es is None
        assert influx is not None

    def test_without_skip_logs_the_es_backend_resolves(self, monkeypatch):
        from saorsa_deploy.cmd import provision_telegraf_combined as mod

        monkeypatch.setattr(
            mod, "load_elastic_state",
            lambda name: {
                "elasticsearch_url": "http://es:9200",
                "elasticsearch_username": "u",
                "elasticsearch_password": "p",
            },
        )
        es, influx = mod._resolve_backends(self._args(), _Console())
        assert es is not None
        assert influx is not None

    def test_skip_logs_keeps_the_name_for_the_metrics_tag(self, monkeypatch):
        """The whole point: `--name` survives, so `testnet_name` is unchanged."""
        from saorsa_deploy.cmd import provision_telegraf_combined as mod

        monkeypatch.setattr(
            mod, "load_elastic_state", lambda name: {}
        )
        args = self._args(skip_logs=True)
        mod._resolve_backends(args, _Console())
        assert args.name == "ant-prod-01"

    def test_explicit_es_flags_are_also_suppressed(self, monkeypatch):
        """`--skip-logs` means no ES pass, however the credentials arrived."""
        from saorsa_deploy.cmd import provision_telegraf_combined as mod

        args = self._args(
            skip_logs=True,
            elasticsearch_url="http://es:9200",
            elasticsearch_username="u",
            elasticsearch_password="p",
        )
        es, _ = mod._resolve_backends(args, _Console())
        assert es is None


class _Console:
    def print(self, *args, **kwargs):
        pass
