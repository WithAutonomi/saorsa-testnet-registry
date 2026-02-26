"""Tests for GitHub OAuth authentication and API token management."""

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from saorsa_web.models.api_token import ApiToken


class TestRequireAuth:
    """Tests for the require_auth decorator on web routes."""

    def test_web_route_redirects_to_login_without_session(self, client):
        """
        GIVEN no authenticated session
        WHEN GET /deployments
        THEN the response redirects to /auth/login
        """
        response = client.get("/deployments")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_web_route_accessible_with_session(self, auth_session):
        """
        GIVEN an authenticated session
        WHEN GET /deployments
        THEN the response is 200
        """
        response = auth_session.get("/deployments")
        assert response.status_code == 200


class TestRequireAuthFlexible:
    """Tests for the require_auth_flexible decorator on API routes."""

    def test_api_returns_401_without_auth_json(self, client):
        """
        GIVEN no authentication and JSON Accept header
        WHEN GET /api/deployments
        THEN the response is 401
        """
        response = client.get(
            "/api/deployments",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 401
        data = response.get_json()
        assert "error" in data

    def test_api_accessible_with_session(self, auth_session):
        """
        GIVEN an authenticated session
        WHEN GET /api/deployments
        THEN the response is 200
        """
        response = auth_session.get("/api/deployments")
        assert response.status_code == 200

    def test_api_accessible_with_bearer_token(self, client, auth_header):
        """
        GIVEN a valid Bearer token
        WHEN GET /api/deployments
        THEN the response is 200
        """
        response = client.get("/api/deployments", headers=auth_header)
        assert response.status_code == 200

    def test_api_returns_401_with_invalid_token(self, client):
        """
        GIVEN an invalid Bearer token
        WHEN GET /api/deployments
        THEN the response is 401
        """
        response = client.get(
            "/api/deployments",
            headers={
                "Authorization": "Bearer invalid-token",
                "Accept": "application/json",
            },
        )
        assert response.status_code == 401

    def test_api_returns_401_with_revoked_token(self, client, api_token, db):
        """
        GIVEN a revoked Bearer token
        WHEN GET /api/deployments
        THEN the response is 401
        """
        raw_token, token_record = api_token
        token_record.revoked_at = datetime.now(timezone.utc)
        db.commit()

        response = client.get(
            "/api/deployments",
            headers={
                "Authorization": f"Bearer {raw_token}",
                "Accept": "application/json",
            },
        )
        assert response.status_code == 401

    def test_api_token_updates_last_used_at(self, client, api_token, db):
        """
        GIVEN a valid Bearer token with no last_used_at
        WHEN GET /api/deployments
        THEN last_used_at is updated
        """
        raw_token, token_record = api_token
        assert token_record.last_used_at is None

        client.get("/api/deployments", headers={"Authorization": f"Bearer {raw_token}"})

        db.expire_all()
        updated = db.get(ApiToken, token_record.id)
        assert updated.last_used_at is not None


class TestTokenManagement:
    """Tests for token CRUD endpoints."""

    def test_create_token_returns_raw_token(self, auth_session, db):
        """
        GIVEN an authenticated session
        WHEN POST /auth/tokens with JSON
        THEN the response contains the raw token (only shown once)
        AND the token is persisted as a hash in the database
        """
        response = auth_session.post(
            "/auth/tokens",
            json={"name": "my-test-token"},
        )
        assert response.status_code == 201
        data = response.get_json()
        assert "token" in data
        assert data["name"] == "my-test-token"
        assert "Store this token" in data["message"]

        # Verify the hash is stored, not the raw token
        token_hash = hashlib.sha256(data["token"].encode()).hexdigest()
        stored = db.query(ApiToken).filter_by(token_hash=token_hash).one()
        assert stored.user == "testuser"
        assert stored.name == "my-test-token"

    def test_create_token_without_name(self, auth_session, db):
        """
        GIVEN an authenticated session
        WHEN POST /auth/tokens without a name
        THEN the token is created with name=None
        """
        response = auth_session.post("/auth/tokens", json={})
        assert response.status_code == 201
        data = response.get_json()
        assert data["name"] is None

    def test_list_tokens_returns_active_tokens(self, auth_session, api_token):
        """
        GIVEN an authenticated session and an existing token
        WHEN GET /auth/api/tokens
        THEN the response lists active tokens without raw values
        """
        response = auth_session.get("/auth/api/tokens")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["tokens"]) >= 1
        # Raw token should never be returned in listing
        for t in data["tokens"]:
            assert "token" not in t
            assert "token_hash" not in t

    def test_revoke_token(self, auth_session, api_token, db):
        """
        GIVEN an authenticated session and an existing token
        WHEN POST /auth/tokens/<id>/revoke
        THEN the token is revoked
        """
        _, token_record = api_token
        token_id = token_record.id
        response = auth_session.post(
            f"/auth/tokens/{token_id}/revoke",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200

        db.expire_all()
        revoked = db.get(ApiToken, token_id)
        assert revoked.revoked_at is not None

    def test_revoke_nonexistent_token_returns_404(self, auth_session):
        """
        GIVEN an authenticated session
        WHEN POST /auth/tokens/99999/revoke
        THEN the response is 404
        """
        response = auth_session.post(
            "/auth/tokens/99999/revoke",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 404

    def test_tokens_page_requires_auth(self, client):
        """
        GIVEN no authenticated session
        WHEN GET /auth/tokens
        THEN redirects to login
        """
        response = client.get("/auth/tokens")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


class TestOAuthCallback:
    """Tests for the OAuth callback with mocked GitHub API."""

    @patch("saorsa_web.web.auth.requests.get")
    @patch("saorsa_web.web.auth.oauth")
    def test_callback_grants_access_for_org_member(
        self, mock_oauth, mock_requests_get, client
    ):
        """
        GIVEN a user who is a member of the maidsafe org
        WHEN the OAuth callback is invoked
        THEN the session is set and user is redirected
        """
        # Mock OAuth token exchange
        mock_oauth.github.authorize_access_token.return_value = {
            "access_token": "fake-access-token",
        }
        user_resp = MagicMock()
        user_resp.json.return_value = {"login": "orgmember"}
        mock_oauth.github.get.return_value = user_resp

        # Mock org membership check
        org_resp = MagicMock()
        org_resp.status_code = 200
        org_resp.json.return_value = [{"login": "maidsafe"}, {"login": "other-org"}]
        mock_requests_get.return_value = org_resp

        response = client.get("/auth/callback")

        assert response.status_code == 302
        assert "/deployments" in response.headers["Location"]

        # Verify session was set
        with client.session_transaction() as sess:
            assert sess["user"] == "orgmember"

    @patch("saorsa_web.web.auth.requests.get")
    @patch("saorsa_web.web.auth.oauth")
    def test_callback_denies_access_for_non_member(
        self, mock_oauth, mock_requests_get, client
    ):
        """
        GIVEN a user who is NOT a member of the maidsafe org
        WHEN the OAuth callback is invoked
        THEN the response is 403
        """
        mock_oauth.github.authorize_access_token.return_value = {
            "access_token": "fake-access-token",
        }
        user_resp = MagicMock()
        user_resp.json.return_value = {"login": "outsider"}
        mock_oauth.github.get.return_value = user_resp

        org_resp = MagicMock()
        org_resp.status_code = 200
        org_resp.json.return_value = [{"login": "some-other-org"}]
        mock_requests_get.return_value = org_resp

        response = client.get("/auth/callback")

        assert response.status_code == 403

    @patch("saorsa_web.web.auth.requests.get")
    @patch("saorsa_web.web.auth.oauth")
    def test_callback_returns_500_on_org_api_failure(
        self, mock_oauth, mock_requests_get, client
    ):
        """
        GIVEN the GitHub orgs API returns an error
        WHEN the OAuth callback is invoked
        THEN the response is 500
        """
        mock_oauth.github.authorize_access_token.return_value = {
            "access_token": "fake-access-token",
        }
        user_resp = MagicMock()
        user_resp.json.return_value = {"login": "someuser"}
        mock_oauth.github.get.return_value = user_resp

        org_resp = MagicMock()
        org_resp.status_code = 403
        org_resp.text = "Forbidden"
        mock_requests_get.return_value = org_resp

        response = client.get("/auth/callback")

        assert response.status_code == 500

    @patch("saorsa_web.web.auth.oauth")
    def test_callback_returns_400_on_oauth_error(self, mock_oauth, client):
        """
        GIVEN the OAuth token exchange fails
        WHEN the OAuth callback is invoked
        THEN the response is 400
        """
        mock_oauth.github.authorize_access_token.side_effect = Exception("OAuth failed")

        response = client.get("/auth/callback")

        assert response.status_code == 400


class TestLogout:
    """Tests for the logout route."""

    def test_logout_clears_session(self, auth_session):
        """
        GIVEN an authenticated session
        WHEN GET /auth/logout
        THEN the session is cleared and user is redirected
        """
        response = auth_session.get("/auth/logout")
        assert response.status_code == 302

        with auth_session.session_transaction() as sess:
            assert "user" not in sess
