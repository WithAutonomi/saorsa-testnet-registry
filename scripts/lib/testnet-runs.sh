# shellcheck shell=bash
#
# Shared by scripts/run-testnet-issues.sh and scripts/run-testnet-chain.sh.
# Source it after lib/claude-session.sh; do not execute it.
#
# A thin wrapper over lib/issue-runs.sh, which holds the loop, the verdict
# parser, the resume-on-API-error behaviour and the interrupt handler for every
# skill that is driven from a list of Linear issues. This file is what makes
# that generic machinery a *testnet* run: the skill it calls, the session name,
# and the interrupt policy "kill `deployments up` and say what may be left".
#
# Provides:
#   run_testnet_issues MODE  — hand every issue in the ISSUES array to the
#                              `run-testnet-from-issue` skill, one after
#                              another. MODE is `independent` (a bad run does
#                              not stop the list) or `chain` (the first run
#                              that is not a PASS ends the list; the rest are
#                              not started). Returns 0 only if every issue it
#                              ran was a PASS.
#   testnet_run_verdict LOG ISSUE  — the verdict word, or NO-VERDICT.
#
# See lib/issue-runs.sh for the verdict line the parser reads and what each
# verdict means.

# shellcheck source-path=SCRIPTDIR
_TESTNET_RUNS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=issue-runs.sh
. "$_TESTNET_RUNS_LIB_DIR/issue-runs.sh"

# The four below are read by run_issue_list in the library sourced above, not
# by this file: they are what make the generic loop a testnet run.
# shellcheck disable=SC2034
RUN_SKILL="run-testnet-from-issue"
# shellcheck disable=SC2034
RUN_SESSION_PREFIX="Testnet Run"
# The testnet fleet is scratch, and a half-built one bills for nothing, so a
# deploy left running by a killed session is killed too.
# shellcheck disable=SC2034
RUN_INTERRUPT_POLICY="kill-deploy"
# shellcheck disable=SC2034
RUN_INCOMPLETE_ADVICE="Each issue that ran to its end is In Review; read its results comment. A NO-VERDICT run may have left infrastructure up."

# Usage: testnet_run_verdict LOG ISSUE  → prints the verdict word
testnet_run_verdict() {
    issue_run_verdict "$@"
}

run_testnet_issues() {
    run_issue_list "$1"
}
