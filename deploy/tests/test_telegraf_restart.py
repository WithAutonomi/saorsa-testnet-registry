"""Tests for the `prod restart-telegraf` probe/restart command builders."""

from saorsa_deploy.provisioning.telegraf_restart import (
    LOG_FORWARDER_SERVICE,
    build_probe_command,
    build_restart_command,
    parse_df,
    parse_held,
    parse_restarted,
    parse_state,
)
from saorsa_deploy.provisioning.volume import MOUNT_POINT


class TestBuildProbeCommand:
    def test_measures_only_and_never_restarts(self):
        cmd = build_probe_command()
        assert "systemctl restart" not in cmd

    def test_scopes_lsof_to_the_forwarders_own_pid(self):
        # Fleet-wide `lsof +L1` would count unlinked files held by any process
        # and report them as reclaimable by restarting telegraf.
        cmd = build_probe_command()
        assert 'lsof -nP -p "$PID" +L1' in cmd
        assert f"systemctl show -p MainPID --value {LOG_FORWARDER_SERVICE}" in cmd

    def test_counts_only_unlinked_regular_files(self):
        # lsof ORs `-p` with `+L1`, so the raw rows include the process's live
        # files too. Measured on WAW1-4: 790 rows, 685 unlinked — a 1.9 GiB
        # overcount without the NLINK filter.
        cmd = build_probe_command()
        assert '$5 == "REG" && $8 == 0' in cmd

    def test_sums_the_size_column(self):
        cmd = build_probe_command()
        assert "s+=$7" in cmd

    def test_reads_free_space_on_the_data_volume(self):
        cmd = build_probe_command()
        assert f"df -B1 --output=size,used,avail {MOUNT_POINT}" in cmd

    def test_tolerates_a_host_without_lsof(self):
        cmd = build_probe_command()
        assert "command -v lsof" in cmd
        assert "HELD=0" in cmd

    def test_emits_all_three_markers(self):
        cmd = build_probe_command()
        for marker in ("ANT_TG:STATE:", "ANT_TG:HELD:", "ANT_TG:DF:"):
            assert marker in cmd


class TestBuildRestartCommand:
    def test_restarts_only_the_log_forwarder(self):
        cmd = build_restart_command()
        assert f"systemctl restart {LOG_FORWARDER_SERVICE}" in cmd
        # The metrics sidecar holds no log fds; the stock unit is not ours.
        assert "telegraf-ant-metrics" not in cmd
        assert "systemctl restart telegraf.service" not in cmd

    def test_measures_before_and_after_in_one_round_trip(self):
        cmd = build_restart_command()
        assert cmd.index("ANT_TG:DF:") < cmd.index("systemctl restart")
        assert cmd.index("systemctl restart") < cmd.index("ANT_TG:DF_AFTER:")

    def test_emits_the_restart_marker(self):
        assert f"ANT_TG:RESTARTED:{LOG_FORWARDER_SERVICE}" in build_restart_command()


class TestParseState:
    def test_parses_state_and_pid(self):
        assert parse_state(["ANT_TG:STATE:active:1234"]) == {
            "state": "active",
            "pid": 1234,
        }

    def test_returns_none_when_absent(self):
        assert parse_state(["something else"]) is None

    def test_returns_none_on_a_non_numeric_pid(self):
        assert parse_state(["ANT_TG:STATE:active:nope"]) is None


class TestParseHeld:
    def test_parses_bytes_and_fd_count(self):
        assert parse_held(["ANT_TG:HELD:14155776000:685"]) == {
            "held_bytes": 14155776000,
            "fds": 685,
        }

    def test_parses_a_host_holding_nothing(self):
        assert parse_held(["ANT_TG:HELD:0:0"]) == {"held_bytes": 0, "fds": 0}

    def test_returns_none_when_absent(self):
        assert parse_held([]) is None


class TestParseDf:
    def test_parses_the_before_marker(self):
        lines = ["ANT_TG:DF:528000000000:520000000000:3650000000"]
        assert parse_df(lines) == {
            "total_bytes": 528000000000,
            "used_bytes": 520000000000,
            "free_bytes": 3650000000,
        }

    def test_after_flag_selects_the_after_marker(self):
        lines = [
            "ANT_TG:DF:528000000000:520000000000:3650000000",
            "ANT_TG:DF_AFTER:528000000000:506000000000:17800000000",
        ]
        assert parse_df(lines)["free_bytes"] == 3650000000
        assert parse_df(lines, after=True)["free_bytes"] == 17800000000

    def test_returns_none_when_df_failed_rather_than_zeroes(self):
        # `df` failing emits an empty marker. Zeroes would read as "the volume
        # is full", which is the opposite of "we could not measure it".
        assert parse_df(["ANT_TG:DF:"]) is None


class TestParseRestarted:
    def test_true_when_the_marker_is_present(self):
        assert parse_restarted([f"ANT_TG:RESTARTED:{LOG_FORWARDER_SERVICE}"]) is True

    def test_false_when_the_restart_failed(self):
        # `set -u` without `-e` runs on past a failed systemctl, so the marker's
        # absence is the only signal that the restart did not happen.
        assert parse_restarted(["ANT_TG:DF:1:2:3"]) is False
