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

__all__ = ["Step", "build_deploy_steps", "build_destroy_steps"]


def build_deploy_steps(manifest: Manifest) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        return build_testnet_deploy_steps(manifest)
    if kind == "testnet-extension":
        return build_extension_deploy_steps(manifest)
    from saorsa_deploy.orchestrator import prod

    return prod.build_deploy_steps(manifest)


def build_destroy_steps(manifest: Manifest) -> list[Step]:
    kind = manifest.kind
    if kind == "testnet":
        return build_testnet_destroy_steps(manifest)
    if kind == "testnet-extension":
        return build_extension_destroy_steps(manifest)
    from saorsa_deploy.orchestrator import prod

    return prod.build_destroy_steps(manifest)
