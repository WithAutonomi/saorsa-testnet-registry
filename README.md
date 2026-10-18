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

All four workspace packages (`saorsa-db`, `saorsa-deploy`, `saorsa-web`,
`saorsa-deploy-mcp`) share a single version number (SemVer), and the release is
made by CI, not by hand. antops runs a `saorsa-deploy` **wheel pinned by
version** in their Linear issue, fetched from a GitHub release — so a change to
the tool that does not move the version is a change no run can ask for.

The flow:

1. **The PR declares the bump.** Its body carries one line that CI parses:

   ```
   Semantic version: breaking | feature | fix | none
   ```

   `breaking` for a change under `deploy/` or `db/` that a manifest or caller
   must adapt to, `feature` for a new command, flag, manifest key or model
   field, `fix` for any other change to the tool, and `none` for a PR that
   changes neither tree (documentation, workflows, the web app). Documentation
   and run manifests inside those trees — `deploy/README.md`,
   `deploy/manifests/`, `db/README.md` — are not a change to the tool.

2. **The author sets the version**, before committing:

   ```bash
   uv run scripts/bump_version.py 1.1.0 --no-commit
   ```

   This edits the four `pyproject.toml` files and `uv.lock` and stops; commit
   them with the rest of the change.

3. **CI checks it.** The `version-bump` job
   (`.github/workflows/version-check.yml`, logic in
   `scripts/check_version_bump.py`) fails the PR unless the four versions agree
   and the head version is exactly the base version bumped the declared way. It
   is a required status check on `main`, so a tool change cannot be merged
   without one. A failing check is fixed by re-bumping, never by editing the
   check.

4. **CI releases on merge.** `.github/workflows/release.yml` runs on every push
   to `main`: if tag `v<version>` does not exist it builds both wheels, creates
   the tag on the merged commit, and publishes a GitHub release titled
   `v<version>` listing the merged PRs, with
   `saorsa_db-<version>-py3-none-any.whl` and
   `saorsa_deploy-<version>-py3-none-any.whl` attached. A merge that declared
   `none` leaves the version alone, so the tag already exists and the job does
   nothing.

Fetching a released version — the exact commands an antops run uses:

```bash
gh release list
gh release download v1.0.0 -p '*.whl' -D /tmp/wheels
uvx --no-sources --find-links /tmp/wheels saorsa-deploy==1.0.0 --version
```

Running `scripts/bump_version.py` without `--no-commit` still commits, tags and
pushes in one go. That path is for an emergency release when CI cannot make
one; it pushes the same `v<version>` tag the release workflow would create.

Pushing a `v*` tag **by hand** also triggers the `deploy.yml` GitHub Action,
which deploys the web app to Digital Ocean App Platform. A tag created by the
release workflow does not: GitHub does not run workflows on events raised with
the built-in `GITHUB_TOKEN`, which is what stops a release from re-triggering
itself. The App Platform deployment is therefore still a by-hand tag push, as
it was before the release workflow existed.

### Prerequisites

A `ANT_DEPLOY_DO_APP_TOKEN` secret must be configured in the GitHub repository settings. Generate a Personal Access Token from the [DigitalOcean API settings](https://cloud.digitalocean.com/account/api/tokens) with the `app:update` scope (under Apps).
