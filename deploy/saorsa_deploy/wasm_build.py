"""The commands that build the WASM client bundle, in one place.

Two callers run the same build: :mod:`saorsa_deploy.provisioning.build_ant_wasm`
on a remote build VM, and ``build ant-wasm-check`` locally in a temp directory.
The point of the local check is to fail on the operator's machine, in a minute,
instead of after a build VM has been created and ~10 minutes spent on it — and
it only has that value if the two run *the same build*. Two copies of
``--no-default-features --features browser-wasm`` drift the moment one is
edited, and the drift is invisible until a staging run fails at the last step
before the clients come up.

So the argv lives here and both import it. The remote side wraps these in shell
strings for pyinfra; the local side hands them to ``subprocess``.
"""

from __future__ import annotations

import shlex

#: Pinned to match ant-client's own CI (`.github/workflows/ci.yml`, job
#: `browser-wasm`). wasm-pack drives wasm-bindgen, whose generated glue must
#: match the `wasm-bindgen` crate version the build resolves -- a mismatch fails
#: at runtime inside the browser/Node loader rather than at build time.
WASM_PACK_VERSION = "0.15.0"

#: ant-client's CI builds and tests the WASM package on Node 22.
NODE_MAJOR = "22"

ANT_CLIENT_REPO = "ant-client"

#: The wasm-pack invocation itself.
#:
#: `--target web` (not nodejs/bundler): it is what ant-client's own CI builds
#: and tests under Node, and what the client's explicit init call expects.
#: `ant-core` declares `default = ["native"]` and a `browser-wasm` feature
#: mutually exclusive with it, so a wasm32 build without BOTH flags stops at an
#: explicit `compile_error!`. `test-utils` is deliberately NOT enabled -- it
#: links a deterministic mock node.
WASM_PACK_ARGV = (
    "wasm-pack",
    "build",
    "--target",
    "web",
    "ant-core",
    "--no-default-features",
    "--features",
    "browser-wasm",
)

#: Where wasm-pack leaves the package, relative to the ant-client checkout.
WASM_PKG_SUBDIR = "ant-core/pkg"

NPM_INSTALL_ARGV = ("npm", "install", "--no-audit", "--no-fund")
NPM_BUILD_ARGV = ("npm", "run", "build")


def clone_argv(repo_owner: str, ref: str, dest: str) -> list[str]:
    """Shallow-clone ant-client at ``ref``, which may be a branch OR a tag.

    `git clone --branch` accepts either, and nothing checks out anything
    afterwards -- which is the whole reason the staging profile can pin the
    WASM tier to an ant-client release tag.
    """
    return [
        "git",
        "clone",
        "--branch",
        ref,
        "--depth",
        "1",
        f"https://github.com/{repo_owner}/{ANT_CLIENT_REPO}.git",
        dest,
    ]


def bundle_load_check_source(pkg_dir: str) -> str:
    """A Node program asserting the built package loads and exports its entry.

    A wasm-bindgen glue/crate mismatch, and a rename of the browser entry point
    upstream, both show up only at LOAD time. Catching them here means the
    failure lands in a build step rather than on a client VM after the
    infrastructure is up and billing.
    """
    js = f"{pkg_dir}/ant_core.js"
    wasm = f"{pkg_dir}/ant_core_bg.wasm"
    return (
        "const {readFileSync}=require('node:fs');"
        f"import('{js}').then(async m=>{{"
        f"await m.default({{module_or_path:readFileSync('{wasm}')}});"
        "if(typeof m.BrowserNetworkClient!=='function')"
        "throw new Error('BrowserNetworkClient missing');"
        "console.log('wasm bundle OK');})"
        ".catch(e=>{console.error(e);process.exit(1);})"
    )


def shell(argv) -> str:
    """Render an argv as a shell string for the remote (pyinfra) path."""
    return shlex.join(argv)
