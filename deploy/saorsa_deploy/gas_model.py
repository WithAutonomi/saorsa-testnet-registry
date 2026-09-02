"""Estimate the native-ETH (gas) funding an uploader client needs for a run.

Uploader wallets are funded with a FIXED amount of ETH, so their runway is
inversely proportional to the gas price. On staging run #566 the Arbitrum
Sepolia base fee had risen ~10x in three days and six of ten uploaders
exhausted mid-run, failing every subsequent upload and pushing two hard gates
below their floors for a reason unrelated to the release under test. This
module turns "how much ETH per uploader" from a hand-set constant into a
figure derived from the current base fee and the previous run's measured
throughput.

Everything here is pure: the caller supplies the current base fee and the
expected upload counts, so the arithmetic is testable without a chain or a
database. Fetching those lives in the CLI wrapper.

The model and its calibration live in
``resources/gas/client-gas-model.yaml`` -- config, not code, so recalibrating
after a price regime change does not need a release. See that file for the
measured basis and the known limits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import yaml

from saorsa_deploy.resources import get_resources_dir

GAS_DIRNAME = "gas"
DEFAULT_GAS_MODEL_FILENAME = "client-gas-model.yaml"


def get_default_gas_model_path() -> Path:
    """Return the absolute path to the shipped default gas model."""
    return get_resources_dir() / GAS_DIRNAME / DEFAULT_GAS_MODEL_FILENAME


class GasModelError(ValueError):
    """Raised when the gas model config is missing keys or malformed.

    A distinct type so the CLI can report it as an operator error naming the
    offending file, rather than as an unhandled crash. Validation is strict:
    a coefficient silently defaulting to zero would recommend funding every
    uploader with nothing, which is precisely the failure this exists to stop.
    """


@dataclass(frozen=True)
class GasModel:
    """Cost-per-upload model, calibrated at ``reference_base_fee_gwei``."""

    version: int
    fixed_eth_per_upload: float
    eth_per_gb: float
    reference_base_fee_gwei: float
    safety_factor: float
    round_up_to_eth: float
    minimum_eth_per_uploader: float

    def eth_per_upload(self, size_gb: float, base_fee_gwei: float) -> float:
        """Modelled ETH for one upload of ``size_gb`` at ``base_fee_gwei``.

        Excludes the safety factor -- that is applied once per uploader in
        :func:`estimate_uploader`, not per upload, so it does not compound.
        """
        if size_gb < 0:
            raise GasModelError(f"size_gb must be >= 0, got {size_gb}")
        if base_fee_gwei <= 0:
            raise GasModelError(
                f"base_fee_gwei must be > 0, got {base_fee_gwei}"
            )
        at_reference = self.fixed_eth_per_upload + self.eth_per_gb * size_gb
        return at_reference * (base_fee_gwei / self.reference_base_fee_gwei)


def _require(data: dict, key: str, path: Path, kind=float) -> float:
    if key not in data:
        raise GasModelError(f"{path}: missing required key '{key}'")
    value = data[key]
    try:
        value = kind(value)
    except (TypeError, ValueError):
        raise GasModelError(
            f"{path}: key '{key}' must be a number, got {value!r}"
        )
    if value <= 0:
        raise GasModelError(
            f"{path}: key '{key}' must be > 0, got {value!r}. A zero or "
            "negative coefficient would silently recommend no funding."
        )
    return value


def load_gas_model(path: Optional[Path] = None) -> GasModel:
    """Load and validate the gas model, defaulting to the shipped config."""
    path = Path(path) if path else get_default_gas_model_path()
    if not path.is_file():
        raise GasModelError(f"gas model config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise GasModelError(f"{path}: not valid YAML: {exc}")
    if not isinstance(raw, dict):
        raise GasModelError(f"{path}: expected a mapping at the top level")
    if "version" not in raw:
        raise GasModelError(
            f"{path}: missing 'version'. Every estimate records the model "
            "version so a later reader can tell whether two estimates are "
            "comparable."
        )
    model = raw.get("model")
    if not isinstance(model, dict):
        raise GasModelError(f"{path}: missing or malformed 'model' section")
    return GasModel(
        version=int(raw["version"]),
        fixed_eth_per_upload=_require(model, "fixed_eth_per_upload", path),
        eth_per_gb=_require(model, "eth_per_gb", path),
        reference_base_fee_gwei=_require(
            model, "reference_base_fee_gwei", path
        ),
        safety_factor=_require(raw, "safety_factor", path),
        round_up_to_eth=_require(raw, "round_up_to_eth", path),
        minimum_eth_per_uploader=_require(
            raw, "minimum_eth_per_uploader", path
        ),
    )


@dataclass(frozen=True)
class UploaderPlan:
    """One uploader's shape, as it will run."""

    name: str
    size_gb: float
    expected_uploads: int
    payment: Optional[str] = None
    current_eth: Optional[float] = None


@dataclass(frozen=True)
class UploaderEstimate:
    """What one uploader needs, and how that compares to what it has."""

    plan: UploaderPlan
    modelled_eth: float
    recommended_eth: float
    current_eth: Optional[float]

    @property
    def shortfall_eth(self) -> float:
        """How far the current setting falls short. 0.0 if it is adequate."""
        if self.current_eth is None:
            return 0.0
        return max(self.recommended_eth - self.current_eth, 0.0)

    @property
    def adequate(self) -> Optional[bool]:
        """True/False if a current setting is known, else None."""
        if self.current_eth is None:
            return None
        return self.current_eth >= self.recommended_eth


def _round_up(value: float, step: float) -> float:
    if step <= 0:
        return value
    # Round the multiple itself before ceil: a value that is already an exact
    # multiple must not be pushed to the next one by float representation
    # error (0.15 / 0.05 is 2.9999999999999996 in binary floating point).
    multiples = value / step
    if math.isclose(multiples, round(multiples), rel_tol=0, abs_tol=1e-9):
        multiples = round(multiples)
    else:
        multiples = math.ceil(multiples)
    return round(multiples * step, 10)


def estimate_uploader(
    plan: UploaderPlan, base_fee_gwei: float, model: GasModel
) -> UploaderEstimate:
    """Estimate one uploader's funding requirement."""
    if plan.expected_uploads < 0:
        raise GasModelError(
            f"{plan.name}: expected_uploads must be >= 0, "
            f"got {plan.expected_uploads}"
        )
    per_upload = model.eth_per_upload(plan.size_gb, base_fee_gwei)
    modelled = per_upload * plan.expected_uploads * model.safety_factor
    recommended = max(
        _round_up(modelled, model.round_up_to_eth),
        model.minimum_eth_per_uploader,
    )
    return UploaderEstimate(
        plan=plan,
        modelled_eth=modelled,
        recommended_eth=recommended,
        current_eth=plan.current_eth,
    )


@dataclass(frozen=True)
class FundingEstimate:
    """The whole fleet's requirement, and whether the funder can cover it."""

    model_version: int
    base_fee_gwei: float
    reference_base_fee_gwei: float
    safety_factor: float
    uploaders: Sequence[UploaderEstimate]
    funder_balance_eth: Optional[float] = None

    @property
    def total_recommended_eth(self) -> float:
        return round(sum(u.recommended_eth for u in self.uploaders), 10)

    @property
    def total_current_eth(self) -> Optional[float]:
        if any(u.current_eth is None for u in self.uploaders):
            return None
        return round(sum(u.current_eth or 0.0 for u in self.uploaders), 10)

    @property
    def price_ratio(self) -> float:
        """Current base fee as a multiple of the calibration reference."""
        return self.base_fee_gwei / self.reference_base_fee_gwei

    @property
    def funder_covers(self) -> Optional[bool]:
        """Whether the funding wallet can provision the recommendation.

        Deliberately compares against the FULL recommended total rather than
        the deficit. Top-up is per-wallet and sequential, so a funder that
        cannot cover the whole draw does not fail cleanly up front -- it funds
        the first few uploaders and then runs dry, leaving a half-provisioned
        run.
        """
        if self.funder_balance_eth is None:
            return None
        return self.funder_balance_eth >= self.total_recommended_eth

    @property
    def changed(self) -> Sequence[UploaderEstimate]:
        """Uploaders whose current setting differs from the recommendation."""
        return [
            u
            for u in self.uploaders
            if u.current_eth is None
            or not math.isclose(
                u.current_eth, u.recommended_eth, rel_tol=0, abs_tol=1e-9
            )
        ]


def estimate_funding(
    plans: Iterable[UploaderPlan],
    base_fee_gwei: float,
    model: GasModel,
    funder_balance_eth: Optional[float] = None,
) -> FundingEstimate:
    """Estimate funding for every uploader in ``plans``."""
    estimates = [estimate_uploader(p, base_fee_gwei, model) for p in plans]
    return FundingEstimate(
        model_version=model.version,
        base_fee_gwei=base_fee_gwei,
        reference_base_fee_gwei=model.reference_base_fee_gwei,
        safety_factor=model.safety_factor,
        uploaders=estimates,
        funder_balance_eth=funder_balance_eth,
    )


# ---------------------------------------------------------------------------
# Projecting throughput from a previous run
# ---------------------------------------------------------------------------

# A run whose uploaders all died early would otherwise project its own
# truncation forward. Guard against dividing by a near-zero active window.
MIN_ACTIVE_HOURS = 0.25


@dataclass(frozen=True)
class ClientRun:
    """One client's successful-upload record from a previous run."""

    host: str
    uploads: int
    active_hours: float
    size_gb: float


@dataclass(frozen=True)
class Throughput:
    """Per-file-size upload RATES derived from a previous run."""

    rate_by_size_gb: dict
    run_hours: float
    truncated: Sequence[ClientRun]


def project_throughput(
    clients: Iterable[ClientRun], run_hours: float
) -> Throughput:
    """Turn a previous run's per-client counts into rates per file size.

    **Rates, not raw counts, and this is the point of the function.** A client
    that exhausted its wallet mid-run stopped uploading, so its raw success
    count understates what a properly funded client would have done. Sizing
    the next run from that count would re-derive exactly the shortfall this
    module exists to prevent — the under-funded run teaches the next run to be
    under-funded. So each client's rate is taken over its OWN active window
    and projected across the full duration by the caller.

    Where several clients share a file size the fastest is kept: under-funding
    fails a run, while over-funding is swept back to the funder on destroy.
    """
    run_hours = max(run_hours, MIN_ACTIVE_HOURS)
    by_size: dict = {}
    truncated = []
    for c in clients:
        if c.uploads <= 0:
            continue
        active = max(c.active_hours, MIN_ACTIVE_HOURS)
        rate = c.uploads / active
        key = round(c.size_gb, 4)
        by_size[key] = max(by_size.get(key, 0.0), rate)
        if active < run_hours * 0.9:
            truncated.append(c)
    return Throughput(
        rate_by_size_gb=by_size, run_hours=run_hours, truncated=truncated
    )


def rate_for_size(size_gb: float, rate_by_size_gb: dict) -> Optional[float]:
    """Rate for the recorded file size closest to ``size_gb``.

    Only borrows across a comparable size: cost is size-dependent, so a 20 MB
    client's rate says nothing useful about a 1 GB one. Returns None rather
    than guessing, so the caller can say which uploaders it could not size
    instead of silently under-funding them.
    """
    if not rate_by_size_gb:
        return None
    key = round(size_gb, 4)
    if key in rate_by_size_gb:
        return rate_by_size_gb[key]
    nearest = min(rate_by_size_gb, key=lambda s: abs(s - size_gb))
    if nearest > 0 and 0.5 <= size_gb / nearest <= 2.0:
        return rate_by_size_gb[nearest]
    return None
