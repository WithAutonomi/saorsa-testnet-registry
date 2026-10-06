"""`saorsa-deploy analysis` — the command group for run-time measurement.

Each module under :mod:`saorsa_deploy.analysis` registers its own subcommands
via a ``register(subparsers)`` function and sets ``func`` on its parser, so the
hub here only knows the list of modules and the argument helpers they share.
"""

from __future__ import annotations

import argparse
import importlib

# Order is the order `--help` lists them in: remote drivers first, then the
# ES/InfluxDB extractors, then the offline pcap analysers.
ANALYSIS_MODULES = (
    "saorsa_deploy.analysis.snapshot",
    "saorsa_deploy.analysis.capture",
    "saorsa_deploy.analysis.restart_nodes",
    "saorsa_deploy.analysis.isolate",
    "saorsa_deploy.analysis.stall_probe",
    "saorsa_deploy.analysis.rot_chunks",
    "saorsa_deploy.analysis.es",
    "saorsa_deploy.analysis.pointer_commitments",
    "saorsa_deploy.analysis.relay",
    "saorsa_deploy.analysis.phase_extract",
    "saorsa_deploy.analysis.recovery",
    "saorsa_deploy.analysis.flowstat",
    "saorsa_deploy.analysis.backstop",
    "saorsa_deploy.analysis.probe_score",
    "saorsa_deploy.analysis.close_delivery",
)


def add_deployment_args(parser: argparse.ArgumentParser) -> None:
    """``--name`` / ``--inventory`` — every command that touches a deployment."""
    parser.add_argument(
        "--name",
        required=True,
        help="Deployment name (e.g. DEV-01). Hosts, NAT tier and SSH key come from its inventory.",
    )
    parser.add_argument(
        "--inventory",
        default=None,
        help="Inventory path override (default: ~/.autonomi/testnets/<name>/inventory.json).",
    )


def add_host_selection_args(parser: argparse.ArgumentParser, require_ips: bool = False) -> None:
    """``--ips`` and ``--tier`` — which node VMs a remote command targets."""
    parser.add_argument(
        "--ips",
        nargs="+",
        default=None,
        required=require_ips,
        help="Node VM IPs to target (must be in the inventory).",
    )
    if not require_ips:
        parser.add_argument(
            "--tier",
            choices=["all", "nat", "plain"],
            default="all",
            help="When --ips is omitted: every node VM, or only the NAT'd / plain tier.",
        )


def add_window_args(parser: argparse.ArgumentParser) -> None:
    """``--start`` / ``--end`` — an ISO-8601 UTC window for ES/InfluxDB queries."""
    parser.add_argument(
        "--start", required=True, help="Window start, ISO-8601 UTC (e.g. 2026-08-25T10:00:00Z)."
    )
    parser.add_argument("--end", required=True, help="Window end, ISO-8601 UTC.")


def add_analysis_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "analysis",
        help=(
            "Run-time measurement for testnet runs: fleet counter snapshots, packet "
            "captures, cohort restarts and network cuts, stall probes, ES/InfluxDB phrase "
            "and counter extraction, and offline pcap analysers."
        ),
    )
    sub = parser.add_subparsers(dest="analysis_subcommand", required=True)
    for dotted in ANALYSIS_MODULES:
        importlib.import_module(dotted).register(sub)


def dispatch_analysis(args: argparse.Namespace) -> None:
    func = getattr(args, "func", None)
    if func is None:
        raise SystemExit("analysis: no subcommand handler registered")
    result = func(args)
    if isinstance(result, int) and result != 0:
        raise SystemExit(result)
