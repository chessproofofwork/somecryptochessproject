"""Immutable network parameters (docs/design/PROTOCOL.md section 15).

Amounts are integer base units: ``1 coin = 100_000_000 units``. No floating
point value is ever accepted by this module. ``NetworkParams`` validates its own
invariants at construction, so an impossible network definition fails at import
time rather than at block validation time.
"""

from __future__ import annotations

import re
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sccp.chess.rulesets import (
    CHESS960_VARIANT as CHESS960_VARIANT,
)
from sccp.chess.rulesets import (
    STANDARD_VARIANT as STANDARD_VARIANT,
)
from sccp.chess.rulesets import (
    canonical_opening_line,
    opening_line_fits,
    ruleset_for,
    supported_rules,
)
from sccp.errors import ConfigError, NetworkError
from sccp.serialization import Domain, domain_hex

UNITS_PER_COIN: Final = 100_000_000
"""Base units per coin. All consensus amounts are integers of this unit."""

MAX_CLOCK_MS: Final = 86_400_000
"""Hard ceiling for a single clock value: one day, so ``mm`` stays two digits."""

DEFAULT_TIME_CONTROL_INITIAL_MS: Final = 600_000
"""Default main time for a network that does not override it: ten minutes."""

DEFAULT_TIME_CONTROL_INCREMENT_MS: Final = 0
"""Default per-move increment for a network that does not override it: sudden death."""

DEFAULT_TIME_CONTROL_DELAY_MS: Final = 0
"""Default per-move delay for a network that does not override it: no delay."""

MAX_PLIES_LIMIT: Final = 1000
"""Hard upper bound for ``max_plies``; longer games are never consensus-valid."""

CLOCK_EVIDENCE_PLIES: Final = 64
"""Most move receipts a clock-evidence bundle may carry (PROTOCOL.md section 8.4).

A flag claim proves the tail of the game, not the whole game: the window starts at
a witness-signed receipt whose absolute clock values anchor the arithmetic, and
every later receipt is checked as a delta from it. Bounding the window is what
keeps the largest transaction the protocol can produce well inside
``max_tx_bytes``; before it existed a 400-ply broadcast flag bundle overflowed the
transaction cap with classical signatures alone.
"""


def units_per_coin(decimals: int = 8) -> int:
    """Base units in one whole coin at ``decimals`` decimal places.

    Native ZQWX and the built-in variants use eight. A registered token fixes
    its own ``decimals`` at registration, so every cap and conversion for that
    asset has to use its value rather than the native constant.
    """
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 18:
        raise ConfigError("decimals must be an integer between 0 and 18")
    return int(10**decimals)


def coins_to_units(coins: int, *, decimals: int = 8) -> int:
    """Convert whole coins to base units."""
    if not isinstance(coins, int) or isinstance(coins, bool):
        raise ConfigError("coin amounts must be integers")
    return coins * units_per_coin(decimals)


def format_coins(units: int) -> str:
    """Format base units as fixed-point coins for a human; never a float.

    A bare ``1200001000`` in an error message reads like an amount of coins, so
    every user-facing message prints through this instead: ``12.00001000``.
    """
    whole, fraction = divmod(abs(units), UNITS_PER_COIN)
    sign = "-" if units < 0 else ""
    return f"{sign}{whole}.{fraction:08d}"


def derive_variant_asset_id(name: str, symbol: str, decimals: int, ruleset: str) -> str:
    """Derive a built-in or legacy variant's asset id from its definition.

    This is the pre-ticket/name-based path: the id commits to the variant name,
    symbol, decimal precision and ruleset. A chain-registered token uses
    :func:`derive_token_asset_id` instead, where the user-chosen name and ticker
    are display-only and a unique ticket supplies the identity.
    """
    return domain_hex(
        Domain.ASSET,
        {
            "variant": name,
            "symbol": symbol,
            "decimals": decimals,
            "ruleset": ruleset,
        },
    )


TOKEN_TICKET_PATTERN: Final = r"^(?:[0-9]{4}|[0-9a-f]{16})$"
"""A unique registration serial: 16 lowercase hex characters or four digits.

The 16-hex form is the normal namespace: eight random bytes give 2^64 possible
tickets, so registrations are not limited to a small human-numbered range. The
four-digit form remains valid for the shipped C960 token and pre-existing
registrations. The ticket makes two registrations distinct even when they
choose the same name and ticker; it is a salt, not a ticker. The printable
ticker is the token's ``symbol`` and may be reused by another registration.
"""


def token_variant_name(ticket: str) -> str:
    """The internal variant name for a registration serial.

    The name is deliberately derived from the ticket rather than from the
    user-chosen display name. Consensus state, miner registration and proof
    hashes need one unique, stable key per token, while two tokens may show the
    same name to users.
    """
    if not isinstance(ticket, str) or re.fullmatch(TOKEN_TICKET_PATTERN, ticket) is None:
        raise ConfigError(
            f"token ticket must be four digits or 16 lowercase hex characters, got {ticket!r}"
        )
    return f"token-{ticket}"


def derive_token_asset_id(ticket: str, decimals: int, ruleset: str) -> str:
    """Derive a registered token's id from its registration serial and rules.

    Names and tickers are display metadata, not identity: they may repeat. The
    unique ticket plus the consensus-relevant decimals and ruleset determine the
    asset id, so two tokens called "Gold" still have different ids.
    """
    token_variant_name(ticket)
    return domain_hex(
        Domain.ASSET,
        {
            "kind": "registered-token",
            "ticket": ticket,
            "decimals": decimals,
            "ruleset": ruleset,
        },
    )


C960_TICKET: Final = "0960"
"""The conventional ticket of the shipped Chess960 token.

The protocol does not reserve this serial or privilege the token in any way:
C960 is an ordinary chain-registered token whose genesis record happens to use
this ticket. The value is exported so the shipped genesis documents, deployment
scripts and tests derive the same asset id without duplicating the recipe.
"""

C960_VARIANT: Final = token_variant_name(C960_TICKET)
"""The internal variant name of the shipped Chess960 token (``token-0960``)."""

C960_ASSET_ID: Final = derive_token_asset_id(C960_TICKET, 8, "chess960")
"""The asset id of the shipped Chess960 token.

Unlike the former built-in constant this commits to a registration ticket, so
C960 has exactly the same identity model as every other custom token.
"""


class TimeControlPeriod(BaseModel):
    """One later stage of a multi-period (sudden-death) time control.

    FIDE-style controls add time when a move-number threshold is reached, for
    example 40 moves at 90 minutes followed by 30 minutes for the rest of the
    game. The token's base fields describe the first period; each entry here
    describes a *later* period and the number of full moves it lasts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    moves: int = Field(ge=1, le=MAX_PLIES_LIMIT // 2)
    """Length of this period in full moves (both seats' moves)."""

    initial_ms: int = Field(ge=0, le=MAX_CLOCK_MS)
    """Time added to *both* clocks when this period starts."""

    increment_ms: int = Field(default=0, ge=0, le=MAX_CLOCK_MS)
    delay_ms: int = Field(default=0, ge=0, le=MAX_CLOCK_MS)


class VariantParams(BaseModel):
    """One chess variant and the asset its settled proofs mint.

    Standard chess is the network's native asset: ``asset`` is ``None`` and the
    reward is ZQWX. Every other variant names a derived asset id and mints only
    that asset. A chain-registered token, C960 included, is stored as a
    ``TokenRecord`` in ledger state rather than in :class:`NetworkParams`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    ticket: str | None = Field(default=None, pattern=TOKEN_TICKET_PATTERN)
    """The registration serial that makes a token unique.

    New tickets are 16 lowercase hex characters; four-digit short tickets remain
    valid for C960 and pre-existing registrations. Built-in variants have no
    ticket. The ticket derives the internal variant name ``token-<ticket>`` and
    salts the token id; it is not the printable ticker. ``symbol`` is the ticker
    and ``display_name`` is the name, and both are display metadata that another
    registration may reuse. The asset id (the network-assigned token id) is the
    identity that balances and pools use.
    """
    rules: str = Field(min_length=1, max_length=32)
    ruleset: str | None = Field(default=None, min_length=1, max_length=32)
    """Registered ruleset that decides how games of this variant are played.

    ``None`` means the variant is its own ruleset, which is how the built-in
    ``standard`` variant is defined. A chain-registered token names an existing
    ruleset (for example ``chess960``) so validation never has to execute code
    supplied by a transaction.
    """
    asset: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=1, max_length=64)
    decimals: int = Field(default=8, ge=0, le=18)
    subsidy: int = Field(gt=0)
    halving_interval: int | None = Field(default=None, ge=1)
    max_supply: int | None = Field(default=None, ge=1)
    min_plies: int = Field(ge=1)
    max_games_per_block: int = Field(default=1, ge=1)
    opening_book: str = Field(default="v1", min_length=1, max_length=64)
    """Opening-book version the relay plays this variant's games from.

    Omission means the packaged ``v1`` book. A chain registration records the
    network's ``book_version``, or ``token-<ticket>`` when that ticketed token
    declares its own opening lines.
    """
    time_control_initial_ms: int = Field(
        default=DEFAULT_TIME_CONTROL_INITIAL_MS, ge=1, le=MAX_CLOCK_MS
    )
    """Main time each seat starts with, in milliseconds."""
    time_control_increment_ms: int = Field(
        default=DEFAULT_TIME_CONTROL_INCREMENT_MS, ge=0, le=MAX_CLOCK_MS
    )
    """Time added after every move, in milliseconds."""
    time_control_delay_ms: int = Field(default=DEFAULT_TIME_CONTROL_DELAY_MS, ge=0, le=MAX_CLOCK_MS)
    """Per-move delay, in milliseconds: time that does not count down on each turn.

    A delay is mutually exclusive with an increment. ``0`` means no delay, so
    ``increment_ms == delay_ms == 0`` is sudden death.
    """
    time_control_first_moves: int | None = Field(default=None, ge=1, le=MAX_PLIES_LIMIT // 2)
    """Full moves in the first period when a later period follows.

    ``None`` means the base control lasts the whole game. When set, each entry
    in ``time_control_periods`` starts at a fixed move boundary, and time is
    added to both clocks at that boundary.
    """
    time_control_periods: tuple[TimeControlPeriod, ...] = ()
    """Later time-control periods, in order. Empty for a single-period control."""
    openings: tuple[tuple[str, ...], ...] = Field(default=(), max_length=256)
    """Token-specific opening lines in canonical SAN.

    The relay selects one line from the epoch beacon and the match id, exactly
    as it selects a line from the packaged book. An empty tuple means the token
    uses the network book named by ``opening_book``. Every line is replayed and
    canonicalised against this variant's ruleset at registration, so a token
    cannot smuggle an illegal line into consensus state.
    """

    @property
    def native(self) -> bool:
        """True when proofs of this variant mint the network's native coin."""
        return self.asset is None

    @property
    def time_control(self) -> tuple[int, int, int]:
        """``(initial_ms, increment_ms, delay_ms)`` for this variant's games."""
        return (
            self.time_control_initial_ms,
            self.time_control_increment_ms,
            self.time_control_delay_ms,
        )

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        ruleset = ruleset_for(self.ruleset or self.name)
        if self.rules != ruleset.pgn_rules:
            raise ConfigError(
                f"variant {self.name!r} is registered with PGN rules "
                f"{ruleset.pgn_rules!r}, not {self.rules!r}; known rules: "
                f"{', '.join(sorted(supported_rules()))}"
            )
        if self.ticket is not None:
            if self.name != token_variant_name(self.ticket):
                raise ConfigError(
                    f"variant {self.name!r} does not match ticket {self.ticket!r}; "
                    f"expected {token_variant_name(self.ticket)!r}"
                )
            if self.asset is None:
                raise ConfigError("a ticketed token needs a network-assigned asset id")
        if self.name == STANDARD_VARIANT and self.asset is not None:
            raise ConfigError("the standard variant is native ZQWX with no derived asset")
        if self.name == STANDARD_VARIANT and self.ruleset not in (None, STANDARD_VARIANT):
            raise ConfigError("the standard variant uses its own ruleset")
        if self.name != STANDARD_VARIANT and self.asset is None:
            raise ConfigError(f"variant {self.name!r} needs a derived asset id")
        if (self.max_supply is None) != (self.halving_interval is None):
            raise ConfigError("a variant sets max_supply and halving_interval together")
        if (
            self.time_control_initial_ms % 1000
            or self.time_control_increment_ms % 1000
            or self.time_control_delay_ms % 1000
        ):
            raise ConfigError(
                "variant time controls must be whole seconds, because the PGN "
                "TimeControl tag is lossless only in whole seconds"
            )
        if self.time_control_increment_ms and self.time_control_delay_ms:
            raise ConfigError(f"variant {self.name!r} uses an increment or a delay, not both")
        if self.time_control_periods and self.time_control_first_moves is None:
            raise ConfigError("a multi-period time control needs time_control_first_moves")
        if self.time_control_first_moves is not None and not self.time_control_periods:
            raise ConfigError("time_control_first_moves is only valid with a later period")
        for index, line in enumerate(self.openings):
            if not 1 <= len(line) <= 12:
                raise ConfigError(
                    f"variant {self.name!r} opening {index} has {len(line)} plies; "
                    "a certified opening carries between 1 and 12"
                )
            try:
                canonical = canonical_opening_line(ruleset.name, line)
            except ConfigError as exc:
                raise ConfigError(
                    f"variant {self.name!r} opening {index} is invalid: {exc}"
                ) from None
            if canonical != tuple(line):
                raise ConfigError(
                    f"variant {self.name!r} opening {index} is not canonical SAN; "
                    f"expected {' '.join(canonical)}"
                )
            if not opening_line_fits(ruleset, line):
                raise ConfigError(
                    f"variant {self.name!r} opening {index} is not playable under "
                    f"ruleset {ruleset.name!r}"
                )
        period_increments = [self.time_control_increment_ms]
        total_moves = self.time_control_first_moves or (MAX_PLIES_LIMIT // 2)
        added = 0
        for period in self.time_control_periods:
            if period.initial_ms % 1000 or period.increment_ms % 1000 or period.delay_ms % 1000:
                raise ConfigError(
                    f"variant {self.name!r} time-control periods must be whole seconds"
                )
            if period.increment_ms and period.delay_ms:
                raise ConfigError(f"variant {self.name!r} uses an increment or a delay, not both")
            total_moves += period.moves
            added += period.initial_ms
            period_increments.append(period.increment_ms)
        if total_moves > MAX_PLIES_LIMIT // 2:
            raise ConfigError(
                f"variant {self.name!r} periods cover {total_moves} moves, "
                f"above the {MAX_PLIES_LIMIT // 2}-move protocol ceiling"
            )
        # A clock never exceeds one day even if the game runs to the protocol's
        # ply ceiling, so ``mm:ss`` formatting cannot overflow. A delay never
        # grows a clock, so only the increments contribute to the upper bound.
        worst_case = self.time_control_initial_ms + added + max(period_increments) * MAX_PLIES_LIMIT
        if worst_case > MAX_CLOCK_MS:
            raise ConfigError(
                f"variant {self.name!r} time control can reach {worst_case} ms, "
                f"above the {MAX_CLOCK_MS} ms clock ceiling"
            )
        return self


def c960_token(*, min_plies: int = 20) -> VariantParams:
    """The shipped Chess960 token definition, as an ordinary custom token.

    Unlike the former built-in variant this has a ticket and its internal name is
    ``token-0960``. The shipped genesis documents register it in
    ``State.tokens``; no consensus path treats it as part of ``NetworkParams``.
    The devnet genesis lowers ``min_plies`` to the one-ply schedule the test
    fixture applies to the rest of that chain.
    """
    return VariantParams(
        name=C960_VARIANT,
        ticket=C960_TICKET,
        rules="chess960",
        ruleset="chess960",
        asset=C960_ASSET_ID,
        symbol="C960",
        display_name="Chess960",
        subsidy=coins_to_units(50),
        halving_interval=210_000,
        max_supply=21_000_000,
        min_plies=min_plies,
        max_games_per_block=1,
        opening_book="none",
    )


C960: Final = c960_token()
"""The shipped Chess960 token definition, for genesis and deployment helpers."""

CHESS960: Final = C960
"""Deprecated alias for :data:`C960`.

Kept so code written against the former built-in variant keeps working while it
migrates to the token name. It is no longer part of any shipped network's
``variants`` tuple.
"""

CHESS960_ASSET_ID: Final = C960_ASSET_ID
"""Deprecated alias for :data:`C960_ASSET_ID`."""


class NetworkParams(BaseModel):
    """One network's consensus constants. Frozen: instances are hashable and safe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Identity
    network_id: str = Field(min_length=1)
    address_hrp: str = Field(min_length=1)
    genesis_timestamp: int | None = None

    # Epochs and matchmaking
    epoch_length: int = Field(ge=1)
    rounds_per_epoch: int = Field(ge=1)
    availability_lead: int = Field(ge=2)
    wins_required: int = Field(ge=1)
    k_min: int = Field(ge=1)
    k_max: int = Field(ge=1)
    claim_window: int = Field(ge=1)
    opponent_cooldown: int = Field(ge=0)

    # Header seal and retargeting
    seal_bits: int = Field(ge=0, le=32)
    target_block_seconds: int = Field(ge=1)
    retarget_interval: int = Field(ge=1)
    retarget_enabled: bool = True

    # Economics
    subsidy: int = Field(gt=0)
    halving_interval: int | None = Field(default=None, ge=1)
    max_supply: int | None = Field(default=None, ge=1)
    coinbase_maturity: int = Field(ge=0)
    min_bond: int = Field(ge=0)
    registration_epochs: int = Field(ge=1)
    unbond_delay: int = Field(ge=0)
    min_fee: int = Field(gt=0)
    dex_fee_bps: int = Field(default=30, ge=0, le=1_000)
    """Constant-product swap fee, in basis points, paid to liquidity providers."""

    # Clocks and timeout settlement
    lag_grace_ms: int = Field(default=250, ge=0, le=1_000)
    timeout_dispute_window: int = Field(default=2, ge=1)
    min_timeout_bond: int = Field(default=0, ge=0)
    clock_witness_quorum: int = Field(default=1, ge=1)
    clock_witness_bond: int = Field(default=0, ge=0)

    # Chess rules
    min_plies: int = Field(ge=1)
    max_plies: int = Field(ge=1, le=MAX_PLIES_LIMIT)
    nodes_per_move: int = Field(gt=0)
    book_version: str = Field(min_length=1)

    # Variants and their assets. The validator always prepends standard chess,
    # so an empty tuple means "native standard chess only". Shipping networks use
    # that empty tuple: every non-native asset, including C960, is a
    # chain-registered token in State.tokens. The field remains for legacy
    # networks and for tests of the built-in path.
    variants: tuple[VariantParams, ...] = ()

    # Beacon
    beacon_grace_blocks: int = Field(ge=0)
    drand_chain: str | None = None
    drand_period_seconds: int | None = Field(default=None, ge=1)
    drand_round_offset: int | None = None

    # Size caps
    max_block_bytes: int = Field(gt=0)
    max_pgn_bytes: int = Field(gt=0)
    max_tx_bytes: int = Field(gt=0)

    # Ledger: pinned genesis and storage policy
    genesis_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mempool_ttl_blocks: int = Field(default=64, ge=1)
    snapshot_interval: int = Field(default=100, ge=1)
    reorg_depth: int = Field(default=10, ge=1)

    @property
    def max_money(self) -> int | None:
        """Supply cap in base units, or ``None`` when the network is uncapped."""
        if self.max_supply is None:
            return None
        return self.max_supply * UNITS_PER_COIN

    def variant(self, name: str) -> VariantParams:
        """Resolve a variant by name, or raise :class:`ConfigError`."""
        for candidate in self.variants:
            if candidate.name == name:
                return candidate
        known = ", ".join(candidate.name for candidate in self.variants)
        raise ConfigError(f"unknown variant {name!r}; this network has: {known}")

    def variant_names(self) -> tuple[str, ...]:
        """Every variant this network settles, standard first."""
        return tuple(candidate.name for candidate in self.variants)

    def variant_for_asset(self, asset: str) -> VariantParams:
        """Resolve the variant that mints ``asset`` (native ZQWX is standard)."""
        if asset == "ZQWX":
            return self.variant(STANDARD_VARIANT)
        for candidate in self.variants:
            if candidate.asset == asset:
                return candidate
        raise ConfigError(f"unknown asset id {asset!r}")

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.retarget_interval % self.epoch_length != 0:
            raise ConfigError("retarget_interval must be a multiple of epoch_length")
        if not self.k_min <= self.wins_required <= self.k_max:
            raise ConfigError("wins_required must satisfy k_min <= k <= k_max")
        if self.min_plies > self.max_plies:
            raise ConfigError("min_plies must not exceed max_plies")
        if self.max_block_bytes <= max(self.max_pgn_bytes, self.max_tx_bytes):
            raise ConfigError("max_block_bytes must exceed the PGN and tx caps")
        if (self.max_supply is None) != (self.halving_interval is None):
            raise ConfigError("max_supply and halving_interval are set together or not at all")
        drand_fields = (self.drand_period_seconds, self.drand_round_offset)
        if self.drand_chain is None and any(field is not None for field in drand_fields):
            raise ConfigError("drand timing constants require drand_chain")
        if self.drand_chain is not None and any(field is None for field in drand_fields):
            raise ConfigError("drand_chain requires drand_period_seconds and drand_round_offset")

        max_money = self.max_money
        if max_money is not None:
            for name, amount in (
                ("subsidy", self.subsidy),
                ("min_bond", self.min_bond),
                ("min_fee", self.min_fee),
                ("min_timeout_bond", self.min_timeout_bond),
                ("clock_witness_bond", self.clock_witness_bond),
            ):
                if amount > max_money:
                    raise ConfigError(f"{name} exceeds MAX_MONEY ({max_money})")

        # Standard chess is the network's own unit of account. It is always
        # first and always mirrors the network's own schedule, even when a
        # caller only lists the optional variants or overrides min_plies. A
        # supplied standard entry contributes only its clock, which is the one
        # standard-token field the network schedule does not already own.
        supplied = tuple(self.variants)
        supplied_standard = next(
            (variant for variant in supplied if variant.name == STANDARD_VARIANT), None
        )
        standard = VariantParams(
            name=STANDARD_VARIANT,
            rules="chess",
            asset=None,
            symbol="ZQWX",
            display_name="ZQWX",
            subsidy=self.subsidy,
            halving_interval=self.halving_interval,
            max_supply=self.max_supply,
            min_plies=self.min_plies,
            max_games_per_block=1,
            opening_book=self.book_version,
            time_control_initial_ms=(
                supplied_standard.time_control_initial_ms
                if supplied_standard is not None
                else DEFAULT_TIME_CONTROL_INITIAL_MS
            ),
            time_control_increment_ms=(
                supplied_standard.time_control_increment_ms
                if supplied_standard is not None
                else DEFAULT_TIME_CONTROL_INCREMENT_MS
            ),
            time_control_delay_ms=(
                supplied_standard.time_control_delay_ms
                if supplied_standard is not None
                else DEFAULT_TIME_CONTROL_DELAY_MS
            ),
        )
        variants = (
            standard,
            *(item for item in supplied if item.name != STANDARD_VARIANT),
        )
        names = [variant.name for variant in variants]
        if len(set(names)) != len(names):
            raise ConfigError("variant names must be unique")
        assets = [variant.asset for variant in variants if variant.asset is not None]
        if len(set(assets)) != len(assets):
            raise ConfigError("two variants cannot mint the same asset")
        object.__setattr__(self, "variants", variants)
        return self


TESTNET: Final = NetworkParams(
    network_id="testnet",
    address_hrp="sccpt",
    epoch_length=5,
    rounds_per_epoch=3,
    availability_lead=2,
    wins_required=1,
    k_min=1,
    k_max=5,
    claim_window=128,
    # Testnet blocks are 30 s. Every block- or epoch-denominated window that
    # protects liveness is doubled from the old 60 s schedule, so a proof, a
    # pending transaction, a maturity, a dispute and a snapshot keep the same
    # wall-clock lifetime they had before; only inclusion gets faster.
    mempool_ttl_blocks=128,
    snapshot_interval=200,
    # Test networks seat the same small roster over and over; a zero cooldown
    # disables the rematch rule (PROTOCOL.md section 12.2) so every decisive game
    # can settle. Only mainnet enforces a cooldown.
    opponent_cooldown=0,
    seal_bits=0,
    target_block_seconds=30,
    retarget_interval=10,
    subsidy=coins_to_units(50),
    coinbase_maturity=40,
    min_bond=coins_to_units(10),
    registration_epochs=20,
    unbond_delay=100,
    min_fee=1_000,
    lag_grace_ms=250,
    timeout_dispute_window=4,
    min_timeout_bond=coins_to_units(1),
    clock_witness_quorum=1,
    clock_witness_bond=coins_to_units(1),
    min_plies=20,
    max_plies=300,
    nodes_per_move=50_000,
    book_version="dev-1",
    beacon_grace_blocks=4,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=131_072,
    genesis_hash="68f4671eb55aa1571e0e6861d946d7d6d523a06e2772851d34f3ca6540f60543",
)

MAINNET: Final = NetworkParams(
    network_id="mainnet",
    address_hrp="sccp",
    epoch_length=20,
    rounds_per_epoch=5,
    availability_lead=2,
    wins_required=3,
    k_min=1,
    k_max=10,
    claim_window=144,
    opponent_cooldown=4,
    seal_bits=16,
    target_block_seconds=600,
    retarget_interval=40,
    subsidy=coins_to_units(50),
    halving_interval=210_000,
    max_supply=21_000_000,
    coinbase_maturity=100,
    min_bond=coins_to_units(500),
    registration_epochs=30,
    unbond_delay=1_000,
    min_fee=1_000,
    lag_grace_ms=250,
    timeout_dispute_window=6,
    min_timeout_bond=coins_to_units(10),
    clock_witness_quorum=3,
    clock_witness_bond=coins_to_units(50),
    min_plies=30,
    max_plies=400,
    nodes_per_move=250_000,
    book_version="v1",
    beacon_grace_blocks=6,
    drand_chain="quicknet",
    drand_period_seconds=3,
    drand_round_offset=1,
    max_block_bytes=1_048_576,
    max_pgn_bytes=262_144,
    max_tx_bytes=131_072,
    genesis_hash="e7949a3123a042ffcd683bfa6cd8beab0507b51f99b882a4dc7fe0646bf5d628",
)

DEVNET: Final = NetworkParams.model_validate(
    {
        **TESTNET.model_dump(),
        "network_id": "devnet",
        # Fully deterministic: one-block epochs, no retargeting, fixed genesis time.
        "epoch_length": 1,
        "target_block_seconds": 1,
        "retarget_enabled": False,
        "wins_required": 1,
        "k_min": 1,
        "min_bond": 0,
        # Devnet is its own deterministic network; pin the windows so changing
        # testnet's block cadence cannot rewrite a devnet fixture's history.
        "claim_window": 64,
        "mempool_ttl_blocks": 64,
        "snapshot_interval": 100,
        "coinbase_maturity": 20,
        "registration_epochs": 10,
        "unbond_delay": 50,
        "timeout_dispute_window": 2,
        "beacon_grace_blocks": 2,
        "genesis_timestamp": 1_700_000_000,
        "genesis_hash": "e10f1b50e67163e25b9ab1afcae03c90731117f693c4a71c9c7aaef59758a496",
    }
)
"""In-process network for tests and CI: mock engines, zero seal difficulty."""

NETWORKS: Final[dict[str, NetworkParams]] = {
    params.network_id: params for params in (TESTNET, MAINNET, DEVNET)
}


def get_network(network: str | NetworkParams) -> NetworkParams:
    """Resolve a network id (or pass a :class:`NetworkParams` through)."""
    if isinstance(network, NetworkParams):
        return network
    try:
        return NETWORKS[network]
    except KeyError:
        known = ", ".join(sorted(NETWORKS))
        raise NetworkError(f"unknown network {network!r}; known networks: {known}") from None


def network_ids() -> tuple[str, ...]:
    """All known network ids, sorted."""
    return tuple(sorted(NETWORKS))
