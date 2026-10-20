"""Forwarding the browser harness's trace records to Elasticsearch (V2-1650).

Three rendered artefacts have to line up or the records arrive somewhere the
report cannot query, which is indistinguishable from a cohort that read nothing:

* the harness's trace file is in the directory telegraf tails, and is in the
  browser role's forward list;
* the rendered telegraf config gives that one file a JSON tail input, naming
  every string field — telegraf DROPS a string field it was not told about, so
  a missing `target` or `peer` would leave every document present and the
  per-chunk table empty;
* the ES index template types those fields under `browser_trace`, which is
  where telegraf actually puts them.

Also asserted: every other role's rendered config is byte-identical to what it
was, because the JSON input is emitted only when a caller asks for one.
"""

from saorsa_deploy.client_roles import ROLE_BROWSER_DOWNLOAD
from saorsa_deploy.provisioning import browser_client as browser
from saorsa_deploy.provisioning.client import AntClientProvisioner
from saorsa_deploy.provisioning.elastic import ElasticProvisioner
from saorsa_deploy.provisioning.telegraf_client import ClientTelegrafProvisioner

TRACE_LOG = "/var/log/ant-client-forward/browser-trace.jsonl"


def _provisioner(role, globs, json_globs=None):
    return ClientTelegrafProvisioner(
        host_ips=["1.2.3.4"],
        ssh_key_path="/x",
        testnet_name="DEV-01",
        elasticsearch_url="http://es:9200",
        elasticsearch_username="u",
        elasticsearch_password="pw",
        role=role,
        tail_globs=globs,
        json_tail_globs=json_globs,
    )


def _browser_config():
    return _provisioner(
        "browser_downloader",
        [browser.BROWSER_STDOUT_LOG, browser.BROWSER_STDERR_LOG],
        [browser.BROWSER_TRACE_LOG],
    )._build_telegraf_config()


# --------------------------------------------------------------------------- #
# the harness's file, and the role that forwards it
# --------------------------------------------------------------------------- #
def test_the_trace_log_is_in_the_directory_telegraf_tails():
    """Not under /var/lib: the records are forwarded as they are produced, so
    they have to be where the forwarder looks. Nothing creates the old trace
    directory any more."""
    assert browser.BROWSER_TRACE_LOG == TRACE_LOG
    assert browser.BROWSER_TRACE_LOG.startswith(browser.BROWSER_FORWARD_LOG_DIR + "/")
    assert not hasattr(browser, "BROWSER_CLIENT_TRACE_DIR")
    assert "trace" not in browser.build_bundle_install_command("http://b/x.tar.gz")


def test_the_browser_role_forwards_the_trace_as_json_and_the_streams_as_text():
    """A role missing from either map forwards NOTHING while reporting nothing
    wrong, which is how both WASM roles silently forwarded nothing for a
    release."""
    text, label = AntClientProvisioner._FORWARD_ROLE_FILES[ROLE_BROWSER_DOWNLOAD]
    assert label == "browser_downloader"
    assert text == [browser.BROWSER_STDOUT_LOG, browser.BROWSER_STDERR_LOG]
    assert browser.BROWSER_TRACE_LOG not in text, "JSON lines must not go through the text input"
    assert AntClientProvisioner._FORWARD_ROLE_JSON_FILES[ROLE_BROWSER_DOWNLOAD] == [
        browser.BROWSER_TRACE_LOG
    ]


def test_no_other_role_has_a_json_input():
    assert set(AntClientProvisioner._FORWARD_ROLE_JSON_FILES) == {ROLE_BROWSER_DOWNLOAD}


# --------------------------------------------------------------------------- #
# the rendered telegraf config
# --------------------------------------------------------------------------- #
def test_the_browser_config_has_a_second_tail_input_for_the_trace():
    cfg = _browser_config()
    assert cfg.count("[[inputs.tail]]") == 2
    assert f'files = ["{TRACE_LOG}"]' in cfg
    assert 'data_format = "json"' in cfg
    assert 'name_override = "browser_trace"' in cfg


def test_the_json_string_fields_are_exactly_the_record_strings():
    """Telegraf's JSON parser keeps numbers automatically and drops every other
    string it was not told to keep. A key missing here arrives nowhere, and for
    `target` or `peer` that leaves the report with no chunk and no holder while
    every document still looks present."""
    cfg = _browser_config()
    assert (
        'json_string_fields = ["operation", "target", "peer", "phase", '
        '"detail", "address", "seeds"]' in cfg
    )
    assert 'tag_keys = ["operation", "phase"]' in cfg


def test_the_stdout_input_is_unchanged_by_the_trace_input():
    """The text half still captures each raw line into `message`."""
    cfg = _browser_config()
    assert f'files = ["{browser.BROWSER_STDOUT_LOG}", "{browser.BROWSER_STDERR_LOG}"]' in cfg
    assert 'data_format = "value"' in cfg
    assert 'data_type = "string"' in cfg
    assert 'dest = "message"' in cfg


def test_the_trace_file_is_tagged_stream_trace_and_checked_first():
    """`.jsonl` is neither stdout nor stderr, and the else branch would label it
    `stdout` -- which is what every query for the raw client log filters on."""
    cfg = _browser_config()
    starlark = cfg[cfg.index("[[processors.starlark]]") :]
    assert 'if p.endswith(".jsonl"):' in starlark
    assert 'metric.tags["stream"] = "trace"' in starlark
    assert starlark.index('.jsonl"') < starlark.index('.stderr"')


def test_a_role_without_json_globs_renders_the_config_it_always_did():
    """Every existing role's forwarder is byte-identical, so this change cannot
    alter what a native uploader ships."""
    cfg = _provisioner("uploader", ["/var/log/ant-client-forward/uploader.stdout.log"])
    rendered = cfg._build_telegraf_config()
    assert rendered.count("[[inputs.tail]]") == 1
    assert "browser_trace" not in rendered
    assert 'data_format = "json"' not in rendered
    for placeholder in ("__TRACE_INPUT__", "__FILES__", "__FROM_BEGINNING__"):
        assert placeholder not in rendered


def test_the_browser_forwarder_buffers_two_hundred_thousand_metrics():
    """~3,000 records per download and four instances writing at once."""
    cfg = _browser_config()
    assert "metric_buffer_limit = 200000" in cfg
    assert "max_undelivered_lines = 50000" in cfg


def test_no_placeholder_survives_in_the_browser_config():
    cfg = _browser_config()
    for placeholder in (
        "__TRACE_INPUT__",
        "__FILES__",
        "__FROM_BEGINNING__",
        "__METRIC_BUFFER_LIMIT__",
        "__MAX_UNDELIVERED_LINES__",
    ):
        assert placeholder not in cfg


# --------------------------------------------------------------------------- #
# the index template
# --------------------------------------------------------------------------- #
def _setup_script():
    p = ElasticProvisioner(host_ip="1.2.3.4", ssh_key_path="/x", testnet_name="DEV-01")
    script = p._build_setup_script()
    compile(script, "es_setup.py", "exec")  # the provisioner runs it as Python
    return script


def test_the_client_template_types_the_trace_fields_under_the_measurement():
    """Under `browser_trace`, because telegraf nests a metric's fields under its
    measurement name -- the same reason the node message is `tail.message` and
    the template's own top-level `message` property matches no document."""
    script = _setup_script()
    block = script[script.index("'browser_trace': {'properties': {") :]
    block = block[: block.index("}},")]
    for field in ("target", "peer", "address"):
        assert f"'{field}': {{'type': 'keyword'}}" in block
    for field in ("round_id", "instance", "hot_file_index", "seq", "id", "boundary", "started_at"):
        assert f"'{field}': {{'type': 'long'}}" in block
    for field in ("detail", "seeds"):
        assert f"'{field}': {{'type': 'text'}}" in block


def test_elapsed_ms_is_a_double():
    """Load-bearing: telegraf serialises a whole float as `0`, so the first
    document would infer `long` and `ignore_malformed` would then DROP every
    fractional value -- silently, which reads as the field being absent."""
    assert "'elapsed_ms': {'type': 'double'}" in _setup_script()


def test_the_tag_fields_are_left_to_dynamic_mapping():
    """`operation` and `phase` are telegraf TAGS, so they land under `tag.*`. An
    explicit keyword mapping there would remove the `.keyword` subfield that
    every `tag.*.keyword` query in `es_log_counts` and `analysis/es.py` reads."""
    script = _setup_script()
    block = script[script.index("/_index_template/ant-client-logs") :]
    block = block[: block.index("Client index template created")]
    assert "'tag':" not in block


def test_the_existing_client_mappings_are_untouched():
    script = _setup_script()
    block = script[script.index("/_index_template/ant-client-logs") :]
    block = block[: block.index("Client index template created")]
    for field in ("host", "role", "stream", "client_index", "testnet_name", "host_role"):
        assert f"'{field}': {{'type': 'keyword'}}" in block
    assert "'message': {'type': 'text'}" in block
    assert "'index.mapping.ignore_malformed': True" in block


def test_the_report_filters_on_the_tag_keyword_subfields():
    """`tag.*` is analysed, so a `term` on the bare field matches nothing while
    still exiting 0 -- the trap `es_log_counts` documents for `tag.level`."""
    from saorsa_deploy import browser_traces as bt

    filters = bt.trace_query_body([1, 2])["query"]["bool"]["filter"]
    fields = [next(iter(next(iter(f.values())))) for f in filters]
    assert fields == [
        "tag.role.keyword",
        "tag.stream.keyword",
        "tag.operation.keyword",
        "browser_trace.round_id",
    ]


def test_the_phase_is_not_a_filter_but_is_fetched():
    """Every phase is needed: `start` is the GET, `finish` carries `found` or
    `NotFound`, and the `discovery` `finish` is the moment a read is early
    relative to. Filtering on one would silently halve the arithmetic."""
    from saorsa_deploy import browser_traces as bt

    body = bt.trace_query_body([1])
    assert "tag.phase" in body["_source"]
    assert "phase" not in repr(body["query"])


def test_both_rpc_and_discovery_are_queried():
    """`decided_before_discovery` has no other source than the `discovery`
    `finish`, and on `rpc` alone it would read 100% on every run."""
    from saorsa_deploy import browser_traces as bt

    filters = bt.trace_query_body([1])["query"]["bool"]["filter"]
    ops = next(f["terms"]["tag.operation.keyword"] for f in filters if "terms" in f)
    assert sorted(ops) == ["discovery", "rpc"]
