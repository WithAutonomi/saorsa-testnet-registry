"""Pydantic models for the declarative deployment manifest.

One ``Manifest`` describes a single deployment. The top-level envelope (name,
kind, purpose, ssh_key_path) is common to every kind; exactly one kind-specific
block must be present and must match ``kind``.

Models use ``extra="forbid"`` so a mistyped key is a loud error rather than a
silently-ignored field — important when the manifest is the sole source of truth
for a real cloud deployment.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from saorsa_deploy.provisioning.clock_skew import parse_skew_duration
from saorsa_deploy.provisioning.network_profile import resolve_network


def _validate_network(value: Union[str, dict]) -> Union[str, dict]:
    """Validate a client ``network`` value — a profile name or an inline mapping.

    Resolution runs here purely to fail fast: an unknown profile name or a
    malformed inline definition is an error at ``config validate`` time rather than
    half-way through a deploy that has already created infrastructure. The value is
    stored unresolved and resolved again at provisioning time, exactly as
    ``clock_skew`` stores its string form.
    """
    resolve_network(value)
    return value


# Either a built-in profile name ("home-fttc") or an inline mapping of the same
# fields for an ad-hoc condition. Omitting it entirely means unshaped.
Network = Annotated[Union[str, dict], AfterValidator(_validate_network)]


def _validate_pool_size(value: Union[str, int]) -> Union[str, int]:
    """Validate a ``pool_min_size``/``pool_max_size`` value at manifest time.

    Parsed here only to fail fast — the value is stored in its written form and
    re-parsed at selection time, exactly as ``clock_skew`` and ``network`` do.
    """
    from saorsa_db.size_class import parse_size

    parse_size(value)
    return value


# A byte size written the way an operator writes it: "5MB", "1.5GB", or a bare
# number of bytes. Binary units, matching how sizes are displayed.
PoolSize = Annotated[Union[str, int], AfterValidator(_validate_pool_size)]


def _check_pool_bounds(spec, label: str, pool_field: str = "pool") -> None:
    """Validate ``pool_min_size``/``pool_max_size`` against their ``pool``.

    Three failures, all of which would otherwise surface only after the deploy
    had started: bounds with no pool request (which would silently do nothing),
    an inverted pair, and bounds that exclude a class the request asks for.
    """
    from saorsa_db.size_class import bounded_byte_range, describe_bounds, parse_size
    from saorsa_db.size_class import parse as parse_size_class

    minimum = getattr(spec, "pool_min_size", None)
    maximum = getattr(spec, "pool_max_size", None)
    if minimum is None and maximum is None:
        return

    pool = getattr(spec, pool_field, None)
    if not pool:
        raise ValueError(
            f"{label}: pool_min_size/pool_max_size narrow a `{pool_field}` "
            f"request; they have no effect without one"
        )

    min_bytes = parse_size(minimum) if minimum is not None else None
    max_bytes = parse_size(maximum) if maximum is not None else None
    if max_bytes == 0:
        raise ValueError(f"{label}: pool_max_size of 0 selects nothing")
    if min_bytes is not None and max_bytes is not None and min_bytes >= max_bytes:
        raise ValueError(
            f"{label}: pool_min_size must be below pool_max_size, which is "
            f"exclusive"
        )

    for name, count in pool.items():
        if not count:
            continue
        try:
            size_class = parse_size_class(str(name))
        except ValueError:
            # An unknown class name is reported by the request parser itself.
            continue
        if bounded_byte_range(size_class, min_bytes, max_bytes) is None:
            raise ValueError(
                f"{label}: the size bounds "
                f"({describe_bounds(min_bytes, max_bytes)}) exclude the "
                f"'{size_class.value}' class entirely, but the request asks "
                f"for {count} of them"
            )


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
    "prod-continuous-uploader": "prod_continuous_uploader",
    "prod-downloaders": "prod_downloaders",
    "prod-local-uploads": "prod_local_uploads",
    "prod-local-downloads": "prod_local_downloads",
    "prod-nodes": "prod_nodes",
    "prod-debug-nodes": "prod_debug_nodes",
    "elasticsearch": "elasticsearch",
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
    # Simulated residential link conditions on this VM: a built-in profile name
    # ("home-fttc") or an inline mapping. Omit for an unshaped cloud connection.
    # NB shaped clients make a run's transfer percentiles incomparable to an
    # unshaped baseline — do not enable on a staging gate run.
    network: Optional[Network] = None
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
    # Simulated residential link conditions on this VM — see Uploader.network.
    # The downstream cap is what matters for a downloader.
    network: Optional[Network] = None
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
    # Survive `deployments down`: the testnet tiers (clients/nodes/bootstraps) are
    # torn down but this VM is left standing so the run's logs stay queryable for
    # a post-mortem. Default false — teardown destroys everything, as before.
    # NOTE retention is deliberately NOT coupled to this: the index still expires
    # on the ILM policy (`retention_days`, default 7), so set that explicitly if
    # the investigation needs longer.
    keep_on_destroy: bool = False

    @model_validator(mode="after")
    def _keep_needs_enabled(self) -> "Elastic":
        # enabled defaults False here, so `elastic: {keep_on_destroy: true}` on its
        # own would silently keep nothing at all. Fail loudly instead.
        if self.keep_on_destroy and not self.enabled:
            raise ValueError(
                "elastic.keep_on_destroy requires elastic.enabled: true "
                "(there is no Elasticsearch stack to keep otherwise)"
            )
        return self


class Monitoring(_Base):
    # On by default — a testnet's client upload/download metrics need an InfluxDB
    # to land in, and `deployments print-uploads`/`record-results` read from it.
    # Opt out explicitly with `monitoring: { enabled: false }`.
    enabled: bool = True
    # The separate Grafana VM is opt-in (off by default — InfluxDB only). Enable
    # dashboards with `monitoring: { enabled: true, grafana: true }`.
    grafana: bool = False
    # Survive `deployments down` — see Elastic.keep_on_destroy. The InfluxDB
    # bucket's own retention defaults to infinite for testnets, so kept metrics
    # stay queryable indefinitely.
    keep_on_destroy: bool = False

    @model_validator(mode="after")
    def _keep_needs_enabled(self) -> "Monitoring":
        if self.keep_on_destroy and not self.enabled:
            raise ValueError(
                "monitoring.keep_on_destroy requires monitoring.enabled: true "
                "(there is no monitoring stack to keep otherwise)"
            )
        return self


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
    # Exactly one file source. `urls` is the legacy hand-written list, where
    # "not already uploaded" is only checked by a name-based preflight.
    # `pool` is a {size_class: count} request against the managed file pool,
    # which cannot return a file that has ever been handed out.
    urls: Optional[str] = None  # path to URL list (uploaded with --public)
    pool: Optional[dict[str, int]] = None
    # Byte bounds narrowing every class in `pool`: min inclusive, max exclusive,
    # counts unchanged. "5MB" / 5242880 are both accepted.
    pool_min_size: Optional[PoolSize] = None
    pool_max_size: Optional[PoolSize] = None
    # Draw only files with no chunks already on the network. A partially
    # uploaded file has ant skip what is stored and send only the remainder, so
    # a run testing whether an upload can be sustained over a slow link would
    # not be testing that at all.
    exclude_partial: bool = False
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
    # Simulated residential link conditions on this uploader VM: a built-in profile
    # name ("home-calibrated") or an inline mapping. This is the primary use case —
    # a cloud VM's ~gigabit uplink is not how real users upload.
    network: Optional[Network] = None

    @model_validator(mode="after")
    def _one_file_source(self) -> "ProdUploader":
        if bool(self.urls) == bool(self.pool):
            raise ValueError(
                "prod_uploaders.uploaders[]: set exactly one of `urls` or `pool`"
            )
        _check_pool_bounds(self, "prod_uploaders.uploaders[]")
        return self


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
    # Simulated residential link conditions on the client VM: a built-in profile
    # name ("home-calibrated") or an inline mapping. Inherited by
    # ProdContinuousDownloaderSpec below, so it covers both download kinds.
    network: Optional[Network] = None
    # Write one JSONL record per chunk-fetch attempt next to the per-op
    # stdout/stderr logs (`ant file download --download-diagnostics`). Default
    # off: the flag only exists on instrumented `ant` builds, so a stock binary
    # would reject it. One file per download — the writer truncates on open.
    download_diagnostics: Optional[bool] = None
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


class ProdContinuousDownloaderSpec(ProdDownloadersSpec):
    sleep_interval_secs: Optional[int] = None
    # InfluxDB bucket retention in days (default 7); 0 = infinite. For a
    # permanent run, periodically archive into Postgres (`deployments archive`).
    influx_retention_days: Optional[int] = None
    # Per-op download-log retention in days before the daily prune timer
    # (default 7); 0 = keep forever.
    log_retention_days: Optional[int] = None


class ProdUploadVerifier(_Base):
    """One region that independently reads back what the uploader wrote.

    Each verifier checks *every* address, deliberately — the point is a second
    region confirming the same file, not dividing the work up. Provider is
    per-entry so the vantage points can differ by cloud as well as by region.
    """

    region: str
    vm_size: Optional[str] = None
    provider: Optional[str] = None
    # Must exceed the largest file the pool request can draw: verification
    # downloads the file before hashing it.
    volume_size: Optional[int] = None


class ProdContinuousUploaderSpec(_Base):
    """Daily production write test (V2-837).

    One uploader VM draws files from the pool once a day, uploads them to the
    production network and reads each back; the verifiers confirm them again
    from other regions. Unlike every other pool consumer, files are claimed
    *by the VM as each upload happens* rather than up front, so the run is
    open-ended and nothing is reserved in advance.
    """

    spec: str  # 'region[:vm_size]' — the single uploader VM
    binary: Binary
    verifiers: list[ProdUploadVerifier] = Field(default_factory=list)

    # What to upload each cycle: a {size_class: count} request against the
    # managed pool. Not fixed at 2 tiny + 1 small — a later run can point this
    # at larger files without a code change.
    pool: dict[str, int]
    pool_min_size: Optional[PoolSize] = None  # inclusive
    pool_max_size: Optional[PoolSize] = None  # exclusive

    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    # Env var naming the SCOPED registry credential the VMs use to claim pool
    # files (see `prod pool client-role`). Named, never inlined — same rule as
    # the wallet key.
    pool_database_url_env: str = "POOL_DATABASE_URL"

    # Seconds to sleep at the end of each upload cycle. 86400 = once a day.
    sleep_interval_secs: Optional[int] = None
    # How often a verifier looks for addresses it has not yet checked.
    verify_interval_secs: Optional[int] = None

    # Disk on the uploader VM. Must exceed the largest file the pool request
    # can draw — the source file is written to disk before upload.
    volume_size: Optional[int] = None

    provider: Optional[str] = None
    monitoring_provider: Optional[str] = None
    monitoring_region: Optional[str] = None
    monitoring_droplet_size: Optional[str] = None
    monitoring_volume_type: Optional[str] = None
    monitoring_volume_size: Optional[int] = None
    grafana: Optional[bool] = None
    keep_monitoring: Optional[bool] = None

    influx_retention_days: Optional[int] = None
    log_retention_days: Optional[int] = None

    payment: Optional[Payment] = None
    log_level: Optional[LogLevel] = None
    bootstrap_cache: Optional[bool] = None
    rust_log: Optional[str] = None
    network: Optional[Network] = None

    @model_validator(mode="after")
    def _check(self):
        if not self.pool:
            raise ValueError(
                "prod_continuous_uploader.pool must request at least one size "
                "class, e.g. {tiny: 2, small: 1}"
            )
        _check_pool_bounds(self, "prod_continuous_uploader")
        if not self.verifiers:
            raise ValueError(
                "prod_continuous_uploader.verifiers must list at least one "
                "region. The whole point of the test is reading the upload "
                "back; without a verifier it only proves the upload command "
                "exited zero."
            )
        _check_upload_disk(self)
        return self


def _check_upload_disk(spec: "ProdContinuousUploaderSpec") -> None:
    """Refuse a pool request that cannot fit on the configured disks.

    Both the uploader and every verifier write the file to local disk — one
    before uploading, the others before hashing. A request that can draw a 5
    GiB file onto a 25 GB root disk fills the filesystem and fails in a way
    that reads as a network fault at 3am, so it is refused at manifest-validate
    time instead.

    Only the *upper* bound of the largest requested class is checked, since
    that is the worst case a draw can produce.
    """
    from saorsa_db.size_class import byte_range, parse, parse_size

    largest_upper = 0
    unbounded_class = None
    for class_name, count in spec.pool.items():
        if not count:
            continue
        size_class = parse(class_name)
        _, upper = byte_range(size_class)
        if upper is None:
            unbounded_class = size_class
            continue
        largest_upper = max(largest_upper, upper)

    # An explicit pool_max_size caps the draw regardless of class.
    if spec.pool_max_size is not None:
        capped = parse_size(spec.pool_max_size)
        if unbounded_class is not None:
            unbounded_class = None
            largest_upper = max(largest_upper, capped)
        else:
            largest_upper = min(largest_upper, capped) if largest_upper else capped

    if unbounded_class is not None and spec.volume_size is None:
        raise ValueError(
            f"prod_continuous_uploader.pool requests {unbounded_class.value}, "
            "which has no upper size bound. Set an explicit volume_size (and "
            "ideally a pool_max_size) so the disk needed is knowable."
        )
    if not largest_upper:
        return

    # Headroom for the OS, the ant binary, per-op logs and the fact that a
    # verifier holds its download while hashing it.
    needed_gb = max(10, int(largest_upper / (1024**3) * 2) + 8)

    def _too_small(configured: Optional[int]) -> bool:
        return configured is not None and configured < needed_gb

    if _too_small(spec.volume_size):
        raise ValueError(
            f"prod_continuous_uploader.volume_size={spec.volume_size} GB is "
            f"too small for this pool request, which can draw a file of up to "
            f"{largest_upper / (1024**3):.2f} GiB. Use at least {needed_gb} GB, "
            "or narrow the request with pool_max_size."
        )
    for index, verifier in enumerate(spec.verifiers):
        if _too_small(verifier.volume_size):
            raise ValueError(
                f"prod_continuous_uploader.verifiers[{index}].volume_size="
                f"{verifier.volume_size} GB is too small for this pool "
                f"request (up to {largest_upper / (1024**3):.2f} GiB per file). "
                f"Use at least {needed_gb} GB — a verifier downloads the whole "
                "file before hashing it."
            )


class ProdProbe(_Base):
    region: str
    vm_size: Optional[str] = None
    count: int


class ProdCanarySpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    upload_spec: str  # 'REGION[:VM_SIZE]'
    probes: list[ProdProbe]
    # Exactly one file source. `pool` is a {size_class: count} request against
    # the managed pool; a canary consumes ~350 distinct files per run and they
    # must never have been uploaded before, or the loss measurement is void.
    urls_file: Optional[str] = None
    pool: Optional[dict[str, int]] = None
    # Byte bounds narrowing every class in `pool`. The canary's reason for
    # wanting them: the chunk floor is 4, so a 14 KB file buys the same minimum
    # loss observation as a 5 MB one, at the same cost of one pool entry.
    pool_min_size: Optional[PoolSize] = None
    pool_max_size: Optional[PoolSize] = None
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
        if bool(self.urls_file) == bool(self.pool):
            raise ValueError(
                "prod_canary: set exactly one of `urls_file` or `pool`"
            )
        _check_pool_bounds(self, "prod_canary")
        return self


class ProdLocalUploadsSpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    urls: Optional[str] = None
    local_dir: Optional[str] = None
    # {size_class: count} against the managed pool. LOCAL entries only — this
    # kind uploads from the operator's disk, so a URL entry is meaningless here.
    pool: Optional[dict[str, int]] = None
    # Byte bounds narrowing every class in `pool`: min inclusive, max exclusive.
    pool_min_size: Optional[PoolSize] = None
    pool_max_size: Optional[PoolSize] = None
    # Draw only files with no chunks already on the network — see the note on
    # ProdUploader.exclude_partial.
    exclude_partial: bool = False
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
        chosen = sum(1 for value in (self.urls, self.local_dir, self.pool) if value)
        if chosen != 1:
            raise ValueError(
                "prod_local_uploads: set exactly one of `urls`, `local_dir` or `pool`"
            )
        _check_pool_bounds(self, "prod_local_uploads")
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


class ProdDebugNodesSpec(_Base):
    """Permanent crash-debug ant-node VMs joining the PRODUCTION network.

    Sibling of ``ProdNodesSpec`` but for panic/crash debugging rather than
    heaptrack memory profiling: RUST_BACKTRACE on every service, core dumps, a
    single global DEBUG ``log_level`` (ant-node has no per-module env filter), and
    a DEDICATED small InfluxDB + short-retention Elasticsearch. Node and
    monitoring/logging VMs each pick their own provider — including Contabo, which
    is root-disk only (no attachable volume). Set ``binary.branch`` +
    ``binary.debug: true`` to build a debug ant-node. ``None`` means "use the CLI
    default"; the CLI defaults ``log_level=debug``, ``rust_backtrace=full``,
    ``coredumps``/``monitoring``/``forward_logs`` on, ``node_count=4`` and
    ``log_retention_days=2``. Those last two are coupled: at DEBUG each service
    emits ~758 log lines/s and a single telegraf forwarder tops out near 2700
    lines/s, so a bigger ``node_count`` silently makes Elasticsearch a partial
    sample and leaves the on-disk logs as the only complete record — which this
    retention then discards. Every service also gets
    ``COLORBT_SHOW_HIDDEN=1`` (unconditional for this kind) and, when
    ``coredumps`` is on, ``gdb``/``elfutils`` installed to read the file cores.
    """

    binary: Binary
    region: str
    provider: Optional[str] = None  # digitalocean|hetzner|vultr|ovh|contabo
    vm_size: Optional[str] = None
    vm_count: Optional[int] = None  # default 1
    node_count: Optional[int] = None  # default 4 (forwarder-bound; see docstring)
    volume_size: Optional[int] = None  # ignored on Contabo (root disk)
    log_level: Optional[LogLevel] = None  # default 'debug'
    rust_backtrace: Optional[str] = None  # default 'full'; '1' or '' also valid
    coredumps: Optional[bool] = None  # default on
    coredump_retention_days: Optional[int] = None  # default 7; 0 = keep forever
    log_retention_days: Optional[int] = None  # default 2; 0 = keep forever
    heaptrack: Optional[bool] = None  # default off (use prod-nodes for heaptrack)
    monitoring: Optional[bool] = None  # dedicated InfluxDB; default on
    monitoring_provider: Optional[str] = None  # digitalocean|ovh|contabo
    monitoring_region: Optional[str] = None
    monitoring_droplet_size: Optional[str] = None
    monitoring_volume_size: Optional[int] = None  # ignored on Contabo
    grafana: Optional[bool] = None  # with monitoring, also a Grafana VM
    influx_retention_days: Optional[int] = None  # default 7; 0 = infinite
    forward_logs: Optional[bool] = None  # dedicated ES for node logs; default on
    elastic_provider: Optional[str] = None  # digitalocean|ovh|contabo
    elastic_region: Optional[str] = None
    elastic_droplet_size: Optional[str] = None
    elastic_volume_size: Optional[int] = None  # ignored on Contabo
    elastic_retention_days: Optional[int] = None  # ILM days; default 3
    elastic_shards: Optional[int] = None  # default 1
    elastic_drop_log_levels: Optional[str] = None
    rewards_address: Optional[str] = None
    port: Optional[int] = None
    keep_monitoring: Optional[bool] = None  # destroy: --keep-monitoring when True
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True
    # Contabo needs an image UUID and the operator public key (registered as a
    # contabo_secret) for whichever tier(s) run on Contabo.
    contabo_image_id: Optional[str] = None
    contabo_ssh_public_key_path: Optional[str] = None


# --------------------------------------------------------------------------- #
# standalone Elasticsearch
# --------------------------------------------------------------------------- #
class ElasticsearchSpec(_Base):
    """A standalone, permanent Elasticsearch instance (kind ``elasticsearch``).

    Unlike the ``elastic:`` block on a testnet/extension — which describes an
    ephemeral stack subordinate to that deployment — this kind IS the whole
    deployment: no nodes, no clients, no binary to build. It exists so log
    producers outside our own fleet (the community beta-channel cohort) have
    somewhere to forward to.

    That difference in audience is what drives ``public_ingest``. A testnet ES
    is reachable only by our VMs and deliberately runs plaintext with anonymous
    access; an instance community machines write to must terminate TLS and
    refuse anonymous callers. ``public_ingest`` is therefore opt-in rather than
    the default: turning it on for an existing testnet stack would change how
    every forwarder has to address it.
    """

    provider: str = "digitalocean"  # digitalocean|ovh|contabo
    region: Optional[str] = None  # default per provider (DO: lon1)
    droplet_size: Optional[str] = None  # default per provider (DO: s-4vcpu-8gb)
    volume_size: Optional[int] = None  # GB; ignored on Contabo (root disk)
    volume_type: Optional[str] = None  # OVH only
    shards: Optional[int] = None  # default 4
    # Short by design: the beta soak is 5 days, so ~14 days of history is ample
    # and the disk is the binding constraint long before the retention is.
    retention_days: int = 14

    # --- public ingest -------------------------------------------------------
    # Off by default so an operator cannot accidentally reconfigure a testnet
    # stack's addressing. When on: ES binds to localhost, anonymous access is
    # disabled, and a TLS-terminating reverse proxy fronts it on 443.
    public_ingest: bool = False
    # The DNS name the certificate is issued for. Must already resolve to this
    # instance before `elastic provision` runs — provisioning preflights it.
    ingest_hostname: Optional[str] = None
    # Adopt an already-allocated reserved IP instead of creating one. The point
    # of a reserved IP is that the DNS record survives a droplet rebuild, which
    # only holds if a rebuild reattaches the SAME address.
    reserved_ip: Optional[str] = None
    # Point ACME at Let's Encrypt's staging CA. Production issuance is rate
    # limited to 5 duplicate certificates per week, which repeated test deploys
    # of the same hostname will exhaust — and the lockout outlasts the debugging
    # session that caused it. Staging certs are untrusted by design.
    letsencrypt_staging: bool = False

    keep_on_destroy: bool = False
    # Contabo needs an image UUID and the operator public key.
    contabo_image_id: Optional[str] = None
    contabo_ssh_public_key_path: Optional[str] = None

    @model_validator(mode="after")
    def _public_ingest_needs_hostname(self) -> "ElasticsearchSpec":
        # ACME cannot issue a certificate for an IP address, so public ingest
        # without a hostname is not a degraded mode — it cannot come up at all.
        # Failing here costs nothing; failing at provision time costs a VM that
        # is already created and billing.
        if self.public_ingest and not self.ingest_hostname:
            raise ValueError(
                "elasticsearch.public_ingest requires ingest_hostname "
                "(the DNS name the TLS certificate is issued for)"
            )
        if self.ingest_hostname and not self.public_ingest:
            raise ValueError(
                "elasticsearch.ingest_hostname requires public_ingest: true "
                "(nothing terminates TLS for that name otherwise)"
            )
        return self

    @model_validator(mode="after")
    def _staging_needs_public_ingest(self) -> "ElasticsearchSpec":
        if self.letsencrypt_staging and not self.public_ingest:
            raise ValueError(
                "elasticsearch.letsencrypt_staging requires public_ingest: true "
                "(no certificate is requested otherwise)"
            )
        return self

    @model_validator(mode="after")
    def _reserved_ip_is_digitalocean_only(self) -> "ElasticsearchSpec":
        # Reserved IPs are modelled in the DigitalOcean module only. Silently
        # ignoring the field elsewhere would promise a stable address the
        # deployment does not actually have — and the DNS record depends on it.
        if self.reserved_ip and self.provider != "digitalocean":
            raise ValueError(
                "elasticsearch.reserved_ip is only supported on provider "
                f"'digitalocean' (got '{self.provider}')"
            )
        return self


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
    prod_continuous_uploader: Optional[ProdContinuousUploaderSpec] = None
    prod_downloaders: Optional[ProdDownloadersSpec] = None
    prod_local_uploads: Optional[ProdLocalUploadsSpec] = None
    prod_local_downloads: Optional[ProdLocalDownloadsSpec] = None
    prod_nodes: Optional[ProdNodesSpec] = None
    prod_debug_nodes: Optional[ProdDebugNodesSpec] = None
    elasticsearch: Optional[ElasticsearchSpec] = None

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
