"""Hetzner Cloud availability preflight.

Hetzner frequently runs out of capacity for specific (region, server_type)
combinations. This module queries the Hetzner Cloud API to check which
regions currently have capacity for a requested server type, so we can
substitute fallback regions before invoking Terraform.

Reference: https://docs.hetzner.cloud/#datacenters
"""

import json
import urllib.error
import urllib.request

HETZNER_API_BASE = "https://api.hetzner.cloud/v1"
REQUEST_TIMEOUT_SECONDS = 15


def _fetch(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{HETZNER_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Hetzner API {path} returned HTTP {e.code}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Hetzner API {path} request failed: {e.reason}") from e


def fetch_server_type_id(server_type_name: str, token: str) -> int:
    """Return the numeric id for a Hetzner server type (e.g. ``cpx41``)."""
    path = "/server_types?per_page=50"
    while path:
        data = _fetch(path, token)
        for server_type in data.get("server_types", []):
            if server_type.get("name") == server_type_name:
                return server_type["id"]
        next_page = data.get("meta", {}).get("pagination", {}).get("next_page")
        path = f"/server_types?per_page=50&page={next_page}" if next_page else None
    raise ValueError(
        f"Hetzner server type '{server_type_name}' not found. "
        "Check the name with `hcloud server-type list`."
    )


def fetch_availability_map(token: str) -> dict[str, set[int]]:
    """Return ``location_name -> set(server_type_id)`` for currently available types.

    A location can host multiple datacenters; availability is the union
    across all datacenters at that location.
    """
    data = _fetch("/datacenters", token)
    avail: dict[str, set[int]] = {}
    for dc in data.get("datacenters", []):
        location = dc.get("location", {}).get("name")
        if not location:
            continue
        available_ids = set(dc.get("server_types", {}).get("available", []))
        avail.setdefault(location, set()).update(available_ids)
    return avail


def resolve_hetzner_regions(
    preferred: list[str],
    server_type: str,
    count: int,
    token: str,
    fallback_pool: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Pick ``count`` Hetzner regions with capacity for ``server_type``.

    Walks ``preferred`` first in the given order, then falls back through any
    extra regions in ``fallback_pool`` (also in order) that are not already in
    ``preferred``.

    Returns ``(regions, notes)`` — ``notes`` is a list of human-readable
    strings describing any substitutions, empty when the preferred regions
    were sufficient.

    Raises ``RuntimeError`` if fewer than ``count`` regions have capacity.
    """
    if count < 1:
        return [], []

    type_id = fetch_server_type_id(server_type, token)
    availability = fetch_availability_map(token)

    resolved: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    candidates = list(preferred) + [
        r for r in (fallback_pool or []) if r not in preferred
    ]
    preferred_set = set(preferred[:count])

    for region in candidates:
        if region in seen:
            continue
        if type_id in availability.get(region, set()):
            resolved.append(region)
            seen.add(region)
            if region not in preferred_set:
                notes.append(
                    f"Hetzner: using fallback region '{region}' "
                    f"(one or more preferred regions lack capacity for "
                    f"{server_type})"
                )
            if len(resolved) == count:
                return resolved, notes

    tried = ", ".join(candidates) if candidates else "(none)"
    raise RuntimeError(
        f"Hetzner: only {len(resolved)} region(s) have capacity for "
        f"{server_type}, but {count} requested (checked: {tried}). "
        "Wait for capacity or pick a different --hetzner-server-type."
    )
