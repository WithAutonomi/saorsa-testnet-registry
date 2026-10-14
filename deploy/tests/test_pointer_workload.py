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
import os
import time
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


# ---------------------------------------------------------------------------
# The handover loop (V2-1354)
# ---------------------------------------------------------------------------


def test_a_take_back_refusal_is_classified_apart_from_an_error(pc):
    """`Error::PointerFinal`. Both the `update` and the second `transfer` on a
    handed-over pointer fail with it, BEFORE paying, which is the property the
    take-back checks exist to demonstrate."""
    stderr = (
        "Error: Pointer update failed: invalid data: pointer is final: "
        "counter 18446744073709551615\n"
    )
    assert pc.classify_failure(stderr, write=True, handover=True) == pc.OUTCOME_ANOMALY
    # And nothing changes for the ordinary ops: the same words on a plain update
    # would mean something else, so the routing is handover-only.
    assert pc.classify_failure(stderr, write=True) == pc.OUTCOME_ERROR


def test_a_rival_final_state_is_an_anomaly(pc):
    """Nothing of OURS finalized the pointer, so something else ended its life.
    That is never routine, which is what `anomaly` means here.

    `rival_final` raises `Error::PointerFinal` for a single rival state, whose
    Display is `pointer is final: {0}` — the same prefix a take-back refusal
    carries, which is why one marker covers both.
    """
    stderr = (
        "Error: pointer is final: pointer de is already final on state 9c held by 4 of 5 peers\n"
    )
    assert pc.classify_failure(stderr, write=True, handover=True) == pc.OUTCOME_ANOMALY


def test_a_forked_pointer_is_an_anomaly_and_not_an_error(pc):
    """`rival_final` and `final_already` raise `Error::PointerForked` when the
    close group holds MORE THAN ONE final state, and its Display shares no
    wording with `PointerFinal`'s: "pointer is forked: pointer {a} holds {n}
    different final states". Matching anything but the error's own prefix filed
    every fork — the one genuinely alarming reading — as a plain error.
    """
    stderr = "Error: pointer is forked: pointer de holds 2 different final states\n"
    assert pc.classify_failure(stderr, write=True, handover=True) == pc.OUTCOME_ANOMALY
    assert pc.POINTER_FORKED_MARKER in stderr
    # And the plain ops are untouched, as for every other handover marker.
    assert pc.classify_failure(stderr, write=True) == pc.OUTCOME_ERROR


def test_a_silent_close_group_peer_refuses_the_transfer_before_paying(pc):
    """ "only N of the G peers of pointer A's close group answered, and a silent
    one could hold a final state; a transfer cannot be taken back, so try again
    when all of them can be asked". The guard working, not a failed handover --
    which is why these are excluded from the completion denominator."""
    stderr = (
        "Error: Pointer transfer failed: only 4 of the 5 peers of pointer ab's "
        "close group answered, and a silent one could hold a final state; a "
        "transfer cannot be taken back, so try again when all of them can be "
        "asked\n"
    )
    assert pc.classify_failure(stderr, write=True, handover=True) == (pc.OUTCOME_READ_SHORTFALL)


def test_a_take_back_on_a_forked_pointer_still_reads_as_refused(pc, tmp_path, monkeypatch):
    """`final_already` answers an update on a FORKED pointer with
    `pointer is forked`, not `pointer is final`. A check that accepted only the
    latter called the refusal an error — on precisely the pointer whose state is
    most worth knowing about.
    """
    client = _stub_client(pc, tmp_path, monkeypatch)
    handed_over = client.do_create()
    client.do_transfer()
    key = client.ledger.own[handed_over]["key"]

    # The close group now reports two rival final states.
    state_path = tmp_path / "stub-state.json"
    stub_state = json.loads(state_path.read_text())
    stub_state["forked"] = True
    state_path.write_text(json.dumps(stub_state))

    client.influx = _CaptureSink()
    client.check_takeback(
        handed_over,
        ["pointer", "update", "--key", key, "--kind", "chunk", "a" * 64],
        attempt="update",
    )
    point = client.influx.points()[0]
    assert point["outcome"] == "ok"
    assert "pointer is forked" in point["error"]
    assert point["wallet_spend_atto"] == "0" and point["gas_spend_wei"] == "0"


def test_a_forked_finality_check_reports_the_holders_of_OUR_state(pc, tmp_path, monkeypatch):
    """`holders` sits inside the state object, and on a fork there are several.

    Reading it off the top level of `finality_json` returned None for every real
    reply, which left the column dead. With the state id the transfer reported,
    the count is our side of the fork — not the other side's, and not a guess.
    """
    client = _stub_client(pc, tmp_path, monkeypatch)
    handed_over = client.do_create()
    client.do_transfer()

    state_path = tmp_path / "stub-state.json"
    stub_state = json.loads(state_path.read_text())
    stub_state["forked"] = True
    state_path.write_text(json.dumps(stub_state))

    client.influx = _CaptureSink()
    client.do_sweep()
    finality = [p for p in client.influx.points() if p["op"] == "finality"]
    assert finality, "the sweep must re-check every own handover"
    point = finality[0]
    assert point["outcome"] == "forked"
    assert point["finality_status"] == "forked"
    # The stub gives our state 3 holders and the rival 2.
    assert point["holders"] == "3"
    assert point["answered"] == "5" and point["group"] == "5"
    assert point["address"] == handed_over


def test_the_final_counter_is_u64_max(pc):
    """It is also how a redirect read is identified in the recorded rows: no
    other operation ever writes this counter."""
    assert pc.FINAL_COUNTER == 2**64 - 1


def test_every_finality_status_maps_to_an_outcome_and_an_unknown_one_errors(pc):
    """An unknown status must not read as a clean handover: a future ant-client
    adding one would otherwise silently pass the gate."""
    assert pc.FINALITY_OUTCOMES["final"] == pc.OUTCOME_OK
    assert pc.FINALITY_OUTCOMES["open"] == pc.OUTCOME_STALE
    assert pc.FINALITY_OUTCOMES["settling"] == pc.OUTCOME_SETTLING
    assert pc.FINALITY_OUTCOMES["unconfirmed"] == pc.OUTCOME_UNCONFIRMED
    assert pc.FINALITY_OUTCOMES["forked"] == pc.OUTCOME_FORKED
    assert pc.FINALITY_OUTCOMES.get("teleported") is None


def test_the_finality_status_is_read_from_either_shape(pc):
    """`finality` reports an object; `transfer` carries one alongside
    `finality_error`. Accepting a bare string costs nothing and means a shape
    change does not silently drop the field."""
    assert pc._finality_status({"status": "final"}) == "final"
    assert pc._finality_status("settling") == "settling"
    assert pc._finality_status({}) is None
    assert pc._finality_status(None) is None


def test_a_handed_over_pointer_is_excluded_from_the_update_candidates(pc, tmp_path):
    """An `update` on it is refused before paying, so keeping it in the candidate
    list would spend a cycle on a certain refusal and record it as a failed
    write. The RECIPIENT stays updatable: it is an ordinary owned pointer."""
    ledger = pc.Ledger(tmp_path / "ledger.json")
    ledger.record_own("aa", "/k/aa.key", 3, "t", "chunk")
    ledger.record_own("bb", "/k/bb.key", 0, "t", "chunk")
    assert sorted(ledger.updatable_own()) == ["aa", "bb"]
    ledger.mark_transferred("aa", "bb")
    assert ledger.updatable_own() == ["bb"]
    # Still published and still swept -- it is a readable pointer, and the final
    # state is exactly what every reader should now see there.
    assert sorted(ledger.established_own()) == ["aa", "bb"]


def test_a_handover_moves_the_expected_state_onto_the_final_one(pc, tmp_path):
    """Every subsequent read -- the redirect read, every sweep, and every other
    client's sweep -- is checked against the final state rather than the state
    the pointer last held."""
    ledger = pc.Ledger(tmp_path / "ledger.json")
    ledger.record_own("aa", "/k/aa.key", 3, "oldtarget", "chunk")
    ledger.mark_transferred("aa", "bb")
    assert ledger.own["aa"]["counter"] == pc.FINAL_COUNTER
    assert ledger.own["aa"]["target"] == "bb"
    assert ledger.own["aa"]["target_kind"] == "pointer"
    assert ledger.transferred["aa"]["recipient"] == "bb"
    # Durable: a handover is irreversible, so a restart that forgot one would go
    # on trying to update a pointer that can never accept another write.
    reloaded = pc.Ledger(tmp_path / "ledger.json")
    assert reloaded.transferred["aa"]["recipient"] == "bb"
    assert reloaded.updatable_own() == []


def test_a_foreign_handover_is_registered_once_and_survives_a_restart(pc, tmp_path):
    ledger = pc.Ledger(tmp_path / "ledger.json")
    assert ledger.add_foreign_transfer("aa", "bb") is True
    assert ledger.add_foreign_transfer("aa", "bb") is False
    ledger.save()
    assert pc.Ledger(tmp_path / "ledger.json").foreign_transfers["aa"]["recipient"] == "bb"
    # Our OWN handover read back off the shared prefix is not a foreign one.
    ledger.mark_transferred("cc", "dd")
    assert ledger.add_foreign_transfer("cc", "dd") is False


def test_a_transfers_file_is_parsed_as_two_columns(pc):
    """NOT with `parse_address_lines`, which takes the first field: that would
    read the old address as a plain pointer and throw the recipient away, leaving
    a reader with an address whose expected state it does not know."""
    old, new = "a" * 64, "b" * 64
    assert pc.parse_transfer_lines(f"{old} {new}\n") == [(old, new)]
    # A one-column line is skipped rather than half-read.
    assert pc.parse_transfer_lines(f"{old}\n") == []
    assert pc.parse_transfer_lines("# comment\n\nnonsense x\n") == []


def test_a_transfers_key_is_told_apart_from_an_address_key(pc):
    """It also matches `is_pointer_key` (the name starts `pointer-`), so it has
    to be tested FIRST wherever the two are dispatched on."""
    assert pc.is_pointer_transfers_key("p/DEV-01/pointer-transfers-lon1-1.txt") is True
    assert pc.is_pointer_transfers_key("p/DEV-01/pointer-lon1-1.txt") is False
    # The overlap that makes the ordering load-bearing.
    assert pc.is_pointer_key("p/DEV-01/pointer-transfers-lon1-1.txt") is True


def test_the_file_consumers_already_skip_the_transfers_object():
    """Both file downloaders must ignore `pointer-transfers-*`, or a handover
    pair would be fetched as content and its failures would land in the download
    success rate as lost content.

    Neither needed a change: the WASM downloader excludes every `pointer-`
    prefixed key (`resources/wasm-client/src/addresses.ts`, `isNonFileKey`) and
    `downloader.sh` filters the listing to `^upload-` (line with
    `grep '^upload-'`). This test is what keeps that true.
    """
    resources = Path(__file__).parent.parent / "saorsa_deploy" / "resources"
    wasm = (resources / "wasm-client" / "src" / "addresses.ts").read_text()
    assert 'name.startsWith("pointer-")' in wasm
    shell = (resources / "scripts" / "downloader.sh").read_text()
    assert "grep '^upload-'" in shell


# ---------------------------------------------------------------------------
# One full handover cycle against a stub `ant`
# ---------------------------------------------------------------------------

#: A stub `ant` that keeps pointer state in a JSON file, so a whole handover
#: cycle can be driven without the real binary. It implements only what the
#: client invokes, and the one behaviour the cycle is ABOUT: a pointer carrying
#: a final state refuses both `update` and a second `transfer`, exits non-zero,
#: and does not move the wallet.
#:
#: **Its JSON and its error strings are the real ones**, taken from ant-client
#: `70052d0b` (`ant-cli/src/commands/data/pointer.rs` `finality_json` /
#: `final_state_json` / `print_transfer`, and ant-core `data/error.rs` /
#: `data/client/pointer.rs`). That matters more here than anywhere else in this
#: file: a stub that printed the shape the script expects rather than the shape
#: the CLI emits would make the whole suite agree with itself and with nothing
#: else. The three shapes worth naming, because each one hid a bug:
#:
#: * `transfer` wraps `finality_json` under its OWN `finality` key, so the
#:   status is at `finality.finality.status` -- one level deeper than on the
#:   `finality` command, which prints `finality_json` as its whole document.
#: * `holders` lives inside the state object (`finality.state.holders`, or one
#:   per entry of `finality.states` on a fork), never at the top level.
#: * A forked pointer is refused with `pointer is forked: ...`, which shares no
#:   wording with `pointer is final: ...`.
_STUB_ANT = r"""#!/usr/bin/env python3
import json, os, secrets, sys

STATE = os.environ["ANT_STUB_STATE"]


def load():
    try:
        return json.loads(open(STATE).read())
    except OSError:
        return {"ant": 10**21, "gas": 10**18, "pointers": {}}


def save(state):
    open(STATE, "w").write(json.dumps(state))


def spend(state):
    state["ant"] -= 10**15
    state["gas"] -= 10**12


FINAL = 18446744073709551615


def final_state_json(entry, holders):
    # `final_state_json`: holders live in HERE, not at the top level.
    return {
        "state_id": entry.get("state_id", "5a" * 32),
        "kind": "pointer",
        "target": entry["target"],
        "holders": holders,
        "transferred_to": entry["target"],
    }


def finality_json(address, entry, state):
    # `finality_json`: {address, group, answered, finality: <status>}.
    group = state.get("group", 5)
    if entry["counter"] != FINAL:
        status = {"status": "open", "counter": entry["counter"]}
    elif state.get("forked"):
        status = {
            "status": "forked",
            "states": [
                final_state_json(entry, 3),
                final_state_json({**entry, "state_id": "bb" * 32}, 2),
            ],
            "majority": None,
        }
    else:
        status = {
            "status": state.get("status", "final"),
            "state": final_state_json(entry, state.get("holders", 5)),
        }
    return {"address": address, "group": group, "answered": group, "finality": status}


def refusal(address, entry):
    # `final_already` / `rival_final`: PointerForked on a fork, else PointerFinal.
    if state.get("forked"):
        return "pointer is forked: pointer " + address + " holds 2 different final states"
    return (
        "pointer is final: pointer "
        + address
        + " has final state "
        + entry.get("state_id", "5a" * 32)
        + " on 5 of 5 peers; an update cannot replace it"
    )

args = [a for a in sys.argv[1:]]
out = []
skip = 0
for i, a in enumerate(args):
    if skip:
        skip -= 1
        continue
    if a in ("--bootstrap", "--evm-network"):
        skip = 1
        continue
    if a in ("-v", "--json"):
        continue
    out.append(a)
args = out
state = load()


def fail(message):
    print("Error: " + message, file=sys.stderr)
    sys.exit(1)


if args[:2] == ["wallet", "address"]:
    if os.environ.get("ANT_STUB_BALANCE_FAILS"):
        fail("wallet unavailable")
    print("0x" + "ab" * 20)
elif args[:2] == ["wallet", "balance"]:
    if os.environ.get("ANT_STUB_BALANCE_FAILS"):
        fail("wallet unavailable")
    print(str(state["ant"]))
elif args[:2] == ["pointer", "keygen"]:
    path = args[args.index("--output") + 1]
    address = secrets.token_hex(32)
    open(path, "w").write(address)
    print(json.dumps({"address": address}))
elif args[:2] == ["pointer", "create"]:
    address = open(args[args.index("--key") + 1]).read().strip()
    target = args[-1]
    state["pointers"][address] = {"counter": 0, "target": target, "kind": "chunk"}
    spend(state)
    save(state)
    print(json.dumps({"address": address, "counter": 0, "target": target, "kind": "chunk"}))
elif args[:2] == ["pointer", "update"]:
    address = open(args[args.index("--key") + 1]).read().strip()
    entry = state["pointers"].get(address)
    if entry is None:
        fail("No pointer at " + address)
    if entry["counter"] == FINAL:
        # `final_already`: PointerForked on a forked pointer, PointerFinal
        # otherwise. The two share no wording.
        fail(refusal(address, entry))
    entry["counter"] += 1
    entry["target"] = args[-1]
    entry["kind"] = args[args.index("--kind") + 1]
    spend(state)
    save(state)
    print(json.dumps({"address": address, **entry}))
elif args[:2] == ["pointer", "transfer"]:
    address = open(args[args.index("--key") + 1]).read().strip()
    recipient = args[-1]
    entry = state["pointers"].get(address)
    if entry is None:
        fail("No pointer at " + address)
    if entry["counter"] == FINAL:
        # `rival_final`, same two errors as the update path.
        fail(refusal(address, entry))
    entry.update({"counter": FINAL, "target": recipient, "kind": "pointer"})
    entry["state_id"] = "5a" * 32
    spend(state)
    save(state)
    # `print_transfer`: the finality check rides under its own key, as the whole
    # `finality_json` document, so its status is at `finality.finality.status`.
    print(
        json.dumps(
            {
                "address": address,
                "state": entry["state_id"],
                "finality": finality_json(address, entry, state),
                "finality_error": None,
            }
        )
    )
elif args[:2] == ["pointer", "get"]:
    entry = state["pointers"].get(args[2])
    if entry is None:
        fail("No pointer at " + args[2])
    print(json.dumps({"address": args[2], **entry}))
elif args[:2] == ["pointer", "finality"]:
    entry = state["pointers"].get(args[2])
    if entry is None:
        fail("No pointer at " + args[2])
    print(json.dumps(finality_json(args[2], entry, state)))
elif args[:2] == ["pointer", "controller"]:
    address, hops = args[2], 0
    while True:
        entry = state["pointers"].get(address)
        if entry is None or entry["counter"] != FINAL or hops > 15:
            break
        address, hops = entry["target"], hops + 1
    print(json.dumps({"pointer": address, "transfers": hops, "frozen": False}))
elif args[:2] == ["pointer", "resolve"]:
    address, hops = args[2], 0
    while hops <= 15:
        entry = state["pointers"].get(address)
        if entry is None:
            fail("Pointer resolve failed: invalid data: pointer chain breaks at " + address)
        if entry["kind"] != "pointer":
            print(json.dumps({"target": entry["target"], "kind": entry["kind"]}))
            break
        address, hops = entry["target"], hops + 1
else:
    fail("stub ant does not implement " + " ".join(args))
"""


class _CaptureSink:
    """Stands in for InfluxSink, keeping the lines the client would have posted."""

    def __init__(self):
        self.lines = []

    @property
    def enabled(self):
        return True

    def write(self, line):
        self.lines.append(line)

    def points(self):
        out = []
        for line in self.lines:
            tags, fields = line.split(" ", 1)
            point = {"op": tags.split("op=", 1)[1]}
            for pair in fields.split(","):
                key, value = pair.split("=", 1)
                point[key] = value.strip('"').rstrip("i")
            out.append(point)
        return out


def _stub_client(pc, tmp_path, monkeypatch, *, balance_fails=False):
    """A PointerClient wired to the stub `ant`, with the RPC and Influx faked."""
    ant = tmp_path / "ant-stub"
    ant.write_text(_STUB_ANT)
    ant.chmod(0o755)
    state_path = tmp_path / "stub-state.json"
    state_path.write_text(json.dumps({"ant": 10**21, "gas": 10**18, "pointers": {}}))

    monkeypatch.setenv("ANT_STUB_STATE", str(state_path))
    if balance_fails:
        monkeypatch.setenv("ANT_STUB_BALANCE_FAILS", "1")
    monkeypatch.setenv("ANT", str(ant))
    monkeypatch.setenv("POINTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REGION", "lon1")
    monkeypatch.setenv("CLIENT_INDEX", "1")
    monkeypatch.setenv("EVM_NETWORK", "arbitrum-sepolia")
    monkeypatch.setenv("RPC_URL", "http://rpc.invalid/")
    monkeypatch.setenv("TRANSFER_INTERVAL_SECS", "300")
    # No address pool and no presigned URLs: the published objects are written
    # locally and nothing goes out, which is what keeps the test offline.
    monkeypatch.delenv("ADDRESSES_S3_BUCKET", raising=False)
    monkeypatch.delenv("UPLOAD_PRESIGNED_URL", raising=False)
    monkeypatch.delenv("TRANSFERS_PRESIGNED_URL", raising=False)
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("RUST_LOG", raising=False)

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        # The only network call left is the gas balance, served from the same
        # state file the stub `ant` spends from.
        gas = json.loads(state_path.read_text())["gas"]
        return _Resp(json.dumps({"result": hex(gas)}).encode())

    monkeypatch.setattr(pc.urllib.request, "urlopen", fake_urlopen)
    client = pc.PointerClient()
    client.influx = _CaptureSink()
    return client


def test_one_full_handover_cycle_emits_one_point_per_step(pc, tmp_path, monkeypatch):
    """The whole loop, against a stub `ant`.

    Nine points: the recipient create and its read-back, the transfer, the
    finality check, the redirect read, the controller walk, the resolve, and the
    two take-backs. Each step is its own point so a failure is attributable to
    the step it happened in rather than to "the handover".
    """
    client = _stub_client(pc, tmp_path, monkeypatch)
    # The pointer to be handed over, created exactly as the create loop does.
    handed_over = client.do_create()
    assert handed_over is not None
    client.influx = _CaptureSink()

    client.do_transfer()
    points = client.influx.points()
    assert [p["op"] for p in points] == [
        "get",
        "create",
        "transfer",
        "finality",
        "get",
        "controller",
        "resolve",
        "takeback",
        "takeback",
    ]

    transfer = points[2]
    assert transfer["outcome"] == "ok"
    assert transfer["address"] == handed_over
    recipient = transfer["recipient"]
    assert transfer["finality_status"] == "final"
    # `u64::MAX` does not fit an Influx integer field, so it travels as a string
    # under its own name. The point would be DROPPED outright otherwise.
    assert "counter_expected" not in transfer
    assert transfer["counter_expected_u64"] == str(pc.FINAL_COUNTER)
    # It paid, and the spend was measured on both sides.
    assert transfer["paid_attempts"] == "1"
    assert int(transfer["wallet_spend_atto"]) > 0
    assert int(transfer["gas_spend_wei"]) > 0

    finality = points[3]
    assert finality["outcome"] == "ok"
    assert (finality["answered"], finality["group"], finality["holders"]) == ("5", "5", "5")

    redirect = points[4]
    assert redirect["outcome"] == "ok"
    assert redirect["recipient"] == recipient
    assert redirect["counter_expected_u64"] == str(pc.FINAL_COUNTER)
    assert redirect["counter_observed_u64"] == str(pc.FINAL_COUNTER)

    assert points[5]["outcome"] == "ok"  # controller followed the hop
    assert points[5]["recipient"] == recipient
    assert points[6]["outcome"] == "ok"  # resolve reached the recipient's chunk

    takebacks = {p["attempt"]: p for p in points[7:]}
    assert set(takebacks) == {"update", "transfer"}
    for attempt, point in takebacks.items():
        # Refused, and it cost nothing. Both halves are the assertion.
        assert point["outcome"] == "ok", attempt
        assert point["wallet_spend_atto"] == "0", attempt
        assert point["gas_spend_wei"] == "0", attempt

    # The ledger moved the expected state onto the final one, and the handover
    # was published for the other clients to read.
    assert client.ledger.transferred[handed_over]["recipient"] == recipient
    assert client.ledger.updatable_own() == [recipient]
    assert client.transfers_file.read_text() == f"{handed_over} {recipient}\n"


def test_a_take_back_whose_balance_cannot_be_read_is_an_error(pc, tmp_path, monkeypatch):
    """Unknown is not zero. Reading an unreadable balance as zero would turn an
    unmeasured take-back into a pass, which is the one outcome that must be
    earned."""
    client = _stub_client(pc, tmp_path, monkeypatch)
    handed_over = client.do_create()
    client.do_transfer()
    key = client.ledger.own[handed_over]["key"]

    # The wallet read now fails, and the take-back is refused exactly as before.
    monkeypatch.setenv("ANT_STUB_BALANCE_FAILS", "1")
    client.influx = _CaptureSink()
    client.check_takeback(
        handed_over, ["pointer", "transfer", "--key", key, "x" * 64], attempt="transfer"
    )
    point = client.influx.points()[0]
    assert point["outcome"] == "error"
    assert point["attempt"] == "transfer"
    # The refusal itself still happened -- it is the MEASUREMENT that is missing.
    assert "pointer is final" in point["error"]


def test_a_cycle_with_nothing_to_hand_over_emits_nothing(pc, tmp_path, monkeypatch):
    """Not an error, and no point: the create loop has simply not produced an
    untransferred pointer yet. A point here would be a handover attempt that
    never attempted anything."""
    client = _stub_client(pc, tmp_path, monkeypatch)
    client.do_transfer()
    assert client.influx.points() == []


def test_the_script_runs_under_python_isolated_mode(pc, tmp_path, monkeypatch):
    """`python3 -I`: no site-packages, no cwd on sys.path.

    It is copied verbatim onto a VM and run by the `ant-pointer` unit, so a
    stdlib-only script that happens to import something from the working
    directory would work here and fail there.
    """
    import subprocess
    import sys

    ant = tmp_path / "ant-stub"
    ant.write_text(_STUB_ANT)
    ant.chmod(0o755)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "ANT_STUB_STATE": str(tmp_path / "stub-state.json"),
        "ANT": str(ant),
        "POINTER_DATA_DIR": str(tmp_path / "data"),
        "REGION": "lon1",
        "CLIENT_INDEX": "1",
        "TRANSFER_INTERVAL_SECS": "300",
        # No RPC and no Influx, so the only subprocess it spawns is the stub.
        "RPC_URL": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-I", str(SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "Pointer client starting" in line:
                assert "transfer/300s" in line
                return
            if proc.poll() is not None:
                raise AssertionError(f"exited {proc.returncode}: {proc.stderr.read()[:2000]}")
        raise AssertionError("no startup line within 30s")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_counter_that_fits_an_i64_stays_an_integer_field(pc):
    """The routing applies to the handover counter and nothing else: every
    existing point must render byte-identically, and a field that is an integer
    on some points and a string on others is a type conflict Influx drops."""
    assert pc.counter_fields(4, 4) == {"counter_expected": 4, "counter_observed": 4}
    assert pc.counter_fields(pc.FINAL_COUNTER, None) == {
        "counter_expected_u64": str(pc.FINAL_COUNTER)
    }
    # Unknown stays unknown: a counter that was not read is omitted, not zeroed.
    assert pc.counter_fields(None, None) == {}
    line = pc.build_line("h", "lon1", "get", pc.counter_fields(pc.FINAL_COUNTER, 3))
    assert 'counter_expected_u64="18446744073709551615"' in line
    assert "counter_observed=3i" in line


def test_a_paid_transfer_that_did_not_land_is_a_lost_write_whatever_its_stderr(
    pc, tmp_path, monkeypatch
):
    """Classified by the PAYMENT first, then by the markers.

    Every refusal the markers name happens inside `pointer_sign_transfer`, before
    a quote is bought. So a transfer that spent and did not land is a lost write
    whatever it printed — and reading a marker first would file a paid failure as
    a refusal that by definition never cost anything, hiding real money.
    """
    client = _stub_client(pc, tmp_path, monkeypatch)
    client.do_create()

    # A transfer that spends and then fails, printing a pre-payment refusal's
    # words. The stub cannot produce this, so the runner is driven directly.
    state_path = tmp_path / "stub-state.json"
    original = client.runner.run

    def spend_then_fail(args, **kwargs):
        if args[:2] == ["pointer", "transfer"]:
            stub = json.loads(state_path.read_text())
            stub["ant"] -= 10**15
            stub["gas"] -= 10**12
            state_path.write_text(json.dumps(stub))
            return pc.AntResult(
                1,
                "",
                "Error: pointer is final: pointer ab is already final on state "
                "9c held by 4 of 5 peers\n",
                900,
            )
        return original(args, **kwargs)

    monkeypatch.setattr(client.runner, "run", spend_then_fail)
    client.influx = _CaptureSink()
    client.do_transfer()

    transfer = next(p for p in client.influx.points() if p["op"] == "transfer")
    assert transfer["outcome"] == "lost_write"
    assert transfer["paid_attempts"] == "1"
    assert int(transfer["wallet_spend_atto"]) > 0
    # And the cycle stops: only an `ok` transfer is followed up.
    assert [p["op"] for p in client.influx.points()][-1] == "transfer"
    # Unpaid, the same stderr is the pre-payment refusal it says it is.
    assert (
        pc.classify_failure(
            "Error: pointer is final: pointer ab is already final on state 9c\n",
            write=True,
            handover=True,
        )
        == pc.OUTCOME_ANOMALY
    )


def test_all_three_pre_payment_shortfalls_share_one_marker(pc):
    """`pointer_sign_transfer` can fall short in three places — its own
    silent-peer refusal, the finality check's under-quorum shortfall, and the
    recipient `get`'s. All three happen before anything is paid, so
    `read_shortfall` is the right class for each and one marker covers them.
    """
    for stderr in (
        "Error: only 4 of the 5 peers of pointer ab's close group answered, and "
        "a silent one could hold a final state; a transfer cannot be taken "
        "back, so try again when all of them can be asked\n",
        "Error: only 2 of the close group answered for pointer ab\n",
    ):
        assert pc.TRANSFER_SHORTFALL_MARKER in stderr
        assert pc.classify_failure(stderr, write=True, handover=True) == (pc.OUTCOME_READ_SHORTFALL)


def test_a_handed_over_pointer_is_read_once_per_sweep_not_twice(pc, tmp_path, monkeypatch):
    """It is swept by `_sweep_handovers`, which knows the recipient and runs the
    finality check beside the read, and skipped by the plain own-pointer loop.
    Doing both read every handover TWICE a sweep and inflated the `sweep` rows on
    exactly the addresses whose state can no longer change.
    """
    client = _stub_client(pc, tmp_path, monkeypatch)
    handed_over = client.do_create()
    client.do_transfer()
    recipient = client.ledger.transferred[handed_over]["recipient"]

    client.influx = _CaptureSink()
    client.do_sweep()
    points = client.influx.points()

    reads = [(p["op"], p["address"]) for p in points if p["op"] in ("sweep", "get")]
    # The recipient is an ordinary owned pointer and is swept as one.
    assert ("sweep", recipient) in reads
    # The handed-over address is read exactly once, as the redirect `get`.
    assert ("sweep", handed_over) not in reads
    assert reads.count(("get", handed_over)) == 1
    # And it still gets its finality check, with the age since the handover.
    finality = [p for p in points if p["op"] == "finality"]
    assert [p["address"] for p in finality] == [handed_over]
    assert "age_secs" in finality[0]
