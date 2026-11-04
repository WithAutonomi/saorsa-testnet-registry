"""Redeploy a running prod client deployment's host-side service script.

There was no way to ship a changed ``prod_downloader.py`` to PROD-DL-01.
``prod continuous-downloader`` has only ``deploy`` and ``destroy``, and
``deploy`` calls ``record_deployment_created`` unconditionally -- so a redeploy
starts a fresh registry row and breaks the continuous series the deployment
exists to produce. ``deployments upgrade-wasm-client`` does redeploy the same
script, but refuses ``client_kind: native``, because its real job is to replace
a WASM bundle.

So this command does the one half of that which every transport needs: write the
script from this package's resources and restart the unit. No Terraform, no
registry row, no bundle (INFRA-484).

Scoped to the two *continuous* kinds, exactly as ``upgrade-wasm-client`` is: the
one-shot kinds are rewritten by every ``up``, and a testnet's client tier dies
with its testnet.

**The script is not the only thing a kind's VM needs to run it.** A
``prod-continuous-uploader`` executes under ``/opt/ant-registry-client`` and
writes Postgres directly, so a script that calls a new repository argument needs
that venv's ``saorsa-db`` wheel and the registry's own schema to have moved
first. Both are checked or delivered here -- see ``KINDS_WITH_REGISTRY_CLIENT``.
"""

from __future__ import annotations

import sys

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.cmd.upgrade_wasm_client import SCRIPT_BY_KIND
from saorsa_deploy.prod_client_units import group_clients_by_unit
from saorsa_deploy.provisioning.service_script import (
    build_service_script_commands,
)
from saorsa_deploy.registry import get_repository, record_event
from saorsa_deploy.registry_client_upgrade import (
    add_install_ops,
    needs_registry_client,
    schema_problem,
    wheel_for_kinds,
)
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state

#: The kinds this command covers, and the set is also its scope: a kind absent
#: from `SCRIPT_BY_KIND` has no script to push and is refused by name.
SUPPORTED_KINDS = tuple(sorted(SCRIPT_BY_KIND))


def _deploy_args(deployment) -> dict:
    args = getattr(deployment, "deploy_args", None)
    return args if isinstance(args, dict) else {}


def _resolve_kind(deployment) -> str:
    return _deploy_args(deployment).get("kind") or ""


def _daily_at(deployment) -> str | None:
    """The deployment's own ``daily_at``, for the timing warning.

    Named rather than generalised to a caution about "the daily window":
    PROD-WDL-01 runs at 02:00 and the other three at 00:00, and an operator
    reading a generic warning has to go and look it up.
    """
    value = _deploy_args(deployment).get("daily_at")
    return str(value) if value else None


def _target_groups(kind: str, clients: list[dict]):
    """``(unit, ips)`` for the hosts that run the script this kind ships.

    `prod-continuous-uploader` is the multi-role kind and its two roles run
    DIFFERENT scripts from DIFFERENT directories: the uploader VM runs
    `prod_continuous_uploader.py` under `/root/ant-prod-continuous-uploader`,
    each verifier runs `prod_upload_verifier.py` under
    `/root/ant-prod-upload-verifier`. Grouping by unit and then writing one
    script per kind sent the uploader's script to a verifier and restarted
    `ant-prod-upload-verifier`: the target directory does not exist there, so
    the write failed and the run reported a partly-upgraded deployment -- and
    had the directory existed, the verifier's own script would have been left
    untouched while the command claimed to have replaced it.

    The unit carries its own script, so the match is derived rather than
    hand-written: a unit whose script is not the one being deployed is not a
    host for this upgrade. INFRA-485 states the same rule from the other side --
    a verifier host appearing in this command's output is a bug in it.

    Returns ``(targets, skipped)``, because a host left alone on purpose still
    has to be reported as left alone.
    """
    wanted = SCRIPT_BY_KIND[kind]
    targets, skipped = [], []
    for unit, ips in group_clients_by_unit(kind, clients):
        (targets if unit.script == wanted else skipped).append((unit, ips))
    return targets, skipped


def cmd_upgrade_service_script(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
            "registry is unreachable. The deployment's kind -- which decides "
            "both the script and the unit -- comes from the registry, so this "
            "cannot run without it."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
    if deployment is None:
        console.print(
            f"[bold red]Error:[/bold red] no deployment named '{args.name}' in the registry."
        )
        sys.exit(1)

    kind = _resolve_kind(deployment)
    if kind not in SCRIPT_BY_KIND:
        console.print(
            f"[bold red]Error:[/bold red] '{args.name}' is a "
            f"'{kind or 'unknown'}' deployment. Only "
            f"{', '.join(SUPPORTED_KINDS)} carry a service script that can be "
            "replaced in place; the one-shot kinds are rewritten by every "
            "`deployments up`."
        )
        sys.exit(1)

    try:
        clients = load_client_state(args.name).get("clients") or []
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/bold red] {args.name}: {exc}")
        sys.exit(1)

    groups, skipped = _target_groups(kind, clients)
    if not groups:
        console.print(
            f"[bold red]Error:[/bold red] '{args.name}' has no client VM "
            f"running {SCRIPT_BY_KIND[kind][1]} with an IP in its S3 client "
            "state."
        )
        sys.exit(1)

    # Everything that can refuse, refuses before a single host is touched: a
    # half-upgraded forever-running deployment is worse than one still running
    # the old script, and both of these are the operator's step, not ours.
    problem = schema_problem(repo, kind)
    if problem:
        console.print(f"[bold red]Error:[/bold red] {problem}")
        sys.exit(1)
    wheel, problem = wheel_for_kinds([kind])
    if problem:
        console.print(f"[bold red]Error:[/bold red] {problem}")
        sys.exit(1)
    if wheel is not None:
        console.print(f"  Built the VM's saorsa-db wheel: {wheel.name}")

    scripts_dir, script_name = SCRIPT_BY_KIND[kind]
    total_hosts = sum(len(ips) for _, ips in groups)
    console.print(
        f"[bold]{args.name}[/bold] ({kind}): installing {script_name} on "
        f"{total_hosts} host(s) across {len(groups)} unit(s)"
    )
    for unit, ips in skipped:
        # Said out loud: these hosts run a different script, so leaving them
        # alone is the correct outcome and not a host that went missing.
        console.print(
            f"  [dim]leaving {len(ips)} {unit.label} host(s) alone — they run "
            f"{unit.script[1] if unit.script else 'another script'} under "
            f"{unit.service}, which this upgrade does not replace[/dim]"
        )

    if args.restart:
        anchor = _daily_at(deployment)
        when = f"{anchor} UTC" if anchor else "its daily anchor (not recorded)"
        console.print(
            f"[yellow]Warning:[/yellow] the restart below is a restart of a "
            f"deployment whose cycle is anchored to {when}. Run it outside "
            f"that window. On an uploader a restart mid-cycle invokes the "
            f"claim-resolution handler, which resolves the batch in flight and "
            f"releases the claims it never started; on a downloader it abandons "
            f"the cycle, which is then re-run at the next anchor."
        )

    all_ips = [ip for _, ips in groups for ip in ips]
    clear_known_hosts(all_ips, console)

    failures: list[str] = []
    for unit, ips in groups:
        console.print()
        action = f"and restarting {unit.service}" if args.restart else "(no restart)"
        console.print(f"  [bold]{unit.label}[/bold] — {len(ips)} host(s), {action}")
        hosts_data = [(ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in ips]
        state = State(inventory=Inventory((hosts_data, {})), config=Config())
        commands = list(build_service_script_commands(scripts_dir, script_name))
        if args.restart:
            commands.append(f"systemctl restart {unit.service}")
        try:
            connect_all(state)
            if wheel is not None:
                add_install_ops(state, wheel, console)
            add_op(
                state,
                server.shell,
                name=f"Install {script_name} and restart {unit.service}",
                commands=commands,
            )
            run_ops(state)
        except Exception as exc:  # noqa: BLE001 — one group must not stop the rest
            console.print(f"    [red]Failed: {exc}[/red]")
            failures.extend(ips)
            continue
        finally:
            try:
                disconnect_all(state)
            except Exception:  # noqa: BLE001
                pass
        # One unreachable host must not abandon the others: a two-role
        # deployment half-upgraded with a record of which half is better than
        # one nobody can tell apart.
        failed = [host.name for host in (state.failed_hosts or set())]
        if failed:
            console.print(f"    [red]Failed on {', '.join(failed)}[/red]")
            failures.extend(failed)

    console.print()
    console.rule("[bold]Service script upgrade[/bold]")
    if failures:
        console.print(f"  [red]{args.name}: FAILED on {', '.join(sorted(set(failures)))}[/red]")
        record_event(
            args.name,
            "service_script_upgrade_failed",
            {
                "script": script_name,
                "kind": kind,
                "failed_hosts": sorted(set(failures)),
                "restarted": bool(args.restart),
                "saorsa_db_wheel": wheel.name if wheel else None,
            },
        )
        sys.exit(1)

    console.print(
        f"  [green]{args.name}[/green]: {script_name} installed on "
        f"{total_hosts} host(s)"
        + (" and services restarted" if args.restart else " (not restarted)")
    )
    record_event(
        args.name,
        "service_script_upgraded",
        {
            "script": script_name,
            "kind": kind,
            "hosts": total_hosts,
            "units": [unit.service for unit, _ in groups],
            "restarted": bool(args.restart),
            # Which VM-side package went with it, so a deployment whose script
            # and wheel came from different releases is visible afterwards.
            "saorsa_db_wheel": wheel.name if wheel else None,
            "registry_client": needs_registry_client(kind),
        },
    )
    if not args.restart:
        console.print(
            "  [yellow]The new script is on disk but the running services are "
            "still executing the old one. Restart them when the window "
            "suits.[/yellow]"
        )
