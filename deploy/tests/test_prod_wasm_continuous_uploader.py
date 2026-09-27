"""The browser path on the daily production write test (V2-1335).

`prod-continuous-uploader` is the only pool consumer that claims files as it
uploads them, so it is also the only place where a claim's outcome is decided on
the VM, cycle after cycle, for as long as the deployment runs. Putting the
browser path on it therefore has to get two things exactly right: which binary
the service spawns, and — because an OOM kills the WASM child rather than the
Python parent — whether a failure happened before or after a payment went out.

Covered: manifest validation, the argv the orchestrator emits and the CLI
accepts, the provisioner's install and env output, the service's transport
dispatch and claim resolution against a real registry, the Influx bucket split,
the inventory fields, and the invariant that a native manifest renders
byte-identically to before this existed.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pydantic
import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimStatus, PoolFileKind
from saorsa_db.models.prod_upload_result import ProdUploadStatus
from saorsa_db.repositories import (
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES
from saorsa_deploy.cmd import prod_continuous_uploader as cli
from saorsa_deploy.cmd.generate_inventory import build_client_config
from saorsa_deploy.config.models import Manifest, ProdContinuousUploaderSpec
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    ProdContinuousUploaderProvisioner,
    ProdUploadVerifierProvisioner,
)

MIB = 1024 * 1024

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy/resources/scripts/prod_continuous_uploader.py"
)

MA = (
    "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiAaaa/p2p/"
    "57a89f84e1652d22fd6b791c29d83f187fbed3ba8e08e265fe3031b3a6f725da"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_continuous_uploader", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block(**overrides):
    block = {
        "spec": "lon1:s-4vcpu-8gb",
        "binary": {"version": "0.3.8"},
        "pool": {"tiny": 2},
        "verifiers": [{"region": "nyc3", "vm_size": "s-1vcpu-2gb"}],
        "client_kind": "wasm",
        "wasm_binary": {"branch": "ant-cli-v0.3.8-rc.1", "repo_owner": "WithAutonomi"},
    }
    block.update(overrides)
    return block


def _manifest(**overrides):
    return Manifest.model_validate(
        {
            "name": "PROD-WUL-01",
            "kind": "prod-continuous-uploader",
            "purpose": "browser-path daily write test",
            "ssh_key_path": "/ssh/key",
            "prod_continuous_uploader": _block(**overrides),
        }
    )


def _deploy_argv(manifest):
    steps = build_deploy_steps(manifest)
    return next(s.argv for s in steps if s.argv[:2] == ["prod", "continuous-uploader"])


def _val(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_a_native_manifest_needs_nothing_new(self):
        spec = ProdContinuousUploaderSpec.model_validate(
            _block(client_kind=None, wasm_binary=None, spec="lon1:s-2vcpu-4gb")
        )
        assert spec.client_kind is None
        assert spec.wasm_binary is None

    def test_wasm_needs_a_bundle_source(self):
        # The bundle is always built from source — there is no release form — so
        # without this the deploy would have nothing to install.
        with pytest.raises(pydantic.ValidationError, match="wasm_binary is required"):
            ProdContinuousUploaderSpec.model_validate(_block(wasm_binary=None))

    def test_a_bundle_source_without_wasm_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="only valid with"):
            ProdContinuousUploaderSpec.model_validate(_block(client_kind=None))

    def test_a_bootstrap_file_without_wasm_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="only valid with"):
            ProdContinuousUploaderSpec.model_validate(
                _block(
                    client_kind=None,
                    wasm_binary=None,
                    wasm_bootstrap_multiaddrs_file="/tmp/ma.txt",
                )
            )

    def test_a_vm_below_eight_gb_is_refused(self):
        # A DigitalOcean slug encodes its memory, so this is a fact rather than a
        # guess: a browser-path transfer holds ~4x the file in WASM memory, and
        # an under-sized box turns every upload into an OOM kill that reads as a
        # network fault.
        with pytest.raises(pydantic.ValidationError, match="at least 8 GB"):
            ProdContinuousUploaderSpec.model_validate(_block(spec="lon1:s-2vcpu-4gb"))

    def test_a_size_less_spec_is_accepted_and_defaulted_by_the_cli(self):
        # Nothing to check here — the default is applied at deploy time, where
        # the provider is known.
        spec = ProdContinuousUploaderSpec.model_validate(_block(spec="lon1"))
        assert spec.spec == "lon1"

    def test_a_non_digitalocean_size_is_not_refused(self):
        # Other providers' size names cannot be read here, so a hard error would
        # block a perfectly adequate Hetzner box.
        spec = ProdContinuousUploaderSpec.model_validate(
            _block(spec="fsn1:cx43", provider="hetzner", volume_size=40)
        )
        assert spec.spec == "fsn1:cx43"

    @pytest.mark.parametrize("size_class", ["large", "xlarge"])
    def test_the_classes_above_the_cap_are_refused(self, size_class):
        # Every file in them is above ant-core's browser cap, so every draw
        # would permanently spend a pool entry that could never be uploaded.
        with pytest.raises(pydantic.ValidationError, match="browser path"):
            ProdContinuousUploaderSpec.model_validate(
                _block(pool={size_class: 1}, volume_size=100)
            )

    def test_medium_is_allowed_because_most_of_it_is_drawable(self):
        spec = ProdContinuousUploaderSpec.model_validate(
            _block(pool={"medium": 1}, volume_size=40)
        )
        assert spec.pool == {"medium": 1}

    @pytest.mark.parametrize(
        "field,value", [("bootstrap_cache", False), ("log_level", "debug")]
    )
    def test_the_options_with_no_browser_meaning_are_refused(self, field, value):
        # Refused, not ignored: a silently-dropped option leaves the operator
        # reading their own manifest as a description of the run.
        with pytest.raises(pydantic.ValidationError, match="cannot be set with"):
            ProdContinuousUploaderSpec.model_validate(_block(**{field: value}))

    def test_rust_log_still_works(self):
        # `ant-wasm` reads RUST_LOG straight from its environment; it is only the
        # `-v` verbosity flag that has no browser-path equivalent.
        spec = ProdContinuousUploaderSpec.model_validate(_block(rust_log="a::b=debug"))
        assert spec.rust_log == "a::b=debug"


# ---------------------------------------------------------------------------
# The argv
# ---------------------------------------------------------------------------


class TestOrchestratorArgv:
    def test_a_native_manifest_emits_no_wasm_flags_and_no_build_step(self):
        manifest = _manifest(
            client_kind=None, wasm_binary=None, spec="lon1:s-2vcpu-4gb"
        )
        steps = build_deploy_steps(manifest)
        assert not any("ant-wasm" in step.argv for step in steps)
        argv = _deploy_argv(manifest)
        assert "--client-kind" not in argv
        assert "--wasm-branch-name" not in argv

    def test_the_transport_and_the_bundle_are_emitted(self):
        argv = _deploy_argv(_manifest())
        assert _val(argv, "--client-kind") == "wasm"
        assert _val(argv, "--wasm-branch-name") == "ant-cli-v0.3.8-rc.1"
        assert _val(argv, "--wasm-repo-owner") == "WithAutonomi"

    def test_a_resumable_bundle_build_precedes_the_deploy(self):
        # The ~10-minute part of the run, and the deploy fetches what it writes.
        steps = build_deploy_steps(_manifest())
        assert steps[0].argv[:2] == ["build", "ant-wasm"]
        assert _val(steps[0].argv, "--branch-name") == "ant-cli-v0.3.8-rc.1"

    def test_a_bootstrap_override_is_passed_through(self):
        argv = _deploy_argv(_manifest(wasm_bootstrap_multiaddrs_file="/tmp/ma.txt"))
        assert _val(argv, "--wasm-bootstrap-multiaddrs-file") == "/tmp/ma.txt"

    def test_a_medium_request_carries_the_cap_so_plan_shows_it(self):
        # An operator who wrote `medium` gets a narrower class than they asked
        # for, and that has to be visible before any money is spent.
        argv = _deploy_argv(_manifest(pool={"medium": 1}, volume_size=40))
        assert _val(argv, "--pool-max-size") == str(WASM_MAX_FILE_BYTES)

    def test_a_tighter_bound_of_the_operators_own_is_not_widened(self):
        argv = _deploy_argv(
            _manifest(pool={"medium": 1}, pool_max_size="200MB", volume_size=40)
        )
        assert _val(argv, "--pool-max-size") == "200MB"

    def test_tiny_and_small_get_no_redundant_bound(self):
        # Both classes are wholly below the cap, and the argv is what the step
        # journal compares, so noise there is not free.
        argv = _deploy_argv(_manifest(pool={"tiny": 2, "small": 1}))
        assert "--pool-max-size" not in argv


class TestTheCliAcceptsWhatTheOrchestratorEmits:
    def _parse(self, monkeypatch, argv):
        from saorsa_deploy.main import main

        captured = {}
        monkeypatch.setattr(
            cli,
            "cmd_prod_continuous_uploader_deploy",
            lambda args: captured.update(vars(args)),
        )
        main(argv)
        return captured

    def test_the_emitted_argv_parses(self, monkeypatch):
        argv = _deploy_argv(_manifest(pool={"medium": 1}, volume_size=40))
        parsed = self._parse(monkeypatch, argv)
        assert parsed["client_kind"] == "wasm"
        assert parsed["wasm_branch_name"] == "ant-cli-v0.3.8-rc.1"
        assert parsed["pool_max_size"] == str(WASM_MAX_FILE_BYTES)

    def test_the_transport_defaults_to_native(self, monkeypatch):
        parsed = self._parse(
            monkeypatch,
            _deploy_argv(_manifest(client_kind=None, wasm_binary=None, spec="lon1")),
        )
        assert parsed["client_kind"] == "native"
        # None, not "info", so an EXPLICIT --log-level can be refused on the
        # browser path. Every consumer resolves None to "info".
        assert parsed["log_level"] is None


class TestTheCliRefusesWhatCannotWork:
    def _args(self, **overrides):
        args = SimpleNamespace(
            client_kind="wasm",
            bootstrap_cache=True,
            log_level=None,
            pool_max_size=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({"bootstrap_cache": False}, "--no-bootstrap-cache"),
            ({"log_level": "debug"}, "--log-level"),
        ],
    )
    def test_the_flags_with_no_browser_meaning_exit(self, overrides, expected, capsys):
        with pytest.raises(SystemExit):
            cli._check_wasm_options(self._args(**overrides), Console())
        assert expected in capsys.readouterr().out

    def test_a_native_deploy_is_never_checked(self):
        cli._check_wasm_options(
            self._args(client_kind="native", bootstrap_cache=False, log_level="debug"),
            Console(),
        )

    def test_an_undrawable_pool_request_exits_before_any_infrastructure(self, capsys):
        with pytest.raises(SystemExit):
            cli._apply_wasm_pool_cap(self._args(), {"large": 1}, Console())
        assert "No infrastructure was created" in capsys.readouterr().out

    def test_a_medium_request_is_clamped(self):
        args = self._args()
        cli._apply_wasm_pool_cap(args, {"medium": 1}, Console())
        assert args.pool_max_size == str(WASM_MAX_FILE_BYTES)

    def test_a_native_pool_request_is_untouched(self):
        args = self._args(client_kind="native")
        cli._apply_wasm_pool_cap(args, {"large": 1}, Console())
        assert args.pool_max_size is None


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _provisioner(**overrides):
    kwargs = dict(
        hosts=[{"ip": "1.1.1.1", "region": "lon1", "provider": "digitalocean"}],
        deployment_id=7,
        deployment_name="PROD-WUL-01",
        pool_database_url="postgresql://scoped@host/db",
        bootstrap_peers=["10.0.0.1:12000"],
        pool_request={"tiny": 2},
        wallet_secret_key="ab" * 32,
        influx_url="http://influx:8086",
        influx_token="tok",
    )
    kwargs.update(overrides)
    return ProdContinuousUploaderProvisioner(**kwargs)


class TestProvisioner:
    def test_a_native_uploader_env_is_byte_identical(self):
        env = _provisioner()._per_host_env(_provisioner().hosts[0])
        for key in ("CLIENT_KIND", "ANT_WASM", "BOOTSTRAP_MULTIADDRS", "RPC_URL"):
            assert key not in env
        assert env["INFLUX_BUCKET"] == "testnet-metrics"

    def test_a_wasm_uploader_gets_the_cli_and_the_bootstraps(self):
        env = _provisioner(
            client_kind="wasm",
            wasm_bundle_url="https://s3/bundle.tar.gz",
            bootstrap_multiaddrs=[MA],
        )._per_host_env(
            {"ip": "1.1.1.1", "region": "lon1", "provider": "digitalocean"}
        )
        assert env["CLIENT_KIND"] == "wasm"
        assert env["ANT_WASM"] == "/usr/local/bin/ant-wasm"
        assert env["BOOTSTRAP_MULTIADDRS"] == MA
        assert env["MAX_UPLOAD_BYTES"] == WASM_MAX_FILE_BYTES
        # Payments sign with viem against a plain JSON-RPC endpoint, so the
        # chain is named by URL rather than by --evm-network.
        assert "arbitrum" in env["RPC_URL"]

    def test_wasm_points_go_to_their_own_bucket(self):
        # The same `prod_upload` measurement the native tier writes, so one
        # bucket would let any panel average two transports measured several
        # times apart.
        env = _provisioner(
            client_kind="wasm",
            wasm_bundle_url="https://s3/bundle.tar.gz",
            bootstrap_multiaddrs=[MA],
        )._per_host_env(
            {"ip": "1.1.1.1", "region": "lon1", "provider": "digitalocean"}
        )
        assert env["INFLUX_BUCKET"] == "wasm-client-metrics"

    def test_the_wallet_key_is_still_0x_prefixed(self):
        env = _provisioner(
            client_kind="wasm",
            wasm_bundle_url="https://s3/bundle.tar.gz",
            bootstrap_multiaddrs=[MA],
        )._per_host_env(
            {"ip": "1.1.1.1", "region": "lon1", "provider": "digitalocean"}
        )
        assert env["SECRET_KEY"].startswith("0x")

    def test_a_bundle_url_is_mandatory(self):
        with pytest.raises(RuntimeError, match="WASM bundle URL"):
            _provisioner(client_kind="wasm", bootstrap_multiaddrs=[MA])

    def test_a_bootstrap_multiaddr_is_mandatory(self):
        # Without one a browser client cannot reach the network at all, and the
        # failure would surface only as an unexplained connection error hours
        # later, on a service that sleeps between cycles.
        with pytest.raises(RuntimeError, match="bootstrap WebRTC"):
            _provisioner(client_kind="wasm", wasm_bundle_url="https://s3/b.tar.gz")

    def test_the_verifier_never_takes_the_transport(self):
        # A digest_mismatch has to stay attributable to the browser-path UPLOAD,
        # so the independent verifiers are native whatever the uploader runs.
        verifier = ProdUploadVerifierProvisioner(
            hosts=[{"ip": "2.2.2.2", "region": "nyc3", "provider": "digitalocean"}],
            deployment_id=7,
            deployment_name="PROD-WUL-01",
            pool_database_url="postgresql://scoped@host/db",
            bootstrap_peers=["10.0.0.1:12000"],
        )
        env = verifier._per_host_env(verifier.hosts[0])
        assert "CLIENT_KIND" not in env
        assert env["ANT"] == "/root/.local/bin/ant"
        assert env["INFLUX_BUCKET"] == "testnet-metrics"

    def _op_names(self, provisioner):
        names = []
        with (
            patch(
                "saorsa_deploy.provisioning.prod_continuous_uploader.add_op",
                side_effect=lambda state, op, **kw: names.append(kw.get("name")),
            ),
            patch(
                "saorsa_deploy.provisioning.prod_continuous_uploader.Inventory"
            ),
            patch("saorsa_deploy.provisioning.prod_continuous_uploader.State"),
            patch("saorsa_deploy.provisioning.prod_continuous_uploader.connect_all"),
            patch("saorsa_deploy.provisioning.prod_continuous_uploader.run_ops"),
            patch(
                "saorsa_deploy.provisioning.prod_continuous_uploader"
                ".add_operator_authorized_keys_op"
            ),
            patch(
                "saorsa_deploy.provisioning.saorsa_db_install.SaorsaDbInstaller.add_ops"
            ),
        ):
            provisioner.execute()
        return names

    def test_a_wasm_uploader_installs_node_and_the_bundle_but_not_ant(self):
        names = self._op_names(
            _provisioner(
                client_kind="wasm",
                wasm_bundle_url="https://s3/bundle.tar.gz",
                bootstrap_multiaddrs=[MA],
            )
        )
        assert "Install Node.js 22" in names
        assert "Install WASM client bundle" in names
        assert "Install ant-wasm CLI" in names
        # Nothing on this VM runs `ant`, not even the read-back, and a WASM
        # deployment may have no `ant` build to fetch at all.
        assert "Download and install ant binary" not in names

    def test_a_native_uploader_installs_ant_and_no_node(self):
        names = self._op_names(_provisioner(binary_url="https://s3/ant.tar.gz"))
        assert "Download and install ant binary" in names
        assert not [n for n in names if "Node" in (n or "") or "WASM" in (n or "")]


# ---------------------------------------------------------------------------
# The service's argv
# ---------------------------------------------------------------------------


class TestServiceArgv:
    def test_the_wasm_upload_argv_has_no_evm_network_or_bootstrap(self, service):
        # The bootstraps come from BOOTSTRAP_MULTIADDRS and the payment identity
        # from a node's own HELLO.
        argv = service._build_wasm_upload_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            payment_type="",
            file_path=Path("/tmp/a.bin"),
            payment_marker_path=Path("/tmp/a.paid"),
        )
        assert argv[0] == "/usr/local/bin/ant-wasm"
        assert argv[1:5] == ["file", "upload", "--public", "--payment-marker"]
        assert "--evm-network" not in argv
        assert "--bootstrap" not in argv
        assert not [a for a in argv if a.startswith("-v")]

    def test_the_payment_mode_is_still_honoured(self, service):
        argv = service._build_wasm_upload_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            payment_type="no-merkle",
            file_path=Path("/tmp/a.bin"),
            payment_marker_path=Path("/tmp/a.paid"),
        )
        assert "--no-merkle" in argv

    def test_the_native_upload_argv_is_unchanged(self, service, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_CACHE", "true")
        argv = service._build_native_upload_argv(
            ant_path="/root/.local/bin/ant",
            evm_network="arbitrum-one",
            log_level="info",
            payment_type="merkle",
            file_path=Path("/tmp/a.bin"),
        )
        assert argv == [
            "/root/.local/bin/ant", "--evm-network", "arbitrum-one", "--json", "-v",
            "file", "upload", "--public", "--merkle", "/tmp/a.bin",
        ]

    def test_the_wasm_readback_argv_uses_ant_wasm(self, service):
        argv = service._build_wasm_download_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            address="abc",
            output_path=Path("/tmp/out"),
        )
        assert argv == [
            "/usr/local/bin/ant-wasm", "file", "download", "abc", "-o", "/tmp/out",
        ]

    def test_the_native_readback_argv_is_unchanged(self, service):
        argv = service._build_native_download_argv(
            ant_path="/root/.local/bin/ant",
            evm_network="arbitrum-one",
            log_level="debug",
            address="abc",
            output_path=Path("/tmp/out"),
        )
        assert argv == [
            "/root/.local/bin/ant", "--evm-network", "arbitrum-one", "-vv",
            "file", "download", "abc", "-o", "/tmp/out",
        ]

    def test_the_verifier_service_is_still_native(self):
        # A separate script entirely — it spawns `ant` and nothing else, which is
        # what keeps a digest_mismatch attributable to the upload.
        verifier = (
            SCRIPT_PATH.parent / "prod_upload_verifier.py"
        ).read_text()
        assert "ant-wasm" not in verifier
        assert "CLIENT_KIND" not in verifier


class TestSuccessClassification:
    def test_an_ant_wasm_success_payload_is_a_success(self, service):
        # `ant-wasm` ALWAYS emits `error`, set to null on success, where
        # `ant --json` omits the key. Keying on presence reported every healthy
        # browser-path upload as a partial — which SPENDS the file's pool claim
        # as though only some of its chunks had been stored.
        assert service._upload_succeeded(
            {"address": "abc", "chunks_stored": 4, "error": None}, 0
        )

    def test_an_error_value_is_a_failure(self, service):
        assert not service._upload_succeeded({"error": "no quotes"}, 0)

    def test_a_non_zero_exit_is_a_failure(self, service):
        assert not service._upload_succeeded({"address": "abc"}, 1)

    def test_no_document_is_a_failure(self, service):
        assert not service._upload_succeeded(None, 0)


class TestWasmFailureOutcome:
    def test_a_reported_payment_means_partial(self, service, tmp_path):
        assert (
            service._wasm_failure_outcome({"payment_sent": True}, tmp_path / "none")
            == service.STATUS_PARTIAL_UPLOAD
        )

    def test_the_marker_alone_means_partial(self, service, tmp_path):
        # The only surviving evidence when an OOM kills the child after the money
        # went out but before it printed anything.
        marker = tmp_path / "a.paid"
        marker.write_text("paid\n")
        assert (
            service._wasm_failure_outcome(None, marker)
            == service.STATUS_PARTIAL_UPLOAD
        )

    def test_neither_means_released(self, service, tmp_path):
        assert (
            service._wasm_failure_outcome(
                {"payment_sent": False}, tmp_path / "none"
            )
            == service.STATUS_FAILED
        )


# ---------------------------------------------------------------------------
# The service's cycle, against a real registry
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield (
        SqlAlchemyDeploymentRepository(session),
        SqlAlchemyProdFilePoolRepository(session),
        session,
    )
    session.close()
    engine.dispose()


class TestOneCycle:
    """One cycle end to end with a fake transfer binary and a real registry."""

    def _run(
        self,
        service,
        registry,
        monkeypatch,
        tmp_path,
        *,
        result,
        exit_code=None,
        write_marker=False,
        wasm=True,
        readback=None,
        readback_stdout="",
    ):
        import json as _json

        repo, pool_repo, session = registry
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        locator = "https://example.com/a.bin"
        # Not claimed here: this kind claims per cycle, on the VM, which is
        # exactly the path under test.
        pool_file, _ = pool_repo.add_pool_file(
            PoolFileKind.URL, locator, "a.bin", size_bytes=5 * MIB
        )

        monkeypatch.setattr(
            service,
            "_open_repositories",
            lambda url: (pool_repo, repo, session),
        )
        posted = []
        monkeypatch.setattr(
            service,
            "_post_influx",
            lambda url, token, org, bucket, line: posted.append((bucket, line)),
        )
        monkeypatch.setattr(
            service,
            "_download",
            lambda url, dest, timeout, **kw: (
                dest.write_bytes(b"x" * (5 * MIB)),
                (5 * MIB, "a" * 64),
            )[1],
        )

        spawned = []

        def _fake_run(cmd, secret_key, log_base, timeout):
            spawned.append(cmd)
            if "download" in cmd:
                out = cmd[cmd.index("-o") + 1]
                Path(out).write_bytes(b"x" * (5 * MIB))
                return (
                    readback if readback is not None else 0,
                    readback_stdout,
                    "",
                )
            if write_marker:
                Path(cmd[cmd.index("--payment-marker") + 1]).write_text("paid\n")
            stdout = _json.dumps(result) if result is not None else ""
            code = exit_code
            if code is None:
                code = 0 if result and not result.get("error") else 1
            return code, stdout, ""

        monkeypatch.setattr(service, "_run_ant", _fake_run)
        # One cycle, then out: the loop is `while True`, so the sleep is what
        # ends the test.
        monkeypatch.setattr(
            service, "LOG_DIR", str(tmp_path / "logs")
        )
        monkeypatch.setattr(
            service.time, "sleep", lambda secs: (_ for _ in ()).throw(SystemExit(0))
        )
        monkeypatch.setattr(
            service, "_sha256_file", lambda path: "a" * 64
        )

        env = {
            "DEPLOYMENT_ID": str(deployment.id),
            "DEPLOYMENT_NAME": "PROD-WUL-01",
            "POOL_DATABASE_URL": "postgresql://scoped@host/db",
            "POOL_REQUEST": '{"tiny": 1}',
            "REGION": "lon1",
            "SECRET_KEY": "0x" + "11" * 32,
            "WORK_DIR": str(tmp_path / "files"),
            "INFLUX_URL": "http://influx:8086",
            "INFLUX_TOKEN": "tok",
            "INFLUX_BUCKET": "wasm-client-metrics" if wasm else "testnet-metrics",
            "SLEEP_INTERVAL_SECS": "600",
        }
        if wasm:
            env["CLIENT_KIND"] = "wasm"
            env["ANT_WASM"] = "/usr/local/bin/ant-wasm"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(SystemExit):
            service.main()

        return SimpleNamespace(
            spawned=spawned,
            posted=posted,
            claim=pool_repo.list_pool_claims(file_id=pool_file.id)[0],
            rows=repo.list_prod_upload_results_for_deployment(deployment.id),
            verifications=repo.list_prod_upload_verifications_for_deployment(
                deployment.id
            ),
        )

    _SUCCESS = {
        "address": "abc",
        "chunks_stored": 4,
        "chunks_failed": 0,
        "total_chunks": 4,
        "elapsed_secs": 12.5,
        "payment_sent": True,
        "error": None,
        "error_kind": None,
    }

    def test_a_success_spends_the_claim_and_labels_the_transport(
        self, service, registry, monkeypatch, tmp_path
    ):
        out = self._run(
            service, registry, monkeypatch, tmp_path, result=self._SUCCESS
        )
        assert out.claim.status is ClaimStatus.UPLOADED
        assert len(out.rows) == 1
        assert out.rows[0].status is ProdUploadStatus.SUCCESS
        assert out.rows[0].client_kind == "wasm"
        # The hash of the bytes handed to the uploader, which is what every
        # later verification compares against.
        assert out.rows[0].sha256 == "a" * 64

    def test_the_upload_and_the_readback_both_run_ant_wasm(
        self, service, registry, monkeypatch, tmp_path
    ):
        out = self._run(
            service, registry, monkeypatch, tmp_path, result=self._SUCCESS
        )
        assert [cmd[0] for cmd in out.spawned] == [
            "/usr/local/bin/ant-wasm",
            "/usr/local/bin/ant-wasm",
        ]
        # The uploader VM's own view as a browser user.
        assert out.verifications[0].status.value == "ok"

    def test_points_go_to_the_wasm_bucket(
        self, service, registry, monkeypatch, tmp_path
    ):
        out = self._run(
            service, registry, monkeypatch, tmp_path, result=self._SUCCESS
        )
        assert {bucket for bucket, _ in out.posted} == {"wasm-client-metrics"}

    def test_a_failure_before_payment_releases_the_claim(
        self, service, registry, monkeypatch, tmp_path
    ):
        # Nothing reached the network, so burning the entry would be a lie.
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result={"error": "no quotes", "payment_sent": False},
        )
        assert out.claim.status is ClaimStatus.RELEASED
        assert out.rows[0].status is ProdUploadStatus.FAILED

    def test_a_failure_after_payment_marks_the_claim_partial(
        self, service, registry, monkeypatch, tmp_path
    ):
        # Chunks may be in production, so a canary must never draw the file.
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result={"error": "store failed", "payment_sent": True},
        )
        assert out.claim.status is ClaimStatus.PARTIAL
        assert out.rows[0].status is ProdUploadStatus.PARTIAL_UPLOAD

    def test_a_crash_after_the_marker_is_a_partial(
        self, service, registry, monkeypatch, tmp_path
    ):
        # The OOM killer reaches the WASM child, not this parent, and the child
        # prints nothing — so the marker is the only evidence left.
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result=None,
            exit_code=-9,
            write_marker=True,
        )
        assert out.claim.status is ClaimStatus.PARTIAL
        assert 'error_kind="crash"' in out.posted[0][1]

    def test_a_crash_with_no_marker_releases_the_claim(
        self, service, registry, monkeypatch, tmp_path
    ):
        out = self._run(
            service, registry, monkeypatch, tmp_path, result=None, exit_code=-9
        )
        assert out.claim.status is ClaimStatus.RELEASED
        assert 'error_kind="crash"' in out.posted[0][1]

    def test_the_native_no_document_rule_is_unchanged(
        self, service, registry, monkeypatch, tmp_path
    ):
        # `rc != 0` with no result document still means ant never started, so the
        # file goes back in the pool — recorded as a partial upload, exactly as
        # before the browser path existed.
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result=None,
            exit_code=1,
            wasm=False,
        )
        assert out.claim.status is ClaimStatus.RELEASED
        assert out.rows[0].status is ProdUploadStatus.PARTIAL_UPLOAD
        assert out.rows[0].client_kind == "native"
        assert out.spawned[0][0] == "/root/.local/bin/ant"

    def test_a_native_partial_still_has_no_error_kind(
        self, service, registry, monkeypatch, tmp_path
    ):
        # Only the browser path reports one, so a native point's field set is
        # exactly what it was.
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result={"error": "store failed", "chunks_stored": 2, "total_chunks": 4},
            wasm=False,
        )
        assert out.claim.status is ClaimStatus.PARTIAL
        assert "error_kind" not in out.posted[0][1]

    def test_the_build_that_ran_reaches_both_tables_and_both_points(
        self, service, registry, monkeypatch, tmp_path
    ):
        # V2-1346: `deployments upgrade-wasm-client` replaces the bundle under
        # this very service, and `deploy_args.wasm_build` records only the
        # CURRENT build — so which build performed a given cycle has to travel
        # with the row, not with the deployment.
        import json as _json

        commit = "aaaa1111bbbb2222cccc3333"
        out = self._run(
            service,
            registry,
            monkeypatch,
            tmp_path,
            result={**self._SUCCESS, "client_build": commit},
            readback_stdout=_json.dumps({"client_build": commit}),
        )
        assert out.rows[0].client_build == commit
        assert out.verifications[0].client_build == commit
        # The two transfer measurements carry it; the per-cycle heartbeat does
        # not, because it describes a cycle rather than an operation.
        transfers = [
            line for _, line in out.posted
            if line.startswith(("prod_upload,", "prod_upload_verify,"))
        ]
        assert len(transfers) == 2
        assert all(f'client_build="{commit}"' in line for line in transfers)

    def test_a_native_cycle_records_no_build(
        self, service, registry, monkeypatch, tmp_path
    ):
        # The native `ant` reports none, so the rows stay NULL and the points
        # keep exactly the field set they had.
        out = self._run(
            service, registry, monkeypatch, tmp_path, result=self._SUCCESS, wasm=False
        )
        assert out.rows[0].client_build is None
        assert out.verifications[0].client_build is None
        assert all("client_build" not in line for _, line in out.posted)


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


class TestInventory:
    def _config(self, **overrides):
        args = {
            "kind": "prod-continuous-uploader",
            "spec": "lon1:s-4vcpu-8gb",
            "pool_request": {"tiny": 2},
            "bootstrap_cache": True,
            "log_level": "info",
        }
        args.update(overrides)
        return build_client_config(args)

    def test_a_native_run_reports_native_and_no_wasm_paths(self):
        config = self._config()
        assert config["client_kind"] == "native"
        assert config["wasm_build"] is None
        assert config["paths"]["ant_wasm_cli"] is None
        assert config["settings"]["bootstrap_cache"] is True

    def test_a_wasm_run_carries_the_bundle_and_the_probe(self):
        # Neither is reconstructable afterwards: the bundle is overwritten per
        # deployment name and the probe was a moment in time.
        config = self._config(
            client_kind="wasm",
            wasm_build={"commit": "abc123", "ref": "ant-cli-v0.3.8-rc.1"},
            wasm_bootstrap={"total": 7, "answered": 7},
        )
        assert config["client_kind"] == "wasm"
        assert config["wasm_build"]["commit"] == "abc123"
        assert config["wasm_bootstrap"]["answered"] == 7
        assert config["paths"]["ant_wasm_cli"] == "/usr/local/bin/ant-wasm"
        assert config["paths"]["ant_wasm_bundle_dir"] == "/opt/ant-wasm-client"
        # The verifiers run `ant` whichever transport uploaded.
        assert config["paths"]["ant_binary"] == "/root/.local/bin/ant"

    def test_the_refused_knobs_read_as_unset_on_a_wasm_run(self):
        # A value there would describe a knob that had no effect on the run.
        config = self._config(client_kind="wasm")
        assert config["settings"]["bootstrap_cache"] is None
        assert config["settings"]["log_level"] is None
