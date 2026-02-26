"""GitHub OAuth authentication for the web interface."""

import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from saorsa_web.db import db_session
from saorsa_web.models.api_token import ApiToken

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

oauth = OAuth()

REQUIRED_ORG = "maidsafe"


def configure_oauth(app):
    """Configure OAuth with the Flask app.

    Must be called during app initialization.
    """
    oauth.init_app(app)
    oauth.register(
        name="github",
        client_id=app.config.get("GITHUB_CLIENT_ID"),
        client_secret=app.config.get("GITHUB_CLIENT_SECRET"),
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user read:org"},
    )


def get_current_user():
    """Get the current authenticated user from session.

    Returns:
        dict with 'username' key if authenticated, None otherwise.
    """
    if "user" in session:
        return {"username": session["user"]}
    return None


def require_auth(f):
    """Decorator to require web authentication via session.

    Redirects to login if not authenticated.
    Sets g.current_user with the GitHub username.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if user is None:
            session["next"] = request.url
            return redirect(url_for("auth.login"))
        g.current_user = user["username"]
        return f(*args, **kwargs)

    return decorated_function


def require_api_token(f):
    """Decorator to require API token authentication.

    Expects Bearer token in Authorization header.
    Sets g.current_user with the GitHub username from the token.
    Returns 401 if not authenticated.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header[7:]
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        api_token = (
            db_session.query(ApiToken)
            .filter(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked_at.is_(None),
            )
            .first()
        )

        if not api_token:
            return jsonify({"error": "Invalid or revoked token"}), 401

        api_token.last_used_at = datetime.now(timezone.utc)
        db_session.commit()

        g.current_user = api_token.user
        return f(*args, **kwargs)

    return decorated_function


def require_auth_flexible(f):
    """Decorator that accepts either session auth or API token.

    Useful for endpoints that should work in both web and API contexts.
    Sets g.current_user with the GitHub username.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # First try session auth
        user = get_current_user()
        if user:
            g.current_user = user["username"]
            return f(*args, **kwargs)

        # Then try API token
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            token_hash = hashlib.sha256(token.encode()).hexdigest()

            api_token = (
                db_session.query(ApiToken)
                .filter(
                    ApiToken.token_hash == token_hash,
                    ApiToken.revoked_at.is_(None),
                )
                .first()
            )

            if api_token:
                api_token.last_used_at = datetime.now(timezone.utc)
                db_session.commit()
                g.current_user = api_token.user
                return f(*args, **kwargs)

        # Neither worked
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"error": "Authentication required"}), 401

        session["next"] = request.url
        return redirect(url_for("auth.login"))

    return decorated_function


# --- Routes ---


@auth_bp.route("/login")
def login():
    """Initiate GitHub OAuth flow."""
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.github.authorize_redirect(redirect_uri)


@auth_bp.route("/callback")
def callback():
    """Handle GitHub OAuth callback."""
    try:
        token = oauth.github.authorize_access_token()
    except Exception as e:
        current_app.logger.error(f"OAuth error: {e}")
        return render_template(
            "auth/error.html", error="Failed to authenticate with GitHub"
        ), 400

    resp = oauth.github.get("user", token=token)
    user_info = resp.json()
    username = user_info.get("login")

    if not username:
        return render_template(
            "auth/error.html", error="Could not get username from GitHub"
        ), 400

    # Check organization membership
    access_token = token.get("access_token")
    headers = {"Authorization": f"Bearer {access_token}"}

    org_check = requests.get(
        "https://api.github.com/user/orgs",
        headers=headers,
    )

    if org_check.status_code == 200:
        orgs = org_check.json()
        org_logins = [org.get("login", "").lower() for org in orgs]

        if REQUIRED_ORG.lower() in org_logins:
            session["user"] = username
            session.permanent = True

            next_url = session.pop("next", None)
            return redirect(next_url or url_for("web.web_deployments.list_deployments"))
        else:
            return render_template(
                "auth/error.html",
                error=f"Access denied. You must be a member of the {REQUIRED_ORG} organization.",
            ), 403
    else:
        current_app.logger.error(
            f"Org check failed: {org_check.status_code} - {org_check.text}"
        )
        return render_template(
            "auth/error.html", error="Failed to verify organization membership"
        ), 500


@auth_bp.route("/logout")
def logout():
    """Clear the session and log out."""
    session.clear()
    return redirect(url_for("web.web_deployments.list_deployments"))


# --- Token management routes ---


@auth_bp.route("/tokens")
@require_auth
def tokens_page():
    """Render the token management page."""
    user_tokens = (
        db_session.query(ApiToken)
        .filter(
            ApiToken.user == g.current_user,
            ApiToken.revoked_at.is_(None),
        )
        .order_by(ApiToken.created_at.desc())
        .all()
    )

    return render_template("auth/tokens.html", tokens=user_tokens, user=g.current_user)


@auth_bp.route("/tokens", methods=["POST"])
@require_auth
def create_token():
    """Create a new API token.

    Returns the token value only once — it cannot be retrieved again.
    """
    if request.is_json:
        name = request.json.get("name")
    else:
        name = request.form.get("name")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    api_token = ApiToken(
        user=g.current_user,
        token_hash=token_hash,
        name=name,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_token)
    db_session.commit()

    if request.is_json:
        return jsonify(
            {
                "id": api_token.id,
                "token": raw_token,
                "name": api_token.name,
                "created_at": api_token.created_at.isoformat(),
                "message": "Store this token securely - it cannot be retrieved again.",
            }
        ), 201

    return render_template(
        "auth/token_created.html",
        token=raw_token,
        token_record=api_token,
        user=g.current_user,
    )


@auth_bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
@require_auth
def revoke_token(token_id):
    """Revoke an API token."""
    api_token = (
        db_session.query(ApiToken)
        .filter(
            ApiToken.id == token_id,
            ApiToken.user == g.current_user,
            ApiToken.revoked_at.is_(None),
        )
        .first()
    )

    if not api_token:
        return jsonify({"error": "Token not found"}), 404

    api_token.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({"message": "Token revoked"}), 200

    return redirect(url_for("auth.tokens_page"))


@auth_bp.route("/api/tokens")
@require_auth
def list_tokens():
    """List all active tokens for the current user."""
    tokens = (
        db_session.query(ApiToken)
        .filter(
            ApiToken.user == g.current_user,
            ApiToken.revoked_at.is_(None),
        )
        .order_by(ApiToken.created_at.desc())
        .all()
    )

    return jsonify(
        {
            "tokens": [
                {
                    "id": t.id,
                    "name": t.name,
                    "created_at": t.created_at.isoformat(),
                    "last_used_at": t.last_used_at.isoformat()
                    if t.last_used_at
                    else None,
                }
                for t in tokens
            ]
        }
    )
