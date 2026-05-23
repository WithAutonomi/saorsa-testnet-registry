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
from collections import defaultdict
from datetime import datetime, timezone
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
                best is None
                or confirmed["confirmed_lost_at"] < best["confirmed_lost_at"]
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
) -> None:
    console.print(
        f"[bold]Querying canary measurements from {influx_url} "
        f"(bucket={influx_bucket})...[/bold]"
    )
    try:
        upload_points = fetch_canary_upload_points(
            url=influx_url, token=influx_token, org=influx_org,
            bucket=influx_bucket, deployment_name=args.name,
        )
        chunk_points = fetch_canary_chunk_points(
            url=influx_url, token=influx_token, org=influx_org,
            bucket=influx_bucket, deployment_name=args.name,
        )
        probe_points = fetch_canary_probe_points(
            url=influx_url, token=influx_token, org=influx_org,
            bucket=influx_bucket, deployment_name=args.name,
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] InfluxDB query failed: {e}")
        sys.exit(1)

    if not upload_points:
        console.print(
            f"[yellow]No prod_canary_upload points found for '{args.name}'. "
            "Nothing to record.[/yellow]"
        )
        return

    console.print(
        f"  uploads={len(upload_points)} chunks={len(chunk_points)} "
        f"probes={len(probe_points)}"
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

    upload_count = 0
    for u in upload_points:
        try:
            verify_status = ProdCanaryInitialVerifyStatus(u["initial_verify_status"])
        except ValueError:
            verify_status = ProdCanaryInitialVerifyStatus.FAILED
        repo.upsert_prod_canary_upload(
            canary_id=u["canary_id"],
            deployment_id=deployment.id,
            source_url=u["source_url"],
            file_name=u["file_name"],
            file_size_bytes=u["file_size_bytes"],
            top_address=u["top_address"],
            num_chunks=u["num_chunks"],
            uploaded_at=u["uploaded_at"],
            initial_verify_status=verify_status,
            storage_cost_atto=_to_decimal(u.get("storage_cost_atto")),
            gas_cost_wei=_to_decimal(u.get("gas_cost_wei")),
            chunk_attempts_total=u.get("chunk_attempts_total"),
        )
        upload_count += 1

    chunk_count = 0
    for c in chunk_points:
        try:
            role = ProdCanaryChunkRole(c["chunk_role"])
        except ValueError:
            role = ProdCanaryChunkRole.DATA
        repo.upsert_prod_canary_chunk(
            canary_id=c["canary_id"],
            chunk_address=c["chunk_address"],
            chunk_role=role,
        )
        chunk_count += 1

    probe_count = 0
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
        repo.upsert_prod_canary_probe(
            canary_id=p["canary_id"],
            probed_at=p["probed_at"],
            probed_by_host=p["probed_by_host"],
            status=status,
            missing_chunk_address=p["missing_chunk_address"] or None,
            terminal_peer_outcomes=peer_outcomes,
            duration_ms=p["duration_ms"],
        )
        probe_count += 1

    # Re-derive loss table from Postgres state (authoritative).
    db_uploads = repo.list_prod_canary_uploads_for_deployment(deployment.id)
    db_probes = repo.list_prod_canary_probes_for_deployment(deployment.id)
    uploads_map = {u.canary_id: u.uploaded_at for u in db_uploads}
    probes_by_canary: dict = defaultdict(list)
    for p in db_probes:
        probes_by_canary[p.canary_id].append(p)

    losses = derive_loss(uploads_map, probes_by_canary)
    existing_loss = {
        loss.canary_id: loss
        for loss in repo.list_prod_canary_loss_for_deployment(deployment.id)
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
    # (e.g. an OK probe arrived for a previously-confirmed canary). This keeps
    # the table aligned with the latest probe sequence.
    for cid in existing_loss.keys() - losses.keys():
        repo.delete_prod_canary_loss(cid)

    table = Table(
        title=f"Canary results recorded for '{args.name}' (id={deployment.id})"
    )
    table.add_column("Uploads", justify="right")
    table.add_column("Chunks", justify="right")
    table.add_column("Probes", justify="right")
    table.add_column("Losses (derived)", justify="right")
    table.add_row(
        str(upload_count), str(chunk_count), str(probe_count), str(len(losses))
    )
    console.print(table)


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
    uploads: list[DeploymentProdCanaryUpload] = (
        repo.list_prod_canary_uploads_for_deployment(deployment.id)
    )
    if not uploads:
        console.print(
            f"[yellow]No canary uploads recorded for '{name}'. "
            "Run record-results first.[/yellow]"
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

    summary = Table(
        title=f"Canary status for '{name}' (id={deployment.id})"
    )
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
        total_ant_atto = sum(
            (u.storage_cost_atto or Decimal(0)) for u in cost_rows
        )
        total_gas_wei = sum(
            (u.gas_cost_wei or Decimal(0)) for u in cost_rows
        )
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
            upload = next(
                (u for u in uploads if u.canary_id == loss.canary_id), None
            )
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
        console.print(
            "[dim]No confirmed losses yet — keep probing.[/dim]"
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
