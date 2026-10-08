from types import SimpleNamespace
from unittest.mock import patch

from saorsa_deploy import ssh


def test_wait_for_ssh_returns_empty_for_no_hosts():
    assert ssh.wait_for_ssh([], "/key") == []


def test_wait_for_ssh_succeeds_when_all_hosts_respond():
    def fake_run(cmd, capture_output=False):
        return SimpleNamespace(returncode=0)

    with patch.object(ssh.subprocess, "run", side_effect=fake_run):
        assert (
            ssh.wait_for_ssh(
                ["1.1.1.1", "2.2.2.2"],
                "/key",
                timeout=5,
                poll_interval=0,
            )
            == []
        )


def test_wait_for_ssh_returns_unreachable_hosts_after_timeout():
    def fake_run(cmd, capture_output=False):
        # 1.1.1.1 succeeds, 2.2.2.2 always fails
        ip = next(arg.split("@", 1)[1] for arg in cmd if arg.startswith("root@"))
        return SimpleNamespace(returncode=0 if ip == "1.1.1.1" else 1)

    with patch.object(ssh.subprocess, "run", side_effect=fake_run):
        not_ready = ssh.wait_for_ssh(
            ["1.1.1.1", "2.2.2.2"],
            "/key",
            timeout=1,
            poll_interval=0,
        )
    assert not_ready == ["2.2.2.2"]


def test_wait_for_ssh_polls_until_host_comes_up():
    counts = {"1.1.1.1": 0}

    def fake_run(cmd, capture_output=False):
        ip = next(arg.split("@", 1)[1] for arg in cmd if arg.startswith("root@"))
        counts[ip] = counts.get(ip, 0) + 1
        # First two attempts fail, third succeeds — verifies the polling loop
        # actually retries instead of giving up after one round.
        return SimpleNamespace(returncode=0 if counts[ip] >= 3 else 1)

    with patch.object(ssh.subprocess, "run", side_effect=fake_run):
        not_ready = ssh.wait_for_ssh(
            ["1.1.1.1"],
            "/key",
            timeout=10,
            poll_interval=0,
        )
    assert not_ready == []
    assert counts["1.1.1.1"] >= 3
