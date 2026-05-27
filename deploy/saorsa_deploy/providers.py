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
    "ovh": ProviderConfig(
        name="ovh",
        # Instance-capable OVH regions today: 8 EU + 1 NA. APAC regions
        # (BOM/SGP/SYD) require per-account activation; add them here once
        # the project has them enabled.
        regions=[
            "GRA9", "SBG5", "DE1", "UK1", "WAW1",
            "EU-WEST-PAR", "EU-SOUTH-MIL", "RBX-A",
            "BHS5",
        ],
        default_region="GRA9",
        tf_dir="ovh",
        state_key_prefix="saorsa-deploy/ovh",
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


@dataclass(frozen=True)
class ClientProviderInfo:
    """Per-provider configuration for client (uploader/downloader) VMs.

    Client VMs are single-VM, storage-less variants of the node manifests.
    Each provider ships its own Terraform manifest dir (``tf_dir``) and names
    the VM-size Terraform variable differently (``size_var``): DigitalOcean
    ``droplet_size``, Hetzner ``server_type``, Vultr ``plan``, OVH
    ``flavor_name``. ``tf_resource_type`` / ``tf_resource_name`` /
    ``tf_size_attr`` are used to recover a VM's size from orphan tfstate when
    client state is missing.
    """

    name: str
    tf_dir: str
    size_var: str
    state_key_prefix: str
    tf_resource_type: str
    tf_resource_name: str
    tf_size_attr: str


CLIENT_PROVIDERS: dict[str, ClientProviderInfo] = {
    "digitalocean": ClientProviderInfo(
        name="digitalocean",
        tf_dir="digitalocean-client",
        size_var="droplet_size",
        state_key_prefix="saorsa-deploy/do-client",
        tf_resource_type="digitalocean_droplet",
        tf_resource_name="client_vm",
        tf_size_attr="size",
    ),
    "vultr": ClientProviderInfo(
        name="vultr",
        tf_dir="vultr-client",
        size_var="plan",
        state_key_prefix="saorsa-deploy/vl-client",
        tf_resource_type="vultr_instance",
        tf_resource_name="client_vm",
        tf_size_attr="plan",
    ),
    "hetzner": ClientProviderInfo(
        name="hetzner",
        tf_dir="hetzner-client",
        size_var="server_type",
        state_key_prefix="saorsa-deploy/hz-client",
        tf_resource_type="hcloud_server",
        tf_resource_name="client_vm",
        tf_size_attr="server_type",
    ),
    "ovh": ClientProviderInfo(
        name="ovh",
        tf_dir="ovh-client",
        size_var="flavor_name",
        state_key_prefix="saorsa-deploy/ovh-client",
        tf_resource_type="ovh_cloud_project_instance",
        tf_resource_name="client_vm",
        # flavor is stored as a flavor_id UUID, not the name; recovery falls
        # back to the manifest default for OVH.
        tf_size_attr="",
    ),
}

# CLI provider tokens accepted in --uploader/--downloader specs, mapped to the
# internal provider name. 'digital_ocean' is the documented client-spec
# spelling; the internal/node-side name is 'digitalocean'.
CLIENT_PROVIDER_ALIASES: dict[str, str] = {
    "digital_ocean": "digitalocean",
    "digitalocean": "digitalocean",
    "vultr": "vultr",
    "hetzner": "hetzner",
    "ovh": "ovh",
}


def resolve_client_provider(token: str) -> ClientProviderInfo:
    """Map a client-spec provider token to its :class:`ClientProviderInfo`.

    Accepts the documented CLI spellings (e.g. ``digital_ocean``) and the
    internal names. Raises ``ValueError`` on an unknown or empty provider.
    """
    internal = CLIENT_PROVIDER_ALIASES.get(token)
    if internal is None:
        valid = ", ".join(sorted(CLIENT_PROVIDER_ALIASES))
        raise ValueError(
            f"Unknown provider '{token}'. Valid providers: {valid}."
        )
    return CLIENT_PROVIDERS[internal]
