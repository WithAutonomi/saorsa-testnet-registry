#!/usr/bin/env bash
#
# Run one or more testnet-run Linear issues, unattended, one after another.
#
#   scripts/run-testnet-issues.sh V2-1281[,V2-1282,...]
#
# Each issue is handed to the `run-testnet-from-issue` skill in a headless
# `claude -p` session. Print mode exits when the agent ends its turn, and the
# skill is written not to end its turn until the run is torn down and the issue
# is updated — so this loop is strictly sequential: the next issue starts only
# when the previous session has exited.
#
# Two safety nets sit outside the agent:
#   * RUN_TIMEOUT (default 12h) kills a session that has not exited — a last
#     resort, since killing mid-run can strand infrastructure, which is why
#   * a short, bounded SWEEP session follows every run (exit or kill) and tears
#     down anything the run left behind for that issue's deployment name.
#
# Env:
#   RUN_TIMEOUT    per-issue wall-clock limit for the run session   (default 12h)
#   SWEEP_TIMEOUT  limit for the post-run sweep session             (default 30m)
#   CLAUDE_MODEL   model for both sessions                          (default claude-opus-5)
#   LOG_DIR        where session logs go   (default ~/.autonomi/testnet-runs)
#   NO_SWEEP=1     skip the sweep session

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <ISSUE>[,<ISSUE>...]" >&2
    echo "  e.g. $(basename "$0") V2-1281,V2-1282" >&2
    exit 2
}

[[ $# -eq 1 && -n "$1" ]] || usage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_TIMEOUT="${RUN_TIMEOUT:-12h}"
SWEEP_TIMEOUT="${SWEEP_TIMEOUT:-30m}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-5}"
LOG_DIR="${LOG_DIR:-$HOME/.autonomi/testnet-runs}"
mkdir -p "$LOG_DIR"

IFS=',' read -r -a ISSUES <<< "$1"
[[ ${#ISSUES[@]} -ge 1 ]] || usage
for issue in "${ISSUES[@]}"; do
    [[ "$issue" =~ ^[A-Z][A-Z0-9]*-[0-9]+$ ]] || { echo "Not a Linear issue id: '$issue'" >&2; usage; }
done

command -v claude  >/dev/null || { echo "claude not on PATH" >&2; exit 1; }
command -v timeout >/dev/null || { echo "timeout(1) not on PATH" >&2; exit 1; }

cd "$REPO_ROOT"

# claude -p returns when the agent ends its turn. `timeout` is the ceiling on
# top of that; 124 means it was killed. Logs are streamed, not buffered, so a
# run can be followed with `tail -f`.
run_session() {
    local name="$1" prompt="$2" limit="$3" log="$4"
    timeout --signal=INT --kill-after=60s "$limit" \
        claude -p "$prompt" \
            --model "$CLAUDE_MODEL" \
            --name "$name" \
            --dangerously-skip-permissions \
            --permission-prompts none \
            --output-format text \
        > "$log" 2>&1
}

failed=()
for issue in "${ISSUES[@]}"; do
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_log="$LOG_DIR/$issue-$stamp.log"
    echo "[$(date -u +%FT%TZ)] START $issue  (log: $run_log)"

    rc=0
    run_session "Testnet Run: $issue" "/run-testnet-from-issue $issue" "$RUN_TIMEOUT" "$run_log" || rc=$?
    if [[ $rc -eq 124 ]]; then
        echo "[$(date -u +%FT%TZ)] KILLED $issue after $RUN_TIMEOUT"
    else
        echo "[$(date -u +%FT%TZ)] EXIT $issue rc=$rc  $(grep -o 'RUN COMPLETE.*' "$run_log" | tail -1)"
    fi
    [[ $rc -eq 0 ]] || failed+=("$issue")

    if [[ -z "${NO_SWEEP:-}" ]]; then
        sweep_log="$LOG_DIR/$issue-$stamp.sweep.log"
        echo "[$(date -u +%FT%TZ)] SWEEP $issue  (log: $sweep_log)"
        src=0
        run_session "Testnet Sweep: $issue" \
"Post-run safety sweep for Linear issue $issue. Read the issue to get the deployment name. \
Check whether any billable compute for that deployment still exists: the S3 deployment state \
key saorsa-deploy/<name>.json, and the provider APIs named in the manifest (DigitalOcean, \
Vultr, Hetzner, OVH) for VMs or volumes named <name>-*. If compute remains, tear it down with \
'uv run saorsa-deploy deployments down --name <name>' (it takes NO --force), verify against S3 \
and the provider APIs again, and post a comment on $issue saying what was found and removed. \
Leave the Elasticsearch and monitoring stacks alone if the issue says they are retained. \
If nothing remains, print 'SWEEP CLEAN' and stop. Do nothing else; never ask a question." \
            "$SWEEP_TIMEOUT" "$sweep_log" || src=$?
        echo "[$(date -u +%FT%TZ)] SWEEP EXIT $issue rc=$src  $(grep -o 'SWEEP CLEAN' "$sweep_log" | tail -1)"
    fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "Runs that did not exit cleanly: ${failed[*]}" >&2
    exit 1
fi
