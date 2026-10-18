"""``clients fetch-browser-traces`` — archive the browser cohort's per-dial traces.

Each browser download leaves a JSONL trace at
``/var/lib/ant-browser-downloader/trace/<round_id>-<instance>.jsonl``: a header
line naming the round and the address, then one `setBrowserTrace` record per
dial, chunk read and discovery. The harness never deletes them, because they ARE
the dataset this role produces — but they live on a VM that is destroyed at the
end of the run, and the three derived fields on the Influx point are summaries,
not the evidence.

So this command copies them off every browser VM (the deployment's own tier and
each `kind: clients` extension's), keeps a local copy under
``~/.autonomi/testnets/<deployment>/browser-traces/<hostname>/``, uploads each to
S3, and points the recorded round at its archived trace.

Three choices worth knowing:

* **It exits 2 when any VM is unreachable.** The command is run while the VMs
  are up, before `clients down`, and a partial archive that exited 0 would be
  indistinguishable from a complete one — after teardown there is no way to tell
  and no way to go back.
* **It never creates a row.** A trace whose round has not been recorded yet
  leaves ``trace_s3_key`` unset and is reported as unmatched; the next
  `record-results` writes the row, and the upsert PRESERVES an existing key, so
  re-running this command afterwards fills it in. Doing it the other way round
  would mean inventing a row with no status, no duration and no first reads.
* **The VMs come from the S3 client state, not the local inventory.** A browser
  cohort is normally a `kind: clients` extension added hours after `up` wrote
  the inventory, so the inventory would not name its VMs at all. The client
  state is what `generate-inventory` itself reads.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import boto3
from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.hosts import SSH_BASE_OPTS
from saorsa_deploy.browser_records import (
    client_hostname,
    client_tiers_with_extensions,
)
from saorsa_deploy.client_roles import is_browser
from saorsa_deploy.provisioning.browser_client import BROWSER_CLIENT_TRACE_DIR
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import S3_ADDRESSES_BUCKET, S3_REGION

#: Where the archived traces live. The public-read addresses bucket, not the
#: tfstate one: a trace holds testnet chunk addresses and peer ids, which is the
#: same class of content the address lists in that bucket already carry, and the
#: tfstate bucket must keep Block Public Access intact.
S3_RESULTS_PREFIX = "saorsa-deploy/results"

#: ``<round_id>-<instance>.jsonl``, which is exactly how the harness names them.
#: Anything else in the directory is ignored rather than uploaded under a
#: guessed key — the file name is how a trace is matched to its recorded round.
TRACE_NAME_RE = re.compile(r"^(\d+)-(\d+)\.jsonl$")


def traces_prefix(deployment: str, hostname: str) -> str:
    return f"{S3_RESULTS_PREFIX}/{deployment}/browser-traces/{hostname}"


def trace_s3_key(deployment: str, hostname: str, round_id: int, instance: int) -> str:
    return f"{traces_prefix(deployment, hostname)}/{round_id}-{instance}.jsonl"


def local_traces_dir(deployment: str, hostname: str) -> Path:
    from saorsa_deploy.local_inventory import default_inventory_path

    # Derived from the inventory path so the two always sit side by side, and so
    # the test suite's redirection of that root covers this too.
    return default_inventory_path(deployment).parent / "browser-traces" / hostname


def browser_vms(deployment: str) -> list[dict]:
    """Every browser VM of ``deployment``, with its address and extension."""
    vms: list[dict] = []
    for extension, state in client_tiers_with_extensions(deployment):
        for client in state.get("clients") or []:
            if not is_browser(client.get("role", "")):
                continue
            vms.append(
                {
                    "hostname": client_hostname(deployment, extension, client),
                    "extension": extension,
                    "ip": client.get("ip") or "",
                    "region": client.get("region"),
                }
            )
    vms.sort(key=lambda v: v["hostname"])
    return vms


def parse_trace_name(name: str) -> tuple[int, int] | None:
    """``"4345-2.jsonl"`` → ``(4345, 2)``, or None for anything else."""
    match = TRACE_NAME_RE.match(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _scp_traces(vm: dict, ssh_key: str, ssh_user: str, target: Path) -> None:
    """Copy every trace off one VM. Raises RuntimeError on any scp failure."""
    target.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "scp",
            "-i",
            os.path.expanduser(ssh_key),
            *SSH_BASE_OPTS,
            # Recursive is wrong here (it would nest a `trace/` directory per
            # re-run); the glob is expanded by the REMOTE shell, so a host with
            # no traces yet fails with "No such file" and is reported as such
            # rather than silently archiving nothing.
            f"{ssh_user}@{vm['ip']}:{BROWSER_CLIENT_TRACE_DIR}/*.jsonl",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"scp exited {result.returncode}")


def cmd_fetch_browser_traces(args) -> None:
    console = Console()
    deployment = args.name

    vms = browser_vms(deployment)
    if not vms:
        console.print(
            f"[yellow]'{deployment}' has no browser-downloader VMs in its "
            f"client state (its own tier or any `kind: clients` extension), so "
            f"there are no traces to fetch.[/yellow]"
        )
        return

    repo = get_repository()
    if repo is None:
        # Not fatal: the traces are the evidence and getting them off the VM
        # before it is destroyed matters more than the pointer to them. Said
        # loudly, because the pointer is what makes one findable later.
        console.print(
            "[yellow]Warning: DATABASE_URL is not set, so the archived traces "
            "will not be linked to their recorded rounds (trace_s3_key stays "
            "NULL). The files are still fetched and uploaded.[/yellow]"
        )
        deployment_row = None
    else:
        deployment_row = repo.get_active_by_name(deployment) or repo.get_by_name(deployment)
        if deployment_row is None:
            console.print(
                f"[yellow]Warning: '{deployment}' is not in the registry, so "
                f"the archived traces will not be linked to any round.[/yellow]"
            )

    s3 = boto3.client("s3", region_name=S3_REGION)
    table = Table(title=f"Browser traces — {deployment}")
    table.add_column("VM")
    table.add_column("files", justify="right")
    table.add_column("bytes", justify="right")
    table.add_column("linked", justify="right")
    table.add_column("unmatched", justify="right")

    unreachable: list[tuple[str, str]] = []
    unmatched_names: list[str] = []
    for vm in vms:
        hostname = vm["hostname"]
        if not vm["ip"]:
            unreachable.append((hostname, "no IP in client state"))
            table.add_row(hostname, "-", "-", "-", "-")
            continue
        target = local_traces_dir(deployment, hostname)
        try:
            _scp_traces(vm, args.ssh_key_path, getattr(args, "ssh_user", "root"), target)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            unreachable.append((hostname, str(e)))
            table.add_row(hostname, "-", "-", "-", "-")
            continue

        files = 0
        total_bytes = 0
        linked = 0
        unmatched = 0
        for path in sorted(target.glob("*.jsonl")):
            parsed = parse_trace_name(path.name)
            if parsed is None:
                continue
            round_id, instance = parsed
            files += 1
            total_bytes += path.stat().st_size
            key = trace_s3_key(deployment, hostname, round_id, instance)
            # Unconditional PUT, so a re-run after more rounds have accumulated
            # replaces each object with the file it has now. The trace of a
            # completed round never changes, so re-uploading it is a no-op in
            # content and the command stays idempotent.
            s3.upload_file(str(path), S3_ADDRESSES_BUCKET, key)
            if deployment_row is None:
                continue
            if repo.set_browser_download_trace_key(
                deployment_row.id, hostname, round_id, instance, key
            ):
                linked += 1
            else:
                unmatched += 1
                unmatched_names.append(f"{hostname}/{path.name}")
        table.add_row(
            hostname,
            f"{files:,}",
            f"{total_bytes:,}",
            f"{linked:,}" if deployment_row is not None else "-",
            f"{unmatched:,}" if deployment_row is not None else "-",
        )

    console.print(table)
    console.print(f"[dim]Local copies: {local_traces_dir(deployment, '<hostname>')}[/dim]")
    console.print(
        f"[dim]Archived to s3://{S3_ADDRESSES_BUCKET}/"
        f"{traces_prefix(deployment, '<hostname>')}/[/dim]"
    )

    if unmatched_names:
        console.print(
            f"[yellow]{len(unmatched_names)} trace(s) match no recorded round "
            f"— their `browser_download` point has not been recorded yet. Run "
            f"`deployments record-results --name {deployment}` and then this "
            f"command again; the upsert preserves a trace_s3_key that is "
            f"already set.[/yellow]"
        )
        for name in unmatched_names[:10]:
            console.print(f"  [yellow]- {name}[/yellow]")
        if len(unmatched_names) > 10:
            console.print(f"  [dim]... and {len(unmatched_names) - 10} more[/dim]")

    if unreachable:
        console.print()
        console.print(
            f"[bold red]Error:[/bold red] {len(unreachable)} browser VM(s) could "
            f"not be reached, so this archive is INCOMPLETE:"
        )
        for hostname, reason in unreachable:
            console.print(f"  [red]- {hostname}: {reason}[/red]")
        console.print(
            "[dim]Fix and re-run before `clients down`: the traces are deleted "
            "with the VM and nothing else holds them.[/dim]"
        )
        sys.exit(2)
