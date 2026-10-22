"""Collect a production client deployment's forensic surface before teardown.

Everything that makes a failed client prod test diagnosable lives on its VMs —
the per-operation ``stdout`` holding each result JSON (``payment_sent``,
``error_kind``, ``client_build``), the ``stderr`` holding the progress, the
storage quotes, the peer sets and the error, the payment markers, and what
systemd saw — and ``deployments down`` destroys all of it. The per-operation
*records* are already in Postgres by then (``record-results``), so they need no
capture; these files are the only copy of anything else.

This was six ``ssh`` invocations in the ``run-client-prod-tests`` skill that an
operator substituted a name, an id, a unit and ONE IP into. Every one of those
substitutions is something the tool already knows, and two of them were wrong by
construction: the unit was picked from a comment, so picking the other one
produced an empty archive and a capture that *looked* done, and one IP cannot
cover a ``prod-uploaders`` run (repeatable ``--uploader``) or a
``prod-continuous-uploader`` (an uploader VM plus N verifiers on a different
unit). So the mapping comes from :mod:`saorsa_deploy.prod_client_units`, the
hosts come from S3 client state, and the verdict is an exit code rather than
three prose sentences for an unattended agent to adjudicate.

An unknown kind is refused rather than guessed at. ``upgrade-ant-binary`` falls
through to the downloader unit when it cannot resolve a kind, which is safe
there because installing a binary is idempotent and the restart simply fails;
here the same guess would write a confident, empty archive, which is the exact
failure this command exists to remove.
"""

import json
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.paths import state_dir
from saorsa_deploy.prod_client_units import (
    LOCAL_KINDS,
    PROD_CLIENT_UNITS,
    ProdClientUnit,
    UnknownProdClientKind,
    group_clients_by_unit,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.state import load_client_state

_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
]

#: What ssh(1) itself exits with when it could not run the command at all, as
#: against whatever exit code the command chose. `ls <logdir>/*.paid` exiting
#: non-zero is an answer — no payment went out — so the two have to be told
#: apart.
_SSH_FAILURE_EXIT = 255

#: The prod client VMs take the operator's `ant_deploy` key. Deliberately not
#: `~/.ssh/id_rsa`, which `generate-inventory` defaults to and which the skill
#: had to work around on every invocation.
DEFAULT_SSH_KEY_PATH = "~/.ssh/ant_deploy"

#: Captures go under the local state directory, so they survive the teardown
#: that is the whole point of taking them, and so `SAORSA_DEPLOY_STATE_DIR`
#: moves them with everything else saorsa-deploy writes locally.
FORENSICS_DIR_NAME = "client-test-forensics"

ARCHIVE_NAME = "logs.tar.gz"
SERVICE_FILE_NAME = "service.txt"
MANIFEST_FILE_NAME = "manifest.txt"
PAYMENT_MARKERS_FILE_NAME = "payment-markers.txt"
SUMMARY_FILE_NAME = "summary.json"

#: Archiving a whole log directory of a long run is the slow step here; the
#: text reads are a few hundred lines each.
_ARCHIVE_TIMEOUT_SECS = 900
_READ_TIMEOUT_SECS = 120


@dataclass
class HostCapture:
    """One VM's capture, and whether it can be relied on."""

    label: str
    unit: str
    ip: str
    directory: str
    files: dict[str, int] = field(default_factory=dict)
    archive_entries: int | None = None
    ok: bool = False
    reason: str | None = None


def _ssh_argv(ip: str, ssh_key: str, command: str) -> list[str]:
    return ["ssh", "-i", ssh_key, *_SSH_OPTS, f"root@{ip}", command]


def verify_archive(path: Path) -> tuple[bool, str | None, int | None]:
    """Whether ``path`` is an archive worth keeping, and how many entries it has.

    Done locally with :mod:`tarfile` rather than by shelling out to
    ``tar -tzf``: a truncated archive then raises here and is reported, instead
    of printing an error to a terminal nobody is reading and leaving the
    capture looking complete. Each distinguishable way of being useless gets
    its own reason, because they mean different things — nothing written at all
    is a local failure, a zero-byte file is an SSH that died before any output,
    and an archive with no entries is a log directory that was empty.
    """
    if not path.is_file():
        return False, f"{path.name} was not written", None
    if path.stat().st_size == 0:
        return False, f"{path.name} is zero bytes — nothing was transferred", None
    try:
        with tarfile.open(path, "r:gz") as archive:
            entries = sum(1 for _ in archive)
    except Exception as exc:  # noqa: BLE001 — any unreadable archive is a failure
        return False, f"{path.name} is not a readable gzip tar: {exc}", None
    if entries == 0:
        return False, f"{path.name} lists no entries — the log directory was empty", 0
    return True, None, entries


def _capture_archive(ip: str, ssh_key: str, unit: ProdClientUnit, dest: Path) -> str | None:
    """Stream the unit's whole log directory into ``dest``, or say why not.

    ``tar -cz -C /var/log <basename>`` keeps the directory name inside the
    archive, so an unpacked capture is self-describing. The bytes go straight to
    the file rather than through memory: a failed upload run's log directory is
    not bounded by anything.
    """
    basename = Path(unit.log_dir).name
    argv = _ssh_argv(ip, ssh_key, f"tar -cz -C {Path(unit.log_dir).parent} {basename}")
    try:
        with dest.open("wb") as handle:
            result = subprocess.run(
                argv,
                stdout=handle,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_ARCHIVE_TIMEOUT_SECS,
            )
    except subprocess.TimeoutExpired:
        return f"timed out after {_ARCHIVE_TIMEOUT_SECS}s archiving {unit.log_dir}"
    if result.returncode == _SSH_FAILURE_EXIT:
        return f"unreachable over SSH: {(result.stderr or '').strip() or 'ssh failed'}"
    if result.returncode != 0:
        return (
            f"tar failed on the host ({result.returncode}): "
            f"{(result.stderr or '').strip() or unit.log_dir}"
        )
    return None


def _capture_text(ip: str, ssh_key: str, commands: list[tuple[str, str]], dest: Path) -> None:
    """Write each ``(heading, command)``'s combined output into one local file.

    Never fails the host: a non-zero exit is frequently the answer rather than
    an error — ``ls <logdir>/*.paid`` exits non-zero precisely when no payment
    went out, which is the evidence that separates a released pool claim from a
    partial one — so the output and the exit code are both recorded and the
    reading is left to whoever opens the file.
    """
    with dest.open("w") as handle:
        for heading, command in commands:
            handle.write(f"== {heading} ==\n")
            handle.write(f"$ {command}\n")
            try:
                result = subprocess.run(
                    _ssh_argv(ip, ssh_key, command),
                    capture_output=True,
                    text=True,
                    timeout=_READ_TIMEOUT_SECS,
                )
            except subprocess.TimeoutExpired:
                handle.write(f"[timed out after {_READ_TIMEOUT_SECS}s]\n\n")
                continue
            handle.write(result.stdout or "")
            if result.stderr:
                handle.write(result.stderr)
            handle.write(f"[exit {result.returncode}]\n\n")


def collect_host(unit: ProdClientUnit, ip: str, ssh_key: str, out_dir: Path) -> HostCapture:
    """Capture one VM, recording a failure rather than raising on one.

    One unreachable host must not abandon the others: on a multi-VM deployment
    the hosts that *can* be read are often the ones holding the failure, and a
    run that stops at the first dead box leaves them behind to be destroyed
    with everything else.
    """
    host_dir = out_dir / f"{unit.service}-{ip}"
    host_dir.mkdir(parents=True, exist_ok=True)
    capture = HostCapture(label=unit.label, unit=unit.service, ip=ip, directory=str(host_dir))

    archive_path = host_dir / ARCHIVE_NAME
    archive_error = _capture_archive(ip, ssh_key, unit, archive_path)

    _capture_text(
        ip,
        ssh_key,
        [
            ("status", f"systemctl status {unit.service} --no-pager"),
            ("result", f"systemctl show {unit.service} -p Result -p ExecMainStatus"),
            ("journal", f"journalctl -u {unit.service} --no-pager -n 500"),
        ],
        host_dir / SERVICE_FILE_NAME,
    )
    _capture_text(
        ip,
        ssh_key,
        [("log directories", "ls -la /var/log/ant-prod-*/")],
        host_dir / MANIFEST_FILE_NAME,
    )
    if unit.is_upload:
        _capture_text(
            ip,
            ssh_key,
            [("payment markers", f"ls -la {unit.log_dir}/*.paid")],
            host_dir / PAYMENT_MARKERS_FILE_NAME,
        )

    for name in (
        ARCHIVE_NAME,
        SERVICE_FILE_NAME,
        MANIFEST_FILE_NAME,
        PAYMENT_MARKERS_FILE_NAME,
    ):
        path = host_dir / name
        if path.is_file():
            capture.files[name] = path.stat().st_size

    if archive_error is not None:
        capture.reason = archive_error
        return capture

    ok, reason, entries = verify_archive(archive_path)
    capture.ok = ok
    capture.reason = reason
    capture.archive_entries = entries
    return capture


def resolve_output_dir(name: str, deployment_id: int | None, override: str | None) -> Path:
    """Where the capture goes.

    The id is in the leaf because a staging name is reused every release train,
    so ``STG-WUL-01`` alone would have one train's capture overwrite the last
    one's. Without a registry there is no id and the name has to do.
    """
    if override:
        return Path(override).expanduser()
    leaf = f"{name}-{deployment_id}" if deployment_id is not None else name
    return state_dir() / FORENSICS_DIR_NAME / leaf


def _resolve_deployment(name: str, console: Console) -> tuple[str | None, int | None, str]:
    """Return ``(kind, deployment_id, client_kind)`` from the registry.

    Best-effort, as ``upgrade-ant-binary``'s own resolution is: the mechanism
    below needs nothing but client state and SSH. What an unavailable registry
    costs is the kind — and so the unit — which is why the caller then requires
    ``--kind``, rather than guessing one and archiving a directory that does not
    exist.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[yellow]Warning:[/yellow] DATABASE_URL not set — no registry id, so "
            "the capture directory is named after the deployment alone."
        )
        return None, None, "native"

    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        console.print(
            f"[yellow]Warning:[/yellow] no registry row for '{name}' — no id, so "
            "the capture directory is named after the deployment alone."
        )
        return None, None, "native"

    deploy_args = deployment.deploy_args if isinstance(deployment.deploy_args, dict) else {}
    return (
        deploy_args.get("kind") or None,
        deployment.id,
        deploy_args.get("client_kind") or "native",
    )


def _client_state_client_kind(clients: list[dict]) -> str:
    """The transport as client state records it, for a registry-less run.

    It changes nothing about what is collected — both transports share the unit
    and the log directory — but it belongs in the summary, which is the only
    description of the capture that outlives the console.
    """
    return "wasm" if any(c.get("client_kind") == "wasm" for c in clients) else "native"


def cmd_collect_client_logs(args) -> None:
    console = Console()

    registry_kind, deployment_id, client_kind = _resolve_deployment(args.name, console)
    kind = getattr(args, "kind", None) or registry_kind
    if not kind:
        console.print(
            "[bold red]Error:[/bold red] could not resolve the deployment kind "
            f"for '{args.name}'. Set DATABASE_URL so the registry can be read, "
            "or pass --kind explicitly. Guessing a kind would archive a log "
            "directory that does not exist on these hosts."
        )
        sys.exit(1)
    if kind in LOCAL_KINDS:
        console.print(
            f"[bold red]Error:[/bold red] '{args.name}' is a '{kind}' deployment — "
            "it runs on this machine, so there is no VM to collect from. Its "
            "per-operation records are in Postgres; read them with "
            "`deployments print-recorded-results`."
        )
        sys.exit(1)
    try:
        units = PROD_CLIENT_UNITS[kind]
    except KeyError:
        console.print(
            f"[bold red]Error:[/bold red] no client units are known for kind "
            f"'{kind}'. Supported: {', '.join(sorted(PROD_CLIENT_UNITS))}."
        )
        sys.exit(1)

    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    if deployment_id is None:
        client_kind = _client_state_client_kind(clients)
    try:
        groups = group_clients_by_unit(kind, clients)
    except UnknownProdClientKind as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    out_dir = resolve_output_dir(args.name, deployment_id, args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print(str(out_dir))

    if not groups:
        console.print(
            f"[bold red]Error:[/bold red] no client IPs found in state for "
            f"'{args.name}' — there is nothing to collect from."
        )
        sys.exit(1)

    ssh_key = str(Path(args.ssh_key_path).expanduser())
    console.print(
        f"Collecting '{kind}' forensics for {args.name} "
        f"({sum(len(ips) for _, ips in groups)} host(s), "
        f"{len(units)} unit(s) known for this kind)"
    )

    captures: list[HostCapture] = []
    for unit, ips in groups:
        console.print()
        console.print(f"[bold]{unit.label}[/bold]: unit {unit.service}, logs {unit.log_dir}")
        for ip in ips:
            capture = collect_host(unit, ip, ssh_key, out_dir)
            captures.append(capture)
            if capture.ok:
                console.print(
                    f"  [green]{ip}[/green]: {capture.archive_entries} archive entry(ies), "
                    f"{len(capture.files)} file(s)"
                )
            else:
                console.print(f"  [red]{ip}: {capture.reason}[/red]")

    failed = [c for c in captures if not c.ok]
    summary = {
        "deployment": args.name,
        "deployment_id": deployment_id,
        "kind": kind,
        "client_kind": client_kind,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(out_dir),
        "ok": not failed,
        "hosts": [asdict(c) for c in captures],
    }
    (out_dir / SUMMARY_FILE_NAME).write_text(json.dumps(summary, indent=2) + "\n")

    console.print()
    console.print(
        f"[bold]Capture complete: {len(captures) - len(failed)}/{len(captures)} "
        f"host(s) verified[/bold] — {out_dir}"
    )
    if failed:
        for capture in failed:
            console.print(f"  [red]{capture.unit} {capture.ip}: {capture.reason}[/red]")
        console.print(
            "  The capture FAILED for the host(s) above. Their evidence is still "
            "only on the VM, so do not destroy them yet."
        )
        sys.exit(1)
