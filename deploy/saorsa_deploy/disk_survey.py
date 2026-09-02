"""Pure logic for the production disk survey: classifying a failed host probe,
and comparing two surveys like-for-like.

Both exist because of V2-1147. A survey makes one SSH probe per host; when that
probe fails the host is dropped from the survey entirely and the fleet totals
are computed from whatever remains — a silent under-count that reads as
reclaimed disk. Survey id 39 (2026-09-01) reported node data falling
28.1 -> 26.9 TiB, an apparent 1.192 TiB reclaim. Two hosts had simply dropped
out; across the 63 hosts present in both runs node data had *grown* 21.5 GiB.

The two dropped hosts were not down. They answered ICMP, port 22 was open, SSH
worked by hand, 15/15 services were running. They were CPU-saturated, so the
probe did not return inside the timeout. "Host is slow" is exactly the
condition a disk-pressure survey most needs to see, which is why a timeout is
classified apart from a refusal rather than sharing one opaque failure list.

No I/O here — no SSH, no database — so the arithmetic that decides whether a
fleet gained or lost disk is testable on its own.
"""

from dataclasses import dataclass, field
from enum import Enum


class SurveyFailureKind(str, Enum):
    """Why a host's probe did not produce a result.

    The distinction is operational, not cosmetic: TIMEOUT means the host is
    probably fine but under load (retry it, and treat its absence from the
    totals as a measurement gap), while UNREACHABLE means the host really is
    not answering (its disk genuinely stopped counting).
    """

    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    AUTH = "auth"
    PROBE_FAILED = "probe_failed"


#: Human-facing one-liners, shown next to the count in the failures table.
FAILURE_KIND_LABELS = {
    SurveyFailureKind.TIMEOUT: "timed out (host slow — probably up)",
    SurveyFailureKind.UNREACHABLE: "unreachable (host may be down)",
    SurveyFailureKind.AUTH: "authentication refused",
    SurveyFailureKind.PROBE_FAILED: "probe failed on the host",
}

# Matched case-insensitively against ssh's stderr. ssh exits 255 for any of its
# own errors, so the exit code alone cannot separate "refused" from "bad key" —
# only the message can.
_UNREACHABLE_MARKERS = (
    "connection refused",
    "connection closed",
    "connection reset",
    "no route to host",
    "host is down",
    "network is unreachable",
    "name or service not known",
    "could not resolve hostname",
    "connection timed out",
    "operation timed out",
    "broken pipe",
)

_AUTH_MARKERS = (
    "permission denied",
    "too many authentication failures",
    "host key verification failed",
    "no such identity",
    "unprotected private key",
)


@dataclass(frozen=True)
class SurveyFailure:
    """One host that could not be surveyed, and why."""

    host: str
    kind: SurveyFailureKind
    message: str
    attempts: int = 1

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "attempts": self.attempts,
        }

    @property
    def label(self) -> str:
        return FAILURE_KIND_LABELS[self.kind]


def classify_survey_failure(
    *,
    timed_out: bool = False,
    returncode: int | None = None,
    stderr: str = "",
    os_error: str | None = None,
) -> tuple[SurveyFailureKind, str]:
    """Classify one failed probe into a kind plus a short message.

    ``timed_out`` is the local subprocess timeout — the probe was still running
    when we gave up, which is the V2-1147 case. ``os_error`` is a failure to
    launch ssh at all. Otherwise the classification comes from ssh's own stderr,
    since exit code 255 covers every ssh-level error indiscriminately.
    """
    if timed_out:
        return SurveyFailureKind.TIMEOUT, "ssh timed out"
    if os_error is not None:
        return SurveyFailureKind.PROBE_FAILED, os_error

    text = (stderr or "").strip()
    message = text or f"ssh exit {returncode}"

    # ssh reports its own errors as 255; any other exit came from the remote
    # command, so the host answered and the probe itself is what failed. The
    # markers below must only be consulted in the ssh-level case — `du` writing
    # "Permission denied" about a directory is not an authentication problem.
    if returncode is not None and returncode != 255:
        return SurveyFailureKind.PROBE_FAILED, message

    haystack = text.lower()
    if any(m in haystack for m in _AUTH_MARKERS):
        return SurveyFailureKind.AUTH, message
    if any(m in haystack for m in _UNREACHABLE_MARKERS):
        return SurveyFailureKind.UNREACHABLE, message
    return SurveyFailureKind.UNREACHABLE, message


def should_retry(kind: SurveyFailureKind) -> bool:
    """Whether a failure of this kind is worth a second attempt.

    Everything except an auth failure is retried. A rejected key will be
    rejected again a minute later, and retrying it only delays the survey; a
    timeout, a refusal and a failed probe can all be transient.
    """
    return kind is not SurveyFailureKind.AUTH


@dataclass(frozen=True)
class SurveyComparison:
    """Two surveys compared, both raw and like-for-like.

    ``raw_delta`` is what the tool used to print and is kept only so the two
    numbers can be shown side by side — on a run that dropped a host it is the
    misleading one. ``common_delta`` is the answer: the change across the hosts
    both runs actually measured.
    """

    common_hosts: tuple[str, ...]
    previous_total: int
    latest_total: int
    previous_common_total: int
    latest_common_total: int
    dropped_hosts: tuple[str, ...]
    added_hosts: tuple[str, ...]

    @property
    def raw_delta(self) -> int:
        return self.latest_total - self.previous_total

    @property
    def common_delta(self) -> int:
        return self.latest_common_total - self.previous_common_total

    @property
    def common_delta_per_host(self) -> float:
        if not self.common_hosts:
            return 0.0
        return self.common_delta / len(self.common_hosts)

    @property
    def coverage_changed(self) -> bool:
        """True when the two runs did not measure the same set of hosts."""
        return bool(self.dropped_hosts or self.added_hosts)

    @property
    def misleading(self) -> bool:
        """True when the raw delta points the opposite way to the like-for-like
        one — the V2-1147 signature, where a dropped host reads as a reclaim.

        A zero on either side is not a disagreement about direction, so both
        must be non-zero and of opposite sign.
        """
        return (self.raw_delta < 0 < self.common_delta) or (
            self.common_delta < 0 < self.raw_delta
        )


def compare_host_data(
    previous: dict[str, int], latest: dict[str, int]
) -> SurveyComparison:
    """Compare two ``{host: node_data_bytes}`` mappings.

    ``dropped_hosts`` are hosts the previous run measured and this one did not
    — the ones whose absence inflates an apparent reclaim.
    """
    prev_hosts = set(previous)
    latest_hosts = set(latest)
    common = sorted(prev_hosts & latest_hosts)
    return SurveyComparison(
        common_hosts=tuple(common),
        previous_total=sum(previous.values()),
        latest_total=sum(latest.values()),
        previous_common_total=sum(previous[h] for h in common),
        latest_common_total=sum(latest[h] for h in common),
        dropped_hosts=tuple(sorted(prev_hosts - latest_hosts)),
        added_hosts=tuple(sorted(latest_hosts - prev_hosts)),
    )


def host_data_bytes(services: list[dict]) -> dict[str, int]:
    """Sum per-service sizes into ``{host: bytes}`` — the survey's own view of
    the same figure the stored child rows give for a past survey."""
    totals: dict[str, int] = {}
    for svc in services:
        host = svc["host"]
        totals[host] = totals.get(host, 0) + int(svc.get("size_bytes", 0) or 0)
    return totals


@dataclass
class RetryOutcome:
    """What the serial retry pass achieved, for reporting."""

    recovered: list[str] = field(default_factory=list)
    still_failed: list[SurveyFailure] = field(default_factory=list)
    skipped: list[SurveyFailure] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.recovered) + len(self.still_failed)
