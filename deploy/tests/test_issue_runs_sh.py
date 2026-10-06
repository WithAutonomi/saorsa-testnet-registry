"""``scripts/lib/issue-runs.sh`` and ``scripts/run-maintenance-issues.sh`` (V2-1412).

The unattended drivers are the only part of this repo that decides, with no
operator present, whether a run counted and what to do about a session that
died — so the three things that cannot be eyeballed are tested here: the
verdict the loop reads out of a session's log, the bound on how many times a
session that dies mid-run is resumed, and the rule that a running
``saorsa-deploy prod`` process is **waited for, never signalled**.

Everything runs against stub `claude`, `sleep`, `pgrep` and `pkill` on `PATH`:
nothing here starts a model session or touches a host.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
LIB = SCRIPTS / "lib"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "jq", "uuidgen")),
    reason="needs bash, jq and uuidgen",
)


def _stub(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bin_dir(tmp_path):
    """A directory prepended to ``PATH``, holding whatever stubs a test needs.

    ``sleep`` is stubbed in every test: the resume loop waits 60 s, then 120 s,
    between attempts, and the point of the test is the count, not the wait.
    """
    path = tmp_path / "bin"
    path.mkdir()
    _stub(path, "sleep", "exit 0")
    return path


def _bash(bin_dir: Path, script: str, env=None, cwd=None):
    environ = dict(os.environ)
    environ["PATH"] = f"{bin_dir}{os.pathsep}{environ['PATH']}"
    environ.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=environ,
        cwd=str(cwd or REPO_ROOT),
        timeout=120,
    )


class TestVerdictParser:
    """One parser serves every skill, so it reads the four words and nothing else."""

    def _verdict(self, bin_dir, tmp_path, log_body, issue="V2-1445"):
        log = tmp_path / "session.log"
        log.write_text(log_body)
        result = _bash(
            bin_dir,
            f". {LIB}/issue-runs.sh && issue_run_verdict {log} {issue}",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    @pytest.mark.parametrize("verdict", ["PASS", "WARN", "FAIL", "ABANDONED"])
    def test_each_verdict_is_read(self, bin_dir, tmp_path, verdict):
        body = f"noise\nRUN COMPLETE V2-1445 {verdict} -\nmore noise\n"
        assert self._verdict(bin_dir, tmp_path, body) == verdict

    def test_a_session_that_printed_no_line_is_no_verdict(self, bin_dir, tmp_path):
        # It died, or was killed: not a result, and never a PASS.
        assert self._verdict(bin_dir, tmp_path, "it just stopped\n") == "NO-VERDICT"

    def test_another_issues_line_is_not_this_issues_verdict(self, bin_dir, tmp_path):
        body = "RUN COMPLETE V2-1444 PASS 123\n"
        assert self._verdict(bin_dir, tmp_path, body) == "NO-VERDICT"

    def test_the_last_line_wins(self, bin_dir, tmp_path):
        # A resumed session's log holds both turns; the final word is the run's.
        body = "RUN COMPLETE V2-1445 WARN -\nRUN COMPLETE V2-1445 PASS -\n"
        assert self._verdict(bin_dir, tmp_path, body) == "PASS"

    def test_the_trailing_field_is_not_read(self, bin_dir, tmp_path):
        # A testnet run ends the line with a registry id and a maintenance run
        # with a dash, which is why one parser can serve both.
        assert self._verdict(bin_dir, tmp_path, "RUN COMPLETE V2-1445 PASS 641\n") == ("PASS")


_CLAUDE_STUB = """
counter="$STUB_COUNTER"
echo "x" >> "$counter"
session=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --session-id|--resume) session="$2"; shift 2 ;;
        *) shift ;;
    esac
done
jq -nc --arg s "$session" --arg t "$STUB_RESULT_TEXT" '{
    type: "result", subtype: "success", session_id: $s,
    duration_ms: 1, total_cost_usd: 0, result: $t
}'
exit "$STUB_EXIT"
"""


class TestResumeLoop:
    """A session that dies mid-run is resumed, but a bounded number of times.

    An API error ends a print-mode session's process; a 529 mid-run once killed
    a testnet session and left the fleet with no operator. The loop exists for
    that, and the bound exists so a session failing for its own reasons cannot
    spin for ever.
    """

    @pytest.fixture(autouse=True)
    def _wire(self, bin_dir, tmp_path):
        self.counter = tmp_path / "invocations"
        self.counter.write_text("")
        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        _stub(bin_dir, "claude", _CLAUDE_STUB)
        self.bin_dir = bin_dir

    def _run(self, exit_code, result_text, max_resumes="2"):
        script = (
            f". {LIB}/claude-session.sh && . {LIB}/issue-runs.sh && "
            "ISSUES=(V2-1445) && RUN_SKILL=run-prod-maintenance-from-issue && "
            'RUN_SESSION_PREFIX="Prod Maintenance" && '
            "RUN_INTERRUPT_POLICY=never-signal && RUN_INCOMPLETE_ADVICE=advice && "
            "run_issue_list independent"
        )
        result = _bash(
            self.bin_dir,
            script,
            env={
                "LOG_DIR": str(self.log_dir),
                "MAX_RESUMES": max_resumes,
                "STUB_COUNTER": str(self.counter),
                "STUB_EXIT": str(exit_code),
                "STUB_RESULT_TEXT": result_text,
            },
        )
        return result, len(self.counter.read_text().split())

    def test_a_clean_run_is_never_resumed(self):
        result, invocations = self._run(0, "RUN COMPLETE V2-1445 PASS -")
        assert invocations == 1
        assert result.returncode == 0
        assert "verdict=PASS" in result.stdout

    def test_a_session_that_died_without_finishing_is_resumed_to_the_bound(self):
        result, invocations = self._run(1, "it died", max_resumes="2")
        assert invocations == 3  # the first attempt plus MAX_RESUMES
        assert result.returncode == 1
        assert "verdict=NO-VERDICT" in result.stdout

    def test_the_bound_is_honoured_at_one(self):
        _, invocations = self._run(1, "it died", max_resumes="1")
        assert invocations == 2

    def test_a_session_that_printed_the_marker_is_not_resumed(self):
        # The marker is the line the skill prints as its very last act, so a
        # non-zero exit after it is a finished run, not a dead one.
        result, invocations = self._run(1, "RUN COMPLETE V2-1445 FAIL -")
        assert invocations == 1
        assert result.returncode == 1
        assert "verdict=FAIL" in result.stdout

    def test_the_session_id_is_recorded_for_review_feedback(self):
        self._run(0, "RUN COMPLETE V2-1445 PASS -")
        recorded = (self.log_dir / "V2-1445.session").read_text()
        assert recorded.startswith("claude --resume ")


class TestNeverSignalPolicy:
    """A running ``saorsa-deploy prod`` process is waited for, never signalled.

    Killing one part way through — between the binary swap and the restart, say
    — is the one way this tooling can itself break a production host, and only
    the live command can run its own rollback.
    """

    @pytest.fixture(autouse=True)
    def _wire(self, bin_dir, tmp_path):
        self.bin_dir = bin_dir
        self.pkill_log = tmp_path / "pkill.args"
        self.pgrep_calls = tmp_path / "pgrep.calls"
        self.pgrep_calls.write_text("")
        #: One shared file both stubs append their own name to, so the order
        #: the handler calls them in is readable.
        self.order_log = tmp_path / "order"
        self.order_log.write_text("")
        _stub(
            bin_dir,
            "pkill",
            f'echo pkill >> {self.order_log}\necho "$@" >> {self.pkill_log}\nexit 0',
        )

    def _pgrep_stub(self, busy_calls: int):
        _stub(
            self.bin_dir,
            "pgrep",
            f"""
echo pgrep >> {self.order_log}
echo "x" >> {self.pgrep_calls}
calls=$(wc -w < {self.pgrep_calls})
# The real pattern is '[s]aorsa-deploy prod', so match the tail of it.
if [[ "$*" == *"aorsa-deploy prod"* && $calls -le {busy_calls} ]]; then
    echo "4242 uv run saorsa-deploy prod bootstrap upgrade --node-version 0.21.0"
    exit 0
fi
exit 1
""",
        )

    def _wait(self):
        return _bash(
            self.bin_dir,
            f". {LIB}/issue-runs.sh && _issue_runs_wait_for_prod",
        )

    def test_it_waits_until_the_command_has_exited(self):
        self._pgrep_stub(busy_calls=3)
        result = self._wait()
        assert result.returncode == 0
        assert "will NOT be signalled" in result.stdout
        assert "has exited" in result.stdout
        assert len(self.pgrep_calls.read_text().split()) > 3

    def test_it_never_signals_the_production_command(self):
        self._pgrep_stub(busy_calls=2)
        self._wait()
        assert not self.pkill_log.exists()

    def test_nothing_is_waited_for_when_nothing_is_running(self):
        self._pgrep_stub(busy_calls=0)
        result = self._wait()
        assert result.returncode == 0
        assert result.stdout == ""

    def test_the_handler_waits_before_it_stops_the_session(self):
        # The prod command runs as a grandchild of the claude session (its Bash
        # tool), so stopping the session first risks killing the command by
        # proxy, mid-swap — exactly what this policy exists to prevent. The
        # wait therefore comes first and the whole handler blocks on it.
        self._pgrep_stub(busy_calls=3)
        result = _bash(
            self.bin_dir,
            ". %s/issue-runs.sh && RUN_INTERRUPT_POLICY=never-signal "
            "SESSION_PID=4243 RUN_SKILL=run-prod-maintenance-from-issue "
            "_issue_runs_interrupted" % LIB,
        )
        assert result.returncode == 130
        order = self.order_log.read_text().split()
        assert "pgrep" in order and "pkill" in order
        # Every consultation of the process table precedes the first signal.
        assert order.index("pkill") > max(
            index for index, call in enumerate(order) if call == "pgrep"
        )

    def test_the_kill_deploy_handler_keeps_the_opposite_order(self):
        # A testnet fleet is scratch: its deploy is killed, not waited for, so
        # the session goes down first and the deploy with it.
        self._pgrep_stub(busy_calls=0)
        result = _bash(
            self.bin_dir,
            ". %s/issue-runs.sh && RUN_INTERRUPT_POLICY=kill-deploy "
            "SESSION_PID=4243 RUN_SKILL=run-testnet-from-issue "
            "_issue_runs_interrupted" % LIB,
        )
        assert result.returncode == 130
        order = self.order_log.read_text().split()
        assert order[0] == "pkill"

    def test_the_maintenance_driver_selects_that_policy(self):
        # And the testnet library keeps the opposite one: a half-built testnet
        # is scratch, so its deploy is killed and named.
        maintenance = (SCRIPTS / "run-maintenance-issues.sh").read_text()
        assert 'RUN_INTERRUPT_POLICY="never-signal"' in maintenance
        result = _bash(
            self.bin_dir,
            f". {LIB}/testnet-runs.sh && echo $RUN_SKILL $RUN_INTERRUPT_POLICY",
        )
        assert result.stdout.split() == ["run-testnet-from-issue", "kill-deploy"]


class TestMaintenanceDriverArguments:
    def test_a_malformed_issue_id_is_rejected(self, bin_dir):
        result = _bash(bin_dir, f"{SCRIPTS / 'run-maintenance-issues.sh'} not-an-issue")
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_no_argument_is_rejected(self, bin_dir):
        result = _bash(bin_dir, f"{SCRIPTS / 'run-maintenance-issues.sh'}")
        assert result.returncode == 2

    def test_a_well_formed_list_passes_validation(self, bin_dir):
        result = _bash(
            bin_dir,
            f". {LIB}/claude-session.sh && validate_issue_ids V2-1445,V2-1446 && "
            'echo "${ISSUES[@]}"',
        )
        assert result.stdout.strip() == "V2-1445 V2-1446"


class TestTestnetDriversAreUnchanged:
    """The testnet scripts keep their behaviour: same skill, same exit codes."""

    def test_the_wrapper_still_exports_the_old_function_names(self, bin_dir, tmp_path):
        log = tmp_path / "session.log"
        log.write_text("RUN COMPLETE V2-1404 PASS 629\n")
        result = _bash(
            bin_dir,
            f". {LIB}/claude-session.sh && . {LIB}/testnet-runs.sh && "
            f"testnet_run_verdict {log} V2-1404 && "
            "declare -F run_testnet_issues > /dev/null && echo has-run-testnet-issues",
        )
        assert result.stdout.split() == ["PASS", "has-run-testnet-issues"]

    @pytest.mark.parametrize("script", ["run-testnet-issues.sh", "run-testnet-chain.sh"])
    def test_they_still_reject_a_malformed_issue_id_with_exit_2(self, bin_dir, script):
        result = _bash(bin_dir, f"{SCRIPTS / script} nope")
        assert result.returncode == 2

    def test_the_stub_contract_is_json(self, bin_dir, tmp_path):
        # Guards the stub itself: `render` pipes the stream through jq, so a
        # stub emitting anything else would make every test above vacuous.
        counter = tmp_path / "invocations"
        counter.write_text("")
        _stub(bin_dir, "claude", _CLAUDE_STUB)
        result = _bash(
            bin_dir,
            "claude -p /x --session-id abc",
            env={
                "STUB_COUNTER": str(counter),
                "STUB_EXIT": "0",
                "STUB_RESULT_TEXT": "RUN COMPLETE V2-1445 PASS -",
            },
        )
        emitted = json.loads(result.stdout)
        assert emitted["session_id"] == "abc"
        assert emitted["result"] == "RUN COMPLETE V2-1445 PASS -"
