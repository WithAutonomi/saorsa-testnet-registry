"""The browser-downloader tier's own facts: which VMs it is, and its round config.

Three consumers need the same two things and none of them can reach the VMs:

* `record-results` needs the **extension** each browser VM belongs to, so a
  cohort added at hour 3 as a `kind: clients` extension is distinguishable from
  the deployment's own tier.
* `analysis browser-reads` needs the **round period** (to turn a `round_id` into
  a wall-clock boundary) and the **instances per VM** (to say how many points a
  round should have produced). It reads Postgres only, by design: it is meant to
  run after the VMs and their InfluxDB are gone.
* `clients fetch-browser-traces` needs the VMs' addresses while they are up.

The S3 client state answers all of it, but only while it exists — `clients
destroy` deletes it. So the role config is DENORMALISED into the deployment's
``deploy_args`` at `clients provision` time, which is the one place in the
registry that survives teardown, exactly as `prod-uploaders` records its
uploaders' hostnames and transports there.

The list is rebuilt from EVERY client tier on each provision pass, not appended
to: `record_deploy_args_update` shallow-merges, so a per-tier append would need
read-modify-write and two provisions racing would lose one of them. Rebuilding
is idempotent and converges whichever tier was provisioned last.
"""

from __future__ import annotations

from saorsa_deploy.client_extension import client_extension_tiers, terraform_name
from saorsa_deploy.client_roles import (
    BROWSER_DEFAULT_INSTANCES,
    BROWSER_DEFAULT_ROUND_PERIOD_SECS,
    ROLE_BROWSER_DOWNLOAD,
    is_browser,
)

#: ``deploy_args`` key holding the browser tier's recorded role config.
BROWSER_CONFIG_KEY = "browser_downloaders"


def client_hostname(deployment: str, extension: str | None, client: dict) -> str:
    """The hostname a client VM reports under, as its Terraform module names it.

    The same construction `client_transport.transport_map_from_client_state`
    uses; `client_index` restarts at 1 per role, so the role is part of it.
    """
    role = client.get("role", ROLE_BROWSER_DOWNLOAD)
    return (
        f"{terraform_name(deployment, extension)}-ant-client-"
        f"{role}-{client.get('region')}-{client.get('client_index')}"
    )


def browser_config_entry(deployment: str, extension: str | None, client: dict) -> dict:
    """One browser VM's recorded role config.

    ``ip`` is deliberately absent: this document outlives the VM, and an address
    that has been released back to the provider and reassigned is worse than no
    address at all. `clients fetch-browser-traces` reads the live client state
    for that.
    """
    return {
        "hostname": client_hostname(deployment, extension, client),
        "extension": extension,
        "region": client.get("region"),
        "client_index": client.get("client_index"),
        "instances": client.get("instances") or BROWSER_DEFAULT_INSTANCES,
        "round_period_secs": (client.get("round_period_secs") or BROWSER_DEFAULT_ROUND_PERIOD_SECS),
        "download_timeout_secs": client.get("download_timeout_secs"),
    }


def browser_vms_in_state(deployment: str, extension: str | None, state: dict | None) -> list[dict]:
    """The browser VMs of one client tier, as role-config entries."""
    if not state:
        return []
    return [
        browser_config_entry(deployment, extension, client)
        for client in state.get("clients") or []
        if is_browser(client.get("role", ""))
    ]


def collect_browser_config(deployment: str, tiers: list[tuple[str | None, dict]]) -> list[dict]:
    """Role-config entries for every browser VM across the given client tiers.

    ``tiers`` is ``(extension, client_state)`` pairs — the deployment's own tier
    carries ``None``. Sorted, so two provisions of the same cohort write
    identical bytes and a diff of ``deploy_args`` means something changed.
    """
    entries: list[dict] = []
    for extension, state in tiers:
        entries.extend(browser_vms_in_state(deployment, extension, state))
    entries.sort(key=lambda e: (e["extension"] or "", e["hostname"]))
    return entries


def client_tiers_with_extensions(deployment: str) -> list[tuple[str | None, dict]]:
    """``(extension, client_state)`` for the deployment's own tier and each extension.

    A missing own tier contributes nothing (a deployment may legitimately have
    no clients of its own and carry only an extension's cohort), and so does an
    extension that cannot be read — this is the reporting-side tolerance
    `client_extension_tiers` documents, not the index scan's.
    """
    from saorsa_deploy.state import load_client_state

    tiers: list[tuple[str | None, dict]] = []
    try:
        tiers.append((None, load_client_state(deployment)))
    except Exception:
        pass
    tiers.extend(client_extension_tiers(deployment))
    return tiers


def record_browser_config(deployment: str, console=None) -> list[dict]:
    """Persist the browser tier's role config into ``deploy_args``.

    Best-effort, like every other `record_deploy_args_update` call in a
    provision pass: without ``DATABASE_URL`` there is no registry to write to
    and client provisioning must not fail over it. The cost of a miss is that
    `analysis browser-reads` has to derive the period from the rows themselves.

    Writes nothing when the deployment has no browser VMs, so an ordinary
    testnet's ``deploy_args`` is untouched.
    """
    from saorsa_deploy.registry import record_deploy_args_update

    entries = collect_browser_config(deployment, client_tiers_with_extensions(deployment))
    if not entries:
        return []
    record_deploy_args_update(deployment, {BROWSER_CONFIG_KEY: entries})
    if console is not None:
        console.print(
            f"  Recorded browser role config for {len(entries)} VM(s) "
            f"(deploy_args.{BROWSER_CONFIG_KEY})"
        )
    return entries


def recorded_browser_config(deploy_args: dict | None) -> list[dict]:
    """The role config a deployment recorded, or an empty list."""
    if not isinstance(deploy_args, dict):
        return []
    entries = deploy_args.get(BROWSER_CONFIG_KEY)
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def extension_by_host(tiers: list[tuple[str | None, dict]], deployment: str) -> dict[str, str]:
    """hostname -> extension name, for every browser VM in the given tiers.

    Only the hosts that HAVE an extension are in the map: the deployment's own
    VMs carry NULL in the column, and a host the scan could not see must read as
    "not recorded" rather than as "the deployment's own".
    """
    out: dict[str, str] = {}
    for extension, state in tiers:
        if extension is None:
            continue
        for entry in browser_vms_in_state(deployment, extension, state):
            out[entry["hostname"]] = extension
    return out


def round_period_from_config(entries: list[dict]) -> int | None:
    """The cohort's round period, or None when the config does not say.

    The period is a property of the COHORT, not of a VM: every instance derives
    its round id from the wall clock, so two VMs on different periods are not
    one cohort at all. Entries that disagree therefore yield None rather than a
    guess — the report says so and falls back to deriving it from the rows.
    """
    periods = {e.get("round_period_secs") for e in entries if e.get("round_period_secs")}
    if len(periods) != 1:
        return None
    return int(periods.pop())


def expected_instances(entries: list[dict]) -> int | None:
    """How many points one round should produce, or None when unknown.

    The sum of each VM's `instances`, which is what the harness launches per
    round. None when no config was recorded, and the report then compares
    nothing rather than comparing against a zero.
    """
    if not entries:
        return None
    total = 0
    for entry in entries:
        try:
            total += int(entry.get("instances") or 0)
        except (TypeError, ValueError):
            continue
    return total or None
