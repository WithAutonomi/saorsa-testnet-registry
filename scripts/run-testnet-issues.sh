#!/usr/bin/env bash
#
# Run one or more INDEPENDENT testnet-run Linear issues, unattended, one after
# another. A bad run does not stop the list: the next issue starts regardless.
# For runs that depend on one another, use scripts/run-testnet-chain.sh.
#
#   scripts/run-testnet-issues.sh V2-1281[,V2-1282,...]
#
# Each issue is handed to the `run-testnet-from-issue` skill in a headless
# `claude -p` session. Print mode exits when the agent ends its turn, and the
# skill does not end its turn until the run is torn down and the issue is
# updated — so the next issue starts only when the previous session has exited.
#
# Exit status: 0 if every run was a PASS, 1 if any was WARN, FAIL, ABANDONED or
# left no verdict (see lib/testnet-runs.sh), 130 on Ctrl-C.
#
# Ctrl-C stops the current session AND any `saorsa-deploy deployments up` it
# started, then tells you what to clean up. Killing the terminal does the same
# via SIGHUP. Nothing here survives the terminal on purpose.
#
# Env:
#   CLAUDE_MODEL   model for the session        (default claude-opus-5)
#   MAX_RESUMES    times a session that dies mid-run (API error) is resumed (default 6)
#   LOG_DIR        where the raw session logs go (default ~/.autonomi/testnet-runs)

set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") <ISSUE>[,<ISSUE>...]" >&2
    echo "  e.g. $(basename "$0") V2-1281,V2-1282" >&2
    exit 2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/claude-session.sh
. "$REPO_ROOT/scripts/lib/claude-session.sh"
# shellcheck source=lib/testnet-runs.sh
. "$REPO_ROOT/scripts/lib/testnet-runs.sh"

[[ $# -eq 1 && -n "$1" ]] || usage
validate_issue_ids "$1" || usage
require_deps

LOG_DIR="${LOG_DIR:-$HOME/.autonomi/testnet-runs}"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

run_testnet_issues independent
