"""Inventory generation for the client-only prod download deployments.

`prod downloaders` / `prod continuous-downloader` never write deployment state
— they join production rather than standing up their own nodes — so the
node-oriented path exits before it can describe them. But their inventory is
not meaningless: the VM IP, the address list the run walks, and the settings
that change how `ant` is invoked are exactly what you need to interpret a
result, and to know which artefacts to pull off the VM before destroy.
"""

import json
from types import SimpleNamespace

import pytest

from saorsa_deploy.cmd.generate_inventory import (
    INVENTORY_SCHEMA_VERSION,
    build_client_config,
    build_client_inventory,
)
from saorsa_deploy.inventory import SUPPORTED_SCHEMA_VERSIONS


def _clients():
    return [
        {
            "client_index": 1,
            "region": "fsn1",
            "ip": "203.0.113.10",
            "role": "download",
            "provider": "hetzner",
            "provider_id": "hz-1",
            "vm_size": "cpx32",
            "bootstrap_cache": True,
        }
    ]


def _deploy_args(**overrides):
    base = {
        "kind": "prod-downloaders",
        "prod_bootstraps_url": "https://example.invalid/bootstrap_peers.toml",
        "addresses_file": "/home/chris/downloads/addresses.txt",
        "addresses": ["aaa", "bbb", "ccc"],
        "monitoring_name": "PROD-DL-02",
        "spec": "fsn1:cpx32",
        "provider": "hetzner",
        "monitoring_provider": "ovh",
        "bootstrap_cache": True,
        "download_diagnostics": True,
        "memory_profiling": False,
        "forward_logs": False,
        "rust_log": None,
        "clock_skew": None,
        "network": None,
        "ant_binary": {
            "kind": "branch",
            "branch": "diagnostics/v2-903-download-request-timing",
            "repo_owner": "WithAutonomi",
            "debug": False,
        },
    }
    base.update(overrides)
    return base


class TestClientConfig:
    def test_carries_the_address_list_and_count(self):
        config = build_client_config(_deploy_args())
        assert config["addresses"] == ["aaa", "bbb", "ccc"]
        assert config["address_count"] == 3
        assert config["addresses_file"] == "/home/chris/downloads/addresses.txt"

    def test_records_the_binary_the_run_used(self):
        config = build_client_config(_deploy_args())
        assert config["binary"]["branch"] == ("diagnostics/v2-903-download-request-timing")
        assert config["binary"]["repo_owner"] == "WithAutonomi"

    def test_one_shot_is_derived_from_the_kind(self):
        assert build_client_config(_deploy_args())["one_shot"] is True
        continuous = build_client_config(
            _deploy_args(kind="prod-continuous-downloader", daily_at="02:00")
        )
        assert continuous["one_shot"] is False
        assert continuous["settings"]["daily_at"] == "02:00"

    def test_a_deployment_predating_the_anchor_reports_its_interval(self):
        """The live permanent monitors carry only the old key."""
        legacy = build_client_config(
            _deploy_args(kind="prod-continuous-downloader", sleep_interval_secs=86400)
        )
        assert legacy["settings"]["sleep_interval_secs"] == 86400
        assert legacy["settings"]["daily_at"] is None

    def test_settings_capture_how_ant_is_invoked(self):
        config = build_client_config(
            _deploy_args(rust_log="debug", memory_profiling=True, clock_skew=7200)
        )
        settings = config["settings"]
        assert settings["rust_log"] == "debug"
        assert settings["memory_profiling"] is True
        assert settings["clock_skew_seconds"] == 7200
        assert settings["bootstrap_cache"] is True

    def test_diagnostics_glob_is_present_only_when_enabled(self):
        on = build_client_config(_deploy_args(download_diagnostics=True))
        assert on["paths"]["diagnostics_glob"].endswith("*.diag.jsonl")

        off = build_client_config(_deploy_args(download_diagnostics=False))
        assert off["paths"]["diagnostics_glob"] is None

    def test_heaptrack_dir_is_present_only_when_profiling(self):
        assert build_client_config(_deploy_args())["paths"]["heaptrack_dir"] is None
        profiled = build_client_config(_deploy_args(memory_profiling=True))
        assert profiled["paths"]["heaptrack_dir"] == "/var/log/ant-heaptrack"

    def test_paths_point_at_the_real_service_locations(self):
        """These are how an operator finds the artefacts before teardown.

        Nothing collects the per-op logs or the diagnostics JSONL
        automatically, and they die with the VM.
        """
        paths = build_client_config(_deploy_args())["paths"]
        assert paths["service_name"] == "ant-prod-downloader"
        assert paths["log_dir"] == "/var/log/ant-prod-downloader"
        assert paths["ant_binary"] == "/root/.local/bin/ant"

    def test_absent_deploy_args_yields_none(self):
        assert build_client_config(None) is None
        assert build_client_config({}) is None


class TestClientInventory:
    def test_has_the_vm_ip_and_ssh_details(self):
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            ssh_key="/k",
            deploy_args=_deploy_args(),
            created_at="2026-08-11T00:00:00Z",
        )
        vm = inv["client_vms"][0]
        assert vm["ip"] == "203.0.113.10"
        assert vm["provider"] == "hetzner"
        assert vm["region"] == "fsn1"
        assert vm["ssh_key"] == "/k"
        assert vm["services"] == ["ant-downloader"]

    def test_node_and_bootstrap_sections_are_empty(self):
        """These kinds join production; they own no nodes or bootstraps.

        Listing production's peers as our VMs would mislead any consumer that
        tried to SSH into them.
        """
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            deploy_args=_deploy_args(),
            created_at="2026-08-11T00:00:00Z",
        )
        assert inv["node_vms"] == []
        assert inv["bootstrap_vms"] == []

    def test_observability_vms_are_included(self):
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            ssh_key="/k",
            deploy_args=_deploy_args(),
            monitoring_state={"vm_ip": "203.0.113.20", "region": "UK1"},
            created_at="2026-08-11T00:00:00Z",
        )
        roles = {v["role"]: v for v in inv["observability_vms"]}
        assert roles["influxdb"]["ip"] == "203.0.113.20"

    def test_firewalls_reflect_the_client_provider(self):
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            deploy_args=_deploy_args(),
            created_at="2026-08-11T00:00:00Z",
        )
        assert set(inv["firewalls"]) == {"hetzner"}

    def test_top_level_shape_matches_the_node_inventory(self):
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            deploy_args=_deploy_args(),
            created_at="2026-08-11T00:00:00Z",
        )
        assert set(inv.keys()) == {
            "schema_version",
            "testnet_id",
            "created_at",
            "binary",
            "firewalls",
            "bootstrap_vms",
            "node_vms",
            "client_vms",
            "observability_vms",
            "client_config",
        }
        assert inv["schema_version"] == INVENTORY_SCHEMA_VERSION

    def test_config_is_none_without_registry_args(self):
        """An unreachable registry degrades the inventory, never fails it."""
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            deploy_args=None,
            created_at="2026-08-11T00:00:00Z",
        )
        assert inv["client_config"] is None
        assert inv["client_vms"][0]["ip"] == "203.0.113.10"

    def test_no_clients_is_an_error(self):
        with pytest.raises(ValueError, match="No client VMs"):
            build_client_inventory("PROD-DL-02", [], deploy_args=_deploy_args())

    def test_is_json_serialisable(self):
        inv = build_client_inventory(
            "PROD-DL-02",
            _clients(),
            deploy_args=_deploy_args(),
            created_at="2026-08-11T00:00:00Z",
        )
        assert json.loads(json.dumps(inv))["client_config"]["address_count"] == 3


class TestTheCommandFallsBackToTheClientPath:
    """No deployment state is the NORMAL shape for these kinds, not an error."""

    def _run(self, monkeypatch, tmp_path, *, clients, deploy_args, kind_state=None):
        from saorsa_deploy.cmd import generate_inventory as mod

        def _no_state(name):
            raise RuntimeError(f"No deployment state found for '{name}'")

        monkeypatch.setattr(mod, "load_deployment_state", kind_state or _no_state)
        monkeypatch.setattr(mod, "load_client_state", lambda name: {"clients": clients})
        monkeypatch.setattr(
            mod,
            "load_monitoring_state",
            lambda name: (_ for _ in ()).throw(RuntimeError("none")),
        )
        monkeypatch.setattr(
            mod,
            "load_elastic_state",
            lambda name: (_ for _ in ()).throw(RuntimeError("none")),
        )
        monkeypatch.setattr(mod, "_load_deploy_args", lambda name: deploy_args)

        out = tmp_path / "inventory.json"
        mod.cmd_generate_inventory(
            SimpleNamespace(
                name="PROD-DL-02",
                ssh_key_path="/k",
                node_count=None,
                output=str(out),
                wallet_secret_key=None,
            )
        )
        return json.loads(out.read_text())

    def test_writes_an_inventory_for_a_prod_downloader(self, monkeypatch, tmp_path):
        inv = self._run(monkeypatch, tmp_path, clients=_clients(), deploy_args=_deploy_args())
        assert inv["client_vms"][0]["ip"] == "203.0.113.10"
        assert inv["client_config"]["address_count"] == 3

    def test_another_kind_still_gets_its_vm_details(self, monkeypatch, tmp_path):
        """client_config's on-VM paths are downloader-specific, so drop it —
        but the VM sections are still accurate and worth writing."""
        inv = self._run(
            monkeypatch,
            tmp_path,
            clients=_clients(),
            deploy_args=_deploy_args(kind="prod-uploaders"),
        )
        assert inv["client_config"] is None
        assert inv["client_vms"][0]["ip"] == "203.0.113.10"

    def test_no_state_and_no_clients_still_errors(self, monkeypatch, tmp_path):
        """A genuinely absent deployment must not yield an empty inventory."""
        with pytest.raises(SystemExit):
            self._run(monkeypatch, tmp_path, clients=[], deploy_args=_deploy_args())


def test_node_inventory_loader_accepts_the_new_schema_version():
    """Bumping the emitted version must not orphan the prod fleet tooling."""
    assert INVENTORY_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


class TestUpGeneratesTheInventory:
    """`deployments up` must snapshot the run, not leave it to be remembered.

    The per-op logs and the diagnostics JSONL live only on the client VM and
    die with it; the address list lives only in the registry. Without this step
    there is no local record of what the run actually did.
    """

    def _steps(self, kind, spec_key, spec):
        from saorsa_deploy.config.models import Manifest
        from saorsa_deploy.orchestrator import build_deploy_steps

        m = Manifest.model_validate(
            {
                "name": "PROD-DL-02",
                "kind": kind,
                "purpose": "test",
                spec_key: spec,
            }
        )
        return build_deploy_steps(m)

    def _labels(self, steps):
        return [s.label for s in steps]

    def test_one_shot_downloaders_get_an_inventory_step(self):
        steps = self._steps(
            "prod-downloaders",
            "prod_downloaders",
            {"spec": "fsn1:cpx32", "binary": {"version": "0.2.7"}},
        )
        assert "Generate inventory snapshot" in self._labels(steps)

    def test_continuous_downloader_gets_an_inventory_step(self):
        steps = self._steps(
            "prod-continuous-downloader",
            "prod_continuous_downloader",
            {"spec": "fsn1:cpx32", "binary": {"version": "0.2.7"}},
        )
        assert "Generate inventory snapshot" in self._labels(steps)

    def test_it_runs_after_the_deploy(self):
        """The inventory reads client state, which the deploy step creates."""
        steps = self._steps(
            "prod-downloaders",
            "prod_downloaders",
            {"spec": "fsn1:cpx32", "binary": {"version": "0.2.7"}},
        )
        labels = self._labels(steps)
        deploy_at = labels.index("Deploy prod one-shot downloaders + monitoring")
        inventory_at = labels.index("Generate inventory snapshot")
        assert deploy_at < inventory_at

    def test_the_argv_carries_name_and_ssh_key(self):
        steps = self._steps(
            "prod-downloaders",
            "prod_downloaders",
            {"spec": "fsn1:cpx32", "binary": {"version": "0.2.7"}},
        )
        argv = next(s.argv for s in steps if s.label == "Generate inventory snapshot")
        assert argv[:2] == ["deployments", "generate-inventory"]
        assert argv[argv.index("--name") + 1] == "PROD-DL-02"
        assert "--ssh-key-path" in argv

    def test_kinds_without_an_inventory_path_get_no_step(self):
        """Only the download kinds can be described today.

        Emitting the step for a kind generate-inventory cannot handle would
        fail the run at its final step, after all the infrastructure is up.
        """
        steps = self._steps(
            "prod-canary",
            "prod_canary",
            {
                "binary": {"version": "0.2.7"},
                "upload_spec": "lon1",
                "probes": [{"region": "lon1", "count": 1}],
                "urls_file": "/tmp/urls.txt",
            },
        )
        assert "Generate inventory snapshot" not in self._labels(steps)
