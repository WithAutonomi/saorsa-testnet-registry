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
"""

from __future__ import annotations

import enum

_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB


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
