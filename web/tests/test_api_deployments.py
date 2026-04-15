"""Tests for the deployments API (read-only)."""

import pytest

from saorsa_db.db import Base
from saorsa_db.models import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_web.app import create_app
from saorsa_web.db import db_session, session_factory


@pytest.fixture()
def app():
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test-secret",
        }
    )

    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory.configure(bind=engine)

    yield application

    db_session.remove()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sample_deployment(app):
    """Create a sample deployment in the database."""
    with app.app_context():
        deployment = Deployment(
            name="TEST-01",
            status=DeploymentStatus.RUNNING,
            deploy_args={"vm_count": 2, "node_count": 10},
        )
        db_session.add(deployment)
        db_session.flush()

        event = DeploymentEvent(
            deployment_id=deployment.id,
            event_type="created",
            details={"deploy_args": deployment.deploy_args},
        )
        db_session.add(event)
        db_session.commit()

        return deployment.id


class TestListDeployments:
    def test_empty_list(self, client):
        resp = client.get("/api/deployments")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_deployments(self, client, sample_deployment):
        resp = client.get("/api/deployments")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["name"] == "TEST-01"
        assert data[0]["status"] == "running"


class TestGetDeployment:
    def test_not_found(self, client):
        resp = client.get("/api/deployments/999")
        assert resp.status_code == 404

    def test_returns_deployment_with_events(self, client, sample_deployment):
        resp = client.get(f"/api/deployments/{sample_deployment}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "TEST-01"
        assert len(data["events"]) == 1
        assert data["events"][0]["event_type"] == "created"


class TestListEvents:
    def test_not_found(self, client):
        resp = client.get("/api/deployments/999/events")
        assert resp.status_code == 404

    def test_returns_events(self, client, sample_deployment):
        resp = client.get(f"/api/deployments/{sample_deployment}/events")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["event_type"] == "created"
