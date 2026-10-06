# shellcheck shell=bash
#
# Shared by every script that hands a list of Linear issues to one skill,
# unattended, one issue after another. Source it after lib/claude-session.sh;
# do not execute it.
#
# Provides:
#   run_issue_list MODE  — hand every issue in the ISSUES array to the skill
#                          named by RUN_SKILL, one after another. MODE is
#                          `independent` (a bad run does not stop the list) or
#                          `chain` (the first run that is not a PASS ends the
#                          list; the rest are not started). Returns 0 only if
#                          every issue it ran was a PASS.
#   issue_run_verdict LOG ISSUE
#                        — the verdict word the skill printed, or NO-VERDICT.
#   install_issue_run_trap
#                        — arm the INT/TERM/HUP handler for RUN_INTERRUPT_POLICY.
#
# Set before calling, by the driver script:
#   RUN_SKILL             the skill to invoke, without the leading slash
#                         (e.g. `run-testnet-from-issue`)
#   RUN_SESSION_PREFIX    what the session is named, before ": <issue>"
#                         (e.g. `Testnet Run`)
#   RUN_INTERRUPT_POLICY  `kill-deploy` or `never-signal` (see below)
#
# The agent cannot choose `claude`'s exit code, so the outcome of a run is read
# from the last line the skill prints:
#
#   RUN COMPLETE <issue> <PASS|WARN|FAIL|ABANDONED> <trailing field>
#
#   PASS       everything the issue asked for happened and the results comment
#              carries no warning
#   WARN       nothing failed, but the comment opens with a warning — a check
#              NOT ASSESSED, or a run that did not do what it was for
#   FAIL       at least one check failed
#   ABANDONED  a precondition or stop condition ended the run early
#
# Every skill prints the same four fields, which is why one parser serves them
# all: the trailing field is a registry id for a testnet run and a `-` for a
# maintenance run, and nothing here reads it.
#
# A session that exits without printing that line (it died, or was killed) is
# NO-VERDICT. Anything but PASS counts as a bad run.
#
# Interrupt policies
#
#   kill-deploy   Ctrl-C stops the session AND any `saorsa-deploy deployments
#                 up` it started, then says what may be left behind. For
#                 testnet runs: the infrastructure is scratch, and a half-built
#                 testnet costs money for nothing.
#   never-signal  Ctrl-C WAITS for any running `saorsa-deploy prod …` to
#                 finish, and stops the session only after it has. For
#                 production maintenance: killing a command part way through —
#                 between the binary swap and the restart, say — is the one way
#                 this tooling can itself break a production host, and the
#                 command runs as a grandchild of the session, so stopping the
#                 session first could kill it by proxy. The operator who truly
#                 must kill it does so by hand, knowingly.
#
# Patterns use the [x] trick so they can never match this script's own command
# line.

# Usage: issue_run_verdict LOG ISSUE  → prints the verdict word
issue_run_verdict() {
    local log="$1" issue="$2" line
    line="$(grep -oE "RUN COMPLETE $issue (PASS|WARN|FAIL|ABANDONED)" "$log" | tail -1)"
    if [[ -n "$line" ]]; then
        echo "${line##* }"
    else
        echo "NO-VERDICT"
    fi
}

_issue_runs_indent() {
    local line
    while IFS= read -r line; do
        echo "    $line"
    done <<< "$1"
}

_issue_runs_stop_session() {
    [[ -n "$SESSION_PID" ]] && pkill -TERM -P "$SESSION_PID" 2>/dev/null
    pkill -TERM -f "[c]laude -p /$RUN_SKILL" 2>/dev/null
    sleep 2
    pkill -KILL -f "[c]laude -p /$RUN_SKILL" 2>/dev/null
}

# The agent is not asked to clean up — it is gone.
_issue_runs_kill_deploy() {
    local ups
    ups="$(pgrep -af '[s]aorsa-deploy deployments up' || true)"
    if [[ -n "$ups" ]]; then
        echo "A deploy was in progress and has been killed:"
        _issue_runs_indent "$ups"
        pkill -TERM -f '[s]aorsa-deploy deployments up' 2>/dev/null
        echo "Infrastructure may exist. Tear it down with:"
        echo "    uv run saorsa-deploy deployments down --name <name>"
        echo "and delete any ant-node-build-<name>-* droplet at DigitalOcean."
    fi
}

# Never signalled: a production host may be mid-operation, and the command's
# own rollback can only run if the command is still alive to run it.
_issue_runs_wait_for_prod() {
    local running
    running="$(pgrep -af '[s]aorsa-deploy prod' || true)"
    [[ -n "$running" ]] || return 0
    echo "A production command is still running and will NOT be signalled:"
    _issue_runs_indent "$running"
    echo "A production host may be mid-operation. Waiting for it to finish."
    echo "To kill it anyway, do so by hand in another terminal — knowing that a"
    echo "host left between the binary swap and the restart has no rollback."
    while pgrep -f '[s]aorsa-deploy prod' >/dev/null; do
        sleep 10
    done
    echo "The production command has exited."
}

_issue_runs_interrupted() {
    trap - INT TERM HUP
    echo
    echo "[$(date -u +%FT%TZ)] INTERRUPTED"
    case "$RUN_INTERRUPT_POLICY" in
        kill-deploy)
            _issue_runs_stop_session
            _issue_runs_kill_deploy
            ;;
        never-signal)
            # The prod command is WAITED FOR FIRST, and only then is the session
            # stopped. It runs as a grandchild of the claude session (its Bash
            # tool), so stopping the session first risks killing the command by
            # proxy, mid-swap -- the one thing this policy exists to prevent,
            # and not something this script can establish either way. Ctrl-C
            # therefore blocks until the command finishes. The cost is that the
            # session is still alive while we wait and could in principle start
            # one more command; that is the lesser risk, because a host left
            # between the binary swap and the restart has no rollback at all.
            _issue_runs_wait_for_prod
            _issue_runs_stop_session
            ;;
        *)
            echo "Unknown RUN_INTERRUPT_POLICY '$RUN_INTERRUPT_POLICY'; stopping the session only" >&2
            _issue_runs_stop_session
            ;;
    esac
    exit 130
}

install_issue_run_trap() {
    trap _issue_runs_interrupted INT TERM HUP
}

run_issue_list() {
    local mode="$1"
    local issue log rc verdict i
    local bad=() skipped=()

    install_issue_run_trap

    for i in "${!ISSUES[@]}"; do
        issue="${ISSUES[$i]}"
        log="$LOG_DIR/$issue-$(date -u +%Y%m%dT%H%M%SZ).log"
        echo "[$(date -u +%FT%TZ)] START $issue  (raw log: $log)"
        run_claude_session "$RUN_SESSION_PREFIX: $issue" "/$RUN_SKILL $issue" "$log" "RUN COMPLETE"
        rc=$?
        verdict="$(issue_run_verdict "$log" "$issue")"
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
        echo "$RUN_INCOMPLETE_ADVICE" >&2
        return 1
    fi
    echo "All runs PASS. Each issue is In Review; read its results comment."
    return 0
}
