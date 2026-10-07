"""Commitment-rotation coverage: were pointer leaves being audited at all? (V2-1413)

The V2-1363 pointer A/B turns on the rate of confirmed `DigestMismatch` subtree
audit failures, which `deployments record-log-counts` now counts from the
catalogue. A clean candidate arm is only meaningful if the audits were actually
opening pointer leaves -- and nothing in ant-node v0.21.0 says so at info level:
a successful round-2 pointer opening has no info-level line anywhere.

The one available proxy is the commitment rotation:

    info!("Storage commitment rotated: hash={hash} key_count={key_count} \
pointers={pointer_count}")   # src/replication/mod.rs:10315 @ v0.21.0

Two properties of that line shape everything here:

* **The whole line is one unparsed string.** `hash=`, `key_count=` and
  `pointers=` are NOT structured fields, so they arrive as `tail.message` and
  nothing else. Telling `pointers=0` from `pointers=7` therefore needs a
  wildcard on `tail.message.keyword`, which `match_phrase` cannot express --
  which is why the catalogue's phrase pattern cannot answer this and this
  command exists.
* **`tail.message` is analysed, `tail.message.keyword` is not.** The phrase
  match is case- and punctuation-insensitive (so it survives cosmetic
  re-wording); the wildcards are exact and case-sensitive, matching the emit
  character for character.

The query shape is taken verbatim from `evaluate_commitment` in
`scripts/rollout-watch/pointer_signals.py`, which has been proven against
ant-prod-01. The only differences are the index (a testnet's own node index
rather than the prod one) and the absence of version-arm filtering: a testnet
run is single-version, so there is no `to_version` split to make.

The verdict is in two parts, because composition and sufficiency are different
questions (V2-1450). `pointers=` present on every rotation and non-zero on some
says the rotations SEEN were well formed; it says nothing about whether enough
were seen to conclude from, and `COMMITMENT_ROTATION_INTERVAL_SECS = 3600` with
no jitter plus the "only on a change" condition means a short window
legitimately holds one or two. So `--min-rotations` / `--min-hosts` carry the
reader's own floors and a shortfall is reported as INSUFFICIENT; with neither
supplied the compositional verdict stands but says in its detail line that
volume and spread were not assessed. Run 647 (V2-1438) printed `PASS` off ONE
rotation on ONE host against a criterion of >=20 on >=5.

Read-only. Exits 0 whatever it finds -- it is a measurement, not a gate; the A/B
comparison reads the numbers out of the run's results comment.
"""

from __future__ import annotations

from rich.table import Table

from saorsa_deploy.analysis.es import EsRun, hosts_filter
from saorsa_deploy.analysis.json_out import add_json_out_arg, console_for, emit_json
from saorsa_deploy.cmd.analysis import (
    add_deployment_args,
    add_window_args,
)
from saorsa_deploy.es_log_counts import HOST_FIELD, MESSAGE_FIELD, TIMESTAMP_FIELD

#: The stable substring of the emit. Matched as a phrase on the analysed field,
#: so it is the same string the catalogue's `storage_commitment_rotated` pattern
#: uses and the two counts are directly comparable.
COMMITMENT_PHRASE = "Storage commitment rotated"

#: The un-analysed subfield the two wildcards run against. A dynamically-mapped
#: keyword has `ignore_above: 256` -- the commitment line is ~100 characters, so
#: it is indexed, but a longer line would have no keyword value at all and the
#: wildcards would silently match nothing while `total` kept counting.
MESSAGE_KEYWORD_FIELD = "tail.message.keyword"

#: Present only on a build that reports pointer leaves; a pre-pointer ant-node
#: emits the same line without it.
POINTERS_WILDCARD = "*pointers=*"
#: Anchored at the end on purpose: `*pointers=0*` would also match
#: `pointers=0...` for any count beginning with a zero digit, of which there are
#: none today but there is no reason to rely on that.
ZERO_POINTERS_WILDCARD = "*pointers=0"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INSUFFICIENT = "INSUFFICIENT"
STATUS_NOT_APPLICABLE = "NOT APPLICABLE"

#: Appended to every verdict that depends on how much was observed, when the
#: caller supplied no floor to measure the observation against. Without it a
#: one-rotation window reads `PASS`, which is what V2-1438's run 647 printed
#: against a criterion of >=20 rotations on >=5 hosts -- a miss by more than an
#: order of magnitude that the status did not reflect.
UNASSESSED_CAVEAT = (
    " -- volume and host spread NOT assessed (pass --min-rotations/--min-hosts to gate on them)"
)

TOP_HOSTS = 10


def build_commitment_query(
    start: str,
    end: str,
    hosts: list[str] | None = None,
    extra_filters: list[dict] | None = None,
    must_not: list[dict] | None = None,
) -> dict:
    """The search body for one of the commitment counts.

    `size: 0` because only the count is wanted, and `track_total_hits` because
    the default caps the reported total at 10,000 -- a 300-node run rotates far
    more often than that, so without it every count over the cap would come back
    as exactly 10,000.
    """
    body: dict = {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"match_phrase": {MESSAGE_FIELD: COMMITMENT_PHRASE}},
                    {"range": {TIMESTAMP_FIELD: {"gte": start, "lte": end}}},
                    *hosts_filter(hosts),
                    *(extra_filters or []),
                ]
            }
        },
        "aggs": {
            "hosts": {"cardinality": {"field": HOST_FIELD}},
            "top_hosts": {"terms": {"field": HOST_FIELD, "size": TOP_HOSTS}},
        },
    }
    if must_not:
        body["query"]["bool"]["must_not"] = list(must_not)
    return body


def _count(response: dict) -> int:
    total = (response.get("hits") or {}).get("total") or {}
    value = total.get("value") if isinstance(total, dict) else total
    return int(value or 0)


def _hosts(response: dict) -> int | None:
    value = ((response.get("aggregations") or {}).get("hosts") or {}).get("value")
    return int(value) if value is not None else None


def _top_hosts(response: dict) -> dict[str, int]:
    buckets = ((response.get("aggregations") or {}).get("top_hosts") or {}).get("buckets") or []
    return {b["key"]: b["doc_count"] for b in buckets}


def shortfall(
    total: int,
    hosts: int | None,
    min_rotations: int | None,
    min_hosts: int | None,
) -> str | None:
    """The reason the observation is too thin to conclude from, or None.

    The floors are measured against `total` and `hosts` -- how many rotations
    were seen and on how many hosts -- NOT against the non-zero subsets. Those
    two describe the OBSERVATION rather than its outcome, so one floor means
    the same thing whichever verdict follows; `non_zero` is 0 by definition in
    the all-`pointers=0` case, and a floor on it would turn that genuine
    finding into a shortfall every time.

    `hosts` is None when the cardinality aggregation is missing, which cannot
    demonstrate spread, so a supplied `--min-hosts` counts as unmet.
    """
    seen_hosts = hosts or 0
    short = []
    if min_rotations is not None and total < min_rotations:
        short.append(f"{total:,} rotation(s) against a floor of {min_rotations:,}")
    if min_hosts is not None and seen_hosts < min_hosts:
        short.append(
            f"{'unknown' if hosts is None else f'{seen_hosts:,}'} rotating host(s) "
            f"against a floor of {min_hosts:,}"
        )
    if not short:
        return None
    return (
        "insufficient observation: " + " and ".join(short) + ". The line is "
        "info-level and emitted at most once an hour per node, and only when "
        "the node's responsible key set changed, so widen the window rather "
        "than reading a verdict into this."
    )


def summarise(
    total: int,
    with_pointers: int,
    zero_pointers: int,
    hosts: int | None,
    hosts_with_non_zero: int | None,
    top_hosts_non_zero: dict[str, int],
    min_rotations: int | None = None,
    min_hosts: int | None = None,
) -> dict:
    """Turn the four responses' numbers into the reported shape.

    `total == 0` is NOT APPLICABLE, never a failure and never a division: no
    rotation in the window says nothing about whether pointer leaves were
    committed, and the window may simply predate the first rotation (the cycle
    is hourly per node and only fires on a change, so a short window can
    legitimately miss it).

    The verdict order is deliberate. A rotation missing `pointers=` is a
    statement about the LINES -- one is proof that a non-pointer-capable
    service is on the fleet -- so it stands at any volume. Everything after it
    is a statement about ABSENCE or about a rate, which needs enough rotations
    to mean anything, so a supplied floor that is unmet short-circuits to
    INSUFFICIENT ahead of both the all-zero FAIL and the PASS.
    """
    non_zero = with_pointers - zero_pointers
    if total == 0:
        return {
            "phrase": COMMITMENT_PHRASE,
            "total": 0,
            "with_pointers": 0,
            "zero_pointers": 0,
            "non_zero": 0,
            "pointer_coverage": None,
            "non_zero_share": None,
            "hosts": hosts,
            "hosts_with_non_zero": hosts_with_non_zero,
            "top_hosts_non_zero": top_hosts_non_zero,
            "min_rotations": min_rotations,
            "min_hosts": min_hosts,
            "status": STATUS_NOT_APPLICABLE,
            "detail": (
                "not applicable: 0 commitment rotation(s) in this window. The "
                "line is info-level and emitted per rotation cycle, so widen the "
                "window before reading anything into a zero audit-failure count."
            ),
        }
    result = {
        "phrase": COMMITMENT_PHRASE,
        "total": total,
        "with_pointers": with_pointers,
        "zero_pointers": zero_pointers,
        "non_zero": non_zero,
        "pointer_coverage": with_pointers / total,
        "non_zero_share": non_zero / total,
        "hosts": hosts,
        "hosts_with_non_zero": hosts_with_non_zero,
        "top_hosts_non_zero": top_hosts_non_zero,
        "min_rotations": min_rotations,
        "min_hosts": min_hosts,
    }
    if with_pointers < total:
        result["status"] = STATUS_FAIL
        result["detail"] = (
            f"{with_pointers:,} of {total:,} rotation(s) carry `pointers=` -- the "
            "rest are emitting the pre-pointer string, so some service on this "
            "fleet is not running a pointer-capable ant-node"
        )
        return result
    thin = shortfall(total, hosts, min_rotations, min_hosts)
    if thin:
        result["status"] = STATUS_INSUFFICIENT
        result["detail"] = thin
        return result
    # Both verdicts below read on volume, so with no floor to measure it
    # against neither may be presented as a sensitivity verdict.
    caveat = UNASSESSED_CAVEAT if (min_rotations is None and min_hosts is None) else ""
    if non_zero == 0:
        result["status"] = STATUS_FAIL
        result["detail"] = (
            f"all {total:,} rotation(s) carry `pointers=`, but every one reports "
            "`pointers=0`: no commitment ever covered a pointer leaf, so a clean "
            "subtree-audit count is not evidence the audit opened one" + caveat
        )
        return result
    result["status"] = STATUS_PASS
    result["detail"] = (
        f"all {total:,} rotation(s) carry `pointers=`; {non_zero:,} report a "
        f"non-zero count across {hosts_with_non_zero} host(s)" + caveat
    )
    return result


def collect(
    run: EsRun,
    start: str,
    end: str,
    hosts: list[str] | None = None,
    min_rotations: int | None = None,
    min_hosts: int | None = None,
) -> dict:
    """Run the four counts against a deployment's own node index."""
    pointers = {"wildcard": {MESSAGE_KEYWORD_FIELD: POINTERS_WILDCARD}}
    zero = {"wildcard": {MESSAGE_KEYWORD_FIELD: ZERO_POINTERS_WILDCARD}}

    total_resp = run.search(build_commitment_query(start, end, hosts))
    with_resp = run.search(build_commitment_query(start, end, hosts, [pointers]))
    zero_resp = run.search(build_commitment_query(start, end, hosts, [zero]))
    # `non_zero` itself is the subtraction, as the rollout-watch check computes
    # it; this fourth query exists only for the HOST SPREAD of the non-zero
    # rotations, which a subtraction of two cardinalities cannot give (the same
    # host appears in both).
    non_zero_resp = run.search(
        build_commitment_query(start, end, hosts, [pointers], must_not=[zero])
    )

    result = summarise(
        total=_count(total_resp),
        with_pointers=_count(with_resp),
        zero_pointers=_count(zero_resp),
        hosts=_hosts(total_resp),
        hosts_with_non_zero=_hosts(non_zero_resp),
        top_hosts_non_zero=_top_hosts(non_zero_resp),
        min_rotations=min_rotations,
        min_hosts=min_hosts,
    )
    result["name"] = run.name
    result["index"] = run.node_index()
    result["start"] = start
    result["end"] = end
    result["host_filter"] = list(hosts) if hosts else None
    return result


def render(result: dict) -> Table:
    table = Table(
        title=(f"{result['name']} commitment rotations, {result['start']} -> {result['end']}")
    )
    table.add_column("measure")
    table.add_column("value", justify="right")
    table.add_row("rotations", f"{result['total']:,}")
    table.add_row("with `pointers=`", f"{result['with_pointers']:,}")
    table.add_row("`pointers=0`", f"{result['zero_pointers']:,}")
    table.add_row("non-zero", f"{result['non_zero']:,}")
    coverage = result["pointer_coverage"]
    table.add_row("pointer-capable share", "n/a" if coverage is None else f"{coverage:.1%}")
    share = result["non_zero_share"]
    table.add_row("non-zero share", "n/a" if share is None else f"{share:.1%}")
    table.add_row("hosts rotating", str(result["hosts"]))
    table.add_row("hosts with a non-zero", str(result["hosts_with_non_zero"]))
    # Only when a floor was supplied: a row reading "-" on every other run
    # would imply the tool had an opinion about volume, which without a floor
    # it does not.
    if result.get("min_rotations") is not None:
        table.add_row("min rotations", f"{result['min_rotations']:,}")
    if result.get("min_hosts") is not None:
        table.add_row("min hosts", f"{result['min_hosts']:,}")
    table.add_row("status", result["status"])
    return table


def cmd_pointer_commitments(args) -> int:
    console = console_for(args.json_out)
    run = EsRun(args.name)
    result = collect(
        run,
        args.start,
        args.end,
        args.hosts,
        min_rotations=args.min_rotations,
        min_hosts=args.min_hosts,
    )
    console.print(render(result))
    console.print(result["detail"])
    if result["top_hosts_non_zero"]:
        console.print(
            "[dim]top hosts by non-zero rotation: "
            + ", ".join(f"{h}={c}" for h, c in result["top_hosts_non_zero"].items())
            + "[/dim]"
        )
    emit_json(result, args.json_out, console)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "pointer-commitments",
        help=(
            "Commitment-rotation coverage: how many rotations reported pointer "
            "leaves, and on how many hosts (makes a clean subtree-audit count "
            "falsifiable)."
        ),
    )
    add_deployment_args(p)
    add_window_args(p)
    p.add_argument("--hosts", nargs="+", default=None, help="Restrict to these tag.host values.")
    p.add_argument(
        "--min-rotations",
        type=int,
        default=None,
        help=(
            "Floor on rotations observed. Below it the status is INSUFFICIENT rather "
            "than a verdict. No default: supplying neither floor leaves volume and "
            "spread unassessed, which the detail line then says."
        ),
    )
    p.add_argument(
        "--min-hosts",
        type=int,
        default=None,
        help="Floor on the number of rotating hosts. See --min-rotations.",
    )
    add_json_out_arg(p)
    p.set_defaults(func=cmd_pointer_commitments)
