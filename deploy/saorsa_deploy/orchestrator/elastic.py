"""Translate the ``elasticsearch`` manifest kind into a step list.

A standalone Elasticsearch deployment has no binary to build, no nodes and no
clients — it is just the observability stack, deployed for its own sake. So this
builder emits the two existing primitives (``elastic infra`` then ``elastic
provision``) rather than a single wrapper command.

Two steps, not one, is deliberate. Public ingest needs a DNS record pointing at
the instance *before* provisioning, because that is when the ACME certificate is
issued — and the address to point at does not exist until ``elastic infra`` has
run. Keeping them as separate journal entries means ``up`` stops at the
provision step with an actionable message, the operator edits DNS once, and
``up --resume`` continues without recreating the VM. Collapsing them into one
step would make the resume re-run infrastructure creation to get back to the
part that failed.
"""

from __future__ import annotations

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator.steps import Step


def _infra_step(manifest: Manifest) -> Step:
    spec = manifest.spec
    argv = [
        "elastic",
        "infra",
        "--name",
        manifest.name,
        # Marks this ES as a deployment in its own right, so it registers a
        # Deployment row. Omitted for a testnet's subordinate stack, whose row
        # is created by `bootstraps infra`.
        "--standalone",
        "--provider",
        spec.provider,
    ]
    if spec.region:
        argv += ["--region", spec.region]
    if spec.droplet_size:
        argv += ["--droplet-size", spec.droplet_size]
    if spec.volume_size is not None:
        argv += ["--volume-size", str(spec.volume_size)]
    if spec.volume_type:
        argv += ["--volume-type", spec.volume_type]
    if spec.public_ingest:
        # The firewall shape is decided at infra time: 443 open for ingest and
        # the bare 9200 rule dropped. Provisioning cannot retrofit that.
        argv.append("--public-ingest")
    if spec.reserved_ip:
        argv += ["--reserved-ip", spec.reserved_ip]
    if spec.keep_on_destroy:
        argv.append("--keep-on-destroy")
    if spec.contabo_image_id:
        argv += ["--contabo-image-id", spec.contabo_image_id]
    if spec.contabo_ssh_public_key_path:
        argv += [
            "--contabo-ssh-public-key-path",
            spec.contabo_ssh_public_key_path,
        ]
    return Step(argv, "Create Elasticsearch infrastructure")


def _provision_step(manifest: Manifest) -> Step:
    spec = manifest.spec
    argv = [
        "elastic",
        "provision",
        "--name",
        manifest.name,
        "--ssh-key-path",
        manifest.ssh_key_path,
        "--retention-days",
        str(spec.retention_days),
    ]
    if spec.shards is not None:
        argv += ["--shards", str(spec.shards)]
    if spec.public_ingest:
        argv += ["--public-ingest", "--ingest-hostname", spec.ingest_hostname]
        if spec.letsencrypt_staging:
            argv.append("--letsencrypt-staging")
    return Step(argv, "Provision Elasticsearch")


def build_deploy_steps(manifest: Manifest) -> list[Step]:
    return [_infra_step(manifest), _provision_step(manifest)]


def build_destroy_steps(manifest: Manifest) -> list[Step]:
    # `elastic destroy` prompts interactively without --force, which would hang
    # a non-TTY `down`.
    return [
        Step(
            ["elastic", "destroy", "--name", manifest.name, "--force"],
            "Destroy Elasticsearch",
        )
    ]
