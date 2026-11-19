from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from icbot.casino.blackjack_fsm import BlackjackFSMState, BlackjackRound
from icbot.casino.blackjack_store import BlackjackStore
from icbot.games.blackjack_engine import GameState

log = logging.getLogger(__name__)


@dataclass
class BJSession:
    """Active Blackjack session for an in-progress or terminal hand."""

    state: GameState
    user_id: int
    guild_id: int
    base_bet: int
    created_ts: float
    last_ts: float
    settled: bool = False
    round: BlackjackRound = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.round is None:
            r = BlackjackRound.create(
                guild_id=self.guild_id,
                user_id=self.user_id,
                base_bet=self.base_bet,
                state=self.state,
                clock=lambda: self.created_ts,
            )
            if self.settled or self.state.done:
                r.fsm_state = BlackjackFSMState.SETTLED
            self.round = r

    @property
    def round_id(self) -> str:
        return self.round.round_id if self.round else ""

    @property
    def fsm_state(self) -> BlackjackFSMState:
        return self.round.fsm_state if self.round else BlackjackFSMState.CREATED


class SessionManager:
    """Thread-safe and async-safe manager tracking active Blackjack sessions per (guild_id, user_id)."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self.sessions: dict[tuple[int, int], BJSession] = {}
        self._clock: Callable[[], float] = clock if clock is not None else time.time

    def get(self, guild_id: int, user_id: int) -> BJSession | None:
        return self.sessions.get((int(guild_id), int(user_id)))

    def set(self, s: BJSession) -> None:
        self.sessions[(int(s.guild_id), int(s.user_id))] = s

    def clear(self, guild_id: int, user_id: int) -> None:
        self.sessions.pop((int(guild_id), int(user_id)), None)

    def cleanup(self, ttl_seconds: int) -> list[BJSession]:
        """Evict stale sessions exceeding inactivity TTL."""
        now = self._clock()
        dead_keys = [k for k, s in self.sessions.items() if (now - s.last_ts) > ttl_seconds]
        evicted: list[BJSession] = []
        for k in dead_keys:
            sess = self.sessions.pop(k, None)
            if sess is not None:
                evicted.append(sess)
        return evicted

    async def recover_interrupted_sessions(
        self,
        store: BlackjackStore,
        *,
        guild_id: int | None = None,
        reason: str = "Interrupted by bot restart",
    ) -> list[dict[str, Any]]:
        """
        Detect orphaned bet reservations in the database from crashed/interrupted sessions
        and refund them safely to ensure zero lost bankroll and zero duplicate credit.
        """
        unsettled = await store.find_unsettled_reservations(guild_id=guild_id)
        recovered: list[dict[str, Any]] = []

        for item in unsettled:
            key = (item["guild_id"], item["user_id"])
            active = self.sessions.get(key)
            if active is None or active.state.done or active.settled:
                new_bal = await store.refund_unsettled_reservation(
                    transaction_id=item["transaction_id"],
                    guild_id=item["guild_id"],
                    user_id=item["user_id"],
                    amount=item["amount"],
                    reason=reason,
                )
                recovered.append(
                    {
                        "transaction_id": item["transaction_id"],
                        "guild_id": item["guild_id"],
                        "user_id": item["user_id"],
                        "refunded_amount": item["amount"],
                        "new_balance": new_bal,
                    }
                )
        return recovered
