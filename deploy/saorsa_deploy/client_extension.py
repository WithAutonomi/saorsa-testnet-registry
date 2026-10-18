"""Client extensions: a client tier added to a deployment that is already running.

Client VMs have always had a lifecycle independent of the node fleet (`clients
infra` / `provision` / `start` / `stop` / `destroy` all take a deployment name
and work against a live testnet), but the only *manifest* was the deployment
manifest, with the client tiers declared inside it and brought up by
``deployments up``. A ``kind: clients`` manifest names an existing deployment
and declares client tiers only, so a run can bring a large cohort up after the
deployment has warmed up, measure, and tear the cohort down while the deployment
carries on.

Three rules in here are load-bearing:

* **The Terraform name is ``<deployment>-<extension>``.** That one substitution
  namespaces the VM hostname, the Terraform workspace directory and the tfstate
  key in a single place, because all three are built from the same ``name``
  variable. Nothing in the Terraform modules changes, and an unextended
  deployment renders byte-identical.
* **``client_index`` continues from the highest index already in use for that
  role**, across the deployment's own clients and its other extensions. The
  index is part of the per-uploader S3 address key
  (``upload-<region>-<n>.txt``), and each flush is a whole-object PUT -- so two
  uploaders sharing an index silently erase each other's address list on every
  flush.
* **An extension never declares binaries.** It reuses what the deployment
  recorded at provision time, so an extension cannot run a different client
  build from the deployment it is measuring.
"""

from __future__ import annotations

import re
import sys

#: An extension name goes into a VM hostname, a Terraform workspace path and an
#: S3 key, so it is restricted to what all three accept without quoting.
EXTENSION_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")

#: Deployment-state key holding the client binaries `clients provision` resolved,
#: so an extension's provision pass can reuse them without being told.
CLIENT_BINARIES_STATE_KEY = "client_binaries"


class ExtensionError(Exception):
    """A client-extension rule was broken (user-facing message)."""


def validate_extension_name(name: str) -> str:
    """Return ``name`` if it is a usable extension name, else raise."""
    from saorsa_deploy.client_roles import ALL_CLIENT_ROLES

    if not name or not EXTENSION_NAME_RE.match(name):
        raise ExtensionError(
            f"invalid client-extension name '{name}': must match "
            f"{EXTENSION_NAME_RE.pattern} (letters, digits and hyphens only — "
            "it becomes a VM hostname, a Terraform workspace and an S3 key)"
        )
    # A tfstate key is `<prefix>-<tf name>-<role>-<region>-<index>.tfstate`, and
    # the orphan-recovery scan in `clients destroy` reads the role off the first
    # thing after the deployment name. An extension called `upload-extra` makes
    # `DEV-01-upload-extra-download-lon1-1` parse as role `upload` of DEV-01, so
    # a recovery run against the DEPLOYMENT would adopt the extension's VM and
    # then destroy against a state key that does not exist.
    clashing = [r for r in ALL_CLIENT_ROLES if name == r or name.startswith(f"{r}-")]
    if clashing:
        raise ExtensionError(
            f"invalid client-extension name '{name}': it starts with the client "
            f"role '{clashing[0]}', which makes its Terraform state key "
            f"ambiguous with the deployment's own clients. Pick a name that "
            f"does not begin with {', '.join(ALL_CLIENT_ROLES)}."
        )
    return name


def terraform_name(deployment: str, extension: str | None) -> str:
    """The ``name`` Terraform variable for a client tier.

    For the deployment's own clients this is the deployment name, unchanged. For
    an extension it is ``<deployment>-<extension>``, which is what makes its VMs
    (``<deployment>-<extension>-ant-client-<role>-<region>-<n>``), its workspace
    and its tfstate key distinguishable from the deployment's own
    (``<deployment>-ant-client-<role>-<region>-<n>``) in every provider console.
    """
    if extension is None:
        return deployment
    return f"{deployment}-{extension}"


def journal_name(deployment: str, extension: str) -> str:
    """Step-journal name for an extension's ``clients up`` run.

    A double hyphen, not a single one: the deployment's own journal is keyed on
    its bare name, and ``DEV-01--browsers`` cannot be mistaken for a deployment
    called ``DEV-01-browsers``.
    """
    return f"{deployment}--{extension}"


def highest_client_indexes(deployment: str, exclude: str | None = None) -> dict[str, int]:
    """Highest ``client_index`` in use per role across ``deployment``.

    Covers the deployment's own client state and every extension's, because the
    per-uploader S3 address key is namespaced by role and index only — not by
    extension — so an index reused by a second tier would collide with a live
    uploader's address object.

    ``exclude`` drops one extension from the scan, and the caller computing its
    OWN indexes must pass its own name: otherwise a re-run (``up --resume``,
    or a retry after a partial apply) would read the indexes it allocated last
    time as "already in use" and shift this time's allocation, which reads as a
    spec mismatch against VMs that are running exactly what was asked for.

    A missing or unreadable state contributes nothing: this is a floor for the
    next index, and the alternative (failing) would stop an extension from being
    created against a deployment whose own clients were never configured.
    """
    from saorsa_deploy import state as state_mod

    highest: dict[str, int] = {}

    def _absorb(doc: dict | None) -> None:
        if not doc:
            return
        for entry in doc.get("clients") or []:
            role = entry.get("role", "upload")
            try:
                index = int(entry.get("client_index") or 0)
            except (TypeError, ValueError):
                continue
            if index > highest.get(role, 0):
                highest[role] = index

    try:
        _absorb(state_mod.load_client_state(deployment))
    except Exception:
        pass
    try:
        names = state_mod.list_client_extensions(deployment)
    except Exception:
        names = []
    for name in names:
        if name == exclude:
            continue
        try:
            _absorb(state_mod.load_client_state(deployment, name))
        except Exception:
            continue
    return highest


def client_extension_tiers(deployment: str) -> list[tuple[str, dict]]:
    """``(extension_name, client_state)`` for each client extension of ``deployment``.

    Deliberately EXCLUDES the deployment's own client tier: every consumer
    already loads that itself (and every test already controls that load), so
    folding the two together here would move a call the callers' own error
    handling depends on. Name order, so output is stable.

    A scan or load failure contributes nothing rather than raising. These
    callers are reporting commands: a missing extension is a tier absent from a
    report, where an exception is a report nobody gets.
    """
    from saorsa_deploy import state as state_mod

    try:
        names = state_mod.list_client_extensions(deployment)
    except Exception:
        return []
    tiers: list[tuple[str, dict]] = []
    for name in names:
        try:
            tiers.append((name, state_mod.load_client_state(deployment, name)))
        except Exception:
            continue
    return tiers


def client_extension_names(deployment: str) -> list[str]:
    """Names of the client extensions standing against ``deployment``, if any.

    A failure to scan returns an empty list rather than raising. The caller is a
    teardown guard: a transient S3 error must not be the reason a deployment
    cannot be torn down, and the ``clients down`` the operator is being asked to
    run is itself idempotent.
    """
    from saorsa_deploy import state as state_mod

    try:
        return state_mod.list_client_extensions(deployment)
    except Exception:
        return []


def refuse_while_client_extensions_exist(deployment: str, console, what: str) -> None:
    """Exit 2 when ``deployment`` still has client extensions standing.

    The extensions' VMs are provisioned against this deployment's bootstraps,
    InfluxDB, Elasticsearch and address pool. Destroying any of those out from
    under them leaves VMs that are running, billing, and failing every
    operation — with the measurements they were created to take unusable. So
    the teardown refuses and names the one command that clears each one.

    No flag overrides it, exactly as ``bootstraps destroy`` already refuses
    while a node extension shares its bootstraps: the override an operator
    would reach for is ``clients down``, which does the thing properly.
    """
    names = client_extension_names(deployment)
    if not names:
        return
    console.print(
        f"[bold red]Error:[/bold red] Cannot {what} '{deployment}' — "
        f"{len(names)} client extension(s) are still standing against it:"
    )
    for name in names:
        console.print(f"  [red]- {name}[/red]")
    console.print()
    console.print("[bold]Tear each one down first (with its manifest, or by name):[/bold]")
    for name in names:
        console.print(f"  saorsa-deploy clients down --name {deployment} --extension {name}")
    console.print(
        "\n[dim]--force does not override this check: the extensions' VMs run "
        "against this deployment's bootstraps, InfluxDB and address pool.[/dim]"
    )
    sys.exit(2)
