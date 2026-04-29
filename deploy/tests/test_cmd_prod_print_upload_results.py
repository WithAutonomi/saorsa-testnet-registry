from saorsa_deploy.cmd.prod_print_upload_results import _fmt_host_summary


def test_fmt_host_summary_records_count():
    assert _fmt_host_summary([{}, {}]) == "2 record(s)"


def test_fmt_host_summary_empty():
    assert _fmt_host_summary([]) == "0 record(s)"
