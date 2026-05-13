"""Daily Slack reporter for a prod canary deployment.

Runs on the canary's monitoring VM via a systemd timer. Reads
``prod_canary_upload`` / ``prod_canary_probe`` from the local Influx,
applies the two-strike loss-confirmation rule per probe-host, computes
deltas against ``$STATE_FILE`` (the previous day's snapshot) and posts a
Slack Block Kit message to ``$SLACK_WEBHOOK_URL``.

Only the Python standard library is used — no third-party deps need to be
installed on the monitoring VM.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_INCONCLUSIVE = "inconclusive"

DEFAULT_STATE_FILE = "/var/lib/ant-prod-canary-reporter/last-report.json"
HTTP_TIMEOUT_SECS = 60

# Hours-to-loss histogram buckets — mirrors deploy/saorsa_deploy/cmd/
# prod_canary_results.py so the daily message matches print-recorded-results.
HOUR_BUCKETS = [
    ("0–6h", 0, 6),
    ("6–24h", 6, 24),
    ("1–3d", 24, 72),
    ("3–7d", 72, 168),
    ("7–30d", 168, 720),
    (">30d", 720, float("inf")),
]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


# ---------------------------------------------------------------------------
# Influx (Flux) queries — stdlib only
# ---------------------------------------------------------------------------


_UPLOAD_FLUX = """from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "prod_canary_upload")
  |> filter(fn: (r) => r.deployment_name == "{deployment}")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""

_PROBE_FLUX = """from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "prod_canary_probe")
  |> filter(fn: (r) => r.deployment_name == "{deployment}")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""


def _flux_query(
    *, url: str, token: str, org: str, flux: str
) -> str:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v2/query?org={org}",
        data=flux.encode("utf-8"),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_rows(body: str) -> list[dict]:
    out: list[dict] = []
    reader = csv.reader(io.StringIO(body))
    header: list[str] | None = None
    for row in reader:
        if not row or not any(row):
            header = None
            continue
        if row[0].startswith("#"):
            continue
        if header is None:
            header = row
            continue
        out.append(dict(zip(header, row)))
    return out


def _parse_int(value: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def _parse_ts(value: str) -> datetime | None:
    """Parse RFC3339 from Influx (handles fractional seconds and Z suffix)."""
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        # Strip nanos if Python can't handle them
        if "." in v and "+" in v:
            head, tz = v.rsplit("+", 1)
            secs, _, frac = head.partition(".")
            return datetime.fromisoformat(f"{secs}.{frac[:6]}+{tz}")
        return None


def _ns_to_dt(ns: int | None) -> datetime | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)


def fetch_uploads(
    *, url: str, token: str, org: str, bucket: str, deployment_name: str
) -> dict[str, dict]:
    """Return ``{canary_id: {file_name, source_url, uploaded_at, num_chunks,
    top_address, ...}}``."""
    body = _flux_query(
        url=url, token=token, org=org,
        flux=_UPLOAD_FLUX.format(bucket=bucket, deployment=deployment_name),
    )
    out: dict[str, dict] = {}
    for record in _parse_rows(body):
        if record.get("_measurement") != "prod_canary_upload":
            continue
        cid = record.get("canary_id", "")
        if not cid:
            continue
        finished_ns = _parse_int(record.get("finished_at", ""))
        uploaded_at = _ns_to_dt(finished_ns) or _parse_ts(record.get("_time", ""))
        if uploaded_at is None:
            continue
        out[cid] = {
            "canary_id": cid,
            "file_name": record.get("file_name", "") or "",
            "source_url": record.get("source_url", "") or "",
            "uploaded_at": uploaded_at,
            "num_chunks": _parse_int(record.get("num_chunks", "")) or 0,
            "top_address": record.get("top_address", "") or "",
            "initial_verify_status": (
                record.get("initial_verify_status", "") or "failed"
            ),
            "host": record.get("host", ""),
        }
    return out


def fetch_probes(
    *, url: str, token: str, org: str, bucket: str, deployment_name: str
) -> list[dict]:
    body = _flux_query(
        url=url, token=token, org=org,
        flux=_PROBE_FLUX.format(bucket=bucket, deployment=deployment_name),
    )
    out: list[dict] = []
    for record in _parse_rows(body):
        if record.get("_measurement") != "prod_canary_probe":
            continue
        cid = record.get("canary_id", "")
        if not cid:
            continue
        probed_at = _parse_ts(record.get("_time", ""))
        if probed_at is None:
            continue
        out.append({
            "canary_id": cid,
            "probed_at": probed_at,
            "probed_by_host": record.get("host", ""),
            "status": record.get("status", "") or "inconclusive",
            "missing_chunk_address": (
                record.get("missing_chunk_address", "") or ""
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Two-strike rule (mirrors derive_loss in prod_canary_results.py)
# ---------------------------------------------------------------------------


def derive_state(
    uploads: dict[str, dict], probes: list[dict]
) -> dict[str, dict]:
    """Return ``{canary_id: {status, ...}}`` for every uploaded canary.

    Status is one of: ``alive``, ``pending``, ``lost``. For ``lost`` the
    earliest-confirmation across vantages wins (matching the global
    derivation). For ``pending`` we surface that at least one vantage has
    seen a failure that hasn't yet been confirmed.
    """
    by_canary_host: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in probes:
        by_canary_host[(p["canary_id"], p["probed_by_host"])].append(p)
    for plist in by_canary_host.values():
        plist.sort(key=lambda x: x["probed_at"])

    state: dict[str, dict] = {}
    for cid, upload in uploads.items():
        best_loss: dict | None = None
        any_pending_host: str | None = None
        any_pending_chunk: str | None = None

        for (canary_id, host), plist in by_canary_host.items():
            if canary_id != cid:
                continue
            first_failed_at = None
            first_failed_chunk: str | None = None
            confirmed = None
            for p in plist:
                status = p["status"]
                if status == PROBE_INCONCLUSIVE:
                    continue
                if status == PROBE_OK:
                    first_failed_at = None
                    first_failed_chunk = None
                    continue
                missing = p["missing_chunk_address"] or ""
                if first_failed_chunk is not None and first_failed_chunk == missing:
                    confirmed = {
                        "first_failed_probe_at": first_failed_at,
                        "confirmed_lost_at": p["probed_at"],
                        "missing_chunk_address": missing,
                        "confirmed_by_host": host,
                    }
                    break
                first_failed_at = p["probed_at"]
                first_failed_chunk = missing
            if confirmed and (
                best_loss is None
                or confirmed["confirmed_lost_at"] < best_loss["confirmed_lost_at"]
            ):
                best_loss = confirmed
            elif first_failed_chunk and confirmed is None:
                any_pending_host = host
                any_pending_chunk = first_failed_chunk

        if best_loss is not None:
            uploaded_at = upload["uploaded_at"]
            hours_to_confirmed = max(
                0.0,
                (
                    best_loss["confirmed_lost_at"] - uploaded_at
                ).total_seconds() / 3600.0,
            )
            state[cid] = {
                "status": "lost",
                "uploaded_at": uploaded_at,
                "file_name": upload["file_name"],
                "missing_chunk_address": best_loss["missing_chunk_address"],
                "confirmed_lost_at": best_loss["confirmed_lost_at"],
                "confirmed_by_host": best_loss["confirmed_by_host"],
                "hours_to_confirmed_loss": hours_to_confirmed,
                "is_data_map": (
                    best_loss["missing_chunk_address"] == upload["top_address"]
                ),
            }
        elif any_pending_chunk:
            state[cid] = {
                "status": "pending",
                "uploaded_at": upload["uploaded_at"],
                "file_name": upload["file_name"],
                "pending_chunk_address": any_pending_chunk,
                "pending_on_host": any_pending_host,
            }
        else:
            state[cid] = {
                "status": "alive",
                "uploaded_at": upload["uploaded_at"],
                "file_name": upload["file_name"],
            }
    return state


# ---------------------------------------------------------------------------
# Per-vantage probe staleness
# ---------------------------------------------------------------------------


def last_probe_per_host(probes: list[dict]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for p in probes:
        host = p["probed_by_host"]
        if not host:
            continue
        cur = out.get(host)
        if cur is None or p["probed_at"] > cur:
            out[host] = p["probed_at"]
    return out


def losses_by_host(state: dict[str, dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in state.values():
        if s["status"] == "lost" and s.get("confirmed_by_host"):
            out[s["confirmed_by_host"]] += 1
    return out


# ---------------------------------------------------------------------------
# Slack Block Kit assembly
# ---------------------------------------------------------------------------


def _fmt_age(now: datetime, when: datetime) -> str:
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    d = secs // 86400
    h = (secs % 86400) // 3600
    return f"{d}d {h}h ago" if h else f"{d}d ago"


def _short_chunk(addr: str) -> str:
    if not addr:
        return "?"
    return f"{addr[:8]}…" if len(addr) > 8 else addr


def build_blocks(
    *,
    deployment_name: str,
    now: datetime,
    state: dict[str, dict],
    probes: list[dict],
    prev_state: dict[str, dict],
) -> list[dict]:
    total = len(state)
    alive = sum(1 for s in state.values() if s["status"] == "alive")
    pending = sum(1 for s in state.values() if s["status"] == "pending")
    lost = sum(1 for s in state.values() if s["status"] == "lost")

    prev_lost_ids = {
        cid for cid, s in prev_state.items() if s.get("status") == "lost"
    }
    new_losses = [
        (cid, s) for cid, s in state.items()
        if s["status"] == "lost" and cid not in prev_lost_ids
    ]
    new_losses.sort(key=lambda x: x[1]["confirmed_lost_at"])

    last_probes = last_probe_per_host(probes)
    host_losses = losses_by_host(state)
    all_hosts = sorted(set(last_probes) | set(host_losses))

    blocks: list[dict] = []

    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"📊 {deployment_name} — canary daily report",
        },
    })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"_{now.strftime('%Y-%m-%d %H:%M UTC')}_",
        }],
    })

    # Probe-staleness warning: any host whose last probe is >6h old, or any
    # canary deployment with zero probes in the last 6h.
    stale_hosts = [
        h for h, t in last_probes.items()
        if (now - t).total_seconds() > 6 * 3600
    ]
    no_recent_probes = (
        all((now - t).total_seconds() > 6 * 3600 for t in last_probes.values())
        if last_probes else True
    )
    if not last_probes:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "⚠️ *No probe data yet.* The probe service may not be "
                    "running, or no probes have been emitted."
                ),
            },
        })
    elif no_recent_probes:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "⚠️ *No probes in the last 6h on any vantage.* "
                    "Probe service is likely dead."
                ),
            },
        })
    elif stale_hosts:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *Stale vantage(s):* {', '.join(stale_hosts)} "
                    "have not probed in the last 6h."
                ),
            },
        })

    # Status
    delta_lost = lost - sum(1 for cid in prev_lost_ids if cid in state)
    delta_lost_text = f" (+{delta_lost})" if delta_lost > 0 else ""
    status_lines = [
        f"• Uploaded: *{total}*",
        f"• Alive: *{alive}*",
        f"• Pending first-failure: *{pending}*",
        f"• Confirmed lost: *{lost}*{delta_lost_text}",
    ]
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Status*\n" + "\n".join(status_lines)},
    })

    # Hours-to-loss histogram
    if lost > 0:
        bucket_counts = {label: 0 for label, *_ in HOUR_BUCKETS}
        for s in state.values():
            if s["status"] != "lost":
                continue
            hrs = s["hours_to_confirmed_loss"]
            for label, lo, hi in HOUR_BUCKETS:
                if lo <= hrs < hi:
                    bucket_counts[label] += 1
                    break
        hist_lines = [
            f"• {label}: {bucket_counts[label]}"
            for label, *_ in HOUR_BUCKETS
        ]
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Hours-to-loss (cumulative)*\n" + "\n".join(hist_lines),
            },
        })

    # Per-vantage
    if all_hosts:
        vant_lines = []
        for h in all_hosts:
            last = last_probes.get(h)
            last_txt = _fmt_age(now, last) if last else "never"
            losses_txt = host_losses.get(h, 0)
            vant_lines.append(
                f"• `{h}` — last probe {last_txt} — confirmed losses: {losses_txt}"
            )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Per-vantage*\n" + "\n".join(vant_lines),
            },
        })

    # New losses since last report
    if new_losses:
        cutoff_text = "since last report"
        head = f"*New losses {cutoff_text}* ({len(new_losses)})"
        items = []
        for cid, s in new_losses[:20]:
            role = "DataMap" if s.get("is_data_map") else "data"
            items.append(
                f"• `{s['file_name'] or cid[:8]}` — "
                f"missing {role} `{_short_chunk(s['missing_chunk_address'])}` — "
                f"uploaded {_fmt_age(now, s['uploaded_at'])}, "
                f"confirmed {_fmt_age(now, s['confirmed_lost_at'])}"
            )
        if len(new_losses) > 20:
            items.append(f"_…and {len(new_losses) - 20} more_")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": head + "\n" + "\n".join(items)},
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_No new losses since last report._",
            }],
        })

    return blocks


# ---------------------------------------------------------------------------
# Slack post + state file I/O
# ---------------------------------------------------------------------------


def post_to_slack(webhook_url: str, blocks: list[dict]) -> None:
    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 300:
                print(
                    f"ERROR: Slack returned HTTP {resp.status}: {body}",
                    file=sys.stderr,
                )
                sys.exit(3)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"ERROR: Slack HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(3)


def _to_persistable(state: dict[str, dict]) -> dict[str, Any]:
    """Strip datetimes / non-JSON values for the on-disk snapshot."""
    out: dict[str, Any] = {}
    for cid, s in state.items():
        out[cid] = {"status": s["status"]}
    return out


def read_prev_state(path: Path) -> dict[str, dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        print(f"WARN: cannot read {path}: {e}", file=sys.stderr)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"WARN: corrupt state file {path}: {e}", file=sys.stderr)
        return {}
    return data.get("state", {})


def write_state(path: Path, state: dict[str, dict], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "written_at": now.isoformat(),
        "state": _to_persistable(state),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    influx_url = _require("INFLUX_URL")
    influx_token = _require("INFLUX_TOKEN")
    influx_org = _optional("INFLUX_ORG", "ant")
    influx_bucket = _optional("INFLUX_BUCKET", "testnet-metrics")
    deployment = _require("DEPLOYMENT_NAME")
    webhook = _require("SLACK_WEBHOOK_URL")
    state_path = Path(_optional("STATE_FILE", DEFAULT_STATE_FILE))

    now = datetime.now(tz=timezone.utc)

    try:
        uploads = fetch_uploads(
            url=influx_url, token=influx_token, org=influx_org,
            bucket=influx_bucket, deployment_name=deployment,
        )
        probes = fetch_probes(
            url=influx_url, token=influx_token, org=influx_org,
            bucket=influx_bucket, deployment_name=deployment,
        )
    except urllib.error.URLError as e:
        # Connection failure to Influx — post a degraded message rather than
        # silently dying, so the operator sees something in Slack.
        print(f"ERROR: Influx query failed: {e}", file=sys.stderr)
        post_to_slack(webhook, [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⚠️ {deployment} — reporter degraded",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Influx query failed at {now.isoformat()}: `{e}`. "
                        "Reporter could not compute today's digest."
                    ),
                },
            },
        ])
        return 4

    if not uploads:
        post_to_slack(webhook, [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📊 {deployment} — canary daily report",
                },
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"_{now.strftime('%Y-%m-%d %H:%M UTC')}_",
                }],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "No `prod_canary_upload` points yet. "
                        "Upload service may not have started, or no URLs "
                        "have been processed."
                    ),
                },
            },
        ])
        write_state(state_path, {}, now)
        return 0

    state = derive_state(uploads, probes)
    prev_state = read_prev_state(state_path)
    blocks = build_blocks(
        deployment_name=deployment,
        now=now,
        state=state,
        probes=probes,
        prev_state=prev_state,
    )
    post_to_slack(webhook, blocks)
    write_state(state_path, state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
