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

from datetime import datetime, timezone

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


def _value(client: dict, key: str, default):
    """A recorded knob, defaulted only when ABSENT -- never when falsy.

    The same rule `AntClientProvisioner._hot_file_env` applies, and for the same
    reason: `or` let a recorded `0` take a non-zero default, which makes the
    registry describe a schedule the VM was never given. `start_stagger_secs`
    shows why the distinction is not academic -- 0 is both its default and a
    legitimate explicit setting.
    """
    recorded = client.get(key)
    return default if recorded is None else recorded


def hot_file_config_entry(deployment: str, extension: str | None, client: dict) -> dict:
    """One reader VM's recorded role config.

    ``ip`` is deliberately absent: this document outlives the VM, and an address
    that has been released back to the provider and reassigned is worse than no
    address at all. ``client_index`` IS here, and is load-bearing -- it is what
    each reader's start offset is derived from, so without it the schedule a
    round actually ran on cannot be reconstructed after teardown.

    ``recorded_at`` is what makes the schedule TIME-VERSIONED: an extension
    recreated under the same name with a different period would otherwise have
    its new period applied to every earlier round, and a round anchored to
    2027-01-15 came out as 2055-07-24. See :func:`period_for_round`.
    """
    return {
        "hostname": client_hostname(deployment, extension, client),
        "extension": extension,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": client.get("region"),
        "client_index": client.get("client_index"),
        "instances": _value(client, "instances", HOT_FILE_DEFAULT_INSTANCES),
        "round_period_secs": _value(
            client, "round_period_secs", HOT_FILE_DEFAULT_ROUND_PERIOD_SECS
        ),
        "download_timeout_secs": _value(
            client, "download_timeout_secs", HOT_FILE_DEFAULT_DOWNLOAD_TIMEOUT_SECS
        ),
        "round_parity": _value(client, "round_parity", HOT_FILE_PARITY_ALL),
        "start_stagger_secs": _value(
            client, "start_stagger_secs", HOT_FILE_DEFAULT_START_STAGGER_SECS
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


#: Key on a recorded entry holding the schedules this host ran on BEFORE the
#: current one, newest last, each with its own ``recorded_at``. The rounds
#: already recorded ran on them, so the period that renders their boundary has
#: to be the one in force at the time rather than whatever the host runs now.
SUPERSEDED_SCHEDULES_KEY = "superseded_schedules"

#: The fields that make up a schedule. A live entry differing from the recorded
#: one on any of them is a re-provision whose predecessor must be kept.
SCHEDULE_FIELDS = (
    "instances",
    "round_period_secs",
    "download_timeout_secs",
    "round_parity",
    "start_stagger_secs",
    "client_index",
)


def _schedule_of(entry: dict) -> dict:
    """One entry's schedule, with the instant it was recorded."""
    out = {field: entry.get(field) for field in SCHEDULE_FIELDS}
    out["recorded_at"] = entry.get("recorded_at")
    return out


def _carry_schedule(previous: dict | None, live: dict) -> dict:
    """``live``, carrying ``previous``'s schedule when the two differ."""
    if previous is None:
        return live
    if _schedule_of(previous) == _schedule_of(live) or all(
        previous.get(field) == live.get(field) for field in SCHEDULE_FIELDS
    ):
        # The same schedule re-recorded: keep whatever history it already had
        # and the ORIGINAL `recorded_at`, so an unchanged cohort's config does
        # not churn on every provision.
        merged = dict(live)
        # Set only when there is one to set, so a merge over entries that carry
        # no `recorded_at` at all stays idempotent rather than growing a
        # `recorded_at: None` key on every pass.
        earliest = previous.get("recorded_at") or live.get("recorded_at")
        if earliest is not None:
            merged["recorded_at"] = earliest
        history = previous.get(SUPERSEDED_SCHEDULES_KEY)
        if history:
            merged[SUPERSEDED_SCHEDULES_KEY] = history
        return merged
    merged = dict(live)
    history = list(previous.get(SUPERSEDED_SCHEDULES_KEY) or [])
    history.append(_schedule_of(previous))
    merged[SUPERSEDED_SCHEDULES_KEY] = history
    return merged


def _recorded_at_secs(value) -> int | None:
    """An entry's ``recorded_at`` as epoch seconds, or None."""
    if not value:
        return None
    try:
        return int(
            datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def period_for_round(entries: list[dict], extension, start_secs: int | None) -> int | None:
    """The round period in force for ``extension`` at ``start_secs``.

    Time-versioned, because an extension may legitimately be recreated under the
    same name with a different period and the schema accepts both. Applying the
    LATEST period to every round rewrote history: a round anchored to
    2027-01-15 08:00:00 rendered as 2055-07-24 once the cohort came back at
    900 s instead of 600 s.

    Two rules, in order:

    * **The period is a property of the COHORT**, so hosts of one extension that
      disagree on their current period are not one cohort and yield None --
      :func:`round_periods_by_extension` is that rule, and this defers to it
      rather than keeping a second copy.
    * Otherwise the newest schedule whose ``recorded_at`` is at or before the
      point's own start wins. A point older than every dated schedule takes the
      OLDEST, which is the closest thing to the one it actually ran on, and an
      undated schedule (a config written before `recorded_at` existed) sorts
      oldest for the same reason.
    """
    current = round_periods_by_extension([e for e in entries if e.get("extension") == extension])
    if extension not in current:
        return None

    timeline: list[tuple[int | None, int]] = []
    for entry in entries:
        if entry.get("extension") != extension:
            continue
        for schedule in [*(entry.get(SUPERSEDED_SCHEDULES_KEY) or []), _schedule_of(entry)]:
            period = schedule.get("round_period_secs")
            if period:
                timeline.append((_recorded_at_secs(schedule.get("recorded_at")), int(period)))
    if not timeline:
        return current[extension]
    # Undated first, then by recorded time.
    timeline.sort(key=lambda item: (item[0] is not None, item[0] or 0))
    if start_secs is None:
        return timeline[-1][1]
    applicable = [p for at, p in timeline if at is None or at <= start_secs]
    return applicable[-1] if applicable else timeline[0][1]


def period_resolver(entries: list[dict]):
    """A ``(extension, start_secs) -> period | None`` callable over ``entries``.

    What `print-downloads --hot-file` hands the summariser, so the boundary of
    each round is rendered with the period that round actually ran on.
    """

    def _resolve(extension, start_secs):
        return period_for_round(entries, extension, start_secs)

    return _resolve


def merge_hot_file_config(recorded: list[dict], live: list[dict]) -> list[dict]:
    """Historical entries plus the live scan, keyed by hostname, live winning.

    **A later provision must not erase a cohort that has already gone.** The
    scan only sees the tiers that exist NOW, so provisioning a `swarm`
    extension, tearing it down and then provisioning `control` would replace
    the recorded list outright and take the swarm cohort's hostnames,
    extension and schedule with it — which is the one thing this document
    exists to survive. A `record-results` pass that first reaches the swarm
    cohort's still-retained Influx points could then resolve no extension for
    them, and the upsert's COALESCE does not help: it protects a row that
    already HAS one, not the first write of a row that never did.

    Live wins on a hostname that appears in both, because a re-provision is how
    a VM's own schedule changes -- but **the schedule it replaces is kept**,
    under :data:`SUPERSEDED_SCHEDULES_KEY`, because the rounds already recorded
    ran on it. Overwriting it outright applied the new period to every earlier
    round: recreating an extension under the same name with period 900 instead
    of 600 moved a round anchored to 2027-01-15 to 2055-07-24.

    Sorted on the same key :func:`collect_hot_file_config` sorts on, so
    repeated provisions of an unchanged cohort write identical bytes.
    """
    by_hostname: dict[str, dict] = {}
    for entry in recorded:
        hostname = entry.get("hostname")
        if hostname:
            by_hostname[hostname] = entry
    for entry in live:
        hostname = entry.get("hostname")
        if not hostname:
            continue
        previous = by_hostname.get(hostname)
        by_hostname[hostname] = _carry_schedule(previous, entry)
    merged = list(by_hostname.values())
    merged.sort(key=lambda e: (e.get("extension") or "", e.get("hostname") or ""))
    return merged


def record_hot_file_config(deployment: str, console=None) -> list[dict]:
    """Persist the hot-file tier's role config into ``deploy_args``.

    MERGED with whatever is already recorded (see :func:`merge_hot_file_config`),
    not replaced: the live scan sees only the tiers standing now, and a cohort
    that has been torn down is exactly the one whose facts have to survive.

    Best-effort, like every other `record_deploy_args_update` call in a provision
    pass: without ``DATABASE_URL`` there is no registry to write to and client
    provisioning must not fail over it. A registry that cannot be read falls
    back to the live scan alone -- which is what the behaviour was, so a miss
    costs no more than it did.

    Writes nothing when neither the scan nor the record holds a reader VM, so an
    ordinary testnet's ``deploy_args`` is untouched.
    """
    from saorsa_deploy.registry import read_deploy_args, record_deploy_args_update

    live = collect_hot_file_config(deployment, client_tiers_with_extensions(deployment))
    entries = merge_hot_file_config(recorded_hot_file_config(read_deploy_args(deployment)), live)
    if not entries:
        return []
    record_deploy_args_update(deployment, {HOT_FILE_CONFIG_KEY: entries})
    if console is not None:
        kept = len(entries) - len(live)
        kept_note = f" ({kept} from an earlier provision)" if kept > 0 else ""
        console.print(
            f"  Recorded hot-file reader role config for {len(entries)} VM(s)"
            f"{kept_note} (deploy_args.{HOT_FILE_CONFIG_KEY})"
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


def round_periods_by_extension(entries: list[dict]) -> dict:
    """``extension -> round_period_secs`` from the recorded role config.

    The period is a property of the COHORT, not of a VM: every reader derives
    its round id from the wall clock, so two VMs on different periods are not
    one cohort at all. Entries that disagree within one extension therefore
    yield NOTHING for it rather than a guess -- the same rule
    `browser_records.round_period_from_config` applies, and the reason a
    consumer that cannot resolve the period must say so instead of inferring
    one.
    """
    seen: dict = {}
    for entry in entries:
        period = entry.get("round_period_secs")
        if not period:
            continue
        seen.setdefault(entry.get("extension"), set()).add(int(period))
    return {key: values.pop() for key, values in seen.items() if len(values) == 1}


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
    """Copy the hot-file selection into ``deploy_args``, keeping what it replaces.

    **The selection does not survive the deployment.** It lives at
    ``addresses/<deployment>/browser-hot-files.json``, and the deployment's own
    `clients destroy` deletes that WHOLE prefix -- while the rows this records
    beside outlive it by design. So it is snapshotted in the same pass that
    records them.

    **Every pass reads the live object**, because `--replace` can change it
    mid-run: returning early on "something is already recorded" meant the
    replacement was never captured, so rows describing the new address sat
    beside metadata describing the old one and the new file's sha256 was lost at
    teardown. The two are merged instead -- see
    :func:`merge_hot_file_selection`.

    The write is skipped when the merge changes nothing, so an unchanged
    selection still costs one S3 GET and no registry write per pass.

    A selection that has already gone (or an S3 error) leaves whatever was
    recorded in place: a snapshot taken while the cohort was running is the
    answer, and there is no later chance to take one.
    """
    from saorsa_deploy.hot_files import (
        SUPERSEDED_FILES_KEY,
        load_hot_files,
        merge_hot_file_selection,
    )
    from saorsa_deploy.registry import record_deploy_args_update

    # Whichever key holds it -- see :func:`recorded_hot_files`. A deployment
    # running both cohorts has one selection, snapshotted by whichever role's
    # pass reached it first, and this writes back under that same key so the two
    # roles cannot end up with two copies that drift.
    recorded = recorded_hot_files(deploy_args)
    key = HOT_FILE_HOT_FILES_KEY
    if isinstance(deploy_args, dict) and isinstance(deploy_args.get(BROWSER_HOT_FILES_KEY), dict):
        key = BROWSER_HOT_FILES_KEY
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

    merged = merge_hot_file_selection(recorded, document)
    if merged == recorded:
        return recorded
    record_deploy_args_update(deployment, {key: merged})
    if console is not None:
        superseded = len(merged.get(SUPERSEDED_FILES_KEY) or [])
        carried = f", carrying {superseded} superseded file(s)" if superseded else ""
        verb = "Updated" if recorded is not None else "Snapshotted"
        console.print(
            f"  {verb} the hot-file selection "
            f"({len(merged['files'])} file(s){carried}) into deploy_args.{key}"
        )
    return merged


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
