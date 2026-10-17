"""The daily anchor's path from the env file to the service loop.

Companion to ``test_daily_anchor.py``, which covers the arithmetic. Here:

* what the two provisioners write into their env files — ``DAILY_AT`` in,
  ``SLEEP_INTERVAL_SECS`` out — and that the **systemd units are byte-identical
  to what ``main`` @ ``152a786`` rendered**, snapshotted below. The units carry
  the restart policies the WASM crash accounting and both in-place upgrade
  commands rely on, so the anchor had to land without touching them;
* what each service loop actually does on a fresh start, after a cycle, on a
  restart, under ``ONE_SHOT`` and when the pool runs dry.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts"

# Rendered from `main` @ 152a786 — the commit this change is based on. Any diff
# here is a changed unit on a permanent production deployment.
BASELINE_DOWNLOADER_UNIT = """[Unit]
Description=Ant Production Continuous Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-prod-downloader.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u /root/ant-prod-downloader/prod_downloader.py
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
"""

BASELINE_ONE_SHOT_DOWNLOADER_UNIT = """[Unit]
Description=Ant Production One-Shot Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-prod-downloader.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u /root/ant-prod-downloader/prod_downloader.py
StandardOutput=journal
StandardError=journal
Restart=no

[Install]
WantedBy=multi-user.target
"""

BASELINE_UPLOADER_UNIT = """[Unit]
Description=Ant Production Daily Write Test (uploader)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/ant-prod-continuous-uploader.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/ant-registry-client/bin/python -u /opt/x/prod_continuous_uploader.py
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def downloader_script():
    return _load("prod_downloader")


@pytest.fixture(scope="module")
def uploader_script():
    return _load("prod_continuous_uploader")


def _downloader_env_script(**kwargs) -> str:
    from saorsa_deploy.provisioning.prod_downloader import ProdDownloaderProvisioner

    captured = {}

    def add_op(state, op, name=None, commands=None, **_):
        captured["script"] = commands[0]

    import saorsa_deploy.provisioning.prod_downloader as module

    original = module.add_op
    module.add_op = add_op
    try:
        provisioner = ProdDownloaderProvisioner(
            downloaders=[{"ip": "1.2.3.4", "region": "lon1", "addresses_file_content": "abc\n"}],
            bootstrap_peers=["1.1.1.1:10000"],
            **kwargs,
        )
        provisioner._add_env_and_addresses_op(None)
    finally:
        module.add_op = original
    return captured["script"]


class TestTheDownloaderEnvFile:
    def test_the_anchor_is_written_and_the_interval_is_gone(self):
        script = _downloader_env_script(daily_at="02:00")
        assert "DAILY_AT=02:00" in script
        assert "SLEEP_INTERVAL_SECS" not in script

    def test_the_state_file_lives_in_the_data_dir_the_env_op_creates(self):
        # The data dir is `mkdir -p`'d by this same script, so the state file's
        # parent exists before the service first writes it.
        script = _downloader_env_script(daily_at="00:00")
        assert "LAST_CYCLE_FILE=/var/lib/ant-prod-downloader/last-cycle" in script
        assert "mkdir -p /var/lib/ant-prod-downloader" in script

    def test_a_one_shot_host_still_renders_an_anchor_it_never_uses(self):
        # `prod downloaders` shares this provisioner and defines no flag; the
        # service exits after one pass without consulting the anchor.
        script = _downloader_env_script(one_shot=True)
        assert "DAILY_AT=00:00" in script
        assert "ONE_SHOT=true" in script


class TestTheUnitsAreUnchanged:
    """A changed unit would be a behaviour change on a permanent deployment."""

    def test_the_continuous_downloader_unit_matches_the_baseline(self):
        from saorsa_deploy.provisioning.prod_downloader import _build_systemd_unit

        assert _build_systemd_unit(one_shot=False) == BASELINE_DOWNLOADER_UNIT

    def test_the_one_shot_downloader_unit_matches_the_baseline(self):
        from saorsa_deploy.provisioning.prod_downloader import _build_systemd_unit

        assert _build_systemd_unit(one_shot=True) == BASELINE_ONE_SHOT_DOWNLOADER_UNIT

    def test_the_uploader_unit_matches_the_baseline(self):
        from saorsa_deploy.provisioning.prod_continuous_uploader import build_systemd_unit

        unit = build_systemd_unit(
            description="Ant Production Daily Write Test (uploader)",
            env_path="/etc/ant-prod-continuous-uploader.env",
            script_path="/opt/x/prod_continuous_uploader.py",
            restart="Restart=on-failure\nRestartSec=30",
        )
        assert unit == BASELINE_UPLOADER_UNIT


class TestTheUploaderEnvFile:
    def _env(self, **kwargs):
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            ProdContinuousUploaderProvisioner,
        )

        provisioner = ProdContinuousUploaderProvisioner(
            hosts=[{"ip": "1.2.3.4", "region": "fsn1"}],
            pool_request={"tiny": 1},
            wallet_secret_key="abc123",
            deployment_id=7,
            deployment_name="PROD-UL-01",
            pool_database_url="postgresql://scoped:pw@host/db",
            bootstrap_peers=["/ip4/1.1.1.1/udp/1/quic-v1"],
            influx_url="http://5.6.7.8:8086",
            influx_token="tok",
            **kwargs,
        )
        return provisioner._per_host_env(provisioner.hosts[0])

    def test_the_anchor_is_written_and_the_interval_is_gone(self):
        env = self._env(daily_at="02:00")
        assert env["DAILY_AT"] == "02:00"
        assert "SLEEP_INTERVAL_SECS" not in env

    def test_the_state_file_lives_beside_the_work_dir(self):
        env = self._env()
        assert env["LAST_CYCLE_FILE"] == "/var/lib/ant-prod-continuous-uploader/last-cycle"

    def test_the_verifier_gets_no_anchor(self):
        """The verifiers keep their own epoch-aligned interval, untouched."""
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            ProdUploadVerifierProvisioner,
        )

        provisioner = ProdUploadVerifierProvisioner(
            hosts=[{"ip": "9.9.9.9", "region": "lon1", "provider": "digitalocean"}],
            deployment_id=7,
            deployment_name="PROD-UL-01",
            pool_database_url="postgresql://scoped:pw@host/db",
            bootstrap_peers=["/ip4/1.1.1.1/udp/1/quic-v1"],
            influx_url="http://5.6.7.8:8086",
            influx_token="tok",
        )
        env = provisioner._per_host_env(provisioner.hosts[0])
        assert "DAILY_AT" not in env
        assert env["VERIFY_INTERVAL_SECS"] == 3600


class _StopCycle(Exception):
    """Raised from a patched sleep to end an otherwise-endless loop."""


class TestTheDownloaderLoop:
    """What the service does on start, after a cycle, on a restart and one-shot."""

    def _run(self, script, monkeypatch, tmp_path, *, one_shot=False, last_cycle=None, now=None):
        state = tmp_path / "last-cycle"
        if last_cycle is not None:
            state.write_text(last_cycle.isoformat())
        addrs = tmp_path / "addrs.txt"
        addrs.write_text("a" * 64 + "\n")

        cycles = []
        monkeypatch.setattr(script, "LOG_DIR", str(tmp_path / "logs"))

        def fake_transfer(*, cmd, stderr_log_path, stdout_log_path, **_):
            cycles.append(cmd)
            Path(cmd[cmd.index("-o") + 1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")
            return 0, "Time: 1.0\n", ""

        monkeypatch.setattr(script, "_run_transfer", fake_transfer)
        monkeypatch.setattr(script, "_post_influx", lambda *a, **k: None)
        if now is not None:
            monkeypatch.setattr(script, "datetime", _FrozenDatetime(now))

        def fake_sleep(_secs):
            raise _StopCycle

        monkeypatch.setattr(script.time, "sleep", fake_sleep)
        for key, value in {
            "REGION": "lon1",
            "ADDRESSES_FILE": str(addrs),
            "DOWNLOAD_DIR": str(tmp_path / "dl"),
            "LAST_CYCLE_FILE": str(state),
            "DAILY_AT": "00:00",
            "ONE_SHOT": "true" if one_shot else "false",
            "BOOTSTRAP_CACHE": "true",
        }.items():
            monkeypatch.setenv(key, value)
        try:
            rc = script.main()
        except _StopCycle:
            rc = None
        return rc, cycles, state

    def test_a_fresh_start_runs_a_cycle_before_sleeping(
        self, downloader_script, monkeypatch, tmp_path
    ):
        # No state file: proof of life on the day the operator deployed it.
        rc, cycles, state = self._run(downloader_script, monkeypatch, tmp_path)
        assert rc is None and len(cycles) == 1
        assert state.exists(), "the completed cycle must be recorded"

    def test_a_restart_after_the_days_cycle_runs_nothing(
        self, downloader_script, monkeypatch, tmp_path
    ):
        # The whole point: an upgrade or a crash-restart after today's cycle
        # waits for tomorrow's anchor instead of re-anchoring the series.
        _, cycles, _ = self._run(
            downloader_script,
            monkeypatch,
            tmp_path,
            now=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
            last_cycle=datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc),
        )
        assert cycles == []

    def test_a_restart_before_the_days_cycle_runs_one(
        self, downloader_script, monkeypatch, tmp_path
    ):
        _, cycles, _ = self._run(
            downloader_script,
            monkeypatch,
            tmp_path,
            now=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
            last_cycle=datetime(2026, 10, 16, 23, 50, tzinfo=timezone.utc),
        )
        assert len(cycles) == 1

    def test_one_shot_exits_after_one_cycle_and_writes_no_state(
        self, downloader_script, monkeypatch, tmp_path
    ):
        rc, cycles, state = self._run(downloader_script, monkeypatch, tmp_path, one_shot=True)
        assert rc == 0 and len(cycles) == 1
        assert not state.exists()

    def test_one_shot_ignores_a_state_file_from_a_previous_run(
        self, downloader_script, monkeypatch, tmp_path
    ):
        rc, cycles, _ = self._run(
            downloader_script,
            monkeypatch,
            tmp_path,
            one_shot=True,
            now=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
            last_cycle=datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc),
        )
        assert rc == 0 and len(cycles) == 1


class _FrozenDatetime:
    """Stands in for the ``datetime`` class so ``now()`` is deterministic."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now if tz is None else self._now.astimezone(tz)

    def fromisoformat(self, value):
        return datetime.fromisoformat(value)

    def strftime(self, *args):  # pragma: no cover - only the uploader needs it
        return self._now.strftime(*args)


class TestTheUploaderLoop:
    """The uploader's two writes of the state file: a cycle, and a dry pool."""

    def _run(self, script, monkeypatch, tmp_path, *, claim_error, last_cycle=None, now=None):
        state = tmp_path / "last-cycle"
        if last_cycle is not None:
            state.write_text(last_cycle.isoformat())

        claims = []

        class _Pool:
            def claim_pool_files(self, **kwargs):
                claims.append(kwargs)
                if claim_error:
                    raise RuntimeError("InsufficientPoolInventory: no tiny files available")
                return []

        class _Session:
            def close(self):
                pass

        monkeypatch.setattr(
            script, "_open_repositories", lambda url: (_Pool(), object(), _Session())
        )
        monkeypatch.setattr(script, "_post_influx", lambda *a, **k: None)
        monkeypatch.setattr(script, "LOG_DIR", str(tmp_path / "logs"))
        if now is not None:
            monkeypatch.setattr(script, "datetime", _FrozenDatetime(now))
        monkeypatch.setattr(script.time, "sleep", lambda _secs: (_ for _ in ()).throw(_StopCycle))
        for key, value in {
            "DEPLOYMENT_ID": "7",
            "DEPLOYMENT_NAME": "PROD-UL-01",
            "POOL_DATABASE_URL": "postgresql://scoped@host/db",
            "POOL_REQUEST": '{"tiny": 1}',
            "REGION": "lon1",
            "SECRET_KEY": "0x" + "11" * 32,
            "WORK_DIR": str(tmp_path / "files"),
            "LAST_CYCLE_FILE": str(state),
            "DAILY_AT": "00:00",
        }.items():
            monkeypatch.setenv(key, value)
        try:
            script.main()
        except _StopCycle:
            pass
        return claims, state

    def test_a_dry_pool_counts_as_the_days_cycle(self, uploader_script, monkeypatch, tmp_path):
        # The shortfall is its own measurement and its own cycle: retrying
        # before the next anchor would churn claims against a pool only an
        # operator topping it up can heal.
        claims, state = self._run(uploader_script, monkeypatch, tmp_path, claim_error=True)
        assert len(claims) == 1
        assert state.exists()

    def test_a_restart_after_the_days_cycle_claims_nothing(
        self, uploader_script, monkeypatch, tmp_path
    ):
        # Nothing may be claimed on a restart that follows the day's cycle —
        # the files would be spent on an upload the series never asked for.
        claims, _ = self._run(
            uploader_script,
            monkeypatch,
            tmp_path,
            claim_error=False,
            now=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
            last_cycle=datetime(2026, 10, 17, 3, 22, tzinfo=timezone.utc),
        )
        assert claims == []
