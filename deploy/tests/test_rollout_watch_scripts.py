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
