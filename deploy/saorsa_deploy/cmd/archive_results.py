"""Archive a deployment's InfluxDB results into Postgres for long-term history.

``deployments archive`` is a thin convenience wrapper over
``deployments record-results``. A permanent ``prod continuous-downloader`` runs
with a bounded InfluxDB retention (~7 days), but its per-op download records
should be retained far longer; archiving copies them into Postgres
(``deployment_download_records`` — idempotent, full-range) so history survives
the Influx expiry. The command is meant to be run from the machine that owns the
registry database (``DATABASE_URL`` set) and can reach the monitoring VM's
InfluxDB.

Two modes:

* **one-shot** (default): run a single ``record-results`` pass and exit. Ideal
  for an external scheduler (cron, a Claude ``/schedule`` cloud agent).
* **--loop**: keep archiving forever, sleeping ``--interval-secs`` between runs.
  A transient failure in one iteration is logged and the loop continues, so a
  brief InfluxDB blip never tears down a long-running archiver.
"""

import time
from types import SimpleNamespace

from rich.console import Console

from saorsa_deploy.cmd.record_results import cmd_record_results


def _record_args(args) -> SimpleNamespace:
    """Build the argument namespace ``cmd_record_results`` expects."""
    return SimpleNamespace(
        name=args.name,
        monitoring_name=args.monitoring_name,
        influx_url=args.influx_url,
        influx_token=args.influx_token,
        influx_org=args.influx_org,
        influx_bucket=args.influx_bucket,
        window=args.window,
        update_slack=getattr(args, "update_slack", False),
    )


def cmd_archive_results(args):
    console = Console()
    record_args = _record_args(args)

    if not getattr(args, "loop", False):
        # One-shot: delegate directly so record-results' own exit codes
        # propagate to the external scheduler.
        cmd_record_results(record_args)
        return

    interval = max(1, getattr(args, "interval_secs", 21600))
    console.print(
        f"[bold]deployments archive: looping every {interval}s for "
        f"'{args.name}' (Ctrl-C to stop)[/bold]"
    )
    while True:
        try:
            cmd_record_results(record_args)
        except SystemExit as e:
            # record-results exits non-zero on transient problems (Influx
            # unreachable, no points yet). In loop mode that must not kill the
            # archiver — log and retry on the next tick.
            console.print(
                f"[yellow]archive: record-results exited ({e.code}); "
                f"retrying in {interval}s.[/yellow]"
            )
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            console.print(
                f"[yellow]archive: record-results raised ({e}); retrying in {interval}s.[/yellow]"
            )
        time.sleep(interval)
