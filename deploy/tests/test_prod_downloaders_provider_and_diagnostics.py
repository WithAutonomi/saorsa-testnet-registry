"""Provider selection and --download-diagnostics on the one-shot downloader kind.

Both options are only useful if they survive the whole path an operator uses: a
manifest field, through validation, into the argv the orchestrator emits, past
argparse, into the client-infra spec token / the service env file, and finally
onto the `ant` command line. Each hop below is one that would silently drop
them — and a silently dropped diagnostics flag produces a run that looks
successful while yielding none of the evidence it exists for.
"""

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps

SERVICE_PATH = (
    Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts/prod_downloader.py"
)


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("prod_downloader", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(**downloader_fields):
    body = {
        "name": "PROD-DL-TEST",
        "kind": "prod-downloaders",
        "purpose": "test",
        "prod_downloaders": {
            "spec": "fsn1:cpx32",
            "binary": {"version": "0.2.7"},
            **downloader_fields,
        },
    }
    return Manifest.model_validate(body)


def _deploy_argv(manifest):
    """The `prod downloaders deploy` step's argv (skipping any build step)."""
    steps = build_deploy_steps(manifest)
    for step in steps:
        if step.argv[:3] == ["prod", "downloaders", "deploy"]:
            return step.argv
    raise AssertionError("no prod downloaders deploy step emitted")


class TestTheOrchestratorEmitsTheFlags:
    def test_provider_fields_become_flags(self):
        argv = _deploy_argv(
            _manifest(
                provider="hetzner",
                monitoring_provider="ovh",
                monitoring_region="UK1",
                monitoring_droplet_size="b3-8",
            )
        )
        assert "--provider" in argv
        assert argv[argv.index("--provider") + 1] == "hetzner"
        assert argv[argv.index("--monitoring-provider") + 1] == "ovh"
        assert argv[argv.index("--monitoring-region") + 1] == "UK1"
        assert argv[argv.index("--monitoring-droplet-size") + 1] == "b3-8"

    def test_absent_provider_fields_emit_no_flags(self):
        argv = _deploy_argv(_manifest())
        assert "--provider" not in argv
        assert "--monitoring-provider" not in argv

    def test_diagnostics_true_emits_the_flag(self):
        assert "--download-diagnostics" in _deploy_argv(_manifest(download_diagnostics=True))

    def test_diagnostics_false_emits_the_negation(self):
        argv = _deploy_argv(_manifest(download_diagnostics=False))
        assert "--no-download-diagnostics" in argv

    def test_diagnostics_absent_emits_nothing(self):
        argv = _deploy_argv(_manifest())
        assert "--download-diagnostics" not in argv
        assert "--no-download-diagnostics" not in argv


class TestTheCliAcceptsWhatTheOrchestratorEmits:
    """The argv a manifest produces must actually parse.

    The orchestrator tests assert on argv and the command tests build a
    Namespace directly; neither goes through argparse, so a flag defined on the
    wrong parser would pass both and still fail every real run.
    """

    def _run(self, monkeypatch, argv):
        from saorsa_deploy.cmd import prod_downloaders as module
        from saorsa_deploy.main import main

        captured = {}
        monkeypatch.setattr(
            module,
            "cmd_prod_downloaders_deploy",
            lambda args: captured.update(vars(args)),
        )
        main(argv)
        return captured

    def test_provider_and_diagnostics_reach_the_command(self, monkeypatch):
        args = self._run(
            monkeypatch,
            _deploy_argv(
                _manifest(
                    provider="hetzner",
                    monitoring_provider="ovh",
                    monitoring_region="UK1",
                    monitoring_droplet_size="b3-8",
                    download_diagnostics=True,
                )
            ),
        )
        assert args["provider"] == "hetzner"
        assert args["monitoring_provider"] == "ovh"
        assert args["monitoring_region"] == "UK1"
        assert args["monitoring_droplet_size"] == "b3-8"
        assert args["download_diagnostics"] is True

    def test_defaults_keep_the_pre_existing_digitalocean_behaviour(self, monkeypatch):
        args = self._run(monkeypatch, _deploy_argv(_manifest()))
        assert args["provider"] == "digitalocean"
        assert args["monitoring_provider"] == "digitalocean"
        assert args["download_diagnostics"] is False

    def test_continuous_downloader_also_parses_diagnostics(self, monkeypatch):
        """The continuous spec inherits the field, so its CLI must accept it.

        Otherwise a manifest could set download_diagnostics on that kind and
        have it silently dropped.
        """
        from saorsa_deploy.cmd import prod_continuous_downloader as module
        from saorsa_deploy.main import main

        m = Manifest.model_validate(
            {
                "name": "PROD-DL-CONT",
                "kind": "prod-continuous-downloader",
                "purpose": "test",
                "prod_continuous_downloader": {
                    "spec": "fsn1:cpx32",
                    "binary": {"version": "0.2.7"},
                    "download_diagnostics": True,
                },
            }
        )
        argv = build_deploy_steps(m)[0].argv
        captured = {}
        monkeypatch.setattr(
            module,
            "cmd_prod_continuous_downloader_deploy",
            lambda args: captured.update(vars(args)),
        )
        main(argv)
        assert captured["download_diagnostics"] is True


class TestClientInfraUsesTheChosenProvider:
    """The provider prefix on the downloader spec token was hardcoded."""

    def _capture_spec(self, monkeypatch, args):
        from saorsa_deploy.cmd import prod_downloaders

        monkeypatch.setattr(prod_downloaders, "_client_stack_exists", lambda name: False)
        captured = {}
        monkeypatch.setattr(
            "saorsa_deploy.cmd.infra_clients.cmd_infra_clients",
            lambda a: captured.update(vars(a)),
        )
        from rich.console import Console

        prod_downloaders._ensure_client_infra(args, Console())
        return captured["downloader"][0]

    def test_hetzner_is_honoured(self, monkeypatch):
        spec = self._capture_spec(
            monkeypatch,
            SimpleNamespace(
                name="X",
                spec="fsn1:cpx32",
                provider="hetzner",
                bootstrap_cache=True,
                network=None,
            ),
        )
        assert spec.startswith("hetzner:fsn1:cpx32:")

    def test_absent_provider_still_defaults_to_digitalocean(self, monkeypatch):
        spec = self._capture_spec(
            monkeypatch,
            SimpleNamespace(
                name="X",
                spec="lon1:s-2vcpu-4gb",
                provider=None,
                bootstrap_cache=True,
                network=None,
            ),
        )
        assert spec.startswith("digitalocean:lon1:s-2vcpu-4gb:")


class TestTheProvisionerRecordsTheToggle:
    def _env_file(self, **kwargs):
        from saorsa_deploy.provisioning.prod_downloader import (
            ProdDownloaderProvisioner,
        )

        captured = {}

        class _Recorder(ProdDownloaderProvisioner):
            def _add_env_and_addresses_op(self, state):
                # Capture the generated script rather than running pyinfra.
                import saorsa_deploy.provisioning.prod_downloader as mod

                real_add_op = mod.add_op
                mod.add_op = lambda s, op, name, commands: captured.update({"script": commands[0]})
                try:
                    super()._add_env_and_addresses_op(state)
                finally:
                    mod.add_op = real_add_op

        provisioner = _Recorder(
            downloaders=[
                {
                    "ip": "1.2.3.4",
                    "region": "fsn1",
                    "addresses_file_content": "abc\n",
                    "bootstrap_cache": True,
                }
            ],
            bootstrap_peers=["/ip4/1.1.1.1/udp/1/quic-v1"],
            one_shot=True,
            **kwargs,
        )
        provisioner._add_env_and_addresses_op(None)
        return captured["script"]

    def test_enabled_writes_true(self):
        assert "DOWNLOAD_DIAGNOSTICS=true" in self._env_file(download_diagnostics=True)

    def test_default_writes_false(self):
        assert "DOWNLOAD_DIAGNOSTICS=false" in self._env_file()


class TestTheServicePassesTheFlagPerDownload:
    def test_flag_is_absent_without_a_path(self, service, tmp_path):
        cmd = _built_cmd(service, tmp_path, diagnostics_path=None)
        assert "--download-diagnostics" not in cmd

    def test_flag_carries_the_path(self, service, tmp_path):
        diag = tmp_path / "0001-abc.diag.jsonl"
        cmd = _built_cmd(service, tmp_path, diagnostics_path=diag)
        assert cmd[cmd.index("--download-diagnostics") + 1] == str(diag)

    def test_the_path_is_per_operation(self, service):
        """ant opens the diagnostics file with truncate.

        A single shared path across a 20-address run would therefore leave only
        the last address's records — the per-op basename is what makes the flag
        useful at all.
        """
        first = service._safe_log_basename("aaaaaaaaaaaaaaaa", 1)
        second = service._safe_log_basename("bbbbbbbbbbbbbbbb", 2)
        assert f"{first}.diag.jsonl" != f"{second}.diag.jsonl"


def _built_cmd(service, tmp_path, *, diagnostics_path):
    """The native ant argv the service would run, without spawning anything."""
    return service._build_native_argv(
        ant_path="/root/.local/bin/ant",
        bootstrap_args=[],
        evm_network="arbitrum-one",
        address="abc",
        output_path=tmp_path / "out",
        diagnostics_path=diagnostics_path,
    )


def test_argparse_rejects_an_unknown_downloader_provider():
    """--provider is constrained, so a typo fails before any VM is created."""
    from saorsa_deploy.main import main

    with pytest.raises(SystemExit):
        main(
            [
                "prod",
                "downloaders",
                "deploy",
                "--name",
                "X",
                "--purpose",
                "p",
                "--spec",
                "fsn1:cpx32",
                "--provider",
                "contabo",
            ]
        )


def test_argparse_namespace_default_is_stable():
    """A bare parser default must match what the command's getattr assumes."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download-diagnostics",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    assert parser.parse_args([]).download_diagnostics is False
