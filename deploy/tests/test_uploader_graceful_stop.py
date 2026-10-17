"""Stopping the daily write test without stranding its pool claims (V2-1613).

The uploader claims its files at the start of a cycle and resolves each claim
inline *after* the upload returns. With no signal handler a `systemctl stop` or
`restart` killed Python on the spot and every claim of the cycle in flight stayed
`claimed` forever — nothing sweeps those, by design, so each one cost an operator
an on-chain audit of the paying wallet to settle (15 of them by 2026-10-17).

Every test here delivers the signal the way the process receives it, by calling
the handler the service installed, and then asserts on what reached the fake
repositories. Nothing touches the network, S3, Postgres or SSH.
"""

import importlib.util
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts"


def _load(name: str):
    """Runtime scripts are deployed as standalone files, not importable modules."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def service():
    return _load("prod_continuous_uploader")


@pytest.fixture(autouse=True)
def _clean_stop_state(service):
    """Module-level stop state, and this process's own handlers, per test.

    `main()` installs handlers on the real process, so they are put back: a
    SIGINT left pointing at the service would break the rest of the session.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    service.STOP.update({"requested": False, "signal": None})
    service._CHILD["proc"] = None
    service._IN_FLIGHT.update(
        {
            "in_cycle": False,
            "claim": None,
            "stage": None,
            "file_name": None,
            "local_path": None,
            "resolved": False,
            "outcome": None,
        }
    )
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _deliver(sig=signal.SIGTERM):
    """Call the handler the service installed, as the kernel would."""
    signal.getsignal(sig)(sig, None)


class _FakeChild:
    """Stands in for the transfer `Popen` while the signal lands."""

    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated += 1

    def kill(self):  # pragma: no cover - the stub never waits
        self.killed += 1


class _FakePool:
    """Records every claim resolution, in order."""

    def __init__(self, claims, files, release_raises_for=()):
        self._claims = claims
        self._files = files
        self._release_raises_for = set(release_raises_for)
        self.calls = []
        self.claim_requests = []
        self.on_get_pool_file = None

    def claim_pool_files(self, **kwargs):
        self.claim_requests.append(kwargs)
        return list(self._claims)

    def get_pool_file(self, file_id):
        if self.on_get_pool_file is not None:
            self.on_get_pool_file(file_id)
        return self._files[file_id]

    def release_pool_claim(self, claim_id, note=None):
        if claim_id in self._release_raises_for:
            raise RuntimeError("connection reset by peer")
        self.calls.append(("released", claim_id, note))

    def mark_pool_claim_partial(self, claim_id, note=None, **_):
        self.calls.append(("partial", claim_id, note))

    def resolve_pool_claim(self, claim_id, note=None, **_):
        self.calls.append(("uploaded", claim_id, note))

    def outcome(self, claim_id):
        for status, recorded_id, _note in self.calls:
            if recorded_id == claim_id:
                return status
        return None

    def note(self, claim_id):
        for _status, recorded_id, note in self.calls:
            if recorded_id == claim_id:
                return note or ""
        return ""


class _FakeDeployments:
    def __init__(self):
        self.rows = []
        self.verifications = []

    def upsert_prod_upload_result(self, **kwargs):
        self.rows.append(SimpleNamespace(**kwargs))

    def upsert_prod_upload_verification(self, **kwargs):  # pragma: no cover - read-back off
        self.verifications.append(SimpleNamespace(**kwargs))


class _FakeSession:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _LoopEnded(Exception):
    """Raised from the patched anchor sleep: the service loop is endless."""


class _Harness:
    """One cycle of three claims, with the signal delivered where asked."""

    def __init__(self, service, tmp_path, monkeypatch):
        self.service = service
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.claims = [
            SimpleNamespace(id=index, file_id=index, locator_used=f"https://x/a{index}.bin")
            for index in (1, 2, 3)
        ]
        self.files = {
            index: SimpleNamespace(
                name=f"a{index}.bin", locator=f"https://x/a{index}.bin", size_bytes=1234
            )
            for index in (1, 2, 3)
        }
        self.pool = _FakePool(self.claims, self.files)
        self.deployments = _FakeDeployments()
        self.session = _FakeSession()
        self.spawned = []
        self.posted = []
        self.state_file = tmp_path / "last-cycle"
        self.run_ant = None
        self.on_download = None

    def _download(self, url, destination, timeout, **_):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"x" * 1234)
        if self.on_download is not None:
            self.on_download(url)
        return 1234, "a" * 64

    def run(self, *, wasm=False, readback=False, env=None, sleep=None):
        service, monkeypatch = self.service, self.monkeypatch
        # The loop is `while True`, so a run that is never stopped has to be
        # ended by the wait for the next anchor.
        monkeypatch.setattr(
            service.time,
            "sleep",
            sleep or (lambda _secs: (_ for _ in ()).throw(_LoopEnded())),
        )
        monkeypatch.setattr(
            service, "_open_repositories", lambda _url: (self.pool, self.deployments, self.session)
        )
        monkeypatch.setattr(
            service,
            "_post_influx",
            lambda url, token, org, bucket, line: self.posted.append(line),
        )
        monkeypatch.setattr(service, "_download", self._download)
        monkeypatch.setattr(service, "LOG_DIR", str(self.tmp_path / "logs"))

        def _run_ant(cmd, secret_key, log_base, timeout):
            self.spawned.append(cmd)
            return self.run_ant(cmd, log_base)

        monkeypatch.setattr(service, "_run_ant", _run_ant)
        values = {
            "DEPLOYMENT_ID": "7",
            "DEPLOYMENT_NAME": "VERIFY-UL",
            "POOL_DATABASE_URL": "postgresql://scoped@host/db",
            "POOL_REQUEST": '{"tiny": 3}',
            "REGION": "lon1",
            "SECRET_KEY": "0x" + "11" * 32,
            "WORK_DIR": str(self.tmp_path / "files"),
            "LAST_CYCLE_FILE": str(self.state_file),
            "DAILY_AT": "00:00",
            "READBACK_VERIFY": "true" if readback else "false",
            "INFLUX_URL": "http://influx:8086",
            "INFLUX_TOKEN": "tok",
        }
        if wasm:
            values["CLIENT_KIND"] = "wasm"
        values.update(env or {})
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        try:
            return service.main()
        except _LoopEnded:
            return None

    @property
    def cycle_point(self):
        for line in self.posted:
            if line.startswith("prod_upload_cycle,"):
                return line
        return ""


@pytest.fixture
def harness(service, tmp_path, monkeypatch):
    return _Harness(service, tmp_path, monkeypatch)


def _killed_during_upload(harness, *, write_marker=False, stdout="", rc=-15):
    """A transfer that is signalled while its child is still running."""

    def _run(cmd, log_base):
        child = _FakeChild()
        harness.service._CHILD["proc"] = child
        if write_marker:
            Path(cmd[cmd.index("--payment-marker") + 1]).write_text("paid\n")
        _deliver()
        harness.service._CHILD["proc"] = None
        assert child.terminated == 1, "the handler must ask the child to stop"
        return rc, stdout, ""

    return _run


class TestAStopDuringANativeUpload:
    """The native path has no payment marker, so a kill means "payment unknown"."""

    @pytest.fixture
    def run(self, harness, capsys):
        harness.run_ant = _killed_during_upload(harness)
        rc = harness.run()
        return rc, harness, capsys.readouterr().out

    def test_the_in_flight_claim_is_held_as_partial(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(1) == "partial"
        assert "killed by signal 15" in harness.pool.note(1)
        assert "payment unknown" in harness.pool.note(1)

    def test_the_claims_the_cycle_never_started_go_back(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(2) == "released"
        assert harness.pool.outcome(3) == "released"
        assert "interrupted before upload (SIGTERM)" in harness.pool.note(2)

    def test_only_the_interrupted_upload_gets_a_result_row(self, run):
        from saorsa_db.models import ProdUploadStatus

        _rc, harness, _journal = run
        assert len(harness.deployments.rows) == 1
        assert harness.deployments.rows[0].file_name == "a1.bin"
        assert harness.deployments.rows[0].status is ProdUploadStatus.PARTIAL_UPLOAD

    def test_the_day_is_not_recorded_as_done_so_the_restart_runs_one(self, run):
        # The restarted service has to run the cycle this one did not finish,
        # which is exactly what an unwritten state file means to the anchor.
        _rc, harness, _journal = run
        assert not harness.state_file.exists()

    def test_the_cycle_heartbeat_says_it_was_interrupted(self, run):
        _rc, harness, _journal = run
        assert "interrupted=true" in harness.cycle_point

    def test_it_exits_zero_because_sigterm_is_a_clean_stop(self, run):
        rc, _harness, _journal = run
        assert rc == 0

    def test_the_journal_carries_the_lines_the_runbooks_grep_for(self, run):
        _rc, _harness, journal = run
        assert "stop requested (SIGTERM) during upload of a1.bin" in journal
        assert "upload PARTIAL_UPLOAD for a1.bin: ant killed by signal 15; payment unknown" in (
            journal
        )
        assert "released claim 2 (a2.bin): interrupted before upload" in journal
        assert "released claim 3 (a3.bin): interrupted before upload" in journal
        assert "cycle 1 interrupted by SIGTERM:" in journal
        assert "stopping (exit 0)" in journal

    def test_the_session_is_closed(self, run):
        _rc, harness, _journal = run
        assert harness.session.closed >= 1


class TestAStopDuringAWasmUpload:
    """The browser path decides by the evidence it has, kill or no kill."""

    def test_a_payment_marker_holds_the_claim_as_partial(self, harness):
        harness.run_ant = _killed_during_upload(harness, write_marker=True)
        assert harness.run(wasm=True) == 0
        assert harness.pool.outcome(1) == "partial"
        assert harness.pool.outcome(2) == "released"
        assert harness.pool.outcome(3) == "released"

    def test_no_marker_and_no_document_releases_the_claim(self, harness):
        # Nothing reached the network, so the file is as unused as before.
        harness.run_ant = _killed_during_upload(harness)
        assert harness.run(wasm=True) == 0
        assert harness.pool.outcome(1) == "released"
        assert harness.pool.outcome(2) == "released"
        assert harness.pool.outcome(3) == "released"


class TestAStopWhoseChildFinishedFirst:
    """The upload completed in the instant before the signal landed."""

    @pytest.fixture
    def run(self, harness, capsys):
        harness.run_ant = _killed_during_upload(
            harness,
            rc=0,
            stdout='{"address": "abc", "chunks_stored": 4, "total_chunks": 4}',
        )
        rc = harness.run(readback=True)
        return rc, harness, capsys.readouterr().out

    def test_the_upload_is_recorded_and_the_claim_spent(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(1) == "uploaded"
        assert harness.pool.outcome(2) == "released"
        assert harness.pool.outcome(3) == "released"

    def test_the_read_back_is_skipped_rather_than_recorded_as_a_failure(self, run):
        # Started into a shutdown it would be killed and written down as a
        # download failure production had no part in; the independent verifier
        # VMs check every address anyway.
        _rc, harness, journal = run
        assert harness.deployments.verifications == []
        assert len(harness.spawned) == 1
        assert "skipping the read-back of a1.bin: stopping" in journal


class TestAStopDuringTheSourceFetch:
    """Nothing was attempted against the network, so nothing is recorded."""

    @pytest.fixture
    def run(self, harness, capsys):
        harness.on_download = lambda _url: _deliver()
        harness.run_ant = lambda cmd, log_base: pytest.fail("no upload may be spawned")
        rc = harness.run()
        return rc, harness, capsys.readouterr().out

    def test_every_claim_goes_back(self, run):
        _rc, harness, _journal = run
        assert [harness.pool.outcome(i) for i in (1, 2, 3)] == ["released"] * 3

    def test_no_result_row_is_written(self, run):
        _rc, harness, _journal = run
        assert harness.deployments.rows == []

    def test_it_exits_zero(self, run):
        rc, _harness, journal = run
        assert rc == 0
        assert "stop requested (SIGTERM) during fetch of a1.bin" in journal


class TestAStopBetweenOps:
    """A finished op keeps its outcome; the rest of the batch goes back."""

    @pytest.fixture
    def run(self, harness, capsys):
        fetches = []

        def _on_download(url):
            fetches.append(url)
            if len(fetches) == 2:
                _deliver()

        harness.on_download = _on_download
        harness.run_ant = lambda cmd, log_base: (
            0,
            '{"address": "abc", "chunks_stored": 4, "total_chunks": 4}',
            "",
        )
        rc = harness.run()
        return rc, harness, capsys.readouterr().out

    def test_the_completed_upload_keeps_its_claim_spent(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(1) == "uploaded"

    def test_the_untouched_claims_are_released(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(2) == "released"
        assert harness.pool.outcome(3) == "released"

    def test_the_cycle_summary_names_the_in_flight_outcome(self, run):
        _rc, harness, journal = run
        assert "in-flight a2.bin -> released, 1 queued claim(s) released" in journal


class TestAStopWhileIdle:
    """Waiting for the next anchor: no claim is held, so nothing is touched."""

    @pytest.fixture
    def run(self, harness, monkeypatch, capsys):
        from datetime import datetime, timezone

        # A cycle completed since the anchor, so the service is in the sleep.
        harness.state_file.write_text(datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc).isoformat())
        monkeypatch.setattr(
            harness.service,
            "datetime",
            _FrozenDatetime(datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)),
        )
        harness.run_ant = lambda cmd, log_base: pytest.fail("nothing may be uploaded")
        rc = harness.run(sleep=lambda _secs: _deliver())
        return rc, harness, capsys.readouterr().out

    def test_it_exits_zero_without_claiming_anything(self, run):
        rc, harness, _journal = run
        assert rc == 0
        assert harness.pool.claim_requests == []
        assert harness.pool.calls == []

    def test_the_line_says_it_was_idle(self, run):
        _rc, _harness, journal = run
        assert "stop requested (SIGTERM) while idle" in journal
        assert "stopping (exit 0)" in journal


class _FrozenDatetime:
    """Stands in for the ``datetime`` class so ``now()`` is deterministic."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now if tz is None else self._now.astimezone(tz)

    def fromisoformat(self, value):
        from datetime import datetime

        return datetime.fromisoformat(value)


class TestARealChild:
    """The wait is sliced, so a signal actually ends it."""

    def test_the_child_is_signalled_and_the_wait_returns(self, service, tmp_path):
        service._install_stop_handler()
        timer = threading.Timer(0.5, os.kill, [os.getpid(), signal.SIGTERM])
        timer.start()
        try:
            rc, _stdout, _stderr = service._run_ant(["sleep", "60"], "", tmp_path / "op", 300)
        finally:
            timer.cancel()
        assert rc == -15
        assert service.STOP["requested"]
        assert service._CHILD["proc"] is None


class TestTheCleanupSurvivesADatabaseFailure:
    """One unreachable row must not strand every other claim of the cycle."""

    @pytest.fixture
    def run(self, harness, capsys):
        harness.pool._release_raises_for = {2}
        harness.run_ant = _killed_during_upload(harness)
        rc = harness.run()
        return rc, harness, capsys.readouterr().out

    def test_the_claims_after_the_failure_are_still_released(self, run):
        _rc, harness, _journal = run
        assert harness.pool.outcome(1) == "partial"
        assert harness.pool.outcome(2) is None
        assert harness.pool.outcome(3) == "released"

    def test_the_failure_is_reported_and_the_exit_is_still_clean(self, run):
        rc, _harness, journal = run
        assert rc == 0
        assert "WARNING: could not release claim 2 on stop: connection reset by peer" in journal


class TestASecondSignal:
    """A stop that overlaps the cleanup must not abandon it."""

    def test_the_handler_returns_instead_of_raising(self, service, capsys):
        # The first signal raises, which is how the cycle reaches its cleanup.
        # Every later one must only say so: raising during the cleanup could
        # abandon a release half-done.
        service._install_stop_handler()
        with pytest.raises(service.StopRequested):
            _deliver()
        _deliver()
        _deliver(signal.SIGINT)
        assert capsys.readouterr().out.count("stop already in progress") == 2

    def test_the_first_signal_wins_the_record(self, service):
        service._install_stop_handler()
        with pytest.raises(service.StopRequested):
            _deliver()
        _deliver(signal.SIGINT)
        assert service.STOP["signal"] == "SIGTERM"


class TestTheKilledNativeOutcomeOnItsOwn:
    """B in isolation: the rule does not depend on a signal having been sent."""

    @pytest.mark.parametrize("rc", [-9, -15])
    def test_a_killed_native_child_with_no_document_is_partial(self, harness, rc):
        harness.run_ant = lambda cmd, log_base: (rc, "", "")
        harness.run()
        assert harness.pool.outcome(1) == "partial"
        assert f"killed by signal {-rc}" in harness.pool.note(1)
        assert "payment unknown" in harness.pool.note(1)

    def test_a_native_child_that_exited_on_its_own_still_releases(self, harness):
        # `rc > 0` with no document is a bad binary or bad arguments: nothing
        # reached the network, so the file is as unused as before.
        harness.run_ant = lambda cmd, log_base: (1, "", "")
        harness.run()
        assert harness.pool.outcome(1) == "released"
        assert "no result document" in harness.pool.note(1)

    def test_a_killed_wasm_child_is_still_decided_by_the_marker(self, harness):
        harness.run_ant = lambda cmd, log_base: (-9, "", "")
        harness.run(wasm=True)
        assert harness.pool.outcome(1) == "released"

    def test_a_killed_wasm_child_that_paid_is_partial(self, harness):
        def _run(cmd, log_base):
            Path(cmd[cmd.index("--payment-marker") + 1]).write_text("paid\n")
            return -9, "", ""

        harness.run_ant = _run
        harness.run(wasm=True)
        assert harness.pool.outcome(1) == "partial"


class TestTheStageDescription:
    """The one line that says what the stop interrupted."""

    def test_an_op_in_flight_names_its_stage_and_file(self, service):
        service._IN_FLIGHT.update({"in_cycle": True, "stage": "upload", "file_name": "a.bin"})
        assert service._stop_stage_description() == "during upload of a.bin"

    def test_a_cycle_with_no_op_in_flight_is_between_ops(self, service):
        service._IN_FLIGHT.update({"in_cycle": True, "stage": None, "file_name": "a.bin"})
        assert service._stop_stage_description() == "between ops"

    def test_no_cycle_at_all_is_idle(self, service):
        service._IN_FLIGHT.update({"in_cycle": False, "stage": None})
        assert service._stop_stage_description() == "while idle"
