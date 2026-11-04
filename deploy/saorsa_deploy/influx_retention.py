"""Reading an InfluxDB bucket's retention, and what it means for a query.

Two callers want it for opposite reasons. ``monitoring set-retention`` reads it
back after a change, because the maintenance run needs a step whose success can
be checked. ``print-downloads`` / ``print-uploads`` read it to answer a
different question: does the requested ``--window`` reach further back than the
bucket can possibly hold?

**Why metadata and not the returned points.** Every query is already
``range(start: <window>)``, so the earliest point it can return is at or after
the window start -- which made ``window_start < oldest_point`` true on almost
every non-empty answer, including complete ones, and left the signal firing
when nothing was missing (INFRA-484 review). Retention is the fact that
actually bounds the history: compare the window against ``now - retention`` and
a complete answer says nothing while a truncated one says so, with no query of
its own and no guess.

It also gets the case item 8 asks for right by construction: a bucket set to
infinite (``monitoring set-retention --days 0``) can never truncate, so the
four permanent deployments stop triggering the signal once they are changed.

A retention that cannot be READ is reported as unknown, never as infinite --
"could not look" is not "nothing there" -- and no caller is allowed to fail
over it, because this is a diagnostic beside the answer rather than the answer.
"""

from __future__ import annotations

import requests

_TIMEOUT = 30


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def find_bucket(url: str, token: str, org: str, name: str) -> dict | None:
    """The named bucket, or None when the org has no such bucket."""
    resp = requests.get(
        f"{url.rstrip('/')}/api/v2/buckets",
        params={"org": org, "name": name},
        headers=_headers(token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    for bucket in resp.json().get("buckets", []):
        if bucket.get("name") == name:
            return bucket
    return None


def retention_seconds(bucket: dict) -> int:
    """The bucket's expiry in seconds, or 0 for infinite.

    An empty ``retentionRules`` list IS infinite in InfluxDB 2.x -- the same
    encoding ``influxdb_setup.py`` writes at create time -- so an absent rule
    is 0 rather than unknown.
    """
    for rule in bucket.get("retentionRules") or []:
        if rule.get("type") == "expire":
            return int(rule.get("everySeconds") or 0)
    return 0


def binding_retention_seconds(url: str, token: str, org: str, names) -> int | None:
    """The retention that bounds a query over ``names``, in seconds.

    ``0`` when every named bucket keeps its data forever, and the SHORTEST
    finite retention otherwise: a query spanning several buckets is truncated
    as soon as any one of them expires its half of the answer. ``None`` when it
    could not be determined, which is not the same as infinite.

    A bucket that does not exist contributes nothing -- a stack provisioned
    before the WASM tier has no WASM bucket, and that is not a short history.
    """
    finite: list[int] = []
    seen_any = False
    for name in names:
        try:
            bucket = find_bucket(url, token, org, name)
        except requests.RequestException:
            return None
        if bucket is None:
            continue
        seen_any = True
        seconds = retention_seconds(bucket)
        if seconds > 0:
            finite.append(seconds)
    if not seen_any:
        return None
    return min(finite) if finite else 0
