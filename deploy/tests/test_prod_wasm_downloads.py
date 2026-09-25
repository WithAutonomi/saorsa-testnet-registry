"""WASM (browser-path) clients on the production download kinds (V2-1271 part B).

Manifest validation, the argv the orchestrator emits, the provisioner's install
and env output, the service's transport dispatch, and the recording/reporting
paths — plus the invariant that a manifest with no WASM fields renders
byte-identically to before.
"""

import importlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pydantic
import pytest

from saorsa_deploy.config.models import (
    ProdContinuousDownloaderSpec,
    ProdDownloadersSpec,
)
from saorsa_deploy.provisioning.prod_downloader import ProdDownloaderProvisioner

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy"
    / "resources"
    / "scripts"
    / "prod_downloader.py"
)

MA = (
    "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiAaaa/p2p/"
    "57a89f84e1652d22fd6b791c29d83f187fbed3ba8e08e265fe3031b3a6f725da"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_downloader", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wasm_spec(**overrides):
    payload = {
        "spec": "lon1:s-4vcpu-8gb",
        "binary": {"version": "0.3.8"},
        "client_kind": "wasm",
        "wasm_binary": {"branch": "ant-cli-v0.3.8-rc.1", "repo_owner": "WithAutonomi"},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


class TestManifestValidation:
    def test_the_default_is_native_and_needs_nothing_new(self):
        spec = ProdDownloadersSpec(spec="lon1", binary={"version": "0.3.8"})
        assert spec.client_kind is None
        assert spec.wasm_binary is None

    def test_wasm_requires_a_bundle_source(self):
        # The bundle is always built from source — there is no release form — so
        # without this the deploy would have nothing to install.
        with pytest.raises(pydantic.ValidationError, match="wasm_binary is required"):
            ProdDownloadersSpec(
                spec="lon1:s-4vcpu-8gb",
                binary={"version": "0.3.8"},
                client_kind="wasm",
            )

    def test_a_wasm_binary_version_is_refused_with_the_tag_form_named(self):
        with pytest.raises(pydantic.ValidationError, match="branch: ant-cli-vX.Y.Z"):
            ProdDownloadersSpec(**_wasm_spec(wasm_binary={"branch": "b", "version": "1"}))

    def test_a_tag_is_accepted_as_the_branch(self):
        # `build ant-wasm` clones with `git clone --branch <ref>`, which takes
        # either, and that is the only way to pin an RC.
        spec = ProdDownloadersSpec(**_wasm_spec())
        assert spec.wasm_binary.branch == "ant-cli-v0.3.8-rc.1"

    @pytest.mark.parametrize(
        "option", ["bootstrap_cache", "memory_profiling", "download_diagnostics"]
    )
    def test_the_options_with_no_browser_path_meaning_are_refused(self, option):
        # Refused rather than ignored: a silently-dropped option leaves the
        # operator reading their own manifest as a description of the run.
        with pytest.raises(pydantic.ValidationError, match=option):
            ProdDownloadersSpec(**_wasm_spec(**{option: True}))

    def test_bootstrap_cache_false_is_refused_too(self):
        # The option says nothing either way on this path, so neither value is
        # meaningful.
        with pytest.raises(pydantic.ValidationError, match="bootstrap_cache"):
            ProdDownloadersSpec(**_wasm_spec(bootstrap_cache=False))

    def test_an_undersized_digitalocean_vm_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="8 GB"):
            ProdDownloadersSpec(**_wasm_spec(spec="lon1:s-2vcpu-4gb"))

    def test_wasm_fields_are_refused_on_a_native_deployment(self):
        # Otherwise a manifest could name a bundle that is never built or
        # installed, and read as though the run used it.
        with pytest.raises(pydantic.ValidationError, match="only valid with"):
            ProdDownloadersSpec(
                spec="lon1",
                binary={"version": "0.3.8"},
                wasm_binary={"branch": "b"},
            )

    def test_the_continuous_kind_validates_identically(self):
        with pytest.raises(pydantic.ValidationError, match="wasm_binary is required"):
            ProdContinuousDownloaderSpec(
                spec="lon1:s-4vcpu-8gb",
                binary={"version": "0.3.8"},
                client_kind="wasm",
            )
        assert (
            ProdContinuousDownloaderSpec(**_wasm_spec(sleep_interval_secs=120)).sleep_interval_secs
            == 120
        )


# ---------------------------------------------------------------------------
# The plan, and whether the argv it emits parses
# ---------------------------------------------------------------------------


def _manifest(kind: str, spec_payload: dict):
    from saorsa_deploy.config.models import Manifest

    key = "prod_downloaders" if kind == "prod-downloaders" else "prod_continuous_downloader"
    return Manifest.model_validate(
        {
            "kind": kind,
            "name": "PROD-WASM-DL-01",
            "purpose": "test",
            "ssh_key_path": "/ssh/key",
            key: spec_payload,
        }
    )


@pytest.mark.parametrize("kind", ["prod-downloaders", "prod-continuous-downloader"])
class TestThePlan:
    def test_a_wasm_run_builds_the_bundle_first(self, kind):
        # The deploy step fetches the bundle from the name-scoped S3 key this
        # step populates, so the ordering is not optional.
        from saorsa_deploy.orchestrator import build_deploy_steps

        steps = build_deploy_steps(_manifest(kind, _wasm_spec()))
        assert steps[0].argv[:2] == ["build", "ant-wasm"]
        assert "--branch-name" in steps[0].argv
        assert steps[0].argv[steps[0].argv.index("--branch-name") + 1] == ("ant-cli-v0.3.8-rc.1")

    def test_a_native_run_emits_no_wasm_step_or_flag(self, kind):
        from saorsa_deploy.orchestrator import build_deploy_steps

        steps = build_deploy_steps(
            _manifest(kind, {"spec": "lon1", "binary": {"version": "0.3.8"}})
        )
        flat = [token for step in steps for token in step.argv]
        assert "ant-wasm" not in flat
        assert "--client-kind" not in flat
        assert not any("--wasm-" in token for token in flat)

    def test_the_deploy_argv_the_orchestrator_emits_actually_parses(self, kind):
        # The `test_pool_size_bounds_plumbing.py` guard exists for exactly this:
        # a flag the orchestrator emits but the CLI does not define fails at the
        # END of an otherwise-successful plan, against real infrastructure.
        from saorsa_deploy.main import main
        from saorsa_deploy.orchestrator import build_deploy_steps

        steps = build_deploy_steps(
            _manifest(
                kind,
                _wasm_spec(wasm_bootstrap_multiaddrs_file="/tmp/ma.txt"),
            )
        )
        # NOT steps[-1]: `up` appends a generate-inventory step for these
        # kinds, so the deploy step is the one naming the prod subcommand.
        argv = next(step.argv for step in steps if step.argv[:1] == ["prod"])

        module_name = (
            "prod_downloaders" if kind == "prod-downloaders" else "prod_continuous_downloader"
        )
        fn_name = (
            "cmd_prod_downloaders_deploy"
            if kind == "prod-downloaders"
            else "cmd_prod_continuous_downloader_deploy"
        )
        module = importlib.import_module(f"saorsa_deploy.cmd.{module_name}")
        captured = {}
        original = getattr(module, fn_name)
        setattr(module, fn_name, lambda args: captured.update(vars(args)))
        try:
            main(argv)
        finally:
            setattr(module, fn_name, original)

        assert captured["client_kind"] == "wasm"
        assert captured["wasm_branch_name"] == "ant-cli-v0.3.8-rc.1"
        assert captured["wasm_repo_owner"] == "WithAutonomi"
        assert captured["wasm_bootstrap_multiaddrs_file"] == "/tmp/ma.txt"


# ---------------------------------------------------------------------------
# The provisioner
# ---------------------------------------------------------------------------


def _env_script(**kwargs) -> str:
    """The env-file shell the provisioner would run, without pyinfra."""
    captured = {}

    class _Recorder(ProdDownloaderProvisioner):
        def _add_env_and_addresses_op(self, state):
            import saorsa_deploy.provisioning.prod_downloader as mod

            real = mod.add_op
            mod.add_op = lambda s, op, name, commands: captured.update({"script": commands[0]})
            try:
                super()._add_env_and_addresses_op(state)
            finally:
                mod.add_op = real

    provisioner = _Recorder(
        downloaders=[
            {
                "ip": "1.2.3.4",
                "region": "lon1",
                "addresses_file_content": "abc\n",
            }
        ],
        bootstrap_peers=["1.1.1.1:10000"],
        sleep_interval_secs=0,
        one_shot=True,
        influx_url="http://influx:8086",
        influx_token="tok",
        **kwargs,
    )
    provisioner._add_env_and_addresses_op(None)
    return captured["script"]


class TestTheProvisioner:
    def test_a_native_env_file_is_unchanged(self):
        # The issue's central invariant: nothing about a native deployment moves.
        script = _env_script()
        assert "CLIENT_KIND" not in script
        assert "ANT_WASM" not in script
        assert "BOOTSTRAP_MULTIADDRS" not in script
        assert "INFLUX_BUCKET=testnet-metrics" in script

    def test_a_wasm_env_file_names_the_cli_and_the_bootstraps(self):
        script = _env_script(
            client_kind="wasm",
            wasm_bundle_url="https://x/bundle.tar.gz",
            bootstrap_multiaddrs=[MA],
        )
        assert "CLIENT_KIND=wasm" in script
        assert "ANT_WASM=/usr/local/bin/ant-wasm" in script
        assert f'BOOTSTRAP_MULTIADDRS="{MA}"' in script
        assert "WASM_DIR=/opt/ant-wasm-client/pkg" in script
        # ant-core's browser cap, so an over-cap entry is skipped up front.
        assert "MAX_DOWNLOAD_BYTES=1000000000" in script

    def test_wasm_operations_go_to_their_own_influx_bucket(self):
        # Both transports write the same `download` measurement, so sharing a
        # bucket would let any panel average two instruments measured ~4x apart.
        script = _env_script(
            client_kind="wasm",
            wasm_bundle_url="https://x/b.tar.gz",
            bootstrap_multiaddrs=[MA],
        )
        assert "INFLUX_BUCKET=wasm-client-metrics" in script

    def test_a_wasm_provisioner_without_a_bundle_is_refused_at_construction(self):
        with pytest.raises(RuntimeError, match="WASM bundle URL"):
            ProdDownloaderProvisioner(
                downloaders=[{"ip": "1.2.3.4", "region": "lon1"}],
                bootstrap_peers=[],
                sleep_interval_secs=0,
                client_kind="wasm",
                bootstrap_multiaddrs=[MA],
            )

    def test_a_wasm_provisioner_without_a_bootstrap_is_refused(self):
        # Without one a browser client cannot reach the network at all, and the
        # failure would surface minutes after the VM is up and billing.
        with pytest.raises(RuntimeError, match="bootstrap WebRTC multiaddr"):
            ProdDownloaderProvisioner(
                downloaders=[{"ip": "1.2.3.4", "region": "lon1"}],
                bootstrap_peers=[],
                sleep_interval_secs=0,
                client_kind="wasm",
                wasm_bundle_url="https://x/b.tar.gz",
            )

    def test_a_wasm_host_installs_the_bundle_and_not_the_ant_binary(self):
        ops = []

        class _Recorder(ProdDownloaderProvisioner):
            pass

        provisioner = _Recorder(
            downloaders=[{"ip": "1.2.3.4", "region": "lon1"}],
            bootstrap_peers=[],
            sleep_interval_secs=0,
            client_kind="wasm",
            wasm_bundle_url="https://x/b.tar.gz",
            bootstrap_multiaddrs=[MA],
        )
        import saorsa_deploy.provisioning.prod_downloader as mod

        real = mod.add_op
        mod.add_op = lambda s, op, name, commands: ops.append((name, commands[0]))
        try:
            provisioner._add_install_wasm_ops(None)
        finally:
            mod.add_op = real

        names = [name for name, _ in ops]
        assert any("Node.js" in n for n in names)
        assert "Install WASM client bundle" in names
        assert "Install ant-wasm CLI" in names
        assert not any("ant binary" in n for n in names)
        bundle_cmd = next(c for n, c in ops if n == "Install WASM client bundle")
        # A bundle built before the CLI existed would install cleanly and then
        # fail every transfer, so it is checked for here.
        assert "dist/cli.js" in bundle_cmd

    def test_the_unit_is_the_native_one_whichever_transport_runs(self):
        # The transport changes which binary the service spawns, not what the
        # service is: same name, same restart policy, same logs.
        from saorsa_deploy.provisioning.prod_downloader import _build_systemd_unit

        unit = _build_systemd_unit(one_shot=True)
        assert "ExecStart=/usr/bin/python3 -u" in unit
        assert "prod_downloader.py" in unit
        assert "Restart=no" in unit


# ---------------------------------------------------------------------------
# The service script
# ---------------------------------------------------------------------------


class TestTheServiceDispatch:
    def test_the_native_argv_is_unchanged(self, service, tmp_path):
        cmd = service._build_native_argv(
            ant_path="/root/.local/bin/ant",
            bootstrap_args=["--bootstrap", "1.1.1.1:10000"],
            evm_network="arbitrum-one",
            address="abc",
            output_path=tmp_path / "out",
        )
        assert cmd == [
            "/root/.local/bin/ant",
            "--bootstrap",
            "1.1.1.1:10000",
            "--evm-network",
            "arbitrum-one",
            "-v",
            "file",
            "download",
            "abc",
            "-o",
            str(tmp_path / "out"),
        ]

    def test_the_wasm_argv_has_no_chain_and_no_bootstrap_flag(self, service, tmp_path):
        # The bootstraps come from BOOTSTRAP_MULTIADDRS in the service
        # environment, and a download pays for nothing.
        cmd = service._build_wasm_argv(
            ant_wasm_path="/usr/local/bin/ant-wasm",
            address="abc",
            output_path=tmp_path / "out",
        )
        assert cmd == [
            "/usr/local/bin/ant-wasm",
            "file",
            "download",
            "abc",
            "-o",
            str(tmp_path / "out"),
        ]
        assert "--evm-network" not in cmd
        assert "--bootstrap" not in cmd

    def test_the_result_line_is_read_from_the_end_of_stdout(self, service):
        stdout = 'noise\n{"address": "a", "elapsed_secs": 1.5}\n'
        assert service._parse_wasm_result(stdout)["elapsed_secs"] == 1.5

    def test_an_absent_result_line_degrades_rather_than_failing(self, service):
        # The exit code still says what happened, so a missing result loses the
        # detail rather than the operation.
        assert service._parse_wasm_result("nothing json here\n") == {}
        assert service._parse_wasm_result("") == {}

    def test_an_unparseable_result_line_is_ignored(self, service):
        assert service._parse_wasm_result("{not json}\n") == {}

    def test_a_later_line_wins(self, service):
        stdout = '{"elapsed_secs": 1}\n{"elapsed_secs": 2}\n'
        assert service._parse_wasm_result(stdout)["elapsed_secs"] == 2


class TestTheAddressListCarriesSizes:
    def test_a_sized_line_is_parsed(self, service):
        assert service._parse_address_line("abc 1048576") == ("abc", 1048576)

    def test_a_bare_line_is_never_over_cap(self, service):
        # Unknown is not "too big": a list written before sizes were published
        # must keep working.
        address, declared = service._parse_address_line("abc")
        assert declared is None

    def test_a_malformed_size_is_treated_as_unknown(self, service):
        assert service._parse_address_line("abc notanumber") == ("abc", None)


class TestTheStatusWireValues:
    def test_skipped_is_an_integer_on_the_wire(self, service):
        # Influx `status` is an integer field; a string would be a type conflict
        # and InfluxDB would drop the whole point.
        assert service.STATUS_SKIPPED == 2
        line = service._build_line(
            host="h",
            region="r",
            status=service.STATUS_SKIPPED,
            total_ms=0,
            download_secs=0.0,
            file_size=0,
            start_ns=1,
            address="abc",
            error_kind="over_cap",
        )
        assert "status=2i" in line
        assert 'address="abc"' in line
        assert 'error_kind="over_cap"' in line

    def test_a_native_line_carries_the_address_too(self, service):
        # Native parity: nothing else maps an operation to what it fetched.
        line = service._build_line(
            host="h",
            region="r",
            status=0,
            total_ms=5,
            download_secs=1.0,
            file_size=10,
            start_ns=1,
            address="abc",
        )
        assert 'address="abc"' in line
        assert "error_kind" not in line

    def test_a_field_string_is_escaped(self, service):
        line = service._build_line(
            host="h",
            region="r",
            status=0,
            total_ms=5,
            download_secs=1.0,
            file_size=10,
            start_ns=1,
            address='a"b\\c',
        )
        assert 'address="a\\"b\\\\c"' in line

    def test_no_explicit_timestamp_is_appended(self, service):
        # InfluxDB stamps server-receive time, so a clock-skewed client is not
        # written into the future.
        line = service._build_line(
            host="h",
            region="r",
            status=0,
            total_ms=5,
            download_secs=1.0,
            file_size=10,
            start_ns=1,
            address="abc",
        )
        assert line.endswith('address="abc"')


class TestTheServiceWalk:
    """The loop, driven end-to-end against a fake `ant-wasm`."""

    def _run(self, service, tmp_path, monkeypatch, *, addresses, fake_cli, wasm=True):
        """Run one ONE_SHOT pass; return the Influx lines it posted."""
        cli = tmp_path / "ant-wasm"
        cli.write_text(fake_cli)
        cli.chmod(0o755)
        addrs = tmp_path / "addresses.txt"
        addrs.write_text(addresses)

        posted: list[str] = []
        monkeypatch.setattr(
            service,
            "_post_influx",
            lambda url, token, org, bucket, line: posted.append(line),
        )
        monkeypatch.setattr(service, "LOG_DIR", str(tmp_path / "logs"))
        for key, value in {
            "CLIENT_KIND": "wasm" if wasm else "native",
            "ANT_WASM": str(cli),
            "ANT": str(cli),
            "MAX_DOWNLOAD_BYTES": "1000000000",
            "REGION": "lon1",
            "ADDRESSES_FILE": str(addrs),
            "DOWNLOAD_DIR": str(tmp_path / "dl"),
            "ONE_SHOT": "true",
            "SLEEP_INTERVAL_SECS": "0",
            "INFLUX_URL": "http://influx:8086",
            "INFLUX_TOKEN": "tok",
            "BOOTSTRAP_CACHE": "true",
        }.items():
            monkeypatch.setenv(key, value)
        assert service.main() == 0
        return posted

    def test_a_declared_over_cap_entry_is_skipped_without_spawning(
        self, service, tmp_path, monkeypatch
    ):
        # Nothing to learn from a child that resolves a data map and then
        # refuses, and it costs a fetch.
        marker = tmp_path / "spawned"
        fake = f"#!/bin/sh\ntouch {marker}\necho '{{}}'\n"
        posted = self._run(
            service,
            tmp_path,
            monkeypatch,
            addresses="bigaddr 1000000001\n",
            fake_cli=fake,
        )
        assert not marker.exists()
        assert len(posted) == 1
        assert "status=2i" in posted[0]
        assert 'error_kind="over_cap"' in posted[0]

    def test_a_bare_over_cap_address_is_attempted_and_recorded_as_a_skip(
        self, service, tmp_path, monkeypatch
    ):
        # The declared size is absent, so the child runs and ant-core refuses the
        # file once the data map resolves. Still a skip, not a failure: the
        # content is on the network and healthy.
        fake = (
            "#!/bin/sh\n"
            'echo \'{"error": "invalid public file size 2000000000", '
            '"error_kind": "over_cap", "elapsed_secs": 0.5}\'\n'
            "exit 1\n"
        )
        posted = self._run(service, tmp_path, monkeypatch, addresses="bare\n", fake_cli=fake)
        assert len(posted) == 1
        assert "status=2i" in posted[0]

    def test_a_signal_kill_is_recorded_as_a_crash_and_the_walk_continues(
        self, service, tmp_path, monkeypatch
    ):
        # The WASM process is the largest RSS on the VM, so an OOM lands on it
        # rather than on this parent.
        fake = "#!/bin/sh\nkill -9 $$\n"
        posted = self._run(
            service,
            tmp_path,
            monkeypatch,
            addresses="a\nb\n",
            fake_cli=fake,
        )
        assert len(posted) == 2
        assert all("status=1i" in line for line in posted)
        assert all('error_kind="crash"' in line for line in posted)

    def test_a_success_records_the_address_and_the_reported_elapsed_time(
        self, service, tmp_path, monkeypatch
    ):
        fake = '#!/bin/sh\necho \'{"address": "a", "elapsed_secs": 2.5, "file_size": 1024}\'\n'
        posted = self._run(service, tmp_path, monkeypatch, addresses="a\n", fake_cli=fake)
        assert "status=0i" in posted[0]
        assert 'address="a"' in posted[0]
        assert "download_secs=2.5" in posted[0]

    def test_one_shot_exits_zero_after_a_single_pass(self, service, tmp_path, monkeypatch):
        fake = '#!/bin/sh\necho \'{"address": "a", "elapsed_secs": 0.1}\'\n'
        posted = self._run(service, tmp_path, monkeypatch, addresses="a\nb\nc\n", fake_cli=fake)
        # Exactly one point per address: main() returning 0 is asserted in _run.
        assert len(posted) == 3

    def test_the_per_op_logs_are_written_for_the_child_too(self, service, tmp_path, monkeypatch):
        # This is what makes `forward_logs` work on the browser path: the
        # forwarder tails /var/log/ant-prod-downloader/*.{stdout,stderr}, and the
        # service tees the `ant-wasm` child's streams into exactly those files.
        fake = '#!/bin/sh\necho progress 1>&2\necho \'{"address": "a", "elapsed_secs": 0.1}\'\n'
        self._run(service, tmp_path, monkeypatch, addresses="a\n", fake_cli=fake)
        logs = sorted(p.name for p in (tmp_path / "logs").iterdir())
        assert any(n.endswith(".stdout") for n in logs)
        assert any(n.endswith(".stderr") for n in logs)
        stderr = next((tmp_path / "logs").glob("*.stderr")).read_text()
        assert "progress" in stderr


# ---------------------------------------------------------------------------
# Recording and reporting
# ---------------------------------------------------------------------------


class TestTheTransportComesFromDeployArgs:
    def _lookup(self, deploy_args):
        from saorsa_deploy.cmd.record_results import _build_client_kind_lookup

        deployment = SimpleNamespace(name="PROD-DL-01", deploy_args=deploy_args)
        return _build_client_kind_lookup(deployment)

    def test_a_recorded_wasm_kind_wins_over_the_s3_state(self):
        # The S3 client state dies at teardown; `deploy_args` does not. Without
        # this a `record-results` re-run after `down` would relabel every WASM op
        # as native and push its failures into the native hard floors.
        lookup = self._lookup({"kind": "prod-downloaders", "client_kind": "wasm"})
        assert lookup("any-host") == "wasm"

    def test_a_missing_client_kind_reads_as_native(self):
        lookup = self._lookup({"kind": "prod-continuous-downloader"})
        assert lookup("any-host") == "native"

    def test_a_testnet_still_resolves_per_host_from_state(self):
        # One transport per deployment is a property of the PROD kinds; a testnet
        # runs both tiers side by side, so its lookup is per-VM.
        lookup = self._lookup({"kind": "testnet"})
        assert lookup("some-host") is None


class TestSkipsAreExcludedFromRates:
    def _record(self, status):
        from saorsa_db.models.op_record import OpRecordStatus

        return SimpleNamespace(
            status=OpRecordStatus(status),
            finished_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
            file_size_bytes=1024,
            duration_ms=100,
        )

    def test_a_skip_leaves_both_halves_of_the_fraction(self):
        # With a six-address list holding two over-cap entries, counting them
        # would cap the gate at 66.7% and fail every run for a reason that is not
        # the release under test.
        from saorsa_deploy.gate_comparison import success_rate

        records = [self._record("success")] * 4 + [self._record("skipped")] * 2
        rate = success_rate(records)
        assert rate["percent"] == 100.0
        assert rate["total"] == 4
        assert rate["skipped"] == 2

    def test_an_all_skipped_run_has_no_rate_but_did_have_operations(self):
        from saorsa_deploy.gate_comparison import success_rate

        rate = success_rate([self._record("skipped")] * 3)
        assert rate["percent"] is None
        assert rate["skipped"] == 3

    def test_the_hard_floor_reads_not_assessed_rather_than_zero(self):
        from saorsa_deploy.gate_comparison import NOT_ASSESSED, _hard_success_floors
        from saorsa_deploy.gate_config import load_gate_config

        run = SimpleNamespace(
            upload_records=[],
            download_records=[self._record("skipped")] * 2,
        )
        results = _hard_success_floors(run, load_gate_config())
        download = next(r for r in results if "Download" in r.gate)
        assert download.verdict == NOT_ASSESSED
        assert "skipped" in download.detail

    def test_the_floor_detail_names_the_exclusion(self):
        from saorsa_deploy.gate_comparison import _hard_success_floors
        from saorsa_deploy.gate_config import load_gate_config

        run = SimpleNamespace(
            upload_records=[],
            download_records=[self._record("success")] * 4 + [self._record("skipped")] * 2,
        )
        results = _hard_success_floors(run, load_gate_config())
        download = next(r for r in results if "Download" in r.gate)
        # Stated rather than implicit: the denominator is smaller than the row
        # count, and a reader comparing the two would think ops went missing.
        assert "2 skipped by the transport, excluded" in download.detail

    def test_the_aggregate_counts_skips_separately(self):
        from saorsa_deploy.influx import aggregate_file_results

        points = [
            {
                "_measurement": "download",
                "host": "h",
                "file_size": 1024,
                "status": status,
                "start_ns": i,
                "total_ms": 10,
                "_time": datetime(2026, 9, 25, tzinfo=timezone.utc),
            }
            for i, status in enumerate([0, 0, 1, 2, 2])
        ]
        agg = aggregate_file_results(points)[0]
        assert (agg["success_count"], agg["failure_count"], agg["skipped_count"]) == (
            2,
            1,
            2,
        )


class TestTheSlackReply:
    def test_skips_and_crashes_are_reported_beside_the_totals(self):
        from saorsa_deploy.slack_results import file_ops_result_blocks

        aggregates = [
            {
                "client_hostname": "h",
                "op_type": "download",
                "file_size_bytes": 1024,
                "success_count": 4,
                "failure_count": 0,
                "avg_duration_ms": 10,
                "min_duration_ms": 5,
                "max_duration_ms": 20,
            }
        ]
        op_records = [
            {"status": "skipped", "client_kind": "wasm", "op_type": "download"},
            {
                "status": "failure",
                "client_kind": "wasm",
                "op_type": "download",
                "error_kind": "crash",
            },
        ]
        text = str(file_ops_result_blocks(aggregates, op_records=op_records))
        assert "Skipped (over-cap)" in text
        assert "Crashes (killed by signal)" in text

    def test_a_native_run_with_no_skips_renders_as_before(self):
        from saorsa_deploy.slack_results import file_ops_result_blocks

        aggregates = [
            {
                "client_hostname": "h",
                "op_type": "download",
                "file_size_bytes": 1024,
                "success_count": 2,
                "failure_count": 1,
                "avg_duration_ms": 10,
                "min_duration_ms": 5,
                "max_duration_ms": 20,
            }
        ]
        text = str(file_ops_result_blocks(aggregates))
        assert "Total successes" in text
        assert "Skipped" not in text
        assert "WASM" not in text


class TestTheDeployPost:
    def _blocks(self, deploy_args):
        from saorsa_deploy.cmd.slack_post import _prod_downloader_blocks

        return str(_prod_downloader_blocks(deploy_args))

    def test_a_wasm_run_names_the_bundle_commit_and_the_probe_result(self):
        text = self._blocks(
            {
                "kind": "prod-downloaders",
                "spec": "lon1:s-4vcpu-8gb",
                "client_kind": "wasm",
                "wasm_build": {
                    "commit": "abcdef1234567890",
                    "ref": "ant-cli-v0.3.8-rc.1",
                    "repo_owner": "WithAutonomi",
                    "node_datachannel_version": "0.33.2",
                },
                "wasm_bootstrap": {"total": 7, "answered": 7},
            }
        )
        assert "abcdef123456" in text
        assert "7/7 answered" in text
        assert "0.33.2" in text
        assert "wasm (browser path" in text

    def test_a_partial_probe_is_marked(self):
        text = self._blocks(
            {
                "kind": "prod-downloaders",
                "spec": "lon1:s-4vcpu-8gb",
                "client_kind": "wasm",
                "wasm_bootstrap": {"total": 7, "answered": 5},
            }
        )
        assert "5/7 answered" in text
        assert "⚠️" in text

    def test_a_missing_sidecar_says_so_rather_than_claiming_a_commit(self):
        text = self._blocks(
            {
                "kind": "prod-downloaders",
                "spec": "lon1:s-4vcpu-8gb",
                "client_kind": "wasm",
            }
        )
        assert "no build provenance recorded" in text

    def test_a_native_run_still_shows_its_client_binary(self):
        text = self._blocks(
            {
                "kind": "prod-downloaders",
                "spec": "lon1",
                "ant_binary": {"version": "0.3.8"},
            }
        )
        assert "Client binary" in text
        assert "WASM bundle" not in text
        assert "Transport" not in text


class TestTheInventory:
    def _config(self, deploy_args):
        from saorsa_deploy.cmd.generate_inventory import build_client_config

        return build_client_config(deploy_args)

    def test_a_wasm_run_records_the_build_the_probe_and_the_paths(self):
        config = self._config(
            {
                "kind": "prod-downloaders",
                "client_kind": "wasm",
                "wasm_build": {"commit": "abc", "ref": "r"},
                "wasm_bootstrap": {"total": 7, "answered": 7},
                "addresses": ["a"],
            }
        )
        assert config["client_kind"] == "wasm"
        assert config["wasm_build"]["commit"] == "abc"
        assert config["wasm_bootstrap"]["answered"] == 7
        # The teardown-critical part: nothing collects these and they die with
        # the VM.
        assert config["paths"]["ant_wasm_cli"] == "/usr/local/bin/ant-wasm"
        assert config["paths"]["ant_wasm_bundle_dir"] == "/opt/ant-wasm-client"
        assert config["paths"]["ant_wasm_build_info"].endswith("build-info.json")

    def test_a_native_run_carries_no_wasm_paths(self):
        config = self._config({"kind": "prod-continuous-downloader", "addresses": ["a"]})
        assert config["client_kind"] == "native"
        assert config["wasm_build"] is None
        assert config["wasm_bootstrap"] is None
        assert config["paths"]["ant_wasm_cli"] is None

    def test_the_schema_version_was_bumped_and_the_loader_accepts_it(self):
        # A consumer reading `client_config.binary` to find out what ran would be
        # wrong on a WASM deployment, so this is a bump rather than a quiet
        # addition.
        from saorsa_deploy.cmd.generate_inventory import INVENTORY_SCHEMA_VERSION
        from saorsa_deploy.inventory import SUPPORTED_SCHEMA_VERSIONS

        assert INVENTORY_SCHEMA_VERSION == 7
        assert INVENTORY_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


class TestTheCliGuards:
    def _args(self, **kwargs):
        base = dict(
            name="X",
            spec="lon1:s-4vcpu-8gb",
            client_kind="wasm",
            provider="digitalocean",
            wasm_branch_name="ant-cli-v0.3.8-rc.1",
            wasm_repo_owner="WithAutonomi",
            bootstrap_cache=True,
            memory_profiling=False,
            download_diagnostics=False,
        )
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_the_forbidden_flags_exit_non_zero(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import check_wasm_flags

        for kwargs in (
            {"memory_profiling": True},
            {"download_diagnostics": True},
            {"bootstrap_cache": False},
        ):
            with pytest.raises(SystemExit) as exc:
                check_wasm_flags(self._args(**kwargs), Console())
            assert exc.value.code == 1

    def test_a_native_run_is_never_checked(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import check_wasm_flags

        # All three are legitimate on the native path.
        check_wasm_flags(
            self._args(
                client_kind="native",
                memory_profiling=True,
                download_diagnostics=True,
                bootstrap_cache=False,
            ),
            Console(),
        )

    def test_a_sizeless_spec_gets_the_wasm_default(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import apply_wasm_vm_size

        args = self._args(spec="lon1")
        apply_wasm_vm_size(args, Console())
        assert args.spec == "lon1:s-4vcpu-8gb"

    def test_a_sizeless_spec_on_another_provider_is_left_alone(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import apply_wasm_vm_size

        # Every other provider's sizes are its own vocabulary; the command's
        # existing per-provider default stands.
        args = self._args(spec="fsn1", provider="hetzner")
        apply_wasm_vm_size(args, Console())
        assert args.spec == "fsn1"

    def test_an_undersized_slug_exits(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import apply_wasm_vm_size

        with pytest.raises(SystemExit):
            apply_wasm_vm_size(self._args(spec="lon1:s-2vcpu-4gb"), Console())

    def test_a_native_run_is_never_resized(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import apply_wasm_vm_size

        args = self._args(client_kind="native", spec="lon1")
        apply_wasm_vm_size(args, Console())
        assert args.spec == "lon1"

    def test_a_wasm_run_without_a_branch_exits(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import resolve_wasm_context

        with pytest.raises(SystemExit):
            resolve_wasm_context(self._args(wasm_branch_name=None), Console())

    def test_a_native_run_resolves_no_context(self):
        from rich.console import Console

        from saorsa_deploy.cmd.prod_wasm_cli import resolve_wasm_context

        assert resolve_wasm_context(self._args(client_kind="native"), Console()) is None

    def test_the_deploy_args_always_name_the_transport(self):
        from saorsa_deploy.cmd.prod_wasm_cli import WasmContext, wasm_deploy_args

        # A missing key would read as "nobody thought about it".
        assert wasm_deploy_args(None) == {"client_kind": "native"}
        recorded = wasm_deploy_args(
            WasmContext(
                bundle_url="u",
                bootstrap_multiaddrs=[MA],
                bootstrap_record={"total": 7, "answered": 7},
                build_info={"commit": "abc"},
            )
        )
        assert recorded["client_kind"] == "wasm"
        assert recorded["wasm_build"] == {"commit": "abc"}
        assert recorded["wasm_bootstrap"]["answered"] == 7


class TestTheContinuousKindForwardsItsNetworkProfile:
    def test_the_infra_spec_carries_the_network_token(self, monkeypatch):
        # Dropped here before this change (the one-shot kind always passed it),
        # so a shaped continuous run reported as unshaped everywhere client_state
        # is read.
        import saorsa_deploy.cmd.prod_continuous_downloader as mod

        captured = {}
        monkeypatch.setattr(
            "saorsa_deploy.cmd.infra_clients.cmd_infra_clients",
            lambda args: captured.update({"downloader": args.downloader}),
        )
        monkeypatch.setattr(mod, "_client_stack_exists", lambda name: False)
        from rich.console import Console

        mod._ensure_client_infra(
            SimpleNamespace(
                name="X",
                spec="lon1:s-2vcpu-4gb",
                provider="digitalocean",
                bootstrap_cache=True,
                network="home-calibrated",
            ),
            Console(),
        )
        assert "network=home-calibrated" in captured["downloader"][0]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    from saorsa_db.db import Base
    from saorsa_db.repositories import SqlAlchemyDeploymentRepository
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


_STARTED = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
_FINISHED = datetime(2026, 9, 25, 10, 5, tzinfo=timezone.utc)


def _download_op(deployment_id, **over):
    from saorsa_db.models import OpRecordStatus

    op = dict(
        deployment_id=deployment_id,
        client_hostname="PROD-DL-01-ant-client-download-lon1-1",
        region="lon1",
        file_size_bytes=1024,
        status=OpRecordStatus.SUCCESS,
        start_ns=1_776_615_435_704_524_798,
        influx_time=_FINISHED,
        started_at=_STARTED,
        finished_at=_FINISHED,
        duration_ms=1000,
        client_kind="wasm",
    )
    op.update(over)
    return op


class TestTheDownloadRecordCarriesItsAddress:
    def test_the_address_is_persisted(self, repo):
        d = repo.create(name="PROD-DL-01", deploy_args={})
        row = repo.upsert_download_record(**_download_op(d.id, address="abc123"))
        assert row.address == "abc123"
        assert row.to_dict()["address"] == "abc123"

    def test_a_row_recorded_without_one_reads_as_None(self, repo):
        # No past row can be backfilled: the Influx points they came from never
        # held an address.
        d = repo.create(name="PROD-DL-01", deploy_args={})
        assert repo.upsert_download_record(**_download_op(d.id)).address is None

    def test_a_re_run_never_erases_a_recorded_address(self, repo):
        # Same COALESCE reasoning as `client_kind`: a re-run against points
        # written before the field existed must not lose what is already stored.
        d = repo.create(name="PROD-DL-01", deploy_args={})
        repo.upsert_download_record(**_download_op(d.id, address="abc123"))
        again = repo.upsert_download_record(**_download_op(d.id, address=None))
        assert again.address == "abc123"

    def test_a_skipped_row_is_stored(self, repo):
        from saorsa_db.models import OpRecordStatus

        d = repo.create(name="PROD-DL-01", deploy_args={})
        row = repo.upsert_download_record(
            **_download_op(d.id, status=OpRecordStatus.SKIPPED, address="big")
        )
        assert row.status is OpRecordStatus.SKIPPED
        # Stored, not discarded: a skip is a real event and the operator needs to
        # see that the address list held an entry this transport cannot carry.
        rows = repo.list_download_records_for_deployment(d.id)
        assert [r.status for r in rows] == [OpRecordStatus.SKIPPED]


class TestTheNativeWalkStillWorks:
    """The loop is shared, so the native path has to be exercised through it too.

    Before this change the transport dispatch did not exist; the risk of adding
    it is that the branch nobody was changing is the one that breaks.
    """

    def test_a_native_pass_records_one_point_per_address(self, service, tmp_path, monkeypatch):
        fake = "#!/bin/sh\necho 'Time: 1.25'\n"
        posted = TestTheServiceWalk()._run(
            service,
            tmp_path,
            monkeypatch,
            addresses="a\nb\n",
            fake_cli=fake,
            wasm=False,
        )
        assert len(posted) == 2
        # `Time:` out of ant's stdout, exactly as before — not the JSON line the
        # WASM CLI prints.
        assert all("download_secs=1.25" in line for line in posted)
        assert all("status=0i" in line for line in posted)

    def test_a_native_failure_is_a_failure_not_a_skip(self, service, tmp_path, monkeypatch):
        fake = "#!/bin/sh\necho boom 1>&2\nexit 3\n"
        posted = TestTheServiceWalk()._run(
            service,
            tmp_path,
            monkeypatch,
            addresses="a\n",
            fake_cli=fake,
            wasm=False,
        )
        assert "status=1i" in posted[0]
        assert "error_kind" not in posted[0]

    def test_a_native_run_ignores_a_declared_size(self, service, tmp_path, monkeypatch):
        # MAX_DOWNLOAD_BYTES is ant-core's BROWSER cap; the native client has no
        # such limit, so a sized line must still be attempted. This caught the
        # skip being gated on the value alone rather than on the transport: the
        # provisioner never writes the key for a native run, so nothing else
        # would have found it until someone set it by hand.
        fake = "#!/bin/sh\necho 'Time: 0.5'\n"
        posted = TestTheServiceWalk()._run(
            service,
            tmp_path,
            monkeypatch,
            addresses="big 2000000000\n",
            fake_cli=fake,
            wasm=False,
        )
        assert "status=0i" in posted[0]


class TestTheDeployTimeAddressValidator:
    """`parse_addresses` gates the deploy, and it had to learn the sized form.

    Found by the live verification, not by a unit test: the SERVICE's parser was
    covered, but the validator the deploy command runs first still demanded
    exactly one token per line — so the very address file the feature exists to
    walk was rejected before any VM was created.
    """

    ADDR = "e6c97f1d0365fe715b6e4796d3b66a78938dbead94f451fa22154648457d6db2"
    OTHER = "d91f87e78c6cb98cb7f380acfba4394c0a0644dc21a573a6f3e191571d5a092c"

    def _parse(self, content):
        from saorsa_deploy.cmd.provision_clients import parse_addresses

        return parse_addresses(content)

    def test_a_bare_line_is_unchanged(self):
        # Every native run's file looks like this, and must record what it always
        # did.
        assert self._parse(f"{self.ADDR}\n") == [self.ADDR]

    def test_a_sized_line_keeps_its_size(self):
        # Kept rather than stripped: the declared size is what makes an over-cap
        # entry skippable without a fetch, so `deploy_args.addresses` has to say
        # which entries carried one — otherwise a `skipped` row has nothing
        # explaining it.
        assert self._parse(f"{self.ADDR} 512550\n") == [f"{self.ADDR} 512550"]

    def test_comments_and_blanks_are_dropped(self):
        content = f"# a note\n\n{self.ADDR}\n   \n{self.OTHER} 42\n"
        assert self._parse(content) == [self.ADDR, f"{self.OTHER} 42"]

    def test_a_pasted_download_command_is_still_caught(self):
        # The check exists for this mistake; accepting a second token must not
        # have opened it up.
        with pytest.raises(ValueError, match="tokens"):
            self._parse(f"ant file download {self.ADDR} -o out.bin\n")

    def test_a_non_numeric_second_token_is_refused(self):
        # `<addr> -o` is two tokens, so only the numeric check catches it.
        with pytest.raises(ValueError, match="not a whole number of bytes"):
            self._parse(f"{self.ADDR} -o\n")

    def test_a_bad_address_is_still_refused(self):
        with pytest.raises(ValueError, match="64-char hex address"):
            self._parse("deadbeef 100\n")

    def test_the_size_is_normalised(self):
        # So the recorded list is a function of the addresses, not of how the
        # operator happened to pad the file.
        assert self._parse(f"{self.ADDR} 007\n") == [f"{self.ADDR} 7"]

    def test_the_count_ignores_comment_lines(self, tmp_path, capsys):
        # An 11-comment, 6-address file reported "17 address(es)" before this.
        from rich.console import Console

        from saorsa_deploy.cmd.provision_clients import (
            _resolve_addresses_file_content,
        )

        path = tmp_path / "addrs.txt"
        path.write_text(f"# one\n# two\n{self.ADDR}\n{self.OTHER} 5\n")
        # A wide console, so Rich does not wrap the count across two lines.
        _resolve_addresses_file_content(
            SimpleNamespace(addresses_file=str(path)), Console(width=200)
        )
        assert "2 address(es)" in capsys.readouterr().out
