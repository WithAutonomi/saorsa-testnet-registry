"""`analysis relay-servers` ranking (V2-1061 / saorsa-transport #138).

Regression cover for a defect found mid-run on DEV-01: the ranking is built
from `Proactive relay allocation prepared` documents, which carry `relay_addr`
but NOT `relay_server`. Keying on `relay_server` alone skipped every document
and printed an empty table with no warning, so the Phase 3 cohort could not be
selected from it.
"""

from saorsa_deploy.analysis.relay import rank_relay_servers


class _Vm:
    def __init__(self, name, ip, nat):
        self.name, self.ip, self.nat = name, ip, nat


VMS = [
    _Vm("relay-a", "10.0.0.1", False),
    _Vm("relay-b", "10.0.0.2", False),
]


def _prepared(host, service, relay_addr):
    """A `prepared` doc: relay_addr only, no relay_server (as ant-node emits it)."""
    return {
        "tag": {"host": host, "service_number": service},
        "tail": {"message": "Proactive relay allocation prepared", "relay_addr": relay_addr},
    }


def test_ranks_prepared_docs_that_carry_only_relay_addr():
    docs = [
        _prepared("nat-1", "ant-node-1", "10.0.0.1:41890"),
        _prepared("nat-2", "ant-node-1", "10.0.0.1:51234"),
        _prepared("nat-3", "ant-node-7", "10.0.0.2:33001"),
    ]
    ranked = rank_relay_servers(docs, VMS)
    assert [r["ip"] for r in ranked] == ["10.0.0.1", "10.0.0.2"]
    assert ranked[0]["clients"] == 2
    assert ranked[0]["vm"] == "relay-a"
    assert ranked[1]["clients"] == 1


def test_one_vm_is_one_bucket_despite_per_allocation_ports():
    """A relay server gives every allocation its own port; that must not split it."""
    docs = [_prepared(f"nat-{i}", "ant-node-1", f"10.0.0.1:{40000 + i}") for i in range(5)]
    ranked = rank_relay_servers(docs, VMS)
    assert len(ranked) == 1
    assert ranked[0]["clients"] == 5


def test_explicit_relay_server_still_wins_and_agrees():
    """`torn down` docs carry relay_server on a different port; same VM bucket."""
    docs = [
        {
            "tag": {"host": "nat-1", "service_number": "ant-node-1"},
            "tail": {
                "message": "Proactive relay torn down",
                "relay_addr": "10.0.0.1:54095",
                "relay_server": "10.0.0.1:59602",
            },
        },
        _prepared("nat-2", "ant-node-2", "10.0.0.1:41890"),
    ]
    ranked = rank_relay_servers(docs, VMS)
    assert len(ranked) == 1
    assert ranked[0]["ip"] == "10.0.0.1"
    assert ranked[0]["clients"] == 2


def test_distinct_host_service_pairs_are_counted_once():
    docs = [_prepared("nat-1", "ant-node-1", "10.0.0.1:41890")] * 3
    ranked = rank_relay_servers(docs, VMS)
    assert ranked[0]["clients"] == 1
    assert ranked[0]["client_hosts"] == 1


def test_document_with_neither_field_is_skipped_not_crashed():
    docs = [
        {"tag": {"host": "nat-1", "service_number": "ant-node-1"}, "tail": {"message": "x"}},
        _prepared("nat-2", "ant-node-1", "10.0.0.2:1234"),
    ]
    ranked = rank_relay_servers(docs, VMS)
    assert len(ranked) == 1
    assert ranked[0]["ip"] == "10.0.0.2"
