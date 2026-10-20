"""Elasticsearch / InfluxDB queries over a run's own observability stack.

`record-results` and `record-analysis` cover the standard per-host aggregates,
but a run also needs numbers neither of them stores: per-phase counts of
specific log strings, per-host deltas of ant-node's cumulative INFO summary
counters, and per-VM UDP *datagram* rates from the net protocol counters. Both
backends die with the testnet, so everything here has to run before teardown.

Things this is careful about, each of which has produced a confidently wrong
number before:

* **Every Flux `range()` carries an explicit `stop`.** Flux defaults it to
  `now()`, which silently drops points from any host whose clock runs ahead.
* **Flux CSV is split on raw text, never `iter_lines()`.** Blank lines separate
  result tables and requests' line iterator eats them, which corrupts the parse
  while still exiting 0.
* **ES `tag.*` fields are analysed**, so terms/cardinality must use `.keyword`,
  and the message field is `tail.message` (not `message`, which no real
  document has). Counts use `track_total_hits`, never the hit count.
* **ES upper-cases `tag.host`** while InfluxDB and `hostname` return lower
  case, so a host filter matches both spellings.
* **The InfluxDB net field is lower case** (`udp_outdatagrams`); the /proc
  spelling `udp_OutDatagrams` returns a silent zero.
"""

from __future__ import annotations

import csv
import io
import statistics
import sys
from typing import Any

import requests
from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.cmd.analysis import add_deployment_args, add_window_args
from saorsa_deploy.es_log_counts import (
    HOST_FIELD,
    LEVEL_FIELD,
    MESSAGE_FIELD,
    TIMESTAMP_FIELD,
    node_index_pattern,
)
from saorsa_deploy.state import load_elastic_state, load_monitoring_state

FLUX_FAR_FUTURE = "2100-01-01T00:00:00Z"
SERVICE_FIELD = "tag.service_number.keyword"
CLIENT_INDEX_TEMPLATE = "telegraf-ant-clients-{name}*"
# Scan page size for document exports. Relay lifecycle lines are a few thousand
# per phase at most; 1000 keeps the number of round trips small without
# tripping the default max_result_window.
EXPORT_PAGE_SIZE = 1000


def host_variants(hosts: list[str]) -> list[str]:
    """Both spellings of every host name, since ES stores `tag.host` upper-cased."""
    out: list[str] = []
    for h in hosts:
        for v in (h, h.upper(), h.lower()):
            if v not in out:
                out.append(v)
    return out


def hosts_filter(hosts: list[str] | None) -> list[dict]:
    if not hosts:
        return []
    return [{"terms": {HOST_FIELD: host_variants(hosts)}}]


class EsRun:
    """A deployment's Elasticsearch and InfluxDB, addressed by deployment name."""

    def __init__(self, name: str):
        self.name = name
        self._elastic: dict | None = None
        self._monitoring: dict | None = None

    # -- state -------------------------------------------------------------
    def elastic(self) -> dict:
        if self._elastic is None:
            self._elastic = load_elastic_state(self.name)
        return self._elastic

    def monitoring(self) -> dict:
        if self._monitoring is None:
            self._monitoring = load_monitoring_state(self.name)
        return self._monitoring

    def es_url(self) -> str:
        return self.elastic()["elasticsearch_url"].rstrip("/")

    def es_headers(self) -> dict:
        return {"Authorization": f"ApiKey {self.elastic()['elasticsearch_api_key']}"}

    def node_index(self) -> str:
        """The node-log index glob (telegraf's strftime suffix collapsed)."""
        return node_index_pattern(self.name)

    def client_index(self) -> str:
        return CLIENT_INDEX_TEMPLATE.format(name=self.name.lower())

    # -- elasticsearch -----------------------------------------------------
    def search(self, body: dict, index: str | None = None) -> dict:
        resp = requests.post(
            f"{self.es_url()}/{index or self.node_index()}/_search",
            headers={**self.es_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=180,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()

    def count_docs(self, index: str | None = None) -> dict:
        # NB not _cat/indices: the read-only API key has no cluster:monitor
        # privilege, so that returns a security_exception rather than a list.
        resp = requests.get(
            f"{self.es_url()}/{index or self.node_index()}/_count",
            headers=self.es_headers(),
            timeout=60,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()

    def count_phrase(
        self,
        phrase: str,
        start: str,
        end: str,
        index: str | None = None,
        field: str = MESSAGE_FIELD,
        hosts: list[str] | None = None,
    ) -> dict:
        """Count a log phrase in a window, with host spread and level breakdown.

        A bare count cannot distinguish a fleet-wide change from one sick host,
        so the host cardinality and the top hosts come back with it.
        """
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "filter": [
                        {"match_phrase": {field: phrase}},
                        {"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}},
                        *hosts_filter(hosts),
                    ]
                }
            },
            "aggs": {
                "hosts": {"cardinality": {"field": HOST_FIELD}},
                "top_hosts": {"terms": {"field": HOST_FIELD, "size": 10}},
                "levels": {"terms": {"field": LEVEL_FIELD, "size": 10}},
            },
        }
        r = self.search(body, index)
        aggs = r["aggregations"]
        return {
            "phrase": phrase,
            "count": r["hits"]["total"]["value"],
            "hosts": aggs["hosts"]["value"],
            "top_hosts": {b["key"]: b["doc_count"] for b in aggs["top_hosts"]["buckets"]},
            "levels": {b["key"]: b["doc_count"] for b in aggs["levels"]["buckets"]},
        }

    def counter_delta(self, field: str, start: str, end: str) -> dict:
        """Delta of a cumulative per-service counter across a window.

        ant-node emits `wire traffic summary (cumulative)` and `audit outcome
        summary (cumulative)` as INFO lines whose structured fields telegraf
        lifts into `tail.*`. The values are monotonic since process start, so a
        rate is max-in-window minus min-in-window, taken PER HOST AND SERVICE
        and then summed -- a fleet-wide max minus min would be meaningless
        across restarts and across hosts with different uptimes.

        A service that restarted mid-window resets its counter, which makes its
        delta an underestimate; single-sample services are reported separately
        so a "0" is visible as "not observed" rather than "nothing happened".
        """
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "filter": [
                        {"exists": {"field": field}},
                        {"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}},
                    ]
                }
            },
            "aggs": {
                "per_host": {
                    "terms": {"field": HOST_FIELD, "size": 200},
                    "aggs": {
                        # `service_number` (not `node_service`, which is a
                        # telegraf METRICS tag from procstat and does not exist
                        # on log documents).
                        "per_service": {
                            "terms": {"field": SERVICE_FIELD, "size": 40},
                            "aggs": {
                                "lo": {"min": {"field": field}},
                                "hi": {"max": {"field": field}},
                                # Carrying the sample timestamps turns the delta
                                # into a RATE over the span actually observed.
                                "t_lo": {"min": {"field": TIMESTAMP_FIELD}},
                                "t_hi": {"max": {"field": TIMESTAMP_FIELD}},
                                "samples": {"value_count": {"field": field}},
                            },
                        }
                    },
                }
            },
        }
        r = self.search(body)
        return summarise_counter_delta(field, r)

    def hits(
        self,
        filters: list[dict],
        start: str,
        end: str,
        index: str | None = None,
        fields: list[str] | None = None,
        page_size: int = EXPORT_PAGE_SIZE,
    ) -> list[dict]:
        """Export matching documents (their `_source`), oldest first.

        Uses `search_after` on (@timestamp, _doc) rather than scroll: no
        server-side context to leak if the caller dies mid-export.
        """
        out: list[dict] = []
        search_after: list | None = None
        while True:
            body: dict[str, Any] = {
                "size": page_size,
                "track_total_hits": False,
                "query": {
                    "bool": {
                        "filter": [
                            *filters,
                            {"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}},
                        ]
                    }
                },
                "sort": [{TIMESTAMP_FIELD: "asc"}, {"_doc": "asc"}],
            }
            if fields:
                body["_source"] = fields
            if search_after:
                body["search_after"] = search_after
            r = self.search(body, index)
            page = r["hits"]["hits"]
            if not page:
                break
            out.extend(h["_source"] for h in page)
            search_after = page[-1].get("sort")
            if len(page) < page_size or not search_after:
                break
        return out

    # -- influxdb ----------------------------------------------------------
    def flux(self, query: str) -> list[dict]:
        """Run a Flux query, returning parsed rows.

        The response is split on raw text: Flux separates result tables with
        blank lines and requests' `iter_lines()` drops them, which silently
        merges tables.
        """
        st = self.monitoring()
        resp = requests.post(
            f"{st['influxdb_url'].rstrip('/')}/api/v2/query",
            params={"org": st["influxdb_org"]},
            headers={
                "Authorization": f"Token {st['influxdb_token']}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            data=query.encode(),
            timeout=300,
            verify=False,
        )
        resp.raise_for_status()
        return parse_flux_csv(resp.text)

    def udp_datagrams(
        self, start: str, end: str, every: str = "1m", field: str = "udp_outdatagrams"
    ) -> list[dict]:
        """Per-host UDP datagram RATE from the net protocol counters.

        The field name is LOWER CASE: /proc/net/snmp calls it `OutDatagrams` and
        telegraf lowercases it. Protocol counters are the point: the interface
        byte counters pool node QUIC with telegraf's own TCP shipping.
        """
        st = self.monitoring()
        return self.flux(
            f'''
from(bucket: "{st["influxdb_bucket"]}")
  |> range(start: {start}, stop: {end})
  |> filter(fn: (r) => r._measurement == "net" and r._field == "{field}")
  |> filter(fn: (r) => r.testnet_name == "{self.name}")
  |> derivative(unit: 1s, nonNegative: true)
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "host", "host_role"])
'''
        )


def parse_flux_csv(text: str) -> list[dict]:
    rows: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        reader = csv.DictReader(io.StringIO(block))
        for row in reader:
            if not row or row.get("result") is None:
                continue
            rows.append(row)
    return rows


def summarise_counter_delta(field: str, r: dict) -> dict:
    per_host: dict[str, float] = {}
    total = 0.0
    total_rate_per_hour = 0.0
    services_seen = 0
    services_single_sample = 0
    spans: list[float] = []
    for hb in r["aggregations"]["per_host"]["buckets"]:
        host_total = 0.0
        for sb in hb["per_service"]["buckets"]:
            lo, hi = sb["lo"]["value"], sb["hi"]["value"]
            if lo is None or hi is None:
                continue
            services_seen += 1
            delta = hi - lo
            host_total += delta
            span = (sb["t_hi"]["value"] - sb["t_lo"]["value"]) / 1000.0  # ms -> s
            if span <= 0:
                # One sample in the window: a delta of 0 here means "not
                # observed", NOT "nothing happened". Counted, not folded in.
                services_single_sample += 1
                continue
            spans.append(span)
            total_rate_per_hour += delta / span * 3600.0
        per_host[hb["key"]] = host_total
        total += host_total
    spans.sort()
    return {
        "field": field,
        "total_delta_in_window": total,
        "rate_per_hour_fleet": round(total_rate_per_hour, 2),
        "services_with_rate": services_seen - services_single_sample,
        "services_single_sample": services_single_sample,
        "median_observed_span_s": round(spans[len(spans) // 2], 1) if spans else None,
        "hosts": len(per_host),
        "per_host": per_host,
        "docs": r["hits"]["total"]["value"],
    }


def per_host_udp_means(rows: list[dict]) -> dict[str, float]:
    by_host: dict[str, list[float]] = {}
    for r in rows:
        try:
            v = float(r["_value"])
        except (TypeError, ValueError, KeyError):
            continue
        by_host.setdefault(r.get("host", "?"), []).append(v)
    return {h: statistics.mean(v) for h, v in by_host.items()}


# -- commands --------------------------------------------------------------
def quiet_tls() -> None:
    """Silence the self-signed-certificate warning every query here provokes.

    The run's Elasticsearch is reachable only by its own VMs for the hours it
    exists, so it has no certificate anybody trusts; a warning per request would
    be most of a report's output.
    """
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]


def cmd_smoke(args) -> int:
    quiet_tls()
    console = Console()
    run = EsRun(args.name)
    pattern = run.elastic()["elasticsearch_index_pattern"]
    console.print(f"ES  {run.es_url()}  index pattern {pattern}")
    st = run.monitoring()
    console.print(f"IDB {st['influxdb_url']}  bucket {st['influxdb_bucket']}")
    console.print(f"node log docs: {run.count_docs()}")
    rows = run.flux(
        f'''
from(bucket: "{st["influxdb_bucket"]}")
  |> range(start: -10m, stop: {FLUX_FAR_FUTURE})
  |> filter(fn: (r) => r._measurement == "net")
  |> keep(columns: ["_field"])
  |> distinct(column: "_field")
'''
    )
    console.print(f"net fields: {sorted({r['_value'] for r in rows})}")
    return 0


def cmd_log_count(args) -> int:
    quiet_tls()
    run = EsRun(args.name)
    index = run.client_index() if args.index == "client" else None
    result = run.count_phrase(args.phrase, args.start, args.end, index=index, hosts=args.hosts)
    emit_json(result, args.json_out, console_for(args.json_out), echo=True)
    return 0


def cmd_counter_delta(args) -> int:
    quiet_tls()
    result = EsRun(args.name).counter_delta(args.field, args.start, args.end)
    emit_json(result, args.json_out, console_for(args.json_out), echo=True)
    return 0


def cmd_udp_rate(args) -> int:
    quiet_tls()
    console = console_for(args.json_out)
    rows = EsRun(args.name).udp_datagrams(args.start, args.end, every=args.every, field=args.field)
    means = per_host_udp_means(rows)
    table = Table(title=f"{args.field} mean rate per host, {args.every} buckets")
    table.add_column("host")
    table.add_column("mean/s", justify="right")
    for host, mean in sorted(means.items()):
        table.add_row(host, f"{mean:.2f}")
    console.print(table)
    console.print(
        "[dim]NAT'd VMs report only their host-namespace residual here; "
        "their node traffic is inside ant_nat (use `analysis snapshot`).[/dim]"
    )
    payload = {"field": args.field, "every": args.every, "per_host_mean": means}
    emit_json(payload, args.json_out, console)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "smoke", help="Prove the run's ES and InfluxDB are reachable and receiving."
    )
    add_deployment_args(p)
    p.set_defaults(func=cmd_smoke)

    p = subparsers.add_parser(
        "log-count",
        help="Count a log phrase in a window, with host spread and level breakdown.",
    )
    add_deployment_args(p)
    p.add_argument(
        "--phrase", required=True, help="Phrase to match (match_phrase on tail.message)."
    )
    add_window_args(p)
    p.add_argument("--index", choices=["node", "client"], default="node")
    p.add_argument("--hosts", nargs="+", default=None, help="Restrict to these tag.host values.")
    add_json_out_arg(p)
    p.set_defaults(func=cmd_log_count)

    p = subparsers.add_parser(
        "counter-delta",
        help=(
            "Delta and fleet rate of a cumulative per-service counter (e.g. tail.wire_tx_bytes)."
        ),
    )
    add_deployment_args(p)
    p.add_argument("--field", required=True)
    add_window_args(p)
    add_json_out_arg(p)
    p.set_defaults(func=cmd_counter_delta)

    p = subparsers.add_parser(
        "udp-rate",
        help=(
            "Per-host UDP datagram rate from InfluxDB's net protocol counters (plain tier only)."
        ),
    )
    add_deployment_args(p)
    add_window_args(p)
    p.add_argument("--every", default="1m")
    p.add_argument("--field", default="udp_outdatagrams")
    add_json_out_arg(p)
    p.set_defaults(func=cmd_udp_rate)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
