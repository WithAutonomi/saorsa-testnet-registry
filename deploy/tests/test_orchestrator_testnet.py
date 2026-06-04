"""Golden-parity tests for the testnet orchestrator.

The deploy sequence MUST match the hand-written reference runbook
``runbooks/staging_rc/up/04-STG_01.sh`` command-for-command. This test locks the
ordering and flag-coupling invariants so a refactor can't silently drift.
"""

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps

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
            "attached_volume_size": 100,
            "providers": {
                "digital_ocean": {
                    "vm_count": 5,
                    "regions_list": ["ams3", "blr1", "fra1", "lon1", "nyc1", "syd1", "sgp1"],
                    "size": "s-8vcpu-16gb",
                },
                "vultr": {
                    "vm_count": 5,
                    "regions_list": ["ams", "ewr", "lax", "lhr", "nrt"],
                    "size": "vhp-8c-16gb-intel",
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
        "--vultr-vm-count",
        "5",
        "--vultr-regions-list",
        "ams,ewr,lax,lhr,nrt",
        "--vultr-plan",
        "vhp-8c-16gb-intel",
        "--attached-volume-size",
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
    ],
    ["nodes", "start", "--name", "STG-01", "--ssh-key-path", SSH],
    ["elastic", "provision-telegraf", "--name", "STG-01", "--ssh-key-path", SSH],
    ["monitoring", "provision-telegraf", "--name", "STG-01", "--ssh-key-path", SSH],
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
    cmds = [s.argv[:2] for s in build_deploy_steps(_minimal())]
    assert ["monitoring", "infra"] in cmds
    assert ["monitoring", "provision"] in cmds
    assert ["monitoring", "provision-telegraf"] in cmds


def test_monitoring_opt_out():
    steps = build_deploy_steps(_minimal(monitoring={"enabled": False}))
    cmds = [s.argv[:2] for s in steps]
    assert ["monitoring", "infra"] not in cmds
    assert ["monitoring", "provision"] not in cmds
    assert ["monitoring", "provision-telegraf"] not in cmds


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
