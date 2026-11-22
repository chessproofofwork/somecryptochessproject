"""Chain-registered variants settle proofs and mint their own token.

A registration is ordinary consensus state: a certificate, a PGN and a win
proof for a token that did not exist at the parent tip must verify exactly like
a built-in variant's, and a block may carry the registration and the proof that
depends on it in the same body.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import chess
import pytest

from sccp.chain.mempool import Mempool
from sccp.chain.tx import txid
from sccp.chess.openings import OpeningBook
from sccp.chess.pgn import validate_pgn
from sccp.config import NetworkParams, VariantParams, derive_variant_asset_id
from sccp.errors import ProofVerificationError, RelayError
from sccp.miner.block_builder import BlockBuilder
from sccp.miner.matchmaker import Matchmaker
from sccp.protocol.models import OpeningRef
from sccp.protocol.pairing import RosterEntry, pair
from sccp.protocol.transcript import (
    build_win_proof,
    create_certificate,
    create_result,
    expected_from_certificate,
    opening_hash,
    pgn_headers,
    render_transcript_pgn,
    sign_certificate,
    sign_result,
)
from sccp.protocol.verification import ProofContext, ProofFailure, verify_win_proof
from sccp.wallet.service import WalletService

TOKEN = "dragonchess"
SYMBOL = "DRG"
DISPLAY_NAME = "Dragon Chess"
TOKEN_MIN_PLIES = 4
TOKEN_SUBSIDY = 40


def _token(*, min_plies: int = TOKEN_MIN_PLIES, opening_book: str = "v1") -> VariantParams:
    """A chain-registered token settled by the built-in standard ruleset."""
    return VariantParams(
        name=TOKEN,
        rules="chess",
        ruleset="standard",
        asset=derive_variant_asset_id(TOKEN, SYMBOL, 8, "standard"),
        symbol=SYMBOL,
        display_name=DISPLAY_NAME,
        subsidy=TOKEN_SUBSIDY,
        min_plies=min_plies,
        opening_book=opening_book,
    )


def _registered_match(
    *,
    params: NetworkParams,
    accounts: Any,
    book: OpeningBook,
    beacon: Any,
    roster: dict[str, RosterEntry],
    token: VariantParams,
) -> Any:
    """Play a scholar's mate as ``token`` and return its proof.

    The roster has already been rewritten to the token, so pairing, the
    certificate and the proof all name a variant the parent state does not
    contain.
    """
    beacon_bytes = bytes.fromhex(beacon.hash)
    matchups = pair(
        list(roster.values()),
        beacon_bytes,
        network=params.network_id,
        epoch=beacon.epoch,
        round_number=0,
        rounds_per_epoch=params.rounds_per_epoch,
    )
    assert len(matchups) == 1
    matchup = matchups[0]
    assert matchup.variant == token.name
    line = book.select(beacon_bytes, matchup.match_id)
    opening = OpeningRef(
        book_version=line.book_version,
        line_id=line.line_id,
        hash=opening_hash(
            line.book_version,
            line.line_id,
            line.moves,
            variant=token.name,
        ),
        moves=line.moves,
        variant=token.name,
    )
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(accounts.values(), start=1)
    }
    variants = {token.name: token}
    certificate = create_certificate(
        matchup,
        opening,
        engine_white=engines[matchup.white],
        engine_black=engines[matchup.black],
        variants=variants,
    )
    by_address = {account.address: account for account in accounts.values()}
    certificate = sign_certificate(certificate, "white", by_address[certificate.white].keypair)
    certificate = sign_certificate(certificate, "black", by_address[certificate.black].keypair)

    board = chess.Board()
    for san in certificate.opening.moves:
        board.push(board.parse_san(san))
    for san in ("Qh5", "Nf6", "Qxf7#"):
        board.push(board.parse_san(san))
    pgn = render_transcript_pgn(
        certificate,
        board,
        result="1-0",
        termination="checkmate",
        variants=variants,
    )
    parsed = validate_pgn(
        pgn,
        params=params,
        expected=expected_from_certificate(certificate),
        require_win=True,
        variants=variants,
    )
    result = create_result(certificate, parsed)
    for seat in ("white", "black"):
        result = sign_result(
            result,
            certificate,
            seat,
            by_address[getattr(certificate, seat)].keypair,
        )
    return build_win_proof(
        certificate,
        pgn,
        result.result_signatures,
        params=params,
        variants=variants,
    )


def test_a_registered_variant_builds_a_standard_pgn_and_verifies(
    protocol_params: NetworkParams,
    protocol_accounts: Any,
    protocol_book: OpeningBook,
    protocol_beacon: Any,
    protocol_availability: Any,
    protocol_public_keys: dict[str, str],
) -> None:
    token = _token()
    roster = {
        account.address: RosterEntry(
            address=account.address,
            max_games=3,
            variant=token.name,
        )
        for account in protocol_accounts.values()
    }
    availability = {item.miner: item.commitment for item in protocol_availability.values()}
    proof = _registered_match(
        params=protocol_params,
        accounts=protocol_accounts,
        book=protocol_book,
        beacon=protocol_beacon,
        roster=roster,
        token=token,
    )

    variants = {token.name: token}
    headers = pgn_headers(proof.certificate, variants=variants)
    assert headers["Rules"] == token.rules
    assert "Variant" not in headers
    assert "FEN" not in headers

    context = ProofContext(
        params=protocol_params,
        beacon=protocol_beacon,
        roster=roster,
        public_keys=protocol_public_keys,
        book=protocol_book,
        availability=availability,
        variants=variants,
    )
    verified = verify_win_proof(proof, context)
    assert verified.proof.variant == token.name
    assert verified.parsed.plies >= token.min_plies

    shorter = token.model_copy(update={"min_plies": 99})
    assert proof.low_ply is False
    short_context = ProofContext(
        params=protocol_params,
        beacon=protocol_beacon,
        roster=roster,
        public_keys=protocol_public_keys,
        book=protocol_book,
        availability=availability,
        variants={token.name: shorter},
    )
    with pytest.raises(ProofVerificationError) as excinfo:
        verify_win_proof(proof, short_context)
    assert excinfo.value.reason is ProofFailure.PGN
    assert "at least 99" in str(excinfo.value)


def test_a_relay_opening_a_registered_variant_uses_its_ruleset(
    protocol_params: NetworkParams,
    protocol_accounts: Any,
    protocol_book: OpeningBook,
    protocol_beacon: Any,
    protocol_public_keys: dict[str, str],
) -> None:
    """The relay resolves a token to its ruleset instead of executing token code.

    A token name is not a registered ruleset, so the certificate builder has to
    look the token up and play it with the ruleset the registration named.
    """
    token = _token(opening_book=protocol_book.version)
    roster = {
        account.address: RosterEntry(
            address=account.address,
            max_games=3,
            variant=token.name,
        )
        for account in protocol_accounts.values()
    }
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(protocol_accounts.values(), start=1)
    }
    relay = Matchmaker(
        protocol_params,
        beacon=protocol_beacon,
        roster=roster,
        public_keys=protocol_public_keys,
        engines=engines,
        book=protocol_book,
        variants={token.name: token},
    )
    relay.require_witnesses = False
    views = relay.open_round(0)
    assert len(views) == 1
    certificate = views[0].certificate
    assert certificate.variant == token.name
    assert certificate.opening.variant == token.name
    assert certificate.opening.book_version == protocol_book.version


def test_a_relay_store_remembers_a_registered_token(
    protocol_params: NetworkParams,
    protocol_accounts: Any,
    protocol_book: OpeningBook,
    protocol_beacon: Any,
    protocol_public_keys: dict[str, str],
    tmp_path: Path,
) -> None:
    """A restart must not forget the ruleset of an already-open session's token.

    Built-in variants come from the network parameters on every start; tokens do
    not, so the relay store carries them alongside its sessions.
    """
    token = _token(opening_book=protocol_book.version)
    roster = {
        account.address: RosterEntry(
            address=account.address,
            max_games=3,
            variant=token.name,
        )
        for account in protocol_accounts.values()
    }
    engines = {
        account.address: f"mock-v1:{index:016d}"
        for index, account in enumerate(protocol_accounts.values(), start=1)
    }
    store = tmp_path / "relay.json"
    relay = Matchmaker(
        protocol_params,
        beacon=protocol_beacon,
        roster=roster,
        public_keys=protocol_public_keys,
        engines=engines,
        book=protocol_book,
        store=store,
    )
    assert token.name not in relay.variants
    relay.set_variants({token.name: token})
    assert store.exists()

    reloaded = Matchmaker(
        protocol_params,
        beacon=protocol_beacon,
        roster=roster,
        public_keys=protocol_public_keys,
        engines=engines,
        book=protocol_book,
        store=store,
    )
    assert reloaded.variants[token.name] == token


def test_a_relay_store_cannot_shadow_a_built_in_variant(
    protocol_params: NetworkParams,
    protocol_accounts: Any,
    protocol_beacon: Any,
    protocol_public_keys: dict[str, str],
    tmp_path: Path,
) -> None:
    """A tampered store cannot replace the network's own variant parameters."""
    store = tmp_path / "relay.json"
    store.write_text(
        json.dumps(
            {
                "format": 2,
                "network": protocol_params.network_id,
                "variants": [protocol_params.variant("standard").model_dump(mode="json")],
                "sessions": [],
            }
        ),
        encoding="utf-8",
    )
    account = next(iter(protocol_accounts.values()))
    with pytest.raises(RelayError, match="cannot redefine built-in variant"):
        Matchmaker(
            protocol_params,
            beacon=protocol_beacon,
            roster={
                account.address: RosterEntry(
                    address=account.address,
                    max_games=1,
                    variant="standard",
                )
            },
            public_keys={account.address: protocol_public_keys[account.address]},
            engines={account.address: "mock-v1:0000000000000001"},
            store=store,
        )


def test_a_registration_and_its_proof_settle_in_the_same_block(
    live_chain: Any,
) -> None:
    chain = live_chain.chain
    record = chain.state.epochs[chain.state.current_epoch()]
    assert record is not None and record.beacon is not None
    token = _token(opening_book=chain.book.version)
    # A token reaches an epoch roster in the epoch after its registration, so
    # stage that roster here in order to exercise the body-ordering rule: the
    # block's transaction creates the token before its proof is checked and its
    # reward is validated. The synthetic part is the roster's history, not the
    # ordering this test asserts.
    record.roster = {
        address: member.model_copy(update={"variant": token.name})
        for address, member in record.roster.items()
    }
    roster = {
        address: RosterEntry(
            address=address,
            max_games=member.max_games,
            variant=member.variant,
        )
        for address, member in record.roster.items()
    }
    accounts = live_chain.accounts
    proof = _registered_match(
        params=chain.params,
        accounts=accounts,
        book=chain.book,
        beacon=record.beacon,
        roster=roster,
        token=token,
    )
    by_address = {account.address: account for account in accounts.values()}
    winner = by_address[proof.winner]
    asset = token.asset
    assert asset is not None

    registration = WalletService(chain.params, chain.state).build_register_token(
        winner.keypair,
        name=token.name,
        ruleset="standard",
        symbol=token.symbol,
        display_name=token.display_name,
        subsidy=token.subsidy,
        decimals=token.decimals,
        min_plies=token.min_plies,
    )
    pool = Mempool(params=chain.params, tip_state=chain.state, book=chain.book)
    pool.add_transaction(registration)
    assert token.name in pool.projection.tokens
    # The proof sees the pending registration, exactly as the block will.
    pool.add_proof(proof)

    builder = BlockBuilder(
        params=chain.params,
        chain=chain,
        mempool=pool,
        keypair=winner.keypair,
    )
    block = builder.build(timestamp=chain.state.timestamps[-1] + 1)
    assert block is not None
    assert block.transactions == (registration,)
    assert block.proofs == (proof,)
    coinbase = block.coinbase
    assert coinbase is not None
    assert coinbase.payload["variant_rewards"] == {asset: TOKEN_SUBSIDY}
    assert coinbase.payload["to"] == proof.winner

    # Dropping the registration invalidates the proof that depended on it, so a
    # later block can never be offered a proof the chain could not settle.
    pool.remove_transaction(txid(registration))
    assert not pool.proofs

    chain.add_block(block)
    state = chain.state
    assert state.tokens[token.name].variant.asset == asset
    assert state.variant(token.name).min_plies == TOKEN_MIN_PLIES
    assert state.variant_for_asset(asset) == token
    assert state.minted_assets[asset] == TOKEN_SUBSIDY
    assert state.account(proof.winner).immature_assets[asset] == TOKEN_SUBSIDY
