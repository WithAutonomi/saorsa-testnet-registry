"""WASM (browser-path) uploaders on the one-shot `prod-uploaders` kind (V2-1334).

Unlike the download kinds, the transport here is per-UPLOADER: a native uploader
and a browser-path uploader in one deployment share the wallet and the monitoring
stack, so the two are measured against the same network at the same time. Their
results have to stay separate at every layer, and everything that only makes
sense for the `ant` process has to be refused rather than ignored.

Covered: manifest validation, the argv the orchestrator emits, the pool draw's
browser cap, the provisioner's per-host install and env output, the service's
transport dispatch and claim-outcome classification, the recording path's
two-bucket read, and the invariant that a manifest with no WASM uploader renders
byte-identically to before.
"""

import argparse
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pydantic
import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus, PoolFileKind
from saorsa_db.models.prod_upload_result import ProdUploadStatus
from saorsa_db.repositories import (
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from saorsa_deploy import pool_selection
from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES
from saorsa_deploy.cmd import prod_uploaders
from saorsa_deploy.cmd.prod_uploaders import (
    parse_prod_uploader_spec,
    parse_prod_wasm_uploader_spec,
)
from saorsa_deploy.cmd.record_results import cmd_record_results
from saorsa_deploy.config.models import Manifest, ProdUploadersSpec
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.prod_uploader import ProdUploaderProvisioner

MIB = 1024 * 1024

SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts/prod_uploader.py"
)

MA = (
    "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiAaaa/p2p/"
    "57a89f84e1652d22fd6b791c29d83f187fbed3ba8e08e265fe3031b3a6f725da"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_uploader", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(**uploaders_fields):
    block = {"binary": {"version": "0.3.8"}}
    block.update(uploaders_fields)
    return Manifest.model_validate(
        {
            "name": "PROD-WASM-UL-01",
            "kind": "prod-uploaders",
            "purpose": "browser-path upload measurement",
            "ssh_key_path": "/ssh/key",
            "prod_uploaders": block,
        }
    )


def _wasm_block(**overrides):
    block = {
        "wasm_binary": {
            "branch": "ant-cli-v0.3.8-rc.1",
            "repo_owner": "WithAutonomi",
        },
        "wasm_uploaders": [{"region": "lon1", "pool": {"tiny": 2}}],
    }
    block.update(overrides)
    return block


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_native_only_needs_nothing_new(self):
        spec = ProdUploadersSpec(
            binary={"version": "0.3.8"},
            uploaders=[{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/tmp/u.txt"}],
        )
        assert spec.wasm_uploaders == []
        assert spec.wasm_binary is None

    def test_a_wasm_uploader_needs_a_bundle_source(self):
        # The bundle is always built from source — there is no release form — so
        # without this the deploy would have nothing to install.
        with pytest.raises(pydantic.ValidationError, match="wasm_binary is required"):
            ProdUploadersSpec(
                binary={"version": "0.3.8"},
                wasm_uploaders=[{"region": "lon1", "pool": {"tiny": 1}}],
            )

    def test_a_bundle_source_without_a_wasm_uploader_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="only valid with"):
            ProdUploadersSpec(
                binary={"version": "0.3.8"},
                uploaders=[{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/u"}],
                wasm_binary={"branch": "main"},
            )

    def test_a_wasm_only_deployment_is_allowed(self):
        spec = ProdUploadersSpec(**_wasm_block(binary={"version": "0.3.8"}))
        assert spec.uploaders == []
        assert len(spec.wasm_uploaders) == 1

    def test_no_uploader_at_all_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="at least one uploader is required"):
            ProdUploadersSpec(binary={"version": "0.3.8"})

    @pytest.mark.parametrize("size_class", ["large", "xlarge"])
    def test_pool_classes_above_the_browser_cap_are_refused(self, size_class):
        # Every file in those classes is above ant-core's 1,000,000,000-byte
        # browser cap, so each drawn file would be a permanently-spent pool entry
        # that could never be uploaded.
        with pytest.raises(pydantic.ValidationError, match="browser path"):
            ProdUploadersSpec(
                **_wasm_block(
                    binary={"version": "0.3.8"},
                    wasm_uploaders=[{"region": "lon1", "pool": {size_class: 1}}],
                )
            )

    def test_a_medium_request_is_allowed_because_it_is_clamped(self):
        spec = ProdUploadersSpec(
            **_wasm_block(
                binary={"version": "0.3.8"},
                wasm_uploaders=[{"region": "lon1", "pool": {"medium": 2}}],
            )
        )
        assert spec.wasm_uploaders[0].pool == {"medium": 2}

    @pytest.mark.parametrize("option", ["bootstrap_cache", "memory_profiling", "log_level"])
    def test_options_with_no_browser_path_meaning_are_refused(self, option):
        # Refused rather than ignored: a silently-dropped option would leave the
        # operator reading their own manifest as a description of the run.
        value = "debug" if option == "log_level" else False
        with pytest.raises(pydantic.ValidationError, match="Extra inputs"):
            ProdUploadersSpec(
                **_wasm_block(
                    binary={"version": "0.3.8"},
                    wasm_uploaders=[{"region": "lon1", "pool": {"tiny": 1}, option: value}],
                )
            )

    def test_an_undersized_digitalocean_vm_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="at least 8 GB"):
            ProdUploadersSpec(
                **_wasm_block(
                    binary={"version": "0.3.8"},
                    wasm_uploaders=[
                        {
                            "region": "lon1",
                            "vm_size": "s-2vcpu-4gb",
                            "pool": {"tiny": 1},
                        }
                    ],
                )
            )

    def test_a_missing_vm_size_is_allowed_and_defaults_later(self):
        spec = ProdUploadersSpec(**_wasm_block(binary={"version": "0.3.8"}))
        assert spec.wasm_uploaders[0].vm_size is None


# ---------------------------------------------------------------------------
# The argv the orchestrator emits
# ---------------------------------------------------------------------------


class TestOrchestratorArgv:
    def test_a_native_only_manifest_is_byte_identical(self):
        steps = build_deploy_steps(
            _manifest(
                uploaders=[
                    {
                        "region": "lon1",
                        "vm_size": "s-2vcpu-4gb",
                        "urls": "/tmp/u.txt",
                    }
                ]
            )
        )
        assert [s.argv for s in steps] == [
            [
                "prod",
                "uploaders",
                "deploy",
                "--name",
                "PROD-WASM-UL-01",
                "--purpose",
                "browser-path upload measurement",
                "--uploader",
                "lon1:s-2vcpu-4gb:urls=/tmp/u.txt",
                "--ant-version",
                "0.3.8",
                "--ssh-key-path",
                "/ssh/key",
            ]
        ]

    def test_a_wasm_uploader_prepends_a_resumable_build_step(self):
        steps = build_deploy_steps(_manifest(**_wasm_block()))
        assert steps[0].argv[:2] == ["build", "ant-wasm"]
        assert "--branch-name" in steps[0].argv
        assert steps[0].argv[steps[0].argv.index("--repo-owner") + 1] == ("WithAutonomi")

    def test_the_deploy_step_carries_the_wasm_spec_and_bundle_flags(self):
        argv = build_deploy_steps(_manifest(**_wasm_block()))[1].argv
        assert argv[argv.index("--wasm-uploader") + 1] == "lon1:pool=tiny:2"
        assert argv[argv.index("--wasm-branch-name") + 1] == "ant-cli-v0.3.8-rc.1"
        assert argv[argv.index("--wasm-repo-owner") + 1] == "WithAutonomi"
        # No --client-kind: the transport is per-uploader here, carried by which
        # flag each entry arrived on.
        assert "--client-kind" not in argv

    def test_an_explicit_vm_size_reaches_the_spec(self):
        argv = build_deploy_steps(
            _manifest(
                **_wasm_block(
                    wasm_uploaders=[
                        {
                            "region": "lon1",
                            "vm_size": "s-8vcpu-16gb",
                            "pool": {"tiny": 1},
                        }
                    ]
                )
            )
        )[1].argv
        assert argv[argv.index("--wasm-uploader") + 1] == ("lon1:s-8vcpu-16gb:pool=tiny:1")

    def test_a_medium_request_shows_its_clamp_in_the_plan(self):
        # An operator who wrote `medium` gets a narrower class than they asked
        # for, so the narrowed bound has to be visible before any money is spent.
        argv = build_deploy_steps(
            _manifest(**_wasm_block(wasm_uploaders=[{"region": "lon1", "pool": {"medium": 2}}]))
        )[1].argv
        spec = argv[argv.index("--wasm-uploader") + 1]
        assert f"pool_max_size={WASM_MAX_FILE_BYTES}" in spec

    def test_a_tighter_user_bound_wins_over_the_clamp(self):
        argv = build_deploy_steps(
            _manifest(
                **_wasm_block(
                    wasm_uploaders=[
                        {
                            "region": "lon1",
                            "pool": {"medium": 2},
                            "pool_max_size": "200MB",
                        }
                    ]
                )
            )
        )[1].argv
        spec = argv[argv.index("--wasm-uploader") + 1]
        assert "pool_max_size=200MB" in spec

    def test_a_sub_cap_class_gains_no_redundant_bound(self):
        # The argv is what the step journal compares, so a token that narrows
        # nothing is not free.
        argv = build_deploy_steps(_manifest(**_wasm_block()))[1].argv
        assert "pool_max_size" not in argv[argv.index("--wasm-uploader") + 1]

    def test_rust_log_is_the_last_token(self):
        argv = build_deploy_steps(
            _manifest(
                **_wasm_block(
                    wasm_uploaders=[
                        {
                            "region": "lon1",
                            "pool": {"tiny": 1},
                            "rust_log": "a::b=debug",
                            "forward_logs": True,
                        }
                    ]
                )
            )
        )[1].argv
        spec = argv[argv.index("--wasm-uploader") + 1]
        assert spec.endswith(":rust_log=a::b=debug")

    def test_a_mixed_manifest_emits_both_flags(self):
        argv = build_deploy_steps(
            _manifest(
                **_wasm_block(
                    uploaders=[
                        {
                            "region": "lon1",
                            "vm_size": "s-2vcpu-4gb",
                            "urls": "/tmp/u.txt",
                        }
                    ]
                )
            )
        )[1].argv
        assert argv.count("--uploader") == 1
        assert argv.count("--wasm-uploader") == 1

    def test_the_emitted_argv_parses(self, monkeypatch):
        # A flag the orchestrator emits but no parser accepts would otherwise
        # fail only against real infrastructure.
        from saorsa_deploy.main import main

        captured = {}
        monkeypatch.setattr(
            prod_uploaders,
            "cmd_prod_uploaders_deploy",
            lambda args: captured.update(vars(args)),
        )
        steps = build_deploy_steps(
            _manifest(
                **_wasm_block(
                    uploaders=[
                        {
                            "region": "lon1",
                            "vm_size": "s-2vcpu-4gb",
                            "urls": "/tmp/u.txt",
                        }
                    ]
                )
            )
        )
        main(steps[1].argv)
        assert captured["wasm_uploader"] == ["lon1:pool=tiny:2"]
        assert captured["wasm_branch_name"] == "ant-cli-v0.3.8-rc.1"
        assert captured["wasm_repo_owner"] == "WithAutonomi"
        # And the spec round-trips through the parser the deploy command uses.
        assert parse_prod_wasm_uploader_spec(captured["wasm_uploader"][0])["client_kind"] == "wasm"


# ---------------------------------------------------------------------------
# The --wasm-uploader spec parser
# ---------------------------------------------------------------------------


class TestSpecParser:
    def test_vm_size_is_optional_and_defaults(self):
        assert parse_prod_wasm_uploader_spec("lon1:pool=tiny:2")["vm_size"] == ("s-4vcpu-8gb")

    def test_an_explicit_vm_size_is_kept(self):
        spec = parse_prod_wasm_uploader_spec("lon1:s-8vcpu-16gb:pool=tiny:2")
        assert spec["vm_size"] == "s-8vcpu-16gb"

    def test_an_undersized_vm_is_refused(self):
        with pytest.raises(ValueError, match="at least 8 GB"):
            parse_prod_wasm_uploader_spec("lon1:s-2vcpu-4gb:pool=tiny:2")

    @pytest.mark.parametrize(
        "token",
        [
            "bootstrap_cache=false",
            "memory_profiling=true",
            "log_level=debug",
        ],
    )
    def test_forbidden_tokens_are_named_in_the_error(self, token):
        with pytest.raises(ValueError, match="cannot be used on a WASM uploader"):
            parse_prod_wasm_uploader_spec(f"lon1:pool=tiny:2:{token}")

    def test_the_browser_cap_narrows_a_medium_draw(self):
        spec = parse_prod_wasm_uploader_spec("lon1:pool=medium:2")
        assert spec["pool_max_size_bytes"] == WASM_MAX_FILE_BYTES

    def test_a_sub_cap_class_is_not_given_a_bound(self):
        spec = parse_prod_wasm_uploader_spec("lon1:pool=tiny:2")
        assert spec["pool_max_size_bytes"] is None

    @pytest.mark.parametrize("size_class", ["large", "xlarge"])
    def test_over_cap_classes_are_refused(self, size_class):
        with pytest.raises(ValueError, match="browser path"):
            parse_prod_wasm_uploader_spec(f"lon1:pool={size_class}:1")

    def test_urls_and_the_usual_tail_tokens_work(self):
        spec = parse_prod_wasm_uploader_spec(
            "lon1:urls=/tmp/u.txt:payment=merkle:clock_skew=2h:"
            "forward_logs=true:rust_log=a::b=debug"
        )
        assert spec["urls_file"] == "/tmp/u.txt"
        assert spec["payment_type"] == "merkle"
        assert spec["clock_skew"] == 7200
        assert spec["forward_logs"] is True
        assert spec["rust_log"] == "a::b=debug"

    def test_the_native_parser_still_marks_its_specs_native(self):
        spec = parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/tmp/u.txt")
        assert spec["client_kind"] == "native"
        assert spec["bootstrap_cache"] is True


# ---------------------------------------------------------------------------
# The pool draw
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


@pytest.fixture
def wired_pool(pool_repo, monkeypatch):
    store: dict = {}
    monkeypatch.setattr(pool_selection, "load_pool_selection", lambda name: store.get(name))
    monkeypatch.setattr(
        pool_selection,
        "save_pool_selection",
        lambda name, selection: store.__setitem__(name, selection),
    )
    monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: pool_repo)
    return pool_repo


class TestThePoolDrawRespectsTheCap:
    def test_an_over_cap_medium_file_is_never_drawn(self, wired_pool):
        # Both are `medium`, but only one can be uploaded over the browser path.
        # Drawing the other would permanently spend a pool entry on a file this
        # transport refuses before touching the network.
        wired_pool.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/i/under.bin",
            "under.bin",
            size_bytes=200 * MIB,
        )
        wired_pool.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/i/over.bin",
            "over.bin",
            size_bytes=WASM_MAX_FILE_BYTES + 1,
        )
        specs = [parse_prod_wasm_uploader_spec("lon1:pool=medium:1")]
        prod_uploaders._select_pool_files(
            argparse.Namespace(name="PROD-WASM-UL-01"), specs, Console()
        )
        assert "under.bin" in specs[0]["_urls_file_content"]
        assert "over.bin" not in specs[0]["_urls_file_content"]

    def test_a_native_uploader_may_still_draw_the_big_file(self, wired_pool):
        wired_pool.add_pool_file(
            PoolFileKind.URL,
            "https://archive.org/download/i/over.bin",
            "over.bin",
            size_bytes=WASM_MAX_FILE_BYTES + 1,
        )
        specs = [parse_prod_uploader_spec("lon1:s-2vcpu-4gb:pool=medium:1")]
        prod_uploaders._select_pool_files(
            argparse.Namespace(name="PROD-UL-NATIVE"), specs, Console()
        )
        assert "over.bin" in specs[0]["_urls_file_content"]

    def test_the_two_tiers_draw_into_separate_slots(self, wired_pool):
        for i in range(4):
            wired_pool.add_pool_file(
                PoolFileKind.URL,
                f"https://archive.org/download/i/f{i}.bin",
                f"f{i}.bin",
                size_bytes=5 * MIB,
            )
        specs = [
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:pool=tiny:2"),
            parse_prod_wasm_uploader_spec("nyc3:pool=tiny:2"),
        ]
        prod_uploaders._select_pool_files(
            argparse.Namespace(name="PROD-WASM-UL-01"), specs, Console()
        )
        native = set(specs[0]["_urls_file_content"].split())
        wasm = set(specs[1]["_urls_file_content"].split())
        assert len(native) == len(wasm) == 2
        # A file handed to one tier must never also be handed to the other.
        assert not native & wasm


# ---------------------------------------------------------------------------
# The provisioner
# ---------------------------------------------------------------------------


def _uploader(ip, region, index, client_kind="native"):
    return {
        "ip": ip,
        "region": region,
        "client_index": index,
        "client_kind": client_kind,
        "urls_file_content": f"https://example.com/{region}.bin\n",
    }


def _provisioner(uploaders, **overrides):
    kwargs = dict(
        uploaders=uploaders,
        bootstrap_peers=["1.2.3.4:5"],
        wallet_secret_key="abc",
        influx_url="http://influx:8086",
        influx_token="tok",
    )
    if any(u.get("client_kind") == "wasm" for u in uploaders):
        kwargs.update(
            wasm_bundle_url="https://s3/bundle.tar.gz",
            bootstrap_multiaddrs=[MA],
        )
    kwargs.update(overrides)
    return ProdUploaderProvisioner(**kwargs)


def _render_env(provisioner, monkeypatch):
    captured = []
    monkeypatch.setattr(
        "saorsa_deploy.provisioning.prod_uploader.add_op",
        lambda state, op, name=None, commands=None, **kw: captured.append(commands),
    )
    provisioner._add_env_and_urls_op(None)
    return captured[0][0]


class TestProvisioner:
    def test_a_wasm_uploader_needs_a_bundle_url(self):
        with pytest.raises(RuntimeError, match="WASM bundle URL"):
            ProdUploaderProvisioner(
                uploaders=[_uploader("1.1.1.1", "lon1", 1, "wasm")],
                bootstrap_peers=["1.2.3.4:5"],
                wallet_secret_key="abc",
                bootstrap_multiaddrs=[MA],
            )

    def test_a_wasm_uploader_needs_a_bootstrap_multiaddr(self):
        with pytest.raises(RuntimeError, match="bootstrap WebRTC multiaddr"):
            ProdUploaderProvisioner(
                uploaders=[_uploader("1.1.1.1", "lon1", 1, "wasm")],
                bootstrap_peers=["1.2.3.4:5"],
                wallet_secret_key="abc",
                wasm_bundle_url="https://s3/bundle.tar.gz",
            )

    def test_a_native_env_file_is_byte_identical(self, monkeypatch):
        # The whole point of gating every WASM line: an existing deployment must
        # render exactly what it rendered before this existed.
        env = _render_env(_provisioner([_uploader("1.1.1.1", "lon1", 1)]), monkeypatch)
        assert "CLIENT_KIND" not in env
        assert "ANT_WASM" not in env
        assert "MAX_UPLOAD_BYTES" not in env
        assert "HOST_INFLUX_BUCKET" not in env
        assert "INFLUX_BUCKET=testnet-metrics" in env

    def test_a_mixed_env_file_routes_each_host_to_its_own_bucket(self, monkeypatch):
        env = _render_env(
            _provisioner(
                [
                    _uploader("1.1.1.1", "lon1", 1),
                    _uploader("2.2.2.2", "nyc3", 1, "wasm"),
                ]
            ),
            monkeypatch,
        )
        assert 'CLIENT_KIND="native"; HOST_INFLUX_BUCKET="testnet-metrics"' in env
        assert 'CLIENT_KIND="wasm"; HOST_INFLUX_BUCKET="wasm-client-metrics"' in env
        assert "INFLUX_BUCKET=$HOST_INFLUX_BUCKET" in env

    def test_the_wasm_env_names_the_cli_bootstraps_cap_and_rpc(self, monkeypatch):
        env = _render_env(_provisioner([_uploader("1.1.1.1", "lon1", 1, "wasm")]), monkeypatch)
        assert "ANT_WASM=/usr/local/bin/ant-wasm" in env
        assert f'BOOTSTRAP_MULTIADDRS="{MA}"' in env
        assert f"MAX_UPLOAD_BYTES={WASM_MAX_FILE_BYTES}" in env
        # The browser path's payment callbacks sign with viem against a plain
        # JSON-RPC endpoint, so the chain is named by URL, not --evm-network.
        assert "RPC_URL=https://arb1.arbitrum.io/rpc" in env

    def test_the_fallback_case_arm_defines_every_variable(self, monkeypatch):
        # An unmatched IP must not write an env file with an empty bucket name.
        env = _render_env(_provisioner([_uploader("1.1.1.1", "lon1", 1, "wasm")]), monkeypatch)
        fallback = [line for line in env.splitlines() if line.startswith("    *)")]
        assert len(fallback) == 1
        assert "HOST_INFLUX_BUCKET=" in fallback[0]

    def _install_op_hosts(self, provisioner, monkeypatch, hosts):
        captured = []
        monkeypatch.setattr(
            "saorsa_deploy.provisioning.prod_uploader.add_op",
            lambda state, op, name=None, commands=None, **kw: captured.append(
                (name, kw.get("host"))
            ),
        )
        state = SimpleNamespace(inventory=SimpleNamespace(get_active_hosts=lambda: hosts))
        provisioner._add_install_wasm_ops(state)
        provisioner._add_install_ant_op(state)
        return captured

    def test_installs_are_targeted_per_transport(self, monkeypatch):
        native = SimpleNamespace(name="1.1.1.1")
        wasm = SimpleNamespace(name="2.2.2.2")
        ops = self._install_op_hosts(
            _provisioner(
                [
                    _uploader("1.1.1.1", "lon1", 1),
                    _uploader("2.2.2.2", "nyc3", 1, "wasm"),
                ]
            ),
            monkeypatch,
            [native, wasm],
        )
        by_name = dict(ops)
        assert by_name["Install WASM client bundle"] == [wasm]
        assert by_name["Install ant-wasm CLI"] == [wasm]
        assert by_name["Download and install ant binary"] == [native]

    def test_a_wasm_only_deployment_installs_no_ant(self, monkeypatch):
        wasm = SimpleNamespace(name="2.2.2.2")
        ops = self._install_op_hosts(
            _provisioner([_uploader("2.2.2.2", "nyc3", 1, "wasm")]),
            monkeypatch,
            [wasm],
        )
        assert "Download and install ant binary" not in dict(ops)

    def test_a_native_only_deployment_adds_no_wasm_op(self, monkeypatch):
        native = SimpleNamespace(name="1.1.1.1")
        ops = self._install_op_hosts(
            _provisioner([_uploader("1.1.1.1", "lon1", 1)]), monkeypatch, [native]
        )
        names = dict(ops)
        assert "Install WASM client bundle" not in names
        # And the ant install keeps addressing the whole inventory, as it always
        # did — `host=None` is what that means.
        assert names["Download and install ant binary"] is None


# ---------------------------------------------------------------------------
# The VM-side service
# ---------------------------------------------------------------------------


class TestServiceArgv:
    def test_the_wasm_argv_is_the_native_shape_plus_a_marker(self, service):
        argv = service._build_wasm_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            file_path=Path("/data/f.bin"),
            payment_type="no-merkle",
            payment_marker_path=Path("/var/log/op.paid"),
        )
        assert argv == [
            "/usr/local/bin/ant-wasm",
            "file",
            "upload",
            "--public",
            "--no-merkle",
            "--payment-marker",
            "/var/log/op.paid",
            "/data/f.bin",
        ]

    def test_the_wasm_argv_names_no_chain_and_no_bootstrap(self, service):
        argv = service._build_wasm_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            file_path=Path("/data/f.bin"),
            payment_type=None,
            payment_marker_path=Path("/var/log/op.paid"),
        )
        # The payment identity comes from a node's own HELLO, and the bootstraps
        # come from BOOTSTRAP_MULTIADDRS in the environment.
        assert "--evm-network" not in argv
        assert "--bootstrap" not in argv

    def test_the_native_argv_is_unchanged(self, service):
        argv = service._build_native_argv(
            ant_path="/root/.local/bin/ant",
            bootstrap_args=["--bootstrap", "1.2.3.4:5"],
            evm_network="arbitrum-one",
            file_path=Path("/data/f.bin"),
            payment_type="merkle",
            log_level="debug",
        )
        assert argv == [
            "/root/.local/bin/ant",
            "--bootstrap",
            "1.2.3.4:5",
            "--evm-network",
            "arbitrum-one",
            "--json",
            "-vv",
            "file",
            "upload",
            "--public",
            "--merkle",
            "/data/f.bin",
        ]


class TestWasmFailureClassification:
    """Which failure a browser-path upload was decides its pool claim.

    A payment that went out means chunks may be on the network and the file must
    never reach a canary, so the claim is marked PARTIAL. A failure before any
    payment means nothing reached the network and the file is untouched, so the
    claim is RELEASED.
    """

    def test_payment_sent_in_the_json_is_a_partial(self, service, tmp_path):
        assert (
            service._wasm_failure_status({"payment_sent": True}, tmp_path / "absent.paid")
            == service.STATUS_PARTIAL_UPLOAD
        )

    def test_no_payment_and_no_marker_is_a_release(self, service, tmp_path):
        assert (
            service._wasm_failure_status({"payment_sent": False}, tmp_path / "absent.paid")
            == service.STATUS_FAILED
        )

    def test_a_marker_alone_is_enough(self, service, tmp_path):
        # The case the JSON cannot describe: a child killed after the money went
        # out but before it printed anything. Trusting only the JSON would return
        # a paid-for file to the pool.
        marker = tmp_path / "op.paid"
        marker.write_text("2026-09-26T00:00:00Z\n")
        assert service._wasm_failure_status(None, marker) == (service.STATUS_PARTIAL_UPLOAD)

    def test_a_missing_result_with_no_marker_is_a_release(self, service, tmp_path):
        assert service._wasm_failure_status(None, tmp_path / "absent.paid") == (
            service.STATUS_FAILED
        )

    def test_over_cap_beats_everything(self, service, tmp_path):
        assert (
            service._wasm_failure_status({"error_kind": "over_cap"}, tmp_path / "absent.paid")
            == service.STATUS_OVER_CAP
        )


class TestServiceInfluxLine:
    def _line(self, service, **overrides):
        defaults = dict(
            host="h",
            region="lon1",
            status=0,
            total_ms=1,
            upload_secs=1.0,
            file_size=1,
            start_ns=1,
            chunks_stored=0,
            chunks_failed=0,
            total_chunks=0,
            chunk_attempts_total=0,
            retries_round_0=0,
            retries_round_1=0,
            retries_round_2=0,
            retries_round_3=0,
            store_duration_p50_ms=0,
            store_duration_p95_ms=0,
            store_duration_max_ms=0,
            address="",
            file_name="f.bin",
            source_url="https://example.com/f.bin",
            reason="",
        )
        defaults.update(overrides)
        return service._build_line(**defaults)

    def test_error_kind_is_emitted_when_present(self, service):
        assert 'error_kind="crash"' in self._line(service, error_kind="crash")

    def test_a_native_point_carries_no_error_kind(self, service):
        assert "error_kind" not in self._line(service)


class TestServiceMainLoop:
    """The loop with a fake `ant-wasm` on PATH, end to end per outcome."""

    def _run(
        self,
        service,
        monkeypatch,
        tmp_path,
        *,
        result,
        exit_code=None,
        write_marker=False,
        file_size=1024,
        env=None,
        download=None,
    ):
        import json as _json

        posted = []
        monkeypatch.setattr(
            service,
            "_post_influx",
            lambda url, token, org, bucket, line: posted.append((bucket, line)),
        )

        def _fake_download(url, dest, timeout):
            dest.write_bytes(b"x" * file_size)
            return file_size

        monkeypatch.setattr(service, "_download", download or _fake_download)

        def _fake_transfer(*, cmd, secret_key, stderr_log_path, stdout_log_path):
            if write_marker:
                marker = cmd[cmd.index("--payment-marker") + 1]
                Path(marker).write_text("paid\n")
            stdout = _json.dumps(result) if result is not None else ""
            code = exit_code
            if code is None:
                code = 0 if result and not result.get("error") else 1
            return code, stdout, ""

        monkeypatch.setattr(service, "_run_transfer", _fake_transfer)

        urls = tmp_path / "urls.txt"
        urls.write_text("https://example.com/a.bin\n")
        monkeypatch.setattr(service, "LOG_DIR", str(tmp_path / "logs"))
        base_env = {
            "SECRET_KEY": "0x" + "11" * 32,
            "REGION": "lon1",
            "CLIENT_INDEX": "1",
            "URLS_FILE": str(urls),
            "WORK_DIR": str(tmp_path / "files"),
            "INFLUX_URL": "http://influx:8086",
            "INFLUX_TOKEN": "tok",
            "INFLUX_BUCKET": "wasm-client-metrics",
            "CLIENT_KIND": "wasm",
            "MAX_UPLOAD_BYTES": str(WASM_MAX_FILE_BYTES),
        }
        base_env.update(env or {})
        for key, value in base_env.items():
            monkeypatch.setenv(key, value)
        assert service.main() == 0
        return posted

    def _status(self, posted):
        assert len(posted) == 1
        bucket, line = posted[0]
        field = [f for f in line.split(" ")[1].split(",") if f.startswith("status=")]
        return bucket, int(field[0].split("=")[1].rstrip("i"))

    def test_a_success_is_status_zero(self, service, monkeypatch, tmp_path):
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result={
                "address": "abc",
                "chunks_stored": 4,
                "total_chunks": 4,
                "elapsed_secs": 1.5,
                "payment_sent": True,
            },
        )
        bucket, status = self._status(posted)
        assert status == service.STATUS_SUCCESS
        # WASM operations go to their own bucket so nothing can average two
        # transports measured several times apart.
        assert bucket == "wasm-client-metrics"

    def test_a_real_ant_wasm_success_payload_is_a_success(
        self, service, monkeypatch, tmp_path
    ):
        # `ant-wasm` ALWAYS emits `error`, set to null on success, where
        # `ant --json` omits the key. Keying on presence reported every healthy
        # browser-path upload as a partial — which spends the file's pool claim
        # as though only some of its chunks had been stored.
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result={
                "address": "abc",
                "chunks_stored": 4,
                "chunks_failed": 0,
                "total_chunks": 4,
                "elapsed_secs": 12.5,
                "storage_cost_atto": "1000",
                "gas_cost_wei": None,
                "file_size": 1024,
                "payment_sent": True,
                "error": None,
                "reason": None,
                "error_kind": None,
            },
            exit_code=0,
        )
        assert self._status(posted)[1] == service.STATUS_SUCCESS

    def test_a_failure_before_payment_is_status_failed(self, service, monkeypatch, tmp_path):
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result={
                "error": "no quotes",
                "reason": "no quotes",
                "error_kind": "quorum",
                "payment_sent": False,
            },
        )
        assert self._status(posted)[1] == service.STATUS_FAILED

    def test_a_failure_after_payment_is_a_partial(self, service, monkeypatch, tmp_path):
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result={
                "error": "store failed",
                "reason": "store failed",
                "error_kind": "quorum",
                "payment_sent": True,
            },
        )
        assert self._status(posted)[1] == service.STATUS_PARTIAL_UPLOAD

    def test_a_crash_after_the_marker_is_a_partial(self, service, monkeypatch, tmp_path):
        # An OOM kills the WASM child, not this parent. The marker is the only
        # surviving evidence the money went out.
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result=None,
            exit_code=-9,
            write_marker=True,
        )
        bucket, status = self._status(posted)
        assert status == service.STATUS_PARTIAL_UPLOAD
        assert 'error_kind="crash"' in posted[0][1]

    def test_a_crash_with_no_marker_is_released(self, service, monkeypatch, tmp_path):
        posted = self._run(service, monkeypatch, tmp_path, result=None, exit_code=-9)
        bucket, status = self._status(posted)
        assert status == service.STATUS_FAILED
        assert 'error_kind="crash"' in posted[0][1]

    def test_an_over_cap_file_is_never_uploaded(self, service, monkeypatch, tmp_path):
        spawned = []
        monkeypatch.setattr(
            service,
            "_run_transfer",
            lambda **kw: spawned.append(kw) or (0, "", ""),
        )
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result=None,
            file_size=WASM_MAX_FILE_BYTES + 1,
            env={"MAX_UPLOAD_BYTES": str(WASM_MAX_FILE_BYTES)},
        )
        assert spawned == []
        bucket, status = self._status(posted)
        assert status == service.STATUS_OVER_CAP
        assert 'error_kind="over_cap"' in posted[0][1]

    def test_the_cap_is_ignored_on_the_native_path(self, service, monkeypatch, tmp_path):
        # Nothing should be able to make the native client skip a file it can
        # upload perfectly well, however the env file was written.
        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result={"address": "abc", "chunks_stored": 1, "total_chunks": 1},
            file_size=WASM_MAX_FILE_BYTES + 1,
            env={
                "CLIENT_KIND": "native",
                "ANT": "/root/.local/bin/ant",
                "INFLUX_BUCKET": "testnet-metrics",
            },
        )
        bucket, status = self._status(posted)
        assert status == service.STATUS_SUCCESS
        assert bucket == "testnet-metrics"

    def test_a_source_fetch_failure_is_unchanged(self, service, monkeypatch, tmp_path):
        def _boom(url, dest, timeout):
            raise OSError("HTTP 404")

        posted = self._run(
            service,
            monkeypatch,
            tmp_path,
            result=None,
            download=_boom,
        )
        assert self._status(posted)[1] == service.STATUS_DOWNLOAD_FAILED


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session_()
    yield (
        SqlAlchemyDeploymentRepository(session),
        SqlAlchemyProdFilePoolRepository(session),
        session,
    )
    session.close()
    engine.dispose()


def _point(**overrides):
    base = {
        "_time": datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
        "host": "PROD-WASM-UL-01-ant-client-upload-lon1-1",
        "region": "lon1",
        "status": 0,
        "total_ms": 1000,
        "upload_secs": 1.0,
        "file_size": 5 * MIB,
        "start_ns": 1_776_700_000_000_000_000,
        "chunks_stored": 4,
        "chunks_failed": 0,
        "total_chunks": 4,
        "address": "addr",
        "file_name": "a.bin",
        "source_url": "https://example.com/a.bin",
        "reason": "",
    }
    base.update(overrides)
    return base


def _deploy_args():
    return {
        "kind": "prod-uploaders",
        "uploaders": [
            {
                "region": "lon1",
                "vm_size": "s-2vcpu-4gb",
                "client_kind": "native",
                "hostname": "PROD-WASM-UL-01-ant-client-upload-lon1-1",
            },
            {
                "region": "nyc3",
                "vm_size": "s-4vcpu-8gb",
                "client_kind": "wasm",
                "hostname": "PROD-WASM-UL-01-ant-client-wasm-upload-nyc3-1",
            },
        ],
    }


def _record(repo, points, name="PROD-WASM-UL-01"):
    args = SimpleNamespace(
        name=name,
        monitoring_name=None,
        influx_url="https://influx.example.com",
        influx_token="tok",
        influx_org="ant",
        influx_bucket="testnet-metrics",
        window="-30d",
    )
    with (
        patch("saorsa_deploy.cmd.record_results.get_repository", return_value=repo),
        patch(
            "saorsa_deploy.cmd.record_results.fetch_all_prod_upload_points",
            return_value=points,
        ),
    ):
        cmd_record_results(args)


class TestRecordingLabelsTheTransport:
    def test_each_row_gets_its_own_transport(self, registry):
        repo, _, _ = registry
        repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        _record(
            repo,
            [
                _point(),
                _point(
                    host="PROD-WASM-UL-01-ant-client-wasm-upload-nyc3-1",
                    region="nyc3",
                    source_url="https://example.com/b.bin",
                    file_name="b.bin",
                    start_ns=2,
                ),
            ],
        )
        rows = {
            r.client_hostname: r.client_kind
            for r in repo.list_prod_upload_results_for_deployment(1)
        }
        assert rows == {
            "PROD-WASM-UL-01-ant-client-upload-lon1-1": "native",
            "PROD-WASM-UL-01-ant-client-wasm-upload-nyc3-1": "wasm",
        }

    def test_an_unlisted_host_is_left_null(self, registry):
        # NULL reads as native everywhere else, and guessing would be worse than
        # saying nothing.
        repo, _, _ = registry
        repo.create(name="PROD-UL-OLD", deploy_args={"kind": "prod-uploaders"})
        _record(repo, [_point()], name="PROD-UL-OLD")
        assert repo.list_prod_upload_results_for_deployment(1)[0].client_kind is None

    def test_a_re_run_never_overwrites_a_known_transport_with_null(self, registry):
        # The row is what survives teardown. An unconditional write would relabel
        # every WASM upload as native and push its failures into the native
        # figures.
        repo, _, session = registry
        deployment = repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        wasm_host = "PROD-WASM-UL-01-ant-client-wasm-upload-nyc3-1"
        _record(repo, [_point(host=wasm_host, region="nyc3")])
        assert repo.list_prod_upload_results_for_deployment(1)[0].client_kind == "wasm"

        deployment.deploy_args = {"kind": "prod-uploaders"}
        session.commit()
        _record(repo, [_point(host=wasm_host, region="nyc3")])
        assert repo.list_prod_upload_results_for_deployment(1)[0].client_kind == "wasm"

    @pytest.mark.parametrize(
        "code,expected",
        [
            (0, ProdUploadStatus.SUCCESS),
            (1, ProdUploadStatus.PARTIAL_UPLOAD),
            (2, ProdUploadStatus.DOWNLOAD_FAILED),
            (3, ProdUploadStatus.OVER_CAP),
            (4, ProdUploadStatus.FAILED),
        ],
    )
    def test_every_status_code_maps(self, registry, code, expected):
        repo, _, _ = registry
        repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        _record(repo, [_point(status=code)])
        assert repo.list_prod_upload_results_for_deployment(1)[0].status is expected


class TestBothBucketsAreRead:
    def test_the_wasm_bucket_is_queried_too(self):
        # A single-bucket read would make a WASM tier that failed every upload
        # look identical to one that was never configured.
        from saorsa_deploy import influx

        queried = []
        with patch.object(
            influx,
            "fetch_prod_upload_points",
            side_effect=lambda **kw: queried.append(kw["bucket"]) or [],
        ):
            influx.fetch_all_prod_upload_points(
                url="http://i",
                token="t",
                org="ant",
                bucket="testnet-metrics",
                wasm_bucket="wasm-client-metrics",
            )
        assert queried == ["testnet-metrics", "wasm-client-metrics"]

    def test_a_missing_wasm_bucket_is_tolerated(self):
        from saorsa_deploy import influx

        def _fetch(**kw):
            if kw["bucket"] == "wasm-client-metrics":
                raise RuntimeError("bucket not found")
            return [_point()]

        with patch.object(influx, "fetch_prod_upload_points", side_effect=_fetch):
            points = influx.fetch_all_prod_upload_points(
                url="http://i",
                token="t",
                org="ant",
                bucket="testnet-metrics",
                wasm_bucket="wasm-client-metrics",
            )
        assert len(points) == 1

    def test_the_same_point_from_both_buckets_is_counted_once(self):
        from saorsa_deploy import influx

        with patch.object(influx, "fetch_prod_upload_points", side_effect=lambda **kw: [_point()]):
            points = influx.fetch_all_prod_upload_points(
                url="http://i",
                token="t",
                org="ant",
                bucket="testnet-metrics",
                wasm_bucket="wasm-client-metrics",
            )
        assert len(points) == 1


# ---------------------------------------------------------------------------
# Pool claim outcomes
# ---------------------------------------------------------------------------


class TestClaimOutcomes:
    def _claim(self, pool_repo, deployment, locator, name):
        from saorsa_db.size_class import SizeClass

        pool_repo.add_pool_file(PoolFileKind.URL, locator, name, size_bytes=5 * MIB)
        return pool_repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 1},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name=deployment.name,
        )[0]

    def _resolve(self, registry, status, *, locator="https://example.com/a.bin"):
        repo, pool_repo, _ = registry
        deployment = repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        claim = self._claim(pool_repo, deployment, locator, "a.bin")
        with patch(
            "saorsa_deploy.registry.get_file_pool_repository",
            return_value=pool_repo,
        ):
            _record(repo, [_point(status=status, source_url=locator)])
        return pool_repo.list_pool_claims(file_id=claim.file_id)[0]

    def test_a_success_spends_the_file(self, registry):
        assert self._resolve(registry, 0).status is ClaimStatus.UPLOADED

    def test_a_failure_after_payment_marks_it_partial(self, registry):
        # Chunks ARE in production, so completing it is useful work but a canary
        # must never draw it.
        assert self._resolve(registry, 1).status is ClaimStatus.PARTIAL

    def test_a_failure_before_payment_releases_it(self, registry):
        # Nothing reached the network, so burning the entry would be a lie.
        assert self._resolve(registry, 4).status is ClaimStatus.RELEASED

    def test_an_over_cap_file_is_released(self, registry):
        assert self._resolve(registry, 3).status is ClaimStatus.RELEASED

    def test_a_source_fetch_failure_releases_it(self, registry):
        assert self._resolve(registry, 2).status is ClaimStatus.RELEASED

    def test_a_later_failure_never_undoes_a_recorded_upload(self, registry):
        repo, pool_repo, _ = registry
        deployment = repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        locator = "https://example.com/a.bin"
        claim = self._claim(pool_repo, deployment, locator, "a.bin")
        with patch(
            "saorsa_deploy.registry.get_file_pool_repository",
            return_value=pool_repo,
        ):
            _record(repo, [_point(status=0, source_url=locator)])
            # A retry from the other host fails before payment. The upload that
            # succeeded is still on the network.
            _record(
                repo,
                [
                    _point(status=0, source_url=locator),
                    _point(
                        status=4,
                        source_url=locator,
                        host="PROD-WASM-UL-01-ant-client-wasm-upload-nyc3-1",
                        region="nyc3",
                        start_ns=2,
                    ),
                ],
            )
        assert pool_repo.list_pool_claims(file_id=claim.file_id)[0].status is ClaimStatus.UPLOADED


# ---------------------------------------------------------------------------
# The teardown guard
# ---------------------------------------------------------------------------


class TestResultsGuardCountsBothTiers:
    def test_wasm_rows_count_towards_the_expectation(self, registry):
        # The guard compares recorded rows against the run's pool claims, which
        # say exactly how many files it took. A WASM row is a recorded row.
        from saorsa_deploy.results_guard import assess

        repo, pool_repo, _ = registry
        deployment = repo.create(name="PROD-WASM-UL-01", deploy_args=_deploy_args())
        from saorsa_db.size_class import SizeClass

        for i in range(2):
            pool_repo.add_pool_file(
                PoolFileKind.URL,
                f"https://example.com/{i}.bin",
                f"{i}.bin",
                size_bytes=5 * MIB,
            )
        pool_repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.TINY: 2},
            ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name=deployment.name,
        )
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.expected == 2 and status.recorded == 0
        assert status.is_concerning

        for i, transport in enumerate(("native", "wasm")):
            repo.upsert_prod_upload_result(
                deployment_id=deployment.id,
                client_hostname=f"host-{i}",
                region="lon1",
                source_url=f"https://example.com/{i}.bin",
                file_name=f"{i}.bin",
                file_size_bytes=5 * MIB,
                status=ProdUploadStatus.SUCCESS,
                address="addr",
                chunks_stored=4,
                chunks_failed=0,
                total_chunks=4,
                duration_ms=1,
                upload_secs=1.0,
                started_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
                finished_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
                client_kind=transport,
            )
        status = assess(repo, pool_repo, deployment, "prod-uploaders")
        assert status.recorded == 2
        assert not status.is_concerning


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestReporting:
    def _rendered(self, rows):
        from saorsa_deploy.cmd import print_recorded_results as module

        console = Console(record=True, width=220)
        module._print_prod_upload_section(rows, "PROD-WASM-UL-01", False, console)
        return console.export_text()

    def _row(self, **overrides):
        base = dict(
            client_hostname="host-native",
            client_kind="native",
            file_name="a.bin",
            file_size_bytes=5 * MIB,
            status=ProdUploadStatus.SUCCESS,
            chunks_stored=4,
            chunks_failed=0,
            total_chunks=4,
            duration_ms=1000,
            storage_cost_atto=None,
            gas_cost_wei=None,
            started_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_a_mixed_run_splits_its_totals(self):
        out = self._rendered(
            [
                self._row(),
                self._row(
                    client_hostname="host-wasm",
                    client_kind="wasm",
                    status=ProdUploadStatus.FAILED,
                ),
                self._row(client_hostname="host-wasm", client_kind="wasm"),
            ]
        )
        flat = " ".join(out.split())
        assert "Upload totals by transport" in out
        assert "native 1/1 = 100.00%" in flat
        assert "wasm 1/2 = 50.00%" in flat
        # Per-host tables name their transport too.
        assert "host-wasm — [wasm]" in flat
        assert "host-native — [native]" in flat

    def test_an_over_cap_row_is_excluded_from_the_denominator(self):
        out = " ".join(
            self._rendered(
                [
                    self._row(client_hostname="host-wasm", client_kind="wasm"),
                    self._row(
                        client_hostname="host-wasm",
                        client_kind="wasm",
                        status=ProdUploadStatus.OVER_CAP,
                    ),
                ]
            ).split()
        )
        assert "wasm 1/1 = 100.00%, 1 over-cap (not uploaded)" in out

    def test_a_native_only_run_renders_as_before(self):
        out = self._rendered([self._row(client_kind=None)])
        assert "Upload totals by transport" not in out
        assert "[native]" not in out

    def test_the_slack_reply_splits_per_transport(self):
        from saorsa_deploy.slack_results import prod_upload_result_blocks

        blocks = prod_upload_result_blocks(
            [
                _point(client_kind="native"),
                _point(status=4, client_kind="wasm", host="wasm-host"),
            ]
        )
        text = str(blocks)
        assert "Native OK" in text
        assert "WASM failed" in text
        assert "Uploads OK" not in text

    def test_the_slack_reply_is_unchanged_without_wasm(self):
        from saorsa_deploy.slack_results import prod_upload_result_blocks

        text = str(prod_upload_result_blocks([_point(host="native-host")]))
        assert "Uploads OK" in text
        assert "WASM" not in text

    def test_slack_post_lists_the_two_tiers_separately(self):
        from saorsa_deploy.cmd.slack_post import _prod_uploaders_blocks

        text = str(
            _prod_uploaders_blocks(
                {
                    **_deploy_args(),
                    "ant_binary": {"version": "0.3.8"},
                    "wasm_build": {
                        "repo_owner": "WithAutonomi",
                        "ref": "ant-cli-v0.3.8-rc.1",
                        "commit": "abcdef1234567890",
                    },
                    "wasm_bootstrap": {"total": 7, "answered": 7, "dead": []},
                }
            )
        )
        assert "*Uploaders:*" in text
        assert "*WASM uploaders:*" in text
        assert "WASM bundle" in text

    def test_slack_post_omits_the_wasm_heading_without_wasm_uploaders(self):
        from saorsa_deploy.cmd.slack_post import _prod_uploaders_blocks

        text = str(
            _prod_uploaders_blocks(
                {
                    "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb"}],
                    "ant_binary": {"version": "0.3.8"},
                }
            )
        )
        assert "WASM" not in text
