"""The pointer client service: classification, the ledger, and the metric shape.

The service runs on a VM and drives a binary that does not exist yet on this
machine, so what is testable here is everything around the spawn -- and that is
where the load-bearing decisions are. Every outcome class is derived from a
string `ant` prints, and getting one wrong does not fail: it silently moves a
routine event into the error bucket, or an error into a routine one.

The strings below are taken from ant-client branch `feat/pointers-immutable-owner`
(ant-core `data/client/pointer.rs`, ant-cli `commands/data/pointer.rs`), read on
2026-09-26.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent.parent / "saorsa_deploy" / "resources" / "scripts" / "pointer_client.py"
)


@pytest.fixture(scope="module")
def pc():
    """Import the workload script by path.

    It is deliberately import-free of `saorsa_deploy` (it is copied verbatim onto
    the VM), so it is not importable as a package module.
    """
    spec = importlib.util.spec_from_file_location("pointer_client", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def test_an_absent_pointer_is_not_found_not_an_error(pc):
    """`ant pointer get` exits 1 on an absent address, like any other failure.

    Mapping it to `error` would put an ANSWER -- the close group agreed there is
    nothing there -- into the bucket that means a fault, and the error rate would
    then track how many pointers had not replicated yet.
    """
    stderr = "Error: No pointer at 3f2a...c1\n"
    assert pc.classify_failure(stderr, write=False) == pc.OUTCOME_NOT_FOUND


def test_a_broken_chain_is_also_not_found(pc):
    """`pointer_resolve` reports a missing hop as "pointer chain breaks at"."""
    stderr = "Error: Pointer resolve failed: invalid data: pointer chain breaks at ab\n"
    assert pc.classify_failure(stderr, write=False) == pc.OUTCOME_NOT_FOUND


def test_a_pointer_that_moved_mid_flight_is_a_lost_write(pc):
    """ant-core's wording for a `Stale` PUT response. The client paid and the
    state did not land, which is not the same as an error."""
    stderr = (
        "Error: Pointer update failed: invalid data: the pointer moved while "
        "this update was in flight; the network now holds state 9c\n"
    )
    assert pc.classify_failure(stderr, write=True) == pc.OUTCOME_LOST_WRITE


def test_a_read_shortfall_is_its_own_class(pc):
    """`Error::CloseGroupShortfall` renders as "close-group PUT shortfall" for a
    READ too -- the variant predates pointers and its Display string still says
    PUT. Matching on the word PUT would put every read shortfall in the write
    bucket."""
    stderr = (
        "Error: Pointer get failed: close-group PUT shortfall (dial churn): "
        "only 2 of the close group answered for pointer ab\n"
    )
    assert pc.classify_failure(stderr, write=False) == pc.OUTCOME_READ_SHORTFALL


def test_an_uncorroborated_read_is_a_shortfall(pc):
    """States were offered but none by enough peers. The network declined to
    say, which is not a failure to answer."""
    stderr = (
        "Error: Pointer get failed: close-group PUT shortfall (dial churn): "
        "no state for pointer ab was named by 2 of the 5 peers that answered\n"
    )
    assert pc.classify_failure(stderr, write=False) == pc.OUTCOME_READ_SHORTFALL


def test_a_write_that_fell_short_of_its_quorum_is_a_lost_write(pc):
    """Same error, opposite side: the write paid and did not reach enough peers."""
    stderr = (
        "Error: Pointer create failed: close-group PUT shortfall (dial churn): "
        "pointer ab stored on 3 of 5 close-group peers\n"
    )
    assert pc.classify_failure(stderr, write=True) == pc.OUTCOME_LOST_WRITE


def test_anything_else_is_an_error(pc):
    assert pc.classify_failure("Error: connection refused\n", write=True) == (pc.OUTCOME_ERROR)
    assert pc.classify_failure("", write=False) == pc.OUTCOME_ERROR


def test_the_error_summary_is_the_error_line(pc):
    stderr = "2026-09-26T10:00:00Z TRACE noise\nError: Pointer get failed: boom\n"
    assert pc.error_summary(stderr) == "Error: Pointer get failed: boom"
    # No `Error:` line at all -- take the last thing said rather than nothing.
    assert pc.error_summary("something went wrong\n") == "something went wrong"
    assert pc.error_summary("   \n") == ""


# ---------------------------------------------------------------------------
# Read-back classification against the ledger
# ---------------------------------------------------------------------------


class _Client:
    """Just enough of PointerClient to exercise `classify_read`."""

    def __init__(self, pc):
        self.classify_read = pc.PointerClient.classify_read.__get__(self)


def test_a_lower_counter_is_stale_and_a_higher_one_is_an_anomaly(pc):
    client = _Client(pc)
    assert client.classify_read(5, "aa", {"counter": 4}) == pc.OUTCOME_STALE
    # Only a write by something else to a pointer we own could raise the counter
    # above what we wrote, so this is never routine. The counter decides before
    # the target is even looked at.
    assert client.classify_read(5, "aa", {"counter": 6}) == pc.OUTCOME_ANOMALY
    assert client.classify_read(5, "aa", {"counter": 5, "target": "aa"}) == (pc.OUTCOME_OK)


def test_the_same_counter_with_a_different_target_is_stale(pc):
    """Two states at one counter converge on the smaller target, so the one we
    wrote lost the merge. The write did not stick, which is what stale means."""
    client = _Client(pc)
    assert client.classify_read(5, "aa", {"counter": 5, "target": "bb"}) == (pc.OUTCOME_STALE)
    assert client.classify_read(5, "aa", {"counter": 5, "target": "aa"}) == (pc.OUTCOME_OK)


def test_a_missing_counter_is_an_error_not_a_pass(pc):
    client = _Client(pc)
    assert client.classify_read(5, "aa", {}) == pc.OUTCOME_ERROR


def test_no_expected_counter_means_nothing_to_be_wrong_about(pc):
    """A foreign pointer read for the first time has no expected state."""
    client = _Client(pc)
    assert client.classify_read(None, None, {"counter": 3}) == pc.OUTCOME_OK


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_the_ledger_survives_a_restart(pc, tmp_path):
    """`Restart=always`, and ownership is immutable: a restart that forgot the
    owner keys would abandon every pointer it had already paid for."""
    path = tmp_path / "ledger.json"
    ledger = pc.Ledger(path)
    ledger.note_key("aa", str(tmp_path / "aa.key"))
    ledger.record_own("aa", str(tmp_path / "aa.key"), 0, "target", "chunk")
    ledger.note_foreign("bb", 7)

    reloaded = pc.Ledger(path)
    assert reloaded.own["aa"]["counter"] == 0
    assert reloaded.own["aa"]["key"].endswith("aa.key")
    assert reloaded.foreign["bb"]["counter"] == 7


def test_a_key_is_recorded_before_the_create_that_pays_for_it(pc, tmp_path):
    """A key written and not recorded is a key the service never looks at again,
    so a create that crashes after paying would strand a paid-for pointer."""
    ledger = pc.Ledger(tmp_path / "ledger.json")
    ledger.note_key("aa", "/k/aa.key")
    assert ledger.own["aa"]["counter"] is None
    # Not yet established: `update` and `sweep` must not touch a pointer whose
    # create has not returned.
    assert ledger.established_own() == []
    ledger.record_own("aa", "/k/aa.key", 0, "t", "chunk")
    assert ledger.established_own() == ["aa"]


def test_note_key_never_overwrites_an_existing_entry(pc, tmp_path):
    ledger = pc.Ledger(tmp_path / "ledger.json")
    ledger.record_own("aa", "/k/aa.key", 4, "t", "chunk")
    ledger.note_key("aa", "/k/other.key")
    assert ledger.own["aa"]["counter"] == 4
    assert ledger.own["aa"]["key"] == "/k/aa.key"


def test_a_foreign_pointer_is_registered_once(pc, tmp_path):
    ledger = pc.Ledger(tmp_path / "ledger.json")
    assert ledger.add_foreign("bb") is True
    assert ledger.add_foreign("bb") is False
    # A pointer we own is never added as foreign: its expected state is known,
    # and treating it as foreign would silently stop checking it.
    ledger.record_own("aa", "/k", 0, "t", "chunk")
    assert ledger.add_foreign("aa") is False


def test_a_truncated_ledger_does_not_stop_the_service(pc, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text('{"own": {"aa":')
    ledger = pc.Ledger(path)
    assert ledger.own == {} and ledger.foreign == {}


# ---------------------------------------------------------------------------
# Published address lists and targets
# ---------------------------------------------------------------------------


def test_published_lines_parse_with_or_without_a_size(pc):
    """Uploaders publish `<address> <bytes>`; pointer clients publish a bare
    address. Both are read here, so a real data map can be used as a target."""
    address = "a" * 64
    assert pc.parse_address_lines(f"{address} 1024\n") == [address]
    assert pc.parse_address_lines(f"{address}\n") == [address]
    # Anything that is not a 32-byte hex address is not an address.
    assert pc.parse_address_lines("nonsense\n# comment\n\n") == []


def test_a_pointer_key_is_told_apart_from_an_upload_key(pc):
    """Both tiers publish under one prefix. A pointer address fetched as a file
    fails every time, and a file address read back as a pointer likewise."""
    assert pc.is_pointer_key("p/DEV-01/pointer-lon1-1.txt") is True
    assert pc.is_pointer_key("p/DEV-01/upload-lon1-1.txt") is False
    assert pc.is_pointer_key("p/DEV-01/wasm-upload-lon1-1.txt") is False


def test_the_target_falls_back_to_a_random_address(pc):
    """So the role never depends on the uploader tier. A node stores 33 opaque
    bytes and never reads the target, so a synthetic one exercises the same
    write path -- only `resolve` can tell the difference."""
    pool = pc.AddressPool("", "", "eu-west-2")
    target = pool.random_chunk_target()
    assert len(target) == 64
    int(target, 16)  # hex


def test_a_populated_pool_supplies_a_real_target(pc):
    pool = pc.AddressPool("bucket", "prefix", "eu-west-2")
    pool.chunk_targets = ["b" * 64]
    assert pool.random_chunk_target() == "b" * 64


# ---------------------------------------------------------------------------
# The Influx line
# ---------------------------------------------------------------------------


def test_the_line_carries_no_timestamp(pc):
    """Deliberate: testnet manifests skew client clocks, and a client-supplied
    timestamp hours in the future is hidden behind every read query's implicit
    `stop: now()`. The client's own view survives in `start_ns`."""
    line = pc.build_line("h", "lon1", "create", {"ok": True, "start_ns": 1})
    assert line.startswith("pointer,host=h,region=lon1,op=create ")
    # No trailing whitespace-separated integer timestamp.
    assert line.count(" ") == 1


def test_field_types_are_explicit_and_nulls_are_omitted(pc):
    line = pc.build_line(
        "h",
        "lon1",
        "sweep",
        {
            "outcome": "stale",
            "ok": False,
            "owned": True,
            "counter_expected": 4,
            "counter_observed": None,
            "chain": 0.25,
        },
    )
    fields = line.split(" ", 1)[1]
    assert 'outcome="stale"' in fields
    assert "ok=0i" in fields
    assert "owned=1i" in fields
    assert "counter_expected=4i" in fields
    # A null field is dropped, never written as 0: "not measured" and "zero" are
    # different, and Influx has no null.
    assert "counter_observed" not in fields
    assert "chain=0.250000" in fields


def test_a_quote_in_a_field_is_escaped(pc):
    line = pc.build_line("h", "lon1", "get", {"error": 'Error: said "no"'})
    assert '\\"no\\"' in line


def test_a_tag_with_a_comma_is_escaped(pc):
    line = pc.build_line("h,1", "lon 1", "get", {"ok": True})
    assert "host=h\\,1" in line
    assert "region=lon\\ 1" in line


# ---------------------------------------------------------------------------
# The ant invocation
# ---------------------------------------------------------------------------


def test_an_empty_rust_log_is_unset_rather_than_exported(pc, monkeypatch):
    """systemd always exports RUST_LOG, and an EMPTY one sets ant's EnvFilter to
    a filter that silences everything -- overriding the `-v` that installs the
    subscriber in the first place. It has to be REMOVED."""
    monkeypatch.setenv("RUST_LOG", "leftover")
    runner = pc.AntRunner("/bin/true", [], "arbitrum-sepolia", "", 10)
    assert "RUST_LOG" not in runner._env()
    runner = pc.AntRunner("/bin/true", [], "arbitrum-sepolia", "ant=debug", 10)
    assert runner._env()["RUST_LOG"] == "ant=debug"


def test_a_json_result_is_read_from_the_end_of_stdout(pc):
    result = pc.AntResult(0, 'noise\n{"a":1}\n{"address":"ab"}\n', "", 5)
    assert result.json() == {"address": "ab"}
    assert pc.AntResult(0, "no json here", "", 5).json() == {}
    assert pc.AntResult(0, "{bad json}", "", 5).json() == {}


def test_the_two_flag_groups_are_independent(pc):
    """`wallet balance` needs `--evm-network` and NO peers.

    Collapsing the two into one switch left the balance read on ant's DEFAULT
    network -- Arbitrum One -- so every write's cost would have been measured
    against a mainnet wallet holding nothing and recorded as a spend of zero.
    """
    import subprocess

    runner = pc.AntRunner("/bin/echo", ["--bootstrap", "1.2.3.4:10000"], "net", "", 10)
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return R()

    original = subprocess.run
    subprocess.run = fake_run
    try:
        runner.run(["wallet", "balance"], bootstrap=False)
        runner.run(["pointer", "keygen", "--output", "k"], bootstrap=False, evm=False)
        runner.run(["pointer", "get", "ab"])
    finally:
        subprocess.run = original

    balance, keygen, get = captured
    assert "--evm-network" in balance and "--bootstrap" not in balance
    assert "--evm-network" not in keygen and "--bootstrap" not in keygen
    assert "--evm-network" in get and "--bootstrap" in get


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_the_balance_read_sends_a_user_agent(pc):
    """The public Arbitrum Sepolia endpoint answers 403 Forbidden to urllib's
    default `Python-urllib/3.x` UA. Measured on the live verification run: every
    gas measurement failed and every write recorded no ETH cost. Any other UA
    gets a 200."""
    import urllib.request

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        raise urllib.error.URLError("stop here")

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        wallet = pc.Wallet.__new__(pc.Wallet)
        wallet.rpc_url = "https://sepolia-rollup.arbitrum.io/rpc"
        wallet.address = "0x" + "ab" * 20
        assert wallet.gas_wei() is None
    finally:
        urllib.request.urlopen = original
    assert seen["ua"] == pc.RPC_USER_AGENT
    assert "urllib" not in seen["ua"].lower()


def test_publishing_writes_the_addresses_even_with_no_presigned_url(pc, tmp_path):
    """The local file is what a restarted client re-publishes from, so it is
    written whether or not the flush can go out."""
    path = tmp_path / "pointer-lon1-1.txt"
    assert pc.publish(["bb", "aa"], path, "") is False
    # Sorted, so a re-publish of an unchanged set is byte-identical.
    assert path.read_text() == "aa\nbb\n"


def test_the_script_is_valid_python_and_standard_library_only(pc):
    """It is copied verbatim onto a VM that has no third-party packages."""
    source = SCRIPT.read_text()
    third_party = ("import requests", "import boto3", "from pyinfra", "import yaml")
    for marker in third_party:
        assert marker not in source, marker
    assert "import saorsa_deploy" not in source
    json.loads("{}")  # the script's own json usage is the stdlib's
