"""Replace the WASM client bundle in place on running prod deployments.

PROD-WUL-01 (`prod-continuous-uploader`) and PROD-WDL-01
(`prod-continuous-downloader`) run permanently against production over the
browser path, and the point of them is a *series*: what the browser client can
do against production today, next to what it could do last week. Iterating on
the client means changing the binary underneath that series without breaking
it, and nothing here could do that:

* `deployments upgrade-ant-binary` covers the native `ant` binary only.
* Re-running `deployments up` calls `record_deployment_created` again, which
  risks a fresh registry row — a new series, not a continuation of the old one.

So this command builds the bundle ONCE and installs that same bundle on every
named deployment's WASM client VM, restarting each service. No Terraform, no
registry row, no manifest.

Deliberately scoped to the two *continuous* kinds. The one-shot kinds
(`prod-downloaders`, `prod-uploaders`) are rebuilt by every `up`, so there is
nothing to upgrade in place; the testnet WASM tier is torn down with its
testnet.

Timing is the operator's: run it outside the daily window. A restart that
triggers a same-day run is acceptable — the uploader claims fresh files and the
downloader re-walks its list — but it is a cycle, and it costs what a cycle
costs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import yaml
from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_source import (
    fetch_ant_wasm_build_info,
    get_ant_wasm_build_url,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import UPLOADER_SERVICE
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as PROD_DOWNLOADER_SERVICE_NAME,
)
from saorsa_deploy.provisioning.wasm_client import (
    build_ant_wasm_cli_command,
    build_bundle_install_command,
)
from saorsa_deploy.registry import (
    get_repository,
    record_deploy_args_update,
    record_event,
)
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state

#: The systemd unit each supported kind runs its WASM client under. The two
#: kinds are named here rather than derived, because the set is also the
#: command's scope: a kind absent from this map is refused before anything is
#: built.
SERVICE_BY_KIND = {
    "prod-continuous-downloader": PROD_DOWNLOADER_SERVICE_NAME,
    "prod-continuous-uploader": UPLOADER_SERVICE,
}

#: Where a deployment's run manifest is looked for, to warn that the next `up`
#: would roll the upgrade back. Relative to the repository root.
RUNS_MANIFEST_DIR = Path(__file__).resolve().parents[2] / "manifests" / "runs"


@dataclass
class UpgradeTarget:
    """One deployment whose WASM client is to be replaced."""

    name: str
    kind: str
    service: str
    host_ips: list[str]
    #: ``deploy_args.wasm_build.commit`` before the upgrade, or None.
    old_commit: str | None
    #: ``wasm_binary.branch`` in the local run manifest, if one names this
    #: deployment. Only used for the drift warning.
    manifest_ref: str | None = None
    failed_hosts: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failed_hosts


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    return deploy_args.get("kind") or ""


def _client_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return "native"
    return deploy_args.get("client_kind") or "native"


def _wasm_build_commit(deploy_args) -> str | None:
    if not isinstance(deploy_args, dict):
        return None
    build = deploy_args.get("wasm_build")
    if not isinstance(build, dict):
        return None
    return build.get("commit")


def _client_ips(name: str, kind: str) -> list[str]:
    """The VM(s) running the WASM client for this deployment.

    The continuous uploader's deployment also carries verifier VMs, and those
    are deliberately NATIVE: a digest mismatch they report has to be
    attributable to the upload rather than to a browser read path, so they must
    not be given a WASM bundle. Every other supported kind is single-role.
    """
    clients = load_client_state(name).get("clients") or []
    if kind == "prod-continuous-uploader":
        clients = [c for c in clients if c.get("role", "upload") == "upload"]
    return [c["ip"] for c in clients if c.get("ip")]


def _manifest_ref(name: str, kind: str) -> str | None:
    """``wasm_binary.branch`` from the local run manifest naming ``name``.

    Read as plain YAML rather than through the manifest loader: this only feeds
    a warning, and a manifest that no longer validates (a schema change, a
    half-edited file) must not be able to fail an upgrade of running
    infrastructure. Returns None when there is no manifest, or none that names
    a WASM bundle.
    """
    section = kind.replace("-", "_")
    try:
        paths = sorted(RUNS_MANIFEST_DIR.glob("*.y*ml"))
    except OSError:
        return None
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or data.get("name") != name:
            continue
        spec = data.get(section)
        if not isinstance(spec, dict):
            continue
        binary = spec.get("wasm_binary")
        if isinstance(binary, dict):
            return binary.get("branch")
    return None


def _resolve_targets(names: list[str], console: Console) -> list[UpgradeTarget]:
    """Turn the named deployments into targets, or refuse the whole run.

    Every check here happens BEFORE the build: a build is ~10 minutes on a VM
    that bills, and finding out afterwards that one of the names was a native
    deployment would have spent it for nothing. Problems are collected and
    reported together rather than one at a time, so a three-name invocation
    with two mistakes is fixed in one go.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
            "registry is unreachable. This command reads each deployment's "
            "kind and client_kind from the registry and records the upgrade "
            "there, so it cannot run without it."
        )
        sys.exit(1)

    targets: list[UpgradeTarget] = []
    problems: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)

        deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
        if deployment is None:
            problems.append(f"'{name}': no deployment with that name in the registry")
            continue

        kind = _resolve_kind(deployment.deploy_args)
        if kind not in SERVICE_BY_KIND:
            problems.append(
                f"'{name}' is a '{kind or 'unknown'}' deployment; only "
                f"{', '.join(sorted(SERVICE_BY_KIND))} can be upgraded in place"
            )
            continue

        client_kind = _client_kind(deployment.deploy_args)
        if client_kind != "wasm":
            problems.append(
                f"'{name}' runs the {client_kind} client; there is no WASM "
                "bundle on it to replace (use `deployments upgrade-ant-binary` "
                "for the native `ant` binary)"
            )
            continue

        try:
            host_ips = _client_ips(name, kind)
        except RuntimeError as exc:
            problems.append(f"'{name}': {exc}")
            continue
        if not host_ips:
            problems.append(f"'{name}': no client VM IP in its S3 client state")
            continue

        targets.append(
            UpgradeTarget(
                name=name,
                kind=kind,
                service=SERVICE_BY_KIND[kind],
                host_ips=host_ips,
                old_commit=_wasm_build_commit(deployment.deploy_args),
                manifest_ref=_manifest_ref(name, kind),
            )
        )

    if problems:
        console.print(
            "[bold red]Error:[/bold red] nothing was built or installed. "
            "`upgrade-wasm-client` supports "
            f"{', '.join(sorted(SERVICE_BY_KIND))} deployments running "
            "`client_kind: wasm`:"
        )
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
        sys.exit(1)
    return targets


def _warn_manifest_drift(targets: list[UpgradeTarget], ref: str, console: Console) -> None:
    """Say so when the local manifest still pins the old ref.

    The manifest is what the next `deployments up` would deploy, so a mismatch
    means this upgrade is one `up` away from being silently rolled back — and
    the series would then change binary again with nothing recording that it
    had.
    """
    for target in targets:
        if target.manifest_ref and target.manifest_ref != ref:
            console.print(
                f"[yellow]Warning:[/yellow] the run manifest for "
                f"'{target.name}' still pins wasm_binary.branch: "
                f"{target.manifest_ref}. The next `deployments up` would roll "
                f"this deployment back off {ref}. Update the manifest to match."
            )


def _build_bundle(args, first_name: str, console: Console) -> tuple[str, dict | None]:
    """Build the bundle ONCE and return ``(bundle_url, build_info)``.

    Under the first named deployment's name, because the S3 key is namespaced
    by name and the bundle has to live somewhere. One build for all targets is
    the point: installing the same bytes everywhere is what makes the
    deployments comparable afterwards, and a build per deployment would give
    each a bundle built from its own checkout of a branch that may have moved
    between them.
    """
    from saorsa_deploy.cmd.build_ant_wasm import cmd_build_ant_wasm

    console.print(
        f"[bold]Building the WASM client once, under '{first_name}', for every "
        f"named deployment...[/bold]"
    )
    cmd_build_ant_wasm(
        SimpleNamespace(
            name=first_name,
            branch_name=args.branch_name,
            repo_owner=args.repo_owner,
            ssh_key_path=args.ssh_key_path,
            build_vm_size=getattr(args, "build_vm_size", None),
        )
    )
    bundle_url = get_ant_wasm_build_url(first_name, args.repo_owner, args.branch_name)
    build_info = fetch_ant_wasm_build_info(first_name, args.repo_owner, args.branch_name)
    if build_info is None:
        console.print(
            "[yellow]Warning:[/yellow] the freshly-built bundle has no "
            "build-info sidecar, so its ant-client commit could not be read. "
            "The install will still happen, but nothing will record which "
            "commit is now running."
        )
    else:
        console.print(
            f"  Built {build_info.get('repo_owner')}/{build_info.get('ref')} "
            f"at {str(build_info.get('commit') or '?')[:12]} "
            f"(node-datachannel {build_info.get('node_datachannel_version')})."
        )
    return bundle_url, build_info


def _install_on_target(
    target: UpgradeTarget, bundle_url: str, ssh_key_path: str, console: Console
) -> None:
    """Install the bundle and restart the service on one deployment's VM(s).

    Failures are recorded on the target and never raised: a three-deployment
    invocation where the second VM is unreachable must still upgrade the third,
    because the alternative is a fleet left on two different builds with no
    record of which is which.
    """
    console.print()
    console.print(
        f"[bold]{target.name}[/bold] ({target.kind}): installing on "
        f"{len(target.host_ips)} host(s), then restarting {target.service}"
    )
    clear_known_hosts(target.host_ips, console)
    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": ssh_key_path}) for ip in target.host_ips]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    try:
        connect_all(state)
        add_op(
            state,
            server.shell,
            name=f"Upgrade the WASM client and restart {target.service}",
            commands=[
                # require_cli: a bundle built before `dist/cli.js` existed would
                # install cleanly and then fail every single transfer.
                build_bundle_install_command(bundle_url, require_cli=True),
                build_ant_wasm_cli_command(),
                f"systemctl restart {target.service}",
            ],
        )
        run_ops(state)
    except Exception as exc:  # noqa: BLE001 — one target must not stop the rest
        console.print(f"  [red]Failed: {exc}[/red]")
        target.failed_hosts = list(target.host_ips)
        return
    finally:
        try:
            disconnect_all(state)
        except Exception:  # noqa: BLE001
            pass

    target.failed_hosts = [host.name for host in (state.failed_hosts or set())]


def cmd_upgrade_wasm_client(args) -> None:
    console = Console()

    targets = _resolve_targets(list(args.name), console)
    _warn_manifest_drift(targets, args.branch_name, console)

    bundle_url, build_info = _build_bundle(args, targets[0].name, console)
    new_commit = (build_info or {}).get("commit")

    for target in targets:
        record_event(
            target.name,
            "wasm_client_upgrade_started",
            {
                "ref": args.branch_name,
                "repo_owner": args.repo_owner,
                "bundle_url": bundle_url,
            },
        )
        _install_on_target(target, bundle_url, args.ssh_key_path, console)

    console.print()
    console.rule("[bold]WASM client upgrade[/bold]")
    for target in targets:
        if not target.succeeded:
            console.print(
                f"  [red]{target.name}: FAILED on "
                f"{', '.join(target.failed_hosts)} — still on "
                f"{(target.old_commit or 'unknown')[:12]}[/red]"
            )
            record_event(
                target.name,
                "wasm_client_upgrade_failed",
                {
                    "ref": args.branch_name,
                    "repo_owner": args.repo_owner,
                    "failed_hosts": target.failed_hosts,
                },
            )
            continue

        # Recorded only on success, and only after the restart: `deploy_args`
        # is what every later reader treats as "the build this deployment is
        # running", so writing it for a VM that never took the bundle would
        # make the registry lie about live infrastructure.
        if build_info is not None:
            record_deploy_args_update(target.name, {"wasm_build": build_info})
        record_event(
            target.name,
            "wasm_client_upgraded",
            {
                "old_commit": target.old_commit,
                "new_commit": new_commit,
                "ref": args.branch_name,
                "repo_owner": args.repo_owner,
                "upgraded_at": _now_iso(),
            },
        )
        console.print(
            f"  [green]{target.name}[/green]: "
            f"{(target.old_commit or 'unknown')[:12]} -> "
            f"{(new_commit or 'unknown')[:12]} "
            f"({target.service} restarted)"
        )

    console.print()
    console.print(
        "Transfers from here on carry the new commit in their `client_build` "
        "field; `deployments print-recorded-results` splits its tables by it."
    )

    if any(not target.succeeded for target in targets):
        sys.exit(1)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
