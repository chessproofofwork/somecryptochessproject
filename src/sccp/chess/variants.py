"""Chess variant helpers built on the ruleset registry.

Standard chess always starts from :data:`chess.STARTING_FEN`. Chess960 chooses
one of 960 legal back-rank layouts and keeps the castling rights that layout
implies; its start position must travel with the game because a PGN without it
is ambiguous. Other tokens can register their own rules in
:mod:`sccp.chess.rulesets`; this module is the small, stable facade the rest of
the code uses.

Nothing here is engine-specific. The relay uses
:func:`select_chess960_start` to pick a position from the beacon, the miner uses
:func:`board_for_variant` to rebuild the same board, and PGN validation uses
:func:`normalise_start_fen` to reject an invalid start position.
"""

from __future__ import annotations

from collections.abc import Mapping

import chess

from sccp.chess.rulesets import (
    CHESS960_POSITIONS,
    chess960_position,
    chess960_start_fen,
    normalise_standard_start_fen,
    normalise_token_start_fen,
    ruleset_for,
    start_position_for,
)
from sccp.chess.rulesets import CHESS960_VARIANT as CHESS960_VARIANT
from sccp.config import NetworkParams
from sccp.errors import ConfigError

__all__ = [
    "CHESS960_POSITIONS",
    "CHESS960_VARIANT",
    "board_for_variant",
    "chess960_start_fen",
    "is_chess960",
    "normalise_start_fen",
    "normalise_token_start_fen",
    "rules_for_network",
    "select_chess960_start",
    "start_position_for",
    "variant_rules",
]


def variant_rules(name: str) -> str:
    """Return the PGN ``Rules`` tag for ``name``."""
    return ruleset_for(name).pgn_rules


def is_chess960(name: str) -> bool:
    """True when ``name`` uses Fischer Random rules."""
    return ruleset_for(name).chess960


def normalise_start_fen(fen: str | None, variant: str) -> str:
    """Return the variant's canonical start FEN, or raise :class:`ConfigError`.

    Standard chess accepts only the ordinary initial position. Chess960 accepts
    exactly the 960 legal layouts, expressed in Shredder-FEN so the rook files
    behind each castling right are explicit. A registered custom ruleset supplies
    its own validator.
    """
    rules = ruleset_for(variant)
    try:
        return rules.normalise_start_fen(fen)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"invalid start FEN {fen!r} for variant {variant!r}: {exc}") from None


def board_for_variant(
    variant: str,
    start_fen: str | None = None,
    *,
    token_start_fen: str | None = None,
) -> chess.Board:
    """Build the initial board for ``variant`` (Chess960 requires ``start_fen``).

    ``token_start_fen`` names a chain-registered token's pinned position. When it
    is set, ``start_fen`` must agree with it; this keeps a certificate from
    smuggling a different board past a token's immutable registration.
    """
    rules = ruleset_for(variant)
    if token_start_fen is not None:
        canonical = normalise_token_start_fen(rules, token_start_fen)
        if start_fen is not None:
            requested = normalise_token_start_fen(rules, start_fen)
            if requested != canonical:
                raise ConfigError(
                    f"start FEN {requested!r} does not match the token's pinned "
                    f"position {canonical!r}"
                )
        return rules.board(canonical)
    canonical = rules.normalise_start_fen(start_fen)
    return rules.board(canonical)


def select_chess960_start(beacon: bytes | str, match_id: str) -> int:
    """Derive a Chess960 start position from the epoch beacon and the match id.

    The relay is not trusted to invent a position: anyone holding the beacon and
    the match id can recompute this index from the certificate.
    """
    return chess960_position(beacon, match_id)


def rules_for_network(
    params: NetworkParams,
    variant: str,
    *,
    variants: Mapping[str, object] | None = None,
) -> str:
    """Return the rules for ``variant`` after checking it is on the network.

    ``variants`` extends ``params`` with chain-registered tokens, whose ruleset
    lives in ledger state rather than in the static network definition.
    """
    if variants is None or variant not in variants:
        params.variant(variant)
    return variant_rules(variant)


# ``normalise_standard_start_fen`` is re-exported for ruleset authors even though
# the facade does not call it directly.
__all__ += ["normalise_standard_start_fen"]
