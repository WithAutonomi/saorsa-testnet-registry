"""Pure-function state machine: apply one ES log event to one NodeUpgrade row.

Rows are mutated in place so the caller can batch-update. All transitions are
idempotent — re-applying the same event is a no-op.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from saorsa_deploy.upgrade_tracker.db import NodeUpgrade

ROLLBACK_PREFIXES = (
    "Download failed:",
    "Signature verification failed:",
    "Binary replacement failed:",
    "Backup creation failed:",
    "Failed to copy from cache",
)
CRITICAL_PREFIX = "CRITICAL: Replacement failed"
START_UPGRADE_PREFIXES = (
    "Starting auto-apply upgrade",
)


def _doc_message(doc: dict) -> Optional[str]:
    tail = doc.get("tail") or {}
    msg = tail.get("message")
    return msg if isinstance(msg, str) else None


def _doc_target(doc: dict) -> Optional[str]:
    tag = doc.get("tag") or {}
    t = tag.get("target")
    return t if isinstance(t, str) else None


def _doc_level(doc: dict) -> Optional[str]:
    tag = doc.get("tag") or {}
    lvl = tag.get("level")
    return lvl if isinstance(lvl, str) else None


def _doc_host(doc: dict) -> Optional[str]:
    tag = doc.get("tag") or {}
    h = tag.get("host")
    return h if isinstance(h, str) else None


def _doc_service_number(doc: dict) -> Optional[str]:
    tag = doc.get("tag") or {}
    s = tag.get("service_number")
    return s if isinstance(s, (str, int)) else None


def _doc_timestamp(doc: dict) -> Optional[str]:
    ts = doc.get("@timestamp")
    return ts if isinstance(ts, str) else None


def _doc_tail_field(doc: dict, key: str) -> Optional[str]:
    tail = doc.get("tail") or {}
    v = tail.get(key)
    if isinstance(v, (str, int, float)):
        return str(v)
    return None


def identify(doc: dict) -> Optional[tuple[str, str]]:
    """Return (host, service_number) if both tags are present."""
    host = _doc_host(doc)
    svc = _doc_service_number(doc)
    if host is None or svc is None:
        return None
    return host, str(svc)


def _add_delay(ts_iso: str, delay_hours: int, delay_minutes: int) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    scheduled = dt + timedelta(hours=delay_hours, minutes=delay_minutes)
    return scheduled.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_rollback_error(msg: str) -> bool:
    return any(msg.startswith(p) for p in ROLLBACK_PREFIXES)


def apply_event(row: NodeUpgrade, doc: dict) -> NodeUpgrade:
    """Return a new NodeUpgrade reflecting this one event. Idempotent."""
    msg = _doc_message(doc)
    ts = _doc_timestamp(doc)
    if msg is None or ts is None:
        return row

    target = _doc_target(doc)
    level = _doc_level(doc)

    new = replace(row, last_synced_at=max(row.last_synced_at, ts))

    # --- startup log: authoritative for running_version / completion ---
    if msg == "ant-node starting" and target == "ant_node":
        observed_version = _doc_tail_field(doc, "version")
        if observed_version:
            new.running_version = observed_version
            if (
                observed_version == new.to_version
                and new.status != "completed"
            ):
                new.status = "completed"
                new.completed_at = ts
        return new

    # --- upgrade lifecycle ---
    if target is None or not target.startswith("ant_node::upgrade"):
        return new

    if msg == "New version available":
        if new.detected_at is None:
            new.detected_at = ts
        return new

    if msg == "New version detected, staged rollout delay calculated":
        if new.scheduled_at is None:
            try:
                dh = int(_doc_tail_field(doc, "delay_hours") or 0)
                dm = int(_doc_tail_field(doc, "delay_minutes") or 0)
                new.scheduled_at = _add_delay(ts, dh, dm)
            except (ValueError, TypeError):
                pass
        if new.status == "pending":
            new.status = "scheduled"
        return new

    if any(msg.startswith(p) for p in START_UPGRADE_PREFIXES):
        if new.started_at is None:
            new.started_at = ts
        if new.status in ("pending", "scheduled"):
            new.status = "in_progress"
        return new

    # --- errors from the upgrade modules ---
    is_warn_or_error = level in ("WARN", "ERROR")
    if is_warn_or_error and (_is_rollback_error(msg) or msg.startswith(CRITICAL_PREFIX)):
        new.error_count += 1
        new.last_error = msg
        new.last_error_at = ts
        # Only rollback-path errors terminally flip the row to rolled_back.
        if _is_rollback_error(msg) and new.status != "completed":
            new.status = "rolled_back"
        return new

    if is_warn_or_error:
        # Track non-terminal warnings (e.g. retry attempts) but keep status.
        new.error_count += 1
        new.last_error = msg
        new.last_error_at = ts
        return new

    return new
