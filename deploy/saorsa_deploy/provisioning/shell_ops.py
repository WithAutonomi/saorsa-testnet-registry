"""Helpers for building pyinfra shell operations efficiently."""


def as_single_shell_command(commands: list[str]) -> str:
    """Join multiple shell commands into ONE command string.

    pyinfra runs each entry of a ``server.shell(commands=[...])`` list as a
    separate SSH round-trip, and an operation only finishes once the slowest
    host has run *all* of them. An op built from 15-16 commands (e.g. one
    ``cat > unit <<EOF`` per node service) therefore costs 15-16 sequential
    round-trips on the slowest host — minutes on a high-latency VM. Collapsing
    them into a single ``sh -c`` script makes it one round-trip per host.

    ``set -e`` preserves fail-fast: any sub-command failing fails the whole op,
    matching the per-command exit-code checking pyinfra did when the commands
    were separate list entries. Commands that already guard with ``|| (...)``
    fallbacks (idempotency echoes, verify-or-exit) stay correct under ``set -e``.
    """
    return "set -e\n" + "\n".join(commands)
