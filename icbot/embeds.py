from __future__ import annotations

from typing import Iterable, Tuple, Union, Optional
import discord

from icbot.config import Settings

# Category tags
EMBED_KIND_STOCK = "stock"
EMBED_KIND_GAME = "game"
EMBED_KIND_SYSTEM = "system"
EMBED_KIND_ALERTS = "alerts"
EMBED_KIND_JOBS = "jobs"

# Discord embed limits
_MAX_TITLE = 256
_MAX_DESC = 4096
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024

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
        EMBED_KIND_ALERTS: "Alerts",
        EMBED_KIND_JOBS: "Jobs",
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
    title = _truncate(title, _MAX_TITLE) or " "
    description = _truncate(description, _MAX_DESC)
    subtitle = (subtitle or "").strip()

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

    # Brand header
    e.set_author(name=_truncate(settings.embed_footer_text, _MAX_TITLE) or "Instigate Cafe")

    # Logo thumbnail
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


def ttc_alert(
    settings: Settings,
    *,
    headline: str,
    details: str | None = None,
    url: str | None = None,
    level: str = "MAJOR",
) -> discord.Embed:
    title = f"🚇 TTC Service Alert • {level}"
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_ALERTS,
        description=f"**{_truncate(headline, 220) or ''}**",
        footer_hint="TTC",
    )
    if details:
        _add_fields(e, [("Details", details, False)])
    if url:
        _add_fields(e, [("Link", url, False)])
    return e


def job_alert(
    settings: Settings,
    *,
    # Backwards compatible inputs:
    title_text: str | None = None,
    title: str | None = None,                 # some callers use title=
    company: str | None = None,
    location: str | None = None,
    source: str | None = None,
    url: str | None = None,
    tags: list[str] | None = None,            # preferred name
    categories: list[str] | None = None,      # alias for tags (some callers use categories=)
    footer_hint: str | None = None,
) -> discord.Embed:
    """
    Jobs-format embed for auto-posted ATS alerts.

    Compatibility:
      - Accepts either `title_text=` or `title=`
      - Accepts either `tags=` or `categories=` (alias)
    """
    headline = (title_text or title or "").strip() or "New job posting"
    src = (source or "").strip()

    header = "💼 Job Alert" + (f" • {src}" if src else "")
    e = _base_embed(
        settings,
        title=header,
        kind=EMBED_KIND_JOBS,
        description=f"**{_truncate(headline, 250) or ''}**",
        footer_hint=footer_hint or ("Job Tracking Software"),
    )

    # Make the embed clickable to the posting
    if url:
        e.url = url

    # Normalize tags/categories
    label_list = tags if tags is not None else categories

    fields: list[Field] = []
    if company:
        fields.append(("Company", company, True))
    if location:
        fields.append(("Location", location, True))
    if label_list:
        fields.append(("Tags", ", ".join(label_list[:12]), False))
    if url:
        fields.append(("Apply", url, False))

    _add_fields(e, fields)
    return e
