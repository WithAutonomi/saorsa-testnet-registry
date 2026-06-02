"""Tests for the prod canary CLI helpers and the global two-strike loss
derivation. Sharding hash and probe stderr parsing are tested by importing
the runtime scripts directly via importlib so the *actual* code that ships
on the VM is exercised."""
from __future__ import annotations

import enum
import importlib.util
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from saorsa_deploy.cmd.prod_canary import (
    parse_probe_spec,
    parse_upload_spec,
)
from saorsa_deploy.cmd.prod_canary_results import (
    derive_loss,
    derive_unavailability_episodes,
)

# ---------------------------------------------------------------------------
# parse_upload_spec / parse_probe_spec
# ---------------------------------------------------------------------------


class TestParseUploadSpec:
    def test_region_only(self):
        spec = parse_upload_spec("lon1")
        assert spec == {"region": "lon1", "vm_size": "s-1vcpu-2gb"}

    def test_region_and_vm_size(self):
        spec = parse_upload_spec("nyc3:s-2vcpu-4gb")
        assert spec == {"region": "nyc3", "vm_size": "s-2vcpu-4gb"}

    def test_empty_region_rejected(self):
        with pytest.raises(ValueError, match="region cannot be empty"):
            parse_upload_spec("")

    def test_too_many_parts_rejected(self):
        with pytest.raises(ValueError):
            parse_upload_spec("lon1:foo:bar")


class TestParseProbeSpec:
    def test_region_with_count(self):
        # Probe VMs default to s-2vcpu-4gb (RAM headroom for concurrent
        # large-file downloads); only the count is required.
        assert parse_probe_spec("lon1:count=2") == {
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            "count": 2,
        }

    def test_region_vm_size_count(self):
        assert parse_probe_spec("nyc3:s-2vcpu-4gb:count=3") == {
            "region": "nyc3",
            "vm_size": "s-2vcpu-4gb",
            "count": 3,
        }

    def test_missing_count_rejected(self):
        with pytest.raises(ValueError, match="count=N is required"):
            parse_probe_spec("lon1:s-2vcpu-4gb")

    def test_count_zero_rejected(self):
        with pytest.raises(ValueError, match="count must be >= 1"):
            parse_probe_spec("lon1:count=0")

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown keys"):
            parse_probe_spec("lon1:count=2:foo=bar")


# ---------------------------------------------------------------------------
# derive_loss — global two-strike rule
# ---------------------------------------------------------------------------


class _Status(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


def _probe(probed_at, status, missing="", host="probe-A", peer_outcomes=None):
    return SimpleNamespace(
        probed_at=probed_at,
        probed_by_host=host,
        status=_Status(status),
        missing_chunk_address=missing,
        terminal_peer_outcomes=peer_outcomes,
    )


def _ts(hours):
    return datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hours)


class TestDeriveLoss:
    def test_two_consecutive_failures_same_chunk_confirms_loss(self):
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(1), "ok"),
                _probe(_ts(2), "failed", missing="ADDR1"),
                _probe(_ts(3), "failed", missing="ADDR1"),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid in losses
        assert losses[cid]["missing_chunk_address"] == "ADDR1"
        assert losses[cid]["first_failed_probe_at"] == _ts(2)
        assert losses[cid]["confirmed_lost_at"] == _ts(3)
        assert losses[cid]["hours_to_first_failure"] == pytest.approx(2.0)
        assert losses[cid]["hours_to_confirmed_loss"] == pytest.approx(3.0)

    def test_different_missing_chunks_resets_first_failed(self):
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(1), "failed", missing="ADDR1"),
                _probe(_ts(2), "failed", missing="ADDR2"),
                _probe(_ts(3), "failed", missing="ADDR2"),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert losses[cid]["missing_chunk_address"] == "ADDR2"
        assert losses[cid]["first_failed_probe_at"] == _ts(2)

    def test_ok_between_failures_resets(self):
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(1), "failed", missing="ADDR1"),
                _probe(_ts(2), "ok"),
                _probe(_ts(3), "failed", missing="ADDR1"),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid not in losses

    def test_inconclusive_skipped(self):
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(1), "failed", missing="ADDR1"),
                _probe(_ts(2), "inconclusive"),
                _probe(_ts(3), "failed", missing="ADDR1"),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid in losses
        assert losses[cid]["confirmed_lost_at"] == _ts(3)

    def test_per_host_isolation(self):
        # Two hosts probing the same canary in alternation must NOT confirm
        # a loss across host boundaries — consecutive failures are
        # per-vantage.
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(1), "failed", missing="ADDR1", host="probe-A"),
                _probe(_ts(2), "failed", missing="ADDR1", host="probe-B"),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid not in losses

    def test_earliest_vantage_wins_when_multiple_confirm(self):
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(5), "failed", missing="ADDR1", host="probe-A"),
                _probe(_ts(6), "failed", missing="ADDR1", host="probe-A"),
                _probe(_ts(2), "failed", missing="ADDR2", host="probe-B"),
                _probe(_ts(3), "failed", missing="ADDR2", host="probe-B"),
            ]
        }
        losses = derive_loss(uploads, probes)
        # probe-B confirmed at hour 3, probe-A at hour 6 → B wins.
        assert losses[cid]["confirmed_lost_at"] == _ts(3)
        assert losses[cid]["missing_chunk_address"] == "ADDR2"

    def test_timeout_dominated_failures_do_not_confirm_loss(self):
        # Two consecutive failures on the same chunk, but each failure was
        # timeout-dominated (only 1 of 7 close-group peers actually said
        # NotFound). These reflect a reachability problem, not loss, so they
        # must be reclassified as inconclusive and NOT confirm a loss.
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        timeout_dominated = {
            "queried": 7, "not_found": 1, "timeout": 6, "network_err": 0,
        }
        probes = {
            cid: [
                _probe(_ts(2), "failed", missing="ADDR1",
                       peer_outcomes=timeout_dominated),
                _probe(_ts(3), "failed", missing="ADDR1",
                       peer_outcomes=timeout_dominated),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid not in losses

    def test_not_found_majority_confirms_loss(self):
        # A strict majority of the close group actively responded NotFound —
        # genuine absence — so the two-strike rule still confirms.
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        genuine = {"queried": 7, "not_found": 7, "timeout": 0, "network_err": 0}
        probes = {
            cid: [
                _probe(_ts(2), "failed", missing="ADDR1", peer_outcomes=genuine),
                _probe(_ts(3), "failed", missing="ADDR1", peer_outcomes=genuine),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid in losses
        assert losses[cid]["confirmed_lost_at"] == _ts(3)

    def test_missing_peer_outcomes_falls_back_to_failed(self):
        # No terminal_peer_outcomes (e.g. ant binary without the chunk_get
        # summary line) → can't downgrade; preserve old failed semantics.
        cid = uuid.uuid4()
        uploads = {cid: _ts(0)}
        probes = {
            cid: [
                _probe(_ts(2), "failed", missing="ADDR1", peer_outcomes=None),
                _probe(_ts(3), "failed", missing="ADDR1", peer_outcomes=None),
            ]
        }
        losses = derive_loss(uploads, probes)
        assert cid in losses


class TestDeriveUnavailabilityEpisodes:
    def test_recovered_episode_has_duration(self):
        up = _ts(0)
        probes = [
            _probe(_ts(1), "ok"),
            _probe(_ts(2), "failed", missing="A"),
            _probe(_ts(3), "failed", missing="A"),  # confirmed at hour 3
            _probe(_ts(8), "ok"),                     # recovered at hour 8
        ]
        eps = derive_unavailability_episodes(up, probes)
        assert len(eps) == 1
        e = eps[0]
        assert e["recovered"] is True
        assert e["confirmed_unavailable_at"] == _ts(3)
        assert e["recovered_at"] == _ts(8)
        assert e["unavailable_hours"] == pytest.approx(5.0)
        assert e["hours_to_confirmed"] == pytest.approx(3.0)
        assert e["episode_no"] == 1

    def test_ongoing_episode_has_no_recovery(self):
        up = _ts(0)
        probes = [
            _probe(_ts(2), "failed", missing="A"),
            _probe(_ts(3), "failed", missing="A"),
        ]
        eps = derive_unavailability_episodes(up, probes)
        assert len(eps) == 1
        assert eps[0]["recovered"] is False
        assert eps[0]["recovered_at"] is None
        assert eps[0]["unavailable_hours"] is None

    def test_two_separate_episodes(self):
        up = _ts(0)
        probes = [
            _probe(_ts(1), "failed", missing="A"),
            _probe(_ts(2), "failed", missing="A"),  # episode 1 confirmed
            _probe(_ts(3), "ok"),                     # episode 1 recovered
            _probe(_ts(5), "failed", missing="A"),
            _probe(_ts(6), "failed", missing="A"),  # episode 2 confirmed
            _probe(_ts(7), "ok"),                     # episode 2 recovered
        ]
        eps = derive_unavailability_episodes(up, probes)
        assert len(eps) == 2
        assert [e["episode_no"] for e in eps] == [1, 2]
        assert all(e["recovered"] for e in eps)

    def test_single_failure_is_not_an_episode(self):
        up = _ts(0)
        probes = [_probe(_ts(2), "failed", missing="A"), _probe(_ts(3), "ok")]
        assert derive_unavailability_episodes(up, probes) == []

    def test_timeout_dominated_does_not_open_episode(self):
        up = _ts(0)
        td = {"queried": 7, "not_found": 1, "timeout": 6}
        probes = [
            _probe(_ts(2), "failed", missing="A", peer_outcomes=td),
            _probe(_ts(3), "failed", missing="A", peer_outcomes=td),
        ]
        assert derive_unavailability_episodes(up, probes) == []


# ---------------------------------------------------------------------------
# Runtime scripts: import via importlib so we exercise the on-VM code.
# ---------------------------------------------------------------------------


_RESOURCES = (
    Path(__file__).resolve().parent.parent
    / "saorsa_deploy"
    / "resources"
    / "scripts"
)


def _import_script(stem: str):
    path = _RESOURCES / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"_canary_{stem}", str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe_mod():
    return _import_script("prod_canary_probe")


@pytest.fixture(scope="module")
def upload_mod():
    return _import_script("prod_canary_upload")


# --- Sharding -------------------------------------------------------------


class TestSharding:
    def test_partition_covers_every_canary_exactly_once(self, probe_mod):
        # 1000 random canary IDs hashed into 4 shards: each ID belongs to
        # exactly one shard and shard sizes are roughly balanced.
        cids = [str(uuid.uuid4()) for _ in range(1000)]
        ownership = [0] * 4
        for cid in cids:
            owners = [
                probe_mod._owns(cid, i, 4) for i in range(4)
            ]
            assert sum(owners) == 1
            ownership[owners.index(True)] += 1
        # Allow a generous tolerance (uniform expected ≈ 250 each).
        for count in ownership:
            assert 200 <= count <= 300

    def test_owns_is_stable(self, probe_mod):
        cid = "11111111-1111-1111-1111-111111111111"
        first = [probe_mod._owns(cid, i, 8) for i in range(8)]
        second = [probe_mod._owns(cid, i, 8) for i in range(8)]
        assert first == second
        assert sum(first) == 1


# --- Probe stderr parsing -------------------------------------------------


class TestProbeOutcomeParsing:
    def test_ok_when_zero_exit_with_data(self, probe_mod):
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=0, stderr_text="", downloaded_bytes=42
        )
        assert status == probe_mod.PROBE_OK
        assert missing == ""
        assert peer == ""

    def test_inconclusive_when_zero_exit_zero_bytes(self, probe_mod):
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=0, stderr_text="", downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_INCONCLUSIVE
        assert missing == ""

    def test_data_map_failure_extracts_address(self, probe_mod):
        stderr = (
            "some preamble\n"
            "Error: Failed to fetch public DataMap: invalid data: "
            "DataMap chunk not found at abcdef0123456789\n"
        )
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text=stderr, downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_FAILED
        assert missing == "abcdef0123456789"
        assert "data_map" in peer

    def test_data_chunk_failure_extracts_address(self, probe_mod):
        stderr = (
            "noise\n"
            "Error: Download failed: encryption error: decryption failed: "
            "Generic error(Chunk not found: 0123456789abcdef)\n"
        )
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text=stderr, downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_FAILED
        assert missing == "0123456789abcdef"
        assert '"data"' in peer

    def test_failure_without_chunk_addr_is_inconclusive(self, probe_mod):
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text="random network glitch\n",
            downloaded_bytes=0,
        )
        assert status == probe_mod.PROBE_INCONCLUSIVE
        assert missing == ""

    def test_peer_summary_parsed(self, probe_mod):
        stderr = (
            "Error: Download failed: encryption error: decryption failed: "
            "Generic error(Chunk not found: ADDRX)\n"
            "INFO chunk_get exhausted close group for ADDRX: "
            "queried=20 not_found=18 timeout=1 network_err=1\n"
        )
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text=stderr, downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_FAILED
        assert missing == "ADDRX"
        import json as _json
        outcomes = _json.loads(peer)
        assert outcomes["queried"] == 20
        assert outcomes["not_found"] == 18
        assert outcomes["timeout"] == 1
        assert outcomes["network_err"] == 1
        assert outcomes["role"] == "data"

    def test_timeout_dominated_failure_is_inconclusive(self, probe_mod):
        # Most of the close group timed out; only 1 of 7 said NotFound. The
        # chunk's presence is undetermined, so this is a reachability problem,
        # not loss → inconclusive (but the address + summary are still kept).
        stderr = (
            "Error: Download failed: encryption error: decryption failed: "
            "Generic error(Chunk not found: ADDRY)\n"
            "INFO chunk_get exhausted close group for ADDRY: "
            "queried=7 not_found=1 timeout=6 network_err=0\n"
        )
        status, missing, peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text=stderr, downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_INCONCLUSIVE
        assert missing == "ADDRY"
        import json as _json
        assert _json.loads(peer)["timeout"] == 6

    def test_not_found_majority_failure_is_failed(self, probe_mod):
        stderr = (
            "Error: Download failed: encryption error: decryption failed: "
            "Generic error(Chunk not found: ADDRZ)\n"
            "INFO chunk_get exhausted close group for ADDRZ: "
            "queried=7 not_found=5 timeout=2 network_err=0\n"
        )
        status, missing, _peer = probe_mod._parse_probe_outcome(
            exit_code=1, stderr_text=stderr, downloaded_bytes=0
        )
        assert status == probe_mod.PROBE_FAILED
        assert missing == "ADDRZ"


# --- Two-strike LOCAL rule (probe-side SQLite) ----------------------------


class TestProbeLocalTwoStrike:
    def _setup(self, probe_mod, tmp_path):
        conn = probe_mod._open_state(tmp_path / "state.db")
        cid = "00000000-0000-0000-0000-000000000001"
        probe_mod._insert_state(conn, cid, "topaddr")
        return conn, cid

    def test_failed_then_failed_same_chunk_marks_unavailable(
        self, probe_mod, tmp_path
    ):
        conn, cid = self._setup(probe_mod, tmp_path)
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t1",
        )
        t = probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t2",
        )
        row = probe_mod._state_row(conn, cid)
        assert row[2] == probe_mod.STATUS_UNAVAILABLE
        # unavailable_since stamped from the first failure, transition reported
        assert row[6] == "t1"
        assert t == ("unavailable", "A")

    def test_unavailable_then_ok_recovers(self, probe_mod, tmp_path):
        conn, cid = self._setup(probe_mod, tmp_path)
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t1",
        )
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t2",
        )
        # recovery: an OK probe after being unavailable returns it to alive
        t = probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_OK,
            missing_chunk="", probed_at_iso="t3",
        )
        row = probe_mod._state_row(conn, cid)
        assert row[2] == probe_mod.STATUS_ALIVE
        assert row[6] is None  # unavailable_since cleared
        assert t == ("recovered", "")

    def test_unavailable_canary_still_probeable(self, probe_mod, tmp_path):
        conn, cid = self._setup(probe_mod, tmp_path)
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t1",
        )
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t2",
        )
        # An unavailable canary must remain in the probe set (so we can see
        # recovery) — the old design dropped it here.
        ids = [c for c, _ in probe_mod._probeable_canaries(conn)]
        assert cid in ids

    def test_failed_then_ok_resets(self, probe_mod, tmp_path):
        conn, cid = self._setup(probe_mod, tmp_path)
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t1",
        )
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_OK,
            missing_chunk="", probed_at_iso="t2",
        )
        row = probe_mod._state_row(conn, cid)
        assert row[2] == probe_mod.STATUS_ALIVE
        assert row[3] is None
        assert row[4] is None

    def test_failed_then_failed_different_chunks_does_not_lose(
        self, probe_mod, tmp_path
    ):
        conn, cid = self._setup(probe_mod, tmp_path)
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="A", probed_at_iso="t1",
        )
        probe_mod._apply_two_strike(
            conn, canary_id=cid, status=probe_mod.PROBE_FAILED,
            missing_chunk="B", probed_at_iso="t2",
        )
        row = probe_mod._state_row(conn, cid)
        assert row[2] == probe_mod.STATUS_ALIVE
        assert row[4] == "B"
