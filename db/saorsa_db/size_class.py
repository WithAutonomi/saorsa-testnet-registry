"""Size classification for production upload-pool files.

A pool file is selected by *size class* rather than by byte count — an operator
asks for "5 small files", not "5 files between 10485760 and 104857599 bytes".

The class is **derived from ``size_bytes``, never stored**. Storing it would
leave every catalogued row carrying a label that a later boundary change
silently invalidates; deriving it means the boundaries live in exactly one
place and a change reclassifies the whole catalogue for free.

Deliberately NOT the same concept as ``file_size_buckets_mb`` in
``deploy/saorsa_deploy/resources/gates/staging-thresholds.yaml``. Those are
half-open MB edges used to bucket *transfer durations* for the staging gates,
are calibrated to the staging file mix, and are recalibrated quarterly. These
are a fixed, operator-facing taxonomy for *selection*. The two must not be
wired into each other, which is why nothing here is called a "bucket".

Boundaries are **binary** (MiB/GiB, matching how file sizes are displayed
throughout the tool) and each class is the **half-open** interval
``[lower, upper)``: exactly 10 MiB is ``small``, not ``tiny``.

A class alone is sometimes too coarse. A canary measures chunk loss, and the
chunk floor is 4 — a 14 KB file yields as many chunks as a 5 MB one — so
"``tiny``, but no smaller than 5 MB" is a real request that no class can
express. :func:`parse_size` and :func:`bounded_byte_range` add explicit byte
bounds that *narrow* a class request rather than replacing it, so the shape of
a selection (37 tiny / 233 small / 112 medium) survives.
"""

from __future__ import annotations

import enum
import re

_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB
_TIB = 1024 * _GIB

#: Binary throughout, and ``MB`` means MiB. That is deliberately the same
#: convention :func:`format_size` prints in, so a bound pasted back from
#: displayed output selects exactly the file it was read from.
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": _KIB,
    "kb": _KIB,
    "kib": _KIB,
    "m": _MIB,
    "mb": _MIB,
    "mib": _MIB,
    "g": _GIB,
    "gb": _GIB,
    "gib": _GIB,
    "t": _TIB,
    "tb": _TIB,
    "tib": _TIB,
}

_SIZE_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


class SizeClass(enum.Enum):
    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    XLARGE = "xlarge"


# Ordered smallest-first. Each entry is (class, lower_inclusive,
# upper_exclusive); ``None`` upper means unbounded. Ordering is relied upon by
# ``ordered()`` for stable display and by the contiguity check below.
_BOUNDS: tuple[tuple[SizeClass, int, int | None], ...] = (
    (SizeClass.TINY, 0, 10 * _MIB),
    (SizeClass.SMALL, 10 * _MIB, 100 * _MIB),
    (SizeClass.MEDIUM, 100 * _MIB, 1 * _GIB),
    (SizeClass.LARGE, 1 * _GIB, 5 * _GIB),
    (SizeClass.XLARGE, 5 * _GIB, None),
)


def ordered() -> list[SizeClass]:
    """Every size class, smallest first."""
    return [entry[0] for entry in _BOUNDS]


def classify(size_bytes: int) -> SizeClass:
    """Return the size class a byte count falls into.

    Raises ``ValueError`` for a negative size — a caller reaching here with one
    has a bug upstream (a failed ``stat``, or a HEAD response parsed wrongly),
    and silently bucketing it as ``tiny`` would hide that.
    """
    if size_bytes < 0:
        raise ValueError(f"size_bytes cannot be negative: {size_bytes}")
    for size_class, lower, upper in _BOUNDS:
        if size_bytes >= lower and (upper is None or size_bytes < upper):
            return size_class
    # Unreachable: the bounds are contiguous from 0 and the last is unbounded.
    raise AssertionError(f"no size class covers {size_bytes} bytes")


def byte_range(size_class: SizeClass) -> tuple[int, int | None]:
    """Return ``(lower_inclusive, upper_exclusive)`` for a class.

    This is the inverse of :func:`classify` and exists so repository queries can
    filter on the indexed ``size_bytes`` column directly. Filtering any other
    way — computing the class per row in Python, or storing it — would defeat
    the index on a catalogue holding hundreds of thousands of rows.
    """
    for candidate, lower, upper in _BOUNDS:
        if candidate is size_class:
            return lower, upper
    raise ValueError(f"unknown size class: {size_class!r}")


def parse(name: str) -> SizeClass:
    """Parse an operator-supplied class name (CLI token or manifest key)."""
    if not isinstance(name, str):
        raise ValueError(f"size class must be a string, got {type(name).__name__}")
    normalised = name.strip().lower()
    for size_class in ordered():
        if size_class.value == normalised:
            return size_class
    valid = ", ".join(c.value for c in ordered())
    raise ValueError(f"unknown size class '{name}'. Valid classes: {valid}")


def parse_size(value: str | int | float) -> int:
    """Parse an operator-supplied byte size: ``5MB``, ``1.5GB``, ``512``.

    Units are binary and case-insensitive; a bare number is bytes. A float is
    accepted only via a string (``"1.5GB"``) and truncated to whole bytes.

    Raises ``ValueError`` on anything unrecognised rather than falling back to a
    default. A bound that silently parsed to zero would widen the selection
    instead of narrowing it, which is the opposite of what was asked for and
    invisible until the wrong files had been spent.
    """
    if isinstance(value, bool):
        raise ValueError(f"size must be a number or a string like '5MB', got {value!r}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        match = _SIZE_PATTERN.match(value)
        if not match:
            raise ValueError(
                f"could not parse size '{value}'. Expected a number with an "
                "optional binary unit, e.g. 5MB, 1.5GB, 512KB or 1048576"
            )
        number, unit = match.group(1), match.group(2).lower()
        if unit not in _SIZE_UNITS:
            valid = ", ".join(sorted(u for u in _SIZE_UNITS if u))
            raise ValueError(
                f"unknown size unit '{unit}' in '{value}'. Valid units: {valid}"
            )
        parsed = int(float(number) * _SIZE_UNITS[unit])
    else:
        raise ValueError(f"size must be a number or a string like '5MB', got {value!r}")
    if parsed < 0:
        raise ValueError(f"size cannot be negative: {value!r}")
    return parsed


def bounded_byte_range(
    size_class: SizeClass,
    min_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
) -> tuple[int, int | None] | None:
    """Intersect a class's range with explicit bounds.

    Returns ``(lower_inclusive, upper_exclusive)``, or ``None`` when the
    intersection is empty — ``max_size`` of 5 MB against the ``medium`` class
    can never match anything, and the caller must say so rather than issue a
    query guaranteed to return no rows.

    ``min_size`` is **inclusive** and ``max_size`` **exclusive**, matching the
    half-open convention the classes themselves use. So ``min_size=10MB,
    max_size=100MB`` selects precisely what ``small`` selects, rather than
    almost.
    """
    lower, upper = byte_range(size_class)
    if min_size_bytes is not None:
        lower = max(lower, min_size_bytes)
    if max_size_bytes is not None:
        upper = max_size_bytes if upper is None else min(upper, max_size_bytes)
    if upper is not None and lower >= upper:
        return None
    return lower, upper


def describe_bounds(
    min_size_bytes: int | None = None, max_size_bytes: int | None = None
) -> str:
    """Render bounds for operator-facing output; empty string when unbounded."""
    if min_size_bytes is None and max_size_bytes is None:
        return ""
    if min_size_bytes is not None and max_size_bytes is not None:
        return f"{format_size(min_size_bytes)} – {format_size(max_size_bytes)}"
    if min_size_bytes is not None:
        return f"≥ {format_size(min_size_bytes)}"
    return f"< {format_size(max_size_bytes)}"


def describe(size_class: SizeClass) -> str:
    """Human-readable interval for a class, e.g. ``"10.00 MB – 100.00 MB"``."""
    lower, upper = byte_range(size_class)
    if upper is None:
        return f"{format_size(lower)}+"
    return f"{format_size(lower)} – {format_size(upper)}"


def format_size(num_bytes: int | None) -> str:
    """Format a byte count for display, in binary units.

    Matches the existing presentation in ``cmd/prod_files.py`` so pool output
    and upload-history output describe the same file identically.
    """
    if num_bytes is None:
        return "<unknown>"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"
