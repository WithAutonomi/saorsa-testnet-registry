"""Persistence and read-only summary for prod-canary results.

Two entry points:

* :func:`record_prod_canary_results` — pull all three Influx measurements
  for a deployment, upsert into Postgres, re-derive ``prod_canary_loss``
  with the global two-strike rule. Idempotent + cron-safe.
* :func:`print_prod_canary_status` — read-only summary from Postgres.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from rich.console import Console
from rich.table import Table
from saorsa_db.models import (
    DeploymentProdCanaryProbe,
    DeploymentProdCanaryUpload,
    ProdCanaryChunkRole,
    ProdCanaryInitialVerifyStatus,
    ProdCanaryProbeStatus,
)

from saorsa_deploy.influx_canary import (
    fetch_canary_chunk_points,
    fetch_canary_probe_points,
    fetch_canary_upload_points,
    flux_instant,
)

PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_INCONCLUSIVE = "inconclusive"


def _is_genuine_absence(peer_outcomes) -> bool:
    """True if a failed fetch reflects genuine chunk absence rather than a
    reachability problem from the probing vantage.

    A genuinely missing chunk shows up as NotFound across the close group;
    a fetch that fails because most peers *timed out* says nothing about
    presence (we never reached enough holders). Require a strict majority of
    the close group to have actively responded NotFound
    (``not_found * 2 > queried``).

    ``peer_outcomes`` is the stored ``terminal_peer_outcomes`` JSON (dict).
    When it's missing or has no ``queried`` count (e.g. recorded by an ant
    binary without the chunk_get summary line), we can't downgrade and fall
    back to treating the failure as genuine — preserving pre-summary
    behaviour.
    """
    if not isinstance(peer_outcomes, dict):
        return True
    queried = peer_outcomes.get("queried")
    if not queried or queried <= 0:
        return True
    not_found = peer_outcomes.get("not_found") or 0
    return not_found * 2 > queried


def _effective_status(probe) -> str:
    """Probe status with timeout-dominated ``failed`` reclassified as
    ``inconclusive``. Mirrors the runtime probe script's reclassification so
    that re-running record-results retroactively corrects records written by
    an older probe script (or before this fix)."""
    status = probe.status.value if hasattr(probe.status, "value") else probe.status
    if status == PROBE_FAILED and not _is_genuine_absence(
        getattr(probe, "terminal_peer_outcomes", None)
    ):
        return PROBE_INCONCLUSIVE
    return status


# ---------------------------------------------------------------------------
# Two-strike global derivation
# ---------------------------------------------------------------------------


def derive_loss(
    uploads: dict,
    probes_by_canary: dict,
) -> dict:
    """Apply the two-strike rule globally.

    ``uploads`` maps ``canary_id -> uploaded_at`` (datetime).
    ``probes_by_canary`` maps ``canary_id -> list[DeploymentProdCanaryProbe]``
    sorted by probed_at.

    Returns ``{canary_id: {first_failed_probe_at, confirmed_lost_at,
    missing_chunk_address, hours_to_first_failure, hours_to_confirmed_loss}}``
    for every canary that is locally-confirmed lost from at least one probe
    VM (per the rule). The earliest-confirmation across vantages wins.
    """
    losses: dict = {}
    for canary_id, probes in probes_by_canary.items():
        if canary_id not in uploads:
            continue
        per_host: dict[str, list[DeploymentProdCanaryProbe]] = defaultdict(list)
        for p in probes:
            per_host[p.probed_by_host].append(p)

        best: dict | None = None
        for host_probes in per_host.values():
            host_probes.sort(key=lambda p: p.probed_at)
            first_failed_at = None
            first_failed_chunk = None
            confirmed = None
            for p in host_probes:
                status = _effective_status(p)
                if status == PROBE_INCONCLUSIVE:
                    continue
                if status == PROBE_OK:
                    first_failed_at = None
                    first_failed_chunk = None
                    continue
                # status == failed
                missing = p.missing_chunk_address or ""
                if first_failed_chunk and first_failed_chunk == missing:
                    confirmed = {
                        "first_failed_probe_at": first_failed_at,
                        "confirmed_lost_at": p.probed_at,
                        "missing_chunk_address": missing,
                    }
                    break
                first_failed_at = p.probed_at
                first_failed_chunk = missing
            if confirmed and (
                best is None or confirmed["confirmed_lost_at"] < best["confirmed_lost_at"]
            ):
                best = confirmed

        if best is None:
            continue
        uploaded_at = uploads[canary_id]
        hours_to_first = max(
            0.0,
            (best["first_failed_probe_at"] - uploaded_at).total_seconds() / 3600.0,
        )
        hours_to_confirmed = max(
            0.0,
            (best["confirmed_lost_at"] - uploaded_at).total_seconds() / 3600.0,
        )
        losses[canary_id] = {
            "first_failed_probe_at": best["first_failed_probe_at"],
            "confirmed_lost_at": best["confirmed_lost_at"],
            "missing_chunk_address": best["missing_chunk_address"],
            "hours_to_first_failure": hours_to_first,
            "hours_to_confirmed_loss": hours_to_confirmed,
        }
    return losses


def derive_unavailability_episodes(uploaded_at, probes: list) -> list[dict]:
    """Walk a single canary's probe stream and return its unavailability
    *episodes* (windows). Each episode is a maximal run during which the chunk
    was confirmed unreachable (two consecutive same-chunk failures) and then
    either recovered (an OK probe) or is still ongoing at the end of the
    stream.

    In practice a canary is probed by exactly one shard host, so a single
    time-ordered walk is correct. Timeout-dominated failures are reclassified
    inconclusive via _effective_status (a reachability artifact, not absence).

    Returns a list of dicts (episode_no starting at 1) ready for
    repo.replace_prod_canary_unavailability.
    """
    ordered = sorted(probes, key=lambda p: p.probed_at)
    episodes: list[dict] = []
    pending_at = None
    pending_chunk = None
    open_ep: dict | None = None  # currently-unavailable episode being built

    def _hours(a, b) -> float:
        return max(0.0, (b - a).total_seconds() / 3600.0)

    for p in ordered:
        status = _effective_status(p)
        if status == PROBE_INCONCLUSIVE:
            continue
        if status == PROBE_OK:
            if open_ep is not None:
                open_ep["recovered_at"] = p.probed_at
                open_ep["recovered"] = True
                open_ep["unavailable_hours"] = _hours(
                    open_ep["confirmed_unavailable_at"], p.probed_at
                )
                episodes.append(open_ep)
                open_ep = None
            pending_at = None
            pending_chunk = None
            continue
        # status == failed
        missing = p.missing_chunk_address or ""
        if open_ep is not None:
            # already in an unavailable episode for (typically) this chunk
            continue
        if pending_chunk is not None and pending_chunk == missing:
            open_ep = {
                "episode_no": len(episodes) + 1,
                "missing_chunk_address": missing,
                "first_failed_probe_at": pending_at,
                "confirmed_unavailable_at": p.probed_at,
                "recovered_at": None,
                "hours_to_confirmed": _hours(uploaded_at, p.probed_at),
                "unavailable_hours": None,
                "recovered": False,
            }
            pending_at = None
            pending_chunk = None
        else:
            pending_at = p.probed_at
            pending_chunk = missing

    if open_ep is not None:
        episodes.append(open_ep)  # ongoing, recovered=False
    return episodes


# ---------------------------------------------------------------------------
# The incremental probe window
# ---------------------------------------------------------------------------

#: How far before the newest recorded probe an incremental pull starts. The
#: margin covers a probe VM whose clock is skewed and a point that arrived
#: late, and re-reading the overlap is free: the upsert makes a re-read of an
#: already-recorded probe a no-op write of the same values.
PROBE_SINCE_MARGIN = timedelta(hours=6)


def _as_utc(value: datetime) -> datetime:
    # Postgres hands back an aware datetime; the SQLite test fixtures do not.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def resolve_probe_start(repo, deployment, args) -> tuple[str, str]:
    """Return the Flux ``range(start:)`` for the probe query, and why.

    A canary deployment's probe series only grows — a probe per canary per
    hour, forever — so re-reading and re-writing all of it every day makes the
    daily run's cost a function of the deployment's age rather than of what
    happened that day. The default therefore asks InfluxDB only for points
    newer than what Postgres already holds, less a margin.

    Raises ``ValueError`` if ``--since`` is not an ISO-8601 instant.
    """
    if getattr(args, "full", False):
        return "0", "--full: the whole probe series"
    raw = (getattr(args, "since", None) or "").strip()
    if raw:
        if raw == "0":
            return "0", "--since 0: the whole probe series"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(
                f"--since '{raw}' is not an ISO-8601 instant "
                "(e.g. 2026-10-11T22:00:00Z) and is not '0'"
            ) from e
        return flux_instant(parsed), f"--since {flux_instant(parsed)}"
    newest = repo.max_prod_canary_probed_at(deployment.id)
    if newest is None:
        # First ever run on this deployment: there is no high-water mark, so
        # anything short of the whole series would silently lose history.
        return "0", "no probes recorded yet: the whole probe series"
    start = _as_utc(newest) - PROBE_SINCE_MARGIN
    return flux_instant(start), (
        f"incremental: 6h before the newest recorded probe ({flux_instant(_as_utc(newest))})"
    )


# ---------------------------------------------------------------------------
# record-results entry point (kind=prod-canary)
# ---------------------------------------------------------------------------


def record_prod_canary_results(
    repo,
    deployment,
    args,
    console: Console,
    influx_url: str,
    influx_token: str,
    influx_org: str,
    influx_bucket: str,
) -> list[dict] | None:
    try:
        probe_start, probe_start_reason = resolve_probe_start(repo, deployment, args)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    full_pull = probe_start == "0"

    console.print(
        f"[bold]Querying canary measurements from {influx_url} (bucket={influx_bucket})...[/bold]"
    )
    console.print(f"  probe window: {probe_start_reason}")
    query_started = time.monotonic()
    try:
        # The upload and chunk queries stay full. They are the canary set
        # itself — 378 rows on PROD-CANARY-04 — written once at upload time
        # and never again, so there is nothing for a window to save.
        upload_points = fetch_canary_upload_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            deployment_name=args.name,
        )
        chunk_points = fetch_canary_chunk_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            deployment_name=args.name,
        )
        probe_points = fetch_canary_probe_points(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            deployment_name=args.name,
            start=probe_start,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)
    query_secs = time.monotonic() - query_started

    if not upload_points:
        console.print(
            f"[yellow]No prod_canary_upload points found for '{args.name}'. "
            "Nothing to record.[/yellow]"
        )
        return None

    console.print(
        f"  uploads={len(upload_points)} chunks={len(chunk_points)} probes={len(probe_points)}"
    )

    def _to_decimal(v):
        # Cost fields are string-encoded uint256 in Influx; convert to Decimal
        # for the Postgres NUMERIC columns. Empty/missing stays NULL.
        if v is None or v == "":
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    write_started = time.monotonic()
    upload_rows = []
    for u in upload_points:
        try:
            verify_status = ProdCanaryInitialVerifyStatus(u["initial_verify_status"])
        except ValueError:
            verify_status = ProdCanaryInitialVerifyStatus.FAILED
        upload_rows.append(
            {
                "canary_id": u["canary_id"],
                "deployment_id": deployment.id,
                "source_url": u["source_url"],
                "file_name": u["file_name"],
                "file_size_bytes": u["file_size_bytes"],
                "top_address": u["top_address"],
                "num_chunks": u["num_chunks"],
                "uploaded_at": u["uploaded_at"],
                "initial_verify_status": verify_status,
                "storage_cost_atto": _to_decimal(u.get("storage_cost_atto")),
                "gas_cost_wei": _to_decimal(u.get("gas_cost_wei")),
                "chunk_attempts_total": u.get("chunk_attempts_total"),
            }
        )
    upload_count = repo.bulk_upsert_prod_canary_uploads(upload_rows)

    chunk_rows = []
    for c in chunk_points:
        try:
            role = ProdCanaryChunkRole(c["chunk_role"])
        except ValueError:
            role = ProdCanaryChunkRole.DATA
        chunk_rows.append(
            {
                "canary_id": c["canary_id"],
                "chunk_address": c["chunk_address"],
                "chunk_role": role,
            }
        )
    chunk_count = repo.bulk_upsert_prod_canary_chunks(chunk_rows)

    probe_rows = []
    for p in probe_points:
        try:
            status = ProdCanaryProbeStatus(p["status"])
        except ValueError:
            status = ProdCanaryProbeStatus.INCONCLUSIVE
        peer_outcomes = None
        if p["terminal_peer_outcomes"]:
            try:
                peer_outcomes = json.loads(p["terminal_peer_outcomes"])
            except (TypeError, ValueError):
                peer_outcomes = None
        probe_rows.append(
            {
                "canary_id": p["canary_id"],
                "probed_at": p["probed_at"],
                "probed_by_host": p["probed_by_host"],
                "status": status,
                "missing_chunk_address": p["missing_chunk_address"] or None,
                "terminal_peer_outcomes": peer_outcomes,
                "duration_ms": p["duration_ms"],
            }
        )
    probe_count = repo.bulk_upsert_prod_canary_probes(probe_rows)
    write_secs = time.monotonic() - write_started

    # Re-derive loss and the episode view from Postgres state (authoritative),
    # NOT from the window that was just pulled: an episode can begin before
    # the window starts and the two-strike rule needs both of its strikes.
    #
    # Scope: every canary this run recorded a probe for. A canary with no new
    # probe has the same history it had last run, so its derived rows are
    # already right and re-writing them would be the whole of the old cost
    # with none of the benefit. A full pull covers every canary, which is what
    # keeps `--full` a true rebuild.
    derive_started = time.monotonic()
    db_uploads = repo.list_prod_canary_uploads_for_deployment(deployment.id)
    uploads_map = {u.canary_id: u.uploaded_at for u in db_uploads}
    if full_pull:
        derive_scope = list(uploads_map)
    else:
        touched = {p["canary_id"] for p in probe_points}
        derive_scope = [cid for cid in uploads_map if cid in touched]

    probes_by_canary: dict = defaultdict(list)
    for probe in repo.list_prod_canary_probe_rows_for_canaries(derive_scope):
        probes_by_canary[probe.canary_id].append(probe)

    losses = derive_loss(uploads_map, probes_by_canary)
    existing_loss = {
        loss.canary_id: loss for loss in repo.list_prod_canary_loss_for_deployment(deployment.id)
    }
    for cid, loss in losses.items():
        repo.upsert_prod_canary_loss(
            canary_id=cid,
            first_failed_probe_at=loss["first_failed_probe_at"],
            confirmed_lost_at=loss["confirmed_lost_at"],
            missing_chunk_address=loss["missing_chunk_address"],
            hours_to_first_failure=loss["hours_to_first_failure"],
            hours_to_confirmed_loss=loss["hours_to_confirmed_loss"],
        )
    # Drop stale loss rows whose probe history no longer satisfies the rule
    # (e.g. an OK probe arrived for a previously-confirmed canary). Restricted
    # to the derivation scope: a canary outside it was not re-derived this
    # run, so the absence of a fresh verdict says nothing about it.
    for cid in (existing_loss.keys() & set(derive_scope)) - losses.keys():
        repo.delete_prod_canary_loss(cid)

    # Re-derive the richer unavailability-episode (window) view: every episode
    # of confirmed unreachability and whether/when it recovered. This is the
    # successor to the binary loss view — it captures the transient-then-
    # recovered behaviour the loss table cannot represent.
    episode_total = 0
    ongoing_total = 0
    for cid in derive_scope:
        episodes = derive_unavailability_episodes(uploads_map[cid], probes_by_canary.get(cid, []))
        repo.replace_prod_canary_unavailability(cid, episodes)
        episode_total += len(episodes)
        ongoing_total += sum(1 for e in episodes if not e["recovered"])
    derive_secs = time.monotonic() - derive_started

    table = Table(title=f"Canary results recorded for '{args.name}' (id={deployment.id})")
    table.add_column("Uploads", justify="right")
    table.add_column("Chunks", justify="right")
    table.add_column("Probes", justify="right")
    table.add_column("Unavail. episodes", justify="right")
    table.add_column("Ongoing now", justify="right")
    table.add_row(
        str(upload_count),
        str(chunk_count),
        str(probe_count),
        str(episode_total),
        str(ongoing_total),
    )
    console.print(table)
    # Printed every run, not behind a flag: this command runs unattended on a
    # daily timer, and the phase split is the only way to see which half of it
    # is growing without reproducing the run by hand.
    console.print(
        f"[dim]Phases: influx query {query_secs:.1f}s, "
        f"writes {write_secs:.1f}s ({probe_count} probe row(s)), "
        f"derivation {derive_secs:.1f}s "
        f"({len(derive_scope)} of {len(uploads_map)} canaries).[/dim]"
    )

    # Build the Slack results summary (alive/pending/confirmed + histogram),
    # mirroring print_prod_canary_status. Returned for an optional threaded
    # reply when record-results was invoked with --update-slack.
    #
    # The counts come from the loss table rather than from this run's derived
    # set, so an incremental run still reports the whole deployment. A canary
    # outside the derivation scope has no probe history loaded, so it counts
    # as alive unless the loss table says otherwise — and a canary that has
    # not been probed in the window has no current status to report anyway.
    loss_rows = repo.list_prod_canary_loss_for_deployment(deployment.id)
    lost_ids = {loss.canary_id for loss in loss_rows}
    alive, pending = _alive_pending_counts(db_uploads, probes_by_canary, lost_ids)
    histogram = _hours_to_loss_histogram(
        [{"hours_to_confirmed_loss": loss.hours_to_confirmed_loss} for loss in loss_rows]
    )
    from saorsa_deploy.slack_results import canary_result_blocks

    return canary_result_blocks(
        {
            "upload_count": upload_count,
            "chunk_count": chunk_count,
            "probe_count": probe_count,
            "total": len(db_uploads),
            "alive": alive,
            "pending": pending,
            "confirmed_lost": len(lost_ids),
            "histogram": histogram,
        }
    )


def _alive_pending_counts(uploads, probes_by_canary, lost_ids) -> tuple[int, int]:
    """Count canaries that are alive vs pending-first-failure (excluding those
    already confirmed lost), per the two-strike pending heuristic used by
    ``print_prod_canary_status``."""
    alive = 0
    pending = 0
    for u in uploads:
        if u.canary_id in lost_ids:
            continue
        host_probes: dict = defaultdict(list)
        for p in probes_by_canary.get(u.canary_id, []):
            host_probes[p.probed_by_host].append(p)
        any_pending = False
        for probes in host_probes.values():
            probes.sort(key=lambda p: p.probed_at)
            last_failed_chunk: str | None = None
            for probe in probes:
                status = _effective_status(probe)
                if status == PROBE_OK:
                    last_failed_chunk = None
                elif status == PROBE_FAILED:
                    last_failed_chunk = probe.missing_chunk_address or ""
            if last_failed_chunk:
                any_pending = True
        if any_pending:
            pending += 1
        else:
            alive += 1
    return alive, pending


def _hours_to_loss_histogram(losses) -> list[tuple[str, int]]:
    counts: dict[str, int] = {label: 0 for label, *_ in _BUCKETS}
    for loss in losses:
        hrs = loss["hours_to_confirmed_loss"]
        for label, lo, hi in _BUCKETS:
            if lo <= hrs < hi:
                counts[label] += 1
                break
    return [(label, counts[label]) for label, *_ in _BUCKETS]


# ---------------------------------------------------------------------------
# print-recorded-results entry point (kind=prod-canary)
# ---------------------------------------------------------------------------


_BUCKETS = [
    ("0–6h", 0, 6),
    ("6–24h", 6, 24),
    ("1–3d", 24, 72),
    ("3–7d", 72, 168),
    ("7–30d", 168, 720),
    (">30d", 720, float("inf")),
]


def print_prod_canary_status(repo, deployment, args, console: Console) -> None:  # noqa: ARG001
    name = deployment.name
    uploads: list[DeploymentProdCanaryUpload] = repo.list_prod_canary_uploads_for_deployment(
        deployment.id
    )
    if not uploads:
        console.print(
            f"[yellow]No canary uploads recorded for '{name}'. Run record-results first.[/yellow]"
        )
        return

    losses = repo.list_prod_canary_loss_for_deployment(deployment.id)
    losses_by_id = {loss.canary_id: loss for loss in losses}

    probes_by_canary: dict = defaultdict(list)
    for p in repo.list_prod_canary_probes_for_deployment(deployment.id):
        probes_by_canary[p.canary_id].append(p)

    pending_first_failure = 0
    alive = 0
    confirmed_lost = len(losses)
    for u in uploads:
        if u.canary_id in losses_by_id:
            continue
        host_probes: dict[str, list] = defaultdict(list)
        for p in probes_by_canary.get(u.canary_id, []):
            host_probes[p.probed_by_host].append(p)
        any_pending = False
        for host_probes_list in host_probes.values():
            host_probes_list.sort(key=lambda p: p.probed_at)
            last_failed_chunk: str | None = None
            for probe in host_probes_list:
                status = _effective_status(probe)
                if status == PROBE_OK:
                    last_failed_chunk = None
                elif status == PROBE_FAILED:
                    last_failed_chunk = probe.missing_chunk_address or ""
                # inconclusive: skip
            if last_failed_chunk:
                any_pending = True
        if any_pending:
            pending_first_failure += 1
        else:
            alive += 1

    summary = Table(title=f"Canary status for '{name}' (id={deployment.id})")
    summary.add_column("Total uploads", justify="right")
    summary.add_column("Alive", justify="right")
    summary.add_column("Pending failure", justify="right")
    summary.add_column("Confirmed lost", justify="right")
    summary.add_row(
        str(len(uploads)),
        str(alive),
        str(pending_first_failure),
        str(confirmed_lost),
    )
    console.print(summary)

    # Cost telemetry. NULL for canaries uploaded before cost recording was
    # wired up (e.g. all of PROD-CANARY-01) — show that fact rather than
    # silently aggregating zero.
    cost_rows = [u for u in uploads if u.storage_cost_atto is not None]
    if cost_rows:
        total_ant_atto = sum((u.storage_cost_atto or Decimal(0)) for u in cost_rows)
        total_gas_wei = sum((u.gas_cost_wei or Decimal(0)) for u in cost_rows)
        # atto = 10^-18; mainnet ANT and ETH both use 18 decimals.
        ant_total = total_ant_atto / Decimal(10) ** 18
        eth_total = total_gas_wei / Decimal(10) ** 18
        cost_table = Table(title="Upload cost totals")
        cost_table.add_column("Rows with cost recorded", justify="right")
        cost_table.add_column("ANT spent", justify="right")
        cost_table.add_column("Gas spent (ETH)", justify="right")
        cost_table.add_row(
            f"{len(cost_rows)}/{len(uploads)}",
            f"{ant_total:.8f}",
            f"{eth_total:.10f}",
        )
        console.print(cost_table)
    else:
        console.print(
            "[dim]No upload cost data recorded for these uploads "
            "(rows pre-date the cost-recording change).[/dim]"
        )

    if losses:
        bucket_counts: dict[str, int] = {label: 0 for label, *_ in _BUCKETS}
        for loss in losses:
            hrs = loss.hours_to_confirmed_loss
            for label, lo, hi in _BUCKETS:
                if lo <= hrs < hi:
                    bucket_counts[label] += 1
                    break
        histogram = Table(title="Hours-to-confirmed-loss histogram")
        histogram.add_column("Bucket")
        histogram.add_column("Count", justify="right")
        for label, *_ in _BUCKETS:
            histogram.add_row(label, str(bucket_counts[label]))
        console.print(histogram)

        # Vantage breakdown — count confirmed losses by the host that
        # confirmed them. We re-derive per-host because losses_by_id stored
        # the global earliest-confirmation; we want a per-vantage view here.
        per_host: dict[str, int] = defaultdict(int)
        for u in uploads:
            if u.canary_id not in losses_by_id:
                continue
            host_probes_map: dict[str, list] = defaultdict(list)
            for p in probes_by_canary.get(u.canary_id, []):
                host_probes_map[p.probed_by_host].append(p)
            for host, host_probes_list in host_probes_map.items():
                host_probes_list.sort(key=lambda p: p.probed_at)
                last_chunk: str | None = None
                lost_here = False
                for probe in host_probes_list:
                    status = _effective_status(probe)
                    if status == PROBE_INCONCLUSIVE:
                        continue
                    if status == PROBE_OK:
                        last_chunk = None
                        continue
                    missing = probe.missing_chunk_address or ""
                    if last_chunk and last_chunk == missing:
                        lost_here = True
                        break
                    last_chunk = missing
                if lost_here:
                    per_host[host] += 1
        if per_host:
            vantages = Table(title="Confirmed losses per probe vantage")
            vantages.add_column("Probe host")
            vantages.add_column("Confirmed losses", justify="right")
            for host in sorted(per_host):
                vantages.add_row(host, str(per_host[host]))
            console.print(vantages)

        # Per-chunk-role breakdown.
        role_counts: dict[str, int] = defaultdict(int)
        for loss in losses:
            # Look up the role of the missing chunk; default to 'data' if
            # we don't have a chunk row recorded (older runs may only have
            # the data_map chunk recorded).
            addr = loss.missing_chunk_address
            upload = next((u for u in uploads if u.canary_id == loss.canary_id), None)
            if upload and addr == upload.top_address:
                role_counts["data_map"] += 1
            else:
                role_counts["data"] += 1
        if role_counts:
            roles_table = Table(title="Confirmed losses by chunk role")
            roles_table.add_column("Role")
            roles_table.add_column("Count", justify="right")
            for role in sorted(role_counts):
                roles_table.add_row(role, str(role_counts[role]))
            console.print(roles_table)
    else:
        console.print("[dim]No confirmed losses yet — keep probing.[/dim]")

    # Unavailability windows — the richer view: chunks go unreachable then
    # mostly recover, so report episode counts + how long they stayed down,
    # plus the genuinely-never-recovered tail.
    episodes = repo.list_prod_canary_unavailability_for_deployment(deployment.id)
    if episodes:
        recovered = [e for e in episodes if e.recovered]
        ongoing = [e for e in episodes if not e.recovered]
        now = datetime.now(timezone.utc)

        ep_summary = Table(title="Unavailability episodes")
        ep_summary.add_column("Total episodes", justify="right")
        ep_summary.add_column("Recovered", justify="right")
        ep_summary.add_column("Ongoing", justify="right")
        ep_summary.add_column("Canaries affected", justify="right")
        ep_summary.add_row(
            str(len(episodes)),
            str(len(recovered)),
            str(len(ongoing)),
            str(len({e.canary_id for e in episodes})),
        )
        console.print(ep_summary)

        if recovered:
            durs = sorted(e.unavailable_hours for e in recovered if e.unavailable_hours is not None)
            if durs:
                n = len(durs)
                median = durs[n // 2]
                p95 = durs[min(n - 1, int(round(0.95 * (n - 1))))]
                console.print(
                    f"[dim]Recovered-episode unavailability: "
                    f"median {median:.1f}h, p95 {p95:.1f}h, "
                    f"max {durs[-1]:.1f}h (n={n}).[/dim]"
                )
            dur_buckets = [
                ("<6h", 0, 6),
                ("6-24h", 6, 24),
                ("1-3d", 24, 72),
                ("3-7d", 72, 168),
                (">7d", 168, float("inf")),
            ]
            counts = {lbl: 0 for lbl, *_ in dur_buckets}
            for e in recovered:
                h = e.unavailable_hours or 0.0
                for lbl, lo, hi in dur_buckets:
                    if lo <= h < hi:
                        counts[lbl] += 1
                        break
            hist = Table(title="Recovered unavailability-duration histogram")
            hist.add_column("Window")
            hist.add_column("Count", justify="right")
            for lbl, *_ in dur_buckets:
                hist.add_row(lbl, str(counts[lbl]))
            console.print(hist)

        if ongoing:
            # The genuinely-concerning tail: still unreachable, and for how
            # long. A long-ongoing episode is the closest thing to real loss.
            ot = Table(title="Currently-unavailable (ongoing) episodes")
            ot.add_column("Chunk")
            ot.add_column("Unavailable for", justify="right")
            for e in sorted(ongoing, key=lambda x: x.confirmed_unavailable_at):
                down_h = (now - e.confirmed_unavailable_at).total_seconds() / 3600.0
                ot.add_row(
                    (e.missing_chunk_address or "?")[:12] + "…",
                    f"{down_h:.1f}h",
                )
            console.print(ot)
    else:
        console.print(
            "[dim]No unavailability episodes recorded — nothing has gone unreachable.[/dim]"
        )

    # Quick-look: when did the first / last upload land?
    first_up = min(u.uploaded_at for u in uploads)
    last_up = max(u.uploaded_at for u in uploads)
    now = datetime.now(timezone.utc)
    age_hrs = (now - first_up).total_seconds() / 3600.0
    console.print(
        f"[dim]First upload at {first_up.isoformat()} "
        f"(deployment age {age_hrs:.1f}h). Last upload at {last_up.isoformat()}.[/dim]"
    )
