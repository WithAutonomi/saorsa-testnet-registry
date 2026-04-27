from saorsa_deploy.cmd.prod_print_upload_results import _fmt_host_summary


def _op(**kw):
    base = {"store_concurrency": 8}
    base.update(kw)
    return base


def test_fmt_host_summary_single_concurrency():
    ops = [_op(store_concurrency=8), _op(store_concurrency=8)]
    assert _fmt_host_summary(ops) == "concurrency=8, 2 record(s)"


def test_fmt_host_summary_omits_concurrency_when_all_missing():
    ops = [_op(store_concurrency=None), _op(store_concurrency=None)]
    assert _fmt_host_summary(ops) == "2 record(s)"


def test_fmt_host_summary_shows_set_when_mixed():
    ops = [
        _op(store_concurrency=8),
        _op(store_concurrency=16),
        _op(store_concurrency=8),
    ]
    assert _fmt_host_summary(ops) == "concurrency={8,16}, 3 record(s)"


def test_fmt_host_summary_handles_partial_missing():
    ops = [_op(store_concurrency=8), _op(store_concurrency=None)]
    assert _fmt_host_summary(ops) == "concurrency=8, 2 record(s)"
