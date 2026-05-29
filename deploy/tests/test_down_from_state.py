"""Tests for `deployments down` state-driven teardown (--name without --config)."""

from saorsa_deploy.orchestrator import build_destroy_steps_from_state


def _argvs(steps):
    return [s.argv for s in steps]


def _patch_state(monkeypatch, *, client, deployment, elastic, monitoring):
    """Patch the four state loaders to present/absent (RuntimeError = absent)."""
    import saorsa_deploy.state as state

    def maker(present):
        def loader(name):
            if not present:
                raise RuntimeError(f"no state for {name}")
            return {"name": name}

        return loader

    monkeypatch.setattr(state, "load_client_state", maker(client))
    monkeypatch.setattr(state, "load_deployment_state", maker(deployment))
    monkeypatch.setattr(state, "load_elastic_state", maker(elastic))
    monkeypatch.setattr(state, "load_monitoring_state", maker(monitoring))


def test_full_testnet_teardown_order(monkeypatch):
    _patch_state(
        monkeypatch, client=True, deployment=True, elastic=True, monitoring=True
    )
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
    _patch_state(
        monkeypatch, client=False, deployment=True, elastic=False, monitoring=True
    )
    cmds = [s.argv[:2] for s in build_destroy_steps_from_state("DEV-02")]
    assert cmds == [
        ["nodes", "destroy"],
        ["bootstraps", "destroy"],
        ["monitoring", "destroy"],
    ]


def test_no_state_yields_no_steps(monkeypatch):
    _patch_state(
        monkeypatch, client=False, deployment=False, elastic=False, monitoring=False
    )
    assert build_destroy_steps_from_state("GONE-01") == []


def test_skip_drain_failures_appended_to_clients_destroy(monkeypatch):
    _patch_state(
        monkeypatch, client=True, deployment=True, elastic=False, monitoring=True
    )
    steps = build_destroy_steps_from_state("DEV-02", skip_drain_failures=True)
    # Only the clients-destroy step carries the flag; the rest are untouched.
    assert _argvs(steps) == [
        ["clients", "destroy", "--name", "DEV-02", "--force", "--skip-drain-failures"],
        ["nodes", "destroy", "--name", "DEV-02", "--force"],
        ["bootstraps", "destroy", "--name", "DEV-02", "--force"],
        ["monitoring", "destroy", "--name", "DEV-02", "--force"],
    ]


def test_skip_drain_failures_defaults_off(monkeypatch):
    _patch_state(
        monkeypatch, client=True, deployment=False, elastic=False, monitoring=False
    )
    steps = build_destroy_steps_from_state("DEV-02")
    assert _argvs(steps) == [
        ["clients", "destroy", "--name", "DEV-02", "--force"],
    ]
