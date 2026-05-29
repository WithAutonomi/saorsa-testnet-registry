import concurrent.futures
import subprocess
import time
from typing import Iterable

# Transient SSH connect failures — "Error reading SSH protocol banner",
# connection reset, EOF — are common when a freshly-booted droplet's sshd is
# still settling or is rate-limiting a burst of parallel connects. pyinfra's
# SSH connector already retries on exactly these exception types
# (SSHException / EOFError / socket errors), but only when the per-host
# `ssh_connect_retries` data value is > 0, and it defaults to 0 (no retries).
#
# We build a fresh one-off Inventory in every provisioner (~two dozen call
# sites), so rather than thread the setting through each one — and silently
# miss any added later — we raise the connector's *default* once at package
# import (see saorsa_deploy/__init__.py). Per-host inventory data still
# overrides these, so individual call sites can opt out or tune if needed.
SSH_CONNECT_RETRIES = 5
SSH_CONNECT_RETRY_MIN_DELAY = 2.0
SSH_CONNECT_RETRY_MAX_DELAY = 5.0


def configure_ssh_connect_retries(
    retries: int = SSH_CONNECT_RETRIES,
    min_delay: float = SSH_CONNECT_RETRY_MIN_DELAY,
    max_delay: float = SSH_CONNECT_RETRY_MAX_DELAY,
) -> bool:
    """Raise pyinfra's default SSH connect-retry count for every connection.

    Mutates the SSH connector's ``DataMeta`` defaults so that, absent an
    explicit per-host override, every ``connect_all`` retries transient
    connection failures (banner read errors, resets, EOF) with a randomised
    backoff between ``min_delay`` and ``max_delay`` seconds.

    Returns ``True`` if the defaults were applied. Defensive against pyinfra's
    internals shifting across versions: if the expected module / keys are
    absent this is a no-op (connections simply keep the old no-retry
    behaviour) rather than crashing the CLI on import.
    """
    try:
        from pyinfra.connectors.ssh import connector_data_meta
    except Exception:
        return False

    applied = False
    for key, value in (
        ("ssh_connect_retries", retries),
        ("ssh_connect_retry_min_delay", min_delay),
        ("ssh_connect_retry_max_delay", max_delay),
    ):
        meta = connector_data_meta.get(key)
        if meta is not None:
            meta.default = value
            applied = True
    return applied


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
