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
    TelegrafMetricsProvisioner,
    build_telegraf_metrics_config,
)

T0 = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class TestConfigAppend:
    def test_default_config_has_no_mmap_collection(self):
        """Every deployment that does not ask for it keeps its exact config.

        The base config now names `properties` explicitly to EXCLUDE telegraf's
        default "mmap". That is not cosmetic: mmap reads the whole of
        /proc/<pid>/smaps, which is free while telegraf is unprivileged (the
        read fails) but costs 6.78% of a core at the 10s agent interval on any
        host granted CAP_SYS_PTRACE.
        """
        config = build_telegraf_metrics_config()
        code = [line for line in config.splitlines() if not line.strip().startswith("#")]
        assert not any("mmap" in line for line in code)
        assert config.count('properties = ["cpu", "memory", "limits"]') == 4

    def test_enabled_config_adds_an_mmap_tier(self):
        config = build_telegraf_metrics_config(include_node_memory=True)
        assert config.count('properties = ["mmap"]') == 4
        assert config.count('interval = "60s"') == 4

    def test_enabled_config_is_a_pure_append(self):
        base = build_telegraf_metrics_config()
        enabled = build_telegraf_metrics_config(include_node_memory=True)
        assert enabled.startswith(base)

    def test_no_exec_input_is_ever_generated(self):
        """The exec route measured ~14.9% of a core with ~12.5% unexplained.

        Native procstat gets the same fields for 1.15%, so no configuration
        this builder produces should contain an exec input.
        """
        for kwargs in ({}, {"include_node_memory": True}, {"include_client_services": True}):
            assert "inputs.exec" not in build_telegraf_metrics_config(**kwargs)

    def test_cheap_tier_keeps_its_ten_second_resolution(self):
        """The two-tier split exists so existing telemetry is NOT downgraded.

        cpu_usage / num_fds / num_threads / memory_rss stay at the 10s agent
        interval; only the mmap tier is throttled to 60s.
        """
        config = build_telegraf_metrics_config(include_node_memory=True)
        cheap = config.split('properties = ["mmap"]')[0]
        assert 'interval = "60s"' not in cheap

    def test_mmap_tier_covers_the_same_units_as_the_base(self):
        config = build_telegraf_metrics_config(include_node_memory=True)
        for glob in ("ant-node-*", "ant-bootstrap-*", "ant-bootstrap", "ant-genesis-node"):
            assert config.count(f'cgroup = "system.slice/{glob}.service"') == 2

    def test_client_and_node_memory_appends_are_independent(self):
        both = build_telegraf_metrics_config(include_client_services=True, include_node_memory=True)
        assert 'properties = ["mmap"]' in both
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


class TestUnitCapabilities:
    """`smaps_rollup` needs ptrace access; `status` does not.

    Telegraf runs as an unprivileged user, and the kernel gates smaps_rollup on
    ptrace_may_access rather than on file permissions — so CAP_DAC_READ_SEARCH,
    which the unit already had, is not enough. Without CAP_SYS_PTRACE the rollup
    source returns nothing on every scrape while status keeps working, which is
    exactly how it went unnoticed on the first canary deploy.
    """

    def _unit(self, host_role, collect_node_memory):
        return TelegrafMetricsProvisioner(
            host_ips=["10.0.0.1"],
            ssh_key_path="/dev/null",
            influxdb_url="http://influx:8086",
            influxdb_token="tok",
            influxdb_org="ant",
            influxdb_bucket="testnet-metrics",
            host_role=host_role,
            collect_node_memory=collect_node_memory,
        )._build_unit_file()

    def test_enabled_unit_grants_ptrace(self):
        unit = self._unit("WAN_NODE", True)
        assert "AmbientCapabilities=CAP_DAC_READ_SEARCH CAP_SYS_PTRACE" in unit

    def test_disabled_unit_keeps_the_original_capability_line(self):
        """Every host that does not opt in keeps the narrower capability set."""
        unit = self._unit("WAN_NODE", False)
        assert "AmbientCapabilities=CAP_DAC_READ_SEARCH\n" in unit
        assert "CAP_SYS_PTRACE" not in unit

    def test_client_role_never_gains_ptrace(self):
        assert "CAP_SYS_PTRACE" not in self._unit("CLIENT", True)

    def test_no_placeholder_survives(self):
        for role, flag in (("WAN_NODE", True), ("WAN_NODE", False)):
            assert "{{" not in self._unit(role, flag)


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
            "ant_node_memory": _memory_points({"vm_rss": 4096, "rss_anon": 3072, "rss_file": 1024})
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
        composition = aggregate_host_metrics(points)[0]["memory_composition_by_service"]
        assert set(composition["ant-node-3"]) == {"vm_rss"}

    def test_unknown_field_is_ignored(self):
        """An allow-list, so a newer collector's field is not stored unread."""
        points = {"ant_node_memory": _memory_points({"vm_rss": 1, "some_new_field": 9})}
        composition = aggregate_host_metrics(points)[0]["memory_composition_by_service"]
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
        assert entry["memory_composition_by_service"]["ant-node-3"]["vm_rss"]["avg"] == 4096


class TestBucketExclusion:
    def test_composition_never_becomes_bucket_rows(self):
        """The load-bearing half of the archival decision (see V2-973).

        One per-service bucket metric costs ~164k rows / ~35 MB for a single
        990-node staging run, against a 10 GB disk already 76% bucket rows.
        Twenty-one of them would be ~5x the entire per-run cost, weekly.
        """
        points = {"ant_node_memory": _memory_points({"vm_rss": 4096, "rollup_pss": 2048})}
        rows = bucket_host_metric_samples(normalise_host_metric_points(points), bucket_secs=300)
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
            mod,
            "load_elastic_state",
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
            mod,
            "load_elastic_state",
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

        monkeypatch.setattr(mod, "load_elastic_state", lambda name: {})
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


class TestCollectorTimerUnits:
    """The collector is delivered by a systemd timer, never `inputs.exec`.

    The exec route measured ~14.9% of a core on a production node, of which
    ~12.5% was telegraf overhead that could not be accounted for. A timer costs
    only the collector's own work.
    """

    def _provisioner(self, **kw):
        return TelegrafMetricsProvisioner(
            host_ips=["10.0.0.1"],
            ssh_key_path="/dev/null",
            influxdb_url="http://influx:8086",
            influxdb_token="tok",
            influxdb_org="ant",
            influxdb_bucket="testnet-metrics",
            host_role="WAN_NODE",
            collect_node_memory=True,
            **kw,
        )

    def _ops(self, **kw):
        recorded = []

        class _State:
            pass

        import saorsa_deploy.provisioning.telegraf_metrics as mod

        real_add_op = mod.add_op

        def fake_add_op(state, op, name=None, commands=None, **_):
            recorded.append((name, commands))

        mod.add_op = fake_add_op
        try:
            self._provisioner(**kw).add_ops(_State(), install=False)
        finally:
            mod.add_op = real_add_op
        return recorded

    def test_timer_and_service_units_are_written(self):
        names = [n for n, _ in self._ops()]
        assert "Write ant-node memory collector systemd units" in names
        assert "Enable and start ant-node memory collector timer" in names

    def test_default_interval_is_the_coarse_fleet_cadence(self):
        body = "\n".join(
            str(c) for n, c in self._ops() if n == "Write ant-node memory collector systemd units"
        )
        assert "OnUnitActiveSec=300s" in body

    def test_investigation_interval_is_configurable(self):
        body = "\n".join(
            str(c)
            for n, c in self._ops(node_memory_interval_secs=60)
            if n == "Write ant-node memory collector systemd units"
        )
        assert "OnUnitActiveSec=60s" in body

    def test_timer_is_jittered(self):
        """Without jitter every node's page-table walk would land together."""
        body = "\n".join(
            str(c) for n, c in self._ops() if n == "Write ant-node memory collector systemd units"
        )
        assert "RandomizedDelaySec=30s" in body

    def test_service_is_hardened_and_bounded(self):
        body = "\n".join(
            str(c) for n, c in self._ops() if n == "Write ant-node memory collector systemd units"
        )
        assert "NoNewPrivileges=yes" in body
        assert "ProtectSystem=strict" in body

    def test_nothing_is_installed_when_disabled(self):
        recorded = []
        import saorsa_deploy.provisioning.telegraf_metrics as mod

        real = mod.add_op
        mod.add_op = lambda state, op, name=None, commands=None, **_: recorded.append(name)
        try:
            TelegrafMetricsProvisioner(
                host_ips=["10.0.0.1"],
                ssh_key_path="/dev/null",
                influxdb_url="u",
                influxdb_token="t",
                influxdb_org="ant",
                influxdb_bucket="b",
                host_role="WAN_NODE",
                collect_node_memory=False,
            ).add_ops(type("S", (), {})(), install=False)
        finally:
            mod.add_op = real
        assert not any("memory" in (n or "") for n in recorded)


class TestProcstatMmapCapture:
    """procstat's native mmap fields are the routine economical signal."""

    def _points(self, field, value, unit_field="memory_rss"):
        return {
            "procstat": [
                {
                    "host": "node-1",
                    "node_service": "ant-node-3",
                    "_field": field,
                    "_value": str(value),
                    "_time": T0,
                }
            ]
        }

    def test_smaps_fields_are_converted_from_kb_to_bytes(self):
        """Telegraf reports memory_rss in bytes but smaps fields in kB.

        The inconsistency is telegraf's; correcting it once at capture is what
        keeps everything stored in bytes.
        """
        entry = aggregate_host_metrics(self._points("memory_pss", 1024))[0]
        stats = entry["memory_composition_by_service"]["ant-node-3"]
        assert stats["procstat_pss"]["avg"] == 1024 * 1024

    def test_memory_rss_keeps_its_units_and_metric_name(self):
        """Every historical run and gate comparison depends on this series."""
        entry = aggregate_host_metrics(self._points("memory_rss", 4096))[0]
        assert entry["rss_by_service"]["ant-node-3"]["avg"] == 4096
        assert entry["memory_composition_by_service"] == {}

    def test_unknown_procstat_field_is_ignored(self):
        """An allow-list: a field we do not map contributes nothing at all.

        No sample means the host has no observed time bounds, so it produces no
        entry — rather than an entry full of empty stats that would read as "we
        looked and there was nothing there".
        """
        assert aggregate_host_metrics(self._points("memory_vms", 99)) == []

    def test_procstat_composition_never_becomes_bucket_rows(self):
        rows = bucket_host_metric_samples(
            normalise_host_metric_points(self._points("memory_anonymous", 512)),
            bucket_secs=300,
        )
        assert rows == []

    def test_rss_still_buckets_normally(self):
        rows = bucket_host_metric_samples(
            normalise_host_metric_points(self._points("memory_rss", 4096)),
            bucket_secs=300,
        )
        assert [r["metric"] for r in rows] == ["rss_bytes"]
