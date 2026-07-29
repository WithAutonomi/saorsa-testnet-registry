"""Golden-parity tests for the testnet orchestrator.

The deploy sequence MUST match the hand-written reference runbook
``runbooks/staging_rc/up/04-STG_01.sh`` command-for-command. This test locks the
ordering and flag-coupling invariants so a refactor can't silently drift.
"""

import pytest

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.orchestrator.steps import StepGroup, iter_units

SSH = "/ssh/key"
PURPOSE = "RC sanity check"

STG_01 = {
    "name": "STG-01",
    "kind": "testnet",
    "purpose": PURPOSE,
    "ssh_key_path": SSH,
    "testnet": {
        "mode": True,
        "binaries": {
            "ant_node": {"version": "0.11.5-rc.4"},
            "ant": {"version": "0.2.5-rc.4"},
        },
        "bootstraps": {"count": 7, "port": 10000},
        "nodes": {
            "per_vm": 15,
            "nat_percent": 30,
            "providers": {
                "digital_ocean": {
                    "vm_count": 5,
                    "regions_list": ["ams3", "blr1", "fra1", "lon1", "nyc1", "syd1", "sgp1"],
                    "size": "s-8vcpu-16gb",
                    "attached_volume_size": 100,
                },
                "vultr": {
                    "vm_count": 5,
                    "regions_list": ["ams", "ewr", "lax", "lhr", "nrt"],
                    "size": "vhp-8c-16gb-intel",
                    "attached_volume_size": 100,
                },
            },
        },
        "clients": {
            "uploaders": [
                {
                    "provider": "vultr",
                    "region": "syd",
                    "file_size_mb": 1000,
                    "vm_size": "vc2-2c-4gb",
                    "payment": "merkle",
                    "ant": "1000",
                    "eth": "0.1",
                },
                {
                    "provider": "ovh",
                    "region": "UK1",
                    "file_size_mb": 1000,
                    "vm_size": "b3-8",
                    "payment": "no-merkle",
                    "ant": "1000",
                    "eth": "0.1",
                },
                {
                    "provider": "vultr",
                    "region": "ewr",
                    "file_size_mb": 500,
                    "vm_size": "vc2-2c-4gb",
                    "payment": "no-merkle",
                    "ant": "1000",
                    "eth": "0.1",
                },
                {
                    "provider": "vultr",
                    "region": "ams",
                    "file_size_mb": 300,
                    "vm_size": "vc2-2c-4gb",
                    "payment": "no-merkle",
                    "ant": "500",
                    "eth": "0.1",
                },
                {
                    "provider": "vultr",
                    "region": "sjc",
                    "file_size_mb": 100,
                    "vm_size": "vc2-2c-4gb",
                    "ant": "500",
                    "eth": "0.1",
                },
                {
                    "provider": "vultr",
                    "region": "sgp",
                    "file_size_mb": 50,
                    "vm_size": "vc2-2c-4gb",
                    "ant": "200",
                    "eth": "0.04",
                },
                {
                    "provider": "vultr",
                    "region": "sgp",
                    "file_size_mb": 25,
                    "vm_size": "vc2-2c-4gb",
                    "ant": "200",
                    "eth": "0.04",
                },
                {
                    "provider": "ovh",
                    "region": "DE1",
                    "file_size_mb": 20,
                    "vm_size": "b3-8",
                    "ant": "200",
                    "eth": "0.04",
                },
                {
                    "provider": "ovh",
                    "region": "BHS5",
                    "file_size_mb": 20,
                    "vm_size": "b3-8",
                    "ant": "200",
                    "eth": "0.04",
                },
                {
                    "provider": "ovh",
                    "region": "UK1",
                    "file_size_mb": 20,
                    "vm_size": "b3-8",
                    "ant": "200",
                    "eth": "0.04",
                },
            ],
            "downloaders": [
                {"provider": "digital_ocean", "region": "tor1"},
                {"provider": "digital_ocean", "region": "lon1"},
            ],
        },
        "elastic": {
            "enabled": True,
            "droplet_size": "s-8vcpu-16gb",
            "volume_size": 500,
            "shards": 8,
        },
        "monitoring": {"enabled": True},
    },
}

# Exact command sequence from runbooks/staging_rc/up/04-STG_01.sh
EXPECTED_DEPLOY = [
    ["bootstraps", "infra", "--name", "STG-01", "--bootstrap-count", "7", "--purpose", PURPOSE],
    [
        "nodes",
        "infra",
        "--name",
        "STG-01",
        "--do-vm-count",
        "5",
        "--do-regions-list",
        "ams3,blr1,fra1,lon1,nyc1,syd1,sgp1",
        "--do-droplet-size",
        "s-8vcpu-16gb",
        "--do-attached-volume-size",
        "100",
        "--vultr-vm-count",
        "5",
        "--vultr-regions-list",
        "ams,ewr,lax,lhr,nrt",
        "--vultr-plan",
        "vhp-8c-16gb-intel",
        "--vultr-attached-volume-size",
        "100",
    ],
    [
        "elastic",
        "infra",
        "--name",
        "STG-01",
        "--droplet-size",
        "s-8vcpu-16gb",
        "--volume-size",
        "500",
    ],
    ["monitoring", "infra", "--name", "STG-01"],
    [
        "clients",
        "infra",
        "--name",
        "STG-01",
        "--uploader",
        "vultr:syd:1000:vc2-2c-4gb:payment=merkle:ant=1000:eth=0.1",
        "--uploader",
        "ovh:UK1:1000:b3-8:payment=no-merkle:ant=1000:eth=0.1",
        "--uploader",
        "vultr:ewr:500:vc2-2c-4gb:payment=no-merkle:ant=1000:eth=0.1",
        "--uploader",
        "vultr:ams:300:vc2-2c-4gb:payment=no-merkle:ant=500:eth=0.1",
        "--uploader",
        "vultr:sjc:100:vc2-2c-4gb:ant=500:eth=0.1",
        "--uploader",
        "vultr:sgp:50:vc2-2c-4gb:ant=200:eth=0.04",
        "--uploader",
        "vultr:sgp:25:vc2-2c-4gb:ant=200:eth=0.04",
        "--uploader",
        "ovh:DE1:20:b3-8:ant=200:eth=0.04",
        "--uploader",
        "ovh:BHS5:20:b3-8:ant=200:eth=0.04",
        "--uploader",
        "ovh:UK1:20:b3-8:ant=200:eth=0.04",
        "--downloader",
        "digital_ocean:tor1",
        "--downloader",
        "digital_ocean:lon1",
    ],
    ["elastic", "provision", "--name", "STG-01", "--shards", "8", "--ssh-key-path", SSH],
    ["monitoring", "provision", "--name", "STG-01", "--ssh-key-path", SSH],
    [
        "nodes",
        "provision-genesis",
        "--name",
        "STG-01",
        "--port",
        "10000",
        "--node-version",
        "0.11.5-rc.4",
        "--testnet",
        "--ssh-key-path",
        SSH,
        "--with-telegraf",
    ],
    [
        "bootstraps",
        "provision",
        "--name",
        "STG-01",
        "--port",
        "10000",
        "--node-version",
        "0.11.5-rc.4",
        "--testnet",
        "--ssh-key-path",
        SSH,
        "--with-telegraf",
    ],
    [
        "nodes",
        "provision",
        "--name",
        "STG-01",
        "--node-count",
        "15",
        "--node-version",
        "0.11.5-rc.4",
        "--testnet",
        "--nat-percent",
        "30",
        "--ssh-key-path",
        SSH,
        "--with-telegraf",
    ],
    ["nodes", "start", "--name", "STG-01", "--ssh-key-path", SSH],
    [
        "clients",
        "provision",
        "--name",
        "STG-01",
        "--ant-version",
        "0.2.5-rc.4",
        "--monitoring-name",
        "STG-01",
        "--ssh-key-path",
        SSH,
    ],
    ["clients", "start", "--name", "STG-01", "--ssh-key-path", SSH],
    ["deployments", "generate-inventory", "--name", "STG-01", "--ssh-key-path", SSH],
]


def _argvs(steps):
    return [s.argv for s in steps]


def test_stg01_deploy_sequence_matches_runbook():
    manifest = Manifest.model_validate(STG_01)
    assert _argvs(build_deploy_steps(manifest)) == EXPECTED_DEPLOY


def test_stg01_destroy_sequence():
    manifest = Manifest.model_validate(STG_01)
    assert _argvs(build_destroy_steps(manifest)) == [
        ["clients", "destroy", "--name", "STG-01", "--force", "--ssh-key-path", SSH],
        ["nodes", "destroy", "--name", "STG-01", "--force"],
        ["bootstraps", "destroy", "--name", "STG-01", "--force"],
        ["elastic", "destroy", "--name", "STG-01", "--force"],
        ["monitoring", "destroy", "--name", "STG-01", "--force"],
    ]


def test_stg01_destroy_skip_drain_failures():
    # The flag rides only on the clients-destroy step; every other step is
    # identical to the default sequence.
    manifest = Manifest.model_validate(STG_01)
    steps = _argvs(build_destroy_steps(manifest, skip_drain_failures=True))
    assert steps[0] == [
        "clients", "destroy", "--name", "STG-01", "--force",
        "--ssh-key-path", SSH, "--skip-drain-failures",
    ]
    assert steps[1:] == [
        ["nodes", "destroy", "--name", "STG-01", "--force"],
        ["bootstraps", "destroy", "--name", "STG-01", "--force"],
        ["elastic", "destroy", "--name", "STG-01", "--force"],
        ["monitoring", "destroy", "--name", "STG-01", "--force"],
    ]


# --------------------------------------------------------------------------- #
# invariants
# --------------------------------------------------------------------------- #
def _minimal(**overrides):
    base = {
        "name": "DEV-01",
        "kind": "testnet",
        "purpose": "p",
        "ssh_key_path": SSH,
        "testnet": {
            "binaries": {"ant_node": {"version": "1.0"}},
            "bootstraps": {"count": 1},
            "nodes": {"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}}},
        },
    }
    base["testnet"].update(overrides)
    return Manifest.model_validate(base)


def test_slack_post_appends_final_step_for_testnet():
    m = Manifest.model_validate(
        {
            "name": "DEV-01",
            "kind": "testnet",
            "purpose": "p",
            "ssh_key_path": SSH,
            "slack_post": True,
            "testnet": {
                "binaries": {"ant_node": {"version": "1.0"}},
                "bootstraps": {"count": 1},
                "nodes": {
                    "per_vm": 5,
                    "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
                },
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[-1].argv == ["deployments", "slack-post", "--name", "DEV-01"]
    # destroy never gets a slack-post step
    assert all(s.argv[:2] != ["deployments", "slack-post"] for s in build_destroy_steps(m))


def test_slack_post_omitted_by_default_for_testnet():
    steps = build_deploy_steps(_minimal())
    assert all(s.argv[:2] != ["deployments", "slack-post"] for s in steps)


def test_single_bootstrap_omits_bootstraps_provision():
    steps = build_deploy_steps(_minimal())
    cmds = [tuple(s.argv[:2]) for s in steps]
    assert ("nodes", "provision-genesis") in cmds
    assert ("bootstraps", "provision") not in cmds


def test_no_clients_omits_client_steps():
    steps = build_deploy_steps(_minimal())
    flat = [s.argv[0:2] for s in steps]
    assert ["clients", "infra"] not in flat
    assert ["clients", "provision"] not in flat


def test_regions_count_emits_regions_flag_not_list():
    steps = build_deploy_steps(_minimal())
    infra = next(s for s in steps if s.argv[:2] == ["nodes", "infra"])
    assert "--do-regions" in infra.argv
    assert infra.argv[infra.argv.index("--do-regions") + 1] == "3"
    assert "--do-regions-list" not in infra.argv


def test_nat_omitted_when_unset():
    steps = build_deploy_steps(_minimal())
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--nat-percent" not in nprov.argv


def test_clock_skew_omitted_when_unset():
    steps = build_deploy_steps(_minimal())
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--clock-skew-count" not in nprov.argv


def test_full_percent_omitted_when_unset():
    steps = build_deploy_steps(_minimal())
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--full-percent" not in nprov.argv
    assert "--full-headroom-mb" not in nprov.argv


def test_full_percent_emitted_when_set():
    m = _minimal(
        nodes={
            "per_vm": 5,
            "full_percent": 25,
            "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
        }
    )
    steps = build_deploy_steps(m)
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--full-percent" in nprov.argv
    assert nprov.argv[nprov.argv.index("--full-percent") + 1] == "25"
    # emitted before the ssh-key flag, like the other simulation flags
    assert nprov.argv.index("--full-percent") < nprov.argv.index("--ssh-key-path")


def test_full_headroom_mb_emitted_when_set():
    m = _minimal(
        nodes={
            "per_vm": 5,
            "full_percent": 25,
            "full_headroom_mb": 512,
            "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
        }
    )
    steps = build_deploy_steps(m)
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert nprov.argv[nprov.argv.index("--full-headroom-mb") + 1] == "512"


def test_clock_skew_count_emitted_when_set():
    m = _minimal(
        nodes={
            "per_vm": 5,
            "clock_skew": 2,
            "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
        }
    )
    steps = build_deploy_steps(m)
    nprov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--clock-skew-count" in nprov.argv
    assert nprov.argv[nprov.argv.index("--clock-skew-count") + 1] == "2"


def test_clients_clock_skew_token_in_specs():
    m = _minimal(
        binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "1.0"}},
        nodes={"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}}},
        clients={
            "uploaders": [
                {"provider": "vultr", "region": "syd", "file_size_mb": 100, "clock_skew": "2h"}
            ],
            "downloaders": [
                {"provider": "digital_ocean", "region": "lon1", "clock_skew": "-2h"}
            ],
        },
    )
    steps = build_deploy_steps(m)
    cinfra = next(s for s in steps if s.argv[:2] == ["clients", "infra"])
    up = cinfra.argv[cinfra.argv.index("--uploader") + 1]
    dl = cinfra.argv[cinfra.argv.index("--downloader") + 1]
    assert up.endswith("clock_skew=2h")
    assert dl.endswith("clock_skew=-2h")


def test_branch_binary_adds_build_step_and_uses_branch_flags():
    m = _minimal(binaries={"ant_node": {"branch": "feat-x", "repo_owner": "me"}})
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant-node"]
    assert "--branch-name" in steps[0].argv and "feat-x" in steps[0].argv
    genesis = next(s for s in steps if s.argv[:2] == ["nodes", "provision-genesis"])
    assert "--branch-name" in genesis.argv
    assert "--node-version" not in genesis.argv


def test_skip_builds_omits_branch_build_steps():
    # Both binaries sourced from a branch ⇒ two build steps by default.
    m = _minimal(
        binaries={
            "ant_node": {"branch": "feat-x", "repo_owner": "me"},
            "ant": {"branch": "feat-y", "repo_owner": "me"},
        }
    )
    default = build_deploy_steps(m)
    assert [s.label for s in default[:2]] == [
        "Build ant-node from branch",
        "Build ant client from branch",
    ]

    skipped = build_deploy_steps(m, skip_builds=True)
    assert "Build ant-node from branch" not in [s.label for s in skipped]
    assert "Build ant client from branch" not in [s.label for s in skipped]
    # Everything after the two build steps is untouched.
    assert _argvs(skipped) == _argvs(default)[2:]


def test_skip_builds_noop_when_no_branch_binaries():
    # Version-pinned binaries emit no build steps, so the flag changes nothing.
    m = _minimal()
    assert _argvs(build_deploy_steps(m, skip_builds=True)) == _argvs(
        build_deploy_steps(m)
    )


def test_both_branch_builds_run_as_parallel_group():
    # Both binaries from a branch ⇒ the two build steps form one ``build``
    # parallel group with a lane each, so ``up`` runs them concurrently.
    m = _minimal(
        binaries={
            "ant_node": {"branch": "feat-x", "repo_owner": "me"},
            "ant": {"branch": "feat-y", "repo_owner": "me"},
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant-node"]
    assert steps[1].argv[:2] == ["build", "ant"]
    assert (steps[0].group, steps[0].lane) == ("build", "ant-node")
    assert (steps[1].group, steps[1].lane) == ("build", "ant")

    # The two contiguous build steps collapse into a single resumable unit.
    units = iter_units(steps)
    assert isinstance(units[0], StepGroup)
    assert units[0].name == "build"
    assert units[0].label == "Build binaries (parallel)"
    assert [[s.argv[:2] for s in lane] for lane in units[0].lanes] == [
        [["build", "ant-node"]],
        [["build", "ant"]],
    ]


def test_single_ant_node_branch_build_stays_sequential():
    # Only ant-node from a branch ⇒ one ungrouped sequential build step
    # (byte-for-byte unchanged from before parallelisation).
    m = _minimal(binaries={"ant_node": {"branch": "feat-x", "repo_owner": "me"}})
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant-node"]
    assert (steps[0].group, steps[0].lane) == (None, None)
    assert isinstance(iter_units(steps)[0], type(steps[0]))  # a bare Step, not StepGroup


def test_single_ant_branch_build_stays_sequential():
    # ant-node version-pinned + ant from a branch ⇒ one ungrouped build step.
    m = _minimal(
        binaries={
            "ant_node": {"version": "1.0"},
            "ant": {"branch": "feat-y", "repo_owner": "me"},
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant"]
    assert (steps[0].group, steps[0].lane) == (None, None)
    assert not isinstance(iter_units(steps)[0], StepGroup)


def test_no_branch_builds_emit_no_build_group():
    # Version-pinned binaries ⇒ no build steps and no build group at all.
    m = _minimal()
    steps = build_deploy_steps(m)
    assert all(s.argv[:1] != ["build"] for s in steps)
    assert all(getattr(s, "group", None) != "build" for s in steps)


def test_no_upgrade_channel_by_default_but_emitted_when_set():
    plain = build_deploy_steps(_minimal())
    genesis = next(s for s in plain if s.argv[:2] == ["nodes", "provision-genesis"])
    assert "--upgrade-channel" not in genesis.argv

    m = _minimal(upgrade_channel="beta")
    steps = build_deploy_steps(m)
    for verb in (["nodes", "provision-genesis"], ["nodes", "provision"]):
        s = next(x for x in steps if x.argv[:2] == verb)
        assert s.argv[s.argv.index("--upgrade-channel") + 1] == "beta"


def test_monitoring_on_by_default():
    # _minimal() has no `monitoring:` block — it should still be enabled.
    steps = build_deploy_steps(_minimal())
    cmds = [s.argv[:2] for s in steps]
    assert ["monitoring", "infra"] in cmds
    assert ["monitoring", "provision"] in cmds
    # Telegraf is folded into the node provision pass via --with-telegraf, not a
    # separate step.
    assert ["deployments", "provision-telegraf"] not in cmds
    nodes_prov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--with-telegraf" in nodes_prov.argv


def test_monitoring_opt_out():
    steps = build_deploy_steps(_minimal(monitoring={"enabled": False}))
    cmds = [s.argv[:2] for s in steps]
    assert ["monitoring", "infra"] not in cmds
    assert ["monitoring", "provision"] not in cmds
    # No monitoring and no elastic → no telegraf fold on the node steps.
    assert ["deployments", "provision-telegraf"] not in cmds
    nodes_prov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--with-telegraf" not in nodes_prov.argv


def _monitoring_infra_argv(steps):
    return next(s.argv for s in steps if s.argv[:2] == ["monitoring", "infra"])


def test_grafana_off_by_default():
    # No `grafana` key, and an explicit `enabled: true` with no grafana key:
    # the infra step must NOT carry --grafana (keeps argv/journal byte-identical).
    assert _monitoring_infra_argv(build_deploy_steps(_minimal())) == [
        "monitoring",
        "infra",
        "--name",
        "DEV-01",
    ]
    enabled_only = build_deploy_steps(_minimal(monitoring={"enabled": True}))
    assert "--grafana" not in _monitoring_infra_argv(enabled_only)


def test_grafana_opt_in_appends_flag():
    steps = build_deploy_steps(
        _minimal(monitoring={"enabled": True, "grafana": True})
    )
    assert _monitoring_infra_argv(steps) == [
        "monitoring",
        "infra",
        "--name",
        "DEV-01",
        "--grafana",
    ]


def test_grafana_opt_in_extension():
    m = Manifest.model_validate(
        {
            "name": "DEV-02",
            "kind": "testnet-extension",
            "purpose": "compat test",
            "ssh_key_path": SSH,
            "extension": {
                "source": "DEV-01",
                "mode": True,
                "binaries": {"ant_node": {"version": "2.0"}},
                "nodes": {
                    "per_vm": 10,
                    "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
                },
                "monitoring": {"enabled": True, "grafana": True},
            },
        }
    )
    assert _monitoring_infra_argv(build_deploy_steps(m)) == [
        "monitoring",
        "infra",
        "--name",
        "DEV-02",
        "--grafana",
    ]


# --------------------------------------------------------------------------- #
# extension kind
# --------------------------------------------------------------------------- #
def _extension():
    return Manifest.model_validate(
        {
            "name": "DEV-02",
            "kind": "testnet-extension",
            "purpose": "compat test",
            "ssh_key_path": SSH,
            "extension": {
                "source": "DEV-01",
                "mode": True,
                "binaries": {"ant_node": {"version": "2.0"}},
                "nodes": {
                    "per_vm": 10,
                    "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
                },
            },
        }
    )


def test_extension_deploy_skips_bootstrap_tier():
    steps = build_deploy_steps(_extension())
    cmds = [s.argv[:2] for s in steps]
    # extension peers with the source's bootstraps: no bootstraps infra / genesis
    assert ["deployments", "extend"] == cmds[0]
    assert ["bootstraps", "infra"] not in cmds
    assert ["nodes", "provision-genesis"] not in cmds
    assert ["bootstraps", "provision"] not in cmds
    assert ["nodes", "provision"] in cmds
    assert ["nodes", "start"] in cmds
    extend = steps[0]
    assert "--source" in extend.argv and "DEV-01" in extend.argv
    # Telegraf (monitoring defaults on for extensions) is folded into the nodes
    # provision pass, which runs before nodes start — so metrics cover the node
    # lifetime from the start without a separate telegraf step.
    assert ["deployments", "provision-telegraf"] not in cmds
    nodes_prov = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert "--with-telegraf" in nodes_prov.argv


def test_extension_destroy_ends_with_unextend():
    steps = build_destroy_steps(_extension())
    assert steps[0].argv[:2] == ["nodes", "destroy"]
    assert steps[-1].argv == ["deployments", "unextend", "--name", "DEV-02", "--force"]


def test_clients_bootstrap_cache_token_in_specs():
    m = _minimal(
        binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "1.0"}},
        nodes={"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}}},
        clients={
            "uploaders": [
                {
                    "provider": "vultr",
                    "region": "syd",
                    "file_size_mb": 100,
                    "bootstrap_cache": False,
                }
            ],
            "downloaders": [
                {"provider": "digital_ocean", "region": "lon1", "bootstrap_cache": True}
            ],
        },
    )
    steps = build_deploy_steps(m)
    cinfra = next(s for s in steps if s.argv[:2] == ["clients", "infra"])
    up = cinfra.argv[cinfra.argv.index("--uploader") + 1]
    dl = cinfra.argv[cinfra.argv.index("--downloader") + 1]
    assert up.endswith("bootstrap_cache=false")
    assert dl.endswith("bootstrap_cache=true")


def test_clients_bootstrap_cache_token_omitted_when_unset():
    m = _minimal(
        binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "1.0"}},
        nodes={"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}}},
        clients={
            "uploaders": [{"provider": "vultr", "region": "syd", "file_size_mb": 100}],
            "downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
        },
    )
    steps = build_deploy_steps(m)
    cinfra = next(s for s in steps if s.argv[:2] == ["clients", "infra"])
    up = cinfra.argv[cinfra.argv.index("--uploader") + 1]
    dl = cinfra.argv[cinfra.argv.index("--downloader") + 1]
    assert "bootstrap_cache" not in up
    assert "bootstrap_cache" not in dl


def test_ovh_nodes_provider_emits_ovh_flags():
    m = _minimal(
        nodes={
            "per_vm": 5,
            "providers": {
                "digital_ocean": {"vm_count": 2, "regions": 3},
                "ovh": {"vm_count": 2, "regions_list": ["GRA9", "UK1"], "size": "b3-32"},
            },
        }
    )
    steps = build_deploy_steps(m)
    infra = next(s for s in steps if s.argv[:2] == ["nodes", "infra"])
    assert "--ovh-vm-count" in infra.argv
    assert infra.argv[infra.argv.index("--ovh-vm-count") + 1] == "2"
    assert infra.argv[infra.argv.index("--ovh-regions-list") + 1] == "GRA9,UK1"
    assert infra.argv[infra.argv.index("--ovh-flavor") + 1] == "b3-32"


def test_unknown_nodes_provider_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        _minimal(
            nodes={"per_vm": 5, "providers": {"linode": {"vm_count": 1, "regions": 1}}}
        )


# --------------------------------------------------------------------------- #
# Node ES forwarder drop-log-levels derived from nodes.log_level
# (now folded into the `nodes provision` step via --with-telegraf)
# --------------------------------------------------------------------------- #
def _es_telegraf_step(m):
    steps = build_deploy_steps(m)
    return next(s for s in steps if s.argv[:2] == ["nodes", "provision"])


def _nodes(**extra):
    base = {"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}}}
    base.update(extra)
    return base


def test_es_drop_log_levels_omitted_for_info_or_unset():
    # No nodes.log_level + no explicit elastic.drop_log_levels -> the flag is
    # omitted entirely so the CLI default (DEBUG,TRACE) applies and the argv
    # stays byte-identical to before this feature (journal-hash stability).
    step = _es_telegraf_step(_minimal(elastic={"enabled": True}))
    assert "--drop-log-levels" not in step.argv

    step_info = _es_telegraf_step(
        _minimal(elastic={"enabled": True}, nodes=_nodes(log_level="info"))
    )
    assert "--drop-log-levels" not in step_info.argv


def test_es_drop_log_levels_empty_for_trace():
    # nodes.log_level: trace -> forward EVERYTHING (drop nothing) so the trace
    # the operator asked for actually reaches Elasticsearch.
    step = _es_telegraf_step(
        _minimal(elastic={"enabled": True}, nodes=_nodes(log_level="trace"))
    )
    assert "--drop-log-levels" in step.argv
    assert step.argv[step.argv.index("--drop-log-levels") + 1] == ""


def test_es_drop_log_levels_trace_only_for_debug():
    # nodes.log_level: debug -> keep DEBUG, drop only the noisier TRACE.
    step = _es_telegraf_step(
        _minimal(elastic={"enabled": True}, nodes=_nodes(log_level="debug"))
    )
    assert step.argv[step.argv.index("--drop-log-levels") + 1] == "TRACE"


def test_es_explicit_drop_log_levels_overrides_log_level():
    # An explicit elastic.drop_log_levels always wins, even when log_level would
    # otherwise widen the set — the volume escape hatch.
    step = _es_telegraf_step(
        _minimal(
            elastic={"enabled": True, "drop_log_levels": "DEBUG,TRACE"},
            nodes=_nodes(log_level="trace"),
        )
    )
    assert step.argv[step.argv.index("--drop-log-levels") + 1] == "DEBUG,TRACE"


def test_es_explicit_empty_drop_log_levels_is_kept():
    # drop_log_levels: "" with an info node level still emits the empty flag
    # (force-keep everything), distinct from the omit-the-flag default case.
    step = _es_telegraf_step(
        _minimal(elastic={"enabled": True, "drop_log_levels": ""})
    )
    assert "--drop-log-levels" in step.argv
    assert step.argv[step.argv.index("--drop-log-levels") + 1] == ""


# --------------------------------------------------------------------------- #
# Client forwarder buffer wiring (elastic.buffer_limit -> clients provision)
# --------------------------------------------------------------------------- #
_CLIENTS = {
    "uploaders": [
        {"provider": "digital_ocean", "region": "lon1", "file_size_mb": 100,
         "forward_logs": True}
    ],
    "downloaders": [{"provider": "digital_ocean", "region": "tor1"}],
}


def test_client_forward_buffer_limit_emitted_when_buffer_set():
    m = _minimal(
        binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "1.0"}},
        elastic={"enabled": True, "buffer_limit": 2000000},
        clients=_CLIENTS,
    )
    steps = build_deploy_steps(m)
    cprov = next(s for s in steps if s.argv[:2] == ["clients", "provision"])
    assert cprov.argv[cprov.argv.index("--forward-buffer-limit") + 1] == "2000000"
    # and the node forwarder (folded into nodes provision) gets --buffer-limit
    es = next(s for s in steps if s.argv[:2] == ["nodes", "provision"])
    assert es.argv[es.argv.index("--buffer-limit") + 1] == "2000000"


def test_client_forward_buffer_limit_omitted_when_unset():
    m = _minimal(
        binaries={"ant_node": {"version": "1.0"}, "ant": {"version": "1.0"}},
        elastic={"enabled": True},
        clients=_CLIENTS,
    )
    steps = build_deploy_steps(m)
    cprov = next(s for s in steps if s.argv[:2] == ["clients", "provision"])
    assert "--forward-buffer-limit" not in cprov.argv


# --------------------------------------------------------------------------- #
# keep_on_destroy — leave the ES / InfluxDB stack standing through `down` so the
# run's logs and metrics stay queryable after the testnet tiers are gone.
# --------------------------------------------------------------------------- #
def _elastic_infra_argv(steps):
    return next(s.argv for s in steps if s.argv[:2] == ["elastic", "infra"])


def _destroy_cmds(manifest):
    return [s.argv[:2] for s in build_destroy_steps(manifest)]


def test_keep_on_destroy_off_by_default_keeps_infra_argv_identical():
    # The flag must only appear when asked for: an existing manifest's step argv
    # (and therefore its `up` journal hash) has to stay byte-identical.
    m = _minimal(elastic={"enabled": True}, monitoring={"enabled": True})
    steps = build_deploy_steps(m)
    assert _elastic_infra_argv(steps) == ["elastic", "infra", "--name", "DEV-01"]
    assert _monitoring_infra_argv(steps) == ["monitoring", "infra", "--name", "DEV-01"]
    # ...and both stacks are still destroyed.
    assert ["elastic", "destroy"] in _destroy_cmds(m)
    assert ["monitoring", "destroy"] in _destroy_cmds(m)


def test_elastic_keep_on_destroy_flags_infra_and_omits_destroy():
    m = _minimal(
        elastic={"enabled": True, "keep_on_destroy": True},
        monitoring={"enabled": True},
    )
    assert _elastic_infra_argv(build_deploy_steps(m)) == [
        "elastic",
        "infra",
        "--name",
        "DEV-01",
        "--keep-on-destroy",
    ]
    cmds = _destroy_cmds(m)
    assert ["elastic", "destroy"] not in cmds
    # monitoring did not opt in, so it is still torn down
    assert ["monitoring", "destroy"] in cmds


def test_monitoring_keep_on_destroy_flags_infra_and_omits_destroy():
    m = _minimal(
        elastic={"enabled": True},
        monitoring={"enabled": True, "keep_on_destroy": True},
    )
    assert _monitoring_infra_argv(build_deploy_steps(m)) == [
        "monitoring",
        "infra",
        "--name",
        "DEV-01",
        "--keep-on-destroy",
    ]
    cmds = _destroy_cmds(m)
    assert ["monitoring", "destroy"] not in cmds
    assert ["elastic", "destroy"] in cmds


def test_keep_on_destroy_composes_with_grafana():
    # Both flags on the same step; --grafana stays first so the argv is stable.
    m = _minimal(
        monitoring={"enabled": True, "grafana": True, "keep_on_destroy": True}
    )
    assert _monitoring_infra_argv(build_deploy_steps(m)) == [
        "monitoring",
        "infra",
        "--name",
        "DEV-01",
        "--grafana",
        "--keep-on-destroy",
    ]


def test_both_keeps_leave_only_the_testnet_tiers_in_destroy():
    m = _minimal(
        elastic={"enabled": True, "keep_on_destroy": True},
        monitoring={"enabled": True, "keep_on_destroy": True},
    )
    assert _argvs(build_destroy_steps(m)) == [
        ["nodes", "destroy", "--name", "DEV-01", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-01", "--force"],
    ]


def test_keep_on_destroy_extension():
    m = Manifest.model_validate(
        {
            "name": "DEV-02",
            "kind": "testnet-extension",
            "purpose": "compat test",
            "ssh_key_path": SSH,
            "extension": {
                "source": "DEV-01",
                "binaries": {"ant_node": {"version": "2.0"}},
                "nodes": {
                    "per_vm": 10,
                    "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
                },
                "elastic": {"enabled": True, "keep_on_destroy": True},
                "monitoring": {"enabled": True, "keep_on_destroy": True},
            },
        }
    )
    steps = build_deploy_steps(m)
    assert "--keep-on-destroy" in _elastic_infra_argv(steps)
    assert "--keep-on-destroy" in _monitoring_infra_argv(steps)
    # nodes destroy + unextend only; the observability stacks survive
    assert _argvs(build_destroy_steps(m)) == [
        ["nodes", "destroy", "--name", "DEV-02", "--force"],
        ["deployments", "unextend", "--name", "DEV-02", "--force"],
    ]
