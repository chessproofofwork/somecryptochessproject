"""Wallet service: balances, builders and history (PROTOCOL.md)."""

from __future__ import annotations

from typing import Any

import pytest

from sccp.chain.state import TokenRecord
from sccp.chain.tx import validate_transaction
from sccp.config import DEVNET, VariantParams, derive_variant_asset_id, format_coins
from sccp.crypto.address import encode_address
from sccp.crypto.pq import hybrid_keypair_from_seed
from sccp.errors import WalletServiceError
from sccp.protocol.models import TxType
from sccp.wallet.service import Balance, WalletService

COINS = 100_000_000


def test_the_service_reports_the_network_and_addresses(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"]
    assert wallet.network == "devnet"
    assert wallet.address_for(alice.keypair) == alice.address
    assert wallet.next_nonce(alice.address) == 3
    stranger = encode_address(hybrid_keypair_from_seed(bytes([9]) * 32).public_key_bytes, DEVNET)
    assert wallet.account(stranger).balance == 0
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.account("sccpt1qunknown")


def test_balances_split_spendable_immature_and_bonded(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"].address
    balance = wallet.balance(alice)
    assert isinstance(balance, Balance)
    assert balance.address == alice
    assert balance.total == balance.spendable + balance.immature + balance.bonded
    assert balance.spendable == 100 * COINS - 3 * DEVNET.min_fee
    assert balance.immature == 0
    assert balance.bonded == 0


def test_the_minimum_fee_is_enforced(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state)
    assert wallet.required_fee(None) == DEVNET.min_fee
    assert wallet.required_fee(DEVNET.min_fee * 2) == DEVNET.min_fee * 2
    with pytest.raises(WalletServiceError, match="below the network minimum"):
        wallet.required_fee(DEVNET.min_fee - 1)


def test_a_transfer_is_signed_and_valid(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    tx = wallet.build_transfer(alice.keypair, bob.address, COINS)
    assert tx.type is TxType.TRANSFER
    assert tx.sender == alice.address
    assert tx.payload["amount"] == COINS
    assert tx.nonce == 3
    assert tx.signature is not None
    validate_transaction(tx, DEVNET, chain.state, height=chain.height + 1)


def test_transfer_guards(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    with pytest.raises(WalletServiceError, match="positive"):
        wallet.build_transfer(alice.keypair, bob.address, 0)
    with pytest.raises(WalletServiceError, match="different recipient"):
        wallet.build_transfer(alice.keypair, alice.address, COINS)
    with pytest.raises(WalletServiceError, match="spendable"):
        wallet.build_transfer(alice.keypair, bob.address, 10**15)
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.build_transfer(alice.keypair, "sccpt1nope", COINS)
    with pytest.raises(WalletServiceError, match="nonce"):
        wallet.build_transfer(alice.keypair, bob.address, COINS, nonce=9)


def test_a_refused_transfer_names_coins_and_the_local_chain(live_chain: Any) -> None:
    """Base units in a refusal look like coins; print coins and say what to check."""
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    spendable = chain.state.spendable(alice.address)
    with pytest.raises(WalletServiceError) as caught:
        wallet.build_transfer(alice.keypair, bob.address, 100 * COINS)
    message = str(caught.value)
    assert f"has {format_coins(spendable)} spendable" in message
    assert (
        f"needs {format_coins(100 * COINS + DEVNET.min_fee)} "
        f"({format_coins(100 * COINS)} plus a {format_coins(DEVNET.min_fee)} fee)"
    ) in message
    assert "--data-dir" in message


def test_register_respects_the_minimum_bond(live_chain: Any) -> None:
    chain = live_chain.chain
    params = DEVNET.model_copy(update={"min_bond": 1_000})
    wallet = WalletService(params, chain.state)
    alice = live_chain.accounts["alice"]
    tx = wallet.build_register(alice.keypair, endpoint="tcp://a", engine="mock-v1")
    assert tx.payload["bond"] == 1_000
    assert tx.type is TxType.REGISTER
    with pytest.raises(WalletServiceError, match="below the minimum"):
        wallet.build_register(alice.keypair, endpoint="tcp://a", engine="mock-v1", bond=999)


def test_register_token_builds_a_signed_variant_registration(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"]
    tx = wallet.build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="chess960",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=50 * COINS,
        initial_supply=COINS,
        max_supply=21_000_000,
        halving_interval=210_000,
    )
    assert tx.type is TxType.REGISTER_TOKEN
    assert tx.payload["variant"] == "dragonchess"
    assert tx.payload["ruleset"] == "chess960"
    assert tx.payload["initial_supply"] == COINS
    # A token omitted from the CLI must play the network's own book, not a
    # placeholder label the relay cannot load.
    assert tx.payload["opening_book"] == DEVNET.book_version
    assert tx.payload["time_control_delay_ms"] == 0
    assert tx.signature is not None
    validate_transaction(tx, DEVNET, chain.state, height=chain.height + 1)

    delayed = wallet.build_register_token(
        alice.keypair,
        name="delaychess",
        ruleset="standard",
        symbol="DLY",
        display_name="Delay Chess",
        subsidy=COINS,
        time_control_initial_ms=180_000,
        time_control_delay_ms=3_000,
    )
    assert delayed.payload["time_control_initial_ms"] == 180_000
    assert delayed.payload["time_control_increment_ms"] == 0
    assert delayed.payload["time_control_delay_ms"] == 3_000
    validate_transaction(delayed, DEVNET, chain.state, height=chain.height + 1)

    with pytest.raises(WalletServiceError, match="increment or a delay"):
        wallet.build_register_token(
            alice.keypair,
            name="mixedchess",
            ruleset="standard",
            symbol="MIX",
            display_name="Mixed Chess",
            subsidy=COINS,
            time_control_increment_ms=2_000,
            time_control_delay_ms=2_000,
        )

    with pytest.raises(WalletServiceError, match="already known"):
        wallet.build_register_token(
            alice.keypair,
            name="chess960",
            ruleset="chess960",
            symbol="C960",
            display_name="Chess960",
            subsidy=COINS,
        )
    with pytest.raises(WalletServiceError, match="cannot be negative"):
        wallet.build_register_token(
            alice.keypair,
            name="dragonchess",
            ruleset="standard",
            symbol="DRG",
            display_name="Dragon Chess",
            subsidy=COINS,
            initial_supply=-1,
        )
    with pytest.raises(WalletServiceError, match="max_plies"):
        wallet.build_register_token(
            alice.keypair,
            name="dragonchess",
            ruleset="standard",
            symbol="DRG",
            display_name="Dragon Chess",
            subsidy=COINS,
            min_plies=DEVNET.max_plies + 1,
        )
    with pytest.raises(WalletServiceError, match="supply cap"):
        wallet.build_register_token(
            alice.keypair,
            name="dragonchess",
            ruleset="standard",
            symbol="DRG",
            display_name="Dragon Chess",
            subsidy=COINS,
            initial_supply=2 * COINS,
            max_supply=1,
            halving_interval=1,
        )
    scaled = wallet.build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="standard",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=1,
        decimals=2,
        initial_supply=100,
        max_supply=1,
        halving_interval=1,
    )
    assert scaled.payload["initial_supply"] == 100
    validate_transaction(scaled, DEVNET, chain.state, height=chain.height + 1)
    with pytest.raises(WalletServiceError, match="supply cap"):
        wallet.build_register_token(
            alice.keypair,
            name="dragonchess",
            ruleset="standard",
            symbol="DRG",
            display_name="Dragon Chess",
            subsidy=1,
            decimals=2,
            initial_supply=101,
            max_supply=1,
            halving_interval=1,
        )


def test_transfer_refuses_a_symbol_shared_by_two_tokens(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    alice = live_chain.accounts["alice"]
    bob = live_chain.accounts["bob"]
    for name in ("alpha", "beta"):
        asset = derive_variant_asset_id(name, "SAME", 8, "standard")
        variant = VariantParams(
            name=name,
            rules="chess",
            ruleset="standard",
            asset=asset,
            symbol="SAME",
            display_name=name.title(),
            subsidy=COINS,
            min_plies=DEVNET.min_plies,
        )
        chain.state.tokens[name] = TokenRecord(
            name=name,
            variant=variant,
            owner=alice.address,
            registered_at_height=0,
        )
    with pytest.raises(WalletServiceError, match="ambiguous"):
        wallet.build_transfer(alice.keypair, bob.address, 1, asset="SAME")
    alpha = derive_variant_asset_id("alpha", "SAME", 8, "standard")
    chain.state.credit_asset(alice.address, alpha, 1)
    assert (
        wallet.build_transfer(alice.keypair, bob.address, 1, asset="alpha").payload["asset"]
        == alpha
    )


def test_availability_and_reveal_target_the_next_block_epoch(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state)
    alice = live_chain.accounts["alice"]
    availability = wallet.build_availability(alice.keypair, nonce_secret="33" * 32, max_games=1)
    expected = chain.height + 1 + DEVNET.availability_lead
    assert availability.payload["epoch"] == expected
    with pytest.raises(WalletServiceError, match="must be committed"):
        wallet.build_availability(alice.keypair, nonce_secret="33" * 32, epoch=expected + 1)
    reveal = wallet.build_reveal(alice.keypair, "33" * 32)
    assert reveal.type is TxType.REVEAL
    assert reveal.payload["epoch"] == chain.height + 2


def test_history_walks_the_adopted_chain(live_chain: Any) -> None:
    chain = live_chain.chain
    wallet = WalletService(DEVNET, chain.state, chain=chain)
    faucet_entries = wallet.history(live_chain.faucet_address)
    assert len(faucet_entries) == 2
    assert {entry.type for entry in faucet_entries} == {TxType.TRANSFER}
    assert all(entry.delta == -(100 * COINS + DEVNET.min_fee) for entry in faucet_entries)
    assert {entry.counterparty for entry in faucet_entries} == {
        live_chain.accounts["alice"].address,
        live_chain.accounts["bob"].address,
    }
    assert all(entry.height == 1 for entry in faucet_entries)

    alice = live_chain.accounts["alice"].address
    entries = wallet.history(alice)
    assert [entry.type for entry in entries] == [
        TxType.TRANSFER,
        TxType.REGISTER,
        TxType.AVAILABILITY,
        TxType.REVEAL,
    ]
    assert entries[0].delta == 100 * COINS
    assert entries[0].counterparty == live_chain.faucet_address
    assert entries[1].delta == -DEVNET.min_fee
    assert entries[1].counterparty is None
    assert [entry.height for entry in entries] == [1, 1, 1, 2]


def test_history_needs_a_chain(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state)
    with pytest.raises(WalletServiceError, match="needs a chain"):
        wallet.history(live_chain.accounts["alice"].address)


def test_history_rejects_a_foreign_address(live_chain: Any) -> None:
    wallet = WalletService(DEVNET, live_chain.chain.state, chain=live_chain.chain)
    with pytest.raises(WalletServiceError, match="not a valid"):
        wallet.history("mainnet1nope")


def _account_view(address: str, *, spendable: int = 50 * COINS, nonce: int = 4) -> dict[str, Any]:
    """The document ``GET /v1/accounts/{address}`` returns, minus the extras."""
    return {
        "network": "devnet",
        "address": address,
        "balance": spendable,
        "immature": 0,
        "bonded": 0,
        "total": spendable,
        "nonce": nonce,
    }


def test_a_wallet_can_be_built_from_a_nodes_account_view() -> None:
    """Balance and nonce are all a transfer needs, so one node answer is enough."""
    keypair = hybrid_keypair_from_seed(bytes([7]) * 32)
    address = encode_address(keypair.public_key_bytes, DEVNET)
    document = {**_account_view(address, nonce=4), "public_key": keypair.public_key_hex}
    wallet = WalletService.from_account_view(DEVNET, document, source="the node at http://n")

    assert wallet.network == "devnet"
    assert wallet.chain is None
    assert wallet.balance(address).spendable == 50 * COINS
    assert wallet.next_nonce(address) == 4
    stranger = encode_address(hybrid_keypair_from_seed(bytes([9]) * 32).public_key_bytes, DEVNET)
    tx = wallet.build_transfer(keypair, stranger, COINS)
    assert tx.nonce == 4
    validate_transaction(tx, DEVNET, wallet.state, height=1)
    with pytest.raises(WalletServiceError, match="needs a chain"):
        wallet.history(address)


def test_an_account_view_must_belong_to_the_requested_network() -> None:
    """A --node on another chain is caught before anything is signed."""
    address = encode_address(hybrid_keypair_from_seed(bytes([7]) * 32).public_key_bytes, DEVNET)
    document = {**_account_view(address), "network": "mainnet"}
    with pytest.raises(WalletServiceError, match="serves mainnet, not devnet"):
        WalletService.from_account_view(DEVNET, document)


def test_an_account_view_must_be_readable() -> None:
    address = encode_address(hybrid_keypair_from_seed(bytes([7]) * 32).public_key_bytes, DEVNET)
    with pytest.raises(WalletServiceError, match="names no network"):
        WalletService.from_account_view(DEVNET, {"address": address})
    with pytest.raises(WalletServiceError, match="names no address"):
        WalletService.from_account_view(DEVNET, {"network": "devnet"})
    with pytest.raises(WalletServiceError, match="unreadable"):
        WalletService.from_account_view(DEVNET, {**_account_view(address), "balance": "plenty"})
