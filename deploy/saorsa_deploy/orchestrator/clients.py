"""Translate a ``kind: clients`` manifest into an ordered step list.

A client *extension* adds client tiers to a deployment that is already running,
so its sequence is the tail of a testnet deploy and nothing else:
``clients infra`` → ``clients provision`` → ``clients start``. Teardown is
``clients stop`` → ``clients destroy``.

Two things are deliberately absent, and both would be wrong rather than merely
redundant:

* **No build steps.** The extension reuses the binaries the deployment recorded
  (``clients provision`` resolves them from deployment state when it is given
  ``--extension`` and no binary flags), so by default it cannot run a different
  client build from the deployment it is measuring. A manifest ``binaries.ant``
  overrides that for the one case the default cannot express — a *released*
  client against branch-built nodes — and is passed straight through as
  ``--ant-version`` on the provision step. The BRANCH form is refused at the
  manifest rather than here: it would need a build lane, which is the step this
  sequence does not have.
* **No ``deployments webrtc-addresses`` step.** The bootstrap WebRTC multiaddrs
  are a property of the DEPLOYMENT's bootstrap tier, collected once by its own
  ``up``. Re-collecting them here would SSH the deployment's bootstrap VMs and
  rewrite its deployment state — an extension touching the thing it extends.
  A WASM extension against a deployment that never collected them is refused by
  ``clients provision``, which is where that check already lives.

The builders are pure (no I/O), so ``clients plan`` prints the sequence without
the deployment having to exist.
"""

from __future__ import annotations

from saorsa_deploy.client_roles import (
    BROWSER_DEFAULT_VM_SIZE,
    ROLE_BROWSER_DOWNLOAD,
    ROLE_HOT_FILE_READ,
)
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator.steps import Step
from saorsa_deploy.orchestrator.testnet import (
    _clients_destroy_argv,
    _clients_infra_argv,
)


def build_clients_deploy_steps(manifest: Manifest) -> list[Step]:
    """``clients infra`` → ``provision`` → ``start`` for an extension's tiers."""
    deployment = manifest.deployment
    extension = manifest.name
    ssh_key = manifest.ssh_key_path
    spec = manifest.clients

    provision_argv = [
        "clients",
        "provision",
        "--name",
        deployment,
        "--extension",
        extension,
        "--ssh-key-path",
        ssh_key,
    ]
    if manifest.binaries is not None:
        # An explicit version is the escape hatch `_apply_recorded_client_binaries`
        # already honours: one binary flag turns the whole reuse off, which is
        # why the manifest refuses this alongside a WASM tier. Only the version
        # form reaches here — the branch form is a manifest-level refusal.
        provision_argv += ["--ant-version", manifest.binaries.ant.version]

    steps: list[Step] = [
        Step(
            _clients_infra_argv(deployment, spec, extension),
            f"Create client VMs for extension '{extension}'",
        ),
        Step(
            provision_argv,
            f"Provision client VMs for extension '{extension}'",
        ),
        Step(
            [
                "clients",
                "start",
                "--name",
                deployment,
                "--extension",
                extension,
                "--ssh-key-path",
                ssh_key,
            ],
            f"Start client services for extension '{extension}'",
        ),
    ]
    if manifest.slack_post:
        # The extension's own post, not `deployments slack-post`: that one takes
        # a DEPLOYMENT name and reads its registry row, which an extension does
        # not have. This posts into the deployment's thread instead.
        steps.append(
            Step(
                [
                    "clients",
                    "slack-post",
                    "--name",
                    deployment,
                    "--extension",
                    extension,
                    "--event",
                    "up",
                ],
                f"Post extension '{extension}' up to Slack",
                best_effort=True,
            )
        )
    return steps


def build_clients_destroy_steps(
    manifest: Manifest, *, skip_drain_failures: bool = False
) -> list[Step]:
    """``clients stop`` → ``clients destroy`` for an extension's tiers.

    The stop is a separate step rather than left to ``destroy``'s own
    best-effort service stop: an uploader extension's wallets are drained during
    the destroy, and a workload still paying out of one races the drain's nonce.
    ``destroy`` stops the wallet-holding VMs itself, so this covers the
    download-only and pointer VMs too — every service is off before any VM goes.
    """
    deployment = manifest.deployment
    extension = manifest.name
    ssh_key = manifest.ssh_key_path
    steps = [
        Step(
            [
                "clients",
                "stop",
                "--name",
                deployment,
                "--extension",
                extension,
                "--ssh-key-path",
                ssh_key,
            ],
            f"Stop client services for extension '{extension}'",
            # A VM that is already gone (a re-run after a partial destroy) has
            # no service to stop, and that must not block the teardown.
            best_effort=True,
        ),
        Step(
            _clients_destroy_argv(deployment, ssh_key, skip_drain_failures, extension),
            f"Destroy client VMs for extension '{extension}'",
        ),
    ]
    if manifest.slack_post:
        # The VM summary travels as an argument because the destroy step above
        # has already deleted the extension's state: by the time this runs
        # there is nothing left to read what the cohort was off.
        steps.append(
            Step(
                [
                    "clients",
                    "slack-post",
                    "--name",
                    deployment,
                    "--extension",
                    extension,
                    "--event",
                    "down",
                    "--vms",
                    vm_summary(manifest),
                ],
                f"Post extension '{extension}' teardown to Slack",
                best_effort=True,
            )
        )
    return steps


#: Role key on each manifest list -> the client ROLE the CLI and Terraform use.
_ROLE_FOR_LIST = {
    "uploaders": "upload",
    "downloaders": "download",
    "wasm_uploaders": "wasm-upload",
    "wasm_downloaders": "wasm-download",
    "pointers": "pointer",
    "browser_downloaders": ROLE_BROWSER_DOWNLOAD,
    "hot_file_readers": ROLE_HOT_FILE_READ,
}

#: Default VM size per role, mirroring the `clients infra` spec parsers. Only
#: used to render the plan when an entry leaves `vm_size` unset.
_DEFAULT_VM_SIZE = "s-2vcpu-4gb"
#: Roles whose default differs from the shared one above.
_ROLE_DEFAULT_VM_SIZE = {ROLE_BROWSER_DOWNLOAD: BROWSER_DEFAULT_VM_SIZE}


def planned_client_vms(
    manifest: Manifest, base_indexes: dict[str, int] | None = None
) -> list[dict]:
    """The VMs ``clients up`` would create, in the order ``clients infra`` makes them.

    ``base_indexes`` maps a role to the highest ``client_index`` already in use
    for it across the deployment (its own clients and its other extensions);
    this tier's indexes continue from there. Passing ``None`` plans from 1,
    which is what ``clients plan`` does when the deployment's state cannot be
    read — the plan is still the right shape, and it says so.
    """
    spec = manifest.clients
    deployment = manifest.deployment
    extension = manifest.name
    base = dict(base_indexes or {})

    vms: list[dict] = []
    for list_name, role in _ROLE_FOR_LIST.items():
        entries = getattr(spec, list_name, []) or []
        default_size = _ROLE_DEFAULT_VM_SIZE.get(role, _DEFAULT_VM_SIZE)
        offset = 0
        for entry in entries:
            # `count` (browser downloaders only) is shorthand for N identical
            # VMs, and the plan prints each one: a 35-VM cohort written as one
            # entry still has to show 35 hostnames and 35 indexes, or the plan
            # is not what `clients up` is about to create.
            for _ in range(getattr(entry, "count", 1)):
                offset += 1
                index = base.get(role, 0) + offset
                vms.append(
                    {
                        "role": role,
                        "provider": entry.provider,
                        "region": entry.region,
                        "vm_size": entry.vm_size or default_size,
                        "client_index": index,
                        "hostname": (
                            f"{deployment}-{extension}-ant-client-{role}-{entry.region}-{index}"
                        ),
                    }
                )
    return vms


def manifest_client_tiers(manifest: Manifest):
    """The client block of any manifest kind that has one, or None.

    Three kinds carry client tiers in three places -- a testnet's
    ``testnet.clients``, an extension's ``extension.clients`` and a
    ``kind: clients`` manifest's top-level ``clients`` -- and the notes below
    are printed for all three, so the lookup lives here rather than being
    repeated at each call site.
    """
    for holder in (manifest.testnet, manifest.extension):
        if holder is not None and getattr(holder, "clients", None) is not None:
            return holder.clients
    return manifest.clients


def reader_offset_secs(client_index: int, instance: int, instances: int, stagger: int) -> int:
    """One reader's start offset from the round boundary.

    ``((k − 1) × instances + (i − 1)) × stagger``, mirroring
    ``reader_offset_secs`` in ``resources/scripts/hot_file_reader.py``. The two
    cannot be shared (that file is copied verbatim onto a VM and imports nothing
    from this package) and a test pins them together.
    """
    if stagger <= 0:
        return 0
    return ((max(client_index, 1) - 1) * instances + (max(instance, 1) - 1)) * stagger


def last_hot_file_reader_offset_secs(max_client_index: int, instances: int, stagger: int) -> int:
    """The offset of the last reader on the highest-indexed VM of a cohort.

    The service's own formula with ``i = instances``:
    ``((k − 1) × instances + (instances − 1)) × stagger``. Keyed on the highest
    allocated ``client_index``, NOT on how many VMs the cohort has — an
    extension's indexes CONTINUE from whatever the deployment already uses, so a
    second cohort of four VMs is indexes 5..8 and its last reader starts far
    later than a first cohort's would.

    Mirrors ``last_reader_offset_secs`` in
    ``resources/scripts/hot_file_reader.py``; the two cannot be shared (that
    file is copied verbatim onto a VM and imports nothing from this package) and
    a test pins them together. A plan that describes a schedule the VMs do not
    run is worse than no plan: counting VMs printed 150 s for a cohort whose
    last reader actually starts at 310 s, which is the figure an operator sizes
    the stagger against.
    """
    if stagger <= 0 or max_client_index <= 0 or instances <= 0:
        return 0
    return ((max_client_index - 1) * instances + (instances - 1)) * stagger


def hot_file_stagger_notes(
    manifest: Manifest, base_indexes: dict[str, int] | None = None
) -> list[str]:
    """One line per hot-file reader cohort, naming the LAST reader's offset.

    Printed because the arithmetic is not checked anywhere: the operator sizes
    the cohort, and a stagger that walks the last reader's start past the
    round's own deadline is a cohort whose later readers never overlap the
    earlier ones -- which is the one thing the stagger exists to arrange.
    Refusing it would be wrong (a deliberately long stagger over a long round is
    a legitimate shape), so it is said out loud instead.

    Grouped by ``(round_period_secs, download_timeout_secs, round_parity,
    start_stagger_secs, instances)``, not per entry: the schedule is a property
    of the COHORT, and two entries that agree on it are one cohort however many
    regions they span.

    **Indexes are allocated exactly as :func:`planned_client_vms` allocates
    them** — one running offset over every entry in the role's list, continuing
    from ``base_indexes``, so a group's highest index accounts for the VMs other
    groups in the same manifest occupy ahead of it as well as for the
    deployment's existing readers. ``base_indexes`` is ``None`` on a deployment
    manifest (its own tier always starts at 1) and on a ``clients plan`` whose
    deployment state could not be read, which is the case that already prints a
    note saying the indexes shown are probably wrong.

    Pure, so `clients plan` and `deployments plan` print the same line.
    """
    entries = getattr(manifest_client_tiers(manifest), "hot_file_readers", None) or []
    base = dict(base_indexes or {}).get(ROLE_HOT_FILE_READ, 0)

    cohorts: dict[tuple, list[int]] = {}
    offset = 0
    for entry in entries:
        key = (
            entry.round_period_secs,
            entry.download_timeout_secs,
            entry.round_parity,
            entry.start_stagger_secs,
            entry.instances,
        )
        for _ in range(entry.count):
            offset += 1
            cohorts.setdefault(key, []).append(base + offset)

    notes: list[str] = []
    for key, indexes in cohorts.items():
        period, deadline, parity, stagger, instances = key
        highest = max(indexes)
        last = last_hot_file_reader_offset_secs(highest, instances, stagger)
        note = (
            f"hot-file readers: {len(indexes)} VM(s) x {instances} reader(s) "
            f"(client_index {min(indexes)}..{highest}), round {period}s, parity "
            f"{parity}, stagger {stagger}s — the last reader starts {last}s "
            f"after the boundary"
        )
        # Compared with the DEADLINE, not with `last + deadline` against the
        # period. The deadline is an absolute offset from the boundary -- the
        # service refuses `offset >= download_timeout_secs` outright -- so it is
        # not an extra download duration added to each start. The old sum said
        # nothing: with period 600, deadline 100 and a 150 s last start,
        # `150 + 100 <= 600` passed silently while six of the sixteen readers
        # were guaranteed never to start at all.
        stranded = sum(
            1
            for index in indexes
            for i in range(1, instances + 1)
            if reader_offset_secs(index, i, instances, stagger) >= deadline
        )
        if stranded:
            note += (
                f"; the {deadline}s deadline is an absolute offset from the "
                f"boundary, so {stranded} of the {len(indexes) * instances} "
                f"readers start at or past it, can never run and will record a "
                f"timeout every round"
            )
        notes.append(note)
    return notes


def vm_summary(manifest: Manifest) -> str:
    """One line naming what the extension's VMs were: ``"<N> VMs — <role>: …"``.

    Pure, and rendered at plan time on purpose: it goes into the teardown's
    ``clients slack-post --vms`` argument, because the destroy step deletes the
    extension's state before that post runs. Roles appear in
    :data:`_ROLE_FOR_LIST` order and regions in first-seen order, so the same
    manifest always produces the same string — the argv is what the step
    journal compares.
    """
    vms = planned_client_vms(manifest)
    clauses: list[str] = []
    for role in _ROLE_FOR_LIST.values():
        regions: dict[str, int] = {}
        for vm in vms:
            if vm["role"] != role:
                continue
            regions[vm["region"]] = regions.get(vm["region"], 0) + 1
        if not regions:
            continue
        counted = ", ".join(f"{region} ×{count}" for region, count in regions.items())
        clauses.append(f"{role}: {counted}")
    return f"{len(vms)} VMs — {'; '.join(clauses)}"
