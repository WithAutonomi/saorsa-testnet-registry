"""ES/InfluxDB extraction for `saorsa-deploy analysis` (es.py, relay.py, phase_extract.py).

The HTTP layer is mocked; what is under test is the query shapes and the
response arithmetic, each of which has produced a confidently wrong number
before when got wrong.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis import es as es_mod
from saorsa_deploy.analysis import phase_extract as pe
from saorsa_deploy.analysis import relay as relay_mod
from saorsa_deploy.analysis.es import EsRun, host_variants, parse_flux_csv, summarise_counter_delta
from saorsa_deploy.analysis.hosts import Vm


def _vm(name, ip, nat=False, services=5):
    return Vm(
        name=name,
        ip=ip,
        role="node",
        nat=nat,
        ssh_user="root",
        ssh_key="~/.ssh/k",
        services=tuple(f"ant-node-{i}" for i in range(1, services + 1)),
    )


@pytest.fixture()
def run():
    r = EsRun("DEV-01")
    r._elastic = {
        "elasticsearch_url": "https://es.example:9200/",
        "elasticsearch_api_key": "k",
        "elasticsearch_index_pattern": "telegraf-ant-nodes-dev-01-%Y.%m.%d",
    }
    r._monitoring = {
        "influxdb_url": "https://idb.example:8086",
        "influxdb_org": "o",
        "influxdb_token": "t",
        "influxdb_bucket": "b",
    }
    return r


# -- es.py -------------------------------------------------------------------
def test_host_variants_cover_es_upper_casing():
    assert host_variants(["dev-01-ant-node-DE1-1"]) == [
        "dev-01-ant-node-DE1-1",
        "DEV-01-ANT-NODE-DE1-1",
        "dev-01-ant-node-de1-1",
    ]


def test_count_phrase_query_shape_and_host_filter(run):
    captured = {}

    def fake_search(body, index=None):
        captured["body"] = body
        captured["index"] = index
        return {
            "hits": {"total": {"value": 42}},
            "aggregations": {
                "hosts": {"value": 3},
                "top_hosts": {"buckets": [{"key": "H1", "doc_count": 40}]},
                "levels": {"buckets": [{"key": "ERROR", "doc_count": 42}]},
            },
        }

    with patch.object(run, "search", side_effect=fake_search):
        out = run.count_phrase("relay recv stream closed", "S", "E", hosts=["h1"])
    assert out == {
        "phrase": "relay recv stream closed",
        "count": 42,
        "hosts": 3,
        "top_hosts": {"H1": 40},
        "levels": {"ERROR": 42},
    }
    body = captured["body"]
    assert body["track_total_hits"] is True
    filters = body["query"]["bool"]["filter"]
    assert {"match_phrase": {"tail.message": "relay recv stream closed"}} in filters
    assert {"terms": {"tag.host.keyword": ["h1", "H1"]}} in filters
    assert body["aggs"]["hosts"] == {"cardinality": {"field": "tag.host.keyword"}}
    assert captured["index"] is None


def test_node_and_client_index_collapse_date_suffix(run):
    assert run.node_index() == "telegraf-ant-nodes-dev-01-*"
    assert run.client_index() == "telegraf-ant-clients-dev-01*"


def test_counter_delta_single_sample_detection_and_rate():
    resp = {
        "hits": {"total": {"value": 9}},
        "aggregations": {
            "per_host": {
                "buckets": [
                    {
                        "key": "H1",
                        "per_service": {
                            "buckets": [
                                # 100 over 600 s -> 600/h
                                {
                                    "lo": {"value": 0},
                                    "hi": {"value": 100},
                                    "t_lo": {"value": 0},
                                    "t_hi": {"value": 600_000},
                                    "samples": {"value": 3},
                                },
                                # one sample: delta 0, span 0 -> not a rate, counted separately
                                {
                                    "lo": {"value": 5},
                                    "hi": {"value": 5},
                                    "t_lo": {"value": 0},
                                    "t_hi": {"value": 0},
                                    "samples": {"value": 1},
                                },
                                {
                                    "lo": {"value": None},
                                    "hi": {"value": None},
                                    "t_lo": {"value": None},
                                    "t_hi": {"value": None},
                                    "samples": {"value": 0},
                                },
                            ]
                        },
                    },
                    {
                        "key": "H2",
                        "per_service": {
                            "buckets": [
                                # 50 over 300 s -> 600/h
                                {
                                    "lo": {"value": 10},
                                    "hi": {"value": 60},
                                    "t_lo": {"value": 0},
                                    "t_hi": {"value": 300_000},
                                    "samples": {"value": 2},
                                },
                            ]
                        },
                    },
                ]
            }
        },
    }
    out = summarise_counter_delta("tail.x", resp)
    assert out["total_delta_in_window"] == 150
    assert out["rate_per_hour_fleet"] == 1200.0
    assert out["services_with_rate"] == 2
    assert out["services_single_sample"] == 1
    assert out["median_observed_span_s"] == 600.0
    assert out["per_host"] == {"H1": 100, "H2": 50}
    assert out["hosts"] == 2 and out["docs"] == 9


def test_flux_csv_split_on_blank_lines_keeps_every_table():
    text = (
        ",result,table,_time,_value,host\n,_result,0,t1,1.5,a\n,_result,0,t2,2.5,a\n"
        "\n"
        ",result,table,_time,_value,host\n,_result,1,t1,7,b\n"
    )
    rows = parse_flux_csv(text)
    assert [(r["host"], r["_value"]) for r in rows] == [("a", "1.5"), ("a", "2.5"), ("b", "7")]


def test_udp_datagrams_query_has_explicit_stop_and_lowercase_field(run):
    seen = {}
    with patch.object(run, "flux", side_effect=lambda q: seen.setdefault("q", q) and []):
        run.udp_datagrams("2026-08-25T10:00:00Z", "2026-08-25T11:00:00Z")
    q = seen["q"]
    assert "range(start: 2026-08-25T10:00:00Z, stop: 2026-08-25T11:00:00Z)" in q
    assert '_field == "udp_outdatagrams"' in q
    assert 'r.testnet_name == "DEV-01"' in q


def test_hits_paginates_with_search_after(run):
    pages = [
        {
            "hits": {
                "hits": [
                    {"_source": {"a": 1}, "sort": [1, 0]},
                    {"_source": {"a": 2}, "sort": [2, 0]},
                ]
            }
        },
        {"hits": {"hits": [{"_source": {"a": 3}, "sort": [3, 0]}]}},
    ]
    bodies = []

    def fake_search(body, index=None):
        bodies.append(body)
        return pages.pop(0)

    with patch.object(run, "search", side_effect=fake_search):
        docs = run.hits([{"match_phrase": {"tail.message": "x"}}], "S", "E", page_size=2)
    assert [d["a"] for d in docs] == [1, 2, 3]
    assert "search_after" not in bodies[0]
    assert bodies[1]["search_after"] == [2, 0]
    assert bodies[0]["sort"] == [{"@timestamp": "asc"}, {"_doc": "asc"}]


def test_search_posts_to_node_index_with_api_key(run):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    with patch.object(es_mod.requests, "post", return_value=Resp()) as post:
        assert run.search({"size": 0}) == {"ok": True}
    args, kwargs = post.call_args
    assert args[0] == "https://es.example:9200/telegraf-ant-nodes-dev-01-*/_search"
    assert kwargs["headers"]["Authorization"] == "ApiKey k"
    assert kwargs["verify"] is False


# -- relay.py ----------------------------------------------------------------
def _count(n, hosts=1):
    return {"phrase": "p", "count": n, "hosts": hosts, "top_hosts": {}, "levels": {}}


def test_relay_report_ratios(run):
    per_phrase = {
        "Proactive relay torn down": 50,
        "relay recv stream closed": 2,
        "MASQUE tunnel for relay": 5,
    }
    with patch.object(
        run, "count_phrase", side_effect=lambda p, s, e, hosts=None: _count(per_phrase.get(p, 0))
    ):
        out = relay_mod.relay_report(run, "S", "E", hosts=["h"])
    assert out["error_per_teardown"] == 0.04
    assert out["died_per_teardown"] == 0.1
    assert out["hosts_filter"] == ["h"]
    assert set(out["counts"]) == {k for k, _ in relay_mod.RELAY_PHRASES}


def test_relay_report_zero_teardowns_gives_none(run):
    with patch.object(run, "count_phrase", side_effect=lambda p, s, e, hosts=None: _count(0)):
        out = relay_mod.relay_report(run, "S", "E")
    assert out["error_per_teardown"] is None
    assert out["died_per_teardown"] is None


def test_teardown_rows_field_mapping_nested_and_flat():
    docs = [
        {
            "@timestamp": "T1",
            "tag": {"host": "H1", "service_number": "3"},
            "tail": {
                "relay_addr": "1.2.3.4:5000",
                "relay_server": "1.2.3.4:10000",
                "allocation_id": 7,
            },
        },
        {
            "@timestamp": "T2",
            "tag.host": "H2",
            "tag.service_number": "4",
            "tail.relay_addr": "5.6.7.8:5001",
            "tail.relay_server": "5.6.7.8:10000",
            "tail.allocation_id": 8,
        },
    ]
    rows = relay_mod.teardown_rows(docs)
    assert rows == [
        {
            "ts": "T1",
            "host": "H1",
            "service": "3",
            "relay_addr": "1.2.3.4:5000",
            "relay_server": "1.2.3.4:10000",
            "allocation_id": 7,
        },
        {
            "ts": "T2",
            "host": "H2",
            "service": "4",
            "relay_addr": "5.6.7.8:5001",
            "relay_server": "5.6.7.8:10000",
            "allocation_id": 8,
        },
    ]


def test_teardown_rows_warns_once_when_relay_addr_missing(capsys):
    from rich.console import Console

    doc = {"@timestamp": "T", "tail": {"message": "Proactive relay torn down", "other": 1}}
    docs = [doc, doc]
    rows = relay_mod.teardown_rows(docs, Console(file=None, force_terminal=False))
    out = capsys.readouterr().out
    assert out.count("warning") == 1
    assert "['message', 'other']" in out
    assert rows[0]["relay_addr"] is None


def test_rank_relay_servers_maps_to_inventory():
    """Ranked by server IP, one bucket per VM.

    The port on `relay_server` is PER ALLOCATION, not a stable listening port:
    measured on DEV-01 (V2-1061), one server IP carried 8 distinct
    `relay_server` ports across 8 clients. Keying on the full `ip:port` would
    split that VM into 8 buckets of one client each and rank nothing, so the
    ports below deliberately differ per row.
    """
    vms = [_vm("DEV-01-ant-node-A", "10.0.0.1"), _vm("DEV-01-ant-node-B", "10.0.0.2", nat=True)]
    docs = [
        {"tag": {"host": "C1", "service_number": "1"}, "tail": {"relay_server": "10.0.0.1:35940"}},
        # duplicate of the row above: must count once
        {"tag": {"host": "C1", "service_number": "1"}, "tail": {"relay_server": "10.0.0.1:35940"}},
        {"tag": {"host": "C1", "service_number": "2"}, "tail": {"relay_server": "10.0.0.1:41157"}},
        {"tag": {"host": "C2", "service_number": "1"}, "tail": {"relay_server": "10.0.0.1:50698"}},
        {"tag": {"host": "C3", "service_number": "1"}, "tail": {"relay_server": "10.0.0.9:37923"}},
        {"tag": {"host": "C4", "service_number": "1"}, "tail": {}},
    ]
    ranked = relay_mod.rank_relay_servers(docs, vms)
    assert [r["relay_server"] for r in ranked] == ["10.0.0.1", "10.0.0.9"]
    top = ranked[0]
    assert top["vm"] == "DEV-01-ant-node-A" and top["ip"] == "10.0.0.1" and top["nat"] is False
    assert top["clients"] == 3 and top["client_hosts"] == 2
    assert ranked[1]["vm"] is None


def test_cmd_relay_servers_reads_inventory(tmp_path, capsys):
    inv = {
        "schema_version": 6,
        "node_vms": [
            {
                "name": "DEV-01-ant-node-A",
                "provider": "ovh",
                "region": "DE1",
                "ip": "10.0.0.1",
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": False,
                "services": ["ant-node-1"],
            },
        ],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    docs = [
        {"tag": {"host": "C1", "service_number": "1"}, "tail": {"relay_server": "10.0.0.1:10000"}}
    ]
    args = SimpleNamespace(
        name="DEV-01",
        inventory=str(path),
        start="S",
        end="E",
        top=10,
        json_out=str(tmp_path / "r.json"),
    )
    with patch.object(EsRun, "hits", return_value=docs):
        assert relay_mod.cmd_relay_servers(args) == 0
    out = json.loads((tmp_path / "r.json").read_text())
    assert out[0]["vm"] == "DEV-01-ant-node-A"
    assert "wrote" in capsys.readouterr().out


# -- phase_extract.py -----------------------------------------------------------
def _snap(ip, ts, nat, **over):
    base = {
        "host": f"h-{ip}",
        "ip": ip,
        "ts": ts,
        "nat": nat,
        "iface": "eth0",
        "host_udp_in": 0,
        "host_udp_out": 0,
        "host_rx_bytes": 0,
        "host_tx_bytes": 0,
        "host_udp_rcvbuf_err": 0,
        "ns_udp_in": 0,
        "ns_udp_out": 0,
        "ns_udp_rcvbuf_err": 0,
        "ns_rx_bytes": 0,
        "ns_tx_bytes": 0,
    }
    base.update(over)
    return base


def test_snapshot_delta_uses_namespace_for_nat_and_excludes_resets(tmp_path):
    before = [
        _snap("1.1.1.1", 0, 0, host_udp_out=1000, host_udp_in=100),
        _snap("2.2.2.2", 0, 1, ns_udp_out=500, host_udp_out=999_999),
        _snap("3.3.3.3", 0, 0, host_udp_out=5000),  # reboots -> reset
        {"ip": "4.4.4.4", "tag": "x", "error": "unreachable"},
    ]
    after = [
        _snap(
            "1.1.1.1",
            100,
            0,
            host_udp_out=1000 + 30_000,
            host_udp_in=100 + 10_000,
            host_tx_bytes=1_000_000,
        ),
        _snap("2.2.2.2", 100, 1, ns_udp_out=500 + 15_000, host_udp_out=999_999 + 10),
        _snap("3.3.3.3", 100, 0, host_udp_out=10),
    ]
    b, a = tmp_path / "b.jsonl", tmp_path / "a.jsonl"
    b.write_text("\n".join(json.dumps(r) for r in before) + "\n")
    a.write_text("\n".join(json.dumps(r) for r in after) + "\n")
    out = pe.snapshot_delta(b, a, node_services=15)
    assert out["vms"] == 2 and out["node_services_per_vm"] == 15
    rows = {r["ip"]: r for r in out["per_vm"]}
    assert rows["1.1.1.1"]["udp_out_per_sec"] == 300.0
    assert rows["1.1.1.1"]["udp_out_per_sec_per_node"] == 20.0
    assert rows["1.1.1.1"]["tx_bytes_per_sec"] == 10_000.0
    assert rows["2.2.2.2"]["udp_out_per_sec"] == 150.0  # ns counters, not host
    assert out["nat"]["udp_out_per_sec_per_node"]["mean"] == 10.0
    assert out["plain"]["udp_out_per_sec_per_node"]["mean"] == 20.0


def test_services_per_node_vm_is_median_from_inventory():
    vms = [_vm("a", "1", services=15), _vm("b", "2", services=15), _vm("c", "3", services=5)]
    assert pe.services_per_node_vm(vms) == 15
    with pytest.raises(ValueError):
        pe.services_per_node_vm([_vm("a", "1", services=0)])


def test_influx_udp_summary_excludes_nat_tier_by_inventory_not_by_value():
    rows = [
        {"host": "Plain-A", "_value": "30"},
        {"host": "Plain-A", "_value": "60"},
        {"host": "nat-b", "_value": "0.1"},
        {"host": "bootstrap-1", "_value": "999"},
    ]
    out = pe.influx_udp_summary(rows, {"plain-a", "nat-b"}, {"nat-b"}, node_services=15)
    assert out["hosts_plain_tier"] == 1 and out["hosts_nat_excluded"] == 1
    assert out["plain_per_vm_mean_of_means"] == 45.0
    assert out["plain_per_node_mean"] == 3.0
    assert out["nat_residual_per_vm_mean"] == 0.1
    assert out["buckets_per_host_median"] == 2


def test_cmd_phase_extract_writes_file(tmp_path):
    inv = {
        "schema_version": 6,
        "node_vms": [
            {
                "name": "DEV-01-ant-node-A",
                "ip": "10.0.0.1",
                "nat_simulated": False,
                "services": ["s1", "s2"],
            },
            {
                "name": "DEV-01-ant-node-B",
                "ip": "10.0.0.2",
                "nat_simulated": True,
                "services": ["s1", "s2"],
            },
        ],
    }
    (tmp_path / "inventory.json").write_text(json.dumps(inv))
    args = SimpleNamespace(
        name="DEV-01",
        inventory=str(tmp_path / "inventory.json"),
        label="p1",
        start="S",
        end="E",
        snap_before=None,
        snap_after=None,
        out_dir=str(tmp_path / "out"),
    )
    counts = {"Connected to peer at": 90, "Connection attempt failed": 10}
    udp_rows = [{"host": "DEV-01-ant-node-A", "_value": "30"}]
    with (
        patch.object(
            EsRun, "count_phrase", side_effect=lambda p, s, e, **kw: _count(counts.get(p, 0))
        ),
        patch.object(EsRun, "counter_delta", return_value={"total_delta_in_window": 1}),
        patch.object(EsRun, "udp_datagrams", return_value=udp_rows),
    ):
        assert pe.cmd_phase_extract(args) == 0
    result = json.loads((tmp_path / "out" / "extract-p1.json").read_text())
    assert result["dial_success_rate"] == 0.9
    assert result["influx_udp_out_1m"]["plain_per_node_mean"] == 15.0
    assert set(result["relay"]) == {p for _, p in relay_mod.RELAY_PHRASES}
    assert result["counters"]["tail.wire_tx_bytes"] == {"total_delta_in_window": 1}
