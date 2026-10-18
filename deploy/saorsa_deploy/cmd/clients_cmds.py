"""`saorsa-deploy clients up | down | plan | ls | config` — client extensions.

A client *extension* is a client tier added to a deployment that is already
running, declared by a ``kind: clients`` manifest. These commands mirror
``deployments up`` / ``down`` / ``plan`` / ``config`` exactly, down to the step
journal and its ``--resume`` semantics, and reuse their executor — so a run that
knows how to drive one knows how to drive the other.

``down`` takes ``--config`` or the ``--name``/``--extension`` pair, for the same
reason ``deployments down`` takes ``--name``: teardown is state-driven, and the
manifest can be gone by the time someone needs the VMs to stop billing.
"""

from __future__ import annotations

import sys

from rich.console import Console

from saorsa_deploy.client_extension import (
    ExtensionError,
    highest_client_indexes,
    journal_name,
    validate_extension_name,
)
from saorsa_deploy.cmd.deploy_cmds import (
    _execute,
    _print_plan,
    _resolve_step_journal,
)
from saorsa_deploy.config import ManifestError, load_manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.orchestrator.clients import planned_client_vms
from saorsa_deploy.orchestrator.steps import Step
from saorsa_deploy.state import (
    S3_KEY_PREFIX,
    delete_step_journal,
    list_client_extensions,
    load_client_state,
    load_deployment_state,
)


class _JournalIdentity:
    """What ``_resolve_step_journal`` reads off a manifest: a name and a kind.

    An extension's journal is keyed on ``<deployment>--<extension>`` rather than
    on ``manifest.name``, so the manifest cannot be handed over as-is. A
    two-field stand-in keeps ONE journal implementation for both commands,
    instead of a second copy of the resume prompt, the divergence report and the
    non-interactive refusal — the three pieces that make a journal hit never
    silently skip a step.
    """

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind


def _load_clients_manifest(args, console: Console):
    """Load a manifest and refuse it unless it is a client extension."""
    try:
        manifest = load_manifest(
            args.config, sets=getattr(args, "set", None), name=getattr(args, "name", None)
        )
    except ManifestError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(2)
    if manifest.kind != "clients":
        console.print(
            f"[bold red]Error:[/bold red] '{args.config}' is a "
            f"'{manifest.kind}' manifest, not a client extension. Use "
            f"`deployments up`/`down` for it; `clients up` takes a "
            f"`kind: clients` manifest."
        )
        sys.exit(2)
    return manifest


def _require_deployment_state(manifest, console: Console) -> dict:
    """The named deployment's S3 state, or exit 2 naming the key that is missing.

    Nothing an extension does makes sense without it: the bootstraps its clients
    dial, the InfluxDB its operations land in and the address pool its
    downloaders read all come from the deployment it names.
    """
    try:
        return load_deployment_state(manifest.deployment)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] no deployment state for "
            f"'{manifest.deployment}' — the key "
            f"s3://.../{S3_KEY_PREFIX}/{manifest.deployment}.json does not "
            f"exist. A client extension is scoped to a deployment that is "
            f"already up; check `deployment:` in the manifest."
        )
        sys.exit(2)


def _base_indexes_or_note(manifest) -> tuple[dict[str, int] | None, str | None]:
    """``client_index`` continuation for the plan, plus a note when it is unknown.

    ``plan`` must work without the deployment existing — it is how an operator
    checks a manifest before anything is up — so an unreadable state is a note,
    not an error. Said out loud, because the indexes printed would then be
    wrong: the real ones continue from whatever the deployment is already using.
    """
    try:
        load_deployment_state(manifest.deployment)
    except RuntimeError:
        return None, (
            f"NB no state for deployment '{manifest.deployment}' — the indexes "
            f"above start at 1. The real ones continue from the deployment's "
            f"own clients, and `clients up` refuses until that state exists."
        )
    return highest_client_indexes(manifest.deployment, exclude=manifest.name), None


def _print_vm_plan(console: Console, manifest, base_indexes, note: str | None) -> None:
    vms = planned_client_vms(manifest, base_indexes)
    console.print(
        f"[bold]Client extension '{manifest.name}' of "
        f"{manifest.deployment}[/bold] — {len(vms)} VM(s):"
    )
    for vm in vms:
        console.print(
            f"  [cyan]{vm['hostname']}[/cyan]  "
            f"{vm['provider']}:{vm['region']} ({vm['vm_size']}) "
            f"role={vm['role']} client_index={vm['client_index']}"
        )
    if note:
        console.print(f"  [yellow]{note}[/yellow]")
    console.print()


def _plan_label(manifest) -> str:
    return f"{manifest.name} (extension of {manifest.deployment})"


def cmd_clients_plan(args):
    """Print the VMs and the command sequence ``clients up`` would run."""
    console = Console()
    manifest = _load_clients_manifest(args, console)
    base_indexes, note = _base_indexes_or_note(manifest)
    _print_vm_plan(console, manifest, base_indexes, note)
    _print_plan(
        console, _plan_label(manifest), manifest.kind, build_deploy_steps(manifest), "Deploy"
    )


def cmd_clients_up(args):
    """Bring a client extension up: infra → provision → start.

    Nothing here checks whether the extension's own state already exists.
    ``clients infra`` compares the recorded spec against the requested one and
    refuses on a mismatch, which is the only check that can tell a legitimate
    resume from a second manifest reusing the name — a name check here would
    refuse the resume this command is built around.
    """
    console = Console()
    manifest = _load_clients_manifest(args, console)
    steps = build_deploy_steps(manifest)
    if getattr(args, "dry_run", False):
        base_indexes, note = _base_indexes_or_note(manifest)
        _print_vm_plan(console, manifest, base_indexes, note)
        _print_plan(console, _plan_label(manifest), manifest.kind, steps, "Deploy")
        return

    _require_deployment_state(manifest, console)

    journal_key = journal_name(manifest.deployment, manifest.name)
    journal, resume_index = _resolve_step_journal(
        console,
        _JournalIdentity(journal_key, manifest.kind),
        steps,
        getattr(args, "resume", False),
    )
    base_indexes = highest_client_indexes(manifest.deployment, exclude=manifest.name)
    _print_vm_plan(console, manifest, base_indexes, None)
    _print_plan(console, _plan_label(manifest), manifest.kind, steps, "Deploy", done=resume_index)
    console.print()
    _execute(console, steps, journal=journal, start=resume_index)
    delete_step_journal(journal_key)
    console.print(
        f"\n[bold green]Client extension '{manifest.name}' of "
        f"{manifest.deployment} is up.[/bold green]"
    )


def _destroy_steps_from_state(
    deployment: str,
    extension: str,
    console: Console,
    *,
    ssh_key: str | None,
    skip_drain_failures: bool,
) -> list[Step] | None:
    """Destroy steps for an extension whose manifest is not to hand.

    Returns ``None`` when there is nothing to destroy (already reported). The
    SSH key comes from the extension's own state, recorded at provision time,
    for the same reason ``clients destroy`` reads it from there: the key the VMs
    were provisioned with is a fact about them, not something to re-type.
    """
    try:
        state = load_client_state(deployment, extension)
    except RuntimeError:
        console.print(
            f"[yellow]No state for extension '{extension}' of '{deployment}'. "
            f"Nothing to destroy.[/yellow]"
        )
        return None

    key = ssh_key or state.get("ssh_key_path") or "~/.ssh/id_rsa"
    destroy_argv = [
        "clients",
        "destroy",
        "--name",
        deployment,
        "--extension",
        extension,
        "--force",
        "--ssh-key-path",
        key,
    ]
    if skip_drain_failures:
        destroy_argv.append("--skip-drain-failures")
    return [
        Step(
            [
                "clients",
                "stop",
                "--name",
                deployment,
                "--extension",
                extension,
                "--ssh-key-path",
                key,
            ],
            f"Stop client services for extension '{extension}'",
            best_effort=True,
        ),
        Step(destroy_argv, f"Destroy client VMs for extension '{extension}'"),
    ]


def cmd_clients_down(args):
    """Tear a client extension down: stop services → drain wallets → destroy VMs."""
    console = Console()
    skip_drain_failures = getattr(args, "skip_drain_failures", False)

    if getattr(args, "config", None):
        manifest = _load_clients_manifest(args, console)
        deployment, extension = manifest.deployment, manifest.name
        steps = build_destroy_steps(manifest, skip_drain_failures=skip_drain_failures)
    elif getattr(args, "name", None) and getattr(args, "extension", None):
        deployment, extension = args.name, args.extension
        try:
            validate_extension_name(extension)
        except ExtensionError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(2)
        from_state = _destroy_steps_from_state(
            deployment,
            extension,
            console,
            ssh_key=getattr(args, "ssh_key_path", None),
            skip_drain_failures=skip_drain_failures,
        )
        if from_state is None:
            return
        steps = from_state
    else:
        console.print(
            "[bold red]Error:[/bold red] provide --config <manifest>, or "
            "--name <deployment> --extension <name>."
        )
        sys.exit(2)

    label = f"{extension} (extension of {deployment})"
    _print_plan(console, label, "clients", steps, "Destroy")
    if getattr(args, "dry_run", False):
        return
    console.print()
    _execute(console, steps)
    delete_step_journal(journal_name(deployment, extension))
    console.print(
        f"\n[bold green]Client extension '{extension}' of {deployment} "
        f"has been destroyed.[/bold green]"
    )
    console.print(
        f"[dim]s3://.../saorsa-deploy/addresses/{deployment}/ was left "
        f"untouched — the address pool belongs to the deployment.[/dim]"
    )


def cmd_clients_ls(args):
    """List a deployment's client extensions, with VM counts and state."""
    console = Console()
    names = list_client_extensions(args.name)
    if not names:
        console.print(f"No client extensions for '{args.name}'.")
        return

    console.print(f"[bold]Client extensions of {args.name}[/bold]")
    for name in names:
        try:
            state = load_client_state(args.name, name)
        except RuntimeError:
            console.print(f"  [yellow]{name}: state unreadable[/yellow]")
            continue
        clients = state.get("clients") or []
        up = [c for c in clients if c.get("ip")]
        roles: dict[str, int] = {}
        for client in clients:
            role = client.get("role", "upload")
            roles[role] = roles.get(role, 0) + 1
        breakdown = ", ".join(f"{count} {role}" for role, count in sorted(roles.items()))
        console.print(
            f"  [cyan]{name}[/cyan]: {len(up)}/{len(clients)} VM(s) up"
            f"{f' — {breakdown}' if breakdown else ''}"
        )
        console.print(
            f"    destroy: saorsa-deploy clients down --name {args.name} --extension {name}"
        )


def cmd_clients_config_validate(args):
    """Validate a ``kind: clients`` manifest; exit 2 when it is invalid."""
    console = Console()
    manifest = _load_clients_manifest(args, console)
    vms = planned_client_vms(manifest)
    console.print(
        f"[bold green]OK[/bold green] — '{args.config}' is a valid client "
        f"extension manifest: '{manifest.name}' adds {len(vms)} client VM(s) "
        f"to [bold]{manifest.deployment}[/bold]."
    )


def cmd_clients_config_show(args):
    """Print the fully-merged, validated client-extension manifest as YAML."""
    import yaml

    manifest = _load_clients_manifest(args, Console())
    sys.stdout.write(
        yaml.safe_dump(
            manifest.model_dump(exclude_none=True), sort_keys=False, default_flow_style=False
        )
    )
