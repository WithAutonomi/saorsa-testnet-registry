"""`prod local-downloads` command.

Run prod-network downloads from the local machine instead of provisioning
a remote downloader VM. Each address is downloaded sequentially with
``ant -v file download <address> -o <output>``; per-address results are
upserted directly into ``deployment_prod_download_results`` (no Influx,
no monitoring VM).

The single input mode is ``--addresses-path FILE`` — one address per line,
``#``-prefixed and blank lines ignored.

By default each download is written to a per-run temp directory and the
file is deleted as soon as the result row is recorded. Pass
``--keep-output-dir DIR`` to write into a directory the operator controls;
downloaded files are then left in place for inspection.

The run is registered as a ``Deployment`` row with
``deploy_args.kind = "prod-local-downloads"``. On completion the deployment
is marked ``stopped`` regardless of per-address failures — failures are
visible in the per-row Postgres records.
"""

from __future__ import annotations

import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from saorsa_db.models import DeploymentStatus, ProdDownloadStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.binary_source import (
    get_ant_custom_build_url,
    get_ant_release_url,
)
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    ensure_local_bootstrap_peers_file,
    fetch_prod_bootstraps,
)
from saorsa_deploy.registry import (
    get_repository,
    record_deployment_created,
    record_status,
)

EVM_NETWORK = "arbitrum-one"
DOWNLOAD_TIMEOUT_SECONDS = 600

_BINARY_CACHE_ROOT = Path("~/.cache/saorsa-deploy/ant-binaries").expanduser()
_LOG_ROOT = Path("~/.local/state/saorsa-deploy/local-downloads").expanduser()
_LOG_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TIME_FIELD_RE = re.compile(r"Time:\s+([0-9.]+)")


# ---------- binary resolution -------------------------------------------------


def _cache_key(branch_name: str | None, repo_owner: str | None, version: str | None) -> str:
    if branch_name:
        owner = repo_owner or "maidsafe"
        return f"branch-{owner}-{branch_name}"
    if version:
        return f"release-{version}"
    return "release-latest"


def _is_immutable_cache_key(key: str) -> bool:
    """Pinned releases are immutable; ``latest`` and branch builds are not."""
    return key.startswith("release-") and key != "release-latest"


def _download_to(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp, open(
        dest, "wb"
    ) as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)


def _extract_ant_from_tarball(tarball: Path, dest: Path) -> None:
    with tarfile.open(tarball, "r:gz") as tar:
        member = next(
            (m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "ant"),
            None,
        )
        if member is None:
            raise RuntimeError(
                f"Release tarball {tarball} does not contain an 'ant' binary"
            )
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"Could not extract 'ant' from {tarball}")
        with open(dest, "wb") as out:
            while True:
                chunk = extracted.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)


def _resolve_ant_binary(
    *,
    branch_name: str | None,
    repo_owner: str | None,
    version: str | None,
    deployment_name: str,
    console: Console,
) -> tuple[Path, str | None]:
    """Resolve, download (if needed), and cache the local ant binary path.

    Mirrors ``prod_local_uploads._resolve_ant_binary``. Returns
    ``(path, resolved_version)`` — concrete version only when known.
    """
    key = _cache_key(branch_name, repo_owner, version)
    target = _BINARY_CACHE_ROOT / key / "ant"

    if branch_name:
        owner = repo_owner or "maidsafe"
        url = get_ant_custom_build_url(deployment_name, owner, branch_name)
        is_tarball = False
        resolved_version: str | None = None
    else:
        url = get_ant_release_url(version)
        is_tarball = url.endswith(".tar.gz")
        if version:
            resolved_version = version
        else:
            match = re.search(r"ant-cli-v(\d+\.\d+\.\d+)", url)
            resolved_version = match.group(1) if match else None

    if target.exists() and _is_immutable_cache_key(key):
        console.print(f"[dim]Using cached ant binary at {target}.[/dim]")
        return target, resolved_version

    console.print(f"[bold]Fetching ant binary:[/bold] {url}")

    target.parent.mkdir(parents=True, exist_ok=True)

    if is_tarball:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=".tar.gz", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            _download_to(url, tmp_path)
            _extract_ant_from_tarball(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        _download_to(url, target)

    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    console.print(f"[green]ant binary ready at {target}[/green]")
    return target, resolved_version


# ---------- ant invocation + parsing -----------------------------------------


def _resolve_bootstrap_args(args, console: Console) -> list[str]:
    """Resolve the --bootstrap argv fragment for the local ant invocations.

    With the bootstrap cache enabled (default), ant is invoked WITHOUT
    --bootstrap: it reads ~/.config/ant/bootstrap_peers.toml and maintains its
    peer cache. The file is created from the fetched prod list only when
    missing — an existing operator file is never overwritten.
    --no-bootstrap-cache restores the legacy explicit --bootstrap invocation.
    """
    if getattr(args, "bootstrap_cache", True):
        try:
            peers_path, created = ensure_local_bootstrap_peers_file()
        except ProdBootstrapsError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        if created:
            console.print(
                f"[bold]Bootstrap cache enabled:[/bold] wrote {peers_path} "
                f"from {PROD_BOOTSTRAPS_URL}"
            )
        else:
            console.print(
                f"[bold]Bootstrap cache enabled:[/bold] using existing {peers_path}"
            )
        return []

    console.print(
        f"[bold]Resolving prod bootstraps from {PROD_BOOTSTRAPS_URL}...[/bold]"
    )
    try:
        peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    bootstrap_args: list[str] = []
    for peer in peers:
        bootstrap_args.extend(["--bootstrap", peer])
    return bootstrap_args


def _parse_download_secs(stdout: str) -> float:
    """Pull the ``Time:`` value out of ant's stdout (mirrors downloader.sh)."""
    match = _TIME_FIELD_RE.search(stdout)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _safe_log_basename(name: str) -> str:
    cleaned = _LOG_NAME_SAFE_RE.sub("_", name).strip("._-")
    return cleaned or "unnamed"


def _run_ant(
    *,
    ant_path: Path,
    bootstrap_args: list[str],
    address: str,
    output_path: Path,
    stderr_log_path: Path,
    stdout_log_path: Path,
) -> tuple[int, str]:
    """Run ``ant file download`` and tee its streams to log files in real time.

    stderr is echoed to ``sys.stderr`` so the operator sees progress live.
    Returns ``(exit_code, stdout_text)``.
    """
    cmd = [
        str(ant_path),
        *bootstrap_args,
        "--evm-network", EVM_NETWORK,
        "-v",
        "file", "download", address,
        "-o", str(output_path),
    ]

    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_chunks: list[str] = []
    stderr_err: list[BaseException] = []
    stdout_err: list[BaseException] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _pump_stderr() -> None:
        try:
            with open(
                stderr_log_path, "w", encoding="utf-8", errors="replace"
            ) as fh:
                assert proc.stderr is not None
                for line in proc.stderr:
                    fh.write(line)
                    sys.stderr.write(line)
                    sys.stderr.flush()
        except BaseException as e:  # noqa: BLE001
            stderr_err.append(e)

    def _pump_stdout() -> None:
        try:
            with open(
                stdout_log_path, "w", encoding="utf-8", errors="replace"
            ) as fh:
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    stdout_chunks.append(line)
        except BaseException as e:  # noqa: BLE001
            stdout_err.append(e)

    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_out = threading.Thread(target=_pump_stdout, daemon=True)
    t_err.start()
    t_out.start()
    exit_code = proc.wait()
    t_err.join()
    t_out.join()

    if stderr_err:
        raise stderr_err[0]
    if stdout_err:
        raise stdout_err[0]

    return exit_code, "".join(stdout_chunks)


# ---------- input collection --------------------------------------------------


def _read_address_list(path: Path) -> list[str]:
    addresses: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            addresses.append(line)
    return addresses


# ---------- main entry --------------------------------------------------------


def cmd_prod_local_downloads(args) -> None:
    console = Console()

    if (args.branch_name and not args.repo_owner) or (
        args.repo_owner and not args.branch_name
    ):
        console.print(
            "[bold red]Error:[/bold red] --branch-name and --repo-owner must be "
            "provided together."
        )
        sys.exit(1)
    if (args.branch_name or args.repo_owner) and args.ant_version:
        console.print(
            "[bold red]Error:[/bold red] --ant-version is mutually exclusive with "
            "--branch-name/--repo-owner."
        )
        sys.exit(1)

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database "
            "is unreachable. `prod local-downloads` records results directly to "
            "Postgres and cannot run without it."
        )
        sys.exit(1)

    addresses_path = Path(args.addresses_path).resolve()
    if not addresses_path.is_file():
        console.print(
            f"[bold red]Error:[/bold red] --addresses-path {addresses_path} is not a file."
        )
        sys.exit(1)
    addresses = _read_address_list(addresses_path)
    if not addresses:
        console.print(
            f"[bold red]Error:[/bold red] address list {addresses_path} is empty."
        )
        sys.exit(1)

    bootstrap_args = _resolve_bootstrap_args(args, console)

    ant_path, resolved_version = _resolve_ant_binary(
        branch_name=args.branch_name,
        repo_owner=args.repo_owner,
        version=args.ant_version,
        deployment_name=args.name,
        console=console,
    )

    host = socket.gethostname()
    log_dir = _LOG_ROOT / args.name
    log_dir.mkdir(parents=True, exist_ok=True)

    binary_descriptor = build_binary_descriptor(
        args.branch_name, args.repo_owner, args.ant_version
    )
    if binary_descriptor.get("kind") == "latest" and resolved_version:
        binary_descriptor = {"kind": "release", "version": resolved_version}

    keep_output = args.keep_output_dir is not None
    if keep_output:
        output_root = Path(args.keep_output_dir).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        temp_output_root: Path | None = None
    else:
        temp_output_root = Path(tempfile.mkdtemp(prefix="saorsa-local-downloads-"))
        output_root = temp_output_root

    deploy_args_payload = {
        "kind": "prod-local-downloads",
        "purpose": args.purpose,
        "ant_binary": binary_descriptor,
        "addresses_path": str(addresses_path),
        "addresses": addresses,
        "address_count": len(addresses),
        "bootstrap_cache": getattr(args, "bootstrap_cache", True),
        "output_mode": "keep" if keep_output else "temp",
        "output_dir": str(output_root),
        "host": host,
        "log_dir": str(log_dir),
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
    }
    deployment_id = record_deployment_created(args.name, deploy_args_payload)
    if deployment_id is None:
        console.print(
            "[bold red]Error:[/bold red] Failed to register deployment "
            f"'{args.name}' in the database."
        )
        sys.exit(1)

    console.print(
        f"[bold]Starting prod local-downloads:[/bold] {len(addresses)} address(es), "
        f"host={host} output={'keep' if keep_output else 'temp'} dir={output_root}"
    )
    console.print(f"[dim]Per-download logs will be written to: {log_dir}[/dim]")

    region = "local"
    failures = 0
    address_count = len(addresses)

    try:
        for idx, address in enumerate(addresses, start=1):
            console.print(f"[{idx}/{address_count}] {address}")
            log_basename = f"{idx:04d}-{_safe_log_basename(address)}"
            stderr_log_path = log_dir / f"{log_basename}.stderr"
            stdout_log_path = log_dir / f"{log_basename}.stdout"
            output_path = output_root / address

            started_at = datetime.now(timezone.utc)
            start_ns = time.time_ns()
            try:
                exit_code, stdout = _run_ant(
                    ant_path=ant_path,
                    bootstrap_args=bootstrap_args,
                    address=address,
                    output_path=output_path,
                    stderr_log_path=stderr_log_path,
                    stdout_log_path=stdout_log_path,
                )
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                end_ns = time.time_ns()
                finished_at = datetime.now(timezone.utc)
                total_ms = (end_ns - start_ns) // 1_000_000
                failures += 1
                console.print(f"  [red]FAILED to spawn ant:[/red] {e}")
                repo.upsert_prod_download_result(
                    deployment_id=deployment_id,
                    client_hostname=host,
                    region=region,
                    address=address,
                    status=ProdDownloadStatus.FAILED,
                    output_path=None,
                    file_size_bytes=None,
                    duration_ms=total_ms,
                    download_secs=None,
                    started_at=started_at,
                    finished_at=finished_at,
                    reason=str(e),
                )
                continue

            end_ns = time.time_ns()
            finished_at = datetime.now(timezone.utc)
            total_ms = (end_ns - start_ns) // 1_000_000
            download_secs = _parse_download_secs(stdout) or None

            file_size: int | None
            if output_path.exists():
                try:
                    file_size = output_path.stat().st_size
                except OSError:
                    file_size = None
            else:
                file_size = None

            if exit_code == 0 and file_size is not None and file_size > 0:
                status = ProdDownloadStatus.SUCCESS
                reason = None
                console.print(
                    f"  [green]ok[/green]: size={file_size} download_secs={download_secs} "
                    f"total={total_ms}ms log={stderr_log_path}"
                )
            else:
                status = ProdDownloadStatus.FAILED
                failures += 1
                reason = f"exit_code={exit_code}"
                if file_size is None or file_size == 0:
                    reason += " (no output file)"
                console.print(
                    f"  [red]FAILED[/red]: exit={exit_code} total={total_ms}ms "
                    f"size={file_size} log={stderr_log_path}"
                )

            if keep_output and status == ProdDownloadStatus.SUCCESS:
                recorded_output_path = str(output_path)
            else:
                recorded_output_path = None

            repo.upsert_prod_download_result(
                deployment_id=deployment_id,
                client_hostname=host,
                region=region,
                address=address,
                status=status,
                output_path=recorded_output_path,
                file_size_bytes=file_size,
                duration_ms=total_ms,
                download_secs=download_secs,
                started_at=started_at,
                finished_at=finished_at,
                reason=reason,
            )

            if not keep_output:
                try:
                    if output_path.exists():
                        output_path.unlink()
                except OSError:
                    pass
    finally:
        if temp_output_root is not None:
            try:
                for leftover in temp_output_root.iterdir():
                    leftover.unlink(missing_ok=True)
                temp_output_root.rmdir()
            except OSError:
                pass

    record_status(args.name, DeploymentStatus.STOPPED)
    console.print(
        f"[bold green]prod local-downloads complete:[/bold green] processed="
        f"{address_count} failures={failures} (deployment marked stopped)"
    )
    console.print(f"[dim]Logs available under: {log_dir}[/dim]")


__all__ = ["cmd_prod_local_downloads"]
