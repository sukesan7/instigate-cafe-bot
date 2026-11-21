# Instigate Cafe Bot — Discord UI & Embed Visual Style Contract

**Primary Modules:** [`icbot/embeds.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/embeds.py), [`icbot/theme.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/theme.py), [`icbot/guild_config.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/guild_config.py), [`icbot/container.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/container.py)

---

## 1. Visual Hierarchy & Embed Anatomy

Every Discord embed built by the bot follows a restrained, predictable layout:

1. **Author Header (`embed.author`)**:
   - Displays the guild's resolved `bot_display_name` (default `"InstigateAI"`, or preset override such as `"TMU Campus Bot"`).
   - Uses `embed_logo_url` as the small `author.icon_url` when configured and validated as `http(s)`.
2. **Concise Title (`embed.title`)**:
   - Action- or subject-first title (max 256 characters, ideally $\le 80$ characters).
   - Avoids stacking redundant status icons or repeating the author name.
3. **Summary Before Detail Fields (`embed.description`)**:
   - Optional italic `subtitle` (e.g., `*Timezone: America/Toronto*`) followed by a 1–3 sentence overview or filter badge summary before detail fields.
4. **Detail Fields (`embed.fields`)**:
   - Paginated views aim for **3–5 fields per page** on rich cards (e.g., `/ic jobs` uses 5 postings per page; `/ic notes` groups up to 8 sources per page; `/ic exam list` caps at 10 compact rows per page).
   - Readable Discord timestamp (`<t:UNIX:R>`, `<t:UNIX:F>`) and cache freshness labels (`stale_data_notice`) are included only when time or cache age is relevant to the data.
5. **Thumbnails & Wall-Clock Timestamps**:
   - **No automatic duplicate logo thumbnails:** `_base_embed` does not duplicate `embed_logo_url` into `embed.thumbnail`, preserving horizontal width for inline fields on mobile and desktop. Thumbnails are used only when representing a specific entity (e.g., user avatar in `/ic profile` or guild icon in `/ic serverinfo`).
   - **No noisy timestamps on static cards:** `embed.timestamp` is omitted by default on static help menus, empty states, and disabled-feature notices, and attached only when explicitly provided.
6. **Restrained Footers (`embed.footer.text`)**:
   - Formatted via `format_footer(settings, *segments, page=..., total_pages=...)` or `_footer_text(...)` with automatic case-insensitive segment deduplication:
     - `Instigate Cafe • Jobs • Page 1/3`
     - `TMU Student Hub • System • Feature Governance`
     - `Campus Utility • Alerts • TTC`

---

## 2. Theme Presets & Semantic Color Contract

### 2.1 Theme-Defined Normal Accent & Server-Scoped Resolution
The original Instigate Cafe purple (`0xA64DFF`) remains the default preset (`instigate_cafe`), but is never forced onto guilds using another preset. Both slash commands and scheduled background alerts (`JobsIngestionPipeline.dispatch_outbox`, `ttc_alerts`, `go_alerts`, `market_watch`, `btc_watch`) resolve per-guild effective settings via `await resolve_guild_settings(bot, guild_id)`:

| Preset (`theme_preset`) | Display Name | Normal Accent (`embed_accent_color`) | Footer Brand (`embed_footer_text`) |
| :--- | :--- | :--- | :--- |
| `instigate_cafe` (default) | `InstigateAI` | `0xA64DFF` (Purple) | `Instigate Cafe` |
| `campus_generic` | `Campus Assistant` | `0x2563EB` (Royal Blue) | `Campus Utility` |
| `student_clean` | `Student Bot` | `0x0284C7` (Sky Blue) | `Student Portal` |
| `toronto_tmu` | `TMU Campus Bot` | `0x004C97` (TMU Blue) | `TMU Student Hub` |
| `emerald_academic` | `Academic Bot` | `0x059669` (Emerald Green) | `Academic Commons` |

### 2.2 Named Semantic Status Colors (`EmbedStatus`)
Resolved via `resolve_embed_color(settings, status=..., color=...)` in [`icbot/embeds.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/embeds.py):

| Semantic Status | Color Hex | When Used |
| :--- | :--- | :--- |
| `"normal"` / `"info"` | `settings.embed_accent_color` | Standard informational responses, listings, schedules, and profiles |
| `"success"` | `0x2ECC71` | Completed mutations, confirmed additions, positive alerts |
| `"warning"` | `0xF39C12` | Degraded upstream providers, stale cache fallbacks, destructive confirmation prompts |
| `"error"` | `0xE74C3C` | Validation failures, rejected inputs, upstream errors |
| `"disabled"` | `0x64748B` | Disabled feature modules (`disabled_feature_embed`) |

*(All preset accents and semantic colors fall within the relative luminance range heuristic $0.03 \le L \le 0.95$; see [`icbot/theme.py`](file:///home/sukesan/dev/discord/campus-bot/icbot/theme.py)).*

---

## 3. Builder Boundary Limits & Unicode Truncation

`_base_embed` and `_add_fields` enforce all Discord embed limits before a payload leaves the builder:

- **Title**: $\le 256$ chars (`_MAX_TITLE`)
- **Author Name**: $\le 256$ chars
- **Footer Text**: $\le 256$ chars
- **Description** (including italic `*subtitle*\n\n` prefix): $\le 4096$ chars (`_MAX_DESC`)
- **Field Name**: $\le 256$ chars (`_MAX_FIELD_NAME`)
- **Field Value**: $\le 1024$ chars (`_MAX_FIELD_VALUE`)
- **Field Count**: $\le 25$ fields (`_MAX_FIELDS`)
- **Total Embed Character Budget**: $\le 6000$ chars (`_MAX_TOTAL`, sum of title, description, author, footer, and all field names/values). Fields that would push the embed past 6,000 characters are truncated to the remaining budget or omitted if no budget remains.
- **Unicode-Safe Truncation (`_truncate` & `_normalize_unicode_safe`)**:
  - Strips NUL bytes (`\x00`) and repairs lone UTF-16 surrogates so JSON serialization never raises `UnicodeEncodeError`.
  - Appends a single-character ellipsis (`…`) when truncating and trims trailing zero-width joiners (`\u200d`), variation selectors (`\ufe0f`), or dangling combining marks at the cut boundary so multi-codepoint emoji sequences are not left corrupted.

---

## 4. Markdown, Hyperlink & Mention Safety Contract

The UI layer distinguishes four separate safety mechanisms rather than wrapping all text in code blocks or escaping bot-authored formatting:

1. **Bot-Generated Formatting (`sanitize_text`)**:
   - Used for strings where the bot itself authors Markdown (`**bold**`, `*italic*`, `` `code` ``, `<t:UNIX:R>`).
   - Neutralizes `@everyone`, `@here`, `<@user_id>`, `<@!user_id>`, and `<@&role_id>` by inserting a zero-width space (`\u200b`).
2. **Untrusted External / User Text (`sanitize_untrusted_text` & `escape_markdown`)**:
   - Applies `discord.utils.escape_markdown` first to neutralize `*`, `_`, `` ` ``, `~`, `|`, `>`, and `\` in untrusted provider or user strings (e.g., scraped job titles, company names, locations, or search queries), then runs `sanitize_text` and length truncation.
3. **Safe Hyperlink URLs (`safe_http_url` & `format_safe_link`)**:
   - Permits only `http://` and `https://` URLs with a valid hostname (`netloc` containing `.`) and zero control characters, whitespace, or unbalanced Markdown delimiters (`(`, `)`, `[`, `]`).
   - Rejects `javascript:`, `data:`, `file:`, `vbscript:`, and `discord:` schemes, falling back to plain text.
4. **Client-Wide `discord.AllowedMentions.none()` Default & Configured Alert Role Exception**:
   - `CampusBot.__init__` sets `allowed_mentions=discord.AllowedMentions.none()` at the client default, and `safe_allowed_mentions()` / `safe_send_alert()` enforce `AllowedMentions.none()` on outbound messages.
   - **Intentional Alert Role Exception (`alert_role_allowed_mentions(role_id)`)**: When a server administrator explicitly configures a notification `role_id` on a job alert subscription (`/ic job_subscribe channel:#careers preset:tech_swe role:@Interns`), `JobsIngestionPipeline.dispatch_outbox` passes `content=f"<@&{role_id}>"` with `allowed_mentions=alert_role_allowed_mentions(role_id)`, which permits **only** that single role object (`everyone=False, users=False, roles=[discord.Object(id=role_id)]`).

---

## 5. Text Examples Across Themes (`embed_to_text_snapshot`)

### Example A: Paginated Job Board (`/ic jobs` — Default `instigate_cafe` vs. `toronto_tmu`)

```text
[EMBED]
Author: InstigateAI
Title: 💼 Campus Job Board • Page 1/3
Description: 🎯 **Preset**: `Software & Tech` • 📊 **Matches**: `12` • 🕒 **Cached**: `5m ago`

ℹ️ *Displaying verified listings from Greenhouse, Lever, and Workday portals.*
Color: 0xA64DFF
Fields (5):
  - Software Engineer Intern — Stripe: 📍 `Toronto, ON` • 🎓 `Intern` • 📡 `Greenhouse`
📅 Posted: <t:1700000000:R> • 🔗 [Apply on Stripe](https://example.invalid/greenhouse/Stripe/id-0)
  ...
Footer: Instigate Cafe • Jobs • Page 1/3
```

When the guild configures `theme_preset="toronto_tmu"` via `/ic admin config theme:toronto_tmu`:

```text
[EMBED]
Author: TMU Campus Bot
Title: 💼 Campus Job Board • Page 1/3
Description: 🎯 **Preset**: `Software & Tech` • 📊 **Matches**: `12` • 🕒 **Cached**: `5m ago`

ℹ️ *Displaying verified listings from Greenhouse, Lever, and Workday portals.*
Color: 0x004C97
Fields (5):
  - Software Engineer Intern — Stripe: 📍 `Toronto, ON` • 🎓 `Intern` • 📡 `Greenhouse`
📅 Posted: <t:1700000000:R> • 🔗 [Apply on Stripe](https://example.invalid/greenhouse/Stripe/id-0)
  ...
Footer: TMU Student Hub • Jobs • Page 1/3
```

### Example B: Empty State & Disabled Feature Cards

```text
[EMBED]
Author: Campus Assistant
Title: No Jobs Found
Description: No postings matched your criteria.

💡 **Tip:** Try searching for a broader term like 'developer' or clearing location filters.
Color: 0x2563EB
Footer: Campus Utility • System • Careers
```

```text
[EMBED]
Author: TMU Campus Bot
Title: Module Disabled • Gaming
Description: The **Cafe Casino** module is currently turned off in this server.

This feature can be enabled by a server administrator.

🔧 **Server Admins:** Use `/ic admin features enable:casino` or `/ic admin config` to enable it.
Color: 0x64748B
Footer: TMU Student Hub • System • Feature Governance
```
