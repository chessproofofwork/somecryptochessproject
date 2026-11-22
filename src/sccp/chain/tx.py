"""Transaction validation and application (PROTOCOL.md section 10).

Validation order is fixed by the protocol: schema -> network -> signature -> nonce ->
balance -> type-specific rules -> size limits. Mempool admission runs the same
checklist; blocks rerun everything from scratch, so a mempool bug can never make
an invalid block valid.

Failures are :class:`~sccp.errors.TxValidationError` with a stable
:class:`TxFailure` reason, which is what makes a double spend, a wrong nonce, an
insufficient balance, a wrong network and a bad signature distinguishable to a
caller instead of five variations of "invalid".

Balance arithmetic uses only integers in base units. Fees are always debited from
spendable balance and never from a bond.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import NoReturn

from pydantic import ValidationError

from sccp.chain import dex
from sccp.chain.state import (
    HtlcRecord,
    HtlcStatus,
    Pool,
    State,
    TokenRecord,
    epoch_of,
    first_matchable_epoch,
)
from sccp.chess.rulesets import (
    canonical_opening_line,
    normalise_token_start_fen,
    ruleset_for,
)
from sccp.config import (
    DEFAULT_TIME_CONTROL_DELAY_MS,
    DEFAULT_TIME_CONTROL_INCREMENT_MS,
    DEFAULT_TIME_CONTROL_INITIAL_MS,
    MAX_TOKEN_OPENING_PLIES,
    TOKEN_TICKET_PATTERN,
    NetworkParams,
    TimeControlPeriod,
    VariantParams,
    derive_token_asset_id,
    derive_variant_asset_id,
    token_variant_name,
    units_per_coin,
)
from sccp.crypto.address import encode_address, validate_address
from sccp.crypto.pq import (
    HYBRID_PUBLIC_KEY_SIZE,
    HybridKeypair,
    hybrid_sign,
    hybrid_verify,
    is_hybrid_public_key_hex,
)
from sccp.errors import (
    AddressError,
    ConfigError,
    KeyEncodingError,
    SerializationError,
    TxValidationError,
)
from sccp.protocol.beacon import commitment
from sccp.protocol.models import Availability, Transaction, TxType
from sccp.serialization import Domain, canonical_json, domain_hex, normalize

MAX_SAFE_TEXT: int = 256
"""Upper bound for the free-text fields a payload may carry."""

MAX_TOKEN_AMOUNT: int = 2**63 - 1
"""Largest integer a token field may carry, so no ledger arithmetic overflows."""

TOKEN_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
"""Variant and ruleset names are lowercase slugs; they appear in URLs and CLIs."""


class TxFailure(StrEnum):
    """Stable machine-readable reasons a transaction can fail."""

    SCHEMA = "schema"
    NETWORK = "network"
    SIGNATURE = "signature"
    NONCE = "nonce"
    BALANCE = "balance"
    FEE = "fee"
    TYPE = "type"
    SIZE = "size"
    DUPLICATE = "duplicate"
    WINDOW = "window"
    BOND = "bond"
    REGISTRATION = "registration"


def _fail(reason: TxFailure, message: str) -> NoReturn:
    raise TxValidationError(reason, message)


def tx_body(tx: Transaction) -> dict[str, object]:
    """The canonical signed body of a transaction, without its signature."""
    return {
        "network": tx.network,
        "type": tx.type.value,
        "from": tx.sender,
        "nonce": tx.nonce,
        "fee": tx.fee,
        "payload": normalize(tx.payload),
    }


def txid(tx: Transaction) -> str:
    """``H("sccp/v1/tx", body_without_signature)`` as lowercase hex."""
    return domain_hex(Domain.TX, tx_body(tx))


def tx_size(tx: Transaction) -> int:
    """Canonical byte size of the transaction, used for the size cap."""
    return len(canonical_json(tx.model_dump(mode="json", by_alias=True)))


def sign_transaction(keypair: HybridKeypair, tx: Transaction) -> Transaction:
    """Return ``tx`` with ``keypair``'s hybrid signature over its txid.

    There is exactly one signing scheme. A transaction signature is always the
    3374-byte ``scheme || ed25519 || mldsa65`` blob, so a validator never has to
    infer a scheme from a length or a key it has not authenticated yet.
    """
    if not isinstance(keypair, HybridKeypair):
        raise TxValidationError(
            TxFailure.SIGNATURE, "signing needs a HybridKeypair (the only scheme)"
        )
    unsigned = tx if tx.signature is None else tx.model_copy(update={"signature": None})
    signature = hybrid_sign(keypair, bytes.fromhex(txid(unsigned)))
    return unsigned.model_copy(update={"signature": signature.hex()})


def make_coinbase(
    miner: str,
    amount: int,
    network: str,
    *,
    variant_rewards: Mapping[str, int] | None = None,
) -> Transaction:
    """The single coinbase transaction of a block.

    A coinbase is an unsigned ``transfer`` from the miner to itself: its
    authorisation is the block seal, not a signature, and it is never applied as
    an ordinary transaction (so it consumes no nonce). ``variant_rewards`` maps
    asset ids to non-native tokens the same block mints; it is omitted when the
    block settled only standard chess.
    """
    if amount < 0:
        _fail(TxFailure.TYPE, "coinbase amount must be non-negative")
    payload: dict[str, object] = {"to": miner, "amount": amount}
    if variant_rewards:
        payload["variant_rewards"] = {
            asset: total for asset, total in sorted(variant_rewards.items()) if total > 0
        }
    return Transaction(
        network=network,
        type=TxType.TRANSFER,
        sender=miner,
        nonce=0,
        fee=0,
        payload=payload,
        signature=None,
    )


def coinbase_id(miner: str, amount: int, network: str) -> str:
    """The txid of a native-only coinbase."""
    return txid(make_coinbase(miner, amount, network))


def resolve_public_key(state: State, tx: Transaction) -> str:
    """The sender's public key hex, from state or a first-use reveal."""
    account = state.accounts.get(tx.sender)
    if account is not None and account.public_key:
        return account.public_key
    candidate = tx.payload.get("public_key")
    if isinstance(candidate, str) and is_hybrid_public_key_hex(candidate):
        return candidate
    _fail(
        TxFailure.SIGNATURE,
        f"{tx.sender} has no registered public key and the payload carries none",
    )


def check_sender_key(
    state: State,
    params: NetworkParams,
    tx: Transaction,
    *,
    verify_signature: bool = True,
) -> str:
    """Verify the sender's hybrid signature and return the public key hex it used.

    The key must be the 1985-byte hybrid blob that re-encodes to ``tx.sender``;
    a key of any other length is a schema violation rather than a second scheme,
    and a signature is always the hybrid blob. Nothing here dispatches on a
    scheme: there is only one (docs/design/PROTOCOL.md section 2).

    ``verify_signature=False`` still checks the key encoding, the address it
    derives and the signature's shape, but skips the expensive hybrid
    verification. It exists for pool maintenance, where the immutable
    transaction was already verified at admission against the same txid. Block
    validation never uses it: :func:`sccp.chain.block.apply_block_body` always
    takes the default, so a mempool bug cannot make an invalid block valid.
    """
    key_hex = resolve_public_key(state, tx)
    try:
        raw_key = bytes.fromhex(key_hex)
    except ValueError:
        _fail(TxFailure.SIGNATURE, "public key is not valid hex")
    if len(raw_key) != HYBRID_PUBLIC_KEY_SIZE or not is_hybrid_public_key_hex(key_hex):
        _fail(
            TxFailure.SIGNATURE,
            f"public key must be a {HYBRID_PUBLIC_KEY_SIZE}-byte hybrid blob, got {len(raw_key)}",
        )
    try:
        derived = encode_address(raw_key, params)
    except (KeyEncodingError, AddressError) as exc:
        _fail(TxFailure.SIGNATURE, f"invalid hybrid public key: {exc}")
    if derived != tx.sender:
        _fail(TxFailure.SIGNATURE, f"public key derives {derived}, not the sender {tx.sender}")
    if tx.signature is None:
        _fail(TxFailure.SIGNATURE, "transaction is not signed")
    try:
        signature = bytes.fromhex(tx.signature)
    except ValueError:
        _fail(TxFailure.SIGNATURE, "signature is not valid hex")
    if verify_signature and not hybrid_verify(raw_key, bytes.fromhex(txid(tx)), signature):
        _fail(TxFailure.SIGNATURE, "hybrid signature does not cover the txid")
    return key_hex


# -- payload field readers ----------------------------------------------------


def _field_int(payload: Mapping[str, object], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be an integer")
    if value < minimum:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be at least {minimum}")
    return value


def _field_text(payload: Mapping[str, object], key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_SAFE_TEXT:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be printable text")
    if not value.isprintable():
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be printable text")
    return value


def _field_token_name(payload: Mapping[str, object], key: str) -> str:
    value = _field_text(payload, key)
    assert value is not None
    if TOKEN_NAME_PATTERN.fullmatch(value) is None:
        _fail(
            TxFailure.SCHEMA,
            f"payload field {key!r} must be a lowercase slug of 1-32 letters, digits, '-' or '_'",
        )
    return value


def _field_optional_int(payload: Mapping[str, object], key: str, *, minimum: int = 1) -> int | None:
    """An optional positive integer; absent means the protocol default."""
    if key not in payload:
        return None
    return _field_int(payload, key, minimum=minimum)


def _field_address(payload: Mapping[str, object], key: str, params: NetworkParams) -> str:
    value = _field_text(payload, key)
    if value is None or not validate_address(value, params):
        _fail(TxFailure.SCHEMA, f"payload field {key!r} is not a {params.network_id} address")
    return value


def _field_digest(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != 64:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be a 32-byte lowercase hex digest")
    try:
        bytes.fromhex(value)
    except ValueError:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be lowercase hex")
    if value != value.lower():
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be lowercase hex")
    return value


def htlc_id(sender: str, nonce: int) -> str:
    """``H("sccp/v1/htlc", {from, nonce})``: the id an HTLC lock creates.

    The id is derived from the sender and nonce, not from the transaction id, so
    a wallet can show the id before the transaction is signed and both sides of a
    swap can name the same record. A nonce is used once, so the id is unique.
    """
    return domain_hex(Domain.HTLC, {"from": sender, "nonce": nonce})


def _field_preimage(payload: Mapping[str, object], key: str) -> str:
    """Read a 32-byte preimage.

    Ethereum's ``bytes32`` and SCCP's ``sha256`` check take the same 32 bytes;
    allowing variable-length input would create two spellings of one secret and a
    way to claim an HTLC with a value the other chain cannot represent.
    """
    value = _field_digest(payload, key)
    return value


def _check_unknown_keys(payload: Mapping[str, object], allowed: frozenset[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _fail(TxFailure.SCHEMA, f"unknown payload fields: {unknown}")


def _require_balance(state: State, address: str, amount: int, what: str) -> None:
    available = state.spendable(address)
    if available < amount:
        _fail(
            TxFailure.BALANCE,
            f"{address} cannot cover {what} of {amount} with a spendable balance of {available}",
        )


def _require_trade_funds(
    state: State,
    address: str,
    asset: str,
    amount: int,
    fee: int,
    what: str,
) -> None:
    """Require the fee in ZQWX and ``amount`` of ``asset``, native or token."""
    _require_balance(state, address, fee, "the fee")
    if asset == dex.NATIVE_ASSET:
        _require_balance(state, address, amount + fee, what)
        return
    available = state.asset_balance(address, asset)
    if available < amount:
        _fail(
            TxFailure.BALANCE,
            f"{address} has {available} of {asset}, cannot cover {what} of {amount}",
        )


def _field_trade_asset(payload: Mapping[str, object], key: str, state: State) -> str:
    """Read a pool side: an omitted key is native ZQWX, a value is an asset id."""
    value = payload.get(key)
    if value is None:
        if key in payload:
            _fail(TxFailure.SCHEMA, f"native ZQWX must omit payload field {key!r}")
        return dex.NATIVE_ASSET
    if not isinstance(value, str) or not value:
        _fail(TxFailure.SCHEMA, f"payload field {key!r} must be an asset id")
    try:
        variant = state.variant_for_asset(value)
    except ConfigError as exc:
        _fail(TxFailure.TYPE, str(exc))
    if variant.native:
        _fail(TxFailure.TYPE, f"native ZQWX must omit payload field {key!r}")
    return value


def _debit_trade_asset(state: State, address: str, asset: str, amount: int) -> None:
    if asset == dex.NATIVE_ASSET:
        state.ensure(address).balance -= amount
        return
    state.debit_asset(address, asset, amount)


def _credit_trade_asset(state: State, address: str, asset: str, amount: int) -> None:
    if asset == dex.NATIVE_ASSET:
        state.ensure(address).balance += amount
        return
    state.credit_asset(address, asset, amount)


# -- per-type validation ------------------------------------------------------

ALLOWED: dict[TxType, frozenset[str]] = {
    TxType.TRANSFER: frozenset({"to", "amount", "asset", "public_key"}),
    TxType.REGISTER: frozenset({"public_key", "endpoint", "engine", "bond", "variant"}),
    TxType.REGISTER_WITNESS: frozenset({"public_key", "endpoint", "bond"}),
    TxType.UPDATE_REGISTRATION: frozenset(
        {"endpoint", "engine", "extend_epochs", "bond", "public_key"}
    ),
    TxType.UNREGISTER: frozenset({"public_key"}),
    TxType.WITHDRAW_BOND: frozenset({"public_key"}),
    TxType.AVAILABILITY: frozenset({"epoch", "max_games", "commitment", "public_key"}),
    TxType.REVEAL: frozenset({"epoch", "nonce", "public_key"}),
    TxType.TIMEOUT_CLAIM: frozenset(
        {"match_id", "certificate", "last_move", "clock_evidence", "bond", "public_key"}
    ),
    TxType.TIMEOUT_DISPUTE: frozenset(
        {"claim_id", "move", "move_signature", "clock_evidence", "public_key"}
    ),
    TxType.SLASH_EVIDENCE: frozenset({"proof_id", "evidence", "public_key"}),
    TxType.CREATE_POOL: frozenset({"asset_a", "amount_a", "asset_b", "amount_b", "public_key"}),
    TxType.ADD_LIQUIDITY: frozenset({"pool_id", "amount_a", "amount_b", "public_key"}),
    TxType.REMOVE_LIQUIDITY: frozenset({"pool_id", "shares", "public_key"}),
    TxType.SWAP: frozenset({"pool_id", "asset_in", "amount_in", "min_amount_out", "public_key"}),
    TxType.HTLC_LOCK: frozenset(
        {"hashlock", "timelock_height", "beneficiary", "amount", "asset", "public_key"}
    ),
    TxType.HTLC_CLAIM: frozenset({"htlc_id", "preimage", "public_key"}),
    TxType.HTLC_REFUND: frozenset({"htlc_id", "public_key"}),
    TxType.REGISTER_TOKEN: frozenset(
        {
            "ticket",
            "variant",
            "ruleset",
            "symbol",
            "display_name",
            "start_fen",
            "openings",
            "decimals",
            "initial_supply",
            "subsidy",
            "halving_interval",
            "max_supply",
            "min_plies",
            "max_games_per_block",
            "opening_book",
            "time_control_initial_ms",
            "time_control_increment_ms",
            "time_control_delay_ms",
            "time_control_first_moves",
            "time_control_periods",
            "public_key",
        }
    ),
}
"""Exactly which payload keys each type may carry."""


def _validate_transfer(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.TRANSFER])
    amount = _field_int(tx.payload, "amount", minimum=1)
    to = _field_address(tx.payload, "to", params)
    if to == tx.sender:
        _fail(TxFailure.TYPE, "a transfer must name a different recipient")
    asset = tx.payload.get("asset")
    if asset is None:
        _require_balance(state, tx.sender, amount + tx.fee, "the transfer")
        return
    if not isinstance(asset, str) or not asset:
        _fail(TxFailure.SCHEMA, "payload field 'asset' must be an asset id")
    try:
        variant = state.variant_for_asset(asset)
    except ConfigError as exc:
        _fail(TxFailure.TYPE, str(exc))
    if variant.native:
        _fail(TxFailure.TYPE, "native ZQWX transfers must omit the asset field")
    # Fees stay in ZQWX even when the transferred amount is a variant token.
    _require_balance(state, tx.sender, tx.fee, "the fee")
    if state.asset_balance(tx.sender, asset) < amount:
        _fail(
            TxFailure.BALANCE,
            f"{tx.sender} has {state.asset_balance(tx.sender, asset)} of {variant.symbol}, "
            f"cannot send {amount}",
        )


def _validate_register(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REGISTER])
    bond = _field_int(tx.payload, "bond")
    endpoint = _field_text(tx.payload, "endpoint")
    engine = _field_text(tx.payload, "engine")
    variant = tx.payload.get("variant", "standard")
    if not isinstance(variant, str) or not variant:
        _fail(TxFailure.SCHEMA, "payload field 'variant' must be a variant name")
    try:
        state.variant(variant)
    except ConfigError as exc:
        _fail(TxFailure.TYPE, str(exc))
    if bond < params.min_bond:
        _fail(TxFailure.BOND, f"register needs a bond of at least {params.min_bond}, got {bond}")
    account = state.account(tx.sender)
    current = epoch_of(height, params)
    if (
        account.registered_until_epoch is not None
        and account.registered_until_epoch >= current
        and account.variant != variant
    ):
        _fail(
            TxFailure.REGISTRATION,
            f"{tx.sender} is registered for {account.variant!r}; unregister before "
            f"switching to {variant!r}",
        )
    if height < account.banned_until:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is banned until height {account.banned_until}")
    _require_balance(state, tx.sender, bond + tx.fee, "the registration bond")
    assert endpoint is not None and engine is not None


def _field_ticket(payload: Mapping[str, object]) -> str:
    """A ticket is a unique registration serial: 16-hex or a short four-digit id."""
    ticket = payload.get("ticket")
    if not isinstance(ticket, str) or re.fullmatch(TOKEN_TICKET_PATTERN, ticket) is None:
        _fail(
            TxFailure.SCHEMA,
            "payload field 'ticket' must be four digits or 16 lowercase hex characters",
        )
    return ticket


def _field_openings(
    payload: Mapping[str, object],
    ruleset_name: str,
    *,
    start_fen: str | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Token opening lines, canonicalised from SAN or UCI by the ruleset."""
    raw = payload.get("openings")
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > 256:
        _fail(TxFailure.SCHEMA, "payload field 'openings' must be an array of at most 256 lines")
    lines: list[tuple[str, ...]] = []
    for index, line in enumerate(raw):
        if isinstance(line, str):
            tokens: list[str] | str = line
        elif isinstance(line, list) and all(isinstance(move, str) for move in line):
            tokens = line
        else:
            _fail(
                TxFailure.SCHEMA,
                f"opening {index} must be a SAN string or an array of SAN moves",
            )
        try:
            canonical = canonical_opening_line(ruleset_name, tokens, start_fen=start_fen)
        except ConfigError as exc:
            _fail(TxFailure.TYPE, f"opening {index}: {exc}")
        if len(canonical) > MAX_TOKEN_OPENING_PLIES:
            _fail(
                TxFailure.TYPE,
                f"opening {index} has more than {MAX_TOKEN_OPENING_PLIES} plies",
            )
        lines.append(canonical)
    return tuple(lines)


def _field_time_control_periods(
    payload: Mapping[str, object],
) -> tuple[int | None, tuple[TimeControlPeriod, ...]]:
    """Parse the FIDE-style later periods carried by a token registration."""
    first_moves = (
        _field_int(payload, "time_control_first_moves", minimum=1)
        if "time_control_first_moves" in payload
        else None
    )
    raw = payload.get("time_control_periods")
    if raw is None:
        periods: tuple[TimeControlPeriod, ...] = ()
    elif not isinstance(raw, list) or len(raw) > 64:
        _fail(
            TxFailure.SCHEMA,
            "payload field 'time_control_periods' must be an array of at most 64 periods",
        )
    else:
        parsed: list[TimeControlPeriod] = []
        for index, period in enumerate(raw):
            if not isinstance(period, Mapping):
                _fail(TxFailure.SCHEMA, f"time-control period {index} must be an object")
            try:
                parsed.append(TimeControlPeriod.model_validate(period))
            except ValidationError as exc:
                detail = "; ".join(error["msg"] for error in exc.errors())
                _fail(TxFailure.SCHEMA, f"time-control period {index}: {detail}")
        periods = tuple(parsed)
    return first_moves, periods


def _parse_token_registration(
    params: NetworkParams, payload: Mapping[str, object]
) -> tuple[VariantParams, int]:
    """Build the token a ``register_token`` payload describes.

    Both validation and application call this function, so the definition that
    is checked is byte-for-byte the definition that is stored. A ticketed token
    uses the ticket as a unique registration serial and keeps the user-chosen
    name and ticker as display metadata; the legacy name-based payload is still
    accepted for pre-ticket chains.
    """
    symbol: str | None
    display_name: str | None
    if "ticket" in payload:
        ticket = _field_ticket(payload)
        if "variant" in payload:
            _fail(
                TxFailure.SCHEMA,
                "a ticketed token derives its internal variant name from the ticket; "
                "remove 'variant'",
            )
        name = token_variant_name(ticket)
        symbol = _field_text(payload, "symbol")
        display_name = _field_text(payload, "display_name")
    else:
        ticket = None
        name = _field_token_name(payload, "variant")
        symbol = _field_text(payload, "symbol")
        display_name = _field_text(payload, "display_name")
    assert symbol is not None and display_name is not None
    ruleset_name = _field_token_name(payload, "ruleset")
    try:
        ruleset = ruleset_for(ruleset_name)
    except ConfigError as exc:
        _fail(TxFailure.TYPE, str(exc))
    start_fen = _field_text(payload, "start_fen", required=False)
    if start_fen is not None:
        try:
            start_fen = normalise_token_start_fen(ruleset, start_fen)
        except ConfigError as exc:
            _fail(TxFailure.TYPE, f"start_fen: {exc}")
    openings = _field_openings(payload, ruleset.name, start_fen=start_fen)
    first_period_moves, periods = _field_time_control_periods(payload)

    initial_supply = (
        _field_int(payload, "initial_supply", minimum=0) if "initial_supply" in payload else 0
    )
    subsidy = _field_int(payload, "subsidy", minimum=1)
    decimals = _field_int(payload, "decimals", minimum=0) if "decimals" in payload else 8
    halving_interval = _field_optional_int(payload, "halving_interval")
    max_supply = _field_optional_int(payload, "max_supply")
    min_plies = (
        _field_int(payload, "min_plies", minimum=1) if "min_plies" in payload else params.min_plies
    )
    max_games_per_block = (
        _field_int(payload, "max_games_per_block", minimum=1)
        if "max_games_per_block" in payload
        else 1
    )
    if "opening_book" in payload:
        opening_book = _field_text(payload, "opening_book")
    elif ticket is not None and openings:
        opening_book = f"token-{ticket}"
    elif start_fen is not None:
        opening_book = "none"
    else:
        opening_book = params.book_version
    initial_ms = (
        _field_int(payload, "time_control_initial_ms", minimum=1)
        if "time_control_initial_ms" in payload
        else DEFAULT_TIME_CONTROL_INITIAL_MS
    )
    increment_ms = (
        _field_int(payload, "time_control_increment_ms", minimum=0)
        if "time_control_increment_ms" in payload
        else DEFAULT_TIME_CONTROL_INCREMENT_MS
    )
    delay_ms = (
        _field_int(payload, "time_control_delay_ms", minimum=0)
        if "time_control_delay_ms" in payload
        else DEFAULT_TIME_CONTROL_DELAY_MS
    )
    for label, value in (
        ("initial_supply", initial_supply),
        ("subsidy", subsidy),
        ("max_supply", max_supply),
    ):
        if value is not None and value > MAX_TOKEN_AMOUNT:
            _fail(TxFailure.TYPE, f"{label} exceeds the {MAX_TOKEN_AMOUNT}-unit ceiling")
    if min_plies > params.max_plies:
        _fail(
            TxFailure.TYPE,
            f"min_plies {min_plies} exceeds the network's max_plies {params.max_plies}",
        )
    if max_supply is not None and initial_supply > max_supply * units_per_coin(decimals):
        _fail(
            TxFailure.TYPE,
            f"initial_supply {initial_supply} exceeds the {max_supply}-coin supply cap",
        )

    asset = (
        derive_token_asset_id(ticket, decimals, ruleset_name)
        if ticket is not None
        else derive_variant_asset_id(name, symbol, decimals, ruleset_name)
    )
    try:
        token = VariantParams(
            name=name,
            ticket=ticket,
            rules=ruleset.pgn_rules,
            ruleset=ruleset_name,
            start_fen=start_fen,
            asset=asset,
            symbol=symbol,
            display_name=display_name,
            decimals=decimals,
            subsidy=subsidy,
            halving_interval=halving_interval,
            max_supply=max_supply,
            min_plies=min_plies,
            max_games_per_block=max_games_per_block,
            opening_book=opening_book or params.book_version,
            time_control_initial_ms=initial_ms,
            time_control_increment_ms=increment_ms,
            time_control_delay_ms=delay_ms,
            time_control_first_moves=first_period_moves,
            time_control_periods=periods,
            openings=openings,
        )
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'token'}: {error['msg']}"
            for error in exc.errors()
        )
        _fail(TxFailure.SCHEMA, f"invalid token parameters: {detail}")
    return token, initial_supply


def _validate_register_token(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    """Validate a permissionless token registration.

    A registration fixes every consensus-relevant token parameter at creation
    time. There is no issuer key and no later mutation: the derived asset id
    commits to the unique ticket, decimals and ruleset, so a transfer or a DEX
    pool names exactly one token definition while display names and tickers may
    repeat.
    """
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REGISTER_TOKEN])
    token, _ = _parse_token_registration(params, tx.payload)
    if token.ticket is not None and token.ticket in state.token_tickets():
        _fail(TxFailure.TYPE, f"ticket {token.ticket} is already registered")
    if token.name in state.variant_names():
        _fail(
            TxFailure.TYPE,
            f"variant {token.name!r} is already known; a token's variant name must be new",
        )
    if any(candidate.asset == token.asset for candidate in state.params.variants) or any(
        existing.variant.asset == token.asset for existing in state.tokens.values()
    ):
        _fail(TxFailure.TYPE, f"asset id {token.asset} is already registered")
    _require_balance(state, tx.sender, tx.fee, "the token registration fee")


def _validate_register_witness(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REGISTER_WITNESS])
    bond = _field_int(tx.payload, "bond")
    if bond < params.clock_witness_bond:
        _fail(
            TxFailure.BOND,
            f"a clock witness needs a bond of at least {params.clock_witness_bond}, got {bond}",
        )
    if tx.sender in state.witnesses:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is already a registered witness")
    _field_text(tx.payload, "endpoint")
    _require_balance(state, tx.sender, bond + tx.fee, "the witness bond")


def _validate_update_registration(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.UPDATE_REGISTRATION])
    account = state.account(tx.sender)
    if account.registered_until_epoch is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered")
    extend = _field_int(tx.payload, "extend_epochs") if "extend_epochs" in tx.payload else 0
    top_up = _field_int(tx.payload, "bond") if "bond" in tx.payload else 0
    if extend == 0 and top_up == 0 and "endpoint" not in tx.payload and "engine" not in tx.payload:
        _fail(TxFailure.TYPE, "update_registration changes nothing")
    for key in ("endpoint", "engine"):
        if key in tx.payload:
            _field_text(tx.payload, key)
    _require_balance(state, tx.sender, top_up + tx.fee, "the bond top-up")


def _validate_unregister(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.UNREGISTER])
    account = state.account(tx.sender)
    if account.registered_until_epoch is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered")
    if account.unbonding_height is not None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is already unbonding")


def _validate_withdraw_bond(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.WITHDRAW_BOND])
    account = state.account(tx.sender)
    if account.unbonding_height is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} has not started unbonding")
    if height < account.unbonding_height:
        _fail(
            TxFailure.WINDOW,
            f"bond is locked until height {account.unbonding_height}, this block is {height}",
        )
    if account.bonded == 0:
        _fail(TxFailure.BOND, f"{tx.sender} has no bond to withdraw")


def _validate_availability(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.AVAILABILITY])
    epoch = _field_int(tx.payload, "epoch")
    max_games = _field_int(tx.payload, "max_games", minimum=1)
    commitment_hex = _field_digest(tx.payload, "commitment")
    current = epoch_of(height, params)
    expected = current + params.availability_lead
    if epoch != expected:
        _fail(
            TxFailure.WINDOW,
            f"availability for epoch {epoch} belongs in epoch "
            f"{epoch - params.availability_lead}; this block is in epoch {current}",
        )
    if max_games > params.rounds_per_epoch:
        _fail(
            TxFailure.TYPE,
            f"max_games {max_games} exceeds the {params.rounds_per_epoch}-game epoch quota",
        )
    account = state.account(tx.sender)
    if account.registered_until_epoch is None or account.registered_until_epoch < epoch:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} is not registered through epoch {epoch}")
    if account.bonded < params.min_bond:
        _fail(TxFailure.BOND, f"{tx.sender} does not hold the minimum bond")
    existing = state.availability.get(epoch, {}).get(tx.sender)
    if existing is not None and existing.commitment != commitment_hex:
        _fail(TxFailure.DUPLICATE, f"{tx.sender} already committed different availability")


def _validate_reveal(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REVEAL])
    epoch = _field_int(tx.payload, "epoch")
    nonce = _field_digest(tx.payload, "nonce")
    current = epoch_of(height, params)
    if epoch != current + 1:
        _fail(
            TxFailure.WINDOW,
            f"reveal for epoch {epoch} is only valid in epoch {epoch - 1}; "
            f"this block is in epoch {current}",
        )
    committed = state.availability.get(epoch, {}).get(tx.sender)
    if committed is None:
        _fail(TxFailure.REGISTRATION, f"{tx.sender} committed no availability for epoch {epoch}")
    if committed.epoch < first_matchable_epoch(params):
        _fail(TxFailure.WINDOW, f"epoch {epoch} is not matchable")
    if commitment(tx.sender, epoch, nonce) != committed.commitment:
        _fail(TxFailure.TYPE, "reveal nonce does not match the published commitment")
    existing = state.reveals.get(epoch, {}).get(tx.sender)
    if existing is not None and existing != nonce:
        _fail(TxFailure.DUPLICATE, f"{tx.sender} already revealed a different nonce")


def _validate_create_pool(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.CREATE_POOL])
    asset_a = _field_trade_asset(tx.payload, "asset_a", state)
    asset_b = _field_trade_asset(tx.payload, "asset_b", state)
    amount_a = _field_int(tx.payload, "amount_a", minimum=1)
    amount_b = _field_int(tx.payload, "amount_b", minimum=1)
    if asset_a == asset_b:
        _fail(TxFailure.TYPE, "a pool needs two different assets")
    asset_a, amount_a, asset_b, amount_b = dex.normalize_pair(asset_a, amount_a, asset_b, amount_b)
    pool_id = dex.pool_id(asset_a, asset_b)
    if pool_id in state.pools:
        _fail(TxFailure.DUPLICATE, f"pool {pool_id} already exists")
    if dex.initial_shares(amount_a, amount_b) <= 0:
        _fail(TxFailure.TYPE, "the initial deposit is too small to mint a share")
    _require_trade_funds(state, tx.sender, asset_a, amount_a, tx.fee, "the first side")
    _require_trade_funds(state, tx.sender, asset_b, amount_b, tx.fee, "the second side")


def _validate_add_liquidity(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.ADD_LIQUIDITY])
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools.get(pool_id)
    if pool is None:
        _fail(TxFailure.TYPE, f"unknown pool {pool_id}")
    amount_a = _field_int(tx.payload, "amount_a", minimum=1)
    amount_b = _field_int(tx.payload, "amount_b", minimum=1)
    if dex.added_shares(pool.reserve_a, pool.reserve_b, pool.total_shares, amount_a, amount_b) <= 0:
        _fail(TxFailure.TYPE, "the deposit is too small to mint a share")
    _require_trade_funds(state, tx.sender, pool.asset_a, amount_a, tx.fee, "the deposit")
    _require_trade_funds(state, tx.sender, pool.asset_b, amount_b, tx.fee, "the deposit")


def _validate_remove_liquidity(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.REMOVE_LIQUIDITY])
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools.get(pool_id)
    if pool is None:
        _fail(TxFailure.TYPE, f"unknown pool {pool_id}")
    shares = _field_int(tx.payload, "shares", minimum=1)
    owned = pool.shares.get(tx.sender, 0)
    if shares > owned:
        _fail(TxFailure.BALANCE, f"{tx.sender} has {owned} pool shares, cannot remove {shares}")
    amount_a, amount_b = dex.removed_amounts(
        pool.reserve_a, pool.reserve_b, pool.total_shares, shares
    )
    if amount_a <= 0 or amount_b <= 0:
        _fail(TxFailure.TYPE, "the redemption is too small to pay both sides")


def _validate_swap(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.SWAP])
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools.get(pool_id)
    if pool is None:
        _fail(TxFailure.TYPE, f"unknown pool {pool_id}")
    asset_in = _field_trade_asset(tx.payload, "asset_in", state)
    if asset_in not in (pool.asset_a, pool.asset_b):
        _fail(TxFailure.TYPE, f"{asset_in or 'ZQWX'} is not a side of pool {pool_id}")
    amount_in = _field_int(tx.payload, "amount_in", minimum=1)
    minimum_out = _field_int(tx.payload, "min_amount_out")
    if asset_in == pool.asset_a:
        reserve_in, reserve_out = pool.reserve_a, pool.reserve_b
    else:
        reserve_in, reserve_out = pool.reserve_b, pool.reserve_a
    amount_out = dex.swap_output(reserve_in, reserve_out, amount_in, params.dex_fee_bps)
    if amount_out <= 0:
        _fail(TxFailure.TYPE, "the swap output is zero")
    if amount_out < minimum_out:
        _fail(
            TxFailure.TYPE,
            f"the swap pays {amount_out}, below the requested minimum of {minimum_out}",
        )
    _require_trade_funds(state, tx.sender, asset_in, amount_in, tx.fee, "the swap input")


def _require_htlc_close_fee(state: State, address: str, record: HtlcRecord, fee: int) -> None:
    """Require a fee, allowing a native HTLC's proceeds to pay it.

    A bridge taker may hold no ZQWX at all: the whole point is to buy some with
    ETH. Requiring a pre-existing native balance would make the first swap
    impossible, so a native claim or refund may pay the network fee out of the
    amount being released. A token HTLC still needs ZQWX for the fee, exactly as
    every other token transaction does.
    """
    available = state.spendable(address)
    if record.asset == dex.NATIVE_ASSET:
        if available + record.amount < fee:
            _fail(
                TxFailure.BALANCE,
                f"{address} needs {fee} for the fee and the htlc pays only {record.amount}",
            )
        return
    if available < fee:
        _fail(TxFailure.BALANCE, f"{address} cannot cover the {fee}-unit fee")


def _validate_htlc_lock(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.HTLC_LOCK])
    _field_digest(tx.payload, "hashlock")
    timelock = _field_int(tx.payload, "timelock_height")
    if timelock <= height:
        _fail(
            TxFailure.WINDOW,
            f"an htlc timelock of {timelock} is not after the current height {height}",
        )
    _field_address(tx.payload, "beneficiary", params)
    amount = _field_int(tx.payload, "amount", minimum=1)
    asset = _field_trade_asset(tx.payload, "asset", state)
    _require_trade_funds(state, tx.sender, asset, amount, tx.fee, "the htlc lock")


def _validate_htlc_claim(params: NetworkParams, state: State, tx: Transaction, height: int) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.HTLC_CLAIM])
    record = state.htlcs.get(_field_digest(tx.payload, "htlc_id"))
    if record is None:
        _fail(TxFailure.TYPE, "unknown htlc")
    if record.status is not HtlcStatus.LOCKED:
        _fail(TxFailure.WINDOW, f"the htlc is already {record.status.value}")
    if tx.sender != record.beneficiary:
        _fail(
            TxFailure.SIGNATURE,
            f"the htlc pays {record.beneficiary}, not the claim's sender {tx.sender}",
        )
    if height >= record.timelock_height:
        _fail(
            TxFailure.WINDOW,
            f"the htlc expired at height {record.timelock_height}; only a refund is valid",
        )
    preimage = _field_preimage(tx.payload, "preimage")
    actual = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if actual != record.hashlock:
        _fail(TxFailure.TYPE, "the preimage does not open the htlc hashlock")
    _require_htlc_close_fee(state, tx.sender, record, tx.fee)


def _validate_htlc_refund(
    params: NetworkParams, state: State, tx: Transaction, height: int
) -> None:
    _check_unknown_keys(tx.payload, ALLOWED[TxType.HTLC_REFUND])
    record = state.htlcs.get(_field_digest(tx.payload, "htlc_id"))
    if record is None:
        _fail(TxFailure.TYPE, "unknown htlc")
    if record.status is not HtlcStatus.LOCKED:
        _fail(TxFailure.WINDOW, f"the htlc is already {record.status.value}")
    if tx.sender != record.sender:
        _fail(TxFailure.SIGNATURE, f"only {record.sender} may refund this htlc")
    if height < record.timelock_height:
        _fail(
            TxFailure.WINDOW,
            f"the htlc cannot be refunded before height {record.timelock_height}",
        )
    _require_htlc_close_fee(state, tx.sender, record, tx.fee)


VALIDATORS = {
    TxType.TRANSFER: _validate_transfer,
    TxType.REGISTER: _validate_register,
    TxType.REGISTER_WITNESS: _validate_register_witness,
    TxType.UPDATE_REGISTRATION: _validate_update_registration,
    TxType.UNREGISTER: _validate_unregister,
    TxType.WITHDRAW_BOND: _validate_withdraw_bond,
    TxType.AVAILABILITY: _validate_availability,
    TxType.REVEAL: _validate_reveal,
    TxType.CREATE_POOL: _validate_create_pool,
    TxType.ADD_LIQUIDITY: _validate_add_liquidity,
    TxType.REMOVE_LIQUIDITY: _validate_remove_liquidity,
    TxType.SWAP: _validate_swap,
    TxType.HTLC_LOCK: _validate_htlc_lock,
    TxType.HTLC_CLAIM: _validate_htlc_claim,
    TxType.HTLC_REFUND: _validate_htlc_refund,
    TxType.REGISTER_TOKEN: _validate_register_token,
}
"""Handlers for every type that :func:`validate_transaction` owns outright."""


# -- application --------------------------------------------------------------


def _apply_transfer(state: State, tx: Transaction, height: int) -> None:
    amount = _field_int(tx.payload, "amount", minimum=1)
    to = tx.payload.get("to")
    if not isinstance(to, str):
        _fail(TxFailure.SCHEMA, "payload field 'to' must be an address")
    asset = tx.payload.get("asset")
    if asset is None:
        state.ensure(tx.sender).balance -= amount
        state.ensure(to).balance += amount
        return
    if not isinstance(asset, str):
        _fail(TxFailure.SCHEMA, "payload field 'asset' must be an asset id")
    state.debit_asset(tx.sender, asset, amount)
    state.credit_asset(to, asset, amount)


def _apply_register(state: State, tx: Transaction, height: int) -> None:
    bond = _field_int(tx.payload, "bond")
    account = state.ensure(tx.sender)
    account.balance -= bond
    account.bonded += bond
    account.endpoint = _field_text(tx.payload, "endpoint")
    account.engine = _field_text(tx.payload, "engine")
    if "variant" in tx.payload:
        account.variant = _field_text(tx.payload, "variant") or "standard"
    account.registered_until_epoch = (
        epoch_of(height, state.params) + state.params.registration_epochs
    )


def _apply_register_token(state: State, tx: Transaction, height: int) -> None:
    token, initial_supply = _parse_token_registration(state.params, tx.payload)
    record = TokenRecord(
        name=token.name,
        variant=token,
        owner=tx.sender,
        registered_at_height=height,
        initial_supply=initial_supply,
    )
    state.tokens[token.name] = record
    if initial_supply:
        assert token.asset is not None
        state.credit_asset(tx.sender, token.asset, initial_supply)
        state.minted_assets[token.asset] = state.minted_assets.get(token.asset, 0) + initial_supply


def _apply_register_witness(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain.state import WitnessRecord

    bond = _field_int(tx.payload, "bond")
    endpoint = _field_text(tx.payload, "endpoint") or ""
    account = state.ensure(tx.sender)
    account.balance -= bond
    account.bonded += bond
    state.witnesses[tx.sender] = WitnessRecord(
        public_key=account.public_key or "",
        endpoint=endpoint,
        bond=bond,
        registered_at_height=height,
        until_epoch=epoch_of(height, state.params) + state.params.registration_epochs,
    )


def _apply_update_registration(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    if "extend_epochs" in tx.payload:
        account.registered_until_epoch = (account.registered_until_epoch or 0) + _field_int(
            tx.payload, "extend_epochs"
        )
    if "bond" in tx.payload:
        top_up = _field_int(tx.payload, "bond")
        account.balance -= top_up
        account.bonded += top_up
    if "endpoint" in tx.payload:
        account.endpoint = _field_text(tx.payload, "endpoint")
    if "engine" in tx.payload:
        account.engine = _field_text(tx.payload, "engine")


def _apply_unregister(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    current = epoch_of(height, state.params)
    if account.registered_until_epoch is not None:
        account.registered_until_epoch = min(account.registered_until_epoch, current)
    account.unbonding_height = height + state.params.unbond_delay


def _apply_withdraw_bond(state: State, tx: Transaction, height: int) -> None:
    account = state.ensure(tx.sender)
    account.balance += account.bonded
    account.bonded = 0
    account.unbonding_height = None
    account.registered_until_epoch = None
    state.witnesses.pop(tx.sender, None)


def _apply_availability(state: State, tx: Transaction, height: int) -> None:
    epoch = _field_int(tx.payload, "epoch")
    max_games = _field_int(tx.payload, "max_games", minimum=1)
    entry = Availability(
        network=state.network,
        miner=tx.sender,
        epoch=epoch,
        max_games=max_games,
        commitment=_field_digest(tx.payload, "commitment"),
    )
    state.availability.setdefault(epoch, {})[tx.sender] = entry


def _apply_reveal(state: State, tx: Transaction, height: int) -> None:
    epoch = _field_int(tx.payload, "epoch")
    state.reveals.setdefault(epoch, {})[tx.sender] = _field_digest(tx.payload, "nonce")


def _apply_timeout_claim(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    record = timeout.validate_claim(state, tx, height)
    state.ensure(tx.sender).balance -= record.bond
    state.timeout_claims[record.claim_id] = record


def _apply_timeout_dispute(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    timeout.apply_dispute(state, tx, height)


def _apply_slash_evidence(state: State, tx: Transaction, height: int) -> None:
    from sccp.chain import timeout

    timeout.apply_slash(state, tx, height)


def _apply_create_pool(state: State, tx: Transaction, height: int) -> None:
    asset_a = _field_trade_asset(tx.payload, "asset_a", state)
    asset_b = _field_trade_asset(tx.payload, "asset_b", state)
    amount_a = _field_int(tx.payload, "amount_a", minimum=1)
    amount_b = _field_int(tx.payload, "amount_b", minimum=1)
    asset_a, amount_a, asset_b, amount_b = dex.normalize_pair(asset_a, amount_a, asset_b, amount_b)
    pool_id = dex.pool_id(asset_a, asset_b)
    shares = dex.initial_shares(amount_a, amount_b)
    _debit_trade_asset(state, tx.sender, asset_a, amount_a)
    _debit_trade_asset(state, tx.sender, asset_b, amount_b)
    state.pools[pool_id] = Pool(
        pool_id=pool_id,
        asset_a=asset_a,
        asset_b=asset_b,
        reserve_a=amount_a,
        reserve_b=amount_b,
        total_shares=shares,
        shares={tx.sender: shares},
    )


def _apply_add_liquidity(state: State, tx: Transaction, height: int) -> None:
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools[pool_id]
    amount_a = _field_int(tx.payload, "amount_a", minimum=1)
    amount_b = _field_int(tx.payload, "amount_b", minimum=1)
    shares = dex.added_shares(pool.reserve_a, pool.reserve_b, pool.total_shares, amount_a, amount_b)
    _debit_trade_asset(state, tx.sender, pool.asset_a, amount_a)
    _debit_trade_asset(state, tx.sender, pool.asset_b, amount_b)
    pool.reserve_a += amount_a
    pool.reserve_b += amount_b
    pool.total_shares += shares
    pool.shares[tx.sender] = pool.shares.get(tx.sender, 0) + shares


def _apply_remove_liquidity(state: State, tx: Transaction, height: int) -> None:
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools[pool_id]
    shares = _field_int(tx.payload, "shares", minimum=1)
    amount_a, amount_b = dex.removed_amounts(
        pool.reserve_a, pool.reserve_b, pool.total_shares, shares
    )
    remaining = pool.shares[tx.sender] - shares
    if remaining:
        pool.shares[tx.sender] = remaining
    else:
        pool.shares.pop(tx.sender, None)
    pool.reserve_a -= amount_a
    pool.reserve_b -= amount_b
    pool.total_shares -= shares
    _credit_trade_asset(state, tx.sender, pool.asset_a, amount_a)
    _credit_trade_asset(state, tx.sender, pool.asset_b, amount_b)
    if pool.total_shares == 0:
        del state.pools[pool_id]


def _apply_swap(state: State, tx: Transaction, height: int) -> None:
    pool_id = _field_digest(tx.payload, "pool_id")
    pool = state.pools[pool_id]
    asset_in = _field_trade_asset(tx.payload, "asset_in", state)
    amount_in = _field_int(tx.payload, "amount_in", minimum=1)
    if asset_in == pool.asset_a:
        reserve_in, reserve_out = pool.reserve_a, pool.reserve_b
        amount_out = dex.swap_output(reserve_in, reserve_out, amount_in, state.params.dex_fee_bps)
        _debit_trade_asset(state, tx.sender, pool.asset_a, amount_in)
        pool.reserve_a += amount_in
        pool.reserve_b -= amount_out
        _credit_trade_asset(state, tx.sender, pool.asset_b, amount_out)
        return
    reserve_in, reserve_out = pool.reserve_b, pool.reserve_a
    amount_out = dex.swap_output(reserve_in, reserve_out, amount_in, state.params.dex_fee_bps)
    _debit_trade_asset(state, tx.sender, pool.asset_b, amount_in)
    pool.reserve_b += amount_in
    pool.reserve_a -= amount_out
    _credit_trade_asset(state, tx.sender, pool.asset_a, amount_out)


def _apply_htlc_lock(state: State, tx: Transaction, height: int) -> None:
    lock_id = htlc_id(tx.sender, tx.nonce)
    asset = _field_trade_asset(tx.payload, "asset", state)
    amount = _field_int(tx.payload, "amount", minimum=1)
    _debit_trade_asset(state, tx.sender, asset, amount)
    state.htlcs[lock_id] = HtlcRecord(
        htlc_id=lock_id,
        sender=tx.sender,
        beneficiary=_field_address(tx.payload, "beneficiary", state.params),
        hashlock=_field_digest(tx.payload, "hashlock"),
        timelock_height=_field_int(tx.payload, "timelock_height"),
        amount=amount,
        created_at_height=height,
        asset=asset,
    )


def _apply_htlc_claim(state: State, tx: Transaction, height: int) -> None:
    record = state.htlcs[_field_digest(tx.payload, "htlc_id")]
    _credit_trade_asset(state, tx.sender, record.asset, record.amount)
    state.ensure(tx.sender).balance -= tx.fee
    record.status = HtlcStatus.CLAIMED
    record.closed_at_height = height
    record.preimage = _field_preimage(tx.payload, "preimage")


def _apply_htlc_refund(state: State, tx: Transaction, height: int) -> None:
    record = state.htlcs[_field_digest(tx.payload, "htlc_id")]
    _credit_trade_asset(state, tx.sender, record.asset, record.amount)
    state.ensure(tx.sender).balance -= tx.fee
    record.status = HtlcStatus.REFUNDED
    record.closed_at_height = height


APPLIERS = {
    TxType.TRANSFER: _apply_transfer,
    TxType.REGISTER: _apply_register,
    TxType.REGISTER_WITNESS: _apply_register_witness,
    TxType.UPDATE_REGISTRATION: _apply_update_registration,
    TxType.UNREGISTER: _apply_unregister,
    TxType.WITHDRAW_BOND: _apply_withdraw_bond,
    TxType.AVAILABILITY: _apply_availability,
    TxType.REVEAL: _apply_reveal,
    TxType.TIMEOUT_CLAIM: _apply_timeout_claim,
    TxType.TIMEOUT_DISPUTE: _apply_timeout_dispute,
    TxType.SLASH_EVIDENCE: _apply_slash_evidence,
    TxType.CREATE_POOL: _apply_create_pool,
    TxType.ADD_LIQUIDITY: _apply_add_liquidity,
    TxType.REMOVE_LIQUIDITY: _apply_remove_liquidity,
    TxType.SWAP: _apply_swap,
    TxType.HTLC_LOCK: _apply_htlc_lock,
    TxType.HTLC_CLAIM: _apply_htlc_claim,
    TxType.HTLC_REFUND: _apply_htlc_refund,
    TxType.REGISTER_TOKEN: _apply_register_token,
}
"""State transitions for every accepted transaction type."""


# -- public API ---------------------------------------------------------------


def validate_transaction(
    tx: Transaction,
    params: NetworkParams,
    state: State,
    *,
    height: int,
    for_mempool: bool = False,
    verify_signature: bool = True,
    size: int | None = None,
) -> None:
    """Run the full transaction checklist for ``tx`` at ``height``.

    ``for_mempool`` additionally rejects a transaction that is still valid on
    chain but already superseded in the pool; the pool itself enforces its own
    ``(sender, nonce)`` uniqueness, so the flag only documents intent.

    ``verify_signature=False`` is for re-checking an entry the pool already
    admitted (the transaction is frozen and its txid is unchanged). ``size``
    likewise lets the pool reuse the canonical size it measured at admission
    instead of serializing a multi-kilobyte transaction again.
    """
    if not isinstance(tx, Transaction):
        _fail(TxFailure.SCHEMA, f"expected a Transaction, got {type(tx).__name__}")
    if tx.network != params.network_id:
        _fail(
            TxFailure.NETWORK,
            f"transaction is for {tx.network!r}, this node is on {params.network_id!r}",
        )
    if not validate_address(tx.sender, params):
        _fail(TxFailure.SCHEMA, f"{tx.sender!r} is not a {params.network_id} address")
    if not isinstance(tx.payload, dict):
        _fail(TxFailure.SCHEMA, "payload must be an object")
    # The signature covers the canonical body, so a payload the canonical encoder
    # cannot represent can never have been signed. Reject it as a schema failure
    # here rather than let check_sender_key's txid() (and tx_size() below) raise a
    # bare SerializationError that bypasses the TxFailure vocabulary.
    try:
        normalize(tx.payload)
    except SerializationError as exc:
        _fail(TxFailure.SCHEMA, f"payload is not canonical JSON: {exc}")

    # Signature before nonce: a forger must not learn whether a nonce is free.
    key_hex = check_sender_key(state, params, tx, verify_signature=verify_signature)

    account = state.account(tx.sender)
    if tx.nonce < account.nonce:
        _fail(
            TxFailure.NONCE,
            f"nonce {tx.nonce} was already used; {tx.sender} is at nonce {account.nonce}",
        )
    if tx.nonce > account.nonce:
        _fail(
            TxFailure.NONCE,
            f"nonce {tx.nonce} is ahead of {account.nonce}; transactions must apply in order",
        )

    if tx.fee < params.min_fee:
        _fail(TxFailure.FEE, f"fee {tx.fee} is below the {params.min_fee}-unit minimum")

    handler = VALIDATORS.get(tx.type)
    if handler is not None:
        handler(params, state, tx, height)
    elif tx.type is TxType.TIMEOUT_CLAIM:
        from sccp.chain import timeout

        timeout.validate_claim(state, tx, height)
    elif tx.type is TxType.TIMEOUT_DISPUTE:
        from sccp.chain import timeout

        timeout.validate_dispute(state, tx, height)
    elif tx.type is TxType.SLASH_EVIDENCE:
        from sccp.chain import timeout

        timeout.validate_slash(state, tx, height)
    else:  # pragma: no cover - the enum is exhaustive
        _fail(TxFailure.TYPE, f"unsupported transaction type {tx.type!r}")

    actual_size = tx_size(tx) if size is None else size
    if actual_size > params.max_tx_bytes:
        _fail(TxFailure.SIZE, f"transaction is {actual_size} bytes, cap is {params.max_tx_bytes}")

    if account.public_key is None and tx.payload.get("public_key") != key_hex:
        _fail(TxFailure.SIGNATURE, "first use must reveal the sender's public key")


def apply_transaction(state: State, tx: Transaction, *, height: int) -> None:
    """Apply a validated transaction to ``state``.

    Callers must have run :func:`validate_transaction` against the same state and
    height; this function assumes every check passed and only mutates.
    """
    account = state.ensure(tx.sender)
    closes_htlc = tx.type in (TxType.HTLC_CLAIM, TxType.HTLC_REFUND)
    if not closes_htlc:
        # An HTLC close may pay its fee from the native amount it releases; the
        # applier debits it after crediting the escrow, so a taker who started
        # with no ZQWX can still claim. Every other type pays the fee up front.
        account.balance -= tx.fee
    account.nonce += 1
    if account.public_key is None:
        account.public_key = resolve_public_key(state, tx)
    applier = APPLIERS.get(tx.type)
    if applier is None:
        _fail(TxFailure.TYPE, f"no state transition for {tx.type!r}")
    applier(state, tx, height)


def transaction_fee_total(transactions: tuple[Transaction, ...] | list[Transaction]) -> int:
    """Sum of the fees a block collects."""
    return sum(tx.fee for tx in transactions)


__all__ = [
    "ALLOWED",
    "APPLIERS",
    "MAX_SAFE_TEXT",
    "VALIDATORS",
    "TxFailure",
    "apply_transaction",
    "check_sender_key",
    "coinbase_id",
    "htlc_id",
    "make_coinbase",
    "resolve_public_key",
    "sign_transaction",
    "transaction_fee_total",
    "tx_body",
    "tx_size",
    "txid",
    "validate_transaction",
]
