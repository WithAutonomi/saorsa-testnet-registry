"""`saorsa-deploy clients estimate-gas` — size uploader wallet funding.

Read-only preflight. It answers one question: at today's gas price, will the
`eth:` figures in this manifest carry the uploaders through a full run?

It exists because they did not on staging run #566. The Arbitrum Sepolia base
fee had risen ~10x in three days, six of ten uploaders exhausted their wallets
from ~T0+3h57m, and every subsequent upload failed. Whole-run upload success
came out at 96.02% and the mixed-version phase at 79.35% — both below their
hard-gate floors, for a reason with nothing to do with the release under test.
Nothing warned beforehand, and mid-run the failure is indistinguishable from a
network fault.

The arithmetic lives in ``saorsa_deploy.gas_model``; this module does the I/O:
fetch the base fee, read the previous run's throughput out of Postgres, read
the manifest, print, and set the exit code.
"""
from __future__ import annotations

import math
import os
import sys
from typing import Optional

from rich.console import Console
from rich.table import Table

from saorsa_deploy.evm import get_evm_network_info
from saorsa_deploy.gas_model import (
    ClientRun,
    GasModelError,
    UploaderPlan,
    estimate_funding,
    load_gas_model,
    project_throughput,
    rate_for_size,
)
from saorsa_deploy.registry import get_repository


def _fetch_base_fee_gwei(rpc_url: str) -> float:
    """Latest block's base fee, in gwei.

    Base fee rather than the effective price actually paid: the model is
    calibrated against base fee so the priority fee and the estimator's
    buffer cancel in the ratio. See the model config for why.
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    block = w3.eth.get_block("latest")
    base_fee = block.get("baseFeePerGas")
    if base_fee is None:
        raise RuntimeError(
            f"{rpc_url} returned a block with no baseFeePerGas; cannot size "
            "funding against a pre-EIP-1559 chain."
        )
    return base_fee / 1e9


def _fetch_funder_balance_eth(rpc_url: str) -> Optional[float]:
    """Native balance of the wallet that funds the uploaders, if resolvable."""
    key = os.environ.get("ANT_DEPLOY_SECRET_KEY")
    if not key:
        return None
    try:
        from eth_account import Account
        from web3 import Web3

        address = Account.from_key(
            key if key.startswith("0x") else "0x" + key
        ).address
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        return w3.eth.get_balance(address) / 1e18
    except Exception:
        return None


def _read_baseline_clients(repo, deployment_id: int):
    """Fetch one run's per-client successful-upload shape.

    I/O only — the projection arithmetic lives in ``gas_model`` so it can be
    tested without a database.
    """
    from sqlalchemy import text

    session = getattr(repo, "session", None)
    if session is None:
        raise RuntimeError("repository exposes no session for the rate query")
    rows = session.execute(
        text(
            """
            SELECT client_hostname AS host,
                   COUNT(*) AS n,
                   MIN(finished_at) AS first_at,
                   MAX(finished_at) AS last_at,
                   MAX(file_size_bytes) AS size_bytes
            FROM deployment_upload_records
            WHERE deployment_id = :did AND status = 'success'
            GROUP BY client_hostname
            """
        ),
        {"did": deployment_id},
    ).all()
    if not rows:
        return [], 0.0
    span_lo = min(r.first_at for r in rows)
    span_hi = max(r.last_at for r in rows)
    run_hours = (span_hi - span_lo).total_seconds() / 3600.0
    clients = [
        ClientRun(
            host=r.host,
            uploads=r.n,
            active_hours=(r.last_at - r.first_at).total_seconds() / 3600.0,
            size_gb=r.size_bytes / 1e9,
        )
        for r in rows
    ]
    return clients, run_hours


def cmd_estimate_client_gas(args) -> None:
    console = Console(width=140)

    try:
        model = load_gas_model(getattr(args, "model_path", None))
    except GasModelError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    from saorsa_deploy.config import ManifestError, load_manifest

    try:
        manifest = load_manifest(
            args.config,
            sets=getattr(args, "set", None),
            name=getattr(args, "name", None),
        )
    except ManifestError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    testnet = getattr(manifest, "testnet", None)
    clients = getattr(testnet, "clients", None) if testnet else None
    uploaders = list(getattr(clients, "uploaders", []) or []) if clients else []
    if not uploaders:
        console.print(
            "[bold red]Error:[/bold red] this manifest declares no "
            "testnet.clients.uploaders, so there are no wallets to fund."
        )
        sys.exit(1)

    network = getattr(args, "network", None) or "arbitrum-sepolia"
    try:
        rpc_url = get_evm_network_info(network)["rpc_url"]
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    base_fee = getattr(args, "base_fee_gwei", None)
    if base_fee is None:
        try:
            base_fee = _fetch_base_fee_gwei(rpc_url)
        except Exception as exc:
            console.print(
                f"[bold red]Error:[/bold red] could not read the base fee "
                f"from {rpc_url}: {exc}\nPass --base-fee-gwei to size against "
                "a figure you supply."
            )
            sys.exit(1)

    by_size: dict = {}
    baseline_hours = 0.0
    baseline_id = getattr(args, "baseline_id", None)
    if baseline_id is not None:
        repo = get_repository()
        if repo is None:
            console.print(
                "[bold red]Error:[/bold red] DATABASE_URL is not set or the "
                "database is unreachable, so --baseline-id cannot be used."
            )
            sys.exit(1)
        try:
            clients_seen, baseline_hours = _read_baseline_clients(
                repo, baseline_id
            )
            throughput = project_throughput(clients_seen, baseline_hours)
            by_size = throughput.rate_by_size_gb
            for c in throughput.truncated:
                console.print(
                    f"[yellow]note:[/yellow] {c.host} was active for only "
                    f"{c.active_hours:.2f}h of the baseline's "
                    f"{throughput.run_hours:.2f}h ({c.uploads} uploads). Its "
                    "rate is projected across the full run, which is what a "
                    "funded client would have done."
                )
        except Exception as exc:
            console.print(
                f"[bold red]Error:[/bold red] could not read throughput for "
                f"deployment {baseline_id}: {exc}"
            )
            sys.exit(1)
        if not by_size:
            console.print(
                f"[bold red]Error:[/bold red] deployment {baseline_id} has no "
                "successful upload records. Pass --uploads-per-hour, or a "
                "baseline that ran."
            )
            sys.exit(1)

    duration = getattr(args, "duration_hours", None) or baseline_hours or 5.0
    flat_rate = getattr(args, "uploads_per_hour", None)

    plans = []
    unmatched = []
    for up in uploaders:
        size_gb = (getattr(up, "file_size_mb", 0) or 0) / 1000.0
        rate = (
            flat_rate
            if flat_rate is not None
            else rate_for_size(size_gb, by_size)
        )
        if rate is None:
            unmatched.append(up)
            continue
        current = getattr(up, "eth", None)
        plans.append(
            UploaderPlan(
                name=f"{getattr(up, 'region', '?')}/{getattr(up, 'file_size_mb', 0)}MB",
                size_gb=size_gb,
                expected_uploads=int(math.ceil(rate * duration)),
                payment=getattr(up, "payment", None),
                current_eth=float(current) if current is not None else None,
            )
        )

    if unmatched:
        console.print(
            "[yellow]warning:[/yellow] no comparable baseline client for "
            + ", ".join(
                f"{getattr(u, 'region', '?')}/{getattr(u, 'file_size_mb', 0)}MB"
                for u in unmatched
            )
            + ". These are NOT sized below — supply --uploads-per-hour or a "
            "baseline that ran the same file sizes."
        )

    if not plans:
        console.print(
            "[bold red]Error:[/bold red] no uploader could be sized."
        )
        sys.exit(1)

    funder = getattr(args, "funder_balance_eth", None)
    if funder is None:
        funder = _fetch_funder_balance_eth(rpc_url)

    estimate = estimate_funding(plans, base_fee, model, funder_balance_eth=funder)

    table = Table(
        title=(
            f"Uploader gas funding — {network} @ {base_fee:.4f} gwei "
            f"({estimate.price_ratio:.2f}x the model's reference), "
            f"model v{model.version}"
        )
    )
    table.add_column("Uploader")
    table.add_column("Size", justify="right")
    table.add_column("Uploads", justify="right")
    table.add_column("Modelled", justify="right")
    table.add_column("Recommend", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Verdict")
    for u in estimate.uploaders:
        if u.adequate is None:
            verdict, cur = "[dim]no current value[/dim]", "-"
        elif u.adequate:
            verdict, cur = "[green]ok[/green]", f"{u.current_eth:g}"
        else:
            verdict = f"[red]short {u.shortfall_eth:.2f}[/red]"
            cur = f"{u.current_eth:g}"
        table.add_row(
            u.plan.name,
            f"{u.plan.size_gb*1000:.0f} MB",
            str(u.plan.expected_uploads),
            f"{u.modelled_eth:.3f}",
            f"{u.recommended_eth:g}",
            cur,
            verdict,
        )
    console.print(table)

    console.print(
        f"Sized for a [bold]{duration:.2f}h[/bold] run at "
        f"[bold]{base_fee:.4f} gwei[/bold], safety factor "
        f"{estimate.safety_factor:g}x."
    )
    if baseline_id is not None:
        console.print(
            f"Throughput projected from deployment [bold]{baseline_id}[/bold] "
            "(per-client rate over its active window, not its raw counts)."
        )
    console.print(
        f"Total to fund: [bold]{estimate.total_recommended_eth:g} ETH[/bold]"
        + (
            f" (currently {estimate.total_current_eth:g} ETH)"
            if estimate.total_current_eth is not None
            else ""
        )
    )

    inadequate = [u for u in estimate.uploaders if u.adequate is False]
    exit_code = 0

    if funder is not None:
        if estimate.funder_covers:
            console.print(
                f"Funding wallet holds {funder:.4f} ETH — covers it."
            )
        else:
            console.print(
                f"[bold red]Funding wallet holds {funder:.4f} ETH, short of "
                f"the {estimate.total_recommended_eth:g} ETH needed.[/bold red] "
                "Top-up is per-wallet and sequential, so this does not fail "
                "cleanly up front: it funds the first few uploaders and then "
                "runs dry, leaving a half-provisioned run."
            )
            exit_code = 2
    else:
        console.print(
            "[yellow]Funding wallet balance not checked[/yellow] "
            "(ANT_DEPLOY_SECRET_KEY unset). Confirm it holds "
            f"{estimate.total_recommended_eth:g} ETH before deploying."
        )

    if inadequate:
        console.print(
            f"\n[bold red]{len(inadequate)} uploader(s) under-funded at this "
            "gas price.[/bold red] Set these in the profile's "
            "clients.uploaders, in order:"
        )
        for u in estimate.uploaders:
            console.print(f"  {u.plan.name:<20} eth: {u.recommended_eth:g}")
        exit_code = 2
    elif not inadequate and all(u.adequate is not None for u in estimate.uploaders):
        console.print("[green]Current funding is adequate.[/green]")

    sys.exit(exit_code)
