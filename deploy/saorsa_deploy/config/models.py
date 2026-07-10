"""Pydantic models for the declarative deployment manifest.

One ``Manifest`` describes a single deployment. The top-level envelope (name,
kind, purpose, ssh_key_path) is common to every kind; exactly one kind-specific
block must be present and must match ``kind``.

Models use ``extra="forbid"`` so a mistyped key is a loud error rather than a
silently-ignored field — important when the manifest is the sole source of truth
for a real cloud deployment.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from saorsa_deploy.provisioning.clock_skew import parse_skew_duration


def _validate_clock_skew(value: str) -> str:
    """Validate a client ``clock_skew`` value as a Go-style signed duration.

    Stores the (normalized) string form; the resolved signed seconds are derived
    again at provisioning time. Raises if the string is not a valid duration.
    """
    parse_skew_duration(value)
    return str(value)


# A signed Go-style duration (e.g. "2h", "-2h", "30m"). `coerce_numbers_to_str`
# on `_Base` turns a bare YAML number into a string before this runs.
ClockSkew = Annotated[str, AfterValidator(_validate_clock_skew)]

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
    "prod-nodes": "prod_nodes",
}

Payment = Literal["merkle", "no-merkle"]
UpgradeChannel = Literal["beta", "stable"]

# ant log level → repeated `-v` verbosity. ant treats `-v`=info, `-vv`=debug,
# `-vvv`=trace; info is the established floor for every upload/download path, so
# it maps to a single `-v` (never "no flag", which would drop to warn/error).
LogLevel = Literal["info", "debug", "trace"]
DEFAULT_LOG_LEVEL: LogLevel = "info"
LOG_LEVEL_VERBOSITY: dict[str, int] = {"info": 1, "debug": 2, "trace": 3}
VALID_LOG_LEVELS: tuple[str, ...] = tuple(LOG_LEVEL_VERBOSITY)


def log_level_to_verbosity(level: str) -> int:
    """Map a log-level name to ant's repeated-`-v` count (info=1, …, trace=3)."""
    return LOG_LEVEL_VERBOSITY[level]


def log_level_to_v_flag(level: str) -> str:
    """Map a log-level name to ant's verbosity flag (e.g. 'debug' → '-vv')."""
    return "-" + ("v" * LOG_LEVEL_VERBOSITY[level])


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
    debug: Optional[bool] = None  # build without --release (branch builds only)

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
        if self.debug and not self.branch:
            raise ValueError(
                "binary: `debug` is only valid with `branch` "
                "(release versions ship prebuilt, optimised binaries)"
            )
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
    to the provider's size flag (DO droplet-size / Hetzner server-type / Vultr
    plan / OVH flavor).
    """

    vm_count: int
    regions: Optional[int] = None
    regions_list: Optional[list[str]] = None
    size: Optional[str] = None
    # GB of attached block-storage volume per VM for this provider. Per-provider
    # (not nodes-level) because providers differ: ovh_3az has NO block storage
    # (3-AZ regions), so setting it there is rejected — those nodes use the root
    # disk. Omit to fall back to the CLI's global --attached-volume-size default.
    attached_volume_size: Optional[int] = None

    @model_validator(mode="after")
    def _regions_exclusive(self) -> "ProviderNodes":
        if self.regions is not None and self.regions_list is not None:
            raise ValueError("provider nodes: set either `regions` or `regions_list`, not both")
        return self


class Nodes(_Base):
    per_vm: int  # nodes provision --node-count
    nat_percent: Optional[int] = None
    clock_skew: Optional[int] = None  # nodes provision --clock-skew-count (N random hosts)
    full_percent: Optional[int] = None  # nodes provision --full-percent (% of non-NAT VMs)
    full_headroom_mb: Optional[int] = None  # nodes provision --full-headroom-mb
    # DEPRECATED at this level — moved to per-provider (nodes.providers.<p>.
    # attached_volume_size). Kept only to emit a clear migration error; see the
    # validator below. Providers differ (ovh_3az has no block storage), so a
    # single nodes-level size is no longer meaningful.
    attached_volume_size: Optional[int] = None
    # Single log level for the node systemd services (nodes + genesis +
    # bootstraps), passed as ant-node `--log-level`. ant-node has NO env-filter
    # / per-module logging — its `--log-level` is bound to the RUST_LOG env var
    # and accepts ONE level only — so this is a single level (info/debug/trace),
    # distinct from the client-side `rust_log` (a full RUST_LOG env-filter).
    log_level: Optional[LogLevel] = None  # nodes/genesis/bootstraps provision --log-level
    providers: dict[str, ProviderNodes]

    @model_validator(mode="after")
    def _known_providers(self) -> "Nodes":
        allowed = {"digital_ocean", "hetzner", "vultr", "ovh", "ovh_3az"}
        unknown = set(self.providers) - allowed
        if unknown:
            raise ValueError(
                f"nodes.providers: unknown provider(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            )
        if not self.providers:
            raise ValueError("nodes.providers: at least one provider is required")
        return self

    @model_validator(mode="after")
    def _volume_size_placement(self) -> "Nodes":
        if self.attached_volume_size is not None:
            raise ValueError(
                "nodes.attached_volume_size has moved to per-provider: set it "
                "under nodes.providers.<provider>.attached_volume_size instead. "
                "It is not valid for ovh_3az (3-AZ regions have no block "
                "storage; those nodes use the root disk)."
            )
        cfg = self.providers.get("ovh_3az")
        if cfg is not None and cfg.attached_volume_size is not None:
            raise ValueError(
                "nodes.providers.ovh_3az: attached_volume_size is not supported "
                "— OVH 3-AZ regions have no block storage, so these nodes use "
                "the root disk. Remove attached_volume_size from ovh_3az."
            )
        return self


class Uploader(_Base):
    provider: str
    region: str
    file_size_mb: int
    vm_size: Optional[str] = None
    payment: Optional[Payment] = None
    ant: Optional[str] = None  # wallet funding (ANT tokens)
    eth: Optional[str] = None  # wallet funding (ETH)
    clock_skew: Optional[ClockSkew] = None  # signed duration skew on this VM (e.g. "2h", "-2h")
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap and ships a
    # bootstrap_peers.toml instead; False keeps the legacy --bootstrap invocation.
    bootstrap_cache: Optional[bool] = None
    # Forward this client's ant stdout+stderr to the deployment's Elasticsearch
    # instance (index telegraf-ant-clients-{name}). Requires elastic.enabled.
    forward_logs: Optional[bool] = None
    # RUST_LOG to set on this client's service; when set it OVERRIDES the `-v`
    # verbosity flag (the service drops `-v` and exports RUST_LOG instead).
    rust_log: Optional[str] = None


class Downloader(_Base):
    provider: str
    region: str
    vm_size: Optional[str] = None
    clock_skew: Optional[ClockSkew] = None  # signed duration skew on this VM (e.g. "2h", "-2h")
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap and ships a
    # bootstrap_peers.toml instead; False keeps the legacy --bootstrap invocation.
    bootstrap_cache: Optional[bool] = None
    # Forward this client's ant stdout+stderr to the deployment's Elasticsearch
    # instance (index telegraf-ant-clients-{name}). Requires elastic.enabled.
    forward_logs: Optional[bool] = None
    # RUST_LOG to set on this client's service; when set it OVERRIDES the `-v`
    # verbosity flag (the service drops `-v` and exports RUST_LOG instead).
    rust_log: Optional[str] = None


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


def _require_elastic_for_forward_logs(clients, elastic, kind: str) -> None:
    """Refuse `forward_logs` on a testnet/extension client unless elastic is on.

    Client logs forward to the deployment's OWN Elasticsearch instance (a
    distinct index from the node logs), so there must be one to forward to.
    """
    if clients is None or (elastic is not None and elastic.enabled):
        return
    wants = [c for c in (clients.uploaders + clients.downloaders) if c.forward_logs]
    if wants:
        raise ValueError(
            f"{kind}: forward_logs requires elastic.enabled: true "
            "(client logs forward to the deployment's Elasticsearch instance)"
        )


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
    # The separate Grafana VM is opt-in (off by default — InfluxDB only). Enable
    # dashboards with `monitoring: { enabled: true, grafana: true }`.
    grafana: bool = False


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

    @model_validator(mode="after")
    def _forward_logs_needs_elastic(self) -> "TestnetSpec":
        _require_elastic_for_forward_logs(self.clients, self.elastic, "testnet")
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

    @model_validator(mode="after")
    def _forward_logs_needs_elastic(self) -> "ExtensionSpec":
        _require_elastic_for_forward_logs(self.clients, self.elastic, "extension")
        return self


# --------------------------------------------------------------------------- #
# prod kinds
# --------------------------------------------------------------------------- #
class ProdUploader(_Base):
    region: str
    vm_size: str
    urls: str  # path to URL list (uploaded with --public)
    payment: Optional[Payment] = None
    # ant log verbosity for this VM's uploads; None omits the token (defaults
    # to info on the VM). info=-v, debug=-vv, trace=-vvv.
    log_level: Optional[LogLevel] = None
    # signed duration skew on this VM (e.g. "2h", "-2h"); disables time sync (testing only)
    clock_skew: Optional[ClockSkew] = None
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap and ships a
    # bootstrap_peers.toml instead; False keeps the legacy --bootstrap invocation.
    bootstrap_cache: Optional[bool] = None
    # Forward this uploader's ant stdout+stderr to a dedicated Elasticsearch
    # instance stood up by the deploy command (index telegraf-ant-clients-{name}).
    forward_logs: Optional[bool] = None
    # Wrap this uploader's ant invocation with heaptrack (installs the heaptrack
    # apt package); profiles → /var/log/ant-heaptrack/ on the VM.
    memory_profiling: Optional[bool] = None
    # RUST_LOG to set on this uploader's service; OVERRIDES the verbosity flag.
    rust_log: Optional[str] = None


class ProdUploadersSpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    monitoring_volume_size: Optional[int] = None
    keep_monitoring: Optional[bool] = None  # destroy: --keep-monitoring when True
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True
    # deploy: --allow-duplicate-files — upload files already in the prod list
    allow_duplicate_files: bool = False
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
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True
    # signed duration skew on this VM (e.g. "2h", "-2h"); disables time sync (testing only)
    clock_skew: Optional[ClockSkew] = None
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap and ships a
    # bootstrap_peers.toml instead; False keeps the legacy --bootstrap invocation.
    bootstrap_cache: Optional[bool] = None
    # Forward the downloader's ant stdout+stderr to a dedicated Elasticsearch
    # instance stood up by the deploy command (index telegraf-ant-clients-{name}).
    forward_logs: Optional[bool] = None
    # Wrap the downloader's ant invocation with heaptrack (installs the heaptrack
    # apt package); profiles → /var/log/ant-heaptrack/ on the VM.
    memory_profiling: Optional[bool] = None
    # RUST_LOG to set on the downloader service; OVERRIDES the verbosity flag.
    rust_log: Optional[str] = None


class ProdContinuousDownloaderSpec(ProdDownloadersSpec):
    sleep_interval_secs: Optional[int] = None
    # Cloud provider for the client VM (digitalocean | hetzner | vultr | ovh;
    # default digitalocean). Non-DO is cheaper for a permanent run and cuts
    # pooled DigitalOcean egress. Needs the provider's creds in the env.
    provider: Optional[str] = None
    # Cloud provider for the paired monitoring VM (digitalocean | ovh; default
    # digitalocean). Only those two have monitoring Terraform modules.
    monitoring_provider: Optional[str] = None
    # Monitoring VM overrides (default lon1 / s-2vcpu-4gb on DO; provider
    # defaults on OVH). monitoring_volume_type is OVH-only.
    monitoring_region: Optional[str] = None
    monitoring_droplet_size: Optional[str] = None
    monitoring_volume_type: Optional[str] = None
    # InfluxDB bucket retention in days (default 7); 0 = infinite. For a
    # permanent run, periodically archive into Postgres (`deployments archive`).
    influx_retention_days: Optional[int] = None
    # Per-op download-log retention in days before the daily prune timer
    # (default 7); 0 = keep forever.
    log_retention_days: Optional[int] = None


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
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True
    # ant peer cache (upload + probe VMs): True (default) invokes ant WITHOUT
    # --bootstrap and ships a bootstrap_peers.toml instead.
    bootstrap_cache: Optional[bool] = None
    # Forward the canary upload + probe VMs' ant stdout+stderr to a dedicated
    # Elasticsearch instance stood up by the deploy command (index
    # telegraf-ant-clients-{name}).
    forward_logs: Optional[bool] = None
    # RUST_LOG to set on the canary services; OVERRIDES the verbosity flag.
    rust_log: Optional[str] = None

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
    # ant log verbosity (info=default, debug, trace). The documented knob.
    log_level: Optional[LogLevel] = None
    # DEPRECATED alias for log_level (a raw `-v` repeat count). Retained for
    # backwards compatibility; log_level wins when both are set.
    verbose: int = 0
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap; the
    # operator's ~/.config/ant/bootstrap_peers.toml is written only if missing.
    bootstrap_cache: Optional[bool] = None

    @model_validator(mode="after")
    def _input_mode(self) -> "ProdLocalUploadsSpec":
        if bool(self.urls) == bool(self.local_dir):
            raise ValueError("prod_local_uploads: set exactly one of `urls` or `local_dir`")
        return self


class ProdLocalDownloadsSpec(_Base):
    binary: Binary
    addresses_path: str
    keep_output_dir: Optional[str] = None
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap; the
    # operator's ~/.config/ant/bootstrap_peers.toml is written only if missing.
    bootstrap_cache: Optional[bool] = None


class ProdNodesSpec(_Base):
    """Profiling ant-node VMs that join the PRODUCTION network under heaptrack.

    ``binary`` is an ANT-NODE source (release version XOR branch build). For a
    profiling run set ``binary.branch`` + ``binary.debug: true`` so the
    orchestrator's ``build ant-node`` step builds without ``--release`` (full
    debug info). All other fields mirror ``prod nodes deploy`` flags; ``None``
    means "use the CLI default".
    """

    binary: Binary
    region: str
    provider: Optional[str] = None  # digitalocean (default) | hetzner | vultr | ovh
    vm_size: Optional[str] = None
    vm_count: Optional[int] = None  # default 1
    node_count: Optional[int] = None  # default 15 (prod fleet density)
    volume_size: Optional[int] = None  # GB attached data volume; default 100
    # heaptrack defaults ON for this kind — set false to run plain prod nodes.
    heaptrack: Optional[bool] = None
    monitoring: Optional[bool] = None  # InfluxDB host metrics; default off
    grafana: Optional[bool] = None  # with monitoring, also a Grafana VM
    monitoring_volume_size: Optional[int] = None
    log_retention_days: Optional[int] = None  # heaptrack-trace prune; 0 = keep
    rewards_address: Optional[str] = None
    log_level: Optional[LogLevel] = None  # ant-node single global level
    port: Optional[int] = None  # initial listen port (default 10000)
    keep_monitoring: Optional[bool] = None  # destroy: --keep-monitoring when True
    # Dedicated per-deployment Elasticsearch for node log forwarding. `enabled:
    # true` stands up a small ES VM named after this deployment and forwards the
    # node JSON logs to it (index telegraf-ant-nodes-<name>), so debug-build
    # profiling nodes don't flood the always-on `ant-prod-01` ES. Reuses the same
    # Elastic model as testnet/extension; volume_size/retention_days/droplet_size/
    # shards size it, drop_log_levels overrides the log_level-derived ES filter.
    elastic: Elastic = Elastic()
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True


# --------------------------------------------------------------------------- #
# top-level manifest
# --------------------------------------------------------------------------- #
class Manifest(_Base):
    name: str
    kind: str
    purpose: str
    ssh_key_path: str = "~/.ssh/id_rsa"
    slack_post: bool = False  # append a final `deployments slack-post` step to `up`

    testnet: Optional[TestnetSpec] = None
    extension: Optional[ExtensionSpec] = None
    prod_uploaders: Optional[ProdUploadersSpec] = None
    prod_canary: Optional[ProdCanarySpec] = None
    prod_continuous_downloader: Optional[ProdContinuousDownloaderSpec] = None
    prod_downloaders: Optional[ProdDownloadersSpec] = None
    prod_local_uploads: Optional[ProdLocalUploadsSpec] = None
    prod_local_downloads: Optional[ProdLocalDownloadsSpec] = None
    prod_nodes: Optional[ProdNodesSpec] = None

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
