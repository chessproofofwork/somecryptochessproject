"""Transactions: the validation checklist and its state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sccp.chain.state import State
from sccp.chain.tx import (
    TxFailure,
    apply_transaction,
    sign_transaction,
    tx_size,
    txid,
    validate_transaction,
)
from sccp.config import (
    DEVNET,
    NetworkParams,
    derive_token_asset_id,
    token_variant_name,
)
from sccp.errors import TxValidationError
from sccp.protocol.models import Transaction, TxType
from sccp.wallet.service import WalletService

HEIGHT = 5
START = 1_700_000_000
COINS = 100_000_000


def _ledger(protocol_params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    """Devnet state one block before :data:`HEIGHT` with funded, keyed accounts."""
    state = State(params=protocol_params, height=HEIGHT - 1, timestamps=[START])
    for account in protocol_accounts.values():
        entry = state.ensure(account.address)
        entry.public_key = account.keypair.public_key_hex
        entry.balance = 1_000 * COINS
        entry.registered_until_epoch = 1_000
    return state


@pytest.fixture()
def ledger(protocol_params: NetworkParams, protocol_accounts: Mapping[str, Any]) -> State:
    return _ledger(protocol_params, protocol_accounts)


def _params(**overrides: Any) -> NetworkParams:
    return DEVNET.model_copy(update=overrides)


def _wallet(state: State, params: NetworkParams = DEVNET) -> WalletService:
    return WalletService(params, state)


def _resigned(tx: Transaction, account: Any) -> Transaction:
    return sign_transaction(account.keypair, tx.model_copy(update={"signature": None}))


def _check(
    tx: Transaction, state: State, params: NetworkParams = DEVNET, height: int = HEIGHT
) -> None:
    validate_transaction(tx, params, state, height=height)


def _reason(tx: Transaction, state: State, **kwargs: Any) -> TxFailure:
    with pytest.raises(TxValidationError) as excinfo:
        _check(tx, state, **kwargs)
    return excinfo.value.reason


def test_transfer_moves_funds_and_charges_the_fee(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    _check(tx, ledger)
    before = ledger.account(alice.address).balance
    apply_transaction(ledger, tx, height=HEIGHT)
    assert ledger.account(alice.address).balance == before - 100 - DEVNET.min_fee
    assert ledger.account(bob.address).balance == 1_000 * COINS + 100
    assert ledger.account(alice.address).nonce == 1


def test_transfer_rejects_a_stale_nonce(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    apply_transaction(ledger, tx, height=HEIGHT)
    replay = _resigned(tx.model_copy(update={"nonce": 0}), alice)
    assert _reason(replay, ledger) is TxFailure.NONCE


def test_transfer_rejects_a_nonce_from_the_future(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    ahead = _resigned(tx.model_copy(update={"nonce": 7}), alice)
    assert _reason(ahead, ledger) is TxFailure.NONCE


def test_transfer_requires_funds(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    huge = _resigned(tx.model_copy(update={"payload": {**tx.payload, "amount": 10**15}}), alice)
    assert _reason(huge, ledger) is TxFailure.BALANCE


def test_immature_coinbase_is_unspendable_until_its_exact_maturity(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    """Mined value cannot pay a transfer until the maturity block releases it."""
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 100)
    reward = 100 + tx.fee
    ledger.accounts[alice.address].balance = 0
    ledger.credit_coinbase(alice.address, reward, HEIGHT)
    assert ledger.account(alice.address).immature == reward
    assert ledger.spendable(alice.address) == 0
    assert _reason(tx, ledger) is TxFailure.BALANCE

    maturity = ledger.params.coinbase_maturity
    assert ledger.release_matured(HEIGHT + maturity - 1) == 0
    assert _reason(tx, ledger) is TxFailure.BALANCE

    assert ledger.release_matured(HEIGHT + maturity) == reward
    assert ledger.spendable(alice.address) == reward
    _check(tx, ledger, height=HEIGHT + maturity)
    apply_transaction(ledger, tx, height=HEIGHT + maturity)
    account = ledger.account(alice.address)
    assert account.balance == 0
    assert account.immature == 0
    assert account.coinbase_queue == []


def test_transfer_rejects_self_payment(ledger: State, protocol_accounts: Mapping[str, Any]) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_transfer(alice.keypair, protocol_accounts["bob"].address, 1)
    selfish = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "to": alice.address}}), alice
    )
    assert _reason(selfish, ledger) is TxFailure.TYPE


def test_transfer_rejects_unknown_payload_keys(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    extra = _resigned(tx.model_copy(update={"payload": {**tx.payload, "memo": "hi"}}), alice)
    assert _reason(extra, ledger) is TxFailure.SCHEMA


@pytest.mark.parametrize("bad_value", [1.5, 2**64, "\ud800", {"nested": [1.5]}, b"\x00"], ids=str)
def test_a_payload_canonical_json_cannot_encode_is_a_schema_failure(
    ledger: State, protocol_accounts: Mapping[str, Any], bad_value: object
) -> None:
    """An unencodable payload must be a ``TxFailure``, not a bare serialization error.

    ``txid`` and ``tx_size`` both walk the payload; letting them raise first meant
    block validation and mempool admission leaked ``SerializationError`` (or, for
    a surrogate, an unhandled ``UnicodeEncodeError``) instead of a stable reason.
    """
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    broken = tx.model_copy(update={"payload": {**tx.payload, "public_key": bad_value}})
    assert _reason(broken, ledger) is TxFailure.SCHEMA


def test_transfer_rejects_a_bad_recipient(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_transfer(alice.keypair, protocol_accounts["bob"].address, 1)
    typo = _resigned(tx.model_copy(update={"payload": {**tx.payload, "to": "sccpt1nope"}}), alice)
    assert _reason(typo, ledger) is TxFailure.SCHEMA


def test_unsigned_and_foreign_transactions_are_rejected(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    assert _reason(tx.model_copy(update={"signature": None}), ledger) is TxFailure.SIGNATURE
    assert _reason(tx.model_copy(update={"network": "testnet"}), ledger) is TxFailure.NETWORK


def test_fee_must_meet_the_network_minimum(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    cheap = _resigned(tx.model_copy(update={"fee": DEVNET.min_fee - 1}), alice)
    assert _reason(cheap, ledger) is TxFailure.FEE


def test_first_use_must_reveal_the_public_key(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    ledger.accounts[alice.address].public_key = None
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 1)
    _check(tx, ledger)
    silent = _resigned(
        tx.model_copy(
            update={
                "payload": {key: value for key, value in tx.payload.items() if key != "public_key"}
            }
        ),
        alice,
    )
    assert _reason(silent, ledger) is TxFailure.SIGNATURE


def test_register_locks_the_bond_and_sets_the_registration_window(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    tx = _wallet(ledger, params).build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=5_000
    )
    _check(tx, ledger, params=params)
    apply_transaction(ledger, tx, height=HEIGHT)
    account = ledger.account(alice.address)
    assert account.bonded == 5_000
    assert account.endpoint == "tcp://alice.test"
    assert account.engine == "mock-v1"
    assert account.registered_until_epoch == HEIGHT + DEVNET.registration_epochs


def test_register_below_the_minimum_bond_is_rejected(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    tx = _wallet(ledger, params).build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=1_000
    )
    short = _resigned(tx.model_copy(update={"payload": {**tx.payload, "bond": 999}}), alice)
    assert _reason(short, ledger, params=params) is TxFailure.BOND


def test_unregister_then_withdraw_bond_after_the_delay(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    params = _params(min_bond=1_000)
    wallet = _wallet(ledger, params)
    register = wallet.build_register(
        alice.keypair, endpoint="tcp://alice.test", engine="mock-v1", bond=5_000
    )
    apply_transaction(ledger, register, height=HEIGHT)

    def retype(tx: Transaction, kind: TxType, payload: dict[str, object]) -> Transaction:
        nonce = ledger.account(alice.address).nonce
        return _resigned(
            tx.model_copy(update={"type": kind, "nonce": nonce, "payload": payload}), alice
        )

    key = {"public_key": alice.keypair.public_key_hex}
    leave = retype(register, TxType.UNREGISTER, key)
    _check(leave, ledger, params=params)
    apply_transaction(ledger, leave, height=HEIGHT)
    account = ledger.account(alice.address)
    assert account.unbonding_height == HEIGHT + DEVNET.unbond_delay
    assert account.registered_until_epoch == HEIGHT // DEVNET.epoch_length

    withdraw = retype(register, TxType.WITHDRAW_BOND, key)
    assert _reason(withdraw, ledger, params=params) is TxFailure.WINDOW

    unlock = HEIGHT + DEVNET.unbond_delay
    _check(withdraw, ledger, params=params, height=unlock)
    balance = ledger.account(alice.address).balance
    apply_transaction(ledger, withdraw, height=unlock)
    account = ledger.account(alice.address)
    assert account.bonded == 0
    assert account.unbonding_height is None
    assert account.registered_until_epoch is None
    assert account.balance == balance - DEVNET.min_fee + 5_000


def test_availability_is_bound_to_its_commit_epoch(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    tx = wallet.build_availability(alice.keypair, nonce_secret="11" * 32, max_games=2)
    assert tx.payload["epoch"] == HEIGHT + DEVNET.availability_lead
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)
    assert ledger.availability[HEIGHT + DEVNET.availability_lead][alice.address].max_games == 2

    early = _resigned(
        tx.model_copy(update={"nonce": 1, "payload": {**tx.payload, "epoch": HEIGHT + 1}}), alice
    )
    assert _reason(early, ledger) is TxFailure.WINDOW
    greedy = _resigned(
        tx.model_copy(
            update={
                "nonce": 1,
                "payload": {**tx.payload, "max_games": DEVNET.rounds_per_epoch + 1},
            }
        ),
        alice,
    )
    assert _reason(greedy, ledger) is TxFailure.TYPE


def test_availability_cannot_be_changed_once_committed(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    first = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, first, height=HEIGHT)
    second = wallet.build_availability(alice.keypair, nonce_secret="22" * 32)
    assert _reason(second, ledger) is TxFailure.DUPLICATE


def test_reveal_needs_a_matching_commitment(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    with pytest.raises(TxValidationError) as excinfo:
        _check(wallet.build_reveal(alice.keypair, "11" * 32), ledger)
    assert excinfo.value.reason is TxFailure.REGISTRATION

    commit = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, commit, height=HEIGHT)
    ledger.height = HEIGHT
    reveal = wallet.build_reveal(alice.keypair, "11" * 32)
    assert reveal.payload["epoch"] == HEIGHT + DEVNET.availability_lead
    _check(reveal, ledger, height=HEIGHT + 1)
    apply_transaction(ledger, reveal, height=HEIGHT + 1)
    assert ledger.reveals[HEIGHT + DEVNET.availability_lead][alice.address] == "11" * 32


def test_reveal_rejects_a_wrong_nonce_and_a_wrong_window(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    commit = wallet.build_availability(alice.keypair, nonce_secret="11" * 32)
    apply_transaction(ledger, commit, height=HEIGHT)
    ledger.height = HEIGHT
    reveal = wallet.build_reveal(alice.keypair, "11" * 32)
    wrong = _resigned(
        reveal.model_copy(update={"payload": {**reveal.payload, "nonce": "22" * 32}}),
        alice,
    )
    assert _reason(wrong, ledger, height=HEIGHT + 1) is TxFailure.TYPE
    late = _resigned(
        wrong.model_copy(
            update={"payload": {**wrong.payload, "nonce": "11" * 32, "epoch": HEIGHT}}
        ),
        alice,
    )
    assert _reason(late, ledger) is TxFailure.WINDOW


def test_register_token_adds_a_variant_and_credits_its_initial_supply(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    tx = wallet.build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="standard",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=50 * COINS,
        initial_supply=123,
        max_supply=21_000_000,
        halving_interval=210_000,
        min_plies=4,
    )
    _check(tx, ledger)
    before = ledger.account(alice.address).balance
    apply_transaction(ledger, tx, height=HEIGHT)

    record = ledger.tokens["dragonchess"]
    asset = record.variant.asset
    assert asset is not None
    assert record.variant.rules == "chess"
    assert record.variant.ruleset == "standard"
    assert record.owner == alice.address
    assert record.registered_at_height == HEIGHT
    assert record.initial_supply == 123
    assert ledger.variant("dragonchess") == record.variant
    assert "dragonchess" in ledger.variant_names()
    assert ledger.variant_for_asset(asset) == record.variant
    assert ledger.account(alice.address).balance == before - DEVNET.min_fee
    assert ledger.account(alice.address).assets[asset] == 123
    assert ledger.minted_assets[asset] == 123

    again = _resigned(tx.model_copy(update={"nonce": 1}), alice)
    assert _reason(again, ledger) is TxFailure.TYPE


def test_a_token_registration_can_certify_a_middle_game_prefix(
    ledger: State,
    protocol_accounts: Mapping[str, Any],
    middle_game_opening: tuple[str, ...],
) -> None:
    """The registration path accepts a 40-ply start and rejects a 41st ply."""
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    tx = wallet.build_register_token(
        alice.keypair,
        ticket="0043",
        display_name="Middle Game",
        symbol="MID",
        ruleset="standard",
        subsidy=COINS,
        openings=[middle_game_opening],
    )
    _check(tx, ledger)
    too_long = _resigned(
        tx.model_copy(
            update={
                "payload": {
                    **tx.payload,
                    "openings": [[*middle_game_opening, "Bxb8"]],
                }
            }
        ),
        alice,
    )
    assert _reason(too_long, ledger) is TxFailure.TYPE
    apply_transaction(ledger, tx, height=HEIGHT)
    assert ledger.tokens["token-0043"].variant.openings == (middle_game_opening,)


def test_a_token_registration_can_pin_a_start_fen(
    ledger: State,
    protocol_accounts: Mapping[str, Any],
) -> None:
    """A token may start every game from its own legal position."""
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    start_fen = "rnbqk1nr/pppp1ppp/8/2b1p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 2 3"
    canonical = "rnbqk1nr/pppp1ppp/8/2b1p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 0 1"
    tx = wallet.build_register_token(
        alice.keypair,
        ticket="0045",
        display_name="Pinned Start",
        symbol="PIN",
        ruleset="standard",
        subsidy=COINS,
        start_fen=start_fen,
        openings=["Nf3 Nf6"],
    )
    assert tx.payload["start_fen"] == canonical
    assert tx.payload["opening_book"] == "token-0045"
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)

    variant = ledger.tokens["token-0045"].variant
    assert variant.start_fen == canonical
    assert variant.openings == (("Nf3", "Nf6"),)
    assert variant.opening_book == "token-0045"

    fixed = wallet.build_register_token(
        alice.keypair,
        ticket="0046",
        display_name="Fixed Start",
        symbol="FIX",
        ruleset="standard",
        subsidy=COINS,
        start_fen=start_fen,
    )
    assert fixed.payload["opening_book"] == "none"
    _check(fixed, ledger, height=HEIGHT + 1)
    apply_transaction(ledger, fixed, height=HEIGHT + 1)
    assert ledger.tokens["token-0046"].variant.start_fen == canonical
    assert ledger.tokens["token-0046"].variant.openings == ()

    invalid = _resigned(
        fixed.model_copy(
            update={
                "nonce": 2,
                "payload": {**fixed.payload, "ticket": "0047", "start_fen": "not a fen"},
            }
        ),
        alice,
    )
    assert _reason(invalid, ledger, height=HEIGHT + 2) is TxFailure.TYPE


def test_a_ticketed_registration_keeps_its_name_and_ticker_as_metadata(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    """A ticket is a registration serial; a name and ticker are display metadata.

    The chain derives a unique internal variant and token id from the ticket,
    stores the chosen name and ticker verbatim, keeps the opening lines as
    canonical SAN, and resolves the record by either the ticket or token id.
    """
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        ticket="0042",
        display_name="Gold Coin",
        symbol="GOLD",
        ruleset="standard",
        subsidy=COINS,
        initial_supply=7,
        openings=["e2e4 e7e5 g1f3", ["d2d4"]],
        time_control_initial_ms=5_400_000,
        time_control_first_moves=40,
        time_control_periods=[{"moves": 20, "initial_ms": 1_800_000, "increment_ms": 30_000}],
    )
    assert tx.payload["ticket"] == "0042"
    assert tx.payload["display_name"] == "Gold Coin"
    assert tx.payload["symbol"] == "GOLD"
    assert "variant" not in tx.payload
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)

    record = ledger.tokens["token-0042"]
    variant = record.variant
    asset = derive_token_asset_id("0042", 8, "standard")
    assert record.ticket == "0042"
    assert record.token_id == asset
    assert variant.name == token_variant_name("0042")
    assert variant.symbol == "GOLD"
    assert variant.display_name == "Gold Coin"
    assert variant.opening_book == "token-0042"
    assert variant.openings == (("e4", "e5", "Nf3"), ("d4",))
    assert variant.time_control_initial_ms == 5_400_000
    assert variant.time_control_first_moves == 40
    assert len(variant.time_control_periods) == 1
    period = variant.time_control_periods[0]
    assert (period.moves, period.initial_ms, period.increment_ms) == (
        20,
        1_800_000,
        30_000,
    )
    assert ledger.token_by_ticket("0042") == record
    assert ledger.token_by_id(asset) == record
    assert ledger.variant_for_asset(asset) == variant

    duplicate = _resigned(tx.model_copy(update={"nonce": 1}), alice)
    assert _reason(duplicate, ledger, height=HEIGHT + 1) is TxFailure.TYPE
    mixed = _resigned(
        tx.model_copy(
            update={
                "nonce": 1,
                "payload": {**tx.payload, "ticket": "0043", "variant": "dragonchess"},
            }
        ),
        alice,
    )
    assert _reason(mixed, ledger, height=HEIGHT + 1) is TxFailure.SCHEMA


def test_a_token_ticket_can_use_the_64_bit_registration_namespace(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    """A 16-hex ticket is a normal registration serial, not a four-digit cap."""
    alice = protocol_accounts["alice"]
    ticket = "7f3a9c2e5b1d4068"
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        ticket=ticket,
        display_name="Long Ticket Coin",
        symbol="LONG",
        ruleset="standard",
        subsidy=COINS,
    )
    assert tx.payload["ticket"] == ticket
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)

    record = ledger.tokens[f"token-{ticket}"]
    assert record.ticket == ticket
    assert record.token_id == derive_token_asset_id(ticket, 8, "standard")
    assert ledger.token_by_ticket(ticket) == record


def test_two_tokens_may_share_a_name_and_ticker(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    """Identity is the token id, so display metadata does not have to be unique."""
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    first = wallet.build_register_token(
        alice.keypair,
        ticket="0042",
        display_name="Gold Coin",
        symbol="GOLD",
        ruleset="standard",
        subsidy=COINS,
    )
    _check(first, ledger, height=HEIGHT)
    apply_transaction(ledger, first, height=HEIGHT)

    second = wallet.build_register_token(
        alice.keypair,
        ticket="0043",
        display_name="Gold Coin",
        symbol="GOLD",
        ruleset="standard",
        subsidy=COINS,
    )
    _check(second, ledger, height=HEIGHT + 1)
    apply_transaction(ledger, second, height=HEIGHT + 1)

    one = ledger.tokens["token-0042"]
    two = ledger.tokens["token-0043"]
    assert one.variant.display_name == two.variant.display_name == "Gold Coin"
    assert one.variant.symbol == two.variant.symbol == "GOLD"
    assert one.token_id != two.token_id


def test_a_ticketed_token_without_its_own_lines_uses_the_network_book(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    """A ticket is not a book by itself; omitting lines keeps the network's."""
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        ticket="0044",
        display_name="Silver Coin",
        symbol="SLV",
        ruleset="standard",
        subsidy=COINS,
    )
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)
    record = ledger.tokens["token-0044"]
    assert record.variant.openings == ()
    assert record.variant.opening_book == DEVNET.book_version


def test_register_token_requires_its_fee_in_the_senders_balance(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="standard",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=COINS,
    )
    ledger.account(alice.address).balance = DEVNET.min_fee - 1
    assert _reason(tx, ledger) is TxFailure.BALANCE


def test_register_token_scales_its_cap_by_the_token_decimals(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        name="centicoin",
        ruleset="standard",
        symbol="CENT",
        display_name="Centicoin",
        subsidy=1,
        decimals=2,
        initial_supply=100,
        max_supply=1,
        halving_interval=10,
    )
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)
    asset = ledger.tokens["centicoin"].variant.asset
    assert asset is not None
    assert ledger.account(alice.address).assets[asset] == 100
    over_cap = _resigned(
        tx.model_copy(
            update={
                "nonce": 1,
                "payload": {
                    **tx.payload,
                    "variant": "overcap",
                    "symbol": "OVER",
                    "initial_supply": 101,
                },
            }
        ),
        alice,
    )
    assert _reason(over_cap, ledger, height=HEIGHT + 1) is TxFailure.TYPE


def test_register_token_rejects_reserved_names_and_unknown_rulesets(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    wallet = _wallet(ledger)
    tx = wallet.build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="standard",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=COINS,
    )
    standard = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "variant": "standard"}}), alice
    )
    assert _reason(standard, ledger) is TxFailure.SCHEMA
    unknown_ruleset = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "ruleset": "nope"}}), alice
    )
    assert _reason(unknown_ruleset, ledger) is TxFailure.TYPE


def test_register_token_rejects_a_bad_slug_and_an_impossible_issuance(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        name="dragonchess",
        ruleset="standard",
        symbol="DRG",
        display_name="Dragon Chess",
        subsidy=COINS,
    )
    bad_slug = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "variant": "Dragon Chess"}}), alice
    )
    assert _reason(bad_slug, ledger) is TxFailure.SCHEMA
    over_cap = _resigned(
        tx.model_copy(
            update={
                "payload": {
                    **tx.payload,
                    "initial_supply": 2 * COINS,
                    "max_supply": 1,
                    "halving_interval": 10,
                }
            }
        ),
        alice,
    )
    assert _reason(over_cap, ledger) is TxFailure.TYPE
    no_halving = _resigned(
        tx.model_copy(update={"payload": {**tx.payload, "max_supply": 10}}), alice
    )
    assert _reason(no_halving, ledger) is TxFailure.SCHEMA


def test_register_token_records_a_delay_and_rejects_a_mixed_bonus(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice = protocol_accounts["alice"]
    tx = _wallet(ledger).build_register_token(
        alice.keypair,
        name="delaychess",
        ruleset="standard",
        symbol="DLY",
        display_name="Delay Chess",
        subsidy=COINS,
        time_control_initial_ms=180_000,
        time_control_delay_ms=3_000,
    )
    _check(tx, ledger)
    apply_transaction(ledger, tx, height=HEIGHT)
    variant = ledger.variant("delaychess")
    assert variant.time_control == (180_000, 0, 3_000)

    mixed = _resigned(
        tx.model_copy(
            update={
                "nonce": 1,
                "payload": {
                    **tx.payload,
                    "variant": "mixedchess",
                    "symbol": "MIX",
                    "time_control_increment_ms": 2_000,
                },
            }
        ),
        alice,
    )
    assert _reason(mixed, ledger, height=HEIGHT + 1) is TxFailure.SCHEMA


def test_txid_ignores_the_signature_and_tracks_the_body(
    ledger: State, protocol_accounts: Mapping[str, Any]
) -> None:
    alice, bob = protocol_accounts["alice"], protocol_accounts["bob"]
    tx = _wallet(ledger).build_transfer(alice.keypair, bob.address, 10)
    assert txid(tx) == txid(tx.model_copy(update={"signature": None}))
    assert txid(tx) != txid(tx.model_copy(update={"fee": DEVNET.min_fee + 1}))
    assert tx_size(
        tx.model_copy(update={"payload": {**tx.payload, "padding": "x" * 50}})
    ) > tx_size(tx)
