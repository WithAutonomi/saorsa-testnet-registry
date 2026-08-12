import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import saorsa_deploy.cmd.record_analysis as record_analysis_mod
from saorsa_deploy import influx
from saorsa_deploy.influx import (
    _parse_flux_csv,
    aggregate_host_metrics,
    fetch_host_metric_points,
)
from saorsa_deploy.main import main


def _cpu_point(host: str, ts: str, idle: float) -> dict:
    return {
        "_time": datetime.fromisoformat(ts).astimezone(timezone.utc),
        "host": host,
        "_measurement": "cpu",
        "cpu": "cpu-total",
        "_field": "usage_idle",
        "_value": str(idle),
    }


def _mem_point(host: str, ts: str, used_percent: float) -> dict:
    return {
        "_time": datetime.fromisoformat(ts).astimezone(timezone.utc),
        "host": host,
        "_measurement": "mem",
        "_field": "used_percent",
        "_value": str(used_percent),
    }


def _net_point(
    host: str,
    ts: str,
    interface: str,
    field: str,
    value: int,
) -> dict:
    return {
        "_time": datetime.fromisoformat(ts).astimezone(timezone.utc),
        "host": host,
        "_measurement": "net",
        "interface": interface,
        "_field": field,
        "_value": str(value),
    }


def _diskio_point(
    host: str,
    ts: str,
    device: str,
    field: str,
    value: int,
    volume_role: str | None = None,
) -> dict:
    point = {
        "_time": datetime.fromisoformat(ts).astimezone(timezone.utc),
        "host": host,
        "_measurement": "diskio",
        # diskio tags the device as `name`, not `device`.
        "name": device,
        "_field": field,
        "_value": str(value),
    }
    if volume_role is not None:
        point["volume_role"] = volume_role
    return point


def test_parse_flux_csv_keeps_the_consumed_columns_and_parses_time():
    body = (
        ",result,table,_start,_stop,_time,host,_measurement,_field,_value\n"
        ",_result,0,2026-04-19T00:00:00Z,2026-04-20T00:00:00Z,2026-04-19T10:00:00Z,"
        "hostA,cpu,usage_idle,95.0\n"
    )
    rows = _parse_flux_csv(body)
    assert len(rows) == 1
    assert rows[0]["host"] == "hostA"
    assert rows[0]["_measurement"] == "cpu"
    assert rows[0]["_field"] == "usage_idle"
    assert rows[0]["_value"] == "95.0"
    assert rows[0]["_time"].tzinfo is not None


def test_parse_flux_csv_drops_columns_no_consumer_reads():
    """The row is projected to what is actually read, not the whole response.

    This previously kept every column, which cost ~1.4 KB per row for ~40 bytes
    of payload and made `record-analysis` need >23 GB on a 990-node run.
    `_start`/`_stop` are the clearest waste: long ISO strings, identical for
    every row of a table, re-allocated per row.
    """
    body = (
        ",result,table,_start,_stop,_time,host,host_role,region,_field,_value\n"
        ",_result,0,2026-04-19T00:00:00Z,2026-04-20T00:00:00Z,2026-04-19T10:00:00Z,"
        "hostA,NODE,lon1,usage_idle,95.0\n"
    )
    rows = _parse_flux_csv(body)
    assert len(rows) == 1
    for dropped in ("_start", "_stop", "result", "table", "host_role", "region"):
        assert dropped not in rows[0]
    # keep=None restores the old keep-everything behaviour for any caller that
    # genuinely needs an unknown column.
    everything = _parse_flux_csv(body, keep=None)
    assert everything[0]["_start"] == "2026-04-19T00:00:00Z"
    assert everything[0]["region"] == "lon1"


def test_parse_flux_csv_interns_repeated_tag_values():
    """Tag columns repeat on every row; they must share one str object.

    Interning is what turns millions of duplicate short strings into a few
    thousand shared ones. Asserted by identity, since equality would hold
    either way.
    """
    header = ",result,table,_time,host,_measurement,_field,_value\n"
    rows_csv = "".join(
        f",_result,0,2026-04-19T10:0{i}:00Z,hostA,cpu,usage_idle,9{i}.0\n"
        for i in range(5)
    )
    rows = _parse_flux_csv(header + rows_csv)
    assert len(rows) == 5
    first = rows[0]
    for other in rows[1:]:
        assert other["host"] is first["host"]
        assert other["_field"] is first["_field"]
        assert other["_measurement"] is first["_measurement"]


def test_parse_flux_csv_accepts_a_line_iterable():
    """The fetch path streams, so the parser must take lines as well as a body."""
    lines = [
        ",result,table,_time,host,_measurement,_field,_value",
        ",_result,0,2026-04-19T10:00:00Z,hostA,cpu,usage_idle,95.0",
    ]
    rows = _parse_flux_csv(iter(lines))
    assert len(rows) == 1
    assert rows[0]["host"] == "hostA"
    assert rows[0]["_value"] == "95.0"


def test_parse_flux_csv_skips_rows_without_host_or_time():
    body = (
        ",result,table,_time,host,_measurement,_field,_value\n"
        ",_result,0,2026-04-19T10:00:00Z,,cpu,usage_idle,95.0\n"
        ",_result,0,,hostA,cpu,usage_idle,95.0\n"
    )
    assert _parse_flux_csv(body) == []


def test_parse_flux_csv_resets_header_on_metadata_row():
    body = (
        "#datatype,string,long,dateTime:RFC3339,string,string,string,double\n"
        ",result,table,_time,host,_measurement,_field,_value\n"
        ",_result,0,2026-04-19T10:00:00Z,hostA,mem,used_percent,42.0\n"
    )
    rows = _parse_flux_csv(body)
    assert len(rows) == 1
    assert rows[0]["_value"] == "42.0"


def test_aggregate_cpu_metrics_computes_utilization_from_idle():
    points = {
        "cpu": [
            _cpu_point("hostA", "2026-04-19T10:00:00+00:00", 90.0),
            _cpu_point("hostA", "2026-04-19T10:00:10+00:00", 70.0),
            _cpu_point("hostA", "2026-04-19T10:00:20+00:00", 80.0),
        ],
        "mem": [],
        "net": [],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    cpu = results[0]["cpu_percent"]
    # utilization = 100 - idle: 10, 30, 20
    assert cpu["samples"] == 3
    assert cpu["avg"] == pytest.approx(20.0)
    assert cpu["min"] == pytest.approx(10.0)
    assert cpu["max"] == pytest.approx(30.0)


def test_aggregate_mem_metrics_passes_through_used_percent():
    points = {
        "cpu": [],
        "mem": [
            _mem_point("hostA", "2026-04-19T10:00:00+00:00", 40.0),
            _mem_point("hostA", "2026-04-19T10:00:10+00:00", 60.0),
        ],
        "net": [],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    mem = results[0]["mem_percent"]
    assert mem["avg"] == pytest.approx(50.0)
    assert mem["min"] == pytest.approx(40.0)
    assert mem["max"] == pytest.approx(60.0)


def test_aggregate_net_metrics_converts_counter_to_rate():
    points = {
        "cpu": [],
        "mem": [],
        "net": [
            _net_point("hostA", "2026-04-19T10:00:00+00:00", "eth0", "bytes_recv", 0),
            _net_point("hostA", "2026-04-19T10:00:10+00:00", "eth0", "bytes_recv", 10_000),
            _net_point("hostA", "2026-04-19T10:00:20+00:00", "eth0", "bytes_recv", 30_000),
            _net_point("hostA", "2026-04-19T10:00:00+00:00", "eth0", "bytes_sent", 0),
            _net_point("hostA", "2026-04-19T10:00:10+00:00", "eth0", "bytes_sent", 1_000),
        ],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    bin_ = results[0]["bytes_in_per_sec"]
    bout = results[0]["bytes_out_per_sec"]
    # bytes_recv rates: (10000-0)/10 = 1000, (30000-10000)/10 = 2000
    assert bin_["samples"] == 2
    assert bin_["min"] == pytest.approx(1000.0)
    assert bin_["max"] == pytest.approx(2000.0)
    assert bin_["avg"] == pytest.approx(1500.0)
    # bytes_sent rate: (1000-0)/10 = 100
    assert bout["samples"] == 1
    assert bout["avg"] == pytest.approx(100.0)


def test_aggregate_net_excludes_loopback_interface():
    points = {
        "cpu": [],
        "mem": [],
        "net": [
            _net_point("hostA", "2026-04-19T10:00:00+00:00", "lo", "bytes_recv", 0),
            _net_point("hostA", "2026-04-19T10:00:10+00:00", "lo", "bytes_recv", 1_000_000),
        ],
    }
    assert aggregate_host_metrics(points) == []


def test_aggregate_net_skips_counter_resets():
    points = {
        "cpu": [],
        "mem": [],
        "net": [
            _net_point("hostA", "2026-04-19T10:00:00+00:00", "eth0", "bytes_recv", 10_000),
            # counter reset — smaller value
            _net_point("hostA", "2026-04-19T10:00:10+00:00", "eth0", "bytes_recv", 500),
            _net_point("hostA", "2026-04-19T10:00:20+00:00", "eth0", "bytes_recv", 2_500),
        ],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    bin_ = results[0]["bytes_in_per_sec"]
    # Only one valid delta: 2500-500 = 2000 over 10s
    assert bin_["samples"] == 1
    assert bin_["avg"] == pytest.approx(200.0)


def test_aggregate_diskio_converts_counters_to_rates():
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 0),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "read_bytes", 10_000
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:20+00:00", "sdb", "read_bytes", 30_000
            ),
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "write_bytes", 0),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "write_bytes", 1_000
            ),
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "reads", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "sdb", "reads", 50),
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "writes", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "sdb", "writes", 20),
        ],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    sdb = results[0]["disk_io_by_device"]["sdb"]

    read = sdb["read_bytes_per_sec"]
    # read_bytes rates: (10000-0)/10 = 1000, (30000-10000)/10 = 2000
    assert read["samples"] == 2
    assert read["min"] == pytest.approx(1000.0)
    assert read["max"] == pytest.approx(2000.0)
    assert read["avg"] == pytest.approx(1500.0)

    assert sdb["write_bytes_per_sec"]["avg"] == pytest.approx(100.0)
    assert sdb["reads_per_sec"]["avg"] == pytest.approx(5.0)
    assert sdb["writes_per_sec"]["avg"] == pytest.approx(2.0)


def test_aggregate_diskio_skips_counter_resets():
    """diskio counters reset when the VM reboots; a negative delta is dropped."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "sda", "write_bytes", 10_000
            ),
            # reboot — counter restarts
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sda", "write_bytes", 500
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:20+00:00", "sda", "write_bytes", 2_500
            ),
        ],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    write = results[0]["disk_io_by_device"]["sda"]["write_bytes_per_sec"]
    # Only one valid delta: (2500-500)/10 = 200
    assert write["samples"] == 1
    assert write["avg"] == pytest.approx(200.0)


def test_aggregate_diskio_keeps_devices_separate():
    """Devices are never averaged together.

    A host's root disk and its node storage volume are not interchangeable:
    an idle root disk averaged against a busy data volume describes neither.
    Rates are also computed WITHIN a device, so the differing absolute counter
    values below can never produce a cross-device delta.
    """
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            # Root disk: idle, but a large absolute counter.
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "sda", "read_bytes", 1_000_000
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sda", "read_bytes", 1_000_000
            ),
            # Data volume: busy, small absolute counter.
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 0),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "read_bytes", 50_000
            ),
        ],
    }
    results = aggregate_host_metrics(points)
    devices = results[0]["disk_io_by_device"]
    assert set(devices) == {"sda", "sdb"}
    # Idle root disk stays 0; it does not dilute the busy data volume.
    assert devices["sda"]["read_bytes_per_sec"]["avg"] == pytest.approx(0.0)
    assert devices["sdb"]["read_bytes_per_sec"]["avg"] == pytest.approx(5000.0)


def test_aggregate_diskio_marks_the_node_storage_volume():
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sda", "read_bytes", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "sda", "read_bytes", 10),
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 0,
                volume_role="node-storage",
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "read_bytes", 50_000,
                volume_role="node-storage",
            ),
        ],
    }
    entry = aggregate_host_metrics(points)[0]
    devices = entry["disk_io_by_device"]
    assert devices["sdb"]["volume_role"] == "node-storage"
    assert devices["sda"]["volume_role"] is None

    device, stats = influx.node_storage_device(entry)
    assert device == "sdb"
    assert stats["read_bytes_per_sec"]["avg"] == pytest.approx(5000.0)


def test_node_storage_device_is_none_when_nothing_is_tagged():
    """Client VMs have no node storage, and hosts provisioned before the
    volume_role tag existed carry no role at all."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "vda", "read_bytes", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "vda", "read_bytes", 10),
        ],
    }
    entry = aggregate_host_metrics(points)[0]
    assert influx.node_storage_device(entry) is None


def test_only_the_exact_node_storage_role_marks_the_volume():
    """A stray volume_role value must not be read as the storage volume."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "vda", "read_bytes", 0,
                volume_role="vda",
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "vda", "read_bytes", 10,
                volume_role="vda",
            ),
        ],
    }
    entry = aggregate_host_metrics(points)[0]
    assert entry["disk_io_by_device"]["vda"]["volume_role"] is None
    assert influx.node_storage_device(entry) is None


def test_aggregate_diskio_ignores_points_without_a_device_tag():
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            {
                "_time": datetime.fromisoformat(
                    "2026-04-19T10:00:00+00:00"
                ).astimezone(timezone.utc),
                "host": "hostA",
                "_measurement": "diskio",
                "_field": "read_bytes",
                "_value": "0",
            },
        ],
    }
    assert aggregate_host_metrics(points) == []


def test_aggregate_absent_diskio_yields_an_empty_device_map():
    """Existing callers that pass no diskio points still get the key."""
    points = {
        "cpu": [_cpu_point("hostA", "2026-04-19T10:00:00+00:00", 80.0)],
        "mem": [],
        "net": [],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    assert results[0]["disk_io_by_device"] == {}


def test_aggregate_diskio_reports_a_device_with_only_one_point():
    """A single point yields no delta, so the device is present but empty
    rather than absent — the device existed, it just had no measurable rate."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 5),
            # A second field with two points, so the host has a tracked window.
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "sdb", "writes", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "sdb", "writes", 10),
        ],
    }
    sdb = aggregate_host_metrics(points)[0]["disk_io_by_device"]["sdb"]
    assert sdb["read_bytes_per_sec"]["samples"] == 0
    assert sdb["read_bytes_per_sec"]["avg"] is None
    assert sdb["writes_per_sec"]["avg"] == pytest.approx(1.0)


def test_serialize_host_entry_persists_disk_io_by_device():
    """`_serialize_host_entry` re-lists keys explicitly, so an omission there
    silently drops the metric on the way to Postgres."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 0,
                volume_role="node-storage",
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "read_bytes", 10_000,
                volume_role="node-storage",
            ),
        ],
    }
    aggregate = aggregate_host_metrics(points)[0]
    serialized = record_analysis_mod._serialize_host_entry(aggregate)
    sdb = serialized["disk_io_by_device"]["sdb"]
    assert sdb["volume_role"] == "node-storage"
    assert sdb["read_bytes_per_sec"]["avg"] == pytest.approx(1000.0)
    for key in ("write_bytes_per_sec", "reads_per_sec", "writes_per_sec"):
        assert key in sdb


def test_serialized_disk_io_is_json_round_trippable():
    """host_metrics is a JSON column; the nested device map must survive it."""
    points = {
        "cpu": [],
        "mem": [],
        "net": [],
        "diskio": [
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "dm-0", "read_bytes", 0,
                volume_role="node-storage",
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "dm-0", "read_bytes", 100,
                volume_role="node-storage",
            ),
        ],
    }
    serialized = record_analysis_mod._serialize_host_entry(
        aggregate_host_metrics(points)[0]
    )
    assert json.loads(json.dumps(serialized)) == serialized


def test_aggregate_tracks_start_and_end_per_host():
    points = {
        "cpu": [
            _cpu_point("hostA", "2026-04-19T10:00:00+00:00", 90.0),
            _cpu_point("hostA", "2026-04-19T10:05:00+00:00", 85.0),
        ],
        "mem": [
            _mem_point("hostA", "2026-04-19T09:58:00+00:00", 40.0),
        ],
        "net": [],
    }
    results = aggregate_host_metrics(points)
    assert len(results) == 1
    assert results[0]["metrics_started_at"] == datetime.fromisoformat(
        "2026-04-19T09:58:00+00:00"
    )
    assert results[0]["metrics_ended_at"] == datetime.fromisoformat(
        "2026-04-19T10:05:00+00:00"
    )


def test_aggregate_multi_host_produces_sorted_results():
    base = datetime.fromisoformat("2026-04-19T10:00:00+00:00")
    points = {
        "cpu": [
            {
                "_time": base,
                "host": "hostZ",
                "_measurement": "cpu",
                "cpu": "cpu-total",
                "_field": "usage_idle",
                "_value": "90.0",
            },
            {
                "_time": base + timedelta(seconds=10),
                "host": "hostA",
                "_measurement": "cpu",
                "cpu": "cpu-total",
                "_field": "usage_idle",
                "_value": "50.0",
            },
        ],
        "mem": [],
        "net": [],
    }
    results = aggregate_host_metrics(points)
    assert [r["host"] for r in results] == ["hostA", "hostZ"]


def test_record_analysis_window_defaults_to_full_run(monkeypatch):
    """Without --window the command records the whole run (Flux start '0')."""
    captured = {}

    def _capture(args):
        captured["window"] = args.window

    monkeypatch.setattr(record_analysis_mod, "cmd_record_analysis", _capture)
    main(["deployments", "record-analysis", "--name", "DEV-01"])
    assert captured["window"] == "0"


def test_record_analysis_window_flag_is_honoured(monkeypatch):
    """An explicit --window narrows the range and is passed through verbatim."""
    captured = {}

    def _capture(args):
        captured["window"] = args.window

    monkeypatch.setattr(record_analysis_mod, "cmd_record_analysis", _capture)
    main(["deployments", "record-analysis", "--name", "DEV-01", "--window=-6h"])
    assert captured["window"] == "-6h"


def test_fetch_host_metric_points_defaults_to_epoch(monkeypatch):
    """The default start '0' is formatted into every host-metric Flux range."""
    seen = []

    def _fake_stream_flux(url, token, org, flux, timeout=30):
        seen.append(flux)
        return iter(())

    monkeypatch.setattr(influx, "_stream_flux", _fake_stream_flux)
    fetch_host_metric_points(
        url="http://x", token="t", org="ant", bucket="testnet-metrics"
    )
    # cpu / mem / net / disk / diskio / procstat / ant_node_memory
    assert len(seen) == 7
    assert all("range(start: 0," in flux for flux in seen)
    assert any('r._measurement == "disk"' in flux for flux in seen)
    assert any('r._measurement == "diskio"' in flux for flux in seen)
    assert any('r._measurement == "procstat"' in flux for flux in seen)
    assert any('r._measurement == "ant_node_memory"' in flux for flux in seen)
    # Every range MUST carry an explicit far-future stop. Flux defaults stop to
    # now(), which silently discarded forward clock-skewed hosts entirely.
    assert all(f"stop: {influx.FLUX_FAR_FUTURE}" in flux for flux in seen)


def test_fetch_host_metric_points_honours_explicit_start(monkeypatch):
    seen = []

    def _fake_stream_flux(url, token, org, flux, timeout=30):
        seen.append(flux)
        return iter(())

    monkeypatch.setattr(influx, "_stream_flux", _fake_stream_flux)
    fetch_host_metric_points(
        url="http://x", token="t", org="ant", bucket="testnet-metrics", start="-6h"
    )
    assert all("range(start: -6h," in flux for flux in seen)
    assert all(f"stop: {influx.FLUX_FAR_FUTURE}" in flux for flux in seen)


def _run_cmd_record_analysis(monkeypatch, repo_obj, diskio_points: list[dict]):
    """Drive the real `cmd_record_analysis` against a fake Influx + repo.

    Exercises the summary-table rendering, which the other command-level tests
    stub out entirely.
    """
    # cmd_record_analysis builds its own Console(); without this Rich falls back
    # to 80 columns under pytest capture and squeezes the values we assert on.
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(record_analysis_mod, "get_repository", lambda: repo_obj)
    monkeypatch.setattr(
        record_analysis_mod,
        "iter_host_metric_points",
        lambda **kwargs: {"cpu": [], "mem": [], "net": [], "diskio": diskio_points},
    )

    args = SimpleNamespace(
        name="DEV-TEST",
        influx_url="http://influx",
        influx_token="token",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        monitoring_name=None,
        window="0",
        summary=None,
    )
    record_analysis_mod.cmd_record_analysis(args)


def test_record_analysis_summary_table_reports_the_storage_volume(
    monkeypatch, repo, capsys
):
    r, _ = repo
    r.create(name="DEV-TEST", deploy_args={})
    _run_cmd_record_analysis(
        monkeypatch,
        r,
        [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "vda", "read_bytes", 0),
            _diskio_point("hostA", "2026-04-19T10:00:10+00:00", "vda", "read_bytes", 0),
            _diskio_point(
                "hostA", "2026-04-19T10:00:00+00:00", "sdb", "read_bytes", 0,
                volume_role="node-storage",
            ),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "sdb", "read_bytes", 20 * 1024**2,
                volume_role="node-storage",
            ),
        ],
    )
    out = capsys.readouterr().out
    # Names the storage device and reports ITS rate -- not an average that the
    # idle root disk would have halved.
    assert "sdb" in out
    assert "2.00 MB/s" in out

    # Both devices still reach Postgres.
    row = r.get_host_result_for_deployment(r.get_by_name("DEV-TEST").id)
    assert set(row.host_metrics[0]["disk_io_by_device"]) == {"sdb", "vda"}


def test_record_analysis_summary_table_handles_no_storage_volume(
    monkeypatch, repo, capsys
):
    """A client VM (or a host provisioned before the volume_role tag) has no
    node-storage device; the table must render rather than raise."""
    r, _ = repo
    r.create(name="DEV-TEST", deploy_args={})
    _run_cmd_record_analysis(
        monkeypatch,
        r,
        [
            _diskio_point("hostA", "2026-04-19T10:00:00+00:00", "vda", "read_bytes", 0),
            _diskio_point(
                "hostA", "2026-04-19T10:00:10+00:00", "vda", "read_bytes", 1_000
            ),
        ],
    )
    out = capsys.readouterr().out
    assert "hostA" in out


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session), session
    session.close()
    engine.dispose()


def test_upsert_host_result_inserts_then_updates_preserving_summary(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})

    first = [
        {
            "host": "hostA",
            "metrics_started_at": "2026-04-19T10:00:00+00:00",
            "metrics_ended_at": "2026-04-19T11:00:00+00:00",
            "cpu_percent": {"avg": 10.0, "min": 5.0, "max": 20.0, "samples": 6},
            "mem_percent": {"avg": 40.0, "min": 30.0, "max": 50.0, "samples": 6},
            "bytes_in_per_sec": {"avg": 1000.0, "min": 500.0, "max": 2000.0, "samples": 5},
            "bytes_out_per_sec": {"avg": 100.0, "min": 50.0, "max": 200.0, "samples": 5},
        }
    ]
    started = datetime.fromisoformat("2026-04-19T10:00:00+00:00")
    ended = datetime.fromisoformat("2026-04-19T11:00:00+00:00")

    row = r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=first,
        metrics_started_at=started,
        metrics_ended_at=ended,
        summary="initial summary",
    )
    assert row.id is not None
    assert row.summary == "initial summary"
    assert row.host_metrics[0]["host"] == "hostA"

    # Re-upsert without a summary — existing summary must be preserved.
    second = [{**first[0], "cpu_percent": {"avg": 15.0, "min": 5.0, "max": 25.0, "samples": 10}}]
    row2 = r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=second,
        metrics_started_at=started,
        metrics_ended_at=ended,
        summary=None,
    )
    assert row2.id == row.id
    assert row2.summary == "initial summary"
    assert row2.host_metrics[0]["cpu_percent"]["avg"] == 15.0


def test_upsert_host_result_is_unique_per_deployment(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    started = datetime.fromisoformat("2026-04-19T10:00:00+00:00")
    ended = datetime.fromisoformat("2026-04-19T11:00:00+00:00")

    r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=[],
        metrics_started_at=started,
        metrics_ended_at=ended,
    )
    r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=[{"host": "hostA"}],
        metrics_started_at=started,
        metrics_ended_at=ended,
    )

    fetched = r.get_host_result_for_deployment(deployment.id)
    assert fetched is not None
    assert fetched.host_metrics == [{"host": "hostA"}]


def test_set_host_result_summary_updates_existing(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    started = datetime.fromisoformat("2026-04-19T10:00:00+00:00")
    ended = datetime.fromisoformat("2026-04-19T11:00:00+00:00")

    r.upsert_host_result(
        deployment_id=deployment.id,
        host_metrics=[],
        metrics_started_at=started,
        metrics_ended_at=ended,
    )

    updated = r.set_host_result_summary(deployment.id, "the tested change worked")
    assert updated.summary == "the tested change worked"


def test_set_host_result_summary_raises_when_no_row(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    with pytest.raises(ValueError):
        r.set_host_result_summary(deployment.id, "nope")


def test_merge_deploy_args_adds_new_keys(repo):
    r, _ = repo
    deployment = r.create(
        name="DEV-MERGE", deploy_args={"bootstrap_count": 3, "name": "DEV-MERGE"}
    )

    updated = r.merge_deploy_args(deployment.id, {"node_count": 12})

    assert updated.deploy_args == {
        "bootstrap_count": 3,
        "name": "DEV-MERGE",
        "node_count": 12,
    }


def test_merge_deploy_args_overwrites_existing_keys(repo):
    r, _ = repo
    deployment = r.create(name="DEV-MERGE", deploy_args={"node_count": 5})

    updated = r.merge_deploy_args(deployment.id, {"node_count": 9})

    assert updated.deploy_args["node_count"] == 9


def test_merge_deploy_args_handles_empty_starting_args(repo):
    r, _ = repo
    deployment = r.create(name="DEV-EMPTY", deploy_args={})

    updated = r.merge_deploy_args(deployment.id, {"kind": "testnet"})

    assert updated.deploy_args == {"kind": "testnet"}


def test_merge_deploy_args_raises_when_deployment_missing(repo):
    r, _ = repo
    with pytest.raises(ValueError):
        r.merge_deploy_args(99999, {"node_count": 1})


def test_parse_flux_csv_memory_is_bounded_per_row():
    """Peak memory per parsed row must stay small, not scale with column count.

    This is the regression guard for the OOM that made `record-analysis`
    unrunnable on a 990-node run: it needed ~33 GB (23 GB RSS + 10 GB swap) and
    was killed three times. Cause was a dict of every CSV column plus a fresh
    `str` per column per row.

    Measured with `tracemalloc` on the retained rows rather than by timing, so
    it fails for the right reason. The bound is deliberately loose — this
    catches a return to keep-everything (~1.4 KB/row), not a 10% drift.
    """
    import tracemalloc

    n_rows = 20_000
    header = (
        ",result,table,_start,_stop,_time,host,host_role,region,"
        "_measurement,_field,_value\n"
    )
    body = header + "".join(
        f",_result,0,2026-04-19T00:00:00Z,2026-04-20T00:00:00Z,"
        f"2026-04-19T10:00:{i % 60:02d}Z,host{i % 40},NODE,lon1,cpu,"
        f"usage_idle,{i % 100}.0\n"
        for i in range(n_rows)
    )

    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    rows = _parse_flux_csv(body)
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()

    assert len(rows) == n_rows
    per_row = (after - before) / n_rows
    assert per_row < 500, f"{per_row:.0f} B/row retained; expected well under 500"


def test_parse_flux_csv_handles_multiple_tables_with_blank_separators():
    """A blank line ends a table; the next row is the NEXT table's header.

    Flux returns one table per series, and consecutive tables do not share a
    column layout. Losing the blank separators makes every table after the
    first parse against the previous header, so columns mismap and rows are
    silently discarded.
    """
    body = (
        ",result,table,_time,host,_measurement,_field,_value\n"
        ",_result,0,2026-04-19T10:00:00Z,hostA,cpu,usage_idle,95.0\n"
        "\n"
        ",result,table,_time,host,_measurement,_field,node_service,_value\n"
        ",_result,1,2026-04-19T10:00:00Z,hostB,procstat,memory_rss,ant-node-1,1024\n"
    )
    rows = _parse_flux_csv(body)
    assert len(rows) == 2
    assert rows[0]["host"] == "hostA"
    assert rows[1]["host"] == "hostB"
    # The second table has an extra column before _value; if the header had not
    # reset, _value would read as the service name.
    assert rows[1]["_value"] == "1024"
    assert rows[1]["node_service"] == "ant-node-1"


def test_stream_preserves_blank_table_separators():
    """The streaming reader must yield blank lines, unlike `iter_lines()`.

    `requests.Response.iter_lines()` discards empty lines. Using it here cut a
    real run's stored buckets from 273,714 to 2,558 and reported
    `node_outbound_bytes_total` as 0 instead of 142.9 TB, while exiting 0.
    """
    from saorsa_deploy.influx import iter_response_lines

    body = (
        ",result,table,_time,host,_measurement,_field,_value\n"
        ",_result,0,2026-04-19T10:00:00Z,hostA,cpu,usage_idle,95.0\n"
        "\n"
        ",result,table,_time,host,_measurement,_field,_value\n"
        ",_result,1,2026-04-19T10:00:00Z,hostB,mem,used_percent,42.0\n"
    )

    class _Resp:
        def iter_content(self, chunk_size=None, decode_unicode=False):
            # Deliberately split mid-line to prove reassembly across chunks.
            for i in range(0, len(body), 7):
                yield body[i : i + 7]

    lines = list(iter_response_lines(_Resp()))
    assert "" in lines, "blank table separator was dropped"
    assert lines == body.split("\n")[:-1]

    # And end to end: parsing the streamed lines matches parsing the body.
    assert _parse_flux_csv(iter(lines)) == _parse_flux_csv(body)


# ---------------------------------------------------------------------------
# Which deployment the analysis is recorded against
# ---------------------------------------------------------------------------


class _ReusedNameRepo:
    """Registry where a name is reused and the newest run is already destroyed.

    Mirrors the real `STG-01` on 2026-08-12: run 513 had just been torn down, so
    `get_active_by_name` (which excludes DESTROYED/FAILED) skipped it and every
    other finished run, returning 442 — an aborted deploy from a month earlier.
    """

    def __init__(self):
        self.recorded_against = None
        self._rows = {
            442: type("D", (), {"id": 442, "name": "STG-01"})(),   # old, pending
            513: type("D", (), {"id": 513, "name": "STG-01"})(),   # newest, destroyed
        }

    def get_active_by_name(self, name):
        return self._rows[442]  # the only non-terminal row

    def get_by_name(self, name):
        return self._rows[513]  # newest by created_at

    def get(self, deployment_id):
        return self._rows.get(deployment_id)

    def upsert_host_result(self, **kwargs):
        self.recorded_against = kwargs["deployment_id"]

    def replace_host_metric_buckets(self, deployment_id, buckets):
        return 0

    def list_upload_records_for_deployment(self, deployment_id):
        return []

    def list_download_records_for_deployment(self, deployment_id):
        return []

    def upsert_derived_metric(self, **kwargs):
        return None


def _resolve_args(**overrides):
    base = dict(
        name=None,
        deployment_id=None,
        monitoring_name="STG-01",
        influx_url="http://x",
        influx_token="t",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        window="0",
        bucket_secs=0,
        summary=None,
        derived=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _drive_resolution(monkeypatch, args):
    repo = _ReusedNameRepo()
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(record_analysis_mod, "get_repository", lambda: repo)
    monkeypatch.setattr(
        record_analysis_mod,
        "iter_host_metric_points",
        lambda **kwargs: {
            "cpu": [
                {
                    "_time": datetime(2026, 8, 11, 22, tzinfo=timezone.utc),
                    "host": "h1",
                    "_field": "usage_idle",
                    "_value": "90.0",
                }
            ]
        },
    )
    record_analysis_mod.cmd_record_analysis(args)
    return repo


def test_record_analysis_by_name_takes_the_newest_run_not_the_newest_active(
    monkeypatch,
):
    """A destroyed newest run must still win over an older non-terminal one.

    The old `get_active_by_name() or get_by_name()` idiom wrote run 513's
    metrics onto 442, an aborted deploy, and derived its per-GB denominators as
    0.0 from 442's empty per-op records — while printing success and exiting 0.
    Newly reachable because compute is now destroyed before extraction, which
    makes the analysed run terminal at analysis time.
    """
    repo = _drive_resolution(monkeypatch, _resolve_args(name="STG-01"))
    assert repo.recorded_against == 513


def test_record_analysis_id_takes_precedence_over_name(monkeypatch):
    repo = _drive_resolution(
        monkeypatch, _resolve_args(name="STG-01", deployment_id=442)
    )
    assert repo.recorded_against == 442


def test_record_analysis_requires_a_name_or_an_id(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _drive_resolution(monkeypatch, _resolve_args())
    assert excinfo.value.code == 1
