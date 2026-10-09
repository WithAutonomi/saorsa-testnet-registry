"""CLI entry point for ``prod canary export``.

Turns a prod-canary deployment's recorded probe history into a small,
public-safe JSON dataset and publishes it to S3, where the
``canary.autonomi.com`` site reads it. The schema here is the **contract**
between this repository and that site, so it is versioned
(``schema_version``) and the key set is closed: the exported documents
contain exactly the keys the schema names and nothing else.

Three rules the implementation is shaped by:

* **Public safety is structural, not a filter.** Nothing is copied wholesale
  out of a row. Every exported value is named explicitly below, so a column
  added to ``prod_canary_probes`` cannot leak by default. In particular
  ``probed_by_host`` — the VM hostname, which carries the deployment name and
  the role — is reduced to a region code with a human label and never
  exported as itself.
* **Postgres only.** No Influx, no S3 deployment state, no SSH. The dataset is
  derived entirely from what ``record-results`` already persisted, so it can
  be published from anywhere with a database URL, and a destroyed canary stays
  exportable as history.
* **Deterministic.** Given the same rows and the same ``--as-of`` the bytes
  are identical, which is what makes the publisher timer's output diffable and
  the tests meaningful. The only clock read is ``--as-of``'s default.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from rich.console import Console

from saorsa_deploy.cmd.prod_canary_results import (
    PROBE_FAILED,
    PROBE_INCONCLUSIVE,
    PROBE_OK,
    _effective_status,
)
from saorsa_deploy.registry import get_repository

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CANARY_KIND = "prod-canary"

#: The probe timer fires hourly on every probe VM. Exported so the site can
#: say "checked every hour" without hard-coding it on its side.
PROBE_INTERVAL_HOURS = 1

#: A probe host is ``{deployment}-ant-client-{role}-{region}-{index}``. Only
#: the ``download`` role probes on a schedule; the upload VM's single
#: post-upload verification comes from the ``upload`` role and is deliberately
#: not a vantage — it measures the upload, not availability over time.
_VANTAGE_RE = re.compile(r"-download-(?P<region>[a-z]+[0-9]+)-[0-9]+$")

#: Region code -> display label. A fixed table rather than a provider lookup:
#: the output must be renderable with no credentials, and these are the only
#: regions a canary has ever probed from. An unknown code is shown as itself.
VANTAGE_LABELS = {
    "lon1": "London",
    "nyc1": "New York",
    "nyc3": "New York",
    "sgp1": "Singapore",
    "ams3": "Amsterdam",
    "fra1": "Frankfurt",
    "sfo3": "San Francisco",
    "tor1": "Toronto",
    "blr1": "Bangalore",
    "syd1": "Sydney",
}

CELL_AVAILABLE = "G"
CELL_UNAVAILABLE = "R"
CELL_NO_DATA = "X"

STALE_NO_RECENT_PROBES = "no_recent_probes"

CACHE_CONTROL = "public, max-age=300"
CONTENT_TYPE = "application/json"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    """Interpret a naive timestamp as UTC; convert an aware one."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """UTC ISO-8601 with a ``Z`` suffix and no fractional seconds."""
    if value is None:
        return None
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%d")


def vantage_of(host: str | None) -> str | None:
    """Region code of a probing host, or None for a non-vantage host."""
    if not host:
        return None
    match = _VANTAGE_RE.search(host)
    return match.group("region") if match else None


def vantage_label(region: str) -> str:
    return VANTAGE_LABELS.get(region, region)


def serialise(document: dict) -> str:
    """The exact on-the-wire form: sorted keys, no whitespace, one newline."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Derivation (pure)
# ---------------------------------------------------------------------------


def _blank_counts() -> dict:
    return {"ok": 0, "failed": 0, "inconclusive": 0, "rate": None}


def _finalise_counts(counts: dict) -> dict:
    """Fill in the rate. ``inconclusive`` never enters the denominator: a
    probe we could not conclude from says nothing about availability, and
    counting it would depress the rate on the probing vantage's bad day."""
    conclusive = counts["ok"] + counts["failed"]
    counts["rate"] = round(counts["ok"] / conclusive, 6) if conclusive else None
    return counts


def _tally(counts: dict, status: str) -> None:
    if status == PROBE_OK:
        counts["ok"] += 1
    elif status == PROBE_FAILED:
        counts["failed"] += 1
    else:
        counts["inconclusive"] += 1


def _days_between(first: date, last: date) -> list[str]:
    if last < first:
        return []
    out = []
    cursor = first
    while cursor <= last:
        out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out


def _episode_span(episode, data_to: datetime) -> tuple[datetime, datetime, bool]:
    """An episode's interval, plus whether its end instant is *inside* it.

    The two endings are not the same kind of thing, and the difference lands
    on a day boundary:

    * A **recovered** episode ends at `recovered_at`, which is the instant
      availability came back — the successful probe itself. So the interval is
      half-open, `[confirmed, recovered)`: a recovery at exactly midnight
      belongs to the previous day, and colouring the new day red would
      contradict the probe that proved the file available.
    * An **ongoing** episode has no recovery. `data_to` is only the end of what
      we know, not evidence of anything, so the interval is closed,
      `[confirmed, data_to]`, and the last day stays red.
    """
    start = _utc(episode.confirmed_unavailable_at)
    recovered_at = getattr(episode, "recovered_at", None)
    if recovered_at:
        return start, _utc(recovered_at), False
    return start, data_to, True


def _is_well_formed(episode) -> bool:
    """A recovery before the confirmation it recovers from is not a span.

    ``record-results`` cannot derive such a row today — the episodes come out
    of one ordered pass over the probe stream — but a future change that could
    must not produce a published document reading "available" from an interval
    that silently matches no day. Dropped loudly instead, so it is a warning in
    the publisher's log rather than a wrong green cell on the site.
    """
    recovered_at = getattr(episode, "recovered_at", None)
    if recovered_at is None:
        return True
    return _utc(recovered_at) >= _utc(episode.confirmed_unavailable_at)


def build_series(
    uploads: list,
    probes: list,
    episodes: list,
    as_of: datetime,
    series_id: str,
    title: str,
    stale_after_hours: float,
) -> dict:
    """Derive the whole series document from rows alone.

    ``uploads``/``probes``/``episodes`` are the three ``prod_canary_*`` row
    sets for one deployment, in any order. Nothing here touches the clock,
    the database or the network.
    """
    as_of = _utc(as_of)

    vantage_probes: list[tuple[str, object, str]] = []
    initial_verification_ok = 0
    for probe in probes:
        region = vantage_of(probe.probed_by_host)
        status = _effective_status(probe)
        if region is None:
            # The upload VM's own post-upload read-back. Excluded from every
            # rate and every cell so the availability record is the probe
            # fleet's alone; kept as a count because "it was there at upload"
            # is worth stating.
            if status == PROBE_OK:
                initial_verification_ok += 1
            continue
        vantage_probes.append((region, probe, status))

    uploaded_ats = [_utc(u.uploaded_at) for u in uploads]
    probed_ats = [_utc(p.probed_at) for _, p, _ in vantage_probes]
    if probed_ats:
        data_to = max(probed_ats)
    elif uploaded_ats:
        # Nothing has probed yet: the record ends at the last upload. The
        # staleness check below then reports it, which is the honest answer.
        data_to = max(uploaded_ats)
    else:
        # No rows at all. `as_of` is the only instant there is, and on its own
        # it would make the document claim it was current as of now — see the
        # staleness check at the end, which refuses to call a series with no
        # probe fresh.
        data_to = as_of

    days = _days_between(min(uploaded_ats).date(), data_to.date()) if uploaded_ats else []
    # Boundaries once, not once per file per day: a canary runs for years and
    # the grid is files x days.
    day_bounds = [
        (
            day,
            datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc),
            datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1),
        )
        for day in days
    ]
    day_set = set(days)

    episodes_by_canary: dict = defaultdict(list)
    for episode in episodes:
        if not _is_well_formed(episode):
            # Dropped from the spans AND from every count, so the document
            # cannot disagree with itself about how many episodes a file had.
            logger.warning(
                "Skipping malformed unavailability episode for canary %s: "
                "recovered_at %s precedes confirmed_unavailable_at %s",
                episode.canary_id,
                _iso(episode.recovered_at),
                _iso(episode.confirmed_unavailable_at),
            )
            continue
        episodes_by_canary[episode.canary_id].append(episode)
    for canary_episodes in episodes_by_canary.values():
        canary_episodes.sort(key=lambda e: _utc(e.confirmed_unavailable_at))

    # Per-canary, per-day probe tallies, built in one pass over the probes so
    # the cost is linear in probes rather than files x days.
    per_canary_day: dict = defaultdict(lambda: defaultdict(lambda: {"conclusive": 0, "failed": 0}))
    rates_all = _blank_counts()
    rates_30d = _blank_counts()
    rates_90d = _blank_counts()
    by_vantage: dict = {}
    window_30 = as_of - timedelta(days=30)
    window_90 = as_of - timedelta(days=90)
    for region, probe, status in vantage_probes:
        probed_at = _utc(probe.probed_at)
        bucket = by_vantage.setdefault(
            region,
            {"all": _blank_counts(), "30d": _blank_counts(), "90d": _blank_counts()},
        )
        _tally(rates_all, status)
        _tally(bucket["all"], status)
        if window_30 < probed_at <= as_of:
            _tally(rates_30d, status)
            _tally(bucket["30d"], status)
        if window_90 < probed_at <= as_of:
            _tally(rates_90d, status)
            _tally(bucket["90d"], status)
        if status != PROBE_INCONCLUSIVE:
            day = _day(probed_at)
            if day in day_set:
                tally = per_canary_day[probe.canary_id][day]
                tally["conclusive"] += 1
                if status == PROBE_FAILED:
                    tally["failed"] += 1

    files = []
    files_unavailable_now = 0
    files_never_unavailable = 0
    episodes_total = 0
    episodes_recovered = 0
    episodes_ongoing = 0
    for upload in sorted(uploads, key=lambda u: (_utc(u.uploaded_at), str(u.canary_id))):
        canary_episodes = episodes_by_canary.get(upload.canary_id, [])
        if not canary_episodes:
            files_never_unavailable += 1
        ongoing = any(getattr(e, "recovered_at", None) is None for e in canary_episodes)
        if ongoing:
            files_unavailable_now += 1
        episodes_total += len(canary_episodes)
        for episode in canary_episodes:
            if getattr(episode, "recovered_at", None) is None:
                episodes_ongoing += 1
            else:
                episodes_recovered += 1

        spans = [_episode_span(e, data_to) for e in canary_episodes]
        day_tallies = per_canary_day.get(upload.canary_id, {})
        cells = []
        failed_checks: dict[str, int] = {}
        for day, day_start, day_end in day_bounds:
            # A day is `[day_start, day_end)`, and the episode is half-open at
            # its own end unless it is ongoing — see `_episode_span`.
            if any(
                start < day_end and (end > day_start or (closed and end >= day_start))
                for start, end, closed in spans
            ):
                cells.append(CELL_UNAVAILABLE)
                continue
            tally = day_tallies.get(day)
            if tally and tally["conclusive"]:
                cells.append(CELL_AVAILABLE)
                if tally["failed"]:
                    failed_checks[day] = tally["failed"]
            else:
                cells.append(CELL_NO_DATA)

        entry = {
            "id": str(upload.canary_id),
            "name": unquote(upload.file_name),
            "bytes": upload.file_size_bytes,
            "chunks": upload.num_chunks,
            "address": upload.top_address,
            "source_url": upload.source_url,
            "uploaded_at": _iso(upload.uploaded_at),
            "available_now": not ongoing,
            "cells": "".join(cells),
        }
        if failed_checks:
            entry["failed_checks"] = failed_checks
        if canary_episodes:
            entry["episodes"] = [
                {
                    "from": _iso(e.confirmed_unavailable_at),
                    "to": _iso(getattr(e, "recovered_at", None)),
                    "chunk": e.missing_chunk_address,
                }
                for e in canary_episodes
            ]
        files.append(entry)

    sizes = [u.file_size_bytes for u in uploads]
    summary = {
        "files": len(uploads),
        "bytes": sum(sizes),
        "chunks": sum(u.num_chunks for u in uploads),
        "initial_verification_ok": initial_verification_ok,
        "days_observed": len(days),
        "files_never_unavailable": files_never_unavailable,
        "files_available_now": len(uploads) - files_unavailable_now,
        "files_unavailable_now": files_unavailable_now,
        "episodes": {
            "total": episodes_total,
            "recovered": episodes_recovered,
            "ongoing": episodes_ongoing,
        },
        "smallest_bytes": min(sizes) if sizes else None,
        "largest_bytes": max(sizes) if sizes else None,
    }

    # A series with no probe at all is never fresh, whatever `data_to` says.
    # Without this a canary exported before `record-results` has ever run —
    # no uploads, no probes, so `data_to` falls back to `as_of` itself — would
    # publish as a healthy, empty, up-to-the-minute series, which is the one
    # reading the site must never show. One reason value, not a second: the
    # site already handles this one, and "there are no recent probes" is
    # literally true when there have never been any.
    stale_reasons = []
    if not probed_ats or data_to < as_of - timedelta(hours=stale_after_hours):
        stale_reasons.append(STALE_NO_RECENT_PROBES)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(as_of),
        "series": {
            "id": series_id,
            "kind": CANARY_KIND,
            "title": title,
            "uploaded_from": _day(min(uploaded_ats)) if uploaded_ats else None,
            "uploaded_to": _day(max(uploaded_ats)) if uploaded_ats else None,
            "first_probe_at": _iso(min(probed_ats)) if probed_ats else None,
            "data_to": _iso(data_to),
            "probe_interval_hours": PROBE_INTERVAL_HOURS,
            "vantages": [
                {"id": region, "label": vantage_label(region)} for region in sorted(by_vantage)
            ],
        },
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "summary": summary,
        "rates": {
            "all": _finalise_counts(rates_all),
            "30d": _finalise_counts(rates_30d),
            "90d": _finalise_counts(rates_90d),
            "by_vantage": {
                region: {window: _finalise_counts(counts) for window, counts in buckets.items()}
                for region, buckets in sorted(by_vantage.items())
            },
        },
        "days": days,
        "files": files,
    }


def index_entry(series_document: dict, path: str) -> dict:
    """The index's per-series row: enough for the site's landing page to list
    every series and say how fresh each is, without fetching them all."""
    series = series_document["series"]
    return {
        "id": series["id"],
        "kind": series["kind"],
        "title": series["title"],
        "path": path,
        "generated_at": series_document["generated_at"],
        "data_to": series["data_to"],
        "stale": series_document["stale"],
        "summary": series_document["summary"],
    }


def merge_index(existing: dict | None, entry: dict, generated_at: str) -> dict:
    """Replace or add this series' entry and keep every other one.

    The index is shared by every series published to the bucket, and each is
    exported by its own command run, so a whole-file rewrite would delete the
    others every time.
    """
    kept = []
    if existing:
        for other in existing.get("series") or []:
            if isinstance(other, dict) and other.get("id") != entry["id"]:
                kept.append(other)
    kept.append(entry)
    kept.sort(key=lambda e: str(e.get("id")))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "series": kept,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


class _DirectoryTarget:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def describe(self, name: str) -> str:
        return str(self.out_dir / name)

    def read_json(self, name: str) -> dict | None:
        path = self.out_dir / name
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def write(self, name: str, body: str) -> int:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / name
        encoded = body.encode("utf-8")
        path.write_bytes(encoded)
        return len(encoded)


class _BucketTarget:
    def __init__(self, client, bucket: str, prefix: str):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def describe(self, name: str) -> str:
        return f"s3://{self.bucket}/{self._key(name)}"

    def read_json(self, name: str) -> dict | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(name))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                return None
            raise
        return json.loads(response["Body"].read())

    def write(self, name: str, body: str) -> int:
        encoded = body.encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(name),
            Body=encoded,
            ContentType=CONTENT_TYPE,
            CacheControl=CACHE_CONTROL,
        )
        return len(encoded)


def _parse_as_of(value: str | None, console: Console) -> datetime:
    if not value:
        return datetime.now(tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        console.print(
            f"[bold red]Error:[/bold red] --as-of '{value}' is not an ISO-8601 timestamp."
        )
        sys.exit(1)
    return _utc(parsed)


def _resolve_deployment(repo, args, console: Console):
    if args.id is not None:
        deployment = repo.get(args.id)
        if deployment is None:
            console.print(
                f"[bold red]Error:[/bold red] No deployment with id={args.id} in the registry."
            )
            sys.exit(1)
        return deployment

    rows = repo.list_by_name(args.name)
    if not rows:
        console.print(
            f"[bold red]Error:[/bold red] No deployment named '{args.name}' in the registry."
        )
        sys.exit(1)
    if len(rows) > 1:
        ids = ", ".join(str(r.id) for r in rows)
        console.print(
            f"[yellow]Warning:[/yellow] {len(rows)} deployments are named "
            f"'{args.name}' (ids: {ids}). Exporting the newest (id={rows[0].id}). "
            "Pass --id to choose another."
        )
    return rows[0]


def cmd_prod_canary_export(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database is unreachable."
        )
        sys.exit(1)

    as_of = _parse_as_of(args.as_of, console)
    deployment = _resolve_deployment(repo, args, console)

    kind = (
        (deployment.deploy_args or {}).get("kind")
        if isinstance(deployment.deploy_args, dict)
        else None
    )
    if kind != CANARY_KIND:
        console.print(
            f"[bold red]Error:[/bold red] Deployment '{deployment.name}' "
            f"(id={deployment.id}) has kind '{kind or 'unknown'}', not "
            f"'{CANARY_KIND}'. Only canary deployments have a probe history to export."
        )
        sys.exit(1)

    series_id = deployment.name.lower()
    document = build_series(
        uploads=repo.list_prod_canary_uploads_for_deployment(deployment.id),
        probes=repo.list_prod_canary_probes_for_deployment(deployment.id),
        episodes=repo.list_prod_canary_unavailability_for_deployment(deployment.id),
        as_of=as_of,
        series_id=series_id,
        title=args.title or deployment.name,
        stale_after_hours=args.stale_after_hours,
    )

    series_name = f"{series_id}.json"
    if args.out_dir:
        target = _DirectoryTarget(Path(args.out_dir).expanduser())
    else:
        target = _BucketTarget(boto3.client("s3"), args.bucket, args.prefix)

    try:
        existing_index = target.read_json("index.json")
    except json.JSONDecodeError:
        console.print(
            f"[bold red]Error:[/bold red] {target.describe('index.json')} is not "
            "valid JSON. Refusing to overwrite it — every other series' entry "
            "lives in that file."
        )
        sys.exit(1)

    index = merge_index(
        existing_index,
        index_entry(document, series_name),
        document["generated_at"],
    )

    # Plain `print`, not the console: rich wraps at 80 columns when stdout is
    # a pipe, which splits a long bucket key or `--out-dir` path across two
    # lines and breaks the one-line-per-file-written contract for whatever is
    # reading the publisher timer's journal.
    series_bytes = target.write(series_name, serialise(document))
    print(f"{target.describe(series_name)}  {series_bytes} bytes")
    index_bytes = target.write("index.json", serialise(index))
    print(f"{target.describe('index.json')}  {index_bytes} bytes")
