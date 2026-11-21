from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import discord

from icbot.config import MAX_SNOWFLAKE, Settings, _is_valid_snowflake
from icbot.theme import EmbedStatus, resolve_semantic_color

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
_USER_ROLE_PING_RE = re.compile(r"<@[!&]?(\d+)>")

Field = tuple[str, str] | tuple[str, str, bool]


def _normalize_unicode_safe(s: str) -> str:
    """Strip NUL bytes and repair lone UTF-16 surrogates so JSON/HTTP serialization never fails."""
    cleaned = str(s).replace("\x00", "")
    try:
        cleaned.encode("utf-8")
        return cleaned
    except UnicodeEncodeError:
        return cleaned.encode("utf-8", errors="replace").decode("utf-8")


def escape_markdown(s: str | None) -> str:
    """Safely escape Discord Markdown characters to prevent formatting breakage or injection."""
    if not s:
        return ""
    return discord.utils.escape_markdown(_normalize_unicode_safe(str(s)))


def sanitize_text(s: str | None, limit: int | None = None) -> str | None:
    """Sanitize text by neutralizing mass pings, mention injections, NUL/surrogates, and truncating.

    Preserves legitimate bot-generated Markdown (`**bold**`, `*italic*`, `` `code` ``, `[label](url)`).
    For untrusted external or user-provided plain text that must not render Markdown, use
    `sanitize_untrusted_text(s, limit=...)`.
    """
    if s is None:
        return None
    text = _normalize_unicode_safe(str(s))
    # Neutralize mass pings (@everyone, @here) with zero-width space
    text = _MENTION_RE.sub("@\u200b\\1", text)
    # Neutralize raw user / role mentions
    text = _USER_ROLE_PING_RE.sub("<@\u200b\\1>", text)
    if limit is not None:
        return _truncate(text, limit)
    return text


def sanitize_untrusted_text(
    s: str | None,
    limit: int | None = None,
    *,
    escape_md: bool = True,
) -> str:
    """Sanitize untrusted external or user-supplied text.

    Escapes Discord Markdown syntax via `discord.utils.escape_markdown` (when `escape_md=True`),
    neutralizes `@everyone`, `@here`, and `<@...>` mentions, strips NUL/surrogates, and truncates
    safely without wrapping everything in code blocks.
    """
    if s is None:
        return ""
    base = escape_markdown(s) if escape_md else _normalize_unicode_safe(str(s))
    return sanitize_text(base, limit) or ""


def safe_http_url(url: str | None) -> str | None:
    """Validate that a hyperlink URL strictly uses `http` or `https` with a non-empty host.

    Rejects `javascript:`, `data:`, `file:`, `vbscript:`, `discord:`, whitespace, control
    characters, and unbalanced parentheses/brackets that could break Markdown `[label](url)`.
    """
    if not url:
        return None
    cleaned = _normalize_unicode_safe(str(url)).strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        return None
    if any(c in cleaned for c in ("\r", "\n", "\t", " ", '"', "'", "<", ">", "\\", "(", ")", "[", "]")):
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        return None
    return cleaned


# Backwards-compatible internal alias
_safe_http_url = safe_http_url


def format_safe_link(label: str, url: str | None, *, fallback: str = "") -> str:
    """Format a safe Discord Markdown hyperlink `[label](url)` if `url` is valid `http(s)`, else fallback."""
    valid_url = safe_http_url(url)
    clean_label = (sanitize_text(label, 120) or "Link").replace("[", "\\[").replace("]", "\\]")
    if not valid_url:
        return sanitize_text(fallback, 200) or clean_label
    return f"[{clean_label}]({valid_url})"


def resolve_embed_color(
    settings: Settings,
    *,
    status: EmbedStatus = "normal",
    color: int | None = None,
) -> int:
    """Resolve the active embed color from guild `settings.embed_accent_color` and semantic `status`."""
    accent = int(getattr(settings, "embed_accent_color", 0xA64DFF))
    return resolve_semantic_color(accent, status=status, override_color=color)


def format_footer(
    settings: Settings,
    *segments: str | None,
    page: int | None = None,
    total_pages: int | None = None,
) -> str:
    """Format a restrained, deduplicated footer such as `Instigate Cafe • Jobs • Page 1/3`."""
    brand = (getattr(settings, "embed_footer_text", "") or "Instigate Cafe").strip()
    raw_parts: list[str] = [brand]
    for seg in segments:
        if seg:
            for sub in str(seg).split("•"):
                cleaned = sub.strip()
                if cleaned:
                    raw_parts.append(cleaned)
    if page is not None and total_pages is not None and int(total_pages) >= 1:
        raw_parts.append(f"Page {max(1, int(page))}/{max(1, int(total_pages))}")

    deduped: list[str] = []
    seen_lower: set[str] = set()
    for p in raw_parts:
        low = p.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            deduped.append(p)
    return _truncate(" • ".join(deduped), _MAX_TITLE) or " "


def stale_data_notice(
    source: str,
    last_updated_unix: int | None = None,
    *,
    is_stale: bool = True,
    reason: str | None = None,
) -> str:
    """Generate an accessible, standardized badge/notice indicating cached or stale provider data."""
    if not is_stale and last_updated_unix is None:
        return ""

    parts: list[str] = []
    if is_stale:
        parts.append(f"⚠️ **[Stale / Cached Data]** Sourced from {source}.")
    else:
        parts.append(f"ℹ️ **[Cached Data]** Sourced from {source}.")

    if last_updated_unix and last_updated_unix > 0:
        parts.append(f"Last updated: <t:{last_updated_unix}:R> (<t:{last_updated_unix}:f>).")

    if reason:
        parts.append(f"*{reason}*")
    elif is_stale:
        parts.append("*Live provider is currently unavailable or refresh timed out; showing cached record.*")

    return " ".join(parts)


def disabled_feature_embed(
    settings: Settings,
    feature_name: str,
    *,
    module_id: str | None = None,
    admin_tip: bool = True,
    category: str | None = None,
    status: EmbedStatus = "disabled",
) -> discord.Embed:
    """Create a standardized, supportive embed when a requested command belongs to a disabled feature."""
    cat_prefix = f" • {category}" if category else ""
    title = f"Module Disabled{cat_prefix}"
    clean_feature = sanitize_text(feature_name, 100) or "This feature"

    lines = [
        f"The **{clean_feature}** module is currently turned off in this server.",
        "",
        "This feature can be enabled by a server administrator.",
    ]
    if admin_tip:
        mod_hint = f" `enable:{module_id}`" if module_id else ""
        lines.append("")
        lines.append(
            f"🔧 **Server Admins:** Use `/ic admin features{mod_hint}` or `/ic admin config` to enable it."
        )

    return system(
        settings,
        title=title,
        summary="\n".join(lines),
        footer_hint="Feature Governance",
        status=status,
    )


def empty_state_embed(
    settings: Settings,
    *,
    title: str,
    message: str,
    suggestion: str | None = None,
    category: str = "System",
    status: EmbedStatus = "normal",
) -> discord.Embed:
    """Standardized empty state embed with clear text and recovery guidance."""
    clean_title = sanitize_text(title, _MAX_TITLE) or "No Results"
    clean_msg = sanitize_text(message, 1000) or "No entries were found."
    lines = [clean_msg]
    if suggestion:
        clean_sugg = sanitize_text(suggestion, 500)
        lines.append("")
        lines.append(f"💡 **Tip:** {clean_sugg}")

    return _base_embed(
        settings,
        title=clean_title,
        kind=EMBED_KIND_SYSTEM,
        description="\n".join(lines),
        footer_hint=category,
        status=status,
    )


def paginated_embed(
    settings: Settings,
    *,
    title: str,
    description: str | None = None,
    fields: Iterable[Field] = (),
    page: int = 0,
    total_pages: int = 1,
    kind: str = EMBED_KIND_SYSTEM,
    footer_hint: str | None = None,
    subtitle: str | None = None,
    status: EmbedStatus = "normal",
    color: int | None = None,
) -> discord.Embed:
    """Standard paginated embed automatically including page indicators in footer."""
    page_safe = max(0, page) + 1
    total_safe = max(1, total_pages)
    hint_parts = []
    if footer_hint:
        hint_parts.append(footer_hint)
    hint_parts.append(f"Page {page_safe}/{total_safe}")
    full_hint = " • ".join(hint_parts)

    e = _base_embed(
        settings,
        title=title,
        kind=kind,
        description=description,
        subtitle=subtitle,
        footer_hint=full_hint,
        status=status,
        color=color,
    )
    _add_fields(e, fields)
    return e


def safe_allowed_mentions() -> discord.AllowedMentions:
    """Return an AllowedMentions instance that prevents all outbound pings (bot/client default)."""
    return discord.AllowedMentions.none()


def alert_role_allowed_mentions(role_id: int | None = None) -> discord.AllowedMentions:
    """Return an explicit AllowedMentions exception permitting ONLY a single admin-configured alert role.

    When `role_id` is None, 0, boolean, or not a valid Discord snowflake, returns
    `discord.AllowedMentions.none()`. Never permits `@everyone`, `@here`, or user pings.
    """
    if role_id is None or isinstance(role_id, bool) or not isinstance(role_id, int):
        return discord.AllowedMentions.none()
    if not _is_valid_snowflake(role_id, allow_zero=False) or role_id > MAX_SNOWFLAKE:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=int(role_id))],
        replied_user=False,
    )


def _truncate(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    if limit <= 0:
        return ""
    s = _normalize_unicode_safe(str(s))
    if len(s) <= limit:
        return s
    if limit == 1:
        return "…"
    cut = s[: limit - 1]
    # Avoid leaving a dangling zero-width joiner, variation selector, or孤立 combining mark at the cut point
    while len(cut) > 1 and (
        cut[-1] in ("\u200d", "\ufe0f", "\ufe0e") or unicodedata.category(cut[-1]).startswith("M")
    ):
        cut = cut[:-1]
    return cut + "…"


def _footer_text(settings: Settings, kind: str, footer_hint: str | None) -> str:
    kind_label = {
        EMBED_KIND_STOCK: "Stock Exchange",
        EMBED_KIND_GAME: "Casino",
        EMBED_KIND_SYSTEM: "System",
        EMBED_KIND_ALERTS: "Alerts",
        EMBED_KIND_JOBS: "Jobs",
    }.get(kind, "Info")

    brand = getattr(settings, "embed_footer_text", "") or "Instigate Cafe"
    raw_segments = [brand, kind_label]
    if footer_hint:
        for seg in str(footer_hint).split("•"):
            cleaned = seg.strip()
            if cleaned:
                raw_segments.append(cleaned)

    deduped: list[str] = []
    seen_lower: set[str] = set()
    for p in raw_segments:
        if not p:
            continue
        low = p.strip().lower()
        if low and low not in seen_lower:
            seen_lower.add(low)
            deduped.append(p.strip())
    return " • ".join(deduped)


def _base_embed(
    settings: Settings,
    *,
    title: str,
    kind: str,
    description: str | None = None,
    subtitle: str | None = None,
    footer_hint: str | None = None,
    status: EmbedStatus = "normal",
    color: int | None = None,
    thumbnail_url: str | None = None,
    timestamp: datetime | None = None,
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

    resolved_color = resolve_embed_color(settings, status=status, color=color)
    e = discord.Embed(
        title=clean_title,
        description=clean_desc,
        color=resolved_color,
    )

    # Consistent brand header with optional small icon_url (no duplicate right-side thumbnail)
    author_name = getattr(settings, "bot_display_name", "") or settings.embed_footer_text or "Instigate Cafe"
    author_kwargs: dict[str, str] = {
        "name": sanitize_text(author_name, _MAX_TITLE) or "Instigate Cafe",
    }
    logo = safe_http_url(getattr(settings, "embed_logo_url", ""))
    if logo:
        author_kwargs["icon_url"] = logo
    e.set_author(**author_kwargs)

    # Set thumbnail ONLY when explicitly provided (e.g., user avatar or server icon),
    # avoiding duplicate brand logo thumbnails that squeeze inline fields.
    safe_thumb = safe_http_url(thumbnail_url)
    if safe_thumb:
        e.set_thumbnail(url=safe_thumb)

    # Restrained footer; attach wall-clock timestamp only when explicitly requested
    footer_val = _footer_text(settings, kind, footer_hint)
    e.set_footer(text=sanitize_text(footer_val, _MAX_TITLE) or " ")
    if timestamp is not None:
        e.timestamp = timestamp
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
    status: EmbedStatus = "normal",
    color: int | None = None,
    thumbnail_url: str | None = None,
    timestamp: datetime | None = None,
) -> discord.Embed:
    e = _base_embed(
        settings,
        title=title,
        kind=EMBED_KIND_SYSTEM,
        description=summary,
        subtitle=subtitle,
        footer_hint=footer_hint,
        status=status,
        color=color,
        thumbnail_url=thumbnail_url,
        timestamp=timestamp,
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

    headline = sanitize_untrusted_text(title_text or title or "", 240) or "New Job Posting"
    company_raw = _display_label(company or "") or _display_label(source or "")
    company_label = sanitize_untrusted_text(company_raw, 120)

    safe_url = safe_http_url(url)

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
        fields.append(("📍 Location", sanitize_untrusted_text(location, 120) or "Not Listed", True))
    if career_level and career_level != "unknown":
        lvl_display = sanitize_untrusted_text(career_level.replace("_", " ").title(), 60)
        fields.append(("🎓 Level", lvl_display, True))

    # Source / ATS provider
    src_display = provider or source
    if src_display:
        clean_src = (
            src_display.split(":", 1)[0].capitalize() if ":" in src_display else src_display.capitalize()
        )
        fields.append(("📡 Source", sanitize_untrusted_text(clean_src, 60), True))

    # Posted date / timestamp
    if posted_date:
        fields.append(("📅 Posted", sanitize_untrusted_text(posted_date, 60) or "", True))
    elif posted_unix and posted_unix > 0:
        fields.append(("📅 Posted", f"<t:{int(posted_unix)}:R>", True))

    if preset and preset != "all":
        fields.append(("🎯 Preset", f"`{sanitize_untrusted_text(preset, 40)}`", True))

    if label_list:
        safe_tags = [sanitize_untrusted_text(t, 40) for t in label_list[:8] if t]
        tag_str = " • ".join(t for t in safe_tags if t)
        if tag_str:
            fields.append(("🏷️ Tags", _truncate(tag_str, 200) or "", False))

    if safe_url:
        fields.append(("🔗 Apply", format_safe_link("Apply Here", safe_url), False))

    _add_fields(e, fields)
    return e


class EmbedFactory:
    """Lightweight factory binding guild-resolved `Settings` to the shared embed builders."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    async def for_interaction(cls, interaction: discord.Interaction[Any]) -> EmbedFactory:
        from icbot.container import resolve_guild_settings

        resolved = await resolve_guild_settings(interaction.client, interaction)
        return cls(resolved)

    def system(
        self,
        *,
        title: str,
        summary: str | None = None,
        fields: Iterable[Field] = (),
        footer_hint: str | None = None,
        subtitle: str | None = None,
        status: EmbedStatus = "normal",
        color: int | None = None,
    ) -> discord.Embed:
        return system(
            self.settings,
            title=title,
            summary=summary,
            fields=fields,
            footer_hint=footer_hint,
            subtitle=subtitle,
            status=status,
            color=color,
        )

    def paginated(
        self,
        *,
        title: str,
        description: str | None = None,
        fields: Iterable[Field] = (),
        page: int = 0,
        total_pages: int = 1,
        kind: str = EMBED_KIND_SYSTEM,
        footer_hint: str | None = None,
        subtitle: str | None = None,
        status: EmbedStatus = "normal",
    ) -> discord.Embed:
        return paginated_embed(
            self.settings,
            title=title,
            description=description,
            fields=fields,
            page=page,
            total_pages=total_pages,
            kind=kind,
            footer_hint=footer_hint,
            subtitle=subtitle,
            status=status,
        )

    def empty_state(
        self,
        *,
        title: str,
        message: str,
        suggestion: str | None = None,
        category: str = "System",
    ) -> discord.Embed:
        return empty_state_embed(
            self.settings,
            title=title,
            message=message,
            suggestion=suggestion,
            category=category,
        )

    def disabled_feature(
        self,
        feature_name: str,
        *,
        module_id: str | None = None,
        admin_tip: bool = True,
        category: str | None = None,
    ) -> discord.Embed:
        return disabled_feature_embed(
            self.settings,
            feature_name,
            module_id=module_id,
            admin_tip=admin_tip,
            category=category,
        )

    def job_alert(self, **kwargs: Any) -> discord.Embed:
        return job_alert(self.settings, **kwargs)

    def ttc_alert(self, **kwargs: Any) -> discord.Embed:
        return ttc_alert(self.settings, **kwargs)

    def go_alert(self, **kwargs: Any) -> discord.Embed:
        return go_alert(self.settings, **kwargs)
