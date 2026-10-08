"""The pool size bounds as they reach the three consumers.

The bounds are only useful if they survive the whole path an operator actually
uses: a manifest field, through validation, through the argv the orchestrator
emits, into the spec token or flag the deploy command parses, and finally into
the claim query. Each hop below is one that would silently drop them.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection
from saorsa_deploy.cmd import prod_canary, prod_local_uploads
from saorsa_deploy.cmd.prod_uploaders import (
    parse_prod_uploader_spec,
    parse_prod_wasm_uploader_spec,
)
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps

KIB = 1024
MIB = 1024 * KIB


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


@pytest.fixture
def fake_s3(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(pool_selection, "load_pool_selection", lambda name: store.get(name))
    monkeypatch.setattr(
        pool_selection,
        "save_pool_selection",
        lambda name, selection: store.__setitem__(name, selection),
    )
    return store


@pytest.fixture
def wired(repo, monkeypatch, fake_s3):
    monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: repo)
    return repo


def _seed(repo, kind, size_bytes, count, prefix):
    for i in range(count):
        repo.add_pool_file(
            kind,
            f"https://example.org/{prefix}{i}.bin"
            if kind is PoolFileKind.URL
            else f"/data/{prefix}{i}.bin",
            f"{prefix}{i}.bin",
            size_bytes=size_bytes,
        )


def _flat(text):
    return " ".join(text.split())


class TestUploaderSpecToken:
    def test_bounds_parse_into_bytes(self):
        spec = parse_prod_uploader_spec(
            "lon1:s-2vcpu-4gb:pool=small:2:pool_min_size=20MB:pool_max_size=60MB"
        )
        assert spec["pool_min_size_bytes"] == 20 * MIB
        assert spec["pool_max_size_bytes"] == 60 * MIB

    def test_absent_bounds_are_none(self):
        spec = parse_prod_uploader_spec("lon1:s-2vcpu-4gb:pool=small:2")
        assert spec["pool_min_size_bytes"] is None
        assert spec["pool_max_size_bytes"] is None

    def test_bounds_on_a_urls_spec_are_refused(self):
        # Silently ignoring them would leave the operator believing a size
        # restriction is in force over a list the pool never selected.
        with pytest.raises(ValueError, match="only apply to pool"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/tmp/u.txt:pool_min_size=5MB")

    def test_bounds_excluding_the_requested_class_are_refused(self):
        with pytest.raises(ValueError, match="exclude the 'small' class"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:pool=small:2:pool_max_size=5MB")

    def test_unparseable_bound_is_refused(self):
        with pytest.raises(ValueError, match="invalid min_size"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:pool=small:2:pool_min_size=biggish")


class TestCanaryFlags:
    def _args(self, **kwargs):
        base = dict(
            name="PROD-CANARY-04",
            pool="tiny:3",
            pool_min_size=None,
            pool_max_size=None,
        )
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_min_size_keeps_sub_5mb_files_out_of_a_canary(self, wired):
        from rich.console import Console

        # The reason the option exists: both of these are `tiny`, and both yield
        # exactly 4 chunks, but only one is worth a permanently-spent entry.
        _seed(wired, PoolFileKind.URL, 14 * KIB, 5, "sliver")
        _seed(wired, PoolFileKind.URL, 6 * MIB, 3, "chunky")

        content, selected = prod_canary._select_canary_pool_files(
            self._args(pool_min_size="5MB"), Console()
        )
        assert len(selected) == 3
        assert all(entry.size_bytes == 6 * MIB for entry in selected)
        assert "sliver" not in content

    def test_shortfall_message_names_the_bound(self, wired, capsys):
        from rich.console import Console

        _seed(wired, PoolFileKind.URL, 14 * KIB, 20, "sliver")
        with pytest.raises(SystemExit):
            prod_canary._select_canary_pool_files(self._args(pool_min_size="5MB"), Console())
        out = _flat(capsys.readouterr().out)
        assert "5.00 MB" in out
        assert "tiny 3 requested, 0 available" in out

    def test_bounds_are_persisted_with_the_selection(self, wired, fake_s3):
        from rich.console import Console

        _seed(wired, PoolFileKind.URL, 6 * MIB, 5, "chunky")
        prod_canary._select_canary_pool_files(self._args(pool_min_size="5MB"), Console())
        slot = fake_s3["PROD-CANARY-04"]["slots"]["canary-upload"]
        assert slot["min_size_bytes"] == 5 * MIB
        assert slot["max_size_bytes"] is None

    def test_invalid_bound_exits_before_claiming(self, wired, capsys):
        from rich.console import Console

        _seed(wired, PoolFileKind.URL, 6 * MIB, 5, "chunky")
        with pytest.raises(SystemExit):
            prod_canary._select_canary_pool_files(
                self._args(pool_min_size="100MB", pool_max_size="10MB"), Console()
            )
        assert "must be below" in _flat(capsys.readouterr().out)
        assert wired.list_pool_claims() == []


class TestLocalUploadsFlags:
    def test_bounds_reach_the_local_selection(self, wired, tmp_path):
        from rich.console import Console

        for i in range(3):
            (tmp_path / f"big{i}.bin").write_bytes(b"x")
        for i in range(3):
            (tmp_path / f"small{i}.bin").write_bytes(b"x")
        for i in range(3):
            wired.add_pool_file(
                PoolFileKind.LOCAL,
                str(tmp_path / f"big{i}.bin"),
                f"big{i}.bin",
                size_bytes=6 * MIB,
            )
            wired.add_pool_file(
                PoolFileKind.LOCAL,
                str(tmp_path / f"small{i}.bin"),
                f"small{i}.bin",
                size_bytes=100 * KIB,
            )

        args = argparse.Namespace(
            name="PROD-LOCAL-01",
            from_pool="tiny:2",
            exclude_partial=False,
            pool_min_size="5MB",
            pool_max_size=None,
        )
        pairs, _ = prod_local_uploads._select_from_pool(args, Console())
        assert len(pairs) == 2
        assert all(name.startswith("big") for _, name in pairs)


def _canary(**canary_fields):
    block = {
        "binary": {"version": "0.3.2"},
        "upload_spec": "lon1",
        "probes": [{"region": "lon1", "count": 1}],
    }
    block.update(canary_fields)
    return Manifest.model_validate(
        {
            "name": "PROD-CANARY-04",
            "kind": "prod-canary",
            "purpose": "loss measurement",
            "ssh_key_path": "/ssh/key",
            "prod_canary": block,
        }
    )


class TestManifest:
    def test_canary_bounds_become_cli_flags(self):
        m = _canary(
            pool={"tiny": 37, "small": 233},
            pool_min_size="5MB",
            pool_max_size="100MB",
        )
        argv = build_deploy_steps(m)[0].argv
        assert argv[argv.index("--pool-min-size") + 1] == "5MB"
        assert argv[argv.index("--pool-max-size") + 1] == "100MB"

    def test_absent_bounds_emit_no_flags(self):
        argv = build_deploy_steps(_canary(pool={"small": 3}))[0].argv
        assert "--pool-min-size" not in argv
        assert "--pool-max-size" not in argv

    def test_uploader_bounds_become_spec_tokens(self):
        m = Manifest.model_validate(
            {
                "name": "PROD-UL-09",
                "kind": "prod-uploaders",
                "purpose": "matrix",
                "ssh_key_path": "/ssh/key",
                "prod_uploaders": {
                    "binary": {"version": "0.3.2"},
                    "uploaders": [
                        {
                            "region": "lon1",
                            "vm_size": "s-2vcpu-4gb",
                            "pool": {"medium": 2},
                            "pool_min_size": "200MB",
                        }
                    ],
                },
            }
        )
        argv = build_deploy_steps(m)[0].argv
        spec = argv[argv.index("--uploader") + 1]
        assert "pool_min_size=200MB" in spec
        # And it round-trips through the parser the deploy command uses — the
        # hop where a token the orchestrator emits but nothing accepts would
        # otherwise fail only against real infrastructure.
        assert parse_prod_uploader_spec(spec)["pool_min_size_bytes"] == 200 * MIB

    def test_wasm_uploader_bounds_become_spec_tokens(self):
        """The browser-path uploader is a pool consumer too.

        Its `pool_max_size` additionally carries ant-core's browser cap for a
        `medium` request, which is why the round-trip through the parser matters
        here: a clamp the orchestrator writes but the parser rejects would only
        fail against real infrastructure.
        """
        m = Manifest.model_validate(
            {
                "name": "PROD-WASM-UL-01",
                "kind": "prod-uploaders",
                "purpose": "browser path",
                "ssh_key_path": "/ssh/key",
                "prod_uploaders": {
                    "binary": {"version": "0.3.8"},
                    "wasm_binary": {"branch": "ant-cli-v0.3.8-rc.1"},
                    "wasm_uploaders": [
                        {
                            "region": "lon1",
                            "pool": {"medium": 2},
                            "pool_min_size": "200MB",
                        }
                    ],
                },
            }
        )
        argv = build_deploy_steps(m)[-1].argv
        spec = argv[argv.index("--wasm-uploader") + 1]
        assert "pool_min_size=200MB" in spec
        assert "pool_max_size=1000000000" in spec
        parsed = parse_prod_wasm_uploader_spec(spec)
        assert parsed["pool_min_size_bytes"] == 200 * MIB
        assert parsed["pool_max_size_bytes"] == 1_000_000_000

    def test_continuous_uploader_bounds_become_cli_flags(self):
        """The daily write test is a pool consumer too, so it belongs here.

        Its argv is also parsed by the real CLI, since a flag the orchestrator
        emits but no parser accepts would otherwise only fail against real
        infrastructure.
        """
        m = Manifest.model_validate(
            {
                "name": "PROD-UL-DAILY-01",
                "kind": "prod-continuous-uploader",
                "purpose": "daily write test",
                "ssh_key_path": "/ssh/key",
                "prod_continuous_uploader": {
                    "spec": "fsn1:cx22",
                    "volume_size": 40,
                    "binary": {"version": "0.3.3"},
                    "pool": {"tiny": 2, "small": 1},
                    "pool_min_size": "5MB",
                    "pool_max_size": "100MB",
                    "verifiers": [{"region": "lon1", "volume_size": 40}],
                },
            }
        )
        argv = build_deploy_steps(m)[0].argv
        assert argv[argv.index("--pool-min-size") + 1] == "5MB"
        assert argv[argv.index("--pool-max-size") + 1] == "100MB"
        assert argv[argv.index("--pool") + 1] == "tiny:2,small:1"

        captured = {}
        from saorsa_deploy.cmd import prod_continuous_uploader as module
        from saorsa_deploy.main import main

        original = module.cmd_prod_continuous_uploader_deploy
        module.cmd_prod_continuous_uploader_deploy = lambda args: captured.update(vars(args))
        try:
            main(argv)
        finally:
            module.cmd_prod_continuous_uploader_deploy = original
        assert captured["pool_min_size"] == "5MB"
        assert captured["pool_max_size"] == "100MB"

    def test_local_uploads_bounds_become_cli_flags(self):
        m = Manifest.model_validate(
            {
                "name": "PROD-LOCAL-05",
                "kind": "prod-local-uploads",
                "purpose": "home line",
                "ssh_key_path": "/ssh/key",
                "prod_local_uploads": {
                    "binary": {"version": "0.3.2"},
                    "pool": {"tiny": 10},
                    "pool_min_size": "5MB",
                },
            }
        )
        argv = build_deploy_steps(m)[0].argv
        assert argv[argv.index("--pool-min-size") + 1] == "5MB"

    def test_bounds_without_a_pool_request_are_refused(self):
        with pytest.raises(Exception, match="have no effect without one"):
            _canary(urls_file="/tmp/urls.txt", pool_min_size="5MB")

    def test_bounds_excluding_a_requested_class_are_refused(self):
        with pytest.raises(Exception, match="exclude the 'medium' class"):
            _canary(pool={"medium": 5}, pool_max_size="5MB")

    def test_inverted_bounds_are_refused(self):
        with pytest.raises(Exception, match="must be below"):
            _canary(pool={"small": 5}, pool_min_size="100MB", pool_max_size="20MB")

    def test_unparseable_bound_is_refused_at_validate_time(self):
        with pytest.raises(Exception, match="could not parse size"):
            _canary(pool={"small": 5}, pool_min_size="quite big")

    def test_a_bare_number_is_bytes(self):
        m = _canary(pool={"tiny": 5}, pool_min_size=5242880)
        argv = build_deploy_steps(m)[0].argv
        assert argv[argv.index("--pool-min-size") + 1] == "5242880"


class TestTheCliAcceptsWhatTheOrchestratorEmits:
    """The argv a manifest produces must actually parse.

    This is not hypothetical: `--pool` was defined on the `prod canary` group
    parser rather than on `canary deploy`, and an option on a group parser has
    to be written BEFORE the subcommand — so every `prod canary deploy --pool …`
    the orchestrator emitted was rejected by argparse. Nothing caught it because
    the orchestrator tests assert on argv and the command tests build a
    Namespace directly; neither goes through the parser.
    """

    def _run(self, monkeypatch, module, func_name, argv):
        from saorsa_deploy.main import main

        captured = {}
        monkeypatch.setattr(module, func_name, lambda args: captured.update(vars(args)))
        main(argv)
        return captured

    def test_canary(self, monkeypatch):
        from saorsa_deploy.cmd import prod_canary as module

        m = _canary(pool={"tiny": 37}, pool_min_size="5MB", pool_max_size="100MB")
        args = self._run(
            monkeypatch, module, "cmd_prod_canary_deploy", build_deploy_steps(m)[0].argv
        )
        assert args["pool"] == "tiny:37"
        assert args["pool_min_size"] == "5MB"
        assert args["pool_max_size"] == "100MB"
        # --urls-file must not be argparse-required, or a pool run cannot parse.
        assert args["urls_file"] is None

    def test_local_uploads(self, monkeypatch):
        from saorsa_deploy.cmd import prod_local_uploads as module

        m = Manifest.model_validate(
            {
                "name": "PROD-LOCAL-05",
                "kind": "prod-local-uploads",
                "purpose": "home line",
                "ssh_key_path": "/ssh/key",
                "prod_local_uploads": {
                    "binary": {"version": "0.3.2"},
                    "pool": {"tiny": 10},
                    "pool_min_size": "5MB",
                },
            }
        )
        args = self._run(
            monkeypatch, module, "cmd_prod_local_uploads", build_deploy_steps(m)[0].argv
        )
        assert args["from_pool"] == "tiny:10"
        assert args["pool_min_size"] == "5MB"

    def test_wasm_uploaders(self, monkeypatch):
        from saorsa_deploy.cmd import prod_uploaders as module

        m = Manifest.model_validate(
            {
                "name": "PROD-WASM-UL-01",
                "kind": "prod-uploaders",
                "purpose": "browser path",
                "ssh_key_path": "/ssh/key",
                "prod_uploaders": {
                    "binary": {"version": "0.3.8"},
                    "wasm_binary": {"branch": "ant-cli-v0.3.8-rc.1"},
                    "wasm_uploaders": [
                        {
                            "region": "lon1",
                            "pool": {"medium": 2},
                            "pool_min_size": "200MB",
                        }
                    ],
                },
            }
        )
        args = self._run(
            monkeypatch,
            module,
            "cmd_prod_uploaders_deploy",
            build_deploy_steps(m)[-1].argv,
        )
        assert "pool_min_size=200MB" in args["wasm_uploader"][0]
        assert "pool_max_size=1000000000" in args["wasm_uploader"][0]

    def test_uploaders(self, monkeypatch):
        from saorsa_deploy.cmd import prod_uploaders as module

        m = Manifest.model_validate(
            {
                "name": "PROD-UL-09",
                "kind": "prod-uploaders",
                "purpose": "matrix",
                "ssh_key_path": "/ssh/key",
                "prod_uploaders": {
                    "binary": {"version": "0.3.2"},
                    "uploaders": [
                        {
                            "region": "lon1",
                            "vm_size": "s-2vcpu-4gb",
                            "pool": {"medium": 2},
                            "pool_min_size": "200MB",
                        }
                    ],
                },
            }
        )
        args = self._run(
            monkeypatch, module, "cmd_prod_uploaders_deploy", build_deploy_steps(m)[0].argv
        )
        assert "pool_min_size=200MB" in args["uploader"][0]
