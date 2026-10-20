"""The holder-served per-chunk arithmetic against REAL id 685 trace records.

The fixture is 1,738 Elasticsearch documents built from the archived traces of
DEV-01 registry id 685 (V2-1621): round 2987404, the first twenty downloads in
`(host, instance)` order, narrowed to two chunks — the 50 MiB file's address
chunk and one of its content chunks. Real, not written down, because the
question these figures answer is "what does ant-core's read path actually do",
which an assumption cannot answer. The same reason
`fixtures/browser-trace/read-startup.jsonl.gz` is a real capture.

Every expected figure below was computed from the raw JSONL by a separate
script, not by this module, and the whole-run versions of them appear in
V2-1621's follow-up comment and in `deploy/README.md`. The run-wide numbers the
same definitions reproduce over all 1,260 R1–R9 traces (892,174 records):

* file-address chunks: 6–7 holders, busiest share 29 % / 59 % / 63 %, first GET
  to a holder **0 %**, decided before discovery **100 %**
* content chunks: 4–9 holders, busiest share median 34 %, max 90 %, 38 of 48
  over 25 %
* 10.04 GETs and 7.94 `NotFound` per chunk per download, against the ADR's own
  validation figures of 3.45 and 1.39

41 of 51 chunks over the 25 % ceiling is the finding; counted the old way — by
the peer each first GET was SENT to — the same run reported 3 of 51.
"""

import gzip
import json
from pathlib import Path

import pytest

from saorsa_deploy.browser_traces import normalise_trace_doc, summarise_chunks

FIXTURES = Path(__file__).parent / "fixtures" / "browser-reads"
FILE_CHUNK = "486d9615d25f849ed814b3896ea07b7e3447374589c1a437e440643fee9ebd12"
CONTENT_CHUNK = "08b6b3ee025486ea0fc6b6e2f76c84a473d1e743b8535c962e7cae3fec06b0f1"

#: Worked out from the raw traces, chunk by chunk. `downloads` is 20 for both:
#: every one of the twenty downloads GET both chunks.
EXPECTED = {
    FILE_CHUNK: {
        "downloads": 20,
        "reads": 20,
        "distinct_holders": 3,
        "holders_answering_found": 3,
        "busiest_holder": "48b64a192132d0172a8f20ef8a9b36ac5d44232f9aea18f2a99a60eac9cf3ebe",
        "busiest_holder_reads": 14,
        "busiest_holder_share": 0.70,
        "gets_per_download": 9.25,
        "not_found_per_download": 7.75,
        "first_get_to_holder": 0.0,
        "decided_before_discovery": 1.0,
    },
    CONTENT_CHUNK: {
        "downloads": 20,
        "reads": 20,
        "distinct_holders": 5,
        "holders_answering_found": 6,
        "busiest_holder": "0925d4e4476d3b6f0764990a75b18d28f28051c504c655f2eb77e3d4878205e0",
        "busiest_holder_reads": 9,
        "busiest_holder_share": 0.45,
        "gets_per_download": 11.65,
        "not_found_per_download": 9.4,
        "first_get_to_holder": 0.0,
        "decided_before_discovery": 1.0,
    },
}


@pytest.fixture(scope="module")
def docs():
    with gzip.open(FIXTURES / "id685-trace-docs.jsonl.gz", "rt") as fh:
        raw = [json.loads(line) for line in fh if line.strip()]
    out = [normalise_trace_doc(d) for d in raw]
    assert all(d is not None for d in out)
    return out


@pytest.fixture(scope="module")
def chunks(docs):
    return {c["chunk"]: c for c in summarise_chunks(docs, [], min_first_reads=20)["chunks"]}


def test_the_fixture_is_the_real_capture(docs):
    assert len(docs) == 1738
    assert {d["round_id"] for d in docs} == {2987404}
    assert len({(d["host"], d["instance"]) for d in docs}) == 20
    assert {d["operation"] for d in docs} == {"rpc", "discovery"}
    assert {d["target"] for d in docs} == {FILE_CHUNK, CONTENT_CHUNK}


@pytest.mark.parametrize("chunk", [FILE_CHUNK, CONTENT_CHUNK])
def test_every_per_chunk_figure(chunks, chunk):
    got = chunks[chunk]
    for key, value in EXPECTED[chunk].items():
        if isinstance(value, float):
            assert got[key] == pytest.approx(value), key
        else:
            assert got[key] == value, key


def test_the_first_get_never_reached_a_holder(chunks):
    """The whole point of the change. Every one of the twenty downloads sent its
    first GET for both chunks to a peer that never answered `found` — on the
    file-address chunk that is a bootstrap seed, which is what the old gate
    counted as the holder serving the read."""
    assert chunks[FILE_CHUNK]["first_get_to_holder"] == 0.0
    assert chunks[CONTENT_CHUNK]["first_get_to_holder"] == 0.0
    assert chunks[FILE_CHUNK]["busiest_holder"] != chunks[CONTENT_CHUNK]["busiest_holder"]


def test_the_cost_per_chunk_is_far_above_the_adr_validation(chunks):
    """9.25 and 11.65 GETs per download here, 7.75 and 9.40 of them `NotFound`,
    against 3.45 GETs and 1.39 `NotFound` in the ADR's own 60-node validation.
    Reported rather than gated — no threshold has been calibrated for it."""
    for chunk in (FILE_CHUNK, CONTENT_CHUNK):
        assert chunks[chunk]["gets_per_download"] > 3.45
        assert chunks[chunk]["not_found_per_download"] > 1.39


def test_both_chunks_breach_the_default_ceiling(chunks):
    """70% and 45% against the 25% G3 ceiling, on 20 first reads each, so both
    are gated. Under the first-GET definition this run read as 3 breaches of
    51 chunks; by holder it is 41."""
    for chunk in (FILE_CHUNK, CONTENT_CHUNK):
        assert chunks[chunk]["gated"] is True
        assert chunks[chunk]["busiest_holder_share"] > 0.25
