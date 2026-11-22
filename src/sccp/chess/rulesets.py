"""The chess ruleset registry.

A network variant is more than a name: it decides how a PGN ``Rules`` tag is
spelled, how a start position is validated, which board class replays the game,
and whether a deterministic start position replaces the opening book. Keeping
those choices in one registry means a new token can add a ruleset without
touching the PGN verifier, the relay or the miner.

The two variants the current networks ship are registered at import time:

``standard``
    Ordinary chess, ``Rules: chess``, fixed initial position, opening book.
``chess960``
    Fischer Random, ``Rules: chess960``, a beacon-selected start position and no
    book moves.

A future token registers its own entry before it builds its
:class:`~sccp.config.NetworkParams`::

    from sccp.chess.rulesets import VariantRules, register_variant_rules

    register_variant_rules(
        VariantRules(
            name="crazyhouse",
            pgn_rules="crazyhouse",
            board=lambda fen: chess.variant.CrazyhouseBoard(fen),
        )
    )

This module deliberately does not import :mod:`sccp.config`: configuration
imports the registry to validate a variant, so the dependency must point one
way only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

import chess
import chess.variant

from sccp.errors import ConfigError

STANDARD_VARIANT: Final = "standard"
"""The native ZQWX variant. It is first in every network's variant list."""

CHESS960_VARIANT: Final = "chess960"
"""Fischer Random: the same pieces, a shuffled back rank and 960 start positions."""

CHESS960_POSITIONS: Final = 960
"""Number of legal Fischer Random start positions."""


def _normalise_fen(fen: str) -> str:
    """Strip move counters so two spellings of one start position compare equal."""
    fields = fen.strip().split()
    if len(fields) < 4:
        raise ConfigError(f"not a FEN: {fen!r}")
    return " ".join(fields[:4]) + " 0 1"


def normalise_standard_start_fen(fen: str | None) -> str:
    """Return the ordinary initial position, rejecting any other start."""
    if fen is not None and _normalise_fen(fen) != _normalise_fen(chess.STARTING_FEN):
        raise ConfigError("standard chess games must start from the initial position")
    return chess.STARTING_FEN


def chess960_start_fen(index: int) -> str:
    """Shredder-FEN of Chess960 position ``index`` (``0..959``)."""
    if not 0 <= index < CHESS960_POSITIONS:
        raise ConfigError(f"Chess960 position must be in [0, {CHESS960_POSITIONS - 1}]")
    return chess.Board.from_chess960_pos(index).shredder_fen()


_START_POSITIONS: Final[frozenset[str]] = frozenset(
    _normalise_fen(chess960_start_fen(index)) for index in range(CHESS960_POSITIONS)
)
"""Every legal Chess960 start position, keyed by its normalised Shredder-FEN."""


def normalise_chess960_start_fen(fen: str | None) -> str:
    """Return a canonical Chess960 Shredder-FEN, or raise :class:`ConfigError`."""
    if fen is None:
        raise ConfigError("a Chess960 game needs its start FEN")
    try:
        board = chess.Board(fen, chess960=True)
    except ValueError as exc:
        raise ConfigError(f"invalid Chess960 start FEN {fen!r}: {exc}") from None
    canonical = _normalise_fen(board.shredder_fen())
    if canonical not in _START_POSITIONS:
        raise ConfigError(f"{fen!r} is not one of the 960 Chess960 start positions")
    return canonical


def chess960_position(beacon: bytes | str, match_id: str) -> int:
    """Derive a Chess960 start index from the epoch beacon and the match id.

    The relay is not trusted to invent a position: anyone holding the beacon and
    the match id can recompute this index from the certificate.
    """
    if isinstance(beacon, str):
        try:
            beacon_bytes = bytes.fromhex(beacon)
        except ValueError:
            raise ConfigError("beacon hash is not valid hex") from None
    else:
        beacon_bytes = beacon
    if len(beacon_bytes) != 32:
        raise ConfigError(f"beacon must be 32 bytes, got {len(beacon_bytes)}")
    try:
        match_bytes = bytes.fromhex(match_id)
    except ValueError:
        raise ConfigError("match id is not valid hex") from None
    digest = hashlib.sha256(b"sccp/v1/chess960-start" + beacon_bytes + match_bytes).digest()
    return int.from_bytes(digest[:8], "big") % CHESS960_POSITIONS


def _chess960_board(start_fen: str) -> chess.Board:
    """Build a board whose castling rights follow Shredder-FEN."""
    return chess.Board(start_fen, chess960=True)


def _canonical_board_fen(board: chess.Board) -> str:
    """Canonicalise a fixed start FEN without its move counters.

    Some python-chess variants keep extra state in the FEN (Crazyhouse pockets,
    Three-check counters), so only the trailing halfmove/fullmove pair is
    dropped. Everything before it participates in the comparison.
    """
    fields = board.fen().split()
    if len(fields) < 4:
        raise ConfigError("not a FEN")
    if len(fields) >= 7:
        return " ".join(fields[:5]) + " 0 1"
    return " ".join(fields[:4]) + " 0 1"


def _fixed_start_fen(
    board_factory: Callable[[str | None], chess.Board],
) -> Callable[[str | None], str]:
    """Build a normaliser that only accepts ``board_factory``'s initial position."""
    expected = _canonical_board_fen(board_factory(None))

    def normalise(fen: str | None) -> str:
        if fen is None:
            return expected
        try:
            board = board_factory(fen)
        except ValueError as exc:
            raise ConfigError(f"invalid start FEN {fen!r}: {exc}") from None
        if _canonical_board_fen(board) != expected:
            raise ConfigError("this variant must start from its fixed initial position")
        return expected

    return normalise


_MOVE_NUMBER_PATTERN: Final = re.compile(r"^(?:[0-9]+\.+|\.\.\.)$")
_RESULT_TOKENS: Final = frozenset({"1-0", "0-1", "1/2-1/2", "*"})


def canonical_opening_line(ruleset_name: str, moves: str | Iterable[str]) -> tuple[str, ...]:
    """Replay a token's opening line and return canonical SAN.

    A registration may spell its opening as SAN or UCI, with or without move
    numbers. The chain stores SAN because that is what a transcript carries, so
    the same replay rule decides the stored form and validates it. Raises
    :class:`ConfigError` when the line is not legal for the named ruleset.
    """
    rules = ruleset_for(ruleset_name)
    board = rules.board(rules.normalise_start_fen(None))
    tokens = moves.split() if isinstance(moves, str) else list(moves)
    line: list[str] = []
    for raw in tokens:
        token = raw.strip()
        if not token or _MOVE_NUMBER_PATTERN.match(token) or token in _RESULT_TOKENS:
            continue
        try:
            move = board.parse_san(token)
        except ValueError:
            try:
                move = board.parse_uci(token)
            except ValueError:
                raise ConfigError(
                    f"{token!r} is not a legal move for ruleset {ruleset_name!r}"
                ) from None
        line.append(board.san(move))
        board.push(move)
    if not line:
        raise ConfigError("an opening line needs at least one move")
    return tuple(line)


def opening_line_fits(ruleset: VariantRules, moves: Iterable[str]) -> bool:
    """Whether ``moves`` is a legal book prefix under ``ruleset``.

    A network book is written for standard chess. Registered tokens may reuse a
    ruleset whose fixed position or victory condition rejects some of those
    lines, so the relay and verifier both ask this question before accepting a
    selected line.
    """
    line = tuple(moves)
    if line and not ruleset.allow_book_moves:
        return False
    try:
        board = ruleset.board(ruleset.normalise_start_fen(None))
    except (ConfigError, ValueError):
        return False
    for san in line:
        if board.is_game_over(claim_draw=False):
            return False
        try:
            move = board.parse_san(san)
        except ValueError:
            return False
        board.push(move)
    return not board.is_game_over(claim_draw=False)


def _variant_board(factory: type[chess.Board]) -> Callable[[str | None], chess.Board]:
    """Adapt a python-chess board class to the ruleset callable shape."""

    def build(fen: str | None = None) -> chess.Board:
        return factory() if fen is None else factory(fen)

    return build


@dataclass(frozen=True, slots=True)
class StartPosition:
    """A deterministic start position selected from the beacon.

    A ruleset whose start is not the ordinary initial position returns one of
    these instead of an opening-book line. ``moves`` is empty for the variants
    that ship today; a ruleset may still attach a short forced prefix.
    """

    line_id: int
    start_fen: str
    moves: tuple[str, ...] = ()
    book_version: str = "none"


@dataclass(frozen=True, slots=True)
class VariantRules:
    """Everything the protocol needs to know about one chess ruleset.

    ``name`` is the network variant name (``certificate.variant``).
    ``pgn_rules`` is the PGN ``Rules`` tag. ``board`` receives an already
    normalised start FEN and must return a board that replays the game.
    ``normalise_start_fen`` validates the PGN's optional ``FEN`` header and
    returns the canonical form the certificate carries.

    ``pgn_variant_tags`` lists acceptable ``Variant`` header values; ``None``
    accepts any value, while an empty set accepts only an absent header.
    ``pgn_variant_header`` is the canonical value a new certificate emits.
    """

    name: str
    pgn_rules: str
    board: Callable[[str], chess.Board] = chess.Board
    normalise_start_fen: Callable[[str | None], str] = normalise_standard_start_fen
    pgn_variant_tags: frozenset[str] | None = frozenset()
    pgn_variant_header: str | None = None
    """Canonical ``Variant`` header to emit, or ``None`` to omit it."""
    chess960: bool = False
    requires_start_fen: bool = False
    shredder_castling: bool = False
    allow_book_moves: bool = True
    start_selector: Callable[[bytes | str, str], StartPosition] | None = None
    description: str = ""

    def accepts_variant_header(self, value: str) -> bool:
        """Whether a PGN ``Variant`` header agrees with this ruleset."""
        return self.pgn_variant_tags is None or value in self.pgn_variant_tags


_REGISTRY: dict[str, VariantRules] = {}


def register_variant_rules(rules: VariantRules, *, replace: bool = False) -> VariantRules:
    """Register a ruleset for a token's ``VariantParams.rules``.

    Registration is explicit because a verifier must know the rules before it
    can validate a certificate. Re-registering an existing name is refused
    unless ``replace`` is true, so a typo cannot silently change consensus.
    """
    if not rules.name or not rules.pgn_rules:
        raise ConfigError("a variant ruleset needs a name and a PGN Rules value")
    if rules.name in _REGISTRY and not replace:
        raise ConfigError(f"variant ruleset {rules.name!r} is already registered")
    _REGISTRY[rules.name] = rules
    return rules


def ruleset_for(name: str) -> VariantRules:
    """Return the registered ruleset for variant ``name``."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ConfigError(f"unknown variant {name!r}; known variants: {known}") from None


def registered_rulesets() -> tuple[VariantRules, ...]:
    """Every registered ruleset, in registration order."""
    return tuple(_REGISTRY.values())


def registered_variants() -> tuple[str, ...]:
    """Every registered variant name, in registration order."""
    return tuple(_REGISTRY)


def ruleset_for_rules(pgn_rules: str) -> VariantRules:
    """Map a PGN ``Rules`` tag back to a registered variant.

    Built-in variants win when two rulesets share a tag. A token that reuses
    ``Rules: chess`` for an alternative start position should always verify with
    a certificate expectation, which names the variant explicitly.
    """
    matches = [rules for rules in _REGISTRY.values() if rules.pgn_rules == pgn_rules]
    if not matches:
        known = ", ".join(sorted({rules.pgn_rules for rules in _REGISTRY.values()}))
        raise ConfigError(f"unsupported Rules tag {pgn_rules!r}; expected one of: {known}")
    for rules in matches:
        if rules.name in (STANDARD_VARIANT, CHESS960_VARIANT):
            return rules
    return matches[0]


def is_registered_ruleset(name: str) -> bool:
    """True when ``name`` has a registered ruleset."""
    return name in _REGISTRY


def supported_rules() -> frozenset[str]:
    """Every PGN ``Rules`` value a registered ruleset may name."""
    return frozenset(rules.pgn_rules for rules in _REGISTRY.values())


def start_position_for(variant: str, beacon: bytes | str, match_id: str) -> StartPosition | None:
    """Return the beacon-selected start for ``variant``, or ``None`` for a book.

    The selected FEN is canonicalised through the ruleset so a relay and a
    verifier cannot disagree about its spelling.
    """
    rules = ruleset_for(variant)
    if rules.start_selector is None:
        return None
    selected = rules.start_selector(beacon, match_id)
    canonical = rules.normalise_start_fen(selected.start_fen)
    return StartPosition(
        line_id=selected.line_id,
        start_fen=canonical,
        moves=selected.moves,
        book_version=selected.book_version,
    )


def _select_chess960_start(beacon: bytes | str, match_id: str) -> StartPosition:
    index = chess960_position(beacon, match_id)
    return StartPosition(line_id=index, start_fen=chess960_start_fen(index))


register_variant_rules(
    VariantRules(
        name=STANDARD_VARIANT,
        pgn_rules="chess",
        pgn_variant_tags=frozenset({"Standard", "Chess"}),
        description="Ordinary chess from the initial position with an opening book.",
    )
)

register_variant_rules(
    VariantRules(
        name=CHESS960_VARIANT,
        pgn_rules="chess960",
        board=_chess960_board,
        normalise_start_fen=normalise_chess960_start_fen,
        pgn_variant_tags=frozenset({"Chess960", "Fischerandom", "Fischer Random"}),
        pgn_variant_header="Chess960",
        chess960=True,
        requires_start_fen=True,
        shredder_castling=True,
        allow_book_moves=False,
        start_selector=_select_chess960_start,
        description="Fischer Random from a beacon-selected back rank.",
    )
)


def _register_python_chess_variant(
    name: str,
    board: type[chess.Board],
    *,
    pgn_variant: str,
    description: str,
) -> VariantRules:
    """Register one of python-chess's fixed-start variants.

    These are *rulesets*, not network variants: a network still ships only its
    own ``variants`` tuple, and a registered token opts into one of these by
    naming it in ``register_token``. The board class is selected by name from
    python-chess, never supplied by a transaction.
    """
    factory = _variant_board(board)
    return register_variant_rules(
        VariantRules(
            name=name,
            pgn_rules=name,
            board=factory,
            normalise_start_fen=_fixed_start_fen(factory),
            pgn_variant_tags=frozenset({pgn_variant, name}),
            pgn_variant_header=pgn_variant,
            description=description,
        )
    )


_register_python_chess_variant(
    "antichess",
    chess.variant.AntichessBoard,
    pgn_variant="Antichess",
    description="Antichess: capturing is compulsory and the first side to lose all pieces wins.",
)
_register_python_chess_variant(
    "atomic",
    chess.variant.AtomicBoard,
    pgn_variant="Atomic",
    description="Atomic chess: every capture explodes the surrounding pieces.",
)
_register_python_chess_variant(
    "crazyhouse",
    chess.variant.CrazyhouseBoard,
    pgn_variant="Crazyhouse",
    description="Crazyhouse: captured pieces can be dropped back onto the board.",
)
_register_python_chess_variant(
    "giveaway",
    chess.variant.GiveawayBoard,
    pgn_variant="Giveaway",
    description="Giveaway chess: the first side to lose every piece wins.",
)
_register_python_chess_variant(
    "horde",
    chess.variant.HordeBoard,
    pgn_variant="Horde",
    description="Horde: one side is a wall of pawns, the other the usual army.",
)
_register_python_chess_variant(
    "kingofthehill",
    chess.variant.KingOfTheHillBoard,
    pgn_variant="King of the Hill",
    description="King of the Hill: reaching one of the four centre squares wins.",
)
_register_python_chess_variant(
    "racingkings",
    chess.variant.RacingKingsBoard,
    pgn_variant="Racing Kings",
    description="Racing Kings: kings race to the eighth rank, and checks are illegal.",
)
_register_python_chess_variant(
    "suicide",
    chess.variant.SuicideBoard,
    pgn_variant="Suicide",
    description="Suicide chess: compulsory captures, and losing every piece wins.",
)
_register_python_chess_variant(
    "threecheck",
    chess.variant.ThreeCheckBoard,
    pgn_variant="Three-check",
    description="Three-check: the first side to give check three times wins.",
)

__all__ = [
    "CHESS960_POSITIONS",
    "CHESS960_VARIANT",
    "STANDARD_VARIANT",
    "StartPosition",
    "VariantRules",
    "canonical_opening_line",
    "chess960_position",
    "chess960_start_fen",
    "is_registered_ruleset",
    "normalise_chess960_start_fen",
    "normalise_standard_start_fen",
    "opening_line_fits",
    "register_variant_rules",
    "registered_rulesets",
    "registered_variants",
    "ruleset_for",
    "ruleset_for_rules",
    "start_position_for",
    "supported_rules",
]
