"""Threshold config validation and verdict persistence (V2-721).

Thresholds live in versioned config rather than code so the quarterly review can
recalibrate them without a release. That only works if a malformed config fails
loudly: a threshold silently defaulting to zero because of a typo would turn a
gate into a rubber stamp while still printing PASS.
"""
import copy

import pytest
import yaml
from saorsa_db.db import Base
from saorsa_db.models.gate_verdict import DeploymentGateVerdict
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.gate_config import (
    GateConfigError,
    get_default_thresholds_path,
    load_gate_config,
    parse_gate_config,
)


@pytest.fixture()
def raw():
    return yaml.safe_load(get_default_thresholds_path().read_text())


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session), session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# The shipped config
# ---------------------------------------------------------------------------


def test_shipped_config_loads_and_matches_the_process_spec():
    config = load_gate_config()
    assert config.version >= 1
    # The defaults written into the process spec's gate tables.
    assert config.upload_success_floor_percent == 99.0
    assert config.download_success_floor_percent == 99.5
    assert config.success_rate_drop_pp == 1.0
    assert config.transfer_duration_p50_increase_percent == 10.0
    assert config.transfer_duration_p95_increase_percent == 20.0
    assert config.node_cpu_increase_percent == 15.0
    assert config.node_rss_increase_percent == 15.0
    assert config.outbound_bytes_per_gb_increase_percent == 10.0
    assert config.disk_bytes_per_gb_increase_percent == 10.0
    # Zero, and a COUNT rather than a percentage: there is no fraction of
    # corrupt content a release may ship.
    assert config.download_verify_mismatch_max == 0
    assert config.noise_rule.min_history_runs == 5
    assert config.noise_rule.sigma_multiplier == 2.0
    assert config.memory_leak.window_hours == 2.0


def test_default_outbound_ratio_is_user_traffic():
    # The spec's wording is "outbound bytes per GB of user traffic". V2-758
    # stored both ratios precisely so this command could choose between them.
    assert load_gate_config().outbound_ratio_metric == "outbound_bytes_per_gb_traffic"


def test_config_is_frozen():
    config = load_gate_config()
    with pytest.raises(Exception):
        config.node_cpu_increase_percent = 99.0


# ---------------------------------------------------------------------------
# Validation refuses rather than defaults
# ---------------------------------------------------------------------------


def test_missing_version_is_rejected(raw):
    data = copy.deepcopy(raw)
    del data["version"]
    with pytest.raises(GateConfigError, match="version"):
        parse_gate_config(data, "test.yaml")


def test_missing_threshold_is_rejected_not_defaulted(raw):
    data = copy.deepcopy(raw)
    del data["soft_gates"]["node_cpu_increase_percent"]
    with pytest.raises(GateConfigError, match="node_cpu_increase_percent"):
        parse_gate_config(data, "test.yaml")


def test_zero_threshold_is_rejected(raw):
    # A zero threshold makes the gate fire on any change at all; far more
    # likely a typo than an intention.
    data = copy.deepcopy(raw)
    data["soft_gates"]["node_cpu_increase_percent"] = 0
    with pytest.raises(GateConfigError, match="greater than zero"):
        parse_gate_config(data, "test.yaml")


def test_missing_verify_mismatch_max_is_rejected_not_defaulted(raw):
    # The one gate in this file whose intended value IS zero. A key that
    # quietly defaulted would look identical to the configured threshold while
    # meaning "nobody set this", which is the whole reason validation is loud.
    data = copy.deepcopy(raw)
    del data["hard_gates"]["download_verify_mismatch_max"]
    with pytest.raises(GateConfigError, match="download_verify_mismatch_max"):
        parse_gate_config(data, "test.yaml")


def test_zero_verify_mismatch_max_is_accepted(raw):
    # Unlike every percentage threshold here, zero is the correct value: the
    # gate is a count of mismatched downloads, and one is too many.
    data = copy.deepcopy(raw)
    data["hard_gates"]["download_verify_mismatch_max"] = 0
    assert parse_gate_config(data, "test.yaml").download_verify_mismatch_max == 0


def test_negative_verify_mismatch_max_is_rejected(raw):
    data = copy.deepcopy(raw)
    data["hard_gates"]["download_verify_mismatch_max"] = -1
    with pytest.raises(GateConfigError, match="cannot be negative"):
        parse_gate_config(data, "test.yaml")


def test_success_floor_above_100_is_rejected(raw):
    data = copy.deepcopy(raw)
    data["hard_gates"]["upload_success_floor_percent"] = 101
    with pytest.raises(GateConfigError, match="cannot exceed 100"):
        parse_gate_config(data, "test.yaml")


def test_unsorted_bucket_edges_are_rejected(raw):
    # Out-of-order edges would assign a file to more than one bucket, or none.
    data = copy.deepcopy(raw)
    data["file_size_buckets_mb"] = [0, 64, 32, 128]
    with pytest.raises(GateConfigError, match="strictly ascending"):
        parse_gate_config(data, "test.yaml")


def test_duplicate_bucket_edges_are_rejected(raw):
    data = copy.deepcopy(raw)
    data["file_size_buckets_mb"] = [0, 32, 32, 64]
    with pytest.raises(GateConfigError, match="strictly ascending"):
        parse_gate_config(data, "test.yaml")


def test_disk_ratio_is_not_a_legal_outbound_metric(raw):
    # Naming it here would silently evaluate the disk gate twice.
    data = copy.deepcopy(raw)
    data["outbound_ratio_metric"] = "disk_bytes_per_gb_stored"
    with pytest.raises(GateConfigError, match="outbound_ratio_metric"):
        parse_gate_config(data, "test.yaml")


def test_window_ratio_below_one_is_rejected(raw):
    # Below 1 it would reject every comparison, including a run against itself.
    data = copy.deepcopy(raw)
    data["max_window_length_ratio"] = 0.5
    with pytest.raises(GateConfigError, match="max_window_length_ratio"):
        parse_gate_config(data, "test.yaml")


def test_min_history_below_two_is_rejected(raw):
    # A standard deviation over fewer than two runs is undefined.
    data = copy.deepcopy(raw)
    data["noise_rule"]["min_history_runs"] = 1
    with pytest.raises(GateConfigError, match="min_history_runs"):
        parse_gate_config(data, "test.yaml")


def test_missing_file_is_reported_clearly():
    with pytest.raises(GateConfigError, match="not found"):
        load_gate_config("/nonexistent/thresholds.yaml")


# ---------------------------------------------------------------------------
# The two WASM advisory settings (V2-1286)
# ---------------------------------------------------------------------------


def test_shipped_config_makes_wasm_floors_advisory_and_relatives_count():
    config = load_gate_config()
    assert config.wasm_floor_gates_advisory is True
    assert config.wasm_relative_gates_advisory is False


@pytest.mark.parametrize(
    "key", ["wasm_floor_gates_advisory", "wasm_relative_gates_advisory"]
)
def test_missing_wasm_advisory_key_is_rejected(raw, key):
    """Absent is an error, not a default.

    Whether a failing gate counts towards the verdict must be stated. A key
    that quietly defaulted would decide, invisibly, whether a FAIL blocks the
    ship -- the same rule every threshold in this file follows.
    """
    data = copy.deepcopy(raw)
    del data[key]
    with pytest.raises(GateConfigError, match=key):
        parse_gate_config(data, "test.yaml")


@pytest.mark.parametrize(
    "key", ["wasm_floor_gates_advisory", "wasm_relative_gates_advisory"]
)
def test_non_boolean_wasm_advisory_is_rejected(raw, key):
    data = copy.deepcopy(raw)
    data[key] = "yes"
    with pytest.raises(GateConfigError, match=key):
        parse_gate_config(data, "test.yaml")


def test_the_retired_single_key_fails_loudly_naming_both_replacements():
    """A config still carrying `wasm_gates_advisory` must not load.

    Silently ignoring it would leave the operator believing they had switched
    WASM gating off (or on) while the shipped defaults quietly applied instead.
    """
    data = yaml.safe_load(get_default_thresholds_path().read_text())
    data["wasm_gates_advisory"] = True
    with pytest.raises(GateConfigError) as exc:
        parse_gate_config(data, "test.yaml")
    message = str(exc.value)
    assert "wasm_floor_gates_advisory" in message
    assert "wasm_relative_gates_advisory" in message


# ---------------------------------------------------------------------------
# Verdict persistence
# ---------------------------------------------------------------------------


def test_upsert_gate_verdict_replaces_in_place(repo):
    # Re-running a comparison must replace its previous answer. Two rows for
    # the same key would make "the verdict for this run" ambiguous at exactly
    # the moment it matters.
    r, session = repo
    candidate = r.create(name="STG-01", deploy_args={})
    baseline = r.create(name="STG-00", deploy_args={})

    r.upsert_gate_verdict(
        deployment_id=candidate.id,
        baseline_deployment_id=baseline.id,
        window_label="full-run",
        overall="SOFT FAIL",
        threshold_config_version=1,
        verdict={"overall": "SOFT FAIL"},
        report_markdown="# first",
    )
    r.upsert_gate_verdict(
        deployment_id=candidate.id,
        baseline_deployment_id=baseline.id,
        window_label="full-run",
        overall="PASS",
        threshold_config_version=2,
        verdict={"overall": "PASS"},
        report_markdown="# second",
    )

    rows = session.query(DeploymentGateVerdict).all()
    assert len(rows) == 1
    assert rows[0].overall == "PASS"
    assert rows[0].threshold_config_version == 2
    assert rows[0].report_markdown == "# second"


def test_different_windows_are_separate_verdicts(repo):
    r, session = repo
    candidate = r.create(name="STG-01", deploy_args={})
    baseline = r.create(name="STG-00", deploy_args={})
    for label in ("full-run", "steady-state"):
        r.upsert_gate_verdict(
            deployment_id=candidate.id,
            baseline_deployment_id=baseline.id,
            window_label=label,
            overall="PASS",
            threshold_config_version=1,
            verdict={},
        )
    assert len(r.list_gate_verdicts_for_deployment(candidate.id)) == 2


def test_different_baselines_are_separate_verdicts(repo):
    r, _ = repo
    candidate = r.create(name="STG-01", deploy_args={})
    for name in ("STG-00", "STG-99"):
        baseline = r.create(name=name, deploy_args={})
        r.upsert_gate_verdict(
            deployment_id=candidate.id,
            baseline_deployment_id=baseline.id,
            window_label="full-run",
            overall="PASS",
            threshold_config_version=1,
            verdict={},
        )
    assert len(r.list_gate_verdicts_for_deployment(candidate.id)) == 2


def test_get_gate_verdict_round_trips_the_payload(repo):
    r, _ = repo
    candidate = r.create(name="STG-01", deploy_args={})
    baseline = r.create(name="STG-00", deploy_args={})
    payload = {"overall": "GRAY", "gates": [{"gate": "Node CPU", "verdict": "GRAY"}]}
    r.upsert_gate_verdict(
        deployment_id=candidate.id,
        baseline_deployment_id=baseline.id,
        window_label="steady-state",
        overall="GRAY",
        threshold_config_version=1,
        verdict=payload,
        report_markdown="# report",
    )
    found = r.get_gate_verdict(candidate.id, baseline.id, "steady-state")
    assert found.verdict == payload
    assert found.to_dict()["report_markdown"] == "# report"


def test_get_gate_verdict_missing_is_none(repo):
    r, _ = repo
    candidate = r.create(name="STG-01", deploy_args={})
    baseline = r.create(name="STG-00", deploy_args={})
    assert r.get_gate_verdict(candidate.id, baseline.id, "full-run") is None
