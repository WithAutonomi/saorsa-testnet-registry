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
#                            handlers and RUN_SESSION_ID for the caller.
#   require_deps           — fail unless claude, jq and uuidgen are on PATH
#   validate_issue_ids     — check a comma-separated list of Linear ids
#   load_repo_dotenv DIR   — export every KEY=value in DIR/.env the shell has
#                            not already set, so the headless session inherits
#                            the repo's environment (DATABASE_URL and friends)

CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"
SESSION_PID=""
# The session id this run will use. Generated up front rather than read back
# from the stream, so it can be printed before the work starts, survives a
# session that dies before emitting anything, and gives the operator something
# to resume when they come back with review feedback.
RUN_SESSION_ID=""

require_deps() {
    local dep
    for dep in claude jq uuidgen; do
        command -v "$dep" >/dev/null || { echo "$dep not on PATH" >&2; exit 1; }
    done
}

# Usage: load_repo_dotenv DIR
#
# The skills run `psql "$DATABASE_URL"`, `aws`, `ssh` and `saorsa-deploy` with
# whatever environment the session inherits. `saorsa-deploy` reads the repo's
# .env itself; a bare `psql "$DATABASE_URL"` in an issue's Procedure does not,
# and an operator starting a run from a fresh shell should not have to export
# the file by hand first. So every runner exports DIR/.env here. A value already
# set in the shell wins, so one key can be overridden for a single run without
# editing the file. Accepts `KEY=value` and `export KEY=value`, one pair of
# surrounding quotes stripped; comments and blank lines skipped; nothing is
# evaluated.
load_repo_dotenv() {
    local file="$1/.env" line key value
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        line="${line#export }"
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if (( ${#value} >= 2 )) && { [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]] || [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; }; then
            value="${value:1:${#value}-2}"
        fi
        [[ -n "${!key+x}" ]] && continue
        export "$key=$value"
    done < "$file"
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
# The session id is assigned here (`--session-id`) instead of being read back
# out of the stream, so the operator is told where the work lives *before* it
# starts. Resuming it later — `claude --resume <id>` — reopens the same
# conversation interactively, which is how review feedback gets handled without
# re-explaining the task.
#
# A print-mode session also ends its process on an API error (a 529 mid-run
# killed a testnet session and left the fleet with no operator). So: a session
# that has not printed MARKER — the line the skill prints as its very last act —
# did not finish, and it is resumed with the same context. Up to MAX_RESUMES
# times, with a growing pause for an outage to clear.
#
# **The exit code is deliberately not part of that test.** Print mode exits when
# the agent ends its turn, and it exits ZERO when it does so cleanly — including
# when the agent ends the turn early, believing it will be woken when some
# background work finishes. It will not be: a background Bash task is a child of
# the `claude` process and is killed at teardown. On 2026-10-06 a maintenance run
# ended that way mid-upgrade against a production bootstrap — `subtype: success`,
# `api_error_status: null`, rc 0, no MARKER, no results comment — and because the
# old condition here also required `rc != 0`, nothing was resumed and the run
# simply vanished. The marker is the only evidence that a skill finished; the
# exit code is evidence of nothing.
MAX_RESUMES="${MAX_RESUMES:-6}"
run_claude_session() {
    local name="$1" prompt="$2" log="$3" marker="$4"
    local attempt=0 rc delay
    RUN_SESSION_ID="$(uuidgen)"
    # The skill puts this in the PR body, so review feedback lands next to the
    # session that produced the change.
    export CLAUDE_RUNNER_SESSION_ID="$RUN_SESSION_ID"
    echo "[$(date -u +%FT%TZ)] session $RUN_SESSION_ID  (resume with: claude --resume $RUN_SESSION_ID)"
    _claude_once "$log" "$prompt" --name "$name" --session-id "$RUN_SESSION_ID"
    rc=$?
    while ! grep -q "$marker" "$log" && (( attempt < MAX_RESUMES )); do
        # Only resume a session that actually started; otherwise the failure is
        # in the invocation itself and resuming would loop on the same error.
        if ! grep -q "$RUN_SESSION_ID" "$log"; then
            echo "[$(date -u +%FT%TZ)] session exited rc=$rc before it started; not resuming" >&2
            break
        fi
        attempt=$((attempt + 1))
        delay=$(( 60 * attempt ))
        echo "[$(date -u +%FT%TZ)] session exited rc=$rc without printing '$marker'; resuming $RUN_SESSION_ID in ${delay}s (attempt $attempt/$MAX_RESUMES)"
        sleep "$delay"
        _claude_once "$log" --resume "$RUN_SESSION_ID" \
"Your previous turn ended at $(date -u +%FT%TZ) without printing '$marker', so the work was NOT done — \
whatever the exit code was. Either an API error killed the session, or you ended your turn early. If you \
ended it waiting to be notified that a background command had finished, understand that no such \
notification can arrive: this is a headless print-mode session, it exits when you end your turn, and a \
background task is killed with it. Any command you launched that way is dead, not running. \
The task is still in progress. Re-read your task list, the current clock and every per-command log and \
exit file on disk, work out where you are relative to the schedule, and continue from there — a command \
that recorded an exit code is finished and is never re-run. Do not start again from the beginning, do not \
repeat steps already recorded as done, and do not ask anything."
        rc=$?
    done
    return $rc
}
