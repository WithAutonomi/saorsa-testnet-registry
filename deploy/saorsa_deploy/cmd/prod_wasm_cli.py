"""The console-facing half of the production WASM client foundation.

:mod:`saorsa_deploy.prod_wasm` holds the logic; this holds the argument checks,
the messages and the exit codes, so the download kinds (and the upload kinds
that follow) share one set of both. Everything here runs BEFORE any
infrastructure is created: a browser client that cannot reach a bootstrap, or
sits on a box too small to hold the transfer, is a VM that bills from creation
and fails every single operation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from rich.console import Console

from saorsa_deploy.binary_source import (
    check_ant_wasm_build_exists,
    fetch_ant_wasm_build_info,
    get_ant_wasm_build_url,
)
from saorsa_deploy.prod_wasm import (
    DEFAULT_WASM_VM_SIZE,
    WasmBootstrapError,
    bootstrap_deploy_args,
    check_wasm_vm_size,
    default_wasm_vm_size,
    resolve_bootstrap_multiaddrs,
    wasm_vm_size_warning,
)

#: CLI flags that have no browser-path meaning. `--bootstrap-cache` selects
#: between ant's peer cache and an explicit `--bootstrap`, and a WASM client does
#: neither; the other two act on the `ant` process, which never runs. Refused
#: rather than ignored — a silently-dropped flag leaves the operator reading
#: their own command line as a description of the run.
FORBIDDEN_WASM_FLAGS = (
    ("memory_profiling", "--memory-profiling"),
    ("download_diagnostics", "--download-diagnostics"),
)


@dataclass
class WasmContext:
    """Everything a WASM deployment needs that a native one does not."""

    bundle_url: str
    bootstrap_multiaddrs: list[str]
    #: ``deploy_args.wasm_bootstrap`` — the source, and what the probe found.
    bootstrap_record: dict
    #: ``deploy_args.wasm_build`` — the build's provenance, or None.
    build_info: dict | None = None
    warnings: list[str] = field(default_factory=list)


def is_wasm_kind(args) -> bool:
    return getattr(args, "client_kind", "native") == "wasm"


def check_wasm_flags(args, console: Console) -> None:
    """Refuse the flags that cannot mean anything on the browser path."""
    if not is_wasm_kind(args):
        return
    offending = [flag for attr, flag in FORBIDDEN_WASM_FLAGS if getattr(args, attr, False)]
    # --no-bootstrap-cache is the only way to say something with this flag on the
    # WASM path, and it still says nothing: check the explicit False too.
    if getattr(args, "bootstrap_cache", True) is False:
        offending.append("--no-bootstrap-cache")
    if not offending:
        return
    console.print(
        f"[bold red]Error:[/bold red] {', '.join(offending)} cannot be used "
        "with --client-kind wasm. The peer cache has no browser-path meaning, "
        "and heaptrack/--download-diagnostics act on the `ant` process, which a "
        "WASM client never runs."
    )
    sys.exit(1)


def apply_wasm_vm_size(args, console: Console) -> None:
    """Default and check the client VM size for a WASM deployment.

    A browser-path download holds ~4x the file in WASM memory (900 MB measured
    at 3.9 GB RSS), so the default is four times the native downloader's box. A
    DigitalOcean slug encodes its memory, so one below the floor is a refusal;
    other providers' names cannot be read here, so those only warn.
    """
    if not is_wasm_kind(args):
        return
    provider = getattr(args, "provider", None) or "digitalocean"
    spec = args.spec
    region, _, vm_size = spec.partition(":")
    if not vm_size:
        default = default_wasm_vm_size(provider)
        if default:
            args.spec = f"{region}:{default}"
            console.print(
                f"  WASM client VM size defaulted to {default} "
                f"(a browser-path transfer holds ~4x the file in memory)."
            )
        return

    problem = check_wasm_vm_size(vm_size, provider)
    if problem:
        console.print(
            f"[bold red]Error:[/bold red] {problem}. Pass a larger size in "
            f"--spec (e.g. {region}:{DEFAULT_WASM_VM_SIZE})."
        )
        sys.exit(1)
    warning = wasm_vm_size_warning(vm_size, provider)
    if warning:
        console.print(f"[yellow]Warning:[/yellow] {warning}.")


def resolve_wasm_context(args, console: Console) -> WasmContext | None:
    """Resolve the bundle, probe the bootstraps, read the build provenance.

    Returns None for a native deployment, so every caller can use one code path.
    Exits non-zero when the deployment could not possibly work: no bundle to
    install, or no bootstrap that answers.
    """
    if not is_wasm_kind(args):
        return None

    branch = getattr(args, "wasm_branch_name", None)
    if not branch:
        console.print(
            "[bold red]Error:[/bold red] --client-kind wasm requires "
            "--wasm-branch-name (a branch OR a tag in the ant-client repo). "
            "The bundle is built from source by `build ant-wasm` — no WASM "
            "package is published as a release."
        )
        sys.exit(1)
    repo_owner = getattr(args, "wasm_repo_owner", None) or "maidsafe"

    bundle_url = get_ant_wasm_build_url(args.name, repo_owner, branch)
    warnings: list[str] = []
    try:
        exists = check_ant_wasm_build_exists(args.name, repo_owner, branch)
    except Exception:
        # A credential or network problem checking S3 is not evidence the bundle
        # is missing, and the install would fail loudly on the VM anyway.
        exists = True
        warnings.append("could not verify the WASM bundle exists in S3")
    if not exists:
        console.print(
            f"[bold red]Error:[/bold red] no WASM bundle for '{args.name}' at "
            f"{repo_owner}/{branch}. Run `saorsa-deploy build ant-wasm --name "
            f"{args.name} --branch-name {branch} --repo-owner {repo_owner}` "
            "first (`deployments up` emits that step for you)."
        )
        sys.exit(1)

    console.print("[bold]Probing production bootstrap WebRTC listeners...[/bold]")
    try:
        multiaddrs, probe_result, source = resolve_bootstrap_multiaddrs(
            getattr(args, "wasm_bootstrap_multiaddrs_file", None),
            skip_probe=getattr(args, "skip_bootstrap_probe", False),
        )
    except WasmBootstrapError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    except OSError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if probe_result.skipped:
        console.print(
            f"  {probe_result.total} bootstrap address(es) from {source}; "
            "probe skipped (--skip-bootstrap-probe)."
        )
    elif probe_result.all_answered:
        console.print(
            f"  {probe_result.answered}/{probe_result.total} answered (source: {source})."
        )
    else:
        console.print(
            f"[yellow]Warning:[/yellow] only "
            f"{probe_result.answered}/{probe_result.total} bootstrap "
            f"address(es) answered. ant-core cools a failed endpoint down for "
            f"30 minutes and moves on, so the run is viable, but read its "
            f"results in that light. Not answering:"
        )
        for dead in probe_result.dead:
            console.print(f"    [yellow]{dead}[/yellow]")
        warnings.append(
            f"{probe_result.total - probe_result.answered} bootstrap address(es) did not answer"
        )

    build_info = fetch_ant_wasm_build_info(args.name, repo_owner, branch)
    if build_info is None:
        console.print(
            "[yellow]Warning:[/yellow] the WASM bundle has no build-info "
            "sidecar, so its ant-client commit is unknown (bundles built before "
            "provenance was recorded). Rebuild with `build ant-wasm` to get it."
        )
        warnings.append("no WASM build provenance recorded")
    else:
        console.print(
            f"  Bundle built from {build_info.get('repo_owner')}/"
            f"{build_info.get('ref')} at {build_info.get('commit', '?')[:12]} "
            f"(node-datachannel {build_info.get('node_datachannel_version')})."
        )

    return WasmContext(
        bundle_url=bundle_url,
        bootstrap_multiaddrs=multiaddrs,
        bootstrap_record=bootstrap_deploy_args(probe_result, source),
        build_info=build_info,
        warnings=warnings,
    )


def wasm_deploy_args(context: WasmContext | None) -> dict:
    """The WASM keys a deployment's ``deploy_args`` carries.

    Always includes ``client_kind`` so a row says which transport it ran even
    when that is the default — a missing key reads as "nobody thought about it".
    """
    if context is None:
        return {"client_kind": "native"}
    return {
        "client_kind": "wasm",
        "wasm_build": context.build_info,
        "wasm_bootstrap": context.bootstrap_record,
    }
