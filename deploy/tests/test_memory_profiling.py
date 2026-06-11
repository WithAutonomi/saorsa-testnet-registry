"""Unit tests for the prod `memory_profiling` (heaptrack) option.

Covers spec/flag parsing (uploaders + downloaders), the orchestrator emissions,
and the provisioner-level wiring. The service-script command wrapping is tested
in test_prod_uploader_service.py / test_prod_downloader_service.py.
"""
import pytest

from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec
from saorsa_deploy.config.models import (
    Binary,
    ProdContinuousDownloaderSpec,
    ProdUploader,
)
from saorsa_deploy.orchestrator.prod import (
    _memory_profiling_flag,
    _prod_uploader_spec,
)
from saorsa_deploy.provisioning.prod_downloader import ProdDownloaderProvisioner
from saorsa_deploy.provisioning.prod_uploader import ProdUploaderProvisioner


def _binary():
    return Binary(branch="rc-2026.6.2", repo_owner="WithAutonomi")


class TestUploaderSpecParsing:
    def test_true(self, tmp_path):
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        d = parse_prod_uploader_spec(f"lon1:s-4vcpu-8gb:urls={urls}:memory_profiling=true")
        assert d["memory_profiling"] is True

    def test_false(self, tmp_path):
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        d = parse_prod_uploader_spec(f"lon1:s-4vcpu-8gb:urls={urls}:memory_profiling=false")
        assert d["memory_profiling"] is False

    def test_default_none(self, tmp_path):
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        d = parse_prod_uploader_spec(f"lon1:s-4vcpu-8gb:urls={urls}")
        assert d["memory_profiling"] is None

    def test_invalid_value_rejected(self, tmp_path):
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        with pytest.raises(ValueError, match="memory_profiling must be"):
            parse_prod_uploader_spec(f"lon1:s-4vcpu-8gb:urls={urls}:memory_profiling=yes")

    def test_rust_log_stays_last_after_memory_profiling(self, tmp_path):
        # rust_log value contains '::' and a ',' — it must survive intact even
        # though memory_profiling sits before it in the spec.
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        rl = "ant_cli=info,saorsa_core::dht=debug"
        d = parse_prod_uploader_spec(
            f"lon1:s-4vcpu-8gb:urls={urls}:memory_profiling=true:rust_log={rl}"
        )
        assert d["memory_profiling"] is True
        assert d["rust_log"] == rl


class TestOrchestratorEmission:
    def test_uploader_token_before_rust_log(self):
        u = ProdUploader(
            region="lon1", vm_size="s-4vcpu-8gb", urls="/tmp/u.txt",
            memory_profiling=True, rust_log="ant_cli=info,saorsa_core::dht=debug",
        )
        spec = _prod_uploader_spec(u)
        assert ":memory_profiling=true:" in spec
        # rust_log must be the trailing token (its value contains ':').
        assert spec.endswith(":rust_log=ant_cli=info,saorsa_core::dht=debug")

    def test_uploader_token_omitted_when_unset(self):
        u = ProdUploader(region="lon1", vm_size="s-4vcpu-8gb", urls="/tmp/u.txt")
        assert "memory_profiling" not in _prod_uploader_spec(u)

    def test_downloader_flag_true(self):
        spec = ProdContinuousDownloaderSpec(
            spec="lon1", binary=_binary(), memory_profiling=True
        )
        assert _memory_profiling_flag(spec) == ["--memory-profiling"]

    def test_downloader_flag_false(self):
        spec = ProdContinuousDownloaderSpec(
            spec="lon1", binary=_binary(), memory_profiling=False
        )
        assert _memory_profiling_flag(spec) == ["--no-memory-profiling"]

    def test_downloader_flag_unset(self):
        spec = ProdContinuousDownloaderSpec(spec="lon1", binary=_binary())
        assert _memory_profiling_flag(spec) == []


class TestProvisionerWiring:
    def test_uploader_any_memory_profiling(self):
        p = ProdUploaderProvisioner(
            uploaders=[
                {"ip": "1.1.1.1", "region": "lon1", "client_index": 1},
                {"ip": "2.2.2.2", "region": "nyc1", "client_index": 2,
                 "memory_profiling": True},
            ],
            bootstrap_peers=[], wallet_secret_key="0xabc",
        )
        assert p._any_memory_profiling() is True

    def test_uploader_none_memory_profiling(self):
        p = ProdUploaderProvisioner(
            uploaders=[{"ip": "1.1.1.1", "region": "lon1", "client_index": 1}],
            bootstrap_peers=[], wallet_secret_key="0xabc",
        )
        assert p._any_memory_profiling() is False

    def test_downloader_stores_flag(self):
        p = ProdDownloaderProvisioner(
            downloaders=[{"ip": "1.1.1.1", "region": "lon1"}],
            bootstrap_peers=[], sleep_interval_secs=0, memory_profiling=True,
        )
        assert p.memory_profiling is True
