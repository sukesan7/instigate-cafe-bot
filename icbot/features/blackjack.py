from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Dict, Tuple, Optional

import discord
from discord import app_commands

from icbot.app import ic
from icbot.games.blackjack_engine import GameState
from icbot.permissions import is_admin_or_allowed
from icbot.embeds import game as game_embed, system as system_embed


bj = app_commands.Group(name="bj", description="Blackjack casino (toy money)")
ic.add_command(bj)


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
        row = await self.db.fetchone("SELECT user_id FROM blackjack_users WHERE user_id=?", (user_id,))
        if row is None:
            await self.db.execute(
                "INSERT INTO blackjack_users (user_id, bankroll) VALUES (?, ?)",
                (user_id, self.starting_bankroll),
            )

    async def get_bankroll(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        row = await self.db.fetchone("SELECT bankroll FROM blackjack_users WHERE user_id=?", (user_id,))
        return int(row[0]) if row else self.starting_bankroll

    async def apply_delta_and_stats(self, user_id: int, delta: int, result_counts: dict[str, int], hands_played: int) -> int:
        await self.ensure_user(user_id)
        row = await self.db.fetchone("SELECT bankroll, wins, losses, pushes, hands_played FROM blackjack_users WHERE user_id=?", (user_id,))
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
        rows = await self.db.fetchall("SELECT user_id, bankroll FROM blackjack_users ORDER BY bankroll DESC LIMIT ?", (limit,))
        return [(int(uid), int(br)) for uid, br in rows]

    async def reset_user(self, user_id: int) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO blackjack_users (user_id, bankroll, hands_played, wins, losses, pushes) VALUES (?, ?, 0, 0, 0, 0)",
            (user_id, self.starting_bankroll),
        )


class SessionManager:
    def __init__(self):
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


async def _reply_or_edit(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


def _ttl_seconds(interaction: discord.Interaction) -> int:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    return int(settings.bj_session_ttl_minutes) * 60


@bj.command(name="balance", description="Show your blackjack bankroll.")
async def balance(interaction: discord.Interaction):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    br = await store.get_bankroll(interaction.user.id)
    embed = system_embed(settings, title="💰 Blackjack Bankroll", summary=f"**${br}**", footer_hint=interaction.user.display_name)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bj.command(name="start", description="Start a blackjack hand with a bet.")
@app_commands.describe(bet="Bet amount (integer)")
async def start(interaction: discord.Interaction, bet: int):
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server (guild).", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]

    _sessions.cleanup(_ttl_seconds(interaction))
    existing = _sessions.get(interaction.guild.id, interaction.user.id)
    if existing and not existing.state.done:
        await interaction.response.send_message("You already have an active blackjack hand. Use `/ic bj hit` or `/ic bj stand`.", ephemeral=True)
        return

    bet = int(bet)
    if bet <= 0:
        await interaction.response.send_message("Bet must be >= 1.", ephemeral=True)
        return

    bankroll = await store.get_bankroll(interaction.user.id)
    if bet > bankroll:
        await interaction.response.send_message(f"❌ You only have ${bankroll}. Lower your bet.", ephemeral=True)
        return

    state = GameState.start(
        bet=bet,
        dealer_hits_soft_17=settings.bj_dealer_hits_soft_17,
        max_splits=settings.bj_max_splits,
    )

    sess = BJSession(state=state, user_id=interaction.user.id, guild_id=interaction.guild.id, created_ts=time.time(), last_ts=time.time())
    _sessions.set(sess)

    # If immediate blackjacks settle now
    if state.hands[0].is_blackjack() or state.dealer.is_blackjack():
        state.done = True
        results = state.settle()
        delta_total = sum(d for _, d in results)
        counts = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        new_bankroll = await store.apply_delta_and_stats(interaction.user.id, delta_total, counts, hands_played=len(results))
        _sessions.clear(interaction.guild.id, interaction.user.id)

        dealer, dealer_total, hands = render_state(state, hide_dealer=False)
        settings = interaction.client.settings  # type: ignore[attr-defined]
        embed = game_embed(
            settings,
            title="🃏 Blackjack — Hand Complete",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
            fields=(
                ("Dealer", f"{dealer}  → **{dealer_total}**"),
                ("Player", hands),
            ),
            footer_hint=interaction.user.display_name,
        )
        await interaction.response.send_message(embed=embed)
        return

    dealer, dealer_total, hands = render_state(state, hide_dealer=True)
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = game_embed(
        settings,
        title="🃏 Blackjack — Started",
        summary="Your hand is live.",
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )

    actions = ["Use `/ic bj hit` or `/ic bj stand`"]
    if state.can_double():
        actions.append("`/ic bj double` available")
    if state.can_split():
        actions.append("`/ic bj split` available")
    embed.set_footer(text=" | ".join(actions))

    await interaction.response.send_message(embed=embed)


@bj.command(name="hit", description="Hit (draw a card).")
async def hit(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    _sessions.cleanup(_ttl_seconds(interaction))

    sess = _sessions.get(interaction.guild.id, interaction.user.id)
    if not sess or sess.state.done:
        await interaction.response.send_message("No active hand. Start one with `/ic bj start bet:...`", ephemeral=True)
        return

    sess.last_ts = time.time()
    sess.state.hit()

    if sess.state.done:
        results = sess.state.settle()
        delta_total = sum(d for _, d in results)
        counts = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        new_bankroll = await store.apply_delta_and_stats(interaction.user.id, delta_total, counts, hands_played=len(results))
        _sessions.clear(interaction.guild.id, interaction.user.id)

        embed = discord.Embed(title="🃏 Blackjack — Hand Complete")
        dealer, dealer_total, hands = render_state(sess.state, hide_dealer=False)
        embed.add_field(name="Dealer", value=f"{dealer}  → **{dealer_total}**", inline=False)
        embed.add_field(name="Player", value=hands, inline=False)
        embed.add_field(name="Result", value=f"Net: **{delta_total:+d}** | New bankroll: **${new_bankroll}**", inline=False)
        await _reply_or_edit(interaction, embed)
        return

    dealer, dealer_total, hands = render_state(sess.state, hide_dealer=True)
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = game_embed(
        settings,
        title="🃏 Blackjack — Hit",
        summary="Card drawn.",
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )
    actions = ["`/ic bj hit`", "`/ic bj stand`"]
    if sess.state.can_double():
        actions.append("`/ic bj double`")
    if sess.state.can_split():
        actions.append("`/ic bj split`")
    embed.set_footer(text=" | ".join(actions))
    await _reply_or_edit(interaction, embed)


@bj.command(name="stand", description="Stand (end your turn for this hand).")
async def stand(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    _sessions.cleanup(_ttl_seconds(interaction))

    sess = _sessions.get(interaction.guild.id, interaction.user.id)
    if not sess or sess.state.done:
        await interaction.response.send_message("No active hand. Start one with `/ic bj start`.", ephemeral=True)
        return

    sess.last_ts = time.time()
    sess.state.stand()

    if sess.state.done:
        results = sess.state.settle()
        delta_total = sum(d for _, d in results)
        counts = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        new_bankroll = await store.apply_delta_and_stats(interaction.user.id, delta_total, counts, hands_played=len(results))
        _sessions.clear(interaction.guild.id, interaction.user.id)

        embed = discord.Embed(title="🃏 Blackjack — Hand Complete")
        dealer, dealer_total, hands = render_state(sess.state, hide_dealer=False)
        embed.add_field(name="Dealer", value=f"{dealer}  → **{dealer_total}**", inline=False)
        embed.add_field(name="Player", value=hands, inline=False)
        embed.add_field(name="Result", value=f"Net: **{delta_total:+d}** | New bankroll: **${new_bankroll}**", inline=False)
        await _reply_or_edit(interaction, embed)
        return

    dealer, dealer_total, hands = render_state(sess.state, hide_dealer=True)
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = game_embed(
        settings,
        title="🃏 Blackjack — Next Hand",
        summary="Proceeding to next hand.",
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )
    embed.set_footer(text="Now acting on next hand (if you split).")
    await _reply_or_edit(interaction, embed)


@bj.command(name="double", description="Double down (double bet, draw exactly one card, then stand).")
async def double(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    _sessions.cleanup(_ttl_seconds(interaction))

    sess = _sessions.get(interaction.guild.id, interaction.user.id)
    if not sess or sess.state.done:
        await interaction.response.send_message("No active hand. Start one with `/ic bj start`.", ephemeral=True)
        return

    bankroll = await store.get_bankroll(interaction.user.id)
    current_bet = sess.state.bets[sess.state.active_index]
    extra_needed = current_bet
    if extra_needed > bankroll:
        await interaction.response.send_message(f"❌ You need at least ${extra_needed} bankroll available to double.", ephemeral=True)
        return

    if not sess.state.can_double():
        await interaction.response.send_message("You can't double right now.", ephemeral=True)
        return

    sess.last_ts = time.time()
    sess.state.double()

    if sess.state.done:
        results = sess.state.settle()
        delta_total = sum(d for _, d in results)
        counts = {}
        for r, _ in results:
            counts[r] = counts.get(r, 0) + 1
        new_bankroll = await store.apply_delta_and_stats(interaction.user.id, delta_total, counts, hands_played=len(results))
        _sessions.clear(interaction.guild.id, interaction.user.id)

        dealer, dealer_total, hands = render_state(sess.state, hide_dealer=False)
        settings = interaction.client.settings  # type: ignore[attr-defined]
        embed = game_embed(
            settings,
            title="🃏 Blackjack — Hand Complete (Double)",
            summary=f"Net: **{delta_total:+d}** | Bankroll: **${new_bankroll}**",
            fields=(
                ("Dealer", f"{dealer}  → **{dealer_total}**"),
                ("Player", hands),
            ),
            footer_hint=interaction.user.display_name,
        )
        await _reply_or_edit(interaction, embed)
        return

    dealer, dealer_total, hands = render_state(sess.state, hide_dealer=True)
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = game_embed(
        settings,
        title="🃏 Blackjack — Doubled",
        summary="Double down executed.",
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )
    embed.set_footer(text="Double completed. Continue with the next hand if split.")
    await _reply_or_edit(interaction, embed)


@bj.command(name="split", description="Split your hand (only if you have a pair).")
async def split(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Blackjack only works in a server.", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    store = BlackjackStore(interaction.client.db, settings.bj_starting_bankroll)  # type: ignore[attr-defined]
    _sessions.cleanup(_ttl_seconds(interaction))

    sess = _sessions.get(interaction.guild.id, interaction.user.id)
    if not sess or sess.state.done:
        await interaction.response.send_message("No active hand. Start one with `/ic bj start`.", ephemeral=True)
        return

    bankroll = await store.get_bankroll(interaction.user.id)
    current_bet = sess.state.bets[sess.state.active_index]
    if current_bet > bankroll:
        await interaction.response.send_message(f"❌ You need at least ${current_bet} bankroll available to split.", ephemeral=True)
        return

    ok = sess.state.split()
    if not ok:
        await interaction.response.send_message("You can't split right now (need a pair and splits remaining).", ephemeral=True)
        return

    sess.last_ts = time.time()
    dealer, dealer_total, hands = render_state(sess.state, hide_dealer=True)
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = game_embed(
        settings,
        title="🃏 Blackjack — Split",
        summary="Hand split into two.",
        fields=(
            ("Dealer", f"{dealer}  → **{dealer_total}**"),
            ("Player", hands),
        ),
        footer_hint=interaction.user.display_name,
    )
    embed.set_footer(text="Now play Hand 1, then Hand 2. Use `/ic bj hit` or `/ic bj stand`.")
    await _reply_or_edit(interaction, embed)


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
    settings = interaction.client.settings  # type: ignore[attr-defined]
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
