"""Shared plumbing for the 2026-09-23 train's rollout-watch verification scripts.

The three scripts beside this one each answer one question about the production
fleet mid-rollout, and they all need the same four things: a read-only client
for ant-prod-01's Elasticsearch, one for its InfluxDB, a way to tell which arm
of the rollout a service is in, and a markdown renderer so the answer can be
pasted straight into the V2-1297 sub-issue.

Two conventions are enforced here rather than left to each script:

* **A control query.** A broken query and a quiet fleet both read zero, so a
  measure that passes *because* it counted nothing is worthless on its own.
  Each script names a control that is non-zero whenever the fleet is alive; if
  the control reads zero, every check that passed on a zero is downgraded to
  UNTRUSTED and the script fails. A check that is NOT APPLICABLE (no services
  in the arm being measured) is not affected -- it made no claim to distrust.
* **`.keyword` everywhere, `size: 0` always.** `tag.*` fields are analysed, so
  a `terms`/`prefix`/`cardinality` on the bare field silently aggregates
  tokens; and the production node index takes ~13.7M documents every two hours,
  so nothing here may page documents out of it.

Usage:
    from _common import EsClient, InfluxClient, parse_window

Example:
    es = EsClient.from_env()
    total = es.count([{"match_phrase": {MESSAGE_FIELD: "ant-node starting"}}], start, end)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
# The scripts import `saorsa_deploy.webrtc_probe` (standard library only, by
# design) without the package being installed on the operator's path.
sys.path.insert(0, str(REPO_ROOT / "deploy"))

DEPLOYMENT_NAME = "ant-prod-01"
NODE_INDEX = "telegraf-ant-nodes-ant-prod-01-*"

ES_URL_ENV = "ANT_PROD_01_ES_URL"
ES_KEY_ENV = "ANT_PROD_01_ES_API_KEY"
INFLUX_URL_ENV = "ANT_PROD_01_INFLUX_URL"
INFLUX_KEY_ENV = "ANT_PROD_01_INFLUX_API_KEY"

#: ant-prod-01's InfluxDB org id. The org *name* (`ant`) also resolves, but the
#: id is what V2-1275 used and cannot be renamed underneath us.
INFLUX_ORG = "37300ec1978aff3e"
INFLUX_BUCKET = "testnet-metrics"

#: Flux defaults `range(stop:)` to `now()`, which drops every point from a host
#: whose clock runs ahead. Prod clocks are not deliberately skewed, but the same
#: mistake has silently emptied a query here before.
FLUX_FAR_FUTURE = "2100-01-01T00:00:00Z"

TIMESTAMP_FIELD = "@timestamp"
HOST_FIELD = "tag.host.keyword"
SERVICE_FIELD = "tag.service_number.keyword"
VERSION_FIELD = "tag.node_version.keyword"
TARGET_FIELD = "tag.target.keyword"
LEVEL_FIELD = "tag.level.keyword"
ROLE_FIELD = "tag.host_role.keyword"
MESSAGE_FIELD = "tail.message"
MESSAGE_KEYWORD_FIELD = "tail.message.keyword"

#: A service counts as running if it logged in the last 15 minutes. Shorter and
#: a quiet service drops out of the denominator; longer and a service that died
#: an hour ago is still counted as running. Same rule as `prod rollout-watch`.
ADOPTION_LOOKBACK = timedelta(minutes=15)
COMPOSITE_PAGE_SIZE = 1000
#: ~73 hosts x ~15 services is a little over 1000 buckets, so two pages is the
#: normal case. The cap only stops a misbehaving server looping forever.
MAX_COMPOSITE_PAGES = 100

#: Builds older than ant-node #225 do not stamp a version on their log lines at
#: all, and telegraf writes the tag as this literal.
UNKNOWN_VERSION = "unknown"
START_MESSAGE = "ant-node starting"

#: Below this share an arm is too small to draw a comparison from: a handful of
#: services produce figures dominated by whichever host happens to be sick.
SUSPEND_BELOW_SHARE = 5.0

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT APPLICABLE"
UNTRUSTED = "UNTRUSTED"

_WINDOW_RE = re.compile(r"^(\d+)(m|h)$")


class RolloutCheckError(RuntimeError):
    """A misconfiguration or an unusable response -- never a failed expectation."""


# -- time ------------------------------------------------------------------
def parse_window(value: str) -> timedelta:
    """`15m`, `1h`, `6h`, `24h` -> a timedelta."""
    m = _WINDOW_RE.match((value or "").strip())
    if not m:
        raise RolloutCheckError(
            f"--window must look like 15m, 1h, 6h or 24h. Got {value!r}."
        )
    amount = int(m.group(1))
    if amount <= 0:
        raise RolloutCheckError("--window must be greater than zero.")
    return timedelta(minutes=amount) if m.group(2) == "m" else timedelta(hours=amount)


def window_label(window: timedelta) -> str:
    seconds = int(window.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_es_timestamp(value: str | None) -> datetime | None:
    """ES returns `@timestamp` as an ISO string; `epoch_millis` when aggregated."""
    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def epoch_millis_to_datetime(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def range_filter(start: datetime, end: datetime) -> dict:
    return {"range": {TIMESTAMP_FIELD: {"gte": iso(start), "lte": iso(end)}}}


# -- clients ---------------------------------------------------------------
def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RolloutCheckError(
            f"{name} is not set. Every script here reads ant-prod-01 from env."
        )
    return value


@dataclass(frozen=True)
class EsClient:
    """Read-only Elasticsearch access to the production node-log index."""

    url: str
    api_key: str
    index: str = NODE_INDEX
    timeout: int = 300

    @classmethod
    def from_env(cls) -> EsClient:
        return cls(
            url=_require_env(ES_URL_ENV).rstrip("/"), api_key=_require_env(ES_KEY_ENV)
        )

    def search(self, body: dict) -> dict:
        resp = requests.post(
            f"{self.url}/{self.index}/_search",
            headers={
                "Authorization": f"ApiKey {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
            verify=False,
        )
        resp.raise_for_status()
        return resp.json()

    def count(self, filters: list[dict], start: datetime, end: datetime) -> int:
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {"bool": {"filter": [range_filter(start, end), *filters]}},
        }
        return self.search(body)["hits"]["total"]["value"]

    def composite_pages(self, build_body, aggregation: str = "services"):
        """Yield every page of a composite aggregation, following `after_key`."""
        after: dict | None = None
        for _ in range(MAX_COMPOSITE_PAGES):
            page = self.search(build_body(after))
            agg = page["aggregations"][aggregation]
            yield page
            after = agg.get("after_key")
            if not after or not agg.get("buckets"):
                return
        raise RolloutCheckError(
            f"composite aggregation {aggregation!r} did not finish in "
            f"{MAX_COMPOSITE_PAGES} pages; the fleet cannot be that large."
        )


@dataclass(frozen=True)
class InfluxClient:
    """Read-only Flux access to ant-prod-01's InfluxDB."""

    url: str
    token: str
    org: str = INFLUX_ORG
    bucket: str = INFLUX_BUCKET
    timeout: int = 300

    @classmethod
    def from_env(cls) -> InfluxClient:
        return cls(
            url=_require_env(INFLUX_URL_ENV).rstrip("/"),
            token=_require_env(INFLUX_KEY_ENV),
        )

    def query(self, flux: str) -> list[dict]:
        resp = requests.post(
            f"{self.url}/api/v2/query",
            params={"org": self.org},
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            data=flux.encode(),
            timeout=self.timeout,
            verify=False,
        )
        resp.raise_for_status()
        return parse_flux_csv(resp.text)


def parse_flux_csv(text: str) -> list[dict]:
    """Parse a Flux CSV response.

    Split on raw text, never `iter_lines()`: Flux separates result tables with
    blank lines and requests' line iterator eats them, which merges two tables
    into one while still exiting 0.
    """
    rows: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        for row in csv.DictReader(io.StringIO(block)):
            if not row or row.get("result") is None:
                continue
            rows.append(row)
    return rows


# -- arms ------------------------------------------------------------------
def service_key(host: str, service: str) -> str:
    return f"{host}/{service}"


@dataclass(frozen=True)
class Arms:
    """Which arm of the rollout every running service is in."""

    to_version: str
    versions: dict[str, str]
    arms: dict[str, str]

    @property
    def new(self) -> list[str]:
        return sorted(k for k, arm in self.arms.items() if arm == "new")

    @property
    def old(self) -> list[str]:
        return sorted(k for k, arm in self.arms.items() if arm == "old")

    @property
    def running(self) -> int:
        return len(self.arms)

    def hosts(self, arm: str) -> set[str]:
        return {k.split("/", 1)[0] for k, a in self.arms.items() if a == arm}

    def share(self, arm: str) -> float:
        if not self.arms:
            return 0.0
        count = len(self.new) if arm == "new" else len(self.old)
        return count / len(self.arms) * 100.0

    def version_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for version in self.versions.values():
            counts[version] = counts.get(version, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def label(self, version: str) -> str:
        if version == UNKNOWN_VERSION:
            return f"unknown (unstamped, pre-{self.to_version})"
        return version


def build_adoption_query(
    start: datetime, end: datetime, after: dict | None = None
) -> dict:
    """Every running service with the version tag on its most recent document."""
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": [range_filter(start, end)]}},
        "aggs": build_composite(
            after,
            {
                "latest": {
                    "top_hits": {
                        "size": 1,
                        "sort": [{TIMESTAMP_FIELD: "desc"}],
                        "_source": ["tag.node_version", "tag.host_role"],
                    }
                }
            },
        ),
    }


def resolve_arms(pages: list[dict], to_version: str) -> Arms:
    """Split running services into the new and old arms of the rollout.

    The version is read off each service's own most recent document, so nothing
    has to be remembered between runs. A missing tag and the literal `unknown`
    mean the same thing -- a build older than ant-node #225, which stamps no
    version at all -- and both land in the old arm.
    """
    versions: dict[str, str] = {}
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            key = service_key(bucket["key"]["host"], bucket["key"]["service"])
            hits = bucket["latest"]["hits"]["hits"]
            tag = (hits[0]["_source"].get("tag") or {}) if hits else {}
            versions[key] = tag.get("node_version") or UNKNOWN_VERSION
    arms = {k: ("new" if v == to_version else "old") for k, v in versions.items()}
    return Arms(to_version=to_version, versions=versions, arms=arms)


def load_arms(es: EsClient, to_version: str, end: datetime) -> Arms:
    start = end - ADOPTION_LOOKBACK
    pages = list(
        es.composite_pages(lambda after: build_adoption_query(start, end, after))
    )
    return resolve_arms(pages, to_version)


# -- per-service message timestamps ---------------------------------------
def build_composite(
    after: dict | None, aggs: dict | None = None, size: int = COMPOSITE_PAGE_SIZE
):
    """A composite aggregation over (host, service), optionally with sub-aggs."""
    composite: dict = {
        "sources": [
            {"host": {"terms": {"field": HOST_FIELD}}},
            {"service": {"terms": {"field": SERVICE_FIELD}}},
        ],
        "size": size,
    }
    if after:
        composite["after"] = after
    node: dict = {"composite": composite}
    if aggs:
        node["aggs"] = aggs
    return {"services": node}


def build_latest_message_query(
    phrase: str, start: datetime, end: datetime, after=None
) -> dict:
    """Per (host, service), the most recent timestamp of a log phrase."""
    return {
        "size": 0,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    range_filter(start, end),
                    {"match_phrase": {MESSAGE_FIELD: phrase}},
                ]
            }
        },
        "aggs": build_composite(after, {"latest": {"max": {"field": TIMESTAMP_FIELD}}}),
    }


def collect_latest(
    es: EsClient, phrase: str, start: datetime, end: datetime
) -> dict[str, datetime]:
    """{service_key: most recent time that phrase was logged} over the window."""
    latest: dict[str, datetime] = {}
    pages = es.composite_pages(
        lambda after: build_latest_message_query(phrase, start, end, after)
    )
    for page in pages:
        for bucket in page["aggregations"]["services"]["buckets"]:
            when = epoch_millis_to_datetime(bucket["latest"]["value"])
            if when is not None:
                latest[service_key(bucket["key"]["host"], bucket["key"]["service"])] = (
                    when
                )
    return latest


# -- checks ----------------------------------------------------------------
@dataclass
class Check:
    """One expectation, with the number that decided it."""

    name: str
    status: str
    detail: str
    #: True when the status is PASS *because* a measure read zero. Those are the
    #: passes a zero control cannot support.
    passed_on_zero: bool = False
    data: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status in (FAIL, UNTRUSTED)


@dataclass
class Control:
    """The query that proves the fleet is being read at all."""

    name: str
    count: int

    @property
    def ok(self) -> bool:
        return self.count > 0


def apply_control(checks: list[Check], control: Control) -> list[Check]:
    """Downgrade every pass-on-zero when the control read zero.

    A zero control means the script cannot tell "nothing happened" from "the
    query is wrong", so a pass that rests on a zero is withdrawn rather than
    reported. Checks that are NOT APPLICABLE are left alone: they claimed
    nothing, so there is nothing to distrust.
    """
    if control.ok:
        return checks
    for check in checks:
        if check.status == PASS and check.passed_on_zero:
            check.status = UNTRUSTED
            check.detail += (
                f" (control `{control.name}` read 0, so this zero is not trusted)"
            )
    return checks


# -- rendering -------------------------------------------------------------
def markdown_table(
    headers: list[str], rows: list[list[str]], align: str | None = None
) -> str:
    """A GitHub-flavoured table. `align` is one char per column: `l`, `r` or `c`."""
    seps = []
    for index in range(len(headers)):
        kind = align[index] if align and index < len(align) else "l"
        seps.append({"l": "--", "r": "--:", "c": ":-:"}[kind])
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(seps) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_checks(checks: list[Check], control: Control) -> str:
    rows = [[c.name, c.status, c.detail] for c in checks]
    rows.append(
        [
            f"control: {control.name}",
            PASS if control.ok else "ZERO",
            f"{control.count:,} document(s) in the window",
        ]
    )
    return markdown_table(["check", "verdict", "detail"], rows, align="lcl")


def render_arms(arms: Arms) -> str:
    rows = [
        [
            arms.label(version),
            f"{count:,}",
            f"{count / max(1, arms.running) * 100:.2f}%",
        ]
        for version, count in arms.version_counts().items()
    ]
    return markdown_table(["version", "services", "share"], rows, align="lrr")


def not_applicable(arms: Arms) -> str:
    return f"not applicable: {len(arms.new)} new-version services"


def add_common_args(parser: argparse.ArgumentParser, *, to_version: str) -> None:
    parser.add_argument(
        "--window", default="1h", help="Measurement window, e.g. 15m, 1h, 6h."
    )
    parser.add_argument(
        "--to-version",
        default=to_version,
        help=f"The version being rolled out (default {to_version}).",
    )
    parser.add_argument(
        "--json-out", default=None, help="Write every figure as JSON here."
    )


def emit(result: dict, markdown: str, json_out: str | None) -> None:
    sys.stdout.write(markdown if markdown.endswith("\n") else markdown + "\n")
    if json_out:
        Path(json_out).expanduser().write_text(
            json.dumps(result, indent=2, default=str)
        )
        print(f"wrote {json_out}", file=sys.stderr)


def quiet_tls() -> None:
    """The production stacks are plain HTTP behind a firewall; silence the warning."""
    requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
