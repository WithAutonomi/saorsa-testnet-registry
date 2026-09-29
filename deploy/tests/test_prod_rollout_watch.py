"""Unit tests for `prod rollout-watch` (V2-1306).

The command's whole claim is that a rollout can be read out of Elasticsearch
with nothing remembered between runs, so what is pinned here is the arithmetic
that turns raw aggregation buckets into the operator-facing report: which
bucket a service lands in, which checkpoint band a share reaches, when the
per-arm comparison is worth making at all, and when repeated starts stop being
an upgrade and become a loop.
"""

from datetime import datetime, timedelta, timezone

import pytest

from saorsa_deploy.cmd.prod_rollout_watch import (
    ATTEMPT_PREFIX,
    BACKOFF_PREFIX,
    REPLACED_PREFIX,
    REUSED_PREFIX,
    SUCCESS_PREFIX,
    build_report,
    build_upgrade_events_query,
    checkpoint_band,
    classify_message,
    parse_window,
    render_report,
    summarise_adoption,
    summarise_restarts,
    summarise_upgrade_events,
)
from saorsa_deploy.main import main

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


# -- fixtures --------------------------------------------------------------
def _adoption_page(entries, after=None):
    """`entries` are (host, service_number, node_version, host_role)."""
    buckets = []
    for host, service, version, role in entries:
        tag = {"node_commit": "abc1234"}
        if version is not None:
            tag["node_version"] = version
        if role is not None:
            tag["host_role"] = role
        buckets.append(
            {
                "key": {"host": host, "service": service},
                "doc_count": 100,
                "latest": {"hits": {"hits": [{"_source": {"tag": tag}}]}},
            }
        )
    page = {"aggregations": {"services": {"buckets": buckets}}}
    if after is not None:
        page["aggregations"]["services"]["after_key"] = after
    return page


def _message_bucket(message, pairs, count):
    hosts = {}
    for host, service in pairs:
        hosts.setdefault(host, []).append(service)
    return {
        "key": message,
        "doc_count": count,
        "hosts": {
            "buckets": [
                {
                    "key": host,
                    "doc_count": count,
                    "services": {"buckets": [{"key": s, "doc_count": count} for s in services]},
                }
                for host, services in hosts.items()
            ]
        },
    }


def _restart_page(entries):
    """`entries` are (host, service_number, starts, versions)."""
    return {
        "aggregations": {
            "services": {
                "buckets": [
                    {
                        "key": {"host": host, "service": service},
                        "doc_count": starts,
                        "versions": {"buckets": [{"key": v, "doc_count": 1} for v in versions]},
                    }
                    for host, service, starts, versions in entries
                ]
            }
        }
    }


def _host_service_response(counts):
    """`counts` is {(host, service): doc_count}."""
    hosts = {}
    for (host, service), count in counts.items():
        hosts.setdefault(host, []).append((service, count))
    return {
        "hits": {"total": {"value": sum(counts.values())}},
        "aggregations": {
            "hosts": {
                "buckets": [
                    {
                        "key": host,
                        "doc_count": sum(c for _, c in services),
                        "services": {"buckets": [{"key": s, "doc_count": c} for s, c in services]},
                    }
                    for host, services in hosts.items()
                ]
            }
        },
    }


class FakeEs:
    """Answers each query kind from a fixture, and records which were asked.

    Dispatching on the body rather than on call order keeps the test honest
    about *which* query it is answering, and lets a test assert that the
    expensive health queries were never issued.
    """

    def __init__(self, *, adoption, upgrade_events=None, restarts=None, health=None):
        self.adoption = adoption
        self.upgrade_events = upgrade_events or {"aggregations": {"messages": {"buckets": []}}}
        self.restarts = restarts or [_restart_page([])]
        self.health = health or {}
        self.kinds: list[str] = []
        self._adoption_page = 0
        self._restart_page = 0

    def __call__(self, body):
        aggs = body.get("aggs", {})
        if "services" in aggs and "composite" in aggs["services"]:
            sub = aggs["services"]["aggs"]
            if "latest" in sub:
                self.kinds.append("adoption")
                page = self.adoption[self._adoption_page]
                self._adoption_page += 1
                return page
            if "versions" in sub:
                self.kinds.append("restarts")
                page = self.restarts[self._restart_page]
                self._restart_page += 1
                return page
            self.kinds.append("service_hours")
            return self.health["service_hours"]
        if "messages" in aggs:
            self.kinds.append("upgrade_events")
            return self.upgrade_events
        if "targets" in aggs:
            self.kinds.append("target_health")
            return self.health["targets"]
        if body["query"]["bool"].get("must_not"):
            self.kinds.append("missing_version")
            return self.health.get("missing_version", _host_service_response({}))
        filters = body["query"]["bool"]["filter"]
        if any("match_phrase" in f for f in filters):
            self.kinds.append("phrase")
            return self.health["phrase"]
        self.kinds.append("wire_tx")
        return self.health["wire_tx"]


# -- adoption --------------------------------------------------------------
MIXED_FLEET = [
    ("PROD-A", "1", "0.20.0", "WAN_NODE"),
    ("PROD-A", "2", "0.20.0", "WAN_NODE"),
    ("BOOT-1", "1", "0.20.0", "BOOTSTRAP"),
    ("PROD-B", "1", "unknown", "WAN_NODE"),
    ("PROD-B", "2", "unknown", "WAN_NODE"),
    ("PROD-B", "3", "unknown", "WAN_NODE"),
    ("PROD-C", "1", "unknown", "WAN_NODE"),
    ("BOOT-2", "1", "unknown", "BOOTSTRAP"),
]


def test_adoption_table_splits_the_fleet_by_version():
    adoption = summarise_adoption([_adoption_page(MIXED_FLEET)], "0.20.0")

    assert adoption["running_services"] == 8
    assert adoption["bootstrap_services"] == 2
    # The to-version leads the table regardless of which bucket is larger.
    new, old = adoption["versions"]
    assert new["label"] == "0.20.0"
    assert (new["services"], new["share"], new["hosts"], new["bootstraps"]) == (
        3,
        37.5,
        2,
        1,
    )
    assert old["label"] == "unknown (unstamped, pre-0.20.0)"
    assert (old["services"], old["share"], old["hosts"], old["bootstraps"]) == (
        5,
        62.5,
        3,
        1,
    )
    assert adoption["band"] == "20–50%"


def test_a_missing_node_version_tag_means_the_same_as_the_literal_unknown():
    # Pre-#225 builds do not stamp at all; that is not a third bucket.
    adoption = summarise_adoption([_adoption_page([("PROD-A", "1", None, "WAN_NODE")])], "0.20.0")
    assert adoption["versions"][0]["label"] == "unknown (unstamped, pre-0.20.0)"


def test_a_stamped_old_arm_keeps_its_own_label():
    # Only the literal `unknown` gets the unstamped note: every future rollout
    # has a real version on the old side.
    adoption = summarise_adoption(
        [
            _adoption_page(
                [
                    ("PROD-A", "1", "0.21.0", "WAN_NODE"),
                    ("PROD-B", "1", "0.20.0", "WAN_NODE"),
                ]
            )
        ],
        "0.21.0",
    )
    assert [row["label"] for row in adoption["versions"]] == ["0.21.0", "0.20.0"]


@pytest.mark.parametrize(
    "share,band",
    [
        (0.0, "none"),
        (9.9, "none"),
        (10.0, "10–20%"),
        (19.99, "10–20%"),
        (20.0, "20–50%"),
        (49.9, "20–50%"),
        (50.0, "50–99%"),
        (98.9, "50–99%"),
        (99.0, "complete"),
        (100.0, "complete"),
    ],
)
def test_checkpoint_band_boundaries(share, band):
    assert checkpoint_band(share) == band


def test_adoption_pages_through_a_composite_aggregation():
    # A fully-adopted fleet, so the report stops after the cheap queries and
    # what is under test is only that both composite pages were consumed.
    entries = [(host, svc, "0.20.0", role) for host, svc, _, role in MIXED_FLEET]
    fake = FakeEs(
        adoption=[
            _adoption_page(entries[:4], after={"host": "PROD-B", "service": "1"}),
            _adoption_page(entries[4:]),
        ]
    )
    result = build_report(
        fake, name="ant-prod-01", to_version="0.20.0", window=timedelta(hours=1), now=NOW
    )
    assert fake.kinds.count("adoption") == 2
    assert result["adoption"]["running_services"] == 8


# -- upgrade events --------------------------------------------------------
# Verbatim lines from the production 0.19.0 -> 0.20.0 rollout (2026-09-24/25),
# so what the matchers are pinned against is what the fleet actually logs.
BARE_START = "Starting auto-apply upgrade..."
APPLY_START = "Starting auto-apply upgrade from 0.19.0 to 0.20.0"
SUCCESS_LINE = "Upgrade to 0.20.0 successful, initiating graceful shutdown"
REPLACED_LINE = "Successfully upgraded to version 0.20.0! Restarting..."
REUSED_LINE = "Binary already upgraded to 0.20.0 by another service, skipping replacement"
BACKOFF_LINE = (
    "Upgrade rollout delay elapsed but previous apply did not succeed; backing off, "
    "next check scheduled for 2026-09-24T14:18:51.302923193+00:00"
)
TRANSIENT_GITHUB = (
    "Error during upgrade process: network error: GitHub API request failed: error sending "
    "request for url (https://api.github.com/repos/WithAutonomi/ant-node/releases)"
)
TRANSIENT_STATUS = (
    "Error during upgrade process: network error: GitHub API returned status: 502 Bad Gateway"
)


def _events_response(buckets):
    return {"aggregations": {"messages": {"buckets": buckets}}}


def _event_counts(summary):
    return {e["message"]: e["count"] for e in summary["events"]}


def test_the_two_start_lines_of_one_attempt_count_one_attempt():
    # node.rs and apply.rs both log a start, back to back. Matching their
    # common prefix counted every attempt on the fleet twice.
    summary = summarise_upgrade_events(
        _events_response(
            [
                _message_bucket(BARE_START, [("PROD-A", "1")], 767),
                _message_bucket(APPLY_START, [("PROD-A", "1")], 767),
            ]
        )
    )
    assert _event_counts(summary)[ATTEMPT_PREFIX] == 767


def test_one_service_upgrading_counts_one_success_and_one_replaced_detail():
    # The host's first upgrader logs both the canonical success and the
    # binary-replacement line; only the first is a success.
    summary = summarise_upgrade_events(
        _events_response(
            [
                _message_bucket(SUCCESS_LINE, [("PROD-A", "1")], 1),
                _message_bucket(REPLACED_LINE, [("PROD-A", "1")], 1),
            ]
        )
    )
    counts = _event_counts(summary)
    assert counts[SUCCESS_PREFIX] == 1
    assert counts[REPLACED_PREFIX] == 1
    assert counts[REUSED_PREFIX] == 0


def test_the_skip_line_counts_as_reused_and_the_details_add_up():
    summary = summarise_upgrade_events(
        _events_response(
            [
                _message_bucket(SUCCESS_LINE, [("PROD-A", "1")], 767),
                _message_bucket(REPLACED_LINE, [("PROD-A", "1")], 66),
                _message_bucket(REUSED_LINE, [("PROD-A", "2")], 701),
            ]
        )
    )
    counts = _event_counts(summary)
    assert counts[REUSED_PREFIX] == 701
    assert counts[REPLACED_PREFIX] + counts[REUSED_PREFIX] == counts[SUCCESS_PREFIX] == 767


def test_the_backoff_line_is_counted():
    # A non-zero backoff against no failures is a failure the matchers missed.
    summary = summarise_upgrade_events(
        _events_response([_message_bucket(BACKOFF_LINE, [("PROD-A", "1")], 1)])
    )
    assert _event_counts(summary)[BACKOFF_PREFIX] == 1


@pytest.mark.parametrize(
    "message,kind,label",
    [
        # Check phase: nothing was attempted.
        (TRANSIENT_GITHUB, "transient", "network error: GitHub API request failed:"),
        (TRANSIENT_STATUS, "transient", "network error: GitHub API returned status:"),
        # A rolled-back apply, and an apply that returned Err.
        (
            "Error during upgrade process: upgrade error: Binary replacement failed: EACCES",
            "failure",
            "Error during upgrade process:",
        ),
        (
            "Error during upgrade process: I/O error: Backup failed: No space left on device",
            "failure",
            "Error during upgrade process:",
        ),
    ],
)
def test_the_three_upgrade_error_sites_are_classified(message, kind, label):
    assert classify_message(message)[:2] == (kind, label)


def test_an_unknown_upgrade_error_variant_is_a_failure():
    # Fail loud: a variant a future ant-node adds must not be silently
    # downgraded to informational.
    kind, label, note = classify_message(
        "Error during upgrade process: quantum error: the future happened"
    )
    assert (kind, label, note) == ("failure", "Error during upgrade process:", None)


def test_a_spawn_failure_carries_its_annotation():
    summary = summarise_upgrade_events(
        _events_response(
            [
                _message_bucket(
                    "Error during upgrade process: upgrade error: Failed to spawn new binary: "
                    "No such file or directory",
                    [("PROD-A", "1")],
                    1,
                )
            ]
        )
    )
    failure = summary["failures"][0]
    assert failure["message"] == "Error during upgrade process:"
    assert failure["notes"] == ["binary already replaced on disk; old version still running"]


def test_transient_errors_are_annotated_recovered_from_the_new_arm():
    summary = summarise_upgrade_events(
        _events_response(
            [_message_bucket(TRANSIENT_GITHUB, [("PROD-A", "1"), ("PROD-B", "2")], 15)]
        ),
        {"PROD-A/1": "new", "PROD-B/2": "old"},
    )
    # Nothing was attempted, so a service now on the new arm went on to
    # upgrade; the one still on the old arm needs a human.
    entry = summary["transient"][0]
    assert entry["message"] == "network error: GitHub API request failed:"
    assert entry["count"] == 15
    assert entry["recovered"] == ["PROD-A/1"]
    assert entry["follow_up"] == ["PROD-B/2"]
    # Transient errors never reach the gate-feeding table.
    assert summary["failures"] == []


def test_transient_errors_are_not_recovered_without_arm_information():
    summary = summarise_upgrade_events(
        _events_response([_message_bucket(TRANSIENT_STATUS, [("PROD-A", "1")], 1)])
    )
    entry = summary["transient"][0]
    assert entry["recovered"] == []
    assert entry["follow_up"] == ["PROD-A/1"]


def test_failures_are_grouped_by_prefix_with_their_host_service_pairs():
    response = _events_response(
        [
            _message_bucket(
                "Download failed: connection reset",
                [("PROD-A", "1"), ("PROD-B", "4")],
                2,
            ),
            _message_bucket("Download failed: 404 not found", [("PROD-A", "1")], 1),
            _message_bucket("Signature download failed: timeout", [("PROD-C", "2")], 5),
            _message_bucket(REPLACED_LINE, [("PROD-A", "1")], 40),
            _message_bucket("some unrelated ant_node line", [("PROD-A", "1")], 9),
        ]
    )
    summary = summarise_upgrade_events(response)

    download, signature = summary["failures"]
    # The two `Download failed:` variants differ only in the interpolated
    # detail, so they are one failure with the union of the services.
    assert download["message"] == "Download failed:"
    assert download["count"] == 3
    assert download["services"] == ["PROD-A/1", "PROD-B/4"]
    assert signature["message"] == "Signature download failed:"
    assert signature["services"] == ["PROD-C/2"]

    counts = _event_counts(summary)
    assert counts[REPLACED_PREFIX] == 40
    # Every category is reported, at zero when nothing was seen.
    assert counts["Staged rollout delay elapsed, ready to upgrade"] == 0
    assert "some unrelated ant_node line" not in counts


def test_the_upgrade_query_reaches_the_node_module_target():
    # Every node.rs line — the canonical success and the check-phase error —
    # logs under `ant_node::node`, which the original filter did not match, so
    # they were dropped from the query entirely.
    query = build_upgrade_events_query(NOW - timedelta(hours=1), NOW)
    target_filter = next(
        f
        for f in query["query"]["bool"]["filter"]
        if "should" in f.get("bool", {})
        and any("tag.target.keyword" in str(s) for s in f["bool"]["should"])
    )
    assert {"term": {"tag.target.keyword": "ant_node::node"}} in target_filter["bool"]["should"]


def test_the_report_renders_both_tables_and_the_detail_arithmetic():
    fake = FakeEs(
        adoption=_one_sided_fleet(old_services=1, new_services=99),
        upgrade_events=_events_response(
            [
                _message_bucket(BARE_START, [("PROD-N0", "1")], 767),
                _message_bucket(APPLY_START, [("PROD-N0", "1")], 767),
                _message_bucket(SUCCESS_LINE, [("PROD-N0", "1")], 767),
                _message_bucket(REPLACED_LINE, [("PROD-N0", "1")], 66),
                _message_bucket(REUSED_LINE, [("PROD-N0", "1")], 701),
                _message_bucket(TRANSIENT_GITHUB, [("PROD-N0", "1")], 3),
            ]
        ),
    )
    report = render_report(
        build_report(
            fake, name="ant-prod-01", to_version="0.20.0", window=timedelta(hours=168), now=NOW
        )
    )
    assert "| upgrade attempts | 767 |" in report
    assert "| upgrades succeeded | 767 |" in report
    assert "| — of which binary replaced | 66 |" in report
    assert "| — of which shared binary reused | 701 |" in report
    assert "| retry backoff | 0 |" in report
    assert "Detail check: 66 replaced + 701 reused = 767, against 767 succeeded." in report
    assert "Failures: none." in report
    assert "network error: GitHub API request failed: | 3 | PROD-N0/1 | all recovered |" in report


# -- restarts --------------------------------------------------------------
def test_restart_loop_is_flagged_above_two_starts():
    restarts = summarise_restarts(
        [
            _restart_page(
                [
                    ("PROD-A", "1", 1, ["0.20.0"]),
                    ("PROD-B", "2", 2, ["unknown", "0.20.0"]),
                    ("PROD-C", "3", 3, ["0.20.0"]),
                ]
            )
        ]
    )
    # A single start is not a restart at all and never reaches the table.
    assert [r["service"] for r in restarts] == ["PROD-C/3", "PROD-B/2"]
    upgraded = next(r for r in restarts if r["service"] == "PROD-B/2")
    looping = next(r for r in restarts if r["service"] == "PROD-C/3")
    assert upgraded["restart_loop"] is False
    assert upgraded["versions"] == ["unknown", "0.20.0"]
    assert looping["restart_loop"] is True


# -- per-arm ---------------------------------------------------------------
def _one_sided_fleet(old_services: int, new_services: int):
    entries = [(f"PROD-N{i}", "1", "0.20.0", "WAN_NODE") for i in range(new_services)]
    entries += [(f"PROD-O{i}", "1", "unknown", "WAN_NODE") for i in range(old_services)]
    return [_adoption_page(entries)]


def test_per_arm_is_suspended_and_unqueried_when_the_old_arm_is_tiny():
    fake = FakeEs(adoption=_one_sided_fleet(old_services=1, new_services=99))
    result = build_report(
        fake, name="ant-prod-01", to_version="0.20.0", window=timedelta(hours=1), now=NOW
    )

    per_arm = result["per_arm"]
    assert per_arm["suspended"] is True
    assert per_arm["short_arm"] == "old"
    assert per_arm["reason"] == "old arm is 1 services (< 5% of running)"
    # The health queries are the expensive half of the report; a suspended
    # comparison must not pay for them.
    assert "target_health" not in fake.kinds
    assert "service_hours" not in fake.kinds
    assert "Per-arm comparison suspended" in render_report(result)


def test_per_arm_is_suspended_before_the_rollout_starts():
    # Nothing is on the to-version yet, so it is the NEW arm that is empty.
    # Comparing an arm of zero against the whole fleet measures nothing.
    fake = FakeEs(adoption=_one_sided_fleet(old_services=100, new_services=0))
    result = build_report(
        fake, name="ant-prod-01", to_version="0.20.0", window=timedelta(hours=1), now=NOW
    )

    per_arm = result["per_arm"]
    assert per_arm["suspended"] is True
    assert per_arm["short_arm"] == "new"
    assert per_arm["reason"] == "new arm is 0 services (< 5% of running)"
    assert "target_health" not in fake.kinds


def test_per_arm_is_evaluated_once_the_old_arm_is_big_enough():
    fake = FakeEs(
        adoption=_one_sided_fleet(old_services=10, new_services=90),
        health={
            "service_hours": {
                "aggregations": {
                    "services": {
                        "buckets": [
                            {
                                "key": {"host": f"PROD-N{i}", "service": "1"},
                                "doc_count": 10,
                                "active": {"buckets": [{}, {}, {}, {}]},
                            }
                            for i in range(90)
                        ]
                        + [
                            {
                                "key": {"host": f"PROD-O{i}", "service": "1"},
                                "doc_count": 10,
                                "active": {"buckets": [{}, {}, {}, {}]},
                            }
                            for i in range(10)
                        ]
                    }
                }
            },
            "targets": {
                "aggregations": {
                    "targets": {
                        "buckets": [
                            {
                                "key": "ant_node::replication",
                                "doc_count": 100,
                                "hosts": {
                                    "buckets": [
                                        {
                                            "key": "PROD-N0",
                                            "doc_count": 90,
                                            "services": {
                                                "buckets": [
                                                    {
                                                        "key": "1",
                                                        "doc_count": 90,
                                                        "levels": {
                                                            "buckets": [
                                                                {
                                                                    "key": "ERROR",
                                                                    "doc_count": 90,
                                                                }
                                                            ]
                                                        },
                                                    }
                                                ]
                                            },
                                        },
                                        {
                                            "key": "PROD-O0",
                                            "doc_count": 10,
                                            "services": {
                                                "buckets": [
                                                    {
                                                        "key": "1",
                                                        "doc_count": 10,
                                                        "levels": {
                                                            "buckets": [
                                                                {
                                                                    "key": "ERROR",
                                                                    "doc_count": 10,
                                                                }
                                                            ]
                                                        },
                                                    }
                                                ]
                                            },
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                }
            },
            "phrase": _host_service_response({("PROD-N0", "1"): 9, ("PROD-O0", "1"): 1}),
            "wire_tx": {
                "aggregations": {
                    "hosts": {
                        "buckets": [
                            {
                                "key": "PROD-N0",
                                "doc_count": 2,
                                "services": {
                                    "buckets": [
                                        {
                                            "key": "1",
                                            "doc_count": 2,
                                            "lo": {"value": 100.0},
                                            "hi": {"value": 1100.0},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            },
        },
    )
    result = build_report(
        fake, name="ant-prod-01", to_version="0.20.0", window=timedelta(hours=1), now=NOW
    )

    per_arm = result["per_arm"]
    assert per_arm["suspended"] is False
    assert per_arm["service_counts"] == {"new": 90, "old": 10}
    # 4 buckets of 15 minutes is one service-hour each.
    assert per_arm["service_hours"] == {"new": 90.0, "old": 10.0}
    errors = next(
        r for r in per_arm["rows"] if r["metric"].startswith("ant_node::replication ERROR")
    )
    assert errors["new"] == pytest.approx(1000.0)
    assert errors["old"] == pytest.approx(1000.0)
    assert errors["ratio"] == pytest.approx(1.0)
    wire = next(r for r in per_arm["rows"] if r["metric"].startswith("wire_tx_bytes"))
    assert wire["new"] == pytest.approx(1000.0 / 90.0)
    # Nothing reported on the old arm: a ratio against zero is not a number.
    assert wire["old"] == 0.0
    assert wire["ratio"] is None


# -- window parsing --------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("15m", timedelta(minutes=15)),
        ("1h", timedelta(hours=1)),
        ("6h", timedelta(hours=6)),
        ("24h", timedelta(hours=24)),
    ],
)
def test_parse_window(value, expected):
    assert parse_window(value) == expected


@pytest.mark.parametrize("value", ["", "1d", "h", "0h", "-1h", "1 h"])
def test_parse_window_rejects_anything_else(value):
    with pytest.raises(Exception):
        parse_window(value)


# -- removal of track-upgrades ---------------------------------------------
def test_track_upgrades_is_no_longer_a_command():
    with pytest.raises(SystemExit) as exc:
        main(["track-upgrades", "summary", "--db", "/tmp/x.db"])
    assert exc.value.code != 0


def test_prod_rollout_watch_is_parsed():
    with pytest.raises(SystemExit) as exc:
        main(["prod", "rollout-watch", "--help"])
    assert exc.value.code == 0
