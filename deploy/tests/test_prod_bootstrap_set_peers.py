"""``prod bootstrap set-peers`` (V2-1309).

The seven production bootstrap units carried hand-written ``--bootstrap`` peer
lists naming hosts that had not been part of the tier for a long time -- of the
three peers five of them listed, none answered. The tier worked anyway because
every host had a populated routing snapshot and peer cache, so the defect was
invisible right up until a host lost its data directory and had at most one
live peer to rejoin through.

The tests here are about the two properties that make the rewrite safe to run
against all seven: a host never lists itself, and every line of the unit that
is not a ``--bootstrap`` line survives byte for byte -- these units carry
hand-maintained arguments (the rewards address, the log flags) that nothing
here knows the reasons for.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.cmd import prod_bootstrap_upgrade as cmd
from saorsa_deploy.prod_bootstrap_upgrade import (
    PEER_FLAG,
    PEER_PORT,
    peers_for,
    set_bootstrap_peers,
    started_after_restart,
    unit_peers,
)

#: The live ``ant-bootstrap.service`` from ant-bootstrap-fsn1, read 2026-09-24,
#: carrying the two stale peers this command exists to replace.
LIVE_UNIT = """[Unit]
Description=Autonomi Production Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ant
ExecStart=/opt/ant-node/ant-node \\
  --port 10000 \\
  --root-dir /mnt/ant-node-storage/data/ant-bootstrap \\
  --enable-logging \\
  --log-dir /mnt/ant-node-storage/logs/ant-bootstrap \\
  --log-format json \\
  --log-max-files 14 \\
  --stop-on-upgrade \\
  --bootstrap 207.148.94.42:10000 \\
  --bootstrap 35.88.183.208:46441 \\
  --rewards-address 0xc20c19b57629fc22E8e1e50a3b924A20b0fA25AA \\
  --evm-network arbitrum-one
Restart=always
RestartSec=20
LimitNOFILE=65536
MemoryMax=3G

[Install]
WantedBy=multi-user.target
"""

LOG_DIR = "/mnt/ant-node-storage/logs/ant-bootstrap"

#: The seven bootstraps, in inventory order.
TIER_IPS = [
    "207.148.94.42",
    "45.32.234.11",
    "149.28.72.19",
    "108.61.23.44",
    "65.108.12.7",
    "5.161.99.3",
    "18.222.1.9",
]


def _starting_line() -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-24T10:00:00.000000Z",
            "level": "INFO",
            "message": "ant-node starting",
            "version": "0.20.0",
            "target": "ant_node",
        }
    )


class TestPeerRule:
    """1. Six peers, the node port, and never the host itself."""

    def test_a_host_never_lists_itself(self):
        for ip in TIER_IPS:
            peers = peers_for(ip, TIER_IPS)
            assert f"{ip}:{PEER_PORT}" not in peers

    def test_every_other_host_is_listed(self):
        peers = peers_for(TIER_IPS[0], TIER_IPS)
        assert peers == [f"{ip}:{PEER_PORT}" for ip in TIER_IPS[1:]]
        assert len(peers) == len(TIER_IPS) - 1

    def test_inventory_order_is_kept(self):
        # The list is what an operator reads back out of the unit, so it should
        # look like the inventory rather than a set's iteration order.
        peers = peers_for(TIER_IPS[3], TIER_IPS)
        assert peers == [f"{ip}:{PEER_PORT}" for ip in TIER_IPS if ip != TIER_IPS[3]]

    def test_the_port_is_the_one_clients_use(self):
        # ant-core's bootstrap_peers.toml lists the whole tier on udp/10000;
        # the stale unit peers named ports (46441, 50141) nothing listens on.
        assert PEER_PORT == 10000

    def test_an_unknown_host_gets_the_whole_list(self):
        peers = peers_for("203.0.113.9", TIER_IPS)
        assert len(peers) == len(TIER_IPS)


class TestUnitRewrite:
    """2. Only the --bootstrap lines move."""

    def test_the_new_peers_are_written(self):
        peers = peers_for(TIER_IPS[4], TIER_IPS)
        edited = set_bootstrap_peers(LIVE_UNIT, peers)
        assert unit_peers(edited) == peers

    def test_the_stale_peers_are_gone(self):
        edited = set_bootstrap_peers(LIVE_UNIT, peers_for(TIER_IPS[4], TIER_IPS))
        assert "35.88.183.208:46441" not in edited

    def test_every_other_line_is_byte_identical(self):
        peers = peers_for(TIER_IPS[4], TIER_IPS)
        edited = set_bootstrap_peers(LIVE_UNIT, peers)
        before = [line for line in LIVE_UNIT.splitlines() if PEER_FLAG not in line]
        after = [line for line in edited.splitlines() if PEER_FLAG not in line]
        assert after == before

    def test_the_peers_stay_where_they_were(self):
        # Rewriting them in place keeps the argument order an operator reads
        # back out of the unit the same as the one they have always seen.
        edited = set_bootstrap_peers(LIVE_UNIT, ["1.1.1.1:10000"])
        lines = edited.splitlines()
        assert lines[lines.index("  --stop-on-upgrade \\") + 1] == (
            "  --bootstrap 1.1.1.1:10000 \\"
        )

    def test_the_continuations_still_parse(self):
        edited = set_bootstrap_peers(LIVE_UNIT, peers_for(TIER_IPS[4], TIER_IPS))
        exec_lines = [
            line
            for line in edited.splitlines()
            if line.startswith("ExecStart=") or line.startswith("  --")
        ]
        assert all(line.endswith(" \\") for line in exec_lines[:-1])
        assert not exec_lines[-1].endswith("\\")

    def test_it_is_idempotent(self):
        peers = peers_for(TIER_IPS[4], TIER_IPS)
        once = set_bootstrap_peers(LIVE_UNIT, peers)
        assert set_bootstrap_peers(once, peers) == once

    def test_a_unit_already_correct_is_returned_unchanged(self):
        # The identity of the object matters less than the bytes, but a no-op
        # is what stops a re-run restarting hosts it already finished.
        peers = ["207.148.94.42:10000", "35.88.183.208:46441"]
        assert set_bootstrap_peers(LIVE_UNIT, peers) == LIVE_UNIT

    def test_dropping_every_peer_removes_the_flag(self):
        edited = set_bootstrap_peers(LIVE_UNIT, [])
        assert PEER_FLAG not in edited
        assert unit_peers(edited) == []

    def test_a_unit_with_no_peers_gains_them(self):
        stripped = set_bootstrap_peers(LIVE_UNIT, [])
        peers = peers_for(TIER_IPS[4], TIER_IPS)
        edited = set_bootstrap_peers(stripped, peers)
        assert unit_peers(edited) == peers
        assert edited.splitlines()[-1] == "WantedBy=multi-user.target"

    def test_trailing_peers_leave_the_block_terminated(self):
        # The peers were the last arguments, so the line that becomes last must
        # not keep a continuation pointing at "Restart=always".
        unit = LIVE_UNIT.replace(
            "  --rewards-address 0xc20c19b57629fc22E8e1e50a3b924A20b0fA25AA \\\n"
            "  --evm-network arbitrum-one\n",
            "",
        ).replace(
            "  --bootstrap 35.88.183.208:46441 \\\n",
            "  --bootstrap 35.88.183.208:46441\n",
        )
        edited = set_bootstrap_peers(unit, ["1.1.1.1:10000", "2.2.2.2:10000"])
        lines = edited.splitlines()
        assert lines[lines.index("Restart=always") - 1] == ("  --bootstrap 2.2.2.2:10000")

    def test_trailing_newline_is_preserved(self):
        assert set_bootstrap_peers(LIVE_UNIT, ["1.1.1.1:10000"]).endswith("\n")

    def test_single_line_exec_start_is_rewritten(self):
        unit = (
            "[Service]\nUser=ant\n"
            "ExecStart=/opt/ant-node/ant-node --root-dir /d --stop-on-upgrade "
            "--bootstrap 9.9.9.9:1 --evm-network arbitrum-one\n"
        )
        edited = set_bootstrap_peers(unit, ["1.1.1.1:10000", "2.2.2.2:10000"])
        assert unit_peers(edited) == ["1.1.1.1:10000", "2.2.2.2:10000"]
        assert "--evm-network arbitrum-one" in edited


class TestRestartGate:
    """3. The host must say it came back."""

    def test_a_start_line_since_the_cursor_passes(self):
        assert started_after_restart(_starting_line())

    def test_no_start_line_does_not_pass(self):
        # The cursor is taken immediately before the restart, so an empty slice
        # means the service never got as far as logging a start.
        assert not started_after_restart("")

    def test_any_version_counts(self):
        # Unlike the upgrade, set-peers swaps no binary: the host comes back on
        # the build it went down on, so there is no version to match against.
        line = json.loads(_starting_line())
        line["version"] = "0.19.0"
        assert started_after_restart(json.dumps(line))

    def test_non_json_lines_are_skipped(self):
        assert not started_after_restart("Sep 24 10:00:00 systemd: Started.\n")


class _FakeHost:
    """One bootstrap's SSH surface: the unit file, and what was run on it."""

    def __init__(self, unit: str = LIVE_UNIT, *, restart_logs: bool = True):
        self.unit = unit
        self.restart_logs = restart_logs
        self.commands: list[str] = []

    def run(self, command: str, stdin: str | None = None) -> str:
        self.commands.append(command)
        if command.startswith("cat /etc/systemd/system/"):
            return self.unit
        if command.startswith("cat > /etc/systemd/system/"):
            self.unit = stdin or ""
            return ""
        if "wc -c" in command:
            return f"{LOG_DIR}/ant-node.log 4096\n"
        if command.startswith("tail -c") or "tail -c" in command:
            return _starting_line() if self.restart_logs else ""
        return ""


def _vm(name: str, ip: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        ip=ip,
        provider="vultr",
        region="ewr",
        ssh_user="root",
        ssh_key="~/.ssh/ant-production",
    )


def _run_set_peers(hosts: dict[str, _FakeHost], limit_to: str | None = None):
    """Drive the command against fake hosts, returning (exit code, hosts)."""
    vms = [_vm(f"ant-bootstrap-{index}", ip) for index, ip in enumerate(TIER_IPS)]
    console = Console(record=True)

    def fake_run(vm, ssh_key, command, *, timeout=120, stdin=None):
        return hosts[vm.ip].run(command, stdin)

    args = SimpleNamespace(
        inventory_path="/nowhere/bootstrap-inventory.json",
        peer_port=PEER_PORT,
        limit_to=limit_to,
        ssh_key_path=None,
        yes=True,
    )
    with (
        patch.object(
            cmd,
            "_load_bootstraps",
            side_effect=lambda _p, limit, _c: (
                vms if limit is None else [v for v in vms if v.name.endswith(limit)]
            ),
        ),
        patch.object(cmd, "_run", side_effect=fake_run),
        patch.object(cmd, "_is_active", return_value=True),
        patch.object(cmd, "clear_known_hosts"),
        patch.object(cmd, "Console", return_value=console),
        patch.object(cmd.time, "sleep"),
        # The gate polls once before it looks at the clock, so a zero timeout
        # still reads the log -- it just does not wait for a second look.
        patch.object(cmd, "HEALTH_TIMEOUT_SECS", 0),
    ):
        try:
            cmd.cmd_prod_bootstrap_set_peers(args)
        except SystemExit as e:
            return e.code
    return 0


class TestSetPeersRun:
    """4. Serial, gated, and never leaving a host on a unit it cannot run."""

    def test_every_host_gets_the_other_six(self):
        hosts = {ip: _FakeHost() for ip in TIER_IPS}
        assert _run_set_peers(hosts) == 0
        for ip in TIER_IPS:
            assert unit_peers(hosts[ip].unit) == peers_for(ip, TIER_IPS)

    def test_a_failed_restart_restores_the_unit_and_stops(self):
        # The third host never logs a start: it is put back on the unit it had
        # and the hosts after it are not touched at all.
        hosts = {ip: _FakeHost() for ip in TIER_IPS}
        hosts[TIER_IPS[2]].restart_logs = False
        assert _run_set_peers(hosts) == 1
        assert hosts[TIER_IPS[2]].unit == LIVE_UNIT
        assert unit_peers(hosts[TIER_IPS[0]].unit) == peers_for(TIER_IPS[0], TIER_IPS)
        assert hosts[TIER_IPS[3]].commands == []

    def test_a_host_already_correct_is_not_restarted(self):
        hosts = {ip: _FakeHost() for ip in TIER_IPS}
        hosts[TIER_IPS[1]].unit = set_bootstrap_peers(LIVE_UNIT, peers_for(TIER_IPS[1], TIER_IPS))
        assert _run_set_peers(hosts) == 0
        assert not any("systemctl restart" in command for command in hosts[TIER_IPS[1]].commands)

    def test_limit_to_still_computes_peers_from_the_whole_inventory(self):
        # A host's six peers are the other six bootstraps whether or not this
        # run is touching them.
        hosts = {ip: _FakeHost() for ip in TIER_IPS}
        assert _run_set_peers(hosts, limit_to="2") == 0
        assert unit_peers(hosts[TIER_IPS[2]].unit) == peers_for(TIER_IPS[2], TIER_IPS)
        assert hosts[TIER_IPS[0]].commands == []


class TestConfirmation:
    """5. No TTY and no -y is a refusal, not a hang."""

    def test_non_tty_without_yes_exits(self):
        console = Console(record=True)
        with patch("sys.stdin.isatty", return_value=False):
            with pytest.raises(SystemExit):
                cmd._confirm_set_peers([_vm("a", TIER_IPS[0])], PEER_PORT, console)
