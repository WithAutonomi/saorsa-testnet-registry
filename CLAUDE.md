# Saorsa Testnet Registry

## Architecture

A system for managing testnet deployments, structured as a uv workspace with two packages:

- **`web/`** (`saorsa-web`) - Flask app serving a REST API (`/api/...`) and web UI (`/deployments`) with Jinja2 + HTMX, plus a background monitor worker
- **`mcp/`** (`saorsa-deploy-mcp`) - MCP server providing tools for Claude to interact with the registry via HTTP
- **GitHub Actions** - Workflow dispatch for launching testnets (`.github/workflows/launch-testnet.yml`)
- **Database** - PostgreSQL, managed via Alembic migrations (inside `web/`)
- **Deployment** - Digital Ocean App Platform (`app.yaml`)

## Project Structure

```
pyproject.toml                  # workspace root (not a package)
app.yaml                        # DO App Platform spec
web/
  pyproject.toml
  wsgi.py                       # gunicorn entry point
  alembic.ini
  alembic/                      # database migrations
  templates/                    # Jinja2 templates
  saorsa_web/
    app.py                      # Flask app factory
    config.py                   # env var configuration
    db.py                       # SQLAlchemy engine/session
    models/deployment.py        # Deployment model
    models/api_token.py         # ApiToken model (auth)
    api/deployments.py          # REST API endpoints
    web/deployments.py          # Web UI views
    web/auth.py                 # GitHub OAuth + token management
    monitor.py                  # Background status monitor (stubbed)
    cli.py                      # CLI for monitor worker
mcp/
  pyproject.toml
  saorsa_deploy_mcp/
    server.py                   # MCP server with deployment tools
```

## Authentication

The web UI and API are protected by GitHub OAuth, restricted to members of the `maidsafe` GitHub organisation.

- **Web UI**: Session-based auth via GitHub OAuth. Users are redirected to `/auth/login` if unauthenticated.
- **API**: Bearer token auth. Users generate tokens from the web UI at `/auth/tokens`. Tokens are SHA-256 hashed in the database; the raw value is shown only once on creation.
- **MCP server**: Uses a `REGISTRY_API_TOKEN` env var to send `Authorization: Bearer <token>` on all API calls.
- **Auth module**: `web/saorsa_web/web/auth.py` — OAuth config, decorators (`require_auth`, `require_api_token`, `require_auth_flexible`), login/callback/logout, token CRUD.
- **Config**: `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` env vars (from a GitHub OAuth App).

### Auth decorators

- `@require_auth` — web routes, redirects to login if no session
- `@require_api_token` — API routes, expects `Authorization: Bearer <token>`, returns 401 otherwise
- `@require_auth_flexible` — accepts either session or Bearer token (used on all `/api/` endpoints)

## Development

```bash
# Install all workspace packages
uv sync --all-packages

# Set up environment
cp .env.example .env
# Edit .env with your local Postgres connection and GitHub OAuth credentials

# Run migrations (from web/ directory)
cd web && uv run alembic upgrade head

# Start dev server
uv run flask --app saorsa_web.app run

# Run the MCP server (stdio transport)
uv run python -m saorsa_deploy_mcp.server

# Run tests
uv run pytest

# Lint
uv run ruff check .
```

## Deploying Testnets

When asked to deploy, launch, or create a testnet, **use the `saorsa-testnet-registry` MCP tools** — do not search the codebase. The MCP server provides:

- `create_deployment` — triggers a deployment workflow (accepts name, vm_count, node_count, genesis_port, etc.)
- `track_deployment` — checks deployment progress by ID
- `testnet://docs/deployment-config` — resource with full parameter documentation

## Key Commands

- `uv run flask --app saorsa_web.app run` - Dev server
- `cd web && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd web && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
- `uv run scripts/bump_version.py <version>` - Bump version, commit, tag, and push (triggers DO deployment)
