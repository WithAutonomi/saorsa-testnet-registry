from dataclasses import dataclass


@dataclass
class ProviderConfig:
    name: str
    regions: list[str]
    default_region: str
    tf_dir: str
    state_key_prefix: str


PROVIDERS = {
    "digitalocean": ProviderConfig(
        name="digitalocean",
        regions=["lon1", "nyc1", "ams3", "sfo3", "sgp1", "blr1", "fra1", "tor1"],
        default_region="lon1",
        tf_dir="digitalocean",
        state_key_prefix="saorsa-deploy/do",
    ),
    "hetzner": ProviderConfig(
        name="hetzner",
        regions=["fsn1", "nbg1", "hel1", "sin"],
        default_region="fsn1",
        tf_dir="hetzner",
        state_key_prefix="saorsa-deploy/hz",
    ),
    "vultr": ProviderConfig(
        name="vultr",
        regions=[
            "ewr", "ord", "dfw", "lax", "ams", "lhr",
            "fra", "sjc", "syd", "nrt", "cdg", "sgp",
        ],
        default_region="ewr",
        tf_dir="vultr",
        state_key_prefix="saorsa-deploy/vl",
    ),
}

DEFAULT_PROVIDER = "digitalocean"


@dataclass
class ProviderSpec:
    """Per-provider deployment specification from CLI flags."""

    name: str
    vm_count: int
    region_count: int


def resolve_regions(
    provider_specs: list[ProviderSpec],
    providers: dict[str, ProviderConfig] | None = None,
) -> list[tuple[str, str]]:
    """Resolve provider/region pairs from per-provider specs.

    Returns a list of (provider_name, region) tuples.

    Only providers with vm_count > 0 are included.
    """
    if providers is None:
        providers = PROVIDERS

    result = []
    for spec in provider_specs:
        if spec.vm_count <= 0:
            continue
        provider = providers[spec.name]
        available = provider.regions
        if spec.region_count > len(available):
            raise ValueError(
                f"Requested {spec.region_count} regions for {spec.name}, "
                f"but only {len(available)} are available"
            )
        for region in available[: spec.region_count]:
            result.append((provider.name, region))

    return result
