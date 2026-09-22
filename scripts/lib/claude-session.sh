# Shared by scripts/run-testnet-issues.sh and scripts/implement-issues.sh.
# Source it; do not execute it.
#
# Provides:
#   render                 — turn claude's stream-json into readable terminal lines
#   run_claude_session NAME PROMPT LOG MARKER
#                          — run `claude -p` headless in the background, render
#                            live, keep the raw stream in LOG, wait, return its
#                            exit code; resume the session if it dies before
#                            printing MARKER. Sets SESSION_PID for interrupt
#                            handlers.
#   require_deps           — fail unless claude and jq are on PATH
#   validate_issue_ids     — check a comma-separated list of Linear ids

CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"
SESSION_PID=""

require_deps() {
    local dep
    for dep in claude jq; do
        command -v "$dep" >/dev/null || { echo "$dep not on PATH" >&2; exit 1; }
    done
}

# Usage: validate_issue_ids "V2-1,V2-2"  → fills the global ISSUES array
validate_issue_ids() {
    IFS=',' read -r -a ISSUES <<< "$1"
    [[ ${#ISSUES[@]} -ge 1 ]] || return 1
    local issue
    for issue in "${ISSUES[@]}"; do
        [[ "$issue" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || { echo "Not a Linear issue id: '$issue'" >&2; return 1; }
    done
}

# What the agent says, each tool call, a trimmed tool result, and the final
# result line with cost — timestamped as they arrive.
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

# Runs in the background and `wait`s so a trapped signal interrupts the wait
# and the caller's handler runs even if claude itself ignores the signal.
_claude_once() {
    local log="$1"; shift
    (
        claude -p "$@" \
            --model "$CLAUDE_MODEL" \
            --dangerously-skip-permissions \
            --output-format stream-json \
            --verbose \
            2>> "$log" | tee -a "$log" | render
        exit "${PIPESTATUS[0]}"
    ) &
    SESSION_PID=$!
    wait "$SESSION_PID"
    local rc=$?
    SESSION_PID=""
    return $rc
}

# run_claude_session NAME PROMPT LOG MARKER
#
# A print-mode session ends its process on an API error (a 529 mid-run killed
# a testnet session and left the fleet with no operator). So: if the session
# exits non-zero WITHOUT having printed MARKER — the line the skill prints as
# its very last act — it did not finish, and it is resumed by session id with
# the same context (task list, T0, schedule). Up to MAX_RESUMES times, with a
# growing pause for the outage to clear. A zero exit is never resumed.
MAX_RESUMES="${MAX_RESUMES:-6}"
run_claude_session() {
    local name="$1" prompt="$2" log="$3" marker="$4"
    local attempt=0 rc session_id delay
    _claude_once "$log" "$prompt" --name "$name"
    rc=$?
    while (( rc != 0 )) && ! grep -q "$marker" "$log" && (( attempt < MAX_RESUMES )); do
        attempt=$((attempt + 1))
        session_id="$(jq -r 'select(.type=="system" and .subtype=="init") | .session_id' "$log" 2>/dev/null | tail -1)"
        if [[ -z "$session_id" ]]; then
            echo "[$(date -u +%FT%TZ)] session exited rc=$rc before it had a session id; cannot resume" >&2
            break
        fi
        delay=$(( 60 * attempt ))
        echo "[$(date -u +%FT%TZ)] session exited rc=$rc without finishing; resuming $session_id in ${delay}s (attempt $attempt/$MAX_RESUMES)"
        sleep "$delay"
        _claude_once "$log" --resume "$session_id" \
"Your previous turn ended unexpectedly at $(date -u +%FT%TZ) (an API error or crash), not because the \
work was done. The task is still in progress. Re-read your task list and the current clock, work out where \
you are relative to the schedule, and continue from there. Do not start again from the beginning, do not \
repeat steps already recorded as done, and do not ask anything."
        rc=$?
    done
    return $rc
}
