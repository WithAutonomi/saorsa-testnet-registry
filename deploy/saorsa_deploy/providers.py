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
        regions=[
            "lon1", "nyc1", "ams3", "sfo3", "sgp1", "blr1", "fra1", "tor1", "syd1",
        ],
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
        # dfw is last because it has the VM plans but frequently lacks block
        # storage clusters (attached-volume creates fail with
        # "Unable to find a block storage cluster in that location").
        regions=[
            "ewr", "ord", "lax", "ams", "lhr", "fra",
            "sjc", "syd", "nrt", "cdg", "sgp", "dfw",
        ],
        default_region="ewr",
        tf_dir="vultr",
        state_key_prefix="saorsa-deploy/vl",
    ),
}

DEFAULT_PROVIDER = "digitalocean"


@dataclass
class ProviderSpec:
    """Per-provider deployment specification from CLI flags.

    ``regions_override``, when set, pins the exact regions to use — the
    default ordering is ignored and no fallback substitution happens.
    ``region_count`` is then implied by ``len(regions_override)``.
    """

    name: str
    vm_count: int
    region_count: int
    regions_override: list[str] | None = None


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
        if spec.regions_override is not None:
            unknown = [r for r in spec.regions_override if r not in provider.regions]
            if unknown:
                raise ValueError(
                    f"Unknown {spec.name} region(s): {', '.join(unknown)}. "
                    f"Known: {', '.join(provider.regions)}"
                )
            for region in spec.regions_override:
                result.append((provider.name, region))
            continue

        available = provider.regions
        if spec.region_count > len(available):
            raise ValueError(
                f"Requested {spec.region_count} regions for {spec.name}, "
                f"but only {len(available)} are available"
            )
        for region in available[: spec.region_count]:
            result.append((provider.name, region))

    return result
