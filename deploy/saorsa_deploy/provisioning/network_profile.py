"""Simulated residential network conditions for client VMs (V2-733).

Uploads and downloads run from cloud VMs on ~gigabit links, which is not how the
users we care about reach the network. This module reshapes a client VM's
connection with ``tc`` so it behaves like a home line — rate caps, added latency
and jitter, packet loss — selected by a named, git-versioned profile so runs are
reproducible and comparable across release trains.

Structure mirrors :mod:`saorsa_deploy.provisioning.clock_skew`, the existing
precedent for an optional per-client-VM host mutation: **pure functions only, no
pyinfra imports**, so every generated shell script is unit-testable, and the
manifest validator in :mod:`saorsa_deploy.config.models` can reuse the same parser
the CLI uses.

Design notes — the non-obvious parts
------------------------------------

**Monitoring and SSH traffic is exempt.** A shaped VM still ships Telegraf metrics
to InfluxDB and (increasingly) forwarded client logs to Elasticsearch. On a 1 Mbit/s
uplink that instrumentation would compete with the transfer for the capped
bandwidth: the measured transfer comes out slower than a real user's would (no real
user ships their own logs), and the run's own results can arrive incomplete —
degrading the instrument used to take the measurement. So the shaper builds an
``htb`` hierarchy with two classes and filters that traffic into an *unshaped* one.

The exemption is keyed on **TCP port** (see :data:`EXEMPT_PORTS`), not address.
That is not a shortcut: the Elasticsearch VM is created *after* the client VMs are
provisioned, so at the moment shaping is applied its address does not yet exist. An
address-based rule would silently miss log forwarding on exactly the runs that
enable it. Explicit IPs can still be supplied and are exempted as well.

The **source fetch** is exempt on the same principle but for a different reason (see
:data:`FETCH_EXEMPT_PORTS`): before it can upload anything, the harness must pull the
file from a public URL over HTTP(S). A real user already has their file, so that
transfer is scaffolding — shaping it measures nothing and can dominate the run, since
it is inbound and therefore capped by ``rate_down`` *and* by loss.

**Queue depth is not a knob, but it is always a choice.** A rate cap implies a
queue; its depth decides whether the link tail-drops (throughput far below the cap,
looking like heavy loss) or bloats (hundreds of ms of standing delay that silently
swamps the configured ``delay``). Rather than inherit whatever the default is, an
explicit AQM is pinned at the leaf: ``cake`` where available, ``fq_codel``
otherwise — both always present on the Ubuntu 24.04 client image. That makes the
baseline "a competent modern router", reproducibly.

**Latency is applied once; loss is applied per direction.** RTT is a round-trip
property, so ``delay``/``jitter`` go on the egress path only — applying them to
both paths would silently double the configured value. Loss is a per-direction
property, so it goes on both; inbound loss is what makes a *download* behave like a
home download, and an egress-only rule would leave the inbound data stream pristine.

**Ingress needs an ``ifb`` mirror.** ``tc`` shapes egress only. Downloads are half
the workload, so the inbound path is mirrored to an intermediate functional block
device and shaped there. Without it a "10 Mbit down" profile would not cap
downloads at all.

**Convergent rather than skip-if-present.** :mod:`clock_skew` guards on a marker
file because re-running ``date -s`` would *compound* the offset. The same hazard
exists here (two ``netem delay 30ms`` rules are 60 ms), but the fix is better: the
script deletes any existing qdisc before adding, so re-provisioning converges on the
configured state instead of refusing. The marker file is retained for observability
— it records what is currently applied — not as a guard.

**Persistence.** Applied by a systemd oneshot unit rather than a bare command, so
the shaping survives a VM reboot; ``tc`` state is otherwise purely in-kernel.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from saorsa_deploy.resources import get_resources_dir

NETWORK_DIRNAME = "network"
DEFAULT_PROFILES_FILENAME = "profiles.yaml"

# Where the apply/clear script and its unit live on the client VM.
SCRIPT_PATH = "/usr/local/sbin/ant-network-impairment.sh"
UNIT_NAME = "ant-network-impairment.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

# Records what is currently applied. Observability only — NOT a guard (see module
# docstring): the script converges rather than refusing on a second run.
MARKER = "/etc/ant-network-impairment-applied"

# Intermediate functional block device carrying mirrored ingress traffic.
IFB_DEVICE = "ifb-ant0"

# TCP ports whose traffic bypasses the shaper, in both directions.
#
# Exempting by PORT rather than by address is deliberate and load-bearing: the
# Elasticsearch VM is created *after* the client VMs are provisioned, so its
# address simply is not known at the moment the shaping is applied. An
# address-based exemption would therefore silently fail to cover log forwarding —
# the single largest instrumentation stream — on exactly the runs that enable it.
# Ports are known up front and never change.
#
#   22    SSH — the operator connects from an address unknown at provision time
#   8086  InfluxDB — Telegraf metrics
#   9200  Elasticsearch — forwarded client logs
#
# The risk of over-exempting is negligible: ant is a UDP/QUIC peer-to-peer client
# on ephemeral ports and never speaks TCP to these three.
EXEMPT_PORTS = (22, 8086, 9200)

# TCP ports carrying the *source fetch*, likewise bypassing the shaper.
#
#   80/443  HTTP(S) — the uploader pulls each source file from a public URL before
#           it can upload anything (`_download` in prod_uploader.py, ahead of the
#           `ant` invocation in the same loop iteration).
#
# This is scaffolding, not measurement. A real home user already HAS the file on
# disk; only this harness has to fetch it first, so shaping that transfer measures
# nothing and costs the whole run its wall-clock. It costs the most on exactly the
# profiles the exercise cares about most: the fetch is inbound, so it is capped by
# `rate_down` AND by loss, and a measured mobile-4g run spent 1h45m fetching 1.41 GB
# of a 2.33 GB file without reaching the upload at all.
#
# Safe for the same reason the instrumentation exemption is: the measured path —
# ant storing chunks to its peers — is UDP/QUIC on ephemeral ports and never TCP
# 80/443, so nothing being measured lands in the exempt class. The one other thing
# that does is the Arbitrum One payment RPC (HTTPS). That is a few KB per payment
# batch against gigabytes of chunks, and it is latency- and chain-bound rather than
# bandwidth-bound, so leaving it unshaped does not flatter the result.
#
# Deliberately a separate tuple from EXEMPT_PORTS: the two exist for different
# reasons, and anyone who later wants the fetch shaped on purpose should be able to
# drop this one without also un-exempting Telegraf.
FETCH_EXEMPT_PORTS = (80, 443)

# Everything filtered into the unshaped class, in the order emitted to the script.
ALL_EXEMPT_PORTS = EXEMPT_PORTS + FETCH_EXEMPT_PORTS

# Ceiling for the root and exempt classes: effectively "unshaped". Well above any
# provider's client VM link, so the exempt class is never itself a bottleneck.
UNSHAPED_RATE = "10gbit"

# tc class ids. 1:10 is the shaped class (htb `default`), 1:20 the exempt one.
SHAPED_CLASSID = "1:10"
EXEMPT_CLASSID = "1:20"

# htb derives a class's `quantum` from rate/r2q unless told otherwise, and warns
# when the result is out of range. The shaped class (as low as 1mbit) and the
# unshaped ones (10gbit) are four orders of magnitude apart, so no single r2q
# satisfies both — verified: r2q 10 warns "quantum too big" on the fast classes and
# r2q 1000 warns "too small" on the slow one. Setting quantum explicitly is the fix,
# and silences the warnings across the whole profile range (1mbit to 300mbit).
SHAPED_QUANTUM = 1514  # one MTU — the correct floor for a rate-limited class
UNSHAPED_QUANTUM = 60000

_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)(bit|kbit|mbit|gbit)$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s)$", re.IGNORECASE)

_RATE_MULTIPLIERS = {"bit": 1, "kbit": 1_000, "mbit": 1_000_000, "gbit": 1_000_000_000}
_DURATION_MULTIPLIERS = {"ms": 1.0, "s": 1000.0}

# Fields a profile (built-in or inline) may set. Anything else is a typo and is
# rejected loudly — a silently-ignored `rate-up` would leave the upload uncapped
# while the run still reported itself as shaped.
_PROFILE_FIELDS = ("description", "rate_down", "rate_up", "delay", "jitter", "loss")
_REQUIRED_PROFILE_FIELDS = ("rate_down", "rate_up")


class NetworkProfileError(ValueError):
    """Raised when a profile name is unknown or a profile definition is malformed.

    A distinct type so the CLI and the manifest validator can report it as an
    operator error naming the offending value, rather than as an unhandled crash.
    """


@dataclass(frozen=True)
class NetworkProfile:
    """A fully resolved set of link conditions, ready to render into ``tc`` commands.

    ``delay_ms``/``jitter_ms`` are *added* round-trip latency on top of whatever the
    VM's own RTT to its peers already is — not an absolute target RTT.
    ``loss_percent`` is applied per direction.
    """

    name: str
    rate_down_bps: int
    rate_up_bps: int
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    description: str = ""
    # Version of the shipped profile file this came from; None for an inline
    # (manifest-supplied) definition, which has no version to speak of.
    profile_set_version: Optional[int] = None
    # "builtin" or "inline" — recorded with results so a run's conditions can be
    # traced back to either a versioned profile or an ad-hoc one.
    source: str = "builtin"

    @property
    def rate_down(self) -> str:
        """The downstream cap as a tc rate token."""
        return format_rate(self.rate_down_bps)

    @property
    def rate_up(self) -> str:
        """The upstream cap as a tc rate token."""
        return format_rate(self.rate_up_bps)

    def to_dict(self) -> dict:
        """Serialise for persistence into deployment state / ``deploy_args``.

        This is the authoritative record of what a run was shaped with — the
        results tables carry only the profile *name*, so this is what makes a past
        run interpretable after the profile file has moved on.
        """
        return {
            "name": self.name,
            "rate_down": self.rate_down,
            "rate_up": self.rate_up,
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "loss_percent": self.loss_percent,
            "profile_set_version": self.profile_set_version,
            "source": self.source,
        }


def get_default_profiles_path() -> Path:
    """Return the absolute path to the shipped profile set."""
    return get_resources_dir() / NETWORK_DIRNAME / DEFAULT_PROFILES_FILENAME


def parse_rate(value: Any, *, field: str = "rate") -> int:
    """Parse a tc rate token (``800kbit``, ``27400kbit``, ``1.5mbit``) into bits/sec.

    Units are the decimal ones tc itself uses — ``kbit`` is 1000 bits, not 1024.
    Raises :class:`NetworkProfileError` on anything malformed or non-positive.
    """
    if value is None:
        raise NetworkProfileError(f"{field} is required (e.g. '10mbit' or '800kbit').")
    text = str(value).strip().lower()
    match = _RATE_RE.match(text)
    if not match:
        raise NetworkProfileError(
            f"Invalid {field} '{value}': expected a tc rate such as '1mbit', "
            f"'800kbit' or '27400kbit' (units bit/kbit/mbit/gbit)."
        )
    amount = float(match.group(1)) * _RATE_MULTIPLIERS[match.group(2).lower()]
    if amount <= 0:
        raise NetworkProfileError(f"Invalid {field} '{value}': must be greater than zero.")
    return int(amount)


def format_rate(bps: int) -> str:
    """Render bits/sec back into the most readable exact tc token.

    Exactness matters more than brevity: a profile of 27400kbit must not come back
    as '27mbit' when it is echoed into a recorded run.
    """
    for unit in ("gbit", "mbit", "kbit"):
        factor = _RATE_MULTIPLIERS[unit]
        if bps >= factor and bps % factor == 0:
            return f"{bps // factor}{unit}"
    return f"{bps}bit"


def parse_duration_ms(value: Any, *, field: str = "delay") -> float:
    """Parse a duration token (``30ms``, ``12.7ms``, ``1s``) into milliseconds.

    A bare number is accepted and read as milliseconds, since that is the only unit
    that makes sense at this scale.
    """
    if value is None:
        return 0.0
    text = str(value).strip().lower()
    if not text:
        return 0.0
    match = _DURATION_RE.match(text)
    if match:
        ms = float(match.group(1)) * _DURATION_MULTIPLIERS[match.group(2).lower()]
    else:
        try:
            ms = float(text)
        except ValueError as exc:
            raise NetworkProfileError(
                f"Invalid {field} '{value}': expected a duration such as '30ms', '12.7ms' or '1s'."
            ) from exc
    if ms < 0:
        raise NetworkProfileError(f"Invalid {field} '{value}': must not be negative.")
    return ms


def parse_loss_percent(value: Any) -> float:
    """Parse a loss percentage — ``0.2``, ``"0.2"`` or ``"0.2%"`` — into a float.

    Rejects anything outside 0–100. A value above 100 is always a unit mix-up (a
    fraction mistaken for a percentage, or vice versa) and would otherwise silently
    black-hole the link.
    """
    if value is None:
        return 0.0
    text = str(value).strip().rstrip("%").strip()
    if not text:
        return 0.0
    try:
        loss = float(text)
    except ValueError as exc:
        raise NetworkProfileError(
            f"Invalid loss '{value}': expected a percentage such as 0.2 or '0.2%'."
        ) from exc
    if loss < 0 or loss > 100:
        raise NetworkProfileError(f"Invalid loss '{value}': must be between 0 and 100 percent.")
    return loss


def _profile_from_mapping(
    name: str,
    data: dict,
    *,
    profile_set_version: Optional[int],
    source: str,
) -> NetworkProfile:
    """Build a :class:`NetworkProfile` from a raw mapping, validating strictly."""
    if not isinstance(data, dict):
        raise NetworkProfileError(
            f"Network profile '{name}' must be a mapping of "
            f"{', '.join(_PROFILE_FIELDS)}; got {type(data).__name__}."
        )
    unknown = sorted(set(data) - set(_PROFILE_FIELDS))
    if unknown:
        raise NetworkProfileError(
            f"Network profile '{name}': unknown field(s) {', '.join(unknown)}. "
            f"Valid fields are {', '.join(_PROFILE_FIELDS)}."
        )
    missing = [f for f in _REQUIRED_PROFILE_FIELDS if data.get(f) is None]
    if missing:
        raise NetworkProfileError(
            f"Network profile '{name}': missing required field(s) "
            f"{', '.join(missing)}. Both a downstream and an upstream cap are "
            f"required — an unbounded direction is not a residential line."
        )
    return NetworkProfile(
        name=name,
        rate_down_bps=parse_rate(data.get("rate_down"), field="rate_down"),
        rate_up_bps=parse_rate(data.get("rate_up"), field="rate_up"),
        delay_ms=parse_duration_ms(data.get("delay"), field="delay"),
        jitter_ms=parse_duration_ms(data.get("jitter"), field="jitter"),
        loss_percent=parse_loss_percent(data.get("loss")),
        description=str(data.get("description") or "").strip(),
        profile_set_version=profile_set_version,
        source=source,
    )


def load_profiles(path: Optional[Path] = None) -> dict[str, NetworkProfile]:
    """Load the shipped (or an override) profile set, keyed by name.

    Validation is strict and fails loudly, for the same reason the gate thresholds
    are: a profile silently defaulting to no cap would leave a run unshaped while
    still reporting itself as shaped, which is worse than having no profiles.
    """
    profiles_path = Path(path) if path else get_default_profiles_path()
    if not profiles_path.is_file():
        raise NetworkProfileError(f"Network profile file not found: {profiles_path}")

    try:
        raw = yaml.safe_load(profiles_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise NetworkProfileError(f"Could not parse {profiles_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise NetworkProfileError(f"{profiles_path}: expected a mapping at the top level.")

    version = raw.get("version")
    if not isinstance(version, int):
        raise NetworkProfileError(
            f"{profiles_path}: 'version' is required and must be an integer. It is "
            f"echoed into every recorded run so an edited profile cannot silently "
            f"change what a past run's label meant."
        )

    entries = raw.get("profiles")
    if not isinstance(entries, dict) or not entries:
        raise NetworkProfileError(f"{profiles_path}: 'profiles' must be a non-empty mapping.")

    return {
        name: _profile_from_mapping(name, data, profile_set_version=version, source="builtin")
        for name, data in entries.items()
    }


def parse_network_token(token: str) -> Union[str, dict]:
    """Parse the CLI/spec-token form back into a profile name or an inline mapping.

    A bare name (``home-fttc``) stays a name. An inline set is comma-delimited
    ``key=value`` pairs (``rate_down=30mbit,rate_up=5mbit,delay=25ms``) —
    deliberately **not** colon-delimited, because client specs already split on
    ``:`` and a raw parameter set would collide with that grammar.
    """
    text = str(token).strip()
    if "=" not in text:
        return text
    data: dict = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise NetworkProfileError(
                f"Invalid network '{token}': '{pair}' is not a key=value pair. Use a "
                f"profile name, or comma-separated pairs like "
                f"'rate_down=30mbit,rate_up=5mbit,delay=25ms'."
            )
        key, value = pair.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def format_network_token(value: Union[str, dict]) -> str:
    """Render a manifest ``network`` value into its CLI/spec-token form.

    Inverse of :func:`parse_network_token`, used by the orchestrator to pass a
    manifest's inline definition through to the deploy command unchanged.
    """
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in value.items() if v is not None)
    return str(value).strip()


def resolve_network(
    value: Union[str, dict, None], *, path: Optional[Path] = None
) -> Optional[NetworkProfile]:
    """Resolve a manifest/CLI ``network`` value into a profile.

    Accepts a built-in profile **name** (``"home-fttc"``), an inline **mapping** of
    the same fields, or the comma-delimited **token** form of that mapping. ``None``
    means unshaped, which is the default everywhere and leaves the deployment
    byte-identical to one built before this feature existed.
    """
    if value is None:
        return None
    if isinstance(value, str) and "=" in value:
        value = parse_network_token(value)
    if isinstance(value, dict):
        return _profile_from_mapping(
            value.get("name") or "inline", value, profile_set_version=None, source="inline"
        )
    name = str(value).strip()
    if not name:
        return None
    profiles = load_profiles(path)
    if name not in profiles:
        raise NetworkProfileError(
            f"Unknown network profile '{name}'. Available: {', '.join(sorted(profiles))}."
        )
    return profiles[name]


def format_network_label(profile: NetworkProfile) -> str:
    """Render a compact one-line summary for console output and Slack posts."""
    parts = [f"{profile.rate_down} down / {profile.rate_up} up"]
    if profile.delay_ms:
        delay = f"+{_trim(profile.delay_ms)}ms RTT"
        if profile.jitter_ms:
            delay += f" ±{_trim(profile.jitter_ms)}ms"
        parts.append(delay)
    if profile.loss_percent:
        parts.append(f"{_trim(profile.loss_percent)}% loss")
    return f"{profile.name} ({', '.join(parts)})"


def _trim(value: float) -> str:
    """Render a float without a trailing ``.0`` (12.0 -> '12', 12.7 -> '12.7')."""
    return f"{value:g}"


def _validate_exempt_ips(exempt_ips: Optional[list[str]]) -> list[str]:
    """Validate and de-duplicate the exempt endpoint IPs, preserving order.

    IPv4 only: the ``u32`` filters below match ``protocol ip``. Every observability
    endpoint in this codebase is addressed by IPv4 (``vm_ip`` in monitoring/elastic
    state), so this is a real limitation but not a live one — an IPv6-only InfluxDB
    would be shaped along with everything else.
    """
    if not exempt_ips:
        return []
    seen: list[str] = []
    for raw in exempt_ips:
        text = str(raw).strip()
        if not text:
            continue
        try:
            parsed = ipaddress.IPv4Address(text)
        except ipaddress.AddressValueError as exc:
            raise NetworkProfileError(
                f"Invalid exempt endpoint '{raw}': expected an IPv4 address. Traffic "
                f"to the monitoring/Elasticsearch endpoints must bypass the shaper."
            ) from exc
        if str(parsed) not in seen:
            seen.append(str(parsed))
    return seen


def _netem_args(delay_ms: float, jitter_ms: float, loss_percent: float) -> str:
    """Build the netem argument string, omitting anything unset.

    Returns "" when there is nothing to emulate, which the caller uses to skip the
    netem qdisc entirely rather than install a no-op one.
    """
    args: list[str] = []
    if delay_ms > 0:
        args.append(f"delay {_trim(delay_ms)}ms")
        if jitter_ms > 0:
            # A normal distribution is a closer match to a real line than netem's
            # default uniform jitter.
            args.append(f"{_trim(jitter_ms)}ms distribution normal")
    if loss_percent > 0:
        args.append(f"loss {_trim(loss_percent)}%")
    return " ".join(args)


def build_network_impairment_script(
    profile: NetworkProfile, exempt_ips: Optional[list[str]] = None
) -> str:
    """Return the apply/clear script installed at :data:`SCRIPT_PATH`.

    Runs under ``/bin/sh`` (dash on Ubuntu) — no bashisms, no ``pipefail``. Takes a
    single argument, ``apply`` or ``clear``; the systemd unit drives both.

    The egress path carries the upstream cap plus delay/jitter/loss; the mirrored
    ingress path carries the downstream cap plus loss only (see the module docstring
    on why latency is applied once and loss per direction).
    """
    ips = _validate_exempt_ips(exempt_ips)
    egress_netem = _netem_args(profile.delay_ms, profile.jitter_ms, profile.loss_percent)
    # Ingress: loss only. Delay is deliberately omitted so RTT is not doubled.
    ingress_netem = _netem_args(0.0, 0.0, profile.loss_percent)

    exempt_ip_lines = "\n".join(f'    EXEMPT_IPS="$EXEMPT_IPS {ip}"' for ip in ips) or "    :"
    exempt_ports = " ".join(str(port) for port in ALL_EXEMPT_PORTS)

    return f"""\
#!/bin/sh
# Simulated residential network conditions for ant client VMs (V2-733).
# Generated by saorsa-deploy — edits will be overwritten on the next provision.
#
# Profile: {profile.name}
#   {profile.rate_down} down / {profile.rate_up} up, \
delay {_trim(profile.delay_ms)}ms, jitter {_trim(profile.jitter_ms)}ms, \
loss {_trim(profile.loss_percent)}%
#
# Traffic to the exempt endpoints (InfluxDB / Elasticsearch) and SSH is filtered
# into an unshaped class so instrumentation never competes with the transfer for
# the capped bandwidth. HTTP(S) is exempt too: that is the harness fetching the
# source file before the upload, which a real user would never do, so shaping it
# would only burn wall-clock. The measured path is ant over UDP/QUIC and is
# unaffected by either exemption.
set -eu

IFB="{IFB_DEVICE}"
MARKER="{MARKER}"
UNSHAPED="{UNSHAPED_RATE}"
EXEMPT_PORTS="{exempt_ports}"

EXEMPT_IPS=""
set_exempt_ips() {{
{exempt_ip_lines}
}}

find_iface() {{
    ip route show default 2>/dev/null | awk '/^default/ {{ print $5; exit }}'
}}

# cake is the better AQM but is a module; fq_codel is always present. Probe once
# and fall back, so a kernel without sch_cake degrades instead of failing.
select_aqm() {{
    if modprobe sch_cake 2>/dev/null; then
        echo cake
    else
        echo fq_codel
    fi
}}

add_aqm() {{
    # $1 dev, $2 parent, $3 handle
    tc qdisc add dev "$1" parent "$2" handle "$3" "$AQM" 2>/dev/null && return 0
    tc qdisc add dev "$1" parent "$2" handle "$3" fq_codel
}}

# Build the htb hierarchy on one device: a shaped default class and an unshaped
# exempt class, with u32 filters steering the exempt traffic.
#   $1 dev, $2 rate, $3 netem args (may be empty), $4 "dst" or "src"
shape_device() {{
    DEV="$1"; RATE="$2"; NETEM="$3"; IPMATCH="$4"

    tc qdisc del dev "$DEV" root 2>/dev/null || true
    tc qdisc add dev "$DEV" root handle 1: htb default 10
    # Explicit `quantum` on every class. htb otherwise derives it from rate/r2q,
    # and the shaped and exempt classes sit four orders of magnitude apart — no
    # single r2q suits both, so it warns "quantum too big"/"too small" on every
    # apply. One MTU for the shaped class, {UNSHAPED_QUANTUM} for the unshaped ones.
    tc class add dev "$DEV" parent 1: classid 1:1 htb rate "$UNSHAPED" \
quantum {UNSHAPED_QUANTUM}
    tc class add dev "$DEV" parent 1:1 classid {SHAPED_CLASSID} htb rate "$RATE" \
ceil "$RATE" quantum {SHAPED_QUANTUM}
    tc class add dev "$DEV" parent 1:1 classid {EXEMPT_CLASSID} htb rate "$UNSHAPED" \
ceil "$UNSHAPED" quantum {UNSHAPED_QUANTUM}

    if [ -n "$NETEM" ]; then
        # netem sits under the shaped class; the AQM becomes netem's child so the
        # queue is managed rather than left to netem's own tail-drop limit.
        tc qdisc add dev "$DEV" parent {SHAPED_CLASSID} handle 10: netem $NETEM
        add_aqm "$DEV" 10:1 100:
    else
        add_aqm "$DEV" {SHAPED_CLASSID} 100:
    fi
    add_aqm "$DEV" {EXEMPT_CLASSID} 200:

    # Observability and SSH are exempted by PORT, not address — the Elasticsearch
    # VM does not exist yet when this runs, so its address is unknowable here.
    # On egress the exempt traffic is leaving for that port (sport is ours on the
    # mirrored path, dport on the way out); the match flips with the direction.
    for port in $EXEMPT_PORTS; do
        if [ "$IPMATCH" = "dst" ]; then
            tc filter add dev "$DEV" protocol ip parent 1: prio 1 u32 \
                match ip protocol 6 0xff match ip dport "$port" 0xffff \
                flowid {EXEMPT_CLASSID}
            tc filter add dev "$DEV" protocol ip parent 1: prio 1 u32 \
                match ip protocol 6 0xff match ip sport "$port" 0xffff \
                flowid {EXEMPT_CLASSID}
        else
            tc filter add dev "$DEV" protocol ip parent 1: prio 1 u32 \
                match ip protocol 6 0xff match ip sport "$port" 0xffff \
                flowid {EXEMPT_CLASSID}
            tc filter add dev "$DEV" protocol ip parent 1: prio 1 u32 \
                match ip protocol 6 0xff match ip dport "$port" 0xffff \
                flowid {EXEMPT_CLASSID}
        fi
    done

    for ip in $EXEMPT_IPS; do
        tc filter add dev "$DEV" protocol ip parent 1: prio 2 u32 \
            match ip "$IPMATCH" "$ip"/32 flowid {EXEMPT_CLASSID}
    done
}}

clear_all() {{
    IFACE=$(find_iface)
    if [ -n "$IFACE" ]; then
        tc qdisc del dev "$IFACE" root 2>/dev/null || true
        tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
    fi
    tc qdisc del dev "$IFB" root 2>/dev/null || true
    ip link set dev "$IFB" down 2>/dev/null || true
    ip link del "$IFB" 2>/dev/null || true
    rm -f "$MARKER"
    echo "ANT_NETEM:CLEARED"
}}

apply_all() {{
    IFACE=$(find_iface)
    if [ -z "$IFACE" ]; then
        echo "ANT_NETEM:ERROR:no-default-route-interface" >&2
        exit 1
    fi
    AQM=$(select_aqm)
    set_exempt_ips

    # Egress: upstream cap, plus delay/jitter/loss.
    shape_device "$IFACE" "{profile.rate_up}" "{egress_netem}" dst

    # Ingress: tc shapes egress only, so mirror inbound traffic to an ifb device
    # and shape that. Without this the downstream cap would do nothing at all.
    modprobe ifb numifbs=0 2>/dev/null || true
    ip link show "$IFB" >/dev/null 2>&1 || ip link add "$IFB" type ifb
    ip link set dev "$IFB" up
    tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
    tc qdisc add dev "$IFACE" handle ffff: ingress
    tc filter add dev "$IFACE" parent ffff: protocol all prio 10 u32 \
        match u32 0 0 action mirred egress redirect dev "$IFB"

    # Exempt matching flips to the source address here: on the mirrored device
    # these are the inbound packets, so the observability endpoint is the source.
    shape_device "$IFB" "{profile.rate_down}" "{ingress_netem}" src

    echo "{profile.name} {profile.rate_down}/{profile.rate_up} aqm=$AQM" > "$MARKER"
    echo "ANT_NETEM:APPLIED:{profile.name}:aqm=$AQM"
}}

case "${{1:-apply}}" in
    apply) apply_all ;;
    clear) clear_all ;;
    *) echo "usage: $0 apply|clear" >&2; exit 2 ;;
esac
"""


def build_network_impairment_unit() -> str:
    """Return the systemd unit that applies the shaping.

    A oneshot with ``RemainAfterExit`` so ``systemctl stop`` clears the rules, and
    ``After=network-online.target`` because the script resolves the interface from
    the default route. Being a unit rather than a bare command is what makes the
    shaping survive a reboot — ``tc`` state lives only in the kernel.
    """
    return f"""\
[Unit]
Description=Simulated residential network conditions for ant client (V2-733)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={SCRIPT_PATH} apply
ExecStop={SCRIPT_PATH} clear

[Install]
WantedBy=multi-user.target
"""


def build_network_impairment_command(
    profile: NetworkProfile, exempt_ips: Optional[list[str]] = None
) -> str:
    """Return the shell command that installs and starts the shaping.

    Writes the script and unit, then enables and (re)starts the unit. Convergent:
    a re-provision overwrites both and restarts, so the VM ends up in the state the
    manifest describes rather than whatever an earlier run left behind.
    """
    script = build_network_impairment_script(profile, exempt_ips)
    unit = build_network_impairment_unit()
    return f"""\
set -eu
cat > {SCRIPT_PATH} <<'ANT_NETEM_SCRIPT_EOF'
{script}ANT_NETEM_SCRIPT_EOF
chmod 0755 {SCRIPT_PATH}
cat > {UNIT_PATH} <<'ANT_NETEM_UNIT_EOF'
{unit}ANT_NETEM_UNIT_EOF
systemctl daemon-reload
systemctl enable {UNIT_NAME} >/dev/null 2>&1 || true
systemctl restart {UNIT_NAME}
systemctl is-active --quiet {UNIT_NAME} || {{
    echo "ANT_NETEM:ERROR:unit-not-active" >&2
    journalctl -u {UNIT_NAME} -n 20 --no-pager >&2 || true
    exit 1
}}
cat {MARKER} 2>/dev/null || true
"""


def build_network_impairment_teardown_command() -> str:
    """Return the shell command that removes the shaping from a VM.

    Not used by the normal destroy path (the VM goes away), but needed to return a
    kept VM to an unshaped state for a follow-up comparison without reprovisioning.
    """
    return f"""\
set -eu
if [ -f {UNIT_PATH} ]; then
    systemctl disable --now {UNIT_NAME} >/dev/null 2>&1 || true
fi
if [ -x {SCRIPT_PATH} ]; then
    {SCRIPT_PATH} clear || true
fi
rm -f {UNIT_PATH} {SCRIPT_PATH}
systemctl daemon-reload
echo "ANT_NETEM:REMOVED"
"""
