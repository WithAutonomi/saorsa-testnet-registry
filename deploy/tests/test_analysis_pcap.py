"""The offline pcap analysers reproduce their documented ground truth.

Each fixture generator in `analysis_fixtures/pcap_synth.py` states what it
built; these tests assert the analysers read it back. They are the same checks
the scripts were verified against before the V2-996/V2-988/V2-1059 runs.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from saorsa_deploy.analysis import backstop, close_delivery, flowstat, probe_score
from tests.analysis_fixtures import pcap_synth

FLOWSTAT_PATH = Path(flowstat.__file__)


@pytest.fixture()
def flowstat_pcap(tmp_path):
    path = tmp_path / "flowstat.pcap"
    truth = pcap_synth.write_flowstat_pcap(path)
    return path, truth


@pytest.fixture()
def backstop_pcap(tmp_path):
    path = tmp_path / "backstop.pcap"
    truth = pcap_synth.write_backstop_pcap(path)
    return path, truth


@pytest.fixture()
def close_pcap(tmp_path):
    path = tmp_path / "close.pcap"
    truth = pcap_synth.write_close_delivery_pcap(path)
    return path, truth


def test_flowstat_reads_back_ground_truth(flowstat_pcap):
    path, _ = flowstat_pcap
    r = flowstat.analyse([str(path)])
    # 3 censored keep-alive flows + the observed 36 s flow + the bulk flow, which
    # is born and dies inside the window too (10 s) but is excluded from cadence.
    assert r["flows_total"] == 5
    assert r["flows_fully_observed"] == 2
    assert r["flows_censored"] == 3
    assert r["lifetime_histogram_observed"]["30-45"] == 1
    assert r["lifetime_histogram_observed"]["5-10"] == 1
    assert sum(r["lifetime_histogram_observed"].values()) == 2
    # Cadence per direction is 2.0 s on the quiescent flows; bulk excluded.
    assert r["cadence_median_of_flow_medians"] == pytest.approx(2.0, abs=0.01)
    assert r["cadence_gap_histogram"]["1.5-2.5"] > 0
    assert r["cadence_gap_histogram"]["0-0.25"] == 0  # the 10 ms bulk gaps are filtered out
    assert r["window_seconds"] == pytest.approx(298.4, abs=0.1)  # last packet at +298.4


def test_flowstat_short_capture_exits_two(flowstat_pcap, tmp_path, capsys):
    path, _ = flowstat_pcap
    out = tmp_path / "out.json"
    args = SimpleNamespace(
        pcaps=[str(path)], start=None, end=None, label="x", json_out=str(out),
        lifetimes_out=None, expect_seconds=1200, expect_tolerance=0.95,
    )
    assert flowstat.run(args) == 2
    assert json.loads(out.read_text())["window_complete"] is False
    assert "FATAL" in capsys.readouterr().err


def test_flowstat_standalone_script(flowstat_pcap, tmp_path):
    """The file must run under a bare python3 on a VM (no saorsa_deploy import)."""
    path, _ = flowstat_pcap
    lifetimes = tmp_path / "lifetimes.txt"
    proc = subprocess.run(
        [sys.executable, "-I", str(FLOWSTAT_PATH), str(path), "--expect-seconds", "300",
         "--lifetimes-out", str(lifetimes)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["window_complete"] is True
    assert sorted(round(float(x)) for x in lifetimes.read_text().split()) == [10, 36]


def test_flowstat_streams_from_stdin(flowstat_pcap):
    path, _ = flowstat_pcap
    proc = subprocess.run(
        [sys.executable, "-I", str(FLOWSTAT_PATH), "-"],
        input=path.read_bytes(), capture_output=True, check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["flows_total"] == 5


def test_backstop_fingerprint(backstop_pcap):
    path, _ = backstop_pcap
    r = backstop.analyse([str(path)])
    assert r["flows"] == 11
    assert r["flows_quiescent"] == 10
    assert r["flows_role_resolved"] == 10
    assert r["acceptor_unsolicited"] == 2
    assert r["flows_with_acceptor_unsolicited"] == 1
    assert r["acceptor_unsolicited_gap_histogram"]["24-27"] == 2
    assert r["acceptor_unsolicited_gap_median"] == 25.0
    flow_c = [f for f in r["flows_detail"] if f["acceptor_unsolicited"]][0]
    assert flow_c["acceptor"] == f"{pcap_synth.LOCAL}:10201"


def test_probe_score(backstop_pcap):
    path, truth = backstop_pcap
    r = probe_score.score(str(path), truth["probes"], pcap_synth.LOCAL)
    by_peer = {row["peer"]: row for row in r["rows"]}
    assert r["flows_scored"] == 3
    assert by_peer["10.0.1.1"]["result"] == "SURVIVED"
    assert by_peer["10.0.1.1"]["first_resume_offset"] == pytest.approx(2.0, abs=0.01)
    assert by_peer["10.0.1.3"]["result"] == "DIED" and by_peer["10.0.1.3"]["replaced"] is False
    assert by_peer["10.0.1.2"]["result"] == "DIED" and by_peer["10.0.1.2"]["replaced"] is True
    assert r["summary_by_seconds"]["21"]["survival_pct"] == 50.0
    assert r["summary_by_seconds"]["31"] == {
        "eligible": 1, "survived": 0, "died": 1, "replaced": 1, "survival_pct": 0.0
    }


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_close_delivery_epoch_timestamps(close_pcap, tmp_path):
    path, truth = close_pcap
    jsonl = tmp_path / "teardowns.jsonl"
    _write_jsonl(jsonl, truth["teardowns"])
    r = close_delivery.analyse(
        str(path), close_delivery.load_teardowns(jsonl), pcap_synth.LOCAL
    )
    assert (r["scored"], r["told"], r["untold"]) == (3, 2, 1)
    assert r["told_fraction"] == pytest.approx(2 / 3, abs=0.001)
    assert r["unscored_outside_window"] == 1
    assert r["teardowns"] == 5
    assert r["teardowns_without_flows"] == 1
    by_remote = {row["remote"]: row for row in r["rows"]}
    assert by_remote["203.0.113.10:41000"]["verdict"] == "TOLD"
    assert by_remote["203.0.113.10:41000"]["last_direction"] == "in"
    assert by_remote["203.0.113.10:41001"]["verdict"] == "TOLD"
    assert by_remote["203.0.113.10:41001"]["outbound_after_last_inbound"] == 1
    untold = by_remote["203.0.113.11:41002"]
    assert untold["verdict"] == "UNTOLD"
    assert untold["outbound_after_last_inbound"] == 3
    assert untold["seconds_from_teardown_to_last"] == pytest.approx(30.0, abs=0.01)
    assert untold["allocation_id"] == 3
    assert "10.0.0.77:20001" not in by_remote


def test_close_delivery_iso_timestamps(close_pcap, tmp_path):
    path, truth = close_pcap
    rows = []
    for t in truth["teardowns"]:
        iso = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows.append({**t, "ts": iso})
    jsonl = tmp_path / "teardowns-iso.jsonl"
    _write_jsonl(jsonl, rows)
    r = close_delivery.analyse(
        str(path), close_delivery.load_teardowns(jsonl), pcap_synth.LOCAL
    )
    assert (r["scored"], r["told"], r["untold"]) == (3, 2, 1)


def test_close_delivery_command_writes_json(close_pcap, tmp_path, capsys):
    path, truth = close_pcap
    jsonl = tmp_path / "teardowns.jsonl"
    _write_jsonl(jsonl, truth["teardowns"])
    out = tmp_path / "close.json"
    args = SimpleNamespace(
        pcap=str(path), teardowns=str(jsonl), local=pcap_synth.LOCAL,
        before=5.0, after=60.0, json_out=str(out),
    )
    assert close_delivery.cmd_close_delivery(args) == 0
    assert json.loads(out.read_text())["told"] == 2
    assert "told_fraction=0.667" in capsys.readouterr().out


def test_analysis_parsers_register():
    """The hub imports every module; the four here must register cleanly."""
    import argparse

    sub = argparse.ArgumentParser().add_subparsers()
    for mod in (flowstat, backstop, probe_score, close_delivery):
        mod.register(sub)
    ns = sub.choices["close-delivery"].parse_args(
        ["x.pcap", "--teardowns", "t.jsonl", "--local", "1.2.3.4"]
    )
    assert ns.func is close_delivery.cmd_close_delivery
    assert ns.after == 60.0
