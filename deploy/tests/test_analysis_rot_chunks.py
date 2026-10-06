"""`analysis rot-chunks` (V2-1442).

The remote bash payloads are proven on a live VM, not here. Under test is
everything the driver decides: the deterministic sample, the manifest
round-trip, the disk classification of every row of both verify tables, the
Elasticsearch attribution join (including the upper-case host match and the
non-target control), and the root-dir resolution the whole thing hangs off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.analysis import rot_chunks
from saorsa_deploy.analysis.remote import RemoteResult

HOST_A_IP = "10.0.0.1"
HOST_B_IP = "10.0.0.2"
HOST_A = "DEV-04-ant-node-nat-1"
HOST_B = "DEV-04-ant-node-plain-1"


def key(i: int) -> str:
    return f"{i:064x}"


@pytest.fixture()
def inventory(tmp_path):
    inv = {
        "schema_version": 7,
        "node_vms": [
            {
                "name": HOST_A,
                "provider": "ovh",
                "provider_id": "x",
                "region": "SBG5",
                "ip": HOST_A_IP,
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": True,
                "services": ["ant-node-1", "ant-node-2"],
            },
            {
                "name": HOST_B,
                "provider": "vultr",
                "provider_id": "y",
                "region": "ord",
                "ip": HOST_B_IP,
                "ssh_user": "root",
                "ssh_key": "~/.ssh/ant_deploy",
                "nat_simulated": False,
                "services": ["ant-node-1"],
            },
        ],
        "bootstrap_vms": [],
        "client_vms": [],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    return str(path)


def _args(**kw):
    base = {
        "name": "DEV-04",
        "inventory": None,
        "ips": None,
        "tier": "all",
        "apply": False,
        "verify": False,
        "attribute": False,
        "tag": "p1",
        "services": None,
        "fraction": 0.5,
        "min_files": 100,
        "start": None,
        "end": None,
        "out": None,
        "json_out": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _exec_start(service: str) -> str:
    return (
        "{ path=/opt/ant-node/ant-node ; argv[]=/opt/ant-node/ant-node --root-dir "
        f"/mnt/ant-node-storage/data/{service} --port 10000 --rewards-address 0x1 ; "
        "ignore_errors=no }"
    )


def _probe_out(hostname: str, services: list[str], with_exec: bool = True) -> str:
    lines = [f"HOST\t{hostname}"]
    for svc in services:
        lines.append(f"EXEC\t{svc}\t{_exec_start(svc) if with_exec else ''}")
    return "\n".join(lines) + "\nSTATUS=done\n"


def _list_out(per_service: dict[str, list[str]]) -> str:
    lines = []
    for svc, keys in per_service.items():
        for i, k in enumerate(keys):
            lines.append(f"FILE\t{svc}\t{k}\t{1000 + i}\tsha-{k[:8]}")
        lines.append(f"SVCEND\t{svc}")
    return "\n".join(lines) + "\nSTATUS=done\n"


def _rot_out(service: str, keys: list[str], failures: dict[str, str] | None = None) -> str:
    failures = failures or {}
    lines = [f"ROT\t{service}\t{k}\t{failures.get(k, 'ok')}" for k in keys]
    return "\n".join(lines) + "\nSTATUS=done\n"


def _fake_remote(listings: dict[str, dict[str, list[str]]], rot_failures=None, probe_exec=True):
    """A run_script stand-in that answers the probe, list and rot payloads."""
    calls = {"probe": [], "list": [], "rot": []}

    def run(vm, script, args=(), timeout=None, stdin_extra=""):
        args = list(args)
        host = HOST_A if vm.ip == HOST_A_IP else HOST_B
        if script == rot_chunks.PROBE_SCRIPT:
            calls["probe"].append((vm.ip, tuple(args)))
            return RemoteResult(vm, 0, _probe_out(host, args, with_exec=probe_exec), "")
        if script == rot_chunks.LIST_SCRIPT:
            calls["list"].append((vm.ip, tuple(args)))
            services = args[0::2]
            return RemoteResult(
                vm,
                0,
                _list_out({s: listings[host].get(s, []) for s in services}),
                "",
            )
        if script == rot_chunks.ROT_SCRIPT:
            _tag, service, _root, _backup, *keys = args
            calls["rot"].append((vm.ip, service, tuple(keys)))
            return RemoteResult(
                vm, 0, _rot_out(service, keys, (rot_failures or {}).get(service)), ""
            )
        raise AssertionError("unexpected script")

    return run, calls


# --- 1. sampling -----------------------------------------------------------


def test_sample_is_deterministic_per_tag_and_host_and_sized_by_fraction():
    keys = [key(i) for i in range(200)]
    a1 = rot_chunks.sample_keys("p1", "HOST-A", "ant-node-1", keys, 0.5)
    a2 = rot_chunks.sample_keys("p1", "HOST-A", "ant-node-1", list(reversed(keys)), 0.5)
    assert a1 == a2 and len(a1) == 100
    assert a1 != rot_chunks.sample_keys("p2", "HOST-A", "ant-node-1", keys, 0.5)
    assert a1 != rot_chunks.sample_keys("p1", "HOST-B", "ant-node-1", keys, 0.5)
    assert a1 != rot_chunks.sample_keys("p1", "HOST-A", "ant-node-2", keys, 0.5)
    assert set(a1) <= set(keys)


def test_sample_count_is_round_of_n_times_fraction():
    keys = [key(i) for i in range(101)]
    assert len(rot_chunks.sample_keys("p1", "h", "s", keys, 0.25)) == round(101 * 0.25)
    assert len(rot_chunks.sample_keys("p1", "h", "s", keys, 1.0)) == 101
    assert rot_chunks.sample_keys("p1", "h", "s", keys, 0.0) == []


def test_service_below_min_files_is_skipped_and_never_rotted(inventory, tmp_path):
    listings = {
        HOST_A: {"ant-node-1": [key(i) for i in range(37)], "ant-node-2": []},
        HOST_B: {"ant-node-1": [key(i) for i in range(500, 700)]},
    }
    run, calls = _fake_remote(listings)
    out = tmp_path / "m.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, ips=[HOST_A_IP], apply=True, out=str(out))
        )
    assert rc == 0
    manifest = json.loads(out.read_text())
    services = {s["service"]: s for s in manifest["hosts"][0]["services"]}
    assert services["ant-node-1"]["skipped"] == "too_few_files:37"
    assert services["ant-node-1"]["rotted"] == [] and services["ant-node-1"]["intact"] == []
    assert services["ant-node-2"]["skipped"] == "too_few_files:0"
    assert calls["rot"] == []


# --- 2. apply manifest round-trip -----------------------------------------


def test_apply_writes_manifest_with_disjoint_rotted_and_intact_sets(inventory, tmp_path):
    a_keys = [key(i) for i in range(200)]
    b_keys = [key(i) for i in range(1000, 1300)]
    listings = {
        HOST_A: {"ant-node-1": a_keys, "ant-node-2": a_keys},
        HOST_B: {"ant-node-1": b_keys},
    }
    run, calls = _fake_remote(listings)
    out = tmp_path / "m.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(
                inventory=inventory,
                ips=[HOST_A_IP, HOST_B_IP],
                apply=True,
                services=["ant-node-1"],
                out=str(out),
            )
        )
    assert rc == 0
    manifest = json.loads(out.read_text())
    assert manifest["name"] == "DEV-04" and manifest["tag"] == "p1"
    assert manifest["applied_at"].endswith("Z") and manifest["finished_at"].endswith("Z")
    assert len(manifest["applied_at"]) == len("2026-10-06T00:00:00Z")
    assert manifest["fraction"] == 0.5 and manifest["min_files"] == 100
    assert [h["ip"] for h in manifest["hosts"]] == [HOST_A_IP, HOST_B_IP]
    host_a = manifest["hosts"][0]
    assert host_a["hostname"] == HOST_A and host_a["nat_simulated"] is True
    assert manifest["hosts"][1]["nat_simulated"] is False
    # --services narrowed the pass to one of the VM's two services
    assert [s["service"] for s in host_a["services"]] == ["ant-node-1"]
    svc = host_a["services"][0]
    assert svc["root"] == "/mnt/ant-node-storage/data/ant-node-1"
    assert svc["total_files"] == 200 and svc["skipped"] is None
    rotted, intact = set(svc["rotted"]), {f["key"] for f in svc["intact"]}
    assert len(rotted) == 100 and rotted.isdisjoint(intact)
    assert rotted | intact == set(a_keys)
    assert all(set(f) == {"key", "size", "sha256"} for f in svc["intact"])
    # the rot script saw exactly the sampled keys, for that service only
    assert [c[1] for c in calls["rot"]] == ["ant-node-1", "ant-node-1"]
    assert set(calls["rot"][0][2]) == rotted


def test_apply_refuses_a_tag_that_already_has_a_manifest(inventory, tmp_path):
    out = tmp_path / "m.json"
    out.write_text("{}")
    run, calls = _fake_remote({})
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, ips=[HOST_A_IP], apply=True, out=str(out))
        )
    assert rc == 2
    assert calls["probe"] == []
    assert out.read_text() == "{}"


def test_apply_reports_a_rot_error_but_still_writes_the_manifest(inventory, tmp_path):
    keys = [key(i) for i in range(200)]
    chosen = rot_chunks.sample_keys("p1", HOST_B, "ant-node-1", keys, 0.5)
    run, _ = _fake_remote(
        {HOST_B: {"ant-node-1": keys}},
        rot_failures={"ant-node-1": {chosen[0]: "error:write_failed"}},
    )
    out = tmp_path / "m.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, ips=[HOST_B_IP], apply=True, out=str(out))
        )
    assert rc == 1
    svc = json.loads(out.read_text())["hosts"][0]["services"][0]
    assert len(svc["rotted"]) == 99 and chosen[0] not in svc["rotted"]
    assert svc["skipped"].startswith("error:")
    # the key that failed to rot stays in the control set -- it is untouched
    assert chosen[0] in {f["key"] for f in svc["intact"]}


def test_apply_without_ips_is_refused(inventory, tmp_path):
    rc = rot_chunks.cmd_rot_chunks(
        _args(inventory=inventory, apply=True, out=str(tmp_path / "m.json"))
    )
    assert rc == 2
    assert not (tmp_path / "m.json").exists()


def test_apply_records_an_unreachable_host_and_exits_one(inventory, tmp_path):
    def run(vm, script, args=(), timeout=None, stdin_extra=""):
        return RemoteResult(vm, 255, "", "ssh: connect to host: Connection refused")

    out = tmp_path / "m.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, ips=[HOST_B_IP], apply=True, out=str(out))
        )
    assert rc == 1
    host = json.loads(out.read_text())["hosts"][0]
    assert host["hostname"] is None and "Connection refused" in host["error"]
    assert host["services"][0]["skipped"].startswith("error:")


# --- 3. verify classification ---------------------------------------------


def _manifest(tmp_path, rotted, intact, hostname=HOST_B, ip=HOST_B_IP):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "name": "DEV-04",
                "tag": "p1",
                "applied_at": "2026-10-06T10:00:00Z",
                "finished_at": "2026-10-06T10:01:00Z",
                "fraction": 0.5,
                "min_files": 100,
                "hosts": [
                    {
                        "ip": ip,
                        "hostname": hostname,
                        "nat_simulated": False,
                        "services": [
                            {
                                "service": "ant-node-1",
                                "root": "/mnt/ant-node-storage/data/ant-node-1",
                                "total_files": len(rotted) + len(intact),
                                "rotted": rotted,
                                "intact": intact,
                                "skipped": None,
                            }
                        ],
                    }
                ],
            }
        )
    )
    return path


def _v(service, kind, k, exists, rotted=False, matches=False, size=0, sha="-"):
    return (
        f"V\t{service}\t{kind}\t{k}\t{str(exists).lower()}\t{str(rotted).lower()}\t"
        f"{str(matches).lower()}\t{size}\t{sha}"
    )


def test_verify_classifies_every_row_of_both_tables(inventory, tmp_path, capsys):
    rotted = [key(i) for i in range(4)]
    intact = [
        {"key": key(10), "size": 100, "sha256": "aaa"},
        {"key": key(11), "size": 100, "sha256": "bbb"},
        {"key": key(12), "size": 100, "sha256": "ccc"},
    ]
    path = _manifest(tmp_path, rotted, intact)
    lines = [
        _v("ant-node-1", "R", rotted[0], False),  # removed
        _v("ant-node-1", "R", rotted[1], True, rotted=True, size=6),  # still rotted
        _v("ant-node-1", "R", rotted[2], True, matches=True, size=100),  # repaired
        _v("ant-node-1", "R", rotted[3], True, size=77),  # other
        _v("ant-node-1", "I", intact[0]["key"], True, size=100, sha="aaa"),  # unchanged
        _v("ant-node-1", "I", intact[1]["key"], True, size=101, sha="bbb"),  # changed (size)
        _v("ant-node-1", "I", intact[2]["key"], False),  # missing
    ]

    def run(vm, script, args=(), timeout=None, stdin_extra=""):
        assert script == rot_chunks.VERIFY_SCRIPT
        specs = args[4:]
        assert [s[:2] for s in specs].count("R:") == 4
        assert [s[:2] for s in specs].count("I:") == 3
        return RemoteResult(vm, 0, "\n".join(lines) + "\nSTATUS=done\n", "")

    report = tmp_path / "verify.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, verify=True, out=str(path), json_out=str(report))
        )
    assert rc == 0
    payload = json.loads(report.read_text())
    classes = {r["key"]: r["class"] for r in payload["keys"]}
    assert classes[rotted[0]] == "removed"
    assert classes[rotted[1]] == "rotted"
    assert classes[rotted[2]] == "repaired"
    assert classes[rotted[3]] == "other"
    assert classes[intact[0]["key"]] == "unchanged"
    assert classes[intact[1]["key"]] == "changed"
    assert classes[intact[2]["key"]] == "missing"
    assert {r["kind"] for r in payload["keys"]} == {"rotted", "intact"}
    assert all(r["host"] == HOST_B and r["service"] == "ant-node-1" for r in payload["keys"])
    assert payload["totals"] == {
        "rotted_keys": 4,
        "repaired": 1,
        "removed": 1,
        "still_rotted": 1,
        "other": 1,
        "intact_keys": 3,
        "unchanged": 1,
        "changed": 1,
        "missing": 1,
    }
    assert payload["verified_at"].endswith("Z")
    out = capsys.readouterr().out
    assert "ROT_VERIFY tag=p1" in out
    assert "repaired=1 removed=1 still_rotted=1 other=1" in out
    assert "intact=3 unchanged=1 changed=1 missing=1" in out


def test_verify_sha_change_alone_is_changed():
    expected = {"key": key(1), "size": 100, "sha256": "aaa"}
    rec = {"exists": True, "size": 100, "sha256": "zzz"}
    assert rot_chunks.classify_intact(rec, expected) == "changed"
    assert rot_chunks.classify_intact({**rec, "sha256": "aaa"}, expected) == "unchanged"


def test_verify_unreachable_host_exits_one_but_still_reports(inventory, tmp_path):
    path = _manifest(tmp_path, [key(1)], [])

    def run(vm, script, args=(), timeout=None, stdin_extra=""):
        return RemoteResult(vm, 255, "", "ssh: connect to host: Connection refused")

    report = tmp_path / "verify.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, verify=True, out=str(path), json_out=str(report))
        )
    assert rc == 1
    # an unanswered key is "removed" on disk, but the host error is what is loud
    assert json.loads(report.read_text())["totals"]["removed"] == 1


def test_verify_missing_manifest_exits_one(inventory, tmp_path):
    rc = rot_chunks.cmd_rot_chunks(
        _args(inventory=inventory, verify=True, out=str(tmp_path / "nope.json"))
    )
    assert rc == 1


# --- 4. attribute ----------------------------------------------------------


def _doc(host, ts, message):
    return {"@timestamp": ts, "tag": {"host": host}, "tail": {"message": message}}


def test_attribute_joins_detections_and_removals_per_key(inventory, tmp_path, capsys):
    k_audit, k_fetch, k_detected, k_none = (key(i) for i in range(1, 5))
    path = _manifest(tmp_path, [k_audit, k_fetch, k_detected, k_none], [])
    docs = {
        "detection": [
            # ES stores tag.host upper-cased; the manifest holds the inventory spelling
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:00:30Z",
                f"committed key {k_audit} does not hash to its address; reporting it for a recheck",
            ),
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:05:00Z",
                f"committed key {k_detected} does not hash to its address; reporting it",
            ),
            # a later duplicate detection must not displace the first
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:20:00Z",
                f"committed key {k_audit} does not hash to its address",
            ),
        ],
        "removal": [
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:02:30Z",
                f"Removed corrupt chunk file {k_audit}; replication will repair it",
            ),
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:03:00Z",
                f"Removed corrupt chunk file {k_fetch}; replication will repair it",
            ),
            # the non-target control: a removal on a host the manifest never touched
            _doc(
                "DEV-04-ANT-NODE-PLAIN-9",
                "2026-10-06T10:04:00Z",
                f"Removed corrupt chunk file {key(99)}",
            ),
            _doc(
                "DEV-04-ANT-NODE-PLAIN-9",
                "2026-10-06T10:06:00Z",
                f"Removed corrupt chunk file {key(98)}",
            ),
        ],
        "not_removable": [
            _doc(
                HOST_B.upper(),
                "2026-10-06T10:07:00Z",
                f"Corrupt chunk {key(55)} could not be removed: EACCES",
            )
        ],
        "overflow": [
            _doc(HOST_B.upper(), "2026-10-06T10:08:00Z", "12 keys were not queued for a recheck")
        ],
        "stale_commitment": [],
    }
    seen = []

    def fake_hits(self, filters, start, end, index=None, fields=None, page_size=1000):
        phrase = filters[0]["match_phrase"]["tail.message"]
        seen.append((phrase, start, end, index, tuple(fields or ())))
        kind = next(k for k, v in rot_chunks.PHRASES.items() if v == phrase)
        return docs[kind]

    report = tmp_path / "attr.json"
    with patch("saorsa_deploy.analysis.es.EsRun.hits", new=fake_hits):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, attribute=True, out=str(path), json_out=str(report))
        )
    assert rc == 0
    payload = json.loads(report.read_text())
    assert payload["window"]["start"] == "2026-10-06T10:00:00Z"  # defaulted to applied_at
    assert all(s[1] == "2026-10-06T10:00:00Z" for s in seen) and len(seen) == 5
    assert all("tail.message" in s[4] and "tag.host" in s[4] for s in seen)

    rows = {r["key"]: r for r in payload["keys"]}
    assert rows[k_audit]["attribution"] == "audit"
    assert rows[k_audit]["detected_at"] == "2026-10-06T10:00:30Z"
    assert rows[k_audit]["removed_at"] == "2026-10-06T10:02:30Z"
    assert rows[k_fetch]["attribution"] == "fetch"
    assert rows[k_fetch]["detected_at"] is None
    assert rows[k_detected]["attribution"] == "detected_only"
    assert rows[k_detected]["removed_at"] is None
    assert rows[k_none]["attribution"] == "none"
    assert all(r["host"] == HOST_B for r in payload["keys"])
    assert not any(k.startswith("_") for r in payload["keys"] for k in r)

    assert payload["totals"]["detected"] == 2 and payload["totals"]["removed"] == 2
    assert payload["totals"]["first_detection_latency_s"] == 30.0
    assert payload["totals"]["median_detect_to_remove_s"] == 120.0
    assert payload["nontarget_removals"] == {
        "count": 2,
        "hosts": {"DEV-04-ANT-NODE-PLAIN-9": 2},
    }
    target = payload["per_target"][0]
    assert target["host"] == HOST_B
    assert target["detection"] == 3 and target["removal"] == 2
    assert target["not_removable"] == 1 and target["overflow"] == 1

    out = capsys.readouterr().out
    assert "ROT_ATTRIBUTE tag=p1 window=2026-10-06T10:00:00Z.." in out
    assert "rotted_keys=4 detected=2 removed=2 audit=1 fetch=1" in out
    assert "detected_only=1 none=1 targets_detected=1/1" in out
    assert "overflow_lines=1 not_removable_lines=1 nontarget_removals=2" in out


def test_attribute_respects_an_explicit_window(inventory, tmp_path):
    path = _manifest(tmp_path, [key(1)], [])
    seen = []

    def fake_hits(self, filters, start, end, index=None, fields=None, page_size=1000):
        seen.append((start, end))
        return []

    with patch("saorsa_deploy.analysis.es.EsRun.hits", new=fake_hits):
        rc = rot_chunks.cmd_rot_chunks(
            _args(
                inventory=inventory,
                attribute=True,
                out=str(path),
                json_out=str(tmp_path / "a.json"),
                start="2026-10-06T11:00:00Z",
                end="2026-10-06T12:00:00Z",
            )
        )
    assert rc == 0
    assert set(seen) == {("2026-10-06T11:00:00Z", "2026-10-06T12:00:00Z")}


def test_attribution_ordering_rules():
    from datetime import datetime, timezone

    t0 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 6, 10, 1, tzinfo=timezone.utc)
    assert rot_chunks.attribution_for(t0, t1) == "audit"
    assert rot_chunks.attribution_for(t0, t0) == "audit"  # equal counts as audit
    assert rot_chunks.attribution_for(t1, t0) == "fetch"  # detection came after
    assert rot_chunks.attribution_for(None, t0) == "fetch"
    assert rot_chunks.attribution_for(t0, None) == "detected_only"
    assert rot_chunks.attribution_for(None, None) == "none"


def test_attribute_missing_manifest_exits_one(inventory, tmp_path):
    rc = rot_chunks.cmd_rot_chunks(
        _args(inventory=inventory, attribute=True, out=str(tmp_path / "nope.json"))
    )
    assert rc == 1


def test_nontarget_removals_match_hosts_case_insensitively():
    per_host = {"DEV-04-ANT-NODE-NAT-1": 3, "DEV-04-ANT-NODE-PLAIN-9": 2}
    control = rot_chunks.nontarget_removals(per_host, ["dev-04-ant-node-nat-1"])
    assert control == {"count": 2, "hosts": {"DEV-04-ANT-NODE-PLAIN-9": 2}}


# --- 5. root-dir resolution ------------------------------------------------


def test_root_dir_parsed_from_exec_start():
    assert (
        rot_chunks.root_dir_for(_exec_start("ant-node-7"), "ant-node-7")
        == "/mnt/ant-node-storage/data/ant-node-7"
    )
    assert (
        rot_chunks.root_dir_for("--root-dir=/srv/other/ant-node-7 --port 10000", "ant-node-7")
        == "/srv/other/ant-node-7"
    )


def test_root_dir_falls_back_when_exec_start_has_none():
    assert rot_chunks.root_dir_for("", "ant-node-3") == "/mnt/ant-node-storage/data/ant-node-3"
    assert rot_chunks.root_dir_for(None, "ant-node-3") == "/mnt/ant-node-storage/data/ant-node-3"
    assert (
        rot_chunks.root_dir_for("/opt/ant-node/ant-node --port 10000", "ant-node-3")
        == "/mnt/ant-node-storage/data/ant-node-3"
    )


def test_apply_uses_the_fallback_root_when_the_unit_has_none(inventory, tmp_path):
    keys = [key(i) for i in range(120)]
    run, calls = _fake_remote({HOST_B: {"ant-node-1": keys}}, probe_exec=False)
    out = tmp_path / "m.json"
    with patch("saorsa_deploy.analysis.remote.run_script", side_effect=run):
        rc = rot_chunks.cmd_rot_chunks(
            _args(inventory=inventory, ips=[HOST_B_IP], apply=True, out=str(out))
        )
    assert rc == 0
    svc = json.loads(out.read_text())["hosts"][0]["services"][0]
    assert svc["root"] == "/mnt/ant-node-storage/data/ant-node-1"
    assert calls["list"][0][1] == ("ant-node-1", "/mnt/ant-node-storage/data/ant-node-1")


def test_listing_ignores_non_key_filenames():
    res = RemoteResult(
        None,
        0,
        "\n".join(
            [
                f"FILE\tant-node-1\t{key(1)}\t10\tsha1",
                "FILE\tant-node-1\tlayout.json\t10\tsha2",
                "FILE\tant-node-1\t.tmp.abc\t10\tsha3",
                "SVCEND\tant-node-1",
            ]
        )
        + "\nSTATUS=done\n",
        "",
    )
    parsed = rot_chunks.parse_listing(res)
    assert [f["key"] for f in parsed["ant-node-1"]["files"]] == [key(1)]


def test_rot_script_never_deletes_or_renames_and_writes_in_place():
    # The property the whole measurement rests on: ant-node takes a different
    # path for a *missing* file than for one that no longer hashes to its key.
    assert " rm " not in rot_chunks.ROT_SCRIPT and "mv " not in rot_chunks.ROT_SCRIPT
    assert 'printf \'%s\' "rotted" > "$F"' in rot_chunks.ROT_SCRIPT
    assert 'cp -p "$F" "$BK/$KEY"' in rot_chunks.ROT_SCRIPT
