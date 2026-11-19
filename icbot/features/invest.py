from __future__ import annotations

import contextlib
from collections.abc import Iterable

import discord
from discord import app_commands

from icbot.app import admin, icse
from icbot.config import Settings
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import stock as stock_embed
from icbot.embeds import system as system_embed
from icbot.errors import safe_error_summary
from icbot.permissions import is_admin_or_allowed
from icbot.registry import guard_feature
from icbot.services.fun_market import FunMarketError, FunMarketStore, format_signed_money
from icbot.utils import parse_signed_int

SIMULATED_MARKET_DISCLAIMER = (
    "Simulated Market Points • Strictly virtual entertainment points with zero monetary value."
)


class AdminMarketConfirmView(discord.ui.View):
    """Two-button confirmation view for high-impact admin market actions."""

    def __init__(
        self,
        *,
        admin_user_id: int,
        settings: Settings,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.admin_user_id = admin_user_id
        self.settings = settings
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_user_id:
            await interaction.response.send_message(
                "❌ Only the admin who initiated this action can confirm or cancel it.",
                ephemeral=True,
            )
            return False
        if not is_admin_or_allowed(interaction, self.settings):
            await interaction.response.send_message(
                "❌ You don’t have permission to confirm this action.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            content="❌ Action cancelled. Catalog and stock prices remain unchanged.",
            view=self,
        )

    async def on_timeout(self) -> None:
        self.confirmed = False
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


async def _post_optional_announcement(
    interaction: discord.Interaction,
    settings: Settings,
    embed: discord.Embed,
) -> None:
    """Post an embed to the stock channel when an admin moves/sets a stock."""
    if settings.stock_channel_id and interaction.guild:
        ch = interaction.guild.get_channel(settings.stock_channel_id)
        if isinstance(ch, discord.TextChannel):
            with contextlib.suppress(discord.Forbidden):
                await ch.send(embed=embed)


def _store(interaction: discord.Interaction, settings: Settings) -> FunMarketStore:
    return get_container(interaction.client).fun_market_store(settings)


def _fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _fmt_stock_line(s) -> str:
    chg = s.weekly_change_abs
    sign = "+" if chg >= 0 else ""
    return f"**${s.symbol}** — ${s.price} ({sign}{chg} / {_fmt_pct(s.weekly_change_pct)})"


def _chunk_lines(lines: Iterable[str], n: int) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for line in lines:
        buf.append(line)
        if len(buf) >= n:
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


@icse.command(name="market", description="Show the current Instigate Cafe Stock Exchange.")
async def market(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    rows = await st.list_stocks(active_only=True)
    if not rows:
        await interaction.response.send_message(
            f"No {settings.stock_exchange_name} stocks configured yet. Edit `icbot/services/fun_market_catalog.py`."
        )
        return

    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)

    fields: list[tuple[str, str, bool]] = []
    for cat, items in by_cat.items():
        lines = [_fmt_stock_line(i) for i in sorted(items, key=lambda x: x.symbol)]
        for idx, chunk in enumerate(_chunk_lines(lines, 8), start=1):
            suffix = f" ({idx})" if len(lines) > 8 else ""
            fields.append((f"{cat.title()}{suffix}", chunk, False))

    wk = rows[0].week_key if rows else "current week"
    embed = system_embed(
        settings,
        title=f"🧪 {settings.stock_exchange_name}",
        summary=(
            f"Weekly change baseline: **{wk}**\n"
            f"*{SIMULATED_MARKET_DISCLAIMER}*\n"
            "Use `/ic icse quote`, `/ic icse buy`, `/ic icse sell`, `/ic icse short`, `/ic icse cover`"
        ),
        fields=fields[:20],
        footer_hint=f"{'ICSE' if settings.primary_stock_symbol == 'IC' else f'{settings.primary_stock_symbol}SE'} • Simulated Points",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="quote", description="Show a single stock quote.")
@app_commands.describe(symbol="Example: IC, CAFE, ROBO")
async def quote(interaction: discord.Interaction, symbol: str):
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        s = await st.get_stock(symbol)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    headline = (
        f"**${s.price}**\n"
        f"{s.name}\n"
        f"Week open: **${s.week_open}** • Change: **{s.weekly_change_abs:+d}** ({_fmt_pct(s.weekly_change_pct)})\n"
        f"*{SIMULATED_MARKET_DISCLAIMER}*"
    )
    embed = stock_embed(
        settings,
        title=f"📈 ${s.symbol} Quote",
        headline=headline,
        footer_hint="Simulated Points (No Cash Value)",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="portfolio", description="Show your market portfolio.")
@app_commands.describe(user="Optional: view another user's portfolio")
async def portfolio(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    snap = await st.portfolio(user_id=target.id)

    pos_lines: list[str] = []
    for p in snap.positions:
        if p.side == "LONG":
            pos_lines.append(
                f"**LONG ${p.symbol}** x{p.qty} @ ${p.avg_price:.2f} • Mkt ${p.market_price} • "
                f"Value ${p.market_value} • UPNL {format_signed_money(p.unrealized_pnl)}"
            )
        else:
            pos_lines.append(
                f"**SHORT ${p.symbol}** x{p.qty} @ ${p.avg_price:.2f} • Mkt ${p.market_price} • "
                f"Collateral ${p.collateral_locked} • Equity ${p.equity_value} • UPNL {format_signed_money(p.unrealized_pnl)}"
            )

    fields: list[tuple[str, str, bool]] = [
        ("Cash", f"**${snap.cash}**", True),
        ("Long Value", f"**${snap.long_value}**", True),
        ("Short Equity", f"**${snap.short_equity}**", True),
        ("Unrealized P/L", f"**{format_signed_money(snap.unrealized_pnl)}**", True),
        ("Net Worth", f"**${snap.net_worth}**", True),
    ]
    if pos_lines:
        fields.append(("Positions", "\n".join(pos_lines[:12]), False))
    else:
        fields.append(("Positions", "No open positions.", False))

    embed = system_embed(
        settings,
        title="📂 Portfolio",
        summary=f"{target.mention} • *{SIMULATED_MARKET_DISCLAIMER}*",
        fields=fields,
        footer_hint="ICSE • Simulated Points",
    )
    await interaction.response.send_message(embed=embed, ephemeral=(target.id == interaction.user.id))


@icse.command(name="buy", description="Buy a stock (long).")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def buy(interaction: discord.Interaction, symbol: str, qty: int):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        s, pos, bankroll = await st.buy_long(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    embed = stock_embed(
        settings,
        title=f"🟢 Bought ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Position: LONG x{pos.qty} @ ${pos.avg_price:.2f}\n"
            f"Cash: **${bankroll}**\n"
            f"*{SIMULATED_MARKET_DISCLAIMER}*"
        ),
        footer_hint=f"{interaction.user.display_name} • Simulated Points",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="sell", description="Sell a long position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def sell(interaction: discord.Interaction, symbol: str, qty: int):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        s, pos, bankroll, realized = await st.sell_long(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    pos_line = "Position closed." if pos is None else f"Remaining LONG x{pos.qty} @ ${pos.avg_price:.2f}"
    embed = stock_embed(
        settings,
        title=f"🔵 Sold ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Realized P/L: **{format_signed_money(realized)}**\n"
            f"{pos_line}\n"
            f"Cash: **${bankroll}**\n"
            f"*{SIMULATED_MARKET_DISCLAIMER}*"
        ),
        footer_hint=f"{interaction.user.display_name} • Simulated Points",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="short", description="Open/increase a short position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def short(interaction: discord.Interaction, symbol: str, qty: int):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        s, pos, bankroll, collateral = await st.open_short(
            user_id=interaction.user.id, symbol=symbol, qty=qty
        )
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    embed = stock_embed(
        settings,
        title=f"🟥 Shorted ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Collateral locked: **${collateral}**\n"
            f"Position: SHORT x{pos.qty} @ ${pos.avg_price:.2f}\n"
            f"Cash: **${bankroll}**\n"
            f"*{SIMULATED_MARKET_DISCLAIMER}*"
        ),
        footer_hint=f"{interaction.user.display_name} • Simulated Points",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="cover", description="Cover (close) a short position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def cover(interaction: discord.Interaction, symbol: str, qty: int):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        s, pos, bankroll, realized, release = await st.cover_short(
            user_id=interaction.user.id, symbol=symbol, qty=qty
        )
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    pos_line = "Position closed." if pos is None else f"Remaining SHORT x{pos.qty} @ ${pos.avg_price:.2f}"
    embed = stock_embed(
        settings,
        title=f"🟨 Covered ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Collateral released: **${release}**\n"
            f"Realized P/L: **{format_signed_money(realized)}**\n"
            f"{pos_line}\n"
            f"Cash: **${bankroll}**\n"
            f"*{SIMULATED_MARKET_DISCLAIMER}*"
        ),
        footer_hint=f"{interaction.user.display_name} • Simulated Points",
    )
    await interaction.response.send_message(embed=embed)


@icse.command(name="trades", description="Show recent market trades.")
@app_commands.describe(limit="Max 25", user="Optional: show another user's trades")
async def trades(interaction: discord.Interaction, limit: int = 10, user: discord.User | None = None):
    target = user or interaction.user
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    rows = await st.recent_trades(user_id=target.id, limit=limit)
    if not rows:
        await interaction.response.send_message("No market trades yet.", ephemeral=True)
        return

    lines = []
    for ts, _uid, sym, side, qty_i, price, cash_delta, rpnl, note in rows:
        note_s = f" — {note}" if note else ""
        lines.append(
            f"<t:{int(ts)}:R> • **{side}** ${sym} x{int(qty_i)} @ ${int(price)} • "
            f"cash {format_signed_money(int(cash_delta))} • rpnl {format_signed_money(int(rpnl))}{note_s}"
        )

    embed = system_embed(
        settings,
        title="🧾 ICSE Market Trades",
        summary=f"{target.mention}",
        fields=[("Recent", "\n".join(lines[:25]), False)],
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed, ephemeral=(target.id == interaction.user.id))


@icse.command(name="history", description="Show recent admin price updates for ICSE stocks.")
@app_commands.describe(limit="Max 25", symbol="Optional symbol filter")
async def history(interaction: discord.Interaction, limit: int = 10, symbol: str | None = None):
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)
    try:
        rows = await st.history(symbol=symbol, limit=limit)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    if not rows:
        await interaction.response.send_message("No stock price history yet.")
        return

    lines = []
    for ts, sym, uid, delta, new_value, reason in rows:
        sign = "+" if int(delta) >= 0 else ""
        reason_s = f" — {reason}" if reason else ""
        lines.append(
            f"<t:{int(ts)}:R> • **${sym}** • <@{int(uid)}> • **{sign}{int(delta)}** → **${int(new_value)}**{reason_s}"
        )

    embed = system_embed(
        settings,
        title="🕰️ Stock Price History",
        summary="Recent admin moves/sets",
        fields=[("History", "\n".join(lines[:25]), False)],
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed)


# -------------------------
# Admin controls (catalog is still code-only)
# -------------------------


@admin.command(name="move", description="Admin: move a stock by +/- delta.")
@app_commands.describe(symbol="Example: IC", change="Examples: +10, -5", reason="Optional reason")
async def admin_move(interaction: discord.Interaction, symbol: str, change: str, reason: str | None = None):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    delta = parse_signed_int(change)
    if delta is None:
        await interaction.response.send_message(
            "❌ Invalid change. Use `+10`, `-5`, or `25`.", ephemeral=True
        )
        return

    st = _store(interaction, settings)
    try:
        s = await st.admin_move(symbol=symbol, delta=delta, user_id=interaction.user.id, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    sign = "+" if int(delta) >= 0 else ""
    title_text = f"📈 ${s.symbol} Up" if int(delta) >= 0 else f"📉 ${s.symbol} Down"
    embed = stock_embed(
        settings,
        title=title_text,
        headline=f"**${s.symbol}** • **{sign}{int(delta)} → ${s.price}**",
        reason=(
            f"{s.name}\nWeek: {s.weekly_change_abs:+d} ({_fmt_pct(s.weekly_change_pct)})"
            + (f"\n{reason}" if reason else "")
        ),
        footer_hint=f"Moved by {interaction.user.name}",
    )
    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, settings, embed)


@admin.command(name="set", description="Admin: set a stock to an exact price.")
@app_commands.describe(symbol="Example: IC", value="New price (>=1)", reason="Optional reason")
async def admin_set(interaction: discord.Interaction, symbol: str, value: int, reason: str | None = None):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction, settings)
    try:
        s = await st.admin_set(symbol=symbol, new_value=value, user_id=interaction.user.id, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(
            f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
        )
        return

    embed = stock_embed(
        settings,
        title=f"🧾 ${s.symbol} Set",
        headline=f"**${s.symbol}** • **${s.price}**",
        reason=(
            f"{s.name}\nWeek: {s.weekly_change_abs:+d} ({_fmt_pct(s.weekly_change_pct)})"
            + (f"\n{reason}" if reason else "")
        ),
        footer_hint=f"Set by {interaction.user.name}",
    )
    await interaction.response.send_message(embed=embed)
    await _post_optional_announcement(interaction, settings, embed)


@admin.command(
    name="sync_catalog", description="Admin: sync stock catalog or reset to default synthetic securities."
)
@app_commands.describe(
    reset_to_default="Reset guild catalog overrides back to synthetic defaults (requires confirmation)"
)
async def sync_catalog_cmd(interaction: discord.Interaction, reset_to_default: bool = False):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction, settings)
    if reset_to_default:
        view = AdminMarketConfirmView(admin_user_id=interaction.user.id, settings=settings)
        await interaction.response.send_message(
            "⚠️ **Warning:** Resetting the catalog will restore all stock definitions to canonical synthetic defaults. "
            "Existing positions are preserved, but inactive custom stocks will no longer be tradable.\n"
            "Do you want to proceed?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return
        await st.reset_guild_catalog()
        rows = await st.list_stocks(active_only=False, include_inactive=True)
        active = [r for r in rows if r.is_active]
        inactive = [r for r in rows if not r.is_active]
        await interaction.followup.send(
            f"✅ Guild stock catalog reset to default synthetic securities. "
            f"Active: **{len(active)}**, inactive: **{len(inactive)}**.",
            ephemeral=True,
        )
        return

    await st.sync_catalog()
    rows = await st.list_stocks(active_only=False, include_inactive=True)
    active = [r for r in rows if r.is_active]
    inactive = [r for r in rows if not r.is_active]
    await interaction.response.send_message(
        f"✅ Catalog synced. Active: **{len(active)}**, inactive: **{len(inactive)}**."
    )


@admin.command(name="weekroll", description="Admin: reset weekly change baseline to current prices.")
@app_commands.describe(note="Optional note")
async def weekroll_cmd(interaction: discord.Interaction, note: str | None = None):
    if not await guard_feature(interaction, "market"):
        return
    settings = await resolve_guild_settings(interaction.client, interaction)
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction, settings)
    wk = await st.week_rollover(user_id=interaction.user.id, note=note)
    await interaction.response.send_message(
        f"✅ Weekly baseline reset for all active stocks. New week key: **{wk}**."
    )


@icse.command(name="stock", description="Show the current primary stock price and history.")
async def stock(interaction: discord.Interaction):
    settings = await resolve_guild_settings(interaction.client, interaction)
    st = _store(interaction, settings)

    sym = (settings.primary_stock_symbol or "IC").strip().upper()
    try:
        s = await st.get_stock(sym)
    except FunMarketError:
        try:
            s = await st.get_stock("IC")
        except FunMarketError as e:
            await interaction.response.send_message(
                f"❌ {safe_error_summary(e, settings.secret_values())}", ephemeral=True
            )
            return

    curr_sym = settings.currency_symbol or "$"
    headline = (
        f"**{curr_sym}{s.price}**\n"
        f"{s.name}\n"
        f"Week open: **{curr_sym}{s.week_open}** • Change: **{s.weekly_change_abs:+d}** ({_fmt_pct(s.weekly_change_pct)})\n"
        f"*{SIMULATED_MARKET_DISCLAIMER}*"
    )

    try:
        rows = await st.history(symbol=s.symbol, limit=5)
    except FunMarketError:
        rows = []

    reason_str = ""
    if rows:
        lines = []
        for ts, _sym, uid, delta, new_value, reason in rows:
            sign = "+" if int(delta) >= 0 else ""
            reason_s = f" — {reason}" if reason else ""
            lines.append(
                f"<t:{int(ts)}:R> <@{int(uid)}>: **{sign}{int(delta)}** → **{curr_sym}{int(new_value)}**{reason_s}"
            )
        reason_str = "\n".join(lines)
    else:
        reason_str = "No recent history."

    embed = stock_embed(
        settings,
        title=f"📈 {s.name} Stock ({curr_sym}{s.symbol})",
        headline=headline,
        reason=reason_str,
        footer_hint="Simulated Points (No Cash Value)",
    )
    await interaction.response.send_message(embed=embed)
