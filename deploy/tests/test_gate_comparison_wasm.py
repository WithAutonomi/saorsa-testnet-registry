"""Per-transport gates in `deployments compare` (V2-1169).

`compare_runs` loaded every op record for a deployment and handed the lot to the
gates, so a run with both client tiers blended them into the upload/download
success floors (both HARD gates) and the transfer percentiles.

A native and a WASM client are not the same instrument — measured on
DEV-01-wasm the WASM tier ran ~2.2x slower on upload and ~4.2x on download — so
the merged figure describes neither. Worse, it moves with however many VMs of
each a run happened to use, which makes two runs incomparable to each other as
well.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from saorsa_deploy.gate_comparison import FAIL, PASS, RunMetrics, compare_runs
from saorsa_deploy.gate_config import load_gate_config

MB = 1024 * 1024
T0 = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeOp:
    status: str
    file_size_bytes: int
    duration_ms: int | None
    finished_at: datetime | None
    client_kind: str | None


def op(status="success", size_mb=10, duration_ms=15000, minutes=0, kind=None):
    return FakeOp(
        status=status,
        file_size_bytes=size_mb * MB,
        duration_ms=duration_ms,
        finished_at=T0 + timedelta(minutes=minutes),
        client_kind=kind,
    )


def ops(n, *, failures=0, kind=None, duration_ms=15000, start=0):
    out = [
        op(duration_ms=duration_ms, minutes=start + i, kind=kind) for i in range(n - failures)
    ]
    out += [
        op(status="failure", duration_ms=duration_ms, minutes=start + n - failures + i, kind=kind)
        for i in range(failures)
    ]
    return out


def _run(deployment_id=1, name="STG-01", label="full-run", **kwargs):
    return RunMetrics(
        deployment_id=deployment_id, name=name, window_label=label, **kwargs
    )


@pytest.fixture
def config():
    return load_gate_config()


def _gates(payload, prefix):
    return [g for g in payload["gates"] if g["gate"].startswith(prefix)]


def _gate(payload, name):
    return next(g for g in payload["gates"] if g["gate"] == name)


# --------------------------------------------------------------------------- #
# native-only runs are untouched
# --------------------------------------------------------------------------- #
class TestNativeOnlyIsUnchanged:
    def test_no_transport_suffix(self, config):
        run = _run(upload_records=ops(10), download_records=ops(10))
        base = _run(deployment_id=2, upload_records=ops(10), download_records=ops(10))
        payload = compare_runs(run, base, config)
        names = [g["gate"] for g in payload["gates"]]
        assert "Upload success floor" in names
        assert not any("[native]" in n or "[wasm]" in n for n in names)

    def test_null_client_kind_counts_as_native(self, config):
        # Every op recorded before the WASM client exists has client_kind NULL
        # and was necessarily native. Treating NULL as "unknown" would drop ~170k
        # historical rows out of every gate.
        run = _run(upload_records=ops(10, kind=None), download_records=ops(10, kind=None))
        base = _run(deployment_id=2, upload_records=ops(10), download_records=ops(10))
        payload = compare_runs(run, base, config)
        assert _gate(payload, "Upload success floor")["verdict"] == PASS

    def test_nothing_is_advisory(self, config):
        run = _run(upload_records=ops(10), download_records=ops(10))
        base = _run(deployment_id=2, upload_records=ops(10), download_records=ops(10))
        payload = compare_runs(run, base, config)
        assert not any(g["advisory"] for g in payload["gates"])


# --------------------------------------------------------------------------- #
# mixed runs
# --------------------------------------------------------------------------- #
def _mixed(deployment_id=1):
    """A run shaped like DEV-01-wasm: native clean, WASM below the floors."""
    return _run(
        deployment_id=deployment_id,
        upload_records=ops(176, kind="native")
        + ops(38, failures=3, kind="wasm", duration_ms=33000),
        download_records=ops(196, kind="native", duration_ms=4600)
        + ops(118, failures=6, kind="wasm", duration_ms=19400),
    )


class TestMixedRunsAreSplit:
    def test_both_transports_get_their_own_gates(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        names = [g["gate"] for g in payload["gates"]]
        assert "Upload success floor [native]" in names
        assert "Upload success floor [wasm]" in names
        assert "Download success floor [native]" in names
        assert "Download success floor [wasm]" in names

    def test_the_merged_gate_is_gone(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        assert "Upload success floor" not in [g["gate"] for g in payload["gates"]]

    def test_native_is_judged_on_native_records_alone(self, config):
        # Blended, 211/214 = 98.6% would fail the 99% floor even though the
        # native tier was perfect.
        payload = compare_runs(_mixed(), _mixed(2), config)
        native = _gate(payload, "Upload success floor [native]")
        assert native["candidate"] == 100.0
        assert native["verdict"] == PASS

    def test_wasm_is_judged_on_wasm_records_alone(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        wasm = _gate(payload, "Upload success floor [wasm]")
        assert wasm["candidate"] == pytest.approx(100 * 35 / 38, abs=0.01)
        assert wasm["verdict"] == FAIL

    def test_transfer_percentiles_are_not_blended(self, config):
        # The whole point. Blended, 15s native and 33s wasm produce a percentile
        # neither tier ever exhibited — and one that shifts with the VM mix.
        payload = compare_runs(_mixed(), _mixed(2), config)
        native = _gate(payload, "Upload duration p50 [0-32MB] [native]")
        wasm = _gate(payload, "Upload duration p50 [0-32MB] [wasm]")
        assert native["candidate"] == 15000.0
        assert wasm["candidate"] == 33000.0

    def test_every_client_gate_carries_a_transport(self, config):
        # No client-record gate may be left un-suffixed on a mixed run, or it is
        # silently still blending.
        payload = compare_runs(_mixed(), _mixed(2), config)
        client_gates = [
            g["gate"]
            for g in payload["gates"]
            if any(k in g["gate"] for k in ("success floor", "success rate", "duration p"))
        ]
        assert client_gates
        assert all(g.endswith("[native]") or g.endswith("[wasm]") for g in client_gates)


class TestWasmFloorGatesAreAdvisory:
    """The FLOORS are advisory; the RELATIVE gates count (V2-1286).

    A floor is an absolute number calibrated on the native transport, and the
    gap the WASM tier shows against it is NAT — a peer behind the simulated
    inbound NAT is unreachable over WebRTC-Direct until relayed WebRTC exists —
    not the release under test. A relative gate asks only whether this release
    made the WASM tier worse than the baseline did, which needs no absolute
    calibration at all.
    """

    def test_wasm_floor_gates_are_flagged_advisory(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        floors = _gates(payload, "Upload success floor [wasm]") + _gates(
            payload, "Download success floor [wasm]"
        )
        assert floors
        assert all(g["advisory"] for g in floors)

    def test_wasm_relative_gates_count(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        relative = [
            g
            for g in payload["gates"]
            if "[wasm]" in g["gate"] and "success floor" not in g["gate"]
        ]
        assert relative
        assert not any(g["advisory"] for g in relative)

    def test_native_gates_are_never_advisory(self, config):
        payload = compare_runs(_mixed(), _mixed(2), config)
        assert not any(g["advisory"] for g in payload["gates"] if "[native]" in g["gate"])

    def test_a_failing_wasm_floor_does_not_block(self, config):
        # WASM is below both floors here, and the ship must not be blocked on a
        # tier whose floors have not been calibrated for it.
        payload = compare_runs(_mixed(), _mixed(2), config)
        assert _gate(payload, "Upload success floor [wasm]")["verdict"] == FAIL
        assert payload["overall"] != "HARD FAIL"

    def test_a_failing_native_hard_gate_still_blocks(self, config):
        candidate = _mixed()
        candidate = replace(
            candidate,
            upload_records=ops(100, failures=20, kind="native")
            + ops(38, failures=3, kind="wasm"),
        )
        payload = compare_runs(candidate, _mixed(2), config)
        assert payload["overall"] == "HARD FAIL"

    def test_a_counted_wasm_relative_fail_is_soft_never_hard(self, config):
        """It must reach the verdict, and must never reach the exit code."""
        # The WASM tier collapses against its own baseline: a success-rate drop
        # far beyond the threshold, on the relative gate that counts.
        candidate = replace(
            _mixed(),
            upload_records=ops(100, kind="native") + ops(40, failures=30, kind="wasm"),
        )
        baseline = replace(
            _mixed(2),
            upload_records=ops(100, kind="native") + ops(40, kind="wasm"),
        )
        payload = compare_runs(candidate, baseline, config)
        rate = _gate(payload, "Upload success rate [wasm]")
        assert rate["verdict"] == FAIL
        assert rate["advisory"] is False
        assert rate["kind"] == "soft"
        assert payload["overall"] == "SOFT FAIL"

    def test_floor_advisory_can_be_switched_off(self, config):
        strict = replace(config, wasm_floor_gates_advisory=False)
        payload = compare_runs(_mixed(), _mixed(2), strict)
        assert not any(g["advisory"] for g in payload["gates"])
        assert payload["overall"] == "HARD FAIL"

    def test_relative_advisory_can_be_switched_on(self, config):
        lax = replace(config, wasm_relative_gates_advisory=True)
        payload = compare_runs(_mixed(), _mixed(2), lax)
        assert all(g["advisory"] for g in payload["gates"] if "[wasm]" in g["gate"])


class TestOneSidedComparisons:
    def test_wasm_missing_from_the_baseline_is_not_assessed_not_failed(self, config):
        # A baseline predating the WASM tier must not make the candidate's WASM
        # gates fail — there is simply nothing to compare against.
        baseline = _run(deployment_id=2, upload_records=ops(50), download_records=ops(50))
        payload = compare_runs(_mixed(), baseline, config)
        rate = _gate(payload, "Upload success rate [wasm]")
        assert rate["verdict"] == "NOT ASSESSED"
        assert payload["overall"] != "HARD FAIL"

    def test_the_native_half_still_compares_normally(self, config):
        baseline = _run(deployment_id=2, upload_records=ops(50), download_records=ops(50))
        payload = compare_runs(_mixed(), baseline, config)
        assert _gate(payload, "Upload success floor [native]")["verdict"] == PASS


# --------------------------------------------------------------------------- #
# Influx-side separation
# --------------------------------------------------------------------------- #
class TestWasmBucketIsSeparate:
    def test_the_bucket_is_distinct_from_the_others(self):
        from saorsa_deploy.provisioning.monitoring import (
            INFLUXDB_BUCKET,
            INFLUXDB_CLIENT_BUCKET,
            INFLUXDB_WASM_BUCKET,
        )

        assert len({INFLUXDB_BUCKET, INFLUXDB_CLIENT_BUCKET, INFLUXDB_WASM_BUCKET}) == 3

    def test_setup_script_creates_it(self):
        from saorsa_deploy.resources import get_resources_dir

        script = (
            get_resources_dir() / "monitoring" / "influxdb_setup.py"
        ).read_text()
        assert "{{INFLUXDB_WASM_BUCKET}}" in script
        # Created as a bucket, not merely mentioned in a comment.
        assert script.count('"name": "{{INFLUXDB_WASM_BUCKET}}"') == 1

    def test_provisioner_substitutes_the_placeholder(self):
        import inspect

        from saorsa_deploy.provisioning import monitoring

        src = inspect.getsource(monitoring)
        assert '.replace("{{INFLUXDB_WASM_BUCKET}}", INFLUXDB_WASM_BUCKET)' in src


class TestRecordResultsReadsBothBuckets:
    def test_points_are_deduplicated_by_op_identity(self):
        # Two buckets must never yield the same operation twice: a double count
        # would inflate the aggregate success counts and the per-GB denominators
        # with nothing downstream looking wrong.
        from unittest.mock import patch

        from saorsa_deploy.cmd.record_results import _fetch_all_op_points

        pt = {"host": "h1", "start_ns": 1, "_measurement": "upload"}
        with patch(
            "saorsa_deploy.influx.fetch_operation_points",
            return_value=[dict(pt)],
        ) as fetch:
            out = _fetch_all_op_points("u", "t", "o", "testnet-metrics", "0")
        assert fetch.call_count == 2  # node bucket + wasm bucket
        assert len(out) == 1

    def test_distinct_operations_are_all_kept(self):
        from unittest.mock import patch

        from saorsa_deploy.cmd.record_results import _fetch_all_op_points

        calls = [
            [{"host": "h1", "start_ns": 1, "_measurement": "upload"}],
            [{"host": "h2", "start_ns": 2, "_measurement": "upload"}],
        ]
        with patch(
            "saorsa_deploy.influx.fetch_operation_points",
            side_effect=calls,
        ):
            out = _fetch_all_op_points("u", "t", "o", "testnet-metrics", "0")
        assert len(out) == 2

    def test_a_missing_wasm_bucket_is_tolerated(self):
        # Every deployment predating the WASM tier has no such bucket.
        from unittest.mock import patch

        from saorsa_deploy.cmd.record_results import _fetch_all_op_points

        def side_effect(**kw):
            if kw["bucket"] == "wasm-client-metrics":
                raise RuntimeError("bucket not found")
            return [{"host": "h1", "start_ns": 1, "_measurement": "upload"}]

        with patch(
            "saorsa_deploy.influx.fetch_operation_points",
            side_effect=side_effect,
        ):
            out = _fetch_all_op_points("u", "t", "o", "testnet-metrics", "0")
        assert len(out) == 1
