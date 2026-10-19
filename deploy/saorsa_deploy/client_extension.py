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


def client_vm_name(
    deployment: str,
    extension: str | None,
    role: str,
    region: str | None,
    client_index: int | None,
) -> str:
    """The one name a client VM is known by, everywhere.

    It is the Terraform VM name, the OS hostname ``clients provision`` sets, the
    Influx ``host`` tag every client service and the browser harness emit, the
    ``browser-traces/<hostname>/`` segment of the trace archive and
    ``deployment_browser_download_records.client_hostname``. One string, so one
    function: the registry joins a trace to its row and labels a row with its
    extension on this value alone, and before 1.4.4 the provisioner built it
    without the extension segment while every reader built it with one, so an
    extension's rows could never be joined or labelled at all.

    ``client_index`` restarts at 1 per role, so the role is part of it.
    """
    return f"{terraform_name(deployment, extension)}-ant-client-{role}-{region}-{client_index}"


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

    **Tolerance here is narrow, and deliberately narrower than in
    :func:`client_extension_tiers`.** A state object that is ABSENT contributes
    nothing, because a deployment may genuinely have no client tier of its own
    and an extension listed a moment ago may have just been torn down — both are
    answers. Any other failure to enumerate or read a tier is NOT an answer, and
    it is raised: the alternative is allocating from a floor that is too low and
    handing a second extension's uploader the index of a live one, whose S3
    address object each flush then overwrites whole. That is the collision this
    function exists to prevent, so it must stop rather than guess. Callers turn
    the failure into a non-zero exit before any VM is created.
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

    # `load_client_state` raises RuntimeError, and only RuntimeError, for a
    # missing key; a credentials or connectivity failure surfaces as its own
    # exception and propagates.
    try:
        _absorb(state_mod.load_client_state(deployment))
    except RuntimeError:
        pass
    # Not guarded at all: a scan that did not complete cannot be read as "no
    # extensions". An empty bucket prefix is an empty list, not an error.
    names = state_mod.list_client_extensions(deployment)
    for name in names:
        if name == exclude:
            continue
        try:
            _absorb(state_mod.load_client_state(deployment, name))
        except RuntimeError:
            # The key went away between the list and the get: the extension was
            # torn down, so it holds no index.
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
    report, where an exception is a report nobody gets. That is the opposite
    choice to :func:`highest_client_indexes`, and the two differ because the
    consequences do: a tier missing from here costs an annotation on some rows,
    while a tier missing from the index scan costs a live uploader its address
    list.
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

    A scan failure is RAISED, for the reason it is in
    :func:`highest_client_indexes`: the caller is a teardown guard, and a scan
    that did not complete read as "no extensions" is how a live cohort's
    address pool gets wiped out from under it. Being unable to tear down on a
    transient S3 error costs a retry; the other way round costs the run. The
    destroy commands read their own state from the same bucket anyway, so a
    scan that cannot complete is a teardown that was going to fail regardless.

    An empty prefix is an empty list, not an error.
    """
    from saorsa_deploy import state as state_mod

    return state_mod.list_client_extensions(deployment)


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

    A scan that cannot complete also refuses, because "could not look" is not
    "nothing there" — and what this guard protects is not recoverable once the
    teardown has run.
    """
    try:
        names = client_extension_names(deployment)
    except Exception as e:  # noqa: BLE001 — unknown is not "none"
        console.print(
            f"[bold red]Error:[/bold red] Cannot {what} '{deployment}' — could "
            f"not determine whether any client extension is standing against "
            f"it: {e}. Refusing, because a scan that did not complete is not "
            f"the same as no extensions, and this teardown cannot be undone. "
            f"Retry once the registry's S3 state is reachable; "
            f"`saorsa-deploy clients ls --name {deployment}` lists them."
        )
        sys.exit(2)
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
