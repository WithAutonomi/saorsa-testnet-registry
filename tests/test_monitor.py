"""Tests for the background monitor (_poll_once and _map_github_status)."""

from unittest.mock import patch

from saorsa_web.github import GitHubAPIError
from saorsa_web.models.deployment import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_web.monitor import _map_github_status, _poll_once


# ---------------------------------------------------------------------------
# _map_github_status tests
# ---------------------------------------------------------------------------


class TestMapGithubStatus:
    def test_map_github_status_queued_returns_running(self):
        gh_run = {"status": "queued", "conclusion": None}
        result = _map_github_status(gh_run)
        assert result == (DeploymentStatus.RUNNING, "running")

    def test_map_github_status_in_progress_returns_running(self):
        gh_run = {"status": "in_progress", "conclusion": None}
        result = _map_github_status(gh_run)
        assert result == (DeploymentStatus.RUNNING, "running")

    def test_map_github_status_completed_success_returns_running(self):
        gh_run = {"status": "completed", "conclusion": "success"}
        result = _map_github_status(gh_run)
        assert result == (DeploymentStatus.RUNNING, "completed")

    def test_map_github_status_completed_failure_returns_failed(self):
        gh_run = {"status": "completed", "conclusion": "failure"}
        result = _map_github_status(gh_run)
        assert result == (DeploymentStatus.FAILED, "failed")

    def test_map_github_status_in_progress_while_destroying_returns_none(self):
        gh_run = {"status": "in_progress", "conclusion": None}
        result = _map_github_status(gh_run, DeploymentStatus.DESTROYING)
        assert result is None

    def test_map_github_status_completed_success_while_destroying_returns_destroyed(
        self,
    ):
        gh_run = {"status": "completed", "conclusion": "success"}
        result = _map_github_status(gh_run, DeploymentStatus.DESTROYING)
        assert result == (DeploymentStatus.DESTROYED, "destroyed")


# ---------------------------------------------------------------------------
# Helper to build a test deployment
# ---------------------------------------------------------------------------

_DEFAULT_DEPLOY_ARGS = {
    "name": "TEST-01",
    "vm_count": 1,
    "node_count": 1,
    "genesis_port": 12000,
}


def _make_deployment(
    name="TEST-01",
    workflow_run_id=99999,
    status=DeploymentStatus.PENDING,
):
    return Deployment(
        name=name,
        workflow_run_id=workflow_run_id,
        deploy_args=dict(_DEFAULT_DEPLOY_ARGS, name=name),
        status=status,
    )


def _gh_run(
    run_id=99999,
    status="in_progress",
    conclusion=None,
    html_url="https://github.com/example/actions/runs/99999",
):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "html_url": html_url,
    }


# ---------------------------------------------------------------------------
# _poll_once tests
# ---------------------------------------------------------------------------


class TestPollOnce:
    @patch("saorsa_web.monitor.get_workflow_run")
    def test_poll_once_updates_deployment_status_on_change(self, mock_get_run, db):
        deployment = _make_deployment(status=DeploymentStatus.PENDING)
        db.add(deployment)
        db.commit()

        mock_get_run.return_value = _gh_run(status="in_progress")

        _poll_once()

        db.expire_all()
        updated = db.query(Deployment).filter_by(name="TEST-01").one()
        assert updated.status == DeploymentStatus.RUNNING

        events = db.query(DeploymentEvent).filter_by(deployment_id=updated.id).all()
        assert len(events) == 1
        assert events[0].event_type == "running"
        assert events[0].details["previous_status"] == "pending"
        assert events[0].details["new_status"] == "running"

    @patch("saorsa_web.monitor.get_workflow_run")
    def test_poll_once_records_completion_event(self, mock_get_run, db):
        deployment = _make_deployment(
            status=DeploymentStatus.RUNNING, workflow_run_id=88888
        )
        db.add(deployment)
        db.commit()

        mock_get_run.return_value = _gh_run(
            run_id=88888, status="completed", conclusion="success"
        )

        _poll_once()

        db.expire_all()
        updated = db.query(Deployment).filter_by(name="TEST-01").one()
        assert updated.status == DeploymentStatus.RUNNING

        events = db.query(DeploymentEvent).filter_by(deployment_id=updated.id).all()
        assert len(events) == 1
        assert events[0].event_type == "completed"
        assert events[0].details["previous_status"] == "running"
        assert events[0].details["new_status"] == "running"

    @patch("saorsa_web.monitor.get_workflow_run")
    def test_poll_once_continues_on_github_api_error(self, mock_get_run, db):
        dep_a = _make_deployment(name="FAIL-01", workflow_run_id=11111)
        dep_b = _make_deployment(
            name="OK-01",
            workflow_run_id=22222,
            status=DeploymentStatus.PENDING,
        )
        db.add_all([dep_a, dep_b])
        db.commit()

        def side_effect(run_id, **kwargs):
            if run_id == 11111:
                raise GitHubAPIError("API down")
            return _gh_run(run_id=22222, status="in_progress")

        mock_get_run.side_effect = side_effect

        _poll_once()

        db.expire_all()
        # First deployment should be unchanged (still pending)
        still_pending = db.query(Deployment).filter_by(name="FAIL-01").one()
        assert still_pending.status == DeploymentStatus.PENDING

        # Second deployment should have been updated
        updated = db.query(Deployment).filter_by(name="OK-01").one()
        assert updated.status == DeploymentStatus.RUNNING

        events = db.query(DeploymentEvent).filter_by(deployment_id=updated.id).all()
        assert len(events) == 1
        assert events[0].event_type == "running"

    @patch("saorsa_web.monitor.get_workflow_run")
    def test_poll_once_no_change_when_status_same(self, mock_get_run, db):
        deployment = _make_deployment(
            status=DeploymentStatus.RUNNING, workflow_run_id=77777
        )
        db.add(deployment)
        db.commit()

        mock_get_run.return_value = _gh_run(run_id=77777, status="in_progress")

        _poll_once()

        db.expire_all()
        unchanged = db.query(Deployment).filter_by(name="TEST-01").one()
        assert unchanged.status == DeploymentStatus.RUNNING

        events = db.query(DeploymentEvent).filter_by(deployment_id=unchanged.id).all()
        assert len(events) == 0
