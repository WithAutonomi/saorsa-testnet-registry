"""Vultr availability preflight.

Vultr plans are not offered in every region — notably the smaller US east
regions frequently lack the larger plans. This module queries the Vultr API
to figure out which regions support a requested plan, so we can substitute
fallback regions before invoking Terraform.

Reference: https://www.vultr.com/api/#tag/plans
"""

import json
import urllib.error
import urllib.request

VULTR_API_BASE = "https://api.vultr.com/v2"
REQUEST_TIMEOUT_SECONDS = 15


def _fetch(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{VULTR_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Vultr API {path} returned HTTP {e.code}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Vultr API {path} request failed: {e.reason}") from e


def fetch_plans(token: str) -> list[dict]:
    """Fetch every Vultr plan with its metadata.

    Returns a list of dicts with Vultr's raw fields — ``id``, ``vcpu_count``,
    ``ram`` (MB), ``disk`` (GB), ``type``, ``locations`` (list of region
    ids where the plan is offered), ``monthly_cost``, etc.
    """
    path = "/plans?per_page=500"
    results: list[dict] = []
    while path:
        data = _fetch(path, token)
        results.extend(data.get("plans", []))
        next_link = data.get("meta", {}).get("links", {}).get("next") or ""
        if not next_link:
            break
        path = f"/plans?per_page=500&cursor={next_link}"
    return results


def fetch_regions(token: str) -> list[dict]:
    """Fetch every Vultr region with its metadata."""
    path = "/regions?per_page=100"
    results: list[dict] = []
    while path:
        data = _fetch(path, token)
        results.extend(data.get("regions", []))
        next_link = data.get("meta", {}).get("links", {}).get("next") or ""
        if not next_link:
            break
        path = f"/regions?per_page=100&cursor={next_link}"
    return results


def fetch_plan_availability_map(token: str) -> dict[str, set[str]]:
    """Return ``region_id -> set(plan_id)`` for plans offered in each region.

    Built by inverting each plan's ``locations`` list. Vultr exposes this
    per-plan, not per-region, so we invert it once for O(1) lookups.
    """
    plans = fetch_plans(token)
    avail: dict[str, set[str]] = {}
    for plan in plans:
        plan_id = plan.get("id")
        if not plan_id:
            continue
        for region in plan.get("locations") or []:
            avail.setdefault(region, set()).add(plan_id)
    return avail


def resolve_vultr_regions(
    preferred: list[str],
    plan: str,
    count: int,
    token: str,
    fallback_pool: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Pick ``count`` Vultr regions that offer ``plan``.

    Walks ``preferred`` first, then any extra regions in ``fallback_pool``
    (preserving their order) that are not already in ``preferred``.

    Returns ``(regions, notes)`` — ``notes`` describes any substitutions,
    empty when the preferred regions sufficed.

    Raises ``RuntimeError`` when the plan doesn't exist or when fewer than
    ``count`` regions offer it.
    """
    if count < 1:
        return [], []

    availability = fetch_plan_availability_map(token)

    regions_offering_plan = {
        region for region, plans in availability.items() if plan in plans
    }
    if not regions_offering_plan:
        known_plans = {p for plans in availability.values() for p in plans}
        hint = ""
        if known_plans:
            hint = (
                " Check `saorsa-deploy utils vultr-availability` for the full "
                "plan/region matrix."
            )
        raise RuntimeError(
            f"Vultr plan '{plan}' is not offered in any region.{hint}"
        )

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
        if region in regions_offering_plan:
            resolved.append(region)
            seen.add(region)
            if region not in preferred_set:
                notes.append(
                    f"Vultr: using fallback region '{region}' "
                    f"(one or more preferred regions don't offer {plan})"
                )
            if len(resolved) == count:
                return resolved, notes

    tried = ", ".join(candidates) if candidates else "(none)"
    raise RuntimeError(
        f"Vultr: only {len(resolved)} region(s) offer {plan}, "
        f"but {count} requested (checked: {tried}). Pick a different "
        "--vultr-plan or expand --vultr-regions."
    )
