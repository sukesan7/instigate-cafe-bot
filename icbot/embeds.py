from __future__ import annotations

from typing import Iterable, Tuple, Union, Optional
import discord

from icbot.config import Settings

# Category tags (so stock embeds are consistent, games embeds consistent, etc.)
EMBED_KIND_STOCK = "stock"
EMBED_KIND_GAME = "game"
EMBED_KIND_SYSTEM = "system"

# Discord embed limits (hard caps)
_MAX_TITLE = 256
_MAX_DESC = 4096
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024


# Field can be ("Name", "Value") or ("Name", "Value", inline_bool)
Field = Union[Tuple[str, str], Tuple[str, str, bool]]


def _truncate(s: Optional[str], limit: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _footer_text(settings: Settings, kind: str, footer_hint: str | None) -> str:
    suffix = {
        EMBED_KIND_STOCK: "Stock",
        EMBED_KIND_GAME: "Games",
        EMBED_KIND_SYSTEM: "System",
    }.get(kind, "Info")

    parts = [settings.embed_footer_text, suffix]
    if footer_hint:
        parts.append(footer_hint)
    return " • ".join([p for p in parts if p])


def _base_embed(
    settings: Settings,
    *,
    title: str,
    kind: str,
    description: str | None = None,
    subtitle: str | None = None,
    footer_hint: str | None = None,
) -> discord.Embed:
    """
    One place to enforce a consistent embed look:
    - Bright purple accent (border)
    - Optional logo thumbnail (top-right)
    - Brand author line (top-left)
    - Consistent footer + timestamp
    - Safe truncation for Discord limits
    """
    title = _truncate(title, _MAX_TITLE) or " "
    description = _truncate(description, _MAX_DESC)
    subtitle = (subtitle or "").strip()

    # Small aesthetic touch: if subtitle exists, prepend it in *small italics* above description
    if subtitle:
        if description:
            description = f"*{subtitle}*\n\n{description}"
        else:
            description = f"*{subtitle}*"

    e = discord.Embed(
        title=title,
        description=description,
        color=settings.embed_accent_color,
    )

    # Brand header (top-left)
    e.set_author(name=_truncate(settings.embed_footer_text, _MAX_TITLE) or "Instigate Cafe")

    # Logo in corner (top-right)
    if getattr(settings, "embed_logo_url", ""):
        e.set_thumbnail(url=settings.embed_logo_url)

    # Footer + timestamp
    e.set_footer(text=_truncate(_footer_text(settings, kind, footer_hint), _MAX_TITLE) or " ")
    e.timestamp = discord.utils.utcnow()
    return e


def _add_fields(e: discord.Embed, fields: Iterable[Field]) -> None:
    for item in fields:
        if not item:
            continue

        # Support both (name, value) and (name, value, inline)
        inline = False
        if len(item) == 2:
            name, value = item  # type: ignore[misc]
        else:
            name, value, inline = item  # type: ignore[misc]

        if value is None:
            continue

        name_s = _truncate(str(name), _MAX_FIELD_NAME) or " "
        value_s = _truncate(str(value), _MAX_FIELD_VALUE) or " "
        e.add_field(name=name_s, value=value_s, inline=bool(inline))


def stock(
    settings: Settings,
    *,
    title: str,
    headline: str,
    reason: str | None = None,
    footer_hint: str | None = None,
) -> discord.Embed:
    """
    Stock-format embed:
      Title: command-specific
      Description: headline (big, bold)
      Fields: optional reason (kept smaller)
    """
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_STOCK,
        description=headline,
        footer_hint=footer_hint,
    )

    if reason:
        _add_fields(e, [("Reason", reason, False)])

    return e


def game(
    settings: Settings,
    *,
    title: str,
    summary: str | None = None,
    fields: Iterable[Field] = (),
    footer_hint: str | None = None,
    controls: str | None = None,
    subtitle: str | None = None,
) -> discord.Embed:
    """
    Game-format embed:
      Title + optional summary
      Fields for dealer/player/state/result (smaller text)
      Optional Controls line (great for button-based interactions)
    """
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_GAME,
        description=summary,
        subtitle=subtitle,
        footer_hint=footer_hint,
    )

    _add_fields(e, fields)

    if controls:
        # Put controls last so it reads naturally
        _add_fields(e, [("Controls", controls, False)])

    return e


def system(
    settings: Settings,
    *,
    title: str,
    summary: str | None = None,
    fields: Iterable[Field] = (),
    footer_hint: str | None = None,
    subtitle: str | None = None,
) -> discord.Embed:
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_SYSTEM,
        description=summary,
        subtitle=subtitle,
        footer_hint=footer_hint,
    )

    _add_fields(e, fields)
    return e
