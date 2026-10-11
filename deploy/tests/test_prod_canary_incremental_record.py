"""`record-results` for prod-canary: batched writes and an incremental pull.

PROD-CANARY-04's first publisher run took 5,062 s for 423,108 probe points —
one `INSERT … ON CONFLICT` and one `commit()` per row, over the whole series,
every day. Two things had to change, and both have a correctness edge that is
easy to get wrong:

* the writes are batched, so the per-chunk upsert must leave exactly what N
  single-row upserts would have left, duplicates and all;
* the probe pull is windowed, so the loss and episode derivation — which reads
  Postgres, not the window — must still see both strikes of a two-strike
  confirmation that began before the window opened.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_canary import (
    DeploymentProdCanaryProbe,
    DeploymentProdCanaryUpload,
    ProdCanaryChunkRole,
    ProdCanaryInitialVerifyStatus,
    ProdCanaryProbeStatus,
)
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from saorsa_deploy.cmd import prod_canary_results
from saorsa_deploy.cmd.prod_canary_results import (
    record_prod_canary_results,
    resolve_probe_start,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


class _Args:
    def __init__(self, name="PROD-CANARY-TEST", since=None, full=False):
        self.name = name
        self.since = since
        self.full = full
        self.update_slack = False


@pytest.fixture
def env():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    repo = SqlAlchemyDeploymentRepository(session)
    deployment = Deployment(name="PROD-CANARY-TEST", deploy_args={"kind": "prod-canary"})
    session.add(deployment)
    session.commit()
    yield repo, session, deployment, engine
    session.close()


def _upload(repo, deployment, canary_id, uploaded_at=T0):
    repo.upsert_prod_canary_upload(
        canary_id=canary_id,
        deployment_id=deployment.id,
        source_url=f"https://example.invalid/{canary_id}",
        file_name=f"{canary_id}.bin",
        file_size_bytes=1024,
        top_address="top",
        num_chunks=3,
        uploaded_at=uploaded_at,
        initial_verify_status=ProdCanaryInitialVerifyStatus.OK,
    )


def _probe(canary_id, hours, host="probe-1", status="ok", missing=None, duration=10):
    return {
        "canary_id": canary_id,
        "probed_at": T0 + timedelta(hours=hours),
        "probed_by_host": host,
        "status": ProdCanaryProbeStatus(status),
        "missing_chunk_address": missing,
        "terminal_peer_outcomes": None,
        "duration_ms": duration,
    }


# ---------------------------------------------------------------------------
# 1. Batched writes
# ---------------------------------------------------------------------------


class TestBulkProbeUpsert:
    def test_matches_single_row_upserts_row_for_row(self, env):
        """Same probes through both paths must leave identical rows.

        The fixture carries duplicates (the same (canary, time, host) written
        twice with different detail) because a batch cannot upsert a row twice
        in one statement — the bulk path has to collapse them the way a
        sequence of single-row upserts would, last write winning.
        """
        repo, session, deployment, _ = env
        bulk_id, single_id = uuid.uuid4(), uuid.uuid4()
        _upload(repo, deployment, bulk_id)
        _upload(repo, deployment, single_id)

        def _rows(canary_id):
            rows = []
            for i in range(2500):
                rows.append(
                    _probe(
                        canary_id,
                        i,
                        host=f"probe-{i % 3}",
                        status="failed" if i % 7 == 0 else "ok",
                        missing=f"chunk-{i}" if i % 7 == 0 else None,
                        duration=i,
                    )
                )
            # 500 conflicting rewrites of the first 500 probes: a later point
            # for the same (canary, time, host) with different detail.
            for i in range(500):
                rows.append(
                    _probe(canary_id, i, host=f"probe-{i % 3}", status="inconclusive", duration=-i)
                )
            return rows

        repo.bulk_upsert_prod_canary_probes(_rows(bulk_id))
        for row in _rows(single_id):
            repo.upsert_prod_canary_probe(
                canary_id=row["canary_id"],
                probed_at=row["probed_at"],
                probed_by_host=row["probed_by_host"],
                status=row["status"],
                missing_chunk_address=row["missing_chunk_address"],
                terminal_peer_outcomes=row["terminal_peer_outcomes"],
                duration_ms=row["duration_ms"],
            )

        def _comparable(canary_id):
            return sorted(
                (
                    p.probed_at,
                    p.probed_by_host,
                    p.status,
                    p.missing_chunk_address,
                    p.duration_ms,
                )
                for p in session.query(DeploymentProdCanaryProbe)
                .filter(DeploymentProdCanaryProbe.canary_id == canary_id)
                .all()
            )

        assert _comparable(bulk_id) == _comparable(single_id)
        assert len(_comparable(bulk_id)) == 2500

    @pytest.mark.parametrize("count", [999, 1000, 1001, 2001])
    def test_one_commit_per_chunk_at_the_boundary(self, env, count):
        """One commit per 1,000 rows, not one per row — which is the whole fix."""
        repo, session, deployment, engine = env
        canary_id = uuid.uuid4()
        _upload(repo, deployment, canary_id)

        commits = []

        def _count_commit(conn):
            commits.append(1)

        event.listen(engine, "commit", _count_commit)
        try:
            written = repo.bulk_upsert_prod_canary_probes(
                [_probe(canary_id, i) for i in range(count)]
            )
        finally:
            event.remove(engine, "commit", _count_commit)

        assert written == count
        assert len(commits) == (count + 999) // 1000
        assert session.query(DeploymentProdCanaryProbe).count() == count

    def test_empty_list_writes_nothing(self, env):
        repo, _, _, _ = env
        assert repo.bulk_upsert_prod_canary_probes([]) == 0
        assert repo.bulk_upsert_prod_canary_uploads([]) == 0
        assert repo.bulk_upsert_prod_canary_chunks([]) == 0

    def test_upload_upsert_keeps_a_cost_a_later_pass_does_not_carry(self, env):
        """The single-row method only overwrote a cost field when given one.

        The bulk path has to COALESCE for the same reason: an Influx point
        written before the upload script emitted cost telemetry carries NULL,
        and assigning it would erase what an earlier pass recorded.
        """
        from decimal import Decimal

        repo, session, deployment, _ = env
        canary_id = uuid.uuid4()
        _upload(repo, deployment, canary_id)
        repo.bulk_upsert_prod_canary_uploads(
            [
                {
                    "canary_id": canary_id,
                    "deployment_id": deployment.id,
                    "source_url": "u",
                    "file_name": "f",
                    "file_size_bytes": 1,
                    "top_address": "t",
                    "num_chunks": 1,
                    "uploaded_at": T0,
                    "initial_verify_status": ProdCanaryInitialVerifyStatus.OK,
                    "storage_cost_atto": Decimal("42"),
                    "gas_cost_wei": Decimal("7"),
                    "chunk_attempts_total": 5,
                }
            ]
        )
        _upload(repo, deployment, canary_id)  # no cost supplied
        row = session.get(DeploymentProdCanaryUpload, canary_id)
        assert row.storage_cost_atto == Decimal("42")
        assert row.gas_cost_wei == Decimal("7")
        assert row.chunk_attempts_total == 5

    def test_chunk_upsert_updates_the_role(self, env):
        repo, session, deployment, _ = env
        canary_id = uuid.uuid4()
        _upload(repo, deployment, canary_id)
        repo.bulk_upsert_prod_canary_chunks(
            [
                {
                    "canary_id": canary_id,
                    "chunk_address": "addr-1",
                    "chunk_role": ProdCanaryChunkRole.DATA,
                },
                {
                    "canary_id": canary_id,
                    "chunk_address": "addr-1",
                    "chunk_role": ProdCanaryChunkRole.DATA_MAP,
                },
            ]
        )
        from saorsa_db.models.prod_canary import DeploymentProdCanaryChunk

        rows = session.query(DeploymentProdCanaryChunk).all()
        assert len(rows) == 1
        assert rows[0].chunk_role is ProdCanaryChunkRole.DATA_MAP


# ---------------------------------------------------------------------------
# 2. The incremental window
# ---------------------------------------------------------------------------


class TestResolveProbeStart:
    def test_no_probes_pulls_the_whole_series(self, env):
        repo, _, deployment, _ = env
        start, reason = resolve_probe_start(repo, deployment, _Args())
        assert start == "0"
        assert "no probes recorded yet" in reason

    def test_default_starts_six_hours_before_the_newest_probe(self, env):
        repo, _, deployment, _ = env
        canary_id = uuid.uuid4()
        _upload(repo, deployment, canary_id)
        repo.bulk_upsert_prod_canary_probes([_probe(canary_id, h) for h in range(48)])
        start, reason = resolve_probe_start(repo, deployment, _Args())
        # newest probe is T0+47h, so the window opens at T0+41h.
        assert start == "2026-10-02T17:00:00Z"
        assert "incremental" in reason

    def test_since_zero_and_full_both_pull_everything(self, env):
        repo, _, deployment, _ = env
        canary_id = uuid.uuid4()
        _upload(repo, deployment, canary_id)
        repo.bulk_upsert_prod_canary_probes([_probe(canary_id, 0)])
        assert resolve_probe_start(repo, deployment, _Args(since="0"))[0] == "0"
        assert resolve_probe_start(repo, deployment, _Args(full=True))[0] == "0"

    def test_explicit_since_is_used_verbatim(self, env):
        repo, _, deployment, _ = env
        start, _ = resolve_probe_start(repo, deployment, _Args(since="2026-09-01T12:30:00Z"))
        assert start == "2026-09-01T12:30:00Z"

    def test_a_bad_since_is_rejected(self, env):
        repo, _, deployment, _ = env
        with pytest.raises(ValueError, match="ISO-8601"):
            resolve_probe_start(repo, deployment, _Args(since="last tuesday"))


# ---------------------------------------------------------------------------
# 3. record-results end to end against a fake Influx
# ---------------------------------------------------------------------------


def _wire_influx(monkeypatch, uploads, chunks, probes, seen_starts):
    """Stand in for the three Influx fetchers, capturing the probe start."""

    def _uploads(**kwargs):
        return list(uploads)

    def _chunks(**kwargs):
        return list(chunks)

    def _probes(**kwargs):
        start = kwargs.get("start", "0")
        seen_starts.append(start)
        if start == "0":
            return list(probes)
        cutoff = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return [p for p in probes if p["probed_at"] >= cutoff]

    monkeypatch.setattr(prod_canary_results, "fetch_canary_upload_points", _uploads)
    monkeypatch.setattr(prod_canary_results, "fetch_canary_chunk_points", _chunks)
    monkeypatch.setattr(prod_canary_results, "fetch_canary_probe_points", _probes)


def _influx_upload(canary_id, uploaded_at=T0):
    return {
        "canary_id": canary_id,
        "host": "upload-1",
        "region": "lon1",
        "source_url": f"https://example.invalid/{canary_id}",
        "file_name": f"{canary_id}.bin",
        "file_size_bytes": 1024,
        "top_address": "top",
        "num_chunks": 3,
        "uploaded_at": uploaded_at,
        "started_at": uploaded_at,
        "duration_ms": 100,
        "initial_verify_status": "ok",
        "storage_cost_atto": "",
        "gas_cost_wei": "",
        "chunk_attempts_total": None,
    }


def _influx_probe(canary_id, hours, status="ok", missing="", host="probe-1"):
    return {
        "canary_id": canary_id,
        "probed_at": T0 + timedelta(hours=hours),
        "probed_by_host": host,
        "status": status,
        "missing_chunk_address": missing,
        "terminal_peer_outcomes": "",
        "duration_ms": 10,
    }


def _utc(value):
    """SQLite hands back a naive datetime where Postgres is aware."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _console():
    from rich.console import Console

    return Console(quiet=True)


class TestIncrementalRecordRun:
    def test_first_run_pulls_everything_then_the_next_pulls_the_window(self, env, monkeypatch):
        repo, session, deployment, _ = env
        canary_id = uuid.uuid4()
        probes = [_influx_probe(canary_id, h) for h in range(48)]
        starts: list[str] = []
        _wire_influx(monkeypatch, [_influx_upload(canary_id)], [], probes, starts)

        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        assert starts == ["0"]
        assert session.query(DeploymentProdCanaryProbe).count() == 48

        # Two more days of probes arrive; the second run asks only for the
        # window and writes only what it read.
        probes.extend(_influx_probe(canary_id, h) for h in range(48, 96))
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        # Newest recorded was T0+47h, so the window opens at T0+41h.
        assert starts[1] == "2026-10-02T17:00:00Z"
        assert session.query(DeploymentProdCanaryProbe).count() == 96

    def test_full_forces_the_whole_series(self, env, monkeypatch):
        repo, _, deployment, _ = env
        canary_id = uuid.uuid4()
        probes = [_influx_probe(canary_id, h) for h in range(10)]
        starts: list[str] = []
        _wire_influx(monkeypatch, [_influx_upload(canary_id)], [], probes, starts)
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        record_prod_canary_results(
            repo, deployment, _Args(full=True), _console(), "http://i", "t", "ant", "b"
        )
        assert starts == ["0", "0"]

    def test_a_confirmed_episode_survives_a_window_that_opens_mid_episode(self, env, monkeypatch):
        """The two strikes straddle the window boundary.

        The first failure is at T0+40h; the probes after it are inconclusive
        (the rule skips those) out to T0+60h, so the second run's window opens
        at T0+54h and holds the second strike ALONE. It must still confirm,
        because the derivation reads the probe history from Postgres rather
        than the window that was pulled.
        """
        repo, _, deployment, _ = env
        canary_id = uuid.uuid4()
        probes = [_influx_probe(canary_id, h) for h in range(40)]
        probes.append(_influx_probe(canary_id, 40, status="failed", missing="chunk-x"))
        probes.extend(_influx_probe(canary_id, h, status="inconclusive") for h in range(41, 61))
        starts: list[str] = []
        _wire_influx(monkeypatch, [_influx_upload(canary_id)], [], probes, starts)

        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        assert repo.list_prod_canary_loss_for_deployment(deployment.id) == []

        # The second strike arrives in the next window, which starts after the
        # first one.
        probes.append(_influx_probe(canary_id, 61, status="failed", missing="chunk-x"))
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        assert starts[1] == "2026-10-03T06:00:00Z"  # T0+54h, after the first strike
        losses = repo.list_prod_canary_loss_for_deployment(deployment.id)
        assert len(losses) == 1
        assert losses[0].missing_chunk_address == "chunk-x"
        assert _utc(losses[0].confirmed_lost_at) == T0 + timedelta(hours=61)
        assert _utc(losses[0].first_failed_probe_at) == T0 + timedelta(hours=40)
        episodes = repo.list_prod_canary_unavailability_for_deployment(deployment.id)
        assert len(episodes) == 1 and episodes[0].recovered is False

    def test_a_recovery_in_the_window_is_not_missed(self, env, monkeypatch):
        """An OK probe in the window closes the episode it recovered from.

        The episode view is the one that can represent transient
        unreachability, so this is where a recovery has to land — the loss row
        stands, because the canary WAS confirmed unreachable once. The point
        of the test is that an incremental run sees the recovery at all: the
        probe that closes the episode is in the window, but the episode it
        closes began before it.
        """
        repo, _, deployment, _ = env
        canary_id = uuid.uuid4()
        probes = [
            _influx_probe(canary_id, 1, status="failed", missing="chunk-x"),
            _influx_probe(canary_id, 2, status="failed", missing="chunk-x"),
        ]
        starts: list[str] = []
        _wire_influx(monkeypatch, [_influx_upload(canary_id)], [], probes, starts)
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        assert len(repo.list_prod_canary_loss_for_deployment(deployment.id)) == 1
        assert (
            repo.list_prod_canary_unavailability_for_deployment(deployment.id)[0].recovered is False
        )

        probes.append(_influx_probe(canary_id, 3))
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        episodes = repo.list_prod_canary_unavailability_for_deployment(deployment.id)
        assert len(episodes) == 1
        assert episodes[0].recovered is True
        assert _utc(episodes[0].recovered_at) == T0 + timedelta(hours=3)

    def test_a_canary_with_no_new_probes_is_left_alone(self, env, monkeypatch):
        """Byte-identical derived rows for an untouched canary.

        This is what makes the incremental pull cheap: the derivation writes
        only for canaries the run saw a probe for. A canary whose probe VM went
        quiet keeps the verdict it already had rather than having it rewritten
        — or, worse, cleared because the window held nothing for it.
        """
        repo, _, deployment, _ = env
        quiet_id, busy_id = uuid.uuid4(), uuid.uuid4()
        probes = [
            _influx_probe(quiet_id, 1, status="failed", missing="chunk-q"),
            _influx_probe(quiet_id, 2, status="failed", missing="chunk-q"),
            # The busy canary's probes sit well after the quiet one's, so the
            # second run's window — six hours before the newest recorded probe
            # — opens after the quiet canary's failures and does not reach
            # them. With both canaries probed at the same hours the window
            # would cover the quiet one too, and the test would assert nothing:
            # a re-derived episode row only compares equal on SQLite, which
            # reuses the deleted rowid where Postgres issues a fresh id.
            _influx_probe(busy_id, 20),
            _influx_probe(busy_id, 21),
        ]
        starts: list[str] = []
        _wire_influx(
            monkeypatch,
            [_influx_upload(quiet_id), _influx_upload(busy_id)],
            [],
            probes,
            starts,
        )
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )

        def _snapshot():
            loss = {
                row.canary_id: row.to_dict()
                for row in repo.list_prod_canary_loss_for_deployment(deployment.id)
            }
            episodes = [
                row.to_dict()
                for row in repo.list_prod_canary_unavailability_for_deployment(deployment.id)
            ]
            return loss, episodes

        before = _snapshot()
        assert quiet_id in before[0]

        # Every derived-row write, by the canary it names. The snapshot alone
        # is not enough: a delete-then-reinsert of the same episode compares
        # equal on SQLite, which reuses the deleted rowid where Postgres
        # issues a fresh id — so on SQLite the snapshot passes whether or not
        # the quiet canary was re-derived. This sees the write itself.
        writes: list[tuple[str, object]] = []

        def _watch(name):
            original = getattr(repo, name)

            def _wrapper(*a, **k):
                writes.append((name, k.get("canary_id", a[0] if a else None)))
                return original(*a, **k)

            monkeypatch.setattr(repo, name, _wrapper)

        for method in (
            "replace_prod_canary_unavailability",
            "upsert_prod_canary_loss",
            "delete_prod_canary_loss",
        ):
            _watch(method)

        # Only the busy canary keeps reporting. The window now reaches back no
        # further than T0+15h, so the quiet canary's failures stay outside it.
        probes.extend(_influx_probe(busy_id, h) for h in range(22, 40))
        record_prod_canary_results(
            repo, deployment, _Args(), _console(), "http://i", "t", "ant", "b"
        )
        assert {canary_id for _, canary_id in writes} == {busy_id}
        assert _snapshot() == before


class TestProbeFluxText:
    """The window has to reach the Flux, not just the fetcher's signature."""

    def test_the_start_lands_in_the_range_call_with_its_stop(self, monkeypatch):
        from saorsa_deploy import influx_canary
        from saorsa_deploy.influx import FLUX_FAR_FUTURE

        seen: list[str] = []
        monkeypatch.setattr(
            influx_canary,
            "_query",
            lambda url, token, org, flux, timeout=60: (seen.append(flux), iter(()))[1],
        )
        influx_canary.fetch_canary_probe_points(
            url="http://x",
            token="t",
            org="ant",
            bucket="b",
            deployment_name="PROD-CANARY-TEST",
            start="2026-10-02T17:00:00Z",
        )
        assert f"range(start: 2026-10-02T17:00:00Z, stop: {FLUX_FAR_FUTURE})" in seen[0]

    def test_a_full_pull_still_starts_at_zero(self, monkeypatch):
        from saorsa_deploy import influx_canary
        from saorsa_deploy.influx import FLUX_FAR_FUTURE

        seen: list[str] = []
        monkeypatch.setattr(
            influx_canary,
            "_query",
            lambda url, token, org, flux, timeout=60: (seen.append(flux), iter(()))[1],
        )
        influx_canary.fetch_canary_probe_points(
            url="http://x", token="t", org="ant", bucket="b", deployment_name="D"
        )
        assert f"range(start: 0, stop: {FLUX_FAR_FUTURE})" in seen[0]
