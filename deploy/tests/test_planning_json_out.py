"""`--json-out` on the commands the daily-planning report reads.

The report renders a day's fetched data without re-running anything, and until
these flags existed its only input for the transfer tests and the disk survey
was the Rich tables. Those truncate the host column to the terminal width
(`cntb-US-cent…`) and wrap a timestamp onto a second line, so a consumer keying
on a host or deriving a cycle boundary from a date was parsing lossy output.

What matters here is the contract the consumer depends on:

* the records go out unaggregated, with full host names and real timestamps;
* an empty result still writes, because "ran, nothing recorded" and "never got
  that far" are different states that a missing file cannot distinguish;
* nothing is written when the flag is absent.
"""

import json
from argparse import Namespace
from datetime import datetime, timezone
from types import SimpleNamespace

from rich.console import Console

from saorsa_deploy.cmd.print_file_ops import _emit_ops_json, _json_point
from saorsa_deploy.cmd.prod_restart_telegraf import _emit_probe_json
from saorsa_deploy.cmd.prod_services import _survey_as_dict

QUIET = Console(quiet=True)

POINT = {
    "_measurement": "download",
    "_time": datetime(2026, 10, 16, 9, 43, 0, tzinfo=timezone.utc),
    "host": "PROD-DL-01-ant-client-download-fsn1-1",
    "start_ns": 1_760_000_000_000_000_000,
    "status": 0,
    "total_ms": 2_104_000,
    "file_size": 27_120_000_000,
}


def _read(path):
    with open(path) as handle:
        return json.load(handle)


def test_point_time_is_serialised_and_other_fields_pass_through():
    out = _json_point(POINT)

    assert out["_time"] == "2026-10-16T09:43:00+00:00"
    assert out["host"] == POINT["host"]
    assert out["status"] == 0
    assert out["total_ms"] == 2_104_000


def test_point_passes_through_a_field_the_helper_does_not_know():
    # A client that starts emitting a new field must reach the consumer
    # without a change here, so the serialiser allowlists nothing.
    out = _json_point({**POINT, "gas_cost_wei": None, "brand_new": 7})

    assert out["brand_new"] == 7
    assert "gas_cost_wei" in out


def test_ops_json_carries_untruncated_host_and_the_record_count(tmp_path):
    target = tmp_path / "dl.json"
    args = Namespace(json_out=str(target))

    _emit_ops_json(
        args,
        QUIET,
        deployment="PROD-DL-01",
        kind="prod-continuous-downloader",
        op_type="download",
        source="influx",
        points=[POINT],
        window="-30d",
    )

    payload = _read(target)
    assert payload["deployment"] == "PROD-DL-01"
    assert payload["op_type"] == "download"
    assert payload["window"] == "-30d"
    assert payload["count"] == 1
    assert payload["ops"][0]["host"] == "PROD-DL-01-ant-client-download-fsn1-1"


def test_ops_json_writes_an_empty_result_rather_than_nothing(tmp_path):
    # A cycle that has not run yet must be distinguishable from a command that
    # failed before it queried. Only a written file with count 0 says the first.
    target = tmp_path / "empty.json"

    _emit_ops_json(
        Namespace(json_out=str(target)),
        QUIET,
        deployment="PROD-WDL-01",
        kind="prod-continuous-downloader",
        op_type="download",
        source="influx",
        points=[],
    )

    payload = _read(target)
    assert payload["count"] == 0
    assert payload["ops"] == []


def test_ops_json_writes_nothing_without_the_flag(tmp_path):
    target = tmp_path / "unwanted.json"

    _emit_ops_json(
        Namespace(json_out=None),
        QUIET,
        deployment="PROD-DL-01",
        kind="x",
        op_type="download",
        source="influx",
        points=[POINT],
    )

    assert not target.exists()


def test_probe_json_keeps_unreachable_hosts_with_their_error(tmp_path):
    # A host missing from the probe makes the fleet total short. Dropping it
    # would leave the consumer reading a short total as a real one.
    target = tmp_path / "probe.json"
    results = [
        {
            "vm": SimpleNamespace(name="hz-fsn1-2", provider="hetzner"),
            "held": {
                "held_bytes": 1_717_986_918,
                "fds": 37,
                "free_bytes": 187_000_000_000,
                "state": "active",
            },
        },
        {
            "vm": SimpleNamespace(name="ovh-BHS5-9", provider="ovh"),
            "error": "ssh: connect timed out",
        },
    ]

    _emit_probe_json(
        Namespace(json_out=str(target), dry_run=True),
        QUIET,
        "ant-prod-01",
        results,
        min_held=0,
        total_held=1_717_986_918,
    )

    payload = _read(target)
    assert payload["host_count"] == 2
    assert payload["reachable_count"] == 1
    assert payload["total_held_bytes"] == 1_717_986_918
    assert payload["dry_run"] is True

    unreachable = [h for h in payload["hosts"] if h["error"]]
    assert len(unreachable) == 1
    assert unreachable[0]["host"] == "ovh-BHS5-9"
    assert unreachable[0]["held_bytes"] is None


def test_probe_json_writes_nothing_without_the_flag(tmp_path):
    target = tmp_path / "unwanted.json"

    _emit_probe_json(Namespace(json_out=None), QUIET, "ant-prod-01", [], 0, 0)

    assert not target.exists()


def test_survey_as_dict_carries_the_coverage_fields():
    # `failed_hosts` NULL means the failed set is unknown, which is not the
    # same as an empty dict, so the field has to survive the round trip.
    survey = SimpleNamespace(
        id=72,
        surveyed_at=datetime(2026, 10, 16, 16, 2, 25, tzinfo=timezone.utc),
        host_count=66,
        service_count=767,
        total_data_bytes=17_287_933_688_156,
        attempted_host_count=66,
        failed_hosts=None,
    )

    out = _survey_as_dict(survey)

    assert out["id"] == 72
    assert out["surveyed_at"] == "2026-10-16T16:02:25+00:00"
    assert out["service_count"] == 767
    assert out["attempted_host_count"] == 66
    assert out["failed_hosts"] is None


def test_survey_as_dict_of_nothing_is_none():
    assert _survey_as_dict(None) is None
