import subprocess


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
