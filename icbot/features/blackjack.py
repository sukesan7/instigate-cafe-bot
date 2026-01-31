from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands

from icbot.app import ic
from icbot.embeds import game as game_embed, system as system_embed
from icbot.games.blackjack_engine import GameState
from icbot.permissions import is_admin_or_allowed

# ---- Config ----
INACTIVITY_SECONDS = 5 * 60  # 5 minutes
TERMINAL_VIEW_TIMEOUT_SECONDS = 15 * 60  # how long the "Play Again" button stays active


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


class BlackjackStore:
    def __init__(self, db, starting_bankroll: int):
        self.db = db
        self.starting_bankroll = starting_bankroll

    async def ensure_user(self, user_id: int) -> None:
        row = await self.db.fetchone(
            "SELECT user_id FROM blackjack_users WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO blackjack_users (user_id, bankroll) VALUES (?, ?)",
                (user_id, self.starting_bankroll),
            )

    async def get_bankroll(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        row = await self.db.fetchone(
            "SELECT bankroll FROM blackjack_users WHERE user_id=?",
            (user_id,),
        )
        return int(row[0]) if row else self.starting_bankroll

    async def get_reset_count(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        row = await self.db.fetchone(
            "SELECT reset_count FROM blackjack_users WHERE user_id=?",
            (user_id,),
        )
        return int(row[0]) if row and row[0] is not None else 0

    async def apply_delta_and_stats(
        self,
        user_id: int,
        delta: int,
        result_counts: dict[str, int],
        hands_played: int,
    ) -> int:
        await self.ensure_user(user_id)
        row = await self.db.fetchone(
            "SELECT bankroll, wins, losses, pushes, hands_played FROM blackjack_users WHERE user_id=?",
            (user_id,),
        )
        bankroll, wins, losses, pushes, played = map(int, row)

        bankroll = bankroll + delta
        wins += result_counts.get("win", 0) + result_counts.get("blackjack", 0)
        losses += result_counts.get("loss", 0) + result_counts.get("bust", 0)
        pushes += result_counts.get("push", 0)
        played += hands_played

        await self.db.execute(
            "UPDATE blackjack_users SET bankroll=?, wins=?, losses=?, pushes=?, hands_played=? WHERE user_id=?",
            (bankroll, wins, losses, pushes, played, user_id),
        )
        return bankroll

    async def leaderboard(self, limit: int = 10) -> list[tuple[int, int]]:
        limit = max(1, min(limit, 25))
        rows = await self.db.fetchall(
            "SELECT user_id, bankroll FROM blackjack_users ORDER BY bankroll DESC LIMIT ?",
            (limit,),
        )
        return [(int(uid), int(br)) for uid, br in rows]

    async def reset_user(self, user_id: int) -> int:
        """Reset bankroll/stats and increment the user's reset counter."""
        await self.ensure_user(user_id)

        row = await self.db.fetchone(
            "SELECT reset_count FROM blackjack_users WHERE user_id=?",
            (user_id,),
        )
        resets = int(row[0]) if row and row[0] is not None else 0
        resets += 1

        await self.db.execute(
            "UPDATE blackjack_users "
            "SET bankroll=?, hands_played=0, wins=0, losses=0, pushes=0, reset_count=? "
            "WHERE user_id=?",
            (self.starting_bankroll, resets, user_id),
        )
        return resets


# ==========
# Sessions
# ==========

class SessionManager:
    def __init__(self) -> None:
        self.sessions: Dict[Tuple[int, int], BJSession] = {}

    def get(self, guild_id: int, user_id: int) -> Optional[BJSession]:
        return self.sessions.get((guild_id, user_id))

    def set(self, s: BJSession) -> None:
        self.sessions[(s.guild_id, s.user_id)] = s

    def clear(self, guild_id: int, user_id: int) -> None:
        self.sessions.pop((guild_id, user_id), None)

    def cleanup(self, ttl_seconds: int) -> None:
        now = time.time()
        dead = [k for k, s in self.sessions.items() if (now - s.last_ts) > ttl_seconds]
        for k in dead:
            self.sessions.pop(k, None)


_sessions = SessionManager()


def _reserved_bets(state: GameState) -> int:
    # Bets are not deducted upfront in this toy economy, so we treat them as "reserved"
    # to correctly gate double/split and to penalize forfeits.
    return int(sum(state.bets))


def render_state(state: GameState, hide_dealer: bool = True) -> tuple[str, str, str]:
    dealer = state.dealer.render(hide_first=hide_dealer)
    dealer_total = "??" if hide_dealer else str(state.dealer.best_total()) + (" (bust)" if state.dealer.is_bust() else "")
    lines = []
    for i, (h, bet) in enumerate(zip(state.hands, state.bets)):
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
        lines.append(f"{prefix} Hand {i+1} (${bet}): {h.render()}  → **{tot}**{f}")
    return dealer, dealer_total, "\n".join(lines)


def _build_live_embed(interaction: discord.Interaction, state: GameState, title: str, summary: str) -> discord.Embed:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    dealer, dealer_total, hands = render_state(state, hide_dealer=True)
    return game_embed(
        settings,
        title=title,
        summary=summary,
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )


def _build_done_embed(
    interaction: discord.Interaction,
    state: GameState,
    title: str,
    summary: str,
) -> discord.Embed:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    dealer, dealer_total, hands = render_state(state, hide_dealer=False)
    return game_embed(
        settings,
        title=title,
        summary=summary,
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
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
        timeout_seconds: Optional[int] = TERMINAL_VIEW_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=timeout_seconds)
        self.guild_id = guild_id
        self.user_id = user_id
        self.last_bet = int(last_bet)
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

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
            except Exception:
                pass

    @discord.ui.button(label="🔁 Play Again", style=discord.ButtonStyle.primary)
    async def play_again_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            if not interaction.guild:
                await interaction.response.send_message("Blackjack only works in a server (guild).", ephemeral=True)
                return

            # Defer because we're going to edit the message
            await interaction.response.defer()

            bet = int(self.last_bet)
            if bet <= 0:
                bet = 10

            try:
                embed, view, is_live = await _begin_hand(interaction, bet=bet)
            except _BJStartError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return

            # Replace this terminal view with the next view
            self.stop()

            msg = await interaction.message.edit(embed=embed, view=view)
            if hasattr(view, "message"):
                view.message = msg  # type: ignore[attr-defined]

            # If the next view is another terminal state, keep its last_bet updated
            if isinstance(view, BlackjackTerminalView):
                view.last_bet = bet


class _BJStartError(RuntimeError):
    pass


async def _begin_hand(interaction: discord.Interaction, *, bet: int) -> tuple[discord.Embed, discord.ui.View, bool]:
    """Common start logic used by the slash command and the Play Again button.

    Returns: (embed, view, is_live)
    - is_live=True: view is a BlackjackView and expects message binding for timeout forfeits
    - is_live=False: view is a BlackjackTerminalView (immediate settle)
    """
    if not interaction.guild:
        raise _BJStartError("Blackjack only works in a server (guild).")

    # cleanup stale sessions
    _sessions.cleanup(INACTIVITY_SECONDS)

    existing = _sessions.get(interaction.guild.id, interaction.user.id)
    if existing and not existing.state.done:
        raise _BJStartError("You already have an active blackjack hand.")

    bet = int(bet)
    if bet <= 0:
        raise _BJStartError("Bet must be >= 1.")

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    bankroll = await store.get_bankroll(interaction.user.id)

    if bet > bankroll:
        raise _BJStartError(f"❌ You only have ${bankroll}. Lower your bet.")

    state = GameState.start(
        bet=bet,
        dealer_hits_soft_17=settings.bj_dealer_hits_soft_17,
        max_splits=settings.bj_max_splits,
    )

    sess = BJSession(
        state=state,
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
        base_bet=bet,
        created_ts=time.time(),
        last_ts=time.time(),
    )
    _sessions.set(sess)

    # Immediate settlement if blackjack occurs at deal
    if state.hands[0].is_blackjack() or state.dealer.is_blackjack():
        state.done = True
        results = state.settle()
        delta_total = sum(d for _, d in results)
        counts: dict[str, int] = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        new_bankroll = await store.apply_delta_and_stats(
            interaction.user.id, delta_total, counts, hands_played=len(results)
        )
        _sessions.clear(interaction.guild.id, interaction.user.id)

        embed = _build_done_embed(
            interaction,
            state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
        )
        view = BlackjackTerminalView(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            last_bet=bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
        )
        return embed, view, False

    # Live game embed + buttons
    embed = _build_live_embed(interaction, state, title="🃏 Blackjack — Started", summary="Your hand is live.")
    view = BlackjackView(guild_id=interaction.guild.id, user_id=interaction.user.id, timeout_seconds=INACTIVITY_SECONDS)
    view._sync_button_states(state)
    return embed, view, True


# ==========
# Interactive View (live hand)
# ==========

class BlackjackView(discord.ui.View):
    """Interactive in-hand buttons."""

    def __init__(self, *, guild_id: int, user_id: int, timeout_seconds: Optional[int] = INACTIVITY_SECONDS):
        super().__init__(timeout=timeout_seconds)
        self.guild_id = guild_id
        self.user_id = user_id
        self.message: Optional[discord.Message] = None
        self._lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your hand.", ephemeral=True)
            return False
        return True

    def _get_session(self) -> Optional[BJSession]:
        return _sessions.get(self.guild_id, self.user_id)

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

    def _renew(self, interaction: discord.Interaction, state: GameState) -> "BlackjackView":
        """Create a new view instance to reset the timeout countdown."""
        new_view = BlackjackView(guild_id=self.guild_id, user_id=self.user_id, timeout_seconds=INACTIVITY_SECONDS)
        new_view.message = interaction.message
        new_view._sync_button_states(state)
        # Stop old view so its timeout doesn't fire later.
        self.stop()
        return new_view

    async def _settle_and_finish(
        self,
        interaction: discord.Interaction,
        state: GameState,
        store: BlackjackStore,
        *,
        base_bet: int,
    ) -> tuple[discord.Embed, BlackjackTerminalView]:
        results = state.settle()
        delta_total = sum(d for _, d in results)

        counts: dict[str, int] = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1

        new_bankroll = await store.apply_delta_and_stats(
            interaction.user.id,
            delta_total,
            counts,
            hands_played=len(results),
        )
        _sessions.clear(self.guild_id, self.user_id)

        embed = _build_done_embed(
            interaction,
            state,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
        )

        terminal_view = BlackjackTerminalView(
            guild_id=self.guild_id,
            user_id=self.user_id,
            last_bet=base_bet,
            timeout_seconds=TERMINAL_VIEW_TIMEOUT_SECONDS,
        )
        terminal_view.message = interaction.message
        return embed, terminal_view

    async def _forfeit_now(self, *, bot: discord.Client, reason: str = "Inactivity timeout") -> None:
        """Forfeit a live hand and swap the UI into a terminal (Play Again) state."""
        sess = self._get_session()
        if not sess or sess.state.done:
            return

        if not self.message:
            return

        settings = bot.settings  # type: ignore[attr-defined]
        store = BlackjackStore(bot.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

        state = sess.state
        reserved = _reserved_bets(state)
        hands_played = max(1, len(state.bets))
        result_counts = {"loss": hands_played}
        new_bankroll = await store.apply_delta_and_stats(self.user_id, -reserved, result_counts, hands_played=hands_played)

        base_bet = int(sess.base_bet)
        _sessions.clear(self.guild_id, self.user_id)

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
                            f"Hand {i+1} (${b}): {h.render()} → **{h.best_total()}**"
                            for i, (h, b) in enumerate(zip(state.hands, state.bets))
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
        )

        # Stop old view so it can't timeout-edit later
        self.stop()

        msg = await self.message.edit(embed=embed, view=terminal_view)
        terminal_view.message = msg

    async def on_timeout(self) -> None:
        # Auto-forfeit on inactivity
        if not self.message:
            return

        try:
            bot = self.message._state._get_client()  # type: ignore[attr-defined]
        except Exception:
            bot = None

        if bot is None:
            # Can't access bot client -> just disable UI to prevent dangling buttons
            self._disable_all()
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
            return

        await self._forfeit_now(bot=bot, reason="No action for 5 minutes")

    # ---- Buttons ----

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done:
                await interaction.response.send_message("No active hand. Start one with `/ic blackjack`.", ephemeral=True)
                return

            await interaction.response.defer()
            sess.last_ts = time.time()
            sess.state.hit()

            settings = interaction.client.settings  # type: ignore[attr-defined]
            store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess.state, store, base_bet=sess.base_bet)
                self.stop()
                msg = await interaction.message.edit(embed=embed, view=term_view)
                term_view.message = msg
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Hit", summary="Card drawn.")
            new_view = self._renew(interaction, sess.state)
            msg = await interaction.message.edit(embed=embed, view=new_view)
            new_view.message = msg

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done:
                await interaction.response.send_message("No active hand. Start one with `/ic blackjack`.", ephemeral=True)
                return

            await interaction.response.defer()
            sess.last_ts = time.time()
            sess.state.stand()

            settings = interaction.client.settings  # type: ignore[attr-defined]
            store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess.state, store, base_bet=sess.base_bet)
                self.stop()
                msg = await interaction.message.edit(embed=embed, view=term_view)
                term_view.message = msg
                return

            embed = _build_live_embed(
                interaction,
                sess.state,
                title="🃏 Blackjack — Stand",
                summary="Standing. Next hand (if split).",
            )
            new_view = self._renew(interaction, sess.state)
            msg = await interaction.message.edit(embed=embed, view=new_view)
            new_view.message = msg

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success)
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done:
                await interaction.response.send_message("No active hand. Start one with `/ic blackjack`.", ephemeral=True)
                return

            if not sess.state.can_double():
                await interaction.response.send_message("❌ You can't double right now.", ephemeral=True)
                return

            settings = interaction.client.settings  # type: ignore[attr-defined]
            store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

            bankroll = await store.get_bankroll(interaction.user.id)
            reserved = _reserved_bets(sess.state)
            available = bankroll - reserved
            current_bet = int(sess.state.bets[sess.state.active_index])
            if available < current_bet:
                await interaction.response.send_message(
                    f"❌ Not enough bankroll available to double. Need **${current_bet}**, available **${available}**.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            sess.last_ts = time.time()
            sess.state.double()

            if sess.state.done:
                embed, term_view = await self._settle_and_finish(interaction, sess.state, store, base_bet=sess.base_bet)
                self.stop()
                msg = await interaction.message.edit(embed=embed, view=term_view)
                term_view.message = msg
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Double", summary="Double down executed.")
            new_view = self._renew(interaction, sess.state)
            msg = await interaction.message.edit(embed=embed, view=new_view)
            new_view.message = msg

    @discord.ui.button(label="Split", style=discord.ButtonStyle.success)
    async def split_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done:
                await interaction.response.send_message("No active hand. Start one with `/ic blackjack`.", ephemeral=True)
                return

            if not sess.state.can_split():
                await interaction.response.send_message("❌ You can't split right now.", ephemeral=True)
                return

            settings = interaction.client.settings  # type: ignore[attr-defined]
            store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

            bankroll = await store.get_bankroll(interaction.user.id)
            reserved = _reserved_bets(sess.state)
            available = bankroll - reserved
            current_bet = int(sess.state.bets[sess.state.active_index])
            if available < current_bet:
                await interaction.response.send_message(
                    f"❌ Not enough bankroll available to split. Need **${current_bet}**, available **${available}**.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            sess.last_ts = time.time()

            ok = sess.state.split()
            if not ok:
                await interaction.followup.send("❌ Split failed (pair required / split limit reached).", ephemeral=True)
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Split", summary="Hand split into two.")
            new_view = self._renew(interaction, sess.state)
            msg = await interaction.message.edit(embed=embed, view=new_view)
            new_view.message = msg

    @discord.ui.button(label="Forfeit", style=discord.ButtonStyle.danger)
    async def forfeit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            sess = self._get_session()
            if not sess or sess.state.done:
                await interaction.response.send_message("No active hand.", ephemeral=True)
                return

            await interaction.response.defer()
            self.message = interaction.message
            await self._forfeit_now(bot=interaction.client, reason="Player forfeited")


# ==========
# Commands
# ==========

@ic.command(name="blackjack", description="Start an interactive blackjack hand (buttons).")
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
    if hasattr(view, "message"):
        view.message = msg  # type: ignore[attr-defined]


# ==========
# Optional: bankroll & admin tools (kept under /ic bj ...)
# ==========

bj = app_commands.Group(name="bj", description="Blackjack bankroll tools (balance/leaderboard/reset)")
ic.add_command(bj)


@bj.command(name="balance", description="Show your blackjack bankroll.")
async def balance(interaction: discord.Interaction):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    br = await store.get_bankroll(interaction.user.id)
    resets = await store.get_reset_count(interaction.user.id)

    embed = system_embed(
        settings,
        title="💰 Blackjack Bankroll",
        summary=f"**${br}**",
        fields=(("Reset Count", f"**{resets}**"),),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bj.command(name="leaderboard", description="Top blackjack bankrolls.")
@app_commands.describe(limit="How many entries (max 25)")
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    rows = await store.leaderboard(limit=limit)
    if not rows:
        await interaction.response.send_message("No blackjack data yet.")
        return

    lines = [f"**{i}.** <@{uid}> — **${br}**" for i, (uid, br) in enumerate(rows, start=1)]
    embed = system_embed(
        settings,
        title="🏆 Blackjack Leaderboard",
        summary="Top bankrolls",
        fields=(("Leaderboard", "\n".join(lines)),),
        footer_hint="Blackjack",
    )
    await interaction.response.send_message(embed=embed)


@bj.command(name="reset", description="Reset a user's blackjack bankroll (admin/allowed roles only).")
@app_commands.describe(user="User to reset")
async def reset(interaction: discord.Interaction, user: discord.User):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to reset bankrolls.", ephemeral=True)
        return

    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    resets = await store.reset_user(user.id)
    await interaction.response.send_message(
        f"✅ Reset bankroll for {user.mention} to **${settings.bj_starting_bankroll}**. "
        f"Reset count: **{resets}**."
    )
