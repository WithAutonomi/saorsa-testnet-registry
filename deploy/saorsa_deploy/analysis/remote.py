"""Run a bash payload on VMs over SSH, in parallel, and copy files back.

The payload travels on stdin (`bash -s -- args...`), exactly as the proven
scripts did, so no file has to be staged on the VM first and the payload text
can be kept verbatim. Commands that must survive the SSH session dying (packet
captures) do so on the remote side by launching through a transient systemd
unit; nothing here tries to daemonise locally.
"""
from __future__ import annotations

import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from saorsa_deploy.analysis.hosts import Vm

T = TypeVar("T")


@dataclass(frozen=True)
class RemoteResult:
    vm: Vm
    returncode: int
    stdout: str
    stderr: str

    @property
    def last_line(self) -> str:
        lines = [ln for ln in self.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_script(
    vm: Vm,
    script: str,
    args: Iterable[str] = (),
    timeout: float | None = None,
    stdin_extra: str = "",
) -> RemoteResult:
    """Run ``script`` on ``vm`` via ``bash -s -- args``; stdout/stderr captured."""
    quoted = " ".join(shlex.quote(a) for a in args)
    cmd = [*vm.ssh_args(), vm.target, f"bash -s -- {quoted}".rstrip()]
    try:
        proc = subprocess.run(
            cmd,
            input=script + stdin_extra,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return RemoteResult(
            vm,
            returncode=124,
            stdout=(
                exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            ),
            stderr=f"timed out after {timeout}s",
        )
    return RemoteResult(vm, proc.returncode, proc.stdout, proc.stderr)


def run_parallel(vms: list[Vm], fn: Callable[[Vm], T], max_workers: int = 32) -> list[T]:
    """Apply ``fn`` to every VM concurrently, preserving input order."""
    if not vms:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(vms))) as pool:
        return list(pool.map(fn, vms))


def push_file(
    vm: Vm, content: str, remote_path: str, timeout: float | None = 120
) -> RemoteResult:
    """Write ``content`` to ``remote_path`` on the VM (via a heredoc-free stdin copy)."""
    target = shlex.quote(remote_path)
    script = f"cat > {target} && chmod 0644 {target} && echo STATUS=pushed"
    quoted_script = shlex.quote(script)
    cmd = [*vm.ssh_args(), vm.target, f"bash -c {quoted_script}"]
    proc = subprocess.run(
        cmd, input=content, capture_output=True, text=True, timeout=timeout, check=False
    )
    return RemoteResult(vm, proc.returncode, proc.stdout, proc.stderr)


def fetch_file(
    vm: Vm, remote_path: str, local_path: Path, timeout: float | None = None
) -> RemoteResult:
    """Copy one file from the VM with scp."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [*vm.scp_args(), f"{vm.target}:{remote_path}", str(local_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return RemoteResult(vm, proc.returncode, proc.stdout, proc.stderr)
