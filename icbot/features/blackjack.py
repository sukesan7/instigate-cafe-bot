from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import aiosqlite
import discord
from discord import app_commands

from icbot.app import casino
from icbot.config import Settings
from icbot.container import get_container, resolve_guild_settings
from icbot.db import Database
from icbot.embeds import game as game_embed
from icbot.games.blackjack_engine import GameState
from icbot.services.user_service import InsufficientBalanceError

log = logging.getLogger(__name__)

# ---- Config ----
INACTIVITY_SECONDS = 5 * 60  # 5 minutes
TERMINAL_VIEW_TIMEOUT_SECONDS = 15 * 60  # how long the "Play Again" button stays active


class _InsufficientWagerError(InsufficientBalanceError):
    def __init__(self, available: int) -> None:
        self.available = int(available)
        super().__init__(f"Insufficient balance (available: {available}).")


# ==========
# Persistence
# ==========


@dataclass
class BJSession:
    state: GameState
    user_id: int
    guild_id: int
    base_bet: int
    created_ts: float
    last_ts: float
    settled: bool = False


class BlackjackStore:
    def __init__(
        self,
        db: Database,
        starting_bankroll: int,
        *,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.starting_bankroll = int(starting_bankroll)
        self._clock: Callable[[], float] = clock if clock is not None else time.time

    async def _ensure_user_conn(self, conn: aiosqlite.Connection, user_id: int) -> None:
        uid = int(user_id)
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
            (uid, self.starting_bankroll, int(self._clock())),
        )
        cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        bal_row = await cur.fetchone()
        bal = int(bal_row[0]) if bal_row is not None else self.starting_bankroll
        await conn.execute(
            "INSERT OR IGNORE INTO blackjack_users (user_id, bankroll) VALUES (?, ?)",
            (uid, bal),
        )

    async def ensure_user(self, user_id: int) -> None:
        """Ensure both the central `users` row and `blackjack_users` stats row exist."""
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, user_id)
            await conn.commit()

    async def get_bankroll(self, user_id: int) -> int:
        """Read balance from the central `users` table."""
        uid = int(user_id)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            await conn.commit()
        return int(row[0]) if row is not None else self.starting_bankroll

    async def get_reset_count(self, user_id: int) -> int:
        uid = int(user_id)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute(
                "SELECT reset_count FROM blackjack_users WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
            await conn.commit()
        return int(row[0]) if row is not None and row[0] is not None else 0

    async def reserve_wager(self, user_id: int, amount: int) -> int:
        """Atomically deduct an active wager up front into escrow."""
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Bet must be >= 1.")
        uid = int(user_id)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            available = int(row[0]) if row is not None else self.starting_bankroll
            if available < amount:
                await conn.commit()
                raise _InsufficientWagerError(available)
            new_balance = available - amount
            await conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, uid))
            await conn.execute("UPDATE blackjack_users SET bankroll=? WHERE user_id=?", (new_balance, uid))
            await conn.commit()
        return new_balance

    async def refund_wager(self, user_id: int, amount: int) -> int:
        """Return an escrowed wager if a hand cannot start or split."""
        amount = int(amount)
        if amount <= 0:
            return await self.get_bankroll(user_id)
        uid = int(user_id)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            cur_balance = int(row[0]) if row is not None else self.starting_bankroll
            new_balance = cur_balance + amount
            await conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, uid))
            await conn.execute("UPDATE blackjack_users SET bankroll=? WHERE user_id=?", (new_balance, uid))
            await conn.commit()
        return new_balance

    async def apply_delta_and_stats(
        self,
        user_id: int,
        delta: int,
        result_counts: dict[str, int],
        hands_played: int,
    ) -> int:
        uid = int(user_id)
        delta = int(delta)
        hands_played = int(hands_played)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            bal_row = await cur.fetchone()
            bankroll = (int(bal_row[0]) if bal_row is not None else self.starting_bankroll) + delta
            await conn.execute("UPDATE users SET balance=? WHERE user_id=?", (bankroll, uid))

            cur2 = await conn.execute(
                "SELECT wins, losses, pushes, hands_played FROM blackjack_users WHERE user_id=?",
                (uid,),
            )
            row = await cur2.fetchone()
            wins, losses, pushes, played = map(int, row) if row is not None else (0, 0, 0, 0)
            wins += result_counts.get("win", 0) + result_counts.get("blackjack", 0)
            losses += result_counts.get("loss", 0) + result_counts.get("bust", 0)
            pushes += result_counts.get("push", 0)
            played += hands_played

            await conn.execute(
                "UPDATE blackjack_users SET bankroll=?, wins=?, losses=?, pushes=?, hands_played=? WHERE user_id=?",
                (bankroll, wins, losses, pushes, played, uid),
            )
            await conn.commit()
        return bankroll

    async def leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        """Leaderboard by balance from the central users table."""
        limit = max(1, min(limit, 25))
        rows = await self.db.fetchall(
            "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        return [(int(uid), int(br)) for uid, br in rows]

    async def reset_user(self, user_id: int) -> int:
        """Reset bankroll/stats and increment the user's reset counter."""
        uid = int(user_id)
        async with self.db._lock, aiosqlite.connect(self.db.path) as conn:
            await self._ensure_user_conn(conn, uid)
            cur = await conn.execute(
                "SELECT reset_count FROM blackjack_users WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
            resets = (int(row[0]) if row is not None and row[0] is not None else 0) + 1

            await conn.execute(
                "UPDATE users SET balance=? WHERE user_id=?",
                (self.starting_bankroll, uid),
            )
            await conn.execute(
                "UPDATE blackjack_users "
                "SET bankroll=?, hands_played=0, wins=0, losses=0, pushes=0, reset_count=? "
                "WHERE user_id=?",
                (self.starting_bankroll, resets, uid),
            )
            await conn.commit()
        return resets


# ==========
# Sessions
# ==========


class SessionManager:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self.sessions: dict[tuple[int, int], BJSession] = {}
        self._clock: Callable[[], float] = clock if clock is not None else time.time

    def get(self, guild_id: int, user_id: int) -> BJSession | None:
        return self.sessions.get((guild_id, user_id))

    def set(self, s: BJSession) -> None:
        self.sessions[(s.guild_id, s.user_id)] = s

    def clear(self, guild_id: int, user_id: int) -> None:
        self.sessions.pop((guild_id, user_id), None)

    def cleanup(self, ttl_seconds: int) -> None:
        now = self._clock()
        dead = [k for k, s in self.sessions.items() if (now - s.last_ts) > ttl_seconds]
        for k in dead:
            self.sessions.pop(k, None)


_sessions = SessionManager()


def _reserved_bets(state: GameState) -> int:
    """Return total active wagers currently escrowed for this hand."""
    return int(sum(state.bets))


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
    """Shown after a hand ends (settled or forfeited)."""

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
        self.guild_id = guild_id
        self.user_id = user_id
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


class _BJStartError(RuntimeError):
    pass


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

    # cleanup stale sessions
    sessions.cleanup(ttl_seconds)

    existing = sessions.get(interaction.guild.id, interaction.user.id)
    if existing and not existing.state.done:
        raise _BJStartError("You already have an active blackjack hand.")

    bet = int(bet)
    if bet <= 0:
        raise _BJStartError("Bet must be >= 1.")

    store = container.blackjack_store(settings)

    try:
        await store.reserve_wager(interaction.user.id, bet)
    except _InsufficientWagerError as exc:
        raise _BJStartError(f"❌ You only have ${exc.available}. Lower your bet.") from exc

    existing_after = sessions.get(interaction.guild.id, interaction.user.id)
    if existing_after and not existing_after.state.done:
        await store.refund_wager(interaction.user.id, bet)
        raise _BJStartError("You already have an active blackjack hand.")

    try:
        state = GameState.start(
            bet=bet,
            dealer_hits_soft_17=settings.bj_dealer_hits_soft_17,
            max_splits=settings.bj_max_splits,
        )
    except Exception:
        await store.refund_wager(interaction.user.id, bet)
        raise

    now_ts = container.clock()
    sess = BJSession(
        state=state,
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
        base_bet=bet,
        created_ts=now_ts,
        last_ts=now_ts,
    )
    sessions.set(sess)

    # Immediate settlement if blackjack occurs at deal
    if state.hands[0].is_blackjack() or state.dealer.is_blackjack():
        sess.settled = True
        state.done = True
        results = state.settle()
        delta_total = sum(d for _, d in results)
        payout_credit = _reserved_bets(state) + delta_total
        counts: dict[str, int] = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        sessions.clear(interaction.guild.id, interaction.user.id)
        new_bankroll = await store.apply_delta_and_stats(
            interaction.user.id, payout_credit, counts, hands_played=len(results)
        )

        embed = _build_done_embed(
            interaction,
            state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
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
        self.guild_id = guild_id
        self.user_id = user_id
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
        # Default: enabled unless rules say otherwise
        self.hit_btn.disabled = state.done
        self.stand_btn.disabled = state.done
        self.double_btn.disabled = state.done or (not state.can_double())
        self.split_btn.disabled = state.done or (not state.can_split())
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
        # Stop old view so its timeout doesn't fire later.
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
        sess.settled = True
        state = sess.state
        results = state.settle()
        delta_total = sum(d for _, d in results)
        payout_credit = _reserved_bets(state) + delta_total

        counts: dict[str, int] = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1

        self._session_mgr.clear(self.guild_id, self.user_id)
        new_bankroll = await store.apply_delta_and_stats(
            interaction.user.id,
            payout_credit,
            counts,
            hands_played=len(results),
        )

        eff_settings = settings or await resolve_guild_settings(interaction.client, interaction)
        embed = _build_done_embed(
            interaction,
            state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
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

    async def _forfeit_now(self, *, bot: discord.Client, reason: str = "Inactivity timeout") -> None:
        """Forfeit a live hand and swap the UI into a terminal (Play Again) state."""
        sess = self._get_session()
        if not sess or sess.state.done or sess.settled:
            return

        if not self.message:
            return

        sess.settled = True
        state = sess.state
        state.done = True
        base_bet = int(sess.base_bet)
        self._session_mgr.clear(self.guild_id, self.user_id)

        settings = await resolve_guild_settings(bot, self.guild_id)
        container = get_container(bot)
        store = container.blackjack_store(settings)

        reserved = _reserved_bets(state)
        hands_played = max(1, len(state.bets))
        result_counts = {"loss": hands_played}
        new_bankroll = await store.apply_delta_and_stats(
            self.user_id, 0, result_counts, hands_played=hands_played
        )

        embed = game_embed(
            settings,
            title="🛑 Blackjack — Forfeit",
            summary=f"Reason: *{reason}* | Penalty: **-{reserved}** | Bankroll: **${new_bankroll}**",
            fields=(
                (
                    "Dealer",
                    f"{state.dealer.render(hide_first=False)}  → **{state.dealer.best_total()}**",
                ),
                (
                    "Player",
                    "\n".join(
                        [
                            f"Hand {i + 1} (${b}): {h.render()} → **{h.best_total()}**"
                            for i, (h, b) in enumerate(zip(state.hands, state.bets, strict=True))
                        ]
                    ),
                ),
            ),
            footer_hint=settings.embed_footer_text,
        )

        terminal_view = BlackjackTerminalView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            last_bet=base_bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
            session_mgr=self._session_mgr,
            clock=self._clock,
        )

        # Stop old view so it can't timeout-edit later
        self.stop()

        msg = await self.message.edit(embed=embed, view=terminal_view)
        terminal_view.message = msg

    async def on_timeout(self) -> None:
        # Auto-forfeit on inactivity
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
                # Can't access bot client -> just disable UI to prevent dangling buttons
                self._disable_all()
                self.stop()
                try:
                    await self.message.edit(view=self)
                except discord.DiscordException as exc:
                    log.debug("Failed to disable orphaned blackjack view: %s", exc)
                return

            await self._forfeit_now(bot=bot, reason="No action for 5 minutes")

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
# Commands
# ==========


@casino.command(name="blackjack", description="Start an interactive blackjack hand (buttons).")
@app_commands.describe(bet="Bet amount (integer). Default 10.")
async def blackjack(interaction: discord.Interaction, bet: int = 10):
    try:
        embed, view, _is_live = await _begin_hand(interaction, bet=int(bet))
    except _BJStartError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    await interaction.response.send_message(embed=embed, view=view)

    # Bind message for timeout-forfeit edits
    msg = await interaction.original_response()
    if isinstance(view, (BlackjackView, BlackjackTerminalView)):
        view.message = msg
