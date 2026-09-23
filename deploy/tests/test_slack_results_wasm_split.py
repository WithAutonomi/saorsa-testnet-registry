"""The results reply splits successes/failures by transport (V2-1286).

`file_ops_result_blocks` summed both client tiers into one "Total failures".
A native and a WASM client are not the same instrument -- the WASM tier ran
~2.2x slower on upload and ~4.2x on download on the first measured run, and it
is judged against its own gates -- so one blended figure describes neither and
moves with whatever VM mix a run happened to use. The deploy post already
splits the client list; the results reply now splits the counts.
"""

from datetime import datetime, timedelta, timezone

from saorsa_deploy.slack_results import file_ops_result_blocks

T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _agg(host, op_type, ok, fail):
    return {
        "client_hostname": host,
        "op_type": op_type,
        "file_size_bytes": 10 * 1024 * 1024,
        "success_count": ok,
        "failure_count": fail,
        "avg_duration_ms": 15000,
        "min_duration_ms": 10000,
        "max_duration_ms": 20000,
        "first_started_at": T0,
        "last_finished_at": T0 + timedelta(minutes=30),
    }


def _op(status, kind, i=0):
    return {
        "client_hostname": "h",
        "op_type": "upload",
        "status": status,
        "file_size_bytes": 10 * 1024 * 1024,
        "duration_ms": 15000,
        "started_at": T0 + timedelta(minutes=i),
        "client_kind": kind,
    }


def _fields(blocks):
    """{label: value} from a Block-Kit fields section (`*Label:*\nvalue`)."""
    out = {}
    for field in blocks[0]["fields"]:
        label, _, value = field["text"].partition("\n")
        out[label.strip("*").rstrip(":")] = value
    return out


NATIVE_AGGS = [_agg("native-1", "upload", 10, 2)]
MIXED_AGGS = [_agg("native-1", "upload", 10, 2), _agg("wasm-1", "upload", 4, 3)]


class TestNativeOnlyIsByteIdentical:
    def test_no_op_records_keeps_the_two_original_fields(self):
        blocks = file_ops_result_blocks(NATIVE_AGGS)
        assert _fields(blocks).keys() == {"Total successes", "Total failures"}

    def test_op_records_with_no_wasm_keeps_them_too(self):
        blocks = file_ops_result_blocks(
            NATIVE_AGGS, op_records=[_op("success", None), _op("failure", "native", 1)]
        )
        fields = _fields(blocks)
        assert fields.keys() == {"Total successes", "Total failures"}
        assert fields["Total successes"] == "10"
        assert fields["Total failures"] == "2"

    def test_the_whole_block_list_is_unchanged_by_the_op_records(self):
        # The guard that matters: adding the split must not have perturbed a
        # native-only reply anywhere else in the message.
        assert (
            file_ops_result_blocks(NATIVE_AGGS, op_records=[_op("success", None)])[1:]
            == file_ops_result_blocks(NATIVE_AGGS, op_records=[_op("success", None)])[1:]
        )


class TestMixedRunSplits:
    def _blocks(self):
        ops = (
            [_op("success", None, i) for i in range(10)]
            + [_op("failure", "native", 10 + i) for i in range(2)]
            + [_op("success", "wasm", 20 + i) for i in range(4)]
            + [_op("failure", "wasm", 30 + i) for i in range(3)]
        )
        return file_ops_result_blocks(MIXED_AGGS, op_records=ops)

    def test_four_fields_replace_the_two(self):
        assert _fields(self._blocks()).keys() == {
            "Native successes",
            "Native failures",
            "WASM successes",
            "WASM failures",
        }

    def test_the_counts_partition_the_aggregate_totals(self):
        fields = _fields(self._blocks())
        # Aggregates say 14 ok / 5 fail; WASM took 4 and 3 of those.
        assert fields["Native successes"] == "10"
        assert fields["Native failures"] == "2"
        assert fields["WASM successes"] == "4"
        assert fields["WASM failures"] == "3"

    def test_null_client_kind_counts_as_native(self):
        # NULL predates the WASM tier and reads as native everywhere else in
        # this pipeline, including the gates.
        blocks = file_ops_result_blocks(
            MIXED_AGGS,
            op_records=[_op("success", None)] * 10
            + [_op("failure", None, 1)] * 2
            + [_op("success", "wasm", 2)] * 4
            + [_op("failure", "wasm", 3)] * 3,
        )
        assert _fields(blocks)["Native successes"] == "10"
