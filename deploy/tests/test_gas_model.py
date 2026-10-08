"""Uploader gas-funding model and its `clients estimate-gas` wrapper.

Staging run #566 lost six of ten uploaders to gas exhaustion mid-run because
their wallets hold a FIXED amount of ETH and the Arbitrum Sepolia base fee had
risen ~10x in three days. That pushed two hard gates below their floors for a
reason unrelated to the release. These tests pin the behaviours that stop it
recurring silently.
"""

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from saorsa_deploy.gas_model import (
    NATIVE,
    WASM,
    ClientRun,
    GasModel,
    GasModelError,
    UploaderPlan,
    estimate_funding,
    estimate_uploader,
    get_default_gas_model_path,
    load_gas_model,
    project_throughput,
    rate_for_size,
    wasm_rate_for_size,
)


@pytest.fixture()
def model():
    return load_gas_model()


@pytest.fixture()
def raw():
    return yaml.safe_load(get_default_gas_model_path().read_text())


# ---------------------------------------------------------------------------
# The shipped config
# ---------------------------------------------------------------------------


def test_shipped_model_loads(model):
    assert model.version >= 1
    assert model.reference_base_fee_gwei > 0
    assert model.safety_factor >= 1.0


def test_model_is_frozen(model):
    with pytest.raises(Exception):
        model.eth_per_gb = 1.0


def test_shipped_model_reproduces_its_own_calibration(model, raw):
    """The config must predict the run it was calibrated from.

    This is the regression guard on the coefficients. An edit that fits the
    numbers badly — or a units slip between ETH and gwei — shows up here as a
    prediction that no longer matches the six measured points, rather than as
    an under-funded run discovered four hours in.
    """
    for point in raw["calibration"]["points"]:
        spent = point["funded_eth"] - point["residual_eth"]
        actual_per_upload = spent / point["uploads"]
        modelled = model.eth_per_upload(point["size_gb"], model.reference_base_fee_gwei)
        rel = abs(modelled - actual_per_upload) / actual_per_upload
        assert rel < 0.20, (
            f"{point['client']}: model predicts {modelled:.3e} ETH/upload but "
            f"the run actually spent {actual_per_upload:.3e} ({rel:.1%} off)"
        )


def test_safety_factor_exceeds_the_worst_calibration_underestimate(model, raw):
    """The safety factor must cover the fit's own error, with room to spare.

    It absorbs fit error AND intra-run price drift AND drift between runs. If
    it ever drops below the fit's worst under-estimate it is absorbing nothing.
    """
    worst_under = 0.0
    for point in raw["calibration"]["points"]:
        spent = point["funded_eth"] - point["residual_eth"]
        actual = spent / point["uploads"]
        modelled = model.eth_per_upload(point["size_gb"], model.reference_base_fee_gwei)
        if modelled < actual:
            worst_under = max(worst_under, (actual - modelled) / actual)
    assert model.safety_factor > 1.0 + worst_under


# ---------------------------------------------------------------------------
# Validation: a malformed model must fail loudly, never default quietly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["fixed_eth_per_upload", "eth_per_gb", "reference_base_fee_gwei"])
def test_missing_model_coefficient_is_rejected(raw, key, tmp_path):
    bad = copy.deepcopy(raw)
    del bad["model"][key]
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(GasModelError, match=key):
        load_gas_model(path)


@pytest.mark.parametrize("key", ["safety_factor", "round_up_to_eth"])
def test_zero_top_level_value_is_rejected(raw, key, tmp_path):
    """A zero here would recommend funding every uploader with nothing."""
    bad = copy.deepcopy(raw)
    bad[key] = 0
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(GasModelError, match=key):
        load_gas_model(path)


def test_missing_version_is_rejected(raw, tmp_path):
    bad = copy.deepcopy(raw)
    del bad["version"]
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(GasModelError, match="version"):
        load_gas_model(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(GasModelError, match="not found"):
        load_gas_model(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def _model(**over):
    base = dict(
        version=1,
        fixed_eth_per_upload=1e-4,
        eth_per_gb=1e-3,
        reference_base_fee_gwei=0.1,
        safety_factor=2.0,
        round_up_to_eth=0.05,
        minimum_eth_per_uploader=0.05,
    )
    base.update(over)
    return GasModel(**base)


def test_cost_scales_linearly_with_base_fee():
    """Doubling the price doubles the cost. This is the whole mechanism."""
    m = _model()
    at_ref = m.eth_per_upload(1.0, 0.1)
    at_double = m.eth_per_upload(1.0, 0.2)
    assert at_double == pytest.approx(at_ref * 2)


def test_cost_is_affine_in_size_not_proportional():
    """A fixed per-transaction overhead exists and must not be modelled away.

    Fitting a pure per-GB rate under-funds small-file clients badly: they pay
    the overhead most often. A 10x smaller file must cost MORE than a tenth.
    """
    m = _model()
    big = m.eth_per_upload(1.0, 0.1)
    small = m.eth_per_upload(0.1, 0.1)
    assert small > big / 10


def test_safety_factor_applied_once_not_per_upload():
    m = _model(safety_factor=3.0, round_up_to_eth=1e-12, minimum_eth_per_uploader=0)
    per = m.eth_per_upload(0.5, 0.1)
    est = estimate_uploader(UploaderPlan("x", 0.5, 100), 0.1, m)
    assert est.modelled_eth == pytest.approx(per * 100 * 3.0)


def test_recommendation_rounds_up_never_down():
    m = _model(round_up_to_eth=0.05, minimum_eth_per_uploader=0)
    est = estimate_uploader(UploaderPlan("x", 1.0, 100), 0.1, m)
    assert est.recommended_eth >= est.modelled_eth
    steps = est.recommended_eth / 0.05
    assert steps == pytest.approx(round(steps), abs=1e-6)


def test_exact_multiple_is_not_pushed_to_the_next_step():
    """0.15 / 0.05 is 2.9999999999999996 in binary floating point."""
    m = _model(
        fixed_eth_per_upload=0.15,
        eth_per_gb=0.0,
        reference_base_fee_gwei=0.1,
        safety_factor=1.0,
        round_up_to_eth=0.05,
        minimum_eth_per_uploader=0,
    )
    est = estimate_uploader(UploaderPlan("x", 0.0, 1), 0.1, m)
    assert est.recommended_eth == pytest.approx(0.15)


def test_minimum_floor_applies_to_a_tiny_workload():
    """A client that cannot afford one transaction fails like a network fault."""
    m = _model(minimum_eth_per_uploader=0.05)
    est = estimate_uploader(UploaderPlan("x", 0.001, 1), 0.1, m)
    assert est.recommended_eth == pytest.approx(0.05)


def test_negative_upload_count_is_rejected():
    with pytest.raises(GasModelError):
        estimate_uploader(UploaderPlan("x", 1.0, -1), 0.1, _model())


def test_zero_base_fee_is_rejected():
    with pytest.raises(GasModelError):
        _model().eth_per_upload(1.0, 0.0)


def test_shortfall_and_adequacy():
    m = _model()
    plans = [
        UploaderPlan("rich", 0.02, 10, current_eth=99.0),
        UploaderPlan("poor", 1.0, 1000, current_eth=0.01),
        UploaderPlan("unset", 1.0, 10),
    ]
    est = estimate_funding(plans, 0.1, m)
    by = {u.plan.name: u for u in est.uploaders}
    assert by["rich"].adequate is True
    assert by["rich"].shortfall_eth == 0.0
    assert by["poor"].adequate is False
    assert by["poor"].shortfall_eth > 0
    assert by["unset"].adequate is None


def test_funder_check_uses_the_full_total_not_the_deficit():
    """Top-up is per-wallet and sequential.

    A funder that cannot cover the whole draw does not fail cleanly up front —
    it funds the first few uploaders and then runs dry, leaving a
    half-provisioned run. So the check is against the full recommendation.
    """
    m = _model()
    plans = [UploaderPlan(f"u{i}", 1.0, 500, current_eth=0.9) for i in range(4)]
    est = estimate_funding(plans, 0.1, m, funder_balance_eth=1.0)
    assert est.total_recommended_eth > 1.0
    assert est.funder_covers is False


def test_funder_unknown_is_none_not_false():
    est = estimate_funding([UploaderPlan("u", 1.0, 1)], 0.1, _model())
    assert est.funder_covers is None


def test_price_ratio_reports_the_multiple_of_the_reference():
    est = estimate_funding([UploaderPlan("u", 1.0, 1)], 0.5, _model())
    assert est.price_ratio == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Projecting throughput — the correction that stops under-funding compounding
# ---------------------------------------------------------------------------


def test_rate_is_taken_over_the_active_window_not_the_run():
    """A client that died early must not teach the next run to under-fund.

    This is the crux. #566's UK1-2 managed 75 uploads before its wallet
    emptied at 3.83h of a 5.18h run. Sizing from the raw count of 75 would
    fund the next run for a truncated run — reproducing the exact shortfall.
    Its RATE (19.6/h) projected over 5.18h is ~101 uploads.
    """
    t = project_throughput(
        [ClientRun("uk1-2", uploads=75, active_hours=3.83, size_gb=1.0)],
        run_hours=5.18,
    )
    rate = t.rate_by_size_gb[1.0]
    assert rate == pytest.approx(75 / 3.83, rel=1e-6)
    assert rate * 5.18 > 95
    assert [c.host for c in t.truncated] == ["uk1-2"]


def test_a_client_that_ran_the_whole_run_is_not_flagged_truncated():
    t = project_throughput(
        [ClientRun("ok", uploads=500, active_hours=5.0, size_gb=0.02)],
        run_hours=5.18,
    )
    assert t.truncated == []


def test_fastest_client_wins_when_several_share_a_size():
    """Under-funding fails a run; over-funding is swept back on destroy."""
    t = project_throughput(
        [
            ClientRun("slow", uploads=100, active_hours=5.0, size_gb=0.02),
            ClientRun("fast", uploads=300, active_hours=5.0, size_gb=0.02),
        ],
        run_hours=5.0,
    )
    assert t.rate_by_size_gb[0.02] == pytest.approx(60.0)


def test_zero_active_window_does_not_divide_by_zero():
    t = project_throughput(
        [ClientRun("blip", uploads=1, active_hours=0.0, size_gb=1.0)],
        run_hours=5.0,
    )
    assert t.rate_by_size_gb[1.0] < 10


def test_client_with_no_uploads_is_ignored():
    t = project_throughput(
        [ClientRun("dead", uploads=0, active_hours=5.0, size_gb=1.0)],
        run_hours=5.0,
    )
    assert t.rate_by_size_gb == {}


def test_rate_lookup_prefers_an_exact_size_match():
    rates = {0.02: 100.0, 1.0: 20.0}
    assert rate_for_size(1.0, rates) == 20.0


def test_rate_lookup_borrows_only_across_comparable_sizes():
    """A 20 MB client's rate says nothing useful about a 1 GB one."""
    rates = {0.02: 100.0}
    assert rate_for_size(1.0, rates) is None
    assert rate_for_size(0.025, rates) == 100.0


def test_rate_lookup_on_empty_history_returns_none():
    assert rate_for_size(1.0, {}) is None


# ---------------------------------------------------------------------------
# The CLI wrapper
# ---------------------------------------------------------------------------

MODULE = "saorsa_deploy.cmd.estimate_client_gas"

MANIFEST = """
kind: testnet
name: TEST-01
purpose: "gas model test fixture"
testnet:
  binaries:
    ant_node:
      version: 0.18.1-rc.1
    ant:
      version: 0.3.6-rc.1
  nodes:
    per_vm: 15
    providers:
      digital_ocean:
        vm_count: 1
        regions_list: [lon1]
        size: s-8vcpu-16gb
        attached_volume_size: 100
  clients:
    uploaders:
      - { provider: ovh, region: SBG5, file_size_mb: 1000, vm_size: b3-8, ant: 1000, eth: 0.5 }
      - { provider: ovh, region: DE1, file_size_mb: 20, vm_size: b3-8, ant: 200, eth: 0.2 }
    downloaders:
      - { provider: digital_ocean, region: tor1 }
"""


@pytest.fixture()
def manifest_path(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(MANIFEST)
    return str(path)


def _args(**over):
    defaults = {
        "config": None,
        "set": None,
        "name": "STG-01",
        "baseline_id": None,
        "uploads_per_hour": 100.0,
        "duration_hours": 5.0,
        "base_fee_gwei": 0.42,
        "network": "arbitrum-sepolia",
        "funder_balance_eth": 100.0,
        "model_path": None,
    }
    defaults.update(over)
    return SimpleNamespace(**defaults)


def test_exits_2_when_uploaders_are_under_funded(capsys, manifest_path):
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=manifest_path, base_fee_gwei=50.0))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "under-funded" in out


def test_exits_0_when_funding_is_ample(capsys, manifest_path):
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=manifest_path, base_fee_gwei=1e-6))
    assert exc.value.code == 0
    assert "adequate" in capsys.readouterr().out


def test_exits_2_when_the_funding_wallet_cannot_cover_it(capsys, manifest_path):
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(
            _args(config=manifest_path, base_fee_gwei=1e-6, funder_balance_eth=0.0001)
        )
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "short of" in out
    assert "half-provisioned" in out


def test_baseline_id_without_a_database_exits_1(capsys, manifest_path):
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with patch(f"{MODULE}.get_repository", return_value=None):
        with pytest.raises(SystemExit) as exc:
            cmd_estimate_client_gas(
                _args(
                    config=manifest_path,
                    baseline_id=1,
                    uploads_per_hour=None,
                )
            )
    assert exc.value.code == 1
    assert "DATABASE_URL" in capsys.readouterr().out


def test_unknown_network_exits_1(capsys, manifest_path):
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=manifest_path, network="not-a-chain"))
    assert exc.value.code == 1
    assert "Unknown EVM network" in capsys.readouterr().out


def test_base_fee_is_not_fetched_when_supplied(manifest_path):
    """--base-fee-gwei must make the command work with no chain access."""
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with patch(f"{MODULE}._fetch_base_fee_gwei") as fetch:
        with pytest.raises(SystemExit):
            cmd_estimate_client_gas(_args(config=manifest_path))
    fetch.assert_not_called()


def test_argv_plumbing_reaches_the_handler():
    """The exact invocation the staging skill emits must parse."""
    from saorsa_deploy.main import main

    seen = {}

    def _stub(args):
        seen["config"] = args.config
        seen["baseline"] = args.baseline_id

    with patch(f"{MODULE}.cmd_estimate_client_gas", _stub):
        main(
            [
                "clients",
                "estimate-gas",
                "--config",
                "deploy/manifests/runs/staging/STG-01.yaml",
                "--baseline-id",
                "566",
            ]
        )
    assert seen["baseline"] == 566


def test_a_current_value_below_the_minimum_floor_is_under_funded(capsys, tmp_path):
    """The floor is a hard requirement, not a suggestion.

    A client funded below `minimum_eth_per_uploader` cannot afford even one
    payment transaction, and that failure is indistinguishable from a network
    fault in the gate data. So it must be reported short even when the
    modelled requirement is vanishingly small.
    """
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST.replace("eth: 0.2", "eth: 0.0001"))
    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=str(path), base_fee_gwei=1e-9))
    assert exc.value.code == 2
    assert "under-funded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Extension manifests must be sizeable too
#
# A `testnet-extension` carries its own client tier, funded from the same
# wallet in the same way, but the command read only `testnet.clients` and so
# refused every extension with "no wallets to fund" — which reads as "nothing
# to check" rather than "this tool cannot see your uploaders". On V2-1150 the
# released cohort (DEV-01-REL) went unsized for exactly that reason, and its
# uploaders exhausted their wallets mid-run.
# ---------------------------------------------------------------------------

EXTENSION_MANIFEST = """
kind: testnet-extension
name: TEST-01-REL
purpose: "gas model test fixture, extension"
extension:
  source: TEST-01
  binaries:
    ant_node:
      version: 0.17.2
    ant:
      version: 0.3.4
  nodes:
    per_vm: 15
    providers:
      digital_ocean:
        vm_count: 1
        regions_list: [lon1]
        size: s-8vcpu-16gb
        attached_volume_size: 100
  clients:
    uploaders:
      - { provider: ovh, region: SBG5, file_size_mb: 1000, vm_size: b3-8, ant: 1000, eth: 0.5 }
      - { provider: ovh, region: DE1, file_size_mb: 20, vm_size: b3-8, ant: 200, eth: 0.2 }
    downloaders:
      - { provider: digital_ocean, region: tor1 }
"""


@pytest.fixture()
def extension_manifest_path(tmp_path):
    path = tmp_path / "extension.yaml"
    path.write_text(EXTENSION_MANIFEST)
    return str(path)


def test_extension_manifest_is_sized_not_refused(capsys, extension_manifest_path):
    """The regression: an extension used to exit 1 with "no wallets to fund"."""
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(
            _args(config=extension_manifest_path, name="TEST-01-REL", base_fee_gwei=1e-6)
        )
    out = capsys.readouterr().out
    assert "no wallets to fund" not in out
    assert exc.value.code == 0
    # both uploaders reached the table
    assert "SBG5/1000MB" in out
    assert "DE1/20MB" in out


def test_extension_under_funding_is_reported_against_the_extension_block(
    capsys, extension_manifest_path
):
    """The remediation advice must name the block the values actually live in."""
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(
            _args(config=extension_manifest_path, name="TEST-01-REL", base_fee_gwei=50.0)
        )
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "extension.clients.uploaders" in out
    assert "testnet.clients.uploaders" not in out


def test_a_testnet_manifest_still_reports_against_the_testnet_block(capsys, manifest_path):
    """Guard the other side of the branch: a base manifest is unchanged."""
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=manifest_path, base_fee_gwei=50.0))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "testnet.clients.uploaders" in out


def test_prod_uploader_kinds_are_still_refused(capsys, tmp_path):
    """Deliberate non-coverage: prod pays from an Arbitrum One mainnet wallet
    and this model is calibrated on Sepolia, so a prod manifest must not be
    silently sized against the wrong chain."""
    from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

    path = tmp_path / "prod.yaml"
    path.write_text(
        """
kind: prod-uploaders
name: PROD-UL-TEST
purpose: "gas model test fixture, prod"
prod_uploaders:
  binary:
    version: 0.3.4
  uploaders:
    - region: lon1
      vm_size: s-4vcpu-8gb
      pool:
        small: 3
"""
    )
    with pytest.raises(SystemExit) as exc:
        cmd_estimate_client_gas(_args(config=str(path), name="PROD-UL-TEST"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    # refused for the right reason: the manifest parsed, we just decline to
    # size a mainnet-funded kind against a Sepolia-calibrated model.
    assert "invalid manifest" not in out
    # Rich wraps the message, so the assertion is on a phrase short enough to
    # survive any wrap point.
    assert "no wallets" in out


# ---------------------------------------------------------------------------
# The WASM (browser-path) uploader tier (V2-1286)
# ---------------------------------------------------------------------------

WASM_MANIFEST = MANIFEST.replace(
    """    downloaders:
      - { provider: digital_ocean, region: tor1 }
""",
    """    downloaders:
      - { provider: digital_ocean, region: tor1 }
    wasm_uploaders:
      - { provider: ovh, region: SBG5, file_size_mb: 900, vm_size: b3-8, ant: 1000, eth: 0.9 }
    wasm_downloaders:
      - { provider: digital_ocean, region: tor1, vm_size: s-4vcpu-8gb }
""",
).replace(
    """    ant:
      version: 0.3.6-rc.1
""",
    """    ant:
      version: 0.3.6-rc.1
    ant_wasm:
      branch: ant-cli-v0.3.8-rc.1
      repo_owner: WithAutonomi
""",
)


@pytest.fixture()
def wasm_manifest_path(tmp_path):
    path = tmp_path / "wasm-manifest.yaml"
    path.write_text(WASM_MANIFEST)
    return str(path)


class TestWasmCostModel:
    """A WASM payment is two transactions where a native one is one."""

    def test_the_fixed_term_is_doubled_and_the_per_gb_term_is_not(self):
        m = _model(wasm_fixed_cost_multiplier=2.0)
        # At zero size only the fixed term remains, so the ratio IS the
        # multiplier.
        assert m.eth_per_upload(0.0, 0.1, WASM) == pytest.approx(
            2 * m.eth_per_upload(0.0, 0.1, NATIVE)
        )
        # The per-GB component is the quote payload, identical either way, so
        # the difference at any size is exactly the extra fixed term.
        diff = m.eth_per_upload(1.0, 0.1, WASM) - m.eth_per_upload(1.0, 0.1, NATIVE)
        assert diff == pytest.approx(m.fixed_eth_per_upload)

    def test_native_is_the_default_transport(self):
        m = _model(wasm_fixed_cost_multiplier=2.0)
        assert m.eth_per_upload(0.5, 0.1) == m.eth_per_upload(0.5, 0.1, NATIVE)

    def test_the_shipped_config_carries_the_multiplier(self, model):
        assert model.wasm_fixed_cost_multiplier == 2.0

    def test_a_missing_multiplier_is_rejected(self, raw, tmp_path):
        bad = copy.deepcopy(raw)
        del bad["wasm_fixed_cost_multiplier"]
        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump(bad))
        with pytest.raises(GasModelError, match="wasm_fixed_cost_multiplier"):
            load_gas_model(path)

    def test_the_plan_carries_the_transport_into_the_estimate(self):
        m = _model(
            wasm_fixed_cost_multiplier=2.0,
            safety_factor=1.0,
            round_up_to_eth=1e-9,
            minimum_eth_per_uploader=0.0,
        )
        common = dict(size_gb=0.0, expected_uploads=100)
        native = estimate_uploader(UploaderPlan(name="n", **common), 0.1, m)
        wasm = estimate_uploader(UploaderPlan(name="w", transport=WASM, **common), 0.1, m)
        assert wasm.modelled_eth == pytest.approx(2 * native.modelled_eth)


class TestTransportThroughput:
    def _clients(self):
        # Same file size on both tiers; WASM is the slower instrument.
        return [
            ClientRun(
                host="native-1", uploads=300, active_hours=3.0, size_gb=1.0, transport=NATIVE
            ),
            ClientRun(host="wasm-1", uploads=45, active_hours=3.0, size_gb=1.0, transport=WASM),
        ]

    def test_native_sizing_ignores_wasm_records(self):
        """The reason the filter exists: WASM is ~6-8x slower at the same size.

        Without it, once WASM is in every staging baseline, a native uploader
        would silently be sized from whichever tier happened to be faster.
        """
        t = project_throughput(self._clients(), 3.0, transport=NATIVE)
        assert t.rate_by_size_gb == {1.0: 100.0}
        assert t.host_by_size_gb == {1.0: "native-1"}

    def test_wasm_sizing_ignores_native_records(self):
        t = project_throughput(self._clients(), 3.0, transport=WASM)
        assert t.rate_by_size_gb == {1.0: 15.0}

    def test_unfiltered_projection_is_unchanged(self):
        # Every pre-WASM caller passed no transport and must keep its answer.
        t = project_throughput(self._clients(), 3.0)
        assert t.rate_by_size_gb == {1.0: 100.0}

    def test_null_client_kind_is_native(self):
        t = project_throughput(
            [ClientRun(host="old", uploads=10, active_hours=1.0, size_gb=0.02)],
            1.0,
            transport=NATIVE,
        )
        assert t.rate_by_size_gb == {0.02: 10.0}


class TestWasmRateResolution:
    def _native(self, sizes):
        return project_throughput(
            [
                ClientRun(
                    host=f"n-{s}",
                    uploads=int(s[1] * 10),
                    active_hours=1.0,
                    size_gb=s[0],
                    transport=NATIVE,
                )
                for s in sizes
            ],
            1.0,
            transport=NATIVE,
        )

    def test_measured_wasm_throughput_is_preferred(self):
        wasm = project_throughput(
            [ClientRun(host="w", uploads=15, active_hours=1.0, size_gb=0.9, transport=WASM)],
            1.0,
            transport=WASM,
        )
        rate, basis = wasm_rate_for_size(0.9, wasm, self._native([(0.9, 10.0)]))
        assert rate == 15.0
        assert basis == ""

    def test_falls_back_to_the_nearest_native_twin_and_names_it(self):
        empty = project_throughput([], 1.0, transport=WASM)
        native = self._native([(1.0486, 7.5), (0.0210, 60.0)])
        rate, basis = wasm_rate_for_size(0.9, empty, native)
        assert rate == 75.0
        assert basis == "approx: native twin n-(1.0486, 7.5)"

    def test_a_tie_takes_the_faster_native_client(self):
        # 0.5 is equidistant from 0.4 and 0.6; over-funding is the safe side.
        empty = project_throughput([], 1.0, transport=WASM)
        native = project_throughput(
            [
                ClientRun(host="slow", uploads=10, active_hours=1.0, size_gb=0.4, transport=NATIVE),
                ClientRun(host="fast", uploads=40, active_hours=1.0, size_gb=0.6, transport=NATIVE),
            ],
            1.0,
            transport=NATIVE,
        )
        rate, basis = wasm_rate_for_size(0.5, empty, native)
        assert rate == 40.0
        assert "fast" in basis

    def test_no_baseline_at_all_gives_no_rate(self):
        empty = project_throughput([], 1.0)
        assert wasm_rate_for_size(0.9, empty, empty) == (None, "")

    def test_the_twin_is_taken_outside_the_native_size_band(self):
        """`rate_for_size` refuses beyond +/-2x; the WASM twin deliberately does not.

        WASM measured ~6-8x slower on the same work, so a native rate projects
        more uploads than the WASM client will manage and over-funds it. The
        surplus is swept back to the funder on destroy; refusing to size the
        uploader leaves it on whatever the manifest happened to say.
        """
        empty = project_throughput([], 1.0, transport=WASM)
        native = self._native([(1.0486, 7.5)])
        assert rate_for_size(0.02, native.rate_by_size_gb) is None
        rate, basis = wasm_rate_for_size(0.02, empty, native)
        assert rate == 75.0
        assert basis.startswith("approx: native twin")


class TestWasmInTheCommand:
    def test_wasm_uploaders_are_sized_and_labelled(self, capsys, wasm_manifest_path):
        from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

        with pytest.raises(SystemExit):
            cmd_estimate_client_gas(_args(config=wasm_manifest_path, base_fee_gwei=1e-6))
        out = capsys.readouterr().out
        assert "[wasm]" in out
        assert "two transactions" in out

    def test_wasm_uploaders_count_toward_the_funder_total(
        self, capsys, wasm_manifest_path, manifest_path
    ):
        """The funder check compares against the FULL total, so omitting the
        WASM tier understated it by a whole wallet per WASM uploader."""
        from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

        def _total(path):
            with pytest.raises(SystemExit):
                cmd_estimate_client_gas(_args(config=path, base_fee_gwei=1e-6))
            out = capsys.readouterr().out
            line = next(ln for ln in out.splitlines() if "Total to fund" in ln)
            return float(line.split("Total to fund:")[1].split("ETH")[0].strip())

        assert _total(wasm_manifest_path) > _total(manifest_path)

    def test_the_advice_names_both_blocks(self, capsys, wasm_manifest_path):
        from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

        with pytest.raises(SystemExit) as exc:
            cmd_estimate_client_gas(_args(config=wasm_manifest_path, base_fee_gwei=50.0))
        assert exc.value.code == 2
        out = capsys.readouterr().out.replace("\n", "")
        assert "testnet.clients.uploaders" in out
        assert "wasm_uploaders" in out

    def test_a_native_only_manifest_says_nothing_about_wasm(self, capsys, manifest_path):
        from saorsa_deploy.cmd.estimate_client_gas import cmd_estimate_client_gas

        with pytest.raises(SystemExit):
            cmd_estimate_client_gas(_args(config=manifest_path, base_fee_gwei=1e-6))
        out = capsys.readouterr().out
        assert "wasm" not in out.lower()
