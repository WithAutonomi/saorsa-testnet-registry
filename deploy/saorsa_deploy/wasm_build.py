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

#: `npm ci` rather than `npm install`: the lockfile is committed, and `install`
#: is free to resolve a newer minor of anything declared with a caret -- so two
#: builds of the same ant-client ref could ship different `node-datachannel`
#: (the WebRTC implementation the whole browser path runs on) with nothing in
#: the artifact recording which. `ci` installs exactly the lockfile.
NPM_INSTALL_ARGV = ("npm", "ci", "--no-audit", "--no-fund")
NPM_BUILD_ARGV = ("npm", "run", "build")

#: Build provenance, written into the tarball root and uploaded beside it.
BUILD_INFO_NAME = "build-info.json"


def build_info_command(
    *,
    checkout_dir: str,
    client_src_dir: str,
    dest_path: str,
    ref: str,
    repo_owner: str,
) -> str:
    """Shell that writes the build's provenance to ``dest_path``.

    The bundle recorded nothing about what went into it: `--branch-name` may be
    a moving branch, so "the bundle built from `web-support`" does not identify
    any particular code, and the resolved `node-datachannel` was invisible
    entirely. Both are read off the build VM at build time -- the commit from
    the checkout, the dependency version from what npm actually installed --
    because neither can be reconstructed afterwards.
    """
    return (
        f"COMMIT=$(git -C {checkout_dir} rev-parse HEAD) && "
        f"cd {client_src_dir} && "
        'ND=$(node -e "console.log('
        "require('./node_modules/node-datachannel/package.json').version)\") && "
        "BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ) && "
        f"cat > {dest_path} << BUILD_INFO_EOF\n"
        "{\n"
        '  "commit": "$COMMIT",\n'
        f'  "ref": "{ref}",\n'
        f'  "repo_owner": "{repo_owner}",\n'
        '  "node_datachannel_version": "$ND",\n'
        f'  "wasm_pack_version": "{WASM_PACK_VERSION}",\n'
        f'  "node_major": "{NODE_MAJOR}",\n'
        '  "built_at": "$BUILT_AT"\n'
        "}\n"
        "BUILD_INFO_EOF"
    )


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
