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
    api/deployments.py          # REST API endpoints
    web/deployments.py          # Web UI views
    monitor.py                  # Background status monitor (stubbed)
    cli.py                      # CLI for monitor worker
mcp/
  pyproject.toml
  saorsa_deploy_mcp/
    server.py                   # MCP server with deployment tools
```

## Development

```bash
# Install all workspace packages
uv sync --all-packages

# Set up environment
cp .env.example .env
# Edit .env with your local Postgres connection

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

## Key Commands

- `uv run flask --app saorsa_web.app run` - Dev server
- `cd web && uv run alembic revision --autogenerate -m "description"` - New migration
- `cd web && uv run alembic upgrade head` - Apply migrations
- `uv run ruff check . --fix` - Lint and fix
