"""`deployments print-uploads` / `print-downloads` must cover the WASM tier.

These commands are the operator's live view of a running deployment, and the
issue procedure for a WASM run says to "check the two tiers separately". They
previously queried only `testnet-metrics`, so a WASM tier that was failing
100% of its operations rendered identically to one that had never been
configured — the exact failure mode the procedure is meant to catch.
"""

from unittest.mock import patch

import pytest
from rich.console import Console

from saorsa_deploy.cmd.print_file_ops import (
    _build_transport_map,
    _print_transport_totals,
)
from saorsa_deploy.influx import fetch_all_operation_points


class TestBothBucketsAreQueried:
    def test_wasm_bucket_is_queried_alongside_the_native_one(self):
        with patch(
            "saorsa_deploy.influx.fetch_operation_points", return_value=[]
        ) as fetch:
            fetch_all_operation_points(
                url="u", token="t", org="o",
                bucket="testnet-metrics", wasm_bucket="wasm-client-metrics",
                start="0",
            )
        buckets = [c.kwargs["bucket"] for c in fetch.call_args_list]
        assert buckets == ["testnet-metrics", "wasm-client-metrics"]

    def test_wasm_operations_reach_the_caller(self):
        native = {"host": "n1", "start_ns": 1, "_measurement": "upload"}
        wasm = {"host": "w1", "start_ns": 2, "_measurement": "upload"}
        with patch(
            "saorsa_deploy.influx.fetch_operation_points",
            side_effect=[[native], [wasm]],
        ):
            out = fetch_all_operation_points(
                url="u", token="t", org="o",
                bucket="testnet-metrics", wasm_bucket="wasm-client-metrics",
                start="0",
            )
        assert {p["host"] for p in out} == {"n1", "w1"}

    def test_querying_the_wasm_bucket_directly_does_not_double_query(self):
        with patch(
            "saorsa_deploy.influx.fetch_operation_points", return_value=[]
        ) as fetch:
            fetch_all_operation_points(
                url="u", token="t", org="o",
                bucket="wasm-client-metrics",
                wasm_bucket="wasm-client-metrics", start="0",
            )
        assert fetch.call_count == 1


class TestTransportMap:
    """A testnet's transport is per-VM, resolved from each client's role."""

    def test_hosts_map_to_their_transport(self):
        state = {"clients": [
            {"role": "upload", "region": "UK1", "client_index": 1},
            {"role": "wasm-upload", "region": "SBG5", "client_index": 1},
            {"role": "wasm-download", "region": "tor1", "client_index": 1},
        ]}
        with patch(
            "saorsa_deploy.cmd.print_file_ops.load_client_state",
            return_value=state,
        ), patch("saorsa_deploy.registry.get_repository", return_value=None):
            out = _build_transport_map("DEV-03")
        assert out["DEV-03-ant-client-upload-UK1-1"] == "native"
        assert out["DEV-03-ant-client-wasm-upload-SBG5-1"] == "wasm"
        assert out["DEV-03-ant-client-wasm-download-tor1-1"] == "wasm"

    def test_missing_client_state_yields_no_labels(self):
        # Better to say nothing than to guess a transport.
        with patch(
            "saorsa_deploy.cmd.print_file_ops.load_client_state",
            side_effect=RuntimeError("no state"),
        ), patch("saorsa_deploy.registry.get_repository", return_value=None):
            assert _build_transport_map("DEV-03") == {}

    def test_a_prod_download_kind_is_labelled_from_deploy_args(self):
        """The VM's role is the native `download` there, so the role lies.

        This is what printed `transport=native` for a live production WASM run
        whose rows had been recorded, correctly, as wasm.
        """
        from types import SimpleNamespace

        repo = SimpleNamespace(
            get_active_by_name=lambda n: SimpleNamespace(
                deploy_args={"kind": "prod-downloaders", "client_kind": "wasm"}
            ),
            get_by_name=lambda n: None,
        )
        with patch("saorsa_deploy.registry.get_repository", return_value=repo):
            out = _build_transport_map("PROD-WASM-DL-01")
        # Every host it wrote points from ran the one transport.
        assert out.get("PROD-WASM-DL-01-ant-client-download-lon1-1") == "wasm"
        assert out.get("any-other-host") == "wasm"


class TestTransportTotals:
    def _render(self, by_host, transports):
        console = Console(record=True, width=100)
        _print_transport_totals("upload", by_host, transports, console)
        return console.export_text()

    def test_the_two_tiers_are_never_blended(self):
        # status: 0 = success, 1 = failure.
        by_host = {
            "h-native": [{"status": 0}, {"status": 0}, {"status": 0}],
            "h-wasm": [{"status": 1}, {"status": 1}],
        }
        transports = {"h-native": "native", "h-wasm": "wasm"}
        out = self._render(by_host, transports)
        assert "native 3/3 = 100.00%" in " ".join(out.split())
        assert "wasm 0/2 = 0.00%" in " ".join(out.split())

    def test_a_native_only_run_renders_exactly_as_before(self):
        by_host = {"h-native": [{"status": 0}]}
        assert self._render(by_host, {"h-native": "native"}) == ""

    def test_hosts_with_no_known_transport_are_not_guessed_into_a_tier(self):
        by_host = {"h-native": [{"status": 0}], "h-mystery": [{"status": 1}]}
        # Only one known tier -> nothing printed, and the unknown host is never
        # silently counted as native.
        assert self._render(by_host, {"h-native": "native"}) == ""

    def test_skips_leave_the_denominator_and_are_reported(self):
        # status 2 is a transport refusal, not an attempt: the content is on the
        # network and healthy, so it must not depress the rate.
        by_host = {
            "h-native": [{"status": 0}],
            "h-wasm": [{"status": 0}, {"status": 2}, {"status": 2}],
        }
        out = " ".join(
            self._render(
                by_host, {"h-native": "native", "h-wasm": "wasm"}
            ).split()
        )
        assert "wasm 1/1 = 100.00%, 2 skipped (over-cap)" in out


if __name__ == "__main__":
    pytest.main([__file__])
