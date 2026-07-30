"""`prod local-uploads` command.

Run prod-network uploads from the local machine instead of provisioning
remote uploader VMs. Each file is uploaded sequentially with
``ant --json file upload --public``; per-file results are upserted directly
into ``deployment_prod_upload_results`` (no Influx, no monitoring VM).

Two input modes:

* ``--urls FILE``: download each URL to a scratch directory, upload the
  resulting file, then delete the scratch copy. The remote source is left
  untouched.
* ``--local-dir-path DIR``: iterate every regular file in ``DIR`` (non-recursive)
  and upload it. Source files are never modified or deleted.

The run is registered as a ``Deployment`` row with
``deploy_args.kind = "prod-local-uploads"``. On completion the deployment
is marked ``stopped`` regardless of per-file failures — failures are
visible in the per-row Postgres records.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from rich.console import Console
from saorsa_db.models import DeploymentStatus, ProdUploadStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.binary_source import (
    get_ant_custom_build_url,
    get_ant_release_url,
)
from saorsa_deploy.config.models import log_level_to_verbosity
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
_VALID_PAYMENT_VALUES = ("merkle", "no-merkle")

_BINARY_CACHE_ROOT = Path("~/.cache/saorsa-deploy/ant-binaries").expanduser()
_LOG_ROOT = Path("~/.local/state/saorsa-deploy/local-uploads").expanduser()
_LOG_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


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

    Returns ``(path, resolved_version)``. ``resolved_version`` is the
    concrete release version when one is known — i.e. either ``--ant-version``
    was set, or we resolved ``/releases/latest`` and parsed the version out
    of the asset URL. ``None`` for branch builds.
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


def _payment_flag(payment_type: str | None) -> list[str]:
    if payment_type == "merkle":
        return ["--merkle"]
    if payment_type == "no-merkle":
        return ["--no-merkle"]
    return []


def _verbosity_flag(verbose: int) -> str:
    return "-" + ("v" * max(1, verbose))


def _parse_ant_json(stdout: str) -> dict | None:
    """Pick the last non-empty JSON line from ant's stdout.

    Mirrors the helper in ``resources/scripts/prod_uploader.py``; duplicated
    rather than imported because that script is intentionally
    saorsa_deploy-import-free.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _cost_to_decimal(value) -> Decimal | None:
    """Convert a string-encoded uint256 cost field (atto/wei) to ``Decimal``.

    ``ant --json file upload`` emits ``storage_cost_atto`` / ``gas_cost_wei`` as
    strings; the Postgres columns are ``NUMERIC``. Empty/missing/unparseable
    (e.g. an older ``ant`` that omits them) stays ``None`` (NULL).
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _classify_status(parsed: dict | None, exit_code: int) -> ProdUploadStatus:
    if parsed is None:
        return ProdUploadStatus.PARTIAL_UPLOAD
    if "error" in parsed:
        return ProdUploadStatus.PARTIAL_UPLOAD
    if exit_code != 0:
        return ProdUploadStatus.PARTIAL_UPLOAD
    return ProdUploadStatus.SUCCESS


def _safe_log_basename(name: str) -> str:
    """Sanitise a file name for use inside a per-upload log filename."""
    cleaned = _LOG_NAME_SAFE_RE.sub("_", name).strip("._-")
    return cleaned or "unnamed"


def _run_ant(
    *,
    ant_path: Path,
    bootstrap_args: list[str],
    secret_key: str,
    file_path: Path,
    payment_type: str | None,
    verbose: int,
    stderr_log_path: Path,
    stdout_log_path: Path,
) -> tuple[int, str]:
    """Run ant and tee its streams to plain-text log files in real time.

    stderr is also echoed to ``sys.stderr`` line-by-line so a slow run is
    visible in the operator's terminal as it happens. stdout is captured
    silently to disk and into a buffer for JSON parsing — there's no value
    in echoing it (``--json`` mode emits a single record at the end).
    Returns ``(exit_code, stdout_text)``.
    """
    cmd = [
        str(ant_path),
        *bootstrap_args,
        "--evm-network", EVM_NETWORK,
        "--json",
        _verbosity_flag(verbose),
        "file", "upload", "--public", *_payment_flag(payment_type),
        str(file_path),
    ]
    env = dict(os.environ)
    env["SECRET_KEY"] = secret_key

    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_chunks: list[str] = []
    stderr_err: list[BaseException] = []
    stdout_err: list[BaseException] = []

    proc = subprocess.Popen(
        cmd,
        env=env,
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
        except BaseException as e:  # noqa: BLE001 — surface in main thread
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


def _read_url_list(path: Path) -> list[str]:
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def _list_local_files(directory: Path) -> list[tuple[Path, str]]:
    """Recursively list regular files under ``directory``.

    Returns a list of ``(absolute_path, relative_posix_path)`` pairs sorted
    by the relative path so siblings group with their parents in a stable
    order. The relative path is used as the file's identifier in DB records,
    logs, and Slack output.
    """
    if not directory.is_dir():
        raise RuntimeError(f"--local-dir-path {directory} is not a directory")
    pairs: list[tuple[Path, str]] = []
    for p in directory.rglob("*"):
        if p.is_file():
            pairs.append((p, p.relative_to(directory).as_posix()))
    pairs.sort(key=lambda item: item[1])
    return pairs


def _safe_filename(url: str, fallback_index: int) -> str:
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path)
    if not name or name in (".", ".."):
        return f"download-{fallback_index}.bin"
    return name


def _download_url_to_tempfile(url: str, scratch_dir: Path, idx: int) -> tuple[Path, int]:
    name = _safe_filename(url, idx)
    dest = scratch_dir / f"{idx:04d}-{name}"
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp, open(
        dest, "wb"
    ) as out:
        size = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return dest, size


# ---------- main entry --------------------------------------------------------


def cmd_prod_local_uploads(args) -> None:
    console = Console()

    secret_key = args.secret_key or os.environ.get("ARBITRUM_ONE_SECRET_KEY")
    if not secret_key:
        console.print(
            "[bold red]Error:[/bold red] --secret-key not provided and "
            "ARBITRUM_ONE_SECRET_KEY env var is unset. A funded Arbitrum One "
            "wallet is required for prod uploads."
        )
        sys.exit(1)
    if not secret_key.startswith("0x"):
        secret_key = "0x" + secret_key

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

    if args.payment is not None and args.payment not in _VALID_PAYMENT_VALUES:
        console.print(
            f"[bold red]Error:[/bold red] --payment must be one of "
            f"{_VALID_PAYMENT_VALUES}; got '{args.payment}'."
        )
        sys.exit(1)

    # Resolve the effective ant verbosity. --log-level is the documented knob;
    # the deprecated -v/--verbose count is honoured only when --log-level is
    # unset (log_level wins when both are supplied).
    verbose = (
        log_level_to_verbosity(args.log_level)
        if args.log_level is not None
        else args.verbose
    )

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database "
            "is unreachable. `prod local-uploads` records results directly to "
            "Postgres and cannot run without it."
        )
        sys.exit(1)

    pool_claims: list = []
    if getattr(args, "from_pool", None):
        local_files, pool_claims = _select_from_pool(args, console)
        input_mode = "pool"
        input_path = f"pool:{args.from_pool}"
        file_count = len(local_files)
        urls = []
    elif args.urls:
        urls = _read_url_list(Path(args.urls))
        if not urls:
            console.print(
                f"[bold red]Error:[/bold red] URL list {args.urls} is empty."
            )
            sys.exit(1)
        input_mode = "urls"
        input_path = str(Path(args.urls).resolve())
        file_count = len(urls)
        local_files: list[tuple[Path, str]] = []
    else:
        local_dir = Path(args.local_dir_path).resolve()
        local_files = _list_local_files(local_dir)
        if not local_files:
            console.print(
                f"[bold red]Error:[/bold red] --local-dir-path {local_dir} contains no "
                "regular files."
            )
            sys.exit(1)
        input_mode = "local-dir"
        input_path = str(local_dir)
        file_count = len(local_files)
        urls = []

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
        # Pin the descriptor to the version we actually downloaded so the
        # deployment record states what was run, not "whatever was latest".
        binary_descriptor = {"kind": "release", "version": resolved_version}

    files_payload: list[dict] = []
    if input_mode == "local-dir":
        for path, rel_name in local_files:
            try:
                files_payload.append(
                    {"name": rel_name, "size_bytes": path.stat().st_size}
                )
            except OSError:
                files_payload.append({"name": rel_name, "size_bytes": None})

    deploy_args_payload = {
        "kind": "prod-local-uploads",
        "purpose": args.purpose,
        "ant_binary": binary_descriptor,
        "input_mode": input_mode,
        "input_path": input_path,
        "file_count": file_count,
        "files": files_payload,
        "payment_type": args.payment,
        "bootstrap_cache": getattr(args, "bootstrap_cache", True),
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
        f"[bold]Starting prod local-uploads:[/bold] {file_count} file(s), "
        f"input_mode={input_mode}, host={host}"
    )
    console.print(f"[dim]Per-upload logs will be written to: {log_dir}[/dim]")

    region = "local"
    failures = 0

    if input_mode == "urls":
        scratch = Path(tempfile.mkdtemp(prefix="saorsa-local-uploads-"))
        try:
            for idx, url in enumerate(urls, start=1):
                console.print(f"[{idx}/{file_count}] {url}")
                started_at = datetime.now(timezone.utc)
                start_ns = time.time_ns()
                try:
                    local_path, file_size = _download_url_to_tempfile(
                        url, scratch, idx
                    )
                except Exception as e:
                    finished_at = datetime.now(timezone.utc)
                    console.print(f"  [red]download FAILED:[/red] {e}")
                    failures += 1
                    repo.upsert_prod_upload_result(
                        deployment_id=deployment_id,
                        client_hostname=host,
                        region=region,
                        source_url=url,
                        file_name=_safe_filename(url, idx),
                        file_size_bytes=0,
                        status=ProdUploadStatus.DOWNLOAD_FAILED,
                        address=None,
                        chunks_stored=0,
                        chunks_failed=0,
                        total_chunks=0,
                        duration_ms=None,
                        upload_secs=None,
                        started_at=started_at,
                        finished_at=finished_at,
                        reason=str(e),
                    )
                    continue

                try:
                    ok = _process_one_upload(
                        repo=repo,
                        deployment_id=deployment_id,
                        host=host,
                        region=region,
                        source_url=url,
                        file_name=local_path.name,
                        file_path=local_path,
                        file_size_bytes=file_size,
                        ant_path=ant_path,
                        bootstrap_args=bootstrap_args,
                        secret_key=secret_key,
                        payment_type=args.payment,
                        verbose=verbose,
                        console=console,
                        started_at=started_at,
                        start_ns=start_ns,
                        log_dir=log_dir,
                        idx=idx,
                    )
                finally:
                    local_path.unlink(missing_ok=True)
                if not ok:
                    failures += 1
        finally:
            try:
                # The scratch dir holds only files we created and just
                # deleted; an empty rmdir is safe even if a file slipped
                # through.
                for leftover in scratch.iterdir():
                    leftover.unlink(missing_ok=True)
                scratch.rmdir()
            except OSError:
                pass
    else:
        for idx, (file_path, rel_name) in enumerate(local_files, start=1):
            console.print(f"[{idx}/{file_count}] {rel_name}")
            started_at = datetime.now(timezone.utc)
            start_ns = time.time_ns()
            try:
                file_size = file_path.stat().st_size
            except OSError as e:
                console.print(f"  [red]stat FAILED:[/red] {e}")
                failures += 1
                continue
            ok = _process_one_upload(
                repo=repo,
                deployment_id=deployment_id,
                host=host,
                region=region,
                source_url=str(file_path),
                file_name=rel_name,
                file_path=file_path,
                file_size_bytes=file_size,
                ant_path=ant_path,
                bootstrap_args=bootstrap_args,
                secret_key=secret_key,
                payment_type=args.payment,
                verbose=verbose,
                console=console,
                started_at=started_at,
                start_ns=start_ns,
                log_dir=log_dir,
                idx=idx,
            )
            if not ok:
                failures += 1
            # Resolve this file's pool claim immediately. Unlike the remote
            # kinds, this command writes its result rows inline, so there is no
            # later `record-results` pass to close the claim — and leaving it
            # open would make a finished upload look merely checked out.
            _resolve_pool_claim(pool_claims, rel_name, ok, console)

    record_status(args.name, DeploymentStatus.STOPPED)
    console.print(
        f"[bold green]prod local-uploads complete:[/bold green] processed="
        f"{file_count} failures={failures} (deployment marked stopped)"
    )
    console.print(f"[dim]Logs available under: {log_dir}[/dim]")


def _process_one_upload(
    *,
    repo,
    deployment_id: int,
    host: str,
    region: str,
    source_url: str,
    file_name: str,
    file_path: Path,
    file_size_bytes: int,
    ant_path: Path,
    bootstrap_args: list[str],
    secret_key: str,
    payment_type: str | None,
    verbose: int,
    console: Console,
    started_at: datetime,
    start_ns: int,
    log_dir: Path,
    idx: int,
) -> bool:
    """Run ant on a single file and upsert the result. Returns True on SUCCESS."""
    log_basename = f"{idx:04d}-{_safe_log_basename(file_name)}"
    stderr_log_path = log_dir / f"{log_basename}.stderr"
    stdout_log_path = log_dir / f"{log_basename}.stdout"

    exit_code, stdout = _run_ant(
        ant_path=ant_path,
        bootstrap_args=bootstrap_args,
        secret_key=secret_key,
        file_path=file_path,
        payment_type=payment_type,
        verbose=verbose,
        stderr_log_path=stderr_log_path,
        stdout_log_path=stdout_log_path,
    )
    end_ns = time.time_ns()
    finished_at = datetime.now(timezone.utc)
    total_ms = (end_ns - start_ns) // 1_000_000

    parsed = _parse_ant_json(stdout)
    status = _classify_status(parsed, exit_code)
    chunks_stored = int((parsed or {}).get("chunks_stored") or 0)
    chunks_failed = int((parsed or {}).get("chunks_failed") or 0)
    total_chunks = int((parsed or {}).get("total_chunks") or 0)
    upload_secs = float((parsed or {}).get("elapsed_secs") or 0.0) or None
    address = str((parsed or {}).get("address") or "") or None
    reason = str((parsed or {}).get("reason") or "") or None
    storage_cost_atto = _cost_to_decimal((parsed or {}).get("storage_cost_atto"))
    gas_cost_wei = _cost_to_decimal((parsed or {}).get("gas_cost_wei"))

    if status == ProdUploadStatus.SUCCESS:
        console.print(
            f"  [green]ok[/green]: address={address} chunks="
            f"{chunks_stored}/{total_chunks} total={total_ms}ms "
            f"log={stderr_log_path}"
        )
    else:
        # Per-upload stderr was already streamed live to the operator's
        # terminal (and to the on-disk log) by ``_run_ant``; no need to
        # reprint a tail here.
        console.print(
            f"  [red]FAILED[/red]: chunks={chunks_stored}/{total_chunks} "
            f"failed={chunks_failed} exit={exit_code} total={total_ms}ms "
            f"reason={reason!r} log={stderr_log_path}"
        )

    repo.upsert_prod_upload_result(
        deployment_id=deployment_id,
        client_hostname=host,
        region=region,
        source_url=source_url,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        status=status,
        address=address,
        chunks_stored=chunks_stored,
        chunks_failed=chunks_failed,
        total_chunks=total_chunks,
        duration_ms=total_ms,
        upload_secs=upload_secs,
        started_at=started_at,
        finished_at=finished_at,
        reason=reason,
        storage_cost_atto=storage_cost_atto,
        gas_cost_wei=gas_cost_wei,
    )

    return status == ProdUploadStatus.SUCCESS


__all__ = ["cmd_prod_local_uploads"]


def _select_from_pool(args, console: Console):
    """Draw local files from the managed pool.

    Returns the same ``(absolute_path, relative_name)`` pairs the
    ``--local-dir-path`` mode produces, so the upload loop is untouched — the
    name recorded against each result stays the file's own name, and nothing
    downstream needs to know the pool exists.

    LOCAL entries only, and not overridable: this command reads from disk, so a
    URL entry would be meaningless here.
    """
    from pathlib import Path as _Path

    from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind

    from saorsa_deploy.pool_selection import (
        PoolSelectionError,
        parse_request_token,
        render_selection,
        select_for_deployment,
    )
    from saorsa_deploy.registry import get_file_pool_repository

    repo = get_file_pool_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] --from-pool needs DATABASE_URL — the "
            "file pool lives in the registry database."
        )
        sys.exit(1)

    try:
        request = parse_request_token(args.from_pool)
    except (PoolSelectionError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    try:
        selected = select_for_deployment(
            repo,
            name=args.name,
            slot="local-uploads",
            kind=PoolFileKind.LOCAL,
            request=request,
            source=ClaimSource.PROD_LOCAL_UPLOADS,
        )
    except PoolSelectionError as exc:
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    console.print("  Pool selection")
    render_selection(console, "local-uploads", request, selected)

    pairs = []
    claims = []
    missing = []
    for entry in selected:
        path = _Path(entry.locator)
        if not path.is_file():
            # The catalogue holds a reference, not the bytes. A file that has
            # moved since it was catalogued must fail loudly here rather than
            # partway through a paid upload run.
            missing.append(entry)
            continue
        pairs.append((path, path.name))
        claims.append({"name": path.name, "claim_id": entry.claim_id})

    if missing:
        console.print(
            f"[bold red]Error:[/bold red] {len(missing)} selected file(s) are no "
            "longer on disk:"
        )
        for entry in missing[:10]:
            console.print(f"[red]  #{entry.file_id} {entry.locator}[/red]")
        console.print(
            "Run `saorsa-deploy prod pool verify --kind local` to retire "
            "entries whose files have moved, then release this selection with "
            "`prod pool release "
            + " ".join(str(e.file_id) for e in missing[:10])
            + "`."
        )
        sys.exit(1)

    return pairs, claims


def _resolve_pool_claim(pool_claims: list, file_name: str, ok: bool, console) -> None:
    """Close the claim for one file as soon as its upload finishes."""
    if not pool_claims:
        return
    entry = next((c for c in pool_claims if c["name"] == file_name), None)
    if entry is None or entry.get("claim_id") is None:
        return

    from saorsa_deploy.registry import get_file_pool_repository

    repo = get_file_pool_repository()
    if repo is None:
        return
    try:
        if ok:
            repo.resolve_pool_claim(
                entry["claim_id"], note="uploaded by prod local-uploads"
            )
        else:
            # A failed upload did not put the content on the network, so the
            # file goes back rather than being burned by an attempt.
            repo.release_pool_claim(
                entry["claim_id"], note="prod local-uploads upload failed"
            )
    except ValueError as exc:
        console.print(f"[yellow]Warning:[/yellow] could not resolve pool claim: {exc}")
