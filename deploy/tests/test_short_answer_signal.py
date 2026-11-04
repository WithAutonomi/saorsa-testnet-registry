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
    """The verdict comes from RETENTION, not from the returned points.

    Every query is `range(start: <window>)`, so the earliest point it can
    return is at or after the window start. Comparing the two made the flag
    true on almost every non-empty answer — complete ones included — which
    left the signal firing when nothing was missing (review finding 3).
    Retention is the fact that actually bounds the history.
    """

    def test_a_complete_answer_is_not_flagged(self):
        """60 days retained indefinitely, 30 requested: nothing is missing.

        The exact case the review reproduced, and the one the old comparison
        got wrong: the oldest point of a complete 30-day answer is still later
        than the 30-day boundary.
        """
        points = [_point(NOW - timedelta(days=d)) for d in range(30)]
        coverage = _window_coverage(points, "-30d", 0, now=NOW)
        assert coverage["window_starts_before_data"] is False

    def test_a_window_wider_than_the_retention_is_flagged(self):
        """The real case: 30 days asked of a 7-day bucket."""
        points = [_point(NOW - timedelta(days=d)) for d in (1, 4, 7)]
        coverage = _window_coverage(points, "-30d", 7 * 86400, now=NOW)
        assert coverage["window_starts_before_data"] is True
        assert coverage["oldest_point"] == (NOW - timedelta(days=7)).isoformat()
        assert coverage["history_starts_at"] == (NOW - timedelta(days=7)).isoformat()
        assert coverage["retention_secs"] == 7 * 86400

    def test_a_window_inside_the_retention_is_not_flagged(self):
        points = [_point(NOW - timedelta(days=d)) for d in (1, 4, 7)]
        assert (
            _window_coverage(points, "-7d", 7 * 86400, now=NOW)["window_starts_before_data"]
            is False
        )

    def test_infinite_retention_can_never_be_flagged(self):
        """Item 8: the permanent deployments stop triggering it at `--days 0`."""
        for window in ("-7d", "-30d", "0"):
            coverage = _window_coverage([_point(NOW)], window, 0, now=NOW)
            assert coverage["window_starts_before_data"] is False, window
            assert coverage["history_starts_at"] is None

    def test_an_empty_answer_still_knows_whether_it_is_short(self):
        """It no longer needs a point to say so — retention is enough."""
        assert _window_coverage([], "-30d", 7 * 86400, now=NOW)["window_starts_before_data"] is True
        assert _window_coverage([], "-30d", 0, now=NOW)["window_starts_before_data"] is False

    def test_an_unreadable_retention_leaves_it_unknown(self):
        """ "Could not look" is not "nothing there"."""
        coverage = _window_coverage([_point(NOW)], "-30d", None, now=NOW)
        assert coverage["window_starts_before_data"] is None
        assert coverage["retention_secs"] is None

    def test_an_unresolvable_window_leaves_it_unknown(self):
        points = [_point(NOW - timedelta(days=1))]
        assert (
            _window_coverage(points, "-1mo", 7 * 86400, now=NOW)["window_starts_before_data"]
            is None
        )

    def test_the_oldest_point_is_still_reported(self):
        """It is what the warning names and what a reader checks by eye."""
        points = [_point(NOW - timedelta(days=d)) for d in (1, 9)]
        coverage = _window_coverage(points, "-30d", 0, now=NOW)
        assert coverage["oldest_point"] == (NOW - timedelta(days=9)).isoformat()

    def test_the_keys_are_always_present(self):
        """So a consumer can tell an unknown from a tool that predates this."""
        for retention in (None, 0, 7 * 86400):
            assert set(_window_coverage([], "-30d", retention, now=NOW)) == {
                "oldest_point",
                "window_start",
                "retention_secs",
                "history_starts_at",
                "window_starts_before_data",
            }


class TestTheBindingRetention:
    """A query spans both buckets, so the shortest finite retention binds."""

    def test_the_shortest_finite_retention_wins(self):
        from saorsa_deploy.influx_retention import binding_retention_seconds

        buckets = {"testnet-metrics": 30 * 86400, "wasm-client-metrics": 7 * 86400}
        with patch(
            "saorsa_deploy.influx_retention.find_bucket",
            lambda _u, _t, _o, name: {
                "name": name,
                "retentionRules": [{"type": "expire", "everySeconds": buckets[name]}],
            },
        ):
            assert binding_retention_seconds("u", "t", "o", list(buckets)) == 7 * 86400

    def test_all_infinite_is_infinite(self):
        from saorsa_deploy.influx_retention import binding_retention_seconds

        with patch(
            "saorsa_deploy.influx_retention.find_bucket",
            lambda _u, _t, _o, name: {"name": name, "retentionRules": []},
        ):
            assert binding_retention_seconds("u", "t", "o", ["a", "b"]) == 0

    def test_one_finite_bucket_binds_even_beside_an_infinite_one(self):
        from saorsa_deploy.influx_retention import binding_retention_seconds

        rules = {"a": [], "b": [{"type": "expire", "everySeconds": 86400}]}
        with patch(
            "saorsa_deploy.influx_retention.find_bucket",
            lambda _u, _t, _o, name: {"name": name, "retentionRules": rules[name]},
        ):
            assert binding_retention_seconds("u", "t", "o", ["a", "b"]) == 86400

    def test_an_absent_bucket_contributes_nothing(self):
        """A stack predating the WASM tier has no WASM bucket."""
        from saorsa_deploy.influx_retention import binding_retention_seconds

        rules = {"a": [{"type": "expire", "everySeconds": 86400}]}
        with patch(
            "saorsa_deploy.influx_retention.find_bucket",
            lambda _u, _t, _o, name: (
                {"name": name, "retentionRules": rules[name]} if name in rules else None
            ),
        ):
            assert binding_retention_seconds("u", "t", "o", ["a", "missing"]) == 86400

    def test_no_bucket_at_all_is_unknown_not_infinite(self):
        from saorsa_deploy.influx_retention import binding_retention_seconds

        with patch("saorsa_deploy.influx_retention.find_bucket", lambda *_a: None):
            assert binding_retention_seconds("u", "t", "o", ["a"]) is None

    def test_a_failed_read_is_unknown_not_infinite(self):
        import requests

        from saorsa_deploy.influx_retention import binding_retention_seconds

        with patch(
            "saorsa_deploy.influx_retention.find_bucket",
            side_effect=requests.ConnectionError("no route"),
        ):
            assert binding_retention_seconds("u", "t", "o", ["a"]) is None

    def test_the_resolver_never_raises(self):
        """A diagnostic must not break the report it sits beside."""
        from saorsa_deploy.cmd.print_file_ops import _resolve_retention

        with patch(
            "saorsa_deploy.cmd.print_file_ops.binding_retention_seconds",
            side_effect=RuntimeError("anything at all"),
        ):
            assert _resolve_retention(("u", "t", "o", "b"), ("b",), None) is None


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

    def test_the_coverage_keys_reach_the_document(self):
        doc = self._emit([_point(NOW - timedelta(days=7))], "-30d")
        assert doc["window"] == "-30d"
        assert doc["oldest_point"] == (NOW - timedelta(days=7)).isoformat()
        # The verdict needs the retention, which `_emit_ops_json` has no way to
        # read on its own; its default is the unknown it should be.
        assert doc["window_starts_before_data"] is None
        assert doc["retention_secs"] is None

    def test_a_caller_supplied_coverage_is_what_is_emitted(self):
        """The callers resolve the retention and pass the coverage in."""
        coverage = _window_coverage([_point(NOW - timedelta(days=7))], "-30d", 7 * 86400, now=NOW)
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
                points=[_point(NOW - timedelta(days=7))],
                window="-30d",
                coverage=coverage,
            )
        assert captured["window_starts_before_data"] is True
        assert captured["retention_secs"] == 7 * 86400

    def test_an_empty_answer_still_emits_them(self):
        doc = self._emit([], "-30d")
        assert doc["count"] == 0
        assert doc["oldest_point"] is None
        assert doc["window_starts_before_data"] is None


class TestTheConsoleWarning:
    def _warn(self, coverage, name="PROD-DL-01"):
        lines: list[str] = []
        console = SimpleNamespace(print=lambda *a, **_k: lines.append(" ".join(map(str, a))))
        _warn_if_truncated(coverage, name, console)
        return lines

    def test_it_names_the_retention_the_gap_and_both_ways_out(self):
        coverage = _window_coverage([_point(NOW - timedelta(days=7))], "-30d", 7 * 86400, now=NOW)
        lines = self._warn(coverage)
        assert len(lines) == 1
        # The retention is the explanation, so it is named...
        assert "7 day(s)" in lines[0]
        assert coverage["history_starts_at"] in lines[0]
        # ...along with the oldest point the issue asks for...
        assert coverage["oldest_point"] in lines[0]
        # ...and both commands that answer it.
        assert "print-recorded-results" in lines[0]
        assert "set-retention" in lines[0]
        assert "PROD-DL-01" in lines[0]

    def test_it_is_silent_on_a_complete_answer(self):
        """The regression: this used to fire on almost every answer."""
        points = [_point(NOW - timedelta(days=d)) for d in range(30)]
        assert self._warn(_window_coverage(points, "-30d", 0, now=NOW)) == []

    def test_it_is_silent_when_the_retention_is_unknown(self):
        assert self._warn(_window_coverage([_point(NOW)], "-30d", None, now=NOW)) == []


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
