"""Unit tests for the chess ruleset registry."""

from __future__ import annotations

import chess
import pytest

from sccp.chess.rulesets import (
    CHESS960_VARIANT,
    STANDARD_VARIANT,
    canonical_opening_line,
    chess960_position,
    chess960_start_fen,
    normalise_token_start_fen,
    opening_line_fits,
    ruleset_for,
    ruleset_for_rules,
    start_position_for,
    supported_rules,
)
from sccp.errors import ConfigError


def test_builtin_rulesets_are_registered() -> None:
    standard = ruleset_for(STANDARD_VARIANT)
    assert standard.pgn_rules == "chess"
    assert standard.requires_start_fen is False
    assert standard.allow_book_moves is True

    chess960 = ruleset_for(CHESS960_VARIANT)
    assert chess960.pgn_rules == "chess960"
    assert chess960.requires_start_fen is True
    assert chess960.allow_book_moves is False
    assert chess960.board(chess960_start_fen(0)).chess960 is True


def test_rules_tags_map_back_to_registered_variants() -> None:
    assert ruleset_for_rules("chess").name == STANDARD_VARIANT
    assert ruleset_for_rules("chess960").name == CHESS960_VARIANT
    assert supported_rules() == frozenset(
        {
            "chess",
            "chess960",
            "antichess",
            "atomic",
            "crazyhouse",
            "giveaway",
            "horde",
            "kingofthehill",
            "racingkings",
            "suicide",
            "threecheck",
        }
    )
    with pytest.raises(ConfigError, match="unsupported Rules tag"):
        ruleset_for_rules("bughouse")


def test_chess960_start_is_stable_and_beacon_driven() -> None:
    beacon = "11" * 32
    match_id = "22" * 32
    index = chess960_position(beacon, match_id)
    assert 0 <= index < 960
    assert chess960_position(bytes.fromhex(beacon), match_id) == index

    selected = start_position_for(CHESS960_VARIANT, beacon, match_id)
    assert selected is not None
    assert selected.line_id == index
    assert selected.start_fen == chess960_start_fen(index)
    assert selected.moves == ()
    assert selected.book_version == "none"

    # Shredder castling rights are only meaningful to a Chess960 board.
    assert chess.Board(selected.start_fen, chess960=True).is_valid()


def test_standard_variant_uses_the_opening_book() -> None:
    assert start_position_for(STANDARD_VARIANT, "11" * 32, "22" * 32) is None


def test_opening_lines_are_checked_against_their_ruleset_start() -> None:
    standard = ruleset_for(STANDARD_VARIANT)
    assert opening_line_fits(standard, ("e4", "e5", "Nf3")) is True
    assert opening_line_fits(standard, ("f3", "e5", "g4", "Qh4#")) is False

    # The horde starts with pawns on e4; a network line cannot be replayed there.
    horde = ruleset_for("horde")
    assert horde.allow_book_moves is True
    assert opening_line_fits(horde, ("e4",)) is False


def test_a_token_start_fen_is_canonical_and_drives_opening_replay() -> None:
    standard = ruleset_for(STANDARD_VARIANT)
    fen = "rnbqk1nr/pppp1ppp/8/2b1p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 2 3"
    canonical = "rnbqk1nr/pppp1ppp/8/2b1p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 0 1"
    assert normalise_token_start_fen(standard, fen) == canonical
    assert opening_line_fits(standard, ("Nf3",), start_fen=fen) is True
    assert opening_line_fits(standard, ("e4",), start_fen=fen) is False
    assert canonical_opening_line("standard", ("Nf3",), start_fen=fen) == ("Nf3",)

    # A pinned Chess960 start overrides the beacon selector but keeps Shredder-FEN.
    chess960 = chess960_start_fen(0)
    assert normalise_token_start_fen(CHESS960_VARIANT, chess960) == chess960
