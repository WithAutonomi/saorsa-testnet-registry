"""What a host-side service script needs on the VM besides the script itself.

`prod-continuous-uploader` is the one client kind whose service runs under the
`/opt/ant-registry-client` venv and writes Postgres itself, so replacing its
script is only half an upgrade:

* the venv's ``saorsa-db`` wheel was installed at provision time, and nothing
  else updates it -- not the operator's CLI, not an Alembic migration -- so a
  script calling a repository argument that wheel does not accept raises
  ``TypeError`` on the service's FIRST operation, a successful upload included,
  before the row is written and before the pool claim in hand is resolved;
* the columns that argument writes have to exist in the registry already, or
  the same operation dies in the ``INSERT`` instead.

Two commands install that script -- ``deployments upgrade-service-script`` and
``deployments upgrade-wasm-client``, which redeploys it alongside a new bundle
-- and a coupling this sharp held in only one of them is a trap for whoever
uses the other (INFRA-484 review). So it lives here, once, and both import it.

Everything returns a problem rather than exiting: one caller refuses
immediately, the other collects every problem in the invocation and reports
them together, because its build is ~10 minutes on a VM that bills.

**The wheel has four possible sources, not one** (INFRA-517). It used to be
built from this workspace and nowhere else, which made both commands
unreachable for the one kind that needs them: every production maintenance
procedure runs the tool as a released, version-pinned install from the shared
wheelhouse and explicitly forbids running it from a checkout, so
``upgrade-wasm-client`` refused in two seconds with advice a production run may
not take. The requirement was never "built from a workspace" -- it is that the
wheel installed on the VM is the **same version** as the `saorsa-deploy`
distribution writing the script, since a release publishes both together. So
the wheel is resolved from the first source that has one, the version equality
is stated and checked rather than held by accident of where the command ran
from, and the source is reported so a run's results comment can record where
the VM's wheel came from.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from pyinfra.api import State
from sqlalchemy import inspect as sa_inspect

from saorsa_deploy.paths import state_dir
from saorsa_deploy.provisioning.saorsa_db_install import (
    SaorsaDbInstaller,
    build_saorsa_db_wheel,
    default_repo_root,
    workspace_has_package,
)

#: Kinds whose service runs under the registry-client venv and talks to
#: Postgres, rather than under `/usr/bin/python3` importing nothing of ours.
#: The download kinds need neither the wheel nor the schema and get neither.
KINDS_WITH_REGISTRY_CLIENT = frozenset({"prod-continuous-uploader"})

#: Columns the upgraded uploader script writes that an older registry does not
#: have, as ``(table, column)``. Checked, never created: a migration is the
#: operator's step, and `alembic upgrade head` is a different command with a
#: different blast radius.
REQUIRED_REGISTRY_COLUMNS = (
    ("deployment_prod_upload_results", "error_kind"),
    ("deployment_upload_records", "error_kind"),
    ("deployment_upload_records", "reason"),
)

#: The workspace package installed on the VM, and the distribution name its
#: wheel carries.
SAORSA_DB_PACKAGE = "saorsa-db"
SAORSA_DB_DIST = "saorsa_db"

#: Where a released install looks for the wheel when there is no workspace.
#: A convention of the operational procedures (`uvx --no-sources --find-links
#: ~/.local/share/saorsa-deploy/wheels`) until this made it a default the tool
#: itself knows, overridable for a scratch directory.
WHEELHOUSE_ENV_VAR = "SAORSA_DEPLOY_WHEELHOUSE"
WHEELHOUSE_DIR_NAME = "wheels"

#: An operator with the file in hand, without reaching for a flag.
WHEEL_PATH_ENV_VAR = "SAORSA_DB_WHEEL"

#: This repository: its releases are the wheel store (there is no index).
GITHUB_REPO = "WithAutonomi/saorsa-testnet-registry"

#: The wheel is tens of kilobytes; this only has to outlast a bad connection.
_DOWNLOAD_TIMEOUT_SECONDS = 120

_METADATA_LINE_RE = re.compile(r"^(?P<field>Name|Version):\s*(?P<value>.+?)\s*$")


class WheelUnavailable(RuntimeError):
    """No source produced a usable `saorsa-db` wheel for this version."""


@dataclass(frozen=True)
class ResolvedWheel:
    """The wheel to install, where it came from, and anything to say about it.

    The source travels with the path because "which wheel went onto the VM" is
    half the answer and "where it came from" is the other half: a wheelhouse
    copy, a working-tree build and a release download are all legitimate and a
    maintenance run's results comment has to be able to say which it was.
    """

    path: Path
    source: str
    version: str | None
    #: Printed in yellow by the caller. Set when the version check was skipped.
    warning: str | None = None

    def describe(self) -> str:
        return f"{self.path.name} ({self.source})"


def running_version() -> str:
    """The version of the `saorsa-deploy` distribution that is executing."""
    return importlib.metadata.version("saorsa-deploy")


def wheelhouse_dir() -> Path:
    """The directory a released install looks for published wheels in."""
    override = os.environ.get(WHEELHOUSE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return state_dir() / WHEELHOUSE_DIR_NAME


def release_wheel_url(version: str) -> str:
    """The `saorsa-db` wheel asset on this repository's ``v<version>`` release."""
    return (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"v{version}/{SAORSA_DB_DIST}-{version}-py3-none-any.whl"
    )


def wheel_distribution(path: Path) -> tuple[str, str]:
    """``(name, version)`` from a wheel's own ``METADATA``.

    Read out of the archive rather than parsed off the file name, because a
    name is evidence of nothing: this is also what proves a downloaded file is
    a readable zip holding the distribution it claims to, before it is
    installed into a production VM's venv.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            entries = sorted(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            if not entries:
                raise ValueError(f"not a wheel: {path} holds no .dist-info/METADATA")
            text = archive.read(entries[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a readable wheel: {path} ({exc})") from None

    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            # The headers end at the first blank line; the long description
            # below it may contain anything, including these field names.
            break
        match = _METADATA_LINE_RE.match(line)
        if match and match.group("field") not in found:
            found[match.group("field")] = match.group("value")
    if "Name" not in found or "Version" not in found:
        raise ValueError(f"{path}: METADATA declares no Name/Version")
    return found["Name"], found["Version"]


def _normalise(name: str) -> str:
    return name.replace("_", "-").strip().lower()


def _checked(
    path: Path,
    source: str,
    expected: str,
    allow_version_mismatch: bool,
) -> ResolvedWheel:
    """``path`` as a resolved wheel, or raise unless it is the right version.

    The invariant the two commands rest on: the wheel on the VM and the
    distribution writing the service script are published together, so they
    must be the same version. A script calling a repository argument an older
    wheel does not accept raises ``TypeError`` on the service's first
    operation, while it holds a pool claim -- which is the whole reason the
    wheel travels with the script at all.
    """
    name, version = wheel_distribution(path)
    if _normalise(name) != SAORSA_DB_PACKAGE:
        raise ValueError(f"{path} is a {name} wheel, not {SAORSA_DB_PACKAGE}")
    if version == expected:
        return ResolvedWheel(path=path, source=source, version=version)
    if not allow_version_mismatch:
        raise WheelUnavailable(
            f"{path.name} ({source}) is {SAORSA_DB_PACKAGE} {version}, but this "
            f"saorsa-deploy is {expected}. A release publishes both wheels at "
            f"the same version, and the VM's wheel has to match the "
            f"distribution writing the service script, or the script's first "
            f"operation raises in the repository call. Pass "
            f"--saorsa-db-wheel with the {expected} wheel, or "
            f"--allow-wheel-version-mismatch if the mismatch is deliberate."
        )
    return ResolvedWheel(
        path=path,
        source=source,
        version=version,
        warning=(
            f"installing {SAORSA_DB_PACKAGE} {version} onto the VM while this "
            f"saorsa-deploy is {expected} — the two are published together and "
            f"are meant to match. --allow-wheel-version-mismatch was passed, "
            f"so this is going ahead: if the script calls a repository "
            f"argument this wheel does not accept, the service's first "
            f"operation will raise while holding a pool claim."
        ),
    )


def _wheelhouse_candidate(directory: Path, version: str) -> tuple[Path | None, str]:
    """``(wheel, note)``: the newest wheel of ``version`` in ``directory``.

    Matching the version matters rather than taking the newest file: the
    operator box's wheelhouse accumulates every version any run has ever
    fetched, so "newest" is whichever release was installed last and has
    nothing to do with the tool that is running.
    """
    if not directory.is_dir():
        return None, f"wheelhouse {directory}: no such directory"
    matches = sorted(
        directory.glob(f"{SAORSA_DB_DIST}-{version}-*.whl"),
        key=lambda path: path.stat().st_mtime,
    )
    if matches:
        return matches[-1], ""
    held = sorted(
        {
            name.split("-")[1]
            for name in (path.name for path in directory.glob(f"{SAORSA_DB_DIST}-*.whl"))
            if len(name.split("-")) > 1
        }
    )
    return None, (
        f"wheelhouse {directory}: holds no {SAORSA_DB_DIST}-{version} wheel"
        + (f" (it holds {', '.join(held)})" if held else " (it holds none)")
        + f". `gh release download v{version} -p '*.whl' -D {directory}` puts "
        "one there, or set "
        f"${WHEELHOUSE_ENV_VAR} to a directory that already has it"
    )


def _download_release_wheel(version: str) -> Path:
    """Fetch the release's `saorsa-db` wheel into a fresh temporary directory."""
    url = release_wheel_url(version)
    dest = Path(tempfile.mkdtemp(prefix=f"{SAORSA_DB_DIST}-release-")) / (
        f"{SAORSA_DB_DIST}-{version}-py3-none-any.whl"
    )
    response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, stream=True)
    response.raise_for_status()
    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    return dest


def resolve_saorsa_db_wheel(
    explicit: str | Path | None = None,
    allow_version_mismatch: bool = False,
    repo_root: Path | None = None,
) -> ResolvedWheel:
    """The `saorsa-db` wheel to install, from the first source that has one.

    In order: an explicit path (``--saorsa-db-wheel`` or
    ``$SAORSA_DB_WHEEL``), a workspace build when there is a workspace, the
    local wheelhouse, then this repository's GitHub release. Raises
    `WheelUnavailable` naming every source it tried and what would make each
    one work -- never advice to run from a checkout, which is the one thing a
    production maintenance procedure may not do.
    """
    expected = running_version()
    notes: list[str] = []

    # 1. An explicit path. Named, so it is never fallen through: an operator
    #    who gave a file meant that file, and silently installing a different
    #    one is how the wrong wheel reaches a production VM.
    given = explicit if explicit else os.environ.get(WHEEL_PATH_ENV_VAR)
    if given:
        source = (
            "--saorsa-db-wheel" if explicit else f"${WHEEL_PATH_ENV_VAR}"
        )  # the flag wins over the variable
        path = Path(str(given)).expanduser()
        if not path.is_file():
            raise WheelUnavailable(f"{source} {path}: no such file")
        return _checked(path, source, expected, allow_version_mismatch)
    notes.append(
        f"--saorsa-db-wheel / ${WHEEL_PATH_ENV_VAR}: not given. Either names a "
        f"{SAORSA_DB_DIST} wheel on this machine"
    )

    # 2. The workspace, unchanged: from a checkout this builds the working
    #    tree's own code, which is what the development flow expects and the
    #    one source deliberately exempt from the version check.
    root = default_repo_root() if repo_root is None else repo_root
    if workspace_has_package(SAORSA_DB_PACKAGE, repo_root):
        built = build_saorsa_db_wheel(repo_root)
        _, version = wheel_distribution(built)
        return ResolvedWheel(
            path=built,
            source=f"built from the workspace at {root}",
            version=version,
            warning=(
                f"this is a working-tree build of {SAORSA_DB_PACKAGE} {version}, "
                "not a released wheel, so the version check does not apply to "
                "it. The VM will run exactly the code in this checkout."
            ),
        )
    # Deliberately not phrased as advice. A released install has no workspace
    # anywhere in it, and telling a production maintenance procedure to go and
    # find one is the one answer it is not allowed to act on.
    notes.append(
        f"workspace build: there is no db/pyproject.toml under {root}, so this "
        "install has no workspace to build from"
    )

    # 3. The wheelhouse the production procedures already populate.
    directory = wheelhouse_dir()
    candidate, note = _wheelhouse_candidate(directory, expected)
    if candidate is not None:
        return _checked(candidate, f"wheelhouse {directory}", expected, allow_version_mismatch)
    notes.append(note)

    # 4. The release itself. Last because it is the only source that needs the
    #    network, and the wheelhouse copy of the same artefact is identical.
    url = release_wheel_url(expected)
    try:
        downloaded = _download_release_wheel(expected)
    except requests.RequestException as exc:
        notes.append(f"GitHub release: {url} could not be fetched ({exc})")
    else:
        return _checked(downloaded, f"GitHub release v{expected}", expected, allow_version_mismatch)

    raise WheelUnavailable(
        f"no source has a {SAORSA_DB_PACKAGE} {expected} wheel. Tried:\n"
        + "\n".join(f"  - {note}" for note in notes)
    )


def needs_registry_client(kind: str) -> bool:
    return kind in KINDS_WITH_REGISTRY_CLIENT


def missing_registry_columns(repo) -> list[str]:
    """Which of ``REQUIRED_REGISTRY_COLUMNS`` the connected registry lacks.

    A table missing entirely is reported the same way a column is -- the
    distinction does not change what the operator has to run.
    """
    inspector = sa_inspect(repo.session.get_bind())
    tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table, column in REQUIRED_REGISTRY_COLUMNS:
        if table not in tables:
            missing.append(f"{table} (table absent)")
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            missing.append(f"{table}.{column}")
    return missing


def schema_problem(repo, kind: str) -> str | None:
    """A ready-made refusal for a registry the new script would die against."""
    if not needs_registry_client(kind):
        return None
    missing = missing_registry_columns(repo)
    if not missing:
        return None
    return (
        f"the registry this is pointed at is missing {', '.join(missing)}. A "
        f"'{kind}' VM writes Postgres itself, so the new script's first upload "
        "would fail in the INSERT -- while holding a pool claim. Run "
        "`cd db && uv run alembic upgrade head` against this registry first."
    )


def wheel_for_kinds(
    kinds,
    explicit_wheel: str | Path | None = None,
    allow_version_mismatch: bool = False,
    repo_root: Path | None = None,
) -> tuple[ResolvedWheel | None, str | None]:
    """``(wheel, problem)``: the wheel to install, resolved once, or why not.

    None for both when no named kind runs under the venv -- and nothing is read,
    built or fetched in that case, since the download kinds need no wheel at
    all.

    A problem rather than an exception whatever went wrong, including a network
    failure on the release path: both callers refuse on the string and neither
    expects to catch anything.
    """
    if not any(needs_registry_client(kind) for kind in kinds):
        return None, None
    try:
        return (
            resolve_saorsa_db_wheel(
                explicit=explicit_wheel,
                allow_version_mismatch=allow_version_mismatch,
                repo_root=repo_root,
            ),
            None,
        )
    except (WheelUnavailable, FileNotFoundError, ValueError, OSError) as exc:
        return None, (
            f"{exc}\nA 'prod-continuous-uploader' VM runs its service under "
            "/opt/ant-registry-client, whose `saorsa-db` wheel has to be "
            "replaced alongside the script and has to be the same version as "
            "this saorsa-deploy."
        )


def add_install_ops(state: State, wheel: Path, console=None) -> None:
    """Fold the wheel install into a pass, BEFORE the script is written.

    Order is the point: the script is what calls the new repository argument,
    so a restart that beat the wheel would raise on the service's first
    operation.
    """
    SaorsaDbInstaller(wheel_path=wheel, console=console).add_ops(state)
