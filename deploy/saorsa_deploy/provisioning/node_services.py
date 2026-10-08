"""Shell command builders for managing a single ant-node systemd service on an
already-provisioned host.

Unlike ``provisioning/node.py`` (which provisions a whole ``--node-count``
fleet in one pyinfra pass), these operate on exactly ONE named service on ONE
host — the mechanism behind ``prod services ls/rm/add``
(``cmd/prod_services.py``), used to free disk space on a quota-constrained
fleet (e.g. Hetzner) by removing one ``ant-node-N`` service and later adding
one back once a quota increase lands.

``add``'s new unit is rendered with the same ``_build_node_exec_start``/
``_build_node_unit_file`` templates ``provisioning/node.py`` uses for a full
fleet, fed with parameters recovered from an existing sibling service's live
unit file (via :func:`parse_node_unit`) so the new service matches its
siblings' binary, EVM network, NAT wrapping, log level, bootstrap peers,
restart policy, backtrace env, upgrade channel, and rewards address. heaptrack
wrapping (the ``prod nodes`` kind) is NOT cloned — a heaptrack-wrapped sibling
yields a new service without heaptrack.
"""

import re

from saorsa_deploy.provisioning.nat import NAT_IP_EXEC_PREFIX
from saorsa_deploy.provisioning.volume import LOG_DIR, MOUNT_POINT

SERVICE_NAME_RE = re.compile(r"^ant-node-([1-9]\d*)$")

# The node's persisted identity keypair, written directly in the node's
# --root-dir by ant-node (constant NODE_IDENTITY_FILENAME in
# ant-node/src/config.rs). The peer ID is BLAKE3(ML-DSA-65 public key) derived
# from this file, so preserving it across a data wipe keeps the node's peer ID
# — and therefore its XOR position and record responsibility — unchanged.
NODE_IDENTITY_FILENAME = "node_identity.key"


def parse_service_name(service_name: str) -> int:
    """Return the 1-based service index for ``ant-node-<N>``.

    The sole validation point for a ``--service-name`` value — raises
    ``ValueError`` for anything else, so every caller (CLI entry points and
    the command builders below) rejects malformed input the same way.
    """
    m = SERVICE_NAME_RE.match(service_name)
    if not m:
        raise ValueError(
            f"--service-name must look like 'ant-node-<positive int>', got {service_name!r}"
        )
    return int(m.group(1))


def build_list_services_command() -> str:
    """Shell script listing every ``ant-node-*`` systemd service on the host,
    its active/enabled state, and its data directory's on-disk size.

    Emits one marker line per discovered service:
      ``ANT_SVC:LIST:<service>:<active>:<enabled>:<bytes>``
    or, if none exist:
      ``ANT_SVC:LIST:NONE``

    Services are discovered by globbing unit files, never by trusting a
    ``--node-count``/inventory value — those can drift after a prior
    ``rm``/``add``.
    """
    return f"""\
set -eu
FOUND=0
for unit in /etc/systemd/system/ant-node-*.service; do
  [ -e "$unit" ] || continue
  FOUND=1
  svc=$(basename "$unit" .service)
  active=$(systemctl is-active "$svc" 2>/dev/null || true)
  enabled=$(systemctl is-enabled "$svc" 2>/dev/null || true)
  bytes=$(du -sb "{MOUNT_POINT}/data/$svc" 2>/dev/null | cut -f1)
  [ -n "$bytes" ] || bytes=0
  echo "ANT_SVC:LIST:$svc:$active:$enabled:$bytes"
done
[ "$FOUND" -eq 1 ] || echo "ANT_SVC:LIST:NONE"
"""


def parse_service_list(stdout_lines) -> list[dict]:
    """Parse ``ANT_SVC:LIST:...`` marker lines into records, sorted by index."""
    records = []
    for raw_line in stdout_lines:
        line = raw_line.strip()
        if not line.startswith("ANT_SVC:LIST:") or line == "ANT_SVC:LIST:NONE":
            continue
        rest = line[len("ANT_SVC:LIST:") :]
        parts = rest.split(":")
        if len(parts) != 4:
            continue
        svc, active, enabled, size_bytes = parts
        try:
            index = parse_service_name(svc)
            size = int(size_bytes)
        except ValueError:
            continue
        records.append(
            {
                "service_name": svc,
                "index": index,
                "active": active,
                "enabled": enabled,
                "size_bytes": size,
            }
        )
    records.sort(key=lambda r: r["index"])
    return records


def build_disk_survey_command() -> str:
    """List every ``ant-node-*`` service (as :func:`build_list_services_command`)
    AND the storage device's total/used/free bytes, in one SSH pass — the
    per-host probe behind ``prod services disk-survey``.

    Emits the same ``ANT_SVC:LIST:...`` lines plus one:
      ``ANT_SVC:DF:<total>:<used>:<avail>``  (bytes; empty fields if df fails).
    """
    return f"""\
set -eu
FOUND=0
for unit in /etc/systemd/system/ant-node-*.service; do
  [ -e "$unit" ] || continue
  FOUND=1
  svc=$(basename "$unit" .service)
  active=$(systemctl is-active "$svc" 2>/dev/null || true)
  enabled=$(systemctl is-enabled "$svc" 2>/dev/null || true)
  bytes=$(du -sb "{MOUNT_POINT}/data/$svc" 2>/dev/null | cut -f1)
  [ -n "$bytes" ] || bytes=0
  echo "ANT_SVC:LIST:$svc:$active:$enabled:$bytes"
done
[ "$FOUND" -eq 1 ] || echo "ANT_SVC:LIST:NONE"
DF=$(df -B1 --output=size,used,avail {MOUNT_POINT} 2>/dev/null | tail -1)
echo "ANT_SVC:DF:$(echo $DF | tr ' ' ':')"
"""


def parse_df_line(stdout_lines) -> dict | None:
    """Parse the ``ANT_SVC:DF:<total>:<used>:<avail>`` marker into a dict of
    byte counts, or None if absent/unparseable."""
    for raw_line in stdout_lines:
        line = raw_line.strip()
        if not line.startswith("ANT_SVC:DF:"):
            continue
        parts = line[len("ANT_SVC:DF:") :].split(":")
        if len(parts) != 3:
            return None
        try:
            return {
                "total_bytes": int(parts[0]),
                "used_bytes": int(parts[1]),
                "free_bytes": int(parts[2]),
            }
        except ValueError:
            return None
    return None


def build_remove_service_command(service_name: str) -> str:
    """Stop, disable, and delete the unit + data/log dirs for exactly
    ``service_name``. Every path/command below is scoped to the literal
    service name — never a glob — so sibling ``ant-node-N`` services on the
    same host are never touched.
    """
    parse_service_name(service_name)
    svc = service_name
    return f"""\
set -eu
systemctl stop {svc} 2>/dev/null || true
systemctl disable {svc} 2>/dev/null || true
rm -f /etc/systemd/system/{svc}.service
systemctl daemon-reload
rm -rf {MOUNT_POINT}/data/{svc}
rm -rf {LOG_DIR}/{svc}
echo "ANT_SVC:REMOVED:{svc}"
"""


def parse_node_unit(content: str) -> dict:
    """Parse a rendered ant-node systemd unit file back into the parameters
    needed to clone it for a new service (feeds ``_build_node_exec_start``/
    ``_build_node_unit_file`` in ``provisioning/node.py``).

    Best-effort: a flag absent from the sibling's ``ExecStart`` is reported as
    unset (``None``/``False``), matching ``_build_node_exec_start``'s own
    "falsy means omit the flag" convention, so re-rendering reproduces the
    sibling's unit exactly (modulo the paths/ports that ``add`` intentionally
    changes for the new service index).
    """
    exec_start_m = re.search(r"^ExecStart=(.*)$", content, re.MULTILINE)
    if not exec_start_m:
        raise ValueError("sibling unit file has no ExecStart= line")
    exec_start = exec_start_m.group(1)

    is_nat = exec_start.startswith(NAT_IP_EXEC_PREFIX)
    if is_nat:
        exec_start = exec_start[len(NAT_IP_EXEC_PREFIX) :]

    def _flag(pattern):
        m = re.search(pattern, exec_start)
        return m.group(1) if m else None

    port_s = _flag(r"--port (\d+)")

    restart_policy_m = re.search(r"^Restart=(\S+)$", content, re.MULTILINE)
    restart_sec_m = re.search(r"^RestartSec=(\d+)", content, re.MULTILINE)
    limit_nofile_m = re.search(r"^LimitNOFILE=(\d+)$", content, re.MULTILINE)
    rust_backtrace_m = re.search(r"^Environment=RUST_BACKTRACE=(.*)$", content, re.MULTILINE)

    return {
        "bootstrap_peers": re.findall(r"--bootstrap (\S+)", exec_start),
        "port": int(port_s) if port_s else None,
        "log_level": _flag(r"--log-level (\S+)"),
        "testnet": "--network-mode testnet" in exec_start,
        "upgrade_channel": _flag(r"--upgrade-channel (\S+)"),
        "evm_network": _flag(r"--evm-network (\S+)"),
        "rewards_address": _flag(r"--rewards-address (\S+)"),
        "is_nat": is_nat,
        "restart_policy": restart_policy_m.group(1) if restart_policy_m else "on-success",
        "restart_sec": int(restart_sec_m.group(1)) if restart_sec_m else 5,
        "limit_nofile": int(limit_nofile_m.group(1)) if limit_nofile_m else None,
        "rust_backtrace": rust_backtrace_m.group(1) if rust_backtrace_m else None,
        "colorbt_show_hidden": "Environment=COLORBT_SHOW_HIDDEN=1" in content,
        "coredumps": "LimitCORE=infinity" in content,
    }


def build_write_unit_and_start_command(service_name: str, unit_content: str) -> str:
    """Write a new unit file, reload systemd, then enable and start exactly
    ``service_name`` — never touching any other unit.

    Finally, re-glob the Elasticsearch log forwarder. The new service's log dir
    (``{LOG_DIR}/<svc>``) is created fresh here (a *new inode*, since a prior
    ``rm`` did ``rm -rf`` on it). The forwarder ``telegraf-ant-elasticsearch``
    tails ``ant-*/ant-node*.log*`` with ``watch_method=inotify``, whose watch is
    still bound to the *old, deleted* directory inode — so without a restart it
    would never discover the new node's log file and that node would forward
    **nothing** to Elasticsearch. Restarting the forwarder makes it re-glob and
    pick up the new dir. Guarded on the unit existing (best-effort ``|| true``)
    so hosts without ES forwarding are unaffected; the metrics forwarder
    (``telegraf-ant-metrics``, procstat) self-heals and needs no bounce.
    """
    parse_service_name(service_name)
    return (
        f"cat > /etc/systemd/system/{service_name}.service << 'UNIT_EOF'\n"
        f"{unit_content}UNIT_EOF\n"
        "systemctl daemon-reload\n"
        f"systemctl enable {service_name}\n"
        f"systemctl start {service_name}\n"
        "if systemctl cat telegraf-ant-elasticsearch >/dev/null 2>&1; then "
        "systemctl restart telegraf-ant-elasticsearch || true; "
        'echo "ANT_SVC:LOGFWD_REGLOBBED:telegraf-ant-elasticsearch"; fi\n'
        f'echo "ANT_SVC:ADDED:{service_name}"'
    )


def build_reset_data_command(service_name: str) -> str:
    """Stop ``service_name``, wipe its data dir **except** the identity key, then
    start it again — so the node re-bootstraps with the SAME peer ID.

    The node's ``--root-dir`` (``{MOUNT_POINT}/data/<svc>``) holds both the
    record stores (``chunks.mdb``/``paid_list.mdb`` — essentially all the bytes)
    and the identity (:data:`NODE_IDENTITY_FILENAME`). Deleting everything under
    the root dir *except* ``node_identity.key`` reclaims the space while keeping
    the node's peer ID — hence its XOR range and record responsibility — so on
    restart it re-fetches exactly its current responsible set and drops any stale
    out-of-range records, deterministically same-or-smaller and with no topology
    churn.

    Unlike ``rm``/``add`` this touches neither the systemd unit (same port) nor
    the log dir (``{LOG_DIR}/<svc>`` is left intact, so the ES forwarder's
    inotify watch is undisturbed — no forwarder restart needed).

    Aborts (exit 3) *before* stopping the service if the identity key is missing,
    so a missing key never turns this into a silent identity-losing wipe. Scoped
    to the literal service name — sibling services are never touched.
    """
    parse_service_name(service_name)
    root_dir = f"{MOUNT_POINT}/data/{service_name}"
    key_path = f"{root_dir}/{NODE_IDENTITY_FILENAME}"
    return f"""\
set -eu
if [ ! -f "{key_path}" ]; then
  echo "ANT_SVC:RESET_ERR:missing_identity_key:{key_path}" >&2
  exit 3
fi
systemctl stop {service_name}
find "{root_dir}" -mindepth 1 -maxdepth 1 ! -name {NODE_IDENTITY_FILENAME} -exec rm -rf {{}} +
systemctl start {service_name}
echo "ANT_SVC:RESET:{service_name}"
"""
