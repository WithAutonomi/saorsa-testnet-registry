"""Manifest → ordered command sequence.

``build_deploy_steps`` / ``build_destroy_steps`` dispatch on ``manifest.kind`` to
the per-kind builders. These are pure functions (no I/O), so ``plan`` can print
the sequence and tests can assert on it without touching the cloud.
"""

from __future__ import annotations

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator.steps import Step
from saorsa_deploy.orchestrator.testnet import (
    build_extension_deploy_steps,
    build_extension_destroy_steps,
    build_testnet_deploy_steps,
    build_testnet_destroy_steps,
)

__all__ = [
    "Step",
    "build_deploy_steps",
    "build_destroy_steps",
    "build_destroy_steps_from_state",
    "retained_observability",
]


def build_deploy_steps(manifest: Manifest, *, skip_builds: bool = False) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        steps = build_testnet_deploy_steps(manifest, skip_builds=skip_builds)
    elif kind == "testnet-extension":
        steps = build_extension_deploy_steps(manifest, skip_builds=skip_builds)
    elif kind == "clients":
        from saorsa_deploy.orchestrator import clients as clients_orch

        steps = clients_orch.build_clients_deploy_steps(manifest)
    elif kind == "elasticsearch":
        # Deliberately NOT routed through prod.py: that path prepends a binary
        # build and appends `generate-inventory`, and a standalone Elasticsearch
        # deployment has neither a binary nor any VMs to inventory.
        from saorsa_deploy.orchestrator import elastic as elastic_orch

        steps = elastic_orch.build_deploy_steps(manifest)
    else:
        from saorsa_deploy.orchestrator import prod

        steps = prod.build_deploy_steps(manifest, skip_builds=skip_builds)

    # Universal tail: post a summary to Slack once the deployment is up. Applies
    # to every kind, so it lives here at the dispatch chokepoint rather than in
    # each per-kind builder. best_effort=True: the deploy is already complete by
    # the time this runs, so a Slack failure must not fail the run.
    #
    # Except `clients`: `deployments slack-post` takes a DEPLOYMENT name and
    # reads its registry row, and an extension has no row of its own, so this
    # tail would exit 1 on "No deployment named <extension>". That kind carries
    # its own `clients slack-post` steps (on both deploy AND destroy), appended
    # by `orchestrator.clients`.
    if manifest.slack_post and kind != "clients":
        steps.append(
            Step(
                ["deployments", "slack-post", "--name", manifest.name],
                "Post summary to Slack",
                best_effort=True,
            )
        )
    return steps


def build_destroy_steps(manifest: Manifest, *, skip_drain_failures: bool = False) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        return build_testnet_destroy_steps(manifest, skip_drain_failures=skip_drain_failures)
    if kind == "testnet-extension":
        return build_extension_destroy_steps(manifest, skip_drain_failures=skip_drain_failures)
    if kind == "clients":
        from saorsa_deploy.orchestrator import clients as clients_orch

        return clients_orch.build_clients_destroy_steps(
            manifest, skip_drain_failures=skip_drain_failures
        )
    if kind == "elasticsearch":
        from saorsa_deploy.orchestrator import elastic as elastic_orch

        return elastic_orch.build_destroy_steps(manifest)
    from saorsa_deploy.orchestrator import prod

    # prod-* kinds tear down via their own `prod <kind> destroy` command, which
    # has no --skip-drain-failures flag, so the flag does not apply there.
    return prod.build_destroy_steps(manifest)


def build_destroy_steps_from_state(name: str, *, skip_drain_failures: bool = False) -> list[Step]:
    """Destroy steps derived from what actually exists in S3 state — for a
    name-only ``down`` (no manifest).

    Teardown is fundamentally name/state-driven, so this probes each component's
    state and emits the matching destroy in reverse order (clients → nodes →
    bootstraps → elastic → monitoring). It covers testnet/extension deployments;
    prod-* kinds are single-command deployments with no testnet state — use
    their ``prod <kind> destroy --name`` command (or ``down --config``).

    An elastic / monitoring stack whose state records ``keep_on_destroy: true``
    (written at ``infra`` time from the manifest's
    ``elastic.keep_on_destroy`` / ``monitoring.keep_on_destroy``) is deliberately
    left standing, so the run's logs/metrics stay queryable once the testnet is
    gone. Reading the intent back off the stored state is what makes this
    manifest-less path behave the same as ``down --config``. State docs written
    before the option existed have no such key, so they destroy exactly as before.

    When ``skip_drain_failures`` is set, the clients-destroy step is told to
    proceed with teardown even if draining a per-uploader wallet fails (the
    abandoned dust is reported but no longer blocks the destroy).
    """
    from saorsa_deploy.state import (
        load_client_state,
        load_deployment_state,
        load_elastic_state,
        load_monitoring_state,
    )

    def _exists(loader) -> bool:
        try:
            return loader(name) is not None
        except Exception:
            return False

    def _destroyable(loader) -> bool:
        """True when the component exists AND has not asked to be kept."""
        try:
            state = loader(name)
        except Exception:
            return False
        if state is None:
            return False
        return not bool(state.get("keep_on_destroy", False))

    steps: list[Step] = []
    if _exists(load_client_state):
        clients_argv = ["clients", "destroy", "--name", name, "--force"]
        if skip_drain_failures:
            clients_argv.append("--skip-drain-failures")
        steps.append(Step(clients_argv, "Destroy clients"))
    if _exists(load_deployment_state):
        steps.append(Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes"))
        steps.append(
            Step(
                ["bootstraps", "destroy", "--name", name, "--force"],
                "Destroy bootstraps",
            )
        )
    if _destroyable(load_elastic_state):
        steps.append(
            Step(["elastic", "destroy", "--name", name, "--force"], "Destroy Elasticsearch")
        )
    if _destroyable(load_monitoring_state):
        steps.append(
            Step(
                ["monitoring", "destroy", "--name", name, "--force"],
                "Destroy monitoring",
            )
        )
    return steps


def retained_observability(
    name: str,
    *,
    elastic_kept: bool | None = None,
    monitoring_kept: bool | None = None,
) -> list[dict]:
    """Components that exist in S3 state and will survive a teardown.

    Returned as ``[{"component", "kind", "endpoint", "destroy_command"}, ...]`` so
    ``down`` can tell the operator exactly what is still running (and still
    billing) and the one command that removes it. Empty when nothing was kept,
    which is the default.

    Deliberately separate from the step builders: keeping a stack means *no step
    is generated*, so without this the retained VMs would be invisible in the
    teardown output.

    ``elastic_kept`` / ``monitoring_kept`` override the stored flag with the
    manifest's own value, and MUST be passed on the ``down --config`` path. The
    two can legitimately disagree: a manifest edited to add ``keep_on_destroy``
    after ``up`` already ran leaves the stored flag false, yet ``down --config``
    generates no destroy step for it — so reading state alone would report
    nothing retained and then claim the deployment "has been destroyed" with the
    VM still up. Whichever source decided the destroy steps must also decide this
    report. ``None`` means "read it from state" (the name-only path, which has no
    manifest).
    """
    from saorsa_deploy.state import load_elastic_state, load_monitoring_state

    def _kept(loader, override: bool | None) -> dict | None:
        try:
            state = loader(name)
        except Exception:
            return None
        if state is None:
            return None
        kept = override if override is not None else bool(state.get("keep_on_destroy", False))
        return state if kept else None

    retained: list[dict] = []
    elastic = _kept(load_elastic_state, elastic_kept)
    if elastic is not None:
        retained.append(
            {
                "component": "Elasticsearch",
                "kind": "logs",
                "endpoint": elastic.get("elasticsearch_url") or elastic.get("vm_ip", ""),
                "info_command": f"saorsa-deploy elastic info --name {name}",
                "destroy_command": f"saorsa-deploy elastic destroy --name {name} --force",
            }
        )
    monitoring = _kept(load_monitoring_state, monitoring_kept)
    if monitoring is not None:
        retained.append(
            {
                "component": "InfluxDB",
                "kind": "metrics",
                "endpoint": monitoring.get("influxdb_url") or monitoring.get("vm_ip", ""),
                "info_command": f"saorsa-deploy monitoring info --name {name}",
                "destroy_command": f"saorsa-deploy monitoring destroy --name {name} --force",
            }
        )
    return retained
