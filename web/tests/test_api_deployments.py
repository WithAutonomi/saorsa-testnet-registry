"""Tests for the deployments API validation logic."""

from unittest.mock import patch

import pytest

from saorsa_web.app import create_app
from saorsa_web.db import Base, db_session, session_factory


@pytest.fixture()
def app():
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test-secret",
            "GITHUB_CLIENT_ID": "fake",
            "GITHUB_CLIENT_SECRET": "fake",
            "WORKFLOW_RUNNER_PAT": "fake",
            "GITHUB_REPO": "owner/repo",
            "WORKFLOW_FILE": "launch-testnet.yml",
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


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Bypass auth by making get_current_user return a fake user."""
    with patch(
        "saorsa_web.web.auth.get_current_user",
        return_value={"username": "test-user"},
    ):
        yield


class TestUpgradeChannelValidation:
    def test_rejects_invalid_upgrade_channel(self, client):
        resp = client.post(
            "/api/deployments",
            json={
                "name": "TEST-01",
                "deploy_args": {"upgrade_channel": "invalid"},
            },
        )
        assert resp.status_code == 400
        assert "upgrade_channel" in resp.get_json()["error"]

    def test_accepts_beta_upgrade_channel(self, client):
        with patch("saorsa_web.api.deployments.dispatch_workflow", return_value=123):
            resp = client.post(
                "/api/deployments",
                json={
                    "name": "TEST-01",
                    "deploy_args": {"upgrade_channel": "beta"},
                },
            )
        assert resp.status_code == 201

    def test_accepts_stable_upgrade_channel(self, client):
        with patch("saorsa_web.api.deployments.dispatch_workflow", return_value=456):
            resp = client.post(
                "/api/deployments",
                json={
                    "name": "TEST-02",
                    "deploy_args": {"upgrade_channel": "stable"},
                },
            )
        assert resp.status_code == 201

    def test_accepts_no_upgrade_channel(self, client):
        with patch("saorsa_web.api.deployments.dispatch_workflow", return_value=789):
            resp = client.post(
                "/api/deployments",
                json={
                    "name": "TEST-03",
                    "deploy_args": {},
                },
            )
        assert resp.status_code == 201

    def test_rejects_empty_string_upgrade_channel(self, client):
        resp = client.post(
            "/api/deployments",
            json={
                "name": "TEST-04",
                "deploy_args": {"upgrade_channel": ""},
            },
        )
        assert resp.status_code == 400
        assert "upgrade_channel" in resp.get_json()["error"]
