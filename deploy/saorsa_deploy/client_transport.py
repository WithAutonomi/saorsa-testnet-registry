"""Which transport performed a client operation: ``native`` or ``wasm``.

Both transports emit the same ``upload``/``download`` Influx measurements — that
is what lets the whole results pipeline work on WASM operations unchanged — so
the measurement cannot say which one produced a point. Two different things can:

* On a **testnet**, the client VM's ROLE. A testnet runs both tiers side by side,
  so the transport is per-VM and the deployment's S3 client state records it.
* On a **production client kind**, the DEPLOYMENT. One transport per deployment,
  recorded in ``deploy_args.client_kind`` at deploy time. The role there is
  always the native ``download``/``upload`` (the VM comes from `clients infra`),
  so reading the role would report every production WASM run as native.

Both rules live here because three commands need them and had drifted: the
recording path learned the ``deploy_args`` rule while `print-downloads` kept
reading the role, so a production WASM run recorded correctly and then printed
``transport=native``.

``deploy_args`` is also the more durable source. `clients destroy` deletes the S3
client state, so after teardown the role is unknowable — while the registry row
is not.
"""

from __future__ import annotations

from saorsa_deploy.client_roles import is_browser, is_wasm

NATIVE = "native"
WASM = "wasm"

#: Kinds whose transport is a property of the deployment rather than of a VM.
DEPLOYMENT_SCOPED_KINDS = frozenset({"prod-downloaders", "prod-continuous-downloader"})


def deployment_scoped_transport(deploy_args: dict | None) -> str | None:
    """The whole deployment's transport, or None when it is not deployment-scoped.

    ``None`` means "ask the per-VM rule instead", and is returned for a testnet
    and for any kind this does not cover. A covered kind with no recorded
    ``client_kind`` reads as native: every such deployment made before the option
    existed was native, and that is what NULL means everywhere else too.
    """
    if not deploy_args:
        return None
    if deploy_args.get("kind") not in DEPLOYMENT_SCOPED_KINDS:
        return None
    return deploy_args.get("client_kind") or NATIVE


def transport_map_from_deploy_args(deploy_args: dict | None) -> dict[str, str]:
    """Map hostname -> transport from a `prod-uploaders` deployment's deploy_args.

    `prod-uploaders` is the one production kind that runs both transports at
    once — a native uploader and a browser-path uploader side by side is the
    point of it — so the transport is per-VM here, not per-deployment. It still
    must not come from the S3 client state: `clients destroy` deletes that, and a
    `record-results` re-run after teardown would then resolve None for every host
    and relabel every WASM upload as native.

    The persisted uploader list carries the hostname each entry's VM reports
    under, so nothing here has to rebuild it. An entry written before the browser
    path existed has neither key and contributes nothing, which leaves its rows
    NULL — and NULL reads as native everywhere else too.
    """
    if not deploy_args:
        return {}
    out: dict[str, str] = {}
    for entry in deploy_args.get("uploaders") or []:
        if not isinstance(entry, dict):
            continue
        hostname = entry.get("hostname")
        transport = entry.get("client_kind")
        if hostname and transport:
            out[hostname] = transport
    return out


def transport_for_role(role: str | None) -> str | None:
    """The transport a client role implies, or None when the question does not apply.

    None for an unknown role, and None for the BROWSER role: it writes its own
    `browser_download` measurement and never an `upload` or a `download`, so it
    has no row for a transport label to go on. Returning `native` for it (which
    an `is_wasm` test alone does) would hand the results pipeline a browser host
    mapped to the wrong transport the moment anything started reading its
    operations.
    """
    if not role or is_browser(role):
        return None
    return WASM if is_wasm(role) else NATIVE


def transport_map_from_client_state(name: str, state: dict | None) -> dict[str, str]:
    """Map hostname -> transport from a deployment's S3 client state.

    The hostname is the one the Influx ``host`` tag carries, built the way the
    provider's Terraform module builds it.
    """
    if not state:
        return {}
    out: dict[str, str] = {}
    for client in state.get("clients", []):
        role = client.get("role")
        transport = transport_for_role(role)
        if transport is None:
            continue
        host = f"{name}-ant-client-{role}-{client.get('region')}-{client.get('client_index')}"
        out[host] = transport
    return out
