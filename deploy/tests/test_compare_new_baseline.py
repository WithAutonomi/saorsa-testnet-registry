"""`deployments compare --new-baseline` (V2-1286).

The staging profile changed shape — a WASM client tier was added on top of the
native one and the NAT fraction dropped from 30% to 10% — so nothing recorded
before that change is a valid baseline for anything recorded after it. The
first run on the new profile has nothing to be compared against, but it still
has to produce a verdict and a report: the absolute success floors are
single-run gates and stand on their own.

Every relative gate is EMITTED as NOT ASSESSED rather than omitted. A gate
missing from the report reads as one nobody thought about; the whole point of
this report is that an unmeasured gate can never be mistaken for a passing one.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.gate_comparison import (
    FAIL,
    NEW_BASELINE_REASON,
    NOT_ASSESSED,
    PASS,
    ComparisonError,
    RunMetrics,
    compare_runs,
)
from saorsa_deploy.gate_config import load_gate_config
from saorsa_deploy.gate_report import render_report

MB = 1024 * 1024
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeOp:
    status: str
    file_size_bytes: int
    duration_ms: int | None
    finished_at: datetime | None
    client_kind: str | None


def ops(n, *, failures=0, kind=None, duration_ms=15000):
    return [
        FakeOp(
            status="success" if i < n - failures else "failure",
            file_size_bytes=10 * MB,
            duration_ms=duration_ms,
            finished_at=T0 + timedelta(minutes=i),
            client_kind=kind,
        )
        for i in range(n)
    ]


HOST_METRICS = [
    {
        "host": "STG-01-ant-node-1",
        "host_role": "WAN_NODE",
        "cpu": {"usage_percent": {"avg": 20.0, "p50": 19.0, "samples": 100}},
        "rss_by_service": {"ant-node-1": {"avg": 2.0e8, "p50": 2.0e8, "max": 2.1e8, "samples": 50}},
    }
]

DERIVED = {
    "outbound_bytes_per_gb_traffic": {"value": 8.0e9, "source": "capture"},
    "disk_bytes_per_gb_stored": {"value": 8.7e9, "source": "capture"},
}

LOG_COUNTS = [
    {
        "event_key": "connection_attempt_failed",
        "event_group": "connection_failures",
        "count": 1000,
        "hosts": 10,
        "total_documents": 500000,
        "observed_start": T0,
        "observed_end": T0 + timedelta(hours=3),
        "pattern_version": 2,
    }
]


def _candidate(**over):
    base = dict(
        deployment_id=595,
        name="STG-01",
        window_label="steady-state",
        window_start=T0,
        window_end=T0 + timedelta(hours=3),
        upload_records=ops(100) + ops(40, failures=4, kind="wasm"),
        download_records=ops(100) + ops(40, failures=2, kind="wasm"),
        host_metrics=HOST_METRICS,
        derived=DERIVED,
        log_event_counts=LOG_COUNTS,
    )
    base.update(over)
    return RunMetrics(**base)


@pytest.fixture
def config():
    return load_gate_config()


class TestOnlySingleRunGatesAreEvaluated:
    def test_the_native_floors_are_evaluated(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        gate = next(g for g in payload["gates"] if g["gate"] == "Upload success floor [native]")
        assert gate["verdict"] == PASS
        assert gate["candidate"] == 100.0

    def test_the_wasm_floors_are_evaluated_and_stay_advisory(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        gate = next(g for g in payload["gates"] if g["gate"] == "Upload success floor [wasm]")
        assert gate["verdict"] == FAIL  # 90% against a 99% floor
        assert gate["advisory"] is True
        assert payload["overall"] != "HARD FAIL"

    def test_a_failing_native_floor_still_hard_fails(self, config):
        candidate = _candidate(upload_records=ops(100, failures=20))
        payload = compare_runs(candidate, None, config, new_baseline=True)
        assert payload["overall"] == "HARD FAIL"

    def test_every_relative_gate_is_not_assessed_with_the_fixed_reason(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        soft = [g for g in payload["gates"] if g["kind"] == "soft"]
        assert soft, "the relative gates must still be listed"
        for gate in soft:
            assert gate["verdict"] == NOT_ASSESSED, gate["gate"]
            assert gate["detail"] == NEW_BASELINE_REASON, gate["gate"]
            assert gate["baseline"] is None
            assert gate["delta_percent"] is None

    @pytest.mark.parametrize(
        "gate",
        [
            "Upload success rate [native]",
            "Upload success rate [wasm]",
            "Node CPU",
            "Memory leak (RSS slope)",
            "Outbound bytes per GB",
            "Disk bytes per GB stored",
        ],
    )
    def test_the_named_gates_are_all_present(self, config, gate):
        """The issue names these explicitly; none may quietly disappear."""
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        assert any(g["gate"] == gate for g in payload["gates"]), gate

    def test_the_connection_error_groups_are_covered_too(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        groups = [g for g in payload["gates"] if "connection" in g["gate"].lower()]
        assert groups
        assert all(g["verdict"] == NOT_ASSESSED for g in groups)


class TestComparabilityIsSkipped:
    def test_a_mismatched_window_label_is_irrelevant(self, config):
        # Nothing to be comparable TO. Normally this raises.
        compare_runs(_candidate(window_label="full-run"), None, config, new_baseline=True)

    def test_a_baseline_is_still_required_without_the_flag(self, config):
        with pytest.raises(ComparisonError, match="baseline run is required"):
            compare_runs(_candidate(), None, config)

    def test_the_full_run_window_warning_is_not_emitted(self, config):
        # That warning is about two windows describing different phases.
        payload = compare_runs(_candidate(window_label="full-run"), None, config, new_baseline=True)
        assert not any("like-for-like" in w for w in payload["warnings"])

    def test_it_says_so_in_the_warnings(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        assert any("NEW BASELINE" in w for w in payload["warnings"])


class TestReport:
    def test_the_header_names_no_baseline(self, config):
        payload = compare_runs(_candidate(), None, config, new_baseline=True)
        report = render_report(payload)
        assert "- Baseline: none (new baseline)" in report
        assert "- Baseline window:" not in report

    def test_the_report_is_byte_identical_for_identical_inputs(self, config):
        a = render_report(compare_runs(_candidate(), None, config, new_baseline=True))
        b = render_report(compare_runs(_candidate(), None, config, new_baseline=True))
        assert a == b


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


class TestStorage:
    def _write(self, repo, deployment_id, overall="PASS"):
        return repo.upsert_gate_verdict(
            deployment_id=deployment_id,
            baseline_deployment_id=None,
            window_label="steady-state",
            overall=overall,
            threshold_config_version=3,
            verdict={"overall": overall},
            report_markdown="# report",
        )

    def test_a_new_baseline_verdict_persists_with_a_null_baseline(self, repo):
        d = repo.create(name="STG-01", deploy_args={})
        row = self._write(repo, d.id)
        assert row.baseline_deployment_id is None

    def test_a_re_run_replaces_rather_than_accumulates(self, repo):
        """SQL equality against NULL is never true.

        A lookup written as `== None` would find nothing and insert a second
        row every time, leaving "the verdict for this run" ambiguous at exactly
        the moment it matters.
        """
        d = repo.create(name="STG-01", deploy_args={})
        self._write(repo, d.id, overall="PASS")
        self._write(repo, d.id, overall="SOFT FAIL")

        rows = repo.list_gate_verdicts_for_deployment(d.id)
        assert len(rows) == 1
        assert rows[0].overall == "SOFT FAIL"

    def test_it_is_retrievable_by_a_null_baseline(self, repo):
        d = repo.create(name="STG-01", deploy_args={})
        self._write(repo, d.id)
        assert repo.get_gate_verdict(d.id, None, "steady-state") is not None

    def test_a_baselined_verdict_for_the_same_run_is_a_separate_row(self, repo):
        d = repo.create(name="STG-01", deploy_args={})
        other = repo.create(name="STG-01-prev", deploy_args={})
        self._write(repo, d.id)
        repo.upsert_gate_verdict(
            deployment_id=d.id,
            baseline_deployment_id=other.id,
            window_label="steady-state",
            overall="PASS",
            threshold_config_version=3,
            verdict={},
        )
        assert len(repo.list_gate_verdicts_for_deployment(d.id)) == 2
