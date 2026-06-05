"""Tests for the `deployments up` step journal: S3 persistence (state.py) and
the pure match/diff logic (orchestrator/journal.py)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.orchestrator import journal as step_journal
from saorsa_deploy.orchestrator.steps import Step
from saorsa_deploy.state import (
    S3_BUCKET,
    S3_JOURNAL_KEY_PREFIX,
    delete_step_journal,
    load_step_journal,
    save_step_journal,
)


@pytest.fixture
def mock_s3():
    with patch("saorsa_deploy.state.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        yield mock_client


class TestJournalState:
    def test_save_puts_json_to_s3(self, mock_s3):
        journal = step_journal.new_journal("DEV-01", "testnet")
        save_step_journal("DEV-01", journal)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == S3_BUCKET
        assert call_kwargs["Key"] == f"{S3_JOURNAL_KEY_PREFIX}/DEV-01.json"
        assert call_kwargs["ContentType"] == "application/json"
        body = json.loads(call_kwargs["Body"])
        assert body["name"] == "DEV-01"
        assert body["kind"] == "testnet"
        assert body["steps"] == []

    def test_load_returns_parsed_journal(self, mock_s3):
        journal = {"name": "DEV-01", "kind": "testnet", "steps": []}
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(journal).encode())
        }
        assert load_step_journal("DEV-01") == journal

    def test_load_returns_none_when_missing(self, mock_s3):
        error_class = type("NoSuchKey", (Exception,), {})
        mock_s3.exceptions.NoSuchKey = error_class
        mock_s3.get_object.side_effect = error_class()
        assert load_step_journal("DEV-01") is None

    def test_delete_removes_journal_key(self, mock_s3):
        delete_step_journal("DEV-01")
        mock_s3.delete_object.assert_called_once_with(
            Bucket=S3_BUCKET, Key=f"{S3_JOURNAL_KEY_PREFIX}/DEV-01.json"
        )


def _plan() -> list[Step]:
    return [
        Step(["build", "ant-node", "--name", "DEV-01"], "Build ant-node from branch"),
        Step(["bootstraps", "infra", "--name", "DEV-01"], "Bootstraps infra"),
        Step(["nodes", "infra", "--name", "DEV-01"], "Nodes infra"),
    ]


def _journal_with(plan: list[Step], count: int) -> dict:
    journal = step_journal.new_journal("DEV-01", "testnet")
    for step in plan[:count]:
        step_journal.record_step(journal, step)
    return journal


class TestRecordStep:
    def test_appends_argv_label_and_status(self):
        journal = step_journal.new_journal("DEV-01", "testnet")
        step_journal.record_step(journal, _plan()[0])
        assert journal["steps"] == [
            {
                "argv": ["build", "ant-node", "--name", "DEV-01"],
                "label": "Build ant-node from branch",
                "status": step_journal.STATUS_SUCCEEDED,
            }
        ]

    def test_records_best_effort_failure_status(self):
        journal = step_journal.new_journal("DEV-01", "testnet")
        step_journal.record_step(
            journal, _plan()[0], step_journal.STATUS_BEST_EFFORT_FAILED
        )
        assert journal["steps"][0]["status"] == step_journal.STATUS_BEST_EFFORT_FAILED


class TestMatchJournal:
    def test_empty_journal_matches_at_zero(self):
        plan = _plan()
        match = step_journal.match_journal(_journal_with(plan, 0), "testnet", plan)
        assert match.matches
        assert match.resume_index == 0

    def test_prefix_match_resumes_after_completed_steps(self):
        plan = _plan()
        match = step_journal.match_journal(_journal_with(plan, 2), "testnet", plan)
        assert match.matches
        assert match.resume_index == 2

    def test_full_journal_matches_whole_plan(self):
        plan = _plan()
        match = step_journal.match_journal(_journal_with(plan, 3), "testnet", plan)
        assert match.matches
        assert match.resume_index == 3

    def test_changed_argv_diverges_at_first_difference(self):
        plan = _plan()
        journal = _journal_with(plan, 3)
        journal["steps"][1]["argv"] = ["bootstraps", "infra", "--name", "OTHER"]
        match = step_journal.match_journal(journal, "testnet", plan)
        assert not match.matches
        assert match.divergence == 1
        assert match.resume_index == 1  # longest matching prefix

    def test_journal_longer_than_plan_diverges(self):
        plan = _plan()
        journal = _journal_with(plan, 3)
        match = step_journal.match_journal(journal, "testnet", plan[:2])
        assert not match.matches
        assert match.divergence == 2
        assert match.resume_index == 2

    def test_kind_change_diverges_at_zero(self):
        plan = _plan()
        journal = _journal_with(plan, 2)
        match = step_journal.match_journal(journal, "testnet-extension", plan)
        assert not match.matches
        assert match.divergence == 0
        assert match.resume_index == 0


class TestRendering:
    def test_summary_lists_completed_steps(self):
        plan = _plan()
        lines = step_journal.render_journal_summary(_journal_with(plan, 2), plan)
        assert "DEV-01" in lines[0]
        assert "2/3 steps completed" in lines[0]
        assert "Build ant-node from branch" in lines[1]
        assert "Bootstraps infra" in lines[2]

    def test_summary_flags_best_effort_failures(self):
        plan = _plan()
        journal = _journal_with(plan, 1)
        journal["steps"][0]["status"] = step_journal.STATUS_BEST_EFFORT_FAILED
        lines = step_journal.render_journal_summary(journal, plan)
        assert "best-effort" in lines[1]

    def test_divergence_shows_both_commands(self):
        plan = _plan()
        journal = _journal_with(plan, 2)
        journal["steps"][1]["argv"] = ["bootstraps", "infra", "--name", "OTHER"]
        match = step_journal.match_journal(journal, "testnet", plan)
        text = "\n".join(
            step_journal.render_divergence(journal, "testnet", plan, match)
        )
        assert "step 2" in text
        assert "--name OTHER" in text
        assert "--name DEV-01" in text

    def test_divergence_explains_kind_change(self):
        plan = _plan()
        journal = _journal_with(plan, 1)
        match = step_journal.match_journal(journal, "testnet-extension", plan)
        text = "\n".join(
            step_journal.render_divergence(journal, "testnet-extension", plan, match)
        )
        assert "testnet-extension" in text
        assert "'testnet'" in text

    def test_divergence_explains_shorter_plan(self):
        plan = _plan()
        journal = _journal_with(plan, 3)
        match = step_journal.match_journal(journal, "testnet", plan[:2])
        text = "\n".join(
            step_journal.render_divergence(journal, "testnet", plan[:2], match)
        )
        assert "3 completed step(s)" in text
        assert "2 step(s)" in text
