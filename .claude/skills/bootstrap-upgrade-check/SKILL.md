---
name: bootstrap-upgrade-check
description: SSH into every bootstrap VM listed in a testnet's `bootstrap-inventory.json` and report whether each node has already auto-upgraded to a target version, is scheduled to restart, or has logged any failures during the upgrade process. Use this skill whenever the user asks to "check the bootstraps", "see if the bootstraps upgraded", "survey bootstrap nodes", "check bootstrap upgrade status", "verify bootstrap rollout", or otherwise wants to know the current upgrade state of the bootstrap-tier VMs (the ones in `bootstrap-inventory.json`, not the regular nodes in `inventory.json`). The track-upgrades SQLite database does NOT cover bootstraps — they have a different inventory file, a different systemd service name (`ant-bootstrap.service`), a different log directory layout (`/mnt/ant-node-storage/logs/ant-bootstrap/`, note the plural "logs"), and different SSH credentials per-host — so handling them needs its own pass. Prefer this skill over hand-rolling SSH loops; it bakes in the per-host quirks and the right grep tactics for the multi-hundred-MB current-day log files.
---

# bootstrap-upgrade-check

Survey every VM in a `bootstrap-inventory.json` file via SSH, look for auto-upgrade lifecycle events in the on-disk ant-node logs, and emit a Markdown table summarising each host's state (currently-running version, last scheduled-restart event, any errors).

## Why this skill exists

The `track-upgrades` SQLite tracker only covers the regular node tier — the VMs in `node_vms[]` of the main `inventory.json`. Bootstrap nodes live in a separate `bootstrap-inventory.json`, run a different systemd unit, log to a different directory, and (for the production network) use a different SSH key. As a result, `track-upgrades summary` shows nothing about them, and a rollout's bootstrap status has to be checked some other way.

The hard-won quirks this skill bakes in:

- **Service name is `ant-bootstrap.service`**, not `ant-node`. Naive `systemctl is-active ant-node` always reports `inactive` even on a perfectly healthy host.
- **Binary path is `/opt/ant-node/ant-node`**, not `/usr/local/bin/antnode`. `--version` works and is fast.
- **Log directory is `/mnt/ant-node-storage/logs/ant-bootstrap/`** (plural `logs`, singular bootstrap subdir). Different from the regular-node layout, which uses `/mnt/ant-node-storage/log/ant-node-N/`.
- **Today's log can be huge** (we've seen 700+ MB for a single host on a busy day). `grep` across a `*.log*` glob is too slow and times out SSH commands. Always use `tac <newest-log> | grep -m1` to read backwards from the end and stop on the first match — that's instant regardless of file size.
- **Per-host SSH credentials.** Bootstrap inventories have `ssh_user` and `ssh_key` per VM. AWS bootstraps use `ubuntu`; others use `root`. Don't hard-code; read the per-VM fields.
- **The `"ant-node starting"` log line is the definitive "I am running version X" signal.** The `tail.version` field on that line is what matters. The `tag.node_version` Telegraf stamps onto every log is *static at provision time* and will lie after an auto-upgrade.

## When to use

Trigger on phrases like:

- "Check the bootstraps"
- "Survey the bootstrap nodes"
- "Have the bootstraps upgraded to X.Y.Z?"
- "Bootstrap rollout status"
- "Check bootstrap-inventory upgrade state"

Also use this proactively after running `track-upgrades-setup` for a rollout: the user almost always wants the bootstrap status alongside the regular-node summary, and the tracker DB doesn't include them.

## What this skill does NOT do

- It does not write or modify anything on the bootstrap hosts. Read-only.
- It does not generate a runbook. The check is fast (a few seconds per host), runs live, and reports the result inline. There's no benefit to deferring it to a script.
- It does not cover regular nodes. Use `track-upgrades-setup` + `track-upgrades summary` for those.

## Inputs

Ask the user for any of these that aren't obvious from context:

1. **Bootstrap inventory path** — usually `~/.autonomi/testnets/<NAME>/bootstrap-inventory.json` for a deployment named `<NAME>`. Read the file with `cat` first to extract the VM list and per-host credentials.
2. **Target version** (optional but recommended) — the version every bootstrap is expected to upgrade to. Used in the output table to mark each host as upgraded / scheduled / pending. If omitted, the table just reports the *current* running version per host without classifying it.

A typical invocation has both pieces visible from the conversation already. Don't ask if you can read them off context.

## Procedure

1. **Read the inventory** with the `Read` tool and extract `node_vms[].{name, ip, ssh_user, ssh_key}` for every bootstrap.
2. **Run the per-host probe in parallel** via a single bash loop. Use a small embedded shell snippet that runs each probe with a hard `timeout 30` — it's cheap, and one slow host shouldn't wedge the survey.
3. **Format the result as a Markdown table** with columns:
   `Region | Status | Version | Detail` — the same shape this skill's prior runs in conversation have used and the user is comfortable with.
4. **Highlight anomalies** in a follow-up paragraph below the table — failures, hosts that should have restarted by now but haven't, any service that is `inactive` when the others are `active`.

## Per-host probe template

This is the canonical command shape. Use it verbatim — the cumulative experience from this thread is encoded in the choices.

```bash
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
    "$SSH_USER@$IP" '
F=$(ls -t /mnt/ant-node-storage/logs/ant-bootstrap/*.log 2>/dev/null | head -1)
echo "service:  $(systemctl is-active ant-bootstrap.service)"
echo "since:    $(systemctl show ant-bootstrap.service --property=ActiveEnterTimestamp --value)"
echo "binary:   $(/opt/ant-node/ant-node --version 2>&1 | head -1)"
echo "startup:  $(tac "$F" 2>/dev/null | grep -m1 "ant-node starting")"
echo "schedule: $(tac "$F" 2>/dev/null | grep -m1 "Node will stop/restart")"
echo "errors:   $(grep -hcE "Binary replacement failed|Download failed|CRITICAL.*Replacement failed" "$F" 2>/dev/null || echo 0)"
'
```

Wrap each invocation in `timeout 30 ssh ...` so a flaky host doesn't block the rest. Don't use `pgrep` to wait — the bash loop's own timeout per host is sufficient.

If the user has a lot of bootstraps (10+) and survey latency matters, parallelise via `&` + `wait`; for the typical 7-bootstrap prod inventory, sequential is fine and the output ordering matches the inventory.

## Interpreting the probe output

- `service: active` + `binary: ant-node <to-version>` + `startup` line shows `version=<to-version>` → **upgraded**.
- `service: active` + `binary: ant-node <from-version>` + `schedule` line in the future → **scheduled**.
- `service: active` + `binary: ant-node <from-version>` + `schedule` line in the past → **stuck** (the auto-upgrade fired but didn't complete; flag this).
- `service: inactive` → **service down**; this is always worth flagging, even if the upgrade-tracking question wasn't about availability.
- `errors: > 0` → flag with the actual error grep output.

The `startup` line's timestamp tells you when the host last restarted — useful for spotting hosts that restarted at the scheduled time vs ones that have been running for weeks unchanged.

## Output format

Always emit a single Markdown table. Use these columns and emoji prefixes for status:

```
| Region | Status         | Version | Detail                              |
|--------|----------------|---------|-------------------------------------|
| ash    | ✅ upgraded    | 0.11.1  | restarted 09:05:26Z                 |
| nrt    | ✅ upgraded    | 0.11.1  | restarted 12:59:01Z                 |
| lax    | 🕐 scheduled   | 0.11.0 → 0.11.1 | restart at 15:56:12Z (~2h50)|
| syd    | ⚠ stuck        | 0.11.0 | scheduled 12:00Z, no restart yet     |
```

Below the table, a short prose summary: "X/Y upgraded, Z scheduled, N failures." Mention errors only if any host had `errors > 0` or was flagged stuck.

## Common pitfalls

- **Globbing `*.log*` on bootstraps is slow.** Today's log alone can be hundreds of MB; rotated archives multiply that. Always `ls -t | head -1` to pick the newest file, then `tac | grep -m1` to read backwards. Survey time per host should be a few seconds, not 30+.
- **Don't query Elasticsearch for bootstrap status.** Telegraf is not provisioned on bootstrap hosts (the `provision-telegraf` step in `track-upgrades-setup` reads from the regular-node inventory, not the bootstrap one). The only source of truth for bootstrap upgrade state is the on-disk ant-node log.
- **AWS bootstraps need `ubuntu` user, not `root`.** The inventory's `ssh_user` field is correct per-host; just use it. Don't hard-code `root@`.
- **`/usr/local/bin/antnode --version` will fail** with "command not found" on bootstrap hosts. The binary is at `/opt/ant-node/ant-node`. Don't fall back to other paths from regular nodes.
- **The `tag.node_version` in any Elasticsearch logs from these hosts is unreliable post-upgrade.** It's the value passed at telegraf-provision time, not the actual binary running. The on-disk log's `"ant-node starting"` line carries the true version in its `version` field.
- **Don't auto-restart stuck services.** Flag and stop. The user decides whether to intervene; an upgrade tracker should never "fix" infrastructure on its own.
