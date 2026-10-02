# Shared by scripts/run-testnet-issues.sh and scripts/run-testnet-chain.sh.
# Source it after lib/claude-session.sh; do not execute it.
#
# Provides:
#   run_testnet_issues MODE  — hand every issue in the ISSUES array to the
#                              `run-testnet-from-issue` skill, one after another.
#                              MODE is `independent` (a bad run does not stop
#                              the list) or `chain` (the first run that is not
#                              a PASS ends the list; the rest are not started).
#                              Returns 0 only if every issue it ran was a PASS.
#
# The agent cannot choose `claude`'s exit code, so the outcome of a run is read
# from the last line the skill prints:
#
#   RUN COMPLETE <issue> <PASS|WARN|FAIL|ABANDONED> <registry-id>
#
#   PASS       every acceptance criterion passed and the results comment
#              carries no warning
#   WARN       no criterion failed, but the comment opens with a warning — a
#              criterion NOT ASSESSED, or a run that did not do what it was for
#   FAIL       at least one acceptance criterion failed
#   ABANDONED  a precondition or stop condition ended the run early
#
# A session that exits without printing that line (it died, or was killed) is
# NO-VERDICT. Anything but PASS counts as a bad run.

# Usage: testnet_run_verdict LOG ISSUE  → prints the verdict word
testnet_run_verdict() {
    local log="$1" issue="$2" line
    line="$(grep -oE "RUN COMPLETE $issue (PASS|WARN|FAIL|ABANDONED)" "$log" | tail -1)"
    if [[ -n "$line" ]]; then
        echo "${line##* }"
    else
        echo "NO-VERDICT"
    fi
}

# On Ctrl-C / hangup: stop the agent, stop any deploy it launched, say what may
# be left behind. The agent is not asked to clean up — it is gone. Patterns use
# the [x] trick so they can never match this script's own command line.
_testnet_runs_interrupted() {
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

run_testnet_issues() {
    local mode="$1"
    local issue log rc verdict i
    local bad=() skipped=()

    trap _testnet_runs_interrupted INT TERM HUP

    for i in "${!ISSUES[@]}"; do
        issue="${ISSUES[$i]}"
        log="$LOG_DIR/$issue-$(date -u +%Y%m%dT%H%M%SZ).log"
        echo "[$(date -u +%FT%TZ)] START $issue  (raw log: $log)"
        run_claude_session "Testnet Run: $issue" "/run-testnet-from-issue $issue" "$log" "RUN COMPLETE"
        rc=$?
        verdict="$(testnet_run_verdict "$log" "$issue")"
        echo "[$(date -u +%FT%TZ)] EXIT $issue rc=$rc verdict=$verdict  $(grep -oE "RUN COMPLETE $issue [^\"\\\\]*" "$log" | tail -1)"
        # Where the work lives, for review feedback later. `claude --resume <id>`
        # reopens this exact conversation; it is also in the /resume picker under
        # the session name.
        printf 'claude --resume %s\n' "$RUN_SESSION_ID" > "$LOG_DIR/$issue.session"
        echo "[$(date -u +%FT%TZ)] RESUME $issue  claude --resume $RUN_SESSION_ID"

        if [[ $rc -ne 0 || "$verdict" != "PASS" ]]; then
            bad+=("$issue:$verdict")
            if [[ "$mode" == "chain" ]]; then
                skipped=("${ISSUES[@]:$((i + 1))}")
                break
            fi
        fi
    done

    if [[ ${#skipped[@]} -gt 0 ]]; then
        echo "Chain stopped at $issue. Not started: ${skipped[*]}" >&2
    fi
    if [[ ${#bad[@]} -gt 0 ]]; then
        echo "Runs that were not a clean PASS: ${bad[*]}" >&2
        echo "Each issue that ran to its end is In Review; read its results comment. A NO-VERDICT run may have left infrastructure up." >&2
        return 1
    fi
    echo "All runs PASS. Each issue is In Review; read its results comment."
    return 0
}
