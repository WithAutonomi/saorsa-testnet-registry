"""The 2026-09-23 train's rollout-watch scripts (V2-1308).

Every script here answers a question about the production fleet, so nothing in
these tests may touch a network: the aggregation responses are fixtures and what
is pinned is the arithmetic that turns them into a verdict. Four of those
decisions are the whole reason the scripts are not ad-hoc queries:

* a service's arm comes from the version on its own latest document, and an
  unstamped build (`unknown`, or no tag at all) is the OLD arm -- getting that
  backwards would put the pre-#225 fleet in the new arm and pass everything;
* a service that restarted and has not logged the listening line since is a
  miss, while one whose window covers neither event is unassessed, because
  otherwise every quiet service becomes a regression;
* traffic coverage excludes services younger than ten minutes, since the first
  summary is emitted five minutes after start;
* the migration signal is a PERSISTENCE measure -- a single `agent = none`
  observation is legitimate and only a recurrence is the bug.

The scripts live outside the package (they are one train's worth of questions),
so they are loaded by path the way the on-VM runtime scripts are.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLOUT_DIR = REPO_ROOT / "scripts" / "rollout-watch"
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _load(path: Path, name: str):
    # `_common` imports must resolve for the scripts that import it.
    if str(ROLLOUT_DIR) not in sys.path:
        sys.path.insert(0, str(ROLLOUT_DIR))
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


common = _load(ROLLOUT_DIR / "_common.py", "_common")
webrtc_coverage = _load(ROLLOUT_DIR / "webrtc_coverage.py", "rw_webrtc_coverage")
traffic_targets = _load(ROLLOUT_DIR / "traffic_targets.py", "rw_traffic_targets")
migration_signal = _load(ROLLOUT_DIR / "migration_signal.py", "rw_migration_signal")
artefact_smoke = _load(REPO_ROOT / "scripts" / "client_artefact_smoke.py", "client_artefact_smoke")


# -- fixtures --------------------------------------------------------------
def adoption_page(entries, after=None):
    """`entries` are (host, service_number, node_version or None)."""
    buckets = []
    for host, service, version in entries:
        tag = {"host_role": "WAN_NODE"}
        if version is not None:
            tag["node_version"] = version
        buckets.append(
            {
                "key": {"host": host, "service": service},
                "doc_count": 10,
                "latest": {"hits": {"hits": [{"_source": {"tag": tag}}]}},
            }
        )
    page = {"aggregations": {"services": {"buckets": buckets}}}
    if after is not None:
        page["aggregations"]["services"]["after_key"] = after
    return page


def arms_of(entries, to_version="0.20.0"):
    return common.resolve_arms([adoption_page(entries)], to_version)


def family(phrase, services, count=None):
    return webrtc_coverage.Family(
        phrase=phrase, count=count if count is not None else len(services), services=list(services)
    )


def signal(name, emitters, kind="target"):
    return traffic_targets.Signal(name=name, kind=kind, emitters=frozenset(emitters))


def pairs_page(entries):
    """`entries` are (host, service, peer, [interval starts as datetimes])."""
    buckets = []
    for host, service, peer, starts in entries:
        buckets.append(
            {
                "key": {"host": host, "service": service, "peer": peer},
                "doc_count": len(starts),
                "intervals": {
                    "buckets": [
                        {"key": int(when.timestamp() * 1000), "doc_count": 1} for when in starts
                    ]
                },
                "observers": {"buckets": [{"key": "obs-" + service, "doc_count": len(starts)}]},
            }
        )
    return {"aggregations": {"services": {"buckets": buckets}}}


# -- 1. arm resolution -----------------------------------------------------
def test_latest_document_version_decides_the_arm():
    arms = arms_of(
        [
            ("HOST-A", "1", "0.20.0"),
            ("HOST-A", "2", "0.19.0"),
            ("HOST-B", "1", "unknown"),
            ("HOST-B", "2", None),
        ]
    )
    assert arms.new == ["HOST-A/1"]
    assert arms.old == ["HOST-A/2", "HOST-B/1", "HOST-B/2"]
    assert arms.running == 4


def test_unstamped_builds_are_the_old_arm_and_labelled_as_such():
    """A missing tag and the literal `unknown` are the same pre-#225 build."""
    arms = arms_of([("HOST-A", "1", None), ("HOST-B", "1", "unknown")])
    assert arms.new == []
    assert arms.version_counts() == {"unknown": 2}
    assert arms.label("unknown") == "unknown (unstamped, pre-0.20.0)"
    assert arms.label("0.19.0") == "0.19.0"


def test_arm_shares_and_hosts():
    arms = arms_of(
        [("HOST-A", "1", "0.20.0"), ("HOST-A", "2", "0.20.0"), ("HOST-B", "1", "0.19.0")]
    )
    assert arms.hosts("new") == {"HOST-A"}
    assert arms.share("new") == pytest.approx(66.666, rel=1e-3)
    assert arms.share("old") == pytest.approx(33.333, rel=1e-3)


def test_composite_pages_follow_after_key():
    class Es:
        def __init__(self):
            self.calls = []

        def search(self, body):
            after = body["aggs"]["services"]["composite"].get("after")
            self.calls.append(after)
            if after is None:
                return adoption_page([("HOST-A", "1", "0.20.0")], after={"host": "HOST-A"})
            return adoption_page([("HOST-B", "1", "0.19.0")])

    es = Es()
    pages = list(
        common.EsClient.composite_pages(
            es, lambda after: common.build_adoption_query(NOW - timedelta(minutes=15), NOW, after)
        )
    )
    assert es.calls == [None, {"host": "HOST-A"}]
    arms = common.resolve_arms(pages, "0.20.0")
    assert arms.new == ["HOST-A/1"] and arms.old == ["HOST-B/1"]


def test_parse_window_and_label():
    assert common.parse_window("15m") == timedelta(minutes=15)
    assert common.parse_window("6h") == timedelta(hours=6)
    assert common.window_label(timedelta(hours=6)) == "6h"
    assert common.window_label(timedelta(minutes=45)) == "45m"
    for bad in ("", "1d", "0h", "h"):
        with pytest.raises(common.RolloutCheckError):
            common.parse_window(bad)


def test_control_downgrades_a_pass_that_rested_on_a_zero():
    checks = [
        common.Check(
            name="zero pass", status=common.PASS, detail="nothing seen", passed_on_zero=True
        ),
        common.Check(name="real pass", status=common.PASS, detail="42 seen"),
        common.Check(name="vacuous", status=common.NOT_APPLICABLE, detail="no new services"),
    ]
    common.apply_control(checks, common.Control(name="starts", count=0))
    assert checks[0].status == common.UNTRUSTED and checks[0].failed
    assert checks[1].status == common.PASS
    # NOT APPLICABLE claimed nothing, so there is nothing to distrust.
    assert checks[2].status == common.NOT_APPLICABLE and not checks[2].failed


def test_a_nonzero_control_leaves_every_check_alone():
    checks = [
        common.Check(
            name="zero pass", status=common.PASS, detail="nothing seen", passed_on_zero=True
        )
    ]
    common.apply_control(checks, common.Control(name="starts", count=17))
    assert checks[0].status == common.PASS


# -- 2. webrtc_coverage ----------------------------------------------------
def test_a_restart_after_the_last_listening_line_is_a_miss():
    started = NOW - timedelta(minutes=5)
    listened = NOW - timedelta(minutes=40)
    coverage = webrtc_coverage.evaluate_listening_coverage(
        ["HOST-A/1"], {"HOST-A/1": started}, {"HOST-A/1": listened}
    )
    assert coverage.covered == []
    assert len(coverage.misses) == 1
    key, reason = coverage.misses[0]
    assert key == "HOST-A/1"
    assert "restarted" in reason and "last listening line" in reason


def test_a_listening_line_after_the_restart_is_covered():
    coverage = webrtc_coverage.evaluate_listening_coverage(
        ["HOST-A/1"],
        {"HOST-A/1": NOW - timedelta(minutes=40)},
        {"HOST-A/1": NOW - timedelta(minutes=39)},
    )
    assert coverage.covered == ["HOST-A/1"] and coverage.misses == []
    assert coverage.share == 100.0


def test_a_restart_with_no_listening_line_at_all_is_a_miss():
    coverage = webrtc_coverage.evaluate_listening_coverage(
        ["HOST-A/1"], {"HOST-A/1": NOW - timedelta(minutes=30)}, {}
    )
    assert [k for k, _ in coverage.misses] == ["HOST-A/1"]
    assert "no listening line since" in coverage.misses[0][1]


def test_a_window_covering_neither_event_is_unassessed_not_a_miss():
    """Otherwise every quiet service reads as a regression after the rollout."""
    coverage = webrtc_coverage.evaluate_listening_coverage(["HOST-A/1"], {}, {})
    assert coverage.unassessed == ["HOST-A/1"]
    assert coverage.misses == [] and coverage.covered == []
    assert coverage.assessed == 0


def test_a_restart_inside_the_grace_window_is_pending_not_a_miss():
    """0.20.0 opens the listener 7-12 min after start; that is not a failure."""
    coverage = webrtc_coverage.evaluate_listening_coverage(
        ["HOST-A/1"], {"HOST-A/1": NOW - timedelta(minutes=11)}, {}, now=NOW
    )
    assert coverage.misses == [] and coverage.covered == []
    assert [k for k, _ in coverage.pending] == ["HOST-A/1"]
    assert coverage.assessed == 0


def test_a_restart_past_the_grace_window_with_no_listening_line_is_a_miss():
    coverage = webrtc_coverage.evaluate_listening_coverage(
        ["HOST-A/1"],
        {"HOST-A/1": NOW - webrtc_coverage.LISTEN_GRACE - timedelta(seconds=1)},
        {},
        now=NOW,
    )
    assert [k for k, _ in coverage.misses] == ["HOST-A/1"]
    assert coverage.pending == []


def test_the_sample_only_draws_new_arm_services():
    """Old-version services never write webrtc-direct.multiaddr."""
    vms = [
        _vm("ant-prod-01-hz-fsn1-1", "10.0.1.1", "hetzner", ("ant-node-1", "ant-node-2")),
        _vm("ant-prod-01-ovh-BHS5-1", "10.0.2.1", "ovh", ("ant-node-1", "ant-node-2")),
    ]
    new_arm = {"ANT-PROD-01-HZ-FSN1-1/2"}
    restricted = webrtc_coverage.new_arm_vms(vms, new_arm)
    assert restricted[0].services == ("ant-node-2",)
    assert restricted[1].services == ()
    chosen = webrtc_coverage.select_sample(restricted, sample=10)
    assert [(vm.name, vm.services[0]) for vm in chosen] == [
        ("ant-prod-01-hz-fsn1-1", "ant-node-2")
    ]


def test_new_arm_matching_does_not_confuse_service_1_with_service_12():
    vm = _vm("ant-prod-01-ovh-GRA9-7", "10.0.2.7", "ovh", ("ant-node-1", "ant-node-12"))
    assert webrtc_coverage.new_arm_services(vm, {"ANT-PROD-01-OVH-GRA9-7/12"}) == (
        "ant-node-12",
    )


def test_warn_family_threshold_is_five_percent_of_the_new_arm():
    arms = arms_of([(f"HOST-{i}", "1", "0.20.0") for i in range(20)])
    assert len(arms.new) == 20
    # One of twenty is 5.0%, which is not "more than 5%".
    at_limit = evaluated(arms, ["HOST-0/1"])
    assert at_limit.status == common.PASS
    # Two of twenty is 10%.
    over = evaluated(arms, ["HOST-0/1", "HOST-1/1"])
    assert over.status == common.FAIL
    assert "over 5.0%" in over.detail


def evaluated(arms, services):
    return webrtc_coverage.evaluate_warn_families(
        [family("WebRTC Direct listener error", services)], arms
    )


def test_warn_families_only_count_the_new_arm():
    """An old-version service's WARN is not evidence about the release."""
    arms = arms_of([("HOST-A", "1", "0.20.0")] + [(f"HOST-{i}", "1", "0.19.0") for i in range(5)])
    check = evaluated(arms, [f"HOST-{i}/1" for i in range(5)])
    assert check.status == common.PASS


def test_warn_families_with_no_new_services_is_not_applicable():
    arms = arms_of([("HOST-A", "1", "0.19.0")])
    check = evaluated(arms, ["HOST-A/1"])
    assert check.status == common.NOT_APPLICABLE
    assert check.detail == "not applicable: 0 new-version services"


def test_a_silent_warn_check_is_flagged_as_passing_on_zero():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    check = webrtc_coverage.evaluate_warn_families(
        [family(phrase, []) for phrase in webrtc_coverage.WARN_FAMILIES], arms
    )
    assert check.status == common.PASS and check.passed_on_zero


def _vm(name, ip, provider, services=("ant-node-1",)):
    from saorsa_deploy.analysis.hosts import Vm

    return Vm(
        name=name,
        ip=ip,
        role="node",
        nat=False,
        ssh_user="root",
        ssh_key="~/.ssh/ant_deploy",
        services=tuple(services),
        provider=provider,
        region="x",
    )


def test_the_sample_spans_at_least_three_hosts_per_provider():
    vms = (
        [_vm(f"hz-{i}", f"10.0.1.{i}", "hetzner") for i in range(6)]
        + [_vm(f"ovh-{i}", f"10.0.2.{i}", "ovh") for i in range(6)]
        + [_vm(f"cb-{i}", f"10.0.3.{i}", "contabo") for i in range(6)]
    )
    chosen = webrtc_coverage.select_sample(vms, sample=10)
    assert len(chosen) == 10
    for provider in webrtc_coverage.SAMPLE_PROVIDERS:
        assert sum(1 for vm in chosen if vm.provider == provider) >= 3
    # Deterministic, so a failure can be chased on the same box twice.
    assert [vm.name for vm in chosen] == [vm.name for vm in webrtc_coverage.select_sample(vms, 10)]


def test_the_sample_skips_hosts_with_no_services():
    vms = [_vm("hz-0", "10.0.1.0", "hetzner", services=()), _vm("hz-1", "10.0.1.1", "hetzner")]
    assert [vm.name for vm in webrtc_coverage.select_sample(vms, sample=5)] == ["hz-1"]


def test_advertised_sample_fails_when_a_probe_gets_no_answer():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    bad = webrtc_coverage.Advertised(
        host="hz-0",
        ip="10.0.1.0",
        provider="hetzner",
        service="ant-node-1",
        multiaddr="/ip4/10.0.1.0/udp/40000/webrtc-direct/certhash/uAAA/p2p/" + "ab" * 32,
        port=40000,
        advertised_ip="10.0.1.0",
        probed=False,
        error="no ICE answer on udp/40000",
    )
    check = webrtc_coverage.evaluate_advertised([bad], arms, sample=1)
    assert check.status == common.FAIL and "no ICE answer" in check.detail


def test_advertised_sample_is_skipped_at_sample_zero():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    check = webrtc_coverage.evaluate_advertised([], arms, sample=0)
    assert check.status == common.NOT_APPLICABLE and "--sample 0" in check.detail


def test_multiaddr_path_is_the_service_data_dir():
    assert webrtc_coverage.multiaddr_path("ant-node-7") == (
        "/mnt/ant-node-storage/data/ant-node-7/webrtc-direct.multiaddr"
    )


# -- 3. traffic_targets ----------------------------------------------------
def test_services_restarted_inside_ten_minutes_are_excluded():
    """The first traffic summary lands five minutes after start."""
    services = ["HOST-A/1", "HOST-A/2", "HOST-B/1"]
    starts = {
        "HOST-A/1": NOW - timedelta(minutes=4),  # too young
        "HOST-A/2": NOW - timedelta(minutes=30),  # old enough
        # HOST-B/1 has no start in the window, so it started before it.
    }
    assert traffic_targets.services_up_long_enough(services, starts, NOW) == [
        "HOST-A/2",
        "HOST-B/1",
    ]


def test_a_service_exactly_at_the_uptime_boundary_is_included():
    starts = {"HOST-A/1": NOW - traffic_targets.MIN_UPTIME}
    assert traffic_targets.services_up_long_enough(["HOST-A/1"], starts, NOW) == ["HOST-A/1"]


def test_target_coverage_measures_only_the_eligible_services():
    arms = arms_of([(f"HOST-{i}", "1", "0.20.0") for i in range(4)])
    # Three of four emitted; the fourth restarted a minute ago and is excluded,
    # so coverage is 3/3 rather than 3/4 (75%, which would fail).
    eligible = ["HOST-0/1", "HOST-1/1", "HOST-2/1"]
    signals = [signal(t, eligible) for t in traffic_targets.NEW_TARGETS]
    check = traffic_targets.evaluate_target_coverage(signals, eligible, arms)
    assert check.status == common.PASS
    assert all(share == 100.0 for share in check.data["shares"].values())


def test_target_coverage_fails_below_ninety_five_percent():
    arms = arms_of([(f"HOST-{i}", "1", "0.20.0") for i in range(20)])
    eligible = [f"HOST-{i}/1" for i in range(20)]
    signals = [signal(t, eligible) for t in traffic_targets.NEW_TARGETS]
    # One target missing from two of twenty services: 90%.
    signals[1] = signal(traffic_targets.NEW_TARGETS[1], eligible[:18])
    check = traffic_targets.evaluate_target_coverage(signals, eligible, arms)
    assert check.status == common.FAIL and "below 95.0%" in check.detail


def test_target_coverage_is_not_applicable_when_every_new_service_is_too_young():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    check = traffic_targets.evaluate_target_coverage(
        [signal(t, []) for t in traffic_targets.NEW_TARGETS], [], arms
    )
    assert check.status == common.NOT_APPLICABLE and "under 10 minutes" in check.detail


def test_old_arm_emission_is_a_failure():
    """An old build emitting V2-834 fields means the arm split is wrong."""
    arms = arms_of([("HOST-A", "1", "0.20.0"), ("HOST-B", "1", "0.19.0")])
    signals = [signal("tail.sock_tx_bytes", ["HOST-A/1", "HOST-B/1"], kind="field")]
    check = traffic_targets.evaluate_old_arm_purity(signals, arms)
    assert check.status == common.FAIL
    assert "tail.sock_tx_bytes" in check.detail


def test_old_arm_purity_passes_on_a_zero_and_says_so():
    arms = arms_of([("HOST-A", "1", "0.20.0"), ("HOST-B", "1", "0.19.0")])
    check = traffic_targets.evaluate_old_arm_purity(
        [signal("tail.sock_tx_bytes", ["HOST-A/1"], kind="field")], arms
    )
    assert check.status == common.PASS and check.passed_on_zero


def test_presence_fails_when_a_field_is_absent_from_the_whole_new_arm():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    signals = [
        signal("tail.sock_tx_bytes", ["HOST-A/1"], kind="field"),
        signal("evm rpc summary (cumulative)", [], kind="message"),
    ]
    check = traffic_targets.evaluate_presence(signals, arms)
    assert check.status == common.FAIL and "evm rpc summary" in check.detail


def test_influx_zero_while_es_has_the_lines_names_the_forwarder():
    arms = arms_of([("HOST-A", "1", "0.20.0")])
    check = traffic_targets.evaluate_influx({}, arms, es_has_lines=True)
    assert check.status == common.FAIL
    assert "tagpass" in check.detail and "forwarder" in check.detail


def test_influx_arrival_matches_hosts_case_insensitively():
    """ES upper-cases `tag.host`; Influx keeps it lower."""
    arms = arms_of([("HOST-A", "1", "0.20.0"), ("HOST-B", "1", "0.20.0")])
    per_target = {t: {"host-a", "host-b"} for t in traffic_targets.NEW_TARGETS}
    check = traffic_targets.evaluate_influx(per_target, arms, es_has_lines=True)
    assert check.status == common.PASS and "2/2" in check.detail


def test_influx_arrival_names_the_missing_hosts():
    arms = arms_of([("HOST-A", "1", "0.20.0"), ("HOST-B", "1", "0.20.0")])
    check = traffic_targets.evaluate_influx(
        {traffic_targets.NEW_TARGETS[0]: {"host-a"}}, arms, es_has_lines=True
    )
    assert check.status == common.FAIL and "host-b" in check.detail


def test_summarise_influx_groups_hosts_per_target():
    rows = [
        {"target": "ant_node::storage::traffic", "_value": "HOST-A"},
        {"target": "ant_node::storage::traffic", "_value": "host-b"},
        {"target": "ant_node::payment::traffic", "_value": "host-b"},
        {"target": "", "_value": "host-c"},
    ]
    assert traffic_targets.summarise_influx(rows) == {
        "ant_node::storage::traffic": {"host-a", "host-b"},
        "ant_node::payment::traffic": {"host-b"},
    }


def test_the_influx_query_pins_an_explicit_stop_and_the_three_targets():
    flux = traffic_targets.build_influx_query("testnet-metrics", NOW - timedelta(hours=1), NOW)
    assert "stop: 2026-09-24T12:00:00Z" in flux
    for target in traffic_targets.NEW_TARGETS:
        assert f'r.target == "{target}"' in flux


# -- 4. migration_signal --------------------------------------------------
def test_a_pair_in_two_adjacent_intervals_is_persistent():
    base = NOW - timedelta(hours=1)
    pages = [
        pairs_page([("HOST-A", "1", "peer-a", [base, base + migration_signal.REPORT_INTERVAL])])
    ]
    summaries = migration_signal.summarise_pairs(pages, arms_of([("HOST-A", "1", "0.20.0")]))
    assert len(summaries["new"].persistent) == 1
    assert summaries["new"].persistent[0][:2] == ("HOST-A/1", "peer-a")
    assert summaries["new"].peers == {"peer-a"}


def test_a_single_interval_appearance_is_not_persistent():
    """#163 removes the recurrence, not the value: one observation is legitimate."""
    base = NOW - timedelta(hours=1)
    pages = [pairs_page([("HOST-A", "1", "peer-a", [base])])]
    summaries = migration_signal.summarise_pairs(pages, arms_of([("HOST-A", "1", "0.20.0")]))
    assert summaries["new"].persistent == []
    assert summaries["new"].documents == 1


def test_non_adjacent_intervals_are_not_persistent():
    """A gap means the pair stopped being reported and came back later."""
    base = NOW - timedelta(hours=2)
    starts = [base, base + 3 * migration_signal.REPORT_INTERVAL]
    pages = [pairs_page([("HOST-A", "1", "peer-a", starts)])]
    summaries = migration_signal.summarise_pairs(pages, arms_of([("HOST-A", "1", "0.20.0")]))
    assert summaries["new"].persistent == []


def test_consecutive_runs_counts_the_longest_adjacent_streak():
    base = NOW - timedelta(hours=3)
    step = migration_signal.REPORT_INTERVAL
    series = [base, base + step, base + step * 2, base + step * 9, base + step * 10]
    assert migration_signal.consecutive_runs([int(t.timestamp() * 1000) for t in series]) == 3
    assert migration_signal.consecutive_runs([]) == 0


def test_persistence_on_the_new_arm_is_a_failure():
    base = NOW - timedelta(hours=1)
    arms = arms_of([("HOST-A", "1", "0.20.0")] + [(f"HOST-{i}", "1", "0.19.0") for i in range(9)])
    pages = [
        pairs_page([("HOST-A", "1", "peer-a", [base, base + migration_signal.REPORT_INTERVAL])])
    ]
    summaries = migration_signal.summarise_pairs(pages, arms)
    check = migration_signal.evaluate_persistence(summaries, arms)
    assert check.status == common.FAIL and "1 pair(s) on the new arm" in check.detail


def test_new_arm_zero_against_a_reproducing_old_arm_passes():
    base = NOW - timedelta(hours=1)
    arms = arms_of(
        [(f"NEW-{i}", "1", "0.20.0") for i in range(5)]
        + [(f"OLD-{i}", "1", "0.19.0") for i in range(5)]
    )
    pages = [
        pairs_page([("OLD-0", "1", "peer-a", [base, base + migration_signal.REPORT_INTERVAL])])
    ]
    summaries = migration_signal.summarise_pairs(pages, arms)
    check = migration_signal.evaluate_persistence(summaries, arms)
    assert check.status == common.PASS and "0 pair(s) on the new arm against 1" in check.detail


def test_zero_on_both_arms_is_untrusted_not_a_pass():
    """The old arm not reproducing the bug leaves the new arm's zero meaningless."""
    arms = arms_of(
        [(f"NEW-{i}", "1", "0.20.0") for i in range(5)]
        + [(f"OLD-{i}", "1", "0.19.0") for i in range(5)]
    )
    summaries = migration_signal.summarise_pairs([pairs_page([])], arms)
    check = migration_signal.evaluate_persistence(summaries, arms)
    assert check.status == common.UNTRUSTED and check.failed
    assert "did not reproduce" in check.detail


def test_a_tiny_old_arm_suspends_the_comparison():
    arms = arms_of([(f"NEW-{i}", "1", "0.20.0") for i in range(30)] + [("OLD-0", "1", "0.19.0")])
    assert arms.share("old") < common.SUSPEND_BELOW_SHARE
    summaries = migration_signal.summarise_pairs([pairs_page([])], arms)
    check = migration_signal.evaluate_persistence(summaries, arms)
    assert check.status == common.PASS and "suspended" in check.detail
    assert check.passed_on_zero


def test_no_new_services_is_not_applicable():
    arms = arms_of([("OLD-0", "1", "0.19.0")])
    summaries = migration_signal.summarise_pairs([pairs_page([])], arms)
    check = migration_signal.evaluate_persistence(summaries, arms)
    assert check.status == common.NOT_APPLICABLE
    assert "0 new-version services" in check.detail


def test_pairs_query_filters_on_peer_state_and_a_missing_agent():
    body = migration_signal.build_pairs_query(NOW - timedelta(hours=1), NOW)
    filters = body["query"]["bool"]["filter"]
    assert {"term": {migration_signal.MIGRATION_EVENT_FIELD: "peer_state"}} in filters
    assert {"term": {migration_signal.AGENT_FIELD: "none"}} in filters
    assert (
        body["aggs"]["services"]["aggs"]["intervals"]["date_histogram"]["fixed_interval"] == "15m"
    )
    assert body["size"] == 0


# -- 5. client_artefact_smoke ---------------------------------------------
# A release's real SHA256SUMS.txt, trimmed: several assets, the musl one marked
# binary with `*`, and one malformed line that must be ignored rather than read
# as a digest.
SUMS = "\n".join(
    [
        f"{'1' * 64}  ant-0.3.8-aarch64-apple-darwin.tar.gz",
        f"{'2' * 64} *ant-0.3.8-x86_64-unknown-linux-musl.tar.gz",
        "not-a-digest  ant-0.3.8-x86_64-pc-windows-msvc.zip",
        "",
    ]
)


def test_sums_file_parsing_picks_the_musl_asset():
    sums = artefact_smoke.expected_digest(SUMS, artefact_smoke.asset_name("0.3.8"))
    assert sums == "2" * 64


def test_an_asset_absent_from_the_sums_file_is_refused():
    with pytest.raises(ValueError, match="is not listed"):
        artefact_smoke.expected_digest(SUMS, artefact_smoke.asset_name("0.3.7"))


def test_release_urls_use_the_ant_cli_tag():
    archive, sums = artefact_smoke.release_urls("0.3.8")
    assert archive == (
        "https://github.com/WithAutonomi/ant-client/releases/download/ant-cli-v0.3.8/"
        "ant-0.3.8-x86_64-unknown-linux-musl.tar.gz"
    )
    assert sums.endswith("/ant-cli-v0.3.8/SHA256SUMS.txt")


def test_version_line_matching():
    assert artefact_smoke.require_version("ant 0.3.8\n", "0.3.8") == "ant 0.3.8"
    with pytest.raises(ValueError, match="expected ant 0.3.8, got ant 0.3.7"):
        artefact_smoke.require_version("ant 0.3.7\n", "0.3.8")
    # ant-node's output must not satisfy an `ant` check.
    with pytest.raises(ValueError, match="not `ant --version` output"):
        artefact_smoke.require_version("ant-node 0.20.0\n", "0.20.0")


def test_dist_tags_parsing():
    assert artefact_smoke.parse_dist_tags('{"latest": "0.3.8", "beta": "0.3.9-rc.1"}') == {
        "latest": "0.3.8",
        "beta": "0.3.9-rc.1",
    }
    with pytest.raises(ValueError, match="did not return JSON"):
        artefact_smoke.parse_dist_tags("npm ERR! code E404")


def test_dist_tags_parsing_accepts_npm_12_array_output():
    # npm 12.0.1, measured at the 0.3.8 publish: `[{"latest": "0.3.8"}]`.
    assert artefact_smoke.parse_dist_tags('[\n  {\n    "latest": "0.3.8"\n  }\n]') == {
        "latest": "0.3.8"
    }
    with pytest.raises(ValueError, match="not an object"):
        artefact_smoke.parse_dist_tags('[{"latest": "0.3.8"}, {"latest": "0.3.7"}]')


def test_byte_comparison_of_two_downloads(tmp_path):
    left, right, other, empty = (tmp_path / n for n in ("a", "b", "c", "d"))
    left.write_bytes(b"chunk-data")
    right.write_bytes(b"chunk-data")
    other.write_bytes(b"chunk-datA")
    empty.write_bytes(b"")
    assert artefact_smoke.files_identical(left, right)
    assert not artefact_smoke.files_identical(left, other)
    # Two empty files are equal bytes but prove nothing, so they do not pass.
    assert not artefact_smoke.files_identical(empty, empty)
    assert not artefact_smoke.files_identical(left, tmp_path / "missing")


def test_npm_update_refusal_recognised():
    assert artefact_smoke.is_npm_refusal("This binary was installed with npm; use npm to update.")
    assert not artefact_smoke.is_npm_refusal("Downloading update...")
    assert not artefact_smoke.is_npm_refusal("")


def test_first_address_skips_comments_and_blank_lines(tmp_path):
    path = tmp_path / "addresses.txt"
    path.write_text("\n# a comment\n  \nabc123\ndef456\n")
    assert artefact_smoke.first_address(path) == "abc123"
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    with pytest.raises(artefact_smoke.SmokeError):
        artefact_smoke.first_address(empty)


def test_the_shipped_address_list_has_a_usable_first_entry():
    address = artefact_smoke.first_address(artefact_smoke.DEFAULT_ADDRESSES_PATH)
    assert len(address) == 64 and all(c in "0123456789abcdef" for c in address)


# ==========================================================================
# The 2026-09-29 train's scripts (V2-1364 Phase B).
#
# Three decisions here are the reason these are scripts and not ad-hoc
# queries, and each one is a bug that was actually hit while writing them:
#
# * `tail.group` does NOT identify a summary. Group 4 on
#   `ant_node::replication::traffic` carries BOTH "audit outcome summary"
#   and "replication responder admission summary", emitted at the same
#   millisecond, and only the first has the `subtree_audit_*` fields. A
#   first/last delta that lands on the wrong one reads a service's whole
#   lifetime as one window's activity.
# * a counter reset is decided PER FIELD, never per service, so one field
#   going backwards cannot make the other seven fall back to their absolute
#   totals.
# * `subtree_audit_fail_digest_mismatch` is NOT zero-tolerance -- a
#   pointer-free fleet already runs a chunk-mismatch background -- while
#   `subtree_audit_fail_key_absent` is.
# ==========================================================================
audit_v2 = _load(ROLLOUT_DIR / "audit_protocol_v2.py", "rw_audit_protocol_v2")
pointer_signals = _load(ROLLOUT_DIR / "pointer_signals.py", "rw_pointer_signals")
sctp_errors = _load(
    ROLLOUT_DIR / "sctp_association_errors.py", "rw_sctp_association_errors"
)


def audit_page(entries, after=None):
    """`entries` are (host, service, first_values, last_values).

    A `None` values dict stands for a document that is NOT an audit summary --
    the admission-summary doc that shares its timestamp.
    """
    buckets = []
    for host, service, first, last in entries:
        def hit(values):
            if values is None:
                return {"_source": {}}
            return {"_source": {"tail": dict(values)}}

        buckets.append(
            {
                "key": {"host": host, "service": service},
                "first": {"hits": {"hits": [hit(first)]}},
                "last": {"hits": {"hits": [hit(last)]}},
            }
        )
    agg = {"buckets": buckets}
    if after:
        agg["after_key"] = after
    return {"aggregations": {"services": agg}}


def _arms(new=(), old=()):
    versions = {k: "0.21.0" for k in new}
    versions.update({k: "0.20.0" for k in old})
    arms = {k: ("new" if k in new else "old") for k in versions}
    return common.Arms(to_version="0.21.0", versions=versions, arms=arms)


def test_group_4_admission_summary_is_not_mistaken_for_an_audit_summary():
    """The bug that read a service's lifetime total as one window's delta."""
    arms = _arms(new=("h/1",))
    pages = [
        audit_page(
            [("h", "1", None, {"subtree_audit_pass": 5000, "subtree_audit_fail_timeout": 90})]
        )
    ]
    summaries = audit_v2.summarise(pages, arms)
    # The `first` document carried no audit fields, so the pair is unusable and
    # the service is skipped rather than contributing 5000 passes.
    assert summaries["new"].measured == 0
    assert summaries["new"].totals["subtree_audit_pass"] == 0


def test_delta_is_last_minus_first():
    arms = _arms(new=("h/1",))
    pages = [
        audit_page(
            [
                (
                    "h",
                    "1",
                    {"subtree_audit_pass": 100, "subtree_audit_fail_timeout": 10},
                    {"subtree_audit_pass": 140, "subtree_audit_fail_timeout": 14},
                )
            ]
        )
    ]
    summaries = audit_v2.summarise(pages, arms)
    assert summaries["new"].measured == 1
    assert summaries["new"].totals["subtree_audit_pass"] == 40
    assert summaries["new"].totals["subtree_audit_fail_timeout"] == 4
    assert summaries["new"].resets == 0


def test_a_counter_reset_is_decided_per_field_not_per_service():
    """One field going backwards must not inflate the other seven."""
    arms = _arms(new=("h/1",))
    pages = [
        audit_page(
            [
                (
                    "h",
                    "1",
                    # `pass` grows normally; `timeout` goes backwards.
                    {"subtree_audit_pass": 1000, "subtree_audit_fail_timeout": 50},
                    {"subtree_audit_pass": 1010, "subtree_audit_fail_timeout": 3},
                )
            ]
        )
    ]
    summaries = audit_v2.summarise(pages, arms)
    # `pass` keeps its true delta of 10 rather than falling back to 1010...
    assert summaries["new"].totals["subtree_audit_pass"] == 10
    # ...and only the field that actually reset uses its absolute value.
    assert summaries["new"].totals["subtree_audit_fail_timeout"] == 3
    assert summaries["new"].resets == 1


def test_key_absent_is_zero_tolerance():
    arms = _arms(new=("h/1",))
    summaries = audit_v2.summarise(
        [
            audit_page(
                [
                    (
                        "h",
                        "1",
                        {"subtree_audit_pass": 1},
                        {"subtree_audit_pass": 9, "subtree_audit_fail_key_absent": 2},
                    )
                ]
            )
        ],
        arms,
    )
    check = audit_v2.evaluate_key_absent(summaries, arms)
    assert check.status == common.FAIL
    assert "could not produce" in check.detail


def test_key_absent_zero_passes_but_rests_on_a_zero():
    arms = _arms(new=("h/1",))
    summaries = audit_v2.summarise(
        [audit_page([("h", "1", {"subtree_audit_pass": 1}, {"subtree_audit_pass": 9})])],
        arms,
    )
    check = audit_v2.evaluate_key_absent(summaries, arms)
    assert check.status == common.PASS
    assert check.passed_on_zero is True


def test_digest_mismatch_is_a_ratio_against_the_old_arm_not_zero_tolerance():
    """A pointer-free fleet already mismatches, so a non-zero new arm can pass."""
    new = tuple(f"n/{i}" for i in range(10))
    old = tuple(f"o/{i}" for i in range(10))
    arms = _arms(new=new, old=old)
    entries = []
    for key in new:
        host, service = key.split("/")
        entries.append(
            (
                host,
                service,
                {"subtree_audit_pass": 0},
                {"subtree_audit_pass": 10, "subtree_audit_fail_digest_mismatch": 1},
            )
        )
    for key in old:
        host, service = key.split("/")
        entries.append(
            (
                host,
                service,
                {"subtree_audit_pass": 0},
                {"subtree_audit_pass": 10, "subtree_audit_fail_digest_mismatch": 1},
            )
        )
    summaries = audit_v2.summarise([audit_page(entries)], arms)
    check = audit_v2.evaluate_digest_mismatch(summaries, arms, hours=1.0, baseline_rate=None)
    # Same rate on both arms -> ratio 1.0 -> pass, despite being non-zero.
    assert check.status == common.PASS
    assert check.data["ratio"] == pytest.approx(1.0)


def test_digest_mismatch_fails_above_the_ratio_limit():
    new = tuple(f"n/{i}" for i in range(10))
    old = tuple(f"o/{i}" for i in range(10))
    arms = _arms(new=new, old=old)
    entries = []
    for key in new:
        host, service = key.split("/")
        entries.append(
            (
                host,
                service,
                {"subtree_audit_pass": 0},
                {"subtree_audit_pass": 10, "subtree_audit_fail_digest_mismatch": 5},
            )
        )
    for key in old:
        host, service = key.split("/")
        entries.append(
            (
                host,
                service,
                {"subtree_audit_pass": 0},
                {"subtree_audit_pass": 10, "subtree_audit_fail_digest_mismatch": 1},
            )
        )
    summaries = audit_v2.summarise([audit_page(entries)], arms)
    check = audit_v2.evaluate_digest_mismatch(summaries, arms, hours=1.0, baseline_rate=None)
    assert check.status == common.FAIL
    assert check.data["ratio"] == pytest.approx(5.0)


def test_digest_mismatch_falls_back_to_the_baseline_once_the_old_arm_drains():
    new = tuple(f"n/{i}" for i in range(20))
    arms = _arms(new=new)
    entries = [
        (
            key.split("/")[0],
            key.split("/")[1],
            {"subtree_audit_pass": 0},
            {"subtree_audit_pass": 10, "subtree_audit_fail_digest_mismatch": 1},
        )
        for key in new
    ]
    summaries = audit_v2.summarise([audit_page(entries)], arms)
    # 1 mismatch per service per hour against a 0.5 baseline is 2x -> FAIL.
    check = audit_v2.evaluate_digest_mismatch(summaries, arms, hours=1.0, baseline_rate=0.5)
    assert check.status == common.FAIL
    # Without a baseline there is nothing to compare against, and that is not a pass.
    check = audit_v2.evaluate_digest_mismatch(summaries, arms, hours=1.0, baseline_rate=None)
    assert check.status == common.NOT_APPLICABLE


def test_audit_recovery_claims_nothing_while_the_fleet_is_mixed():
    new = tuple(f"n/{i}" for i in range(10))
    old = tuple(f"o/{i}" for i in range(10))
    arms = _arms(new=new, old=old)
    entries = [
        (k.split("/")[0], k.split("/")[1], {"subtree_audit_pass": 5}, {"subtree_audit_pass": 5})
        for k in new + old
    ]
    summaries = audit_v2.summarise([audit_page(entries)], arms)
    check = audit_v2.evaluate_recovery(summaries, arms)
    # Zero passes on the new arm is EXPECTED mid-rollout: the two protocol ids
    # cannot audit each other. It must not be reported as a failure.
    assert check.status == common.NOT_APPLICABLE


def test_audit_recovery_fails_once_uniform_and_still_silent():
    new = tuple(f"n/{i}" for i in range(20))
    arms = _arms(new=new)
    entries = [
        (k.split("/")[0], k.split("/")[1], {"subtree_audit_pass": 5}, {"subtree_audit_pass": 5})
        for k in new
    ]
    summaries = audit_v2.summarise([audit_page(entries)], arms)
    check = audit_v2.evaluate_recovery(summaries, arms)
    assert check.status == common.FAIL
    assert "should have resumed" in check.detail


# -- SCTP ------------------------------------------------------------------
def test_sctp_rate_is_per_1k_service_hours():
    rate = sctp_errors.ArmRate(arm="new", services=100, errors=50, hosts=7, hours=2.0)
    # 50 errors / (100 services * 2h) * 1000 = 250
    assert rate.per_1k_service_hours == pytest.approx(250.0)


def test_sctp_ratio_suspended_until_both_arms_are_large_enough():
    new = tuple(f"n/{i}" for i in range(1))
    old = tuple(f"o/{i}" for i in range(99))
    arms = _arms(new=new, old=old)
    rates = {
        "new": sctp_errors.ArmRate("new", 1, 100, 1, 1.0),
        "old": sctp_errors.ArmRate("old", 99, 99, 50, 1.0),
    }
    check = sctp_errors.evaluate(rates, arms, baseline_rate=None)
    assert check.status == common.NOT_APPLICABLE


def test_sctp_ratio_compares_arms_when_both_are_large_enough():
    new = tuple(f"n/{i}" for i in range(50))
    old = tuple(f"o/{i}" for i in range(50))
    arms = _arms(new=new, old=old)
    rates = {
        "new": sctp_errors.ArmRate("new", 50, 200, 30, 1.0),
        "old": sctp_errors.ArmRate("old", 50, 100, 30, 1.0),
    }
    check = sctp_errors.evaluate(rates, arms, baseline_rate=None)
    assert check.status == common.FAIL  # 2.0x against a 1.5x limit
    assert check.data["ratio"] == pytest.approx(2.0)

    rates["new"] = sctp_errors.ArmRate("new", 50, 140, 30, 1.0)
    check = sctp_errors.evaluate(rates, arms, baseline_rate=None)
    assert check.status == common.PASS  # 1.4x


# -- listener delay (V2-1331) ---------------------------------------------
def test_listen_delay_excludes_a_listening_line_older_than_the_start():
    """A previous run's listening line would otherwise be a negative delay."""
    base = NOW
    starts = {"h/1": base, "h/2": base}
    listens = {
        "h/1": base + timedelta(minutes=9),
        "h/2": base - timedelta(minutes=30),  # from the run before this one
    }
    out = webrtc_coverage.summarise_listen_delays(["h/1", "h/2"], starts, listens)
    assert out["measured"] == 1
    assert out["stale_pairs"] == 1
    assert out["median_secs"] == pytest.approx(540.0)


def test_listen_delay_reports_services_that_never_logged_a_listener():
    base = NOW
    starts = {"h/1": base, "h/2": base}
    listens = {"h/1": base + timedelta(minutes=2)}
    out = webrtc_coverage.summarise_listen_delays(["h/1", "h/2"], starts, listens)
    assert out["no_listen_line"] == ["h/2"]
    assert out["measured"] == 1


def test_listen_delay_percentiles_over_a_known_spread():
    base = NOW
    keys = [f"h/{i}" for i in range(11)]
    starts = {k: base for k in keys}
    # 0,60,120,...,600 seconds
    listens = {k: base + timedelta(seconds=60 * i) for i, k in enumerate(keys)}
    out = webrtc_coverage.summarise_listen_delays(keys, starts, listens)
    assert out["min_secs"] == pytest.approx(0.0)
    assert out["median_secs"] == pytest.approx(300.0)
    assert out["p90_secs"] == pytest.approx(540.0)
    assert out["max_secs"] == pytest.approx(600.0)


def test_listen_delay_is_empty_without_pairs_rather_than_raising():
    out = webrtc_coverage.summarise_listen_delays(["h/1"], {}, {})
    assert out["measured"] == 0
    assert out["median_secs"] is None
    assert "no delay could be measured" in webrtc_coverage.render_listen_delays(out)


# -- pointer signals -------------------------------------------------------
def test_pointer_rpc_errors_are_not_applicable_when_no_pointer_rpc_happened():
    """Production has no pointer client, so an empty result must not read as a pass."""
    check = pointer_signals.evaluate_rpc_errors({"total": 0, "events": {}})
    assert check.status == common.NOT_APPLICABLE


def test_pointer_rpc_errors_fail_on_an_error_outcome():
    rpc = {
        "total": 10,
        "events": {"pointer_put_rpc": {"success": 9, "error": 1}},
    }
    check = pointer_signals.evaluate_rpc_errors(rpc)
    assert check.status == common.FAIL


def test_pointer_rpc_not_found_is_an_answer_not_an_error():
    rpc = {"total": 5, "events": {"pointer_get_rpc": {"not_found": 5}}}
    check = pointer_signals.evaluate_rpc_errors(rpc)
    assert check.status == common.PASS
