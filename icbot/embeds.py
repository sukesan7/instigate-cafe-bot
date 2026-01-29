from __future__ import annotations
from typing import Iterable, Optional, Tuple
import discord

from icbot.config import Settings


# Category tags (so stock embeds are consistent, games embeds consistent, etc.)
EMBED_KIND_STOCK = "stock"
EMBED_KIND_GAME = "game"
EMBED_KIND_SYSTEM = "system"


def _base_embed(settings: Settings, *, title: str, kind: str, description: str | None = None) -> discord.Embed:
    """
    One place to enforce a consistent embed look:
    - Bright purple accent (border)
    - Optional logo thumbnail
    - Consistent footer + timestamp
    """
    e = discord.Embed(
        title=title,
        description=description,
        color=settings.embed_accent_color,
    )
    if settings.embed_logo_url:
        e.set_thumbnail(url=settings.embed_logo_url)

    # Footer keeps category hint (small + consistent)
    suffix = {
        EMBED_KIND_STOCK: "Stock",
        EMBED_KIND_GAME: "Games",
        EMBED_KIND_SYSTEM: "System",
    }.get(kind, "Info")
    e.set_footer(text=f"{settings.embed_footer_text} • {suffix}")
    e.timestamp = discord.utils.utcnow()
    return e


def stock(settings: Settings, *, title: str, headline: str, reason: str | None = None, footer_hint: str | None = None) -> discord.Embed:
    """
    Stock-format embed:
      Title: command-specific
      Description: headline (big, bold)
      Fields: optional reason
    """
    e = _base_embed(settings, title=title, kind=EMBED_KIND_STOCK, description=headline)
    if reason:
        e.add_field(name="Reason", value=reason, inline=False)
    if footer_hint:
        e.set_footer(text=f"{settings.embed_footer_text} • Stock • {footer_hint}")
    return e


def game(settings: Settings, *, title: str, summary: str | None = None, fields: Iterable[Tuple[str, str]] = (), footer_hint: str | None = None) -> discord.Embed:
    """
    Game-format embed:
      Title + optional summary
      Fields for dealer/player/state/result (smaller text)
    """
    e = _base_embed(settings, title=title, kind=EMBED_KIND_GAME, description=summary)
    for name, value in fields:
        if value is None or value == "":
            continue
        e.add_field(name=name, value=value, inline=False)
    if footer_hint:
        e.set_footer(text=f"{settings.embed_footer_text} • Games • {footer_hint}")
    return e


def system(settings: Settings, *, title: str, summary: str | None = None, fields: Iterable[Tuple[str, str]] = (), footer_hint: str | None = None) -> discord.Embed:
    e = _base_embed(settings, title=title, kind=EMBED_KIND_SYSTEM, description=summary)
    for name, value in fields:
        if value is None or value == "":
            continue
        e.add_field(name=name, value=value, inline=False)
    if footer_hint:
        e.set_footer(text=f"{settings.embed_footer_text} • {footer_hint}")
    return e
