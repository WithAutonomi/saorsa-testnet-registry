# Shared by scripts/run-testnet-issues.sh and scripts/implement-issues.sh.
# Source it; do not execute it.
#
# Provides:
#   render                 — turn claude's stream-json into readable terminal lines
#   run_claude_session NAME PROMPT LOG
#                          — run `claude -p` headless in the background, render
#                            live, keep the raw stream in LOG, wait, return its
#                            exit code. Sets SESSION_PID for interrupt handlers.
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
run_claude_session() {
    local name="$1" prompt="$2" log="$3"
    (
        claude -p "$prompt" \
            --model "$CLAUDE_MODEL" \
            --name "$name" \
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
