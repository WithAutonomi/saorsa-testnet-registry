"""Grafana "Egress accounting" dashboard: accounted vs actual node egress (V2-1225).

On 2026-09-14 an 18-hour bandwidth episode on a prod node (5.4 -> 10.0 MB/s
sent, sustained) was invisible in every dashboard. The traffic-accounting
counters that would have shown it (V2-623) were correct -- they were simply
never plotted, because they lived in Elasticsearch while the host ``net`` byte
counters they must be divided by live in InfluxDB. The ES telegraf forwarder now
ALSO writes those summaries to Influx (``telegraf.py``,
``INFLUX_TRAFFIC_TARGET_PATTERNS``), and this module builds the five panels that
reconcile them against the NIC from that one bucket.

Everything here is pure: the builders return Grafana panel dicts and the
``MonitoringProvisioner`` writes the JSON. Kept out of ``monitoring.py`` only for
size; the panel signature ``(grid_pos, *, bucket)`` is the one
``_build_host_dashboard`` expects, so the dashboard shares its Role / Host
template variables with the host-statistics dashboards.

Three facts the Flux below is shaped by (measured on ant-prod-01's ES,
2026-09-17):

* The summaries are **cumulative per process, emitted every 5 minutes**. So each
  series is windowed with ``last`` and differenced with ``nonNegative`` (a
  restart resets the counter; the window in which that happens is dropped, not
  negated), then summed across a host's services. Windows are 10 minutes so
  every window holds at least one sample.
* ``bytes_relayed`` exists under **two** targets: ``saorsa_transport::relay::
  traffic`` (the relay-client leg, small) and ``saorsa_transport::masque::
  relay_server`` (the relay-server leg, the one that carries gigabytes). The
  ratio uses the relay-server one; every filter names target AND field.
* The ``tail`` points reach Influx with the **upper-cased** ``host`` tag (the ES
  forwarder upper-cases it for Elasticsearch, and processors run before every
  output) while ``net`` carries the lowercase hostname. The tail side is
  lower-cased in Flux before the join, which keeps the ES documents untouched.
"""

from __future__ import annotations

# Window over which the cumulative counters are differenced. Must be >= the
# summary cadence (5 min) so every window holds a sample.
WINDOW = "10m"
WINDOW_SECS = 600.0

TARGET_ENDPOINT = "saorsa_transport::traffic"
TARGET_MASQUE_RELAY_SERVER = "saorsa_transport::masque::relay_server"
TARGET_REPLICATION = "ant_node::replication::traffic"

#: The numerator of the coverage ratio, as (target, field) pairs. ``wire_tx_bytes``
#: (saorsa_core::traffic) and ``fetch_response_tx_bytes`` (replication) are
#: deliberately NOT here: they measure the same bytes at a higher layer and
#: would double-count.
ACCOUNTED_EGRESS_FIELDS: tuple[tuple[str, str], ...] = (
    (TARGET_ENDPOINT, "udp_tx_bytes"),
    (TARGET_MASQUE_RELAY_SERVER, "bytes_relayed"),
)

#: Coverage below this reads red on the headline panel.
COVERAGE_RED_BELOW_PERCENT = 80

#: Rows on the unaccounted-egress table.
UNACCOUNTED_TOP_N = 15

DATASOURCE = {"type": "influxdb", "uid": "influxdb"}

NUMERATOR_NOTE = (
    "### What is being reconciled\n\n"
    "**Accounted egress** = `udp_tx_bytes` (target `saorsa_transport::traffic`: "
    "bytes sent by QUIC connections on the tracked endpoint) **+** `bytes_relayed` "
    "(target `saorsa_transport::masque::relay_server`: bytes forwarded by this node "
    "as a MASQUE relay). **Actual egress** = the host's busiest non-loopback "
    "interface (`net.bytes_sent`). Both are cumulative counters differenced over "
    f"{WINDOW} windows and summed across the host's node services.\n\n"
    "`wire_tx_bytes` and `fetch_response_tx_bytes` are **deliberately excluded** "
    "from the ratio: they measure the same bytes at a higher layer and would "
    "double-count. Until V2-834 Part C.1 lands (relay connections counted both "
    "virtually and on the carrier) the numerator can over-count, so coverage is an "
    "**upper bound**.\n\n"
    "A restart resets a service's counters; that window is dropped for that "
    "service, not negated."
)


def _accounted_filter() -> str:
    clauses = " or ".join(
        f'(r["target"] == "{target}" and r["_field"] == "{field}")'
        for target, field in ACCOUNTED_EGRESS_FIELDS
    )
    return f"({clauses})"


def _counter_increase_per_host(
    bucket: str,
    field_filter: str,
    *,
    extra_group: tuple[str, ...] = (),
    stream: str,
) -> str:
    """Flux assigning ``stream`` = per-host bytes per window from ``tail`` counters.

    ``field_filter`` is a Flux boolean over ``r`` selecting the (target, field)
    pairs to include. Series are differenced per (host, service, target, field)
    and then summed per host (plus ``extra_group``) per window.
    """
    group_cols = ", ".join(f'"{c}"' for c in ("host", *extra_group))
    # After `difference` every series' rows sit on the same window boundaries,
    # so the cross-service sum is a plain per-(host, _time) sum. A second
    # aggregateWindow here would re-stamp each increase at the NEXT window's
    # stop (one window late against the NIC side of the join) and emit a bogus
    # zero row at the range end.
    return (
        f'{stream} = from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "tail")\n'
        f"  |> filter(fn: (r) => {field_filter})\n"
        "  |> map(fn: (r) => ({r with host: strings.toLower(v: r.host)}))\n"
        '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
        '  |> group(columns: ["host", "service_number", "target", "_field"])\n'
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        "  |> difference(nonNegative: true)\n"
        f'  |> group(columns: [{group_cols}, "_time"])\n'
        "  |> sum()\n"
        f"  |> group(columns: [{group_cols}])\n"
    )


def _nic_increase_per_host(bucket: str, *, stream: str) -> str:
    """Flux assigning ``stream`` = per-host NIC bytes sent per window.

    ``net.bytes_sent`` is per interface and interface names differ by provider
    (``eth0``, ``ens3``, ...), so the busiest non-loopback interface per window
    stands in for "the public NIC" rather than a hard-coded name. ``all`` is the
    protocol-counter pseudo-interface telegraf emits and carries no bytes.
    """
    return (
        f'{stream} = from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "net" and r["_field"] == "bytes_sent")\n'
        '  |> filter(fn: (r) => r["interface"] != "lo" and r["interface"] != "all")\n'
        '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
        '  |> group(columns: ["host", "interface"])\n'
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        "  |> difference(nonNegative: true)\n"
        '  |> group(columns: ["host", "_time"])\n'
        "  |> max()\n"
        '  |> group(columns: ["host"])\n'
        '  |> keep(columns: ["_time", "_value", "host"])\n'
    )


def _joined_accounted_and_nic(bucket: str) -> str:
    return (
        'import "strings"\n\n'
        + _counter_increase_per_host(bucket, _accounted_filter(), stream="accounted")
        + "\n"
        + _nic_increase_per_host(bucket, stream="nic")
        + "\n"
        'joined = join(tables: {a: accounted, n: nic}, on: ["_time", "host"])\n'
    )


def _timeseries(
    title: str,
    grid_pos: dict,
    query: str,
    *,
    unit: str,
    description: str,
    thresholds: dict | None = None,
    legend_calcs: tuple[str, ...] = ("mean", "last"),
    display_name: str = "${__field.labels.host}",
    max_value: float | None = None,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "displayName": display_name,
        "custom": {"lineWidth": 1, "fillOpacity": 0},
    }
    if max_value is not None:
        defaults["min"] = 0
        defaults["max"] = max_value
    if thresholds is not None:
        defaults["thresholds"] = thresholds
        defaults["custom"]["thresholdsStyle"] = {"mode": "line+area"}
    return {
        "title": title,
        "description": description,
        "type": "timeseries",
        "datasource": DATASOURCE,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "gridPos": grid_pos,
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "calcs": list(legend_calcs),
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [{"query": query, "refId": "A"}],
    }


def panel_coverage(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 1: accounted / actual egress, per host, as a percentage."""
    query = _joined_accounted_and_nic(bucket) + (
        "joined\n"
        "  |> map(fn: (r) => ({r with\n"
        "    _value: if float(v: r._value_n) > 0.0\n"
        "      then 100.0 * float(v: r._value_a) / float(v: r._value_n)\n"
        "      else 0.0,\n"
        "  }))\n"
        '  |> keep(columns: ["_time", "_value", "host"])\n'
        '  |> yield(name: "coverage")'
    )
    return _timeseries(
        "Egress accounting coverage (%)",
        grid_pos,
        query,
        unit="percent",
        max_value=120,
        description=(
            "(udp_tx_bytes + bytes_relayed) / net.bytes_sent per host, from the "
            f"cumulative counters over {WINDOW} windows. The legend's mean is the "
            "figure for the selected range. Red below "
            f"{COVERAGE_RED_BELOW_PERCENT}%: most of the host's egress is bytes "
            "no counter explains. Upper bound until V2-834 C.1."
        ),
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "red", "value": None},
                {"color": "green", "value": COVERAGE_RED_BELOW_PERCENT},
            ],
        },
    )


def panel_unaccounted(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 2: the absolute gap as a rate, mean over the range, top-N hosts."""
    query = _joined_accounted_and_nic(bucket) + (
        "joined\n"
        "  |> map(fn: (r) => ({r with\n"
        f"    _value: (float(v: r._value_n) - float(v: r._value_a)) / {WINDOW_SECS},\n"
        "  }))\n"
        "  |> mean()\n"
        "  |> group()\n"
        '  |> sort(columns: ["_value"], desc: true)\n'
        f"  |> limit(n: {UNACCOUNTED_TOP_N})\n"
        '  |> keep(columns: ["host", "_value"])\n'
        '  |> yield(name: "unaccounted")'
    )
    return {
        "title": f"Unaccounted egress (mean over range, top {UNACCOUNTED_TOP_N})",
        "description": (
            "net.bytes_sent − (udp_tx_bytes + bytes_relayed) as a rate, averaged "
            "over the selected range. The absolute gap, which is what costs money."
        ),
        "type": "table",
        "datasource": DATASOURCE,
        "fieldConfig": {
            "defaults": {"unit": "Bps"},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "_value"},
                    "properties": [
                        {"id": "displayName", "value": "Unaccounted egress"},
                        {"id": "custom.width", "value": 220},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "host"},
                    "properties": [{"id": "displayName", "value": "Host"}],
                },
            ],
        },
        "gridPos": grid_pos,
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Unaccounted egress", "desc": True}],
        },
        "targets": [{"query": query, "refId": "A"}],
    }


def _rate_query(bucket: str, field_filter: str, *, extra_group: tuple[str, ...] = ()) -> str:
    return (
        'import "strings"\n\n'
        + _counter_increase_per_host(
            bucket, field_filter, extra_group=extra_group, stream="bytes"
        )
        + "\nbytes\n"
        f"  |> map(fn: (r) => ({{r with _value: float(v: r._value) / {WINDOW_SECS}}}))\n"
        '  |> yield(name: "rate")'
    )


def panel_relay_throughput(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 3: MASQUE relay-server throughput per host, with the direction split."""
    field_filter = (
        f'r["target"] == "{TARGET_MASQUE_RELAY_SERVER}" and '
        '(r["_field"] == "bytes_relayed" or r["_field"] == "forwarded_to_client_bytes" '
        'or r["_field"] == "forwarded_to_target_bytes")'
    )
    return _timeseries(
        "Relay throughput (MASQUE relay server)",
        grid_pos,
        _rate_query(bucket, field_filter, extra_group=("_field",)),
        unit="Bps",
        display_name="${__field.labels.host} ${__field.labels._field}",
        description=(
            "Rate of bytes_relayed per host (target saorsa_transport::masque::"
            "relay_server), with its forwarded_to_client_bytes / "
            "forwarded_to_target_bytes split. bytes_relayed is the relay leg "
            "counted in the coverage numerator."
        ),
    )


def panel_active_reservations(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 4: active MASQUE relay reservations per host (summed over services)."""
    query = (
        'import "strings"\n\n'
        f'from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "tail")\n'
        f'  |> filter(fn: (r) => r["target"] == "{TARGET_MASQUE_RELAY_SERVER}" '
        'and r["_field"] == "active_reservations")\n'
        "  |> map(fn: (r) => ({r with host: strings.toLower(v: r.host)}))\n"
        '  |> filter(fn: (r) => r["host"] =~ /^$host$/)\n'
        '  |> group(columns: ["host", "service_number"])\n'
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        '  |> group(columns: ["host", "_time"])\n'
        "  |> sum()\n"
        '  |> group(columns: ["host"])\n'
        '  |> yield(name: "reservations")'
    )
    return _timeseries(
        "Active relay reservations",
        grid_pos,
        query,
        unit="short",
        legend_calcs=("last", "max"),
        description=(
            "active_reservations (target saorsa_transport::masque::relay_server), "
            "summed across the host's node services. A gauge, not a counter."
        ),
    )


def panel_replication_serving(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 5: bytes served in replication fetch responses, per host."""
    field_filter = (
        f'r["target"] == "{TARGET_REPLICATION}" and r["_field"] == "fetch_response_tx_bytes"'
    )
    return _timeseries(
        "Replication serving",
        grid_pos,
        _rate_query(bucket, field_filter),
        unit="Bps",
        description=(
            "Rate of fetch_response_tx_bytes per host (target ant_node::"
            "replication::traffic). Attribution only -- NOT part of the coverage "
            "numerator, since these bytes are already inside udp_tx_bytes."
        ),
    )


def panel_numerator_note(grid_pos: dict, *, bucket: str) -> dict:  # noqa: ARG001
    """The numerator definition, on the dashboard itself."""
    return {
        "title": "Numerator definition",
        "type": "text",
        "gridPos": grid_pos,
        "options": {"mode": "markdown", "content": NUMERATOR_NOTE},
    }


#: Panel specs in dashboard order, in the ``(panel_fn, kwargs)`` shape
#: ``MonitoringProvisioner._build_host_dashboard`` lays out.
EGRESS_ACCOUNTING_PANEL_SPECS: tuple[tuple, ...] = (
    (panel_coverage, {"h": 10}),
    (panel_unaccounted, {"h": 10}),
    (panel_relay_throughput, {}),
    (panel_active_reservations, {}),
    (panel_replication_serving, {}),
    (panel_numerator_note, {"h": 7}),
)

DASHBOARD_TITLE = "Egress accounting"
DASHBOARD_UID = "ant-egress-accounting"
DASHBOARD_TAG = "egress-accounting"
