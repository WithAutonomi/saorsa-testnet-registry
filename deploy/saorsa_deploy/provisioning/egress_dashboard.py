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

Facts the Flux below is shaped by, measured on ant-prod-01's ES (2026-09-17)
and on a DEV-01 run of the V2-834 branch (2026-09-18):

* The summaries are **cumulative per process, emitted every 5 minutes**. So each
  series is windowed with ``last`` and differenced with ``nonNegative`` (a
  restart resets the counter; the window in which that happens is dropped, not
  negated), then summed across a host's services. Windows are 10 minutes so
  every window holds at least one sample.
* **One series can carry several log lines per tick.** The V2-834 build emits
  the endpoint summary once per endpoint -- two lines ~30 ms apart, one with
  ``udp_tx_bytes = 0`` -- and ``saorsa_transport::relay::traffic`` interleaves
  per-session lines with the summary under the same field names. Differenced
  as one series that flaps (``0, X, 0, X``) and ``nonNegative`` then keeps +X
  every tick: measured at 25x the NIC. So every series is first collapsed per
  **emission burst** (a 1-minute window): ``sum`` for per-endpoint counters,
  ``max`` for process-wide counters printed on more than one line and for a
  summary that shares field names with per-session lines.
* ``bytes_relayed`` exists under **two** targets: ``saorsa_transport::relay::
  traffic`` (the stream relay server, raw UDP sockets, dominant on the V2-834
  build) and ``saorsa_transport::masque::relay_server`` (the MASQUE
  reservation relay, dominant on the shipped build; its summary is only emitted
  from the CONNECT path, so an idle relay has no line at all). Both are relay
  legs outside the QUIC endpoint's ``udp_tx_bytes``, so the ratio adds both.
* The ``tail`` points reach Influx with the **upper-cased** ``host`` tag (the ES
  forwarder upper-cases it for Elasticsearch, and processors run before every
  output) while ``net`` carries the OS hostname -- lowercase on prod, but a
  testnet keeps its deployment name's case (``DEV-01-ant-node-lon1-1``). Both
  sides are lower-cased before the join.
* On a NAT-simulated VM the root namespace sees ``veth-nat``, whose
  ``bytes_sent`` is traffic sent INTO the node's netns -- the node's inbound.
  The NIC side therefore excludes ``veth*`` and takes the busiest remaining
  non-loopback interface per window (interface names differ by provider).
"""

from __future__ import annotations

# Window over which the cumulative counters are differenced. Must be >= the
# summary cadence (5 min) so every window holds a sample.
WINDOW = "10m"
WINDOW_SECS = 600.0

# Window that collapses the lines one series emits within a single 5-minute
# tick (they land ~30 ms apart) into one sample. Must be < the cadence.
BURST_WINDOW = "1m"

TARGET_ENDPOINT = "saorsa_transport::traffic"
TARGET_RELAY = "saorsa_transport::relay::traffic"
TARGET_MASQUE_RELAY_SERVER = "saorsa_transport::masque::relay_server"
TARGET_REPLICATION = "ant_node::replication::traffic"
TARGET_CORE = "saorsa_core::traffic"
TARGET_STORAGE = "ant_node::storage::traffic"

#: The numerator of the coverage ratio, as (target, field, burst_fn) triples.
#: ``wire_tx_bytes`` (saorsa_core::traffic) and ``fetch_response_tx_bytes``
#: (replication) are deliberately NOT here: they measure the same bytes at a
#: higher layer and would double-count. ``udp_tx_bytes`` is per endpoint (sum
#: the burst); the relay fields are totals that share names with per-session
#: lines (max the burst).
ACCOUNTED_EGRESS_FIELDS: tuple[tuple[str, str, str], ...] = (
    (TARGET_ENDPOINT, "udp_tx_bytes", "sum"),
    # V2-834 splits a NAT'd node's traffic to its relay out of udp_tx_bytes
    # into this per-endpoint counter (a real socket; the virtual connections
    # riding on it are virtual_udp_tx_bytes and are NOT added). Absent on older
    # builds, where those bytes were still inside udp_tx_bytes. Measured on a
    # NAT'd DEV-01 node: coverage 5% without it, ~85% with it.
    (TARGET_ENDPOINT, "relay_carrier_udp_tx_bytes", "sum"),
    (TARGET_RELAY, "bytes_relayed", "max"),
    (TARGET_MASQUE_RELAY_SERVER, "bytes_relayed", "max"),
)

#: What the V2-834 build's socket-boundary counters say the process sent /
#: received on real UDP sockets -- the ground truth its acceptance gate
#: ("within 2% of the NIC") is measured by. ``sock_*`` is process-wide and
#: printed on every endpoint summary line, hence ``max``. The relay server's
#: per-session sockets are outside it: their ingress is ``raw_rx_bytes`` on the
#: relay summary (V2-834 Part A.4; measured on DEV-01 bootstraps: ingress
#: coverage 21-33% without it), their egress is already in ``bytes_relayed`` /
#: ``sock_tx_bytes``. Absent on older builds.
GROUND_TRUTH_TX: tuple[tuple[str, str, str], ...] = (
    (TARGET_ENDPOINT, "sock_tx_bytes", "max"),
)
GROUND_TRUTH_RX: tuple[tuple[str, str, str], ...] = (
    (TARGET_ENDPOINT, "sock_rx_bytes", "max"),
    (TARGET_RELAY, "raw_rx_bytes", "max"),
)

#: Egress decomposition: (label, target, field, burst_fn). Labels are what the
#: legend shows. Fields absent on a build simply produce no series.
EGRESS_DECOMPOSITION: tuple[tuple[str, str, str, str], ...] = (
    ("DHT / wire messaging (wire_tx_bytes)", TARGET_CORE, "wire_tx_bytes", "max"),
    ("chunk GET serving (get_success_tx_bytes)", TARGET_STORAGE, "get_success_tx_bytes", "max"),
    ("replication fetch responses (fetch_response_tx_bytes)", TARGET_REPLICATION,
     "fetch_response_tx_bytes", "max"),
    ("relay server (bytes_relayed)", TARGET_RELAY, "bytes_relayed", "max"),
    ("MASQUE relay server (bytes_relayed)", TARGET_MASQUE_RELAY_SERVER, "bytes_relayed", "max"),
    ("relay client stream (stream_tx_bytes)", TARGET_ENDPOINT, "stream_tx_bytes", "max"),
    ("QUIC retransmission (lost_bytes)", TARGET_ENDPOINT, "lost_bytes", "sum"),
    ("stateless packets (stateless_tx_bytes)", TARGET_ENDPOINT, "stateless_tx_bytes", "max"),
    ("failed dials (failed_dial_tx_bytes)", TARGET_ENDPOINT, "failed_dial_tx_bytes", "max"),
)

#: Coverage below this reads red on the headline panel.
COVERAGE_RED_BELOW_PERCENT = 80

#: Rows on the unaccounted-egress table.
UNACCOUNTED_TOP_N = 15

DATASOURCE = {"type": "influxdb", "uid": "influxdb"}

NUMERATOR_NOTE = (
    "### What is being reconciled\n\n"
    "**Accounted egress** = `udp_tx_bytes` (target `saorsa_transport::traffic`: "
    "bytes sent by QUIC connections on the tracked endpoint; on V2-834 builds "
    "plus `relay_carrier_udp_tx_bytes`, a NAT'd node's carrier to its relay, "
    "which that build splits out) **+** `bytes_relayed` "
    "from both relay servers (`saorsa_transport::relay::traffic`, the stream relay "
    "on raw UDP sockets, and `saorsa_transport::masque::relay_server`, the MASQUE "
    "reservation relay -- whose summary only appears once a CONNECT has arrived). "
    "**Actual egress** = the host's busiest interface that is not loopback and not "
    "a NAT-simulation `veth*` (`net.bytes_sent`). Both are cumulative counters "
    f"differenced over {WINDOW} windows and summed across the host's node "
    "services; the lines one service emits within a tick are collapsed first.\n\n"
    "`wire_tx_bytes` and `fetch_response_tx_bytes` are **deliberately excluded** "
    "from the ratio: they measure the same bytes at a higher layer and would "
    "double-count. Until V2-834 Part C.1 lands (relay connections counted both "
    "virtually and on the carrier) the numerator can over-count, so coverage is an "
    "**upper bound**.\n\n"
    "**Socket ground truth** (`sock_tx_bytes` / `sock_rx_bytes`) exists only on "
    "V2-834 builds: process-wide bytes at the real-socket boundary, the figure "
    "V2-834's acceptance gate (within 2% of the NIC) is measured by. Empty on "
    "older builds.\n\n"
    "A restart resets a service's counters; that window is dropped for that "
    "service, not negated."
)


def _field_clause(target: str, field: str) -> str:
    return f'(r["target"] == "{target}" and r["_field"] == "{field}")'


def _lower_host() -> str:
    return "  |> map(fn: (r) => ({r with host: strings.toLower(v: r.host)}))\n"


# The Host variable lists the bucket's raw host tags (mixed case on a testnet),
# while both join sides are lower-cased -- so the match is case-insensitive.
HOST_FILTER = '  |> filter(fn: (r) => r["host"] =~ /(?i)^$host$/)\n'


def _counter_increase(
    bucket: str,
    field_filter: str,
    *,
    burst_fn: str,
    group_by: tuple[str, ...] = ("host",),
) -> str:
    """Flux pipeline (no assignment) yielding bytes per window from ``tail`` counters.

    ``field_filter`` selects the (target, field) pairs; ``burst_fn`` (``sum`` or
    ``max``) collapses the lines a series emits within one tick; the differenced
    increases are then summed per ``group_by`` (plus ``_time``). After
    ``difference`` every series' rows sit on the same window boundaries, so the
    cross-series total is a plain per-(group, _time) sum -- a second
    aggregateWindow would re-stamp each increase at the NEXT window's stop (one
    window late against the NIC side of the join) and emit a bogus zero row at
    the range end.
    """
    cols = ", ".join(f'"{c}"' for c in group_by)
    return (
        f'from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "tail")\n'
        f"  |> filter(fn: (r) => {field_filter})\n"
        + _lower_host()
        + HOST_FILTER +
        '  |> group(columns: ["host", "service_number", "target", "_field"])\n'
        f"  |> aggregateWindow(every: {BURST_WINDOW}, fn: {burst_fn}, createEmpty: false)\n"
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        "  |> difference(nonNegative: true)\n"
        f'  |> group(columns: [{cols}, "_time"])\n'
        "  |> sum()\n"
        f"  |> group(columns: [{cols}])\n"
    )


def _counter_increase_multi(
    bucket: str,
    specs: tuple[tuple[str, str, str], ...],
    *,
    stream: str,
    group_by: tuple[str, ...] = ("host",),
) -> str:
    """Assign ``stream`` = per-group bytes per window over several (target,
    field, burst_fn) specs, each collapsed with its own burst function and then
    unioned and summed per (group, _time)."""
    by_fn: dict[str, list[str]] = {}
    for target, field, fn in specs:
        by_fn.setdefault(fn, []).append(_field_clause(target, field))
    parts = []
    names = []
    for i, (fn, clauses) in enumerate(sorted(by_fn.items())):
        name = f"{stream}_{fn}"
        names.append(name)
        parts.append(
            f"{name} = "
            + _counter_increase(bucket, "(" + " or ".join(clauses) + ")",
                                burst_fn=fn, group_by=group_by)
        )
    cols = ", ".join(f'"{c}"' for c in group_by)
    if len(names) == 1:
        combine = f"{stream} = {names[0]}\n"
    else:
        tables = ", ".join(names)
        combine = (
            f"{stream} = union(tables: [{tables}])\n"
            f'  |> group(columns: [{cols}, "_time"])\n'
            "  |> sum()\n"
            f"  |> group(columns: [{cols}])\n"
        )
    return "\n".join(parts) + "\n" + combine


def _nic_increase_per_host(bucket: str, *, field: str = "bytes_sent", stream: str) -> str:
    """Flux assigning ``stream`` = per-host NIC bytes per window.

    ``net.<field>`` is per interface and interface names differ by provider
    (``eth0``, ``ens3``, ...), so the busiest interface per window stands in for
    "the public NIC" rather than a hard-coded name. Excluded: ``lo``; ``all``
    (the protocol-counter pseudo-interface, no bytes); and ``veth*`` -- on a
    NAT-simulated VM the root namespace's end of the veth pair reports the
    node's traffic in the OPPOSITE direction (its ``bytes_sent`` is what was
    sent into the netns), and it is the busiest interface on the host.
    """
    return (
        f'{stream} = from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r["_measurement"] == "net" and r["_field"] == "{field}")\n'
        '  |> filter(fn: (r) => r["interface"] !~ /^(lo|all|veth.*)$/)\n'
        + _lower_host()
        + HOST_FILTER +
        '  |> group(columns: ["host", "interface"])\n'
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        "  |> difference(nonNegative: true)\n"
        '  |> group(columns: ["host", "_time"])\n'
        "  |> max()\n"
        '  |> group(columns: ["host"])\n'
        '  |> keep(columns: ["_time", "_value", "host"])\n'
    )


def _joined(
    bucket: str,
    specs: tuple[tuple[str, str, str], ...],
    *,
    nic_field: str = "bytes_sent",
) -> str:
    """``joined`` = per-host (accounted, nic) bytes per window."""
    return (
        'import "strings"\n\n'
        + _counter_increase_multi(bucket, specs, stream="accounted")
        + "\n"
        + _nic_increase_per_host(bucket, field=nic_field, stream="nic")
        + "\n"
        'joined = join(tables: {a: accounted, n: nic}, on: ["_time", "host"])\n'
    )


def _joined_accounted_and_nic(bucket: str) -> str:
    return _joined(bucket, ACCOUNTED_EGRESS_FIELDS)


COVERAGE_MAP = (
    "  |> map(fn: (r) => ({r with\n"
    "    _value: if float(v: r._value_n) > 0.0\n"
    "      then 100.0 * float(v: r._value_a) / float(v: r._value_n)\n"
    "      else 0.0,\n"
    "  }))\n"
    '  |> keep(columns: ["_time", "_value", "host"])\n'
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
    query = _joined_accounted_and_nic(bucket) + "joined\n" + COVERAGE_MAP + (
        '  |> yield(name: "coverage")'
    )
    return _timeseries(
        "Egress accounting coverage (%)",
        grid_pos,
        query,
        unit="percent",
        max_value=120,
        description=(
            "(udp_tx_bytes + bytes_relayed from both relay servers) / "
            f"net.bytes_sent per host, from the cumulative counters over {WINDOW} "
            "windows. The legend's mean is the figure for the selected range. Red "
            f"below {COVERAGE_RED_BELOW_PERCENT}%: most of the host's egress is "
            "bytes no counter explains. Upper bound until V2-834 C.1."
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


def _rate_query(
    bucket: str,
    specs: tuple[tuple[str, str, str], ...],
    *,
    group_by: tuple[str, ...] = ("host",),
) -> str:
    return (
        'import "strings"\n\n'
        + _counter_increase_multi(bucket, specs, stream="bytes", group_by=group_by)
        + "\nbytes\n"
        f"  |> map(fn: (r) => ({{r with _value: float(v: r._value) / {WINDOW_SECS}}}))\n"
        '  |> yield(name: "rate")'
    )


def _ground_truth_panel(
    title: str, grid_pos: dict, *, bucket: str,
    specs: tuple[tuple[str, str, str], ...], nic_field: str, direction: str,
) -> dict:
    fields = " + ".join(f for _t, f, _fn in specs)
    query = _joined(bucket, specs, nic_field=nic_field) + "joined\n" + COVERAGE_MAP + (
        '  |> yield(name: "coverage")'
    )
    return _timeseries(
        title,
        grid_pos,
        query,
        unit="percent",
        max_value=120,
        description=(
            f"({fields}) / net.{nic_field} per host: bytes the process {direction} "
            "on real UDP sockets (V2-834's socket-boundary counters, incl. the "
            "relay server's raw sockets) against the NIC. "
            "This is the figure V2-834's acceptance gate (within 2%) is measured "
            "by. Only V2-834 builds emit the field -- empty on older ones."
        ),
        thresholds={
            "mode": "absolute",
            "steps": [
                {"color": "red", "value": None},
                {"color": "green", "value": COVERAGE_RED_BELOW_PERCENT},
            ],
        },
    )


def panel_ground_truth_egress(grid_pos: dict, *, bucket: str) -> dict:
    return _ground_truth_panel(
        "Socket ground truth — egress (%)", grid_pos, bucket=bucket,
        specs=GROUND_TRUTH_TX, nic_field="bytes_sent", direction="sent",
    )


def panel_ground_truth_ingress(grid_pos: dict, *, bucket: str) -> dict:
    return _ground_truth_panel(
        "Socket ground truth — ingress (%)", grid_pos, bucket=bucket,
        specs=GROUND_TRUTH_RX, nic_field="bytes_recv", direction="received",
    )


def panel_egress_decomposition(grid_pos: dict, *, bucket: str) -> dict:
    """Stacked rate per counter, summed over the selected hosts."""
    specs = tuple((t, f, fn) for _label, t, f, fn in EGRESS_DECOMPOSITION)
    query = _rate_query(bucket, specs, group_by=("_field",))
    overrides = [
        {
            "matcher": {"id": "byName", "options": field},
            "properties": [{"id": "displayName", "value": label}],
        }
        for label, _t, field, _fn in EGRESS_DECOMPOSITION
    ]
    panel = _timeseries(
        "Egress decomposition (selected hosts, stacked)",
        grid_pos,
        query,
        unit="Bps",
        display_name="${__field.labels._field}",
        description=(
            "Rate of each attributed egress counter, summed across the hosts "
            "selected by the Host variable. Layers overlap (wire and chunk RPC are "
            "inside udp_tx_bytes; relay legs are outside it), so this is an "
            "attribution picture, not a partition of the NIC. Counters a build "
            "does not emit are simply absent. The full decomposition is V2-1226."
        ),
    )
    panel["fieldConfig"]["defaults"]["custom"].update(
        {"stacking": {"mode": "normal", "group": "A"}, "fillOpacity": 40}
    )
    panel["fieldConfig"]["overrides"] = overrides
    return panel


def panel_relay_throughput(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 3: MASQUE relay-server throughput per host, with the direction split."""
    specs = tuple(
        (target, field, "max")
        for target in (TARGET_RELAY, TARGET_MASQUE_RELAY_SERVER)
        for field in ("bytes_relayed", "forwarded_to_client_bytes", "forwarded_to_target_bytes")
    )
    return _timeseries(
        "Relay throughput (relay servers)",
        grid_pos,
        _rate_query(bucket, specs, group_by=("host", "_field")),
        unit="Bps",
        display_name="${__field.labels.host} ${__field.labels._field}",
        description=(
            "Rate of bytes_relayed per host over both relay servers (targets "
            "saorsa_transport::relay::traffic and saorsa_transport::masque::"
            "relay_server), with the forwarded_to_client_bytes / "
            "forwarded_to_target_bytes split. bytes_relayed is the relay leg "
            "counted in the coverage numerator."
        ),
    )


def panel_active_reservations(grid_pos: dict, *, bucket: str) -> dict:
    """Panel: relay occupancy per host -- MASQUE reservations and stream relay
    sessions, each summed over the host's services. Gauges, not counters."""
    clause = (
        f"({_field_clause(TARGET_MASQUE_RELAY_SERVER, 'active_reservations')} or "
        f"{_field_clause(TARGET_RELAY, 'active_sessions')})"
    )
    query = (
        'import "strings"\n\n'
        f'from(bucket: "{bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "tail")\n'
        f"  |> filter(fn: (r) => {clause})\n"
        + _lower_host()
        + HOST_FILTER +
        '  |> group(columns: ["host", "service_number", "_field"])\n'
        f"  |> aggregateWindow(every: {BURST_WINDOW}, fn: max, createEmpty: false)\n"
        f"  |> aggregateWindow(every: {WINDOW}, fn: last, createEmpty: false)\n"
        '  |> group(columns: ["host", "_field", "_time"])\n'
        "  |> sum()\n"
        '  |> group(columns: ["host", "_field"])\n'
        '  |> yield(name: "occupancy")'
    )
    return _timeseries(
        "Active relay reservations / sessions",
        grid_pos,
        query,
        unit="short",
        legend_calcs=("last", "max"),
        display_name="${__field.labels.host} ${__field.labels._field}",
        description=(
            "active_reservations (saorsa_transport::masque::relay_server) and "
            "active_sessions (saorsa_transport::relay::traffic), summed across "
            "the host's node services. Gauges, not counters."
        ),
    )


def panel_replication_serving(grid_pos: dict, *, bucket: str) -> dict:
    """Panel 5: bytes served in replication fetch responses, per host."""
    return _timeseries(
        "Replication serving",
        grid_pos,
        _rate_query(bucket, ((TARGET_REPLICATION, "fetch_response_tx_bytes", "max"),)),
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
    (panel_ground_truth_egress, {"w": 12, "h": 9}),
    (panel_ground_truth_ingress, {"w": 12, "h": 9}),
    (panel_egress_decomposition, {"h": 10}),
    (panel_relay_throughput, {}),
    (panel_active_reservations, {}),
    (panel_replication_serving, {}),
    (panel_numerator_note, {"h": 9}),
)

DASHBOARD_TITLE = "Egress accounting"
DASHBOARD_UID = "ant-egress-accounting"
DASHBOARD_TAG = "egress-accounting"
