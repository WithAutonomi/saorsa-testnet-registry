"""Rendering tests for the disk I/O section of `deployments print-recorded-results`.

Disk I/O was added to `deployment_host_results.host_metrics` (a JSON column)
after rows already existed, so the renderer must degrade cleanly on rows that
predate it rather than showing a table full of dashes.
"""

import io

from rich.console import Console

from saorsa_deploy.cmd.print_recorded_results import _print_disk_io_table


def _render(metrics: list[dict]) -> str:
    buf = io.StringIO()
    # Wide enough that Rich does not wrap the numbers we assert on.
    console = Console(file=buf, width=250, no_color=True)
    _print_disk_io_table(metrics, console)
    return buf.getvalue()


def _stat(avg, min_, max_, samples=2) -> dict:
    return {"avg": avg, "min": min_, "max": max_, "samples": samples}


def _device(role=None, read_avg=2 * 1024 * 1024, read_max=8 * 1024 * 1024) -> dict:
    return {
        "volume_role": role,
        "read_bytes_per_sec": _stat(read_avg, 0.0, read_max),
        "write_bytes_per_sec": _stat(1024 * 1024, 0.0, 4 * 1024 * 1024),
        "reads_per_sec": _stat(50.0, 0.0, 200.0),
        "writes_per_sec": _stat(25.0, 0.0, 100.0),
    }


def _entry(host: str = "node-1") -> dict:
    """A node VM: idle root disk plus a busy tagged storage volume."""
    return {
        "host": host,
        "cpu_percent": _stat(20.0, 10.0, 30.0),
        "disk_io_by_device": {
            "vda": _device(read_avg=0.0, read_max=0.0),
            "sdb": _device(role="node-storage"),
        },
    }


class TestBackwardsCompatibility:
    def test_pre_diskio_rows_render_no_disk_section(self):
        # A row recorded before disk I/O collection existed.
        legacy = [
            {
                "host": "node-1",
                "cpu_percent": _stat(20.0, 10.0, 30.0),
                "mem_percent": _stat(50.0, 40.0, 60.0),
                "bytes_in_per_sec": _stat(1000.0, 0.0, 2000.0),
                "bytes_out_per_sec": _stat(500.0, 0.0, 1000.0),
            }
        ]
        assert _render(legacy) == ""

    def test_empty_metrics_render_nothing(self):
        assert _render([]) == ""

    def test_empty_device_map_renders_nothing(self):
        assert _render([{"host": "node-1", "disk_io_by_device": {}}]) == ""

    def test_mixed_rows_still_render_the_section(self):
        output = _render([
            {"host": "legacy-host", "cpu_percent": _stat(20.0, 10.0, 30.0)},
            _entry("new-host"),
        ])
        assert "Disk I/O" in output
        assert "new-host" in output


class TestRendering:
    def test_renders_a_row_per_device_not_per_host(self):
        output = _render([_entry("node-1")])
        assert "vda" in output
        assert "sdb" in output
        # Host repeated on both of its device rows.
        assert output.count("node-1") == 2

    def test_marks_the_node_storage_volume(self):
        output = _render([_entry()])
        assert "node-storage" in output

    def test_untagged_devices_show_a_placeholder_role(self):
        output = _render([{"host": "client-1", "disk_io_by_device": {"vda": _device()}}])
        assert "node-storage" not in output
        assert "-" in output

    def test_devices_are_ordered_deterministically(self):
        output = _render([_entry()])
        assert output.index("sdb") < output.index("vda")

    def test_throughput_is_formatted_as_bandwidth(self):
        output = _render([_entry()])
        assert "2.00 MB/s" in output
        assert "8.00 MB/s" in output

    def test_iops_are_rendered_as_plain_numbers(self):
        output = _render([_entry()])
        assert "50.0" in output
        assert "200.0" in output

    def test_headers_cover_throughput_and_iops_both_directions(self):
        output = _render([_entry()])
        for header in ("Device", "Role", "Read", "Write", "IOPS"):
            assert header in output

    def test_no_cross_device_average_is_shown(self):
        # The idle root disk must stay visibly idle rather than being folded
        # into a host-level mean with the busy storage volume.
        output = _render([_entry()])
        assert "0.0 KB/s" in output
