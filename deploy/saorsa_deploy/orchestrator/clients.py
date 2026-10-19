"""Translate a ``kind: clients`` manifest into an ordered step list.

A client *extension* adds client tiers to a deployment that is already running,
so its sequence is the tail of a testnet deploy and nothing else:
``clients infra`` → ``clients provision`` → ``clients start``. Teardown is
``clients stop`` → ``clients destroy``.

Two things are deliberately absent, and both would be wrong rather than merely
redundant:

* **No build steps.** The extension reuses the binaries the deployment recorded
  (``clients provision`` resolves them from deployment state when it is given
  ``--extension`` and no binary flags), so it cannot run a different client
  build from the deployment it is measuring.
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

    steps: list[Step] = [
        Step(
            _clients_infra_argv(deployment, spec, extension),
            f"Create client VMs for extension '{extension}'",
        ),
        Step(
            [
                "clients",
                "provision",
                "--name",
                deployment,
                "--extension",
                extension,
                "--ssh-key-path",
                ssh_key,
            ],
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
