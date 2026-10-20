"""`print-recorded-results --name` resolves the LIVE row, never a pinned id.

A permanent prod deployment is torn down and stood up again under the same
name, which gives it a new registry id. A caller pinning the old id keeps
exiting 0 and printing a well-formed report for the destroyed row, so nothing
says the deployment moved on: `PROD-WUL-01` was reported as not having run for
three days after a redeploy moved it from id 611 to 673, while it was in fact
uploading every day under the new row.

`--id` stays exactly as it was -- a historical row is what it is for -- so
these tests pin both halves: the name path follows the live row, and the id
path still reads the exact one it was given, destroyed or not.
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.print_recorded_results import cmd_print_recorded_results

DESTROYED_ID = 611
LIVE_ID = 673
NAME = "PROD-WUL-01"

_PROD_UPLOADER_ARGS = {"kind": "prod-continuous-uploader", "client_kind": "wasm"}


class _FakeRepo:
    """Only the reads this command makes, with the rows left empty.

    What is under test is which deployment row gets selected, so every result
    list is empty: the sections then print their "nothing recorded" lines and
    the header still names the row that was chosen.
    """

    def __init__(self, active=None, by_name=None, by_id=None):
        self._active = active
        self._by_name = by_name
        self._by_id = by_id or {}
        self.active_calls = []
        self.by_name_calls = []
        self.get_calls = []

    def get_active_by_name(self, name):
        self.active_calls.append(name)
        return self._active

    def get_by_name(self, name):
        self.by_name_calls.append(name)
        return self._by_name

    def get(self, deployment_id):
        self.get_calls.append(deployment_id)
        return self._by_id.get(deployment_id)

    def get_host_result_for_deployment(self, deployment_id):
        return None

    def list_derived_metrics_for_deployment(self, deployment_id):
        return []

    def list_prod_upload_results_for_deployment(self, deployment_id):
        return []

    def list_prod_upload_verifications_for_deployment(self, deployment_id):
        return []


def _deployment(deployment_id):
    return SimpleNamespace(
        id=deployment_id,
        name=NAME,
        deploy_args=dict(_PROD_UPLOADER_ARGS),
    )


def _args(**overrides):
    base = dict(deployment_id=None, name=None, json_out=None, details=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(repo, args, capsys):
    with patch("saorsa_deploy.cmd.print_recorded_results.get_repository", return_value=repo):
        cmd_print_recorded_results(args)
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# Resolving by name
# ---------------------------------------------------------------------------


def test_name_selects_the_active_row_not_the_destroyed_one(capsys):
    """The regression itself: both rows carry the name, and the live one wins.

    Not "the newest id" and not "the first found" -- the repository's own
    non-terminal lookup is what decides, so a deployment recreated with a
    lower id would still resolve correctly.
    """
    repo = _FakeRepo(
        active=_deployment(LIVE_ID),
        by_name=_deployment(DESTROYED_ID),
        by_id={DESTROYED_ID: _deployment(DESTROYED_ID), LIVE_ID: _deployment(LIVE_ID)},
    )

    out = _run(repo, _args(name=NAME), capsys)

    assert repo.active_calls == [NAME]
    # The active lookup answered, so the fallback is never reached.
    assert repo.by_name_calls == []
    assert f"id={LIVE_ID}" in out
    assert f"id={DESTROYED_ID}" not in out


def test_name_falls_back_to_the_most_recent_row_once_destroyed(capsys):
    """A fully destroyed deployment stays readable by name.

    Its rows are the record of what it did, and they outlive it -- refusing the
    name because nothing is running would make the command unusable for exactly
    the post-teardown reading it exists for.
    """
    repo = _FakeRepo(active=None, by_name=_deployment(DESTROYED_ID))

    out = _run(repo, _args(name=NAME), capsys)

    assert repo.active_calls == [NAME]
    assert repo.by_name_calls == [NAME]
    assert f"id={DESTROYED_ID}" in out


def test_name_matching_nothing_exits_1_naming_the_name(capsys):
    repo = _FakeRepo(active=None, by_name=None)

    with pytest.raises(SystemExit) as exc:
        _run(repo, _args(name="NO-SUCH-DEPLOYMENT"), capsys)

    assert exc.value.code == 1
    assert "NO-SUCH-DEPLOYMENT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Resolving by id -- unchanged
# ---------------------------------------------------------------------------


def test_id_still_selects_that_exact_row_including_a_destroyed_one(capsys):
    """Historical rows must stay reachable: several analyses depend on it."""
    repo = _FakeRepo(
        active=_deployment(LIVE_ID),
        by_name=_deployment(LIVE_ID),
        by_id={DESTROYED_ID: _deployment(DESTROYED_ID)},
    )

    out = _run(repo, _args(deployment_id=DESTROYED_ID), capsys)

    assert repo.get_calls == [DESTROYED_ID]
    # The name is never consulted on the id path, so a live row of the same
    # name cannot displace the one that was asked for.
    assert repo.active_calls == []
    assert repo.by_name_calls == []
    assert f"id={DESTROYED_ID}" in out


def test_an_unknown_id_exits_1_naming_the_id(capsys):
    repo = _FakeRepo(by_id={})

    with pytest.raises(SystemExit) as exc:
        _run(repo, _args(deployment_id=99999), capsys)

    assert exc.value.code == 1
    assert "99999" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The parser: exactly one of the two
# ---------------------------------------------------------------------------
#
# The real parser is built inside `main()`, so -- as `test_cmd_ls.py` does for
# the same reason -- it is exercised through the CLI. Both cases are rejected
# by argparse before any database is touched.


def test_the_parser_registers_both_sources():
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "deployments", "print-recorded-results", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--id" in result.stdout
    assert "--name" in result.stdout


def test_the_parser_rejects_both_at_once():
    result = subprocess.run(
        [
            "uv",
            "run",
            "saorsa-deploy",
            "deployments",
            "print-recorded-results",
            "--id",
            str(DESTROYED_ID),
            "--name",
            NAME,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr or "mutually exclusive" in result.stderr


def test_the_parser_rejects_neither():
    """`required=True` lives on the group, and is not merely dropped from
    `--id`: omitting both has to fail at the parser rather than further in,
    where it would read as a missing deployment."""
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "deployments", "print-recorded-results"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "one of the arguments" in result.stderr
