"""``prod bootstrap upgrade`` / ``prod bootstrap webrtc-addresses`` (V2-1307).

These two verbs run against the seven production bootstraps every client on the
network enters through, with no canary in front of them, so the tests here are
mostly about the refusals: a binary that is not the one the release published, a
unit this command must not touch, a "healthy" host that is actually the old
version restarted by ``Restart=always``, and a published address list that is
short a host.

The unit fixture is the live ``ant-bootstrap.service`` from ant-bootstrap-fsn1
(read 2026-09-24), not one rendered by ``build_systemd_unit`` — the live units
carry hand-maintained arguments the deploy-time renderer never wrote, and the
append-only edit exists precisely to preserve them.
"""

import hashlib
import hmac
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy import webrtc_probe
from saorsa_deploy.cmd import prod_bootstrap_upgrade as cmd
from saorsa_deploy.prod_bootstrap_upgrade import (
    WEBRTC_PORT_FLAG,
    add_webrtc_port,
    build_log_tail_command,
    evaluate_health,
    expected_digest,
    parse_log_cursor,
    parse_sha256sums,
    parse_unit,
    parse_version_output,
    release_urls,
    remove_webrtc_port,
    require_version,
    supports_webrtc,
)

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

BINARY_PATH = "/opt/ant-node/ant-node"
ROOT_DIR = "/mnt/ant-node-storage/data/ant-bootstrap"
LOG_DIR = "/mnt/ant-node-storage/logs/ant-bootstrap"
MULTIADDR = (
    "/ip4/10.0.0.7/udp/10001/webrtc-direct/certhash/uEiDabc_123"
    "/p2p/" + "a" * 64
)


def _starting_line(version: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-24T10:00:00.000000Z",
            "level": "INFO",
            "message": "ant-node starting",
            "version": version,
            "commit": "f1da771",
            "target": "ant_node",
        }
    )


def _webrtc_line(bind: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-09-24T10:00:01.000000Z",
            "level": "INFO",
            "message": "ADR-0015 WebRTC Direct listening",
            "bind": bind,
            "target": "saorsa_transport",
        }
    )


class TestReleasePreflight:
    """1. The archive that reaches seven production hosts is the published one."""

    def test_digest_is_read_from_the_sums_file(self):
        digest = "a" * 64
        sums = f"{digest}  ant-node-cli-linux-x64.tar.gz\n{'b' * 64}  other.tar.gz\n"
        assert expected_digest(sums, "ant-node-cli-linux-x64.tar.gz") == digest

    def test_binary_starred_form_is_accepted(self):
        # sha256sum's binary mode writes ` *name`; GitHub releases carry both.
        sums = f"{'c' * 64} *ant-node-cli-linux-x64.tar.gz\n"
        assert expected_digest(sums, "ant-node-cli-linux-x64.tar.gz") == "c" * 64

    def test_asset_absent_from_the_sums_file_is_refused(self):
        # "Nothing to check against" must not reach production by the same code
        # path as "checked and matched".
        with pytest.raises(ValueError, match="not listed"):
            expected_digest(f"{'d' * 64}  something-else.tar.gz\n", "ant-node.tar.gz")

    def test_empty_sums_file_is_refused(self):
        with pytest.raises(ValueError, match="not listed"):
            expected_digest("", "ant-node-cli-linux-x64.tar.gz")

    def test_unparseable_lines_are_ignored(self):
        sums = f"# a comment\n\n{'e' * 64}  ant-node-cli-linux-x64.tar.gz\n"
        assert parse_sha256sums(sums) == {"ant-node-cli-linux-x64.tar.gz": "e" * 64}

    def test_version_output_is_parsed(self):
        assert parse_version_output("ant-node 0.20.0\n") == "0.20.0"

    def test_wrong_version_is_refused(self):
        with pytest.raises(ValueError, match="expected ant-node 0.20.0"):
            require_version("ant-node 0.19.0\n", "0.20.0")

    def test_matching_version_passes(self):
        require_version("ant-node 0.20.0\n", "0.20.0")

    def test_output_that_is_not_ant_node_is_refused(self):
        # A wrong-architecture or truncated download that runs at all looks
        # like this, and must not be read as a version.
        with pytest.raises(ValueError, match="not ant-node --version output"):
            require_version("bash: cannot execute binary file\n", "0.20.0")

    def test_release_urls_point_at_the_tag(self):
        archive, sums = release_urls("0.20.0")
        assert archive.endswith(
            "/WithAutonomi/ant-node/releases/download/v0.20.0/"
            "ant-node-cli-linux-x64.tar.gz"
        )
        assert sums.endswith("/v0.20.0/SHA256SUMS.txt")


class TestUnitParsing:
    """2. The live unit is read, not assumed."""

    def test_binary_path_comes_from_exec_start(self):
        # Not from bootstrap_production.BINARY_INSTALL_PATH: the live tier runs
        # /opt/ant-node/ant-node, and installing over the constant would leave
        # the service running the old binary while reporting success.
        assert parse_unit(LIVE_UNIT).binary_path == BINARY_PATH

    def test_root_dir_comes_from_exec_start(self):
        assert parse_unit(LIVE_UNIT).root_dir == ROOT_DIR

    def test_log_dir_comes_from_exec_start(self):
        assert parse_unit(LIVE_UNIT).log_dir == LOG_DIR

    def test_service_user_comes_from_the_unit(self):
        assert parse_unit(LIVE_UNIT).service_user == "ant"

    def test_no_webrtc_port_on_the_live_unit(self):
        assert parse_unit(LIVE_UNIT).webrtc_port is None

    def test_webrtc_port_is_read_back_after_the_edit(self):
        assert parse_unit(add_webrtc_port(LIVE_UNIT, 10001)).webrtc_port == 10001

    def test_unit_without_stop_on_upgrade_is_refused(self):
        # The node's own auto-upgrader is live on such a host, so a binary
        # swapped in underneath it can be replaced minutes later.
        unit = LIVE_UNIT.replace("  --stop-on-upgrade \\\n", "")
        with pytest.raises(ValueError, match="--stop-on-upgrade"):
            parse_unit(unit)

    def test_unit_without_root_dir_is_refused(self):
        unit = LIVE_UNIT.replace(f"  --root-dir {ROOT_DIR} \\\n", "")
        with pytest.raises(ValueError, match="--root-dir"):
            parse_unit(unit)

    def test_unit_without_exec_start_is_refused(self):
        with pytest.raises(ValueError, match="no ExecStart"):
            parse_unit("[Service]\nType=simple\n")

    def test_single_line_exec_start_is_parsed(self):
        unit = (
            "[Service]\nUser=ant\n"
            f"ExecStart={BINARY_PATH} --root-dir {ROOT_DIR} --stop-on-upgrade\n"
        )
        parsed = parse_unit(unit)
        assert (parsed.binary_path, parsed.root_dir) == (BINARY_PATH, ROOT_DIR)


class TestUnitEdit:
    """3. The edit is append-only and idempotent."""

    def test_flag_is_appended(self):
        assert f"  {WEBRTC_PORT_FLAG} 10001" in add_webrtc_port(LIVE_UNIT, 10001)

    def test_every_other_line_is_byte_identical(self):
        # The live units carry hand-maintained arguments (stale --bootstrap
        # peers among them) that a re-render from build_systemd_unit would
        # silently discard.
        before = LIVE_UNIT.splitlines()
        after = add_webrtc_port(LIVE_UNIT, 10001).splitlines()
        assert len(after) == len(before) + 1
        # Only the previously-last ExecStart line changes, and only by a "\".
        last = before.index("  --evm-network arbitrum-one")
        assert after[:last] == before[:last]
        assert after[last] == before[last] + " \\"
        assert after[last + 1] == f"  {WEBRTC_PORT_FLAG} 10001"
        assert after[last + 2 :] == before[last + 1 :]

    def test_applying_it_twice_yields_one_flag(self):
        once = add_webrtc_port(LIVE_UNIT, 10001)
        assert add_webrtc_port(once, 10001) == once
        assert once.count(WEBRTC_PORT_FLAG) == 1

    def test_an_existing_pin_is_left_alone(self):
        # Re-pinning a port clients have already cached a multiaddr for is
        # worse than leaving a deliberate choice as it is.
        once = add_webrtc_port(LIVE_UNIT, 42768)
        assert add_webrtc_port(once, 10001) == once

    def test_removal_is_the_exact_inverse(self):
        assert remove_webrtc_port(add_webrtc_port(LIVE_UNIT, 10001)) == LIVE_UNIT

    def test_removal_of_an_absent_flag_is_a_no_op(self):
        assert remove_webrtc_port(LIVE_UNIT) == LIVE_UNIT

    def test_single_line_exec_start_round_trips(self):
        unit = (
            "[Service]\nUser=ant\n"
            f"ExecStart={BINARY_PATH} --root-dir {ROOT_DIR} --stop-on-upgrade\n"
        )
        edited = add_webrtc_port(unit, 10001)
        assert f"{WEBRTC_PORT_FLAG} 10001" in edited
        assert remove_webrtc_port(edited) == unit

    def test_trailing_newline_is_preserved(self):
        assert add_webrtc_port(LIVE_UNIT, 10001).endswith("\n")


class TestHealthGate:
    """4. Only the new version, bound to the pinned port, counts."""

    def test_matching_start_and_listener_pass(self):
        log = "\n".join([_starting_line("0.20.0"), _webrtc_line("0.0.0.0:10001")])
        verdict = evaluate_health(log, "0.20.0", 10001, expect_webrtc=True)
        assert verdict.ok
        assert verdict.bind == "0.0.0.0:10001"

    def test_old_version_restarting_does_not_pass(self):
        # Restart=always brings the host back on the *old* binary when the new
        # one will not run, and that also writes "ant-node starting".
        log = "\n".join([_starting_line("0.19.0"), _webrtc_line("0.0.0.0:10001")])
        assert not evaluate_health(log, "0.20.0", 10001, expect_webrtc=True).ok

    def test_listener_on_another_port_does_not_pass(self):
        log = "\n".join([_starting_line("0.20.0"), _webrtc_line("0.0.0.0:45231")])
        verdict = evaluate_health(log, "0.20.0", 10001, expect_webrtc=True)
        assert not verdict.ok
        assert verdict.webrtc is False

    def test_missing_listener_line_does_not_pass(self):
        verdict = evaluate_health(
            _starting_line("0.20.0"), "0.20.0", 10001, expect_webrtc=True
        )
        assert not verdict.ok

    def test_below_0_20_0_the_start_line_alone_passes(self):
        verdict = evaluate_health(
            _starting_line("0.19.0"), "0.19.0", 10001, expect_webrtc=False
        )
        assert verdict.ok
        assert verdict.webrtc is None
        assert "not expected" in verdict.describe()

    def test_no_start_line_never_passes(self):
        assert not evaluate_health(
            _webrtc_line("0.0.0.0:10001"), "0.20.0", 10001, expect_webrtc=False
        ).ok

    def test_non_json_lines_are_skipped(self):
        log = "\n".join(["not json at all", "", _starting_line("0.20.0")])
        assert evaluate_health(log, "0.20.0", 10001, expect_webrtc=False).ok

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("0.19.0", False),
            ("0.20.0", True),
            ("0.20.1", True),
            ("0.21.0", True),
            ("1.0.0", True),
            ("0.20.0-rc.1", True),
            ("", False),
        ],
    )
    def test_webrtc_support_by_version(self, version, expected):
        assert supports_webrtc(version) is expected


class TestLogCursor:
    def test_cursor_is_parsed(self):
        assert parse_log_cursor(f"{LOG_DIR}/ant-node.2026-09-24.log 4096\n") == (
            f"{LOG_DIR}/ant-node.2026-09-24.log",
            4096,
        )

    def test_missing_cursor_reads_as_no_offset(self):
        assert parse_log_cursor("") == (None, 0)

    def test_tail_starts_after_the_cursor(self):
        command = build_log_tail_command(LOG_DIR, f"{LOG_DIR}/today.log", 4096)
        # +1: tail -c counts from byte one, so the offset is the last byte the
        # cursor saw, not the first byte wanted.
        assert "tail -c +4097" in command

    def test_a_rotated_log_is_read_whole(self):
        # The offset into yesterday's file means nothing in today's.
        command = build_log_tail_command(LOG_DIR, f"{LOG_DIR}/today.log", 4096)
        assert 'else cat "$f"' in command


class TestWebrtcProbe:
    """6. The probe is a valid ICE check, not a bare STUN binding."""

    def _request(self):
        client_pwd = webrtc_probe.random_ice_string()
        ufrag = webrtc_probe.server_ufrag(client_pwd)
        txid = b"\x01" * 12
        return (
            webrtc_probe.build_binding_request(f"{ufrag}:client", ufrag, txid),
            ufrag,
            txid,
        )

    def test_request_is_a_binding_request(self):
        request, _, txid = self._request()
        parsed = webrtc_probe.parse_stun_message(request)
        assert parsed.message_type == webrtc_probe.BINDING_REQUEST
        assert parsed.transaction_id == txid

    def test_server_ufrag_uses_the_v2_scheme(self):
        # saorsa-transport admits a dialler whose server fragment is this,
        # which is what lets a probe with no SDP exchange be answered at all.
        assert webrtc_probe.server_ufrag("pwd") == "saorsa+webrtc+v2/pwd"

    def test_username_carries_both_fragments(self):
        request, ufrag, _ = self._request()
        parsed = webrtc_probe.parse_stun_message(request)
        username = dict(parsed.attributes)[webrtc_probe.ATTR_USERNAME]
        assert username.decode() == f"{ufrag}:client"

    def test_fingerprint_recomputes(self):
        request, _, _ = self._request()
        # Everything up to the FINGERPRINT attribute, with the header length
        # declaring the fingerprint's own 8 bytes.
        body = request[:-8]
        header = (
            struct.pack("!HH", webrtc_probe.BINDING_REQUEST, len(body) - 20 + 8)
            + request[4:20]
        )
        expected = (
            zlib.crc32(header + body[20:]) & 0xFFFFFFFF
        ) ^ webrtc_probe.FINGERPRINT_XOR
        assert struct.unpack("!I", request[-4:])[0] == expected

    def test_message_integrity_recomputes(self):
        request, ufrag, _ = self._request()
        # The digest covers everything before MESSAGE-INTEGRITY, with the
        # header length declaring MESSAGE-INTEGRITY's own 24 bytes.
        integrity_at = len(request) - 8 - 24
        body = request[20:integrity_at]
        header = (
            struct.pack("!HH", webrtc_probe.BINDING_REQUEST, len(body) + 24)
            + request[4:20]
        )
        expected = hmac.new(ufrag.encode(), header + body, hashlib.sha1).digest()
        assert request[integrity_at + 4 : integrity_at + 24] == expected

    def test_attributes_are_in_order(self):
        request, _, _ = self._request()
        types = [t for t, _ in webrtc_probe.parse_stun_message(request).attributes]
        assert types == [
            webrtc_probe.ATTR_USERNAME,
            webrtc_probe.ATTR_MESSAGE_INTEGRITY,
            webrtc_probe.ATTR_FINGERPRINT,
        ]

    def test_ice_strings_are_fresh_and_well_formed(self):
        first, second = (
            webrtc_probe.random_ice_string(),
            webrtc_probe.random_ice_string(),
        )
        assert first != second
        assert len(first) == 32
        assert all(c.isalnum() or c in "+/" for c in first)

    def test_a_listener_challenge_parses(self):
        # The listener answers with a connectivity check of its own: a Binding
        # Request carrying ICE-CONTROLLED and PRIORITY. Any well-formed STUN
        # message back is the probe's pass condition.
        attrs = b""
        for attr_type, value in (
            (webrtc_probe.ATTR_USERNAME, b"client:server"),
            (0x8029, b"\x00" * 8),  # ICE-CONTROLLED
            (0x0024, struct.pack("!I", 0x7E0000FF)),  # PRIORITY
        ):
            padded = value + b"\x00" * (-len(value) % 4)
            attrs += struct.pack("!HH", attr_type, len(value)) + padded
        challenge = (
            struct.pack(
                "!HHI",
                webrtc_probe.BINDING_REQUEST,
                len(attrs),
                webrtc_probe.MAGIC_COOKIE,
            )
            + b"\x02" * 12
            + attrs
        )
        parsed = webrtc_probe.parse_stun_message(challenge)
        assert 0x8029 in dict(parsed.attributes)

    @pytest.mark.parametrize(
        "data,reason",
        [
            (b"", "too short"),
            (b"\x00" * 19, "too short"),
            (b"\xc0" + b"\x00" * 19, "leading bits"),
            (b"\x00\x01\x00\x00" + b"\x00" * 16, "magic cookie"),
        ],
    )
    def test_malformed_datagrams_are_refused(self, data, reason):
        # A stray datagram from something else on the port must not read as a
        # reachable WebRTC listener.
        with pytest.raises(ValueError):
            webrtc_probe.parse_stun_message(data)

    def test_a_listener_that_answers_is_reachable(self):
        # The socket path end to end, against a loopback stand-in that replies
        # with the challenge a real WebRTC-Direct listener sends.
        import socket
        import threading

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(5)
        port = listener.getsockname()[1]

        def respond():
            try:
                _, addr = listener.recvfrom(2048)
            except OSError:
                return
            attrs = struct.pack("!HH", 0x8029, 8) + b"\x00" * 8
            listener.sendto(
                struct.pack(
                    "!HHI",
                    webrtc_probe.BINDING_REQUEST,
                    len(attrs),
                    webrtc_probe.MAGIC_COOKIE,
                )
                + b"\x03" * 12
                + attrs,
                addr,
            )

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        try:
            assert webrtc_probe.probe("127.0.0.1", port, 5.0) is True
        finally:
            thread.join(timeout=5)
            listener.close()

    def test_a_silent_listener_is_not_reachable(self):
        # saorsa-transport's admission filter answers an unauthenticated or
        # malformed check with silence, which is also what a blocked port looks
        # like — so a timeout is the only negative signal there is.
        import socket

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        try:
            assert webrtc_probe.probe("127.0.0.1", port, 0.2, attempts=2) is False
        finally:
            listener.close()

    def test_a_declared_length_that_does_not_match_is_refused(self):
        message = (
            struct.pack("!HHI", 0x0001, 8, webrtc_probe.MAGIC_COOKIE) + b"\x00" * 12
        )
        with pytest.raises(ValueError, match="declared length"):
            webrtc_probe.parse_stun_message(message)


def _vm(name, ip, provider="hetzner", ssh_user="root"):
    return SimpleNamespace(
        name=name,
        provider=provider,
        provider_id="",
        region="fsn1",
        ip=ip,
        ssh_user=ssh_user,
        ssh_key="~/.ssh/ant-production",
        services=(),
    )


class _FakeHost:
    """Records every command run against one host and answers the reads."""

    def __init__(self, unit=LIVE_UNIT, log="", multiaddr=MULTIADDR, active=True):
        self.unit = unit
        self.log = log
        self.multiaddr = multiaddr
        self.active = active
        self.commands: list[str] = []
        self.written_units: list[str] = []

    def run(self, command: str, stdin: str | None) -> str:
        self.commands.append(command)
        if command.startswith(f"cat > {cmd.UNIT_FILE_PATH}"):
            self.written_units.append(stdin or "")
            self.unit = stdin or ""
            return ""
        if command == f"cat {cmd.UNIT_FILE_PATH}":
            return self.unit
        if command.endswith(f"/{cmd.MULTIADDR_FILENAME}"):
            if self.multiaddr is None:
                raise RuntimeError("No such file or directory")
            return self.multiaddr
        if "--version" in command:
            return "ant-node 0.20.0\n"
        if command.startswith("f=$(ls -1t") and "wc -c" in command:
            return f"{LOG_DIR}/today.log 0\n"
        if "tail -c" in command or command.startswith("cat $(ls"):
            return self.log
        return ""


@pytest.fixture
def fake_hosts(monkeypatch):
    """Patch out every SSH/network call the two commands make."""
    hosts: dict[str, _FakeHost] = {}

    def _run(vm, ssh_key, command, *, timeout=120, stdin=None):
        return hosts[vm.name].run(command, stdin)

    def _ssh(vm, ssh_key, command, *, timeout=120, stdin=None):
        host = hosts[vm.name]
        host.commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="active" if host.active else "failed",
            stderr="",
        )

    monkeypatch.setattr(cmd, "_run", _run)
    monkeypatch.setattr(cmd, "_ssh", _ssh)
    monkeypatch.setattr(cmd, "clear_known_hosts", lambda *a, **k: None)
    monkeypatch.setattr(cmd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cmd, "INTER_HOST_WAIT_SECS", 0)
    monkeypatch.setattr(cmd, "_HEALTH_POLL_SECS", 0)
    return hosts


def _upgrade_args(**overrides):
    defaults = {
        "node_version": "0.20.0",
        "inventory_path": "inventory.json",
        "webrtc_port": 10001,
        "limit_to": None,
        "skip_firewall": True,
        "ssh_key_path": None,
        "yes": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestUpgradeFailurePath:
    """5. A failed host is restored, and the run stops before the next one."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch, fake_hosts, tmp_path):
        self.hosts = fake_hosts
        self.vms = [_vm("ant-bootstrap-fsn1", "1.1.1.1"), _vm("ant-bootstrap-ash", "2.2.2.2")]
        monkeypatch.setattr(cmd, "_load_bootstraps", lambda *a, **k: self.vms)
        archive = tmp_path / "ant-node.tar.gz"
        archive.write_bytes(b"")
        monkeypatch.setattr(cmd, "_preflight_binary", lambda *a, **k: archive)
        monkeypatch.setattr(cmd, "_install_binary", lambda *a, **k: None)
        monkeypatch.setattr(cmd.webrtc_probe, "probe", lambda *a, **k: True)

    def _run_upgrade(self, monkeypatch, healthy_log, timeout=0):
        monkeypatch.setattr(cmd, "HEALTH_TIMEOUT_SECS", timeout)
        for vm in self.vms:
            self.hosts[vm.name] = _FakeHost(log=healthy_log)
        with pytest.raises(SystemExit) as exit_info:
            cmd.cmd_prod_bootstrap_upgrade(_upgrade_args())
        return exit_info.value.code

    def test_a_failed_gate_exits_non_zero(self, monkeypatch):
        assert self._run_upgrade(monkeypatch, "") == 1

    def test_the_backup_binary_is_restored(self, monkeypatch):
        self._run_upgrade(monkeypatch, "")
        commands = self.hosts["ant-bootstrap-fsn1"].commands
        assert f"mv {BINARY_PATH}.backup {BINARY_PATH}" in commands

    def test_the_appended_flag_is_removed(self, monkeypatch):
        self._run_upgrade(monkeypatch, "")
        written = self.hosts["ant-bootstrap-fsn1"].written_units
        # Written twice: the edit, then the restore.
        assert WEBRTC_PORT_FLAG in written[0]
        assert WEBRTC_PORT_FLAG not in written[-1]
        assert written[-1] == LIVE_UNIT

    def test_the_next_host_is_never_touched(self, monkeypatch):
        # Seven bootstraps restarted onto a bad binary is an outage for
        # everyone, so a failure stops the run where it stands.
        self._run_upgrade(monkeypatch, "")
        assert self.hosts["ant-bootstrap-ash"].commands == []

    def test_an_old_version_in_the_log_still_fails(self, monkeypatch):
        # Restart=always brings the host back on the old binary, which writes a
        # start line of its own.
        log = "\n".join([_starting_line("0.19.0"), _webrtc_line("0.0.0.0:10001")])
        assert self._run_upgrade(monkeypatch, log, timeout=1) == 1

    def test_a_pre_0_20_0_version_never_gets_the_flag(self, monkeypatch):
        # ant-node 0.19.0 has no --webrtc-direct-port (verified on the live
        # tier), and clap exits on an unknown argument — so appending it would
        # leave a bootstrap that cannot start, which is exactly what a
        # same-version rehearsal of this command must not do.
        monkeypatch.setattr(cmd, "HEALTH_TIMEOUT_SECS", 1)
        for vm in self.vms:
            self.hosts[vm.name] = _FakeHost(log=_starting_line("0.19.0"))
        cmd.cmd_prod_bootstrap_upgrade(_upgrade_args(node_version="0.19.0"))
        for vm in self.vms:
            assert self.hosts[vm.name].written_units == []
            assert WEBRTC_PORT_FLAG not in self.hosts[vm.name].unit

    def test_a_pre_0_20_0_version_collects_no_addresses(self, monkeypatch):
        monkeypatch.setattr(cmd, "HEALTH_TIMEOUT_SECS", 1)
        collected = []
        monkeypatch.setattr(
            cmd, "_collect_multiaddrs", lambda *a, **k: collected.append(a)
        )
        for vm in self.vms:
            self.hosts[vm.name] = _FakeHost(log=_starting_line("0.19.0"))
        cmd.cmd_prod_bootstrap_upgrade(_upgrade_args(node_version="0.19.0"))
        assert collected == []

    def test_a_failed_probe_rolls_the_host_back(self, monkeypatch):
        monkeypatch.setattr(cmd.webrtc_probe, "probe", lambda *a, **k: False)
        log = "\n".join([_starting_line("0.20.0"), _webrtc_line("0.0.0.0:10001")])
        assert self._run_upgrade(monkeypatch, log, timeout=1) == 1
        assert self.hosts["ant-bootstrap-ash"].commands == []
        assert self.hosts["ant-bootstrap-fsn1"].written_units[-1] == LIVE_UNIT


class TestWebrtcAddresses:
    """7. The published list is complete, probed, and on the inventory IP."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch, fake_hosts, tmp_path):
        self.hosts = fake_hosts
        self.out = tmp_path / "prod_bootstrap_webrtc_multiaddrs.txt"
        self.vms = [
            _vm("ant-bootstrap-nrt", "207.148.94.42", provider="vultr"),
            _vm("ant-bootstrap-fsn1", "49.12.119.240"),
        ]
        monkeypatch.setattr(cmd, "_load_bootstraps", lambda *a, **k: self.vms)
        monkeypatch.setattr(cmd.webrtc_probe, "probe", lambda *a, **k: True)

    def _args(self):
        return SimpleNamespace(
            inventory_path="inventory.json",
            out=str(self.out),
            ssh_key_path=None,
        )

    def _populate(self, **overrides):
        for vm in self.vms:
            self.hosts[vm.name] = _FakeHost(**overrides.get(vm.name, {}))

    def test_addresses_are_rewritten_to_the_inventory_ip(self):
        # The two Hetzner bootstraps carry a second public address on the same
        # interface, and ant-node may advertise either; only the inventory one
        # is in the client's bootstrap list.
        self._populate()
        cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        lines = self.out.read_text().splitlines()
        assert lines[0].startswith("/ip4/207.148.94.42/udp/10001/webrtc-direct/")
        assert lines[1].startswith("/ip4/49.12.119.240/udp/10001/webrtc-direct/")

    def test_the_certhash_and_peer_id_are_kept(self):
        self._populate()
        cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        for line in self.out.read_text().splitlines():
            assert line.endswith("/webrtc-direct/certhash/uEiDabc_123/p2p/" + "a" * 64)

    def test_output_is_in_inventory_order(self):
        self._populate()
        cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        hosts = [line.split("/")[2] for line in self.out.read_text().splitlines()]
        assert hosts == [vm.ip for vm in self.vms]

    def test_a_missing_file_refuses_and_writes_nothing(self):
        # A short list published as the production bootstrap set silently
        # narrows every browser client's way in.
        self._populate(**{"ant-bootstrap-fsn1": {"multiaddr": None}})
        with pytest.raises(SystemExit) as exit_info:
            cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        assert exit_info.value.code == 1
        assert not self.out.exists()

    def test_a_malformed_address_refuses(self):
        self._populate(**{"ant-bootstrap-nrt": {"multiaddr": "/ip4/1.2.3.4/tcp/80"}})
        with pytest.raises(SystemExit):
            cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        assert not self.out.exists()

    def test_a_failed_probe_refuses(self, monkeypatch):
        monkeypatch.setattr(cmd.webrtc_probe, "probe", lambda *a, **k: False)
        self._populate()
        with pytest.raises(SystemExit):
            cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        assert not self.out.exists()

    def test_an_existing_file_survives_a_refusal(self):
        self.out.write_text("previous\n")
        self._populate(**{"ant-bootstrap-fsn1": {"multiaddr": None}})
        with pytest.raises(SystemExit):
            cmd.cmd_prod_bootstrap_webrtc_addresses(self._args())
        assert self.out.read_text() == "previous\n"


class TestBootstrapInventory:
    """The bootstrap inventory has no provider_id, and nothing here needs one."""

    def test_it_loads_without_provider_ids(self, tmp_path):
        path = tmp_path / "bootstrap-inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "testnet_id": "ant-prod-01-bootstraps",
                    "node_vms": [
                        {
                            "name": "ant-bootstrap-nrt",
                            "provider": "vultr",
                            "region": "nrt",
                            "ip": "207.148.94.42",
                            "ssh_user": "root",
                            "ssh_key": "~/.ssh/ant-production",
                        }
                    ],
                }
            )
        )
        vms = cmd._load_bootstraps(str(path), None, Console())
        assert [vm.name for vm in vms] == ["ant-bootstrap-nrt"]
        assert vms[0].provider_id == ""

    def test_limit_to_selects_by_region_suffix(self, tmp_path):
        path = tmp_path / "bootstrap-inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "node_vms": [
                        {
                            "name": f"ant-bootstrap-{region}",
                            "provider": "vultr",
                            "region": region,
                            "ip": f"10.0.0.{index}",
                            "ssh_user": "root",
                            "ssh_key": "~/.ssh/ant-production",
                        }
                        for index, region in enumerate(("nrt", "syd", "ewr"), start=1)
                    ],
                }
            )
        )
        vms = cmd._load_bootstraps(str(path), "syd,nrt", Console())
        # --limit-to order, not inventory order: it names the sequence to walk.
        assert [vm.name for vm in vms] == ["ant-bootstrap-syd", "ant-bootstrap-nrt"]

    def test_an_unmatched_limit_to_is_refused(self, tmp_path):
        path = tmp_path / "bootstrap-inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "node_vms": [
                        {
                            "name": "ant-bootstrap-nrt",
                            "provider": "vultr",
                            "region": "nrt",
                            "ip": "10.0.0.1",
                            "ssh_user": "root",
                            "ssh_key": "~/.ssh/ant-production",
                        }
                    ],
                }
            )
        )
        with pytest.raises(SystemExit):
            cmd._load_bootstraps(str(path), "lax", Console())


class TestPrivilegeEscalation:
    """The AWS bootstrap logs in as ubuntu; the other six are root."""

    def test_root_commands_are_not_wrapped(self):
        vm = _vm("ant-bootstrap-fsn1", "1.1.1.1")
        assert cmd._privileged(vm, "systemctl restart x") == "systemctl restart x"

    def test_non_root_commands_go_through_sudo(self):
        vm = _vm("ant-bootstrap-aws", "1.1.1.1", provider="aws", ssh_user="ubuntu")
        wrapped = cmd._privileged(vm, "systemctl restart x")
        assert wrapped.startswith("sudo -n sh -c ")
        assert "systemctl restart x" in wrapped

    def test_shell_metacharacters_survive_the_wrap(self):
        vm = _vm("ant-bootstrap-aws", "1.1.1.1", provider="aws", ssh_user="ubuntu")
        wrapped = cmd._privileged(vm, "a && b | c > d")
        assert wrapped == "sudo -n sh -c 'a && b | c > d'"


class TestDefaultOutputPath:
    def test_it_resolves_against_the_package(self):
        # Not the cwd: this command is run from wherever the operator happens
        # to be, and the file it writes is committed to the source tree.
        assert cmd.DEFAULT_MULTIADDR_OUT.name == "prod_bootstrap_webrtc_multiaddrs.txt"
        assert cmd.DEFAULT_MULTIADDR_OUT.parent.name == "resources"
        assert Path(cmd.DEFAULT_MULTIADDR_OUT).is_absolute()


class TestNoLiveInfrastructureIsTouched:
    def test_probe_is_the_only_network_call_in_the_pure_module(self):
        # webrtc_probe must stay importable with nothing but the standard
        # library: the rollout-watch scripts bundle imports it directly.
        source = Path(webrtc_probe.__file__).read_text()
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                assert "saorsa_deploy" not in line
                assert "requests" not in line
                assert "pyinfra" not in line


@patch("saorsa_deploy.cmd.prod_bootstrap_upgrade._cli")
class TestFirewallPreflight:
    def test_vultr_needs_no_action(self, cli, capsys):
        cmd._preflight_firewalls(
            [_vm("ant-bootstrap-nrt", "1.1.1.1", provider="vultr")], 10001, Console()
        )
        cli.assert_not_called()

    def test_hetzner_adds_the_missing_rule(self, cli):
        cli.side_effect = [
            SimpleNamespace(
                returncode=0, stdout=json.dumps({"firewalls": [{"id": 42}]}), stderr=""
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": "ant-bootstrap-fsn1",
                        "rules": [
                            {"direction": "in", "protocol": "udp", "port": "10000"}
                        ],
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        cmd._preflight_firewalls([_vm("ant-bootstrap-fsn1", "1.1.1.1")], 10001, Console())
        add_rule = cli.call_args_list[-1].args[0]
        assert add_rule[:4] == ["hcloud", "firewall", "add-rule", "42"]
        assert "--protocol" in add_rule and "udp" in add_rule
        assert "10001" in add_rule

    def test_hetzner_skips_an_existing_rule(self, cli):
        cli.side_effect = [
            SimpleNamespace(
                returncode=0, stdout=json.dumps({"firewalls": [{"id": 42}]}), stderr=""
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": "ant-bootstrap-fsn1",
                        "rules": [
                            {"direction": "in", "protocol": "udp", "port": "10001"}
                        ],
                    }
                ),
                stderr="",
            ),
        ]
        cmd._preflight_firewalls([_vm("ant-bootstrap-fsn1", "1.1.1.1")], 10001, Console())
        assert cli.call_count == 2

    def test_aws_treats_a_duplicate_as_success(self, cli):
        cli.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps(["sg-1"]), stderr=""),
            SimpleNamespace(
                returncode=254,
                stdout="",
                stderr="An error occurred (InvalidPermission.Duplicate) ...",
            ),
        ]
        cmd._preflight_firewalls(
            [_vm("ant-bootstrap-aws", "1.1.1.1", provider="aws", ssh_user="ubuntu")],
            10001,
            Console(),
        )
        assert cli.call_count == 2

    def test_a_real_aws_failure_stops_the_run(self, cli):
        cli.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps(["sg-1"]), stderr=""),
            SimpleNamespace(returncode=254, stdout="", stderr="UnauthorizedOperation"),
        ]
        # Restarting onto a listener the provider still blocks would fail the
        # ICE probe and roll every host back, one at a time.
        with pytest.raises(SystemExit):
            cmd._preflight_firewalls(
                [_vm("ant-bootstrap-aws", "1.1.1.1", provider="aws", ssh_user="ubuntu")],
                10001,
                Console(),
            )
