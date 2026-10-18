"""Tests for `deployments down` state-driven teardown (--name without --config)."""

import pytest

from saorsa_deploy.orchestrator import (
    build_destroy_steps_from_state,
    retained_observability,
)


def _argvs(steps):
    return [s.argv for s in steps]


def _patch_state(
    monkeypatch,
    *,
    client,
    deployment,
    elastic,
    monitoring,
    elastic_extra=None,
    monitoring_extra=None,
):
    """Patch the four state loaders to present/absent (RuntimeError = absent).

    ``elastic_extra`` / ``monitoring_extra`` are merged into that component's
    state doc, which is how ``keep_on_destroy`` is exercised.
    """
    import saorsa_deploy.state as state

    def maker(present, extra=None):
        def loader(name):
            if not present:
                raise RuntimeError(f"no state for {name}")
            return {"name": name, **(extra or {})}

        return loader

    monkeypatch.setattr(state, "load_client_state", maker(client))
    monkeypatch.setattr(state, "load_deployment_state", maker(deployment))
    monkeypatch.setattr(state, "load_elastic_state", maker(elastic, elastic_extra))
    monkeypatch.setattr(state, "load_monitoring_state", maker(monitoring, monitoring_extra))


def test_full_testnet_teardown_order(monkeypatch):
    _patch_state(monkeypatch, client=True, deployment=True, elastic=True, monitoring=True)
    steps = build_destroy_steps_from_state("DEV-01")
    assert _argvs(steps) == [
        ["clients", "destroy", "--name", "DEV-01", "--force"],
        ["nodes", "destroy", "--name", "DEV-01", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-01", "--force"],
        ["elastic", "destroy", "--name", "DEV-01", "--force"],
        ["monitoring", "destroy", "--name", "DEV-01", "--force"],
    ]


def test_only_existing_components_are_torn_down(monkeypatch):
    # Nodes + bootstraps + monitoring, but no clients and no elastic.
    _patch_state(monkeypatch, client=False, deployment=True, elastic=False, monitoring=True)
    cmds = [s.argv[:2] for s in build_destroy_steps_from_state("DEV-02")]
    assert cmds == [
        ["nodes", "destroy"],
        ["bootstraps", "destroy"],
        ["monitoring", "destroy"],
    ]


def test_no_state_yields_no_steps(monkeypatch):
    _patch_state(monkeypatch, client=False, deployment=False, elastic=False, monitoring=False)
    assert build_destroy_steps_from_state("GONE-01") == []


def test_skip_drain_failures_appended_to_clients_destroy(monkeypatch):
    _patch_state(monkeypatch, client=True, deployment=True, elastic=False, monitoring=True)
    steps = build_destroy_steps_from_state("DEV-02", skip_drain_failures=True)
    # Only the clients-destroy step carries the flag; the rest are untouched.
    assert _argvs(steps) == [
        ["clients", "destroy", "--name", "DEV-02", "--force", "--skip-drain-failures"],
        ["nodes", "destroy", "--name", "DEV-02", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-02", "--force"],
        ["monitoring", "destroy", "--name", "DEV-02", "--force"],
    ]


def test_skip_drain_failures_defaults_off(monkeypatch):
    _patch_state(monkeypatch, client=True, deployment=False, elastic=False, monitoring=False)
    steps = build_destroy_steps_from_state("DEV-02")
    assert _argvs(steps) == [
        ["clients", "destroy", "--name", "DEV-02", "--force"],
    ]


# --------------------------------------------------------------------------- #
# keep_on_destroy — the state-driven path is the one that can ONLY work if the
# intent was persisted at `infra` time; there is no manifest here to consult.
# --------------------------------------------------------------------------- #
def test_kept_elastic_is_not_destroyed(monkeypatch):
    _patch_state(
        monkeypatch,
        client=False,
        deployment=True,
        elastic=True,
        monitoring=True,
        elastic_extra={"keep_on_destroy": True},
    )
    assert _argvs(build_destroy_steps_from_state("DEV-01")) == [
        ["nodes", "destroy", "--name", "DEV-01", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-01", "--force"],
        ["monitoring", "destroy", "--name", "DEV-01", "--force"],
    ]


def test_kept_monitoring_is_not_destroyed(monkeypatch):
    _patch_state(
        monkeypatch,
        client=False,
        deployment=True,
        elastic=True,
        monitoring=True,
        monitoring_extra={"keep_on_destroy": True},
    )
    assert _argvs(build_destroy_steps_from_state("DEV-01")) == [
        ["nodes", "destroy", "--name", "DEV-01", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-01", "--force"],
        ["elastic", "destroy", "--name", "DEV-01", "--force"],
    ]


def test_both_kept_leaves_only_the_testnet_tiers(monkeypatch):
    _patch_state(
        monkeypatch,
        client=True,
        deployment=True,
        elastic=True,
        monitoring=True,
        elastic_extra={"keep_on_destroy": True},
        monitoring_extra={"keep_on_destroy": True},
    )
    assert [s.argv[:2] for s in build_destroy_steps_from_state("DEV-01")] == [
        ["clients", "destroy"],
        ["nodes", "destroy"],
        ["bootstraps", "destroy"],
    ]


def test_explicit_false_and_legacy_state_both_destroy(monkeypatch):
    # State docs written before this option existed have no `keep_on_destroy`
    # key at all. Absent and explicit-false must behave identically — destroy —
    # or an old deployment's teardown would start leaking billed VMs.
    for extra in ({}, {"keep_on_destroy": False}):
        _patch_state(
            monkeypatch,
            client=False,
            deployment=False,
            elastic=True,
            monitoring=True,
            elastic_extra=extra,
            monitoring_extra=extra,
        )
        assert [s.argv[:2] for s in build_destroy_steps_from_state("OLD-01")] == [
            ["elastic", "destroy"],
            ["monitoring", "destroy"],
        ]


def test_retained_observability_reports_endpoints_and_cleanup(monkeypatch):
    _patch_state(
        monkeypatch,
        client=False,
        deployment=True,
        elastic=True,
        monitoring=True,
        elastic_extra={
            "keep_on_destroy": True,
            "elasticsearch_url": "https://10.0.0.1:9200",
        },
        monitoring_extra={"keep_on_destroy": True, "influxdb_url": "http://10.0.0.2:8086"},
    )
    retained = retained_observability("DEV-01")
    assert [r["component"] for r in retained] == ["Elasticsearch", "InfluxDB"]
    assert retained[0]["endpoint"] == "https://10.0.0.1:9200"
    assert retained[1]["endpoint"] == "http://10.0.0.2:8086"
    assert retained[0]["destroy_command"] == "saorsa-deploy elastic destroy --name DEV-01 --force"
    assert (
        retained[1]["destroy_command"] == "saorsa-deploy monitoring destroy --name DEV-01 --force"
    )


def test_retained_observability_falls_back_to_vm_ip(monkeypatch):
    # `infra` records the keep intent before `provision` records the URL, so a
    # deployment torn down between those two steps has only the VM IP.
    _patch_state(
        monkeypatch,
        client=False,
        deployment=False,
        elastic=True,
        monitoring=False,
        elastic_extra={"keep_on_destroy": True, "vm_ip": "10.0.0.9"},
    )
    assert retained_observability("DEV-01")[0]["endpoint"] == "10.0.0.9"


def test_retained_observability_empty_when_nothing_kept(monkeypatch):
    _patch_state(monkeypatch, client=True, deployment=True, elastic=True, monitoring=True)
    assert retained_observability("DEV-01") == []


# --------------------------------------------------------------------------- #
# `deployments down` reporting. Keeping a stack means NO destroy step is
# generated for it, so without an explicit report the surviving VMs are invisible
# in the teardown output — and they keep billing until someone remembers them.
# --------------------------------------------------------------------------- #
def _down_args(name="DEV-01"):
    from types import SimpleNamespace

    return SimpleNamespace(config=None, name=name, skip_drain_failures=False, dry_run=False)


def test_down_reports_retained_instead_of_nothing_to_destroy(monkeypatch, capsys):
    from saorsa_deploy.cmd import deploy_cmds

    monkeypatch.setattr(deploy_cmds, "build_destroy_steps_from_state", lambda *a, **k: [])
    monkeypatch.setattr(
        deploy_cmds,
        "retained_observability",
        lambda name: [
            {
                "component": "InfluxDB",
                "kind": "metrics",
                "endpoint": "http://10.0.0.2:8086",
                "info_command": "saorsa-deploy monitoring info --name DEV-01",
                "destroy_command": "saorsa-deploy monitoring destroy --name DEV-01 --force",
            }
        ],
    )
    deploy_cmds.cmd_down(_down_args())
    out = capsys.readouterr().out
    assert "No resources found in state" not in out
    assert "keep_on_destroy" in out
    assert "http://10.0.0.2:8086" in out
    assert "monitoring destroy --name DEV-01 --force" in out


def test_down_still_reports_nothing_to_destroy_when_truly_empty(monkeypatch, capsys):
    from saorsa_deploy.cmd import deploy_cmds

    monkeypatch.setattr(deploy_cmds, "build_destroy_steps_from_state", lambda *a, **k: [])
    monkeypatch.setattr(deploy_cmds, "retained_observability", lambda name: [])
    deploy_cmds.cmd_down(_down_args("GONE-01"))
    out = capsys.readouterr().out
    assert "No resources found in state" in out


def test_down_does_not_claim_full_destruction_when_a_stack_is_kept(monkeypatch, capsys):
    from saorsa_deploy.cmd import deploy_cmds
    from saorsa_deploy.orchestrator.steps import Step

    monkeypatch.setattr(
        deploy_cmds,
        "build_destroy_steps_from_state",
        lambda *a, **k: [Step(["nodes", "destroy", "--name", "DEV-01", "--force"], "n")],
    )
    monkeypatch.setattr(
        deploy_cmds,
        "retained_observability",
        lambda name: [
            {
                "component": "Elasticsearch",
                "kind": "logs",
                "endpoint": "https://10.0.0.1:9200",
                "info_command": "saorsa-deploy elastic info --name DEV-01",
                "destroy_command": "saorsa-deploy elastic destroy --name DEV-01 --force",
            }
        ],
    )
    monkeypatch.setattr(deploy_cmds, "_execute", lambda *a, **k: None)
    monkeypatch.setattr(deploy_cmds, "delete_step_journal", lambda name: None)
    deploy_cmds.cmd_down(_down_args())
    out = capsys.readouterr().out
    assert "has been destroyed" not in out
    assert "torn down" in out
    assert "Elasticsearch" in out


def test_manifest_keep_overrides_the_stored_flag(monkeypatch):
    # A manifest edited to add keep_on_destroy AFTER `up` already ran leaves the
    # stored flag false. `down --config` generates no destroy step for it, so the
    # report must follow the manifest — otherwise the run claims full destruction
    # with the VM still up.
    _patch_state(
        monkeypatch,
        client=False,
        deployment=True,
        elastic=True,
        monitoring=True,
        elastic_extra={"keep_on_destroy": False, "vm_ip": "10.0.0.1"},
        monitoring_extra={"keep_on_destroy": False, "vm_ip": "10.0.0.2"},
    )
    assert retained_observability("DEV-01") == []
    overridden = retained_observability("DEV-01", elastic_kept=True, monitoring_kept=False)
    assert [r["component"] for r in overridden] == ["Elasticsearch"]


def test_manifest_override_can_also_clear_a_stored_keep(monkeypatch):
    # Inverse: the stack was created with --keep-on-destroy but the manifest no
    # longer asks for it, so `down --config` DOES destroy it — don't report it as
    # retained.
    _patch_state(
        monkeypatch,
        client=False,
        deployment=True,
        elastic=True,
        monitoring=False,
        elastic_extra={"keep_on_destroy": True, "vm_ip": "10.0.0.1"},
    )
    assert retained_observability("DEV-01", elastic_kept=False) == []


def test_manifest_keep_overrides_for_testnet_and_prod_kinds():
    from saorsa_deploy.cmd.deploy_cmds import _manifest_keep_overrides
    from saorsa_deploy.config.models import Manifest

    testnet = Manifest.model_validate(
        {
            "name": "DEV-01",
            "kind": "testnet",
            "purpose": "p",
            "testnet": {
                "binaries": {"ant_node": {"version": "1.0"}},
                "nodes": {
                    "per_vm": 5,
                    "providers": {"digital_ocean": {"vm_count": 1, "regions": 1}},
                },
                "elastic": {"enabled": True, "keep_on_destroy": True},
                "monitoring": {"enabled": True},
            },
        }
    )
    assert _manifest_keep_overrides(testnet) == {
        "elastic_kept": True,
        "monitoring_kept": False,
    }

    # prod kinds have no elastic:/monitoring: blocks — the equivalent intent lives
    # in their spec-level keep_elastic / keep_monitoring.
    prod = Manifest.model_validate(
        {
            "name": "PROD-DL-01",
            "kind": "prod-downloaders",
            "purpose": "p",
            "prod_downloaders": {
                "binary": {"version": "0.2.4"},
                "spec": "lon1",
                "keep_monitoring": True,
            },
        }
    )
    assert _manifest_keep_overrides(prod) == {
        "elastic_kept": False,
        "monitoring_kept": True,
    }


def test_down_by_name_refuses_while_a_client_extension_stands(monkeypatch, capsys):
    """And refuses BEFORE the "nothing to destroy" report.

    A deployment whose own state is already gone can still have a client
    extension standing; reporting "nothing to destroy" would leave those VMs
    billing with nothing left pointing at them.
    """
    import saorsa_deploy.state as state
    from saorsa_deploy.cmd import deploy_cmds

    monkeypatch.setattr(state, "list_client_extensions", lambda name: ["browsers"])
    monkeypatch.setattr(
        deploy_cmds,
        "build_destroy_steps_from_state",
        lambda *a, **k: pytest.fail("built destroy steps under a live extension"),
    )
    monkeypatch.setattr(deploy_cmds, "_execute", lambda *a, **k: pytest.fail("ran a destroy step"))

    with pytest.raises(SystemExit) as exc:
        deploy_cmds.cmd_down(_down_args())

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "browsers" in out
    assert "clients down --name DEV-01 --extension browsers" in out


def test_down_by_name_refuses_when_the_extension_scan_cannot_complete(monkeypatch):
    """The scan reaching S3 unauthenticated is what turned CI red, and the
    now-strict guard is right to refuse: the destroy would have failed a moment
    later reading its own state from the same bucket."""
    import saorsa_deploy.state as state
    from saorsa_deploy.cmd import deploy_cmds

    def boom(name):
        raise OSError("Unable to locate credentials")

    monkeypatch.setattr(state, "list_client_extensions", boom)
    monkeypatch.setattr(deploy_cmds, "_execute", lambda *a, **k: pytest.fail("ran a destroy step"))

    with pytest.raises(SystemExit) as exc:
        deploy_cmds.cmd_down(_down_args())

    assert exc.value.code == 2
