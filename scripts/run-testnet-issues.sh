#!/usr/bin/env bash
#
# Run one or more testnet-run Linear issues, unattended, one after another.
#
#   scripts/run-testnet-issues.sh V2-1281[,V2-1282,...]
#
# Each issue is handed to the `run-testnet-from-issue` skill in a headless
# `claude -p` session. Print mode exits when the agent ends its turn, and the
# skill does not end its turn until the run is torn down and the issue is
# updated — so the next issue starts only when the previous session has exited.
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

[[ $# -eq 1 && -n "$1" ]] || usage
validate_issue_ids "$1" || usage
require_deps

LOG_DIR="${LOG_DIR:-$HOME/.autonomi/testnet-runs}"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

# On Ctrl-C / hangup: stop the agent, stop any deploy it launched, say what may
# be left behind. The agent is not asked to clean up — it is gone. Patterns use
# the [x] trick so they can never match this script's own command line.
interrupted() {
    trap - INT TERM HUP
    echo
    echo "[$(date -u +%FT%TZ)] INTERRUPTED — stopping the session"
    [[ -n "$SESSION_PID" ]] && pkill -TERM -P "$SESSION_PID" 2>/dev/null
    pkill -TERM -f '[c]laude -p /run-testnet-from-issue' 2>/dev/null
    local ups
    ups="$(pgrep -af '[s]aorsa-deploy deployments up' || true)"
    if [[ -n "$ups" ]]; then
        echo "A deploy was in progress and has been killed:"
        echo "$ups" | sed 's/^/    /'
        pkill -TERM -f '[s]aorsa-deploy deployments up' 2>/dev/null
        echo "Infrastructure may exist. Tear it down with:"
        echo "    uv run saorsa-deploy deployments down --name <name>"
        echo "and delete any ant-node-build-<name>-* droplet at DigitalOcean."
    fi
    sleep 2
    pkill -KILL -f '[c]laude -p /run-testnet-from-issue' 2>/dev/null
    exit 130
}
trap interrupted INT TERM HUP

failed=()
for issue in "${ISSUES[@]}"; do
    log="$LOG_DIR/$issue-$(date -u +%Y%m%dT%H%M%SZ).log"
    echo "[$(date -u +%FT%TZ)] START $issue  (raw log: $log)"
    run_claude_session "Testnet Run: $issue" "/run-testnet-from-issue $issue" "$log" "RUN COMPLETE"
    rc=$?
    echo "[$(date -u +%FT%TZ)] EXIT $issue rc=$rc  $(grep -o 'RUN COMPLETE [^"\\]*' "$log" | tail -1)"
    # Where the work lives, for review feedback later. `claude --resume <id>`
    # reopens this exact conversation; it is also in the /resume picker under
    # the session name.
    printf 'claude --resume %s\n' "$RUN_SESSION_ID" > "$LOG_DIR/$issue.session"
    echo "[$(date -u +%FT%TZ)] RESUME $issue  claude --resume $RUN_SESSION_ID"
    [[ $rc -eq 0 ]] || failed+=("$issue")
done

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "Runs that did not exit cleanly: ${failed[*]}" >&2
    exit 1
fi
