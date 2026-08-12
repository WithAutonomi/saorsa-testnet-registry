"""Tests for the per-process RSS composition collector (V2-972).

The collector runs on node VMs under Telegraf's `inputs.exec`. Everything it
does that could be wrong in a way Influx would silently accept — the unit
conversion, the field type, what happens to a process that has gone away — is
exercised here against fixtures rather than against a live `/proc`.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy"
    / "resources"
    / "scripts"
    / "node_memory_collector.py"
)


def _load_collector():
    """Import the collector by path.

    It lives under `resources/scripts/` because it is shipped to hosts as a
    file rather than imported by the package, so it is not on the import path.
    """
    spec = importlib.util.spec_from_file_location(
        "node_memory_collector", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


# Real output from `systemctl show --property=Id --property=MainPID
# --property=ActiveState 'ant-node-*.service'`, trimmed to three units.
SYSTEMCTL_OUTPUT = """Id=ant-node-9.service
MainPID=3164913
ActiveState=active

Id=ant-node-10.service
MainPID=3168899
ActiveState=active

Id=ant-node-11.service
MainPID=0
ActiveState=inactive
"""

# Real /proc/<pid>/status lines from a production node.
STATUS_TEXT = """Name:\tantnode
Umask:\t0022
State:\tS (sleeping)
VmSize:\t44199712 kB
VmRSS:\t  416304 kB
RssAnon:\t  389688 kB
RssFile:\t   26616 kB
RssShmem:\t       0 kB
VmSwap:\t       0 kB
Threads:\t42
"""

# Real /proc/<pid>/smaps_rollup from a production node (kernel 6.8).
ROLLUP_TEXT = """51eaa400000-7ffefe1f1000 ---p 00000000 00:00 0    [rollup]
Rss:              416260 kB
Pss:              408915 kB
Pss_Dirty:        389688 kB
Pss_Anon:         389680 kB
Pss_File:          19235 kB
Pss_Shmem:             0 kB
Shared_Clean:       7964 kB
Shared_Dirty:          0 kB
Private_Clean:     18608 kB
Private_Dirty:    389688 kB
Referenced:       407044 kB
Anonymous:        389680 kB
KSM:                   0 kB
LazyFree:              0 kB
AnonHugePages:    276480 kB
Swap:                  0 kB
SwapPss:               0 kB
Locked:                0 kB
"""


class TestParseSystemctlShow:
    def test_returns_active_units_with_real_pids(self):
        units = collector.parse_systemctl_show(SYSTEMCTL_OUTPUT)
        assert units == [
            ("ant-node-9.service", 3164913),
            ("ant-node-10.service", 3168899),
        ]

    def test_skips_inactive_unit(self):
        """An inactive unit must produce no point at all.

        Reporting it would mean either fabricating zeros or, once its MainPID
        of 0 were treated as a PID, attributing some other process's memory to
        a node that is not running.
        """
        units = collector.parse_systemctl_show(SYSTEMCTL_OUTPUT)
        assert all(unit != "ant-node-11.service" for unit, _ in units)

    def test_skips_active_unit_with_no_main_pid(self):
        output = "Id=ant-node-1.service\nMainPID=0\nActiveState=active\n"
        assert collector.parse_systemctl_show(output) == []

    def test_tolerates_unparseable_pid(self):
        output = "Id=ant-node-1.service\nMainPID=nonsense\nActiveState=active\n"
        assert collector.parse_systemctl_show(output) == []

    def test_empty_output_yields_no_units(self):
        assert collector.parse_systemctl_show("") == []

    def test_final_record_without_trailing_blank_line_is_kept(self):
        output = "Id=ant-node-1.service\nMainPID=42\nActiveState=active"
        assert collector.parse_systemctl_show(output) == [
            ("ant-node-1.service", 42)
        ]


class TestParseProcFields:
    def test_status_values_are_converted_to_bytes(self):
        """/proc reports kB; everything stored must be bytes.

        The conversion happens once, here, because a field key whose type or
        unit varies between writes is rejected by InfluxDB for the life of the
        shard.
        """
        values = collector.parse_proc_fields(
            STATUS_TEXT, collector.STATUS_FIELDS
        )
        assert values["vm_rss"] == 416304 * 1024
        assert values["rss_anon"] == 389688 * 1024
        assert values["rss_file"] == 26616 * 1024
        assert values["rss_shmem"] == 0
        assert values["vm_swap"] == 0

    def test_status_ignores_unwanted_keys(self):
        values = collector.parse_proc_fields(
            STATUS_TEXT, collector.STATUS_FIELDS
        )
        assert set(values) == set(collector.STATUS_FIELDS.values())
        assert "VmSize" not in values

    def test_rss_composition_sums_to_total(self):
        """The issue's reconciliation criterion, on real kernel output."""
        v = collector.parse_proc_fields(STATUS_TEXT, collector.STATUS_FIELDS)
        assert v["rss_anon"] + v["rss_file"] + v["rss_shmem"] == v["vm_rss"]

    def test_rollup_values_are_converted_to_bytes(self):
        values = collector.parse_proc_fields(
            ROLLUP_TEXT, collector.ROLLUP_FIELDS
        )
        assert values["rollup_rss"] == 416260 * 1024
        assert values["rollup_pss"] == 408915 * 1024
        assert values["rollup_private_dirty"] == 389688 * 1024
        assert values["rollup_anon_huge_pages"] == 276480 * 1024

    def test_pss_does_not_exceed_rss(self):
        """The issue's second reconciliation criterion."""
        v = collector.parse_proc_fields(ROLLUP_TEXT, collector.ROLLUP_FIELDS)
        assert v["rollup_pss"] <= v["rollup_rss"]

    def test_absent_kernel_key_is_omitted_not_zeroed(self):
        """A kernel that does not expose a field must produce no field.

        An invented zero is indistinguishable from a real zero, which would
        silently misreport (say) swap as known-empty on a kernel that never
        said.
        """
        without_pss_shmem = "\n".join(
            line for line in ROLLUP_TEXT.splitlines()
            if not line.startswith("Pss_Shmem")
        )
        values = collector.parse_proc_fields(
            without_pss_shmem, collector.ROLLUP_FIELDS
        )
        assert "rollup_pss_shmem" not in values

    def test_malformed_value_is_skipped(self):
        values = collector.parse_proc_fields(
            "VmRSS:\tnot-a-number kB\nRssAnon:\t100 kB\n",
            collector.STATUS_FIELDS,
        )
        assert values == {"rss_anon": 100 * 1024}

    def test_empty_text_yields_nothing(self):
        assert collector.parse_proc_fields("", collector.STATUS_FIELDS) == {}


class TestReadProc:
    def test_missing_process_returns_none(self):
        """A vanished process must be distinguishable from an empty read.

        PID 1 exists; a PID that cannot exist stands in for a process that
        exited between the systemctl call and the /proc read.
        """
        assert collector.read_proc(
            2**30, "status", collector.STATUS_FIELDS
        ) is None


class TestBuildLine:
    def test_shape_is_valid_line_protocol(self):
        line = collector.build_line(
            "ant-node-3", 1234, {"vm_rss": 4096, "rss_anon": 2048}, 1700000000000000000
        )
        assert line == (
            "ant_node_memory,node_service=ant-node-3 "
            "pid=1234i,rss_anon=2048i,vm_rss=4096i 1700000000000000000"
        )

    def test_pid_is_a_field_not_a_tag(self):
        """A PID tag would mint a new series on every node restart.

        Keying on `node_service` is what keeps `ant-node-3` one series across
        its restarts, which the issue requires explicitly.
        """
        line = collector.build_line("ant-node-3", 99, {"vm_rss": 1}, 1)
        tag_section = line.split(" ", 1)[0]
        assert "pid" not in tag_section
        assert "pid=99i" in line.split(" ")[1]

    def test_every_value_is_an_integer_field(self):
        line = collector.build_line("ant-node-3", 99, {"vm_rss": 4096}, 1)
        assert "vm_rss=4096i" in line
        assert "vm_rss=4096.0" not in line

    def test_fields_are_ordered_deterministically(self):
        line = collector.build_line(
            "n", 1, {"z_last": 1, "a_first": 2, "m_mid": 3}, 1
        )
        assert line.index("a_first") < line.index("m_mid") < line.index("z_last")

    @pytest.mark.parametrize(
        "service,expected",
        [
            ("ant-node-3", "ant-node-3"),
            ("has space", "has\\ space"),
            ("has,comma", "has\\,comma"),
            ("has=equals", "has\\=equals"),
        ],
    )
    def test_tag_values_are_escaped(self, service, expected):
        line = collector.build_line(service, 1, {"vm_rss": 1}, 1)
        assert f"node_service={expected} " in line


class TestCollect:
    def test_emits_one_line_per_readable_service(self, monkeypatch):
        monkeypatch.setattr(
            collector, "list_units",
            lambda timeout: [("ant-node-1.service", 11), ("ant-node-2.service", 22)],
        )
        monkeypatch.setattr(
            collector, "read_proc",
            lambda pid, filename, wanted: {"vm_rss": pid * 1024},
        )
        lines = collector.collect("status", 10.0, now_ns=7)
        assert len(lines) == 2
        assert "node_service=ant-node-1 pid=11i,vm_rss=11264i 7" in lines[0]

    def test_unreadable_process_does_not_abort_the_scrape(self, monkeypatch):
        """One failed read must not cost the other services their metrics."""
        monkeypatch.setattr(
            collector, "list_units",
            lambda timeout: [
                ("ant-node-1.service", 11),
                ("ant-node-2.service", 22),
                ("ant-node-3.service", 33),
            ],
        )
        monkeypatch.setattr(
            collector, "read_proc",
            lambda pid, filename, wanted: (
                None if pid == 22 else {"vm_rss": 1}
            ),
        )
        lines = collector.collect("status", 10.0, now_ns=1)
        assert len(lines) == 2
        assert not any("ant-node-2" in line for line in lines)

    def test_process_with_no_readable_fields_emits_nothing(self, monkeypatch):
        monkeypatch.setattr(
            collector, "list_units", lambda timeout: [("ant-node-1.service", 11)]
        )
        monkeypatch.setattr(
            collector, "read_proc", lambda pid, filename, wanted: {}
        )
        assert collector.collect("status", 10.0, now_ns=1) == []

    def test_no_services_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(collector, "list_units", lambda timeout: [])
        assert collector.collect("rollup", 10.0, now_ns=1) == []

    def test_all_points_in_a_scrape_share_one_timestamp(self, monkeypatch):
        monkeypatch.setattr(
            collector, "list_units",
            lambda timeout: [(f"ant-node-{i}.service", i) for i in range(1, 4)],
        )
        monkeypatch.setattr(
            collector, "read_proc",
            lambda pid, filename, wanted: {"vm_rss": 1},
        )
        lines = collector.collect("status", 10.0, now_ns=42)
        assert all(line.endswith(" 42") for line in lines)

    def test_deadline_stops_the_scrape_early(self, monkeypatch):
        """Overrunning returns what was gathered, not nothing.

        smaps_rollup costs ~105 ms per process, so a host with unusually large
        address spaces could exceed its collection interval; partial data beats
        a scrape that stacks up behind its successor.
        """
        monkeypatch.setattr(
            collector, "list_units",
            lambda timeout: [(f"ant-node-{i}.service", i) for i in range(1, 6)],
        )
        ticks = iter([0.0, 0.0, 1.0, 99.0, 99.0, 99.0, 99.0])
        monkeypatch.setattr(collector.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(
            collector, "read_proc",
            lambda pid, filename, wanted: {"vm_rss": 1},
        )
        lines = collector.collect("status", 10.0, now_ns=1)
        assert 0 < len(lines) < 5

    def test_rollup_source_reads_smaps_rollup(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            collector, "list_units", lambda timeout: [("ant-node-1.service", 11)]
        )

        def fake_read(pid, filename, wanted):
            seen["filename"] = filename
            seen["wanted"] = wanted
            return {"rollup_rss": 1}

        monkeypatch.setattr(collector, "read_proc", fake_read)
        collector.collect("rollup", 10.0, now_ns=1)
        assert seen["filename"] == "smaps_rollup"
        assert seen["wanted"] is collector.ROLLUP_FIELDS


class TestUnitPatterns:
    def test_covers_the_same_units_as_the_procstat_stanzas(self):
        """Tag values must line up with the existing per-service RSS series.

        The Telegraf config's four procstat blocks define which services get a
        `node_service` tag; collecting a different set would produce
        composition for services that have no RSS series to sit beside.
        """
        assert set(collector.UNIT_PATTERNS) == {
            "ant-node-*.service",
            "ant-bootstrap-*.service",
            "ant-bootstrap.service",
            "ant-genesis-node.service",
        }
