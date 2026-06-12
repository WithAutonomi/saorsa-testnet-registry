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
    SKEW_APT_CONF,
    SKEW_MARKER,
    build_clock_skew_command,
    format_skew_offset,
    parse_skew_duration,
    select_skew_ips,
    skew_direction_for_ip,
)


class TestParseSkewDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2h", 7200),
            ("-2h", -7200),
            ("+2h", 7200),
            ("30m", 1800),
            ("-30m", -1800),
            ("90s", 90),
            ("1h30m", 5400),
            ("-1h30m", -5400),
            ("1h30m15s", 5415),
            (" 2H ", 7200),  # whitespace + case-insensitive
        ],
    )
    def test_valid_durations(self, value, expected):
        assert parse_skew_duration(value) == expected

    def test_accepts_numeric_string(self):
        # YAML may coerce a bare number; only valid if it carries a unit.
        assert parse_skew_duration("45s") == 45

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "true", "false", "yes", "0", "0h", "0m0s", "2", "2x", "h", "-", "2h30", "1.5h"],
    )
    def test_invalid_durations_raise(self, value):
        with pytest.raises(ValueError):
            parse_skew_duration(value)

    def test_none_raises(self):
        with pytest.raises(ValueError):
            parse_skew_duration(None)


class TestFormatSkewOffset:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (7200, "+2h"),
            (-7200, "-2h"),
            (1800, "+30m"),
            (-1800, "-30m"),
            (5400, "+1h30m"),
            (-5415, "-1h30m15s"),
            (90, "+1m30s"),
            (45, "+45s"),
            (0, "+0s"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert format_skew_offset(seconds) == expected


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
        cmd = build_clock_skew_command(7200)
        assert SKEW_MARKER in cmd
        assert "ANT_SKEW:ALREADY" in cmd
        assert "ANT_SKEW:APPLIED" in cmd

    def test_disables_time_sync(self):
        cmd = build_clock_skew_command(7200)
        assert "timedatectl set-ntp false" in cmd
        assert "systemd-timesyncd" in cmd
        assert "chrony" in cmd

    def test_positive_offset_is_ahead(self):
        cmd = build_clock_skew_command(7200)
        assert "+7200 seconds" in cmd
        assert "ANT_SKEW:APPLIED:+2h" in cmd

    def test_negative_offset_is_behind(self):
        cmd = build_clock_skew_command(-7200)
        assert "-7200 seconds" in cmd
        assert "ANT_SKEW:APPLIED:-2h" in cmd

    def test_sub_hour_offset(self):
        cmd = build_clock_skew_command(1800)
        assert "+1800 seconds" in cmd
        assert "ANT_SKEW:APPLIED:+30m" in cmd

    def test_sets_clock(self):
        assert "date -s" in build_clock_skew_command(7200)

    def test_writes_apt_date_tolerance(self):
        cmd = build_clock_skew_command(-7200)
        assert SKEW_APT_CONF in cmd
        assert 'Acquire::Check-Date "false";' in cmd
        assert 'Acquire::Check-Valid-Until "false";' in cmd

    def test_apt_tolerance_written_before_marker_short_circuit(self):
        # The apt drop-in must be written even on hosts already skewed by an
        # earlier build (marker present) — so it lands before the early exit.
        cmd = build_clock_skew_command(-7200)
        assert cmd.index(SKEW_APT_CONF) < cmd.index('echo "ANT_SKEW:ALREADY"')


class TestUploaderSpecClockSkew:
    def test_default_none(self):
        assert parse_uploader_spec("digital_ocean:lon1:50")["clock_skew"] is None

    def test_positive_duration(self):
        spec = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=2h")
        assert spec["clock_skew"] == 7200

    def test_negative_duration(self):
        spec = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=-2h")
        assert spec["clock_skew"] == -7200

    def test_with_vm_size_and_payment(self):
        spec = parse_uploader_spec(
            "hetzner:fsn1:100:cpx32:payment=merkle:clock_skew=30m"
        )
        assert spec["clock_skew"] == 1800
        assert spec["vm_size"] == "cpx32"
        assert spec["payment_type"] == "merkle"

    def test_boolean_value_now_rejected(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_uploader_spec("digital_ocean:lon1:50:clock_skew=true")

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_uploader_spec("digital_ocean:lon1:50:clock_skew=maybe")


class TestDownloaderSpecClockSkew:
    def test_default_none(self):
        assert parse_downloader_spec("digital_ocean:lon1")["clock_skew"] is None

    def test_positive_duration(self):
        spec = parse_downloader_spec("digital_ocean:lon1:clock_skew=2h")
        assert spec["clock_skew"] == 7200

    def test_negative_with_vm_size(self):
        spec = parse_downloader_spec("vultr:nrt:vc2-4c-8gb:clock_skew=-2h")
        assert spec["clock_skew"] == -7200
        assert spec["vm_size"] == "vc2-4c-8gb"

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown key"):
            parse_downloader_spec("digital_ocean:lon1:bogus=1")

    def test_boolean_value_now_rejected(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_downloader_spec("digital_ocean:lon1:clock_skew=true")

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_downloader_spec("digital_ocean:lon1:clock_skew=nope")


class TestSpecSignatureIncludesClockSkew:
    def test_signature_differs_on_clock_skew(self):
        base = parse_uploader_spec("digital_ocean:lon1:50")
        skewed = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=2h")
        assert _spec_signature(base) != _spec_signature(skewed)

    def test_signature_differs_on_direction(self):
        fast = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=2h")
        slow = parse_uploader_spec("digital_ocean:lon1:50:clock_skew=-2h")
        assert _spec_signature(fast) != _spec_signature(slow)


class TestProdUploaderSpecClockSkew:
    def test_default_none(self):
        spec = parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/x.txt")
        assert spec["clock_skew"] is None

    def test_positive_duration(self):
        spec = parse_prod_uploader_spec(
            "lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=2h"
        )
        assert spec["clock_skew"] == 7200

    def test_negative_duration(self):
        spec = parse_prod_uploader_spec(
            "lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=-90m"
        )
        assert spec["clock_skew"] == -5400

    def test_boolean_value_now_rejected(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=true")

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="clock_skew"):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb:urls=/x.txt:clock_skew=x")


class TestOrchestratorEmitsClockSkew:
    def test_uploader_spec_appends_duration(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=1000, clock_skew="2h")
        assert _uploader_spec(u).endswith("clock_skew=2h")

    def test_uploader_spec_omits_when_unset(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=1000)
        assert "clock_skew" not in _uploader_spec(u)

    def test_downloader_spec_appends_duration(self):
        d = Downloader(provider="digital_ocean", region="lon1", clock_skew="-2h")
        assert _downloader_spec(d).endswith("clock_skew=-2h")

    def test_downloader_spec_omits_when_unset(self):
        d = Downloader(provider="digital_ocean", region="lon1")
        assert "clock_skew" not in _downloader_spec(d)


class TestManifestModels:
    def test_uploader_accepts_duration(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=10, clock_skew="-2h")
        assert u.clock_skew == "-2h"

    def test_uploader_clock_skew_optional(self):
        u = Uploader(provider="vultr", region="syd", file_size_mb=10)
        assert u.clock_skew is None

    def test_uploader_rejects_boolean(self):
        with pytest.raises(ValueError):
            Uploader(provider="vultr", region="syd", file_size_mb=10, clock_skew=True)

    def test_uploader_rejects_bad_duration(self):
        with pytest.raises(ValueError):
            Uploader(provider="vultr", region="syd", file_size_mb=10, clock_skew="nope")

    def test_downloader_accepts_duration(self):
        d = Downloader(provider="digital_ocean", region="lon1", clock_skew="30m")
        assert d.clock_skew == "30m"

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
