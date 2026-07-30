import pytest

from saorsa_deploy.provisioning.network_profile import (
    EXEMPT_CLASSID,
    EXEMPT_PORTS,
    IFB_DEVICE,
    MARKER,
    SCRIPT_PATH,
    SHAPED_CLASSID,
    SHAPED_QUANTUM,
    UNIT_NAME,
    UNSHAPED_QUANTUM,
    NetworkProfile,
    NetworkProfileError,
    build_network_impairment_command,
    build_network_impairment_script,
    build_network_impairment_teardown_command,
    build_network_impairment_unit,
    format_network_label,
    format_rate,
    load_profiles,
    parse_duration_ms,
    parse_loss_percent,
    parse_rate,
    resolve_network,
)


def _profile(**overrides) -> NetworkProfile:
    base = dict(
        name="test",
        rate_down_bps=10_000_000,
        rate_up_bps=1_000_000,
        delay_ms=30.0,
        jitter_ms=5.0,
        loss_percent=0.2,
    )
    base.update(overrides)
    return NetworkProfile(**base)


class TestParseRate:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1mbit", 1_000_000),
            ("800kbit", 800_000),
            ("27400kbit", 27_400_000),
            ("1.5mbit", 1_500_000),
            ("10gbit", 10_000_000_000),
            ("500bit", 500),
            (" 1MBIT ", 1_000_000),
        ],
    )
    def test_valid(self, value, expected):
        assert parse_rate(value) == expected

    @pytest.mark.parametrize("value", ["", "10", "10mb", "mbit", "-1mbit", "0mbit", "fast"])
    def test_invalid(self, value):
        with pytest.raises(NetworkProfileError):
            parse_rate(value)

    def test_none_is_rejected(self):
        with pytest.raises(NetworkProfileError):
            parse_rate(None)

    def test_error_names_the_field(self):
        with pytest.raises(NetworkProfileError, match="rate_up"):
            parse_rate("nonsense", field="rate_up")


class TestFormatRate:
    @pytest.mark.parametrize(
        "bps,expected",
        [
            (1_000_000, "1mbit"),
            (800_000, "800kbit"),
            # Must NOT round to '27mbit' — the exact value is echoed into recorded runs.
            (27_400_000, "27400kbit"),
            (10_000_000_000, "10gbit"),
            (500, "500bit"),
        ],
    )
    def test_roundtrip(self, bps, expected):
        assert format_rate(bps) == expected
        assert parse_rate(expected) == bps


class TestParseDurationMs:
    @pytest.mark.parametrize(
        "value,expected",
        [("30ms", 30.0), ("12.7ms", 12.7), ("1s", 1000.0), ("0.2ms", 0.2), (None, 0.0), ("", 0.0)],
    )
    def test_valid(self, value, expected):
        assert parse_duration_ms(value) == pytest.approx(expected)

    def test_bare_number_reads_as_milliseconds(self):
        assert parse_duration_ms("15") == pytest.approx(15.0)

    @pytest.mark.parametrize("value", ["-5ms", "fast", "30 ms extra"])
    def test_invalid(self, value):
        with pytest.raises(NetworkProfileError):
            parse_duration_ms(value)


class TestParseLossPercent:
    @pytest.mark.parametrize(
        "value,expected",
        [(0.2, 0.2), ("0.2", 0.2), ("0.2%", 0.2), (None, 0.0), ("", 0.0), (0, 0.0)],
    )
    def test_valid(self, value, expected):
        assert parse_loss_percent(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [-1, 101, "150%", "lots"])
    def test_invalid(self, value):
        with pytest.raises(NetworkProfileError):
            parse_loss_percent(value)


class TestShippedProfiles:
    """The shipped set is what operators actually select; validate it loads clean."""

    def test_expected_profiles_present(self):
        assert set(load_profiles()) == {
            "home-adsl",
            "home-calibrated",
            "home-fttc",
            "home-fttp",
            "mobile-4g",
        }

    def test_every_profile_is_asymmetric_and_versioned(self):
        for name, profile in load_profiles().items():
            assert profile.rate_down_bps > profile.rate_up_bps, name
            assert profile.profile_set_version == 1, name
            assert profile.source == "builtin", name
            assert profile.description, name

    def test_calibrated_matches_the_measurement(self):
        """Guards the one profile derived from a real measurement (2026-07-30)."""
        profile = load_profiles()["home-calibrated"]
        assert profile.rate_down == "27400kbit"
        assert profile.rate_up == "6290kbit"
        assert profile.delay_ms == pytest.approx(12.7)
        assert profile.jitter_ms == pytest.approx(0.2)
        # Measured 0/300 packets: below what that sample can resolve, encoded as 0
        # rather than inventing a plausible small value.
        assert profile.loss_percent == 0.0


class TestResolveNetwork:
    def test_none_means_unshaped(self):
        assert resolve_network(None) is None
        assert resolve_network("") is None

    def test_builtin_name(self):
        profile = resolve_network("home-adsl")
        assert profile.name == "home-adsl"
        assert profile.source == "builtin"

    def test_unknown_name_lists_the_alternatives(self):
        with pytest.raises(NetworkProfileError, match="home-fttc"):
            resolve_network("home-vdsl")

    def test_inline_mapping(self):
        profile = resolve_network(
            {"rate_down": "30mbit", "rate_up": "5mbit", "delay": "25ms", "loss": "0.2%"}
        )
        assert profile.source == "inline"
        assert profile.profile_set_version is None
        assert profile.rate_up == "5mbit"

    def test_inline_rejects_unknown_field(self):
        """A silently-ignored typo would leave a run unshaped but labelled shaped."""
        with pytest.raises(NetworkProfileError, match="rate-up"):
            resolve_network({"rate_down": "30mbit", "rate_up": "5mbit", "rate-up": "1mbit"})

    @pytest.mark.parametrize("missing", ["rate_down", "rate_up"])
    def test_inline_requires_both_directions(self, missing):
        data = {"rate_down": "30mbit", "rate_up": "5mbit"}
        del data[missing]
        with pytest.raises(NetworkProfileError, match=missing):
            resolve_network(data)


class TestFormatNetworkLabel:
    def test_full(self):
        assert format_network_label(_profile(name="home-adsl")) == (
            "home-adsl (10mbit down / 1mbit up, +30ms RTT ±5ms, 0.2% loss)"
        )

    def test_omits_unset_dimensions(self):
        label = format_network_label(_profile(name="fast", delay_ms=0, jitter_ms=0, loss_percent=0))
        assert label == "fast (10mbit down / 1mbit up)"


class TestBuildScript:
    def test_shapes_both_directions(self):
        script = build_network_impairment_script(_profile())
        # Egress carries the upstream cap...
        assert 'shape_device "$IFACE" "1mbit"' in script
        # ...and the mirrored ingress device the downstream one.
        assert 'shape_device "$IFB" "10mbit"' in script

    def test_delay_is_egress_only(self):
        """Applying delay to both paths would silently double the configured RTT."""
        script = build_network_impairment_script(_profile())
        assert 'shape_device "$IFACE" "1mbit" "delay 30ms 5ms distribution normal loss 0.2%"' in (
            script
        )
        # Ingress gets loss but no delay.
        assert 'shape_device "$IFB" "10mbit" "loss 0.2%"' in script

    def test_loss_is_applied_per_direction(self):
        """Inbound loss is what makes a download behave like a home download."""
        script = build_network_impairment_script(_profile(loss_percent=1.5))
        # Assert on the shape_device calls, not a bare substring count — the
        # generated script's header comment also names the loss figure.
        assert 'shape_device "$IFACE" "1mbit" "delay 30ms 5ms distribution normal loss 1.5%"' in (
            script
        )
        assert 'shape_device "$IFB" "10mbit" "loss 1.5%"' in script

    def test_zero_loss_leaves_ingress_netem_off(self):
        script = build_network_impairment_script(_profile(loss_percent=0))
        assert 'shape_device "$IFB" "10mbit" ""' in script

    def test_no_impairment_beyond_rate_skips_netem_entirely(self):
        script = build_network_impairment_script(
            _profile(delay_ms=0, jitter_ms=0, loss_percent=0)
        )
        assert 'shape_device "$IFACE" "1mbit" ""' in script

    def test_jitter_requires_delay_and_uses_normal_distribution(self):
        script = build_network_impairment_script(_profile(delay_ms=20, jitter_ms=4))
        assert "delay 20ms 4ms distribution normal" in script

    def test_explicit_quantum_on_every_class(self):
        """htb warns on every apply otherwise — the classes span four orders of magnitude."""
        script = build_network_impairment_script(_profile())
        assert f"quantum {SHAPED_QUANTUM}" in script
        assert f"quantum {UNSHAPED_QUANTUM}" in script

    def test_observability_and_ssh_exempt_by_port(self):
        """Port, not address: the ES VM does not exist yet when shaping is applied."""
        script = build_network_impairment_script(_profile())
        assert 'EXEMPT_PORTS="22 8086 9200"' in script
        # Matched in both directions on both devices, so the filter loop must cover
        # sport and dport rather than assuming which end initiated.
        assert 'match ip dport "$port" 0xffff' in script
        assert 'match ip sport "$port" 0xffff' in script
        assert f"flowid {EXEMPT_CLASSID}" in script

    def test_exempt_ports_cover_influx_and_elasticsearch(self):
        assert EXEMPT_PORTS == (22, 8086, 9200)

    def test_exempt_endpoints_are_emitted(self):
        script = build_network_impairment_script(_profile(), ["10.1.2.3", "10.1.2.4"])
        assert 'EXEMPT_IPS="$EXEMPT_IPS 10.1.2.3"' in script
        assert 'EXEMPT_IPS="$EXEMPT_IPS 10.1.2.4"' in script

    def test_exempt_endpoints_are_deduplicated(self):
        script = build_network_impairment_script(_profile(), ["10.1.2.3", "10.1.2.3"])
        assert script.count('EXEMPT_IPS="$EXEMPT_IPS 10.1.2.3"') == 1

    def test_invalid_exempt_endpoint_is_rejected(self):
        with pytest.raises(NetworkProfileError, match="influx.example.com"):
            build_network_impairment_script(_profile(), ["influx.example.com"])

    def test_no_exempt_endpoints_is_valid(self):
        script = build_network_impairment_script(_profile(), [])
        # Port-based exemptions still apply with no explicit endpoints resolved.
        assert 'EXEMPT_PORTS="22 8086 9200"' in script

    def test_ingress_uses_an_ifb_mirror(self):
        """Without this the downstream cap would not apply to downloads at all."""
        script = build_network_impairment_script(_profile())
        assert 'ip link add "$IFB" type ifb' in script or "type ifb" in script
        assert "action mirred egress redirect" in script
        assert IFB_DEVICE in script

    def test_exempt_match_direction_flips_on_the_mirror(self):
        """On the mirrored device the observability endpoint is the packet SOURCE."""
        script = build_network_impairment_script(_profile())
        assert 'shape_device "$IFACE"' in script and " dst" in script
        assert 'shape_device "$IFB"' in script and " src" in script

    def test_aqm_falls_back_when_cake_is_absent(self):
        script = build_network_impairment_script(_profile())
        assert "modprobe sch_cake" in script
        assert "fq_codel" in script

    def test_converges_rather_than_refusing(self):
        """Re-applying must replace, not stack: two netem delays would double the RTT."""
        script = build_network_impairment_script(_profile())
        assert 'tc qdisc del dev "$DEV" root' in script

    def test_supports_apply_and_clear(self):
        script = build_network_impairment_script(_profile())
        assert "apply) apply_all ;;" in script
        assert "clear) clear_all ;;" in script

    def test_marker_records_what_is_applied(self):
        script = build_network_impairment_script(_profile(name="home-fttc"))
        assert MARKER in script
        assert "ANT_NETEM:APPLIED:home-fttc" in script


class TestBuildCommands:
    def test_install_writes_script_and_unit_then_starts(self):
        cmd = build_network_impairment_command(_profile())
        assert f"cat > {SCRIPT_PATH}" in cmd
        assert f"chmod 0755 {SCRIPT_PATH}" in cmd
        assert "systemctl daemon-reload" in cmd
        assert f"systemctl enable {UNIT_NAME}" in cmd
        assert f"systemctl restart {UNIT_NAME}" in cmd

    def test_install_fails_loudly_if_the_unit_does_not_come_up(self):
        cmd = build_network_impairment_command(_profile())
        assert "ANT_NETEM:ERROR:unit-not-active" in cmd
        assert "journalctl" in cmd

    def test_unit_survives_reboot(self):
        """tc state is kernel-only; without the unit a reboot silently unshapes the VM."""
        unit = build_network_impairment_unit()
        assert "WantedBy=multi-user.target" in unit
        assert "After=network-online.target" in unit
        assert "RemainAfterExit=yes" in unit

    def test_teardown_removes_unit_and_script(self):
        cmd = build_network_impairment_teardown_command()
        assert f"systemctl disable --now {UNIT_NAME}" in cmd
        assert f"{SCRIPT_PATH} clear" in cmd
        assert "ANT_NETEM:REMOVED" in cmd


class TestToDict:
    def test_records_everything_needed_to_interpret_a_past_run(self):
        """Results tables carry only the name; this is what makes it interpretable."""
        data = load_profiles()["home-adsl"].to_dict()
        assert data == {
            "name": "home-adsl",
            "rate_down": "10mbit",
            "rate_up": "1mbit",
            "delay_ms": 30.0,
            "jitter_ms": 5.0,
            "loss_percent": 0.2,
            "profile_set_version": 1,
            "source": "builtin",
        }


class TestShapedClassIds:
    def test_default_class_is_the_shaped_one(self):
        """htb `default 10` must point at the shaped class or nothing gets capped."""
        script = build_network_impairment_script(_profile())
        assert "htb default 10" in script
        assert SHAPED_CLASSID == "1:10"


class TestManifestAndOrchestratorWiring:
    """The manifest -> argv path, which is how this is actually used."""

    def test_manifest_accepts_a_profile_name(self):
        from saorsa_deploy.config.models import Uploader

        assert Uploader(
            provider="digital_ocean", region="nyc1", file_size_mb=100, network="home-fttc"
        ).network == "home-fttc"

    def test_manifest_rejects_an_unknown_profile_at_validation_time(self):
        """Must fail at `config validate`, not part-way through a live deploy."""
        import pydantic

        from saorsa_deploy.config.models import Uploader

        with pytest.raises(pydantic.ValidationError, match="home-vdsl"):
            Uploader(
                provider="digital_ocean", region="nyc1", file_size_mb=100, network="home-vdsl"
            )

    def test_manifest_accepts_an_inline_mapping(self):
        from saorsa_deploy.config.models import Downloader

        d = Downloader(
            provider="digital_ocean",
            region="tor1",
            network={"rate_down": "30mbit", "rate_up": "5mbit"},
        )
        assert d.network["rate_up"] == "5mbit"

    def test_uploader_spec_token_round_trips(self):
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec
        from saorsa_deploy.config.models import Uploader
        from saorsa_deploy.orchestrator.testnet import _uploader_spec

        u = Uploader(
            provider="digital_ocean", region="nyc1", file_size_mb=100, network="home-adsl"
        )
        assert parse_uploader_spec(_uploader_spec(u))["network"] == "home-adsl"

    def test_inline_token_survives_the_colon_delimited_spec(self):
        """The inline form must not use ':' — client specs already split on it."""
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec
        from saorsa_deploy.config.models import Uploader
        from saorsa_deploy.orchestrator.testnet import _uploader_spec

        u = Uploader(
            provider="digital_ocean",
            region="nyc1",
            file_size_mb=100,
            network={"rate_down": "30mbit", "rate_up": "5mbit", "delay": "25ms"},
        )
        token = _uploader_spec(u)
        assert ":network=rate_down=30mbit,rate_up=5mbit,delay=25ms" in token
        resolved = resolve_network(parse_uploader_spec(token)["network"])
        assert resolved.rate_up == "5mbit"
        assert resolved.source == "inline"

    def test_rust_log_still_comes_last(self):
        """network must not displace rust_log from the trailing position."""
        from saorsa_deploy.config.models import Uploader
        from saorsa_deploy.orchestrator.testnet import _uploader_spec

        u = Uploader(
            provider="digital_ocean",
            region="nyc1",
            file_size_mb=100,
            network="home-adsl",
            rust_log="ant_cli=debug,saorsa_core::dht=trace",
        )
        spec = _uploader_spec(u)
        assert spec.index("network=") < spec.index("rust_log=")

    def test_omitting_network_leaves_the_spec_unchanged(self):
        """Backwards compatibility: an unshaped deployment must be byte-identical."""
        from saorsa_deploy.config.models import Uploader
        from saorsa_deploy.orchestrator.testnet import _uploader_spec

        u = Uploader(provider="digital_ocean", region="nyc1", file_size_mb=100)
        assert _uploader_spec(u) == "digital_ocean:nyc1:100"

    def test_prod_downloader_gets_a_flag_not_a_token(self):
        from saorsa_deploy.config.models import Binary, Manifest, ProdDownloadersSpec
        from saorsa_deploy.orchestrator.prod import _downloaders

        manifest = Manifest(
            name="PROD-DL-TEST",
            kind="prod-downloaders",
            purpose="test",
            ssh_key_path="~/.ssh/ant_deploy",
            prod_downloaders=ProdDownloadersSpec(
                spec="lon1", binary=Binary(version="0.3.1"), network="home-fttc"
            ),
        )
        argv = _downloaders(manifest)[0].argv
        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "home-fttc"


class TestRecordingLookup:
    """Only the profile NAME is denormalised onto op rows; deploy_args holds the rest."""

    def test_resolves_from_a_single_spec_level_profile(self):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.record_results import _build_network_profile_lookup

        lookup = _build_network_profile_lookup(
            SimpleNamespace(deploy_args={"network": {"name": "home-adsl"}}, name="X")
        )
        assert lookup("any-host") == "home-adsl"

    def test_unshaped_deployment_records_nothing(self):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.record_results import _build_network_profile_lookup

        lookup = _build_network_profile_lookup(SimpleNamespace(deploy_args={}, name=""))
        assert lookup("any-host") is None

    def test_malformed_deploy_args_never_yield_a_wrong_profile(self):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.record_results import _build_network_profile_lookup

        lookup = _build_network_profile_lookup(
            SimpleNamespace(deploy_args={"network": 42}, name="")
        )
        assert lookup("any-host") is None
