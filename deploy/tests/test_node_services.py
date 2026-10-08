"""Tests for the single-service ant-node command builders/parsers used by
`prod services ls/rm/add`."""

import pytest

from saorsa_deploy.provisioning.node import _build_node_exec_start, _build_node_unit_file
from saorsa_deploy.provisioning.node_services import (
    NODE_IDENTITY_FILENAME,
    build_disk_survey_command,
    build_list_services_command,
    build_remove_service_command,
    build_reset_data_command,
    build_write_unit_and_start_command,
    parse_df_line,
    parse_node_unit,
    parse_service_list,
    parse_service_name,
)


class TestParseServiceName:
    @pytest.mark.parametrize(
        "name,expected",
        [("ant-node-1", 1), ("ant-node-7", 7), ("ant-node-42", 42)],
    )
    def test_parses_valid_names(self, name, expected):
        assert parse_service_name(name) == expected

    @pytest.mark.parametrize(
        "bad",
        ["ant-node-0", "ant-node--1", "ant-node-", "ant-node-1a", "ant-bootstrap-1", "nope"],
    )
    def test_rejects_invalid_names(self, bad):
        with pytest.raises(ValueError):
            parse_service_name(bad)


class TestBuildListServicesCommand:
    def test_globs_ant_node_units_not_a_fixed_count(self):
        cmd = build_list_services_command()
        assert "for unit in /etc/systemd/system/ant-node-*.service" in cmd

    def test_emits_marker_per_service_and_none_marker(self):
        cmd = build_list_services_command()
        assert 'echo "ANT_SVC:LIST:$svc:$active:$enabled:$bytes"' in cmd
        assert "ANT_SVC:LIST:NONE" in cmd

    def test_du_targets_data_dir(self):
        cmd = build_list_services_command()
        assert 'du -sb "/mnt/ant-node-storage/data/$svc"' in cmd


class TestParseServiceList:
    def test_parses_marker_lines_sorted_by_index(self):
        lines = [
            "ANT_SVC:LIST:ant-node-3:active:enabled:300",
            "ANT_SVC:LIST:ant-node-1:inactive:disabled:100",
            "some other pyinfra noise",
        ]
        records = parse_service_list(lines)
        assert [r["service_name"] for r in records] == ["ant-node-1", "ant-node-3"]
        assert records[0]["active"] == "inactive"
        assert records[0]["size_bytes"] == 100
        assert records[1]["index"] == 3

    def test_none_marker_yields_no_records(self):
        assert parse_service_list(["ANT_SVC:LIST:NONE"]) == []

    def test_ignores_malformed_lines(self):
        assert parse_service_list(["ANT_SVC:LIST:bad:only:three"]) == []


class TestBuildRemoveServiceCommand:
    def test_scoped_to_exact_service_name(self):
        cmd = build_remove_service_command("ant-node-7")
        assert "systemctl stop ant-node-7" in cmd
        assert "systemctl disable ant-node-7" in cmd
        assert "rm -f /etc/systemd/system/ant-node-7.service" in cmd
        assert "rm -rf /mnt/ant-node-storage/data/ant-node-7" in cmd
        assert "rm -rf /mnt/ant-node-storage/log/ant-node-7" in cmd
        assert "ANT_SVC:REMOVED:ant-node-7" in cmd
        # Never a glob — sibling services must be untouched.
        assert "ant-node-*" not in cmd

    def test_does_not_reference_other_service_indices(self):
        cmd = build_remove_service_command("ant-node-2")
        assert "ant-node-1" not in cmd
        assert "ant-node-3" not in cmd

    def test_rejects_invalid_service_name(self):
        with pytest.raises(ValueError):
            build_remove_service_command("not-a-service")


class TestBuildWriteUnitAndStartCommand:
    def test_writes_enables_and_starts_only_the_target_service(self):
        cmd = build_write_unit_and_start_command("ant-node-9", "[Unit]\n...\n")
        assert "cat > /etc/systemd/system/ant-node-9.service" in cmd
        assert "systemctl enable ant-node-9" in cmd
        assert "systemctl start ant-node-9" in cmd
        assert "ANT_SVC:ADDED:ant-node-9" in cmd

    def test_reglobs_es_log_forwarder_guarded_on_its_presence(self):
        # The recreated log dir (new inode) defeats telegraf-ant-elasticsearch's
        # inotify watch, so add must bounce it — but only where it exists.
        cmd = build_write_unit_and_start_command("ant-node-9", "[Unit]\n...\n")
        assert "systemctl cat telegraf-ant-elasticsearch" in cmd
        assert "systemctl restart telegraf-ant-elasticsearch" in cmd
        # Guarded + best-effort so hosts without ES forwarding are unaffected
        # and a forwarder hiccup never fails an otherwise-successful add.
        assert "if systemctl cat telegraf-ant-elasticsearch >/dev/null 2>&1; then" in cmd
        assert "|| true" in cmd
        # The metrics forwarder self-heals and must NOT be bounced here.
        assert "telegraf-ant-metrics" not in cmd

    def test_rejects_invalid_service_name(self):
        with pytest.raises(ValueError):
            build_write_unit_and_start_command("bad-name", "content")


class TestBuildResetDataCommand:
    def test_stops_wipes_except_identity_then_starts(self):
        cmd = build_reset_data_command("ant-node-7")
        root = "/mnt/ant-node-storage/data/ant-node-7"
        # stop before wipe, start after
        assert "systemctl stop ant-node-7" in cmd
        assert "systemctl start ant-node-7" in cmd
        assert (
            cmd.index("systemctl stop ant-node-7")
            < cmd.index(f'find "{root}"')
            < cmd.index("systemctl start ant-node-7")
        )
        # wipe everything in the data root EXCEPT the identity key
        assert f'find "{root}" -mindepth 1 -maxdepth 1' in cmd
        assert f"! -name {NODE_IDENTITY_FILENAME}" in cmd
        assert cmd.rstrip().endswith('echo "ANT_SVC:RESET:ant-node-7"')

    def test_aborts_before_stopping_if_identity_key_missing(self):
        cmd = build_reset_data_command("ant-node-7")
        key = f"/mnt/ant-node-storage/data/ant-node-7/{NODE_IDENTITY_FILENAME}"
        # the existence guard + exit must come before the stop
        assert f'if [ ! -f "{key}" ]; then' in cmd
        assert "exit 3" in cmd
        assert cmd.index("exit 3") < cmd.index("systemctl stop ant-node-7")

    def test_does_not_touch_log_dir_unit_or_forwarder(self):
        cmd = build_reset_data_command("ant-node-7")
        # in-place reset keeps the unit, log dir, and ES forwarder untouched
        assert "/etc/systemd/system/ant-node-7.service" not in cmd
        assert "/mnt/ant-node-storage/log/" not in cmd
        assert "telegraf-ant-elasticsearch" not in cmd
        assert "daemon-reload" not in cmd

    def test_scoped_to_exact_service_name(self):
        cmd = build_reset_data_command("ant-node-7")
        assert "ant-node-8" not in cmd
        assert "ant-node-70" not in cmd

    def test_rejects_invalid_service_name(self):
        with pytest.raises(ValueError):
            build_reset_data_command("bad-name")


class TestBuildDiskSurveyCommand:
    def test_lists_services_and_emits_df(self):
        cmd = build_disk_survey_command()
        # reuses the same per-service LIST marker discovery
        assert "ant-node-*.service" in cmd
        assert "ANT_SVC:LIST:$svc:$active:$enabled:$bytes" in cmd
        assert "ANT_SVC:LIST:NONE" in cmd
        # plus a single df marker for the storage device
        assert "df -B1 --output=size,used,avail /mnt/ant-node-storage" in cmd
        assert "ANT_SVC:DF:" in cmd


class TestParseDfLine:
    def test_parses_total_used_avail(self):
        lines = [
            "ANT_SVC:LIST:ant-node-1:active:enabled:100",
            "ANT_SVC:DF:422415106048:417612537856:490823680",
        ]
        assert parse_df_line(lines) == {
            "total_bytes": 422415106048,
            "used_bytes": 417612537856,
            "free_bytes": 490823680,
        }

    def test_missing_marker_returns_none(self):
        assert parse_df_line(["ANT_SVC:LIST:ant-node-1:active:enabled:100"]) is None

    def test_malformed_marker_returns_none(self):
        assert parse_df_line(["ANT_SVC:DF:onlyone"]) is None
        assert parse_df_line(["ANT_SVC:DF:a:b:c"]) is None


class TestParseNodeUnit:
    _UNIT_LEVEL_KEYS = {
        "is_nat",
        "restart_policy",
        "restart_sec",
        "limit_nofile",
        "rust_backtrace",
        "coredumps",
        "colorbt_show_hidden",
    }

    def _sibling_unit(self, **overrides):
        exec_params = {
            "bootstrap_peers": ["1.2.3.4:12000", "5.6.7.8:12000"],
            "root_dir": "/mnt/ant-node-storage/data/ant-node-3",
            "log_dir": "/mnt/ant-node-storage/log/ant-node-3",
            "port": 10002,
            "log_level": "debug",
            "testnet": False,
            "upgrade_channel": None,
            "rewards_address": "0xabc",
            "evm_network": "arbitrum-one",
        }
        unit_overrides = {k: v for k, v in overrides.items() if k in self._UNIT_LEVEL_KEYS}
        exec_params.update({k: v for k, v in overrides.items() if k not in self._UNIT_LEVEL_KEYS})
        exec_start = _build_node_exec_start(**exec_params)
        return _build_node_unit_file(
            "ant-node-3",
            exec_start,
            is_nat=unit_overrides.get("is_nat", False),
            restart_policy=unit_overrides.get("restart_policy", "always"),
            restart_sec=unit_overrides.get("restart_sec", 10),
            limit_nofile=unit_overrides.get("limit_nofile", 65536),
            rust_backtrace=unit_overrides.get("rust_backtrace"),
            coredumps=unit_overrides.get("coredumps", False),
            colorbt_show_hidden=unit_overrides.get("colorbt_show_hidden", False),
        )

    def test_round_trips_plain_prod_config(self):
        unit = self._sibling_unit()
        parsed = parse_node_unit(unit)
        assert parsed["bootstrap_peers"] == ["1.2.3.4:12000", "5.6.7.8:12000"]
        assert parsed["port"] == 10002
        assert parsed["log_level"] == "debug"
        assert parsed["testnet"] is False
        assert parsed["evm_network"] == "arbitrum-one"
        assert parsed["rewards_address"] == "0xabc"
        assert parsed["restart_policy"] == "always"
        assert parsed["restart_sec"] == 10
        assert parsed["limit_nofile"] == 65536
        assert parsed["is_nat"] is False
        assert parsed["coredumps"] is False

    def test_detects_nat_wrapping_and_strips_prefix_before_parsing_flags(self):
        unit = self._sibling_unit(is_nat=True)
        parsed = parse_node_unit(unit)
        assert parsed["is_nat"] is True
        assert parsed["port"] == 10002  # still parsed correctly despite the NAT prefix

    def test_detects_debug_node_crash_env_and_coredumps(self):
        unit = self._sibling_unit(rust_backtrace="full", colorbt_show_hidden=True, coredumps=True)
        parsed = parse_node_unit(unit)
        assert parsed["rust_backtrace"] == "full"
        assert parsed["colorbt_show_hidden"] is True
        assert parsed["coredumps"] is True

    def test_missing_optional_flags_are_none_or_false(self):
        unit = self._sibling_unit(port=None, log_level=None)
        parsed = parse_node_unit(unit)
        assert parsed["port"] is None
        assert parsed["log_level"] is None
        assert parsed["rust_backtrace"] is None
        assert parsed["colorbt_show_hidden"] is False

    def test_raises_on_missing_exec_start(self):
        with pytest.raises(ValueError):
            parse_node_unit("[Unit]\nDescription=nope\n")
