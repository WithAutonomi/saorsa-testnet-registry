"""`--json-out -` / `/dev/stdout` must leave stdout holding one JSON object (V2-1450).

The shell helper every pointer-run issue carries is

    cnum() { uv run saorsa-deploy analysis log-count ... --json-out /dev/stdout \
             2>/dev/null | jq -r '.count // 0' | tail -1; }

and on DEV-06 run 647 it returned 0 for every phrase. The command printed the
object once as its human output and once as the `--json-out` destination, then
a `wrote /dev/stdout` trailer on top -- so `jq` aborted on the concatenation
and `// 0` turned the abort into a confident zero. `tail -1` cannot mitigate a
parse abort.

So these tests are about the *stream*, not the arithmetic: exactly one object,
nothing else, and a real file path unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.analysis import backstop, close_delivery, probe_score
from saorsa_deploy.analysis import es as es_mod
from saorsa_deploy.analysis import json_out as jo
from saorsa_deploy.analysis import relay as relay_mod
from saorsa_deploy.analysis.es import EsRun

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: What the three `analysis log-count` / `counter-delta` / `udp-rate` call sites
#: and the pcap analysers all have to satisfy.
STDOUT_SPELLINGS = ["-", "/dev/stdout"]


def only_object(captured: str) -> dict:
    """The captured stdout parsed as one JSON object, or a failure saying why not."""
    assert captured.strip(), "nothing was written to stdout"
    return json.loads(captured)


@pytest.fixture()
def es_run():
    r = EsRun("DEV-06")
    r._elastic = {
        "elasticsearch_url": "https://es.example:9200/",
        "elasticsearch_api_key": "k",
        "elasticsearch_index_pattern": "telegraf-ant-nodes-dev-06-%Y.%m.%d",
    }
    r._monitoring = {
        "influxdb_url": "https://idb.example:8086",
        "influxdb_org": "o",
        "influxdb_token": "t",
        "influxdb_bucket": "b",
    }
    return r


# -- the helper itself -------------------------------------------------------
@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_both_stdout_spellings_are_recognised(spelling):
    assert jo.is_stdout(spelling) is True


@pytest.mark.parametrize("other", [None, "", "out.json", "/tmp/dev/stdout", "/dev/stderr"])
def test_a_path_is_not_a_stdout_target(other):
    """`/dev/stderr` deliberately is not: the human output does not share it, so
    there is nothing to suppress and the trailer is still worth printing."""
    assert jo.is_stdout(other) is False


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_console_for_a_stdout_target_prints_nothing(spelling):
    """The table, the preamble and the `[dim]` footnotes are all suppressed --
    one stray line in the caller's `jq` input is as fatal as a second object."""
    assert jo.console_for(spelling).quiet is True
    assert jo.console_for("out.json").quiet is False


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_emit_to_stdout_writes_the_object_once_and_no_trailer(spelling, capsys):
    jo.emit_json({"count": 7}, spelling, jo.console_for(spelling), echo=True)
    out = capsys.readouterr().out
    assert only_object(out) == {"count": 7}
    assert "wrote" not in out


def test_emit_to_a_file_keeps_the_echo_and_the_trailer(tmp_path, capsys):
    path = tmp_path / "r.json"
    jo.emit_json({"count": 7}, str(path), Console(), echo=True)
    out = capsys.readouterr().out
    assert json.loads(path.read_text()) == {"count": 7}
    assert f"wrote {path}" in out
    assert json.loads(out.split("wrote")[0]) == {"count": 7}


def test_emit_with_no_destination_echoes_only(capsys):
    jo.emit_json({"count": 7}, None, Console(), echo=True)
    out = capsys.readouterr().out
    assert only_object(out) == {"count": 7}
    assert "wrote" not in out


def test_emit_without_echo_and_without_a_destination_writes_nothing(capsys):
    jo.emit_json({"count": 7}, None, Console())
    assert capsys.readouterr().out == ""


# -- the three es.py call sites ---------------------------------------------
def _log_count_args(json_out):
    return SimpleNamespace(
        name="DEV-06",
        inventory=None,
        phrase="Added peer",
        start="S",
        end="E",
        index="client",
        hosts=None,
        json_out=json_out,
    )


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_log_count_to_stdout_is_one_object(spelling, es_run, capsys):
    response = {
        "hits": {"total": {"value": 1356}},
        "aggregations": {
            "hosts": {"value": 7},
            "top_hosts": {"buckets": [{"key": "DEV-06-ANT-UPLOADER-LON1-1", "doc_count": 900}]},
            "levels": {"buckets": [{"key": "INFO", "doc_count": 1356}]},
        },
    }
    with (
        patch.object(EsRun, "search", return_value=response),
        patch.object(EsRun, "elastic", return_value=dict(es_run.elastic())),
    ):
        assert es_mod.cmd_log_count(_log_count_args(spelling)) == 0
    assert only_object(capsys.readouterr().out)["count"] == 1356


def test_log_count_to_a_file_still_echoes_and_reports_where(es_run, tmp_path, capsys):
    response = {
        "hits": {"total": {"value": 3}},
        "aggregations": {
            "hosts": {"value": 1},
            "top_hosts": {"buckets": []},
            "levels": {"buckets": []},
        },
    }
    path = tmp_path / "count.json"
    with (
        patch.object(EsRun, "search", return_value=response),
        patch.object(EsRun, "elastic", return_value=dict(es_run.elastic())),
    ):
        assert es_mod.cmd_log_count(_log_count_args(str(path))) == 0
    out = capsys.readouterr().out
    assert json.loads(path.read_text())["count"] == 3
    assert f"wrote {path}" in out


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_counter_delta_to_stdout_is_one_object(spelling, es_run, capsys):
    args = SimpleNamespace(
        name="DEV-06",
        inventory=None,
        field="tail.wire_tx_bytes",
        start="S",
        end="E",
        json_out=spelling,
    )
    with (
        patch.object(
            EsRun, "counter_delta", return_value={"field": "tail.wire_tx_bytes", "fleet_total": 12}
        ),
        patch.object(EsRun, "elastic", return_value=dict(es_run.elastic())),
    ):
        assert es_mod.cmd_counter_delta(args) == 0
    assert only_object(capsys.readouterr().out)["fleet_total"] == 12


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_udp_rate_to_stdout_suppresses_its_table_and_footnote(spelling, es_run, capsys):
    """`udp-rate` prints a rich table and a `[dim]` NAT footnote. Both share the
    stream the JSON is going to, so both have to go."""
    args = SimpleNamespace(
        name="DEV-06",
        inventory=None,
        start="S",
        end="E",
        every="1m",
        field="udp_outdatagrams",
        json_out=spelling,
    )
    rows = [{"host": "h1", "_value": "10"}, {"host": "h1", "_value": "20"}]
    with (
        patch.object(EsRun, "udp_datagrams", return_value=rows),
        patch.object(EsRun, "elastic", return_value=dict(es_run.elastic())),
    ):
        assert es_mod.cmd_udp_rate(args) == 0
    out = capsys.readouterr().out
    assert only_object(out)["per_host_mean"] == {"h1": 15.0}
    assert "mean rate per host" not in out
    assert "ant_nat" not in out


# -- the pcap analysers and relay-report ------------------------------------
@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_backstop_to_stdout_is_one_object(spelling, capsys):
    summary = {"host": "x", "flows_role_resolved": 2}
    args = SimpleNamespace(
        pcaps=["x.pcap"],
        start=None,
        end=None,
        label="x",
        json_out=spelling,
        no_detail=True,
    )
    with patch.object(backstop, "analyse", return_value=dict(summary, flows_detail=[1])):
        assert backstop.cmd_backstop(args) == 0
    assert only_object(capsys.readouterr().out) == summary


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_probe_score_to_stdout_is_one_object(spelling, capsys):
    args = SimpleNamespace(
        pcap="x.pcap",
        probes="p.jsonl",
        local="10.0.0.1",
        resume_window=15.0,
        active_window=30.0,
        json_out=spelling,
    )
    with (
        patch.object(probe_score, "load_jsonl", return_value=[]),
        patch.object(probe_score, "score", return_value={"flows_scored": 4}),
    ):
        assert probe_score.cmd_probe_score(args) == 0
    assert only_object(capsys.readouterr().out) == {"flows_scored": 4}


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_close_delivery_to_stdout_suppresses_its_summary(spelling, capsys):
    args = SimpleNamespace(
        pcap="x.pcap",
        teardowns="t.jsonl",
        local="10.0.0.1",
        before=5.0,
        after=60.0,
        json_out=spelling,
    )
    result = {
        "scored": 1,
        "told": 1,
        "untold": 0,
        "told_fraction": 1.0,
        "unscored_no_teardown": 0,
        "unscored_outside_window": 0,
        "teardowns": 1,
        "teardowns_without_flows": 0,
        "rows": [
            {
                "remote": "10.0.0.2:10000",
                "allocation_id": None,
                "verdict": "TOLD",
                "last_direction": "in",
                "outbound_after_last_inbound": 0,
                "seconds_from_teardown_to_last": 0.5,
            }
        ],
    }
    with (
        patch.object(close_delivery, "load_teardowns", return_value=[]),
        patch.object(close_delivery, "analyse", return_value=result),
    ):
        assert close_delivery.cmd_close_delivery(args) == 0
    out = capsys.readouterr().out
    assert only_object(out) == result
    assert "told=" not in out


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_relay_servers_to_stdout_suppresses_its_table(spelling, tmp_path, capsys):
    inv = {
        "schema_version": 6,
        "node_vms": [
            {
                "name": "DEV-06-ant-node-A",
                "provider": "ovh",
                "region": "DE1",
                "ip": "10.0.0.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": False,
                "services": ["ant-node-1"],
            }
        ],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    docs = [
        {"tag": {"host": "C1", "service_number": "1"}, "tail": {"relay_server": "10.0.0.1:10000"}}
    ]
    args = SimpleNamespace(
        name="DEV-06", inventory=str(path), start="S", end="E", top=10, json_out=spelling
    )
    with patch.object(EsRun, "hits", return_value=docs):
        assert relay_mod.cmd_relay_servers(args) == 0
    out = capsys.readouterr().out
    assert json.loads(out)[0]["vm"] == "DEV-06-ant-node-A"
    assert "relay servers by client count" not in out


# -- the regression that actually bit: the cnum pipeline --------------------
CNUM_FIXTURE_COUNT = 1356


def _cnum_fixture_response():
    return {
        "hits": {"total": {"value": CNUM_FIXTURE_COUNT}},
        "aggregations": {
            "hosts": {"value": 7},
            "top_hosts": {"buckets": [{"key": "DEV-06-ANT-NODE-DE1-1", "doc_count": 400}]},
            "levels": {"buckets": [{"key": "INFO", "doc_count": CNUM_FIXTURE_COUNT}]},
        },
    }


@pytest.mark.parametrize("spelling", STDOUT_SPELLINGS)
def test_the_cnum_pipeline_returns_the_true_count(spelling, es_run, capsys):
    """`jq -r '.count // 0'` over the command's stdout, in Python: the stdout
    must parse, so the `// 0` default is never reached."""
    with (
        patch.object(EsRun, "search", return_value=_cnum_fixture_response()),
        patch.object(EsRun, "elastic", return_value=dict(es_run.elastic())),
    ):
        assert es_mod.cmd_log_count(_log_count_args(spelling)) == 0
    captured = capsys.readouterr().out
    assert json.loads(captured).get("count", 0) == CNUM_FIXTURE_COUNT


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_the_cnum_pipeline_through_real_jq(tmp_path):
    """The same pipeline through the real `jq`, since the defect was a `jq`
    parse abort rather than anything Python's `json` would have objected to."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import json, sys\n"
        "from types import SimpleNamespace\n"
        "from unittest.mock import patch\n"
        "from saorsa_deploy.analysis import es as es_mod\n"
        "from saorsa_deploy.analysis.es import EsRun\n"
        f"RESPONSE = {_cnum_fixture_response()!r}\n"
        "ELASTIC = {'elasticsearch_url': 'https://es.example:9200/',\n"
        "           'elasticsearch_api_key': 'k',\n"
        "           'elasticsearch_index_pattern': 'telegraf-ant-nodes-dev-06-%Y.%m.%d'}\n"
        "args = SimpleNamespace(name='DEV-06', inventory=None, phrase='Added peer',\n"
        "                       start='S', end='E', index='client', hosts=None,\n"
        "                       json_out='/dev/stdout')\n"
        "with patch.object(EsRun, 'search', return_value=RESPONSE), \\\n"
        "     patch.object(EsRun, 'elastic', return_value=ELASTIC):\n"
        "    sys.exit(es_mod.cmd_log_count(args))\n"
    )
    # `pipefail` is what makes this a regression test rather than a smoke test:
    # with the object emitted twice and a `wrote /dev/stdout` trailer on top,
    # `jq` printed the count twice and then exited 5 on the trailer, which
    # `tail -1` hid. The count being right is not enough -- `jq` has to have
    # parsed the whole stream.
    pipeline = (
        "set -o pipefail; "
        f"{subprocess.list2cmdline([sys.executable, str(driver)])} 2>/dev/null "
        "| jq -r '.count // 0' | tail -1"
    )
    proc = subprocess.run(
        ["bash", "-c", pipeline],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"{CNUM_FIXTURE_COUNT}\n"
