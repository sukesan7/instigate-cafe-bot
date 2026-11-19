from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from icbot.errors import BotError
from icbot.games.blackjack_engine import GameState


class BlackjackFSMState(StrEnum):
    """Finite-state machine states for an interactive Blackjack round."""

    CREATED = "created"
    BET_RESERVED = "bet_reserved"
    DEALT = "dealt"
    PLAYER_TURN = "player_turn"
    DEALER_TURN = "dealer_turn"
    SETTLED = "settled"
    FORFEITED = "forfeited"
    TIMED_OUT = "timed_out"
    REFUNDED = "refunded"


LEGAL_TRANSITIONS: dict[BlackjackFSMState, set[BlackjackFSMState]] = {
    BlackjackFSMState.CREATED: {
        BlackjackFSMState.BET_RESERVED,
        BlackjackFSMState.REFUNDED,
    },
    BlackjackFSMState.BET_RESERVED: {
        BlackjackFSMState.DEALT,
        BlackjackFSMState.REFUNDED,
    },
    BlackjackFSMState.DEALT: {
        BlackjackFSMState.PLAYER_TURN,
        BlackjackFSMState.SETTLED,
        BlackjackFSMState.FORFEITED,
        BlackjackFSMState.TIMED_OUT,
        BlackjackFSMState.REFUNDED,
    },
    BlackjackFSMState.PLAYER_TURN: {
        BlackjackFSMState.PLAYER_TURN,
        BlackjackFSMState.DEALER_TURN,
        BlackjackFSMState.SETTLED,
        BlackjackFSMState.FORFEITED,
        BlackjackFSMState.TIMED_OUT,
        BlackjackFSMState.REFUNDED,
    },
    BlackjackFSMState.DEALER_TURN: {
        BlackjackFSMState.SETTLED,
        BlackjackFSMState.REFUNDED,
    },
    BlackjackFSMState.SETTLED: set(),
    BlackjackFSMState.FORFEITED: set(),
    BlackjackFSMState.TIMED_OUT: set(),
    BlackjackFSMState.REFUNDED: set(),
}

TERMINAL_FSM_STATES: frozenset[BlackjackFSMState] = frozenset(
    {
        BlackjackFSMState.SETTLED,
        BlackjackFSMState.FORFEITED,
        BlackjackFSMState.TIMED_OUT,
        BlackjackFSMState.REFUNDED,
    }
)


class IllegalStateTransitionError(BotError, ValueError):
    """Raised when an illegal FSM transition is attempted."""


@dataclass(frozen=True)
class FSMTransitionEvent:
    """Audit record of a state transition during a Blackjack round."""

    from_state: BlackjackFSMState
    to_state: BlackjackFSMState
    action: str
    timestamp: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BlackjackRound:
    """Domain model tracking a Blackjack round, its FSM lifecycle, and settlement identity."""

    round_id: str
    settlement_id: str
    guild_id: int
    user_id: int
    base_bet: int
    state: GameState | None = None
    fsm_state: BlackjackFSMState = BlackjackFSMState.CREATED
    events: list[FSMTransitionEvent] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)
    last_ts: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        *,
        guild_id: int,
        user_id: int,
        base_bet: int,
        state: GameState | None = None,
        clock: Any = None,
    ) -> BlackjackRound:
        now_ts = float(clock() if clock is not None else time.time())
        token = uuid.uuid4().hex[:8]
        round_id = f"bj_round:{int(guild_id)}:{int(user_id)}:{int(now_ts)}:{token}"
        settlement_id = f"bj_settle:{round_id}"
        instance = cls(
            round_id=round_id,
            settlement_id=settlement_id,
            guild_id=int(guild_id),
            user_id=int(user_id),
            base_bet=int(base_bet),
            state=state,
            fsm_state=BlackjackFSMState.CREATED,
            created_ts=now_ts,
            last_ts=now_ts,
        )
        instance.events.append(
            FSMTransitionEvent(
                from_state=BlackjackFSMState.CREATED,
                to_state=BlackjackFSMState.CREATED,
                action="init",
                timestamp=now_ts,
                details={"base_bet": int(base_bet)},
            )
        )
        return instance

    @property
    def is_terminal(self) -> bool:
        return self.fsm_state in TERMINAL_FSM_STATES

    def can_transition_to(self, target: BlackjackFSMState) -> bool:
        return target in LEGAL_TRANSITIONS.get(self.fsm_state, set())

    def transition_to(
        self,
        target: BlackjackFSMState,
        action: str,
        *,
        clock: Any = None,
        **details: Any,
    ) -> None:
        if not self.can_transition_to(target):
            raise IllegalStateTransitionError(
                f"Cannot transition Blackjack round from {self.fsm_state.value} to {target.value} (action: {action})."
            )
        now_ts = float(clock() if clock is not None else time.time())
        event = FSMTransitionEvent(
            from_state=self.fsm_state,
            to_state=target,
            action=action,
            timestamp=now_ts,
            details=details,
        )
        self.events.append(event)
        self.fsm_state = target
        self.last_ts = now_ts
        if self.is_terminal and self.state is not None:
            self.state.done = True
