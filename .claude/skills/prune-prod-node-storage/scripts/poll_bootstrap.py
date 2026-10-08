#!/usr/bin/env python3
"""Poll the ant-prod-01 production Elasticsearch for the ant-node
"Replication bootstrap complete" marker for one host + service.

Used by the prune-prod-node-storage skill to detect when a freshly re-added
node service has finished bootstrapping (i.e. pulled in the records it is now
responsible for) after a `prod services rm` + `prod services add` cycle.

The marker `Replication bootstrap complete` is emitted at INFO by
ant-node/src/replication/mod.rs:complete_bootstrap() -- the single convergence
point that flips is_bootstrapping=false once every bootstrap fetch/verify has
drained. It is a static literal (no interpolation) so match_phrase is exact.

Credentials come from the ENVIRONMENT ONLY (never baked in):
  PROD_ES_URL      e.g. http://162.19.238.53:9200
  PROD_ES_API_KEY  the Elasticsearch ApiKey value (base64 "id:key")

The query is scoped so it can only ever match the node we just re-added:
  tag.host.keyword          == <HOST upper-cased>       (inventory name uppercased)
  tag.service_number.keyword == <N>                     (the N in ant-node-N)
  match_phrase tail.message == "Replication bootstrap complete"
  @timestamp                >= <--since>                (the moment `add` ran)

Because the marker fires ~once at (re)bootstrap (not periodically), the first
hit at/after --since is the completion of THIS re-add.

Single-shot exit codes:
  0  complete  -- marker found; prints the first matching @timestamp
  2  pending   -- no marker yet
  1  error     -- missing env / bad args / unrecoverable query error

--wait mode: loop every --interval seconds (default 300) until complete
(exit 0) or --max-wait-hours elapses (exit 3; default 0 = wait forever).
Transient query errors in --wait mode are logged to stderr and retried --
they never abort the wait. Run it as a background process so the 5-minute
sleeps don't block the foreground and the caller is notified on completion.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_INDEX = "telegraf-ant-nodes-ant-prod-01-*"
DEFAULT_MARKER = "Replication bootstrap complete"
DEFAULT_INTERVAL = 300  # 5 minutes
DEFAULT_REQUEST_TIMEOUT = 60  # per-HTTP-request, seconds


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def service_number(service, service_number_arg):
    if service_number_arg is not None:
        n = str(service_number_arg).strip()
    else:
        m = re.match(r"^ant-node-([1-9]\d*)$", service.strip())
        if not m:
            eprint(f"error: --service must look like 'ant-node-N', got {service!r}")
            sys.exit(1)
        n = m.group(1)
    if not n.isdigit():
        eprint(
            f"error: could not derive a service number from {service!r}/{service_number_arg!r}"
        )
        sys.exit(1)
    return n


def build_query(host_upper, svc_num, since, marker):
    return {
        "size": 1,
        "sort": [{"@timestamp": "asc"}],  # first completion at/after `since`
        "_source": ["@timestamp", "tag.host", "tag.service_number", "tail.message"],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tag.host.keyword": host_upper}},
                    {"term": {"tag.service_number.keyword": svc_num}},
                    {"match_phrase": {"tail.message": marker}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
    }


class QueryError(Exception):
    pass


def run_query(url, api_key, index, query, request_timeout):
    endpoint = f"{url.rstrip('/')}/{index}/_search"
    data = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Authorization", f"ApiKey {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise QueryError(f"HTTP {e.code} from ES: {detail}")
    except urllib.error.URLError as e:
        raise QueryError(f"network error reaching ES: {e.reason}")
    except (TimeoutError, json.JSONDecodeError) as e:
        raise QueryError(f"bad response from ES: {e}")
    if "error" in body:
        raise QueryError(f"ES query error: {json.dumps(body['error'])[:500]}")
    hits = body.get("hits", {}).get("hits", [])
    if hits:
        return hits[0].get("_source", {}).get("@timestamp")
    return None


def emit(status, host_upper, svc_num, timestamp=None):
    print(
        json.dumps(
            {
                "status": status,
                "host": host_upper,
                "service_number": svc_num,
                "timestamp": timestamp,
            }
        ),
        flush=True,
    )


def main():
    p = argparse.ArgumentParser(
        description="Poll prod ES for ant-node bootstrap completion."
    )
    p.add_argument(
        "--host",
        required=True,
        help="host name from inventory (any case; uppercased for tag.host.keyword)",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--service", help="service name, e.g. ant-node-7")
    g.add_argument("--service-number", help="service number, e.g. 7")
    p.add_argument(
        "--since",
        required=True,
        help="ISO8601 UTC lower bound (the moment `prod services add` ran), "
        "e.g. 2026-07-24T14:30:00Z",
    )
    p.add_argument("--index", default=DEFAULT_INDEX)
    p.add_argument("--marker", default=DEFAULT_MARKER)
    p.add_argument(
        "--wait",
        action="store_true",
        help="loop until complete instead of a single check",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="seconds between polls in --wait mode (default 300)",
    )
    p.add_argument(
        "--max-wait-hours",
        type=float,
        default=0.0,
        help="give up after N hours in --wait mode (default 0 = forever)",
    )
    p.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="per-HTTP-request timeout in seconds (default 60)",
    )
    args = p.parse_args()

    url = os.environ.get("PROD_ES_URL")
    api_key = os.environ.get("PROD_ES_API_KEY")
    if not url or not api_key:
        eprint(
            "error: PROD_ES_URL and PROD_ES_API_KEY must both be set in the environment."
        )
        sys.exit(1)

    host_upper = args.host.strip().upper()
    svc_num = service_number(args.service, args.service_number)
    query = build_query(host_upper, svc_num, args.since, args.marker)

    if not args.wait:
        try:
            ts = run_query(url, api_key, args.index, query, args.request_timeout)
        except QueryError as e:
            eprint(f"error: {e}")
            sys.exit(1)
        if ts:
            emit("complete", host_upper, svc_num, ts)
            sys.exit(0)
        emit("pending", host_upper, svc_num)
        sys.exit(2)

    # --wait mode: poll forever (or until --max-wait-hours), retrying transient errors.
    deadline = (
        None
        if args.max_wait_hours <= 0
        else time.monotonic() + args.max_wait_hours * 3600
    )
    eprint(
        f"[poll] waiting for '{args.marker}' on {host_upper} ant-node-{svc_num} "
        f"since {args.since} (every {args.interval}s"
        f"{', no deadline' if deadline is None else f', deadline {args.max_wait_hours}h'})"
    )
    while True:
        try:
            ts = run_query(url, api_key, args.index, query, args.request_timeout)
            if ts:
                emit("complete", host_upper, svc_num, ts)
                sys.exit(0)
            eprint(f"[poll] pending {host_upper} ant-node-{svc_num} ...")
        except QueryError as e:
            eprint(f"[poll] transient error (will retry): {e}")
        if deadline is not None and time.monotonic() >= deadline:
            emit("timeout", host_upper, svc_num)
            eprint(f"[poll] gave up after {args.max_wait_hours}h")
            sys.exit(3)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
