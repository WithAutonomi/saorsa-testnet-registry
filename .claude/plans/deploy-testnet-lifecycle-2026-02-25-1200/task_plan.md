# Task Plan: Deploy Testnet Full Lifecycle

**Created**: 2026-02-25
**Status**: approved
**Directory**: deploy-testnet-lifecycle-2026-02-25-1200

## Goal
Implement end-to-end testnet deployment: MCP tool triggers API, API dispatches GitHub Actions workflow (using saorsa-deploy), background monitor tracks progress and records events in DB, new MCP tool for status tracking.

## Current Phase
Phase 3: Implementation

## Interview Summary
- **Project Type**: Web application (fullstack)
- **Work Type**: New feature from scratch
- **Scope**: Large (API, DB, GH Actions, MCP, monitor, tests)
- **Constraints**: None
- **Patterns**: Follow existing codebase conventions

## Review Gates
- [x] Unit tests pass (`uv run pytest`) — 20/20 passed
- [x] Ruff lint passes (`uv run ruff check .`) — all checks passed
- [ ] Alembic migrations apply cleanly (needs live DB to verify)
- [x] Input validation on API endpoints (name + deploy_args validated)

## Phases

### Phase 1: Requirements & Discovery
- [x] Conduct interview
- [x] Document requirements
- [x] Explore codebase
- **Status:** complete

### Phase 2: Planning & Structure
- [x] Research existing codebase patterns
- [x] Define technical approach
- [x] Break down into tasks
- [ ] Get user approval
- **Status:** in_progress

### Phase 3: Implementation
- [x] Task 1: Database model changes + migration
- [x] Task 2: GitHub Actions workflow
- [x] Task 3: GitHub API integration module
- [x] Task 4: API endpoint enhancement
- [x] Task 5: Background monitor implementation
- [x] Task 6: MCP server updates
- [x] Task 7: Unit tests
- **Status:** complete

### Phase 4: Testing & Verification
- [ ] Run review gates
- [ ] Fix any failures
- [ ] Document results
- **Status:** pending

### Phase 5: Delivery
- [ ] Final review with user
- [ ] Clean up any temporary code
- [ ] Mark plan complete
- **Status:** pending

## Tasks Breakdown

| # | Task | Description | Files | Dependencies |
|---|------|-------------|-------|--------------|
| 1 | DB model changes | Add `DeploymentEvent` model, add `workflow_run_id` + `deploy_args` (JSON) to `Deployment`, new Alembic migration | `models/deployment.py`, `alembic/versions/` | None |
| 2 | GH Actions workflow | Rewrite `launch-testnet.yml`: `deploy_args` JSON input, setup Terraform + Python + saorsa-deploy, SSH key, env vars, run 3 saorsa-deploy commands | `.github/workflows/launch-testnet.yml` | None |
| 3 | GitHub API module | New module for dispatching workflows and checking run status via GH API. Uses `WORKFLOW_RUNNER_PAT`. | `saorsa_web/github.py` | None |
| 4 | API endpoint | Update `POST /api/deployments` to accept `deploy_args`, dispatch workflow (fire-and-forget), record deployment + created event, return workflow info. Add `GET /api/deployments/<id>/events` endpoint. | `api/deployments.py` | 1, 3 |
| 5 | Background monitor | Implement polling: find active deployments, check GH workflow run status, update deployment status, record events on status changes and completion. | `monitor.py` | 1, 3 |
| 6 | MCP server | Update `create_deployment` to accept deploy config args. Add `track_deployment` tool that polls `get_deployment` and returns status info. | `mcp/server.py` | 4 |
| 7 | Unit tests | Tests for MCP tools (mock HTTP), API endpoint (mock workflow dispatch + DB), monitor (mock GH API + DB). BDD-style for API. | `tests/` | 1-6 |

## Detailed Task Descriptions

### Task 1: Database Model Changes

**`Deployment` model additions:**
- `workflow_run_id: int` (NOT NULL) — GitHub Actions workflow run ID
- `deploy_args: JSON` (NOT NULL) — JSON blob of deployment arguments

**Note:** GH dispatch API returns 204 (no run ID). The API will dispatch then poll runs endpoint to get the run ID before creating the DB record.

**New `DeploymentEvent` model:**
- `id: int` (PK)
- `deployment_id: int` (FK → deployments.id)
- `event_type: str` — e.g., "created", "workflow_dispatched", "infra_started", "completed", "failed"
- `details: Optional[JSON]` — extra context (error messages, etc.)
- `created_at: datetime` (UTC)

**Relationship:** `Deployment.events` → list of `DeploymentEvent`

**Migration:** Single Alembic migration adding columns + new table.

### Task 2: GitHub Actions Workflow

**Input:** `deploy_args` (required, JSON string)

**`deploy_args` JSON schema:**
```json
{
  "name": "DEV-01",
  "vm_count": 3,
  "node_count": 25,
  "genesis_port": 12000,
  "testnet": true,
  "attached_volume_size": 20,
  "region_counts": "3",
  "ip_version": "v4",
  "log_level": "info",
  "port": null,
  "region": null
}
```

**Steps:**
1. Parse `deploy_args` JSON
2. Setup Terraform (`hashicorp/setup-terraform`)
3. Setup Python + install `saorsa-deploy` from GitHub
4. Write SSH private key to `~/.ssh/id_rsa`
5. Set env vars from secrets (DO_TOKEN, AWS keys)
6. Run `saorsa-deploy infra --name $NAME --vm-count $VM_COUNT [optional args]`
7. Run `saorsa-deploy provision-genesis --name $NAME --port $GENESIS_PORT [optional args]`
8. Run `saorsa-deploy provision --name $NAME --node-count $NODE_COUNT [optional args]`

### Task 3: GitHub API Module

**`web/saorsa_web/github.py`:**
- `dispatch_workflow(name, deploy_args)` — POST to GH API to trigger workflow_dispatch
- `get_workflow_runs(name)` — GET recent workflow runs, filter by deployment name
- `get_workflow_run(run_id)` — GET specific run status
- Uses `WORKFLOW_RUNNER_PAT` from config
- Uses `requests` (already implied by Flask ecosystem) or `httpx`

### Task 4: API Endpoint Enhancement

**`POST /api/deployments` changes:**
- Accept: `{ name: str, deploy_args: dict }`
- Actions:
  1. Dispatch GH workflow via `github.py`
  2. Poll for `workflow_run_id` (the dispatch API returns 204, no run ID)
  3. If dispatch or run ID retrieval fails → return error (e.g., 502), do NOT create DB record
  4. On success: create `Deployment` record with name, deploy_args, workflow_run_id, status=pending
  5. Create `DeploymentEvent` with type="created"
  6. Return: deployment dict including events

**New endpoint: `GET /api/deployments/<id>/events`**
- Returns list of events for a deployment

### Task 5: Background Monitor

**Polling loop:**
1. Query deployments with status in (pending, running)
2. For each, check GH Actions workflow run status
3. Map GH run status → deployment status:
   - `queued`/`in_progress` → running
   - `completed` + `success` → stopped (or a new "completed" status)
   - `completed` + `failure` → failed
4. If status changed, update deployment and create event
5. Sleep for interval

**Needs:** GH API module from Task 3, DB access (init_db pattern for standalone worker)

### Task 6: MCP Server Updates

**`create_deployment` changes:**
- Accept deployment config params: name, vm_count, node_count, genesis_port, testnet, etc.
- Build deploy_args dict and POST to API

**New `track_deployment` tool:**
- Accept deployment_id
- GET deployment from API
- Return formatted status with latest events

### Task 7: Unit Tests

**Test structure:** `tests/` at workspace root (or `web/tests/` and `mcp/tests/`)

**API tests (BDD-style):**
- GIVEN a valid deployment request WHEN POST /api/deployments THEN workflow dispatched AND deployment recorded AND event created AND response contains deployment info
- GIVEN dispatch fails WHEN POST /api/deployments THEN deployment still recorded with error event
- Test input validation (missing name, invalid deploy_args)

**MCP tests:**
- Mock httpx calls, verify correct endpoint called with correct payload
- Test create_deployment builds proper deploy_args
- Test track_deployment returns formatted status

**Monitor tests:**
- Mock GH API responses, verify status transitions
- Test event creation on status change
- Test completion event

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| `deploy_args` as JSON string in GH workflow | Avoids input count limits, flexible for future args |
| `deploy_args` as JSON column in DB | Store full config for audit/replay |
| Separate `DeploymentEvent` table | Clean audit trail, multiple events per deployment |
| `github.py` as separate module | Reusable by both API and monitor, testable in isolation |
| Use `requests` for GH API in web package | Lighter than httpx for sync Flask context; no new dep needed if we use urllib, but requests is cleaner |
| Dispatch must succeed or return error | Deployment only recorded after successful dispatch + confirmed workflow_run_id |
| Monitor polls at 60s interval | Matches existing CLI default and app.yaml config |
| Fine-grained PAT for WORKFLOW_RUNNER_PAT | Scoped to repo + Actions:Read+Write; narrower than classic PAT |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|

## Notes
- `WORKFLOW_RUNNER_PAT` already exists in config — just needs a real value
- Monitor worker already has CLI entry point and DO App Platform config
- Need to decide: add `requests` as dependency to web package, or use `urllib`
- Consider adding "completed" to DeploymentStatus enum (currently only: pending, running, stopped, failed)
- Re-read this plan before major decisions
