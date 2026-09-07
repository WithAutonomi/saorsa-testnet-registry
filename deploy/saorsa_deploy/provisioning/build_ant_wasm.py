"""Builds the WASM client bundle on a remote build VM and uploads it to S3.

Mirrors :class:`~saorsa_deploy.provisioning.build_ant.AntBuilder`, but the
artifact is a tarball rather than a single binary, and the toolchain needs
`wasm-pack` and Node in addition to Rust.

A separate build path is unavoidable rather than a preference:

* `ant-core` declares `default = ["native"]` and a `browser-wasm` feature that is
  mutually exclusive with it -- a wasm32 build without `browser-wasm` stops at an
  explicit ``compile_error!``.
* The artifact comes out of `wasm-pack` (which runs `wasm-bindgen` and
  `wasm-opt` after cargo), not `cargo build --bin`.

So `build ant` cannot produce this no matter what flags it is given.
"""

import os

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import files, server
from rich.console import Console

from saorsa_deploy.binary_source import (
    ANT_WASM_ARCHIVE_NAME,
    BUILDS_BUCKET,
    BUILDS_KEY_PREFIX,
    get_ant_wasm_build_url,
)
from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.authorized_keys import add_operator_authorized_keys_op
from saorsa_deploy.resources import get_resources_dir

#: Pinned to match ant-client's own CI (`.github/workflows/ci.yml`, job
#: `browser-wasm`). wasm-pack drives wasm-bindgen, whose generated glue must
#: match the `wasm-bindgen` crate version the build resolves -- a mismatch fails
#: at runtime inside the browser/Node loader rather than at build time.
WASM_PACK_VERSION = "0.15.0"

#: ant-client's CI builds and tests the WASM package on Node 22.
NODE_MAJOR = "22"

BUILD_DIR = "/root/ant-client"
CLIENT_SRC_DIR = "/root/wasm-client"
STAGE_DIR = "/root/ant-wasm-stage"


class AntWasmBuilder:
    """Builds ant-core to WASM plus the TypeScript client, and uploads a tarball."""

    def __init__(
        self,
        name: str,
        ip: str,
        ssh_key_path: str,
        repo_owner: str,
        branch_name: str,
        console: Console | None = None,
    ):
        self.name = name
        self.ip = ip
        self.ssh_key_path = ssh_key_path
        self.repo_owner = repo_owner
        self.branch_name = branch_name
        self.console = console or Console()
        self.s3_key = (
            f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/{ANT_WASM_ARCHIVE_NAME}"
        )

    def execute(self) -> str:
        """Build the bundle and upload it. Returns the S3 URL."""
        aws_access_key = os.environ.get("ANT_BUILD_AWS_ACCESS_KEY_ID")
        aws_secret_key = os.environ.get("ANT_BUILD_AWS_SECRET_ACCESS_KEY")
        if not aws_access_key or not aws_secret_key:
            raise RuntimeError(
                "ANT_BUILD_AWS_ACCESS_KEY_ID and ANT_BUILD_AWS_SECRET_ACCESS_KEY must be set"
            )

        self.console.print(f"Connecting to {self.ip} as root...")
        inventory = Inventory(
            ([(self.ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})], {}),
        )
        state = State(inventory=inventory, config=Config())
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Wait for cloud-init to finish",
                commands=["cloud-init status --wait || true"],
            )
            add_operator_authorized_keys_op(state)
            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )
            add_op(
                state,
                server.shell,
                name="Install build dependencies",
                commands=[
                    "apt-get update -qq && apt-get install -y -qq "
                    "curl build-essential pkg-config libssl-dev git unzip ca-certificates"
                ],
            )
            add_op(
                state,
                server.shell,
                name="Install AWS CLI v2",
                commands=[
                    'curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" '
                    "-o /tmp/awscliv2.zip && "
                    "unzip -q /tmp/awscliv2.zip -d /tmp && "
                    "/tmp/aws/install --update && "
                    "rm -rf /tmp/awscliv2.zip /tmp/aws"
                ],
            )
            add_op(
                state,
                server.shell,
                name=f"Install Node.js {NODE_MAJOR}",
                commands=[
                    f"curl -fsSL https://deb.nodesource.com/setup_{NODE_MAJOR}.x | bash - && "
                    "apt-get install -y -qq nodejs"
                ],
            )
            add_op(
                state,
                server.shell,
                name="Install Rust toolchain",
                commands=[
                    "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
                ],
            )
            add_op(
                state,
                server.shell,
                name="Add wasm32 target",
                commands=["/root/.cargo/bin/rustup target add wasm32-unknown-unknown"],
            )
            add_op(
                state,
                server.shell,
                name=f"Install wasm-pack {WASM_PACK_VERSION}",
                commands=[
                    f"/root/.cargo/bin/cargo install wasm-pack "
                    f"--version {WASM_PACK_VERSION} --locked"
                ],
            )
            add_op(
                state,
                server.shell,
                name=f"Clone {self.repo_owner}/ant-client ({self.branch_name})",
                commands=[
                    # Remove any leftover checkout first: a reused build droplet
                    # already has /root/ant-client and a bare clone aborts on it.
                    f"rm -rf {BUILD_DIR} && "
                    f"git clone --branch {self.branch_name} --depth 1 "
                    f"https://github.com/{self.repo_owner}/ant-client.git {BUILD_DIR}"
                ],
            )
            add_op(
                state,
                server.shell,
                name="Build ant-core to WASM",
                commands=[
                    f"cd {BUILD_DIR} && "
                    "PATH=/root/.cargo/bin:$PATH "
                    # `--target web` (not nodejs/bundler): it is what ant-client's
                    # own CI builds and tests under Node, and what the client's
                    # explicit init call expects. `test-utils` is deliberately
                    # NOT enabled -- it links a deterministic mock node.
                    "wasm-pack build --target web ant-core "
                    "--no-default-features --features browser-wasm"
                ],
            )

            # Ship the TypeScript client from this repo rather than the checkout:
            # it lives here, versioned with the tool that provisions it.
            self._upload_client_sources(state)

            add_op(
                state,
                server.shell,
                name="Build the TypeScript client",
                commands=[
                    f"cd {CLIENT_SRC_DIR} && npm install --no-audit --no-fund && "
                    "npm run build"
                ],
            )
            add_op(
                state,
                server.shell,
                name="Assemble the bundle",
                commands=[
                    # node_modules is bundled rather than installed on each client
                    # VM: node-datachannel ships a prebuilt native addon, so a
                    # per-VM `npm install` would be a network fetch and a
                    # toolchain dependency on every client at provision time.
                    f"rm -rf {STAGE_DIR} && mkdir -p {STAGE_DIR} && "
                    f"cp -r {BUILD_DIR}/ant-core/pkg {STAGE_DIR}/pkg && "
                    f"cp -r {CLIENT_SRC_DIR}/dist {STAGE_DIR}/dist && "
                    f"cp -r {CLIENT_SRC_DIR}/node_modules {STAGE_DIR}/node_modules && "
                    f"cp {CLIENT_SRC_DIR}/package.json {STAGE_DIR}/package.json && "
                    f"cd {STAGE_DIR} && tar czf /root/{ANT_WASM_ARCHIVE_NAME} ."
                ],
            )
            add_op(
                state,
                server.shell,
                name="Verify the bundle loads",
                commands=[
                    # A wasm-bindgen glue/crate mismatch only shows up at load
                    # time, so catch it here rather than on a client VM after the
                    # infrastructure is already up and billing.
                    f"cd {STAGE_DIR} && node -e \""
                    "const {readFileSync}=require('node:fs');"
                    "import('./pkg/ant_core.js').then(async m=>{"
                    "await m.default({module_or_path:readFileSync('./pkg/ant_core_bg.wasm')});"
                    "if(typeof m.BrowserNetworkClient!=='function')"
                    "throw new Error('BrowserNetworkClient missing');"
                    "console.log('wasm bundle OK');})"
                    ".catch(e=>{console.error(e);process.exit(1);})\""
                ],
            )
            add_op(
                state,
                server.shell,
                name="Upload bundle to S3",
                commands=[
                    f"AWS_ACCESS_KEY_ID={aws_access_key} "
                    f"AWS_SECRET_ACCESS_KEY={aws_secret_key} "
                    f"aws s3 cp /root/{ANT_WASM_ARCHIVE_NAME} "
                    f"s3://{BUILDS_BUCKET}/{self.s3_key}"
                ],
            )

            self.console.print("Running build operations...")
            try:
                run_ops(state)
            except PyinfraError:
                pass
        finally:
            disconnect_all(state)

        if state.failed_hosts:
            raise RuntimeError("WASM client build failed on build VM")

        return get_ant_wasm_build_url(self.name, self.repo_owner, self.branch_name)

    def _upload_client_sources(self, state) -> None:
        """Copy the TypeScript client from this package onto the build VM."""
        src = get_resources_dir() / "wasm-client"
        add_op(
            state,
            server.shell,
            name="Prepare client source directory",
            commands=[f"rm -rf {CLIENT_SRC_DIR} && mkdir -p {CLIENT_SRC_DIR}/src"],
        )
        for rel in ("package.json", "tsconfig.json"):
            add_op(
                state,
                files.put,
                name=f"Upload {rel}",
                src=str(src / rel),
                dest=f"{CLIENT_SRC_DIR}/{rel}",
            )
        for path in sorted((src / "src").glob("*.ts")):
            add_op(
                state,
                files.put,
                name=f"Upload src/{path.name}",
                src=str(path),
                dest=f"{CLIENT_SRC_DIR}/src/{path.name}",
            )
