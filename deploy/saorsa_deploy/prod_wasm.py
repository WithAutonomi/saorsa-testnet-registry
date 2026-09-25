"""The shared foundation every production WASM (browser-path) client kind needs.

Three things, all of which have to happen before any infrastructure is created:

* resolving the production bootstrap WebRTC multiaddrs a browser client needs to
  reach the network at all,
* proving those addresses actually answer, and
* refusing a VM too small to hold a browser-path transfer.

Kept out of the deploy commands because the download kinds, the one-shot
uploaders (V2-1334) and the continuous uploader (V2-1335) all need the identical
checks, and a second copy of "is this bootstrap alive" would drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from saorsa_deploy import webrtc_probe
from saorsa_deploy.resources import get_resources_dir

#: The published production bootstrap list, written by
#: `prod bootstrap webrtc-addresses` and committed. It is the default source
#: because it is the only list there is: nothing publishes these addresses at a
#: URL, which is why there is no URL flag to override it with.
PROD_BOOTSTRAP_WEBRTC_FILENAME = "prod_bootstrap_webrtc_multiaddrs.txt"

#: A browser-path download holds ~4x the file in WASM memory (900 MB measured at
#: 3.9 GB RSS on smoke run 587), so the default is four times the native
#: downloader's box rather than the same one.
DEFAULT_WASM_VM_SIZE = "s-4vcpu-8gb"

#: Below this a browser-path transfer of any useful size is an OOM kill, which
#: the service records as a crash — a run that looks like a network fault.
MIN_WASM_VM_RAM_MB = 8192

#: DigitalOcean's slugs encode their memory, so the check is exact for DO and
#: advisory everywhere else. `s-4vcpu-8gb`, `g-2vcpu-8gb`, `m3-2vcpu-16gb`,
#: `so1_5-2vcpu-16gb` all match; `s-2vcpu-4gb` matches and is refused.
_DO_SLUG_RAM = re.compile(r"^[a-z0-9_]+-\d+vcpu-(\d+)(gb|mb)$")


def get_prod_bootstrap_webrtc_path() -> str:
    """Absolute path to the committed production bootstrap multiaddr list."""
    return str(get_resources_dir() / PROD_BOOTSTRAP_WEBRTC_FILENAME)


def read_bootstrap_multiaddrs(path: str) -> list[str]:
    """Read a multiaddr list, skipping blanks and ``#`` comments."""
    with open(path, encoding="utf-8") as handle:
        lines = [raw.strip() for raw in handle]
    return [line for line in lines if line and not line.startswith("#")]


def parse_multiaddr_endpoint(multiaddr: str) -> tuple[str, int] | None:
    """Pull ``(ip, udp_port)`` out of a WebRTC-Direct multiaddr.

    Returns None for anything that does not carry both, so a malformed line is
    reported rather than silently probed against nothing.
    """
    parts = multiaddr.strip("/").split("/")
    ip = None
    port = None
    for i, token in enumerate(parts[:-1]):
        if token == "ip4":
            ip = parts[i + 1]
        elif token == "udp":
            try:
                port = int(parts[i + 1])
            except ValueError:
                return None
    if not ip or port is None:
        return None
    return ip, port


@dataclass(frozen=True)
class BootstrapProbeResult:
    """What the preflight ICE probe found."""

    total: int
    answered: int
    dead: tuple[str, ...]
    #: True when the probe was not run at all (``--skip-bootstrap-probe``).
    skipped: bool = False

    @property
    def all_answered(self) -> bool:
        return self.answered == self.total and self.total > 0


def probe_bootstrap_multiaddrs(
    multiaddrs: list[str],
    *,
    probe: Callable[[str, int], bool] = webrtc_probe.probe,
) -> BootstrapProbeResult:
    """ICE-probe every entry and report how many answered.

    A real ICE connectivity check, not a STUN binding: saorsa-transport's
    admission filter returns ``Decision::Ignore`` on every failure, so an
    unauthenticated probe cannot tell a firewalled port from a healthy one.
    :func:`webrtc_probe.probe` sends the credentials that get answered.
    """
    dead: list[str] = []
    answered = 0
    for multiaddr in multiaddrs:
        endpoint = parse_multiaddr_endpoint(multiaddr)
        if endpoint is None:
            dead.append(multiaddr)
            continue
        ip, port = endpoint
        if probe(ip, port):
            answered += 1
        else:
            dead.append(multiaddr)
    return BootstrapProbeResult(total=len(multiaddrs), answered=answered, dead=tuple(dead))


class WasmBootstrapError(RuntimeError):
    """No configured bootstrap answered, so nothing would be deployable."""


def resolve_bootstrap_multiaddrs(
    path: str | None,
    *,
    skip_probe: bool = False,
    probe: Callable[[str, int], bool] = webrtc_probe.probe,
) -> tuple[list[str], BootstrapProbeResult, str]:
    """Resolve, and prove, the bootstrap list for a prod WASM deployment.

    Returns ``(multiaddrs, probe_result, source_path)``.

    Raises :class:`WasmBootstrapError` when the list is empty or when NOTHING
    answers. Nothing answering means no browser client can reach the network at
    all, so the alternative is a VM that bills from the moment it is created and
    fails every single transfer. A partial answer only warns: ant-core gives a
    failed endpoint a 30-minute cooldown and moves on, so a live subset is a
    working deployment — and the caller names the dead entries so the run's
    results can be read in that light.

    ``skip_probe`` exists because the listener opens 2–15 minutes after a
    bootstrap restarts (V2-1331): a tier that was just upgraded is legitimately
    silent, and that must not be an unoverridable block.
    """
    source = path or get_prod_bootstrap_webrtc_path()
    multiaddrs = read_bootstrap_multiaddrs(source)
    if not multiaddrs:
        raise WasmBootstrapError(
            f"bootstrap multiaddr list '{source}' is empty — a WASM client has "
            "no other way into the network. Refresh it with `prod bootstrap "
            "webrtc-addresses`."
        )
    if skip_probe:
        return (
            multiaddrs,
            BootstrapProbeResult(total=len(multiaddrs), answered=0, dead=(), skipped=True),
            source,
        )

    result = probe_bootstrap_multiaddrs(multiaddrs, probe=probe)
    if result.answered == 0:
        raise WasmBootstrapError(
            f"none of the {result.total} bootstrap address(es) in '{source}' "
            "answered an ICE connectivity check, so a WASM client could not "
            "reach the network. Refresh the list with `prod bootstrap "
            "webrtc-addresses`, or pass --skip-bootstrap-probe if the tier was "
            "just restarted (the listener opens 2-15 minutes later)."
        )
    return multiaddrs, result, source


def bootstrap_deploy_args(result: BootstrapProbeResult, source: str) -> dict:
    """The ``deploy_args.wasm_bootstrap`` record for a run."""
    return {
        "source": source,
        "total": result.total,
        "answered": result.answered,
        "probe_skipped": result.skipped,
        "dead": list(result.dead),
    }


def digitalocean_slug_ram_mb(slug: str) -> int | None:
    """Memory encoded in a DigitalOcean size slug, or None if unrecognised."""
    match = _DO_SLUG_RAM.match((slug or "").strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    return amount * 1024 if match.group(2) == "gb" else amount


def default_wasm_vm_size(provider: str | None) -> str | None:
    """The VM size a WASM entry gets when the manifest names none.

    Only DigitalOcean has a slug to default to; every other provider's sizes are
    its own vocabulary, so the caller's existing per-provider default stands and
    :func:`check_wasm_vm_size` warns instead.
    """
    if (provider or "digitalocean") == "digitalocean":
        return DEFAULT_WASM_VM_SIZE
    return None


def check_wasm_vm_size(vm_size: str | None, provider: str | None) -> str | None:
    """Return a problem description for a too-small WASM VM, else None.

    DigitalOcean slugs encode their memory, so a slug below the floor is
    REFUSED: it is a fact, not a guess, and an under-sized box turns every
    transfer into an OOM kill that reads as a network failure. Other providers'
    sizes cannot be interpreted here, so the caller warns and proceeds — a hard
    error on an unparseable name would block a perfectly adequate Hetzner box.
    """
    if not vm_size:
        return None
    ram = digitalocean_slug_ram_mb(vm_size)
    if ram is None:
        return None
    if ram >= MIN_WASM_VM_RAM_MB:
        return None
    return (
        f"VM size '{vm_size}' has {ram // 1024} GB of RAM; a browser-path "
        f"transfer holds ~4x the file in WASM memory, so a WASM client needs at "
        f"least {MIN_WASM_VM_RAM_MB // 1024} GB (default "
        f"{DEFAULT_WASM_VM_SIZE})"
    )


def wasm_vm_size_warning(vm_size: str | None, provider: str | None) -> str | None:
    """Advisory note for a non-DigitalOcean WASM VM size, else None."""
    if not vm_size or (provider or "digitalocean") == "digitalocean":
        return None
    if digitalocean_slug_ram_mb(vm_size) is not None:
        return None
    return (
        f"cannot check the memory of {provider} size '{vm_size}'; a browser-path "
        f"transfer holds ~4x the file in WASM memory, so make sure it has at "
        f"least {MIN_WASM_VM_RAM_MB // 1024} GB of RAM"
    )
