"""Browser-download rounds: Influx -> Postgres -> the views that read them.

The `browser_downloaders` role writes one `browser_download` point per Chromium
instance per round and leaves a per-dial trace on the VM. Both are perishable --
InfluxDB retention is bounded and the VMs are destroyed after the rounds -- so
what these tests pin is that everything which has to survive teardown does:
the rows, the extension that distinguishes a cohort added as an extension from
the deployment's own tier, and the pointer to each round's archived trace.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models import OpRecordStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.print_file_ops import summarise_browser_rounds
from saorsa_deploy.cmd.print_recorded_results import (
    _print_browser_downloads_section,
)
from saorsa_deploy.cmd.record_results import _record_browser_downloads
from saorsa_deploy.influx import parse_browser_download_csv

T0 = datetime(2026, 10, 18, 9, 10, tzinfo=timezone.utc)
#: 2987191 x 600 = 2026-10-18T09:10:00Z, the boundary the harness stamps.
ROUND = 2987191
BOUNDARY_NS = ROUND * 600 * 1_000_000_000

HOST = "DEV-03-browsers-ant-client-browser-download-lon1-1"

_CSV_HEADER = (
    ",result,table,_time,_measurement,host,host_role,region,round_id,instance,"
    "hot_file_index,testnet_name,address,connect_ms,error_kind,error_text,"
    "file_size,first_reads,refused_dials,seeds,stalls,start_ns,started_late_ms,"
    "status,total_ms"
)


def _csv(*rows: str) -> str:
    return "\n".join([_CSV_HEADER, *rows]) + "\n"


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyDeploymentRepository(session)


# ---------------------------------------------------------------------------
# Parsing the Influx CSV
# ---------------------------------------------------------------------------


def test_a_browser_point_parses_with_every_field():
    body = _csv(
        f",_result,0,2026-10-18T09:10:09Z,browser_download,{HOST},browser_downloader,"
        f"lon1,{ROUND},2,1,DEV-03,abcd,812,,,52428800,"
        f'"[{{""chunk"": ""c1"", ""holder"": ""p1"", ""known_holders"": 7}}]",'
        f'0,"seed-a,seed-b",1,{BOUNDARY_NS},480,success,9120'
    )
    point = parse_browser_download_csv(body)[0]
    assert point["host"] == HOST
    assert point["round_id"] == ROUND
    assert point["instance"] == 2
    assert point["hot_file_index"] == 1
    assert point["status"] == "success"
    assert point["total_ms"] == 9120
    assert point["connect_ms"] == 812
    assert point["start_ns"] == BOUNDARY_NS
    assert point["started_late_ms"] == 480
    assert point["seeds"] == ["seed-a", "seed-b"]
    assert point["stalls"] == 1
    assert point["refused_dials"] == 0
    assert point["first_reads"] == [{"chunk": "c1", "holder": "p1", "known_holders": 7}]


def test_a_point_missing_the_round_or_instance_is_dropped():
    """Both are tags and both are the operation's identity, so a point without
    either cannot be stored or counted -- it would collide with whatever else
    that host emitted in the same second."""
    body = _csv(
        f",_result,0,2026-10-18T09:10:09Z,browser_download,{HOST},browser_downloader,"
        f"lon1,,2,1,DEV-03,abcd,800,,,52428800,[],0,seed-a,0,{BOUNDARY_NS},0,success,900",
        f",_result,0,2026-10-18T09:10:09Z,browser_download,{HOST},browser_downloader,"
        f"lon1,{ROUND},,1,DEV-03,abcd,800,,,52428800,[],0,seed-a,0,{BOUNDARY_NS},0,success,900",
    )
    assert parse_browser_download_csv(body) == []


def test_a_skip_parses_with_no_hot_file_index():
    """The harness drops an empty tag rather than writing it blank, so a round
    that ran before the operator selected carries no index at all."""
    header = _CSV_HEADER.replace(",hot_file_index", "")
    body = (
        header + "\n"
        f",_result,0,2026-10-18T09:00:00Z,browser_download,{HOST},browser_downloader,"
        f"lon1,{ROUND},3,DEV-03,,0,no_hot_files,,0,[],0,seed-a,0,{BOUNDARY_NS},12,skipped,0\n"
    )
    point = parse_browser_download_csv(body)[0]
    assert point["hot_file_index"] is None
    assert point["status"] == "skipped"
    assert point["error_kind"] == "no_hot_files"


def test_unparseable_first_reads_read_as_none_not_empty():
    """None and `[]` are different facts: "the harness recorded no first reads"
    must not be stored as "this download read nothing"."""
    body = _csv(
        f",_result,0,2026-10-18T09:10:09Z,browser_download,{HOST},browser_downloader,"
        f"lon1,{ROUND},1,1,DEV-03,abcd,800,,,52428800,not-json,0,seed-a,0,"
        f"{BOUNDARY_NS},0,success,900"
    )
    assert parse_browser_download_csv(body)[0]["first_reads"] is None


def test_another_measurement_is_ignored():
    body = _csv(
        f",_result,0,2026-10-18T09:10:09Z,download,{HOST},browser_downloader,"
        f"lon1,{ROUND},1,1,DEV-03,abcd,800,,,52428800,[],0,seed-a,0,"
        f"{BOUNDARY_NS},0,success,900"
    )
    assert parse_browser_download_csv(body) == []


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _point(**extra):
    point = {
        "_time": T0,
        "host": HOST,
        "region": "lon1",
        "round_id": ROUND,
        "instance": 2,
        "hot_file_index": 1,
        "status": "success",
        "error_kind": None,
        "error_text": None,
        "address": "ab" * 32,
        "file_size": 52428800,
        "total_ms": 9120,
        "connect_ms": 812,
        "start_ns": BOUNDARY_NS,
        "started_late_ms": 480,
        "seeds": ["seed-a", "seed-b"],
        "stalls": 1,
        "refused_dials": 0,
        "first_reads": [{"chunk": "c1", "holder": "p1", "known_holders": 7}],
    }
    point.update(extra)
    return point


def _record(repo, deployment, points, tiers=None, console=None, hot_files=None):
    """Drive `_record_browser_downloads` over scripted points, offline.

    Three dependencies are patched, not one: besides the Influx query, the
    client-tier scan and the hot-file read both reach S3, so an unpatched test
    makes a live call and fails wherever there are no credentials, which is CI.

    `hot_files` is the document the snapshot step finds in S3 (None = no
    selection there, which is also what a torn-down deployment looks like). The
    snapshot writes through the real `record_deploy_args_update`, so a test can
    assert on the deployment's own `deploy_args`.
    """
    args = SimpleNamespace(name=deployment.name)
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_browser_download_points",
            return_value=points,
        ),
        patch(
            "saorsa_deploy.cmd.record_results._client_tiers",
            return_value=tiers if tiers is not None else [],
        ),
        patch("saorsa_deploy.hot_files.load_hot_files", return_value=hot_files),
        patch("saorsa_deploy.registry.get_repository", return_value=repo),
    ):
        _record_browser_downloads(
            repo,
            deployment,
            args,
            console or Console(),
            "http://influx",
            "token",
            "ant",
        )


def _deployment(repo, name="DEV-03"):
    return repo.create(name=name, deploy_args={"kind": "testnet", "bootstrap_count": 1})


def _browser_client():
    """The client-state entry whose hostname is :data:`HOST`."""
    return {
        "role": "browser-download",
        "region": "lon1",
        "client_index": 1,
        "instances": 4,
        "round_period_secs": 600,
    }


#: A hot-file selection as `clients select-hot-files` writes it.
_HOT_FILES = {
    "deployment": "DEV-03",
    "selected_at": "2026-10-18T08:55:00Z",
    "size_bytes": 52428800,
    "files": [
        {
            "index": 0,
            "address": "a0" * 32,
            "bytes": 52428800,
            "source_key": "upload-lon1-1.txt",
            "line": 4,
        }
    ],
}


def test_a_point_maps_onto_a_row(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.client_hostname == HOST
    assert row.round_id == ROUND
    assert row.instance == 2
    assert row.hot_file_index == 1
    assert row.status is OpRecordStatus.SUCCESS
    assert row.region == "lon1"
    assert row.total_ms == 9120
    assert row.connect_ms == 812
    assert row.started_late_ms == 480
    assert row.seeds == ["seed-a", "seed-b"]
    assert row.stalls == 1
    assert row.refused_dials == 0
    assert row.first_reads == [{"chunk": "c1", "holder": "p1", "known_holders": 7}]
    assert row.file_size_bytes == 52428800
    # `started_at` is the round's BOUNDARY, which is what makes a window
    # selection over these rows select whole rounds. The tz is reattached
    # because the SQLite fixture drops it; Postgres' `timestamptz` does not.
    assert row.started_at.replace(tzinfo=timezone.utc) == T0


def test_each_status_maps_onto_its_enum(repo):
    deployment = _deployment(repo)
    _record(
        repo,
        deployment,
        [
            _point(instance=1, status="success"),
            _point(instance=2, status="failure", error_kind="timeout"),
            _point(instance=3, status="skipped", error_kind="no_hot_files"),
        ],
    )
    rows = repo.list_browser_download_records_for_deployment(deployment.id)
    assert [r.status for r in rows] == [
        OpRecordStatus.SUCCESS,
        OpRecordStatus.FAILURE,
        OpRecordStatus.SKIPPED,
    ]


def test_an_unrecognised_status_is_counted_unusable_not_coerced(repo):
    """A status this build does not understand must not be stored as `failure`
    in the column the completion gate reads."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(status="weird")])
    assert repo.list_browser_download_records_for_deployment(deployment.id) == []


def test_a_point_with_no_start_ns_is_unusable(repo):
    """`start_ns` is the round boundary, so without it the row cannot be placed
    in a window at all."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(start_ns=None)])
    assert repo.list_browser_download_records_for_deployment(deployment.id) == []


def test_a_re_run_revises_rather_than_duplicates(repo):
    """Keyed on (deployment, host, round, instance): `record-results` is run
    repeatedly over the same Influx range during a long run."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(total_ms=9120)])
    _record(repo, deployment, [_point(total_ms=9500, stalls=3)])
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.total_ms == 9500
    assert row.stalls == 3


def test_two_instances_of_one_round_are_two_rows(repo):
    """One point per instance per round, so the instance is part of the key --
    without it a cohort of four would collapse to one row per round."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(instance=1), _point(instance=2)])
    rows = repo.list_browser_download_records_for_deployment(deployment.id)
    assert [r.instance for r in rows] == [1, 2]


def test_the_trace_key_survives_a_re_run(repo):
    """`clients fetch-browser-traces` writes it and the Influx point never
    carries it, so an unconditional assignment on upsert would erase the only
    pointer to a trace -- and the traces die with the VM."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    assert repo.set_browser_download_trace_key(
        deployment.id, HOST, ROUND, 2, "results/DEV-03/browser-traces/h/1-2.jsonl"
    )
    _record(repo, deployment, [_point(total_ms=9999)])
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.trace_s3_key == "results/DEV-03/browser-traces/h/1-2.jsonl"
    assert row.total_ms == 9999


def test_setting_a_trace_key_for_an_unrecorded_round_reports_false(repo):
    """Not an error: the trace is archived while the VMs are up, before the
    round's point has necessarily been recorded. The caller says so and the
    next `record-results` creates the row."""
    deployment = _deployment(repo)
    assert repo.set_browser_download_trace_key(deployment.id, HOST, ROUND, 2, "k") is False


def test_the_extension_is_resolved_from_the_client_tier(repo):
    """A browser cohort is normally a `kind: clients` extension added hours
    after the deployment came up, so without this its rows would be
    indistinguishable from the deployment's own tier."""
    deployment = _deployment(repo)
    tiers = [("browsers", {"clients": [_browser_client()]})]
    _record(repo, deployment, [_point()], tiers=tiers)
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.extension == "browsers"


def test_a_re_run_after_the_extension_state_is_gone_keeps_the_extension(repo):
    """The case the DEV-01 procedure actually runs: `record-results` once with
    the cohort up, then again after `clients down`. The extension's S3 client
    state is gone by then, so the live tiers resolve nothing — and writing that
    None would relabel the whole cohort as the deployment's own tier, which is
    exactly the distinction the column exists to make."""
    deployment = _deployment(repo)
    tiers = [("browsers", {"clients": [_browser_client()]})]
    _record(repo, deployment, [_point()], tiers=tiers)
    assert repo.list_browser_download_records_for_deployment(deployment.id)[0].extension == (
        "browsers"
    )

    # Second pass: the tier is torn down and nothing recorded the config either,
    # so only the upsert's COALESCE stands between the row and a NULL.
    _record(repo, deployment, [_point(total_ms=1234)], tiers=[])
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.extension == "browsers"
    assert row.total_ms == 1234


def test_the_extension_is_resolved_from_the_recorded_config_first(repo):
    """`clients provision` denormalises hostname -> extension into deploy_args
    for exactly this reason, so a re-run after teardown resolves it from there
    rather than relying on the COALESCE to save a row it never wrote."""
    deployment = repo.create(
        name="DEV-03",
        deploy_args={
            "kind": "testnet",
            "bootstrap_count": 1,
            "browser_downloaders": [
                {
                    "hostname": HOST,
                    "extension": "browsers",
                    "instances": 4,
                    "round_period_secs": 600,
                },
            ],
        },
    )
    # No live tiers at all: the cohort is gone, and this is a FIRST recording of
    # the round — so there is no existing row for a COALESCE to protect.
    _record(repo, deployment, [_point()], tiers=[])
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.extension == "browsers"


def test_the_recorded_config_wins_over_a_live_tier_that_disagrees(repo):
    """They cannot disagree in practice — the extension name is part of the
    hostname — but the order is load-bearing, so it is pinned."""
    deployment = repo.create(
        name="DEV-03",
        deploy_args={
            "kind": "testnet",
            "bootstrap_count": 1,
            "browser_downloaders": [{"hostname": HOST, "extension": "recorded"}],
        },
    )
    _record(
        repo,
        deployment,
        [_point()],
        tiers=[("live", {"clients": [_browser_client()]})],
    )
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.extension == "recorded"


# ---------------------------------------------------------------------------
# The hot-file selection snapshot
# ---------------------------------------------------------------------------


def test_the_hot_file_selection_is_snapshotted_into_deploy_args(repo):
    """The selection lives under `addresses/<deployment>/`, which the
    deployment's own `clients destroy` deletes WHOLE — while the report is meant
    to run after teardown and needs the document to say which file each round
    read. So it is snapshotted in the pass that records the rows."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    assert repo.get(deployment.id).deploy_args["browser_hot_files"] == _HOT_FILES


def test_the_snapshot_is_additive_and_survives_the_selection_being_deleted(repo):
    """A snapshot taken while the cohort was running is the answer, and there is
    no later chance to take one — so a pass that finds nothing in S3 must leave
    the earlier snapshot alone rather than clearing it."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    # Second pass after `clients destroy` wiped the address prefix.
    _record(repo, deployment, [_point()], hot_files=None)
    assert repo.get(deployment.id).deploy_args["browser_hot_files"] == _HOT_FILES


def test_an_s3_failure_on_the_snapshot_warns_and_keeps_what_was_recorded(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    console = Console(record=True, width=200)
    args = SimpleNamespace(name=deployment.name)
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_browser_download_points",
            return_value=[_point()],
        ),
        patch("saorsa_deploy.cmd.record_results._client_tiers", return_value=[]),
        patch(
            "saorsa_deploy.hot_files.load_hot_files",
            side_effect=RuntimeError("no credentials"),
        ),
        patch("saorsa_deploy.registry.get_repository", return_value=repo),
    ):
        _record_browser_downloads(repo, deployment, args, console, "http://influx", "t", "ant")
    assert "could not read the hot-file selection" in console.export_text()
    assert repo.get(deployment.id).deploy_args["browser_hot_files"] == _HOT_FILES


def test_nothing_is_snapshotted_for_a_deployment_with_no_selection(repo):
    """An ordinary testnet with a browser tier but no selection yet keeps its
    deploy_args clean, so the report's "nothing recorded" branch is reachable."""
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=None)
    assert "browser_hot_files" not in repo.get(deployment.id).deploy_args


def test_a_deployments_own_browser_vm_records_no_extension(repo):
    """NULL is what "declared in the deployment's own manifest" means, so the
    deployment's own tier must not be labelled with anything."""
    deployment = _deployment(repo)
    tiers = [
        (
            None,
            {"clients": [{"role": "browser-download", "region": "lon1", "client_index": 1}]},
        )
    ]
    host = "DEV-03-ant-client-browser-download-lon1-1"
    _record(repo, deployment, [_point(host=host)], tiers=tiers)
    (row,) = repo.list_browser_download_records_for_deployment(deployment.id)
    assert row.extension is None


def test_a_failed_influx_query_warns_and_records_nothing(repo):
    """The browser tier is one part of a testnet's results; its query failing
    must not stop the file-op and pointer records being written."""
    deployment = _deployment(repo)
    console = Console(record=True, width=200)
    args = SimpleNamespace(name=deployment.name)
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_browser_download_points",
            side_effect=RuntimeError("influx down"),
        ),
        patch("saorsa_deploy.cmd.record_results._client_tiers", return_value=[]),
    ):
        _record_browser_downloads(repo, deployment, args, console, "http://influx", "t", "ant")
    assert "browser_download query failed" in console.export_text()
    assert repo.list_browser_download_records_for_deployment(deployment.id) == []


# ---------------------------------------------------------------------------
# print-downloads --browser (the live Influx view)
# ---------------------------------------------------------------------------


def test_the_live_view_summarises_one_row_per_round():
    points = [
        _point(round_id=ROUND, instance=1, total_ms=1000, stalls=1),
        _point(round_id=ROUND, instance=2, total_ms=3000, stalls=0, refused_dials=2),
        _point(
            round_id=ROUND,
            instance=3,
            status="failure",
            error_kind="timeout",
            stalls=0,
        ),
        _point(
            round_id=ROUND,
            instance=4,
            status="skipped",
            error_kind="round_overrun",
            stalls=0,
        ),
        _point(round_id=ROUND + 1, instance=1, total_ms=2000, stalls=0),
    ]
    first, second = summarise_browser_rounds(points)
    assert first["round_id"] == ROUND
    assert first["boundary"] == T0
    assert first["instances"] == 4
    assert (first["success"], first["failure"], first["skipped"]) == (2, 1, 1)
    # Percentiles over the SUCCESSES only: a failure has no duration worth
    # comparing and a skip never ran.
    assert first["p50_ms"] == 2000.0
    assert first["error_kinds"] == {"timeout": 1}
    assert first["stalls"] == 1
    assert first["refused_dials"] == 2
    assert second["round_id"] == ROUND + 1
    assert second["instances"] == 1


def test_the_live_view_orders_rounds_oldest_first():
    points = [_point(round_id=ROUND + 2), _point(round_id=ROUND), _point(round_id=ROUND + 1)]
    assert [r["round_id"] for r in summarise_browser_rounds(points)] == [
        ROUND,
        ROUND + 1,
        ROUND + 2,
    ]


# ---------------------------------------------------------------------------
# print-recorded-results (the persisted Postgres view)
# ---------------------------------------------------------------------------


def _fixture_rows(name: str) -> list[dict]:
    path = Path(__file__).parent / "fixtures" / "browser-reads" / name
    return json.loads(path.read_text())


def test_the_recorded_section_renders_rounds_statuses_and_kinds(repo):
    deployment = _deployment(repo)
    for raw in _fixture_rows("spread-rows.json"):
        repo.upsert_browser_download_record(
            deployment_id=deployment.id,
            client_hostname=raw["client_hostname"],
            round_id=raw["round_id"],
            instance=raw["instance"],
            status=OpRecordStatus(raw["status"]),
            started_at=datetime.fromisoformat(raw["started_at"]),
            extension=raw["extension"],
            region=raw["region"],
            error_kind=raw["error_kind"],
            trace_s3_key=raw["trace_s3_key"],
        )
    console = Console(record=True, width=220)
    _print_browser_downloads_section(
        repo.list_browser_download_records_for_deployment(deployment.id),
        deployment.name,
        console,
    )
    text = console.export_text()
    assert "Browser downloads" in text
    # 4 rounds in the window plus the one before it = 5 distinct round ids.
    assert "rounds" in text and "5" in text
    assert "no_hot_files" in text
    assert "not_found" in text
    assert "browsers" in text


def test_the_recorded_section_prints_nothing_without_browser_rows():
    """Every run before V2-1617 has none, and their output must not change."""
    console = Console(record=True, width=120)
    _print_browser_downloads_section([], "DEV-01", console)
    assert console.export_text().strip() == ""
