"""Declarative deployment manifests.

A single YAML manifest describes one deployment's full desired state. The
orchestrator (``saorsa_deploy.orchestrator``) turns a validated manifest into the
ordered sequence of primitive ``saorsa-deploy`` commands, replacing the
hand-edited bash runbooks. See ``deploy/README.md`` for the format reference.
"""

from saorsa_deploy.config.loader import (
    ManifestError,
    load_manifest,
    merge_manifest_dict,
)
from saorsa_deploy.config.models import KIND_TO_BLOCK, Manifest

__all__ = [
    "Manifest",
    "KIND_TO_BLOCK",
    "ManifestError",
    "load_manifest",
    "merge_manifest_dict",
]
