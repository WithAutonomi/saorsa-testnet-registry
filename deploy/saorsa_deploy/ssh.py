import concurrent.futures
import subprocess
import time
from typing import Iterable


def clear_known_hosts(ips, console=None):
    """Remove IPs from SSH known_hosts to prevent host key verification failures.

    Cloud providers may reuse IP addresses for different machines, causing SSH
    to reject connections due to host key mismatches. This runs `ssh-keygen -R`
    for each IP before provisioning.
    """
    for ip in ips:
        result = subprocess.run(
            ["ssh-keygen", "-R", ip],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and console:
            console.print(f"[dim]Cleared known_hosts entry for {ip}[/dim]")


def wait_for_ssh(
    ips: Iterable[str],
    ssh_key_path: str,
    console=None,
    timeout: int = 180,
    poll_interval: int = 5,
    per_check_timeout: int = 5,
) -> list[str]:
    """Block until SSH is accepting connections on every host in ``ips``.

    Polls all hosts in parallel every ``poll_interval`` seconds, up to a
    total ``timeout`` budget. Returns the list of hosts that did NOT come
    up in time (empty on full success).

    A successful poll runs ``ssh -o BatchMode=yes ... root@<ip> true``
    against the host — that's a stronger signal than a TCP probe because
    it confirms sshd has accepted auth, not just that port 22 is open
    (cloud-init can hold the TCP listener before sshd is ready).
    """
    pending = set(ips)
    if not pending:
        return []

    if console:
        console.print(
            f"[dim]Waiting for SSH on {len(pending)} host(s) "
            f"(timeout {timeout}s)...[/dim]"
        )

    deadline = time.time() + timeout

    def _check(ip: str) -> tuple[str, bool]:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", f"ConnectTimeout={per_check_timeout}",
                "-o", "BatchMode=yes",
                "-i", ssh_key_path,
                f"root@{ip}",
                "true",
            ],
            capture_output=True,
        )
        return ip, result.returncode == 0

    while pending and time.time() < deadline:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(pending))
        ) as executor:
            results = list(executor.map(_check, list(pending)))
        for ip, ok in results:
            if ok and ip in pending:
                pending.discard(ip)
                if console:
                    console.print(f"[dim]  SSH ready on {ip}[/dim]")
        if pending and time.time() < deadline:
            time.sleep(poll_interval)

    return sorted(pending)
