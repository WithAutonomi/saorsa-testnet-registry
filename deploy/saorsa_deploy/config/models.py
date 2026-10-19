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

from saorsa_deploy.client_roles import (
    BROWSER_DEFAULT_DOWNLOAD_TIMEOUT_SECS,
    BROWSER_DEFAULT_INSTANCES,
    BROWSER_DEFAULT_ROUND_PERIOD_SECS,
    BROWSER_MAX_INSTANCES,
    BROWSER_MIN_ROUND_PERIOD_SECS,
)
from saorsa_deploy.client_roles import WASM_MAX_FILE_BYTES as _WASM_MAX_FILE_BYTES
from saorsa_deploy.providers import PROVIDERS
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

DAILY_AT_MESSAGE = "daily_at must be HH:MM (24-hour, UTC)"


def validate_daily_at(value: str) -> str:
    """Validate a ``daily_at`` anchor — exactly ``HH:MM``, 24-hour, UTC implied.

    Strict about the shape rather than lenient, because the value is the one
    thing that makes a permanent daily series readable day against day: a
    quietly-accepted ``"0:00"`` or ``"00:00:00"`` would be a deployment whose
    cycle starts at a time nobody chose.
    """
    text = str(value)
    parts = text.split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        raise ValueError(DAILY_AT_MESSAGE)
    hours, minutes = parts
    if not (hours.isdigit() and minutes.isdigit()):
        raise ValueError(DAILY_AT_MESSAGE)
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        raise ValueError(DAILY_AT_MESSAGE)
    return text


# The UTC wall-clock time a permanent daily cycle starts at. A fixed anchor
# rather than an interval: a sleep taken after each cycle moves the start time
# by the cycle's own duration every day, and a restart re-anchors it to the
# restart moment.
DailyAt = Annotated[str, AfterValidator(validate_daily_at)]


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
        raise ValueError(f"{label}: pool_min_size must be below pool_max_size, which is exclusive")

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
    # A client tier added to a deployment that is already running. The block IS
    # the role lists, so the kind maps onto `clients:` itself rather than a
    # wrapper -- there is nothing else in such a manifest to configure.
    "clients": "clients",
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


#: Client transport for a production client deployment. `native` runs the `ant`
#: binary; `wasm` runs ant-core compiled to WASM over WebRTC-Direct.
ClientKind = Literal["native", "wasm"]


class _Base(BaseModel):
    # extra="forbid": a mistyped key is a loud error, not a silent no-op.
    # coerce_numbers_to_str: YAML parses `ant: 1000` / `eth: 0.1` as int/float, but
    # the funding fields are strings (passed through to the CLI verbatim).
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


# Manifest provider keys -> saorsa_deploy.providers.PROVIDERS keys.
_PROVIDER_KEY = {
    "digital_ocean": "digitalocean",
    "hetzner": "hetzner",
    "vultr": "vultr",
    "ovh": "ovh",
    "ovh_3az": "ovh_3az",
}


def _check_known_regions(provider_key: str, regions: list[str], where: str) -> None:
    """Reject region names the deploy tool does not know.

    `bootstraps infra` / `nodes infra` validate `--do-regions-list` against
    PROVIDERS at run time (providers.resolve_regions, bootstrap.py); until
    2026-10-06 `config validate` and `plan` did not, so a manifest naming a
    region the provider offers but the tool does not know (V2-1449: nyc2)
    passed validation and failed after the build and half the infrastructure
    had been created. Same check, same wording, at manifest-load time.
    """
    cfg = PROVIDERS.get(_PROVIDER_KEY.get(provider_key, provider_key))
    if cfg is None:
        return
    unknown = [r for r in regions if r not in cfg.regions]
    if unknown:
        raise ValueError(
            f"{where}: unknown {cfg.name} region(s): {', '.join(unknown)}. "
            f"Known: {', '.join(cfg.regions)}"
        )


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


#: Cargo features the WASM bundle is built with when a manifest says nothing.
#: `ant-core` declares `default = ["native"]` and a `browser-wasm` feature
#: mutually exclusive with it, so a wasm32 build without it stops at an explicit
#: `compile_error!` -- this is the floor, never empty.
DEFAULT_WASM_FEATURES = "browser-wasm"


class AntWasmBinary(Binary):
    """The testnet `ant-core` WASM bundle source, with its cargo features.

    `features` exists because the browser-downloader role needs the bundle's
    `setBrowserTrace` hook, which is compiled in only by the `test-utils`
    feature -- and that feature also links a deterministic mock node, so it is
    emphatically not the default. The string is passed to
    ``wasm-pack build … --features <features>`` verbatim rather than being a
    list, so a manifest says exactly what cargo is told.
    """

    features: Optional[str] = None

    @property
    def resolved_features(self) -> str:
        return self.features or DEFAULT_WASM_FEATURES


# --------------------------------------------------------------------------- #
# testnet
# --------------------------------------------------------------------------- #
class TestnetBinaries(_Base):
    ant_node: Binary
    ant: Optional[Binary] = None  # only required when native clients are present
    # The `ant-core` WASM package, required when WASM clients are present.
    #
    # Branch form only: no WASM package is published as a GitHub release, so
    # there is no version to pin. It is built from source by `build ant-wasm`
    # (wasm-pack against the ant-client checkout) exactly as `ant` is built by
    # `build ant`.
    #
    # `AntWasmBinary`, not `Binary`: it additionally carries the cargo
    # `features` the bundle is built with (the browser-downloader role needs a
    # `test-utils` build for its trace hook).
    ant_wasm: Optional[AntWasmBinary] = None


class Bootstraps(_Base):
    count: int = 1
    port: int = 10000
    # Pin the WebRTC-Direct listener on every bootstrap to this UDP port.
    #
    # Bootstraps are the one tier where the browser-facing address must not
    # move: a WASM/browser client bootstraps itself from a bootstrap multiaddr, and
    # left unset ant-node DERIVES the WebRTC port from the QUIC port
    # (automatic_webrtc_port), so it silently changes if `port` above ever
    # changes. Non-bootstrap nodes deliberately keep the derived/random port --
    # their address changes propagate over the DHT the same way QUIC's do.
    #
    # NB the port is only half of a stable address: the multiaddr also embeds
    # the DTLS certificate hash, which ant-node persists at
    # <root_dir>/webrtc-direct.pem. Wiping a bootstrap's data directory rotates
    # it and invalidates every cached browser address.
    webrtc_port: Optional[int] = None
    regions_list: Optional[list[str]] = None  # bootstraps infra --do-regions-list
    attached_volume_size: Optional[int] = None

    @model_validator(mode="after")
    def _known_regions(self) -> "Bootstraps":
        if self.regions_list:
            _check_known_regions("digital_ocean", self.regions_list, "bootstraps.regions_list")
        return self


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
    def _known_regions(self) -> "Nodes":
        for key, cfg in self.providers.items():
            if cfg.regions_list:
                _check_known_regions(key, cfg.regions_list, f"nodes.providers.{key}.regions_list")
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


#: ant-core refuses a browser upload larger than this, client-side, in about
#: 0.2s and without touching the network:
#:
#:     invalid browser data: browser files are limited to 1000000000 bytes
#:
#: It is a DECIMAL gigabyte, while `file_size_mb` is binary (the provisioner
#: emits `FILE_SIZE_KB = file_size_mb * 1024` and the client reads
#: `fileSizeBytes = FILE_SIZE_KB * 1024`). So the obvious `file_size_mb: 1000`
#: is 1,048,576,000 bytes and is rejected on every single attempt -- on the
#: cancelled V2-1174 run that was 227 of 235 WASM upload failures, which made
#: the tier's success rate describe nothing. Validated here so a manifest fails
#: before any VM is created rather than after the run is already billing.
#: Canonical value lives in `client_roles` (the provisioner reads it too).
WASM_MAX_FILE_BYTES = _WASM_MAX_FILE_BYTES
WASM_MAX_FILE_MB = WASM_MAX_FILE_BYTES // (1024 * 1024)  # 953


class WasmUploader(_Base):
    """An uploader driving `ant-core` compiled to WASM instead of the native `ant`.

    Exists because `ant-cli` cannot exercise the WASM path at all: it links
    native `ant-core`, so it takes the native transport no matter how it is
    invoked. This client runs a Node.js program that imports the WASM build and
    reaches nodes over WebRTC-Direct, which is the whole point -- it is the only
    way to measure the browser path the way the native client is measured.

    Deliberately NOT a flag on `Uploader`. The role is what gives a client VM
    its hostname, its Terraform workspace and its S3 state key, so a WASM and a
    native uploader in the same region would otherwise be indistinguishable in
    all three -- and telling them apart is the entire purpose of the comparison.
    """

    provider: str
    region: str
    file_size_mb: int
    vm_size: Optional[str] = None
    payment: Optional[Payment] = None
    ant: Optional[str] = None  # wallet funding (ANT tokens)
    eth: Optional[str] = None  # wallet funding (ETH)
    clock_skew: Optional[ClockSkew] = None  # signed duration skew on this VM (e.g. "2h", "-2h")
    # Simulated residential link conditions on this VM — see Uploader.network.
    network: Optional[Network] = None
    # Forward this client's stdout+stderr to the deployment's Elasticsearch
    # instance (index telegraf-ant-clients-{name}). Requires elastic.enabled.
    forward_logs: Optional[bool] = None
    # RUST_LOG for the WASM client's tracing subscriber.
    rust_log: Optional[str] = None

    # NB there is no `bootstrap_cache` here. That option selects between ant's
    # peer cache and an explicit `--bootstrap` invocation, and neither exists on
    # this path: a WASM client bootstraps itself from the bootstrap tier's
    # webrtc-direct multiaddrs and discovers the rest of the network over
    # authenticated find_node.

    @model_validator(mode="after")
    def _file_size_within_the_browser_cap(self):
        size_bytes = self.file_size_mb * 1024 * 1024
        if size_bytes > WASM_MAX_FILE_BYTES:
            raise ValueError(
                f"clients.wasm_uploaders: file_size_mb {self.file_size_mb} is "
                f"{size_bytes:,} bytes, over ant-core's browser limit of "
                f"{WASM_MAX_FILE_BYTES:,}. The largest whole MiB that fits is "
                f"{WASM_MAX_FILE_MB}. NB `file_size_mb` is binary (MiB) while "
                f"the cap is decimal, so 1000 does not fit."
            )
        return self


class WasmDownloader(_Base):
    """A downloader driving `ant-core` compiled to WASM. See :class:`WasmUploader`."""

    provider: str
    region: str
    vm_size: Optional[str] = None
    clock_skew: Optional[ClockSkew] = None  # signed duration skew on this VM (e.g. "2h", "-2h")
    # Simulated residential link conditions on this VM — see Uploader.network.
    network: Optional[Network] = None
    forward_logs: Optional[bool] = None
    rust_log: Optional[str] = None


#: Default workload rates for a pointer client. Together they are 60 creates and
#: 120 updates an hour -- 180 paid writes, one every 20 seconds. That is the
#: figure the run issues size wallet funding and Arbitrum Sepolia RPC load
#: against, so changing a default here changes what a run costs.
POINTER_CREATE_INTERVAL_SECS = 60
POINTER_UPDATE_INTERVAL_SECS = 30
POINTER_SWEEP_INTERVAL_SECS = 600
POINTER_CHAIN_FRACTION = 0.10
#: Hand a pointer over this often (V2-1354). 0 -- the default -- runs no handover
#: loop at all, which is what every train whose RC predates `ant pointer
#: transfer` has to take: on such a build the transfer step would fail every
#: cycle and record an error that says nothing about the release.
POINTER_TRANSFER_INTERVAL_SECS = 0


class PointerClient(_Base):
    """A client exercising pointers -- paid, owner-signed mutable references.

    Deliberately a role of its own rather than an option on `Uploader`, for the
    reason the WASM roles are: the role names the VM, its Terraform workspace and
    its S3 state key. It is also a different instrument. An uploader measures a
    transfer; this measures whether a reference that was written can be read back
    and whether it goes stale -- which the ADR says it sometimes will, since an
    eclipsed reader can be handed an older, validly signed state and cannot tell.

    It carries a funded wallet like an uploader because every write -- create and
    update alike -- is one paid single-node quote.
    """

    provider: str
    region: str
    vm_size: Optional[str] = None
    ant: Optional[str] = None  # wallet funding (ANT tokens)
    eth: Optional[str] = None  # wallet funding (ETH)
    clock_skew: Optional[ClockSkew] = None  # signed duration skew (e.g. "2h", "-2h")
    # Simulated residential link conditions on this VM — see Uploader.network.
    network: Optional[Network] = None
    # ant peer cache: True (default) invokes ant WITHOUT --bootstrap and ships a
    # bootstrap_peers.toml instead; False keeps the legacy --bootstrap invocation.
    bootstrap_cache: Optional[bool] = None
    # Forward this client's ant stdout+stderr to the deployment's Elasticsearch
    # instance (index telegraf-ant-clients-{name}). Requires elastic.enabled.
    forward_logs: Optional[bool] = None
    # RUST_LOG for the `ant pointer` invocations this client makes.
    rust_log: Optional[str] = None

    # --- workload ---------------------------------------------------------
    # Create a brand-new pointer this often. Creation runs for the WHOLE run
    # rather than seeding a fixed pool up front, because the question is whether
    # a pointer decays with age, and that needs pointers of every age present at
    # every moment.
    create_interval_secs: int = POINTER_CREATE_INTERVAL_SECS
    # Update a uniformly-random owned pointer this often, then read it back.
    update_interval_secs: int = POINTER_UPDATE_INTERVAL_SECS
    # Minimum gap between sweeps of every known pointer (own and foreign). A
    # sweep that takes longer than this simply starts the next one immediately;
    # a large pool makes a sweep take longer, it does not drop pointers from it.
    sweep_interval_secs: int = POINTER_SWEEP_INTERVAL_SECS
    # Fraction of updates that re-point at another OWNED pointer (target kind
    # `pointer`) instead of a chunk, and are then followed with `resolve`. 0
    # disables chaining.
    chain_fraction: float = POINTER_CHAIN_FRACTION
    # Hand one owned pointer over to a freshly created recipient this often, then
    # check the result from near and far and prove the take-backs are refused.
    # 0 (the default) disables the loop entirely -- the one interval here that
    # accepts zero, because `ant pointer transfer` does not exist on a build that
    # predates V2-1354 and the loop would record an error every cycle.
    transfer_interval_secs: int = POINTER_TRANSFER_INTERVAL_SECS
    # Safety cap on how many pointers one client will own. 0 (the default) means
    # no cap: the pool is meant to be unbounded so decay with age is observable.
    pointer_max: int = 0

    @model_validator(mode="after")
    def _workload_is_runnable(self) -> "PointerClient":
        if self.transfer_interval_secs < 0:
            raise ValueError(
                f"clients.pointers: transfer_interval_secs must be >= 0 "
                f"(0 = no handover loop), got {self.transfer_interval_secs}."
            )
        for field in ("create_interval_secs", "update_interval_secs", "sweep_interval_secs"):
            value = getattr(self, field)
            if value <= 0:
                raise ValueError(
                    f"clients.pointers: {field} must be > 0, got {value}. A zero "
                    "interval is a busy loop against a paid operation, not a "
                    "disabled one."
                )
        if not 0.0 <= self.chain_fraction <= 1.0:
            raise ValueError(
                f"clients.pointers: chain_fraction must be between 0 and 1, got "
                f"{self.chain_fraction}."
            )
        if self.pointer_max < 0:
            raise ValueError(
                f"clients.pointers: pointer_max must be >= 0 (0 = uncapped), got "
                f"{self.pointer_max}."
            )
        return self

    @property
    def writes_per_hour(self) -> float:
        """Paid writes this client's configuration performs per hour.

        Creates plus updates: both are one paid single-node quote, at the same
        price, so the sum is what sizes the wallet. Reads are free. Rates are
        deterministic, which is why sizing a pointer client needs no baseline
        throughput the way an uploader does.

        A handover cycle is TWO paid writes, not one: the recipient has to be
        created before the pointer can be handed to it, and the transfer itself
        signs a final state. The take-backs, the finality checks and the
        controller walk are all free -- a take-back is refused before it pays,
        which is the property the loop exists to prove.
        """
        rate = 3600.0 / self.create_interval_secs + 3600.0 / self.update_interval_secs
        if self.transfer_interval_secs > 0:
            rate += 2 * 3600.0 / self.transfer_interval_secs
        return rate


class BrowserDownloader(_Base):
    """A VM running N real headless Chromium instances on one hot file.

    The WASM roles run ant-core compiled to WASM under Node.js with a
    datachannel shim, one process per VM, drawing random files from the address
    pool. That cannot measure what ADR-0023 (hot-record read spreading) changes:
    the behaviour of many *real browsers* reading the *same* record at the *same
    moment*. This role launches a fresh Chromium context per instance per round,
    all of them reading one operator-chosen file on a shared wall-clock
    schedule, and keeps a per-dial trace from the bundle's `test-utils` tracing.

    No file size and no wallet: it only reads, and what it reads is named by
    `clients select-hot-files`, not by this entry.
    """

    provider: str
    region: str
    vm_size: Optional[str] = None
    # Chromium instances per VM. Capped at the per-IP WebRTC session quota: a
    # node refuses a fifth session from one source address, so a fifth instance
    # would be refused by every node it dials and measure the quota rather than
    # the read path.
    instances: int = BROWSER_DEFAULT_INSTANCES
    # Rounds begin on wall-clock multiples of this, which is what makes the
    # cohort simultaneous without any coordination between VMs.
    round_period_secs: int = BROWSER_DEFAULT_ROUND_PERIOD_SECS
    # Harness deadline per download, measured from the round boundary.
    download_timeout_secs: int = BROWSER_DEFAULT_DOWNLOAD_TIMEOUT_SECS
    # Forward the harness stdout+stderr to the deployment's Elasticsearch
    # instance (index telegraf-ant-clients-{name}). Requires elastic.enabled.
    forward_logs: Optional[bool] = None
    # Identical VMs to create from this entry, with consecutive client_index
    # values. The one client role that has this: a hot-read cohort is 35 VMs of
    # the same shape, and listing 35 identical entries is a manifest nobody can
    # read and a diff nobody can review.
    count: int = 1

    # NB no `clock_skew`, `network` or `rust_log`. The measurement is *when*
    # each instance read and *which holder served it*, so a skewed clock would
    # desynchronise the one thing being measured; shaping would do the same to
    # the dial timings the derived fields are read off.

    @model_validator(mode="after")
    def _within_the_limits(self) -> "BrowserDownloader":
        if self.count < 1:
            raise ValueError(
                f"clients.browser_downloaders: count must be >= 1, got {self.count}. "
                "Omit the key for a single VM."
            )
        if not 1 <= self.instances <= BROWSER_MAX_INSTANCES:
            raise ValueError(
                f"clients.browser_downloaders: instances must be between 1 and "
                f"{BROWSER_MAX_INSTANCES}, got {self.instances}. A node admits at "
                f"most {BROWSER_MAX_INSTANCES} WebRTC sessions per source IPv4 "
                f"address (max_connections_per_ip), and one browser client holds "
                f"one session per node, so a further instance would be refused by "
                f"every node it dials."
            )
        if self.round_period_secs < BROWSER_MIN_ROUND_PERIOD_SECS:
            raise ValueError(
                f"clients.browser_downloaders: round_period_secs must be >= "
                f"{BROWSER_MIN_ROUND_PERIOD_SECS}, got {self.round_period_secs}. "
                "A shorter round is less than a cold browser client's own startup."
            )
        if self.download_timeout_secs >= self.round_period_secs:
            raise ValueError(
                f"clients.browser_downloaders: download_timeout_secs "
                f"({self.download_timeout_secs}) must be < round_period_secs "
                f"({self.round_period_secs}), or a round can still be running when "
                "the next one is due and the cohort stops being simultaneous."
            )
        return self


class Clients(_Base):
    uploaders: list[Uploader] = []
    downloaders: list[Downloader] = []
    # WASM (browser-path) clients. Independent of the native pair above: a
    # testnet may run either, or both side by side for a comparison.
    wasm_uploaders: list[WasmUploader] = []
    wasm_downloaders: list[WasmDownloader] = []
    # Pointer clients. Independent of both transfer tiers: a pointer client
    # neither publishes file addresses nor consumes them, so it is not subject to
    # the uploader/downloader pairing rule below. It does READ the shared upload
    # address pool for realistic `chunk` targets, and falls back to random
    # 32-byte addresses when the pool is empty, so it never depends on uploaders.
    pointers: list[PointerClient] = []
    # Browser (real headless Chromium) downloaders. Not subject to the pairing
    # rule either: the file they read is named by `clients select-hot-files` out
    # of what is already in the pool, so a cohort is added to a deployment whose
    # uploads have already happened -- which is the only time a hot file exists.
    browser_downloaders: list[BrowserDownloader] = []

    @model_validator(mode="after")
    def _both_roles(self) -> "Clients":
        # Downloaders fetch addresses that uploaders publish; one without the
        # other is pointless. Mirror the deploy-runbook skill's refusal.
        #
        # The rule is applied to each transport SEPARATELY rather than to the
        # union. Addresses are a shared pool, so a union check would pass for a
        # deployment with native uploaders and WASM downloaders — but that
        # deployment never exercises a WASM upload or a native download, which
        # is precisely what configuring that tier was for.
        if (self.uploaders or self.downloaders) and not (self.uploaders and self.downloaders):
            raise ValueError(
                "clients: need at least one uploader AND one downloader "
                "(downloaders consume addresses uploaders publish)"
            )
        if (self.wasm_uploaders or self.wasm_downloaders) and not (
            self.wasm_uploaders and self.wasm_downloaders
        ):
            raise ValueError(
                "clients: need at least one wasm_uploader AND one wasm_downloader "
                "(wasm downloaders consume addresses wasm uploaders publish)"
            )
        return self

    @property
    def all_entries(self) -> list:
        """Every client entry across both transports, for cross-cutting checks."""
        return [
            *self.uploaders,
            *self.downloaders,
            *self.wasm_uploaders,
            *self.wasm_downloaders,
            *self.pointers,
            *self.browser_downloaders,
        ]

    @property
    def has_wasm(self) -> bool:
        """Whether anything here needs the `ant-core` WASM bundle.

        True for the browser role too, and that is the point: it runs the same
        bundle (in Chromium rather than under Node), so it needs
        ``binaries.ant_wasm``, the `build ant-wasm` step, and the bootstrap
        WebRTC-Direct addresses collected before `clients provision`. The
        uploader/downloader PAIRING rule is checked on the two wasm-* lists
        alone and is unaffected.
        """
        return bool(self.wasm_uploaders or self.wasm_downloaders or self.browser_downloaders)

    @property
    def has_pointers(self) -> bool:
        return bool(self.pointers)

    @property
    def has_browser(self) -> bool:
        return bool(self.browser_downloaders)


class ClientsExtensionSpec(_Base):
    """The client tiers of a ``kind: clients`` manifest — a client EXTENSION.

    Reuses the same per-entry models as a deployment manifest's ``clients:``
    block, so the grammar an operator already knows carries over verbatim. Two
    rules differ, both deliberately:

    * **There is no uploader/downloader pairing rule.** A deployment manifest
      refuses downloaders without uploaders, because a tier that consumes
      addresses nobody publishes measures nothing. An extension reads the
      deployment's EXISTING address pool, so "add 20 more downloaders at hour 3"
      is the point of it, not a mistake.
    * **At least one role list must be non-empty.** An extension with no VMs is
      an S3 state object and a teardown guard standing in the way of a
      ``deployments down``, which is strictly worse than not creating it.

    No binaries: see :mod:`saorsa_deploy.client_extension`.
    """

    uploaders: list[Uploader] = []
    downloaders: list[Downloader] = []
    wasm_uploaders: list[WasmUploader] = []
    wasm_downloaders: list[WasmDownloader] = []
    pointers: list[PointerClient] = []
    browser_downloaders: list[BrowserDownloader] = []

    @model_validator(mode="after")
    def _at_least_one_tier(self) -> "ClientsExtensionSpec":
        if not self.all_entries:
            raise ValueError(
                "clients: at least one of uploaders / downloaders / "
                "wasm_uploaders / wasm_downloaders / pointers / "
                "browser_downloaders must be non-empty "
                "(an extension with no VMs would only stand in the way of a "
                "`deployments down`)"
            )
        return self

    @property
    def all_entries(self) -> list:
        return [
            *self.uploaders,
            *self.downloaders,
            *self.wasm_uploaders,
            *self.wasm_downloaders,
            *self.pointers,
            *self.browser_downloaders,
        ]

    @property
    def has_wasm(self) -> bool:
        """Whether anything here needs the `ant-core` WASM bundle.

        True for the browser role too, and that is the point: it runs the same
        bundle (in Chromium rather than under Node), so it needs
        ``binaries.ant_wasm``, the `build ant-wasm` step, and the bootstrap
        WebRTC-Direct addresses collected before `clients provision`. The
        uploader/downloader PAIRING rule is checked on the two wasm-* lists
        alone and is unaffected.
        """
        return bool(self.wasm_uploaders or self.wasm_downloaders or self.browser_downloaders)

    @property
    def has_pointers(self) -> bool:
        return bool(self.pointers)

    @property
    def has_browser(self) -> bool:
        return bool(self.browser_downloaders)


def _require_client_binaries(clients, binaries, kind: str) -> None:
    """Each client transport needs its own binary; neither substitutes for the other.

    `ant` is the native CLI and `ant_wasm` is the `ant-core` WASM package. A
    deployment running only WASM clients has no use for `ant`, and one running
    only native clients has no use for the WASM package, so the two are required
    independently rather than together.
    """
    if clients is None:
        return
    if (clients.uploaders or clients.downloaders or clients.pointers) and binaries.ant is None:
        # Pointer clients drive the native `ant pointer` subcommands, so they
        # need `ant` for the same reason the native transfer clients do.
        raise ValueError(f"{kind}.binaries.ant is required when clients are configured")
    if clients.has_wasm:
        if binaries.ant_wasm is None:
            raise ValueError(
                f"{kind}.binaries.ant_wasm is required when wasm clients are configured"
            )
        if not binaries.ant_wasm.is_branch:
            raise ValueError(
                f"{kind}.binaries.ant_wasm must use the branch form "
                "(no ant-core WASM package is published as a release, so there "
                "is no version to pin — it is built from source by `build ant-wasm`)"
            )


def _require_elastic_for_forward_logs(clients, elastic, kind: str) -> None:
    """Refuse `forward_logs` on a testnet/extension client unless elastic is on.

    Client logs forward to the deployment's OWN Elasticsearch instance (a
    distinct index from the node logs), so there must be one to forward to.
    """
    if clients is None or (elastic is not None and elastic.enabled):
        return
    wants = [c for c in clients.all_entries if c.forward_logs]
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
        _require_client_binaries(self.clients, self.binaries, "testnet")
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
        _require_client_binaries(self.clients, self.binaries, "extension")
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
            raise ValueError("prod_uploaders.uploaders[]: set exactly one of `urls` or `pool`")
        _check_pool_bounds(self, "prod_uploaders.uploaders[]")
        return self


class WasmBinary(_Base):
    """The `ant-core` WASM bundle source for a production WASM client.

    Branch form only, and `branch` takes a **branch or a tag** — `build
    ant-wasm` clones with `git clone --branch <ref>`, which accepts either, so
    an RC is pinned with `branch: ant-cli-v0.3.8-rc.1`. `version` is refused
    because no WASM package is published as a release; there is nothing for a
    version to name.
    """

    branch: str
    repo_owner: Optional[str] = None
    version: Optional[str] = None

    @model_validator(mode="after")
    def _no_version(self) -> "WasmBinary":
        if self.version:
            raise ValueError(
                "wasm_binary: `version` is not supported — no ant-core WASM "
                "package is published as a release. Pin a git tag with "
                "`branch: ant-cli-vX.Y.Z` instead (V2-1337 tracks a released "
                "version)"
            )
        return self


class ProdWasmUploader(_Base):
    """One browser-path uploader VM on a `prod-uploaders` deployment.

    Deliberately NOT a flag on :class:`ProdUploader`: the transport decides the
    VM's role, its Terraform workspace and its S3 state key, so telling the two
    tiers apart in all three is the point of running both. It also decides which
    options exist at all — `bootstrap_cache` selects between ant's peer cache and
    an explicit `--bootstrap` and a browser client does neither, while
    `memory_profiling` and `log_level` act on the `ant` process, which never
    runs. Those three are absent rather than ignored, so `extra="forbid"` refuses
    them by name.
    """

    region: str
    # Optional, unlike the native uploader's: a browser-path transfer holds ~4x
    # the file in WASM memory, so an omitted size defaults to the WASM floor
    # rather than to whatever the native tier happens to use.
    vm_size: Optional[str] = None
    urls: Optional[str] = None
    pool: Optional[dict[str, int]] = None
    pool_min_size: Optional[PoolSize] = None
    pool_max_size: Optional[PoolSize] = None
    exclude_partial: bool = False
    payment: Optional[Payment] = None
    clock_skew: Optional[ClockSkew] = None
    forward_logs: Optional[bool] = None
    rust_log: Optional[str] = None
    network: Optional[Network] = None

    @model_validator(mode="after")
    def _one_file_source(self) -> "ProdWasmUploader":
        if bool(self.urls) == bool(self.pool):
            raise ValueError("prod_uploaders.wasm_uploaders[]: set exactly one of `urls` or `pool`")
        _check_pool_bounds(self, "prod_uploaders.wasm_uploaders[]")
        return self

    @model_validator(mode="after")
    def _pool_within_the_browser_cap(self) -> "ProdWasmUploader":
        from saorsa_deploy.prod_wasm import check_wasm_pool_request

        problem = check_wasm_pool_request(self.pool)
        if problem:
            raise ValueError(f"prod_uploaders.wasm_uploaders[]: {problem}")
        return self

    @model_validator(mode="after")
    def _vm_is_big_enough(self) -> "ProdWasmUploader":
        from saorsa_deploy.prod_wasm import check_wasm_vm_size

        # prod uploaders are DigitalOcean-only (the spec carries no provider
        # field), so the slug's encoded memory makes this an exact check.
        problem = check_wasm_vm_size(self.vm_size, "digitalocean")
        if problem:
            raise ValueError(f"prod_uploaders.wasm_uploaders[].vm_size: {problem}")
        return self


class ProdUploadersSpec(_Base):
    binary: Binary
    secret_key_env: str = "ARBITRUM_ONE_SECRET_KEY"
    monitoring_volume_size: Optional[int] = None
    keep_monitoring: Optional[bool] = None  # destroy: --keep-monitoring when True
    keep_elastic: Optional[bool] = None  # destroy: --keep-elastic when True
    # deploy: --allow-duplicate-files — upload files already in the prod list
    allow_duplicate_files: bool = False
    uploaders: list[ProdUploader] = []
    # Browser-path uploaders, alongside (or instead of) the native ones. The two
    # tiers share the deployment's monitoring stack and wallet but nothing else.
    wasm_uploaders: list[ProdWasmUploader] = []
    # The WASM bundle source, required when any wasm_uploader is configured.
    wasm_binary: Optional[WasmBinary] = None
    # Override the committed production bootstrap WebRTC multiaddr list. No URL
    # form: nothing publishes these addresses at a URL.
    wasm_bootstrap_multiaddrs_file: Optional[str] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ProdUploadersSpec":
        if not self.uploaders and not self.wasm_uploaders:
            raise ValueError(
                "prod_uploaders: at least one uploader is required (uploaders "
                "and/or wasm_uploaders)"
            )
        return self

    @model_validator(mode="after")
    def _wasm_options(self) -> "ProdUploadersSpec":
        if not self.wasm_uploaders:
            if self.wasm_binary is not None:
                raise ValueError(
                    "prod_uploaders.wasm_binary is only valid with prod_uploaders.wasm_uploaders"
                )
            if self.wasm_bootstrap_multiaddrs_file is not None:
                raise ValueError(
                    "prod_uploaders.wasm_bootstrap_multiaddrs_file is only "
                    "valid with prod_uploaders.wasm_uploaders"
                )
            return self
        if self.wasm_binary is None:
            raise ValueError(
                "prod_uploaders.wasm_binary is required when wasm_uploaders "
                "are configured (the bundle is built from source by `build "
                "ant-wasm`)"
            )
        return self


def _check_prod_wasm_download(spec, kind: str) -> None:
    """Validate a download spec's ``client_kind: wasm`` configuration.

    Three options are refused rather than ignored: `bootstrap_cache` selects
    between ant's peer cache and an explicit `--bootstrap`, and a browser client
    does neither; `memory_profiling` and `download_diagnostics` both act on the
    `ant` process, which is not what runs. A silently-ignored option is worse
    than an error — the operator would read the manifest as describing the run.
    """
    if spec.client_kind != "wasm":
        if spec.wasm_binary is not None:
            raise ValueError(f"{kind}.wasm_binary is only valid with `client_kind: wasm`")
        if spec.wasm_bootstrap_multiaddrs_file is not None:
            raise ValueError(
                f"{kind}.wasm_bootstrap_multiaddrs_file is only valid with `client_kind: wasm`"
            )
        return

    if spec.wasm_binary is None:
        raise ValueError(
            f"{kind}.wasm_binary is required when `client_kind: wasm` "
            "(the bundle is built from source by `build ant-wasm`)"
        )
    forbidden = [
        name
        for name in ("bootstrap_cache", "memory_profiling", "download_diagnostics")
        if getattr(spec, name) is not None
    ]
    if forbidden:
        raise ValueError(
            f"{kind}: {', '.join(forbidden)} cannot be set with "
            "`client_kind: wasm` — bootstrap_cache has no browser-path meaning, "
            "and memory_profiling/download_diagnostics act on the `ant` process, "
            "which a WASM client never runs"
        )

    from saorsa_deploy.prod_wasm import check_wasm_vm_size

    parts = spec.spec.split(":", 1)
    vm_size = parts[1] if len(parts) == 2 else None
    problem = check_wasm_vm_size(vm_size, spec.provider)
    if problem:
        raise ValueError(f"{kind}.spec: {problem}")


class ProdDownloadersSpec(_Base):
    spec: str  # 'region[:vm_size]' (single client VM)
    binary: Binary
    # Which transport this deployment's single client VM runs: the native `ant`
    # binary over QUIC, or ant-core compiled to WASM over WebRTC-Direct. ONE
    # transport per deployment — the role names the VM and its state, and a
    # blended result describes neither instrument (the WASM tier measured
    # ~4.2x slower on download).
    client_kind: Optional[ClientKind] = None
    # The WASM bundle source, required under `client_kind: wasm`.
    wasm_binary: Optional[WasmBinary] = None
    # Override the committed production bootstrap WebRTC multiaddr list. No URL
    # form: nothing publishes these addresses at a URL.
    wasm_bootstrap_multiaddrs_file: Optional[str] = None
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

    @model_validator(mode="after")
    def _wasm_options(self) -> "ProdDownloadersSpec":
        _check_prod_wasm_download(self, "prod_downloaders")
        return self


class ProdContinuousDownloaderSpec(ProdDownloadersSpec):
    # UTC wall-clock time each daily cycle starts at. The service runs a cycle
    # immediately on a fresh deploy, then waits for this time every day after —
    # so the series is read day against day at the same hour, whatever the cycle
    # itself takes and however often the VM is restarted.
    daily_at: DailyAt = "00:00"
    # InfluxDB bucket retention in days (default 7); 0 = infinite. For a
    # permanent run, periodically archive into Postgres (`deployments archive`).
    influx_retention_days: Optional[int] = None
    # Per-op download-log retention in days before the daily prune timer
    # (default 7); 0 = keep forever.
    log_retention_days: Optional[int] = None

    @model_validator(mode="after")
    def _wasm_options_continuous(self) -> "ProdContinuousDownloaderSpec":
        _check_prod_wasm_download(self, "prod_continuous_downloader")
        return self


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

    # Which transport the UPLOADER VM runs: the native `ant` binary over QUIC,
    # or ant-core compiled to WASM over WebRTC-Direct. One transport per
    # deployment, as on the download kinds — the browser path measured several
    # times slower, so a blended daily series would describe neither instrument.
    # The verifiers stay native whatever this says, so a digest_mismatch remains
    # attributable to the upload rather than to the read-back.
    client_kind: Optional[ClientKind] = None
    # The WASM bundle source, required under `client_kind: wasm`.
    wasm_binary: Optional[WasmBinary] = None
    # Override the committed production bootstrap WebRTC multiaddr list. No URL
    # form: nothing publishes these addresses at a URL.
    wasm_bootstrap_multiaddrs_file: Optional[str] = None

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

    # UTC wall-clock time each daily cycle starts at — see
    # ProdContinuousDownloaderSpec.daily_at; the rule is the same on both kinds.
    daily_at: DailyAt = "00:00"
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
        _check_prod_wasm_upload(self, "prod_continuous_uploader")
        return self


def _check_prod_wasm_upload(spec, kind: str) -> None:
    """Validate the write test's ``client_kind: wasm`` configuration.

    Two options are refused rather than ignored: `bootstrap_cache` selects
    between ant's peer cache and an explicit `--bootstrap`, and a browser client
    does neither, while `log_level` sets the `ant` verbosity flag that installs
    its tracing subscriber — `ant-wasm` takes `RUST_LOG` straight from the
    environment and has no verbosity flag at all. A silently-ignored option is
    worse than an error: the operator would read the manifest as a description of
    the run.

    The pool draw is checked against ant-core's browser cap here rather than on
    the VM, because a drawn over-cap file is a permanently-spent pool entry that
    could never have been uploaded.
    """
    if spec.client_kind != "wasm":
        if spec.wasm_binary is not None:
            raise ValueError(f"{kind}.wasm_binary is only valid with `client_kind: wasm`")
        if spec.wasm_bootstrap_multiaddrs_file is not None:
            raise ValueError(
                f"{kind}.wasm_bootstrap_multiaddrs_file is only valid with `client_kind: wasm`"
            )
        return

    if spec.wasm_binary is None:
        raise ValueError(
            f"{kind}.wasm_binary is required when `client_kind: wasm` "
            "(the bundle is built from source by `build ant-wasm`)"
        )
    forbidden = [
        name for name in ("bootstrap_cache", "log_level") if getattr(spec, name) is not None
    ]
    if forbidden:
        raise ValueError(
            f"{kind}: {', '.join(forbidden)} cannot be set with "
            "`client_kind: wasm` — bootstrap_cache has no browser-path meaning, "
            "and log_level sets the `ant` verbosity flag, which a WASM client "
            "never takes (use rust_log)"
        )

    from saorsa_deploy.prod_wasm import check_wasm_pool_request, check_wasm_vm_size

    problem = check_wasm_pool_request(spec.pool)
    if problem:
        raise ValueError(f"{kind}.pool: {problem}")

    parts = spec.spec.split(":", 1)
    vm_size = parts[1] if len(parts) == 2 else None
    problem = check_wasm_vm_size(vm_size, spec.provider)
    if problem:
        raise ValueError(f"{kind}.spec: {problem}")


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
            raise ValueError("prod_canary: set exactly one of `urls_file` or `pool`")
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
            raise ValueError("prod_local_uploads: set exactly one of `urls`, `local_dir` or `pool`")
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

    # Minimum level accepted from community forwarders. Enforced server-side
    # because a participant's forwarder config is theirs to change; anything
    # more verbose is dropped at ingest. Changeable at runtime afterwards with
    # `elastic set-log-level`, so this is only the starting policy.
    min_log_level: Optional[LogLevel] = None  # default 'info'

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
    # Append a final `deployments slack-post` step to `up` — or, on
    # `kind: clients`, a `clients slack-post` step to each of `clients up` and
    # `clients down`, threaded under the deployment's own post.
    slack_post: bool = False

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
    clients: Optional[ClientsExtensionSpec] = None

    #: The deployment a `kind: clients` manifest extends. Required for that kind
    #: and refused for every other -- a deployment manifest IS the deployment,
    #: so a `deployment:` key on one would name something that does not exist.
    deployment: Optional[str] = None

    @model_validator(mode="after")
    def _deployment_only_on_clients_kind(self) -> "Manifest":
        if self.kind == "clients":
            if not self.deployment:
                raise ValueError(
                    "kind 'clients' requires a top-level `deployment:` naming the "
                    "testnet whose state this client tier reads (its bootstraps, "
                    "InfluxDB, Elasticsearch and address pool)"
                )
            # The extension's own name becomes part of a VM hostname, a
            # Terraform workspace path and an S3 key, so it is checked here
            # rather than at the first command that would build one of them.
            from saorsa_deploy.client_extension import (
                ExtensionError,
                validate_extension_name,
            )

            try:
                validate_extension_name(self.name)
            except ExtensionError as exc:
                raise ValueError(str(exc)) from exc
        elif self.deployment is not None:
            raise ValueError(
                f"`deployment:` is only valid on kind 'clients' (got kind "
                f"'{self.kind}'); a deployment manifest names itself with `name:`"
            )
        return self

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
