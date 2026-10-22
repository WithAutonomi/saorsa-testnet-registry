"""Which systemd unit(s) and log directory each production client kind runs.

The names themselves live in the provisioner modules that write the units, and
are imported from there rather than re-spelled here: a second copy of
``ant-prod-uploader`` is exactly the drift this map exists to prevent. What this
module adds is the *mapping* — which kind runs which unit, where that unit
writes its per-operation logs, and for the one multi-role kind which role maps
to which unit. That knowledge was previously spread across
``group_hosts_by_unit``'s control flow and a comment in the
``run-client-prod-tests`` skill telling an operator which of two unit names to
paste, and picking the wrong one produces a confident, empty forensic capture.

This is the prod-client counterpart of :mod:`saorsa_deploy.client_roles`, which
owns the testnet role-to-unit map for the same reason.

**Both transports share the unit and the log directory.** A WASM client spawns
``ant-wasm`` where a native one spawns ``ant``, but everything around the
spawn — the service, its unit name, its per-operation log files and their
directory — is unchanged by the transport, so there is no WASM variant to look
up here.
"""

from dataclasses import dataclass, field

from saorsa_deploy.client_roles import ROLE_UPLOAD
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_LOG_DIR as CONTINUOUS_UPLOADER_LOG_DIR,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    VERIFIER_LOG_DIR as UPLOAD_VERIFIER_LOG_DIR,
)
from saorsa_deploy.provisioning.prod_downloader import LOG_DIR as PROD_DOWNLOADER_LOG_DIR
from saorsa_deploy.provisioning.prod_downloader import SERVICE_NAME as PROD_DOWNLOADER_SERVICE
from saorsa_deploy.provisioning.prod_uploader import LOG_DIR as PROD_UPLOADER_LOG_DIR
from saorsa_deploy.provisioning.prod_uploader import SERVICE_NAME as PROD_UPLOADER_SERVICE


class UnknownProdClientKind(ValueError):
    """Raised for a kind this map has no unit for.

    A caller that wants a unit name must handle this rather than fall back to a
    guess: the unit names a service on a host, and a command aimed at one that
    does not exist there fails in whichever way looks most like success —
    nothing to restart, nothing to archive.
    """


@dataclass(frozen=True)
class ProdClientUnit:
    """One systemd unit a prod client kind's VMs run.

    ``label`` is what the unit's host group is called in output ("uploader",
    "downloader", "verifier"); ``is_upload`` says whether its operations pay,
    which is what decides whether there are payment markers to collect.
    """

    label: str
    service: str
    log_dir: str
    is_upload: bool
    #: Roles that run this unit. Empty together with ``catch_all`` means every
    #: VM of the kind does.
    roles: tuple[str, ...] = field(default=())
    #: Whether a role claimed by no other unit of the kind falls to this one.
    catch_all: bool = True


#: The one-shot uploader kind: every VM walks its own file list under
#: `ant-prod-uploader`, native and WASM alike.
PROD_UPLOADERS_UNIT = ProdClientUnit(
    label="uploader",
    service=PROD_UPLOADER_SERVICE,
    log_dir=PROD_UPLOADER_LOG_DIR,
    is_upload=True,
)

#: Both download kinds: every VM runs `prod_downloader.py` under
#: `ant-prod-downloader`, the one-shot kind with `ONE_SHOT=true`.
PROD_DOWNLOADER_UNIT = ProdClientUnit(
    label="downloader",
    service=PROD_DOWNLOADER_SERVICE,
    log_dir=PROD_DOWNLOADER_LOG_DIR,
    is_upload=False,
)

#: The daily write test's uploader VM. Role `upload`, and a client state entry
#: with no role at all reads as this one — see :func:`unit_for_role`.
CONTINUOUS_UPLOADER_UNIT = ProdClientUnit(
    label="uploader",
    service=UPLOADER_SERVICE,
    log_dir=CONTINUOUS_UPLOADER_LOG_DIR,
    is_upload=True,
    roles=(ROLE_UPLOAD,),
    catch_all=False,
)

#: The daily write test's verifier VMs: every other role on that kind. They
#: download and hash, so nothing on them pays and there are no markers.
UPLOAD_VERIFIER_UNIT = ProdClientUnit(
    label="verifier",
    service=VERIFIER_SERVICE,
    log_dir=UPLOAD_VERIFIER_LOG_DIR,
    is_upload=False,
    catch_all=True,
)

#: Kind -> its units, in the order they are reported.
PROD_CLIENT_UNITS: dict[str, tuple[ProdClientUnit, ...]] = {
    "prod-uploaders": (PROD_UPLOADERS_UNIT,),
    "prod-downloaders": (PROD_DOWNLOADER_UNIT,),
    "prod-continuous-downloader": (PROD_DOWNLOADER_UNIT,),
    "prod-continuous-uploader": (CONTINUOUS_UPLOADER_UNIT, UPLOAD_VERIFIER_UNIT),
}

#: The kinds that run on the operator's own machine. They have no VM, so there
#: is no unit to name and nothing to reach over SSH.
LOCAL_KINDS = frozenset({"prod-local-uploads", "prod-local-downloads"})


def units_for_kind(kind: str) -> tuple[ProdClientUnit, ...]:
    """The units a kind's client VMs run, in reporting order."""
    try:
        return PROD_CLIENT_UNITS[kind]
    except KeyError:
        raise UnknownProdClientKind(kind)


def unit_for_role(kind: str, role: str | None) -> ProdClientUnit:
    """The unit a client of ``role`` runs on ``kind``.

    A client state entry with no ``role`` reads as the uploader, which is how
    the single-role prod client kinds were recorded before roles existed.
    """
    units = units_for_kind(kind)
    resolved = role or ROLE_UPLOAD
    for unit in units:
        if resolved in unit.roles:
            return unit
    for unit in units:
        if unit.catch_all:
            return unit
    raise UnknownProdClientKind(f"{kind}: no unit runs role {resolved!r}")


def group_clients_by_unit(kind: str, clients: list[dict]) -> list[tuple[ProdClientUnit, list[str]]]:
    """Split a deployment's client VMs into ``(unit, ips)``, in unit order.

    Every VM is grouped, including a WASM continuous-uploader's uploader VM:
    the transport changes which binary the service spawns, not whether the
    service exists or where it logs. Entries with no IP are dropped — they are
    specs for a VM that was never created.
    """
    by_service: dict[str, list[str]] = {}
    for client in clients:
        ip = client.get("ip")
        if not ip:
            continue
        unit = unit_for_role(kind, client.get("role"))
        by_service.setdefault(unit.service, []).append(ip)
    return [
        (unit, by_service[unit.service])
        for unit in units_for_kind(kind)
        if by_service.get(unit.service)
    ]
