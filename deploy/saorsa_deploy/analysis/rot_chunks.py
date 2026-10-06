"""`analysis rot-chunks` — inject, verify and attribute rotted chunk files.

ant-node PR #242 (V2-1382) makes a node whose chunk file no longer hashes to
its key report that key from round 1 of a subtree audit, remove the file, stop
claiming the key and let replication refetch it. Nothing rots on its own inside
a six-hour testnet, so a run that tests the repair path has to corrupt the files
itself and then prove, from disk and from Elasticsearch, what happened to each
key.

Three modes, one per invocation:

* ``--apply`` lists every chunk file of the targeted services, samples a
  deterministic fraction of them, backs each one up and overwrites it **in
  place, under the same name** with the bytes ``rotted``, and writes a manifest.
  The file is never deleted or renamed: a *missing* file goes down a different
  code path in ant-node, so a run that removed them would measure the wrong
  thing.
* ``--verify`` re-reads those keys from the manifest and classifies each one —
  repaired (byte-identical to the backup), still rotted, removed, or something
  else — alongside a control set of untouched keys from the same services.
* ``--attribute`` joins the manifest with the node log index to say *why* each
  key was removed: an audit detection before the removal is the repair path
  under test; a removal with no prior detection is an ordinary fetch-side
  discovery.

The sample is ``random.Random(f"{tag}:{host}:{service}")``, so re-running the
same tag against the same host picks the same keys and a re-applied tag is
refused outright rather than rotting a second slice.

Every timestamp written or printed is UTC ISO-8601 to the second with a ``Z``
suffix, so a run's phase script can compare them as plain strings.
"""

# ruff: noqa: E501 -- the remote bash payloads are kept verbatim.
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from saorsa_deploy.analysis import hosts, remote
from saorsa_deploy.analysis.es import EsRun, host_variants
from saorsa_deploy.cmd.analysis import add_deployment_args, add_host_selection_args
from saorsa_deploy.es_log_counts import MESSAGE_FIELD, TIMESTAMP_FIELD

HOST_SRC = "tag.host"

# The bytes a rotted file is overwritten with -- six of them, which is also what
# ant-node's own e2e test writes.
ROT_BYTES = "rotted"
BACKUP_ROOT = "/mnt/ant-node-storage/rot-backup"
DEFAULT_ROOT_TEMPLATE = "/mnt/ant-node-storage/data/{service}"

KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MESSAGE_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# `systemctl show -p ExecStart --value` prints one line holding the whole argv,
# so the root dir is whatever follows `--root-dir` (either spelling).
ROOT_DIR_RE = re.compile(r"--root-dir[= ]+(\S+)")

# The five ant-node phrases the attribution rests on. Substrings of the real
# WARN lines, never a paraphrase: the key is interpolated into each message, so
# only the invariant part can be matched.
PHRASES = {
    # "Subtree audit: committed key {key} does not hash to its address; …"
    "detection": "does not hash to its address",
    # "Removed corrupt chunk file {key}; replication will repair it"
    "removal": "Removed corrupt chunk file",
    # "Corrupt chunk {key} could not be removed: …"
    "not_removable": "could not be removed",
    # the recheck queue overflowed; carries no key
    "overflow": "were not queued for a recheck",
    # "… missing bytes for committed key {key}"
    "stale_commitment": "missing bytes for committed key",
}

PROBE_SCRIPT = r"""set -u
printf 'HOST\t%s\n' "$(hostname)"
for SVC in "$@"; do
  EXEC=$(systemctl show -p ExecStart --value "$SVC" 2>/dev/null | tr '\n' ' ' || true)
  printf 'EXEC\t%s\t%s\n' "$SVC" "$EXEC"
done
echo "STATUS=done"
"""

# Args are (service, root) pairs. Depth 2 under <root>/chunks is exactly the
# <xy>/<64 hex> layout; anything whose name is not a key (layout.json, .lock,
# .tmp.*) is dropped here and again in Python.
LIST_SCRIPT = r"""set -u
while [ "$#" -gt 1 ]; do
  SVC="$1"; ROOT="$2"; shift 2
  DIR="$ROOT/chunks"
  if [ ! -d "$DIR" ]; then
    printf 'SVCERR\t%s\tno_chunk_dir:%s\n' "$SVC" "$DIR"
    continue
  fi
  find "$DIR" -mindepth 2 -maxdepth 2 -type f 2>/dev/null | while IFS= read -r F; do
    B=$(basename "$F")
    case "$B" in
      layout.json|*.lock|.tmp.*|*.tmp.*) continue ;;
    esac
    SZ=$(stat -c %s "$F" 2>/dev/null) || continue
    SH=$(sha256sum "$F" 2>/dev/null | cut -d' ' -f1) || continue
    printf 'FILE\t%s\t%s\t%s\t%s\n' "$SVC" "$B" "$SZ" "$SH"
  done
  printf 'SVCEND\t%s\n' "$SVC"
done
echo "STATUS=done"
"""

# Args: tag, service, root, backup root, key-list file. The key list is STAGED
# AS A FILE, never argv: ssh hands the whole `bash -s -- …` command to the
# remote shell as ONE argument, which the kernel caps at MAX_ARG_STRLEN
# (128 KiB) -- about 1,950 keys -- and a service one busy hour larger than that
# would fail outright. (The `stdin_extra` seam cannot carry it either: bash
# reads a script from a pipe in blocks, so a `while read` loop eats the rest of
# the script text. Measured: the statement after the loop never runs, at every
# payload size.)
#
# The redirect truncates and rewrites the SAME inode under the SAME name --
# never a delete or a rename.
ROT_SCRIPT = r"""set -u
TAG="$1"; SVC="$2"; ROOT="$3"; BACKUP="$4"; KEYFILE="$5"
BK="$BACKUP/$TAG/$SVC"
# The injection time of THIS service, from the host's own clock: the latency
# origin for `--attribute`, which the manifest-wide applied_at is not (it
# precedes the listing and its full-service sha256 pass).
printf 'ROTAT\t%s\t%s\n' "$SVC" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if ! mkdir -p "$BK"; then
  printf 'ROTERR\t%s\terror:backup_dir:%s\n' "$SVC" "$BK"
  rm -f "$KEYFILE"
  echo "STATUS=done"
  exit 0
fi
while IFS= read -r KEY; do
  [ -n "$KEY" ] || continue
  XY="${KEY: -2}"
  F="$ROOT/chunks/$XY/$KEY"
  if [ ! -f "$F" ]; then
    printf 'ROT\t%s\t%s\terror:absent_before_rot\n' "$SVC" "$KEY"
    continue
  fi
  if ! cp -p "$F" "$BK/$KEY" 2>/dev/null; then
    printf 'ROT\t%s\t%s\terror:backup_failed\n' "$SVC" "$KEY"
    continue
  fi
  if printf '%s' "__ROT_BYTES__" > "$F" 2>/dev/null; then
    printf 'ROT\t%s\t%s\tok\n' "$SVC" "$KEY"
  else
    printf 'ROT\t%s\t%s\terror:write_failed\n' "$SVC" "$KEY"
  fi
done < "$KEYFILE"
rm -f "$KEYFILE"
echo "STATUS=done"
""".replace("__ROT_BYTES__", ROT_BYTES)

# Args: tag, service, root, backup root, spec-list file holding `R:<key>` /
# `I:<key>` lines. Staged as a file for the same reason as the rot keys, and
# more pressingly: verify sends a service's rotted AND intact keys together.
VERIFY_SCRIPT = r"""set -u
TAG="$1"; SVC="$2"; ROOT="$3"; BACKUP="$4"; SPECFILE="$5"
BK="$BACKUP/$TAG/$SVC"
while IFS= read -r SPEC; do
  [ -n "$SPEC" ] || continue
  KIND="${SPEC%%:*}"; KEY="${SPEC#*:}"
  XY="${KEY: -2}"
  F="$ROOT/chunks/$XY/$KEY"
  if [ ! -f "$F" ]; then
    printf 'V\t%s\t%s\t%s\tfalse\tfalse\tfalse\t0\t-\n' "$SVC" "$KIND" "$KEY"
    continue
  fi
  SZ=$(stat -c %s "$F" 2>/dev/null || echo 0)
  if [ "$KIND" = "R" ]; then
    ROTBYTES=false
    if [ "$SZ" = "__ROT_LEN__" ] && [ "$(cat "$F")" = "__ROT_BYTES__" ]; then ROTBYTES=true; fi
    MB=false
    if cmp -s "$F" "$BK/$KEY"; then MB=true; fi
    printf 'V\t%s\t%s\t%s\ttrue\t%s\t%s\t%s\t-\n' "$SVC" "$KIND" "$KEY" "$ROTBYTES" "$MB" "$SZ"
  else
    SH=$(sha256sum "$F" 2>/dev/null | cut -d' ' -f1)
    printf 'V\t%s\t%s\t%s\ttrue\tfalse\tfalse\t%s\t%s\n' "$SVC" "$KIND" "$KEY" "$SZ" "$SH"
  fi
done < "$SPECFILE"
rm -f "$SPECFILE"
echo "STATUS=done"
""".replace("__ROT_LEN__", str(len(ROT_BYTES))).replace("__ROT_BYTES__", ROT_BYTES)


# -- helpers ---------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 to the second with a `Z`, the one format this tool writes."""
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(dt: datetime | None = None) -> str:
    """Filename-safe UTC stamp for the verify/attribute outputs."""
    return (dt or utc_now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _get(src: dict, dotted: str):
    if dotted in src:
        return src[dotted]
    cur = src
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def manifest_path(name: str, tag: str, out: str | None = None) -> Path:
    if out:
        return Path(out).expanduser()
    return Path(f"~/.autonomi/testnets/{name}/rot-chunks-{tag}.json").expanduser()


def sidecar_path(name: str, tag: str, kind: str, out: str | None, when: str) -> Path:
    if out:
        return Path(out).expanduser()
    return Path(f"~/.autonomi/testnets/{name}/rot-chunks-{tag}.{kind}-{when}.json").expanduser()


def root_dir_for(exec_start: str | None, service: str) -> str:
    """The service's root dir: `--root-dir` from the unit, else the default path."""
    if exec_start:
        m = ROOT_DIR_RE.search(exec_start)
        if m:
            return m.group(1).rstrip(";")
    return DEFAULT_ROOT_TEMPLATE.format(service=service)


def sample_keys(
    tag: str, hostname: str, service: str, keys: list[str], fraction: float
) -> list[str]:
    """The deterministic slice of a service's keys this tag rots.

    Seeded on the tag, the host and the service, so a re-run of the same tag
    picks the same keys and two hosts under one tag pick different ones.
    """
    ordered = sorted(keys)
    count = round(len(ordered) * fraction)
    count = max(0, min(count, len(ordered)))
    return sorted(random.Random(f"{tag}:{hostname}:{service}").sample(ordered, count))


def _safe(text: str) -> str:
    """A tag or service name reduced to what is safe in a remote path."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text) or "x"


def stage_lines(vm: hosts.Vm, lines: list[str], tag: str, service: str, kind: str) -> str | None:
    """Write a key list to a file on the VM; returns its path, or None on failure.

    Keys never travel on the command line -- see the note above `ROT_SCRIPT`.
    The remote script deletes the file when it is done with it.
    """
    path = f"/var/tmp/rot-chunks-{_safe(tag)}-{_safe(service)}.{kind}"
    res = remote.push_file(vm, "".join(f"{line}\n" for line in lines), path)
    return path if res.returncode == 0 else None


def _rows(res: remote.RemoteResult, marker: str) -> list[list[str]]:
    out = []
    for line in res.stdout.splitlines():
        parts = line.rstrip("\n").split("\t")
        if parts and parts[0] == marker:
            out.append(parts[1:])
    return out


def parse_probe(res: remote.RemoteResult) -> tuple[str | None, dict[str, str]]:
    hostname = None
    for parts in _rows(res, "HOST"):
        if parts:
            hostname = parts[0]
    execs = {p[0]: (p[1] if len(p) > 1 else "") for p in _rows(res, "EXEC") if p}
    return hostname, execs


def parse_listing(res: remote.RemoteResult) -> dict[str, dict]:
    """`service -> {"files": [{key, size, sha256}], "error": str | None}`."""
    listing: dict[str, dict] = {}
    for parts in _rows(res, "FILE"):
        if len(parts) < 4 or not KEY_RE.match(parts[1]):
            continue
        entry = listing.setdefault(parts[0], {"files": [], "error": None})
        try:
            size = int(parts[2])
        except ValueError:
            continue
        entry["files"].append({"key": parts[1], "size": size, "sha256": parts[3]})
    for parts in _rows(res, "SVCEND"):
        if parts:
            listing.setdefault(parts[0], {"files": [], "error": None})
    for parts in _rows(res, "SVCERR"):
        if len(parts) >= 2:
            listing[parts[0]] = {"files": [], "error": parts[1]}
    return listing


def parse_rot(res: remote.RemoteResult) -> tuple[dict[str, str], str | None, str | None]:
    """`key -> "ok" | "error:…"`, a service-wide error, and the host's rot time."""
    outcomes = {p[1]: p[2] for p in _rows(res, "ROT") if len(p) >= 3}
    svc_errors = [p[1] for p in _rows(res, "ROTERR") if len(p) >= 2]
    rotted_at = [p[1] for p in _rows(res, "ROTAT") if len(p) >= 2]
    return (
        outcomes,
        (svc_errors[0] if svc_errors else None),
        (rotted_at[0] if rotted_at else None),
    )


def parse_verify(res: remote.RemoteResult) -> dict[tuple[str, str], dict]:
    """`(service, key) -> the remote record`."""
    out: dict[tuple[str, str], dict] = {}
    for parts in _rows(res, "V"):
        if len(parts) < 8:
            continue
        service, kind, key, exists, rotted, matches, size, sha = parts[:8]
        try:
            size_i = int(size)
        except ValueError:
            size_i = 0
        out[(service, key)] = {
            "service": service,
            "kind": "rotted" if kind == "R" else "intact",
            "key": key,
            "exists": exists == "true",
            "is_rotted_bytes": rotted == "true",
            "matches_backup": matches == "true",
            "size": size_i,
            "sha256": None if sha == "-" else sha,
        }
    return out


def classify_rotted(rec: dict | None) -> str:
    if rec is None or not rec["exists"]:
        return "removed"
    if rec["is_rotted_bytes"]:
        return "rotted"
    if rec["matches_backup"]:
        return "repaired"
    return "other"


def classify_intact(rec: dict | None, expected: dict) -> str:
    if rec is None or not rec["exists"]:
        return "missing"
    if rec["size"] != expected["size"] or rec["sha256"] != expected["sha256"]:
        return "changed"
    return "unchanged"


# -- apply -----------------------------------------------------------------
def apply_host(
    vm: hosts.Vm, services: list[str], tag: str, fraction: float, min_files: int
) -> dict:
    """List, sample and rot every targeted service on one VM, in order."""
    host_rec: dict = {
        "ip": vm.ip,
        "hostname": None,
        "nat_simulated": vm.nat,
        "services": [],
        "error": None,
    }
    probe = remote.run_script(vm, PROBE_SCRIPT, services, timeout=60)
    hostname, execs = parse_probe(probe)
    if hostname is None:
        host_rec["error"] = (probe.stderr or probe.stdout).strip()[-400:] or "unreachable"
        host_rec["services"] = [
            {
                "service": svc,
                "root": None,
                "total_files": 0,
                "rotted": [],
                "intact": [],
                "rotted_at": None,
                "errors": None,
                "skipped": f"error:{host_rec['error']}",
            }
            for svc in services
        ]
        return host_rec
    host_rec["hostname"] = hostname

    roots = {svc: root_dir_for(execs.get(svc), svc) for svc in services}
    pairs: list[str] = []
    for svc in services:
        pairs.extend([svc, roots[svc]])
    listing_res = remote.run_script(vm, LIST_SCRIPT, pairs, timeout=300 * max(1, len(services)))
    listing = parse_listing(listing_res)
    if not listing:
        host_rec["error"] = (listing_res.stderr or listing_res.stdout).strip()[
            -400:
        ] or "no listing"

    for svc in services:
        entry = listing.get(svc)
        svc_rec: dict = {
            "service": svc,
            "root": roots[svc],
            "total_files": 0,
            "rotted": [],
            "intact": [],
            "rotted_at": None,
            "errors": None,
            "skipped": None,
        }
        if entry is None:
            svc_rec["skipped"] = f"error:{host_rec['error'] or 'no listing for service'}"
            host_rec["services"].append(svc_rec)
            continue
        if entry["error"]:
            svc_rec["skipped"] = f"error:{entry['error']}"
            host_rec["services"].append(svc_rec)
            continue
        files = {f["key"]: f for f in entry["files"]}
        svc_rec["total_files"] = len(files)
        if len(files) < min_files:
            # Below the floor nothing is touched at all -- the rot script is
            # never invoked for this service.
            svc_rec["skipped"] = f"too_few_files:{len(files)}"
            host_rec["services"].append(svc_rec)
            continue
        chosen = sample_keys(tag, hostname, svc, list(files), fraction)
        keyfile = stage_lines(vm, chosen, tag, svc, "keys")
        if keyfile is None:
            svc_rec["skipped"] = "error:could not stage the key list"
            host_rec["services"].append(svc_rec)
            continue
        rot_res = remote.run_script(
            vm,
            ROT_SCRIPT,
            [tag, svc, roots[svc], BACKUP_ROOT, keyfile],
            timeout=60 + 2 * len(chosen),
        )
        outcomes, svc_error, rotted_at = parse_rot(rot_res)
        svc_rec["rotted_at"] = rotted_at
        if svc_error:
            svc_rec["skipped"] = svc_error
            host_rec["services"].append(svc_rec)
            continue
        rotted = [k for k in chosen if outcomes.get(k) == "ok"]
        failed = {k: outcomes.get(k, "error:no_result") for k in chosen if k not in rotted}
        svc_rec["rotted"] = rotted
        # Everything not successfully rotted stays in the control set: it is
        # still an untouched file, and the verify pass must watch it as one.
        svc_rec["intact"] = [files[k] for k in sorted(files) if k not in set(rotted)]
        if failed:
            # A per-key failure is NOT a skip while other keys rotted: the
            # service was injected, and counting it as skipped would drop the
            # run's `injected` gate by one over a transient `cp`. The exit code
            # still goes to 1 from `errors` being set.
            svc_rec["errors"] = {k: v for k, v in sorted(failed.items())}
            if not rotted:
                svc_rec["skipped"] = "error:" + "; ".join(
                    f"{k[:8]}:{v}" for k, v in sorted(failed.items())
                )
        host_rec["services"].append(svc_rec)
    return host_rec


def cmd_apply(args: argparse.Namespace, console: Console) -> int:
    path = manifest_path(args.name, args.tag, args.out)
    if path.exists():
        console.print(
            f"[bold red]tag {args.tag} has already been applied: {path} exists[/bold red]\n"
            "Pick a new tag, or delete that manifest if the run it describes is over."
        )
        return 2
    if not args.ips:
        console.print("[bold red]--apply needs --ips: the target set is never implicit[/bold red]")
        return 2

    vms = hosts.select_vms(args.name, args.ips, args.inventory)
    started = utc_now()
    results = remote.run_parallel(
        vms,
        lambda vm: apply_host(
            vm,
            list(args.services) if args.services else list(vm.services),
            args.tag,
            args.fraction,
            args.min_files,
        ),
    )
    manifest = {
        "name": args.name,
        "tag": args.tag,
        "applied_at": iso(started),
        "finished_at": iso(utc_now()),
        "fraction": args.fraction,
        "min_files": args.min_files,
        "hosts": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))

    table = Table(title=f"rot-chunks apply tag={args.tag}")
    for col in ("host", "service", "total", "rotted", "intact", "skipped", "key errors"):
        table.add_column(col)
    injected = skipped = rotted_keys = services = key_errors = 0
    for host in results:
        for svc in host["services"]:
            services += 1
            if svc["skipped"]:
                skipped += 1
            else:
                injected += 1
            rotted_keys += len(svc["rotted"])
            key_errors += len(svc["errors"] or {})
            table.add_row(
                host["hostname"] or host["ip"],
                svc["service"],
                str(svc["total_files"]),
                str(len(svc["rotted"])),
                str(len(svc["intact"])),
                svc["skipped"] or "",
                str(len(svc["errors"] or {})) if svc["errors"] else "",
            )
    console.print(table)
    console.print(f"wrote {path}")
    # Plain print, not the console: the summary line is grepped by the run's
    # phase script and rich would soft-wrap it at the terminal width.
    print(
        f"ROT_APPLY tag={args.tag} hosts={len(results)} services={services} "
        f"injected={injected} skipped={skipped} rotted_keys={rotted_keys} "
        f"key_errors={key_errors}"
    )
    errored = [h["ip"] for h in results if h["error"]] + [
        h["ip"]
        for h in results
        if any((s["skipped"] or "").startswith("error:") or s["errors"] for s in h["services"])
    ]
    if errored:
        console.print(
            f"[bold red]{len(set(errored))} host(s) reported an error: "
            f"{', '.join(sorted(set(errored)))}[/bold red]"
        )
        return 1
    return 0


# -- verify ----------------------------------------------------------------
def verify_host(
    vm: hosts.Vm, host_rec: dict, tag: str
) -> tuple[dict[tuple[str, str], dict], str | None, set[str]]:
    """Re-read every manifest key of one host.

    Returns the records, any host error, and the services that were asked and
    answered nothing -- an unanswered service is NOT an empty chunk store, so
    its keys must not be classified from the absence of a record.
    """
    records: dict[tuple[str, str], dict] = {}
    error: str | None = None
    unanswered: set[str] = set()
    for svc in host_rec["services"]:
        specs = [f"R:{k}" for k in svc["rotted"]] + [f"I:{f['key']}" for f in svc["intact"]]
        if not specs or not svc["root"]:
            continue
        specfile = stage_lines(vm, specs, tag, svc["service"], "specs")
        if specfile is None:
            unanswered.add(svc["service"])
            error = "could not stage the key list"
            continue
        res = remote.run_script(
            vm,
            VERIFY_SCRIPT,
            [tag, svc["service"], svc["root"], BACKUP_ROOT, specfile],
            timeout=120 + 2 * len(specs),
        )
        parsed = parse_verify(res)
        if not parsed:
            error = (res.stderr or res.stdout).strip()[-400:] or "no verify records"
            unanswered.add(svc["service"])
        records.update(parsed)
    return records, error, unanswered


def classify_host(
    host_rec: dict,
    records: dict[tuple[str, str], dict],
    unanswered: set[str] | None = None,
) -> list[dict]:
    """Classify one host's manifest keys against what the host reported.

    A key of a service that answered nothing is `unknown`, never `removed` or
    `missing`: an unreachable host would otherwise fabricate a removal for
    every injected key and a loss for every control key.
    """
    unanswered = unanswered or set()
    rows = []
    host = host_rec["hostname"] or host_rec["ip"]
    for svc in host_rec["services"]:
        name = svc["service"]
        blind = name in unanswered
        for key in svc["rotted"]:
            rows.append(
                {
                    "host": host,
                    "service": name,
                    "key": key,
                    "kind": "rotted",
                    "class": "unknown" if blind else classify_rotted(records.get((name, key))),
                }
            )
        for f in svc["intact"]:
            rows.append(
                {
                    "host": host,
                    "service": name,
                    "key": f["key"],
                    "kind": "intact",
                    "class": "unknown"
                    if blind
                    else classify_intact(records.get((name, f["key"])), f),
                }
            )
    return rows


def verify_totals(rows: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["class"]] += 1
    rotted = sum(1 for r in rows if r["kind"] == "rotted")
    intact = sum(1 for r in rows if r["kind"] == "intact")
    return {
        "rotted_keys": rotted,
        "repaired": counts["repaired"],
        "removed": counts["removed"],
        "still_rotted": counts["rotted"],
        "other": counts["other"],
        "intact_keys": intact,
        "unchanged": counts["unchanged"],
        "changed": counts["changed"],
        "missing": counts["missing"],
        # Not read, as against read and found absent. Deliberately in no other
        # bucket: `repaired` + `removed` + `still_rotted` + `other` falls short
        # of `rotted_keys` by exactly the rotted keys nobody could read.
        "unknown": counts["unknown"],
    }


def cmd_verify(args: argparse.Namespace, console: Console) -> int:
    path = manifest_path(args.name, args.tag, args.out)
    if not path.exists():
        console.print(f"[bold red]no manifest at {path}[/bold red]")
        return 1
    manifest = json.loads(path.read_text())
    by_ip = {h["ip"]: h for h in manifest["hosts"]}
    vms = hosts.select_vms(args.name, list(by_ip), args.inventory)

    results = remote.run_parallel(vms, lambda vm: verify_host(vm, by_ip[vm.ip], args.tag))
    rows: list[dict] = []
    errors: list[str] = []
    unreachable: list[str] = []
    for vm, (records, error, unanswered) in zip(vms, results, strict=False):
        if error:
            errors.append(f"{vm.ip}: {error}")
        if unanswered:
            unreachable.append(vm.ip)
        rows.extend(classify_host(by_ip[vm.ip], records, unanswered))

    verified_at = iso(utc_now())
    totals = verify_totals(rows)
    out_path = sidecar_path(args.name, args.tag, "verify", args.json_out, stamp())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "tag": args.tag,
                "verified_at": verified_at,
                "keys": rows,
                "totals": totals,
                "unreachable_hosts": unreachable,
            },
            indent=2,
        )
    )

    table = Table(title=f"rot-chunks verify tag={args.tag}")
    for col in (
        "host",
        "service",
        "rotted",
        "repaired",
        "removed",
        "still rotted",
        "other",
        "intact",
        "unchanged",
        "changed",
        "missing",
        "unknown",
    ):
        table.add_column(col)
    per_service: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        per_service[(row["host"], row["service"])].append(row)
    for (host, service), group in sorted(per_service.items()):
        t = verify_totals(group)
        table.add_row(
            host,
            service,
            str(t["rotted_keys"]),
            str(t["repaired"]),
            str(t["removed"]),
            str(t["still_rotted"]),
            str(t["other"]),
            str(t["intact_keys"]),
            str(t["unchanged"]),
            str(t["changed"]),
            str(t["missing"]),
            str(t["unknown"]),
        )
    console.print(table)
    console.print(f"wrote {out_path}")
    print(
        f"ROT_VERIFY tag={args.tag} verified_at={verified_at} "
        f"rotted_keys={totals['rotted_keys']} repaired={totals['repaired']} "
        f"removed={totals['removed']} still_rotted={totals['still_rotted']} "
        f"other={totals['other']} intact={totals['intact_keys']} "
        f"unchanged={totals['unchanged']} changed={totals['changed']} "
        f"missing={totals['missing']} unknown={totals['unknown']} "
        f"unreachable_hosts={','.join(unreachable) if unreachable else '-'}"
    )
    if errors:
        console.print("[bold red]host error(s): " + "; ".join(errors) + "[/bold red]")
        return 1
    return 0


# -- attribute -------------------------------------------------------------
def fetch_docs(run: EsRun, start: str, end: str) -> dict[str, list[dict]]:
    """Export the five phrases FLEET-WIDE: the non-target hosts are the control."""
    fields = [TIMESTAMP_FIELD, HOST_SRC, MESSAGE_FIELD]
    return {
        kind: run.hits(
            [{"match_phrase": {MESSAGE_FIELD: phrase}}],
            start,
            end,
            index=run.node_index(),
            fields=fields,
        )
        for kind, phrase in PHRASES.items()
    }


def index_docs(docs: list[dict]) -> tuple[dict[tuple[str, str], datetime], dict[str, int]]:
    """`(HOST, key) -> earliest timestamp`, plus a per-host line count.

    Hosts are upper-cased on the way in because ES stores `tag.host` that way
    while the inventory and `hostname` do not.
    """
    first: dict[tuple[str, str], datetime] = {}
    per_host: dict[str, int] = defaultdict(int)
    for src in docs:
        ts_text = _get(src, TIMESTAMP_FIELD)
        host = _get(src, HOST_SRC)
        if not ts_text or not host:
            continue
        host = str(host).upper()
        per_host[host] += 1
        message = _get(src, MESSAGE_FIELD) or ""
        m = MESSAGE_KEY_RE.search(message)
        if not m:
            continue
        key = m.group(0).lower()
        ts = parse_ts(ts_text)
        cur = first.get((host, key))
        if cur is None or ts < cur:
            first[(host, key)] = ts
    return first, dict(per_host)


def attribution_for(detected: datetime | None, removed: datetime | None) -> str:
    if removed is not None:
        return "audit" if detected is not None and detected <= removed else "fetch"
    return "detected_only" if detected is not None else "none"


def injected_hosts(manifest: dict) -> list[str]:
    """The hosts something was actually injected on.

    A host skipped for `too_few_files` carries no rotted key, so counting it in
    the `targets_detected` denominator would read as a detection miss.
    """
    return [
        h["hostname"] or h["ip"]
        for h in manifest["hosts"]
        if any(s["skipped"] is None for s in h["services"])
    ]


def attribute_keys(manifest: dict, detections: dict, removals: dict) -> list[dict]:
    fallback = parse_ts(manifest["applied_at"]) if manifest.get("applied_at") else None
    rows = []
    for host_rec in manifest["hosts"]:
        host = host_rec["hostname"] or host_rec["ip"]
        es_host = str(host).upper()
        for svc in host_rec["services"]:
            # Latency is measured from THIS service's own injection, not from
            # the manifest's applied_at, which precedes the probe and the
            # full-service sha256 listing pass by however long those took.
            origin = parse_ts(svc["rotted_at"]) if svc.get("rotted_at") else fallback
            for key in svc["rotted"]:
                detected = detections.get((es_host, key.lower()))
                removed = removals.get((es_host, key.lower()))
                rows.append(
                    {
                        "host": host,
                        "service": svc["service"],
                        "key": key,
                        "detected_at": iso(detected),
                        "removed_at": iso(removed),
                        "attribution": attribution_for(detected, removed),
                        "_detected": detected,
                        "_removed": removed,
                        "_origin": origin,
                    }
                )
    return rows


def _service_stats(group: list[dict]) -> dict:
    detected = [r for r in group if r["_detected"] is not None]
    removed = [r for r in group if r["_removed"] is not None]
    latencies = [
        r["_detected"].timestamp() - r["_origin"].timestamp()
        for r in detected
        if r.get("_origin") is not None
    ]
    latency = round(min(latencies), 1) if latencies else None
    gaps = [
        (r["_removed"] - r["_detected"]).total_seconds()
        for r in group
        if r["_detected"] is not None and r["_removed"] is not None
    ]
    return {
        "rotted": len(group),
        "detected": len(detected),
        "removed": len(removed),
        "audit": sum(1 for r in group if r["attribution"] == "audit"),
        "fetch": sum(1 for r in group if r["attribution"] == "fetch"),
        "detected_only": sum(1 for r in group if r["attribution"] == "detected_only"),
        "none": sum(1 for r in group if r["attribution"] == "none"),
        "first_detection_latency_s": latency,
        "median_detect_to_remove_s": round(statistics.median(gaps), 1) if gaps else None,
    }


def nontarget_removals(per_host: dict[str, int], target_hosts: list[str]) -> dict:
    """Removal lines on hosts the manifest never touched — the control reading."""
    variants = {v.upper() for v in host_variants(list(target_hosts))}
    others = {h: n for h, n in per_host.items() if h.upper() not in variants}
    return {"count": sum(others.values()), "hosts": dict(sorted(others.items()))}


def cmd_attribute(args: argparse.Namespace, console: Console) -> int:
    path = manifest_path(args.name, args.tag, args.out)
    if not path.exists():
        console.print(f"[bold red]no manifest at {path}[/bold red]")
        return 1
    manifest = json.loads(path.read_text())
    start = args.start or manifest["applied_at"]
    end = args.end or iso(utc_now())

    run = EsRun(args.name)
    docs = fetch_docs(run, start, end)
    indexed = {kind: index_docs(rows) for kind, rows in docs.items()}
    detections, _ = indexed["detection"]
    removals, removal_hosts = indexed["removal"]

    rows = attribute_keys(manifest, detections, removals)
    target_hosts = [h["hostname"] or h["ip"] for h in manifest["hosts"]]
    per_service: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        per_service[(row["host"], row["service"])].append(row)

    table = Table(title=f"rot-chunks attribute tag={args.tag}")
    for col in (
        "host",
        "service",
        "rotted",
        "detected",
        "removed",
        "audit",
        "fetch",
        "detected only",
        "none",
        "first detect s",
        "median d→r s",
    ):
        table.add_column(col)
    for (host, service), group in sorted(per_service.items()):
        s = _service_stats(group)
        table.add_row(
            host,
            service,
            str(s["rotted"]),
            str(s["detected"]),
            str(s["removed"]),
            str(s["audit"]),
            str(s["fetch"]),
            str(s["detected_only"]),
            str(s["none"]),
            "-"
            if s["first_detection_latency_s"] is None
            else f"{s['first_detection_latency_s']:.1f}",
            "-"
            if s["median_detect_to_remove_s"] is None
            else f"{s['median_detect_to_remove_s']:.1f}",
        )
    console.print(table)

    per_target = []
    for host in target_hosts:
        es_host = str(host).upper()
        per_target.append(
            {
                "host": host,
                **{kind: indexed[kind][1].get(es_host, 0) for kind in PHRASES},
            }
        )
    phrase_table = Table(title="phrase counts per target host")
    phrase_table.add_column("host")
    for kind in PHRASES:
        phrase_table.add_column(kind)
    for entry in per_target:
        phrase_table.add_row(entry["host"], *[str(entry[kind]) for kind in PHRASES])
    console.print(phrase_table)

    control = nontarget_removals(removal_hosts, target_hosts)
    totals = _service_stats(rows)
    injected = injected_hosts(manifest)
    detected_hosts = {r["host"] for r in rows if r["_detected"] is not None}
    payload_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    out_path = sidecar_path(args.name, args.tag, "attribute", args.json_out, stamp())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "tag": args.tag,
                "window": {"start": start, "end": end},
                "keys": payload_rows,
                "per_target": per_target,
                "injected_hosts": injected,
                "nontarget_removals": control,
                "totals": totals,
            },
            indent=2,
        )
    )

    console.print(
        f"non-target removal lines: {control['count']} "
        f"on {len(control['hosts'])} host(s) outside the manifest"
    )
    console.print(f"wrote {out_path}")
    overflow = sum(indexed["overflow"][1].values())
    not_removable = sum(indexed["not_removable"][1].values())
    print(
        f"ROT_ATTRIBUTE tag={args.tag} window={start}..{end} "
        f"rotted_keys={totals['rotted']} detected={totals['detected']} "
        f"removed={totals['removed']} audit={totals['audit']} fetch={totals['fetch']} "
        f"detected_only={totals['detected_only']} none={totals['none']} "
        f"targets_detected={len(detected_hosts)}/{len(injected)} "
        f"overflow_lines={overflow} not_removable_lines={not_removable} "
        f"nontarget_removals={control['count']}"
    )
    return 0


def cmd_rot_chunks(args: argparse.Namespace) -> int:
    console = Console()
    if args.apply:
        return cmd_apply(args, console)
    if args.verify:
        return cmd_verify(args, console)
    return cmd_attribute(args, console)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "rot-chunks",
        help=(
            "Corrupt a deterministic sample of chunk files on the given node VMs "
            "(in place, never deleted), then verify from disk what happened to each key "
            "and attribute each removal to an audit detection or to a fetch."
        ),
    )
    add_deployment_args(p)
    # --ips is optional here because only --apply targets hosts directly; verify
    # and attribute take their host set from the manifest the apply wrote.
    add_host_selection_args(p, require_ips=False)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="Rot a sample of chunk files.")
    mode.add_argument(
        "--verify", action="store_true", help="Re-read the manifest's keys from disk."
    )
    mode.add_argument(
        "--attribute", action="store_true", help="Join the manifest with the node log index."
    )
    p.add_argument("--tag", required=True, help="Names the manifest and seeds the sample.")
    p.add_argument(
        "--services",
        nargs="+",
        default=None,
        help="Services to rot on each VM (default: every service in the inventory entry).",
    )
    p.add_argument(
        "--fraction",
        type=float,
        default=0.5,
        help="Fraction of each service's chunk files to rot (default 0.5).",
    )
    p.add_argument(
        "--min-files",
        type=int,
        default=100,
        help="Leave a service alone below this many chunk files (default 100).",
    )
    p.add_argument("--start", default=None, help="--attribute window start (default: applied_at).")
    p.add_argument("--end", default=None, help="--attribute window end (default: now).")
    p.add_argument(
        "--out",
        default=None,
        help="Manifest path (default ~/.autonomi/testnets/<name>/rot-chunks-<tag>.json).",
    )
    p.add_argument(
        "--json-out",
        default=None,
        help="Verify/attribute report path (default: alongside the manifest, UTC-stamped).",
    )
    p.set_defaults(func=cmd_rot_chunks)
