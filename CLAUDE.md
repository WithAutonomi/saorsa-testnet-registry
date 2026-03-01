# Saorsa Testnet Registry

## Architecture

A system for managing testnet deployments, structured as a uv workspace with three packages:

- **`web/`** (`saorsa-web`) - Flask app serving a REST API (`/api/...`) and web UI (`/deployments`) with Jinja2 + HTMX, plus a background monitor worker
- **`mcp/`** (`saorsa-deploy-mcp`) - MCP server providing tools for Claude to interact with the registry via HTTP
- **`deploy/`** (`saorsa-deploy`) - CLI tool that provisions testnets via Terraform and Pyinfra
- **GitHub Actions** - Workflow dispatch for launching (`.github/workflows/launch-testnet.yml`) and destroying (`.github/workflows/destroy-testnet.yml`) testnets
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
deploy/
  pyproject.toml
  README.md                     # Command reference, argument tables, architecture
  saorsa_deploy/
    main.py                     # CLI entry point
    binary_source.py            # GitHub release / custom branch binary resolution
    bootstrap.py                # Bootstrap VM (genesis node) setup
    build_droplet.py            # Build VM provisioning
    executor.py                 # Command execution wrapper
    providers.py                # Provider configuration (Digital Ocean)
    resources.py                # Resource management
    ssh.py                      # SSH operations
    state.py                    # Deployment state management (S3)
    terraform.py                # Terraform orchestration
    cmd/                        # CLI subcommands (infra, provision, destroy, etc.)
    provisioning/               # Pyinfra provisioning logic (genesis, node, build)
    resources/                  # Terraform manifests (digitalocean/, aws-build-infra/)
  tests/                        # pytest test suite
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

## Deploying & Destroying Testnets

When asked to deploy, launch, create, or destroy a testnet, **use the `saorsa-testnet-registry` MCP tools** — do not search the codebase. The MCP server provides:

- `create_deployment` — triggers a deployment workflow (accepts name, vm_count, node_count, genesis_port, etc.)
- `destroy_deployment` — triggers a destroy workflow to tear down infrastructure (accepts name)
- `list_deployments` — lists all deployments
- `get_deployment` — gets deployment details including events (accepts deployment_id)
- `track_deployment` — checks deployment progress by ID
- `testnet://docs/deployment-config` — resource with full parameter documentation

### Deployment Statuses

`pending` → `running` → `stopped` (success) or `failed`

When destroying: `running`/`stopped` → `destroying` → `destroyed` (success) or `failed`

### API Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/deployments` | List all deployments |
| GET | `/api/deployments/<id>` | Get deployment with events |
| POST | `/api/deployments` | Create deployment (dispatch launch workflow) |
| POST | `/api/deployments/destroy` | Destroy deployment by name (dispatch destroy workflow) |
| PUT | `/api/deployments/<id>` | Update deployment status |
| DELETE | `/api/deployments/<id>` | Delete deployment record |
| GET | `/api/deployments/<id>/events` | List deployment events |

## Key Commands

- `uv run flask --app saorsa_web.app run` - Dev server
- `cd web && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd web && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
- `uv run scripts/bump_version.py <version>` - Bump version, commit, tag, and push (triggers DO deployment)
