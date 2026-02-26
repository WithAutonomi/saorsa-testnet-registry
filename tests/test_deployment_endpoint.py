"""BDD-style tests for the POST /api/deployments endpoint and related routes."""

from unittest.mock import patch

from saorsa_web.github import GitHubAPIError
from saorsa_web.models.deployment import Deployment, DeploymentEvent


class TestCreateDeployment:
    """Tests for POST /api/deployments."""

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=12345)
    def test_create_deployment_dispatches_workflow_and_records_in_db(
        self, mock_dispatch, auth_session, db
    ):
        """
        GIVEN a valid request with name and deploy_args
        WHEN POST /api/deployments
        THEN dispatch_workflow is called with the deploy_args
        AND a deployment is recorded in the database with correct fields
        AND a "created" event is recorded in the database
        AND the response contains the deployment info with status 201
        """
        payload = {
            "name": "test-network-1",
            "deploy_args": {"node_count": 10, "region": "lon1"},
        }

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 201
        data = response.get_json()
        assert data["name"] == "test-network-1"
        assert data["status"] == "pending"
        assert data["workflow_run_id"] == 12345
        assert "deploy_args" not in data

        # Verify dispatch_workflow was called with the correct arguments
        mock_dispatch.assert_called_once_with(
            {"node_count": 10, "region": "lon1"},
            pat="fake-pat-for-testing",
            repo="test-owner/test-repo",
            workflow_file="test-workflow.yml",
            branch="main",
        )

        # Verify the deployment was persisted in the database
        deployment = db.query(Deployment).filter_by(name="test-network-1").one()
        assert deployment.workflow_run_id == 12345
        assert deployment.status.value == "pending"
        assert deployment.deploy_args == {"node_count": 10, "region": "lon1"}

        # Verify a "created" event was recorded
        event = (
            db.query(DeploymentEvent)
            .filter_by(deployment_id=deployment.id, event_type="created")
            .one()
        )
        assert event.details == {"workflow_run_id": 12345}

    @patch("saorsa_web.api.deployments.dispatch_workflow")
    def test_create_deployment_returns_error_when_dispatch_fails(
        self, mock_dispatch, auth_session, db
    ):
        """
        GIVEN a valid request
        WHEN POST /api/deployments and dispatch_workflow raises GitHubAPIError
        THEN the response is 502 with error message
        AND no deployment is recorded in the database
        """
        mock_dispatch.side_effect = GitHubAPIError("dispatch failed: 403 Forbidden")

        payload = {
            "name": "test-network-fail",
            "deploy_args": {"node_count": 5},
        }

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 502
        data = response.get_json()
        assert "error" in data
        assert "dispatch failed" in data["error"]

        # Verify no deployment was created
        count = db.query(Deployment).filter_by(name="test-network-fail").count()
        assert count == 0

    def test_create_deployment_validates_name_required(self, auth_session):
        """
        GIVEN a request with missing name
        WHEN POST /api/deployments
        THEN the response is 400
        """
        payload = {"deploy_args": {"node_count": 5}}

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 400
        data = response.get_json()
        assert "name" in data["error"].lower()

    def test_create_deployment_validates_deploy_args_required(self, auth_session):
        """
        GIVEN a request with missing deploy_args
        WHEN POST /api/deployments
        THEN the response is 400
        """
        payload = {"name": "test-network-no-args"}

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 400
        data = response.get_json()
        assert "deploy_args" in data["error"]

    def test_create_deployment_returns_401_without_auth(self, client):
        """
        GIVEN no authentication
        WHEN POST /api/deployments with JSON Accept header
        THEN the response is 401
        """
        payload = {
            "name": "test-network-no-auth",
            "deploy_args": {"node_count": 5},
        }

        response = client.post(
            "/api/deployments",
            json=payload,
            headers={"Accept": "application/json"},
        )

        assert response.status_code == 401


class TestGetDeployment:
    """Tests for GET /api/deployments/<id>."""

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=99999)
    def test_get_deployment_returns_events(self, mock_dispatch, auth_session, db):
        """
        GIVEN a deployment exists with events
        WHEN GET /api/deployments/{id}
        THEN the response includes events list
        """
        # Create a deployment via the API (which also creates a "created" event)
        create_response = auth_session.post(
            "/api/deployments",
            json={"name": "net-with-events", "deploy_args": {"size": "small"}},
        )
        assert create_response.status_code == 201
        deployment_id = create_response.get_json()["id"]

        # Fetch the deployment
        response = auth_session.get(f"/api/deployments/{deployment_id}")

        assert response.status_code == 200
        data = response.get_json()
        assert data["id"] == deployment_id
        assert data["name"] == "net-with-events"
        assert isinstance(data["events"], list)
        assert len(data["events"]) >= 1
        assert data["events"][0]["event_type"] == "created"


class TestListDeploymentEvents:
    """Tests for GET /api/deployments/<id>/events."""

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=77777)
    def test_list_deployment_events(self, mock_dispatch, auth_session, db):
        """
        GIVEN a deployment exists with events
        WHEN GET /api/deployments/{id}/events
        THEN the response is a list of events
        """
        # Create a deployment via the API
        create_response = auth_session.post(
            "/api/deployments",
            json={"name": "net-events-list", "deploy_args": {"nodes": 3}},
        )
        assert create_response.status_code == 201
        deployment_id = create_response.get_json()["id"]

        # Add a second event directly in the database
        extra_event = DeploymentEvent(
            deployment_id=deployment_id,
            event_type="status_changed",
            details={"from": "pending", "to": "running"},
        )
        db.add(extra_event)
        db.commit()

        # Fetch the events list
        response = auth_session.get(f"/api/deployments/{deployment_id}/events")

        assert response.status_code == 200
        events = response.get_json()
        assert isinstance(events, list)
        assert len(events) == 2

        event_types = [e["event_type"] for e in events]
        assert "created" in event_types
        assert "status_changed" in event_types
