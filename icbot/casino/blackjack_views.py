from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast

import discord

from icbot.casino.blackjack_fsm import BlackjackFSMState, BlackjackRound
from icbot.casino.blackjack_session import BJSession, SessionManager
from icbot.casino.blackjack_settlement import (
    reserved_bets,
    settle_blackjack_round,
)
from icbot.casino.blackjack_store import BlackjackStore, _InsufficientWagerError
from icbot.config import Settings
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import game as game_embed
from icbot.games.blackjack_engine import GameState

log = logging.getLogger(__name__)

# ---- Config ----
INACTIVITY_SECONDS = 5 * 60  # 5 minutes
TERMINAL_VIEW_TIMEOUT_SECONDS = 15 * 60  # how long the "Play Again" button stays active

_sessions = SessionManager()


class _BJStartError(RuntimeError):
    """Raised when a new hand cannot be started."""


def _reserved_bets(state: GameState) -> int:
    """Return total active wagers currently escrowed for this hand."""
    return reserved_bets(state)


def _get_settings(client: Any) -> Settings:
    return cast(Settings, getattr(client, "settings", None) or Settings())


def _get_store(client: Any, settings: Settings | None = None) -> BlackjackStore:
    return get_container(client).blackjack_store(settings)


def render_state(state: GameState, hide_dealer: bool = True) -> tuple[str, str, str]:
    dealer = state.dealer.render(hide_first=hide_dealer)
    dealer_total = (
        "??"
        if hide_dealer
        else str(state.dealer.best_total()) + (" (bust)" if state.dealer.is_bust() else "")
    )
    lines = []
    for i, (h, bet) in enumerate(zip(state.hands, state.bets, strict=True)):
        prefix = "➡️" if i == state.active_index and not state.done else "  "
        tot = h.best_total()
        flags = []
        if h.doubled:
            flags.append("D")
        if getattr(h, "_stood", False):
            flags.append("S")
        if h.is_bust():
            flags.append("BUST")
        if h.is_blackjack():
            flags.append("BJ")
        f = f" [{' '.join(flags)}]" if flags else ""
        lines.append(f"{prefix} Hand {i + 1} (${bet}): {h.render()}  → **{tot}**{f}")
    return dealer, dealer_total, "\n".join(lines)


def _build_live_embed(
    interaction: discord.Interaction,
    state: GameState,
    title: str,
    summary: str,
    *,
    settings: Settings | None = None,
) -> discord.Embed:
    eff_settings = settings or _get_settings(interaction.client)
    dealer, dealer_total, hands = render_state(state, hide_dealer=True)
    return game_embed(
        eff_settings,
        title=title,
        summary=summary,
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=f"Blackjack • {interaction.user.display_name}",
    )


def _build_done_embed(
    interaction: discord.Interaction,
    state: GameState,
    title: str,
    summary: str,
    *,
    settings: Settings | None = None,
) -> discord.Embed:
    eff_settings = settings or _get_settings(interaction.client)
    dealer, dealer_total, hands = render_state(state, hide_dealer=False)
    return game_embed(
        eff_settings,
        title=title,
        summary=summary,
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=f"Blackjack • {interaction.user.display_name}",
    )


# ==========
# Terminal View (Play Again)
# ==========


class BlackjackTerminalView(discord.ui.View):
    """Shown after a hand ends (settled, forfeited, or timed out)."""

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        last_bet: int,
        timeout_seconds: int | None = TERMINAL_VIEW_TIMEOUT_SECONDS,
        session_mgr: SessionManager | None = None,
        clock: Callable[[], float] | None = None,
    ):
        super().__init__(timeout=timeout_seconds)
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.last_bet = int(last_bet)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._session_mgr = session_mgr if session_mgr is not None else _sessions
        self._clock: Callable[[], float] = clock if clock is not None else self._session_mgr._clock

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your hand.", ephemeral=True)
            return False
        return True

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException as exc:
                log.debug("Failed to disable expired blackjack terminal view: %s", exc)

    @discord.ui.button(label="🔁 Play Again", style=discord.ButtonStyle.primary)
    async def play_again_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Start a new hand with `/ic casino blackjack`.",
                    ephemeral=True,
                )
                return

            if not interaction.guild:
                await interaction.response.send_message(
                    "Blackjack only works in a server (guild).", ephemeral=True
                )
                return

            # Defer because we're going to edit the message
            await interaction.response.defer()

            bet = int(self.last_bet)
            if bet <= 0:
                bet = 10

            try:
                embed, view, _is_live = await _begin_hand(interaction, bet=bet)
            except _BJStartError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

            # Replace this terminal view with the next view
            self.stop()

            target_msg = interaction.message or self.message
            if target_msg is not None:
                msg = await target_msg.edit(embed=embed, view=view)
                if isinstance(view, (BlackjackView, BlackjackTerminalView)):
                    view.message = msg

            # If the next view is another terminal state, keep its last_bet updated
            if isinstance(view, BlackjackTerminalView):
                view.last_bet = bet


# ==========
# Interactive View (live hand)
# ==========


class BlackjackView(discord.ui.View):
    """Interactive in-hand buttons."""

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        timeout_seconds: int | None = INACTIVITY_SECONDS,
        session_mgr: SessionManager | None = None,
        clock: Callable[[], float] | None = None,
    ):
        super().__init__(timeout=timeout_seconds)
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._session_mgr = session_mgr if session_mgr is not None else _sessions
        self._clock: Callable[[], float] = clock if clock is not None else self._session_mgr._clock

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your hand.", ephemeral=True)
            return False
        return True

    def _get_session(self) -> BJSession | None:
        return self._session_mgr.get(self.guild_id, self.user_id)

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _sync_button_states(self, state: GameState) -> None:
        self.hit_btn.disabled = state.done or not state.can_hit()
        self.stand_btn.disabled = state.done or not state.can_stand()
        self.double_btn.disabled = state.done or not state.can_double()
        self.split_btn.disabled = state.done or not state.can_split()
        self.forfeit_btn.disabled = state.done

    def _renew(self, interaction: discord.Interaction, state: GameState) -> BlackjackView:
        """Create a new view instance to reset the timeout countdown."""
        new_view = BlackjackView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            timeout_seconds=INACTIVITY_SECONDS,
            session_mgr=self._session_mgr,
            clock=self._clock,
        )
        new_view.message = interaction.message
        new_view._sync_button_states(state)
        self.stop()
        return new_view

    async def _settle_and_finish(
        self,
        interaction: discord.Interaction,
        sess: BJSession,
        store: BlackjackStore,
        *,
        settings: Settings | None = None,
    ) -> tuple[discord.Embed, BlackjackTerminalView]:
        settlement = await settle_blackjack_round(sess.round, store, clock=self._clock)
        sess.settled = True
        self._session_mgr.clear(self.guild_id, self.user_id)

        eff_settings = settings or await resolve_guild_settings(interaction.client, interaction)
        embed = _build_done_embed(
            interaction,
            sess.state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{settlement.net_delta:+d}** | Bankroll: **${settlement.new_bankroll}**",
            settings=eff_settings,
        )

        terminal_view = BlackjackTerminalView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            last_bet=sess.base_bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
            session_mgr=self._session_mgr,
            clock=self._clock,
        )
        terminal_view.message = interaction.message
        return embed, terminal_view

    async def _forfeit_now(
        self,
        *,
        bot: discord.Client,
        reason: str = "Inactivity timeout",
        is_timeout: bool = False,
    ) -> None:
        """Forfeit a live hand and swap the UI into a terminal (Play Again) state."""
        sess = self._get_session()
        if not sess or sess.state.done or sess.settled:
            return

        if not self.message:
            return

        settings = await resolve_guild_settings(bot, self.guild_id)
        container = get_container(bot)
        store = container.blackjack_store(settings)

        settlement = await settle_blackjack_round(
            sess.round,
            store,
            is_forfeit=not is_timeout,
            is_timeout=is_timeout,
            clock=self._clock,
        )
        sess.settled = True
        self._session_mgr.clear(self.guild_id, self.user_id)

        embed = game_embed(
            settings,
            title="🛑 Blackjack — Forfeit",
            summary=f"Reason: *{reason}* | Penalty: **-{settlement.reserved_bets}** | Bankroll: **${settlement.new_bankroll}**",
            fields=(
                (
                    "Dealer",
                    f"{sess.state.dealer.render(hide_first=False)}  → **{sess.state.dealer.best_total()}**",
                ),
                (
                    "Player",
                    "\n".join(
                        [
                            f"Hand {i + 1} (${b}): {h.render()} → **{h.best_total()}**"
                            for i, (h, b) in enumerate(zip(sess.state.hands, sess.state.bets, strict=True))
                        ]
                    ),
                ),
            ),
            footer_hint=settings.embed_footer_text,
        )

        terminal_view = BlackjackTerminalView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            last_bet=sess.base_bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
            session_mgr=self._session_mgr,
            clock=self._clock,
        )

        self.stop()
        msg = await self.message.edit(embed=embed, view=terminal_view)
        terminal_view.message = msg

    async def on_timeout(self) -> None:
        if not self.message or self.is_finished():
            return

        async with self._lock:
            if not self.message or self.is_finished():
                return

            try:
                bot = cast(Any, self.message)._state._get_client()
            except (AttributeError, RuntimeError):
                bot = None

            if bot is None:
                self._disable_all()
                self.stop()
                try:
                    await self.message.edit(view=self)
                except discord.DiscordException as exc:
                    log.debug("Failed to disable orphaned blackjack view: %s", exc)
                return

            await self._forfeit_now(bot=bot, reason="No action for 5 minutes", is_timeout=True)

    # ---- Buttons ----

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done or sess.settled:
                await interaction.response.send_message(
                    "No active hand. Start one with `/ic blackjack`.", ephemeral=True
                )
                return
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Use the buttons on the latest message.", ephemeral=True
                )
                return

            await interaction.response.defer()
            sess.last_ts = self._clock()
            sess.state.hit()
            sess.round.last_ts = sess.last_ts

            settings = await resolve_guild_settings(interaction.client, interaction)
            store = get_container(interaction.client).blackjack_store(settings)

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess, store, settings=settings)
                self.stop()
                target_msg = interaction.message or self.message
                if target_msg is not None:
                    msg = await target_msg.edit(embed=embed, view=term_view)
                    term_view.message = msg
                return

            embed = _build_live_embed(
                interaction,
                sess.state,
                title="🃏 Blackjack — Hit",
                summary="Card drawn.",
                settings=settings,
            )
            new_view = self._renew(interaction, sess.state)
            target_msg = interaction.message or self.message
            if target_msg is not None:
                msg = await target_msg.edit(embed=embed, view=new_view)
                new_view.message = msg

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done or sess.settled:
                await interaction.response.send_message(
                    "No active hand. Start one with `/ic blackjack`.", ephemeral=True
                )
                return
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Use the buttons on the latest message.", ephemeral=True
                )
                return

            await interaction.response.defer()
            sess.last_ts = self._clock()
            sess.state.stand()
            sess.round.last_ts = sess.last_ts

            settings = await resolve_guild_settings(interaction.client, interaction)
            store = get_container(interaction.client).blackjack_store(settings)

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess, store, settings=settings)
                self.stop()
                target_msg = interaction.message or self.message
                if target_msg is not None:
                    msg = await target_msg.edit(embed=embed, view=term_view)
                    term_view.message = msg
                return

            embed = _build_live_embed(
                interaction,
                sess.state,
                title="🃏 Blackjack — Stand",
                summary="Standing. Next hand (if split).",
                settings=settings,
            )
            new_view = self._renew(interaction, sess.state)
            target_msg = interaction.message or self.message
            if target_msg is not None:
                msg = await target_msg.edit(embed=embed, view=new_view)
                new_view.message = msg

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done or sess.settled:
                await interaction.response.send_message(
                    "No active hand. Start one with `/ic blackjack`.", ephemeral=True
                )
                return
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Use the buttons on the latest message.", ephemeral=True
                )
                return

            if not sess.state.can_double():
                await interaction.response.send_message("❌ You can't double right now.", ephemeral=True)
                return

            settings = await resolve_guild_settings(interaction.client, interaction)
            store = get_container(interaction.client).blackjack_store(settings)
            current_bet = int(sess.state.bets[sess.state.active_index])
            try:
                await store.reserve_wager(interaction.user.id, current_bet)
            except _InsufficientWagerError as exc:
                await interaction.response.send_message(
                    f"❌ Not enough bankroll available to double. Need **${current_bet}**, available **${exc.available}**.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            sess.last_ts = self._clock()
            sess.state.double()
            sess.round.last_ts = sess.last_ts

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess, store, settings=settings)
                self.stop()
                target_msg = interaction.message or self.message
                if target_msg is not None:
                    msg = await target_msg.edit(embed=embed, view=term_view)
                    term_view.message = msg
                return

            embed = _build_live_embed(
                interaction,
                sess.state,
                title="🃏 Blackjack — Double",
                summary="Double down executed.",
                settings=settings,
            )
            new_view = self._renew(interaction, sess.state)
            target_msg = interaction.message or self.message
            if target_msg is not None:
                msg = await target_msg.edit(embed=embed, view=new_view)
                new_view.message = msg

    @discord.ui.button(label="Split", style=discord.ButtonStyle.success)
    async def split_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done or sess.settled:
                await interaction.response.send_message(
                    "No active hand. Start one with `/ic blackjack`.", ephemeral=True
                )
                return
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Use the buttons on the latest message.", ephemeral=True
                )
                return

            if not sess.state.can_split():
                await interaction.response.send_message("❌ You can't split right now.", ephemeral=True)
                return

            settings = await resolve_guild_settings(interaction.client, interaction)
            store = get_container(interaction.client).blackjack_store(settings)
            current_bet = int(sess.state.bets[sess.state.active_index])
            try:
                await store.reserve_wager(interaction.user.id, current_bet)
            except _InsufficientWagerError as exc:
                await interaction.response.send_message(
                    f"❌ Not enough bankroll available to split. Need **${current_bet}**, available **${exc.available}**.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            sess.last_ts = self._clock()

            ok = sess.state.split()
            sess.round.last_ts = sess.last_ts
            if not ok:
                await store.refund_wager(interaction.user.id, current_bet)
                await interaction.followup.send(
                    "❌ Split failed (pair required / split limit reached).", ephemeral=True
                )
                return

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess, store, settings=settings)
                self.stop()
                target_msg = interaction.message or self.message
                if target_msg is not None:
                    msg = await target_msg.edit(embed=embed, view=term_view)
                    term_view.message = msg
                return

            embed = _build_live_embed(
                interaction,
                sess.state,
                title="🃏 Blackjack — Split",
                summary="Hand split into two.",
                settings=settings,
            )
            new_view = self._renew(interaction, sess.state)
            target_msg = interaction.message or self.message
            if target_msg is not None:
                msg = await target_msg.edit(embed=embed, view=new_view)
                new_view.message = msg

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.danger)
    async def forfeit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done or sess.settled:
                await interaction.response.send_message(
                    "No active hand. Start one with `/ic blackjack`.", ephemeral=True
                )
                return
            if self.is_finished():
                await interaction.response.send_message(
                    "⏳ That menu has expired. Use the buttons on the latest message.", ephemeral=True
                )
                return

            await interaction.response.defer()
            self.message = interaction.message or self.message
            await self._forfeit_now(bot=interaction.client, reason="Player forfeited")


# ==========
# Hand Orchestration
# ==========


async def _begin_hand(
    interaction: discord.Interaction, *, bet: int
) -> tuple[discord.Embed, BlackjackView | BlackjackTerminalView, bool]:
    """Common start logic used by the slash command and the Play Again button.

    Returns: (embed, view, is_live)
    - is_live=True: view is a BlackjackView and expects message binding for timeout forfeits
    - is_live=False: view is a BlackjackTerminalView (immediate settle)
    """
    if not interaction.guild:
        raise _BJStartError("Blackjack only works in a server (guild).")

    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    sessions = container.bj_sessions
    ttl_seconds = max(INACTIVITY_SECONDS, int(getattr(settings, "bj_session_ttl_minutes", 5) or 5) * 60)

    # Cleanup stale sessions
    sessions.cleanup(ttl_seconds)

    existing = sessions.get(interaction.guild.id, interaction.user.id)
    if existing and not existing.state.done:
        raise _BJStartError("You already have an active blackjack hand.")

    bet = int(bet)
    if bet <= 0:
        raise _BJStartError("Bet must be >= 1.")

    store = container.blackjack_store(settings)

    # Perform crash recovery check for any dangling reservations for this user
    await sessions.recover_interrupted_sessions(
        store,
        guild_id=interaction.guild.id,
        reason="Interrupted by bot restart (refunded before new hand)",
    )

    now_ts = container.clock()
    round_obj = BlackjackRound.create(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        base_bet=bet,
        clock=container.clock,
    )
    bet_tx_id = f"bj_bet:{round_obj.round_id}"

    try:
        await store.reserve_wager(interaction.user.id, bet, idempotency_key=bet_tx_id)
    except _InsufficientWagerError as exc:
        raise _BJStartError(f"❌ You only have ${exc.available}. Lower your bet.") from exc

    round_obj.transition_to(
        BlackjackFSMState.BET_RESERVED,
        action="reserve_wager",
        clock=container.clock,
        amount=bet,
    )

    existing_after = sessions.get(interaction.guild.id, interaction.user.id)
    if existing_after and not existing_after.state.done:
        await store.refund_wager(interaction.user.id, bet, idempotency_key=f"bj_refund:{round_obj.round_id}")
        raise _BJStartError("You already have an active blackjack hand.")

    try:
        state = GameState.start(
            bet=bet,
            dealer_hits_soft_17=settings.bj_dealer_hits_soft_17,
            max_splits=settings.bj_max_splits,
        )
    except Exception:
        await store.refund_wager(interaction.user.id, bet, idempotency_key=f"bj_refund:{round_obj.round_id}")
        raise

    round_obj.state = state
    round_obj.transition_to(
        BlackjackFSMState.DEALT,
        action="deal_cards",
        clock=container.clock,
    )

    sess = BJSession(
        state=state,
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
        base_bet=bet,
        created_ts=now_ts,
        last_ts=now_ts,
        round=round_obj,
    )
    sessions.set(sess)

    # Immediate settlement if blackjack occurs at deal
    if state.hands[0].is_blackjack() or state.dealer.is_blackjack():
        state.done = True
        settlement = await settle_blackjack_round(sess.round, store, clock=container.clock)
        sess.settled = True
        sessions.clear(interaction.guild.id, interaction.user.id)

        embed = _build_done_embed(
            interaction,
            state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{settlement.net_delta:+d}** | Bankroll: **${settlement.new_bankroll}**",
            settings=settings,
        )
        term_view = BlackjackTerminalView(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            last_bet=bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
            session_mgr=sessions,
            clock=container.clock,
        )
        return embed, term_view, False

    # Live game embed + buttons
    sess.round.transition_to(
        BlackjackFSMState.PLAYER_TURN,
        action="begin_turn",
        clock=container.clock,
    )
    embed = _build_live_embed(
        interaction,
        state,
        title="🃏 Blackjack — Started",
        summary="Your hand is live.",
        settings=settings,
    )
    live_view = BlackjackView(
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        timeout_seconds=INACTIVITY_SECONDS,
        session_mgr=sessions,
        clock=container.clock,
    )
    live_view._sync_button_states(state)
    return embed, live_view, True
