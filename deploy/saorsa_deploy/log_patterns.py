"""Load and validate the versioned log-string catalogue (V2-842).

Connection errors are counted on every staging run and the counts are gated on.
The strings themselves belong to saorsa-core / saorsa-transport / ant-node and
change when those crates change, so they live in a git-versioned YAML file
shipped with the package -- like the gate thresholds beside them -- and every
recorded count carries the catalogue ``version`` that produced it.

Validation is strict and fails loudly, for the same reason ``gate_config.py``'s
is: a pattern silently dropped by a typo'd key would record no count, the gate
would read NOT ASSESSED, and nothing would say why. A missing measurement that
announces itself is recoverable; one that does not is a hole in the record.

Nothing here does I/O beyond reading the file, and the parsed result is frozen,
so the query builder cannot be handed a half-populated catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from saorsa_deploy.resources import get_resources_dir

LOG_PATTERNS_DIRNAME = "log_patterns"
DEFAULT_PATTERN_SET = "connection-errors"

# How a group's member counts combine into the one number the gate sees.
AGGREGATE_MAX = "max"
AGGREGATE_SUM = "sum"
_VALID_AGGREGATES = (AGGREGATE_MAX, AGGREGATE_SUM)

# Which forwarded index the counts are taken from. Only the node tier today:
# client logs land in a different index with a different document shape, so
# pooling them would compare unlike things.
SCOPE_NODES = "nodes"
_VALID_SCOPES = (SCOPE_NODES,)

# Mirrors the column widths on `deployment_log_event_counts`. Checked here so a
# too-long key fails at load with a clear message rather than at the very end of
# a capture, after the Elasticsearch queries have already run.
_MAX_KEY_LENGTH = 64
_MAX_PHRASE_LENGTH = 512


class LogPatternError(ValueError):
    """Raised when the pattern catalogue is missing keys or malformed."""


@dataclass(frozen=True)
class LogPatternGroup:
    """A set of patterns the comparison evaluates as one gate."""

    key: str
    label: str
    aggregate: str
    note: str = ""
    #: Reported in full but never part of the overall verdict. For a group that
    #: measures the ENVIRONMENT the run happened in rather than the release --
    #: the payment RPC being rate-limited, say -- a FAIL would blame the
    #: release for the weather. The comparison still renders the row, marked
    #: advisory, so the figure is in front of the reader beside the gates it
    #: confounds.
    advisory: bool = False
    #: For an advisory group only: the gates a rise in this group casts doubt
    #: on, in plain words. The comparison repeats it as a WARNING whenever the
    #: candidate's per-hour rate is well above the baseline's, so the caveat
    #: sits at the top of the report rather than in a YAML comment nobody reads.
    confounds: str = ""


@dataclass(frozen=True)
class LogPattern:
    """One log string to count, and which group's gate it feeds."""

    key: str
    group: str
    phrase: str
    module: str = ""
    note: str = ""


@dataclass(frozen=True)
class LogPatternSet:
    """A whole catalogue: its version, its groups, and its patterns."""

    name: str
    version: int
    source_path: str
    index_scope: str
    groups: tuple[LogPatternGroup, ...]
    patterns: tuple[LogPattern, ...]

    def group(self, key: str) -> LogPatternGroup | None:
        for group in self.groups:
            if group.key == key:
                return group
        return None

    def patterns_in_group(self, key: str) -> tuple[LogPattern, ...]:
        return tuple(p for p in self.patterns if p.group == key)


def get_default_patterns_path() -> Path:
    """Absolute path to the shipped default catalogue."""
    return (
        get_resources_dir() / LOG_PATTERNS_DIRNAME / f"{DEFAULT_PATTERN_SET}.yaml"
    )


def _require_str(entry: dict, key: str, path: Path, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LogPatternError(
            f"{path}: {where} is missing a non-empty '{key}:'."
        )
    return value.strip()


def parse_pattern_set(data: dict, source_path: str, name: str) -> LogPatternSet:
    """Validate a parsed catalogue mapping into a :class:`LogPatternSet`."""
    path = Path(source_path)
    if not isinstance(data, dict):
        raise LogPatternError(f"{path}: expected a YAML mapping at the top level.")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise LogPatternError(
            f"{path}: 'version:' must be an integer. It is stored on every "
            "recorded count so two runs cannot silently disagree about what "
            "was counted."
        )

    index_scope = data.get("index_scope")
    if index_scope not in _VALID_SCOPES:
        raise LogPatternError(
            f"{path}: 'index_scope:' must be one of {', '.join(_VALID_SCOPES)}, "
            f"got {index_scope!r}."
        )

    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise LogPatternError(f"{path}: 'groups:' must be a non-empty list.")

    groups: list[LogPatternGroup] = []
    seen_groups: set[str] = set()
    for index, entry in enumerate(raw_groups):
        if not isinstance(entry, dict):
            raise LogPatternError(f"{path}: groups[{index}] must be a mapping.")
        key = _require_str(entry, "key", path, f"groups[{index}]")
        if len(key) > _MAX_KEY_LENGTH:
            raise LogPatternError(
                f"{path}: group key '{key}' exceeds {_MAX_KEY_LENGTH} characters."
            )
        if key in seen_groups:
            raise LogPatternError(
                f"{path}: duplicate group key '{key}'. Two groups sharing a key "
                "would make the gate they produce ambiguous."
            )
        seen_groups.add(key)
        aggregate = entry.get("aggregate")
        if aggregate not in _VALID_AGGREGATES:
            raise LogPatternError(
                f"{path}: group '{key}' has aggregate {aggregate!r}; must be one "
                f"of {', '.join(_VALID_AGGREGATES)}. 'max' is for one event "
                "logged at several layers, 'sum' for genuinely distinct events."
            )
        advisory = entry.get("advisory", False)
        if not isinstance(advisory, bool):
            raise LogPatternError(
                f"{path}: group '{key}' has advisory {advisory!r}; must be true "
                "or false. An advisory group is reported but never decides the "
                "verdict, so a value that is neither would be a silent choice."
            )
        confounds = str(entry.get("confounds") or "").strip()
        if confounds and not advisory:
            raise LogPatternError(
                f"{path}: group '{key}' has 'confounds:' but is not advisory. "
                "Only an environmental (advisory) group can confound the other "
                "gates; a counted group IS one of the gates."
            )
        groups.append(
            LogPatternGroup(
                key=key,
                label=_require_str(entry, "label", path, f"group '{key}'"),
                aggregate=aggregate,
                note=str(entry.get("note") or "").strip(),
                advisory=advisory,
                confounds=confounds,
            )
        )

    raw_patterns = data.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise LogPatternError(f"{path}: 'patterns:' must be a non-empty list.")

    patterns: list[LogPattern] = []
    seen_keys: set[str] = set()
    for index, entry in enumerate(raw_patterns):
        if not isinstance(entry, dict):
            raise LogPatternError(f"{path}: patterns[{index}] must be a mapping.")
        key = _require_str(entry, "key", path, f"patterns[{index}]")
        if len(key) > _MAX_KEY_LENGTH:
            raise LogPatternError(
                f"{path}: pattern key '{key}' exceeds {_MAX_KEY_LENGTH} characters."
            )
        if key in seen_keys:
            raise LogPatternError(
                f"{path}: duplicate pattern key '{key}'. Counts are stored one "
                "row per key, so the second would overwrite the first."
            )
        seen_keys.add(key)

        phrase = _require_str(entry, "phrase", path, f"pattern '{key}'")
        if len(phrase) > _MAX_PHRASE_LENGTH:
            raise LogPatternError(
                f"{path}: pattern '{key}' has a phrase longer than "
                f"{_MAX_PHRASE_LENGTH} characters, which the registry cannot "
                "store. A long phrase is also fragile -- prefer a stable "
                "substring."
            )

        group = _require_str(entry, "group", path, f"pattern '{key}'")
        if group not in seen_groups:
            raise LogPatternError(
                f"{path}: pattern '{key}' names group '{group}', which is not "
                "defined under 'groups:'. Its count would be recorded and then "
                "never gated on."
            )

        patterns.append(
            LogPattern(
                key=key,
                group=group,
                phrase=phrase,
                module=str(entry.get("module") or "").strip(),
                note=str(entry.get("note") or "").strip(),
            )
        )

    # A group with no patterns produces a gate with nothing behind it, which
    # would report NOT ASSESSED forever and look like a measurement failure.
    empty = [g.key for g in groups if not any(p.group == g.key for p in patterns)]
    if empty:
        raise LogPatternError(
            f"{path}: group(s) {', '.join(sorted(empty))} have no patterns. An "
            "empty group produces a gate that can never be measured."
        )

    return LogPatternSet(
        name=name,
        version=version,
        source_path=str(source_path),
        index_scope=index_scope,
        groups=tuple(groups),
        patterns=tuple(patterns),
    )


def load_pattern_set(path: str | Path | None = None) -> LogPatternSet:
    """Load the catalogue, defaulting to the one shipped with the package."""
    resolved = Path(path) if path else get_default_patterns_path()
    if not resolved.is_file():
        raise LogPatternError(f"{resolved}: pattern catalogue not found.")
    try:
        data = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as exc:
        raise LogPatternError(
            f"{resolved}: could not be parsed as YAML: {exc}"
        ) from exc
    return parse_pattern_set(data, str(resolved), resolved.stem)
