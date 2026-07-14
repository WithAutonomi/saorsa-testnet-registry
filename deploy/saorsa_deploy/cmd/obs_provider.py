"""Shared provider plumbing for the monitoring + elastic (observability) stacks.

Both stacks support DigitalOcean (the default), OVH, and Contabo. DigitalOcean
uses the ``digitalocean`` provider with ``droplet_size`` / ``volume_size``; OVH
is OpenStack-backed and needs the Public Cloud project id (``service_name``) plus
``flavor_name`` / ``attached_volume_size`` / ``volume_type``; Contabo uses the
``contabo`` provider with a ``product_id`` and — because it has no attachable
block storage — is root-disk-only (no volume). Centralising the per-provider
resource directory, tfstate-key abbreviation, default region, and default sizes
here keeps the infra and destroy commands in sync.
"""

import os
import sys

from rich.console import Console

OBS_PROVIDERS = ("digitalocean", "ovh", "contabo")

# tfstate key abbreviation per provider (DigitalOcean keeps the historical
# "do-" prefix so existing state keys are unchanged).
_ABBREV = {"digitalocean": "do", "ovh": "ovh", "contabo": "contabo"}

_DEFAULT_REGION = {"digitalocean": "lon1", "ovh": "GRA9", "contabo": "EU"}

# Per-provider, per-stack default VM size. The InfluxDB / Elasticsearch boxes
# want CPU+RAM; the Grafana box is tiny (it only renders panels). Contabo values
# are product ids (verify current ids via `cntb get products`).
_DEFAULT_INFLUXDB_SIZE = {"digitalocean": "s-4vcpu-8gb", "ovh": "b3-8", "contabo": "V45"}
_DEFAULT_GRAFANA_SIZE = {"digitalocean": "s-1vcpu-2gb", "ovh": "d2-4", "contabo": "V44"}
_DEFAULT_ELASTIC_SIZE = {"digitalocean": "s-4vcpu-8gb", "ovh": "b3-8", "contabo": "V45"}


def resolve_provider(args) -> str:
    provider = getattr(args, "provider", None) or "digitalocean"
    if provider not in OBS_PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider}'. Choose one of: "
            f"{', '.join(OBS_PROVIDERS)}."
        )
    return provider


def state_key_abbrev(provider: str) -> str:
    return _ABBREV[provider]


def default_region(provider: str) -> str:
    return _DEFAULT_REGION[provider]


def default_influxdb_size(provider: str) -> str:
    return _DEFAULT_INFLUXDB_SIZE[provider]


def default_grafana_size(provider: str) -> str:
    return _DEFAULT_GRAFANA_SIZE[provider]


def default_elastic_size(provider: str) -> str:
    return _DEFAULT_ELASTIC_SIZE[provider]


def require_ovh_service_name(console: Console) -> str:
    """Return the OVH Public Cloud project id, or exit with a clear error.

    Mirrors the env-var sourcing the `clients infra` flow uses for OVH.
    """
    service_name = os.environ.get("OVH_CLOUD_PROJECT_SERVICE")
    if not service_name:
        console.print(
            "[bold red]Error:[/bold red] provider 'ovh' requires the "
            "OVH_CLOUD_PROJECT_SERVICE environment variable (the OVH Public "
            "Cloud project ID) so the OVH manifest can target the right "
            "project. Export it (and the OVH_APPLICATION_KEY / "
            "OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY / OVH_ENDPOINT "
            "credentials) and retry."
        )
        sys.exit(1)
    return service_name
