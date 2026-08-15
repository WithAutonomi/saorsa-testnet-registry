"""Install `saorsa-db` onto a client VM so it can reach the registry.

Every other VM-side runtime script in this package is deliberately stdlib-only,
fed by a systemd ``EnvironmentFile``. The daily production write test (V2-837)
is the exception: its uploader claims files from the production file pool as
each upload happens, and its verifiers read the address list back, so both need
a real database connection.

**Why install the package rather than hand-roll the SQL.** `claim_pool_files`
carries the parts that are easy to get subtly wrong — resolving a duplicate
group to its canonical id, arbitrating the race on the partial unique index
with a savepoint per insert, and pre-checking the whole request so a shortfall
in one size class cannot leave another class already spent. A second
implementation on the VM would have to track every change to that logic, and
the failure mode when it drifts is double-spending a file that can never be
un-uploaded.

**Why a venv.** Ubuntu 24.04 marks its system Python externally-managed
(PEP 668), so `pip install` into it fails without `--break-system-packages`.
A venv sidesteps that argument entirely and keeps the VM's own tooling
untouched. Services then run under `VENV_PYTHON` rather than `/usr/bin/python3`.

**Why the wheel travels over SSH.** It is tens of kilobytes and pyinfra is
already connected. Publishing it to S3 would mean a bucket, a URL and a
lifecycle to keep in step with the source tree, all to move a file to a host we
are already logged into.
"""

import subprocess
import tempfile
from pathlib import Path

from pyinfra.api import State
from pyinfra.api.operation import add_op
from pyinfra.operations import files, server

#: Where the venv lives on the VM. Named for what it is used for, not for the
#: package, because the services that consume it are the point.
VENV_DIR = "/opt/ant-registry-client"
VENV_PYTHON = f"{VENV_DIR}/bin/python"
VENV_PIP = f"{VENV_DIR}/bin/pip"
REMOTE_WHEEL_DIR = "/root/saorsa-db-wheel"

#: Prints the running interpreter's "major.minor", for the versioned
#: `pythonX.Y-venv` package name. Kept as a constant so the quoting inside the
#: install command stays readable.
_PY_VERSION_SHELL = (
    "python3 -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])'"
)

#: Runtime dependencies actually needed on a VM. `alembic` is a declared
#: dependency of the package but only for generating and applying migrations,
#: which never happens from a client VM — so it is installed with `--no-deps`
#: and these are named explicitly instead.
RUNTIME_DEPS = (
    "sqlalchemy>=2.0",
    "psycopg2-binary>=2.9",
    "python-dotenv>=1.1",
)


def build_saorsa_db_wheel(repo_root: Path | None = None) -> Path:
    """Build the `saorsa-db` wheel and return its path.

    Built fresh at provision time rather than pinned to a published version so
    the VM runs exactly the claim logic in this working tree — a VM holding a
    stale copy of the pool semantics is the failure this whole module exists to
    avoid.
    """
    if repo_root is None:
        # provisioning/ -> saorsa_deploy/ -> deploy/ -> repo root
        repo_root = Path(__file__).resolve().parents[3]
    if not (repo_root / "db" / "pyproject.toml").is_file():
        raise FileNotFoundError(
            "cannot build the saorsa-db wheel: no db/pyproject.toml under "
            f"{repo_root}"
        )

    out_dir = Path(tempfile.mkdtemp(prefix="saorsa-db-wheel-"))
    # Built from the workspace root with --package rather than from db/: uv
    # resolves the workspace from the invocation directory, and building in
    # place would drop the artifact into the source tree.
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--package",
            "saorsa-db",
            "--out-dir",
            str(out_dir),
        ],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(out_dir.glob("saorsa_db-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"uv build produced no wheel in {out_dir}")
    # Newest by name; a clean out_dir should only ever hold one.
    return wheels[-1]


def build_install_commands(wheel_name: str) -> list[str]:
    """Shell commands that put the wheel in place. Pure, so it is testable."""
    remote_wheel = f"{REMOTE_WHEEL_DIR}/{wheel_name}"
    deps = " ".join(f'"{dep}"' for dep in RUNTIME_DEPS)
    return [
        # `apt-get update` first, and it is not optional: a stock Hetzner
        # Ubuntu 24.04 image ships with no package lists, so installing
        # python3-venv fails with "has no installation candidate" even though
        # universe is enabled and the package plainly exists. Measured on the
        # real VM — the lists were the whole problem.
        #
        # The versioned name is a fallback because some images only carry
        # python3.N-venv, and which one resolves varies by provider image.
        f"test -x {VENV_PYTHON} || ("
        f"DEBIAN_FRONTEND=noninteractive apt-get update -q && "
        f"(DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv || "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q "
        f"\"python$({_PY_VERSION_SHELL})-venv\") && "
        f"python3 -m venv {VENV_DIR})",
        # Dependencies are requested every time but pip no-ops once they are
        # satisfied, so this stays cheap on re-provision.
        f"{VENV_PIP} install --quiet --upgrade {deps}",
        # The package itself is force-reinstalled so a re-provision always
        # lands the current build. `--no-deps` keeps that from dragging the
        # dependency set through a full reinstall each time.
        f"{VENV_PIP} install --quiet --force-reinstall --no-deps {remote_wheel}",
        # Fail loudly here rather than at 3am inside the service.
        f'{VENV_PYTHON} -c "import saorsa_db.repositories" && '
        f"echo 'SAORSA_DB:INSTALLED'",
    ]


class SaorsaDbInstaller:
    """Op provider that installs `saorsa-db` into a venv on each host.

    Exposes the same ``add_ops(state, install=...)`` seam the telegraf
    providers use, so it can be folded into an existing provisioning pass
    rather than costing a second walk over the hosts.
    """

    def __init__(self, wheel_path: Path | None = None, console=None):
        self.wheel_path = wheel_path
        self.console = console

    def _resolve_wheel(self) -> Path:
        if self.wheel_path is None:
            self.wheel_path = build_saorsa_db_wheel()
            if self.console:
                self.console.print(
                    f"  Built saorsa-db wheel: {self.wheel_path.name}"
                )
        return self.wheel_path

    def add_ops(self, state: State, install: bool = True) -> None:
        wheel = self._resolve_wheel()
        add_op(
            state,
            server.shell,
            name="Create saorsa-db wheel directory",
            commands=[f"mkdir -p {REMOTE_WHEEL_DIR}"],
        )
        add_op(
            state,
            files.put,
            name="Upload saorsa-db wheel",
            src=str(wheel),
            dest=f"{REMOTE_WHEEL_DIR}/{wheel.name}",
            mode="644",
            add_deploy_dir=False,
        )
        add_op(
            state,
            server.shell,
            name="Install saorsa-db into the registry client venv",
            commands=build_install_commands(wheel.name),
        )
