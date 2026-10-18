import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from saorsa_deploy.client_extension import client_extension_tiers
from saorsa_deploy.client_roles import is_pointer, unit_for_role
from saorsa_deploy.evm import get_evm_network_info
from saorsa_deploy.local_inventory import (
    DEVNET_MANIFEST_FILENAME,
    default_inventory_path,
    retire_local_inventory,
)
from saorsa_deploy.provisioning.client import POINTER_DATA_DIR
from saorsa_deploy.provisioning.prod_downloader import (
    ADDRESSES_PATH as PROD_DOWNLOADER_ADDRESSES_PATH,
)
from saorsa_deploy.provisioning.prod_downloader import (
    ANT_INSTALL_PATH as PROD_DOWNLOADER_ANT_INSTALL_PATH,
)
from saorsa_deploy.provisioning.prod_downloader import (
    DOWNLOAD_DIR as PROD_DOWNLOADER_DOWNLOAD_DIR,
)
from saorsa_deploy.provisioning.prod_downloader import (
    ENV_PATH as PROD_DOWNLOADER_ENV_PATH,
)
from saorsa_deploy.provisioning.prod_downloader import (
    HEAPTRACK_DIR as PROD_DOWNLOADER_HEAPTRACK_DIR,
)
from saorsa_deploy.provisioning.prod_downloader import (
    LOG_DIR as PROD_DOWNLOADER_LOG_DIR,
)
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as PROD_DOWNLOADER_SERVICE_NAME,
)
from saorsa_deploy.provisioning.volume import MOUNT_POINT
from saorsa_deploy.provisioning.wasm_client import (
    ANT_WASM_CLI_PATH,
    WASM_BUILD_INFO_PATH,
    WASM_CLIENT_DIR,
)
from saorsa_deploy.state import (
    load_client_state,
    load_deployment_state,
    load_elastic_state,
    load_monitoring_state,
)

#: 7 adds `client_kind`, `wasm_build` and `wasm_bootstrap` to the download
#: kinds' `client_config`, plus the `ant_wasm_*` paths. A bump rather than a
#: quiet addition because a consumer that reads `client_config.binary` to find
#: out what ran would be wrong on a WASM deployment — nothing is installed at
#: the `ant` path there.
INVENTORY_SCHEMA_VERSION = 7
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_KEY = "~/.ssh/id_rsa"
DEVNET_DATA_DIR = f"{MOUNT_POINT}/data"
DEVNET_EVM_NETWORK = "arbitrum-sepolia"

FIREWALL_PLACEHOLDER = "managed by terraform"


def _default_output_path(name: str) -> Path:
    # One definition, shared with the readers and with the retirement path:
    # `saorsa_deploy.local_inventory`.
    return default_inventory_path(name)


def _deployment_id_field(deployment_id: int | None) -> dict:
    """``{"deployment_id": id}``, or nothing at all.

    Omitted rather than written as null when the registry is unreachable: a
    consumer gating on the id (`jq -r '.deployment_id // empty'`) must be able
    to tell "this snapshot is not from the run I mean" from "I cannot say".
    """
    return {} if deployment_id is None else {"deployment_id": deployment_id}


def _build_binary_field(state: dict) -> dict:
    binary = state.get("binary") or {}
    version = binary.get("version")
    branch = binary.get("branch")
    owner = binary.get("repo_owner")
    url = binary.get("url", "")

    if version:
        label = version
    elif branch and owner:
        label = f"branch:{owner}/{branch}"
    else:
        label = ""

    return {
        "ant_node_version": label,
        "ant_node_release_url": url,
    }


def _build_firewalls_field(state: dict, clients: list[dict] | None = None) -> dict:
    providers_used = set()
    for key in state.get("vm_ips") or {}:
        providers_used.add(key.split("/", 1)[0])
    if state.get("bootstrap_nodes"):
        providers_used.add("digitalocean")
    if clients:
        providers_used.add("digitalocean")
    return {provider: FIREWALL_PLACEHOLDER for provider in sorted(providers_used)}


def _build_node_vms_field(state: dict, ssh_key: str, node_count: int) -> list[dict]:
    name = state["name"]
    vm_ips = state.get("vm_ips") or {}
    vm_ids = state.get("vm_ids") or {}
    nat_ips = set(state.get("nat_vm_ips") or [])
    full_ips = set(state.get("full_vm_ips") or [])
    services = [f"ant-node-{i + 1}" for i in range(node_count)]

    entries = []
    for key in sorted(vm_ips.keys()):
        provider, region = key.split("/", 1)
        ips = vm_ips[key]
        ids = vm_ids.get(key, [])
        for index, ip in enumerate(ips):
            provider_id = ids[index] if index < len(ids) else ""
            is_nat = ip in nat_ips
            is_full = ip in full_ips
            entries.append(
                {
                    "name": f"{name}-ant-node-{region}-{index + 1}",
                    "provider": provider,
                    "provider_id": provider_id,
                    "region": region,
                    "ip": ip,
                    "ssh_user": DEFAULT_SSH_USER,
                    "ssh_key": ssh_key,
                    "nat_simulated": is_nat,
                    "storage_full": is_full,
                    "nodes": [
                        {
                            "service_name": svc,
                            "nat_simulated": is_nat,
                            "storage_full": is_full,
                        }
                        for svc in services
                    ],
                    "services": list(services),
                }
            )
    return entries


def _build_bootstrap_vms_field(state: dict, ssh_key: str) -> list[dict]:
    name = state["name"]
    bootstrap_nodes = state.get("bootstrap_nodes") or []

    entries = []
    for node in sorted(bootstrap_nodes, key=lambda n: n.get("bootstrap_index", 0)):
        # Skip peers seeded from the PRODUCTION network (region "prod"): they are
        # external nodes we neither own nor can SSH into (prod-nodes /
        # prod-debug-nodes join production rather than run their own bootstraps),
        # so listing them as our bootstrap VMs would mislead a consumer.
        if node.get("region") == "prod":
            continue
        index = node["bootstrap_index"]
        service = f"ant-bootstrap-{index}"
        entries.append(
            {
                "name": f"{name}-ant-bootstrap-{index}",
                "provider": "digitalocean",
                "provider_id": node.get("provider_id", ""),
                "region": node["region"],
                "ip": node["ip"],
                "bootstrap_index": index,
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                "nodes": [{"service_name": service}],
                "services": [service],
            }
        )
    return entries


def _build_client_vms_field(name: str, clients: list[dict] | None, ssh_key: str) -> list[dict]:
    if not clients:
        return []

    entries = []
    for client in sorted(clients, key=lambda c: c.get("client_index", 0)):
        index = client["client_index"]
        # A client added by a `kind: clients` extension carries its extension
        # name, and its VM is named `<deployment>-<extension>-ant-client-...`.
        # Named in the entry too: an inventory that lists an extension's VMs
        # indistinguishably from the deployment's own would have an operator
        # reading one tier's teardown command as the other's.
        extension = client.get("extension")
        prefix = f"{name}-{extension}" if extension else name
        region = client["region"]
        # client_index is assigned PER ROLE (uploaders 1..N and downloaders
        # 1..N independently), so the role must be part of the name or an
        # uploader and downloader sharing a region collide. This also matches
        # the VM's actual hostname (set by the client provisioner), which is
        # what the InfluxDB `host` tag carries.
        role = client.get("role", "upload")
        service = unit_for_role(role)
        # A pointer client's workload rates, and where its owner keys and ledger
        # live. The paths are the teardown-critical part, as they are for the
        # prod download kinds: the owner keys are what make a pointer updatable
        # AT ALL, ownership is immutable, and nothing collects them -- they die
        # with the VM, and with them every pointer the client created.
        pointer_fields = (
            {
                "create_interval_secs": client.get("create_interval_secs"),
                "update_interval_secs": client.get("update_interval_secs"),
                "sweep_interval_secs": client.get("sweep_interval_secs"),
                "chain_fraction": client.get("chain_fraction"),
                # 0 = no handover loop (V2-1354). Named either way, so the
                # inventory of a run that did not exercise handovers says so
                # rather than leaving the reader to infer it from an absence.
                "transfer_interval_secs": client.get("transfer_interval_secs"),
                "pointer_max": client.get("pointer_max"),
                "paths": {
                    "owner_keys": f"{POINTER_DATA_DIR}/keys",
                    "ledger": f"{POINTER_DATA_DIR}/ledger.json",
                    "published_addresses": (f"{POINTER_DATA_DIR}/pointer-{region}-{index}.txt"),
                    "published_transfers": (
                        f"{POINTER_DATA_DIR}/pointer-transfers-{region}-{index}.txt"
                    ),
                },
            }
            if is_pointer(role)
            else {}
        )
        entries.append(
            {
                "name": f"{prefix}-ant-client-{role}-{region}-{index}",
                "provider": client.get("provider", "digitalocean"),
                "provider_id": client.get("provider_id", ""),
                "region": region,
                "ip": client.get("ip", ""),
                "role": role,
                "client_index": index,
                "file_size_mb": client.get("file_size_mb"),
                "vm_size": client.get("vm_size", ""),
                # ant peer cache mode: true → no --bootstrap (cache enabled);
                # null for client state predating the option.
                "bootstrap_cache": client.get("bootstrap_cache"),
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                # Each client VM runs exactly one role's service — the other
                # unit is never installed on it.
                "nodes": [{"service_name": service}],
                "services": [service],
                **({"pointer_config": pointer_fields} if pointer_fields else {}),
                # Only present on an extension's VMs, the way `pointer_config`
                # is only present on a pointer client's. So every existing
                # deployment's inventory stays byte-identical and the schema
                # version does not move: nothing an existing consumer reads
                # changes meaning, a new key simply appears on new VMs.
                **({"extension": extension} if extension else {}),
            }
        )
    return entries


def _build_observability_vms_field(
    name: str,
    ssh_key: str,
    monitoring_state: dict | None,
    elastic_state: dict | None,
) -> list[dict]:
    """VMs for the deployment's own monitoring / logging stack (InfluxDB, Grafana,
    Elasticsearch). Empty when the deployment has no such stack."""
    entries: list[dict] = []
    if monitoring_state and monitoring_state.get("vm_ip"):
        region = monitoring_state.get("region", "")
        entries.append(
            {
                "name": f"{name}-ant-monitoring-{region}",
                "provider": monitoring_state.get("provider", ""),
                "region": region,
                "ip": monitoring_state["vm_ip"],
                "role": "influxdb",
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                "services": ["influxdb"],
            }
        )
        if monitoring_state.get("grafana_ip"):
            entries.append(
                {
                    "name": f"{name}-ant-grafana-{region}",
                    "provider": monitoring_state.get("provider", ""),
                    "region": region,
                    "ip": monitoring_state["grafana_ip"],
                    "role": "grafana",
                    "ssh_user": DEFAULT_SSH_USER,
                    "ssh_key": ssh_key,
                    "services": ["grafana"],
                }
            )
    if elastic_state and elastic_state.get("vm_ip"):
        region = elastic_state.get("region", "")
        entries.append(
            {
                "name": f"{name}-ant-elastic-{region}",
                "provider": elastic_state.get("provider", ""),
                "region": region,
                "ip": elastic_state["vm_ip"],
                "role": "elasticsearch",
                "ssh_user": DEFAULT_SSH_USER,
                "ssh_key": ssh_key,
                "services": ["elasticsearch"],
            }
        )
    return entries


def build_inventory(
    state: dict,
    ssh_key: str = DEFAULT_SSH_KEY,
    node_count: int | None = None,
    created_at: str | None = None,
    clients: list[dict] | None = None,
    monitoring_state: dict | None = None,
    elastic_state: dict | None = None,
    deployment_id: int | None = None,
) -> dict:
    """Build the schema-version-4 inventory dict from a loaded deployment state.

    Pure function, easy to unit test. `clients` is optional; pass the list from
    client state when present, or omit/None for deployments without client VMs.
    `monitoring_state` / `elastic_state` are optional; pass them to include the
    deployment's own InfluxDB / Grafana / Elasticsearch VMs.
    """
    resolved_node_count = node_count if node_count is not None else state.get("node_count")
    if resolved_node_count is None:
        raise ValueError(
            "node_count is missing from deployment state. "
            "Run 'provision' first (or pass --node-count to override)."
        )

    created = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "testnet_id": state["name"],
        **_deployment_id_field(deployment_id),
        "created_at": created,
        "binary": _build_binary_field(state),
        "firewalls": _build_firewalls_field(state, clients),
        "bootstrap_vms": _build_bootstrap_vms_field(state, ssh_key),
        "node_vms": _build_node_vms_field(state, ssh_key, resolved_node_count),
        "client_vms": _build_client_vms_field(state["name"], clients, ssh_key),
        "observability_vms": _build_observability_vms_field(
            state["name"], ssh_key, monitoring_state, elastic_state
        ),
        # Only populated for the client-only prod kinds (see
        # build_client_inventory); a node-bearing deployment has no single
        # client workload to describe.
        "client_config": None,
    }


# Kinds that deploy client VMs against the PRODUCTION network and keep no
# deployment state of their own — `generate-inventory` builds their inventory
# from client state + the registry row instead.
PROD_DOWNLOAD_KINDS = frozenset({"prod-downloaders", "prod-continuous-downloader"})

#: The daily write test. Client-only like the download kinds, but its
#: configuration and on-VM paths are entirely different, so it gets its own
#: client_config builder rather than being folded into the set above.
PROD_UPLOAD_TEST_KIND = "prod-continuous-uploader"
PROD_CLIENT_KINDS = PROD_DOWNLOAD_KINDS | {PROD_UPLOAD_TEST_KIND}


def build_client_config(deploy_args: dict | None) -> dict | None:
    """Describe how a prod client deployment is configured.

    Dispatches on kind: the daily write test has a different shape from the
    download kinds (a pool request rather than a fixed address list, two VM
    roles rather than one).
    """
    if not deploy_args:
        return None
    if deploy_args.get("kind") == PROD_UPLOAD_TEST_KIND:
        return _build_write_test_config(deploy_args)
    return _build_download_config(deploy_args)


def _build_write_test_config(deploy_args: dict) -> dict:
    """Configuration for a prod-continuous-uploader.

    The uploaded address list grows for as long as the deployment runs, so it
    is deliberately NOT inlined here the way a download kind's fixed list is —
    an inventory is a snapshot and would go stale immediately, and a year of
    daily uploads is a thousand addresses. The registry tables are named
    instead, and they are authoritative.
    """
    from saorsa_deploy.provisioning.prod_continuous_uploader import (
        UPLOADER_DATA_DIR,
        UPLOADER_ENV_PATH,
        UPLOADER_LOG_DIR,
        UPLOADER_SERVICE,
        VERIFIER_DATA_DIR,
        VERIFIER_ENV_PATH,
        VERIFIER_LOG_DIR,
        VERIFIER_SERVICE,
    )
    from saorsa_deploy.provisioning.saorsa_db_install import VENV_PYTHON

    client_kind = deploy_args.get("client_kind") or "native"
    is_wasm = client_kind == "wasm"

    return {
        "kind": PROD_UPLOAD_TEST_KIND,
        "one_shot": False,
        # Which transport the UPLOADER wrote with. The verifiers are native
        # whatever this says, which is why there is one key rather than two.
        "client_kind": client_kind,
        # Provenance of the WASM bundle, and what the preflight bootstrap probe
        # found. Both only exist for a WASM run, and both are unreconstructable
        # afterwards — the bundle is overwritten per deployment name and the
        # probe was a moment in time.
        "wasm_build": deploy_args.get("wasm_build") if is_wasm else None,
        "wasm_bootstrap": deploy_args.get("wasm_bootstrap") if is_wasm else None,
        "evm_network": "arbitrum-one",
        "bootstrap_peers_url": deploy_args.get("prod_bootstraps_url"),
        "binary": deploy_args.get("ant_binary"),
        "spec": deploy_args.get("spec"),
        "verifiers": deploy_args.get("verifiers") or [],
        "provider": deploy_args.get("provider"),
        "monitoring_provider": deploy_args.get("monitoring_provider"),
        "monitoring_name": deploy_args.get("monitoring_name"),
        # What it draws each cycle, not what it has drawn: files are claimed by
        # the VM as it uploads, so there is no fixed list to record.
        "pool_request": deploy_args.get("pool_request"),
        "pool_min_size": deploy_args.get("pool_min_size"),
        "pool_max_size": deploy_args.get("pool_max_size"),
        "settings": {
            # None on the browser path: the peer cache and the `ant` verbosity
            # flag are both refused there, so a value would describe a knob that
            # had no effect on the run.
            "bootstrap_cache": (None if is_wasm else deploy_args.get("bootstrap_cache")),
            "payment_type": deploy_args.get("payment_type"),
            "log_level": None if is_wasm else deploy_args.get("log_level"),
            "rust_log": deploy_args.get("rust_log"),
            "readback_verify": deploy_args.get("readback_verify", True),
            # The anchor, plus the interval every deployment created before it
            # carries instead. Both are reported as stored: a consumer reading a
            # permanent monitor's row needs to know which rule it actually runs.
            "daily_at": deploy_args.get("daily_at"),
            "sleep_interval_secs": deploy_args.get("sleep_interval_secs"),
            "verify_interval_secs": deploy_args.get("verify_interval_secs"),
            "influx_retention_days": deploy_args.get("influx_retention_days"),
            "log_retention_days": deploy_args.get("log_retention_days"),
        },
        # Where results actually live. Unlike the download kinds, whose per-op
        # logs die with the VM, this deployment's results are already durable —
        # the VMs write them to Postgres as they go. The on-VM logs are only
        # useful for diagnosing the service itself.
        "results": {
            "authoritative_store": "postgres",
            "upload_table": "deployment_prod_upload_results",
            "verification_table": "deployment_prod_upload_verifications",
            "claim_table": "prod_file_pool_claims",
            "influx_measurements": [
                "prod_upload",
                "prod_upload_verify",
                "prod_upload_pool_shortfall",
            ],
            "note": (
                "Uploads and read-back verifications are written to Postgres "
                "by the VMs as they happen; the Influx copies expire with the "
                "bucket retention. `deployments record-results` is a no-op for "
                "this kind."
            ),
        },
        "paths": {
            "uploader": {
                "service_name": UPLOADER_SERVICE,
                "env_file": UPLOADER_ENV_PATH,
                "data_dir": UPLOADER_DATA_DIR,
                "log_dir": UPLOADER_LOG_DIR,
                "stdout_glob": f"{UPLOADER_LOG_DIR}/*.stdout",
                "stderr_glob": f"{UPLOADER_LOG_DIR}/*.stderr",
            },
            "verifier": {
                "service_name": VERIFIER_SERVICE,
                "env_file": VERIFIER_ENV_PATH,
                "data_dir": VERIFIER_DATA_DIR,
                "log_dir": VERIFIER_LOG_DIR,
            },
            # Always given: the verifiers run it whichever transport uploaded,
            # and on a WASM run the uploader VM has nothing installed there —
            # the three `ant_wasm_*` entries name what actually wrote.
            "ant_binary": "/root/.local/bin/ant",
            "ant_wasm_cli": ANT_WASM_CLI_PATH if is_wasm else None,
            "ant_wasm_bundle_dir": WASM_CLIENT_DIR if is_wasm else None,
            "ant_wasm_build_info": WASM_BUILD_INFO_PATH if is_wasm else None,
            "registry_client_python": VENV_PYTHON,
        },
    }


def _build_download_config(deploy_args: dict | None) -> dict | None:
    """Describe how a prod download deployment is configured.

    The VM IP alone doesn't say what the run is doing. This carries the address
    list it walks, the knobs that change how `ant` is invoked, and the on-VM
    paths where the evidence lands — the things you need to interpret a result
    or go fetch the artefacts before teardown.

    Sourced from the registry's ``deploy_args`` because it is the only place
    the resolved address list is recorded; returns None when that is
    unavailable, so a caller can still emit the VM sections.
    """
    if not deploy_args:
        return None

    kind = deploy_args.get("kind")
    addresses = deploy_args.get("addresses") or []
    diagnostics = bool(deploy_args.get("download_diagnostics", False))
    client_kind = deploy_args.get("client_kind") or "native"
    is_wasm = client_kind == "wasm"

    config = {
        "kind": kind,
        "one_shot": kind == "prod-downloaders",
        # Which transport measured this run. Everything else here means
        # something different depending on it: the browser path runs `ant-wasm`
        # rather than `ant`, and refuses a file over 1 GB.
        "client_kind": client_kind,
        # Provenance of the WASM bundle, and what the preflight bootstrap probe
        # found. Both only exist for a WASM run, and both are unreconstructable
        # afterwards — the bundle is overwritten per deployment name and the
        # probe was a moment in time.
        "wasm_build": deploy_args.get("wasm_build") if is_wasm else None,
        "wasm_bootstrap": deploy_args.get("wasm_bootstrap") if is_wasm else None,
        "evm_network": "arbitrum-one",
        "bootstrap_peers_url": deploy_args.get("prod_bootstraps_url"),
        "addresses_file": deploy_args.get("addresses_file"),
        "address_count": len(addresses),
        "addresses": list(addresses),
        "binary": deploy_args.get("ant_binary"),
        "spec": deploy_args.get("spec"),
        "provider": deploy_args.get("provider"),
        "monitoring_provider": deploy_args.get("monitoring_provider"),
        "monitoring_name": deploy_args.get("monitoring_name"),
        "settings": {
            # None on the browser path: the peer cache, heaptrack and
            # --download-diagnostics are all refused there (they act on `ant`,
            # which never runs), so a value would describe a knob that had no
            # effect on the run.
            "bootstrap_cache": (None if is_wasm else deploy_args.get("bootstrap_cache")),
            "download_diagnostics": diagnostics,
            "memory_profiling": bool(deploy_args.get("memory_profiling", False)),
            "forward_logs": bool(deploy_args.get("forward_logs", False)),
            "rust_log": deploy_args.get("rust_log"),
            "clock_skew_seconds": deploy_args.get("clock_skew"),
            "network_profile": deploy_args.get("network"),
            # Only meaningful for the continuous kind; a one-shot run exits
            # after a single pass. `sleep_interval_secs` is what deployments
            # created before the daily anchor carry.
            "daily_at": deploy_args.get("daily_at"),
            "sleep_interval_secs": deploy_args.get("sleep_interval_secs"),
        },
        # Where the run's artefacts live on the VM. The per-op logs and the
        # diagnostics JSONL are destroyed with the VM and nothing collects them
        # automatically, so a consumer needs these paths to scp them off first.
        "paths": {
            "service_name": PROD_DOWNLOADER_SERVICE_NAME,
            # The native binary path is always given: the service, unit and log
            # layout are the native ones whichever transport runs. On a WASM run
            # nothing is installed there, and the two `ant_wasm_*` entries below
            # name what actually ran.
            "ant_binary": PROD_DOWNLOADER_ANT_INSTALL_PATH,
            "ant_wasm_cli": ANT_WASM_CLI_PATH if is_wasm else None,
            "ant_wasm_bundle_dir": WASM_CLIENT_DIR if is_wasm else None,
            "ant_wasm_build_info": WASM_BUILD_INFO_PATH if is_wasm else None,
            "env_file": PROD_DOWNLOADER_ENV_PATH,
            "addresses_file": PROD_DOWNLOADER_ADDRESSES_PATH,
            "download_dir": PROD_DOWNLOADER_DOWNLOAD_DIR,
            "log_dir": PROD_DOWNLOADER_LOG_DIR,
            "stdout_glob": f"{PROD_DOWNLOADER_LOG_DIR}/*.stdout",
            "stderr_glob": f"{PROD_DOWNLOADER_LOG_DIR}/*.stderr",
            "diagnostics_glob": (
                f"{PROD_DOWNLOADER_LOG_DIR}/*.diag.jsonl" if diagnostics else None
            ),
            "heaptrack_dir": (
                PROD_DOWNLOADER_HEAPTRACK_DIR if deploy_args.get("memory_profiling") else None
            ),
        },
    }
    return config


ELASTICSEARCH_KIND = "elasticsearch"


def build_elasticsearch_config(elastic_state: dict, ssh_key: str) -> dict:
    """How to reach a standalone Elasticsearch instance, for whoever reads this.

    Two different routes, and conflating them is the trap this section exists to
    avoid: the public URL accepts **writes only** — its proxy allowlists the bulk
    and document endpoints and 404s everything else — so a read API key used
    against it fails with a 404 that looks like a missing index rather than a
    permissions problem. Reads have to reach Elasticsearch on the box, which
    means SSH. Both are spelled out rather than left to be discovered.

    The admin password is deliberately absent: this file gets shared. Anyone who
    needs it has SSH access and can read it from the box, or run `elastic info`.
    """
    vm_ip = elastic_state.get("vm_ip", "")
    hostname = elastic_state.get("elasticsearch_ingest_hostname", "")
    ssh_key_path = str(Path(ssh_key).expanduser())
    return {
        "kind": ELASTICSEARCH_KIND,
        "public_ingest": bool(elastic_state.get("public_ingest", False)),
        # --- writing (community forwarders) --------------------------------
        "ingest": {
            "url": elastic_state.get("elasticsearch_url", ""),
            "hostname": hostname,
            "index_pattern": elastic_state.get("elasticsearch_index_pattern", ""),
            "index_naming": "beta-nodes-YYYY.MM.DD (UTC, one shared daily index)",
            "time_field": "@timestamp",
            "bulk_action": "create",
            "auth_header": "Authorization: ApiKey <credential>",
            "max_body_bytes": 50_000_000,
            "min_log_level": elastic_state.get("elasticsearch_min_log_level", ""),
            "note": (
                "Write-only. The proxy exposes _bulk/_doc/_create and 404s "
                "everything else; the action must be `create` (an `index` "
                "action is refused 403). Documents below min_log_level are "
                "dropped at ingest and answer 200/noop, which is not an error. "
                "Mint a participant credential with `saorsa-deploy elastic "
                "mint-key --user <handle>`."
            ),
        },
        # --- reading (us, and the beta-health-verdict skill) ----------------
        "read": {
            "api_key": elastic_state.get("elasticsearch_api_key", ""),
            "index_patterns": [elastic_state.get("elasticsearch_index_pattern", "")],
            "url": "http://localhost:9200",
            "tunnel_command": (
                f"ssh -i {ssh_key_path} -L 9200:localhost:9200 {DEFAULT_SSH_USER}@{vm_ip}"
            ),
            "note": (
                "Elasticsearch binds to loopback and 9200 is firewalled, so "
                "this key CANNOT be used against the public URL — that returns "
                "404 at the proxy. Open the tunnel above (or query from the box "
                "over SSH) and use the key against http://localhost:9200."
            ),
        },
        "retention_days": elastic_state.get("elasticsearch_retention_days"),
        "paths": {
            "data": "/mnt/elastic-storage/elasticsearch",
            "logs": "/mnt/elastic-storage/elasticsearch/logs",
            "credentials": "/etc/elasticsearch/ant-credentials",
            "proxy_config": "/etc/caddy/Caddyfile",
        },
        "services": ["elasticsearch", "caddy"],
    }


def build_elasticsearch_inventory(
    name: str,
    *,
    elastic_state: dict,
    ssh_key: str,
) -> dict:
    """Inventory for a standalone Elasticsearch deployment.

    No node/bootstrap/client VMs and no devnet manifest: this kind stands up one
    observability VM and nothing else. The VM itself is described in
    ``observability_vms`` alongside every other stack's, so a reader does not
    need a special case to find it.
    """
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "name": name,
        "node_vms": [],
        "bootstrap_vms": [],
        "client_vms": [],
        "observability_vms": _build_observability_vms_field(name, ssh_key, None, elastic_state),
        "client_config": None,
        "elasticsearch_config": build_elasticsearch_config(elastic_state, ssh_key),
    }


def build_client_inventory(
    name: str,
    clients: list[dict] | None,
    ssh_key: str = DEFAULT_SSH_KEY,
    deploy_args: dict | None = None,
    monitoring_state: dict | None = None,
    elastic_state: dict | None = None,
    created_at: str | None = None,
    deployment_id: int | None = None,
) -> dict:
    """Build the inventory for a client-only deployment against production.

    Same schema as :func:`build_inventory`, with ``node_vms`` and
    ``bootstrap_vms`` empty — these kinds join the production network rather
    than standing up their own — plus a populated ``client_config``.

    Pure function, like its node-bearing sibling.
    """
    if not clients:
        raise ValueError(
            f"No client VMs recorded for '{name}'. The deployment has no "
            "client state — was it created, and not yet destroyed?"
        )

    created = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    providers = {c.get("provider", "digitalocean") for c in clients}

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "testnet_id": name,
        **_deployment_id_field(deployment_id),
        "created_at": created,
        # No ant-node runs in these deployments; the `ant` client binary is
        # described by client_config.binary instead.
        "binary": {"ant_node_version": "", "ant_node_release_url": ""},
        "firewalls": {p: FIREWALL_PLACEHOLDER for p in sorted(providers)},
        "bootstrap_vms": [],
        "node_vms": [],
        "client_vms": _build_client_vms_field(name, clients, ssh_key),
        "observability_vms": _build_observability_vms_field(
            name, ssh_key, monitoring_state, elastic_state
        ),
        "client_config": build_client_config(deploy_args),
    }


def _bootstrap_multiaddrs(state: dict) -> list[str]:
    bootstrap_port = state.get("bootstrap_port")
    bootstrap_nodes = state.get("bootstrap_nodes") or []
    multiaddrs = []
    for node in sorted(bootstrap_nodes, key=lambda n: n.get("bootstrap_index", 0)):
        port = node.get("port", bootstrap_port)
        multiaddrs.append(f"/ip4/{node['ip']}/udp/{port}/quic")
    return multiaddrs


def _total_node_count(state: dict, node_count_per_vm: int) -> int:
    vm_ips = state.get("vm_ips") or {}
    node_vm_count = sum(len(ips) for ips in vm_ips.values())
    return node_count_per_vm * node_vm_count


def build_devnet_manifest(
    state: dict,
    wallet_private_key: str,
    evm_network: str = DEVNET_EVM_NETWORK,
    node_count: int | None = None,
    created_at: str | None = None,
) -> dict:
    """Build the `ant_node::devnet::DevnetManifest` dict for a deployment.

    Pure function for testability. Shape matches ant-node byte-for-byte so
    that `serde_json::from_str::<DevnetManifest>` accepts the output.

    Args:
        state: Loaded deployment state (from `load_deployment_state`).
        wallet_private_key: EVM wallet private key to embed in the manifest's
            `evm` block.
        evm_network: EVM network key into `saorsa_deploy.evm` (default
            arbitrum-sepolia).
        node_count: Nodes-per-VM; defaults to `state['node_count']`. The
            manifest emits the cluster total (per-VM × node VM count).
        created_at: Override timestamp (for deterministic tests). Default is
            `datetime.now(UTC).isoformat()`.
    """
    resolved_per_vm = node_count if node_count is not None else state.get("node_count")
    if resolved_per_vm is None:
        raise ValueError(
            "node_count is missing from deployment state. "
            "Run 'provision' first (or pass --node-count to override)."
        )

    bootstrap_port = state.get("bootstrap_port")
    if not bootstrap_port:
        raise ValueError(
            "bootstrap_port is missing from deployment state. "
            "Has the nodes provision-genesis command been run?"
        )

    evm_info = get_evm_network_info(evm_network)
    created = created_at or datetime.now(timezone.utc).isoformat()

    return {
        "base_port": bootstrap_port,
        "node_count": _total_node_count(state, resolved_per_vm),
        "bootstrap": _bootstrap_multiaddrs(state),
        "data_dir": DEVNET_DATA_DIR,
        "created_at": created,
        "evm": {
            "rpc_url": evm_info["rpc_url"],
            "wallet_private_key": wallet_private_key,
            "payment_token_address": evm_info["payment_token_address"],
            "payment_vault_address": evm_info["payment_vault_address"],
        },
    }


def _resolve_wallet_secret_key(args) -> str:
    """Resolve the wallet secret key from args or env.

    Priority: --wallet-secret-key > ANT_DEPLOY_SECRET_KEY. Exits on miss.
    """
    key = getattr(args, "wallet_secret_key", None) or os.environ.get("ANT_DEPLOY_SECRET_KEY")
    if not key:
        Console().print(
            "[bold red]Error:[/bold red] No wallet secret key provided. "
            "Use --wallet-secret-key or set ANT_DEPLOY_SECRET_KEY."
        )
        sys.exit(1)
    return key


def _load_registry_row(name: str):
    """The deployment's registry row, if the registry is reachable.

    Best-effort by design: the registry is optional everywhere else in this
    tool, so an unset DATABASE_URL degrades the inventory rather than failing
    it.
    """
    from saorsa_deploy.registry import get_repository

    repo = get_repository()
    if repo is None:
        return None
    return repo.get_active_by_name(name) or repo.get_by_name(name)


def _load_deploy_args(name: str) -> dict | None:
    """Read the deployment's ``deploy_args`` from the registry, if reachable."""
    deployment = _load_registry_row(name)
    if deployment is None:
        return None
    return deployment.deploy_args or None


def _load_deployment_id(name: str) -> int | None:
    """The registry row id, for stamping onto the inventory header.

    It is what lets a consumer tell a snapshot of THIS run from one left by an
    earlier deployment of the same name — file existence cannot, because the
    names are reused every run. Absent whenever the registry is unreachable;
    never written as null (see :func:`build_inventory`).
    """
    try:
        deployment = _load_registry_row(name)
    except Exception:
        return None
    return getattr(deployment, "id", None)


def _write_inventory_file(args, inventory: dict, console: Console) -> Path:
    """Write the inventory to --output (or the default path) and summarise it."""
    if args.output:
        output_path = Path(os.path.expanduser(args.output))
    else:
        output_path = _default_output_path(args.name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2) + "\n")
    console.print(f"[bold green]Wrote inventory to {output_path}[/bold green]")

    config = inventory.get("elasticsearch_config")
    if config:
        for vm in inventory["observability_vms"]:
            console.print(
                f"  {vm['role']} VM {vm['name']} — {vm['provider']}/{vm['region']} {vm['ip']}"
            )
        console.print(f"  ingest:    {config['ingest']['url']} (write-only)")
        console.print(f"  index:     {config['ingest']['index_pattern']}")
        console.print(
            f"  min level: {config['ingest']['min_log_level'] or 'unrecorded'}"
            f"   retention: {config.get('retention_days') or 'unrecorded'}d"
        )
        # The read key is in the file. Say so plainly rather than let it be
        # discovered by whoever the file is forwarded to.
        console.print(
            "  [yellow]Contains a read-only API key for the log indices. It "
            "cannot write, and it cannot be used against the public URL — but "
            "treat the file accordingly.[/yellow]"
        )
    return output_path


def _write_client_inventory(
    args,
    clients: list[dict],
    monitoring_state: dict | None,
    elastic_state: dict | None,
    state_error: str | None,
    console: Console,
) -> None:
    """Emit an inventory for a client-only deployment against production.

    Reached when there is no deployment state. That is the normal shape for the
    prod download kinds; for anything else it means the deployment doesn't
    exist, so say so with the original error rather than inventing an empty
    inventory.
    """
    deploy_args = _load_deploy_args(args.name)

    # A standalone Elasticsearch deployment has neither deployment state nor
    # client VMs — the instance IS the deployment — so it would otherwise fall
    # through the client path below and report "nothing to describe".
    if elastic_state and (deploy_args or {}).get("kind") == ELASTICSEARCH_KIND:
        inventory = build_elasticsearch_inventory(
            args.name, elastic_state=elastic_state, ssh_key=args.ssh_key_path
        )
        _write_inventory_file(args, inventory, console)
        return

    if not clients:
        console.print(f"[bold red]Error:[/bold red] {state_error}")
        console.print(
            "[dim]No client VMs are recorded for this name either, so there is "
            "nothing to describe.[/dim]"
        )
        # This is the command a teardown is verified with, so it is also where
        # a snapshot left by a deployment destroyed before the destroy commands
        # learned to retire their own gets retired. Default path only: with
        # --output the operator named a particular file, not this one.
        if not args.output:
            retire_local_inventory(args.name, console)
        sys.exit(1)

    kind = (deploy_args or {}).get("kind")
    if deploy_args is None:
        console.print(
            "[yellow]No registry row reachable (is DATABASE_URL set?) — "
            "writing VM details without the download configuration.[/yellow]"
        )
    elif kind not in PROD_CLIENT_KINDS:
        # Emit anyway rather than refuse: the VM/observability sections are
        # still accurate. Only client_config is kind-specific, and its on-VM
        # paths would be wrong for another kind's service.
        console.print(
            f"[yellow]'{args.name}' is a {kind} deployment, not one of "
            f"{sorted(PROD_CLIENT_KINDS)} — writing VM details without the "
            "client configuration.[/yellow]"
        )
        deploy_args = None

    try:
        inventory = build_client_inventory(
            args.name,
            clients=clients,
            ssh_key=args.ssh_key_path,
            deploy_args=deploy_args,
            monitoring_state=monitoring_state,
            elastic_state=elastic_state,
            deployment_id=_load_deployment_id(args.name),
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if args.output:
        output_path = Path(os.path.expanduser(args.output))
    else:
        output_path = _default_output_path(args.name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2) + "\n")

    console.print(f"[bold green]Wrote inventory to {output_path}[/bold green]")
    for vm in inventory["client_vms"]:
        console.print(f"  client VM {vm['name']} — {vm['provider']}/{vm['region']} {vm['ip']}")
    console.print(f"  {len(inventory['observability_vms'])} observability VM(s)")

    config = inventory["client_config"]
    if config:
        # Keyed off the kind rather than probing for fields: the write test has
        # no address list at all (it claims files per cycle), so summarising it
        # with the download kinds' vocabulary means a KeyError at the end of an
        # otherwise-successful deploy.
        if config.get("kind") == PROD_UPLOAD_TEST_KIND:
            request = config.get("pool_request") or {}
            drawn = ", ".join(f"{count} {name}" for name, count in request.items())
            console.print(
                f"  uploads {drawn or 'nothing'} per cycle, "
                f"{len(config.get('verifiers') or [])} verifier region(s)"
            )
        else:
            console.print(
                f"  {config['address_count']} address(es), "
                f"{'one-shot' if config['one_shot'] else 'continuous'}, "
                f"diagnostics="
                f"{'on' if config['settings']['download_diagnostics'] else 'off'}"
            )
    console.print(
        "[dim]Production deployment (joins the production network) — no node "
        "VMs, no bootstrap VMs, no devnet manifest.[/dim]"
    )


def cmd_generate_inventory(args):
    """Generate a schema-version-4 inventory.json for a deployment."""
    console = Console()

    console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
    # The prod client kinds (downloaders / continuous-downloader) never write
    # deployment state — they join production rather than standing up nodes —
    # so an absent state is not necessarily an error.
    state_error: str | None = None
    try:
        state = load_deployment_state(args.name)
    except RuntimeError as e:
        state = None
        state_error = str(e)

    clients: list[dict] = []
    try:
        client_state = load_client_state(args.name)
    except RuntimeError:
        client_state = None
    if client_state:
        clients = client_state.get("clients") or []
    # Plus each client extension's VMs. They are the deployment's client VMs —
    # nothing else inventories them, and their per-VM paths (a pointer client's
    # owner keys, a diagnostics log) die with the VM. Tagged with the extension
    # they belong to, so the two tiers' teardown commands cannot be confused.
    for extension, tier in client_extension_tiers(args.name):
        for client in tier.get("clients") or []:
            clients.append({**client, "extension": extension})

    # The deployment's own monitoring / logging stack, if any (best-effort).
    try:
        monitoring_state = load_monitoring_state(args.name)
    except RuntimeError:
        monitoring_state = None
    try:
        elastic_state = load_elastic_state(args.name)
    except RuntimeError:
        elastic_state = None

    if state is None:
        _write_client_inventory(
            args, clients, monitoring_state, elastic_state, state_error, console
        )
        return

    try:
        inventory = build_inventory(
            state,
            ssh_key=args.ssh_key_path,
            node_count=args.node_count,
            clients=clients,
            monitoring_state=monitoring_state,
            elastic_state=elastic_state,
            deployment_id=_load_deployment_id(args.name),
        )
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # The devnet manifest describes a self-hosted network (our own genesis +
    # bootstraps). prod-* kinds JOIN production (bootstraps tagged region "prod"),
    # where it — and its wallet key — is meaningless, so skip it for those.
    prod_seeded = any(n.get("region") == "prod" for n in (state.get("bootstrap_nodes") or []))
    manifest = None
    if not prod_seeded:
        wallet_secret_key = _resolve_wallet_secret_key(args)
        try:
            manifest = build_devnet_manifest(
                state,
                wallet_private_key=wallet_secret_key,
                node_count=args.node_count,
            )
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

    if args.output:
        output_path = Path(os.path.expanduser(args.output))
    else:
        output_path = _default_output_path(args.name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, indent=2) + "\n")

    node_vm_count = len(inventory["node_vms"])
    services_per_vm = len(inventory["node_vms"][0]["services"]) if node_vm_count else 0
    total_nodes = node_vm_count * services_per_vm
    bootstrap_vm_count = len(inventory["bootstrap_vms"])
    client_vm_count = len(inventory["client_vms"])
    obs_vm_count = len(inventory["observability_vms"])
    nat_vm_count = sum(1 for vm in inventory["node_vms"] if vm.get("nat_simulated"))
    console.print(f"[bold green]Wrote inventory to {output_path}[/bold green]")
    console.print(f"  {bootstrap_vm_count} bootstrap VM(s)")
    nat_suffix = f" ({nat_vm_count} NAT-simulated)" if nat_vm_count else ""
    console.print(
        f"  {node_vm_count} node VMs × {services_per_vm} service(s) each = "
        f"{total_nodes} nodes{nat_suffix}"
    )
    console.print(f"  {client_vm_count} client VM(s)")
    console.print(f"  {obs_vm_count} observability VM(s)")

    if manifest is not None:
        manifest_path = output_path.parent / DEVNET_MANIFEST_FILENAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        console.print(f"[bold green]Wrote devnet manifest to {manifest_path}[/bold green]")
        console.print(
            f"  {len(manifest['bootstrap'])} bootstrap multiaddr(s), "
            f"{manifest['node_count']} nodes total, "
            f"evm={DEVNET_EVM_NETWORK}"
        )
    else:
        console.print(
            "[dim]Skipped devnet manifest (production deployment — joins the "
            "production network).[/dim]"
        )
