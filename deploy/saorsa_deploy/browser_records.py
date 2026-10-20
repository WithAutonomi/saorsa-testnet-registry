"""The browser-downloader tier's own facts: which VMs it is, and its round config.

Two consumers need facts about the tier that neither of them can reach a VM for:

* `record-results` needs the **extension** each browser VM belongs to, so a
  cohort added at hour 3 as a `kind: clients` extension is distinguishable from
  the deployment's own tier.
* `analysis browser-reads` needs the **round period** (to turn a `round_id` into
  a wall-clock boundary), the **instances per VM** (to say how many points a
  round should have produced) and the **hot-file selection** (to say which file
  each round read). It reads Postgres and the run's Elasticsearch, never a VM:
  it is meant to run once the VMs and their InfluxDB are gone.

The S3 client state answers all of it, but only while it exists — `clients
destroy` deletes it, and the deployment's own teardown also deletes the whole
``addresses/<deployment>/`` prefix the hot-file selection lives under. So both
are DENORMALISED into the deployment's ``deploy_args``, which is the one place in
the registry that survives teardown, exactly as `prod-uploaders` records its
uploaders' hostnames and transports there: the **role config** at `clients
provision` time, and the **hot-file selection** in the `record-results` pass that
records the rows it describes.

The list is rebuilt from EVERY client tier on each provision pass, not appended
to: `record_deploy_args_update` shallow-merges, so a per-tier append would need
read-modify-write and two provisions racing would lose one of them. Rebuilding
is idempotent and converges whichever tier was provisioned last.
"""

from __future__ import annotations

from saorsa_deploy.client_extension import client_extension_tiers, client_vm_name
from saorsa_deploy.client_roles import (
    BROWSER_DEFAULT_INSTANCES,
    BROWSER_DEFAULT_ROUND_PERIOD_SECS,
    ROLE_BROWSER_DOWNLOAD,
    is_browser,
)

#: ``deploy_args`` key holding the browser tier's recorded role config.
BROWSER_CONFIG_KEY = "browser_downloaders"

#: ``deploy_args`` key holding the snapshotted hot-file selection. The document
#: itself lives under the deployment's S3 ADDRESS prefix, which the deployment's
#: own `clients destroy` deletes whole -- so the report, which runs after
#: teardown, reads this copy.
BROWSER_HOT_FILES_KEY = "browser_hot_files"


def client_hostname(deployment: str, extension: str | None, client: dict) -> str:
    """The hostname a client VM reports under, from its client-state entry.

    :func:`client_extension.client_vm_name` is the one definition of that name —
    the same string `clients provision` sets as the OS hostname and
    `client_transport.transport_map_from_client_state` keys on.
    """
    return client_vm_name(
        deployment,
        extension,
        client.get("role", ROLE_BROWSER_DOWNLOAD),
        client.get("region"),
        client.get("client_index"),
    )


def browser_config_entry(deployment: str, extension: str | None, client: dict) -> dict:
    """One browser VM's recorded role config.

    ``ip`` is deliberately absent: this document outlives the VM, and an address
    that has been released back to the provider and reassigned is worse than no
    address at all. Nothing needs one — the per-dial traces are forwarded to
    Elasticsearch as they are produced, so no command SSHes a browser VM after
    the fact.
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


def extension_by_host_from_config(entries: list[dict]) -> dict[str, str]:
    """hostname -> extension name from the RECORDED role config.

    The durable half of the same answer. ``clients down`` deletes the
    extension's S3 client state, so :func:`extension_by_host` resolves nothing
    for a cohort that has been torn down -- and a `record-results` re-run after
    that point is exactly when the second pass of a run's procedure happens.
    Only entries that name an extension are included, for the reason above.
    """
    out: dict[str, str] = {}
    for entry in entries:
        hostname = entry.get("hostname")
        extension = entry.get("extension")
        if hostname and extension:
            out[hostname] = extension
    return out


def resolve_extension_by_host(
    deploy_args: dict | None, tiers: list[tuple[str | None, dict]], deployment: str
) -> dict[str, str]:
    """hostname -> extension, recorded config first and the live tiers second.

    The recorded config wins because it survives teardown, and the two cannot
    disagree: the extension name is part of the hostname, so a host determines
    its own extension. The live tiers still fill the gaps -- a tier provisioned
    without ``DATABASE_URL``, or one added since the config was last written.
    """
    out = dict(extension_by_host(tiers, deployment))
    out.update(extension_by_host_from_config(recorded_browser_config(deploy_args)))
    return out


def recorded_hot_files(deploy_args: dict | None) -> dict | None:
    """The hot-file selection a deployment recorded, or None.

    Only a document with a ``files`` list counts: a half-written or empty
    snapshot must read as "nothing recorded" so the report falls back to S3
    rather than reporting a selection of no files.
    """
    if not isinstance(deploy_args, dict):
        return None
    document = deploy_args.get(BROWSER_HOT_FILES_KEY)
    if not isinstance(document, dict):
        return None
    if not isinstance(document.get("files"), list):
        return None
    return document


def snapshot_hot_files(deployment: str, console=None) -> dict | None:
    """Copy the hot-file selection from S3 into ``deploy_args``.

    **The selection does not survive the deployment.** It lives at
    ``addresses/<deployment>/browser-hot-files.json``, and the deployment's own
    `clients destroy` deletes that WHOLE prefix -- while `analysis
    browser-reads` is meant to run after teardown and needs the document to say
    which file each round read. So it is snapshotted beside the role config, in
    the same pass that records the rows it describes.

    Best-effort, and deliberately only ever ADDITIVE: a selection that has
    already gone (or an S3 error) leaves whatever was recorded earlier in place,
    because a snapshot taken while the cohort was running is the answer and
    there is no later chance to take one.
    """
    from saorsa_deploy.hot_files import load_hot_files
    from saorsa_deploy.registry import record_deploy_args_update

    try:
        document = load_hot_files(deployment)
    except Exception as e:  # noqa: BLE001 -- a results pass must not fail on it
        if console is not None:
            console.print(
                f"[yellow]Warning: could not read the hot-file selection for "
                f"'{deployment}' ({e}); any snapshot already recorded is "
                f"kept.[/yellow]"
            )
        return None
    if not document or not isinstance(document.get("files"), list):
        return None
    record_deploy_args_update(deployment, {BROWSER_HOT_FILES_KEY: document})
    if console is not None:
        console.print(
            f"  Snapshotted the hot-file selection "
            f"({len(document['files'])} file(s)) into "
            f"deploy_args.{BROWSER_HOT_FILES_KEY}"
        )
    return document


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
