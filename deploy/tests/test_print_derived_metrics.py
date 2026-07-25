"""Rendering tests for the occupancy and derived-metric sections of
`deployments print-recorded-results` (V2-758).

Both sections are additive to data that already exists — occupancy is a new key
in the `host_metrics` JSON, derived metrics are a new table — so the renderer
must show nothing at all for runs recorded before they existed, rather than a
table of dashes implying a measurement of zero.
"""
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from rich.console import Console

from saorsa_deploy.cmd.print_recorded_results import (
    _print_derived_metrics_section,
    _print_disk_usage_table,
)
from saorsa_deploy.derived_metrics import BYTES_PER_GB

UTC = timezone.utc
NODE_MOUNT = "/mnt/ant-node-storage"


def _render(fn, payload) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=250, no_color=True)
    fn(payload, console)
    return buf.getvalue()


def _usage(first, last, minimum=None, maximum=None) -> dict:
    return {
        "avg": (first + last) / 2,
        "min": minimum if minimum is not None else first,
        "max": maximum if maximum is not None else last,
        "samples": 10,
        "first": first,
        "last": last,
        "growth_bytes": last - first,
    }


def _entry(host="node-1") -> dict:
    return {
        "host": host,
        "disk_usage_by_mount": {
            "/": {"volume_role": None, "used_bytes": _usage(BYTES_PER_GB, BYTES_PER_GB)},
            NODE_MOUNT: {
                "volume_role": "node-storage",
                "used_bytes": _usage(2 * BYTES_PER_GB, 9 * BYTES_PER_GB),
            },
        },
    }


class TestDiskUsageBackwardsCompatibility:
    def test_rows_predating_occupancy_capture_render_nothing(self):
        # Every run before this landed: `[[inputs.disk]]` was collected but
        # never queried, so no row has the key.
        legacy = [{"host": "node-1", "disk_io_by_device": {"sdb": {}}}]
        assert _render(_print_disk_usage_table, legacy) == ""

    def test_empty_metrics_render_nothing(self):
        assert _render(_print_disk_usage_table, []) == ""

    def test_empty_mount_map_renders_nothing(self):
        assert _render(
            _print_disk_usage_table, [{"host": "n1", "disk_usage_by_mount": {}}]
        ) == ""


class TestDiskUsageRendering:
    def test_renders_a_row_per_mountpoint(self):
        output = _render(_print_disk_usage_table, [_entry()])
        assert NODE_MOUNT in output
        assert output.count("node-1") == 2

    def test_marks_the_node_storage_mount(self):
        assert "node-storage" in _render(_print_disk_usage_table, [_entry()])

    def test_shows_growth_between_the_endpoints(self):
        output = _render(_print_disk_usage_table, [_entry()])
        assert "7.00 GB" in output

    def test_shows_the_peak_spread_alongside_growth(self):
        # A run pruned mid-window has growth well below its spread, and that
        # difference is the only way to tell the two apart after the fact.
        entry = {
            "host": "node-1",
            "disk_usage_by_mount": {
                NODE_MOUNT: {
                    "volume_role": "node-storage",
                    "used_bytes": _usage(
                        BYTES_PER_GB, 2 * BYTES_PER_GB, maximum=9 * BYTES_PER_GB
                    ),
                }
            },
        }
        output = _render(_print_disk_usage_table, [entry])
        assert "1.00 GB" in output   # growth
        assert "8.00 GB" in output   # spread


def _metric(
    metric,
    value,
    unit="bytes_per_gb",
    window_label="full-run",
    source="capture",
    numerator=None,
    denominator=None,
):
    return SimpleNamespace(
        metric=metric,
        value=value,
        unit=unit,
        window_label=window_label,
        source=source,
        numerator=numerator,
        denominator=denominator,
        window_start=datetime(2026, 7, 1, tzinfo=UTC),
        window_end=datetime(2026, 7, 1, 4, tzinfo=UTC),
    )


class TestDerivedMetricsSection:
    def test_no_rows_renders_nothing(self):
        assert _render(_print_derived_metrics_section, []) == ""

    def test_ratios_render_as_an_amplification_factor(self):
        # "4x the data we push per GB stored" is the unit the gate is reasoned
        # about in; a raw byte count per GB is unreadable at fleet scale.
        output = _render(
            _print_derived_metrics_section,
            [_metric("outbound_bytes_per_gb_uploaded", 4 * BYTES_PER_GB)],
        )
        assert "4.00 GB/GB" in output

    def test_shows_the_window_and_its_source(self):
        # An exact capture and an approximate bucket re-derivation must be
        # distinguishable at a glance, or they will be compared.
        output = _render(
            _print_derived_metrics_section,
            [
                _metric("outbound_bytes_per_gb_uploaded", BYTES_PER_GB),
                _metric(
                    "outbound_bytes_per_gb_uploaded",
                    2 * BYTES_PER_GB,
                    window_label="steady-state",
                    source="buckets",
                ),
            ],
        )
        assert "full-run" in output
        assert "steady-state" in output
        assert "capture" in output
        assert "buckets" in output

    def test_shows_the_numerator_and_denominator_of_a_ratio(self):
        output = _render(
            _print_derived_metrics_section,
            [
                _metric(
                    "disk_bytes_per_gb_stored",
                    3 * BYTES_PER_GB,
                    numerator=6 * BYTES_PER_GB,
                    denominator=2.0,
                )
            ],
        )
        assert "6.00 GB" in output
        assert "2.00 GB" in output

    def test_component_rows_render_without_a_ratio(self):
        output = _render(
            _print_derived_metrics_section,
            [_metric("node_outbound_bytes_total", 5 * BYTES_PER_GB, unit="bytes")],
        )
        assert "5.00 GB" in output
        assert "node_outbound_bytes_total" in output
