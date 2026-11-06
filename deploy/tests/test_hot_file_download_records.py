"""Hot-file reads: Influx -> Postgres -> the views that read them (INFRA-531).

The `hot_file_readers` role writes one `hot_file_download` point per native
reader per round, carrying the twenty-five swarm counters `ant --json` prints.
Both halves are perishable -- InfluxDB retention is bounded and the VMs are
destroyed after the rounds -- so what these tests pin is that everything which
has to survive teardown does: the rows, the swarm figures, and the extension
that distinguishes the branch cohort from the released control on a deployment
running both.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models import SWARM_COUNTER_COLUMNS, OpRecordStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.print_recorded_results import _print_hot_file_reads_section
from saorsa_deploy.cmd.record_results import _record_hot_file_downloads

#: 3000000 x 600 = 2027-01-15T08:00:00Z, the boundary a 600 s round lands on.
ROUND = 3000000
PERIOD = 600
#: Reader 2 on the VM with `client_index` 1 and a 2 s stagger: boundary + 2 s.
START_NS = (ROUND * PERIOD + 2) * 1_000_000_000
T0 = datetime.fromtimestamp(START_NS / 1_000_000_000, tz=timezone.utc)

HOST = "DEV-03-branch-ant-client-hot-file-read-lon1-1"
CONTROL_HOST = "DEV-03-control-ant-client-hot-file-read-fra1-2"


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyDeploymentRepository(session)


#: Every swarm counter set to a distinct value, so a column wired to the wrong
#: field is a failed assertion rather than a coincidence.
SWARM_VALUES = {name: 100 + i for i, name in enumerate(SWARM_COUNTER_COLUMNS)}


def _point(**extra):
    """One parsed `hot_file_download` point, as `influx.py` hands it over."""
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
        "address": "a0" * 32,
        "file_size": 52428800,
        "total_ms": 12800,
        "client_elapsed_ms": 12500,
        "start_ns": START_NS,
        "started_late_ms": 42,
        "verify_status": 0,
        "swarm_present": 1,
        "swarm_rooms": [{"tag": "abc", "links": 6}],
        **SWARM_VALUES,
    }
    point.update(extra)
    return point


def _record(repo, deployment, points, tiers=None, console=None, hot_files=None):
    """Drive `_record_hot_file_downloads` over scripted points, offline.

    Three dependencies are patched, not one: besides the Influx query, the
    client-tier scan and the hot-file read both reach S3, so an unpatched test
    makes a live call and fails wherever there are no credentials, which is CI.
    """
    args = SimpleNamespace(name=deployment.name)
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_hot_file_download_points",
            return_value=points,
        ),
        patch(
            "saorsa_deploy.cmd.record_results._client_tiers",
            return_value=tiers if tiers is not None else [],
        ),
        patch("saorsa_deploy.hot_files.load_hot_files", return_value=hot_files),
        patch("saorsa_deploy.registry.get_repository", return_value=repo),
    ):
        _record_hot_file_downloads(
            repo,
            deployment,
            args,
            console or Console(),
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )


def _deployment(repo, name="DEV-03", **deploy_args):
    return repo.create(
        name=name,
        deploy_args={"kind": "testnet", "bootstrap_count": 1, **deploy_args},
    )


def _reader_client(region="lon1", index=1, parity="even"):
    return {
        "role": "hot-file-read",
        "region": region,
        "client_index": index,
        "instances": 4,
        "round_period_secs": PERIOD,
        "download_timeout_secs": 300,
        "round_parity": parity,
        "start_stagger_secs": 2,
    }


#: A hot-file selection as `clients select-hot-files` writes it, with the
#: `sha256` this issue added.
_HOT_FILES = {
    "deployment": "DEV-03",
    "selected_at": "2027-01-15T07:55:00Z",
    "size_bytes": 52428800,
    "files": [
        {
            "index": 0,
            "address": "a0" * 32,
            "bytes": 52428800,
            "source_key": "upload-lon1-1.txt",
            "line": 4,
            "sha256": "b1" * 32,
        }
    ],
}


# ---------------------------------------------------------------------------
# the row
# ---------------------------------------------------------------------------


def test_a_point_maps_onto_a_row(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.client_hostname == HOST
    assert row.round_id == ROUND
    assert row.instance == 2
    assert row.hot_file_index == 1
    assert row.status is OpRecordStatus.SUCCESS
    assert row.region == "lon1"
    assert row.address == "a0" * 32
    assert row.file_size_bytes == 52428800
    assert row.total_ms == 12800
    assert row.client_elapsed_ms == 12500
    assert row.started_late_ms == 42
    assert row.verify_status == 0
    assert row.swarm_present is True
    assert row.swarm_rooms == [{"tag": "abc", "links": 6}]
    # `started_at` is the reader's SCHEDULED start, which is what makes a window
    # selection over these rows select whole rounds. The tz is reattached
    # because the SQLite fixture drops it; Postgres' `timestamptz` does not.
    assert row.started_at.replace(tzinfo=timezone.utc) == T0


def test_every_swarm_counter_lands_in_its_own_column(repo):
    # Each counter carries a distinct value, so a column wired to the wrong
    # field fails here rather than reading plausibly.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    for name, value in SWARM_VALUES.items():
        assert getattr(row, name) == value, name


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
    rows = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert [r.status for r in rows] == [
        OpRecordStatus.SUCCESS,
        OpRecordStatus.FAILURE,
        OpRecordStatus.SKIPPED,
    ]


def test_an_unrecognised_status_is_unusable_not_coerced(repo):
    # A status this build does not understand must not be stored as `failure` in
    # a column a success rate reads.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(status="weird")])
    assert repo.list_hot_file_download_records_for_deployment(deployment.id) == []


def test_a_point_with_no_scheduled_start_is_unusable(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(start_ns=None)])
    assert repo.list_hot_file_download_records_for_deployment(deployment.id) == []


def test_a_released_cohort_s_rows_say_so(repo):
    # `swarm_present = False`, not NULL: the binary reported no swarm, which is
    # a different fact from "this column was never recorded".
    deployment = _deployment(repo)
    _record(
        repo,
        deployment,
        [_point(swarm_present=0, **{name: 0 for name in SWARM_COUNTER_COLUMNS})],
    )
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.swarm_present is False
    assert row.records_from_peers == 0


def test_an_unrecorded_swarm_present_is_null_not_false(repo):
    # A point written before the field existed must read as "not recorded".
    deployment = _deployment(repo)
    _record(repo, deployment, [_point(swarm_present=None)])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.swarm_present is None


# ---------------------------------------------------------------------------
# idempotence and the COALESCE
# ---------------------------------------------------------------------------


def test_a_second_pass_revises_rather_than_duplicates(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    _record(repo, deployment, [_point(total_ms=9999, records_from_peers=42)])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.total_ms == 9999
    assert row.records_from_peers == 42


def test_the_extension_is_resolved_from_the_live_tiers(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], tiers=[("branch", {"clients": [_reader_client()]})])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension == "branch"


def test_the_extension_is_resolved_from_the_recorded_config_after_teardown(repo):
    # `clients down` deletes the extension's S3 client state, and the second
    # `record-results` of a run's procedure happens after exactly that.
    deployment = _deployment(
        repo,
        hot_file_readers=[{"hostname": HOST, "extension": "branch", "region": "lon1"}],
    )
    _record(repo, deployment, [_point()], tiers=[])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension == "branch"


def test_a_re_run_that_cannot_resolve_the_extension_does_not_erase_it(repo):
    # The COALESCE: the Influx point does not carry the extension, so a pass
    # after teardown resolves None for every host -- and writing that would
    # relabel a whole cohort as the deployment's own tier, which on a run with a
    # branch cohort and a released control merges the two arms.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], tiers=[("branch", {"clients": [_reader_client()]})])
    _record(repo, deployment, [_point()], tiers=[])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension == "branch"


def test_a_row_recorded_without_an_extension_gains_one_later(repo):
    """The COALESCE refuses None, not a value.

    A first pass run before the extension's client state could be read (or
    without ``DATABASE_URL`` for the role config) leaves the column NULL; the
    next pass that can resolve it fills it in. The other direction -- a host
    whose extension CHANGES -- cannot happen: the extension name is part of the
    hostname, so a host determines its own extension and the two sources cannot
    disagree.
    """
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], tiers=[])
    assert repo.list_hot_file_download_records_for_deployment(deployment.id)[0].extension is None
    _record(repo, deployment, [_point()], tiers=[("branch", {"clients": [_reader_client()]})])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension == "branch"


def test_the_deployment_s_own_tier_carries_no_extension(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], tiers=[(None, {"clients": [_reader_client()]})])
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension is None


def test_two_cohorts_on_one_deployment_stay_apart(repo):
    deployment = _deployment(repo)
    _record(
        repo,
        deployment,
        [
            _point(host=HOST, instance=1),
            _point(host=CONTROL_HOST, instance=1, swarm_present=0),
        ],
        tiers=[
            ("branch", {"clients": [_reader_client()]}),
            ("control", {"clients": [_reader_client(region="fra1", index=2, parity="odd")]}),
        ],
    )
    rows = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert {r.client_hostname: r.extension for r in rows} == {
        HOST: "branch",
        CONTROL_HOST: "control",
    }


def test_a_different_round_or_reader_is_a_different_row(repo):
    deployment = _deployment(repo)
    _record(
        repo,
        deployment,
        [
            _point(round_id=ROUND, instance=1),
            _point(round_id=ROUND, instance=2),
            _point(round_id=ROUND + 2, instance=1),
        ],
    )
    assert len(repo.list_hot_file_download_records_for_deployment(deployment.id)) == 3


# ---------------------------------------------------------------------------
# the hot-file snapshot
# ---------------------------------------------------------------------------


def test_the_selection_is_snapshotted_into_deploy_args(repo):
    # It lives under `addresses/<deployment>/`, which the deployment's own
    # `clients destroy` deletes WHOLE.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    refreshed = repo.get_by_name(deployment.name)
    assert refreshed.deploy_args["hot_file_readers_hot_files"] == _HOT_FILES


def test_the_snapshot_carries_the_sha256(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    refreshed = repo.get_by_name(deployment.name)
    files = refreshed.deploy_args["hot_file_readers_hot_files"]["files"]
    assert files[0]["sha256"] == "b1" * 32


def test_a_replaced_selection_is_captured(repo):
    """`clients select-hot-files --replace` changes what every reader fetches.

    Returning early on "something is already recorded" meant the replacement was
    never captured, so rows describing the new address sat beside metadata
    describing the old one — and the new file's sha256 and source were lost at
    teardown.
    """
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    replacement = {
        "deployment": "DEV-03",
        "selected_at": "2027-01-15T11:00:00Z",
        "size_bytes": 52428800,
        "files": [
            {
                "index": 0,
                "address": "c2" * 32,
                "bytes": 52428800,
                "source_key": "upload-fra1-2.txt",
                "line": 9,
                "sha256": "d3" * 32,
            }
        ],
    }
    deployment = repo.get_by_name(deployment.name)
    _record(repo, deployment, [_point()], hot_files=replacement)

    recorded = repo.get_by_name(deployment.name).deploy_args["hot_file_readers_hot_files"]
    # The CURRENT selection, because rows written after the replacement record
    # that address.
    assert [f["address"] for f in recorded["files"]] == ["c2" * 32]
    assert recorded["files"][0]["sha256"] == "d3" * 32
    # And the one it replaced, because rows written BEFORE it record that
    # address and would otherwise have no bytes, sha256 or source at all.
    superseded = recorded["superseded_files"]
    assert [f["address"] for f in superseded] == ["a0" * 32]
    assert superseded[0]["sha256"] == "b1" * 32


def test_a_third_selection_carries_both_of_its_predecessors(repo):
    from saorsa_deploy.hot_file_records import merge_hot_file_selection

    def _doc(address, digest):
        return {
            "deployment": "DEV-03",
            "files": [{"index": 0, "address": address, "bytes": 1, "sha256": digest}],
        }

    first = merge_hot_file_selection(None, _doc("aa", "11"))
    second = merge_hot_file_selection(first, _doc("bb", "22"))
    third = merge_hot_file_selection(second, _doc("cc", "33"))
    assert [f["address"] for f in third["files"]] == ["cc"]
    assert sorted(f["address"] for f in third["superseded_files"]) == ["aa", "bb"]


def test_an_unchanged_selection_is_byte_identical_to_the_live_document(repo):
    # So the common case writes what it always did, with no `superseded_files`
    # key appearing on a run that never replaced anything.
    from saorsa_deploy.hot_file_records import merge_hot_file_selection

    assert merge_hot_file_selection(_HOT_FILES, _HOT_FILES) == _HOT_FILES
    assert "superseded_files" not in merge_hot_file_selection(None, _HOT_FILES)


def test_an_unchanged_selection_writes_nothing_on_a_later_pass(repo):
    # One S3 GET per pass is the cost of noticing a replacement; a registry
    # write per pass would not be.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    deployment = repo.get_by_name(deployment.name)
    with patch("saorsa_deploy.registry.record_deploy_args_update") as update:
        _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    assert update.call_count == 0


def test_a_replacement_updates_the_key_the_browser_role_used(repo):
    # A deployment running both cohorts has ONE selection, snapshotted by
    # whichever role's pass reached it first. The replacement must land on that
    # same key rather than opening a second copy that can drift.
    deployment = _deployment(repo, browser_hot_files=_HOT_FILES)
    replacement = {**_HOT_FILES, "files": [{"index": 0, "address": "c2" * 32, "bytes": 1}]}
    _record(repo, deployment, [_point()], hot_files=replacement)
    deploy_args = repo.get_by_name(deployment.name).deploy_args
    assert "hot_file_readers_hot_files" not in deploy_args
    assert [f["address"] for f in deploy_args["browser_hot_files"]["files"]] == ["c2" * 32]


def test_a_selection_already_gone_leaves_an_earlier_snapshot_alone(repo):
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    _record(repo, deployment, [_point()], hot_files=None)
    refreshed = repo.get_by_name(deployment.name)
    assert refreshed.deploy_args["hot_file_readers_hot_files"] == _HOT_FILES


def test_the_browser_role_s_snapshot_is_not_duplicated(repo):
    # Both cohorts read the ONE selection object, so two copies of it in
    # `deploy_args` could only ever drift.
    deployment = _deployment(repo, browser_hot_files=_HOT_FILES)
    _record(repo, deployment, [_point()], hot_files=_HOT_FILES)
    refreshed = repo.get_by_name(deployment.name)
    assert "hot_file_readers_hot_files" not in refreshed.deploy_args
    assert refreshed.deploy_args["browser_hot_files"] == _HOT_FILES


def test_either_key_is_read_back(repo):
    from saorsa_deploy.hot_file_records import recorded_hot_files

    assert recorded_hot_files({"browser_hot_files": _HOT_FILES}) == _HOT_FILES
    assert recorded_hot_files({"hot_file_readers_hot_files": _HOT_FILES}) == _HOT_FILES
    assert recorded_hot_files({}) is None
    # A half-written snapshot reads as nothing recorded.
    assert recorded_hot_files({"hot_file_readers_hot_files": {"files": "x"}}) is None


# ---------------------------------------------------------------------------
# a failed query / an empty answer
# ---------------------------------------------------------------------------


def test_a_failed_query_warns_and_writes_nothing(repo):
    deployment = _deployment(repo)
    args = SimpleNamespace(name=deployment.name)
    console = Console(record=True, width=200)
    with patch(
        "saorsa_deploy.cmd.record_results.fetch_hot_file_download_points",
        side_effect=RuntimeError("influx is down"),
    ):
        _record_hot_file_downloads(
            repo, deployment, args, console, "http://influx", "t", "ant", "testnet-metrics"
        )
    assert "hot_file_download query failed" in console.export_text()
    assert repo.list_hot_file_download_records_for_deployment(deployment.id) == []


def test_no_points_says_nothing_and_scans_nothing(repo):
    # A deployment with no reader tier must not pay for a client-state scan or
    # an S3 GET on every `record-results` pass.
    deployment = _deployment(repo)
    args = SimpleNamespace(name=deployment.name)
    console = Console(record=True, width=200)
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_hot_file_download_points",
            return_value=[],
        ),
        patch(
            "saorsa_deploy.cmd.record_results._client_tiers",
            side_effect=AssertionError("scanned"),
        ),
    ):
        _record_hot_file_downloads(
            repo, deployment, args, console, "http://influx", "t", "ant", "testnet-metrics"
        )
    assert console.export_text().strip() == ""


# ---------------------------------------------------------------------------
# the repository's single-row helper and the model's round trip
# ---------------------------------------------------------------------------


def test_the_single_row_helper_round_trips(repo):
    deployment = _deployment(repo)
    row = repo.upsert_hot_file_download_record(
        deployment_id=deployment.id,
        client_hostname=HOST,
        round_id=ROUND,
        instance=1,
        status=OpRecordStatus.SUCCESS,
        started_at=T0,
        extension="branch",
        region="lon1",
        swarm_present=True,
        records_from_peers=7,
        swarm_rooms=[{"tag": "abc"}],
    )
    assert row.extension == "branch"
    assert row.records_from_peers == 7
    assert row.swarm_rooms == [{"tag": "abc"}]
    as_dict = row.to_dict()
    assert as_dict["status"] == "success"
    assert as_dict["swarm_present"] is True
    for name in SWARM_COUNTER_COLUMNS:
        assert name in as_dict, name


def test_an_empty_batch_writes_nothing(repo):
    assert repo.upsert_hot_file_download_records([]) == 0


def test_the_rows_go_with_the_deployment(repo):
    # The FK cascades, as every other per-op table's does.
    deployment = _deployment(repo)
    _record(repo, deployment, [_point()])
    repo.session.delete(repo.session.get(type(deployment), deployment.id))
    repo.session.commit()
    assert repo.list_hot_file_download_records_for_deployment(deployment.id) == []


# ---------------------------------------------------------------------------
# the persisted view
# ---------------------------------------------------------------------------


def test_the_recorded_section_splits_by_extension(repo):
    deployment = _deployment(repo)
    _record(
        repo,
        deployment,
        [
            _point(host=HOST, instance=1, records_from_peers=7),
            _point(host=CONTROL_HOST, instance=1, swarm_present=0, records_from_peers=0),
        ],
        tiers=[
            ("branch", {"clients": [_reader_client()]}),
            ("control", {"clients": [_reader_client(region="fra1", index=2, parity="odd")]}),
        ],
    )
    console = Console(record=True, width=240)
    _print_hot_file_reads_section(
        repo.list_hot_file_download_records_for_deployment(deployment.id),
        deployment.name,
        console,
    )
    text = console.export_text()
    assert "Hot-file reads" in text
    assert "branch" in text and "control" in text
    # One of the two cohorts reported a swarm, the other did not.
    assert "reads reporting a swarm" in text
    assert "1/2" in text


def test_the_recorded_section_prints_nothing_without_rows():
    console = Console(record=True, width=120)
    _print_hot_file_reads_section([], "DEV-03", console)
    assert console.export_text().strip() == ""


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def _migration():
    """The revision that creates the table, imported by path."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "c3f8d41b6e92_add_deployment_hot_file_download_records.py"
    )
    spec = importlib.util.spec_from_file_location("hot_file_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_creates_and_drops_the_table_on_a_fresh_sqlite():
    """Executed, not just read.

    The whole chain cannot be run here -- earlier revisions are Postgres-only
    (`ALTER TYPE`) -- so this applies the one revision against a SQLite engine
    carrying only `deployments`. That is enough to catch what a structural read
    cannot: a column type with no SQLite rendering, which is what a bare
    `JSONB` is and what made `CREATE TABLE` fail to compile before the
    `with_variant` was added.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from saorsa_db.models import Deployment, DeploymentHotFileDownloadRecord
    from sqlalchemy import inspect

    module = _migration()
    engine = create_engine("sqlite://")
    Deployment.__table__.create(engine)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()

    columns = {c["name"] for c in inspect(engine).get_columns(module._TABLE)}
    # Set equality with the model: a column in one and not the other is a row
    # the upsert cannot write or a column that reads NULL forever.
    assert columns == set(DeploymentHotFileDownloadRecord.__table__.columns.keys())
    indexes = {i["name"] for i in inspect(engine).get_indexes(module._TABLE)}
    assert "ix_deployment_hot_file_download_records_deployment_id" in indexes

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
    assert module._TABLE not in inspect(engine).get_table_names()


def test_the_migration_revises_the_previous_head_and_reuses_the_status_enum():
    module = _migration()
    assert module.revision == "c3f8d41b6e92"
    assert module.down_revision == "a2e71c4b9f36"
    # `oprecordstatus` belongs to `72f6ffb82c1a`; redeclaring it fails with
    # "type already exists" against every real database, and dropping it on the
    # downgrade would take the two op-record tables with it.
    body = module.__file__ and open(module.__file__).read()
    assert "create_type=False" in body
    assert "DROP TYPE" not in body.upper()


def test_the_migration_lists_the_same_counters_as_the_model():
    assert set(_migration()._SWARM_COUNTERS) == set(SWARM_COUNTER_COLUMNS)


# ---------------------------------------------------------------------------
# the recorded role config survives a later provision
# ---------------------------------------------------------------------------


def _record_config(repo, deployment, tiers, console=None):
    """Drive `record_hot_file_config` over a scripted client-tier scan."""
    from saorsa_deploy.hot_file_records import record_hot_file_config

    with (
        patch(
            "saorsa_deploy.hot_file_records.client_tiers_with_extensions",
            return_value=tiers,
        ),
        patch("saorsa_deploy.registry.get_repository", return_value=repo),
    ):
        return record_hot_file_config(deployment.name, console)


def _recorded(repo, deployment):
    from saorsa_deploy.hot_file_records import HOT_FILE_CONFIG_KEY

    return repo.get_by_name(deployment.name).deploy_args.get(HOT_FILE_CONFIG_KEY) or []


def test_a_later_provision_does_not_erase_an_earlier_cohort(repo):
    """The live scan sees only the tiers standing NOW.

    Provision a `swarm` extension, tear it down, then provision `control`:
    replacing the recorded list outright took the swarm cohort's hostnames,
    extension and schedule with it — which is the one thing this document exists
    to survive. A `record-results` pass that then first reached the swarm
    cohort's still-retained Influx points could resolve no extension for them,
    and the upsert's COALESCE does not help: it protects a row that already HAS
    one, not the first write of a row that never did.
    """
    deployment = _deployment(repo)
    _record_config(repo, deployment, [("swarm", {"clients": [_reader_client()]})])
    assert [e["extension"] for e in _recorded(repo, deployment)] == ["swarm"]

    _record_config(
        repo,
        deployment,
        [("control", {"clients": [_reader_client(region="fra1", index=2, parity="odd")]})],
    )
    kept = _recorded(repo, deployment)
    assert sorted(e["extension"] for e in kept) == ["control", "swarm"]
    # The schedule, not merely the name: it is what says when each reader was due.
    swarm = next(e for e in kept if e["extension"] == "swarm")
    assert swarm["round_parity"] == "even"
    assert swarm["start_stagger_secs"] == 2
    assert swarm["client_index"] == 1


def test_a_torn_down_cohort_s_rows_still_resolve_their_extension(repo):
    # The consequence the merge exists for: `record-results` reaching the swarm
    # cohort's retained points after it was torn down and `control` provisioned.
    deployment = _deployment(repo)
    _record_config(repo, deployment, [("swarm", {"clients": [_reader_client()]})])
    _record_config(
        repo,
        deployment,
        [("control", {"clients": [_reader_client(region="fra1", index=2, parity="odd")]})],
    )
    deployment = repo.get_by_name(deployment.name)
    _record(
        repo, deployment, [_point(host="DEV-03-swarm-ant-client-hot-file-read-lon1-1")], tiers=[]
    )
    (row,) = repo.list_hot_file_download_records_for_deployment(deployment.id)
    assert row.extension == "swarm"


def test_a_re_provision_of_the_same_vm_takes_the_new_schedule(repo):
    # Live wins on a hostname present in both: a re-provision is how a VM's own
    # schedule changes.
    deployment = _deployment(repo)
    _record_config(repo, deployment, [("swarm", {"clients": [_reader_client()]})])
    changed = {**_reader_client(), "round_period_secs": 900, "start_stagger_secs": 5}
    _record_config(repo, deployment, [("swarm", {"clients": [changed]})])
    (entry,) = _recorded(repo, deployment)
    assert entry["round_period_secs"] == 900
    assert entry["start_stagger_secs"] == 5


def test_an_unreadable_registry_falls_back_to_the_live_scan(repo):
    # Which is what the behaviour was before it merged at all, so a miss costs
    # no more than it did.
    deployment = _deployment(repo)
    with patch("saorsa_deploy.registry.read_deploy_args", return_value=None):
        _record_config(repo, deployment, [("swarm", {"clients": [_reader_client()]})])
    assert [e["extension"] for e in _recorded(repo, deployment)] == ["swarm"]


def test_a_deployment_with_no_readers_records_nothing(repo):
    deployment = _deployment(repo)
    assert _record_config(repo, deployment, []) == []
    assert _recorded(repo, deployment) == []


def test_the_merge_is_sorted_so_repeated_provisions_write_identical_bytes():
    from saorsa_deploy.hot_file_records import merge_hot_file_config

    recorded = [{"hostname": "b", "extension": "z"}, {"hostname": "a", "extension": "a"}]
    live = [{"hostname": "c", "extension": "m"}]
    once = merge_hot_file_config(recorded, live)
    assert once == merge_hot_file_config(once, live)
    assert [e["extension"] for e in once] == ["a", "m", "z"]
