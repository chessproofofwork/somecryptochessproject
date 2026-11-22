"""Match clocks, signed moves and clock evidence (PROTOCOL.md section 8.4).

A token declares its time control in :class:`~sccp.config.VariantParams`. A match
starts with both clocks at that control's ``initial_ms``; only the side to move
spends time. A move deducts ``max(0, elapsed - lag_grace_ms - delay_ms)`` from
the mover and adds the control's increment, if any. The move is rejected when
the result would be ``<= 0``. A control with neither an increment nor a delay is
sudden death. The relay is authoritative for live play; the on-chain verifier
only sees the signed move stream and the witness receipts in
:class:`~sccp.protocol.models.ClockEvidence`.
:data:`FIXED_TIME_CONTROL` remains the historical default for callers that have
no token parameters, but new code should pass the token's control explicitly.

Nothing here trusts a relay timestamp: move signatures cover the protocol move
payload, and witness signatures cover the receipt fields. Clock arithmetic is
deterministic integer arithmetic, so the same evidence validates identically on
every platform.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import chess

from sccp.config import (
    CLOCK_EVIDENCE_PLIES,
    DEFAULT_TIME_CONTROL_DELAY_MS,
    DEFAULT_TIME_CONTROL_INCREMENT_MS,
    DEFAULT_TIME_CONTROL_INITIAL_MS,
    TimeControlPeriod,
    VariantParams,
)
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, PublicKeyLike, sign_domain, verify_domain
from sccp.crypto.pq import (
    HybridKeypair,
    HybridPublicKey,
    ed25519_public_key,
    hybrid_public_key,
    hybrid_sign_domain,
    hybrid_verify_domain,
)
from sccp.errors import ClockError, KeyEncodingError
from sccp.protocol.commitments import (
    checkpoint_payload,
    clock_root,
    evidence_root,
    receipt_leaf,
)
from sccp.protocol.models import (
    MAX_CLOCK_MS,
    MAX_PLIES_LIMIT,
    CheckpointSignature,
    ClockEvidence,
    ClockWitnessSignature,
    Color,
    CommittedMove,
    FlagReceipt,
    MatchCertificate,
    MoveEvidence,
    MoveMessage,
    MoveReceipt,
    ResultValue,
    SignedMove,
    StreamCheckpoint,
    TimeControl,
    TimeoutClaim,
)
from sccp.serialization import Domain

FIXED_TIME_CONTROL: Final = TimeControl(
    initial_ms=DEFAULT_TIME_CONTROL_INITIAL_MS,
    increment_ms=DEFAULT_TIME_CONTROL_INCREMENT_MS,
)
"""The historical v1 control: 10 minutes per seat, no increment.

New networks set the control per variant in :class:`~sccp.config.VariantParams`;
this constant is the default for callers that predate token parameters.
"""

DEFAULT_LAG_GRACE_MS: Final = 250
"""Network-jitter credit granted once per move (PROTOCOL.md section 8.4)."""

MAX_LAG_GRACE_MS: Final = 1_000
"""Largest relay-policy grace; it never enters consensus or the game hash."""

OPPOSITE: Final[dict[Color, Color]] = {"white": "black", "black": "white"}

_TIME_CONTROL_SEGMENT_PATTERN: Final = re.compile(
    r"(?:(?P<moves>[0-9]+)/)?(?P<initial>[0-9]+)\+(?P<bonus>[0-9]+)(?P<delay>d)?\Z"
)


def opposite(seat: Color) -> Color:
    """Return the other colour."""
    return OPPOSITE[seat]


def _deduction_ms(elapsed_ms: int, lag_grace_ms: int, delay_ms: int) -> int:
    """The part of ``elapsed_ms`` charged to the mover.

    A delay is free time at the start of every turn, so it is subtracted from
    the elapsed time exactly like the relay's lag grace. A move made inside the
    delay costs nothing; unused delay is not carried to the next turn.
    """
    return max(0, elapsed_ms - lag_grace_ms - delay_ms)


def period_index(control: TimeControl, ply: int) -> int:
    """Which period of ``control`` applies at ``ply`` (0 is the base period)."""
    if not control.periods or control.first_period_moves is None:
        return 0
    remaining = ply - control.first_period_moves * 2
    if remaining < 0:
        return 0
    for index, period in enumerate(control.periods, start=1):
        if remaining < period.moves * 2:
            return index
        remaining -= period.moves * 2
    return len(control.periods)


def period_at(control: TimeControl, ply: int) -> TimeControl | TimeControlPeriod:
    """The clock parameters in force at ``ply``."""
    index = period_index(control, ply)
    return control if index == 0 else control.periods[index - 1]


def period_bonus_at(control: TimeControl, ply: int) -> int:
    """Millis added to *both* clocks exactly when move ``ply`` is reached.

    The first period starts at ply 0 with the base ``initial_ms``; each later
    period contributes its ``initial_ms`` on the ply that starts it.
    """
    if not control.periods or control.first_period_moves is None:
        return 0
    boundary = control.first_period_moves * 2
    for period in control.periods:
        if ply == boundary:
            return period.initial_ms
        boundary += period.moves * 2
    return 0


def bonus_through(control: TimeControl, ply: int) -> int:
    """Total period time added to each clock by the time ``ply`` is reached.

    A clock-evidence window may start after one or more period boundaries. The
    witness receipts then carry the accumulated period time, so verification
    has to add every bonus at or before the window before it can compare an
    idle clock. This is also the total a verifier can reconstruct for a PGN
    whose clock evidence starts mid-game.
    """
    if not control.periods or control.first_period_moves is None:
        return 0
    boundary = control.first_period_moves * 2
    total = 0
    for period in control.periods:
        if boundary > ply:
            break
        total += period.initial_ms
        boundary += period.moves * 2
    return total


def time_control_for(variant: VariantParams) -> TimeControl:
    """Build the protocol time control a token variant declares."""
    return TimeControl(
        initial_ms=variant.time_control_initial_ms,
        increment_ms=variant.time_control_increment_ms,
        delay_ms=variant.time_control_delay_ms,
        first_period_moves=variant.time_control_first_moves,
        periods=variant.time_control_periods,
    )


def validate_time_control(
    control: TimeControl,
    *,
    max_plies: int,
    require_fixed: bool = False,
    required: TimeControl | None = None,
) -> None:
    """Check the clock bounds and, optionally, the token's declared control.

    ``required`` is the token-level control and takes precedence over the legacy
    ``require_fixed`` flag. Passing neither checks only the protocol bounds.
    A delay never grows a clock, so the worst case uses the increment only.
    """
    expected = required
    fixed_only = require_fixed and required is None
    if fixed_only:
        expected = FIXED_TIME_CONTROL
    if expected is not None and control != expected:
        declared = "protocol-fixed" if fixed_only else "token's declared"
        raise ClockError(
            f"time control {control.model_dump()} is not the {declared} {expected.model_dump()}"
        )
    if control.increment_ms and control.delay_ms:
        raise ClockError("a time control uses an increment or a delay, not both")
    added = 0
    increments = [control.increment_ms]
    total_moves = control.first_period_moves or (max_plies // 2)
    for period in control.periods:
        if period.increment_ms and period.delay_ms:
            raise ClockError("a time control uses an increment or a delay, not both")
        added += period.initial_ms
        increments.append(period.increment_ms)
        total_moves += period.moves
    if total_moves > max_plies // 2:
        raise ClockError(
            f"time control periods cover {total_moves} moves, above max_plies={max_plies}"
        )
    upper = control.initial_ms + added + max(increments) * max_plies
    if upper > MAX_CLOCK_MS:
        raise ClockError(
            f"time control exceeds {MAX_CLOCK_MS} ms for max_plies={max_plies}: "
            f"{control.initial_ms} + {added} + {max(increments)} * {max_plies} = {upper}"
        )


def _format_period(initial_ms: int, bonus_ms: int, *, delay: bool) -> str:
    suffix = "d" if delay else ""
    return f"{initial_ms // 1000}+{bonus_ms // 1000}{suffix}"


def time_control_tag(control: TimeControl) -> str:
    """Render a control as a lossless PGN ``TimeControl`` tag.

    ``600+5`` is an increment, ``600+5d`` is a delay, and ``600+0`` is sudden
    death. The ``d`` suffix is what python-chess and common PGN tools use to
    distinguish a delay from an increment of the same length. FIDE-style
    multi-period controls use the standard colon-separated form, for example
    ``40/5400+0:20/1800+30``: 40 moves at 90 minutes, then a 20-move period of
    30 minutes with a 30-second increment. Every later period states its move
    count so the tag is lossless and the added time lands on one ply.
    """
    delay = bool(control.delay_ms)
    first = _format_period(
        control.initial_ms, control.delay_ms if delay else control.increment_ms, delay=delay
    )
    if not control.periods or control.first_period_moves is None:
        return first
    segments = [f"{control.first_period_moves}/{first}"]
    for period in control.periods:
        period_delay = bool(period.delay_ms)
        rendered = _format_period(
            period.initial_ms,
            period.delay_ms if period_delay else period.increment_ms,
            delay=period_delay,
        )
        segments.append(f"{period.moves}/{rendered}")
    return ":".join(segments)


def time_control_from_tag(tag: str) -> TimeControl:
    """Parse a PGN ``TimeControl`` tag, rejecting fractional seconds and junk.

    Both the single-period form (``600+5``) and the FIDE multi-period form
    (``40/5400+0:20/1800+30``) are accepted. Every later period must name its
    length in moves; a bare trailing segment would be ambiguous about when the
    added time starts and how long the period lasts.
    """
    raw_segments = tag.split(":")
    if not raw_segments or any(not segment for segment in raw_segments):
        raise ClockError(f"invalid TimeControl tag: {tag!r}")
    matches = [_TIME_CONTROL_SEGMENT_PATTERN.fullmatch(segment) for segment in raw_segments]
    if any(match is None for match in matches):
        raise ClockError(f"invalid TimeControl tag: {tag!r}")
    parsed = [match for match in matches if match is not None]

    def fields(match: re.Match[str]) -> tuple[int, int, bool, int | None]:
        initial_seconds = int(match.group("initial"))
        bonus_seconds = int(match.group("bonus"))
        is_delay = match.group("delay") is not None
        moves = int(match.group("moves")) if match.group("moves") is not None else None
        if initial_seconds < 1:
            raise ClockError(f"invalid TimeControl tag: {tag!r}")
        if is_delay and bonus_seconds == 0:
            raise ClockError(f"invalid TimeControl tag: {tag!r}; a delay must be positive")
        return initial_seconds, bonus_seconds, is_delay, moves

    first_initial, first_bonus, first_delay, first_moves = fields(parsed[0])
    if len(parsed) == 1:
        if first_moves is not None:
            raise ClockError(f"invalid TimeControl tag: {tag!r}; a move limit needs a later period")
        periods: tuple[TimeControlPeriod, ...] = ()
        first_period_moves = None
    elif first_moves is None:
        raise ClockError(f"invalid TimeControl tag: {tag!r}; the first period needs a move limit")
    else:
        later: list[TimeControlPeriod] = []
        for match in parsed[1:]:
            initial, bonus, is_delay, moves = fields(match)
            if moves is None:
                raise ClockError(
                    f"invalid TimeControl tag: {tag!r}; every later period needs a move limit"
                )
            later.append(
                TimeControlPeriod(
                    moves=moves,
                    initial_ms=initial * 1000,
                    increment_ms=0 if is_delay else bonus * 1000,
                    delay_ms=bonus * 1000 if is_delay else 0,
                )
            )
        periods = tuple(later)
        first_period_moves = first_moves

    worst_case = first_initial * 1000
    for period in periods:
        worst_case += period.initial_ms + max(period.increment_ms, 0) * MAX_PLIES_LIMIT
    if not first_delay:
        worst_case += first_bonus * 1000 * MAX_PLIES_LIMIT
    if worst_case > MAX_CLOCK_MS:
        raise ClockError(
            f"TimeControl tag {tag!r} exceeds MAX_CLOCK_MS for max_plies={MAX_PLIES_LIMIT}"
        )
    return TimeControl(
        initial_ms=first_initial * 1000,
        increment_ms=0 if first_delay else first_bonus * 1000,
        delay_ms=first_bonus * 1000 if first_delay else DEFAULT_TIME_CONTROL_DELAY_MS,
        first_period_moves=first_period_moves,
        periods=periods,
    )


def _check_grace(lag_grace_ms: int) -> int:
    if not 0 <= lag_grace_ms <= MAX_LAG_GRACE_MS:
        raise ClockError(f"lag_grace_ms must be in [0, {MAX_LAG_GRACE_MS}]")
    return lag_grace_ms


def _check_registered_key(public_keys: Mapping[str, str], address: str) -> PublicKeyLike:
    """The Ed25519 half of ``address``'s registered hybrid key.

    Registries hold full hybrid blobs; the move and receipt streams verify
    against the classical half and are anchored by a hybrid checkpoint, so the
    projection happens here, once, and never by guessing a key's length.
    """
    key = public_keys.get(address)
    if key is None:
        raise ClockError(f"no registered public key for {address}")
    try:
        return ed25519_public_key(key)
    except KeyEncodingError as exc:
        raise ClockError(f"registered key for {address} is malformed: {exc}") from None


def _check_registered_hybrid_key(public_keys: Mapping[str, str], address: str) -> HybridPublicKey:
    """The full hybrid key behind ``address``, used for checkpoint signatures."""
    key = public_keys.get(address)
    if key is None:
        raise ClockError(f"no registered public key for {address}")
    try:
        return hybrid_public_key(key)
    except KeyEncodingError as exc:
        raise ClockError(f"registered key for {address} is malformed: {exc}") from None


@dataclass(slots=True)
class MatchClock:
    """A mutable two-clock state for one live session.

    ``turn_started_at_ms`` comes from the relay's monotonic clock. Negative
    elapsed time is clamped to zero, so a clock source that steps backwards
    cannot create time.
    """

    control: TimeControl
    white_ms: int
    black_ms: int
    turn: Color
    turn_started_at_ms: int
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS
    started: bool = True
    ply: int = 0
    """Moves played so far, used to select the FIDE time-control period."""

    @classmethod
    def start(
        cls,
        control: TimeControl = FIXED_TIME_CONTROL,
        *,
        now_ms: int,
        lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
        ply: int = 0,
    ) -> MatchClock:
        """Start both clocks with White to move.

        ``ply`` is the number of board plies already played when the clocks
        start. The certified opening is not charged to either clock, but its
        moves still count toward FIDE period boundaries, so a session started
        after a 4-ply opening passes ``ply=4`` and receives any period bonus
        that boundary had already unlocked.
        """
        _check_grace(lag_grace_ms)
        if now_ms < 0:
            raise ClockError("now_ms must not be negative")
        if ply < 0:
            raise ClockError("ply must not be negative")
        initial = control.initial_ms + bonus_through(control, ply)
        return cls(
            control=control,
            white_ms=initial,
            black_ms=initial,
            turn="white",
            turn_started_at_ms=now_ms,
            lag_grace_ms=lag_grace_ms,
            ply=ply,
        )

    def elapsed_ms(self, now_ms: int) -> int:
        """Whole milliseconds since the current turn began, never negative."""
        if now_ms < 0:
            raise ClockError("now_ms must not be negative")
        return max(0, now_ms - self.turn_started_at_ms)

    def remaining_ms(self, now_ms: int) -> dict[Color, int]:
        """The remaining clocks after applying elapsed time, lag grace and delay.

        A clock that is not running (never started, or stopped after the game
        ended) does not count down: its stored values are returned unchanged.
        """
        if not self.started:
            return {"white": self.white_ms, "black": self.black_ms}
        period = period_at(self.control, self.ply)
        deduction = _deduction_ms(self.elapsed_ms(now_ms), self.lag_grace_ms, period.delay_ms)
        current = self.white_ms if self.turn == "white" else self.black_ms
        remaining = max(0, current - deduction)
        if self.turn == "white":
            return {"white": remaining, "black": self.black_ms}
        return {"white": self.white_ms, "black": remaining}

    def current_remaining_ms(self, now_ms: int) -> int:
        """Remaining time for the side to move."""
        return self.remaining_ms(now_ms)[self.turn]

    def is_flagged(self, now_ms: int) -> bool:
        """True when the side to move has no time left before making a move."""
        return self.current_remaining_ms(now_ms) <= 0

    def apply_move(self, now_ms: int) -> int:
        """Deduct the mover's elapsed time, apply delay/increment and switch turns.

        Returns the mover's remaining time after the move. Raises
        :class:`~sccp.errors.ClockError` when the move arrives at or after zero.
        """
        if not self.started:
            raise ClockError("the match clock has not started")
        started_at = max(self.turn_started_at_ms, now_ms)
        period = period_at(self.control, self.ply)
        deduction = _deduction_ms(self.elapsed_ms(now_ms), self.lag_grace_ms, period.delay_ms)
        current = self.white_ms if self.turn == "white" else self.black_ms
        after = current - deduction
        if after <= 0:
            raise ClockError(f"{self.turn} has no time left: {current} ms - {deduction} ms <= 0")
        after += period.increment_ms
        if self.turn == "white":
            self.white_ms = after
        else:
            self.black_ms = after
        self.turn = opposite(self.turn)
        # Never move the turn boundary backwards: a clock source that steps back
        # must not give the next mover more elapsed time than the previous one.
        self.turn_started_at_ms = started_at
        self.ply += 1
        # A FIDE time control adds its next-period time to both seats together
        # when the move limit is reached, after the mover's own increment.
        bonus = period_bonus_at(self.control, self.ply)
        if bonus:
            self.white_ms += bonus
            self.black_ms += bonus
            after += bonus
        return after

    def flag(self, now_ms: int) -> Color | None:
        """Return the flagged colour, or ``None`` when both clocks are positive."""
        return self.turn if self.is_flagged(now_ms) else None

    def stop(self, now_ms: int) -> None:
        """Freeze the clocks once the game is over.

        The stored values become the snapshot at ``now_ms`` and ``started`` is
        cleared, so later reads (a finished match view) are stable instead of
        counting down a game that is no longer being played.
        """
        frozen = self.remaining_ms(now_ms)
        self.white_ms = frozen["white"]
        self.black_ms = frozen["black"]
        self.started = False

    def snapshot(self, now_ms: int) -> dict[Color, int]:
        """The relay-visible ``{white_ms, black_ms}`` pair."""
        return self.remaining_ms(now_ms)


def flag_result(board: chess.Board, *, flagged: Color) -> ResultValue:
    """FIDE flag-fall result: a win unless the winner cannot mate by any line."""
    winner = opposite(flagged)
    if board.is_insufficient_material():
        return "1/2-1/2"
    return "1-0" if winner == "white" else "0-1"


def move_payload(move: SignedMove | MoveMessage) -> dict[str, object]:
    """The exact payload a seat signs for one move."""
    return {
        "network": move.network,
        "match_id": move.match_id,
        "seat": move.seat,
        "ply": move.ply,
        "san": move.san,
    }


def _stream_key(keypair: Keypair | HybridKeypair) -> Keypair:
    """The Ed25519 half that signs a move or receipt.

    Callers hold an account key (hybrid) while moves and receipts are the cheap
    inner-half stream, so the projection happens here once instead of at every
    call site. This is not a scheme choice: both branches are the same identity,
    and the hybrid checkpoint anchors whatever this half signed.
    """
    if isinstance(keypair, HybridKeypair):
        return keypair.ed25519
    return keypair


def sign_move(move: SignedMove | MoveMessage, keypair: Keypair | HybridKeypair) -> MoveMessage:
    """Sign ``move`` and return the same object with its signature filled in.

    ``clock_ms`` on a :class:`MoveMessage` is display data and is not covered by
    the signature; a plain :class:`SignedMove` is returned as a ``MoveMessage``
    with ``clock_ms = 0``.
    """
    if isinstance(move, MoveMessage):
        updated = move
    else:
        updated = MoveMessage(**move.model_dump(), clock_ms=0)
    signature = sign_domain(_stream_key(keypair), Domain.MOVE, move_payload(updated)).hex()
    return updated.model_copy(update={"signature": signature})


def verify_move_signature(
    move: SignedMove | MoveMessage,
    *,
    certificate: MatchCertificate,
    public_keys: Mapping[str, str],
) -> None:
    """Verify a move's seat, network, match id and Ed25519 signature."""
    if move.network != certificate.network:
        raise ClockError(
            f"move is for {move.network!r}, certificate is for {certificate.network!r}"
        )
    if move.match_id != certificate.match_id:
        raise ClockError("move does not belong to this certificate")
    if move.seat not in (certificate.white, certificate.black):
        raise ClockError(f"{move.seat} is not a seat of {certificate.match_id}")
    expected: Color = "white" if move.ply % 2 == 0 else "black"
    expected_address = certificate.white if expected == "white" else certificate.black
    if move.seat != expected_address:
        raise ClockError(
            f"ply {move.ply} belongs to {expected} ({expected_address}), "
            f"but the move is signed by {move.seat}"
        )
    key = _check_registered_key(public_keys, move.seat)
    try:
        signature = bytes.fromhex(move.signature)
    except ValueError:
        raise ClockError("move signature is not valid hex") from None
    if not verify_domain(key, Domain.MOVE, move_payload(move), signature):
        raise ClockError(f"invalid {move.seat} move signature at ply {move.ply}")


def sign_checkpoint(
    checkpoint: StreamCheckpoint,
    keypair: HybridKeypair,
    *,
    witness: str | None = None,
) -> StreamCheckpoint:
    """Add this witness's hybrid signature to ``checkpoint``."""
    address = witness or encode_address(keypair.public_key_bytes, checkpoint.network)
    if any(item.witness == address for item in checkpoint.witnesses):
        raise ClockError(f"witness {address} already signed this checkpoint")
    signature = hybrid_sign_domain(keypair, Domain.COMMITMENT, checkpoint_payload(checkpoint)).hex()
    updated = (*checkpoint.witnesses, CheckpointSignature(witness=address, signature=signature))
    return checkpoint.model_copy(update={"witnesses": updated})


def verify_checkpoint_signatures(
    checkpoint: StreamCheckpoint,
    *,
    public_keys: Mapping[str, str],
    quorum: int,
) -> frozenset[str]:
    """Verify a checkpoint's witness quorum and return the unique witnesses.

    Checkpoint signatures are hybrid, unlike the per-ply receipt signatures they
    anchor: this is the one place a quantum attacker has to break ML-DSA-65 to
    fabricate evidence (PROTOCOL.md section 8.5).
    """
    if quorum < 1:
        raise ClockError("clock_witness_quorum must be at least one")
    payload = checkpoint_payload(checkpoint)
    seen: set[str] = set()
    for entry in checkpoint.witnesses:
        if entry.witness in seen:
            raise ClockError(f"witness {entry.witness} signed one checkpoint more than once")
        seen.add(entry.witness)
        key = _check_registered_hybrid_key(public_keys, entry.witness)
        try:
            signature = bytes.fromhex(entry.signature)
        except ValueError:
            raise ClockError("checkpoint signature is not valid hex") from None
        if not hybrid_verify_domain(key, Domain.COMMITMENT, payload, signature):
            raise ClockError(f"invalid checkpoint signature from {entry.witness}")
    if len(seen) < quorum:
        raise ClockError(f"checkpoint has {len(seen)} witness signature(s), quorum is {quorum}")
    return frozenset(seen)


def build_checkpoint(
    *,
    network: str,
    kind: Literal["moves", "clock"],
    match_id: str,
    ply: int,
    root: str,
    signers: Iterable[tuple[str, HybridKeypair]],
) -> StreamCheckpoint:
    """Build and co-sign a stream checkpoint from ``(address, keypair)`` signers."""
    checkpoint = StreamCheckpoint(network=network, kind=kind, match_id=match_id, ply=ply, root=root)
    for address, keypair in signers:
        checkpoint = sign_checkpoint(checkpoint, keypair, witness=address)
    return checkpoint


def build_move_evidence(
    certificate: MatchCertificate,
    san_moves: Sequence[str],
    signers: Iterable[tuple[str, HybridKeypair]],
) -> MoveEvidence:
    """Assemble a refused-checkmate bundle: plies plus a hybrid checkpoint.

    Every ply is committed as ``{ply, seat, san}``; the checkpoint's root is
    folded from exactly those leaves, so a verifier recomputes it without any
    per-ply signature (PROTOCOL.md sections 8.5 and 9.3).
    """
    opening_plies = len(certificate.opening.moves)
    moves = tuple(
        CommittedMove(
            ply=ply,
            seat=certificate.white if ply % 2 == 0 else certificate.black,
            san=san_moves[ply],
        )
        for ply in range(opening_plies, len(san_moves))
    )
    if not moves:
        raise ClockError("a refused checkmate needs at least one post-opening move")
    checkpoint = build_checkpoint(
        network=certificate.network,
        kind="moves",
        match_id=certificate.match_id,
        ply=len(san_moves),
        root=evidence_root(certificate.network, certificate.match_id, moves),
        signers=signers,
    )
    return MoveEvidence(moves=moves, checkpoint=checkpoint)


def clock_window(receipts: Sequence[MoveReceipt]) -> tuple[MoveReceipt, ...]:
    """The suffix of ``receipts`` that fits the evidence window.

    Claim builders and the verifier agree through this one helper, so a bundle
    can never be assembled with more receipts than the verifier accepts.
    """
    return tuple(receipts[-CLOCK_EVIDENCE_PLIES:])


def build_clock_evidence(
    *,
    time_control: TimeControl,
    receipts: Sequence[MoveReceipt],
    final: FlagReceipt,
    signers: Iterable[tuple[str, HybridKeypair]],
    network: str,
) -> ClockEvidence:
    """Assemble flag evidence: the receipt window plus a hybrid checkpoint.

    The window is the last :data:`sccp.config.CLOCK_EVIDENCE_PLIES` receipts,
    which is what keeps a 400-ply flag bundle inside ``max_tx_bytes``: the
    arithmetic only ever needs a suffix, and the first receipt in the window
    anchors it.
    """
    window = tuple(receipts[-CLOCK_EVIDENCE_PLIES:])
    checkpoint = build_checkpoint(
        network=network,
        kind="clock",
        match_id=final.match_id,
        ply=final.ply,
        root=clock_root(network, window, final),
        signers=signers,
    )
    return ClockEvidence(
        time_control=time_control, final=final, receipts=window, checkpoint=checkpoint
    )


def verify_move_evidence(
    evidence: MoveEvidence | None,
    *,
    certificate: MatchCertificate,
    public_keys: Mapping[str, str],
    san_moves: Sequence[str],
    quorum: int,
) -> None:
    """Verify a refused-checkmate move stream against the replayed SAN list.

    Every post-opening move must be present, in order and authored by the seat
    whose turn it was; then the bundle's root must be the fold of exactly those
    leaves and the checkpoint must carry a witness quorum's hybrid signature.
    ``ply`` is the zero-based index into ``san_moves`` (PROTOCOL.md section 9).
    """
    if evidence is None or not evidence.moves:
        raise ClockError("move_evidence is empty")
    opening_plies = len(certificate.opening.moves)
    expected_plies = list(range(opening_plies, len(san_moves)))
    if [move.ply for move in evidence.moves] != expected_plies:
        raise ClockError("move_evidence does not cover every post-opening move exactly once")
    for move in evidence.moves:
        if move.san != san_moves[move.ply]:
            raise ClockError(
                f"move_evidence SAN at ply {move.ply} is {move.san!r}, "
                f"the PGN says {san_moves[move.ply]!r}"
            )
        expected_seat = certificate.white if move.ply % 2 == 0 else certificate.black
        if move.seat != expected_seat:
            raise ClockError(
                f"ply {move.ply} was played by {expected_seat}, "
                f"but the evidence attributes it to {move.seat}"
            )
    checkpoint = evidence.checkpoint
    if checkpoint.kind != "moves":
        raise ClockError(f"checkpoint kind is {checkpoint.kind!r}, expected 'moves'")
    if checkpoint.network != certificate.network:
        raise ClockError("move checkpoint is for another network")
    if checkpoint.match_id != certificate.match_id:
        raise ClockError("move checkpoint does not belong to this certificate")
    if checkpoint.ply != len(san_moves):
        raise ClockError(
            f"move checkpoint commits to {checkpoint.ply} plies, the game has {len(san_moves)}"
        )
    if checkpoint.root != evidence_root(certificate.network, certificate.match_id, evidence.moves):
        raise ClockError("move checkpoint root does not cover the evidence")
    verify_checkpoint_signatures(checkpoint, public_keys=public_keys, quorum=quorum)


def clock_receipt_payload(network: str, receipt: MoveReceipt | FlagReceipt) -> dict[str, object]:
    """The exact payload a witness signs for a move or flag receipt.

    This is also the commitment leaf (PROTOCOL.md section 8.5), so the signed
    bytes and the folded bytes are the same dict rather than two definitions
    that can drift apart.
    """
    return receipt_leaf(network, receipt)


def sign_receipt(
    receipt: MoveReceipt | FlagReceipt,
    keypair: Keypair | HybridKeypair,
    *,
    network: str,
    witness: str | None = None,
) -> MoveReceipt | FlagReceipt:
    """Add this witness's Ed25519 receipt signature to ``receipt``.

    The witness's hybrid key anchors the window through
    :func:`build_clock_evidence`; this per-ply signature is the cheap half.
    """
    address = witness or encode_address(keypair.public_key_bytes, network)
    if any(item.witness == address for item in receipt.witnesses):
        raise ClockError(f"witness {address} already signed this receipt")
    signature = sign_domain(
        _stream_key(keypair), Domain.CLOCK, clock_receipt_payload(network, receipt)
    ).hex()
    updated = (*receipt.witnesses, ClockWitnessSignature(witness=address, signature=signature))
    return receipt.model_copy(update={"witnesses": updated})


def verify_receipt_signatures(
    receipt: MoveReceipt | FlagReceipt,
    *,
    network: str,
    public_keys: Mapping[str, str],
    quorum: int,
) -> frozenset[str]:
    """Verify the witnesses on one receipt and return their unique addresses."""
    if quorum < 1:
        raise ClockError("clock_witness_quorum must be at least one")
    seen: set[str] = set()
    payload = clock_receipt_payload(network, receipt)
    for witness_signature in receipt.witnesses:
        if witness_signature.witness in seen:
            raise ClockError(
                f"witness {witness_signature.witness} signed one receipt more than once"
            )
        seen.add(witness_signature.witness)
        key = _check_registered_key(public_keys, witness_signature.witness)
        try:
            signature = bytes.fromhex(witness_signature.signature)
        except ValueError:
            raise ClockError("clock witness signature is not valid hex") from None
        if not verify_domain(key, Domain.CLOCK, payload, signature):
            raise ClockError(f"invalid clock witness signature from {witness_signature.witness}")
    if len(seen) < quorum:
        raise ClockError(f"receipt has {len(seen)} witness signature(s), quorum is {quorum}")
    return frozenset(seen)


def verify_clock_evidence(
    evidence: ClockEvidence,
    *,
    certificate: MatchCertificate,
    san_moves: Sequence[str],
    result: ResultValue,
    winner_seat: Color,
    public_keys: Mapping[str, str],
    quorum: int,
    max_plies: int,
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
    require_fixed: bool = True,
    expected_time_control: TimeControl | None = None,
) -> None:
    """Verify a complete flag bundle against a replayed game.

    ``san_moves`` is the full SAN list including the certified opening prefix.
    Move receipts use zero-based SAN indexes; the final flag receipt uses the
    total board ply count, so ``final.ply == len(san_moves)``.

    ``expected_time_control`` is the token's declared control. It takes
    precedence over the legacy ``require_fixed`` flag, which remains true by
    default so old callers keep the historical 10+0 check.
    """
    _check_grace(lag_grace_ms)
    validate_time_control(
        evidence.time_control,
        max_plies=max_plies,
        require_fixed=require_fixed,
        required=expected_time_control,
    )
    if evidence.time_control != certificate.time_control:
        raise ClockError("clock_evidence time_control does not match the certificate")
    validate_time_control(
        certificate.time_control,
        max_plies=max_plies,
        require_fixed=require_fixed,
        required=expected_time_control,
    )

    plies = len(san_moves)
    final = evidence.final
    if final.match_id != certificate.match_id:
        raise ClockError("flag receipt does not belong to this certificate")
    if plies < 1:
        raise ClockError("a flag at ply 0 is a no-show, not a win")
    if final.ply != plies:
        raise ClockError(f"flag receipt is for ply {final.ply}, but the game has {plies} plies")

    checkpoint = evidence.checkpoint
    if checkpoint.kind != "clock":
        raise ClockError(f"checkpoint kind is {checkpoint.kind!r}, expected 'clock'")
    if checkpoint.network != certificate.network:
        raise ClockError("clock checkpoint is for another network")
    if checkpoint.match_id != certificate.match_id:
        raise ClockError("clock checkpoint does not belong to this certificate")
    if checkpoint.ply != plies:
        raise ClockError(
            f"clock checkpoint commits to {checkpoint.ply} plies, the game has {plies}"
        )
    if checkpoint.root != clock_root(certificate.network, evidence.receipts, final):
        raise ClockError("clock checkpoint root does not cover the evidence")
    verify_checkpoint_signatures(checkpoint, public_keys=public_keys, quorum=quorum)
    flagged = opposite(winner_seat)
    to_move: Color = "white" if plies % 2 == 0 else "black"
    to_move_address = certificate.white if to_move == "white" else certificate.black
    if final.seat != to_move_address:
        raise ClockError(
            f"flag receipt names {final.seat}, but the side to move at ply {plies} "
            f"is {to_move} ({to_move_address})"
        )
    expected_result = "1-0" if winner_seat == "white" else "0-1"
    if result != expected_result:
        raise ClockError(
            f"time forfeit result {result!r} does not agree with winner seat {winner_seat!r}"
        )
    flagged_ms = final.white_ms if flagged == "white" else final.black_ms
    winner_ms = final.white_ms if winner_seat == "white" else final.black_ms
    if flagged_ms != 0:
        raise ClockError("the flagged seat's clock is not zero")
    if winner_ms <= 0:
        raise ClockError("the winner's clock is not positive")
    verify_receipt_signatures(
        final,
        network=certificate.network,
        public_keys=public_keys,
        quorum=quorum,
    )

    opening_plies = len(certificate.opening.moves)
    if len(evidence.receipts) > CLOCK_EVIDENCE_PLIES:
        raise ClockError(
            f"clock evidence carries {len(evidence.receipts)} receipts, the window is "
            f"{CLOCK_EVIDENCE_PLIES}"
        )
    if plies > opening_plies and not evidence.receipts:
        raise ClockError("clock evidence carries no move receipts")
    window_start = evidence.receipts[0].ply if evidence.receipts else plies
    if window_start < opening_plies:
        raise ClockError("clock evidence predates the certified opening")
    # The window is a suffix of the post-opening stream: it need not start at
    # the first move, but inside the window every ply must be present exactly
    # once and in order, and its first receipt is the arithmetic anchor.
    expected_indexes = list(range(window_start, plies))
    if [receipt.ply for receipt in evidence.receipts] != expected_indexes:
        raise ClockError("clock receipts are not a consecutive window ending at the flag")

    control = evidence.time_control
    running: dict[Color, int] = {
        "white": control.initial_ms,
        "black": control.initial_ms,
    }
    previous_received: int | None = None
    for receipt in evidence.receipts:
        if receipt.match_id != certificate.match_id:
            raise ClockError("clock receipt does not belong to this certificate")
        if receipt.san != san_moves[receipt.ply]:
            raise ClockError(
                f"receipt SAN at ply {receipt.ply} is {receipt.san!r}, "
                f"the PGN says {san_moves[receipt.ply]!r}"
            )
        mover_seat: Color = "white" if receipt.ply % 2 == 0 else "black"
        mover_address = certificate.white if mover_seat == "white" else certificate.black
        if receipt.seat != mover_address:
            raise ClockError(
                f"ply {receipt.ply} belongs to {mover_seat} ({mover_address}), "
                f"receipt names {receipt.seat}"
            )
        if previous_received is not None and receipt.received_at_ms < previous_received:
            raise ClockError("clock receipt timestamps are not monotonic")
        verify_receipt_signatures(
            receipt,
            network=certificate.network,
            public_keys=public_keys,
            quorum=quorum,
        )
        white_after = receipt.white_ms
        black_after = receipt.black_ms
        actual_after = white_after if mover_seat == "white" else black_after
        if previous_received is None:
            # The first receipt is the absolute arithmetic anchor. When the
            # window starts at the first post-opening move, the idle clock is
            # still at the starting value (including any period bonus the
            # opening crossed); a later suffix has already spent time, so both
            # clocks come from the signed receipt itself. The witness quorum
            # and clock checkpoint anchor those absolute values.
            if not 0 < white_after <= MAX_CLOCK_MS:
                raise ClockError("first receipt's white clock is out of range")
            if not 0 < black_after <= MAX_CLOCK_MS:
                raise ClockError("first receipt's black clock is out of range")
            idle = opposite(mover_seat)
            actual_idle = white_after if idle == "white" else black_after
            running = {"white": white_after, "black": black_after}
            if window_start == opening_plies:
                base = control.initial_ms + bonus_through(control, receipt.ply + 1)
                if actual_idle != base:
                    raise ClockError("receipt changed the clock of the idle side")
                running = {"white": base, "black": base}
                running[mover_seat] = actual_after
        else:
            elapsed = receipt.received_at_ms - previous_received
            period = period_at(control, receipt.ply)
            # The receipt records the position after this move, so its period
            # boundary is the one reached by ``receipt.ply + 1`` plies.
            bonus = period_bonus_at(control, receipt.ply + 1)
            deduction = _deduction_ms(elapsed, lag_grace_ms, period.delay_ms)
            expected_after = running[mover_seat] - deduction + period.increment_ms + bonus
            if expected_after <= 0:
                raise ClockError(
                    f"clock arithmetic says {mover_seat} flagged before ply {receipt.ply}"
                )
            if actual_after != expected_after:
                raise ClockError(
                    f"receipt clock for {mover_seat} at ply {receipt.ply} is "
                    f"{actual_after}, expected {expected_after}"
                )
            idle = opposite(mover_seat)
            actual_idle = white_after if idle == "white" else black_after
            if actual_idle != running[idle] + bonus:
                raise ClockError("receipt changed the clock of the idle side")
            running[mover_seat] = actual_after
            running[idle] += bonus
        previous_received = receipt.received_at_ms

    if previous_received is not None:
        final_elapsed = final.received_at_ms - previous_received
        if final_elapsed < 0:
            raise ClockError("flag receipt predates the last move receipt")
        final_period = period_at(control, plies)
        expected_flagged = running[flagged] - _deduction_ms(
            final_elapsed, lag_grace_ms, final_period.delay_ms
        )
        if expected_flagged > 0:
            raise ClockError(
                "flag receipt arrives before the flagged clock reaches zero "
                f"({expected_flagged} ms remaining)"
            )
        actual_winner = final.white_ms if winner_seat == "white" else final.black_ms
        if actual_winner != running[winner_seat]:
            raise ClockError("flag receipt changed the winner's clock")
    else:
        # No post-opening moves: the winner must still be at the initial clock,
        # plus any period time the certified opening had already unlocked. A
        # normal relay rejects this one-ply case at flag time.
        base = control.initial_ms + bonus_through(control, plies)
        if winner_ms != base:
            raise ClockError("flag receipt without move receipts changed the winner's clock")


def verify_timeout_claim(
    claim: TimeoutClaim,
    *,
    certificate: MatchCertificate,
    san_moves: Sequence[str],
    public_keys: Mapping[str, str],
    quorum: int,
    max_plies: int,
    lag_grace_ms: int = DEFAULT_LAG_GRACE_MS,
    expected_time_control: TimeControl | None = None,
) -> Color:
    """Verify a bonded timeout claim and return the winner's seat.

    The claim is the refused-result path: the winner presents its last signed
    move plus a complete clock bundle. Chain-level checks (bond locking, the
    dispute window and the claim registry) belong to the transaction layer; this
    function validates every field the chain would otherwise have to trust.
    """
    if claim.bond <= 0:
        raise ClockError("a timeout claim must lock a positive bond")
    if claim.network != certificate.network:
        raise ClockError("timeout claim and certificate name different networks")
    if claim.match_id != certificate.match_id:
        raise ClockError("timeout claim does not belong to this certificate")
    if claim.certificate != certificate:
        raise ClockError("timeout claim embeds a different certificate")
    if not san_moves:
        raise ClockError("a timeout claim needs at least one played move")

    last_move = claim.last_move
    last_ply = len(san_moves) - 1
    if last_move.ply != last_ply:
        raise ClockError(f"last move is ply {last_move.ply}, but the game ends at ply {last_ply}")
    if last_move.san != san_moves[last_ply]:
        raise ClockError(
            f"last move SAN is {last_move.san!r}, the PGN says {san_moves[last_ply]!r}"
        )
    verify_move_signature(last_move, certificate=certificate, public_keys=public_keys)

    final = claim.clock_evidence.final
    if final.seat == certificate.white:
        flagged: Color = "white"
    elif final.seat == certificate.black:
        flagged = "black"
    else:
        raise ClockError("flag receipt does not name a seat of this certificate")
    winner_seat = opposite(flagged)
    result: ResultValue = "1-0" if winner_seat == "white" else "0-1"
    verify_clock_evidence(
        claim.clock_evidence,
        certificate=certificate,
        san_moves=san_moves,
        result=result,
        winner_seat=winner_seat,
        public_keys=public_keys,
        quorum=quorum,
        max_plies=max_plies,
        lag_grace_ms=lag_grace_ms,
        expected_time_control=expected_time_control,
    )
    if last_move.seat != (certificate.white if winner_seat == "white" else certificate.black):
        raise ClockError("the timeout claim's last move was not made by the winner")
    return winner_seat


# Friendly aliases: callers may use any of these names without importing the
# implementation dataclass.
ChessClock = MatchClock
Clock = MatchClock


__all__ = [
    "DEFAULT_LAG_GRACE_MS",
    "FIXED_TIME_CONTROL",
    "MAX_LAG_GRACE_MS",
    "OPPOSITE",
    "ChessClock",
    "Clock",
    "MatchClock",
    "clock_receipt_payload",
    "flag_result",
    "move_payload",
    "opposite",
    "sign_move",
    "sign_receipt",
    "time_control_for",
    "time_control_from_tag",
    "time_control_tag",
    "validate_time_control",
    "verify_clock_evidence",
    "verify_move_evidence",
    "verify_move_signature",
    "verify_receipt_signatures",
    "verify_timeout_claim",
]
