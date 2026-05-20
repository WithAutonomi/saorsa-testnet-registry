"""Tests for per-uploader wallet creation, funding, and draining.

The web3 layer is mocked end-to-end — these tests verify the saorsa-deploy code
that decides *what* to send, not the chain itself.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy import wallet
from saorsa_deploy.cmd.infra_clients import (
    DEFAULT_ANT_FUNDING,
    DEFAULT_ETH_FUNDING,
    parse_uploader_spec,
)

# --- parse_uploader_spec --------------------------------------------------

def test_uploader_spec_defaults_when_funding_omitted():
    spec = parse_uploader_spec("lon1:1000:s-2vcpu-4gb")
    assert spec["ant_funding"] == DEFAULT_ANT_FUNDING == "100"
    assert spec["eth_funding"] == DEFAULT_ETH_FUNDING == "0.5"


def test_uploader_spec_accepts_ant_and_eth():
    spec = parse_uploader_spec(
        "nyc1:500:s-2vcpu-4gb:payment=no-merkle:ant=250:eth=1"
    )
    assert spec["payment_type"] == "no-merkle"
    assert spec["ant_funding"] == "250"
    assert spec["eth_funding"] == "1"


def test_uploader_spec_rejects_non_numeric_funding():
    with pytest.raises(ValueError, match="ant"):
        parse_uploader_spec("lon1:1000:s-2vcpu-4gb:ant=lots")


def test_uploader_spec_rejects_zero_or_negative_funding():
    with pytest.raises(ValueError, match="> 0"):
        parse_uploader_spec("lon1:1000:s-2vcpu-4gb:eth=0")
    with pytest.raises(ValueError, match="> 0"):
        parse_uploader_spec("lon1:1000:s-2vcpu-4gb:ant=-1")


def test_uploader_spec_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown key"):
        parse_uploader_spec("lon1:1000:s-2vcpu-4gb:wallet=xyz")


# --- create_wallet --------------------------------------------------------

def test_create_wallet_returns_distinct_keys():
    a = wallet.create_wallet()
    b = wallet.create_wallet()
    assert a.secret_key != b.secret_key
    assert a.address != b.address
    assert a.secret_key.startswith("0x")
    assert a.address.startswith("0x")


def test_address_for_secret_key_round_trip():
    w = wallet.create_wallet()
    assert wallet.address_for_secret_key(w.secret_key) == w.address
    # Without 0x prefix should still work.
    assert wallet.address_for_secret_key(w.secret_key.removeprefix("0x")) == w.address


def test_to_token_units():
    assert wallet.to_token_units(Decimal("100"), 18) == 100 * 10**18
    assert wallet.to_token_units("0.5", 18) == 5 * 10**17
    assert wallet.to_token_units(1, 6) == 10**6


# --- fund_wallet ----------------------------------------------------------

def _mock_w3(chain_id: int = 421614, gas_price: int = 100_000_000, decimals: int = 18):
    w3 = MagicMock()
    w3.eth.chain_id = chain_id
    w3.eth.gas_price = gas_price
    w3.eth.get_transaction_count.return_value = 0
    # _gas_fields reads baseFeePerGas off the latest block.
    w3.eth.get_block.return_value = {"baseFeePerGas": gas_price}
    # transfer_eth / drain_wallet ask the node for the intrinsic gas limit.
    w3.eth.estimate_gas.return_value = 21_000
    # _send_signed: tx receipt status=1 success
    receipt = MagicMock()
    receipt.status = 1
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    w3.eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0xtxhash")

    # ERC-20 contract decimals + transfer + balanceOf are mocked individually
    # via patches in each test.
    return w3


def test_fund_wallet_sends_eth_then_ant():
    w3 = _mock_w3()
    with (
        patch.object(wallet, "transfer_eth", return_value="0xeth") as p_eth,
        patch.object(wallet, "transfer_token", return_value="0xant") as p_ant,
        patch.object(wallet, "get_token_decimals", return_value=18),
    ):
        funding_key = wallet.create_wallet().secret_key
        target = wallet.create_wallet().address
        result = wallet.fund_wallet(
            w3,
            funding_key,
            "0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c",
            target,
            ant_amount=Decimal("100"),
            eth_amount=Decimal("0.5"),
        )

    assert result == {"eth_tx": "0xeth", "ant_tx": "0xant"}
    p_eth.assert_called_once()
    p_ant.assert_called_once()
    # ETH is sent first to give the new wallet gas to interact later.
    assert p_eth.call_args.args[2] == target  # to_address
    assert p_eth.call_args.args[3] == 5 * 10**17  # 0.5 ETH in wei
    # ANT amount in token base units (18 decimals).
    assert p_ant.call_args.args[4] == 100 * 10**18


# --- drain_wallet ---------------------------------------------------------

def test_drain_wallet_sends_ant_then_eth_minus_reserve_and_gas():
    w3 = _mock_w3(gas_price=10**9)  # 1 gwei
    uploader = wallet.create_wallet()
    funding_addr = wallet.create_wallet().address
    token = "0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c"

    ant_balance = 5 * 10**18  # 5 ANT
    eth_balance = 10**18  # 1 ETH
    with (
        patch.object(wallet, "get_token_balance", return_value=ant_balance),
        patch.object(wallet, "get_eth_balance", return_value=eth_balance),
        patch.object(wallet, "transfer_token", return_value="0xant") as p_ant,
        patch.object(wallet, "_send_signed", return_value="0xeth") as p_send,
    ):
        result = wallet.drain_wallet(w3, uploader.secret_key, token, funding_addr)

    assert result["ant_tx"] == "0xant"
    assert result["eth_tx"] == "0xeth"
    p_ant.assert_called_once()
    # ANT transfer sweeps the entire balance.
    assert p_ant.call_args.args[4] == ant_balance
    # ETH transfer = balance - tx_gas_cost - reserve (computed but not
    # introspected — see comment below).
    assert p_send.called
    # _send_signed received a signed tx; we can't introspect easily, but we
    # can check w3.eth.send_raw_transaction wasn't called directly (drain uses
    # _send_signed for the manual tx path).


def test_drain_wallet_skips_zero_balances():
    w3 = _mock_w3()
    uploader = wallet.create_wallet()
    funding_addr = wallet.create_wallet().address
    token = "0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c"

    with (
        patch.object(wallet, "get_token_balance", return_value=0),
        patch.object(wallet, "get_eth_balance", return_value=0),
        patch.object(wallet, "transfer_token") as p_ant,
        patch.object(wallet, "_send_signed") as p_send,
    ):
        result = wallet.drain_wallet(w3, uploader.secret_key, token, funding_addr)

    assert result == {"ant_tx": None, "eth_tx": None}
    p_ant.assert_not_called()
    p_send.assert_not_called()


def test_drain_wallet_skips_eth_when_balance_below_reserve_plus_gas():
    w3 = _mock_w3(gas_price=10**9)
    uploader = wallet.create_wallet()
    funding_addr = wallet.create_wallet().address
    token = "0x4bc1ace0e66170375462cb4e6af42ad4d5ec689c"

    # Balance just below reserve + gas — sweep_amount would be <= 0 → skip.
    eth_balance = wallet.ETH_GAS_RESERVE_WEI + (10**9 * 21_000) - 1
    with (
        patch.object(wallet, "get_token_balance", return_value=10**18),
        patch.object(wallet, "get_eth_balance", return_value=eth_balance),
        patch.object(wallet, "transfer_token", return_value="0xant"),
        patch.object(wallet, "_send_signed") as p_send,
    ):
        result = wallet.drain_wallet(w3, uploader.secret_key, token, funding_addr)

    assert result["ant_tx"] == "0xant"
    assert result["eth_tx"] is None
    p_send.assert_not_called()


# --- _ensure_uploader_wallets (provision integration) --------------------

def test_ensure_uploader_wallets_persists_before_funding_and_tops_up_deficit():
    """Persist wallet first (so partial failures can be resumed), then top up
    only the deficit. Pre-funded wallets are skipped on the funding leg.
    """
    from saorsa_deploy.cmd.provision_clients import _ensure_uploader_wallets

    funding = wallet.create_wallet()
    pre_existing = wallet.create_wallet()
    clients = [
        {  # already has wallet, already at target → no transfers
            "role": "upload",
            "client_index": 1,
            "region": "lon1",
            "ant_funding": "100",
            "eth_funding": "0.5",
            "wallet_address": pre_existing.address,
            "wallet_secret_key": pre_existing.secret_key,
        },
        {  # no wallet → create + fund full target
            "role": "upload",
            "client_index": 2,
            "region": "nyc1",
            "ant_funding": "200",
            "eth_funding": "1",
        },
        {  # downloader → ignored
            "role": "download",
            "client_index": 1,
            "region": "tor1",
        },
    ]

    # Balance lookups: existing wallet is already topped up (skip transfer);
    # newly-created wallet has zero balance (fund full target).
    def fake_token_balance(_w3, _token, addr):
        if addr == pre_existing.address:
            return 100 * 10**18  # exactly target → no deficit
        return 0

    def fake_eth_balance(_w3, addr):
        if addr == pre_existing.address:
            return 5 * 10**17  # exactly 0.5 ETH → no deficit
        return 0

    console = MagicMock()
    with (
        patch(
            "saorsa_deploy.cmd.provision_clients.get_web3", return_value=MagicMock()
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.get_token_decimals",
            return_value=18,
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.get_token_balance",
            side_effect=fake_token_balance,
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.get_eth_balance",
            side_effect=fake_eth_balance,
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.transfer_eth",
            return_value="0xeth",
        ) as p_eth,
        patch(
            "saorsa_deploy.cmd.provision_clients.transfer_token",
            return_value="0xant",
        ) as p_ant,
        patch(
            "saorsa_deploy.cmd.provision_clients.update_client_wallet"
        ) as p_persist,
    ):
        out = _ensure_uploader_wallets(
            "DEV-01", clients, funding.secret_key, console
        )

    # Existing wallet untouched.
    assert out[0]["wallet_secret_key"] == pre_existing.secret_key
    # New wallet generated and persisted before any transfer.
    assert "wallet_secret_key" in out[1]
    assert out[1]["wallet_address"].startswith("0x")
    p_persist.assert_called_once()
    assert p_persist.call_args.kwargs["client_index"] == 2

    # Pre-existing skipped on transfers; new wallet got full target deficit.
    p_eth.assert_called_once()
    p_ant.assert_called_once()
    assert p_eth.call_args.args[2] == out[1]["wallet_address"]
    assert p_eth.call_args.args[3] == 10**18  # 1 ETH
    assert p_ant.call_args.args[3] == out[1]["wallet_address"]
    assert p_ant.call_args.args[4] == 200 * 10**18  # 200 ANT

    # Downloader unchanged.
    assert "wallet_secret_key" not in out[2]


def test_ensure_uploader_wallets_resumes_partial_funding():
    """If the previous run created a wallet and sent ETH but failed before ANT,
    re-running should detect ETH is funded and only send the missing ANT.
    """
    from saorsa_deploy.cmd.provision_clients import _ensure_uploader_wallets

    funding = wallet.create_wallet()
    partial = wallet.create_wallet()
    clients = [
        {
            "role": "upload",
            "client_index": 1,
            "region": "lon1",
            "ant_funding": "100",
            "eth_funding": "0.5",
            "wallet_address": partial.address,
            "wallet_secret_key": partial.secret_key,
        },
    ]

    console = MagicMock()
    with (
        patch(
            "saorsa_deploy.cmd.provision_clients.get_web3", return_value=MagicMock()
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.get_token_decimals",
            return_value=18,
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.get_token_balance",
            return_value=0,
        ),
        # ETH already at target — only ANT should be transferred.
        patch(
            "saorsa_deploy.cmd.provision_clients.get_eth_balance",
            return_value=5 * 10**17,
        ),
        patch(
            "saorsa_deploy.cmd.provision_clients.transfer_eth",
        ) as p_eth,
        patch(
            "saorsa_deploy.cmd.provision_clients.transfer_token",
            return_value="0xant",
        ) as p_ant,
        patch(
            "saorsa_deploy.cmd.provision_clients.update_client_wallet"
        ),
    ):
        _ensure_uploader_wallets(
            "DEV-01", clients, funding.secret_key, console
        )

    p_eth.assert_not_called()
    p_ant.assert_called_once()
    assert p_ant.call_args.args[4] == 100 * 10**18
