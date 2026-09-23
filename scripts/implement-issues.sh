#!/usr/bin/env bash
#
# Implement one or more Linear issues against saorsa-deploy, unattended, one
# after another, each ending in a PR on the fork.
#
#   scripts/implement-issues.sh V2-1300[,V2-1301,...]
#
# Each issue is handed to the `implement-issue` skill in a headless `claude -p`
# session: branch from origin/main, implement + test as the issue specifies,
# commit, push, open a PR, move the issue to In Review. The next issue starts
# only when the previous session has exited, and each one branches from
# origin/main, so the issues are independent of one another.
#
# Ctrl-C stops the current session. Whatever it had done to the working tree
# is left as-is on that issue's branch for you to inspect.
#
# Env:
#   CLAUDE_MODEL   model for the session        (default claude-opus-5)
#   MAX_RESUMES    times a session that dies mid-run (API error) is resumed (default 6)
#   LOG_DIR        where the raw session logs go (default ~/.autonomi/implement-runs)

set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") <ISSUE>[,<ISSUE>...]" >&2
    echo "  e.g. $(basename "$0") V2-1300,V2-1301" >&2
    exit 2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/claude-session.sh
. "$REPO_ROOT/scripts/lib/claude-session.sh"

[[ $# -eq 1 && -n "$1" ]] || usage
validate_issue_ids "$1" || usage
require_deps
command -v gh >/dev/null || { echo "gh not on PATH" >&2; exit 1; }

LOG_DIR="${LOG_DIR:-$HOME/.autonomi/implement-runs}"
mkdir -p "$LOG_DIR"
cd "$REPO_ROOT"

# The skill refuses a dirty tree, so say so here rather than once per issue.
if [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is not clean — commit or stash first:" >&2
    git status --short >&2
    exit 1
fi

interrupted() {
    trap - INT TERM HUP
    echo
    echo "[$(date -u +%FT%TZ)] INTERRUPTED — stopping the session"
    [[ -n "$SESSION_PID" ]] && pkill -TERM -P "$SESSION_PID" 2>/dev/null
    pkill -TERM -f '[c]laude -p /implement-issue' 2>/dev/null
    sleep 2
    pkill -KILL -f '[c]laude -p /implement-issue' 2>/dev/null
    echo "Branch $(git rev-parse --abbrev-ref HEAD) left as-is; uncommitted changes:"
    git status --short | sed 's/^/    /'
    exit 130
}
trap interrupted INT TERM HUP

failed=()
for issue in "${ISSUES[@]}"; do
    log="$LOG_DIR/$issue-$(date -u +%Y%m%dT%H%M%SZ).log"
    echo "[$(date -u +%FT%TZ)] START $issue  (raw log: $log)"
    run_claude_session "Implement: $issue" "/implement-issue $issue" "$log" "IMPLEMENT COMPLETE"
    rc=$?
    echo "[$(date -u +%FT%TZ)] EXIT $issue rc=$rc  $(grep -o 'IMPLEMENT COMPLETE [^"\\]*' "$log" | tail -1)"
    # Where the work lives, for review feedback later. `claude --resume <id>`
    # reopens this exact conversation; it is also in the /resume picker under
    # the session name.
    printf 'claude --resume %s\n' "$RUN_SESSION_ID" > "$LOG_DIR/$issue.session"
    echo "[$(date -u +%FT%TZ)] RESUME $issue  claude --resume $RUN_SESSION_ID"
    [[ $rc -eq 0 ]] || failed+=("$issue")
    # Each issue must start from a clean tree on origin/main; a session that
    # died mid-edit would otherwise contaminate the next branch.
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "Tree left dirty by $issue on $(git rev-parse --abbrev-ref HEAD); stopping so nothing bleeds into the next issue." >&2
        git status --short >&2
        exit 1
    fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "Issues that did not exit cleanly: ${failed[*]}" >&2
    exit 1
fi
