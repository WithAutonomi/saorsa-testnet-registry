"""Shell builders for the browser-downloader client VM (real headless Chromium).

The role runs a Node.js harness that drives Playwright, which drives Chromium,
which loads the deployment's own `ant-core` WASM bundle from a loopback HTTP
server the harness runs itself. Four pieces have to be on the VM:

* Node 22 — the same major the bundle is built and tested against, installed by
  the shared :func:`~saorsa_deploy.provisioning.wasm_client.build_node_install_command`.
* Playwright at a pinned version plus its Chromium and that Chromium's system
  libraries (``--with-deps``).
* The harness itself, committed under ``resources/browser-client/`` and copied
  on, so it is versioned with the tool that provisions it.
* The WASM bundle, the same artefact the WASM roles get, unpacked beside it.

Everything here returns a shell string and touches nothing; the caller wraps
them in pyinfra ops and the tests assert on the rendered text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

BROWSER_CLIENT_DIR = "/opt/ant-browser-downloader"
BROWSER_CLIENT_DATA_DIR = "/var/lib/ant-browser-downloader"
BROWSER_CLIENT_TRACE_DIR = f"{BROWSER_CLIENT_DATA_DIR}/trace"
BROWSER_CLIENT_ENV_PATH = "/etc/ant-browser-downloader.env"
#: The bundle goes under the harness directory rather than the WASM roles'
#: ``/opt/ant-wasm-client``: a browser VM runs neither the Node harness nor the
#: `ant-wasm` CLI that live there, and the loopback server serves this one tree.
BROWSER_BUNDLE_DIR = f"{BROWSER_CLIENT_DIR}/bundle"
BROWSER_CLIENT_ENTRY = f"{BROWSER_CLIENT_DIR}/harness.mjs"

#: Where the harness tees its own streams. Same directory and the same 1 GiB cap
#: as the native roles, so one telegraf tail config shape covers every role.
BROWSER_FORWARD_LOG_DIR = "/var/log/ant-client-forward"
BROWSER_STDOUT_LOG = f"{BROWSER_FORWARD_LOG_DIR}/browser-downloader.stdout.log"
BROWSER_STDERR_LOG = f"{BROWSER_FORWARD_LOG_DIR}/browser-downloader.stderr.log"

#: Pinned, like wasm-pack is: Playwright's browser build is tied to the npm
#: package version, and "whatever npx resolves today" would make two VMs in one
#: cohort run different Chromium builds -- which is precisely the variable the
#: cohort exists to hold constant.
PLAYWRIGHT_VERSION = "1.56.0"

#: Loopback port the harness serves the bundle on. A page is needed for WASM +
#: WebRTC in Chromium (a `file://` origin has no secure context for the
#: datachannel APIs), and serving it locally means the page itself needs no
#: external network.
BROWSER_BUNDLE_PORT = 8723


def build_playwright_install_command(
    version: str = PLAYWRIGHT_VERSION,
    *,
    attempts: int = 3,
    retry_delay_secs: int = 15,
) -> str:
    """Install Playwright and its Chromium, with the system libraries it needs.

    ``--with-deps`` is what pulls the ~100 shared libraries a headless Chromium
    links; without them the browser launches and immediately exits with a
    missing-library error that reads like a harness bug. Retried, because this
    is the one provisioning step that fetches ~200 MB from two different hosts
    (npm and Playwright's CDN), and it ends in a hard check that the browser is
    actually there — a partial install must fail the op rather than leave every
    round reporting a launch failure.
    """
    return (
        f"mkdir -p {BROWSER_CLIENT_DIR} && cd {BROWSER_CLIENT_DIR} && "
        "ATTEMPT=0; OK=0; "
        f"while [ $ATTEMPT -lt {attempts} ]; do "
        "ATTEMPT=$((ATTEMPT + 1)); "
        f"if npm install --no-audit --no-fund playwright@{version} "
        f"&& PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright "
        f"./node_modules/.bin/playwright install --with-deps chromium; "
        "then OK=1; break; fi; "
        f"if [ $ATTEMPT -lt {attempts} ]; then sleep {retry_delay_secs}; fi; "
        "done; "
        'if [ "$OK" != 1 ]; then '
        f"echo 'PLAYWRIGHT:FAILED after {attempts} attempts'; exit 1; fi; "
        # The real check, not `playwright install --dry-run` (which prints what
        # it would do and exits 0 whatever is on disk): ask the installed
        # package where its Chromium is and test that the file is executable.
        # A partial install has to fail this op, because the alternative is a VM
        # that bills while every round reports a launch failure.
        "EXE=$(PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright node -e "
        "\"console.log(require('playwright').chromium.executablePath())\" "
        "2>/dev/null); "
        'if [ -z "$EXE" ] || [ ! -x "$EXE" ]; then '
        "echo \"PLAYWRIGHT:NO_CHROMIUM at '${EXE:-unknown}'\"; exit 1; fi; "
        f'echo "PLAYWRIGHT:INSTALLED {version} ($EXE)"'
    )


def build_bundle_install_command(bundle_url: str) -> str:
    """Fetch and unpack the WASM bundle into the harness's served directory.

    Always re-fetched rather than presence-gated: the bundle is namespaced by
    deployment name and `build ant-wasm` overwrites it, so a re-provision after
    a rebuild has to pick up the new one.
    """
    return (
        f"rm -rf {BROWSER_BUNDLE_DIR} && "
        f"mkdir -p {BROWSER_BUNDLE_DIR} {BROWSER_CLIENT_TRACE_DIR} "
        f"{BROWSER_FORWARD_LOG_DIR} && "
        f"wget -q '{bundle_url}' -O /tmp/ant-browser-bundle.tar.gz && "
        f"tar xzf /tmp/ant-browser-bundle.tar.gz -C {BROWSER_BUNDLE_DIR} && "
        f"rm -f /tmp/ant-browser-bundle.tar.gz && "
        f"test -f {BROWSER_BUNDLE_DIR}/pkg/ant_core.js && "
        f"test -f {BROWSER_BUNDLE_DIR}/pkg/ant_core_bg.wasm"
    )


def build_trace_hook_check_command() -> str:
    """Fail the pass unless the installed bundle exports ``setBrowserTrace``.

    The precondition in ``clients provision`` checks what the deployment
    RECORDED it built with; this checks the bytes that actually landed. Both are
    worth having: the recorded value catches the mistake before a VM exists, and
    this catches a bundle that was rebuilt without the feature under the same
    branch name. Without the hook every round would run and report nothing in
    `stalls`, `refused_dials` or `first_reads` — zeros that read as a clean
    network rather than as a missing instrument.
    """
    return (
        f"grep -q 'setBrowserTrace' {BROWSER_BUNDLE_DIR}/pkg/ant_core.js || "
        "(echo 'BUNDLE:NO_TRACE_HOOK - the bundle was not built with the "
        "test-utils feature, so setBrowserTrace does not exist and every "
        "per-dial trace would be empty'; exit 1)"
    )


def render_env_file(env: Mapping[str, object], *, quoted: Iterable[str] = ()) -> str:
    """Render an env dict as an ``EnvironmentFile`` body, in insertion order."""
    quote = set(quoted)
    lines = [f'{key}="{value}"' if key in quote else f"{key}={value}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def build_systemd_unit() -> str:
    """systemd unit for the long-running harness.

    ``Restart=always``, not ``on-failure``: the harness is meant to run for the
    life of the deployment, every round is independent, and a round it missed
    because it was not running is a hole in the measurement that nothing can
    fill in afterwards.
    """
    return f"""\
[Unit]
Description=Ant Browser Downloader (headless Chromium hot-read cohort)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={BROWSER_CLIENT_ENV_PATH}
Environment=NODE_ENV=production
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
WorkingDirectory={BROWSER_CLIENT_DIR}
ExecStart=/usr/bin/node {BROWSER_CLIENT_ENTRY}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
