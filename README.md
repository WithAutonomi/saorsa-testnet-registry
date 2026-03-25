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

The MCP server is a separate workspace package that communicates with the registry over HTTP. It requires an API token for authentication. To run it standalone:

```bash
REGISTRY_API_TOKEN=your-token-here uv run python -m saorsa_deploy_mcp.server
```

To obtain a token:

1. Log in to the web UI via GitHub OAuth
2. Navigate to `/auth/tokens`
3. Create a new token and copy the value (it is only shown once)

#### Claude Code Configuration

Add the following to `~/.claude.json` to make the MCP tools available in Claude Code:

```json
{
  "mcpServers": {
    "saorsa-testnet-registry": {
      "command": "/home/chris/.local/bin/uv",
      "args": [
        "run",
        "python",
        "-m",
        "saorsa_deploy_mcp.server"
      ],
      "cwd": "/home/chris/dev/github.com/jacderida/saorsa-testnet-registry",
      "env": {
        "REGISTRY_URL": "http://localhost:5000",
        "REGISTRY_API_TOKEN": "your-token-here",
        "VIRTUAL_ENV": "/home/chris/dev/github.com/jacderida/saorsa-testnet-registry/.venv"
      }
    }
  }
}
```

Replace the various paths above with pointers to your own binaries and virtualenv locations.

| Variable | Description |
|----------|-------------|
| `REGISTRY_URL` | URL of the web app (defaults to `http://localhost:5000`) |
| `REGISTRY_API_TOKEN` | API token generated from the web UI at `/auth/tokens` |

#### Available MCP Tools

| Tool | Description |
|------|-------------|
| `create_deployment` | Create a new testnet and trigger the launch workflow |
| `destroy_deployment` | Destroy a testnet and tear down its infrastructure |
| `list_deployments` | List all testnet deployments |
| `get_deployment` | Get details of a specific deployment including events |
| `track_deployment` | Track deployment progress (poll for status updates) |

Example usage in Claude Code:

- "Deploy a testnet called DEV-01 with 3 VMs, 25 nodes, genesis port 12000"
- "Destroy the DEV-01 testnet"
- "Check the status of deployment 5"
- "List all deployments"

### Linting

```bash
uv run ruff check .
```

## Releasing

Both workspace packages share a single version number (SemVer). To cut a new release:

```bash
uv run scripts/bump_version.py 0.2.0
```

This script:

1. Updates the version in both `web/pyproject.toml` and `mcp/pyproject.toml`
2. Creates a git commit (`chore: bump version to 0.2.0`)
3. Creates a git tag (`v0.2.0`)
4. Pushes the commit and tag to the remote

Pushing the tag triggers the `deploy.yml` GitHub Action, which deploys the app to Digital Ocean App Platform.

### Prerequisites

A `ANT_DEPLOY_DO_APP_TOKEN` secret must be configured in the GitHub repository settings. Generate a Personal Access Token from the [DigitalOcean API settings](https://cloud.digitalocean.com/account/api/tokens) with the `app:update` scope (under Apps).
