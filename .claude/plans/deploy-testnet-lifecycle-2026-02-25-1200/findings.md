# Findings: deploy-testnet-lifecycle

## Requirements
- MCP tool to trigger deployment via natural language ("Deploy a DEV-01 testnet with config...")
- MCP `create_deployment` calls web API `POST /api/deployments`
- API endpoint: dispatch GH workflow (fire-and-forget), record deployment + event in DB, return workflow info
- GH workflow uses `saorsa-deploy` tool: `infra`, `provision-genesis`, `provision` commands
- Background monitor polls deployment status, updates DB
- New MCP `track_deployment` tool for polling status
- Deployment events recorded in DB (created, status changes, completed)
- `deploy_args` workflow input to hold all deployment arguments (JSON)

## Codebase State (as explored)

### Current Deployment Model
- Fields: id, name, status (enum: pending/running/stopped/failed), created_at, updated_at
- Single table `deployments`, one migration

### Current API (`POST /api/deployments`)
- Accepts: `{ name, status? }` — very simple
- No workflow dispatch, no events

### Current MCP Tools
- `create_deployment(name, status="pending")` — POST to registry
- `list_deployments()` — GET all
- `get_deployment(deployment_id)` — GET one
- All use async httpx, return formatted strings

### Monitor
- Stubbed: infinite loop with sleep, TODO comment

### GitHub Actions Workflow
- Stubbed: echoes inputs, TODO comments
- Current inputs: name, node_count, node_version

### Config
- `WORKFLOW_RUNNER_PAT` already defined (empty default) — for GH API calls
- `DATABASE_URL` for PostgreSQL

### Tests
- **None exist** — starting from scratch

### Key Patterns
- Blueprint architecture (api_bp, web_bp)
- Module-level `db_session` (scoped_session)
- Enum for status, serialized as `.value`
- MCP returns formatted strings (no exceptions)
- Templates in `web/templates/`, HTMX for polling

## saorsa-deploy Tool Arguments

### `infra` command
| Argument | Required | Default | Notes |
|----------|----------|---------|-------|
| `--name` | Yes | - | Deployment name (e.g., DEV-01) |
| `--vm-count` | Yes | - | VMs per provider per region |
| `--attached-volume-size` | No | 20 | Volume size in GB |
| `--region-counts` | No | 3 | Comma-separated region counts |
| `--testnet` | No (flag) | false | DO only, lon1 region |

### `provision-genesis` command
| Argument | Required | Default | Notes |
|----------|----------|---------|-------|
| `--name` | Yes | - | Must match infra name |
| `--port` | Yes | - | Genesis node port |
| `--ip-version` | No | - | v4 or v6 |
| `--log-level` | No | - | Node log level |
| `--ssh-key-path` | No | ~/.ssh/id_rsa | SSH key path |
| `--testnet` | No (flag) | false | Run with --testnet |

### `provision` command
| Argument | Required | Default | Notes |
|----------|----------|---------|-------|
| `--name` | Yes | - | Must match infra name |
| `--node-count` | Yes | - | Node services per VM |
| `--ip-version` | No | - | v4 or v6 |
| `--log-level` | No | - | Node log level |
| `--port` | No | - | Port range start |
| `--region` | No | - | Provision specific region |
| `--ssh-key-path` | No | ~/.ssh/id_rsa | SSH key path |
| `--testnet` | No (flag) | false | Run with --testnet |

## Secrets Required in GH Actions
| Env Var | Secret Name |
|---------|-------------|
| `DO_TOKEN` | `SAORSA_DEPLOY_DO_TOKEN` |
| `AWS_ACCESS_KEY_ID` | `SAORSA_DEPLOY_AWS_ACCESS_KEY_ID` |
| `AWS_SECRET_ACCESS_KEY_ID` | `SAORSA_DEPLOY_AWS_SECRET_ACCESS_KEY_ID` |
| SSH key → `~/.ssh/id_rsa` | `SAORSA_DEPLOY_SSH_PRIVATE_KEY` |

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| `deploy_args` as JSON string | Flexible, avoids GH workflow input limits, easy to parse |
| GitHub API for workflow dispatch | `WORKFLOW_RUNNER_PAT` already configured; fire-and-forget |
| DeploymentEvent as separate table | Clean audit trail, multiple events per deployment |
| Monitor polls GH Actions API | Check workflow run status to determine deployment state |

## Resources
- saorsa-deploy repo: https://github.com/jacderida/saorsa-deploy
- GH Actions API: `POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches`
- GH Workflow Runs API: `GET /repos/{owner}/{repo}/actions/runs`

---
*Updated during Phase 2 planning*
