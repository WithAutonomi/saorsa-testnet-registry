import os
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.client_extension import (
    ExtensionError,
    highest_client_indexes,
    terraform_name,
    validate_extension_name,
)
from saorsa_deploy.client_roles import (  # noqa: F401  (re-exported for callers)
    ALL_CLIENT_ROLES,
    BROWSER_DEFAULT_DOWNLOAD_TIMEOUT_SECS,
    BROWSER_DEFAULT_INSTANCES,
    BROWSER_DEFAULT_ROUND_PERIOD_SECS,
    BROWSER_DEFAULT_VM_SIZE,
    BROWSER_MAX_INSTANCES,
    BROWSER_MIN_ROUND_PERIOD_SECS,
    ROLE_BROWSER_DOWNLOAD,
    ROLE_DOWNLOAD,
    ROLE_LABELS,
    ROLE_ORDER,
    ROLE_POINTER,
    ROLE_UPLOAD,
    ROLE_WASM_DOWNLOAD,
    ROLE_WASM_UPLOAD,
    UPLOAD_ROLES,
    WASM_ROLES,
    is_pointer,
)
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import CLIENT_PROVIDERS, resolve_client_provider
from saorsa_deploy.provisioning.clock_skew import format_skew_offset, parse_skew_duration
from saorsa_deploy.provisioning.network_profile import NetworkProfileError, resolve_network
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_client_state,
    save_client_state,
    update_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

DEFAULT_VM_SIZE = "s-2vcpu-4gb"


_VALID_PAYMENT_VALUES = ("merkle", "no-merkle")
# NB: `rust_log` is NOT in these key sets — it is extracted as a trailing
# remainder token before the `:`-split (its value may contain `:` via RUST_LOG
# module paths like `a::b=trace`), so the `key=value` loop never sees it.
_VALID_UPLOADER_KEYS = (
    "payment",
    "ant",
    "eth",
    "clock_skew",
    "bootstrap_cache",
    "forward_logs",
    "network",
)
_VALID_DOWNLOADER_KEYS = ("clock_skew", "bootstrap_cache", "forward_logs", "network")
# No `bootstrap_cache` on either WASM role: that option chooses between ant's
# peer cache and an explicit --bootstrap invocation, and a WASM client does
# neither. It seeds from the bootstrap tier's webrtc-direct multiaddrs and
# discovers the rest of the network over authenticated find_node.
_VALID_WASM_UPLOADER_KEYS = ("payment", "ant", "eth", "clock_skew", "forward_logs", "network")
_VALID_WASM_DOWNLOADER_KEYS = ("clock_skew", "forward_logs", "network")
# A pointer client carries a wallet (every write is a paid quote) and the
# workload rates, which are what the run's cost and its Arbitrum Sepolia
# transaction rate are derived from.
_VALID_POINTER_KEYS = (
    "ant",
    "eth",
    "clock_skew",
    "bootstrap_cache",
    "forward_logs",
    "network",
    "create_interval_secs",
    "update_interval_secs",
    "sweep_interval_secs",
    "chain_fraction",
    "transfer_interval_secs",
    "pointer_max",
)
# A browser downloader takes no wallet, no clock skew and no network shaping:
# what it measures is WHEN each instance read and WHICH holder served it, and a
# shifted clock or a shaped link would distort exactly that. `rust_log` is absent
# too -- the transfers run inside Chromium, not in a Rust process whose
# subscriber there is to configure.
_VALID_BROWSER_DOWNLOADER_KEYS = (
    "instances",
    "round_period_secs",
    "download_timeout_secs",
    "forward_logs",
)
DEFAULT_ANT_FUNDING = "100"
DEFAULT_ETH_FUNDING = "0.5"
# ant peer cache default: invoke ant WITHOUT --bootstrap, shipping a
# bootstrap_peers.toml instead. bootstrap_cache=false restores --bootstrap.
DEFAULT_BOOTSTRAP_CACHE = True
# Pointer workload defaults. Mirror config/models.py's POINTER_* constants —
# duplicated rather than imported so the CLI does not depend on the manifest
# schema, exactly as DEFAULT_ANT_FUNDING does. `test_pointer_clients.py` asserts
# the two agree.
DEFAULT_POINTER_CREATE_INTERVAL_SECS = 60
DEFAULT_POINTER_UPDATE_INTERVAL_SECS = 30
DEFAULT_POINTER_SWEEP_INTERVAL_SECS = 600
DEFAULT_POINTER_CHAIN_FRACTION = 0.10
# 0 = no handover loop. The only pointer interval whose zero is a setting rather
# than a busy loop: `ant pointer transfer` does not exist on a pre-V2-1354 build.
DEFAULT_POINTER_TRANSFER_INTERVAL_SECS = 0
DEFAULT_POINTER_MAX = 0


def _parse_clock_skew(spec: str, value: str) -> int:
    """Validate a ``clock_skew=`` spec option into signed seconds.

    Accepts a Go-style signed duration (e.g. ``2h``, ``-2h``, ``30m``).
    """
    try:
        return parse_skew_duration(value)
    except ValueError as e:
        raise ValueError(f"Invalid spec '{spec}': {e}")


def _parse_bootstrap_cache(spec: str, value: str) -> bool:
    """Validate a ``bootstrap_cache=`` spec option into a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"Invalid spec '{spec}': bootstrap_cache must be 'true' or 'false', got '{value}'."
    )


def _parse_forward_logs(spec: str, value: str) -> bool:
    """Validate a ``forward_logs=`` spec option into a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"Invalid spec '{spec}': forward_logs must be 'true' or 'false', got '{value}'."
    )


def _parse_network(spec: str, value: str) -> str:
    """Validate a ``network=`` spec option as a built-in profile name.

    Only a bare name is accepted here: this grammar is colon-delimited and a raw
    parameter set ("rate_up=1mbit,delay=30ms") would collide with it. Inline
    overrides are manifest-only. Resolution happens again at provisioning time;
    this is a fail-fast check so a typo doesn't surface after infrastructure has
    already been created.
    """
    try:
        resolve_network(value)
    except NetworkProfileError as e:
        raise ValueError(f"Invalid spec '{spec}': {e}")
    return value


def _extract_rust_log(spec: str) -> tuple[str, str | None]:
    """Split off a trailing ``rust_log=...`` token from a client spec.

    ``rust_log`` must be the LAST option in the spec because its value can
    contain ``:`` (RUST_LOG module paths use ``::``), which would otherwise
    collide with the ``:`` spec delimiter. Everything after the first
    ``:rust_log=`` is taken verbatim as the value. Returns
    ``(spec_without_rust_log, rust_log_or_None)``.
    """
    marker = ":rust_log="
    idx = spec.find(marker)
    if idx == -1:
        if spec.startswith("rust_log="):
            idx, marker = 0, "rust_log="
        else:
            return spec, None
    value = spec[idx + len(marker) :]
    if not value:
        raise ValueError(f"Invalid spec '{spec}': rust_log cannot be empty.")
    return spec[:idx], value


def _parse_positive_decimal(spec: str, key: str, value: str) -> str:
    """Validate a numeric uploader-spec option and return it as a normalized string."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(
            f"Invalid uploader spec '{spec}': '{key}' must be a number, got '{value}'."
        )
    if amount <= 0:
        raise ValueError(f"Invalid uploader spec '{spec}': '{key}' must be > 0, got '{value}'.")
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, "f").rstrip("0").rstrip(".") or "0"


def _split_spec(spec: str, valid_keys: tuple, label: str) -> tuple[list[str], dict, str | None]:
    """Shared token split for every client spec grammar.

    Returns ``(positional, options, rust_log)``. ``rust_log`` is pulled off the
    end BEFORE the ``:``-split because its value can contain ``:`` (RUST_LOG
    module paths use ``::``).
    """
    spec_body, rust_log = _extract_rust_log(spec)
    positional: list[str] = []
    options: dict[str, str] = {}
    for part in spec_body.split(":"):
        if "=" in part:
            key, value = part.split("=", 1)
            if key not in valid_keys:
                raise ValueError(f"Invalid {label} spec '{spec}': unknown key '{key}'.")
            if key in options:
                raise ValueError(f"Invalid {label} spec '{spec}': duplicate key '{key}'.")
            options[key] = value
        else:
            positional.append(part)
    return positional, options, rust_log


def _common_options(spec: str, options: dict, valid_keys: tuple) -> dict:
    """The options every client role shares."""
    out = {
        "clock_skew": (
            _parse_clock_skew(spec, options["clock_skew"]) if "clock_skew" in options else None
        ),
        "forward_logs": (
            _parse_forward_logs(spec, options["forward_logs"])
            if "forward_logs" in options
            else None
        ),
        "network": _parse_network(spec, options["network"]) if "network" in options else None,
    }
    # bootstrap_cache is native-only: it selects between ant's peer cache and an
    # explicit --bootstrap invocation, neither of which exists on the WASM path.
    if "bootstrap_cache" in valid_keys:
        out["bootstrap_cache"] = (
            _parse_bootstrap_cache(spec, options["bootstrap_cache"])
            if "bootstrap_cache" in options
            else DEFAULT_BOOTSTRAP_CACHE
        )
    return out


def _parse_uploader_like(spec: str, valid_keys: tuple, role: str, label: str) -> dict:
    """Parse ``provider:region:file_size_mb[:vm_size][:key=value...]``."""
    positional, options, rust_log = _split_spec(spec, valid_keys, label)

    if len(positional) < 3 or len(positional) > 4:
        raise ValueError(
            f"Invalid {label} spec '{spec}'. "
            f"Expected format: provider:region:file_size_mb[:vm_size]"
            f"[:payment=MODE][:ant=N][:eth=N]"
        )
    try:
        provider = resolve_client_provider(positional[0]).name
    except ValueError as e:
        raise ValueError(f"Invalid {label} spec '{spec}': {e}")
    region = positional[1]
    if not region:
        raise ValueError(f"Invalid {label} spec '{spec}': region cannot be empty.")
    try:
        file_size_mb = int(positional[2])
    except ValueError:
        raise ValueError(
            f"Invalid file size '{positional[2]}' in {label} spec '{spec}'. "
            f"Must be an integer (MB)."
        )
    vm_size = positional[3] if len(positional) == 4 else DEFAULT_VM_SIZE

    payment_type: str | None = options.get("payment")
    if payment_type is not None and payment_type not in _VALID_PAYMENT_VALUES:
        raise ValueError(
            f"Invalid {label} spec '{spec}': payment must be one of "
            f"{', '.join(_VALID_PAYMENT_VALUES)}."
        )

    ant_funding = (
        _parse_positive_decimal(spec, "ant", options["ant"])
        if "ant" in options
        else DEFAULT_ANT_FUNDING
    )
    eth_funding = (
        _parse_positive_decimal(spec, "eth", options["eth"])
        if "eth" in options
        else DEFAULT_ETH_FUNDING
    )

    return {
        "role": role,
        "provider": provider,
        "region": region,
        "file_size_mb": file_size_mb,
        "vm_size": vm_size,
        "payment_type": payment_type,
        "ant_funding": ant_funding,
        "eth_funding": eth_funding,
        "rust_log": rust_log,
        **_common_options(spec, options, valid_keys),
    }


def _parse_downloader_like(spec: str, valid_keys: tuple, role: str, label: str) -> dict:
    """Parse ``provider:region[:vm_size][:key=value...]``."""
    positional, options, rust_log = _split_spec(spec, valid_keys, label)

    if len(positional) < 2 or len(positional) > 3:
        raise ValueError(
            f"Invalid {label} spec '{spec}'. "
            f"Expected format: provider:region[:vm_size][:clock_skew=DURATION]"
        )
    try:
        provider = resolve_client_provider(positional[0]).name
    except ValueError as e:
        raise ValueError(f"Invalid {label} spec '{spec}': {e}")
    region = positional[1]
    if not region:
        raise ValueError(f"Invalid {label} spec '{spec}': region cannot be empty.")
    vm_size = positional[2] if len(positional) == 3 else DEFAULT_VM_SIZE

    return {
        "role": role,
        "provider": provider,
        "region": region,
        "vm_size": vm_size,
        "rust_log": rust_log,
        **_common_options(spec, options, valid_keys),
    }


def parse_uploader_spec(spec: str) -> dict:
    """Parse an uploader spec.

    Format:
    ``provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:network=PROFILE][:rust_log=VALUE]``

    ``provider`` is mandatory and must be one of digital_ocean, vultr,
    hetzner, ovh. Region names and VM sizes vary per provider and are passed
    through verbatim — the user is responsible for supplying valid strings.
    ``rust_log``, if present, MUST be the final token (its value may contain
    ``:``).

    Returns a dict with keys: role=upload, provider (canonical internal name),
    region, file_size_mb, vm_size, payment_type (None unless ``payment=merkle``
    or ``payment=no-merkle``), ant_funding (str numeric, default "100"),
    eth_funding (str numeric, default "0.5"), bootstrap_cache (bool, default
    True — invoke ant without --bootstrap, shipping a bootstrap_peers.toml),
    forward_logs (bool or None), rust_log (str or None).
    """
    return _parse_uploader_like(spec, _VALID_UPLOADER_KEYS, ROLE_UPLOAD, "uploader")


def parse_downloader_spec(spec: str) -> dict:
    """Parse a downloader spec:
    'provider:region[:vm_size][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL]
    [:network=PROFILE][:rust_log=VALUE]'.

    ``provider`` is mandatory (digital_ocean, vultr, hetzner, ovh). ``rust_log``,
    if present, MUST be the final token (its value may contain ``:``). Returns a
    dict with keys: role=download, provider (canonical internal name), region,
    vm_size, clock_skew (signed seconds int, or None when unset),
    bootstrap_cache (bool, default True), forward_logs (bool or None), rust_log
    (str or None).
    """
    return _parse_downloader_like(spec, _VALID_DOWNLOADER_KEYS, ROLE_DOWNLOAD, "downloader")


def parse_wasm_uploader_spec(spec: str) -> dict:
    """Parse a WASM uploader spec:
    ``provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N]``
    ``[:clock_skew=DURATION][:forward_logs=BOOL][:network=PROFILE][:rust_log=VALUE]``

    Identical to :func:`parse_uploader_spec` minus ``bootstrap_cache``, which has
    no meaning on the WASM path. ``rust_log``, if present, MUST be the final
    token (its value may contain ``:``).
    """
    parsed = _parse_uploader_like(
        spec, _VALID_WASM_UPLOADER_KEYS, ROLE_WASM_UPLOAD, "wasm uploader"
    )
    return parsed


def parse_wasm_downloader_spec(spec: str) -> dict:
    """Parse a WASM downloader spec:
    ``provider:region[:vm_size][:clock_skew=DURATION][:forward_logs=BOOL]``
    ``[:network=PROFILE][:rust_log=VALUE]``

    Identical to :func:`parse_downloader_spec` minus ``bootstrap_cache``.
    """
    return _parse_downloader_like(
        spec, _VALID_WASM_DOWNLOADER_KEYS, ROLE_WASM_DOWNLOAD, "wasm downloader"
    )


def parse_browser_downloader_spec(spec: str) -> dict:
    """Parse a browser-downloader spec.

    Format: ``provider:region[:vm_size][:instances=N][:round_period_secs=N]``
    ``[:download_timeout_secs=N][:forward_logs=BOOL]``

    Positionals match ``--downloader`` (nothing is uploaded and there is no file
    size — the file is named by `clients select-hot-files`). The three integer
    options carry the same limits the manifest schema enforces, so a hand-typed
    `clients infra` cannot stand up a cohort a manifest would have refused.
    """
    parsed = _parse_downloader_like(
        spec, _VALID_BROWSER_DOWNLOADER_KEYS, ROLE_BROWSER_DOWNLOAD, "browser downloader"
    )
    _, options, _ = _split_spec(spec, _VALID_BROWSER_DOWNLOADER_KEYS, "browser downloader")

    # `rust_log` is pulled off the end of every spec before the key check, so
    # the unknown-key loop never sees it and it would be accepted here and then
    # dropped. Refused by name instead: the transfers run inside Chromium, and
    # an operator reading their own argv back as a description of the run must
    # not find a setting in it that nothing applied.
    if parsed.get("rust_log") is not None:
        raise ValueError(
            f"Invalid browser downloader spec '{spec}': rust_log has no meaning "
            f"on this role — the transfers run inside Chromium, not in a Rust "
            f"process whose tracing subscriber there is to configure."
        )

    # `_parse_downloader_like` applies the shared client default; this role's
    # default is a size up, because four Chromium instances each holding ~4x
    # their file in WASM memory do not fit in 4 GB.
    if parsed["vm_size"] == DEFAULT_VM_SIZE and ":" + DEFAULT_VM_SIZE not in spec:
        parsed["vm_size"] = BROWSER_DEFAULT_VM_SIZE

    instances = (
        _parse_browser_int(spec, "instances", options["instances"])
        if "instances" in options
        else BROWSER_DEFAULT_INSTANCES
    )
    if not 1 <= instances <= BROWSER_MAX_INSTANCES:
        raise ValueError(
            f"Invalid browser downloader spec '{spec}': instances must be between "
            f"1 and {BROWSER_MAX_INSTANCES}, got {instances}. A node admits at "
            f"most {BROWSER_MAX_INSTANCES} WebRTC sessions per source IPv4 "
            f"address (max_connections_per_ip), and one browser client holds one "
            f"session per node, so a further instance would be refused by every "
            f"node it dials."
        )
    round_period = (
        _parse_browser_int(spec, "round_period_secs", options["round_period_secs"])
        if "round_period_secs" in options
        else BROWSER_DEFAULT_ROUND_PERIOD_SECS
    )
    if round_period < BROWSER_MIN_ROUND_PERIOD_SECS:
        raise ValueError(
            f"Invalid browser downloader spec '{spec}': round_period_secs must be "
            f">= {BROWSER_MIN_ROUND_PERIOD_SECS}, got {round_period}. A shorter "
            f"round is less than a cold browser client's own startup."
        )
    download_timeout = (
        _parse_browser_int(spec, "download_timeout_secs", options["download_timeout_secs"])
        if "download_timeout_secs" in options
        else BROWSER_DEFAULT_DOWNLOAD_TIMEOUT_SECS
    )
    if download_timeout >= round_period:
        raise ValueError(
            f"Invalid browser downloader spec '{spec}': download_timeout_secs "
            f"({download_timeout}) must be < round_period_secs ({round_period}), "
            f"or a round can still be running when the next one is due and the "
            f"cohort stops being simultaneous."
        )

    parsed["instances"] = instances
    parsed["round_period_secs"] = round_period
    parsed["download_timeout_secs"] = download_timeout
    return parsed


def _parse_browser_int(spec: str, key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValueError(
            f"Invalid browser downloader spec '{spec}': '{key}' must be an integer, got '{value}'."
        )


def _parse_positive_int(spec: str, key: str, value: str) -> int:
    """Validate an integer spec option that must be strictly positive."""
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(
            f"Invalid pointer spec '{spec}': '{key}' must be an integer, got '{value}'."
        )
    if parsed <= 0:
        raise ValueError(
            f"Invalid pointer spec '{spec}': '{key}' must be > 0, got {parsed}. A "
            "zero interval is a busy loop against a paid operation, not a "
            "disabled one."
        )
    return parsed


def parse_pointer_spec(spec: str) -> dict:
    """Parse a pointer-client spec.

    Format:
    ``provider:region[:vm_size][:ant=N][:eth=N][:clock_skew=DURATION][:bootstrap_cache=BOOL][:forward_logs=BOOL][:network=PROFILE][:create_interval_secs=N][:update_interval_secs=N][:sweep_interval_secs=N][:chain_fraction=F][:transfer_interval_secs=N][:pointer_max=N][:rust_log=VALUE]``

    Positionals match ``--downloader`` (there is no file size — a pointer record
    is a fixed ~5 KB), but the role carries an uploader's wallet options because
    every pointer write is a paid single-node quote. ``rust_log``, if present,
    MUST be the final token (its value may contain ``:``).
    """
    parsed = _parse_downloader_like(spec, _VALID_POINTER_KEYS, ROLE_POINTER, "pointer")
    _, options, _ = _split_spec(spec, _VALID_POINTER_KEYS, "pointer")

    # A pointer client has no file to upload, but it IS an upload-type role: it
    # mints a per-VM wallet, is topped up from the funding wallet and is drained
    # on destroy. `file_size_mb: 0` is what lets every one of those shared paths
    # treat it as one without special-casing (the same value the prod uploaders
    # carry, where the files are real and of arbitrary size).
    parsed["file_size_mb"] = 0
    parsed["payment_type"] = None
    parsed["ant_funding"] = (
        _parse_positive_decimal(spec, "ant", options["ant"])
        if "ant" in options
        else DEFAULT_ANT_FUNDING
    )
    parsed["eth_funding"] = (
        _parse_positive_decimal(spec, "eth", options["eth"])
        if "eth" in options
        else DEFAULT_ETH_FUNDING
    )
    parsed["create_interval_secs"] = (
        _parse_positive_int(spec, "create_interval_secs", options["create_interval_secs"])
        if "create_interval_secs" in options
        else DEFAULT_POINTER_CREATE_INTERVAL_SECS
    )
    parsed["update_interval_secs"] = (
        _parse_positive_int(spec, "update_interval_secs", options["update_interval_secs"])
        if "update_interval_secs" in options
        else DEFAULT_POINTER_UPDATE_INTERVAL_SECS
    )
    parsed["sweep_interval_secs"] = (
        _parse_positive_int(spec, "sweep_interval_secs", options["sweep_interval_secs"])
        if "sweep_interval_secs" in options
        else DEFAULT_POINTER_SWEEP_INTERVAL_SECS
    )
    if "chain_fraction" in options:
        try:
            fraction = float(options["chain_fraction"])
        except ValueError:
            raise ValueError(
                f"Invalid pointer spec '{spec}': chain_fraction must be a number "
                f"between 0 and 1, got '{options['chain_fraction']}'."
            )
        if not 0.0 <= fraction <= 1.0:
            raise ValueError(
                f"Invalid pointer spec '{spec}': chain_fraction must be between 0 "
                f"and 1, got {fraction}."
            )
        parsed["chain_fraction"] = fraction
    else:
        parsed["chain_fraction"] = DEFAULT_POINTER_CHAIN_FRACTION
    if "transfer_interval_secs" in options:
        # Not `_parse_positive_int`: 0 is the legal "no handover loop" value, so
        # the >0 refusal that protects the paid create/update loops from a busy
        # loop would reject the default here.
        try:
            transfer_interval = int(options["transfer_interval_secs"])
        except ValueError:
            raise ValueError(
                f"Invalid pointer spec '{spec}': transfer_interval_secs must be "
                f"an integer, got '{options['transfer_interval_secs']}'."
            )
        if transfer_interval < 0:
            raise ValueError(
                f"Invalid pointer spec '{spec}': transfer_interval_secs must be "
                f">= 0 (0 = no handover loop), got {transfer_interval}."
            )
        parsed["transfer_interval_secs"] = transfer_interval
    else:
        parsed["transfer_interval_secs"] = DEFAULT_POINTER_TRANSFER_INTERVAL_SECS
    if "pointer_max" in options:
        try:
            cap = int(options["pointer_max"])
        except ValueError:
            raise ValueError(
                f"Invalid pointer spec '{spec}': pointer_max must be an integer, "
                f"got '{options['pointer_max']}'."
            )
        if cap < 0:
            raise ValueError(
                f"Invalid pointer spec '{spec}': pointer_max must be >= 0 "
                f"(0 = uncapped), got {cap}."
            )
        parsed["pointer_max"] = cap
    else:
        parsed["pointer_max"] = DEFAULT_POINTER_MAX
    return parsed


def _spec_signature(spec: dict) -> tuple:
    """Identity tuple for matching requested vs. recorded client specs."""
    return (
        spec.get("role"),
        spec.get("provider"),
        spec.get("region"),
        spec.get("client_index"),
        spec.get("vm_size"),
        spec.get("file_size_mb"),
        spec.get("payment_type"),
        spec.get("ant_funding"),
        spec.get("eth_funding"),
        spec.get("clock_skew"),
        spec.get("bootstrap_cache", DEFAULT_BOOTSTRAP_CACHE),
        spec.get("forward_logs"),
        spec.get("rust_log"),
        spec.get("network"),
        # Pointer workload rates. Part of the identity so that changing a rate
        # in the manifest is a spec MISMATCH rather than a silent reuse of VMs
        # still running the old rates — the same reason every other spec field
        # is here. `None` on every non-pointer role, so their signature is
        # unchanged and existing client state still matches.
        spec.get("create_interval_secs"),
        spec.get("update_interval_secs"),
        spec.get("sweep_interval_secs"),
        spec.get("chain_fraction"),
        # `or 0` for this one key, because 0 IS the default and "the key is
        # absent" means exactly the same thing: no handover loop. Without it,
        # client state recorded before V2-1354 would MISMATCH a request that
        # takes the default, and a resumed run would refuse to reuse VMs that
        # are running precisely what was asked for. A positive value still
        # mismatches, which is the point of the field being in the signature.
        spec.get("transfer_interval_secs") or 0,
        spec.get("pointer_max"),
        # Browser-downloader knobs. In the signature for the same reason the
        # pointer rates are: changing one in the manifest must be a spec
        # MISMATCH rather than a silent reuse of VMs still running the old
        # value. `None` on every other role, so their signature is unchanged.
        spec.get("instances"),
        spec.get("round_period_secs"),
        spec.get("download_timeout_secs"),
    )


def _merge_client_specs(
    created: list[dict], resumed: list[dict], blank_new: bool = False
) -> list[dict]:
    """Full client list: the VMs kept from a previous run plus the new ones.

    Restores the canonical uploaders-then-downloaders ordering so a resumed
    state file is indistinguishable from one written by a clean run.
    """
    out = [dict(c) for c in resumed]
    out.extend({**spec, "ip": ""} if blank_new else dict(spec) for spec in created)
    # Canonical ordering so a resumed state file is indistinguishable from a
    # clean run's. Roles are ranked explicitly rather than by an upload/download
    # boolean, which would interleave the native and WASM tiers.
    out.sort(key=lambda s: (ROLE_ORDER.get(s["role"], 99), s["client_index"]))
    return out


def _print_client_line(console, spec: dict) -> None:
    """Render one already-provisioned client VM."""
    label = ROLE_LABELS.get(spec["role"], spec["role"])
    skew_str = (
        f", clock-skew {format_skew_offset(spec['clock_skew'])}" if spec.get("clock_skew") else ""
    )
    console.print(
        f"  {label} #{spec['client_index']}: {spec.get('ip', 'N/A')} "
        f"({spec.get('provider', 'digitalocean')}:{spec['region']}, "
        f"{spec['vm_size']}{skew_str})"
    )


def _client_specs_match(existing: list[dict], requested: list[dict]) -> bool:
    if len(existing) != len(requested):
        return False
    return sorted(_spec_signature(s) for s in existing) == sorted(
        _spec_signature(s) for s in requested
    )


def _resolve_extension(args, console) -> str | None:
    """Validate ``--extension`` and return it, or None when it is absent."""
    extension = getattr(args, "extension", None)
    if extension is None:
        return None
    try:
        return validate_extension_name(extension)
    except ExtensionError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(2)


def cmd_infra_clients(args):
    """Execute the clients infra command: create client VMs via Terraform."""
    console = Console()

    extension = _resolve_extension(args, console)
    # The Terraform `name` variable, which is what namespaces the VM hostname,
    # the workspace directory and the tfstate key — all three are built from it.
    tf_name = terraform_name(args.name, extension)

    # (flag value, parser) per role. Each role is indexed from 1 independently,
    # because the index is part of the VM hostname and the Terraform state key,
    # which are already namespaced by role.
    role_inputs = [
        (args.uploader or [], parse_uploader_spec),
        (args.downloader or [], parse_downloader_spec),
        (getattr(args, "wasm_uploader", None) or [], parse_wasm_uploader_spec),
        (getattr(args, "wasm_downloader", None) or [], parse_wasm_downloader_spec),
        (getattr(args, "pointer", None) or [], parse_pointer_spec),
        (getattr(args, "browser_downloader", None) or [], parse_browser_downloader_spec),
    ]

    if not any(raw for raw, _ in role_inputs):
        console.print(
            "[bold red]Error:[/bold red] At least one --uploader, --downloader, "
            "--wasm-uploader, --wasm-downloader, --pointer or "
            "--browser-downloader spec is required."
        )
        sys.exit(1)

    # An extension's indexes CONTINUE from the highest already in use for that
    # role across the deployment — its own clients and its other extensions —
    # because the per-uploader S3 address key is `upload-<region>-<n>.txt` and
    # each flush is a whole-object PUT. Two uploaders sharing an index silently
    # erase each other's address list on every flush. The deployment's own tier
    # keeps starting at 1, so nothing about it changes.
    base_indexes: dict[str, int] = {}
    if extension:
        try:
            base_indexes = highest_client_indexes(args.name, exclude=extension)
        except Exception as e:  # noqa: BLE001 — any scan failure must stop the run
            # Allocating from an incomplete scan would hand this tier an index a
            # live uploader already owns, and each flush of the loser's address
            # object is a whole-object PUT. Stop before any VM is created.
            console.print(
                f"[bold red]Error:[/bold red] could not determine the client "
                f"indexes already in use for '{args.name}': {e}. Refusing to "
                f"allocate, because an index that collides with a live "
                f"uploader's would erase its S3 address list on every flush. "
                f"Retry once the registry's S3 state is reachable."
            )
            sys.exit(2)

    client_specs: list[dict] = []
    try:
        for raw_specs, parser in role_inputs:
            for i, spec_str in enumerate(raw_specs, start=1):
                spec = parser(spec_str)
                spec["client_index"] = base_indexes.get(spec["role"], 0) + i
                if extension is not None:
                    spec["extension"] = extension
                client_specs.append(spec)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if extension is not None:
        console.print(
            f"[bold]Creating {len(client_specs)} client VM(s) for extension "
            f"'{extension}' of {args.name}...[/bold]"
        )
    else:
        console.print(f"[bold]Creating {len(client_specs)} client VM(s) for {args.name}...[/bold]")
    for spec in client_specs:
        skew_str = (
            f", clock-skew {format_skew_offset(spec['clock_skew'])}"
            if spec.get("clock_skew")
            else ""
        )
        label = ROLE_LABELS.get(spec["role"], spec["role"])
        if is_pointer(spec["role"]):
            ant = spec.get("ant_funding", DEFAULT_ANT_FUNDING)
            eth = spec.get("eth_funding", DEFAULT_ETH_FUNDING)
            transfer_interval = spec.get("transfer_interval_secs") or 0
            handover = f"transfer/{transfer_interval}s, " if transfer_interval else ""
            console.print(
                f"  {label} #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} ({spec['vm_size']}, "
                f"create/{spec['create_interval_secs']}s "
                f"update/{spec['update_interval_secs']}s "
                f"sweep/{spec['sweep_interval_secs']}s, "
                f"chain {spec['chain_fraction']:.0%}, "
                f"{handover}"
                f"fund {ant} ANT + {eth} ETH{skew_str})"
            )
        elif spec["role"] == ROLE_BROWSER_DOWNLOAD:
            console.print(
                f"  {label} #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} ({spec['vm_size']}, "
                f"{spec['instances']} Chromium instance(s), "
                f"round/{spec['round_period_secs']}s, "
                f"timeout {spec['download_timeout_secs']}s)"
            )
        elif spec["role"] in UPLOAD_ROLES:
            payment = spec.get("payment_type")
            payment_str = f", payment={payment}" if payment else ""
            ant = spec.get("ant_funding", DEFAULT_ANT_FUNDING)
            eth = spec.get("eth_funding", DEFAULT_ETH_FUNDING)
            console.print(
                f"  {label} #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} "
                f"({spec['vm_size']}, {spec['file_size_mb']}MB uploads"
                f"{payment_str}, fund {ant} ANT + {eth} ETH{skew_str})"
            )
        else:
            console.print(
                f"  {label} #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} ({spec['vm_size']}{skew_str})"
            )
    console.print()

    # Idempotent reuse: if our saorsa-side client state already records every
    # VM in this request (with a populated IP), skip the terraform apply.
    # Mirrors `bootstraps infra` so re-running a deploy runbook after a
    # mid-pipeline failure doesn't trip on its own previously-provisioned VMs.
    try:
        existing_state = load_client_state(args.name, extension)
    except RuntimeError:
        existing_state = None

    # `resumed_ips` maps a spec signature to the IP of a VM that already exists,
    # so a partial failure re-creates only what is missing.
    resumed_ips: dict[tuple, str] = {}
    resumed_clients: list[dict] = []

    if existing_state and existing_state.get("clients"):
        existing_clients = existing_state["clients"]
        if not _client_specs_match(existing_clients, client_specs):
            scope = f"extension '{extension}' of '{args.name}'" if extension else f"'{args.name}'"
            destroy_flags = f"--name {args.name}" + (
                f" --extension {extension}" if extension else ""
            )
            console.print(
                f"[bold red]Error:[/bold red] Client state for {scope} "
                f"already exists but does not match the requested spec. "
                f"Run `clients destroy {destroy_flags} --force` first if you "
                f"want to recreate with a different layout."
            )
            sys.exit(2 if extension else 1)

        resumed_clients = [c for c in existing_clients if c.get("ip")]
        resumed_ips = {_spec_signature(c): c["ip"] for c in resumed_clients}
        if len(resumed_ips) == len(existing_clients):
            console.print(
                f"[bold]Reusing {len(existing_clients)} existing client VM(s) "
                f"for '{args.name}'.[/bold]"
            )
            for spec in existing_clients:
                _print_client_line(console, spec)
            return

        # Partial state: the spec matches, but some VMs never came up (a
        # provider capacity failure mid-apply). Keep the ones that did and
        # create only the rest — destroying a healthy client tier to recover
        # two VMs would churn the whole workload.
        client_specs = [spec for spec in client_specs if _spec_signature(spec) not in resumed_ips]
        console.print(
            f"[bold]Resuming: reusing {len(resumed_ips)} existing client VM(s), "
            f"creating the {len(client_specs)} that are missing.[/bold]"
        )
        for spec in existing_clients:
            if spec.get("ip"):
                _print_client_line(console, spec)
        console.print()

    # OVH targets a specific Public Cloud project; the manifest needs its UUID
    # as a Terraform variable. Mirror the migration command's env-var sourcing.
    ovh_service_name = None
    if any(spec["provider"] == "ovh" for spec in client_specs):
        ovh_service_name = os.environ.get("OVH_CLOUD_PROJECT_SERVICE")
        if not ovh_service_name:
            console.print(
                "[bold red]Error:[/bold red] One or more OVH clients were "
                "requested but OVH_CLOUD_PROJECT_SERVICE is not set. Export it "
                "(the OVH Public Cloud project ID) so the OVH manifest can "
                "target the right project."
            )
            sys.exit(1)

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in client_specs:
        info = CLIENT_PROVIDERS[spec["provider"]]
        tf_source = resources_dir / info.tf_dir
        workspace_dir = (
            workspace_base / f"{info.name}-client-{tf_name}-{spec['role']}"
            f"-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{info.state_key_prefix}-{tf_name}-{spec['role']}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": tf_name,
            "role": spec["role"],
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            info.size_var: spec["vm_size"],
        }
        if info.name == "ovh":
            variables["service_name"] = ovh_service_name
        config = TerraformRunConfig(
            provider=info.name,
            region=spec["region"],
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    # Check for stale Terraform state files. On a resume the state belonging to
    # a VM we are re-creating is not stale — it is that VM's own state, left by
    # the apply whose create call failed, and applying against it is correct.
    stale_keys = (
        []
        if resumed_ips
        else [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    )
    if stale_keys:
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        for key in stale_keys:
            console.print(f"[yellow]Warning: Deleting stale Terraform state: {key}[/yellow]")
            delete_terraform_state(key)

    # Save client state UPFRONT (with empty IPs) so that clients destroy can
    # always tear down whatever terraform may have started, even on partial
    # or total failure. IPs get merged in after the apply completes.
    upfront_specs = _merge_client_specs(client_specs, resumed_clients, blank_new=True)
    try:
        save_client_state(args.name, upfront_specs, extension)
    except Exception as e:
        console.print(f"[bold red]Failed to save initial client state:[/bold red] {e}")
        console.print(
            "[bold red]Aborting — without saved state, clients destroy "
            "couldn't clean up a partial apply.[/bold red]"
        )
        sys.exit(1)

    results = execute_terraform_runs(configs)

    # Merge whichever IPs came back (partial success included).
    for spec, result in zip(client_specs, results):
        if result.success and result.outputs.get("vm_ip"):
            spec["ip"] = result.outputs["vm_ip"]
        else:
            spec["ip"] = ""

    try:
        update_client_state(
            args.name,
            {"clients": _merge_client_specs(client_specs, resumed_clients)},
            extension,
        )
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to update client state: {e}[/yellow]")

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} client VM(s) failed.[/bold red]")
        sys.exit(1)

    console.print()
    console.print(
        f"[bold green]All {len(results)} client VM(s) provisioned successfully.[/bold green]"
    )
    for spec in client_specs:
        # ROLE_LABELS, not an upload/download ternary: that spelled every WASM
        # and pointer VM "Downloader" in the one place an operator reads to
        # check what was actually created.
        label = ROLE_LABELS.get(spec["role"], spec["role"])
        console.print(
            f"  {label} #{spec['client_index']}: {spec.get('ip', 'N/A')} "
            f"({spec['region']}, {spec['vm_size']})"
        )
