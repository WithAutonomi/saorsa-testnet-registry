"""InfluxDB Flux helpers for prod canary measurements.

The canary upload service writes to three measurements:

* ``prod_canary_upload`` — one point per uploaded file (canary metadata)
* ``prod_canary_chunk``  — one point per chunk of an upload (currently the
  data-map address only; data-chunk addresses surface through probe
  failures)
* ``prod_canary_probe``  — one point per probe (initial verification +
  every subsequent probe)

All three are deployment-scoped via the ``deployment_name`` tag. Chunk
addresses and canary IDs live as fields (high cardinality kills Influx as
tags).

Each fetcher returns a list of dicts shaped like the corresponding
SQLAlchemy model so the caller can pass the result straight to the upsert
helpers in :mod:`saorsa_db.repositories`.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

import requests

from saorsa_deploy.influx import _parse_int, _parse_ts

_UPLOAD_FLUX = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "prod_canary_upload")
  |> filter(fn: (r) => r.deployment_name == "{deployment}")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""

_CHUNK_FLUX = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "prod_canary_chunk")
  |> filter(fn: (r) => r.deployment_name == "{deployment}")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""

_PROBE_FLUX = """from(bucket: "{bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "prod_canary_probe")
  |> filter(fn: (r) => r.deployment_name == "{deployment}")
  |> pivot(rowKey: ["_time", "host"], columnKey: ["_field"], valueColumn: "_value")
"""


def _query(
    url: str, token: str, org: str, flux: str, timeout: int = 60
) -> str:
    resp = requests.post(
        f"{url.rstrip('/')}/api/v2/query",
        params={"org": org},
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        data=flux,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _rows(body: str) -> list[dict]:
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


def _ns_to_dt(ns: int | None) -> datetime | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)


def _to_uuid(value: str) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def fetch_canary_upload_points(
    *,
    url: str,
    token: str,
    org: str,
    bucket: str,
    deployment_name: str,
    start: str = "0",
) -> list[dict]:
    """Return one dict per upload row. Keys mirror the Postgres model."""
    body = _query(
        url,
        token,
        org,
        _UPLOAD_FLUX.format(bucket=bucket, start=start, deployment=deployment_name),
    )
    points: list[dict] = []
    for record in _rows(body):
        if record.get("_measurement") != "prod_canary_upload":
            continue
        cid = _to_uuid(record.get("canary_id", ""))
        if cid is None:
            continue
        finished_ns = _parse_int(record.get("finished_at", ""))
        started_ns = _parse_int(record.get("started_at", ""))
        uploaded_at = _ns_to_dt(finished_ns) or _parse_ts(record["_time"])
        started_at = _ns_to_dt(started_ns) or uploaded_at
        points.append({
            "canary_id": cid,
            "host": record.get("host", ""),
            "region": record.get("region", ""),
            "source_url": record.get("source_url", "") or "",
            "file_name": record.get("file_name", "") or "",
            "file_size_bytes": _parse_int(record.get("file_size_bytes", "")) or 0,
            "top_address": record.get("top_address", "") or "",
            "num_chunks": _parse_int(record.get("num_chunks", "")) or 0,
            "uploaded_at": uploaded_at,
            "started_at": started_at,
            "duration_ms": _parse_int(record.get("duration_ms", "")) or 0,
            "initial_verify_status": (
                record.get("initial_verify_status", "") or "failed"
            ),
        })
    return points


def fetch_canary_chunk_points(
    *,
    url: str,
    token: str,
    org: str,
    bucket: str,
    deployment_name: str,
    start: str = "0",
) -> list[dict]:
    body = _query(
        url,
        token,
        org,
        _CHUNK_FLUX.format(bucket=bucket, start=start, deployment=deployment_name),
    )
    points: list[dict] = []
    for record in _rows(body):
        if record.get("_measurement") != "prod_canary_chunk":
            continue
        cid = _to_uuid(record.get("canary_id", ""))
        addr = record.get("chunk_address", "") or ""
        if cid is None or not addr:
            continue
        points.append({
            "canary_id": cid,
            "chunk_address": addr,
            "chunk_role": record.get("chunk_role", "") or "data",
        })
    return points


def fetch_canary_probe_points(
    *,
    url: str,
    token: str,
    org: str,
    bucket: str,
    deployment_name: str,
    start: str = "0",
) -> list[dict]:
    body = _query(
        url,
        token,
        org,
        _PROBE_FLUX.format(bucket=bucket, start=start, deployment=deployment_name),
    )
    points: list[dict] = []
    for record in _rows(body):
        if record.get("_measurement") != "prod_canary_probe":
            continue
        cid = _to_uuid(record.get("canary_id", ""))
        if cid is None:
            continue
        probed_at = _parse_ts(record["_time"])
        points.append({
            "canary_id": cid,
            "probed_at": probed_at,
            "probed_by_host": record.get("host", ""),
            "status": record.get("status", "") or "inconclusive",
            "missing_chunk_address": (
                record.get("missing_chunk_address", "") or ""
            ),
            "terminal_peer_outcomes": (
                record.get("terminal_peer_outcomes", "") or ""
            ),
            "duration_ms": _parse_int(record.get("duration_ms", "")),
        })
    return points
