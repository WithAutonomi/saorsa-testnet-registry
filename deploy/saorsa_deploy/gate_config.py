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

    success_rate_drop_pp: float
    transfer_duration_p50_increase_percent: float
    transfer_duration_p95_increase_percent: float
    node_cpu_increase_percent: float
    node_rss_increase_percent: float
    outbound_bytes_per_gb_increase_percent: float
    disk_bytes_per_gb_increase_percent: float

    memory_leak: MemoryLeakConfig
    noise_rule: NoiseRuleConfig

    node_rss_service_percentile: float = 95.0
    file_size_buckets_mb: tuple[float, ...] = field(default=())
    outbound_ratio_metric: str = "outbound_bytes_per_gb_traffic"
    max_window_length_ratio: float = 1.25
    max_activity_coverage_ratio: float = 1.3


_HARD_GATE_KEYS = (
    "upload_success_floor_percent",
    "download_success_floor_percent",
)

_SOFT_GATE_KEYS = (
    "success_rate_drop_pp",
    "transfer_duration_p50_increase_percent",
    "transfer_duration_p95_increase_percent",
    "node_cpu_increase_percent",
    "node_rss_increase_percent",
    "outbound_bytes_per_gb_increase_percent",
    "disk_bytes_per_gb_increase_percent",
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

    return GateConfig(
        version=version,
        source_path=str(source_path),
        **hard_values,
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
