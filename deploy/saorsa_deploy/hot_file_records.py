"""The hot-file reader tier's own facts: which VMs it is, and its round config.

The same problem :mod:`saorsa_deploy.browser_records` solves for the browser
cohort, and solved the same way. Two consumers need facts about the tier that
neither can reach a VM for:

* `record-results` needs the **extension** each reader VM belongs to. The DEV-01
  run adds two cohorts to one deployment -- a branch build on even rounds and a
  released control on odd -- so without it the two are indistinguishable in the
  table, which is the whole comparison.
* Anything reading the rows afterwards needs the **round period**, the **readers
  per VM**, the **parity** and the **stagger** to say how many points a round
  should have produced and when each reader was due.

The S3 client state answers all of it, but only while it exists -- `clients
destroy` deletes it. So it is DENORMALISED into the deployment's ``deploy_args``
at `clients provision` time, which is the one place in the registry that
survives teardown.

The list is rebuilt from EVERY client tier on each provision pass, not appended
to: `record_deploy_args_update` shallow-merges, so a per-tier append would need
read-modify-write and two provisions racing would lose one of them. Rebuilding
is idempotent and converges whichever tier was provisioned last.
"""

from __future__ import annotations

from saorsa_deploy.browser_records import (
    BROWSER_HOT_FILES_KEY,
    client_tiers_with_extensions,
)
from saorsa_deploy.client_extension import client_vm_name
from saorsa_deploy.client_roles import (
    HOT_FILE_DEFAULT_DOWNLOAD_TIMEOUT_SECS,
    HOT_FILE_DEFAULT_INSTANCES,
    HOT_FILE_DEFAULT_ROUND_PERIOD_SECS,
    HOT_FILE_DEFAULT_START_STAGGER_SECS,
    HOT_FILE_PARITY_ALL,
    ROLE_HOT_FILE_READ,
    is_hot_file_read,
)

#: ``deploy_args`` key holding the hot-file tier's recorded role config.
HOT_FILE_CONFIG_KEY = "hot_file_readers"

#: ``deploy_args`` key holding the snapshotted hot-file selection for this role.
#: A key of its own rather than reusing the browser role's
#: :data:`BROWSER_HOT_FILES_KEY`, because the two roles are provisioned and
#: recorded independently -- but only ever written when the browser one is NOT
#: already there, so a deployment running both cohorts off one selection keeps
#: one copy of it rather than two that can drift.
HOT_FILE_HOT_FILES_KEY = "hot_file_readers_hot_files"


def client_hostname(deployment: str, extension: str | None, client: dict) -> str:
    """The hostname a reader VM reports under, from its client-state entry.

    :func:`client_extension.client_vm_name` is the one definition of that name --
    the same string `clients provision` sets as the OS hostname and the reader
    tags its points with.
    """
    return client_vm_name(
        deployment,
        extension,
        client.get("role", ROLE_HOT_FILE_READ),
        client.get("region"),
        client.get("client_index"),
    )


def hot_file_config_entry(deployment: str, extension: str | None, client: dict) -> dict:
    """One reader VM's recorded role config.

    ``ip`` is deliberately absent: this document outlives the VM, and an address
    that has been released back to the provider and reassigned is worse than no
    address at all. ``client_index`` IS here, and is load-bearing -- it is what
    each reader's start offset is derived from, so without it the schedule a
    round actually ran on cannot be reconstructed after teardown.
    """
    return {
        "hostname": client_hostname(deployment, extension, client),
        "extension": extension,
        "region": client.get("region"),
        "client_index": client.get("client_index"),
        "instances": client.get("instances") or HOT_FILE_DEFAULT_INSTANCES,
        "round_period_secs": (
            client.get("round_period_secs") or HOT_FILE_DEFAULT_ROUND_PERIOD_SECS
        ),
        "download_timeout_secs": (
            client.get("download_timeout_secs") or HOT_FILE_DEFAULT_DOWNLOAD_TIMEOUT_SECS
        ),
        "round_parity": client.get("round_parity") or HOT_FILE_PARITY_ALL,
        "start_stagger_secs": (
            client.get("start_stagger_secs") or HOT_FILE_DEFAULT_START_STAGGER_SECS
        ),
    }


def hot_file_vms_in_state(deployment: str, extension: str | None, state: dict | None) -> list[dict]:
    """The hot-file reader VMs of one client tier, as role-config entries."""
    if not state:
        return []
    return [
        hot_file_config_entry(deployment, extension, client)
        for client in state.get("clients") or []
        if is_hot_file_read(client.get("role", ""))
    ]


def collect_hot_file_config(deployment: str, tiers: list[tuple[str | None, dict]]) -> list[dict]:
    """Role-config entries for every reader VM across the given client tiers.

    ``tiers`` is ``(extension, client_state)`` pairs -- the deployment's own tier
    carries ``None``. Sorted, so two provisions of the same cohort write
    identical bytes and a diff of ``deploy_args`` means something changed.
    """
    entries: list[dict] = []
    for extension, state in tiers:
        entries.extend(hot_file_vms_in_state(deployment, extension, state))
    entries.sort(key=lambda e: (e["extension"] or "", e["hostname"]))
    return entries


def record_hot_file_config(deployment: str, console=None) -> list[dict]:
    """Persist the hot-file tier's role config into ``deploy_args``.

    Best-effort, like every other `record_deploy_args_update` call in a provision
    pass: without ``DATABASE_URL`` there is no registry to write to and client
    provisioning must not fail over it.

    Writes nothing when the deployment has no reader VMs, so an ordinary
    testnet's ``deploy_args`` is untouched.
    """
    from saorsa_deploy.registry import record_deploy_args_update

    entries = collect_hot_file_config(deployment, client_tiers_with_extensions(deployment))
    if not entries:
        return []
    record_deploy_args_update(deployment, {HOT_FILE_CONFIG_KEY: entries})
    if console is not None:
        console.print(
            f"  Recorded hot-file reader role config for {len(entries)} VM(s) "
            f"(deploy_args.{HOT_FILE_CONFIG_KEY})"
        )
    return entries


def recorded_hot_file_config(deploy_args: dict | None) -> list[dict]:
    """The role config a deployment recorded, or an empty list."""
    if not isinstance(deploy_args, dict):
        return []
    entries = deploy_args.get(HOT_FILE_CONFIG_KEY)
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def extension_by_host_from_config(entries: list[dict]) -> dict[str, str]:
    """hostname -> extension name from the RECORDED role config.

    Only the hosts that HAVE an extension are in the map: the deployment's own
    VMs carry NULL in the column, and a host the scan could not see must read as
    "not recorded" rather than as "the deployment's own".
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
    out: dict[str, str] = {}
    for extension, state in tiers:
        if extension is None:
            continue
        for entry in hot_file_vms_in_state(deployment, extension, state):
            out[entry["hostname"]] = extension
    out.update(extension_by_host_from_config(recorded_hot_file_config(deploy_args)))
    return out


def snapshot_hot_files(deployment: str, deploy_args: dict | None, console=None) -> dict | None:
    """Copy the hot-file selection into ``deploy_args`` -- unless it is there already.

    **The selection does not survive the deployment.** It lives at
    ``addresses/<deployment>/browser-hot-files.json``, and the deployment's own
    `clients destroy` deletes that WHOLE prefix -- while the rows this records
    beside outlive it by design. So it is snapshotted in the same pass that
    records them.

    One copy, not two: when the BROWSER role has already snapshotted the same
    document under its own key, nothing is written here. Both roles read the one
    object, and two copies of it in ``deploy_args`` could only ever drift.

    Best-effort and deliberately only ever ADDITIVE: a selection that has
    already gone (or an S3 error) leaves whatever was recorded earlier in place,
    because a snapshot taken while the cohort was running is the answer and
    there is no later chance to take one.
    """
    from saorsa_deploy.hot_files import load_hot_files
    from saorsa_deploy.registry import record_deploy_args_update

    # Either key counts -- see :func:`recorded_hot_files`. That is what makes
    # this a no-op when the browser role has already snapshotted the same
    # document, and what makes a second `record-results` pass a no-op too.
    if recorded_hot_files(deploy_args) is not None:
        return None
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
    record_deploy_args_update(deployment, {HOT_FILE_HOT_FILES_KEY: document})
    if console is not None:
        console.print(
            f"  Snapshotted the hot-file selection "
            f"({len(document['files'])} file(s)) into "
            f"deploy_args.{HOT_FILE_HOT_FILES_KEY}"
        )
    return document


def recorded_hot_files(deploy_args: dict | None) -> dict | None:
    """The selection recorded under either key, or None.

    Either key, and the BROWSER one first: a deployment running both cohorts off
    one selection has it under whichever role's pass reached it first, and a
    reader of the rows does not care which.

    Only a document with a ``files`` list counts: a half-written or empty
    snapshot must read as "nothing recorded".
    """
    if not isinstance(deploy_args, dict):
        return None
    for key in (BROWSER_HOT_FILES_KEY, HOT_FILE_HOT_FILES_KEY):
        document = deploy_args.get(key)
        if isinstance(document, dict) and isinstance(document.get("files"), list):
            return document
    return None
