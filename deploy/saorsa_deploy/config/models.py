"""Pydantic models for the declarative deployment manifest.

One ``Manifest`` describes a single deployment. The top-level envelope (name,
kind, purpose, ssh_key_path) is common to every kind; exactly one kind-specific
block must be present and must match ``kind``.

Models use ``extra="forbid"`` so a mistyped key is a loud error rather than a
silently-ignored field — important when the manifest is the sole source of truth
for a real cloud deployment.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# kind string (hyphenated, as stored in deploy_args["kind"]) -> manifest block attr
KIND_TO_BLOCK: dict[str, str] = {
    "testnet": "testnet",
    "testnet-extension": "extension",
    "prod-uploaders": "prod_uploaders",
    "prod-canary": "prod_canary",
    "prod-continuous-downloader": "prod_continuous_downloader",
    "prod-downloaders": "prod_downloaders",
    "prod-local-uploads": "prod_local_uploads",
    "prod-local-downloads": "prod_local_downloads",
}

Payment = Literal["merkle", "no-merkle"]
UpgradeChannel = Literal["beta", "stable"]


class _Base(BaseModel):
    # extra="forbid": a mistyped key is a loud error, not a silent no-op.
    # coerce_numbers_to_str: YAML parses `ant: 1000` / `eth: 0.1` as int/float, but
    # the funding fields are strings (passed through to the CLI verbatim).
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class Binary(_Base):
    """Binary source: a release ``version`` XOR a ``branch`` build.

    Mirrors the mutually-exclusive ``--<x>-version`` vs
    ``--branch-name``/``--repo-owner`` rule the CLI enforces per binary.
    """

    version: Optional[str] = None
    branch: Optional[str] = None
    repo_owner: Optional[str] = None
    validation: Optional[str] = None  # --validation-string (branch builds only)

    @model_validator(mode="after")
    def _exclusive(self) -> "Binary":
        if self.branch and self.version:
            raise ValueError("binary: set either `version` or `branch`, not both")
        if not self.branch and not self.version:
            raise ValueError("binary: one of `version` or `branch` is required")
        if self.repo_owner and not self.branch:
            raise ValueError("binary: `repo_owner` is only valid with `branch`")
        if self.validation and not self.branch:
            raise ValueError("binary: `validation` is only valid with `branch`")
        return self

    @property
    def is_branch(self) -> bool:
        return self.branch is not None


# --------------------------------------------------------------------------- #
# testnet
# --------------------------------------------------------------------------- #
class TestnetBinaries(_Base):
    ant_node: Binary
    ant: Optional[Binary] = None  # only required when clients are present


class Bootstraps(_Base):
    count: int = 1
    port: int = 10000
    regions_list: Optional[list[str]] = None  # bootstraps infra --do-regions-list
    attached_volume_size: Optional[int] = None


class ProviderNodes(_Base):
    """Per-provider node VM allocation. ``vm_count`` is PER REGION (matches CLI).

    Provider total = ``vm_count`` × number-of-regions. Supply regions either as
    an explicit ``regions_list`` or a ``regions`` count (not both). ``size`` maps
    to the provider's size flag (DO droplet-size / Hetzner server-type / Vultr plan).
    """

    vm_count: int
    regions: Optional[int] = None
    regions_list: Optional[list[str]] = None
    size: Optional[str] = None

    @model_validator(mode="after")
    def _regions_exclusive(self) -> "ProviderNodes":
        if self.regions is not None and self.regions_list is not None:
            raise ValueError("provider nodes: set either `regions` or `regions_list`, not both")
        return self


class Nodes(_Base):
    per_vm: int  # nodes provision --node-count
    nat_percent: Optional[int] = None
    attached_volume_size: Optional[int] = None
    providers: dict[str, ProviderNodes]

    @model_validator(mode="after")
    def _known_providers(self) -> "Nodes":
        allowed = {"digital_ocean", "hetzner", "vultr"}
        unknown = set(self.providers) - allowed
        if unknown:
            raise ValueError(
                f"nodes.providers: unknown provider(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            )
        if not self.providers:
            raise ValueError("nodes.providers: at least one provider is required")
        return self


class Uploader(_Base):
    provider: str
    region: str
    file_size_mb: int
    vm_size: Optional[str] = None
    payment: Optional[Payment] = None
    ant: Optional[str] = None  # wallet funding (ANT tokens)
    eth: Optional[str] = None  # wallet funding (ETH)


class Downloader(_Base):
    provider: str
    region: str
    vm_size: Optional[str] = None


class Clients(_Base):
    uploaders: list[Uploader] = []
    downloaders: list[Downloader] = []

    @model_validator(mode="after")
    def _both_roles(self) -> "Clients":
        # Downloaders fetch addresses that uploaders publish; one without the
        # other is pointless. Mirror the deploy-runbook skill's refusal.
        if (self.uploaders or self.downloaders) and not (self.uploaders and self.downloaders):
            raise ValueError(
                "clients: need at least one uploader AND one downloader "
                "(downloaders consume addresses uploaders publish)"
            )
        return self


class Elastic(_Base):
    enabled: bool = False
    droplet_size: Optional[str] = None
    volume_size: Optional[int] = None
    shards: Optional[int] = None
    retention_days: Optional[int] = None
    drop_log_levels: Optional[str] = None
    buffer_limit: Optional[int] = None


class Monitoring(_Base):
    # On by default — a testnet's client upload/download metrics need an InfluxDB
    # to land in, and `deployments print-uploads`/`record-results` read from it.
    # Opt out explicitly with `monitoring: { enabled: false }`.
    enabled: bool = True


class TestnetSpec(_Base):
    mode: bool = False
    upgrade_channel: Optional[UpgradeChannel] = None
    binaries: TestnetBinaries
    bootstraps: Bootstraps = Bootstraps()
    nodes: Nodes
    clients: Optional[Clients] = None
    elastic: Elastic = Elastic()
    monitoring: Monitoring = Monitoring()

    @model_validator(mode="after")
    def _ant_binary_when_clients(self) -> "TestnetSpec":
        if self.clients and self.binaries.ant is None:
            raise ValueError("testnet.binaries.ant is required when clients are configured")
        return self


class ExtensionSpec(_Base):
    """An extension testnet reuses another testnet's bootstraps (no bootstrap tier)."""

    source: str
    mode: bool = False
    upgrade_channel: Optional[UpgradeChannel] = None
    binaries: TestnetBinaries
    nodes: Nodes
    clients: Optional[Clients] = None
    elastic: Elastic = Elastic()
    monitoring: Monitoring = Monitoring()

    @model_validator(mode="after")
    def _ant_binary_when_clients(self) -> "ExtensionSpec":
        if self.clients and self.binaries.ant is None:
            raise ValueError("extension.binaries.ant is required when clients are configured")
        return self


# --------------------------------------------------------------------------- #
# prod kinds
# --------------------------------------------------------------------------- #
class ProdUploader(_Base):
    region: str
    vm_size: str
    urls: str  # path to URL list (uploaded with --public)
    payment: Optional[Payment] = None


class ProdUploadersSpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    monitoring_volume_size: Optional[int] = None
    keep_monitoring: Optional[bool] = None  # destroy: --keep-monitoring when True
    uploaders: list[ProdUploader]

    @model_validator(mode="after")
    def _at_least_one(self) -> "ProdUploadersSpec":
        if not self.uploaders:
            raise ValueError("prod_uploaders.uploaders: at least one is required")
        return self


class ProdDownloadersSpec(_Base):
    spec: str  # 'region[:vm_size]' (single client VM)
    binary: Binary
    addresses_path: Optional[str] = None
    monitoring_volume_size: Optional[int] = None
    keep_monitoring: Optional[bool] = None


class ProdContinuousDownloaderSpec(ProdDownloadersSpec):
    sleep_interval_secs: Optional[int] = None


class ProdProbe(_Base):
    region: str
    vm_size: Optional[str] = None
    count: int


class ProdCanarySpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    upload_spec: str  # 'REGION[:VM_SIZE]'
    probes: list[ProdProbe]
    urls_file: str
    upload_sleep_secs: Optional[int] = None
    probe_concurrency: Optional[int] = None
    monitoring_volume_size: Optional[int] = None
    keep_monitoring: Optional[bool] = None

    @model_validator(mode="after")
    def _at_least_one_probe(self) -> "ProdCanarySpec":
        if not self.probes:
            raise ValueError("prod_canary.probes: at least one probe is required")
        return self


class ProdLocalUploadsSpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    urls: Optional[str] = None
    local_dir: Optional[str] = None
    payment: Optional[Payment] = None
    verbose: int = 0

    @model_validator(mode="after")
    def _input_mode(self) -> "ProdLocalUploadsSpec":
        if bool(self.urls) == bool(self.local_dir):
            raise ValueError("prod_local_uploads: set exactly one of `urls` or `local_dir`")
        return self


class ProdLocalDownloadsSpec(_Base):
    binary: Binary
    addresses_path: str
    keep_output_dir: Optional[str] = None


# --------------------------------------------------------------------------- #
# top-level manifest
# --------------------------------------------------------------------------- #
class Manifest(_Base):
    name: str
    kind: str
    purpose: str
    ssh_key_path: str = "~/.ssh/id_rsa"

    testnet: Optional[TestnetSpec] = None
    extension: Optional[ExtensionSpec] = None
    prod_uploaders: Optional[ProdUploadersSpec] = None
    prod_canary: Optional[ProdCanarySpec] = None
    prod_continuous_downloader: Optional[ProdContinuousDownloaderSpec] = None
    prod_downloaders: Optional[ProdDownloadersSpec] = None
    prod_local_uploads: Optional[ProdLocalUploadsSpec] = None
    prod_local_downloads: Optional[ProdLocalDownloadsSpec] = None

    @model_validator(mode="after")
    def _kind_block_consistent(self) -> "Manifest":
        if self.kind not in KIND_TO_BLOCK:
            raise ValueError(f"unknown kind '{self.kind}'; valid: {sorted(KIND_TO_BLOCK)}")
        expected = KIND_TO_BLOCK[self.kind]
        present = [b for b in KIND_TO_BLOCK.values() if getattr(self, b) is not None]
        if expected not in present:
            raise ValueError(f"kind '{self.kind}' requires a top-level `{expected}:` block")
        extra = [b for b in present if b != expected]
        if extra:
            raise ValueError(
                f"kind '{self.kind}' must only carry `{expected}:`; also found: {extra}"
            )
        return self

    @property
    def spec(self) -> _Base:
        """The kind-specific block for this manifest."""
        return getattr(self, KIND_TO_BLOCK[self.kind])
