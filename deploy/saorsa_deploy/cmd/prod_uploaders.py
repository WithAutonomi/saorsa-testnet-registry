"""Higher-level orchestration of a prod uploaders deployment.

A prod uploaders deployment is a multi-VM client deployment that runs
``ant --json file upload --public`` against the **production** Autonomi
network. Each uploader is given a list of public source URLs; the service
downloads every URL in turn, uploads it, records per-file metrics in Influx,
and exits when its list is exhausted. Teardown is explicit via
``prod uploaders destroy``.

Like the prod continuous-downloader command, this is a thin orchestrator:
it shells out to existing primitive commands (``clients infra``,
``monitoring infra``, ``monitoring provision``, ``clients destroy``,
``monitoring destroy``) and uses a custom prod-uploader Pyinfra provisioner
for the per-host service.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.client_roles import ROLE_UPLOAD, ROLE_WASM_UPLOAD
from saorsa_deploy.cmd.prod_wasm_cli import resolve_wasm_context, wasm_deploy_args
from saorsa_deploy.config.models import VALID_LOG_LEVELS
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.clock_skew import parse_skew_duration
from saorsa_deploy.provisioning.network_profile import (
    NetworkProfileError,
    resolve_network,
)
from saorsa_deploy.provisioning.prod_uploader import (
    SERVICE_NAME,
    ProdUploaderProvisioner,
)
from saorsa_deploy.registry import (
    get_repository,
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_client_state, load_monitoring_state

MONITORING_REGION = "lon1"
DEFAULT_MONITORING_VOLUME_SIZE = 50


# `rust_log` is a known key so the tail-rejoin below keeps its value intact even
# when it contains ':' (RUST_LOG module paths use '::'); keep it last in a spec.
_KNOWN_SPEC_KEYS = (
    "urls",
    "pool",
    "pool_min_size",
    "pool_max_size",
    "exclude_partial",
    "payment",
    "log_level",
    "clock_skew",
    "network",
    "bootstrap_cache",
    "forward_logs",
    "memory_profiling",
    "rust_log",
)
_VALID_PAYMENT_VALUES = ("merkle", "no-merkle")

# The tokens a browser-path uploader accepts, and the three it refuses. Refused
# rather than ignored: `bootstrap_cache` selects between ant's peer cache and an
# explicit `--bootstrap` and a WASM client does neither, while `memory_profiling`
# and `log_level` act on the `ant` process, which never runs. A silently-dropped
# token would leave the operator reading their own spec as a description of the
# run. They stay in the tokeniser's key set so the message can name them.
_FORBIDDEN_WASM_SPEC_KEYS = ("bootstrap_cache", "memory_profiling", "log_level")
_KNOWN_WASM_SPEC_KEYS = tuple(
    key for key in _KNOWN_SPEC_KEYS if key not in _FORBIDDEN_WASM_SPEC_KEYS
)


def _parse_bootstrap_cache_token(spec: str, value: str) -> bool:
    """Validate a ``bootstrap_cache=`` uploader-spec token into a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"Invalid uploader spec '{spec}': bootstrap_cache must be 'true' or "
        f"'false', got '{value}'."
    )


def _parse_forward_logs_token(spec: str, value: str) -> bool:
    """Validate a ``forward_logs=`` uploader-spec token into a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"Invalid uploader spec '{spec}': forward_logs must be 'true' or "
        f"'false', got '{value}'."
    )


def _parse_memory_profiling_token(spec: str, value: str) -> bool:
    """Validate a ``memory_profiling=`` uploader-spec token into a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"Invalid uploader spec '{spec}': memory_profiling must be 'true' or "
        f"'false', got '{value}'."
    )


def _parse_log_level_token(spec: str, value: str) -> str:
    """Validate a ``log_level=`` uploader-spec token (info|debug|trace)."""
    if value not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid uploader spec '{spec}': log_level must be one of "
            f"{', '.join(VALID_LOG_LEVELS)}, got '{value}'."
        )
    return value


def _parse_network_token(spec: str, value: str) -> str:
    """Validate a ``network=`` uploader-spec token.

    Fails fast so an unknown profile surfaces before any VM is created.
    """
    try:
        resolve_network(value)
    except NetworkProfileError as e:
        raise ValueError(f"Invalid spec '{spec}': {e}")
    return value

def _parse_clock_skew_token(spec: str, value: str) -> int:
    """Validate a ``clock_skew=`` uploader-spec token into signed seconds.

    Accepts a Go-style signed duration (e.g. ``2h``, ``-2h``, ``30m``).
    """
    try:
        return parse_skew_duration(value)
    except ValueError as e:
        raise ValueError(f"Invalid uploader spec '{spec}': {e}")


def _parse_spec_options(
    spec: str, parts: list[str], known_keys: tuple, label: str
) -> dict[str, str]:
    """Parse the ``key=value`` tail of an uploader spec into a dict.

    Tokens are rejoined with ':' so a path (or a ``pool=small:2`` request, or a
    ``rust_log`` filter containing ``::``) is preserved verbatim; a ':' starts a
    new token only when what follows is a KNOWN key. Shared by the native and
    browser-path parsers so the two can never disagree about tokenisation — they
    differ only in which keys they accept.
    """
    tail_tokens: list[str] = []
    buf = parts[0]
    for next_part in parts[1:]:
        if "=" in next_part and next_part.split("=", 1)[0] in known_keys:
            tail_tokens.append(buf)
            buf = next_part
        else:
            buf = f"{buf}:{next_part}"
    tail_tokens.append(buf)

    options: dict[str, str] = {}
    for token in tail_tokens:
        if "=" not in token:
            raise ValueError(
                f"Invalid {label} spec '{spec}': '{token}' is not key=value."
            )
        key, value = token.split("=", 1)
        if key not in known_keys:
            raise ValueError(
                f"Invalid {label} spec '{spec}': unknown key '{key}'."
            )
        if key in options:
            raise ValueError(
                f"Invalid {label} spec '{spec}': duplicate key '{key}'."
            )
        options[key] = value
    return options


def _parse_file_source(spec: str, options: dict, label: str) -> dict:
    """Resolve the ``urls=``/``pool=`` half of an uploader spec.

    Exactly one source. ``pool=`` draws from the managed file pool, which cannot
    return a file that has ever been handed out; ``urls=`` is the legacy
    hand-written list, where that guarantee comes only from the name-based
    duplicate preflight.
    """
    has_urls = bool(options.get("urls"))
    has_pool = bool(options.get("pool"))
    if has_urls and has_pool:
        raise ValueError(
            f"Invalid {label} spec '{spec}': set either urls=PATH or "
            "pool=CLASS:COUNT, not both."
        )
    if not has_urls and not has_pool:
        raise ValueError(
            f"Invalid {label} spec '{spec}'. One of urls=PATH or "
            "pool=CLASS:COUNT (e.g. pool=small:3,medium:2) is required."
        )

    pool_request = None
    pool_min_size_bytes = None
    pool_max_size_bytes = None
    if has_pool:
        from saorsa_deploy.pool_selection import (
            PoolSelectionError,
            parse_request_token,
            parse_size_bounds,
            validate_bounds,
        )

        try:
            pool_request = parse_request_token(options["pool"])
            pool_min_size_bytes, pool_max_size_bytes = parse_size_bounds(
                options.get("pool_min_size"), options.get("pool_max_size")
            )
            validate_bounds(pool_request, pool_min_size_bytes, pool_max_size_bytes)
        except (PoolSelectionError, ValueError) as exc:
            raise ValueError(f"Invalid {label} spec '{spec}': {exc}") from exc
    elif options.get("pool_min_size") or options.get("pool_max_size"):
        # Silently ignoring them would leave the operator believing a size
        # restriction is in force over a hand-written URL list, which the pool
        # is not selecting from at all.
        raise ValueError(
            f"Invalid {label} spec '{spec}': pool_min_size/pool_max_size only "
            "apply to pool=… selection, not to urls=…"
        )

    return {
        "urls_file": options.get("urls") or None,
        "pool_request": pool_request,
        "pool_min_size_bytes": pool_min_size_bytes,
        "pool_max_size_bytes": pool_max_size_bytes,
    }


def parse_prod_uploader_spec(spec: str) -> dict:
    """Parse a ``--uploader`` spec.

    Format:
    ``REGION:VM_SIZE:urls=PATH[:payment=MODE][:log_level=LEVEL][:bootstrap_cache=BOOL][:forward_logs=BOOL][:rust_log=VALUE]``.
    Tail tokens are key=value; ``urls`` is required, ``payment`` is optional
    and must be ``merkle`` or ``no-merkle``, ``log_level`` is optional and must
    be ``info``/``debug``/``trace`` (omitted → info on the VM). ``forward_logs``
    forwards this VM's ant stdout+stderr to a dedicated Elasticsearch instance;
    ``rust_log`` (keep it last) sets RUST_LOG, overriding the verbosity flag.
    Returns a dict with keys ``region``, ``vm_size``, ``urls_file`` (str),
    ``payment_type`` (str | None), ``log_level`` (str | None),
    ``bootstrap_cache`` (bool, default True — invoke ant without --bootstrap,
    shipping a bootstrap_peers.toml), ``forward_logs`` (bool | None), and
    ``rust_log`` (str | None).
    """
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"Invalid uploader spec '{spec}'. "
            f"Expected format: region:vm_size:urls=PATH[:payment=MODE]"
        )

    region = parts[0]
    vm_size = parts[1]
    if not region:
        raise ValueError(f"Invalid uploader spec '{spec}': region cannot be empty.")
    if not vm_size:
        raise ValueError(f"Invalid uploader spec '{spec}': vm_size cannot be empty.")

    options = _parse_spec_options(spec, parts[2:], _KNOWN_SPEC_KEYS, "uploader")

    source = _parse_file_source(spec, options, "uploader")

    payment_type: str | None = options.get("payment")
    if payment_type is not None and payment_type not in _VALID_PAYMENT_VALUES:
        raise ValueError(
            f"Invalid uploader spec '{spec}': payment must be one of "
            f"{', '.join(_VALID_PAYMENT_VALUES)}."
        )

    log_level = (
        _parse_log_level_token(spec, options["log_level"])
        if "log_level" in options
        else None
    )
    network = _parse_network_token(spec, options["network"]) if "network" in options else None
    clock_skew = (
        _parse_clock_skew_token(spec, options["clock_skew"])
        if "clock_skew" in options
        else None
    )
    bootstrap_cache = (
        _parse_bootstrap_cache_token(spec, options["bootstrap_cache"])
        if "bootstrap_cache" in options
        else True
    )
    forward_logs = (
        _parse_forward_logs_token(spec, options["forward_logs"])
        if "forward_logs" in options
        else None
    )
    memory_profiling = (
        _parse_memory_profiling_token(spec, options["memory_profiling"])
        if "memory_profiling" in options
        else None
    )
    rust_log = options.get("rust_log") or None

    exclude_partial = (
        _parse_bootstrap_cache_token(spec, options["exclude_partial"])
        if "exclude_partial" in options
        else False
    )

    return {
        "region": region,
        "vm_size": vm_size,
        "client_kind": "native",
        **source,
        "exclude_partial": exclude_partial,
        "payment_type": payment_type,
        "log_level": log_level,
        "clock_skew": clock_skew,
        "network": network,
        "bootstrap_cache": bootstrap_cache,
        "forward_logs": forward_logs,
        "memory_profiling": memory_profiling,
        "rust_log": rust_log,
    }


def parse_prod_wasm_uploader_spec(spec: str) -> dict:
    """Parse a ``--wasm-uploader`` spec.

    Format: ``REGION[:VM_SIZE]:<tokens>``. ``VM_SIZE`` is optional — a
    browser-path transfer holds ~4x the file in WASM memory, so an omitted size
    takes the WASM default rather than the native tier's box. The accepted tokens
    are the native ones minus ``bootstrap_cache``/``memory_profiling``/
    ``log_level``, which are refused by name.

    Returns the same dict shape as :func:`parse_prod_uploader_spec` with
    ``client_kind="wasm"``, so everything downstream — selection, provisioning,
    the recorded ``file_names`` — handles one kind of entry.
    """
    from saorsa_deploy.prod_wasm import (
        check_wasm_pool_request,
        check_wasm_vm_size,
        clamp_wasm_pool_max_size,
        default_wasm_vm_size,
        wasm_pool_max_size_is_clamped,
    )

    parts = spec.split(":")
    region = parts[0]
    if not region:
        raise ValueError(
            f"Invalid wasm uploader spec '{spec}': region cannot be empty."
        )
    if len(parts) < 2:
        raise ValueError(
            f"Invalid wasm uploader spec '{spec}'. Expected format: "
            f"region[:vm_size]:urls=PATH or region[:vm_size]:pool=CLASS:COUNT"
        )
    # VM_SIZE is optional, so the second field is a size only when it is not
    # already the first key=value token.
    second = parts[1]
    if "=" in second and second.split("=", 1)[0] in _KNOWN_WASM_SPEC_KEYS:
        vm_size = None
        tail = parts[1:]
    else:
        vm_size = second or None
        tail = parts[2:]
    if not tail:
        raise ValueError(
            f"Invalid wasm uploader spec '{spec}'. One of urls=PATH or "
            "pool=CLASS:COUNT is required."
        )

    options = _parse_spec_options(
        spec,
        tail,
        # The forbidden keys are tokenised so they can be rejected by name; an
        # unknown-key error would not say why the token cannot be used here.
        _KNOWN_WASM_SPEC_KEYS + _FORBIDDEN_WASM_SPEC_KEYS,
        "wasm uploader",
    )
    offending = [key for key in _FORBIDDEN_WASM_SPEC_KEYS if key in options]
    if offending:
        raise ValueError(
            f"Invalid wasm uploader spec '{spec}': "
            f"{', '.join(offending)} cannot be used on a WASM uploader. The "
            "peer cache has no browser-path meaning, and heaptrack/log_level "
            "act on the `ant` process, which a WASM client never runs."
        )

    source = _parse_file_source(spec, options, "wasm uploader")
    problem = check_wasm_pool_request(source["pool_request"])
    if problem:
        raise ValueError(f"Invalid wasm uploader spec '{spec}': {problem}")
    if wasm_pool_max_size_is_clamped(
        source["pool_request"], source["pool_max_size_bytes"]
    ):
        # ant-core refuses a file above the browser cap before touching the
        # network, so a drawn over-cap file is a permanently-spent pool entry
        # that could never be uploaded. Narrow the draw instead. Only `medium`
        # straddles the cap, so only `medium` gains a bound — writing one onto a
        # tiny/small request would put a value in the persisted draw that
        # narrowed nothing.
        source["pool_max_size_bytes"] = clamp_wasm_pool_max_size(
            source["pool_max_size_bytes"]
        )

    if vm_size is None:
        vm_size = default_wasm_vm_size("digitalocean")
    else:
        size_problem = check_wasm_vm_size(vm_size, "digitalocean")
        if size_problem:
            raise ValueError(f"Invalid wasm uploader spec '{spec}': {size_problem}.")

    payment_type: str | None = options.get("payment")
    if payment_type is not None and payment_type not in _VALID_PAYMENT_VALUES:
        raise ValueError(
            f"Invalid wasm uploader spec '{spec}': payment must be one of "
            f"{', '.join(_VALID_PAYMENT_VALUES)}."
        )

    return {
        "region": region,
        "vm_size": vm_size,
        "client_kind": "wasm",
        **source,
        "exclude_partial": (
            _parse_forward_logs_token(spec, options["exclude_partial"])
            if "exclude_partial" in options
            else False
        ),
        "payment_type": payment_type,
        # Absent by construction on the browser path; kept in the dict so every
        # consumer reads one shape.
        "log_level": None,
        "clock_skew": (
            _parse_clock_skew_token(spec, options["clock_skew"])
            if "clock_skew" in options
            else None
        ),
        "network": (
            _parse_network_token(spec, options["network"])
            if "network" in options
            else None
        ),
        "bootstrap_cache": None,
        "forward_logs": (
            _parse_forward_logs_token(spec, options["forward_logs"])
            if "forward_logs" in options
            else None
        ),
        "memory_profiling": None,
        "rust_log": options.get("rust_log") or None,
    }


def _role_index(parsed_specs: list[dict], spec: dict) -> int:
    """The 1-based index `clients infra` will give this uploader's VM.

    Each ROLE is indexed from 1 independently, so a mixed deployment's native
    uploader #1 and WASM uploader #1 are different VMs with different hostnames.
    """
    peers = (
        _wasm_specs(parsed_specs)
        if spec.get("client_kind") == "wasm"
        else _native_specs(parsed_specs)
    )
    for index, candidate in enumerate(peers, start=1):
        if candidate is spec:
            return index
    raise ValueError("spec is not one of parsed_specs")


def _paying_address(args) -> str:
    """The address the shared wallet key signs from, or a note saying it is unknown.

    Every uploader — native and browser-path alike — signs with this one key, so
    naming the address is the only way for an operator to check the balance of
    the wallet that is about to spend real ETH. Best-effort and silent about
    failure: :func:`_resolve_secret_key` already refuses a missing key with a
    proper message, and a malformed one fails loudly at the first upload.
    """
    from eth_account import Account

    key = getattr(args, "secret_key", None) or os.environ.get(
        "ARBITRUM_ONE_SECRET_KEY"
    )
    if not key:
        return "unknown (no wallet key resolved yet)"
    try:
        return Account.from_key(key if key.startswith("0x") else f"0x{key}").address
    except Exception:
        return "unknown (could not derive from the wallet key)"


def _native_specs(parsed_specs: list[dict]) -> list[dict]:
    return [s for s in parsed_specs if s.get("client_kind") != "wasm"]


def _wasm_specs(parsed_specs: list[dict]) -> list[dict]:
    return [s for s in parsed_specs if s.get("client_kind") == "wasm"]


def _uploader_hostname(name: str, spec: dict, client_index: int) -> str:
    """The Influx ``host`` tag an uploader VM will report under.

    Built from the deployment name, the role and the per-role index exactly as
    the client Terraform modules build it. Derived here so the transport of each
    recorded operation can be resolved from the registry row alone: `clients
    destroy` deletes the S3 client state, and a `record-results` re-run after
    teardown must not relabel a WASM upload as native.
    """
    role = ROLE_WASM_UPLOAD if spec.get("client_kind") == "wasm" else ROLE_UPLOAD
    return f"{name}-ant-client-{role}-{spec['region']}-{client_index}"


def _read_urls_file(path: str) -> str:
    """Read and validate the URL list, returning its content for distribution."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read --uploader urls={path}: {e}") from e
    lines = [line.strip() for line in content.splitlines()]
    has_urls = any(line and not line.startswith("#") for line in lines)
    if not has_urls:
        raise ValueError(f"--uploader urls={path} contains no URLs.")
    return content


def _extract_file_names(urls_content: str) -> list[str]:
    """Parse a URL-list file into the bare filename of each upload target.

    Strips comments/blank lines, then takes the last path segment of each URL.
    Falls back to the raw URL if parsing yields an empty name.
    """
    from urllib.parse import urlparse

    names: list[str] = []
    for line in urls_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        candidate = Path(parsed.path).name if parsed.path else ""
        names.append(candidate or line)
    return names


def _check_for_duplicate_files(
    parsed_specs: list[dict],
    allow_duplicate_files: bool,
    console: Console,
) -> None:
    """Refuse to deploy if a file to be uploaded shares its name with a file
    already recorded as successfully uploaded to production.

    Runs before any infrastructure is created. It reads the
    ``deployment_prod_upload_results`` table (the same successful-upload data
    that ``prod files`` prints) and compares each upload target's file name
    — the last path segment of its source URL, exactly as ``prod files`` and
    the uploader service record it — against the recorded set.

    A collision aborts the deploy. ``allow_duplicate_files`` downgrades it to a
    warning so an operator can deliberately re-upload missing chunks or
    exercise the already-stored merkle fast path.

    When no database is configured there is nothing to compare against, so the
    check is skipped with a warning rather than blocking the deploy.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[yellow]Warning:[/yellow] No DATABASE_URL configured — skipping "
            "the duplicate-file check against previously uploaded files."
        )
        return

    existing_names = {
        row.file_name
        for row in repo.list_all_prod_upload_results(failed_only=False)
        if row.file_name
    }

    # Track which uploader spec requested each colliding name so the error
    # points the operator straight at the list that needs editing.
    collisions: list[tuple[int, str]] = []
    for index, spec in enumerate(parsed_specs, start=1):
        for name in _extract_file_names(spec["_urls_file_content"]):
            if name in existing_names:
                collisions.append((index, name))

    if not collisions:
        return

    if allow_duplicate_files:
        console.print(
            f"[yellow]Warning:[/yellow] {len(collisions)} file(s) to be uploaded "
            "already exist in the prod uploads list; proceeding anyway "
            "(--allow-duplicate-files)."
        )
        for index, name in collisions:
            console.print(f"[yellow]  uploader #{index}: {name}[/yellow]")
        return

    console.print(
        f"[bold red]Error:[/bold red] {len(collisions)} file(s) to be uploaded "
        "already exist in the prod uploads list (same file name):"
    )
    for index, name in collisions:
        console.print(f"[red]  uploader #{index}: {name}[/red]")
    console.print(
        "Refusing to deploy. Re-run with [bold]--allow-duplicate-files[/bold] to "
        "override (e.g. to re-upload missing chunks or test that already-stored "
        "chunks are not paid for again)."
    )
    console.print("[dim]See the full list with: saorsa-deploy prod files[/dim]")
    sys.exit(1)


def _resolve_secret_key(args, console: Console) -> str:
    key = getattr(args, "secret_key", None)
    if key:
        console.print("  Wallet key: provided via --secret-key")
        return key
    key = os.environ.get("ARBITRUM_ONE_SECRET_KEY")
    if key:
        console.print("  Wallet key: from ARBITRUM_ONE_SECRET_KEY env var")
        return key
    console.print(
        "[bold red]Error:[/bold red] No wallet secret key provided. "
        "Use --secret-key or set ARBITRUM_ONE_SECRET_KEY (a funded Arbitrum One "
        "wallet — distinct from the testnet ANT_DEPLOY_SECRET_KEY)."
    )
    sys.exit(1)


def _resolve_ant_binary(args, console: Console):
    """Mirror provision_clients's ant binary resolution: returns (url, is_archive)."""
    if args.branch_name:
        repo_owner = getattr(args, "repo_owner", None) or "maidsafe"
        from saorsa_deploy.binary_source import (
            check_ant_custom_build_exists,
            get_ant_custom_build_url,
        )
        if not check_ant_custom_build_exists(args.name, repo_owner, args.branch_name):
            console.print(
                f"[bold red]Error:[/bold red] No custom ant build found for "
                f"{repo_owner}/{args.branch_name}. Run 'build-ant' first."
            )
            sys.exit(1)
        url = get_ant_custom_build_url(args.name, repo_owner, args.branch_name)
        console.print(
            f"  Ant binary: custom build ({repo_owner}/{args.branch_name})"
        )
        return url, False

    if args.ant_version:
        from saorsa_deploy.binary_source import (
            check_ant_release_exists,
            get_ant_release_url,
        )
        if not check_ant_release_exists(args.ant_version):
            console.print(
                f"[bold red]Error:[/bold red] Ant release v{args.ant_version} not found."
            )
            sys.exit(1)
        url = get_ant_release_url(args.ant_version)
        console.print(f"  Ant binary: release v{args.ant_version}")
        return url, True

    console.print("  Ant binary: latest release")
    return None, True


def _monitoring_stack_exists(name: str) -> bool:
    try:
        load_monitoring_state(name)
        return True
    except RuntimeError:
        return False


def _monitoring_has_influx(name: str) -> bool:
    """True only when monitoring state carries usable InfluxDB creds.

    A monitoring VM whose ``monitoring provision`` never finished leaves state
    with a ``vm_ip`` but no ``influxdb_url``/``influxdb_token``. That is NOT
    "fully provisioned": ``_ensure_monitoring`` must re-provision it rather than
    silently skip (which would later fail with "monitoring state has no Influx
    URL/token").
    """
    try:
        state = load_monitoring_state(name)
    except RuntimeError:
        return False
    return bool(state.get("influxdb_url") and state.get("influxdb_token"))


def _client_stack_exists(name: str) -> bool:
    try:
        state = load_client_state(name)
        return bool(state.get("clients"))
    except RuntimeError:
        return False


def _ensure_monitoring(args, console: Console) -> None:
    from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra
    from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

    if _monitoring_stack_exists(args.name):
        if _monitoring_has_influx(args.name):
            console.print(
                f"[dim]Monitoring stack '{args.name}' already exists — skipping "
                "monitoring infra / monitoring provision.[/dim]"
            )
            return
        # Infra VM exists but a prior provision never wrote the Influx creds —
        # re-provision (the VM is already there) instead of skipping and later
        # failing the uploader provisioning.
        console.print(
            f"[yellow]Monitoring VM for '{args.name}' exists but has no "
            "InfluxDB creds (a prior provision didn't finish) — "
            "re-provisioning.[/yellow]"
        )
        cmd_provision_monitoring(SimpleNamespace(
            name=args.name,
            ssh_key_path=args.ssh_key_path,
        ))
        return

    console.print("[bold cyan]Step 1/5: creating monitoring VM[/bold cyan]")
    cmd_monitoring_infra(SimpleNamespace(
        name=args.name,
        region=MONITORING_REGION,
        volume_size=args.monitoring_volume_size,
        # A single client VM forwards little; keep the InfluxDB box small
        # rather than the larger testnet default.
        droplet_size="s-2vcpu-4gb",
        force=False,
        grafana=getattr(args, "grafana", False),
    ))

    console.print("[bold cyan]Step 2/5: provisioning InfluxDB + Grafana[/bold cyan]")
    cmd_provision_monitoring(SimpleNamespace(
        name=args.name,
        ssh_key_path=args.ssh_key_path,
    ))


def _ensure_client_infra(args, parsed_specs: list[dict], console: Console) -> None:
    """Create the uploader VMs via clients infra.

    The existing clients infra parser expects uploader specs of the form
    ``provider:region:file_size_mb[:vm_size]``. We synthesise those with the
    ``digital_ocean`` provider (prod uploaders are Digital Ocean only — the
    ``--uploader`` spec carries no provider field) and file_size_mb=0 because
    that field is only consumed by the legacy testnet uploader.sh and is
    unused for prod uploads.
    """
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — skipping "
            "clients infra.[/dim]"
        )
        return

    # bootstrap_cache and network are carried through so client_state records
    # the real per-VM cache mode and link conditions — the print/slack surfaces
    # read them from there, and a run that is shaped must say so.
    synth_uploader_specs = [
        f"digital_ocean:{s['region']}:0:{s['vm_size']}"
        f":bootstrap_cache={str(s.get('bootstrap_cache', True)).lower()}"
        + (f":network={s['network']}" if s.get("network") else "")
        for s in _native_specs(parsed_specs)
    ]
    # A browser-path uploader gets the `wasm-upload` ROLE, not a flag on an
    # `upload` VM: the role names the VM, its Terraform workspace and its S3
    # state key, and telling the two tiers apart in all three is the point of
    # running both. `bootstrap_cache` is absent from the WASM spec form because
    # the role has no such option.
    synth_wasm_specs = [
        f"digital_ocean:{s['region']}:0:{s['vm_size']}"
        + (f":network={s['network']}" if s.get("network") else "")
        for s in _wasm_specs(parsed_specs)
    ]
    console.print(
        f"[bold cyan]Step 3/5: creating "
        f"{len(synth_uploader_specs) + len(synth_wasm_specs)} "
        f"uploader VM(s)[/bold cyan]"
    )
    cmd_infra_clients(SimpleNamespace(
        name=args.name,
        uploader=synth_uploader_specs,
        downloader=[],
        wasm_uploader=synth_wasm_specs,
        wasm_downloader=[],
        force=False,
    ))


def _pair_specs_with_vms(
    name: str, parsed_specs: list[dict], console: Console
) -> list[dict]:
    """Match each uploader spec to the VM `clients infra` created for it.

    Pairing is per ROLE by ascending ``client_index``: `clients infra` indexes
    each role from 1 independently, so a mixed deployment's native uploader #1
    and WASM uploader #1 are different VMs. Zipping one flat list against the
    other would hand a native URL list to a browser-path VM.
    """
    state = load_client_state(name)
    clients = state.get("clients", [])

    out: list[dict] = []
    for role, specs in (
        (ROLE_UPLOAD, _native_specs(parsed_specs)),
        (ROLE_WASM_UPLOAD, _wasm_specs(parsed_specs)),
    ):
        if not specs:
            continue
        vms = sorted(
            (c for c in clients if c.get("role", ROLE_UPLOAD) == role),
            key=lambda c: c["client_index"],
        )
        if len(vms) != len(specs):
            console.print(
                f"[bold red]Error:[/bold red] expected {len(specs)} {role} "
                f"VM(s) in client state but found {len(vms)}."
            )
            sys.exit(1)
        for spec, client in zip(specs, vms):
            if not client.get("ip"):
                console.print(
                    f"[bold red]Error:[/bold red] {role} "
                    f"#{client['client_index']} ({client['region']}) has no IP "
                    "— VM creation failed."
                )
                sys.exit(1)
            out.append({
                "ip": client["ip"],
                "region": spec["region"],
                "client_index": client["client_index"],
                "client_kind": spec.get("client_kind") or "native",
                "urls_file_content": spec["_urls_file_content"],
                "payment_type": spec.get("payment_type"),
                "log_level": spec.get("log_level"),
                "clock_skew": spec.get("clock_skew", False),
                "network": resolve_network(spec.get("network")),
                "bootstrap_cache": spec.get("bootstrap_cache", True),
                "forward_logs": spec.get("forward_logs"),
                "memory_profiling": spec.get("memory_profiling"),
                "rust_log": spec.get("rust_log"),
            })
    return out


def _upload_role_clients(name: str) -> list[dict]:
    """Every uploader VM of this deployment, native and browser-path.

    Both tiers run the same ``ant-prod-uploader`` unit — the transport changes
    which binary the service spawns, not what the service is — so start, stop and
    the log-forwarder install all address them together.
    """
    state = load_client_state(name)
    return [
        c
        for c in state.get("clients", [])
        if c.get("ip") and c.get("role", ROLE_UPLOAD) in (ROLE_UPLOAD, ROLE_WASM_UPLOAD)
    ]


def _provision_uploaders(
    args,
    parsed_specs: list[dict],
    console: Console,
    wasm_context=None,
) -> None:
    from saorsa_deploy.state import load_monitoring_state as _load_mon

    uploaders_for_provisioner = _pair_specs_with_vms(
        args.name, parsed_specs, console
    )

    secret_key = _resolve_secret_key(args, console)
    if _native_specs(parsed_specs):
        binary_url, binary_is_archive = _resolve_ant_binary(args, console)
    else:
        # Nothing on a WASM-only deployment runs `ant`, so resolving (and
        # installing) it would fetch a binary for no reason — and would fail a
        # deploy whose only `--branch-name` build is the WASM bundle.
        binary_url, binary_is_archive = None, True

    console.print(
        "[bold cyan]Step 4a/5: fetching production bootstrap peers[/bold cyan]"
    )
    try:
        bootstrap_peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    console.print(f"  Using {len(bootstrap_peers)} production peers.")

    mon_state = _load_mon(args.name)
    influx_url = mon_state.get("influxdb_url", "")
    influx_token = mon_state.get("influxdb_token", "")
    influx_org = mon_state.get("influxdb_org", "ant")
    influx_bucket = mon_state.get("influxdb_bucket", "testnet-metrics")
    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx URL/token "
            "— run monitoring provision first."
        )
        sys.exit(1)

    host_ips = [u["ip"] for u in uploaders_for_provisioner]
    clear_known_hosts(host_ips, console)

    not_ready = wait_for_ssh(host_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(
            f"[bold red]Error:[/bold red] SSH never came up on: "
            f"{', '.join(not_ready)}"
        )
        sys.exit(1)

    console.print(
        "[bold cyan]Step 4b/5: provisioning ant-prod-uploader on uploader "
        "VMs[/bold cyan]"
    )
    provisioner = ProdUploaderProvisioner(
        uploaders=uploaders_for_provisioner,
        bootstrap_peers=bootstrap_peers,
        wallet_secret_key=secret_key,
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        wasm_bundle_url=wasm_context.bundle_url if wasm_context else None,
        bootstrap_multiaddrs=(
            wasm_context.bootstrap_multiaddrs if wasm_context else None
        ),
        console=console,
    )
    provisioner.execute()


def _start_uploaders(args, console: Console) -> None:
    clients = _upload_role_clients(args.name)
    if not clients:
        console.print("[bold red]Error:[/bold red] No uploader VMs found in state.")
        sys.exit(1)

    host_ips = [c["ip"] for c in clients]
    console.print(
        f"[bold cyan]Step 5/5: starting ant-prod-uploader on "
        f"{len(host_ips)} VM(s)[/bold cyan]"
    )
    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state_obj = State(inventory=inventory, config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name=f"Start {SERVICE_NAME}",
            commands=[f"systemctl enable --now {SERVICE_NAME}"],
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)

    failed = state_obj.failed_hosts
    if failed:
        for host in failed:
            console.print(f"  [red]Failed to start on {host.name}[/red]")
        sys.exit(1)


def cmd_prod_uploaders_deploy(args):
    """Deploy a prod uploaders testbed against the Autonomi production network."""
    console = Console()

    wasm_raw = getattr(args, "wasm_uploader", None) or []
    if not args.uploader and not wasm_raw:
        console.print(
            "[bold red]Error:[/bold red] At least one --uploader or "
            "--wasm-uploader spec is required."
        )
        sys.exit(1)

    # Native first, then browser-path. The order fixes each entry's pool
    # selection slot, so adding a WASM uploader to an existing manifest cannot
    # move a native uploader's slot and make it redraw files it already holds.
    parsed_specs: list[dict] = []
    for raw, parser in (
        *((raw, parse_prod_uploader_spec) for raw in args.uploader),
        *((raw, parse_prod_wasm_uploader_spec) for raw in wasm_raw),
    ):
        try:
            spec = parser(raw)
            if spec.get("urls_file"):
                spec["_urls_file_content"] = _read_urls_file(spec["urls_file"])
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        parsed_specs.append(spec)

    # Everything a browser-path uploader needs is resolved and PROVED before any
    # infrastructure exists — the bundle is in S3, and the production bootstraps
    # answer an ICE probe. A VM created without either bills from creation and
    # fails every upload.
    wasm_context = resolve_wasm_context(
        SimpleNamespace(
            name=args.name,
            client_kind="wasm" if wasm_raw else "native",
            wasm_branch_name=getattr(args, "wasm_branch_name", None),
            wasm_repo_owner=getattr(args, "wasm_repo_owner", None),
            wasm_bootstrap_multiaddrs_file=getattr(
                args, "wasm_bootstrap_multiaddrs_file", None
            ),
            skip_bootstrap_probe=getattr(args, "skip_bootstrap_probe", False),
        ),
        console,
    )

    # Draw from the pool before any infrastructure exists. A shortfall found
    # after Terraform has stood up VMs against a funded mainnet wallet costs
    # real money and leaves a half-built deployment behind.
    pool_selections = _select_pool_files(args, parsed_specs, console)

    # Earliest possible gate for the legacy urls= path: refuse before any
    # infrastructure is created if a file to be uploaded is already present in
    # the prod uploads list. Pool-backed uploaders skip this by construction —
    # selection cannot return a file that has already been handed out. Filter on
    # pool_request, not _urls_file_content: selection has already written the
    # drawn URLs into the latter, and a basename match on a pool file is a false
    # positive (every DVD rip names its first title VTS_01_1.VOB).
    _check_for_duplicate_files(
        [s for s in parsed_specs if not s.get("pool_request")],
        getattr(args, "allow_duplicate_files", False),
        console,
    )

    console.print(f"[bold]prod uploaders deploy: {args.name}[/bold]")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    for i, spec in enumerate(parsed_specs, start=1):
        url_count = sum(
            1 for line in spec["_urls_file_content"].splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        source = "pool" if spec.get("pool_request") else "urls"
        # The bracket is escaped for Rich, which would otherwise read `[wasm]`
        # as a style tag and print the line with the marker silently missing.
        transport = " \\[wasm]" if spec.get("client_kind") == "wasm" else ""
        console.print(
            f"  Uploader #{i}{transport}: {spec['region']} ({spec['vm_size']}, "
            f"{source}={url_count})"
        )
    if wasm_context:
        # Every WASM uploader signs with the same shared wallet as the native
        # tier; naming the paying address is the only way to check its balance
        # before a run spends real ETH.
        console.print(f"  Paying address: {_paying_address(args)}")
    console.print()

    deploy_args_payload = {
        "kind": "prod-uploaders",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "monitoring_name": args.name,
        "uploaders": [
            {
                "region": s["region"],
                "vm_size": s["vm_size"],
                # Per-ROW, not per-deployment: a mixed run has both, and the
                # recording path reads this list to label each operation's
                # transport. It has to survive teardown, which the S3 client
                # state does not.
                "client_kind": s.get("client_kind") or "native",
                "hostname": _uploader_hostname(
                    args.name, s, _role_index(parsed_specs, s)
                ),
                "urls_file": s["urls_file"],
                "pool_request": (
                    {k.value: v for k, v in s["pool_request"].items()}
                    if s.get("pool_request")
                    else None
                ),
                "pool_min_size_bytes": s.get("pool_min_size_bytes"),
                "pool_max_size_bytes": s.get("pool_max_size_bytes"),
                "exclude_partial": s.get("exclude_partial", False),
                "file_names": _extract_file_names(s["_urls_file_content"]),
                "payment_type": s.get("payment_type"),
                "clock_skew": s.get("clock_skew", False),
                "bootstrap_cache": s.get("bootstrap_cache", True),
                "forward_logs": s.get("forward_logs"),
                "memory_profiling": s.get("memory_profiling"),
                "rust_log": s.get("rust_log"),
                # Resolved knob set, not the bare name: this is the record that
                # keeps a past run interpretable after the profile file moves on,
                # and it is what `slack-post` renders the rates from.
                "network": (
                    resolve_network(s.get("network")).to_dict()
                    if s.get("network")
                    else None
                ),
            }
            for s in parsed_specs
        ],
        "purpose": args.purpose,
        "ant_binary": build_binary_descriptor(
            args.branch_name, args.repo_owner, args.ant_version,
            debug=getattr(args, "debug", False),
        ),
    }
    if wasm_context:
        # `client_kind` is per-uploader here, so only the bundle provenance and
        # the probe result are deployment-wide. Dropping the top-level
        # `client_kind` key keeps a mixed run from claiming one transport.
        deploy_args_payload.update(
            {
                key: value
                for key, value in wasm_deploy_args(wasm_context).items()
                if key != "client_kind"
            }
        )
    deployment_id = record_deployment_created(args.name, deploy_args_payload)
    # Claims were taken before this row existed — selection has to run before
    # any infrastructure, and record_deployment_created always inserts a fresh
    # row, so the id cannot be known any earlier.
    _attach_pool_claims(pool_selections, deployment_id, args.name)
    record_event(args.name, "prod_uploaders_deploy_started", deploy_args_payload)

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, parsed_specs, console)
        _provision_uploaders(args, parsed_specs, console, wasm_context)
        _start_uploaders(args, console)
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            provision_client_telegraf_best_effort,
        )
        provision_client_telegraf_best_effort(
            args.name, args.ssh_key_path, console=console
        )
        # Client log forwarding (opt-in per uploader): stand up a dedicated ES
        # and install the forwarder on the uploader VMs.
        if any(s.get("forward_logs") for s in parsed_specs):
            from saorsa_deploy.cmd.prod_log_forwarding import (
                PROD_UPLOADER_GLOBS,
                ensure_client_elastic,
                install_client_forwarder,
            )

            es_creds = ensure_client_elastic(
                args.name, args.ssh_key_path, console
            )
            # forward_logs is per-uploader; only install on the opted-in VMs.
            # Paired per role, since each role is indexed from 1 independently.
            up_ips = [
                entry["ip"]
                for entry, spec in zip(
                    _pair_specs_with_vms(args.name, parsed_specs, console),
                    _native_specs(parsed_specs) + _wasm_specs(parsed_specs),
                )
                if spec.get("forward_logs")
            ]
            install_client_forwarder(
                name=args.name,
                role="uploader",
                host_ips=up_ips,
                tail_globs=PROD_UPLOADER_GLOBS,
                es_creds=es_creds,
                ssh_key_path=args.ssh_key_path,
                console=console,
            )
    except SystemExit:
        record_event(args.name, "prod_uploaders_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_uploaders_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_uploaders_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(
        f"[bold green]prod uploaders '{args.name}' is running.[/bold green]"
    )
    console.print(
        f"[dim]Tail upload progress: saorsa-deploy deployments print-uploads "
        f"--name {args.name}[/dim]"
    )
    console.print(
        f"[dim]Persist uploads to DB:  saorsa-deploy deployments record-results "
        f"--name {args.name}[/dim]"
    )


def _discard_pool_selection(name: str) -> None:
    """Drop the persisted draw once the deployment it belonged to is gone.

    The selection is keyed on the deployment name, which is reused every
    release train, so leaving it behind means the next run of the same name
    inherits it. ``select_for_deployment`` refuses a spent draw regardless —
    that is the load-bearing guard — but a destroyed deployment has no draw to
    resume, so keeping it only preserves something misleading.

    Best-effort: the deployment is already torn down by this point, and failing
    the destroy over leftover state would strand a run that actually succeeded.
    """
    from saorsa_deploy.state import delete_pool_selection

    try:
        delete_pool_selection(name)
    except Exception:
        pass


def cmd_prod_uploaders_destroy(args):
    """Destroy a prod uploaders testbed."""
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

    console = Console()
    console.print(f"[bold]prod uploaders destroy: {args.name}[/bold]")

    # Before anything is destroyed: this teardown takes the monitoring VM, and
    # with it any per-file results that were never recorded to Postgres.
    if not getattr(args, "skip_results_check", False):
        from saorsa_deploy.results_guard import check_before_destroy

        check_before_destroy(console, args.name, "prod-uploaders")

    record_event(args.name, "prod_uploaders_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        if _client_stack_exists(args.name):
            try:
                _stop_uploaders(args, console)
            except (SystemExit, Exception) as e:
                # Stopping is best-effort: a half-provisioned VM may have no
                # systemd unit yet, and a dead/unreachable VM will never stop
                # cleanly. Either way we must still proceed to clients destroy
                # so the Terraform-managed VMs get torn down.
                console.print(
                    f"[yellow]stop reported a failure ({e!s}); continuing "
                    "with destroy.[/yellow]"
                )

            console.print("[bold cyan]Destroying uploader VMs...[/bold cyan]")
            cmd_destroy_clients(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No client stack found for '{args.name}' — skipping "
                "uploader destroy.[/dim]"
            )

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact (--keep-monitoring); "
                "metrics remain queryable.[/dim]"
            )
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No monitoring stack found for '{args.name}' — "
                "skipping.[/dim]"
            )

        from saorsa_deploy.cmd.prod_log_forwarding import destroy_client_elastic

        destroy_client_elastic(
            args.name, getattr(args, "keep_elastic", False), console
        )
    except SystemExit:
        record_event(args.name, "prod_uploaders_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_uploaders_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    _discard_pool_selection(args.name)

    record_event(args.name, "prod_uploaders_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(
        f"[bold green]prod uploaders '{args.name}' destroyed.[/bold green]"
    )


def _stop_uploaders(args, console: Console) -> None:
    clients = _upload_role_clients(args.name)
    if not clients:
        return

    host_ips = [c["ip"] for c in clients]
    console.print(
        f"[bold cyan]Stopping {SERVICE_NAME} on {len(host_ips)} "
        f"VM(s)...[/bold cyan]"
    )
    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state_obj = State(inventory=inventory, config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name=f"Stop {SERVICE_NAME}",
            commands=[
                f"systemctl disable --now {SERVICE_NAME} 2>/dev/null || true",
            ],
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)


def _select_pool_files(args, parsed_specs: list[dict], console: Console) -> list:
    """Resolve every ``pool=`` uploader spec into concrete URLs.

    Each uploader VM is its own selection slot, so a resumed deploy hands each
    VM back the files it already had rather than drawing a fresh set and
    permanently burning the first one.

    The chosen URLs are written into the same ``_urls_file_content`` the rest of
    this command already distributes, so nothing downstream — provisioning, the
    VM-side fetch, the recorded ``file_names`` — needs to know the pool exists.
    """
    from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind

    from saorsa_deploy.pool_selection import (
        PoolSelectionError,
        render_selection,
        select_for_deployment,
    )
    from saorsa_deploy.registry import get_file_pool_repository

    pooled = [s for s in parsed_specs if s.get("pool_request")]
    if not pooled:
        return []

    repo = get_file_pool_repository()
    if repo is None:
        # Unlike the old name-based duplicate check, this cannot degrade to a
        # warning: the pool IS the never-reuse guarantee, and without it there
        # is no list of files to upload at all.
        console.print(
            "[bold red]Error:[/bold red] an uploader asks for pool=… but no "
            "DATABASE_URL is configured. The file pool lives in the registry "
            "database.\nNo infrastructure was created."
        )
        sys.exit(1)

    console.print("  Pool selection")
    selections = []
    for index, spec in enumerate(parsed_specs, start=1):
        if not spec.get("pool_request"):
            continue
        try:
            selected = select_for_deployment(
                repo,
                name=args.name,
                slot=f"uploader-{index}",
                kind=PoolFileKind.URL,
                request=spec["pool_request"],
                source=ClaimSource.PROD_UPLOADERS,
                exclude_partial=spec.get("exclude_partial", False),
                min_size_bytes=spec.get("pool_min_size_bytes"),
                max_size_bytes=spec.get("pool_max_size_bytes"),
            )
        except PoolSelectionError as exc:
            console.print(f"\n[bold red]Error:[/bold red] {exc}")
            sys.exit(1)
        render_selection(
            console,
            f"uploader #{index} ({spec['region']})",
            spec["pool_request"],
            selected,
            spec.get("pool_min_size_bytes"),
            spec.get("pool_max_size_bytes"),
        )
        spec["_urls_file_content"] = "\n".join(e.locator for e in selected) + "\n"
        selections.append(selected)

    total = sum(len(s) for s in selections)
    console.print(
        f"    {total} file(s) claimed; selection persisted to S3 state "
        "(a resume reuses it)."
    )
    return selections


def _attach_pool_claims(selections: list, deployment_id, name: str) -> None:
    if not selections or not deployment_id:
        return
    from saorsa_deploy.pool_selection import attach_deployment
    from saorsa_deploy.registry import get_file_pool_repository

    repo = get_file_pool_repository()
    if repo is not None:
        attach_deployment(repo, selections, deployment_id, name)
