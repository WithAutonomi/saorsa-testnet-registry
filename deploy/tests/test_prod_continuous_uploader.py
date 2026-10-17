"""Tests for the daily production write test (V2-837).

Layered the same way as `test_prod_downloaders_provider_and_diagnostics.py`,
which is the template for proving a setting survives every hop: the manifest
schema, the argv the orchestrator emits, that the real CLI parses that argv,
what the provisioner writes into the env file, and what the runtime scripts do
with it.
"""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps

RESOURCES = Path(__file__).resolve().parents[1] / "saorsa_deploy" / "resources" / "scripts"


def _load_script(name: str):
    """Runtime scripts are deployed as standalone files, not importable modules."""
    spec = importlib.util.spec_from_file_location(name, RESOURCES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def uploader_script():
    return _load_script("prod_continuous_uploader")


@pytest.fixture(scope="module")
def verifier_script():
    return _load_script("prod_upload_verifier")


def _manifest(**overrides):
    spec = {
        "spec": "fsn1:cx22",
        "provider": "hetzner",
        "volume_size": 40,
        "pool": {"tiny": 2, "small": 1},
        "daily_at": "00:00",
        "verify_interval_secs": 3600,
        "binary": {"version": "0.3.3"},
        "verifiers": [
            {
                "region": "lon1",
                "vm_size": "s-1vcpu-2gb",
                "provider": "digitalocean",
                "volume_size": 40,
            },
            {"region": "GRA9", "vm_size": "b3-8", "provider": "ovh", "volume_size": 40},
        ],
    }
    spec.update(overrides)
    return Manifest.model_validate(
        {
            "name": "PROD-UL-DAILY-01",
            "kind": "prod-continuous-uploader",
            "purpose": "daily production write test",
            "ssh_key_path": "~/.ssh/ant_deploy",
            "prod_continuous_uploader": spec,
        }
    )


def _manifest_without_daily_at():
    """The uploader spec with `daily_at` left out, to read its default."""
    spec = dict(_manifest().spec.model_dump(exclude_none=True))
    spec.pop("daily_at")
    return Manifest.model_validate(
        {
            "name": "PROD-UL-DAILY-01",
            "kind": "prod-continuous-uploader",
            "purpose": "daily production write test",
            "ssh_key_path": "~/.ssh/ant_deploy",
            "prod_continuous_uploader": spec,
        }
    ).spec


def _deploy_argv(manifest):
    return build_deploy_steps(manifest)[0].argv


def _val(argv, flag):
    return argv[argv.index(flag) + 1]


class TestTheSchema:
    def test_a_full_manifest_validates(self):
        manifest = _manifest()
        assert manifest.spec.pool == {"tiny": 2, "small": 1}
        assert len(manifest.spec.verifiers) == 2
        assert manifest.spec.verifiers[1].provider == "ovh"

    def test_the_pool_request_is_not_fixed_to_two_tiny_one_small(self):
        """The whole point of taking a request: larger files later."""
        manifest = _manifest(pool={"medium": 1}, volume_size=40)
        assert manifest.spec.pool == {"medium": 1}

    def test_a_verifier_is_required(self):
        """Without one, the test only proves the upload command exited zero."""
        with pytest.raises(ValidationError, match="verifiers"):
            _manifest(verifiers=[])

    def test_an_empty_pool_request_is_rejected(self):
        with pytest.raises(ValidationError, match="at least one size class"):
            _manifest(pool={})

    def test_a_typo_is_a_loud_error(self):
        with pytest.raises(ValidationError):
            _manifest(daily_att="00:00")

    def test_the_retired_sleep_interval_is_refused_by_name(self):
        """Interval mode is gone, so a manifest still carrying it must not run.

        A drifting daily cadence is the defect this replaced; silently ignoring
        the old key would leave the operator reading their own manifest as a
        description of a run that no longer honours it.
        """
        with pytest.raises(ValidationError, match="sleep_interval_secs"):
            _manifest(sleep_interval_secs=86400)

    @pytest.mark.parametrize("value", ["00:00", "23:59", "02:00"])
    def test_daily_at_accepts_a_strict_hh_mm(self, value):
        assert _manifest(daily_at=value).spec.daily_at == value

    def test_daily_at_defaults_to_midnight(self):
        assert _manifest_without_daily_at().daily_at == "00:00"

    @pytest.mark.parametrize("value", ["24:00", "0:00", "00:00:00", "0000", "00:60"])
    def test_daily_at_rejects_anything_else(self, value):
        with pytest.raises(ValidationError, match="daily_at must be HH:MM"):
            _manifest(daily_at=value)


class TestTheDiskGuard:
    """A draw that cannot fit the disk must fail at validate time.

    Both roles write the file to local disk — the uploader before uploading,
    each verifier before hashing — so an oversized draw fills the filesystem
    and fails in a way that reads as a network fault.
    """

    def test_a_large_draw_onto_a_small_disk_is_rejected(self):
        with pytest.raises(ValidationError, match="too small"):
            _manifest(pool={"large": 1}, volume_size=12)

    def test_a_large_draw_onto_an_adequate_disk_is_accepted(self):
        assert _manifest(pool={"large": 1}, volume_size=40).spec.volume_size == 40

    def test_a_verifier_disk_is_checked_too(self):
        with pytest.raises(ValidationError, match=r"verifiers\[0\]"):
            _manifest(
                pool={"medium": 1},
                volume_size=40,
                verifiers=[{"region": "lon1", "volume_size": 5}],
            )

    def test_an_unbounded_class_without_a_volume_size_is_refused(self):
        """`xlarge` has no upper bound, so the disk needed is unknowable."""
        with pytest.raises(ValidationError, match="no upper size bound"):
            _manifest(pool={"xlarge": 1}, volume_size=None)

    def test_pool_max_size_makes_an_unbounded_class_knowable(self):
        manifest = _manifest(pool={"xlarge": 1}, pool_max_size="8GB", volume_size=60)
        assert manifest.spec.pool_max_size == "8GB"


class TestTheOrchestratorArgv:
    def test_the_pool_request_is_emitted_in_size_class_order(self):
        """Argv is compared byte-for-byte by the step journal.

        A manifest that happens to write `small` before `tiny` must not read as
        a changed plan on the next run.
        """
        argv = _deploy_argv(_manifest(pool={"small": 1, "tiny": 2}))
        assert _val(argv, "--pool") == "tiny:2,small:1"

    def test_each_verifier_becomes_its_own_flag(self):
        argv = _deploy_argv(_manifest())
        verifiers = [argv[i + 1] for i, a in enumerate(argv) if a == "--verifier"]
        assert verifiers == ["digitalocean:lon1:s-1vcpu-2gb", "ovh:GRA9:b3-8"]

    def test_the_anchor_is_always_emitted(self):
        """`plan` has to show the anchor, and the journal's argv has to pin it.

        Unconditional rather than omit-when-default: the anchor is the whole
        cadence, so a plan that does not name it reads as a run with none.
        """
        assert _val(_deploy_argv(_manifest()), "--daily-at") == "00:00"
        assert _val(_deploy_argv(_manifest(daily_at="02:00")), "--daily-at") == "02:00"
        assert "--sleep-interval-secs" not in _deploy_argv(_manifest())

    def test_unset_options_emit_no_flag(self):
        argv = _deploy_argv(_manifest())
        assert "--pool-min-size" not in argv
        assert "--pool-max-size" not in argv
        assert "--grafana" not in argv
        assert "--payment" not in argv

    def test_rust_log_is_last_because_its_value_can_contain_colons(self):
        argv = _deploy_argv(_manifest(rust_log="ant_networking::x=debug"))
        assert _val(argv, "--rust-log") == "ant_networking::x=debug"

    def test_an_inventory_step_is_appended_and_is_best_effort(self):
        """A snapshot failure must not fail a deploy that is already billing."""
        steps = build_deploy_steps(_manifest())
        assert steps[-1].argv[:2] == ["deployments", "generate-inventory"]
        assert steps[-1].best_effort is True

    def test_a_branch_binary_prepends_a_build_step(self):
        steps = build_deploy_steps(_manifest(binary={"branch": "feat-x", "repo_owner": "maidsafe"}))
        assert steps[0].argv[:2] == ["build", "ant"]

    def test_destroy_argv(self):
        argv = build_destroy_steps(_manifest())[0].argv
        assert argv[:3] == ["prod", "continuous-uploader", "destroy"]
        assert _val(argv, "--name") == "PROD-UL-DAILY-01"


class TestTheCliAcceptsWhatTheOrchestratorEmits:
    """Neither the orchestrator nor the command tests go through argparse.

    A flag defined on the wrong parser would pass both and still fail every
    real run, so the emitted argv is parsed by the real CLI here.
    """

    def _run(self, monkeypatch, argv):
        from saorsa_deploy.cmd import prod_continuous_uploader as module
        from saorsa_deploy.main import main

        captured = {}
        monkeypatch.setattr(
            module,
            "cmd_prod_continuous_uploader_deploy",
            lambda args: captured.update(vars(args)),
        )
        main(argv)
        return captured

    def test_a_full_manifests_argv_parses(self, monkeypatch):
        args = self._run(
            monkeypatch,
            _deploy_argv(
                _manifest(
                    pool_min_size="5MB",
                    payment="merkle",
                    log_level="debug",
                    grafana=True,
                )
            ),
        )
        assert args["pool"] == "tiny:2,small:1"
        assert args["verifier"] == [
            "digitalocean:lon1:s-1vcpu-2gb",
            "ovh:GRA9:b3-8",
        ]
        assert args["pool_min_size"] == "5MB"
        assert args["payment"] == "merkle"
        assert args["log_level"] == "debug"
        assert args["grafana"] is True
        assert args["provider"] == "hetzner"

    def test_defaults_are_sensible(self, monkeypatch):
        args = self._run(monkeypatch, _deploy_argv(_manifest()))
        assert args["daily_at"] == "00:00"
        assert args["verify_interval_secs"] == 3600
        assert args["readback_verify"] is True
        assert args["pool_database_url_env"] == "POOL_DATABASE_URL"


class TestTheRecordedCadenceAndRunway:
    """`deploy_args` is what every reader works from, and the runway is in days."""

    def test_the_cli_default_and_an_explicit_anchor_both_resolve(self):
        from saorsa_deploy.cmd.prod_continuous_uploader import _daily_at

        assert _daily_at(SimpleNamespace(daily_at="02:00")) == "02:00"
        # The attribute is absent on no real parse path, but a None would make
        # the env file write an empty DAILY_AT and override the script default.
        assert _daily_at(SimpleNamespace(daily_at=None)) == "00:00"
        assert _daily_at(SimpleNamespace()) == "00:00"

    def test_deploy_args_carry_the_anchor_and_not_the_retired_interval(self, monkeypatch):
        """Every reader of the cadence — slack-post, the inventory — uses this."""
        from saorsa_deploy.cmd import prod_continuous_uploader as module

        captured = {}
        monkeypatch.setattr(module, "_parse_pool_request", lambda raw, console: {"tiny": 1})
        monkeypatch.setattr(module, "_check_wasm_options", lambda *a: None)
        monkeypatch.setattr(module, "_apply_wasm_pool_cap", lambda *a: None)
        monkeypatch.setattr(module, "apply_wasm_vm_size", lambda *a: None)
        monkeypatch.setattr(module, "_resolve_pool_database_url", lambda *a: "postgresql://x/y")
        monkeypatch.setattr(module, "_preflight_pool", lambda *a: None)
        monkeypatch.setattr(module, "resolve_wasm_context", lambda *a: None)
        monkeypatch.setattr(module, "record_event", lambda *a, **k: None)
        monkeypatch.setattr(module, "record_status", lambda *a, **k: None)
        monkeypatch.setattr(module, "_ensure_monitoring", lambda *a: None)
        monkeypatch.setattr(module, "_ensure_client_infra", lambda *a: None)
        monkeypatch.setattr(module, "_provision", lambda *a: None)
        monkeypatch.setattr(module, "_start", lambda *a: None)
        monkeypatch.setattr(
            module,
            "_resolve_deployment_row",
            lambda name, deploy_args, console: captured.update(deploy_args) or 1,
        )
        module.cmd_prod_continuous_uploader_deploy(
            SimpleNamespace(
                name="PROD-UL-01",
                purpose="daily write test",
                spec="fsn1:cx23",
                secret_key="0x" + "11" * 32,
                daily_at="02:00",
                verifier=["lon1"],
                pool="tiny:1",
                branch_name=None,
                repo_owner=None,
                ant_version="0.3.3",
            )
        )
        assert captured["daily_at"] == "02:00"
        assert "sleep_interval_secs" not in captured

    def test_the_runway_counts_one_cycle_a_day(self, monkeypatch, capsys):
        """Cycles and days are the same number now the cadence is anchored."""
        from rich.console import Console

        from saorsa_deploy.cmd import prod_continuous_uploader as module

        class _Repo:
            def pool_stats(self):
                return {"url": {"by_class": {"tiny": {"available": 20}, "small": {"available": 7}}}}

        monkeypatch.setattr(module, "get_file_pool_repository", lambda: _Repo())
        module._preflight_pool(
            SimpleNamespace(), {"tiny": 2, "small": 1}, Console(width=200, no_color=True)
        )
        out = capsys.readouterr().out
        assert "7 cycles" in out and "~7 days at one cycle a day" in out


class TestTheProvisionerEnvironment:
    def _provisioners(self):
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            ProdContinuousUploaderProvisioner,
            ProdUploadVerifierProvisioner,
        )

        common = dict(
            deployment_id=7,
            deployment_name="PROD-UL-DAILY-01",
            pool_database_url="postgresql://scoped:pw@host/db",
            bootstrap_peers=["/ip4/1.1.1.1/udp/1/quic-v1"],
            influx_url="http://5.6.7.8:8086",
            influx_token="tok",
        )
        uploader = ProdContinuousUploaderProvisioner(
            hosts=[{"ip": "1.2.3.4", "region": "fsn1"}],
            pool_request={"tiny": 2, "small": 1},
            wallet_secret_key="abc123",
            **common,
        )
        verifier = ProdUploadVerifierProvisioner(
            hosts=[{"ip": "9.9.9.9", "region": "lon1", "provider": "digitalocean"}],
            **common,
        )
        return uploader, verifier

    def test_the_uploader_gets_the_pool_request_as_json(self):
        uploader, _ = self._provisioners()
        env = uploader._per_host_env(uploader.hosts[0])
        assert json.loads(env["POOL_REQUEST"]) == {"tiny": 2, "small": 1}

    def test_the_wallet_key_is_0x_prefixed(self):
        uploader, _ = self._provisioners()
        assert uploader._per_host_env(uploader.hosts[0])["SECRET_KEY"] == "0xabc123"

    def test_the_verifier_never_receives_the_wallet_key(self):
        """Verification only downloads, which costs nothing.

        Withholding the key keeps a funded mainnet wallet off two more
        internet-facing VMs.
        """
        _, verifier = self._provisioners()
        assert "SECRET_KEY" not in verifier._per_host_env(verifier.hosts[0])

    def test_both_roles_receive_the_scoped_database_url(self):
        uploader, verifier = self._provisioners()
        for provisioner in (uploader, verifier):
            env = provisioner._per_host_env(provisioner.hosts[0])
            assert env["POOL_DATABASE_URL"] == "postgresql://scoped:pw@host/db"
            assert env["DEPLOYMENT_ID"] == 7

    def test_the_services_run_under_the_venv_not_system_python(self):
        """They import saorsa_db, which is not installed system-wide."""
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            build_systemd_unit,
        )

        unit = build_systemd_unit(
            description="d", env_path="/e", script_path="/s", restart="Restart=always"
        )
        assert "/opt/ant-registry-client/bin/python" in unit
        assert "ExecStart=/usr/bin/python3" not in unit

    def test_restart_policies_differ_by_role(self):
        uploader, verifier = self._provisioners()
        assert "Restart=on-failure" in uploader.restart_directive
        assert "Restart=always" in verifier.restart_directive

    def test_env_rendering_omits_empty_values(self):
        """An empty line would override the script's own default with ''."""
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            build_env_file_lines,
        )

        rendered = build_env_file_lines({"A": "1", "B": "", "C": 3})
        assert rendered == "A=1\nC=3"


class TestLogPruning:
    def test_a_retention_window_installs_a_daily_timer(self):
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            build_log_prune_command,
        )

        command = build_log_prune_command("svc", "/var/log/x", 7)
        assert "/usr/bin/find /var/log/x -type f -mtime +7 -delete" in command
        assert "OnCalendar=daily" in command
        assert "systemctl enable --now svc-logprune.timer" in command

    def test_zero_removes_the_timer_so_toggling_off_takes_effect(self):
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            build_log_prune_command,
        )

        command = build_log_prune_command("svc", "/var/log/x", 0)
        assert "disable --now svc-logprune.timer" in command
        assert "find" not in command


class TestTheSaorsaDbInstall:
    def test_the_venv_is_created_only_when_absent_but_the_wheel_always_lands(self):
        """Re-provisioning must pick up a rebuilt wheel.

        The venv is expensive and presence-gated; the package itself is
        force-reinstalled so a VM never runs stale claim logic.
        """
        from saorsa_deploy.provisioning.saorsa_db_install import (
            build_install_commands,
        )

        commands = build_install_commands("saorsa_db-0.2.4-py3-none-any.whl")
        joined = "\n".join(commands)
        assert "test -x /opt/ant-registry-client/bin/python ||" in joined
        assert "--force-reinstall --no-deps" in joined
        assert "import saorsa_db.repositories" in joined

    def test_apt_lists_are_refreshed_before_installing(self):
        """A stock Hetzner Ubuntu 24.04 image ships with no package lists.

        Without `apt-get update` first, installing python3-venv fails with
        "has no installation candidate" even though universe is enabled and the
        package exists — which is exactly how the first live provision died.
        """
        from saorsa_deploy.provisioning.saorsa_db_install import (
            build_install_commands,
        )

        venv_step = build_install_commands("x.whl")[0]
        assert "apt-get update" in venv_step
        assert venv_step.index("apt-get update") < venv_step.index("apt-get install")

    def test_the_versioned_venv_package_is_a_fallback(self):
        """Which of python3-venv / python3.N-venv resolves varies by image."""
        from saorsa_deploy.provisioning.saorsa_db_install import (
            build_install_commands,
        )

        venv_step = build_install_commands("x.whl")[0]
        assert "python3-venv ||" in venv_step
        assert '-venv")' in venv_step or '-venv"' in venv_step

    def test_both_postgres_drivers_are_installed(self):
        """Which driver a bare `postgresql://` URL selects depends on SQLAlchemy.

        2.0 picks psycopg2; 2.1 changed the default to psycopg 3. The URL comes
        from `prod pool client-role` and names no driver, so a VM that resolved
        SQLAlchemy 2.1 with only psycopg2 installed died at startup with
        `No module named 'psycopg'` — measured on a fresh uploader VM on
        2026-09-26.
        """
        from saorsa_deploy.provisioning.saorsa_db_install import (
            build_install_commands,
        )

        deps_step = build_install_commands("x.whl")[1]
        assert "psycopg2-binary" in deps_step
        assert "psycopg[binary]" in deps_step


class TestTheUploaderScript:
    def test_it_imports_without_the_database_packages(self, uploader_script):
        """saorsa_db is imported inside main(), so the module stays testable."""
        assert hasattr(uploader_script, "main")

    def test_the_peer_cache_suppresses_bootstrap_arguments(self, uploader_script, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_CACHE", "true")
        assert uploader_script._resolve_bootstrap_args() == []
        monkeypatch.setenv("BOOTSTRAP_CACHE", "false")
        monkeypatch.setenv("BOOTSTRAP_ARGS", "--bootstrap p1 --bootstrap p2")
        assert uploader_script._resolve_bootstrap_args() == [
            "--bootstrap",
            "p1",
            "--bootstrap",
            "p2",
        ]

    def test_the_last_json_line_wins(self, uploader_script):
        """ant prints progress before the result."""
        parsed = uploader_script._parse_ant_json(
            'noise\n{"a": 1}\n{"address": "xyz", "chunks_stored": 4}'
        )
        assert parsed["address"] == "xyz"

    def test_a_download_hashes_while_streaming(self, uploader_script, tmp_path):
        """A multi-gigabyte file must not be read twice."""
        import hashlib

        payload = b"content" * 5000
        source = tmp_path / "src.bin"
        source.write_bytes(payload)
        destination = tmp_path / "dst.bin"
        size, digest = uploader_script._download(source.as_uri(), destination, timeout=30)
        assert size == len(payload)
        assert digest == hashlib.sha256(payload).hexdigest()
        assert destination.read_bytes() == payload

    def test_influx_lines_omit_empty_strings(self, uploader_script):
        line = uploader_script._build_line(
            "prod_upload",
            {"host": "h", "region": "fsn1"},
            {"status": 0, "address": "abc", "reason": ""},
        )
        assert line.startswith("prod_upload,host=h,region=fsn1 ")
        assert "status=0i" in line
        assert 'address="abc"' in line
        assert "reason" not in line

    def test_no_client_timestamp_is_written(self, uploader_script):
        """InfluxDB stamps receive-time so a skewed VM cannot write the future.

        A point landing hours ahead is silently dropped by every range query,
        which is how a clock-skewed host once vanished from a whole capture.
        """
        line = uploader_script._build_line("m", {"host": "h"}, {"status": 0})
        assert not line.rstrip().split()[-1].isdigit() or "status=0i" in line
        assert line.count(" ") == 1

    def test_the_shortfall_measurement_is_separate_from_uploads(self, uploader_script):
        """ "The pool ran dry" must never look like "the network refused"."""
        assert uploader_script.EXHAUSTION_MEASUREMENT != uploader_script.UPLOAD_MEASUREMENT


class TestTheVerifierScript:
    def test_cycles_are_epoch_aligned(self, verifier_script):
        """Two regions reporting at unrelated times make an availability
        difference indistinguishable from a timing difference."""
        import time

        for interval in (3600, 900):
            wait = verifier_script._seconds_until_next_cycle(interval)
            assert 0 < wait <= interval
            assert (time.time() + wait) % interval < 2

    def test_hashing_matches_hashlib(self, verifier_script, tmp_path):
        import hashlib

        path = tmp_path / "f.bin"
        path.write_bytes(b"payload" * 1000)
        assert verifier_script._sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_it_keeps_no_local_state(self, verifier_script):
        """The work queue is a database query, not a SQLite ledger.

        The canary probe needs SQLite because it cannot reach Postgres; this
        service can, which is what lets a replaced VM resume and an outage of
        any length be caught up.
        """
        source = (RESOURCES / "prod_upload_verifier.py").read_text()
        # The word appears in the docstring explaining why there is none, so
        # this looks for actual use rather than a mention.
        assert "import sqlite3" not in source
        assert "sqlite3." not in source
        assert "list_unverified_uploads_for_host" in source


class TestTheClientRoleCommand:
    """`.env` is loaded as an import side effect of `saorsa_db.db`.

    Every other command reaches the database through `saorsa_deploy.registry`,
    which imports it. This one takes a raw administrative connection and
    imports nothing from `saorsa_db`, so it has to load `.env` itself — without
    that it reports "No DATABASE_URL configured" while sitting next to a `.env`
    that defines one.
    """

    def test_dotenv_is_loaded_before_the_variable_is_read(self, monkeypatch):
        """The regression: reading os.environ without loading .env first.

        `load_dotenv` is stubbed rather than pointed at a real file because it
        searches upward from the *calling module's* location, so it finds this
        repo's own .env no matter what the working directory is — which would
        make the test pass for the wrong reason (and print real credentials on
        failure). What matters is that `_admin_url` calls it before looking.
        """
        from rich.console import Console

        from saorsa_deploy.cmd import pool_client_role as module

        monkeypatch.delenv("DATABASE_URL", raising=False)

        def fake_load_dotenv(*args, **kwargs):
            os.environ["DATABASE_URL"] = "postgresql://admin:pw@example.org/reg"
            return True

        monkeypatch.setattr(module, "load_dotenv", fake_load_dotenv)
        assert module._admin_url(Console()) == "postgresql://admin:pw@example.org/reg"

    def test_a_genuinely_absent_url_still_exits(self, monkeypatch):
        from rich.console import Console

        from saorsa_deploy.cmd import pool_client_role as module

        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(module, "load_dotenv", lambda *a, **k: False)
        with pytest.raises(SystemExit):
            module._admin_url(Console())

    def test_the_role_name_is_validated_because_ddl_cannot_parameterise_it(self):
        from saorsa_deploy.pool_client_role import (
            PoolClientRoleError,
            build_grant_statements,
            validate_role_name,
        )

        assert validate_role_name("ant_pool_client") == "ant_pool_client"
        for bad in ("drop table x", "Robert'); DROP TABLE claims;--", "1abc", ""):
            with pytest.raises(PoolClientRoleError):
                validate_role_name(bad)
        with pytest.raises(PoolClientRoleError):
            build_grant_statements("ok_name", "")

    def test_the_grants_never_include_delete_or_ddl(self):
        """A compromised VM must not be able to destroy history."""
        from saorsa_deploy.pool_client_role import build_grant_statements

        joined = " ".join(build_grant_statements("r", "pw")).upper()
        assert "DELETE" not in joined
        assert "DROP TABLE" not in joined
        assert "CREATE TABLE" not in joined

    def test_the_catalogue_is_read_only_but_claims_are_writable(self):
        from saorsa_deploy.pool_client_role import build_grant_statements

        statements = build_grant_statements("r", "pw")
        catalogue = next(s for s in statements if "ON prod_file_pool TO" in s)
        claims = next(s for s in statements if "ON prod_file_pool_claims TO" in s)
        assert catalogue.startswith("GRANT SELECT ON")
        assert "INSERT" in claims and "UPDATE" in claims

    def test_the_client_url_keeps_the_admin_urls_sslmode(self):
        """A scoped client must not end up connecting less securely."""
        from saorsa_deploy.pool_client_role import build_client_url

        url = build_client_url(
            "postgresql://admin:pw@db.example.org:25060/reg?sslmode=require",
            "ant_pool_client",
            "s3cret",
        )
        assert url.startswith("postgresql://ant_pool_client:s3cret@db.example.org:25060/reg")
        assert url.endswith("?sslmode=require")

    def test_a_password_with_url_characters_is_escaped(self):
        from saorsa_deploy.pool_client_role import build_client_url

        url = build_client_url("postgresql://admin:pw@h/db", "role", "a/b@c:d?e")
        assert "a%2Fb%40c%3Ad%3Fe" in url
        assert url.count("@") == 1


class TestTheInfraSpecsAreWhatClientsInfraExpects:
    """The two client roles do NOT share a spec grammar.

    A downloader is `provider:region[:vm_size]`; an uploader is
    `provider:region:file_size_mb[:vm_size]`, where the third positional is a
    legacy field from the testnet uploader that generated random files. Getting
    that wrong is invisible until `clients infra` parses it — which happens
    AFTER the monitoring VM has been created and started billing, so these
    round-trip through the real parsers.
    """

    TOKEN = "bootstrap_cache=true"

    def test_the_uploader_spec_carries_the_legacy_file_size_field(self):
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_uploader_infra_spec,
        )

        spec = build_uploader_infra_spec("hetzner", "fsn1:cx22", self.TOKEN)
        assert spec == "hetzner:fsn1:0:cx22:bootstrap_cache=true"
        parsed = parse_uploader_spec(spec)
        assert parsed["provider"] == "hetzner"
        assert parsed["region"] == "fsn1"
        assert parsed["vm_size"] == "cx22"
        # Zero because this kind uploads real pool files rather than generated
        # ones, exactly as `prod uploaders` does.
        assert parsed["file_size_mb"] == 0

    def test_an_uploader_without_a_vm_size_still_parses(self):
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_uploader_infra_spec,
        )

        parsed = parse_uploader_spec(build_uploader_infra_spec("hetzner", "fsn1", self.TOKEN))
        assert parsed["region"] == "fsn1"

    def test_a_verifier_naming_its_provider_is_honoured(self):
        from saorsa_deploy.cmd.infra_clients import parse_downloader_spec
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_verifier_infra_spec,
        )

        parsed = parse_downloader_spec(
            build_verifier_infra_spec("hetzner", "ovh:GRA9:b3-8", self.TOKEN)
        )
        assert parsed["provider"] == "ovh"
        assert parsed["region"] == "GRA9"
        assert parsed["vm_size"] == "b3-8"

    def test_a_verifier_without_a_provider_inherits_the_uploaders(self):
        from saorsa_deploy.cmd.infra_clients import parse_downloader_spec
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_verifier_infra_spec,
        )

        parsed = parse_downloader_spec(
            build_verifier_infra_spec("hetzner", "lon1:s-1vcpu-2gb", self.TOKEN)
        )
        assert parsed["provider"] == "hetzner"
        assert parsed["region"] == "lon1"
        assert parsed["vm_size"] == "s-1vcpu-2gb"

    def test_a_two_field_provider_spec_is_not_mistaken_for_region_and_size(self):
        """`ovh:GRA9` and `lon1:s-1vcpu-2gb` both have two fields.

        Counting fields to decide whether a provider is present reads the first
        as region `ovh`, size `GRA9` — and builds the VM in the wrong cloud.
        """
        from saorsa_deploy.cmd.infra_clients import parse_downloader_spec
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_verifier_infra_spec,
        )

        parsed = parse_downloader_spec(build_verifier_infra_spec("hetzner", "ovh:GRA9", self.TOKEN))
        assert parsed["provider"] == "ovh"
        assert parsed["region"] == "GRA9"

    def test_every_spec_the_run_manifest_produces_parses(self):
        """End to end from the manifest, through the command, to the parsers."""
        from saorsa_deploy.cmd.infra_clients import (
            parse_downloader_spec,
            parse_uploader_spec,
        )
        from saorsa_deploy.cmd.prod_continuous_uploader import (
            build_uploader_infra_spec,
            build_verifier_infra_spec,
        )

        manifest = _manifest()
        argv = _deploy_argv(manifest)
        spec = argv[argv.index("--spec") + 1]
        provider = argv[argv.index("--provider") + 1]
        verifiers = [argv[i + 1] for i, a in enumerate(argv) if a == "--verifier"]

        parse_uploader_spec(build_uploader_infra_spec(provider, spec, self.TOKEN))
        for verifier in verifiers:
            parse_downloader_spec(build_verifier_infra_spec(provider, verifier, self.TOKEN))


class TestPrintUploadsDispatch:
    """`print-uploads` must not fall through to the file-ops path.

    The write test writes the same `prod_upload` measurement as prod-uploaders,
    but the fall-through queries the `upload`/`download` measurements instead
    and reports "no upload operations recorded" for a deployment that is
    working perfectly.
    """

    def _dispatch(self, monkeypatch, kind, op_type):
        from saorsa_deploy.cmd import print_file_ops as module

        monkeypatch.setattr(module, "_resolve_kind", lambda name, console: kind)
        called = {}
        monkeypatch.setattr(
            module,
            "_run_prod_uploads",
            lambda args, console: called.setdefault("path", "prod_upload"),
        )
        monkeypatch.setattr(
            module,
            "_run_file_ops",
            lambda args, op, console: called.setdefault("path", "file_ops"),
        )
        module._dispatch(SimpleNamespace(name="PROD-UL-01"), op_type)
        return called.get("path", "none")

    def test_uploads_use_the_prod_upload_measurement(self, monkeypatch):
        assert self._dispatch(monkeypatch, "prod-continuous-uploader", "upload") == "prod_upload"

    def test_downloads_explain_where_verifications_live(self, monkeypatch):
        """It does not download; it re-reads, and those rows are in Postgres."""
        assert self._dispatch(monkeypatch, "prod-continuous-uploader", "download") == "none"

    def test_prod_uploaders_is_unaffected(self, monkeypatch):
        assert self._dispatch(monkeypatch, "prod-uploaders", "upload") == "prod_upload"


class TestLogsAreBounded:
    """A 40 GB root disk with per-op logs needs a prune timer, on both roles."""

    def test_both_roles_get_a_prune_timer(self):
        from saorsa_deploy.provisioning.prod_continuous_uploader import (
            UPLOADER_LOG_DIR,
            UPLOADER_SERVICE,
            VERIFIER_LOG_DIR,
            VERIFIER_SERVICE,
            build_log_prune_command,
        )

        for service, log_dir in (
            (UPLOADER_SERVICE, UPLOADER_LOG_DIR),
            (VERIFIER_SERVICE, VERIFIER_LOG_DIR),
        ):
            command = build_log_prune_command(service, log_dir, 7)
            assert f"find {log_dir} -type f -mtime +7 -delete" in command
            assert f"systemctl enable --now {service}-logprune.timer" in command


class TestTheUploadTableShowsWhenItHappened:
    """A permanent daily run is a time series.

    Without a timestamp the table cannot answer "did today's cycle run?",
    which is the question the whole deployment exists to answer.
    """

    def _render(self, ops):
        from rich.console import Console

        from saorsa_deploy.cmd import print_file_ops as module

        console = Console(record=True, width=200)
        module._prod_print_table(
            {"host-1": ops},
            {"host-1": "default"},
            {"host-1": True},
            {},
            console,
        )
        return console.export_text()

    def test_the_completion_time_is_rendered(self):
        from datetime import datetime, timezone

        output = self._render(
            [
                {
                    "file_name": "a.jpg",
                    "file_size": 1024,
                    "status": 0,
                    "chunks_stored": 4,
                    "total_chunks": 4,
                    "chunks_failed": 0,
                    "total_ms": 1500,
                    "start_ns": 1,
                    "_time": datetime(2026, 8, 15, 0, 37, 31, tzinfo=timezone.utc),
                }
            ]
        )
        assert "2026-08-15" in output
        assert "00:37:31" in output

    def test_a_missing_timestamp_does_not_break_the_table(self):
        output = self._render(
            [
                {
                    "file_name": "a.jpg",
                    "file_size": 1024,
                    "status": 0,
                    "chunks_stored": 4,
                    "total_chunks": 4,
                    "chunks_failed": 0,
                    "total_ms": 1500,
                    "start_ns": 1,
                }
            ]
        )
        assert "a.jpg" in output


class TestSourceFetchRetries:
    """The Internet Archive supplies most of the pool and is frequently flaky.

    Giving up on the first error costs the whole day's measurement: on
    2026-08-17 all three files 503'd within the same second and the cycle
    produced no production signal at all.
    """

    def test_backoff_doubles_and_is_capped(self, uploader_script):
        assert uploader_script._backoff_delays(5, 10, 300) == [10, 20, 40, 80]
        assert uploader_script._backoff_delays(6, 10, 60) == [10, 20, 40, 60, 60]

    def test_one_delay_per_retry_not_per_attempt(self, uploader_script):
        """The first try is immediate; only the retries wait."""
        assert len(uploader_script._backoff_delays(5, 10, 300)) == 4
        assert uploader_script._backoff_delays(1, 10, 300) == []

    def test_the_jitter_actually_varies(self, uploader_script):
        """A whole cycle fails at once when the origin is down, so an
        unjittered schedule sends every file back at the same moments.

        Derived from the attempt number alone it would be identical for every
        file and every host — no jitter at all.
        """
        draws = {uploader_script._jittered(40) for _ in range(25)}
        assert len(draws) > 1, "every call returned the same wait"
        assert all(20 <= d <= 60 for d in draws), "jitter must stay within +/-50%"

    def test_transient_http_is_retried(self, uploader_script):
        import urllib.error

        for code in (429, 500, 502, 503, 504):
            error = urllib.error.HTTPError("u", code, "m", {}, None)
            assert uploader_script._is_retryable_fetch_error(error), code

    def test_a_missing_file_is_not_retried(self, uploader_script):
        """404/403 is a fact about the file, not the weather."""
        import urllib.error

        for code in (403, 404, 410):
            error = urllib.error.HTTPError("u", code, "m", {}, None)
            assert not uploader_script._is_retryable_fetch_error(error), code

    def test_network_errors_are_retried(self, uploader_script):
        import urllib.error

        assert uploader_script._is_retryable_fetch_error(TimeoutError())
        assert uploader_script._is_retryable_fetch_error(urllib.error.URLError("dns"))

    def test_a_flaky_source_eventually_succeeds(self, uploader_script, monkeypatch):
        import urllib.error

        calls = {"n": 0}

        def flaky(url, destination, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            return 1234, "a" * 64

        monkeypatch.setattr(uploader_script, "_download_once", flaky)
        monkeypatch.setattr(uploader_script.time, "sleep", lambda s: None)

        size, digest = uploader_script._download(
            "https://archive.org/x", Path("/tmp/x"), 30, attempts=5, base_secs=1
        )
        assert (size, digest) == (1234, "a" * 64)
        assert calls["n"] == 3

    def test_it_gives_up_after_the_configured_attempts(self, uploader_script, monkeypatch):
        import urllib.error

        calls = {"n": 0}

        def always_503(url, destination, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 503, "busy", {}, None)

        monkeypatch.setattr(uploader_script, "_download_once", always_503)
        monkeypatch.setattr(uploader_script.time, "sleep", lambda s: None)

        with pytest.raises(urllib.error.HTTPError):
            uploader_script._download(
                "https://archive.org/x", Path("/tmp/x"), 30, attempts=4, base_secs=1
            )
        assert calls["n"] == 4

    def test_a_permanent_error_costs_only_one_attempt(self, uploader_script, monkeypatch):
        import urllib.error

        calls = {"n": 0}

        def gone(url, destination, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError(url, 404, "gone", {}, None)

        monkeypatch.setattr(uploader_script, "_download_once", gone)
        monkeypatch.setattr(uploader_script.time, "sleep", lambda s: None)

        with pytest.raises(urllib.error.HTTPError):
            uploader_script._download(
                "https://archive.org/x", Path("/tmp/x"), 30, attempts=5, base_secs=1
            )
        assert calls["n"] == 1, "a 404 must not burn the retry budget"

    def test_a_partial_file_is_discarded_between_attempts(
        self, uploader_script, monkeypatch, tmp_path
    ):
        """A truncated source would upload as if whole, then verify against its
        own truncated hash and look perfectly healthy."""
        import urllib.error

        destination = tmp_path / "f.bin"
        calls = {"n": 0}

        def truncating(url, dest, timeout):
            calls["n"] += 1
            dest.write_bytes(b"half")
            if calls["n"] < 2:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            return 4, "b" * 64

        monkeypatch.setattr(uploader_script, "_download_once", truncating)
        monkeypatch.setattr(uploader_script.time, "sleep", lambda s: None)
        seen = []
        real_unlink = Path.unlink

        def spy(self, missing_ok=False):
            seen.append(self.name)
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", spy)
        uploader_script._download("https://archive.org/x", destination, 30, attempts=3, base_secs=1)
        assert "f.bin" in seen, "the failed attempt's partial file must be removed"


class TestTheCycleHeartbeat:
    """ "Production is broken" and "we could not test production" must not look
    the same — for a monitor, that is the most important distinction there is."""

    def test_the_cycle_measurement_is_separate(self, uploader_script):
        assert uploader_script.CYCLE_MEASUREMENT == "prod_upload_cycle"
        assert uploader_script.CYCLE_MEASUREMENT != uploader_script.UPLOAD_MEASUREMENT
        assert uploader_script.CYCLE_MEASUREMENT != uploader_script.EXHAUSTION_MEASUREMENT

    def test_the_line_carries_the_signal_flag(self, uploader_script):
        line = uploader_script._build_line(
            uploader_script.CYCLE_MEASUREMENT,
            {"host": "h", "region": "fsn1"},
            {"attempted": 3, "succeeded": 0, "source_failed": 3, "produced_signal": False},
        )
        assert "produced_signal=false" in line
        assert "source_failed=3i" in line


class TestTheSourceFailureLabel:
    """`dl-fail` in an upload table reads as "the read-back failed", i.e. as a
    production fault — the opposite of what it means."""

    def test_status_two_reads_as_a_source_failure(self):
        from saorsa_deploy.cmd.print_file_ops import (
            _PROD_STATUS_LABELS,
            _PROD_STATUS_TEXT,
        )

        assert "src-fail" in _PROD_STATUS_LABELS[2]
        assert "dl-fail" not in _PROD_STATUS_LABELS[2]
        assert _PROD_STATUS_TEXT[2] == "src-fail"

    def test_the_recorded_results_view_agrees(self):
        from saorsa_db.models import ProdUploadStatus

        from saorsa_deploy.cmd.print_recorded_results import _PROD_STATUS_LABELS

        label = _PROD_STATUS_LABELS[ProdUploadStatus.DOWNLOAD_FAILED]
        assert "src-fail" in label and "dl-fail" not in label
