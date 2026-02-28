"""BDD-style tests for the POST /api/deployments endpoint and related routes."""

from unittest.mock import patch

from saorsa_web.github import GitHubAPIError
from saorsa_web.models.deployment import Deployment, DeploymentEvent, DeploymentStatus


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

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=11111)
    def test_create_deployment_with_node_version(self, mock_dispatch, auth_session, db):
        """
        GIVEN deploy_args containing node_version
        WHEN POST /api/deployments
        THEN the deployment is created and deploy_args include node_version
        """
        payload = {
            "name": "test-versioned",
            "deploy_args": {
                "node_count": 10,
                "node_version": "0.3.3",
            },
        }

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 201

        deployment = db.query(Deployment).filter_by(name="test-versioned").one()
        assert deployment.deploy_args["node_version"] == "0.3.3"

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=22222)
    def test_create_deployment_with_branch_name(self, mock_dispatch, auth_session, db):
        """
        GIVEN deploy_args containing branch_name and repo_owner
        WHEN POST /api/deployments
        THEN the deployment is created and deploy_args include both fields
        """
        payload = {
            "name": "test-branched",
            "deploy_args": {
                "node_count": 10,
                "branch_name": "feat/my-change",
                "repo_owner": "maidsafe",
            },
        }

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 201

        deployment = db.query(Deployment).filter_by(name="test-branched").one()
        assert deployment.deploy_args["branch_name"] == "feat/my-change"
        assert deployment.deploy_args["repo_owner"] == "maidsafe"

    def test_create_deployment_rejects_node_version_and_branch_name(self, auth_session):
        """
        GIVEN deploy_args containing both node_version and branch_name
        WHEN POST /api/deployments
        THEN the response is 400 with a mutual exclusivity error
        """
        payload = {
            "name": "test-conflict",
            "deploy_args": {
                "node_count": 10,
                "node_version": "0.3.3",
                "branch_name": "feat/conflict",
            },
        }

        response = auth_session.post("/api/deployments", json=payload)

        assert response.status_code == 400
        data = response.get_json()
        assert "mutually exclusive" in data["error"]

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


class TestDestroyDeployment:
    """Tests for POST /api/deployments/destroy."""

    def _create_deployment(self, db, name, status=DeploymentStatus.RUNNING):
        """Helper to insert a deployment directly in the DB."""
        deployment = Deployment(
            name=name,
            deploy_args={"node_count": 10},
            workflow_run_id=11111,
            status=status,
        )
        db.add(deployment)
        db.commit()
        return deployment

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=99999)
    def test_destroy_deployment_dispatches_workflow_and_updates_status(
        self, mock_dispatch, auth_session, db
    ):
        """
        GIVEN a deployment exists in RUNNING state
        WHEN POST /api/deployments/destroy with {"name": "..."}
        THEN dispatch_workflow is called with the destroy workflow
        AND the deployment status becomes "destroying"
        AND a "destroy_triggered" event is recorded
        AND the workflow_run_id is updated
        """
        self._create_deployment(db, "destroy-test-1")

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "destroy-test-1"}
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["name"] == "destroy-test-1"
        assert data["status"] == "destroying"
        assert data["workflow_run_id"] == 99999

        mock_dispatch.assert_called_once_with(
            pat="fake-pat-for-testing",
            repo="test-owner/test-repo",
            workflow_file="destroy-testnet.yml",
            inputs={"name": "destroy-test-1"},
        )

        deployment = db.query(Deployment).filter_by(name="destroy-test-1").one()
        assert deployment.status == DeploymentStatus.DESTROYING
        assert deployment.workflow_run_id == 99999

        event = (
            db.query(DeploymentEvent)
            .filter_by(deployment_id=deployment.id, event_type="destroy_triggered")
            .one()
        )
        assert event.details["workflow_run_id"] == 99999
        assert event.details["previous_status"] == "running"

    @patch("saorsa_web.api.deployments.dispatch_workflow", return_value=88888)
    def test_destroy_deployment_works_for_stopped_status(
        self, mock_dispatch, auth_session, db
    ):
        """
        GIVEN a deployment exists in STOPPED state
        WHEN POST /api/deployments/destroy
        THEN the destroy succeeds
        """
        self._create_deployment(db, "destroy-stopped", status=DeploymentStatus.STOPPED)

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "destroy-stopped"}
        )

        assert response.status_code == 200
        assert response.get_json()["status"] == "destroying"

    def test_destroy_deployment_returns_404_when_not_found(self, auth_session):
        """
        GIVEN no deployment exists with the given name
        WHEN POST /api/deployments/destroy
        THEN the response is 404
        """
        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "nonexistent"}
        )

        assert response.status_code == 404
        assert "not found" in response.get_json()["error"].lower()

    def test_destroy_deployment_returns_409_for_pending(self, auth_session, db):
        """
        GIVEN a deployment exists in PENDING state
        WHEN POST /api/deployments/destroy
        THEN the response is 409 (not destroyable)
        """
        self._create_deployment(db, "destroy-pending", status=DeploymentStatus.PENDING)

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "destroy-pending"}
        )

        assert response.status_code == 409
        assert "cannot be destroyed" in response.get_json()["error"].lower()

    def test_destroy_deployment_returns_409_for_destroying(self, auth_session, db):
        """
        GIVEN a deployment is already DESTROYING
        WHEN POST /api/deployments/destroy
        THEN the response is 409
        """
        self._create_deployment(
            db, "destroy-in-progress", status=DeploymentStatus.DESTROYING
        )

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "destroy-in-progress"}
        )

        assert response.status_code == 409

    def test_destroy_deployment_returns_409_for_destroyed(self, auth_session, db):
        """
        GIVEN a deployment is already DESTROYED
        WHEN POST /api/deployments/destroy
        THEN the response is 409
        """
        self._create_deployment(
            db, "already-destroyed", status=DeploymentStatus.DESTROYED
        )

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "already-destroyed"}
        )

        assert response.status_code == 409

    def test_destroy_deployment_validates_name_required(self, auth_session):
        """
        GIVEN a request with missing name
        WHEN POST /api/deployments/destroy
        THEN the response is 400
        """
        response = auth_session.post(
            "/api/deployments/destroy", json={"other": "field"}
        )

        assert response.status_code == 400
        assert "name" in response.get_json()["error"].lower()

    def test_destroy_deployment_returns_401_without_auth(self, client):
        """
        GIVEN no authentication
        WHEN POST /api/deployments/destroy
        THEN the response is 401
        """
        response = client.post(
            "/api/deployments/destroy",
            json={"name": "no-auth-destroy"},
            headers={"Accept": "application/json"},
        )

        assert response.status_code == 401

    @patch("saorsa_web.api.deployments.dispatch_workflow")
    def test_destroy_deployment_returns_502_when_dispatch_fails(
        self, mock_dispatch, auth_session, db
    ):
        """
        GIVEN a valid deployment
        WHEN POST /api/deployments/destroy and dispatch_workflow raises
        THEN the response is 502 and the deployment status is unchanged
        """
        mock_dispatch.side_effect = GitHubAPIError("dispatch failed")
        self._create_deployment(db, "destroy-dispatch-fail")

        response = auth_session.post(
            "/api/deployments/destroy", json={"name": "destroy-dispatch-fail"}
        )

        assert response.status_code == 502

        deployment = db.query(Deployment).filter_by(name="destroy-dispatch-fail").one()
        assert deployment.status == DeploymentStatus.RUNNING
