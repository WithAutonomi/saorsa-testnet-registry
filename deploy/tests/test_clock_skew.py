import pytest

from saorsa_deploy.cmd.infra_clients import (
    _spec_signature,
    parse_downloader_spec,
    parse_uploader_spec,
)
from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec
from saorsa_deploy.config.models import Downloader, Nodes, ProviderNodes, Uploader
from saorsa_deploy.orchestrator.testnet import _downloader_spec, _uploader_spec
from saorsa_deploy.provisioning.clock_skew import (
    SKEW_MARKER,
    build_clock_skew_command,
    select_skew_ips,
    skew_direction_for_ip,
)


class TestSelectSkewIps:
    def test_zero_count_returns_empty(self):
        assert select_skew_ips({"do/lon1": ["1.1.1.1", "1.1.1.2"]}, 0) == []

    def test_negative_count_returns_empty(self):
        assert select_skew_ips({"do/lon1": ["1.1.1.1"]}, -3) == []

    def test_empty_input_returns_empty(self):
        assert select_skew_ips({}, 5) == []

    def test_empty_lanes_return_empty(self):
        assert select_skew_ips({"do/lon1": []}, 5) == []

    def test_count_larger_than_total_clamps(self):
        vm_ips = {"do/lon1": ["1.1.1.1", "1.1.1.2"]}
        assert len(select_skew_ips(vm_ips, 99)) == 2

    def test_one_host_picks_one(self):
        vm_ips = {
            "do/lon1": ["1.1.1.1", "1.1.1.2"],
            "vultr/nrt": ["2.2.2.1"],
        }
        assert len(select_skew_ips(vm_ips, 1)) == 1

    def test_spreads_across_regions_first(self):
        # With count=2 over two regions, one host should come from each.
        vm_ips = {
            "do/lon1": ["1.1.1.1", "1.1.1.2"],
            "vultr/nrt": ["2.2.2.1", "2.2.2.2"],
        }
        result = select_skew_ips(vm_ips, 2)
        assert "1.1.1.1" in result
        assert "2.2.2.1" in result

    def test_deterministic(self):
        vm_ips = {
            "do/lon1": ["1.1.1.2", "1.1.1.1"],
            "vultr/nrt": ["2.2.2.1"],
        }
        assert select_skew_ips(vm_ips, 2) == select_skew_ips(vm_ips, 2)


class TestSkewDirectionForIp:
    def test_returns_plus_or_minus_one(self):
        for ip in ("1.1.1.1", "2.2.2.2", "10.0.0.5", "203.0.113.9"):
            assert skew_direction_for_ip(ip) in (1, -1)

    def test_deterministic(self):
        assert skew_direction_for_ip("1.2.3.4") == skew_direction_for_ip("1.2.3.4")

    def test_varies_across_ips(self):
        # Across a spread of IPs we should see both directions.
        dirs = {skew_direction_for_ip(f"10.0.0.{i}") for i in range(1, 40)}
        assert dirs == {1, -1}


class TestBuildClockSkewCommand:
    def test_has_marker_guard(self):
        cmd = build_clock_skew_command(1)
        assert SKEW_MARKER in cmd
        assert "ANT_SKEW:ALREADY" in cmd
        assert "ANT_SKEW:APPLIED" in cmd

    def test_disables_time_sync(self):
        cmd = build_clock_skew_command(1)
        assert "timedatectl set-ntp false" in cmd
        assert "systemd-timesyncd" in cmd
        assert "chrony" in cmd

    def test_positive_direction_is_ahead(self):
        cmd = build_clock_skew_command(1)
        assert "+3 hours" in cmd
        assert "-3 hours" not in cmd

    def test_negative_direction_is_behind(self):
        cmd = build_clock_skew_command(-1)
        assert "-3 hours" in cmd

    def test_custom_hours(self):
        assert "+6 hours" in build_clock_skew_command(1, hours=6)

    def test_sets_clock(self):
        assert "date -s" in build_clock_skew_command(1)


class TestUploaderSpecClockSkew:
    def test_default_false(self):
        assert parse_uploader_spec("digital_ocean:lon1:50")["clock_skew"] is False

    def test_true(self):
        spec = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=true")
        assert spec["clock_skew"] is True

    def test_false_explicit(self):
        spec = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=false")
        assert spec["clock_skew"] is False

    def test_with_vm_size_and_payment(self):
        spec = parse_uploader_spec(
            "hetzner:fsn1:100:cpx32:payment=merkle:clock_skew=true"
        )
        assert spec["clock_skew"] is True
        assert spec["vm_size"] == "cpx32"
        assert spec["payment_type"] == "merkle"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_uploader_spec("digital_ocean:lon1:50:clock_skew=maybe")


class TestDownloaderSpecClockSkew:
    def test_default_false(self):
        assert parse_downloader_spec("digital_ocean:lon1")["clock_skew"] is False

    def test_true(self):
        spec = parse_downloader_spec("digital_ocean:lon1:clock_skew=true")
        assert spec["clock_skew"] is True

    def test_with_vm_size(self):
        spec = parse_downloader_spec("vultr:nrt:vc2-4c-8gb:clock_skew=true")
        assert spec["clock_skew"] is True
        assert spec["vm_size"] == "vc2-4c-8gb"

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown key"):
            parse_downloader_spec("digital_ocean:lon1:bogus=1")

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_downloader_spec("digital_ocean:lon1:clock_skew=nope")


class TestSpecSignatureIncludesClockSkew:
    def test_signature_differs_on_clock_skew(self):
        base = parse_uploader_spec("digital_ocean:lon1:50")
        skewed = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=true")
        assert _spec_signature(base) != _spec_signature(skewed)


class TestProdUploaderSpecClockSkew:
    def test_default_false(self):
        spec = parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/x.txt")
        assert spec["clock_skew"] is False

    def test_true(self):
        spec = parse_prod_uploader_spec(
            "lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=true"
        )
        assert spec["clock_skew"] is True

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=x")


class TestOrchestratorEmitsClockSkew:
    def test_uploader_spec_appends_token(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=1000, clock_skew=True)
        assert _uploader_spec(u).endswith("clock_skew=true")

    def test_uploader_spec_omits_when_false(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=1000)
        assert "clock_skew" not in _uploader_spec(u)

    def test_downloader_spec_appends_token(self):
        d = Downloader(provider="digital_ocean", region="lon1", clock_skew=True)
        assert _downloader_spec(d).endswith("clock_skew=true")

    def test_downloader_spec_omits_when_false(self):
        d = Downloader(provider="digital_ocean", region="lon1")
        assert "clock_skew" not in _downloader_spec(d)


class TestManifestModels:
    def test_nodes_accepts_clock_skew(self):
        n = Nodes(
            per_vm=15,
            clock_skew=1,
            providers={"digital_ocean": ProviderNodes(vm_count=1, regions=1)},
        )
        assert n.clock_skew == 1

    def test_nodes_clock_skew_optional(self):
        n = Nodes(
            per_vm=15,
            providers={"digital_ocean": ProviderNodes(vm_count=1, regions=1)},
        )
        assert n.clock_skew is None
