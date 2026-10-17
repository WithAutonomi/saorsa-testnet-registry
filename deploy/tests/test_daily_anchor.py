"""The daily anchor shared by the two continuous prod client service scripts.

Both scripts are pushed to their VM as a single self-contained file, so they
cannot import a shared module — the anchor block is duplicated by hand and kept
byte-identical. Every test here therefore runs against **both** copies: a change
applied to one script and not the other is what this file exists to catch.

The scripts are loaded by path, the way ``test_prod_downloader_service.py`` does.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts"
DOWNLOADER_PATH = SCRIPTS_DIR / "prod_downloader.py"
UPLOADER_PATH = SCRIPTS_DIR / "prod_continuous_uploader.py"

BLOCK_START = (
    "# --- daily anchor: keep byte-identical in prod_downloader.py "
    "and prod_continuous_uploader.py ---"
)
BLOCK_END = "# --- end daily anchor ---"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _anchor_block(path: Path) -> str:
    text = path.read_text()
    start = text.index(BLOCK_START) + len(BLOCK_START)
    return text[start : text.index(BLOCK_END)]


@pytest.fixture(scope="module", params=["downloader", "uploader"])
def anchor(request):
    """The anchor functions from one of the two scripts."""
    return _load(DOWNLOADER_PATH if request.param == "downloader" else UPLOADER_PATH)


def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestDailyAnchorBlocksIdentical:
    """The one property that keeps two hand-copied blocks from drifting."""

    def test_blocks_are_identical_and_non_empty(self):
        downloader = _anchor_block(DOWNLOADER_PATH)
        uploader = _anchor_block(UPLOADER_PATH)
        assert downloader.strip(), "the downloader's anchor block is empty"
        assert downloader == uploader


class TestMostRecentAnchor:
    def test_today_when_the_time_has_passed(self, anchor):
        now = _utc(2026, 10, 17, 15, 0)
        assert anchor.most_recent_anchor(now, "00:00") == _utc(2026, 10, 17)

    def test_yesterday_when_the_time_is_still_to_come(self, anchor):
        now = _utc(2026, 10, 17, 1, 59)
        assert anchor.most_recent_anchor(now, "02:00") == _utc(2026, 10, 16, 2, 0)

    def test_now_exactly_on_the_anchor_is_today(self, anchor):
        now = _utc(2026, 10, 17, 2, 0)
        assert anchor.most_recent_anchor(now, "02:00") == now

    def test_a_non_utc_now_is_converted_not_truncated(self, anchor):
        # The anchor is a UTC wall-clock time, so a `now` carrying any other
        # offset must be compared in UTC rather than having its own clock read.
        now = datetime(2026, 10, 17, 1, 0, tzinfo=timezone(timedelta(hours=3)))
        assert anchor.most_recent_anchor(now, "00:00") == _utc(2026, 10, 16)


class TestSecondsUntilNextCycle:
    def test_no_state_file_runs_now(self, anchor):
        # A fresh deploy proves itself immediately rather than waiting up to a
        # day to show whether it works at all.
        assert anchor.seconds_until_next_cycle(_utc(2026, 10, 17, 15, 0), "00:00", None) == 0.0

    def test_cycle_completed_since_the_anchor_waits_for_the_next(self, anchor):
        now = _utc(2026, 10, 17, 15, 0)
        wait = anchor.seconds_until_next_cycle(now, "00:00", _utc(2026, 10, 17, 3, 22))
        assert wait == (_utc(2026, 10, 18) - now).total_seconds()

    def test_cycle_completed_before_the_anchor_runs_now(self, anchor):
        now = _utc(2026, 10, 17, 15, 0)
        assert anchor.seconds_until_next_cycle(now, "00:00", _utc(2026, 10, 16, 23, 50)) == 0.0

    def test_waits_before_a_late_anchor_and_runs_on_it(self, anchor):
        # 02:00 with yesterday's cycle already done: a minute before the anchor
        # there is still a wait, a second after it the cycle is due.
        last = _utc(2026, 10, 16, 2, 5)
        before = anchor.seconds_until_next_cycle(_utc(2026, 10, 17, 1, 59), "02:00", last)
        assert before == 60.0
        assert anchor.seconds_until_next_cycle(_utc(2026, 10, 17, 2, 0, 1), "02:00", last) == 0.0

    def test_exactly_on_the_anchor_with_a_cycle_one_second_earlier_runs_now(self, anchor):
        now = _utc(2026, 10, 17, 0, 0)
        last = now - timedelta(seconds=1)
        assert anchor.seconds_until_next_cycle(now, "00:00", last) == 0.0

    def test_an_overrunning_cycle_waits_for_the_anchor_after_the_one_it_passed(self, anchor):
        # Started at 2026-10-17 00:00Z and finished 25 hours later, so it ran
        # past the 10-18 anchor. The next cycle is 10-19, not immediately.
        completed = _utc(2026, 10, 18, 1, 0)
        wait = anchor.seconds_until_next_cycle(completed, "00:00", completed)
        assert wait == (_utc(2026, 10, 19) - completed).total_seconds()

    def test_malformed_daily_at_is_treated_as_midnight(self, anchor):
        # The arithmetic never raises on a bad anchor: parse_daily_at is what
        # logs and substitutes, and this is the belt-and-braces half.
        now = _utc(2026, 10, 17, 15, 0)
        assert (
            anchor.seconds_until_next_cycle(now, "nonsense", _utc(2026, 10, 17, 3, 22))
            == (_utc(2026, 10, 18) - now).total_seconds()
        )


class TestParseDailyAt:
    @pytest.mark.parametrize("value", ["00:00", "02:00", "23:59", "09:05"])
    def test_accepts_a_strict_hh_mm(self, anchor, value):
        assert anchor.parse_daily_at(value) == value

    @pytest.mark.parametrize("value", [None, "", "24:00", "0:00", "00:00:00", "0000", "00:60"])
    def test_falls_back_to_midnight_with_a_logged_line(self, anchor, value, capsys):
        assert anchor.parse_daily_at(value) == "00:00"
        out = capsys.readouterr().out
        assert "DAILY_AT" in out and "HH:MM" in out

    def test_surrounding_whitespace_is_tolerated(self, anchor):
        assert anchor.parse_daily_at("  02:00 ") == "02:00"


class TestReadLastCycle:
    def test_missing_file_is_unknown(self, anchor, tmp_path):
        assert anchor.read_last_cycle(tmp_path / "nope") is None

    def test_garbage_is_unknown_and_logged(self, anchor, tmp_path, capsys):
        path = tmp_path / "last-cycle"
        path.write_text("garbage")
        assert anchor.read_last_cycle(path) is None
        assert "garbage" in capsys.readouterr().out

    def test_empty_file_is_unknown(self, anchor, tmp_path):
        path = tmp_path / "last-cycle"
        path.write_text("")
        assert anchor.read_last_cycle(path) is None

    def test_a_naive_instant_is_read_as_utc(self, anchor, tmp_path):
        path = tmp_path / "last-cycle"
        path.write_text("2026-10-17T03:22:00")
        assert anchor.read_last_cycle(path) == _utc(2026, 10, 17, 3, 22)


class TestWriteLastCycle:
    def test_round_trips_to_the_second(self, anchor, tmp_path):
        path = tmp_path / "state" / "last-cycle"
        when = _utc(2026, 10, 17, 3, 22, 45)
        anchor.write_last_cycle(path, when)
        assert anchor.read_last_cycle(path) == when

    def test_creates_the_parent_directory(self, anchor, tmp_path):
        path = tmp_path / "missing" / "deeper" / "last-cycle"
        anchor.write_last_cycle(path, _utc(2026, 10, 17))
        assert path.exists()

    def test_the_write_is_atomic(self, anchor, tmp_path):
        # Through a temp file and os.replace, so a crash mid-write leaves the
        # previous instant rather than a half-written one — which would read as
        # unknown and run an extra cycle.
        path = tmp_path / "last-cycle"
        anchor.write_last_cycle(path, _utc(2026, 10, 17))
        assert not (tmp_path / "last-cycle.tmp").exists()

    def test_overwrites_an_earlier_instant(self, anchor, tmp_path):
        path = tmp_path / "last-cycle"
        anchor.write_last_cycle(path, _utc(2026, 10, 16))
        anchor.write_last_cycle(path, _utc(2026, 10, 17))
        assert anchor.read_last_cycle(path) == _utc(2026, 10, 17)


class TestWaitForNextCycle:
    """The sleeping wrapper: slices, and never sleeps when a cycle is due."""

    def test_returns_immediately_when_a_cycle_is_due(self, anchor, tmp_path, monkeypatch):
        slept = []
        monkeypatch.setattr(anchor.time, "sleep", slept.append)
        anchor.wait_for_next_cycle("00:00", tmp_path / "last-cycle")
        assert slept == []

    def test_sleeps_in_slices_until_the_anchor(self, anchor, tmp_path, monkeypatch):
        path = tmp_path / "last-cycle"
        # A cycle completed one minute after today's anchor, so the next is
        # nearly a day away — but no single sleep may be longer than a slice,
        # or a clock step could overshoot the anchor unnoticed.
        anchor.write_last_cycle(path, _utc(2026, 10, 17, 0, 1))
        slept = []

        def fake_sleep(secs):
            slept.append(secs)
            if len(slept) == 3:
                # Third slice: pretend the day has passed so the wait ends.
                path.write_text(_utc(2026, 10, 16, 0, 1).isoformat())

        monkeypatch.setattr(anchor.time, "sleep", fake_sleep)
        monkeypatch.setattr(anchor, "datetime", _FrozenDatetime(_utc(2026, 10, 17, 0, 2)))
        anchor.wait_for_next_cycle("00:00", path)
        assert slept == [anchor.ANCHOR_SLEEP_SLICE_SECS] * 3

    def test_logs_the_next_anchor_instant(self, anchor, tmp_path, monkeypatch, capsys):
        # The unattended maintenance runs grep the journal for the ISO instant,
        # so it has to be in the line and not only in the seconds count.
        path = tmp_path / "last-cycle"
        anchor.write_last_cycle(path, _utc(2026, 10, 17, 0, 1))
        monkeypatch.setattr(anchor, "datetime", _FrozenDatetime(_utc(2026, 10, 17, 0, 2)))
        monkeypatch.setattr(anchor.time, "sleep", lambda _: path.unlink())
        anchor.wait_for_next_cycle("00:00", path)
        out = capsys.readouterr().out
        assert _utc(2026, 10, 18).isoformat() in out


class _FrozenDatetime:
    """Stands in for the ``datetime`` class so ``now()`` is deterministic."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now if tz is None else self._now.astimezone(tz)

    def fromisoformat(self, value):
        return datetime.fromisoformat(value)
