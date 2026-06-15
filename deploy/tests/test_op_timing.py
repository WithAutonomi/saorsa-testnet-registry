import io
from types import SimpleNamespace

from rich.console import Console

from saorsa_deploy.provisioning.progress import (
    OpTimingHandler,
    _format_op_duration,
    print_op_timing_summary,
)


def _state(handler, op_meta):
    return SimpleNamespace(callback_handlers=[handler], op_meta=op_meta)


def _console():
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False), buf


def test_records_op_name_and_duration():
    h = OpTimingHandler()
    state = _state(h, {"h1": SimpleNamespace(names={"Install Telegraf"})})
    OpTimingHandler.operation_start(state, "h1")
    OpTimingHandler.operation_end(state, "h1")
    assert len(h.timings) == 1
    name, dur = h.timings[0]
    assert name == "Install Telegraf"
    assert dur >= 0


def test_end_without_start_is_ignored():
    h = OpTimingHandler()
    state = _state(h, {"h1": SimpleNamespace(names={"Whatever"})})
    OpTimingHandler.operation_end(state, "h1")
    assert h.timings == []


def test_records_multiple_ops_in_completion_order():
    h = OpTimingHandler()
    meta = {
        "a": SimpleNamespace(names={"First"}),
        "b": SimpleNamespace(names={"Second"}),
    }
    state = _state(h, meta)
    OpTimingHandler.operation_start(state, "a")
    OpTimingHandler.operation_end(state, "a")
    OpTimingHandler.operation_start(state, "b")
    OpTimingHandler.operation_end(state, "b")
    assert [n for n, _ in h.timings] == ["First", "Second"]


def test_print_summary_is_sorted_slowest_first_with_total():
    h = OpTimingHandler()
    h.timings = [("Fast op", 2.0), ("Slow op", 120.0), ("Mid op", 30.0)]
    console, buf = _console()
    print_op_timing_summary(console, h, title="nodes provision", connect_seconds=5.0)
    out = buf.getvalue()
    assert "Operation timings" in out
    assert "nodes provision" in out
    assert "(SSH connect)" in out
    assert "ops total" in out
    # Slowest op is listed before the fastest.
    assert out.index("Slow op") < out.index("Fast op")


def test_print_summary_noop_when_empty():
    h = OpTimingHandler()
    console, buf = _console()
    print_op_timing_summary(console, h, title="x")
    assert buf.getvalue() == ""


def test_format_op_duration():
    assert _format_op_duration(8.34) == "8.3s"
    assert _format_op_duration(60) == "1m 00s"
    assert _format_op_duration(134) == "2m 14s"
    assert _format_op_duration(3789) == "1h 03m 09s"
