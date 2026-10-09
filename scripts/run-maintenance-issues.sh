#!/usr/bin/env bash
#
# Run one or more PRODUCTION MAINTENANCE Linear issues, unattended, one after
# another. A bad run does not stop the list: the next issue starts regardless.
# There is no chain mode — a maintenance that depends on another one finishing
# is a judgement a human makes between the two.
#
#   scripts/run-maintenance-issues.sh V2-1445[,V2-1446,...]
#
# Each issue is handed to the `run-prod-maintenance-from-issue` skill in a
# headless `claude -p` session. The skill runs `saorsa-deploy` commands,
# read-only checks and commands the issue spells out verbatim, and refuses an
# issue holding any step it would have to compose itself. The repo's `.env` is
# exported into the session (shell values win), so an issue's `psql
# "$DATABASE_URL"` works from a fresh shell. Print mode exits when the agent ends its
# turn, and the skill does not end its turn until the issue is updated — so the
# next issue starts only when the previous session has exited.
#
# Exit status: 0 if every run was a PASS, 1 if any was WARN, FAIL, ABANDONED or
# left no verdict (see lib/issue-runs.sh), 130 on Ctrl-C.
#
# Ctrl-C stops the current session and then **waits** for any
# `saorsa-deploy prod …` process to finish; it is never signalled. Killing a
# command part way through — between the binary swap and the restart, say — is
# the one way this tooling can itself break a production host, and only the
# live command can run its own rollback. The operator who truly must kill it
# does so by hand, knowingly. Killing the terminal does the same via SIGHUP.
#
# Env:
#   CLAUDE_MODEL   model for the session        (default claude-opus-5)
#   MAX_RESUMES    times a session that dies mid-run (API error) is resumed (default 6)
#   LOG_DIR        where the raw session logs go (default ~/.autonomi/maintenance-runs)

set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") <ISSUE>[,<ISSUE>...]" >&2
    echo "  e.g. $(basename "$0") V2-1445" >&2
    exit 2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/claude-session.sh
. "$REPO_ROOT/scripts/lib/claude-session.sh"
load_repo_dotenv "$REPO_ROOT"
# shellcheck source=lib/issue-runs.sh
. "$REPO_ROOT/scripts/lib/issue-runs.sh"

[[ $# -eq 1 && -n "$1" ]] || usage
validate_issue_ids "$1" || usage
require_deps

# The four below are read by run_issue_list in lib/issue-runs.sh, not by this
# file: they are what make the generic loop a production maintenance run.
# shellcheck disable=SC2034
RUN_SKILL="run-prod-maintenance-from-issue"
# shellcheck disable=SC2034
RUN_SESSION_PREFIX="Prod Maintenance"
# shellcheck disable=SC2034
RUN_INTERRUPT_POLICY="never-signal"
# shellcheck disable=SC2034
RUN_INCOMPLETE_ADVICE="Each issue that ran to its end is In Review; read its results comment. A NO-VERDICT run may have left a production host part way through an operation — read its per-command logs under \$LOG_DIR and \`prod bootstrap status\` before doing anything else."

LOG_DIR="${LOG_DIR:-$HOME/.autonomi/maintenance-runs}"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT" || exit 1

run_issue_list independent
