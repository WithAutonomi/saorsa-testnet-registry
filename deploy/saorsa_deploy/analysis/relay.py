"""Relay lifecycle extraction for teardown-ordering runs (V2-1061 / ADR-013).

The gate metric of that run is a ratio of two log lines: the endpoint driver's
`ERROR I/O error: relay recv stream closed` against `Proactive relay torn down`.
On saorsa-transport 0.36.0 they track 1:1 because every requested teardown
destroys the tunnel under the endpoint; after PR #138 only a tunnel that
genuinely broke should produce the error.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis import hosts as hosts_mod
from saorsa_deploy.analysis.es import EsRun, hosts_filter
from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.cmd.analysis import add_deployment_args, add_window_args
from saorsa_deploy.es_log_counts import MESSAGE_FIELD, TIMESTAMP_FIELD

# (key, phrase) — one entry per relay lifecycle line, with the code path that
# emits it. All are INFO unless noted, so they are present at fleet log level.
RELAY_PHRASES: tuple[tuple[str, str], ...] = (
    # saorsa-transport NatTraversalEndpoint::prepare_proactive_relay
    ("prepared", "Proactive relay allocation prepared"),
    # saorsa-transport publish_proactive_relay after the canary quorum passes
    ("activated", "Activated canary-verified proactive relay"),
    # saorsa-transport ProactiveRelay::teardown — every REQUESTED teardown
    ("torn_down", "Proactive relay torn down"),
    # saorsa-transport tunnel-death watcher — an UNREQUESTED tunnel loss
    ("tunnel_died", "MASQUE tunnel for relay"),
    # saorsa-core reachability driver — health monitor found the tunnel dead
    ("unhealthy_rebind", "driver: relay tunnel unhealthy, rebinding"),
    # saorsa-core reachability driver — transport emitted RelayLost
    ("relay_lost_rebind", "driver: RelayLost event received, rebinding"),
    # saorsa-transport ProactiveRelay::drop — the forced-cleanup fallback (WARN)
    ("owner_dropped", "Relay owner dropped before graceful teardown"),
    # saorsa-transport MASQUE relay SERVER — capacity slot released
    ("server_session_closed", "MASQUE relay session closed"),
    # saorsa-transport EndpointDriver failure path (ERROR) — the V2-986 line
    ("recv_stream_closed", "relay recv stream closed"),
    # saorsa-transport ProactiveRelay::teardown 1 s drain budget hit (DEBUG —
    # absent at fleet info level; counted so its absence is explicit)
    ("drain_timed_out", "Timed out draining proactive relay endpoint"),
)

TEARDOWN_PHRASE = "Proactive relay torn down"
PREPARED_PHRASE = "Proactive relay allocation prepared"

# telegraf flattens ant-node's JSON log fields under `tail.` (the message is
# `tail.message`, counters are `tail.wire_tx_bytes`), so the structured fields
# on the relay lines are expected at these names. Constants so they can be
# corrected in one place if a field turns out to be flattened differently.
HOST_SRC = "tag.host"
SERVICE_SRC = "tag.service_number"
RELAY_ADDR_SRC = "tail.relay_addr"
RELAY_SERVER_SRC = "tail.relay_server"
ALLOCATION_ID_SRC = "tail.allocation_id"


def _get(src: dict, dotted: str):
    """Read a possibly-nested key (`tail.relay_addr`) from a `_source` document."""
    if dotted in src:
        return src[dotted]
    cur = src
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _tail_keys(src: dict) -> list[str]:
    tail = src.get("tail")
    if isinstance(tail, dict):
        return sorted(tail.keys())
    return sorted(k for k in src if k.startswith("tail."))


def relay_report(run: EsRun, start: str, end: str, hosts: list[str] | None = None) -> dict:
    counts = {
        key: run.count_phrase(phrase, start, end, hosts=hosts) for key, phrase in RELAY_PHRASES
    }
    torn = counts["torn_down"]["count"]
    return {
        "start": start,
        "end": end,
        "hosts_filter": hosts,
        "counts": counts,
        "error_per_teardown": (
            round(counts["recv_stream_closed"]["count"] / torn, 4) if torn else None
        ),
        "died_per_teardown": round(counts["tunnel_died"]["count"] / torn, 4) if torn else None,
    }


def teardown_rows(docs: list[dict], console: Console | None = None) -> list[dict]:
    rows = []
    warned = False
    for src in docs:
        relay_addr = _get(src, RELAY_ADDR_SRC)
        if relay_addr is None and not warned and console is not None:
            console.print(
                f"[yellow]warning:[/yellow] {RELAY_ADDR_SRC} missing on the first "
                f"teardown document; available tail keys: {_tail_keys(src)}"
            )
            warned = True
        rows.append(
            {
                "ts": _get(src, TIMESTAMP_FIELD),
                "host": _get(src, HOST_SRC),
                "service": _get(src, SERVICE_SRC),
                "relay_addr": relay_addr,
                "relay_server": _get(src, RELAY_SERVER_SRC),
                "allocation_id": _get(src, ALLOCATION_ID_SRC),
            }
        )
    return rows


def _server_ip(src: dict) -> str | None:
    """The relay server's IP for a relay document.

    `Proactive relay torn down` carries an explicit `relay_server`, but
    `Proactive relay allocation prepared` -- the line this ranking is built from
    -- carries only `relay_addr`. Both are addresses ON the relay server, so
    either identifies the VM once the port is dropped. Keying on `relay_server`
    alone silently skipped every prepared document and produced an empty
    ranking.
    """
    addr = _get(src, RELAY_SERVER_SRC) or _get(src, RELAY_ADDR_SRC)
    if addr is None:
        return None
    return str(addr).rsplit(":", 1)[0].strip("[]")


def rank_relay_servers(docs: list[dict], vms: list[hosts_mod.Vm]) -> list[dict]:
    """Distinct (host, service) clients per relay server, mapped to inventory VMs.

    Grouped by server IP, not by the full `ip:port`: a relay server hands each
    allocation its own port, so grouping on the full address would split one
    VM into one bucket per client and rank nothing.
    """
    clients: dict[str, set[tuple]] = defaultdict(set)
    for src in docs:
        ip = _server_ip(src)
        if ip is None:
            continue
        clients[ip].add((_get(src, HOST_SRC), _get(src, SERVICE_SRC)))
    by_ip = {v.ip: v for v in vms}
    out = []
    for ip, members in clients.items():
        vm = by_ip.get(ip)
        out.append(
            {
                "relay_server": ip,
                "vm": vm.name if vm else None,
                "ip": ip,
                "nat": vm.nat if vm else None,
                "clients": len(members),
                "client_hosts": len({m[0] for m in members}),
                "client_list": sorted(f"{h}/{s}" for h, s in members),
            }
        )
    out.sort(key=lambda r: (-r["clients"], r["relay_server"]))
    return out


# -- commands --------------------------------------------------------------
def cmd_relay_report(args) -> int:
    import requests

    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    console = console_for(args.json_out)
    result = relay_report(EsRun(args.name), args.start, args.end, hosts=args.hosts)
    table = Table(title=f"relay lifecycle {args.start} → {args.end}")
    table.add_column("key")
    table.add_column("phrase")
    table.add_column("count", justify="right")
    table.add_column("hosts", justify="right")
    table.add_column("levels")
    for key, phrase in RELAY_PHRASES:
        c = result["counts"][key]
        levels = ", ".join(f"{k}={v}" for k, v in c["levels"].items()) or "-"
        table.add_row(key, phrase, str(c["count"]), str(c["hosts"]), levels)
    console.print(table)
    console.print(f"error_per_teardown = {result['error_per_teardown']}")
    console.print(f"died_per_teardown  = {result['died_per_teardown']}")
    if args.hosts:
        console.print(f"[dim]hosts filter: {', '.join(args.hosts)}[/dim]")
    emit_json(result, args.json_out, console)
    return 0


def cmd_teardowns(args) -> int:
    import requests

    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    console = Console()
    run = EsRun(args.name)
    docs = run.hits(
        [{"match_phrase": {MESSAGE_FIELD: TEARDOWN_PHRASE}}, *hosts_filter(args.hosts)],
        args.start,
        args.end,
    )
    rows = teardown_rows(docs, console)
    with open(args.out, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    console.print(f"wrote {len(rows)} teardown rows to {args.out}")
    return 0


def cmd_relay_servers(args) -> int:
    import requests

    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    console = console_for(args.json_out)
    run = EsRun(args.name)
    docs = run.hits([{"match_phrase": {MESSAGE_FIELD: PREPARED_PHRASE}}], args.start, args.end)
    ranked = rank_relay_servers(docs, hosts_mod.node_vms(args.name, args.inventory))
    table = Table(title=f"relay servers by client count {args.start} → {args.end}")
    for col in ("relay_server", "vm", "ip", "nat", "clients", "client hosts"):
        table.add_column(col, justify="right" if col in ("clients", "client hosts") else "left")
    for r in ranked[: args.top]:
        table.add_row(
            r["relay_server"],
            r["vm"] or "?",
            r["ip"],
            "-" if r["nat"] is None else str(r["nat"]),
            str(r["clients"]),
            str(r["client_hosts"]),
        )
    console.print(table)
    emit_json(ranked, args.json_out, console)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "relay-report",
        help=(
            "Counts of every relay lifecycle line in a window plus the "
            "ERROR-per-teardown ratio (V2-1061's gate metric)."
        ),
    )
    add_deployment_args(p)
    add_window_args(p)
    p.add_argument("--hosts", nargs="+", default=None, help="Restrict to these tag.host values.")
    add_json_out_arg(p)
    p.set_defaults(func=cmd_relay_report)

    p = subparsers.add_parser(
        "teardowns",
        help="Export every `Proactive relay torn down` document as JSONL (join key for pcaps).",
    )
    add_deployment_args(p)
    add_window_args(p)
    p.add_argument("--hosts", nargs="+", default=None)
    p.add_argument("--out", required=True, help="Output JSONL path.")
    p.set_defaults(func=cmd_teardowns)

    p = subparsers.add_parser(
        "relay-servers",
        help="Rank relay servers by number of NAT'd clients, mapped to inventory VMs.",
    )
    add_deployment_args(p)
    add_window_args(p)
    p.add_argument("--top", type=int, default=10)
    add_json_out_arg(p)
    p.set_defaults(func=cmd_relay_servers)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
