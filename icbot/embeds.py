from __future__ import annotations

import re
from collections.abc import Iterable

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
_MAX_FIELDS = 25
_MAX_TOTAL = 6000

_MENTION_RE = re.compile(r"@(everyone|here)")
_USER_ROLE_PING_RE = re.compile(r"<@&?(\d+)>")

Field = tuple[str, str] | tuple[str, str, bool]


def sanitize_text(s: str | None, limit: int | None = None) -> str | None:
    """Sanitize untrusted text by neutralizing mass pings, mention injections, and truncating."""
    if s is None:
        return None
    text = str(s)
    # Neutralize mass pings (@everyone, @here) with zero-width space
    text = _MENTION_RE.sub("@\u200b\\1", text)
    # Neutralize raw user / role mentions
    text = _USER_ROLE_PING_RE.sub("<@\u200b\\1>", text)
    if limit is not None:
        return _truncate(text, limit)
    return text


def safe_allowed_mentions() -> discord.AllowedMentions:
    """Return an AllowedMentions instance that prevents all outbound pings."""
    return discord.AllowedMentions.none()


def _truncate(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    if limit <= 0:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _footer_text(settings: Settings, kind: str, footer_hint: str | None) -> str:
    kind_label = {
        EMBED_KIND_STOCK: "Stock Exchange",
        EMBED_KIND_GAME: "Casino",
        EMBED_KIND_SYSTEM: "System",
        EMBED_KIND_ALERTS: "Alerts",
        EMBED_KIND_JOBS: "Jobs",
    }.get(kind, "Info")

    parts = [settings.embed_footer_text, kind_label]
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
    clean_title = sanitize_text(title, _MAX_TITLE) or " "
    clean_subtitle = sanitize_text((subtitle or "").strip()) or ""
    clean_desc: str | None = None

    if clean_subtitle:
        prefix = f"*{clean_subtitle}*"
        if description:
            body_budget = _MAX_DESC - len(prefix) - 2
            if body_budget > 0:
                clean_desc = f"{prefix}\n\n{sanitize_text(description, body_budget) or ''}"
            else:
                clean_desc = sanitize_text(prefix, _MAX_DESC)
        else:
            clean_desc = sanitize_text(prefix, _MAX_DESC)
    else:
        clean_desc = sanitize_text(description, _MAX_DESC)

    e = discord.Embed(
        title=clean_title,
        description=clean_desc,
        color=settings.embed_accent_color,
    )

    # Brand header with logo
    author_name = getattr(settings, "bot_display_name", "") or settings.embed_footer_text or "Instigate Cafe"
    author_kwargs: dict[str, str] = {
        "name": sanitize_text(author_name, _MAX_TITLE) or "Instigate Cafe",
    }
    logo = getattr(settings, "embed_logo_url", "")
    if logo:
        author_kwargs["icon_url"] = logo
    e.set_author(**author_kwargs)

    # Logo thumbnail
    if logo:
        e.set_thumbnail(url=logo)

    # Footer + timestamp
    footer_val = _footer_text(settings, kind, footer_hint)
    e.set_footer(text=sanitize_text(footer_val, _MAX_TITLE) or " ")
    e.timestamp = discord.utils.utcnow()
    return e


def _add_fields(e: discord.Embed, fields: Iterable[Field]) -> None:
    for item in fields:
        if len(e.fields) >= _MAX_FIELDS:
            break
        if not item:
            continue

        inline = False
        if len(item) == 2:
            name, value = item  # type: ignore[misc]
        else:
            name, value, inline = item  # type: ignore[misc]

        if value is None:
            continue

        remaining = _MAX_TOTAL - len(e)
        if remaining <= 1:
            break

        name_s = sanitize_text(str(name), min(_MAX_FIELD_NAME, remaining)) or " "
        value_budget = min(_MAX_FIELD_VALUE, remaining - len(name_s))
        if value_budget <= 0:
            break

        value_s = sanitize_text(str(value), value_budget) or " "
        if len(name_s) + len(value_s) > remaining:
            break
        e.add_field(name=name_s, value=value_s, inline=bool(inline))


def embed_to_text_snapshot(embed: discord.Embed) -> str:
    """Render a deterministic plain-text snapshot of a Discord embed for visual verification."""
    lines: list[str] = ["[EMBED]"]
    if embed.author and embed.author.name:
        icon_part = f" (icon: {embed.author.icon_url})" if embed.author.icon_url else ""
        lines.append(f"Author: {embed.author.name}{icon_part}")
    if embed.title:
        url_part = f" (url: {embed.url})" if embed.url else ""
        lines.append(f"Title: {embed.title}{url_part}")
    if embed.description:
        lines.append(f"Description: {embed.description}")
    if embed.color:
        lines.append(f"Color: 0x{embed.color.value:06X}")
    if embed.fields:
        lines.append(f"Fields ({len(embed.fields)}):")
        for f in embed.fields:
            inline_tag = " [inline]" if f.inline else ""
            lines.append(f"  - {f.name}: {f.value}{inline_tag}")
    if embed.footer and embed.footer.text:
        icon_part = f" (icon: {embed.footer.icon_url})" if embed.footer.icon_url else ""
        lines.append(f"Footer: {embed.footer.text}{icon_part}")
    return "\n".join(lines)


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


def go_alert(
    settings: Settings,
    *,
    headline: str,
    details: str | None = None,
    level: str = "MAJOR",
    meta: str | None = None,
    posted_dt: str | None = None,
    lines: list[str] | None = None,
) -> discord.Embed:
    title = f"🚆 GO Transit Service Alert • {level}"
    subtitle = None
    if posted_dt:
        subtitle = posted_dt
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_ALERTS,
        description=f"**{_truncate(headline, 220) or ''}**",
        footer_hint="GO Transit",
        subtitle=subtitle,
    )
    fields: list[Field] = []
    if meta:
        fields.append(("Category", meta, False))
    if lines:
        fields.append(("Lines", ", ".join(lines[:10]), False))
    if details:
        fields.append(("Details", details, False))
    _add_fields(e, fields)
    return e


def _safe_http_url(url: str | None) -> str | None:
    """Validate that a URL strictly uses http or https without whitespace or control chars."""
    if not url:
        return None
    cleaned = url.strip()
    if cleaned.lower().startswith(("http://", "https://")):
        if any(c in cleaned for c in ("\r", "\n", "\t", " ", '"', "<", ">", "\\")):
            return None
        return cleaned
    return None


def job_alert(
    settings: Settings,
    *,
    # Backwards compatible inputs:
    title_text: str | None = None,
    title: str | None = None,  # some callers use title=
    company: str | None = None,
    location: str | None = None,
    source: str | None = None,
    url: str | None = None,
    tags: list[str] | None = None,  # preferred name
    categories: list[str] | None = None,  # alias for tags (some callers use categories=)
    career_level: str | None = None,
    posted_date: str | None = None,
    posted_unix: int | None = None,
    preset: str | None = None,
    provider: str | None = None,
    footer_hint: str | None = None,
) -> discord.Embed:
    """
    Consistent, safe jobs-format embed for auto-posted ATS alerts and search listings.

    Clean format:
      Title: 💼 <Job Title>
      Company, Location, Level, Source, Posted Date, Tags
      Apply: Safe markdown link
    """

    def _display_label(raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return ""
        # strip internal prefixes like "workday:RBC", "lever:palantir", "greenhouse:stripe"
        if ":" in raw:
            raw = raw.split(":", 1)[1].strip()
        # prettify slugs: "wintermute-trading" -> "Wintermute Trading"
        raw = raw.replace("-", " ").strip()
        if not raw:
            return ""
        return " ".join(w.capitalize() for w in raw.split())

    headline = (title_text or title or "").strip() or "New Job Posting"
    company_label = _display_label(company or "") or _display_label(source or "")

    safe_url = _safe_http_url(url)

    e = _base_embed(
        settings,
        title=f"💼 {_truncate(headline, 240)}",
        kind=EMBED_KIND_JOBS,
        footer_hint=footer_hint or "Tracking System",
    )

    # Make the embed title clickable if URL is verified safe
    if safe_url:
        e.url = safe_url

    # Normalize tags/categories
    label_list = tags if tags is not None else categories

    fields: list[Field] = []
    if company_label:
        fields.append(("🏢 Company", f"**{company_label}**", True))
    if location:
        fields.append(("📍 Location", sanitize_text(location, 120) or "Not Listed", True))
    if career_level and career_level != "unknown":
        lvl_display = career_level.replace("_", " ").title()
        fields.append(("🎓 Level", lvl_display, True))

    # Source / ATS provider
    src_display = provider or source
    if src_display:
        clean_src = (
            src_display.split(":", 1)[0].capitalize() if ":" in src_display else src_display.capitalize()
        )
        fields.append(("📡 Source", clean_src, True))

    # Posted date / timestamp
    if posted_date:
        fields.append(("📅 Posted", sanitize_text(posted_date, 60) or "", True))
    elif posted_unix and posted_unix > 0:
        fields.append(("📅 Posted", f"<t:{posted_unix}:R>", True))

    if preset and preset != "all":
        fields.append(("🎯 Preset", f"`{preset}`", True))

    if label_list:
        tag_str = " • ".join(label_list[:8])
        fields.append(("🏷️ Tags", sanitize_text(tag_str, 200) or "", False))

    if safe_url:
        fields.append(("🔗 Apply", f"[Apply Here]({safe_url})", False))

    _add_fields(e, fields)
    return e
