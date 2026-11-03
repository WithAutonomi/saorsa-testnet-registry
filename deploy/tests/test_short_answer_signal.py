"""A short answer has to say it is short (INFRA-484).

InfluxDB does not refuse a window wider than its retention -- it returns
whatever is inside it -- so `print-downloads --window -30d` against a 7-day
bucket comes back looking like a complete 30 days, and nothing in the output
says otherwise. That is how nine days of PROD-WDL-01's download history read as
present when the bucket no longer had it, and why the planning issue it fed
concluded the history was lost.

So the JSON now carries how far back the answer actually reaches, and the prod
upload path finally passes `--window` instead of querying from the epoch.

The JSON half matters most: a warning on a console nobody reads is how this went
unnoticed for nine days.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.print_file_ops import (
    _emit_ops_json,
    _resolve_window_start,
    _warn_if_truncated,
    _window_coverage,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _point(when):
    return {"_time": when, "host": "hostA", "status": 0}


class TestResolveWindowStart:
    @pytest.mark.parametrize(
        "window,expected_delta",
        [
            ("-30d", timedelta(days=30)),
            ("-7d", timedelta(days=7)),
            ("-6h", timedelta(hours=6)),
            ("-90m", timedelta(minutes=90)),
            ("-45s", timedelta(seconds=45)),
            ("-2w", timedelta(weeks=2)),
            ("-1.5h", timedelta(hours=1.5)),
        ],
    )
    def test_relative_durations(self, window, expected_delta):
        assert _resolve_window_start(window, now=NOW) == NOW - expected_delta

    def test_zero_is_the_epoch(self):
        assert _resolve_window_start("0") == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_an_rfc3339_timestamp(self):
        assert _resolve_window_start("2026-10-01T00:00:00Z") == datetime(
            2026, 10, 1, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("window", [None, "", "-1mo", "-2y", "now()", "garbage", "-d"])
    def test_an_unresolvable_window_yields_none_rather_than_a_guess(self, window):
        """Claiming a history is short when it is not would be worse.

        `mo` and `y` are CALENDAR units in Flux with no fixed length, so they
        land here on purpose.
        """
        assert _resolve_window_start(window, now=NOW) is None


class TestWindowCoverage:
    def test_a_window_reaching_past_the_data_is_flagged(self):
        """The retention case: 30 days asked for, 7 days of data."""
        points = [_point(NOW - timedelta(days=d)) for d in (1, 4, 7)]
        coverage = _window_coverage(points, "-30d")
        assert coverage["window_starts_before_data"] is True
        assert coverage["oldest_point"] == (NOW - timedelta(days=7)).isoformat()
        assert coverage["window_start"] is not None

    def test_a_window_inside_the_data_is_not_flagged(self):
        points = [_point(NOW - timedelta(days=d)) for d in (1, 10, 30)]
        assert _window_coverage(points, "-7d")["window_starts_before_data"] is False

    def test_no_points_leaves_it_unknown_rather_than_true(self):
        """A query that returned nothing says nothing about retention."""
        coverage = _window_coverage([], "-30d")
        assert coverage["window_starts_before_data"] is None
        assert coverage["oldest_point"] is None

    def test_an_unresolvable_window_leaves_it_unknown(self):
        points = [_point(NOW - timedelta(days=1))]
        assert _window_coverage(points, "-1mo")["window_starts_before_data"] is None

    def test_the_keys_are_always_present(self):
        """So a consumer can tell an unresolvable window from an old tool."""
        for window in ("-30d", "-1mo", None):
            assert set(_window_coverage([], window)) == {
                "oldest_point",
                "window_start",
                "window_starts_before_data",
            }


class TestTheJsonCarriesIt:
    def _emit(self, points, window):
        captured = {}
        with patch(
            "saorsa_deploy.cmd.print_file_ops.emit_json",
            lambda doc, dest, console: captured.update(doc),
        ):
            _emit_ops_json(
                SimpleNamespace(json_out="-"),
                SimpleNamespace(print=lambda *_, **__: None),
                deployment="PROD-DL-01",
                kind="prod-continuous-downloader",
                op_type="download",
                source="influx",
                points=points,
                window=window,
            )
        return captured

    def test_the_three_keys_reach_the_document(self):
        doc = self._emit([_point(NOW - timedelta(days=7))], "-30d")
        assert doc["window"] == "-30d"
        assert doc["window_starts_before_data"] is True
        assert doc["oldest_point"] == (NOW - timedelta(days=7)).isoformat()

    def test_an_empty_answer_still_emits_them(self):
        doc = self._emit([], "-30d")
        assert doc["count"] == 0
        assert doc["oldest_point"] is None
        assert doc["window_starts_before_data"] is None


class TestTheConsoleWarning:
    def test_it_names_the_oldest_timestamp_and_the_durable_command(self):
        lines: list[str] = []
        console = SimpleNamespace(print=lambda *a, **_k: lines.append(" ".join(map(str, a))))
        coverage = _window_coverage([_point(NOW - timedelta(days=7))], "-30d")
        _warn_if_truncated(coverage, "PROD-DL-01", console)
        assert len(lines) == 1
        assert coverage["oldest_point"] in lines[0]
        assert "print-recorded-results" in lines[0]
        assert "PROD-DL-01" in lines[0]

    def test_it_is_silent_when_the_answer_is_complete(self):
        lines: list[str] = []
        console = SimpleNamespace(print=lambda *a, **_k: lines.append(" ".join(map(str, a))))
        # A point older than the window start: the answer reaches all the way.
        old_enough = datetime.now(timezone.utc) - timedelta(days=60)
        _warn_if_truncated(_window_coverage([_point(old_enough)], "-30d"), "DEV-01", console)
        # And an empty answer, which says nothing about retention either way.
        _warn_if_truncated(_window_coverage([], "-30d"), "DEV-01", console)
        assert lines == []


class TestProdUploadsHonourTheWindow:
    """`_run_prod_uploads` called with no `start`, so it queried from the epoch.

    With a deployment's retention set to infinite, epoch-to-now grows without
    bound -- so this stopped being tidiness.
    """

    def test_the_window_is_passed_through(self):
        from saorsa_deploy.cmd.print_file_ops import _run_prod_uploads

        captured = {}

        def _fetch(**kwargs):
            captured.update(kwargs)
            return []

        args = SimpleNamespace(
            name="PROD-UL-01",
            monitoring_name=None,
            influx_url="http://influx",
            influx_token="token",
            influx_org="ant",
            influx_bucket="testnet-metrics",
            window="-30d",
            json_out=None,
            details=False,
            summary=False,
            recent=False,
        )
        with (
            patch("saorsa_deploy.cmd.print_file_ops.fetch_all_prod_upload_points", _fetch),
            patch("saorsa_deploy.cmd.print_file_ops._resolve_kind", return_value="prod-uploaders"),
        ):
            _run_prod_uploads(args, SimpleNamespace(print=lambda *_, **__: None))

        assert captured["start"] == "-30d"

    def test_the_emitted_json_records_the_window_it_used(self):
        """`"window": null` in a prod-upload JSON was the symptom."""
        from saorsa_deploy.cmd.print_file_ops import _run_prod_uploads

        doc = {}
        args = SimpleNamespace(
            name="PROD-UL-01",
            monitoring_name=None,
            influx_url="http://influx",
            influx_token="token",
            influx_org="ant",
            influx_bucket="testnet-metrics",
            window="-30d",
            json_out="-",
            details=False,
            summary=False,
            recent=False,
        )
        with (
            patch(
                "saorsa_deploy.cmd.print_file_ops.fetch_all_prod_upload_points",
                lambda **_kwargs: [],
            ),
            patch("saorsa_deploy.cmd.print_file_ops._resolve_kind", return_value="prod-uploaders"),
            patch(
                "saorsa_deploy.cmd.print_file_ops.emit_json",
                lambda document, dest, console: doc.update(document),
            ),
        ):
            _run_prod_uploads(args, SimpleNamespace(print=lambda *_, **__: None))

        assert doc["window"] == "-30d"
        assert "window_starts_before_data" in doc


class TestTheSummaryCountsByKind:
    """Item 10: a count to group by, which is what `reason` alone cannot give."""

    def test_kinds_are_counted_including_skips(self):
        from saorsa_deploy.cmd.print_file_ops import _summarise_host

        ops = [
            {"status": 0, "total_ms": 10, "start_ns": 1},
            {"status": 1, "total_ms": 10, "start_ns": 2, "error_kind": "not_found"},
            {"status": 1, "total_ms": 10, "start_ns": 3, "error_kind": "not_found"},
            {"status": 1, "total_ms": 10, "start_ns": 4, "error_kind": "crash"},
            {"status": 2, "total_ms": 0, "start_ns": 5, "error_kind": "over_cap"},
        ]
        summary = _summarise_host(ops, "download", now=NOW)
        assert summary["error_kinds"] == {"not_found": 2, "crash": 1, "over_cap": 1}

    def test_a_clean_run_has_no_kinds_and_prints_nothing(self):
        from saorsa_deploy.cmd.print_file_ops import _print_error_kinds, _summarise_host

        summary = _summarise_host(
            [{"status": 0, "total_ms": 10, "start_ns": 1}], "download", now=NOW
        )
        assert summary["error_kinds"] == {}
        lines: list[str] = []
        _print_error_kinds({}, SimpleNamespace(print=lambda *a, **_k: lines.append(a)))
        assert lines == []
