from datetime import datetime, timedelta, timezone

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


def test_parse_flux_csv_keeps_every_column_and_parses_time():
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

    def _fake_run_flux(url, token, org, flux, timeout=30):
        seen.append(flux)
        return ""

    monkeypatch.setattr(influx, "_run_flux", _fake_run_flux)
    fetch_host_metric_points(
        url="http://x", token="t", org="ant", bucket="testnet-metrics"
    )
    assert len(seen) == 3
    assert all("range(start: 0)" in flux for flux in seen)


def test_fetch_host_metric_points_honours_explicit_start(monkeypatch):
    seen = []

    def _fake_run_flux(url, token, org, flux, timeout=30):
        seen.append(flux)
        return ""

    monkeypatch.setattr(influx, "_run_flux", _fake_run_flux)
    fetch_host_metric_points(
        url="http://x", token="t", org="ant", bucket="testnet-metrics", start="-6h"
    )
    assert all("range(start: -6h)" in flux for flux in seen)


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
