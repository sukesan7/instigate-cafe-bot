from __future__ import annotations

from typing import Iterable

import discord
from discord import app_commands

from icbot.app import ic
from icbot.embeds import stock as stock_embed, system as system_embed
from icbot.permissions import is_admin_or_allowed
from icbot.services.fun_market import FunMarketStore, FunMarketError, format_signed_money
from icbot.utils import parse_signed_int


invest = app_commands.Group(name="invest", description="ICSE server stock market")
ic.add_command(invest)


def _store(interaction: discord.Interaction) -> FunMarketStore:
    settings = interaction.client.settings  # type: ignore[attr-defined]
    db = interaction.client.db  # type: ignore[attr-defined]
    return FunMarketStore(
        db,
        starting_bankroll=int(getattr(settings, "bj_starting_bankroll", 1000) or 1000),
        tz_name=str(getattr(settings, "market_daily_report_tz", "America/Toronto") or "America/Toronto"),
    )


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


@invest.command(name="market", description="Show the current Instigate Cafe Stock Exchange.")
async def market(interaction: discord.Interaction):
    st = _store(interaction)
    rows = await st.list_stocks(active_only=True)
    if not rows:
        await interaction.response.send_message("No Instigate Cafe Stock Exchange stocks configured yet. Edit `icbot/services/fun_market_catalog.py`.")
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

    settings = interaction.client.settings  # type: ignore[attr-defined]
    wk = rows[0].week_key if rows else "current week"
    embed = system_embed(
        settings,
        title="🧪 Instigate Cafe Stock Exchange",
        summary=f"Weekly change baseline: **{wk}** • Use `/ic invest quote`, `/buy`, `/sell`, `/short`, `/cover`",
        fields=fields[:20],
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="quote", description="Show a single stock quote.")
@app_commands.describe(symbol="Example: IC, DISD, AGIS")
async def quote(interaction: discord.Interaction, symbol: str):
    st = _store(interaction)
    try:
        s = await st.get_stock(symbol)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    headline = (
        f"**${s.price}**\n"
        f"{s.name}\n"
        f"Week open: **${s.week_open}** • Change: **{s.weekly_change_abs:+d}** ({_fmt_pct(s.weekly_change_pct)})"
    )
    embed = stock_embed(settings, title=f"📈 ${s.symbol} Quote", headline=headline)
    await interaction.response.send_message(embed=embed)


@invest.command(name="portfolio", description="Show your market portfolio.")
@app_commands.describe(user="Optional: view another user's portfolio")
async def portfolio(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    st = _store(interaction)
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

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = system_embed(
        settings,
        title="📂 Portfolio",
        summary=f"{target.mention}",
        fields=fields,
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed, ephemeral=(target.id == interaction.user.id))


@invest.command(name="buy", description="Buy a stock (long).")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def buy(interaction: discord.Interaction, symbol: str, qty: int):
    st = _store(interaction)
    try:
        s, pos, bankroll = await st.buy_long(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(
        settings,
        title=f"🟢 Bought ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Position: LONG x{pos.qty} @ ${pos.avg_price:.2f}\n"
            f"Cash: **${bankroll}**"
        ),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="sell", description="Sell a long position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def sell(interaction: discord.Interaction, symbol: str, qty: int):
    st = _store(interaction)
    try:
        s, pos, bankroll, realized = await st.sell_long(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    pos_line = "Position closed." if pos is None else f"Remaining LONG x{pos.qty} @ ${pos.avg_price:.2f}"
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(
        settings,
        title=f"🔵 Sold ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Realized P/L: **{format_signed_money(realized)}**\n"
            f"{pos_line}\n"
            f"Cash: **${bankroll}**"
        ),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="short", description="Open/increase a short position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def short(interaction: discord.Interaction, symbol: str, qty: int):
    st = _store(interaction)
    try:
        s, pos, bankroll, collateral = await st.open_short(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(
        settings,
        title=f"🟥 Shorted ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Collateral locked: **${collateral}**\n"
            f"Position: SHORT x{pos.qty} @ ${pos.avg_price:.2f}\n"
            f"Cash: **${bankroll}**"
        ),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="cover", description="Cover (close) a short position.")
@app_commands.describe(symbol="Stock symbol", qty="Number of shares")
async def cover(interaction: discord.Interaction, symbol: str, qty: int):
    st = _store(interaction)
    try:
        s, pos, bankroll, realized, release = await st.cover_short(user_id=interaction.user.id, symbol=symbol, qty=qty)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    pos_line = "Position closed." if pos is None else f"Remaining SHORT x{pos.qty} @ ${pos.avg_price:.2f}"
    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = stock_embed(
        settings,
        title=f"🟨 Covered ${s.symbol}",
        headline=f"**{qty} shares @ ${s.price}**",
        reason=(
            f"Collateral released: **${release}**\n"
            f"Realized P/L: **{format_signed_money(realized)}**\n"
            f"{pos_line}\n"
            f"Cash: **${bankroll}**"
        ),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="trades", description="Show recent market trades.")
@app_commands.describe(limit="Max 25", user="Optional: show another user's trades")
async def trades(interaction: discord.Interaction, limit: int = 10, user: discord.User | None = None):
    target = user or interaction.user
    st = _store(interaction)
    rows = await st.recent_trades(user_id=target.id, limit=limit)
    if not rows:
        await interaction.response.send_message("No market trades yet.", ephemeral=True)
        return

    lines = []
    for ts, uid, sym, side, qty_i, price, cash_delta, rpnl, note in rows:
        note_s = f" — {note}" if note else ""
        lines.append(
            f"<t:{int(ts)}:R> • **{side}** ${sym} x{int(qty_i)} @ ${int(price)} • "
            f"cash {format_signed_money(int(cash_delta))} • rpnl {format_signed_money(int(rpnl))}{note_s}"
        )

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = system_embed(
        settings,
        title="🧾 ICSE Market Trades",
        summary=f"{target.mention}",
        fields=[("Recent", "\n".join(lines[:25]), False)],
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed, ephemeral=(target.id == interaction.user.id))


@invest.command(name="leaderboard", description="Rank users by ICSE market net worth (cash + positions).")
@app_commands.describe(limit="How many entries (max 25)")
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    st = _store(interaction)
    rows = await st.leaderboard_by_networth(limit=limit)
    if not rows:
        await interaction.response.send_message("No data yet.")
        return

    lines = []
    for i, snap in enumerate(rows, start=1):
        lines.append(
            f"**{i}.** <@{snap.user_id}> — **${snap.net_worth}** "
            f"(cash ${snap.cash}, long ${snap.long_value}, short ${snap.short_equity})"
        )

    settings = interaction.client.settings  # type: ignore[attr-defined]
    embed = system_embed(
        settings,
        title="🏦 Instigate Cafe Stock Exchange Market Net-Worth Leaderboard",
        summary="Cash + marked-to-market stock positions",
        fields=[("Leaderboard", "\n".join(lines[:25]), False)],
        footer_hint="ICSE",
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="history", description="Show recent admin price updates for ICSE stocks.")
@app_commands.describe(limit="Max 25", symbol="Optional symbol filter")
async def history(interaction: discord.Interaction, limit: int = 10, symbol: str | None = None):
    st = _store(interaction)
    try:
        rows = await st.history(symbol=symbol, limit=limit)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
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

    settings = interaction.client.settings  # type: ignore[attr-defined]
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


@invest.command(name="move", description="Admin: move a stock by +/- delta.")
@app_commands.describe(symbol="Example: IC", change="Examples: +10, -5", reason="Optional reason")
async def admin_move(interaction: discord.Interaction, symbol: str, change: str, reason: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    delta = parse_signed_int(change)
    if delta is None:
        await interaction.response.send_message("❌ Invalid change. Use `+10`, `-5`, or `25`.", ephemeral=True)
        return

    st = _store(interaction)
    try:
        s = await st.admin_move(symbol=symbol, delta=delta, user_id=interaction.user.id, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    sign = "+" if int(delta) >= 0 else ""
    embed = stock_embed(
        settings,
        title=("📈 Stock Up" if int(delta) >= 0 else "📉 Stock Down"),
        headline=f"**${s.symbol}** • **{sign}{int(delta)} → ${s.price}**",
        reason=(f"{s.name}\nWeek: {s.weekly_change_abs:+d} ({_fmt_pct(s.weekly_change_pct)})" + (f"\n{reason}" if reason else "")),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="set", description="Admin: set a stock to an exact price.")
@app_commands.describe(symbol="Example: IC", value="New price (>=1)", reason="Optional reason")
async def admin_set(interaction: discord.Interaction, symbol: str, value: int, reason: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction)
    try:
        s = await st.admin_set(symbol=symbol, new_value=value, user_id=interaction.user.id, reason=reason)
    except FunMarketError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    embed = stock_embed(
        settings,
        title="🧾 Fun Stock Set",
        headline=f"**${s.symbol}** • **${s.price}**",
        reason=(f"{s.name}\nWeek: {s.weekly_change_abs:+d} ({_fmt_pct(s.weekly_change_pct)})" + (f"\n{reason}" if reason else "")),
        footer_hint=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=embed)


@invest.command(name="sync", description="Admin: sync the code-defined stock catalog into the DB.")
async def sync_catalog_cmd(interaction: discord.Interaction):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction)
    await st.sync_catalog()
    rows = await st.list_stocks(active_only=False, include_inactive=True)
    active = [r for r in rows if r.is_active]
    inactive = [r for r in rows if not r.is_active]
    await interaction.response.send_message(
        f"✅ Catalog synced from code (`icbot/services/fun_market_catalog.py`). "
        f"Active: **{len(active)}**, inactive: **{len(inactive)}**."
    )


@invest.command(name="weekroll", description="Admin: reset weekly change baseline to current prices.")
@app_commands.describe(note="Optional note")
async def weekroll_cmd(interaction: discord.Interaction, note: str | None = None):
    settings = interaction.client.settings  # type: ignore[attr-defined]
    if not is_admin_or_allowed(interaction, settings):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    st = _store(interaction)
    wk = await st.week_rollover(user_id=interaction.user.id, note=note)
    await interaction.response.send_message(f"✅ Weekly baseline reset for all active stocks. New week key: **{wk}**.")
