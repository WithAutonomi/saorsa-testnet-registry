"""Load and validate the versioned staging gate thresholds (V2-721).

The release process requires that thresholds "live in versioned config, not
code, so calibration doesn't require a release of the tool" -- there is a
quarterly threshold review that recalibrates the soft-gate percentages against
accumulated run history. So the numbers live in a YAML file shipped with the
package, git-versioned like the deployment profiles beside it, and every loaded
config carries a ``version`` that is echoed into the report and stored on the
persisted verdict.

Validation is strict and fails loudly. A threshold silently defaulting to zero
because of a typo in a key name would turn a gate into a rubber stamp while
still printing PASS, which is worse than not having the gate at all.

Nothing here does I/O beyond reading the file: the parsed result is a frozen
dataclass so the comparison functions cannot be handed a half-populated config.
"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from saorsa_deploy.resources import get_resources_dir

GATES_DIRNAME = "gates"
DEFAULT_THRESHOLDS_FILENAME = "staging-thresholds.yaml"


def get_default_thresholds_path() -> Path:
    """Return the absolute path to the shipped default threshold config."""
    return get_resources_dir() / GATES_DIRNAME / DEFAULT_THRESHOLDS_FILENAME


class GateConfigError(ValueError):
    """Raised when the threshold config is missing keys or malformed.

    A distinct type so the CLI can report it as an operator error with the
    offending file path, rather than as an unhandled crash.
    """


@dataclass(frozen=True)
class MemoryLeakConfig:
    window_hours: float
    max_growth_percent_per_hour: float
    min_points: int
    service_percentile: float = 50.0
    tail_threshold_percent_per_hour: float = 25.0


@dataclass(frozen=True)
class NoiseRuleConfig:
    min_history_runs: int
    sigma_multiplier: float


@dataclass(frozen=True)
class GateConfig:
    """Every threshold the comparison applies, plus the version that produced it."""

    version: int
    source_path: str

    upload_success_floor_percent: float
    download_success_floor_percent: float

    pointer_write_success_floor_percent: float
    pointer_read_success_floor_percent: float

    success_rate_drop_pp: float
    transfer_duration_p50_increase_percent: float
    transfer_duration_p95_increase_percent: float
    node_cpu_increase_percent: float
    node_rss_increase_percent: float
    outbound_bytes_per_gb_increase_percent: float
    disk_bytes_per_gb_increase_percent: float
    connection_error_increase_percent: float
    pointer_stale_read_rate_increase_percent: float
    pointer_lost_write_rate_increase_percent: float
    pointer_write_latency_p50_increase_percent: float
    pointer_write_latency_p95_increase_percent: float
    pointer_read_latency_p50_increase_percent: float
    pointer_read_latency_p95_increase_percent: float
    pointer_cost_per_write_increase_percent: float

    memory_leak: MemoryLeakConfig
    noise_rule: NoiseRuleConfig

    node_rss_service_percentile: float = 95.0
    file_size_buckets_mb: tuple[float, ...] = field(default=())
    outbound_ratio_metric: str = "outbound_bytes_per_gb_traffic"
    max_window_length_ratio: float = 1.25
    max_activity_coverage_ratio: float = 1.3
    # WASM (browser-path) client gates, split into the two kinds because they
    # deserve opposite answers.
    #
    # FLOOR gates -- "did 99% of uploads succeed" -- are judged against
    # absolute numbers calibrated on the native transport, which the WASM tier
    # does not yet meet: on the first measured run it managed 92.1% upload and
    # 94.9% download against floors of 99% and 99.5%. That is not a regression,
    # it is a NAT'd peer being unreachable over WebRTC-Direct until relayed
    # WebRTC exists. Gating on it would hard-fail every run on a tier that is
    # not yet what ships.
    #
    # RELATIVE gates -- success rate and per-bucket p50/p95 against the
    # BASELINE -- need no absolute calibration at all: they ask whether this
    # release made the WASM tier worse than the last one did, which is exactly
    # the question a soft gate exists to answer. So they COUNT.
    #
    # Neither ever reaches the exit code: that is reserved for the native hard
    # floors.
    wasm_floor_gates_advisory: bool = True
    wasm_relative_gates_advisory: bool = False
    # Pointer client gates (V2-1339). The two FLOORS are absolute numbers on a
    # tier whose expected stale-read rate is not known from any run yet -- the
    # ADR says a non-zero rate is normal but not how large -- so gating a ship on
    # them would be gating on a guess. Advisory by default, and promotion is a
    # decision for after a few trains. The RELATIVE gates count as soft gates
    # like every other, because "did this release make the pointer tier worse
    # than the last one did" needs no calibration.
    #
    # NO pointer gate ever reaches the exit code, advisory or not: that is
    # reserved for the native file-op hard floors.
    pointer_floor_gates_advisory: bool = True


_HARD_GATE_KEYS = (
    "upload_success_floor_percent",
    "download_success_floor_percent",
)

#: Pointer floors. In their own section rather than `hard_gates`, because they
#: are absolute but are never hard: a `[pointer]` FAIL is reported and folded
#: into the overall verdict, and never into the exit code.
_POINTER_FLOOR_KEYS = (
    "pointer_write_success_floor_percent",
    "pointer_read_success_floor_percent",
)

_SOFT_GATE_KEYS = (
    "success_rate_drop_pp",
    "transfer_duration_p50_increase_percent",
    "transfer_duration_p95_increase_percent",
    "node_cpu_increase_percent",
    "node_rss_increase_percent",
    "outbound_bytes_per_gb_increase_percent",
    "disk_bytes_per_gb_increase_percent",
    "connection_error_increase_percent",
    # Pointer soft gates, all relative to the baseline.
    "pointer_stale_read_rate_increase_percent",
    "pointer_lost_write_rate_increase_percent",
    "pointer_write_latency_p50_increase_percent",
    "pointer_write_latency_p95_increase_percent",
    "pointer_read_latency_p50_increase_percent",
    "pointer_read_latency_p95_increase_percent",
    "pointer_cost_per_write_increase_percent",
)

# The three ratio metrics `record-analysis` / `derive-metrics` can write. Only
# the two outbound ones are a legal choice for the outbound gate; naming the
# disk ratio here would silently evaluate the disk gate twice.
_VALID_OUTBOUND_METRICS = (
    "outbound_bytes_per_gb_traffic",
    "outbound_bytes_per_gb_uploaded",
)


def _require_section(data: dict, key: str, path: Path) -> dict:
    section = data.get(key)
    if not isinstance(section, dict):
        raise GateConfigError(
            f"{path}: missing or malformed '{key}:' section (expected a mapping)."
        )
    return section


def _require_number(
    section: dict, key: str, path: Path, section_name: str, *, positive: bool = True
) -> float:
    if key not in section:
        raise GateConfigError(f"{path}: '{section_name}.{key}' is missing.")
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateConfigError(
            f"{path}: '{section_name}.{key}' must be a number, got {value!r}."
        )
    if positive and value <= 0:
        raise GateConfigError(
            f"{path}: '{section_name}.{key}' must be greater than zero, got {value}. "
            "A zero or negative threshold would make the gate always fail or "
            "never fire."
        )
    return float(value)


def _require_bool(data: dict, key: str, path: Path) -> bool:
    """Require an explicit boolean. Absent is an error, not a default.

    Whether a gate counts towards the verdict is not a detail to infer. A key
    that quietly defaulted would decide, invisibly, whether a FAIL blocks the
    ship -- the same reason every threshold in this file is required.
    """
    if key not in data:
        raise GateConfigError(
            f"{path}: '{key}:' is missing. It decides whether a failing gate "
            "counts towards the overall verdict, which must be stated rather "
            "than inferred."
        )
    value = data[key]
    if not isinstance(value, bool):
        raise GateConfigError(
            f"{path}: '{key}:' must be true or false, got {value!r}."
        )
    return value


def parse_gate_config(data: dict, source_path: str) -> GateConfig:
    """Validate a parsed threshold mapping into a :class:`GateConfig`.

    Separate from the file read so the validation is unit-testable without a
    temporary file, and so a caller holding a config from elsewhere can reuse it.
    """
    path = Path(source_path)
    if not isinstance(data, dict):
        raise GateConfigError(f"{path}: expected a YAML mapping at the top level.")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise GateConfigError(
            f"{path}: 'version:' must be an integer. It is stored on every "
            "verdict so two reports cannot silently disagree."
        )

    hard = _require_section(data, "hard_gates", path)
    pointer_floors = _require_section(data, "pointer_floor_gates", path)
    soft = _require_section(data, "soft_gates", path)
    leak = _require_section(data, "memory_leak", path)
    noise = _require_section(data, "noise_rule", path)

    hard_values = {
        key: _require_number(hard, key, path, "hard_gates") for key in _HARD_GATE_KEYS
    }
    for key, value in hard_values.items():
        if value > 100:
            raise GateConfigError(
                f"{path}: 'hard_gates.{key}' is {value}, but a success floor is a "
                "percentage and cannot exceed 100."
            )

    pointer_floor_values = {
        key: _require_number(pointer_floors, key, path, "pointer_floor_gates")
        for key in _POINTER_FLOOR_KEYS
    }
    for key, value in pointer_floor_values.items():
        if value > 100:
            raise GateConfigError(
                f"{path}: 'pointer_floor_gates.{key}' is {value}, but a success "
                "floor is a percentage and cannot exceed 100."
            )

    soft_values = {
        key: _require_number(soft, key, path, "soft_gates") for key in _SOFT_GATE_KEYS
    }

    buckets = data.get("file_size_buckets_mb")
    if not isinstance(buckets, list) or len(buckets) < 2:
        raise GateConfigError(
            f"{path}: 'file_size_buckets_mb:' must be a list of at least two "
            "edges (each bucket is half-open [lower, upper))."
        )
    if any(isinstance(b, bool) or not isinstance(b, (int, float)) for b in buckets):
        raise GateConfigError(f"{path}: 'file_size_buckets_mb:' must contain numbers.")
    if list(buckets) != sorted(buckets) or len(set(buckets)) != len(buckets):
        raise GateConfigError(
            f"{path}: 'file_size_buckets_mb:' must be strictly ascending. Out-of-order "
            "or duplicate edges would assign a file to more than one bucket, or to "
            "none."
        )

    outbound_metric = data.get("outbound_ratio_metric")
    if outbound_metric not in _VALID_OUTBOUND_METRICS:
        raise GateConfigError(
            f"{path}: 'outbound_ratio_metric:' must be one of "
            f"{', '.join(_VALID_OUTBOUND_METRICS)}, got {outbound_metric!r}."
        )

    def _ratio(key: str) -> float:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
            raise GateConfigError(
                f"{path}: '{key}:' must be a number >= 1, got {value!r}. Below 1 it "
                "would flag every comparison, including a run against itself."
            )
        return float(value)

    max_ratio = _ratio("max_window_length_ratio")
    max_coverage_ratio = _ratio("max_activity_coverage_ratio")

    min_points = leak.get("min_points")
    if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 2:
        raise GateConfigError(
            f"{path}: 'memory_leak.min_points:' must be an integer >= 2. A line "
            "cannot be fitted through fewer than two points."
        )

    def _percentile(container: dict, key: str, section_name: str) -> float:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GateConfigError(
                f"{path}: '{section_name}.{key}' must be a number between 0 and 100."
            )
        if not 0 < value <= 100:
            raise GateConfigError(
                f"{path}: '{section_name}.{key}' must be greater than 0 and at most "
                f"100, got {value}."
            )
        return float(value)

    rss_percentile = _percentile(soft, "node_rss_service_percentile", "soft_gates")
    leak_percentile = _percentile(leak, "service_percentile", "memory_leak")

    min_history = noise.get("min_history_runs")
    if (
        isinstance(min_history, bool)
        or not isinstance(min_history, int)
        or min_history < 2
    ):
        raise GateConfigError(
            f"{path}: 'noise_rule.min_history_runs:' must be an integer >= 2. A "
            "standard deviation over fewer than two runs is undefined."
        )

    if "wasm_gates_advisory" in data:
        raise GateConfigError(
            f"{path}: 'wasm_gates_advisory:' has been replaced by TWO keys, "
            "because the two kinds of WASM gate deserve opposite answers: "
            "'wasm_floor_gates_advisory:' (the success FLOORS -- reported, "
            "never in the verdict, because a NAT'd peer is unreachable over "
            "WebRTC-Direct until relayed WebRTC exists) and "
            "'wasm_relative_gates_advisory:' (success rate and per-bucket "
            "p50/p95 against the BASELINE -- soft gates that COUNT). Set both."
        )

    return GateConfig(
        version=version,
        source_path=str(source_path),
        **hard_values,
        **pointer_floor_values,
        **soft_values,
        memory_leak=MemoryLeakConfig(
            window_hours=_require_number(leak, "window_hours", path, "memory_leak"),
            max_growth_percent_per_hour=_require_number(
                leak, "max_growth_percent_per_hour", path, "memory_leak"
            ),
            min_points=min_points,
            service_percentile=leak_percentile,
            tail_threshold_percent_per_hour=_require_number(
                leak, "tail_threshold_percent_per_hour", path, "memory_leak"
            ),
        ),
        noise_rule=NoiseRuleConfig(
            min_history_runs=min_history,
            sigma_multiplier=_require_number(
                noise, "sigma_multiplier", path, "noise_rule"
            ),
        ),
        node_rss_service_percentile=rss_percentile,
        file_size_buckets_mb=tuple(float(b) for b in buckets),
        outbound_ratio_metric=outbound_metric,
        max_window_length_ratio=max_ratio,
        max_activity_coverage_ratio=max_coverage_ratio,
        wasm_floor_gates_advisory=_require_bool(
            data, "wasm_floor_gates_advisory", path
        ),
        wasm_relative_gates_advisory=_require_bool(
            data, "wasm_relative_gates_advisory", path
        ),
        pointer_floor_gates_advisory=_require_bool(
            data, "pointer_floor_gates_advisory", path
        ),
    )


def load_gate_config(path: str | Path | None = None) -> GateConfig:
    """Load the threshold config, defaulting to the one shipped with the package."""
    resolved = Path(path) if path else get_default_thresholds_path()
    if not resolved.is_file():
        raise GateConfigError(f"{resolved}: threshold config not found.")
    try:
        data = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as exc:
        raise GateConfigError(f"{resolved}: could not be parsed as YAML: {exc}") from exc
    return parse_gate_config(data, str(resolved))
