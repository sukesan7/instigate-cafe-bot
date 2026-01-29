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

# ---- Config (hard requirement from you) ----
INACTIVITY_SECONDS = 5 * 60  # 5 minutes


# ==========
# Persistence
# ==========

@dataclass
class BJSession:
    state: GameState
    user_id: int
    guild_id: int
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

    async def reset_user(self, user_id: int) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO blackjack_users (user_id, bankroll, hands_played, wins, losses, pushes) "
            "VALUES (?, ?, 0, 0, 0, 0)",
            (user_id, self.starting_bankroll),
        )


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
# Interactive View
# ==========

class BlackjackView(discord.ui.View):
    """
    Button UI:
      - Only the owner can press buttons
      - Inactivity timeout (5 minutes) => auto-forfeit
      - Each click "renews" the view to reset the 5-min timer
    """

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
        """
        Creates a new view instance to reset the timeout countdown
        (discord.py View timeout does NOT auto-reset on interaction).
        """
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
    ) -> tuple[discord.Embed, "BlackjackView"]:
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
        done_view = BlackjackView(guild_id=self.guild_id, user_id=self.user_id, timeout_seconds=None)
        done_view.message = interaction.message
        done_view._disable_all()
        done_view.stop()
        return embed, done_view

    async def _forfeit_now(self, *, bot: discord.Client, reason: str = "Inactivity timeout") -> None:
        """
        Forfeit:
          - Lose all reserved bets (sum of current bets)
          - Mark as losses in stats
          - Disable buttons and edit message
        """
        sess = self._get_session()
        if not sess or sess.state.done:
            return

        # Need a message to edit
        if not self.message:
            return

        settings = bot.settings  # type: ignore[attr-defined]
        store = BlackjackStore(bot.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

        state = sess.state
        reserved = _reserved_bets(state)
        hands_played = max(1, len(state.bets))
        result_counts = {"loss": hands_played}
        new_bankroll = await store.apply_delta_and_stats(self.user_id, -reserved, result_counts, hands_played=hands_played)

        _sessions.clear(self.guild_id, self.user_id)

        # Show dealer revealed for closure (even though the hand didn't finish normally)
        fake_interaction = type("X", (), {})()
        fake_interaction.client = bot
        fake_interaction.user = type("U", (), {"display_name": "Forfeit"})()

        embed = game_embed(
            settings,
            title="🛑 Blackjack — Forfeit",
            summary=f"Reason: *{reason}* | Penalty: **-{reserved}** | Bankroll: **${new_bankroll}**",
            fields=(
                ("Dealer", f"{state.dealer.render(hide_first=False)}  → **{state.dealer.best_total()}**"),
                ("Player", "\n".join([f"Hand {i+1} (${b}): {h.render()} → **{h.best_total()}**" for i, (h, b) in enumerate(zip(state.hands, state.bets))])),
            ),
            footer_hint=settings.embed_footer_text,
        )

        self._disable_all()
        self.stop()
        await self.message.edit(embed=embed, view=self)

    async def on_timeout(self) -> None:
        # Auto-forfeit on inactivity
        try:
            if self.message:
                bot = self.message.guild.me._state._get_client()  # internal; fallback below
                # If above ever breaks across versions, we rely on bot injected via message.edit path not available here.
        except Exception:
            bot = None

        # Best effort: use client via message._state (discord.py stable)
        if self.message and bot is None:
            try:
                bot = self.message._state._get_client()  # type: ignore[attr-defined]
            except Exception:
                bot = None

        if bot is None:
            # Can't access bot client -> just disable UI to prevent dangling buttons
            self._disable_all()
            if self.message:
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
                embed, view = await self._settle_and_finish(interaction, sess.state, store)
                await interaction.message.edit(embed=embed, view=view)
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Hit", summary="Card drawn.")
            new_view = self._renew(interaction, sess.state)
            await interaction.message.edit(embed=embed, view=new_view)

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
                embed, view = await self._settle_and_finish(interaction, sess.state, store)
                await interaction.message.edit(embed=embed, view=view)
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Stand", summary="Standing. Next hand (if split).")
            new_view = self._renew(interaction, sess.state)
            await interaction.message.edit(embed=embed, view=new_view)

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
                embed, view = await self._settle_and_finish(interaction, sess.state, store)
                await interaction.message.edit(embed=embed, view=view)
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Double", summary="Double down executed.")
            new_view = self._renew(interaction, sess.state)
            await interaction.message.edit(embed=embed, view=new_view)

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
                await interaction.response.send_message("❌ Split failed (pair required / split limit reached).", ephemeral=True)
                return

            embed = _build_live_embed(interaction, sess.state, title="🃏 Blackjack — Split", summary="Hand split into two.")
            new_view = self._renew(interaction, sess.state)
            await interaction.message.edit(embed=embed, view=new_view)

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
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server (guild).", ephemeral=True)
        return

    # cleanup stale sessions
    _sessions.cleanup(INACTIVITY_SECONDS)

    existing = _sessions.get(interaction.guild.id, interaction.user.id)
    if existing and not existing.state.done:
        await interaction.response.send_message("You already have an active blackjack hand.", ephemeral=True)
        return

    bet = int(bet)
    if bet <= 0:
        await interaction.response.send_message("Bet must be >= 1.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    bankroll = await store.get_bankroll(interaction.user.id)

    if bet > bankroll:
        await interaction.response.send_message(f"❌ You only have ${bankroll}. Lower your bet.", ephemeral=True)
        return

    state = GameState.start(
        bet=bet,
        dealer_hits_soft_17=settings.bj_dealer_hits_soft_17,
        max_splits=settings.bj_max_splits,
    )

    sess = BJSession(
        state=state,
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
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
        await interaction.response.send_message(embed=embed)
        return

    # Live game embed + buttons
    embed = _build_live_embed(interaction, state, title="🃏 Blackjack — Started", summary="Your hand is live.")
    view = BlackjackView(guild_id=interaction.guild.id, user_id=interaction.user.id, timeout_seconds=INACTIVITY_SECONDS)
    view._sync_button_states(state)

    await interaction.response.send_message(embed=embed, view=view)

    # Bind message for timeout-forfeit edits
    msg = await interaction.original_response()
    view.message = msg


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
    embed = system_embed(settings, title="💰 Blackjack Bankroll", summary=f"**${br}**", footer_hint=interaction.user.display_name)
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
    embed = system_embed(settings, title="🏆 Blackjack Leaderboard", summary="Top bankrolls", fields=(("Leaderboard", "\n".join(lines)),), footer_hint="Blackjack")
    await interaction.response.send_message(embed=embed)


@bj.command(name="reset", description="Reset a user's blackjack bankroll (admin/allowed roles only).")
@app_commands.describe(user="User to reset")
async def reset(interaction: discord.Interaction, user: discord.User):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission to reset bankrolls.", ephemeral=True)
        return

    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    await store.reset_user(user.id)
    await interaction.response.send_message(f"✅ Reset bankroll for {user.mention} to **${settings.bj_starting_bankroll}**.")
