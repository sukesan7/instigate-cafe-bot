from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from icbot.casino.blackjack_fsm import (
    BlackjackFSMState,
    BlackjackRound,
    IllegalStateTransitionError,
)
from icbot.casino.blackjack_session import BJSession, SessionManager
from icbot.casino.blackjack_settlement import (
    settle_blackjack_round,
)
from icbot.casino.blackjack_store import BlackjackStore
from icbot.casino.blackjack_views import (
    BlackjackTerminalView,
    BlackjackView,
    render_state,
)
from icbot.db import Database
from icbot.games import blackjack_engine as bj
from tests.helpers import build_fake_member


class _FixedShoe:
    def __init__(self, cards: list[bj.Card]) -> None:
        self._cards = list(reversed(cards))

    def draw(self) -> bj.Card:
        if not self._cards:
            raise AssertionError("FixedShoe exhausted unexpectedly in test")
        return self._cards.pop()

    def needs_reshuffle(self) -> bool:
        return False


# ==============================================================================
# 1. FSM Legal Transitions & Invariant Auditing
# ==============================================================================


def test_fsm_legal_transitions_and_audit_events():
    """Verify legal state transitions emit structured audit events and advance state."""
    clock_val = 1700000000.0
    state = bj.GameState.start(bet=100, shoe=bj.Shoe(seed=42))
    round_obj = BlackjackRound.create(
        guild_id=100,
        user_id=200,
        base_bet=100,
        state=state,
        clock=lambda: clock_val,
    )

    assert round_obj.fsm_state == BlackjackFSMState.CREATED
    assert round_obj.is_terminal is False
    assert len(round_obj.events) == 1
    assert round_obj.events[0].action == "init"

    # Transition to BET_RESERVED
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.BET_RESERVED,
        action="reserve_wager",
        clock=lambda: clock_val,
        amount=100,
    )
    assert round_obj.fsm_state == BlackjackFSMState.BET_RESERVED

    # Transition to DEALT
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.DEALT,
        action="deal_cards",
        clock=lambda: clock_val,
    )
    assert round_obj.fsm_state == BlackjackFSMState.DEALT

    # Transition to PLAYER_TURN
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.PLAYER_TURN,
        action="begin_turn",
        clock=lambda: clock_val,
    )
    assert round_obj.fsm_state == BlackjackFSMState.PLAYER_TURN

    # Self-transition within PLAYER_TURN (e.g. hit or split)
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.PLAYER_TURN,
        action="player_hit",
        clock=lambda: clock_val,
        card="K♠",
    )
    assert round_obj.fsm_state == BlackjackFSMState.PLAYER_TURN

    # Transition to DEALER_TURN
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.DEALER_TURN,
        action="player_stand",
        clock=lambda: clock_val,
    )
    assert round_obj.fsm_state == BlackjackFSMState.DEALER_TURN

    # Transition to SETTLED (terminal)
    clock_val += 1.0
    round_obj.transition_to(
        BlackjackFSMState.SETTLED,
        action="settle",
        clock=lambda: clock_val,
        delta=100,
    )
    assert round_obj.fsm_state == BlackjackFSMState.SETTLED
    assert round_obj.is_terminal is True
    assert round_obj.state.done is True

    # Audit event verification
    assert len(round_obj.events) == 7
    event_actions = [e.action for e in round_obj.events]
    assert event_actions == [
        "init",
        "reserve_wager",
        "deal_cards",
        "begin_turn",
        "player_hit",
        "player_stand",
        "settle",
    ]


def test_fsm_illegal_transition_rejections():
    """Verify illegal transitions are rejected with IllegalStateTransitionError."""
    state = bj.GameState.start(bet=50, shoe=bj.Shoe(seed=42))
    round_obj = BlackjackRound.create(
        guild_id=1,
        user_id=2,
        base_bet=50,
        state=state,
    )

    # Cannot skip directly from CREATED to SETTLED
    with pytest.raises(
        IllegalStateTransitionError, match="Cannot transition Blackjack round from created to settled"
    ):
        round_obj.transition_to(BlackjackFSMState.SETTLED, action="skip_to_settle")

    # Cannot skip from CREATED to DEALER_TURN
    with pytest.raises(
        IllegalStateTransitionError, match="Cannot transition Blackjack round from created to dealer_turn"
    ):
        round_obj.transition_to(BlackjackFSMState.DEALER_TURN, action="skip_to_dealer")

    # Advance to terminal state
    round_obj.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve")
    round_obj.transition_to(BlackjackFSMState.DEALT, action="deal")
    round_obj.transition_to(BlackjackFSMState.FORFEITED, action="forfeit")

    # Terminal state cannot transition to anything
    assert round_obj.is_terminal is True
    with pytest.raises(
        IllegalStateTransitionError, match="Cannot transition Blackjack round from forfeited to player_turn"
    ):
        round_obj.transition_to(BlackjackFSMState.PLAYER_TURN, action="revive")

    with pytest.raises(
        IllegalStateTransitionError, match="Cannot transition Blackjack round from forfeited to settled"
    ):
        round_obj.transition_to(BlackjackFSMState.SETTLED, action="settle_after_forfeit")


# ==============================================================================
# 2. Simulated Process Crash at Every State & Restart Recovery
# ==============================================================================


async def test_process_crash_simulation_at_every_state_and_recovery(tmp_db: Database):
    """
    Simulate process crash/restart at every state of a Blackjack round:
    - CREATED: no wager escrowed -> recovery leaves bankroll untouched.
    - BET_RESERVED: wager escrowed in SQLite -> recovery detects dangling bet and refunds.
    - DEALT: cards dealt, crashed -> recovery refunds escrowed bet.
    - PLAYER_TURN: mid-game crash -> recovery refunds escrowed bet.
    - SETTLED: round resolved in ledger -> recovery sees settlement, does NOT issue double credit.
    """
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=555)
    sessions = SessionManager()
    user_id = 9001

    # 1. Crash at CREATED
    await store.ensure_user(user_id)
    assert await store.get_bankroll(user_id) == 1000
    recovered = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered) == 0
    assert await store.get_bankroll(user_id) == 1000

    # 2. Crash at BET_RESERVED (bet was escrowed, then bot crashed before cards dealt)
    round_r1 = BlackjackRound.create(guild_id=555, user_id=user_id, base_bet=200)
    bet_tx_1 = f"bj_bet:{round_r1.round_id}"
    await store.reserve_wager(user_id, 200, idempotency_key=bet_tx_1)
    round_r1.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=200)
    assert await store.get_bankroll(user_id) == 800

    # Simulate crash: process dies, in-memory sessions are empty
    sessions.sessions.clear()

    # Restart recovery runs
    recovered = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered) == 1
    assert recovered[0]["transaction_id"] == bet_tx_1
    assert recovered[0]["refunded_amount"] == 200
    assert recovered[0]["new_balance"] == 1000
    assert await store.get_bankroll(user_id) == 1000

    # Second recovery pass is idempotent (no duplicate refunds)
    recovered_again = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered_again) == 0
    assert await store.get_bankroll(user_id) == 1000

    # 3. Crash at DEALT
    round_r2 = BlackjackRound.create(guild_id=555, user_id=user_id, base_bet=300)
    bet_tx_2 = f"bj_bet:{round_r2.round_id}"
    await store.reserve_wager(user_id, 300, idempotency_key=bet_tx_2)
    round_r2.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=300)
    round_r2.transition_to(BlackjackFSMState.DEALT, action="deal")
    assert await store.get_bankroll(user_id) == 700

    # Process crash
    sessions.sessions.clear()

    # Recovery refunds $300
    recovered = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered) == 1
    assert recovered[0]["refunded_amount"] == 300
    assert await store.get_bankroll(user_id) == 1000

    # 4. Crash at PLAYER_TURN
    round_r3 = BlackjackRound.create(guild_id=555, user_id=user_id, base_bet=150)
    bet_tx_3 = f"bj_bet:{round_r3.round_id}"
    await store.reserve_wager(user_id, 150, idempotency_key=bet_tx_3)
    round_r3.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=150)
    round_r3.transition_to(BlackjackFSMState.DEALT, action="deal")
    round_r3.transition_to(BlackjackFSMState.PLAYER_TURN, action="begin_turn")
    assert await store.get_bankroll(user_id) == 850

    # Process crash
    sessions.sessions.clear()

    # Recovery refunds $150
    recovered = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered) == 1
    assert recovered[0]["refunded_amount"] == 150
    assert await store.get_bankroll(user_id) == 1000

    # 5. Hand properly settled (win, loss, or push) -> crash after settlement
    round_r4 = BlackjackRound.create(guild_id=555, user_id=user_id, base_bet=100)
    bet_tx_4 = f"bj_bet:{round_r4.round_id}"
    await store.reserve_wager(user_id, 100, idempotency_key=bet_tx_4)
    round_r4.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=100)

    # Shoe where player 19 beats dealer 17
    shoe = _FixedShoe([bj.Card("10", "♠"), bj.Card("10", "♦"), bj.Card("9", "♥"), bj.Card("7", "♣")])
    state = bj.GameState.start(bet=100, shoe=shoe)  # type: ignore[arg-type]
    round_r4.state = state
    round_r4.transition_to(BlackjackFSMState.DEALT, action="deal")
    round_r4.transition_to(BlackjackFSMState.PLAYER_TURN, action="begin_turn")

    # Stand and settle: net win +100, payout +200, bankroll 900 + 200 = 1100
    state.stand()
    settle_res = await settle_blackjack_round(round_r4, store)
    assert settle_res.net_delta == 100
    assert settle_res.payout_credit == 200
    assert settle_res.new_bankroll == 1100
    assert await store.get_bankroll(user_id) == 1100

    # Simulate process restart after settlement
    sessions.sessions.clear()

    # Recovery must NOT refund the already settled round (no phantom money!)
    recovered = await sessions.recover_interrupted_sessions(store, guild_id=555)
    assert len(recovered) == 0
    assert await store.get_bankroll(user_id) == 1100


# ==============================================================================
# 3. Game Replay Lifecycle (Deal, Hit, Stand, Double, Split, Expiry)
# ==============================================================================


async def test_game_replay_deterministic_split_and_double_down(tmp_db: Database):
    """Replay complete multi-hand game lifecycle with split, double, and settlement."""
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=1)
    user_id = 42
    await store.ensure_user(user_id)

    # Fixed cards:
    # Deal: player gets 8♠, dealer gets 6♦, player gets 8♥ (pair of 8s!), dealer gets 10♣
    # Hand 1 split draw: draws 3♣ (total 11) -> double down draws 10♠ (total 21)
    # Hand 2 split draw: draws 9♦ (total 17) -> stands on 17
    # Dealer turn (has 16): draws 10♥ -> dealer busts with 26!
    # Expected outcome:
    # Hand 1 ($200): win vs dealer bust -> +200 net
    # Hand 2 ($100): win vs dealer bust -> +100 net
    # Total net: +300. Payout: $200 + $100 + $300 = $600.
    # Bankroll: 1000 - 100 (initial) - 100 (split) - 100 (double) + 600 (payout) = 1300.
    shoe = _FixedShoe(
        [
            bj.Card("8", "♠"),
            bj.Card("6", "♦"),
            bj.Card("8", "♥"),
            bj.Card("10", "♣"),
            bj.Card("3", "♣"),  # split card hand 1
            bj.Card("9", "♦"),  # split card hand 2
            bj.Card("10", "♠"),  # double card hand 1
            bj.Card("10", "♥"),  # dealer draw -> busts on 26
        ]
    )

    state = bj.GameState.start(bet=100, shoe=shoe)  # type: ignore[arg-type]
    round_obj = BlackjackRound.create(guild_id=1, user_id=user_id, base_bet=100, state=state)
    await store.reserve_wager(user_id, 100, idempotency_key=f"bj_bet:{round_obj.round_id}")
    round_obj.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=100)
    round_obj.transition_to(BlackjackFSMState.DEALT, action="deal")
    round_obj.transition_to(BlackjackFSMState.PLAYER_TURN, action="begin_turn")

    # Split pair of 8s
    assert state.can_split() is True
    await store.reserve_wager(user_id, 100, idempotency_key=f"bj_split:{round_obj.round_id}:1")
    assert state.split() is True
    assert len(state.hands) == 2
    assert state.bets == [100, 100]

    # Hand 1: 8 + 3 = 11. Can double!
    assert state.active_index == 0
    assert state.can_double() is True
    await store.reserve_wager(user_id, 100, idempotency_key=f"bj_double:{round_obj.round_id}:0")
    state.double()
    assert state.bets == [200, 100]

    # Hand 1 auto-stands after double. Active index advances to 1.
    assert state.active_index == 1
    assert state.active_hand().best_total() == 17
    # Hand 2 stands on 17
    state.stand()

    # Both hands completed -> state is done!
    assert state.done is True

    # Settle round
    settlement = await settle_blackjack_round(round_obj, store)
    assert settlement.net_delta == 300
    assert settlement.payout_credit == 600
    assert settlement.new_bankroll == 1300
    assert await store.get_bankroll(user_id) == 1300


# ==============================================================================
# 4. Security & Isolation: Unauthorized Guild Member Click Rejection
# ==============================================================================


async def test_unauthorized_guild_member_click_rejection(make_interaction, tmp_db: Database):
    """Verify other guild members cannot interact with another user's live hand or terminal view."""
    owner = build_fake_member(user_id=1001, display_name="HandOwner")
    attacker = build_fake_member(user_id=9999, display_name="Intruder")

    session_mgr = SessionManager()
    live_view = BlackjackView(
        guild_id=1,
        user_id=owner.id,
        session_mgr=session_mgr,
    )

    # 1. Interaction check rejects attacker
    fake_inter_attacker = make_interaction(user=attacker)
    allowed = await live_view.interaction_check(fake_inter_attacker)
    assert allowed is False
    assert fake_inter_attacker.last_reply.ephemeral is True
    assert "Not your hand" in (fake_inter_attacker.last_reply.content or "")

    # 2. Interaction check allows owner
    fake_inter_owner = make_interaction(user=owner)
    assert await live_view.interaction_check(fake_inter_owner) is True

    # 3. Terminal view also rejects attacker
    term_view = BlackjackTerminalView(
        guild_id=1,
        user_id=owner.id,
        last_bet=100,
        session_mgr=session_mgr,
    )
    fake_inter_attacker_term = make_interaction(user=attacker)
    assert await term_view.interaction_check(fake_inter_attacker_term) is False
    assert "Not your hand" in (fake_inter_attacker_term.last_reply.content or "")


# ==============================================================================
# 5. Concurrency & Race Condition Protection
# ==============================================================================


async def test_racey_double_click_protection_and_idempotency(make_interaction, tmp_db: Database):
    """Verify rapid concurrent clicks inside BlackjackView do not trigger double actions."""
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=1)
    user = build_fake_member(user_id=2002, display_name="FastClicker")
    await store.ensure_user(user.id)

    # Deal player 12 vs dealer 17
    shoe = _FixedShoe(
        [
            bj.Card("10", "♠"),
            bj.Card("10", "♦"),
            bj.Card("2", "♥"),
            bj.Card("7", "♣"),
            bj.Card("10", "♣"),  # Draw on hit -> busts to 22
        ]
    )
    state = bj.GameState.start(bet=100, shoe=shoe)  # type: ignore[arg-type]
    session_mgr = SessionManager()
    round_obj = BlackjackRound.create(guild_id=1, user_id=user.id, base_bet=100, state=state)
    await store.reserve_wager(user.id, 100, idempotency_key=f"bj_bet:{round_obj.round_id}")
    round_obj.transition_to(BlackjackFSMState.BET_RESERVED, action="reserve", amount=100)
    round_obj.transition_to(BlackjackFSMState.DEALT, action="deal")
    round_obj.transition_to(BlackjackFSMState.PLAYER_TURN, action="begin_turn")
    sess = BJSession(
        state=state,
        user_id=user.id,
        guild_id=1,
        base_bet=100,
        created_ts=1.0,
        last_ts=1.0,
        round=round_obj,
    )
    session_mgr.set(sess)

    view = BlackjackView(
        guild_id=1,
        user_id=user.id,
        session_mgr=session_mgr,
    )
    view.message = AsyncMock()

    inter1 = make_interaction(user=user)
    inter2 = make_interaction(user=user)

    # Click 1 hits -> busts to 22, settles hand and stops view
    await view.hit_btn.callback(inter1)
    assert state.done is True
    assert sess.settled is True
    assert view.is_finished() is True

    # Click 2 arrives immediately after (stale menu callback)
    await view.hit_btn.callback(inter2)
    assert inter2.last_reply.ephemeral is True
    assert "No active hand" in (inter2.last_reply.content or "") or "expired" in (
        inter2.last_reply.content or ""
    )

    # Bankroll reflects exact-once settlement (-100 net -> 900)
    assert await store.get_bankroll(user.id) == 900


# ==============================================================================
# 6. Inactivity Timeout & Safe UI Disablement
# ==============================================================================


async def test_inactivity_timeout_auto_forfeit_and_view_expiry(make_interaction, tmp_db: Database):
    """Verify on_timeout auto-forfeits active wager and disables buttons safely."""
    user = build_fake_member(user_id=3003, display_name="AFKPlayer")
    store = BlackjackStore(tmp_db, starting_bankroll=1000, guild_id=1)
    await store.ensure_user(user.id)

    shoe = _FixedShoe([bj.Card("9", "♠"), bj.Card("10", "♦"), bj.Card("7", "♥"), bj.Card("6", "♣")])
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=shoe):
        start_inter = make_interaction(user=user, command_name="ic casino blackjack")
        from icbot.features.blackjack import blackjack

        await blackjack.callback(start_inter, bet=150)

    view = start_inter.last_reply.view
    assert isinstance(view, BlackjackView)

    # Fire inactivity timeout
    await view.on_timeout()

    # Hand should be forfeited
    sess = view._get_session()
    assert sess is None  # Session cleared after forfeit
    assert view.is_finished() is True

    # Bankroll reflects full wager forfeiture (1000 - 150 = 850)
    assert await store.get_bankroll(user.id, guild_id=start_inter.guild.id) == 850


# ==============================================================================
# 7. UX Quality Gate: Embed Readability & Rendering
# ==============================================================================


def test_ux_quality_gate_embed_readability():
    """Verify render_state formats cards, scores, dealer hidden card, and flags."""
    shoe = _FixedShoe(
        [
            bj.Card("A", "♠"),  # Player 1
            bj.Card("10", "♦"),  # Dealer 1 (hidden)
            bj.Card("7", "♥"),  # Player 2
            bj.Card("9", "♣"),  # Dealer 2 (shown)
        ]
    )
    state = bj.GameState.start(bet=50, shoe=shoe)  # type: ignore[arg-type]

    # Live state: dealer's first card hidden
    dealer, dealer_tot, player = render_state(state, hide_dealer=True)
    assert "?? 9♣" in dealer
    assert dealer_tot == "??"
    assert "A♠ 7♥" in player
    assert "**18**" in player  # Soft 18

    # Terminal state: dealer cards revealed
    dealer_rev, dealer_tot_rev, _ = render_state(state, hide_dealer=False)
    assert "10♦ 9♣" in dealer_rev
    assert dealer_tot_rev == "19"
