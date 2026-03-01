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
}

DEFAULT_PROVIDER = "digitalocean"


def resolve_regions(
    region_counts: str,
    testnet: bool,
    providers: dict[str, ProviderConfig] | None = None,
) -> list[tuple[str, str]]:
    """Resolve provider/region pairs based on arguments.

    Returns a list of (provider_name, region) tuples.

    If testnet is True, returns only the default provider with its default region.
    Otherwise, parses region_counts as comma-separated integers, one per provider,
    and selects that many regions from each provider's region list.
    """
    if providers is None:
        providers = PROVIDERS

    if testnet:
        default = providers[DEFAULT_PROVIDER]
        return [(default.name, default.default_region)]

    counts = [int(c.strip()) for c in region_counts.split(",")]
    provider_names = sorted(providers.keys())

    if len(counts) == 1:
        counts = counts * len(provider_names)
    elif len(counts) != len(provider_names):
        raise ValueError(
            f"Expected {len(provider_names)} region counts (one per provider), got {len(counts)}"
        )

    result = []
    for provider_name, count in zip(provider_names, counts):
        provider = providers[provider_name]
        available = provider.regions
        if count > len(available):
            raise ValueError(
                f"Requested {count} regions for {provider_name}, "
                f"but only {len(available)} are available"
            )
        for region in available[:count]:
            result.append((provider_name, region))

    return result
