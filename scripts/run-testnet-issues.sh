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
#   LOG_DIR        where the raw session logs go (default ~/.autonomi/testnet-runs)

set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") <ISSUE>[,<ISSUE>...]" >&2
    echo "  e.g. $(basename "$0") V2-1281,V2-1282" >&2
    exit 2
}

[[ $# -eq 1 && -n "$1" ]] || usage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"
LOG_DIR="${LOG_DIR:-$HOME/.autonomi/testnet-runs}"
mkdir -p "$LOG_DIR"

IFS=',' read -r -a ISSUES <<< "$1"
[[ ${#ISSUES[@]} -ge 1 ]] || usage
for issue in "${ISSUES[@]}"; do
    [[ "$issue" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || { echo "Not a Linear issue id: '$issue'" >&2; usage; }
done

for dep in claude jq; do
    command -v "$dep" >/dev/null || { echo "$dep not on PATH" >&2; exit 1; }
done

cd "$REPO_ROOT"

# Render claude's stream-json events as they arrive: what the agent says, each
# tool call, a trimmed tool result, and the final result line with cost.
render() {
    jq --unbuffered -r '
        def clip(n): if length > n then .[:n] + " …" else . end;
        def ts: (now | strftime("%H:%M:%S"));
        if .type == "assistant" then
            .message.content[]
            | if .type == "text" then "\(ts) │ " + (.text | gsub("\n"; "\n         │ "))
              elif .type == "tool_use" then
                  "\(ts) ▶ \(.name): " + ((.input.description // .input.command // .input.prompt // .input.file_path // (.input | tojson)) | tostring | clip(160))
              else empty end
        elif .type == "user" then
            .message.content[]? | select(.type == "tool_result")
            | "\(ts) ◀ " + ((.content | if type == "array" then map(.text? // "") | join(" ") else tostring end) | gsub("\n"; " ⏎ ") | clip(240))
        elif .type == "result" or has("duration_api_ms") then
            "\(ts) ■ \(.subtype // .stop_reason // "end") in \((.duration_ms // .duration_api_ms) / 60000 | floor)m, cost $\(.total_cost_usd // 0 | . * 100 | round / 100)"
        else empty end'
}

# On Ctrl-C / hangup: stop the agent, stop any deploy it launched, say what may
# be left behind. The agent is not asked to clean up — it is gone. Patterns use
# the [x] trick so they can never match this script's own command line.
SESSION_PID=""
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

    # Run in the background and `wait` so a trapped signal interrupts the wait
    # and runs the handler even if claude itself ignores the signal.
    (
        claude -p "/run-testnet-from-issue $issue" \
            --model "$CLAUDE_MODEL" \
            --name "Testnet Run: $issue" \
            --dangerously-skip-permissions \
            --output-format stream-json \
            --verbose \
            2>> "$log" | tee -a "$log" | render
        exit "${PIPESTATUS[0]}"
    ) &
    SESSION_PID=$!
    wait "$SESSION_PID"
    rc=$?

    echo "[$(date -u +%FT%TZ)] EXIT $issue rc=$rc  $(grep -o 'RUN COMPLETE [^"\\]*' "$log" | tail -1)"
    [[ $rc -eq 0 ]] || failed+=("$issue")
done

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "Runs that did not exit cleanly: ${failed[*]}" >&2
    exit 1
fi
