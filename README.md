# Saorsa Testnet Registry

A system for managing testnet deployments for a peer-to-peer network. Provides a web interface, REST API, and MCP server for Claude integration, backed by PostgreSQL and deployed on Digital Ocean's App Platform.

## Development

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL

### Database Setup

Create a dedicated Postgres user and database:

```bash
psql -U postgres -c "CREATE ROLE saorsa_testnet WITH LOGIN PASSWORD 'saorsa_testnet';"
psql -U postgres -c "CREATE DATABASE saorsa_testnet_registry OWNER saorsa_testnet;"
```

Verify the connection:

```bash
psql -U saorsa_testnet -d saorsa_testnet_registry -c "SELECT 1"
```

### Environment

Copy the example env file and adjust if needed:

```bash
cp .env.example .env
```

The defaults connect to the local database created above. The `.env` file is loaded by both the web app and Alembic.

### Install Dependencies

```bash
uv sync --all-packages
```

This installs both workspace packages (`saorsa-web` and `saorsa-deploy-mcp`) into a shared virtualenv.

### Run Migrations

```bash
cd web && uv run alembic upgrade head
```

### Start the Dev Server

```bash
uv run flask --app saorsa_web.app run
```

The web UI is at `http://localhost:5000/deployments` and the API at `http://localhost:5000/api/deployments`.

### MCP Server

The MCP server is a separate workspace package that communicates with the registry over HTTP. To run it:

```bash
uv run python -m saorsa_deploy_mcp.server
```

### Linting

```bash
uv run ruff check .
```
