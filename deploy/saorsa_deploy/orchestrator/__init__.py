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
]


def build_deploy_steps(manifest: Manifest) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        return build_testnet_deploy_steps(manifest)
    if kind == "testnet-extension":
        return build_extension_deploy_steps(manifest)
    from saorsa_deploy.orchestrator import prod

    return prod.build_deploy_steps(manifest)


def build_destroy_steps(
    manifest: Manifest, *, skip_drain_failures: bool = False
) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        return build_testnet_destroy_steps(
            manifest, skip_drain_failures=skip_drain_failures
        )
    if kind == "testnet-extension":
        return build_extension_destroy_steps(
            manifest, skip_drain_failures=skip_drain_failures
        )
    from saorsa_deploy.orchestrator import prod

    # prod-* kinds tear down via their own `prod <kind> destroy` command, which
    # has no --skip-drain-failures flag, so the flag does not apply there.
    return prod.build_destroy_steps(manifest)


def build_destroy_steps_from_state(
    name: str, *, skip_drain_failures: bool = False
) -> list[Step]:
    """Destroy steps derived from what actually exists in S3 state — for a
    name-only ``down`` (no manifest).

    Teardown is fundamentally name/state-driven, so this probes each component's
    state and emits the matching destroy in reverse order (clients → nodes →
    bootstraps → elastic → monitoring). It covers testnet/extension deployments;
    prod-* kinds are single-command deployments with no testnet state — use
    their ``prod <kind> destroy --name`` command (or ``down --config``).

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

    steps: list[Step] = []
    if _exists(load_client_state):
        clients_argv = ["clients", "destroy", "--name", name, "--force"]
        if skip_drain_failures:
            clients_argv.append("--skip-drain-failures")
        steps.append(Step(clients_argv, "Destroy clients"))
    if _exists(load_deployment_state):
        steps.append(
            Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes")
        )
        steps.append(
            Step(
                ["bootstraps", "destroy", "--name", name, "--force"],
                "Destroy bootstraps",
            )
        )
    if _exists(load_elastic_state):
        steps.append(
            Step(["elastic", "destroy", "--name", name, "--force"], "Destroy Elasticsearch")
        )
    if _exists(load_monitoring_state):
        steps.append(
            Step(
                ["monitoring", "destroy", "--name", name, "--force"],
                "Destroy monitoring",
            )
        )
    return steps
