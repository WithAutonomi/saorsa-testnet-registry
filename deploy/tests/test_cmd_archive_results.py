"""Unit tests for `deployments archive` (cmd_archive_results)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.archive_results import cmd_archive_results


def _args(**overrides):
    defaults = {
        "name": "PROD-DL-01",
        "monitoring_name": None,
        "window": "-7d",
        "loop": False,
        "interval_secs": 21600,
        "influx_url": None,
        "influx_token": None,
        "influx_org": None,
        "influx_bucket": None,
        "update_slack": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestOneShot:
    def test_delegates_once_to_record_results(self):
        with patch(
            "saorsa_deploy.cmd.archive_results.cmd_record_results"
        ) as p_record:
            cmd_archive_results(_args())
        p_record.assert_called_once()

    def test_forwards_expected_fields(self):
        with patch(
            "saorsa_deploy.cmd.archive_results.cmd_record_results"
        ) as p_record:
            cmd_archive_results(
                _args(window="-24h", monitoring_name="MON", update_slack=True)
            )
        forwarded = p_record.call_args.args[0]
        assert forwarded.name == "PROD-DL-01"
        assert forwarded.window == "-24h"
        assert forwarded.monitoring_name == "MON"
        assert forwarded.update_slack is True

    def test_one_shot_propagates_systemexit(self):
        # One-shot mode must NOT swallow record-results' exit code, so an
        # external scheduler (cron) sees the failure.
        with patch(
            "saorsa_deploy.cmd.archive_results.cmd_record_results",
            side_effect=SystemExit(1),
        ):
            with pytest.raises(SystemExit):
                cmd_archive_results(_args())


class TestLoop:
    def test_loop_retries_after_systemexit_and_keeps_going(self):
        # In loop mode a transient failure must not tear the loop down. We let
        # it run two iterations then break out via a sentinel on sleep.
        calls = {"n": 0}

        def _record(_a):
            calls["n"] += 1
            raise SystemExit(1)

        def _sleep(_secs):
            if calls["n"] >= 2:
                raise KeyboardInterrupt  # break the otherwise-infinite loop

        with (
            patch(
                "saorsa_deploy.cmd.archive_results.cmd_record_results",
                side_effect=_record,
            ),
            patch("saorsa_deploy.cmd.archive_results.time.sleep", _sleep),
        ):
            with pytest.raises(KeyboardInterrupt):
                cmd_archive_results(_args(loop=True))
        assert calls["n"] >= 2

    def test_loop_sleeps_between_iterations(self):
        slept = []

        def _sleep(secs):
            slept.append(secs)
            raise KeyboardInterrupt

        with (
            patch("saorsa_deploy.cmd.archive_results.cmd_record_results"),
            patch("saorsa_deploy.cmd.archive_results.time.sleep", _sleep),
        ):
            with pytest.raises(KeyboardInterrupt):
                cmd_archive_results(_args(loop=True, interval_secs=42))
        assert slept == [42]
