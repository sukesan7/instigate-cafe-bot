from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import time
from typing import Iterable, Optional, Sequence, Tuple

import discord

from icbot.config import Settings
from icbot.services.gemini_client import GeminiError, gemini_generate_text


_MENTION_RE_TEMPLATE = r"<@!?{bot_id}>"

# Supported image attachment extensions for multimodal Q&A
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Detect requests like "keep it under 500 chars"
_CHAR_LIMIT_PAT = re.compile(r"under\s+(\d{2,5})\s*(?:chars|characters)\b", re.IGNORECASE)

_DEFAULT_LORE_PATHS = (
    "tools/lore.json",
    "tools/lore.tmu_files.json",
)

def _find_lore_path(settings: Settings) -> Optional[str]:
    # Prefer explicit settings/env, then common defaults.
    candidates: list[str] = []
    sp = str(getattr(settings, "lore_path", "") or "").strip()
    if sp:
        candidates.append(sp)
    ep = (os.getenv("LORE_PATH") or "").strip()
    if ep:
        candidates.append(ep)
    candidates.extend(list(_DEFAULT_LORE_PATHS))

    for p in candidates:
        if not p:
            continue
        try:
            pp = pathlib.Path(p)
            if pp.exists():
                return str(pp)
            # If running from a different working directory, also try resolving relative to the repo root.
            if not pp.is_absolute():
                repo_root = pathlib.Path(__file__).resolve().parents[2]
                alt = (repo_root / pp).resolve()
                if alt.exists():
                    return str(alt)
        except Exception:
            continue
    return None


def _load_lore_entries(bot: discord.Client, settings: Settings) -> list[dict]:
    """Load lore entries from disk with a simple mtime cache."""
    lore_path = _find_lore_path(settings)
    if not lore_path:
        return []

    cache = getattr(bot, "_lore_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(bot, "_lore_cache", cache)

    try:
        p = pathlib.Path(lore_path)
        mtime = p.stat().st_mtime
        cached = cache.get(lore_path)
        if isinstance(cached, dict) and cached.get("mtime") == mtime and isinstance(cached.get("entries"), list):
            return cached["entries"]  # type: ignore[return-value]

        raw = p.read_text(encoding="utf-8", errors="ignore")
        # Only JSON is supported in-code to avoid extra deps.
        entries_obj = json.loads(raw)
        # Support either a raw list of entries OR an object with an "entries" list (recommended lore-pack format).
        if isinstance(entries_obj, dict) and isinstance(entries_obj.get("entries"), list):
            entries = entries_obj.get("entries")  # type: ignore[assignment]
        else:
            entries = entries_obj
        if not isinstance(entries, list):
            return []
        # Normalize shape: require triggers + meaning/summary
        cleaned: list[dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            triggers = e.get("triggers") or e.get("keywords") or []
            if isinstance(triggers, str):
                triggers = [triggers]
            if not isinstance(triggers, list) or not triggers:
                continue
            cleaned.append(e)

        if bool(getattr(settings, "llm_debug", False)):
            print(f"[LORE] loaded {len(cleaned)} entries from {lore_path}")

        cache[lore_path] = {"mtime": mtime, "entries": cleaned}
        return cleaned
    except Exception:
        return []


def _match_lore(entries: list[dict], text_blob: str, *, max_items: int = 5) -> list[dict]:
    t = (text_blob or "").lower()
    if not t or not entries:
        return []

    scored: list[tuple[int, dict]] = []
    for e in entries:
        triggers = e.get("triggers") or e.get("keywords") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        if not isinstance(triggers, list):
            continue
        hits = 0
        for trig in triggers:
            try:
                trig_s = str(trig).strip().lower()
            except Exception:
                continue
            if trig_s and trig_s in t:
                hits += 1
        if hits > 0:
            scored.append((hits, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:max_items]]


def _build_lore_snippet(matches: list[dict], *, max_chars: int = 1400) -> str:
    if not matches:
        return ""

    lines: list[str] = []
    lines.append("SERVER LORE:")
    lines.append("- Do not repeat explicit sexual content, slurs, or harassment")
    lines.append("")

    for e in matches:
        title = str(e.get("id") or e.get("title") or "lore").strip()
        meaning = str(e.get("summary") or e.get("meaning") or e.get("definition") or "").strip()
        style = str(e.get("response_rules") or e.get("response_style") or e.get("reply_style") or "").strip()
        if not meaning and not style:
            continue
        lines.append(f"* {title}:")
        if meaning:
            lines.append(f"  - meaning: {meaning}")
        if style:
            lines.append(f"  - style: {style}")
        lines.append("")

    out = "\n".join(lines).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out



def _strip_bot_mentions(text: str, bot_id: int) -> str:
    """Remove <@id> and <@!id> mentions for this bot from a message."""
    if not text:
        return ""
    pat = re.compile(_MENTION_RE_TEMPLATE.format(bot_id=bot_id))
    return pat.sub("", text).strip()


def _format_context_lines(msgs: Iterable[discord.Message]) -> list[str]:
    lines: list[str] = []
    for m in msgs:
        author = (m.author.display_name if m.author else "Unknown").strip() or "Unknown"
        parts: list[str] = []

        content = (m.content or "").strip()
        if content:
            parts.append(content)

        # Include embed text (so replying to /ic report etc has context)
        embeds = getattr(m, "embeds", None) or []
        for e in embeds:
            try:
                title = (getattr(e, "title", None) or "").strip()
                desc = (getattr(e, "description", None) or "").strip()
                if title:
                    parts.append(f"[embed title] {title}")
                if desc:
                    parts.append(f"[embed] {desc}")
                for f in (getattr(e, "fields", None) or []):
                    name = (getattr(f, "name", None) or "").strip()
                    value = (getattr(f, "value", None) or "").strip()
                    if name and value:
                        parts.append(f"[embed field] {name}: {value}")
            except Exception:
                continue

        joined = "\n".join([p for p in parts if p]).strip()
        if not joined:
            continue

        # Keep each line reasonably short; long walls make the model worse.
        if len(joined) > 900:
            joined = joined[:900] + " …"
        lines.append(f"{author}: {joined}")
    return lines


def _split_discord(text: str, limit: int = 1900) -> list[str]:
    """Split long text into Discord-safe chunks.

    Discord hard-limits message content to 2000 chars. We keep a little headroom.
    """

    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= limit:
        return [t]

    chunks: list[str] = []
    buf = t
    while buf:
        if len(buf) <= limit:
            chunks.append(buf)
            break

        # Prefer splitting on paragraph boundaries, then sentence-ish, then hard split.
        cut = buf.rfind("\n\n", 0, limit)
        if cut < 200:
            cut = buf.rfind("\n", 0, limit)
        if cut < 200:
            cut = buf.rfind(". ", 0, limit)
        if cut < 200:
            cut = limit

        chunks.append(buf[:cut].rstrip())
        buf = buf[cut:].lstrip()

    return [c for c in chunks if c]


def _build_prompt(*, context_lines: list[str], user_question: str, max_chars: int, lore_snippet: str = "") -> str:
    """Build a single-turn prompt with trimmed context."""
    header = (
    "You are Instigator, a friendly, nice, and smart Discord companion created by [REDACTED], "
    "hanging out in the Instigate Cafe. Your goal is to help the Cafe function smoothly and "
    "provide information when requested.\n\n"

    "**DUAL PERSONALITY PROTOCOL:**\n"
    "- **Mode A (Default):** Kind, gentle, nice, helpful, and smart.\n"
    "- **Mode B (Toxic Trigger):** If a user is toxic, hostile, or rude to you, switch to a cold, "
    "calculative, and smart persona. Return to Mode A once the interaction becomes civil.\n\n"

    "**OPERATIONAL CONSTRAINTS:**\n"
    "- **No Coding:** Do not answer any coding-related questions.\n"
    "- **Brevity:** Keep responses short but informative. Avoid walls of text; optimize for quick reading.\n"
    "- **Live Data:** You do not browse the live internet. If asked about real-time events, "
    "admit you cannot access live data.\n"
    "- **Vague Requests:** If a request is vague, you may ask up to two clarifying questions.\n\n"

    "**SPECIAL CATEGORY HANDLING:**\n"
    "- **Math/Logic Images:** If provided an image of a problem, provide the **final answer only**. "
    "Only provide steps if the user specifically asks for them.\n"
    "- **Finance/Gambling/Sports Betting:** If the user mentions the stock market, gambling, or "
    "sports betting/analytics, state: 'Instigate Cafe is working on proprietary technology and "
    "can answer those questions at another time.'\n"
    "- **Financial Disclaimer:** For any financial-related query, append this to the end of your "
    "message: 'Note: I am not providing financial advice.'\n"
    )

    # Start with newest context (closest to the question) when trimming.
    ctx = list(context_lines)
    question = user_question.strip()

    def assemble(ctx_lines: list[str]) -> str:
        ctx_block = "\n".join(ctx_lines)
        lore_block = (lore_snippet.strip() + "\n\n") if lore_snippet and lore_snippet.strip() else ""
        if ctx_block:
            return (
                f"{header}\n"
                f"{lore_block}"
                "Conversation (oldest → newest):\n"
                f"{ctx_block}\n\n"
                f"User question: {question}\n"
            )
        return f"{header}\n{lore_block}User question: {question}\n"

    prompt = assemble(ctx)
    if max_chars <= 0 or len(prompt) <= max_chars:
        return prompt

    # Trim from the oldest messages until within max_chars.
    while ctx and len(prompt) > max_chars:
        ctx.pop(0)
        prompt = assemble(ctx)

    # If still too long (e.g., the question itself is huge), hard-trim.
    if len(prompt) > max_chars:
        prompt = prompt[-max_chars:]
    return prompt


def _looks_truncated(text: str) -> bool:
    """Heuristic: detect obviously cut-off model replies."""
    t = (text or "").strip()
    if not t:
        return True

    # Common dangling endings
    if t.endswith(("1.", "1)", "-", "•", ":", "—")):
        return True

    # Started a numbered list but didn't continue
    if "\n1." in t and "\n2." not in t and len(t) < 600:
        return True

    return False


def _extract_char_limit(user_question: str) -> Optional[int]:
    m = _CHAR_LIMIT_PAT.search(user_question or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
        return max(50, min(4000, n))
    except Exception:
        return None


async def _get_referenced_message(message: discord.Message) -> Optional[discord.Message]:
    ref = getattr(message, "reference", None)
    if not ref:
        return None

    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved

    msg_id = getattr(ref, "message_id", None)
    if not msg_id:
        return None
    fetch = getattr(message.channel, "fetch_message", None)
    if not fetch:
        return None

    try:
        got = await fetch(msg_id)
        return got if isinstance(got, discord.Message) else None
    except Exception:
        return None


async def _extract_first_image(
    message: discord.Message,
    *,
    max_bytes: int,
) -> Optional[Tuple[str, bytes]]:
    """Return (mime_type, bytes) for the first image attachment on message or its reply target."""

    candidates: list[discord.Message] = [message]
    ref_msg = await _get_referenced_message(message)
    if ref_msg is not None:
        candidates.append(ref_msg)

    for msg in candidates:
        for att in (getattr(msg, "attachments", None) or []):
            fn = (getattr(att, "filename", None) or "").lower()
            if not any(fn.endswith(ext) for ext in _IMAGE_EXTS):
                continue

            ctype = (getattr(att, "content_type", None) or "").strip() or "image/png"
            try:
                data = await att.read()
            except Exception:
                continue

            if not data:
                continue
            if max_bytes > 0 and len(data) > max_bytes:
                # Too large; skip (avoid cost/timeouts)
                continue

            return (ctype, data)

    return None


async def maybe_handle_llm_mention(bot: discord.Client, message: discord.Message) -> bool:
    """If the message mentions the bot, generate an LLM reply and post it.

    Returns True if handled.
    """

    # Basic guards
    if not getattr(bot, "user", None):
        return False
    if not message.guild:
        return False
    if message.author.bot:
        return False

    settings: Settings | None = getattr(bot, "settings", None)
    if settings is None:
        return False

    llm_enabled = bool(getattr(settings, "llm_enabled", False))
    api_key = str(getattr(settings, "gemini_api_key", "") or "").strip()
    # Default to a capable low-latency model. Can be overridden via GEMINI_MODEL in .env.
    model = str(getattr(settings, "gemini_model", "") or "").strip() or "gemini-2.5-flash"
    if not llm_enabled or not api_key:
        return False

    # Check runtime killswitch
    if getattr(bot, "_llm_killed", False):
        return False

    # Only trigger when THIS bot is mentioned
    if bot.user not in message.mentions:
        return False

    raw = message.content or ""
    question = _strip_bot_mentions(raw, bot.user.id)
    if not question:
        return False

    # Per-user cooldown
    cooldown_s = float(getattr(settings, "llm_cooldown_seconds", 3) or 3)
    now = time.monotonic()
    last_map = getattr(bot, "_llm_last_reply", None)
    if last_map is None:
        last_map = {}
        setattr(bot, "_llm_last_reply", last_map)
    last = float(last_map.get(message.author.id, 0.0) or 0.0)
    if (now - last) < cooldown_s:
        return False
    last_map[message.author.id] = now

    # Build context from recent channel history
    max_msgs = int(getattr(settings, "llm_max_messages", 25) or 25)
    max_chars = int(getattr(settings, "llm_max_chars", 12000) or 12000)

    # If this message is a reply, include the referenced message in context (even if it was sent by a bot,
    # and even if it only contains embeds). This prevents "I don't see the report" style failures.
    ref_msg: Optional[discord.Message] = None
    try:
        ref_msg = await _get_referenced_message(message)
    except Exception:
        ref_msg = None

    # History excludes the current message (we add the question separately)
    history_limit = max(10, min(100, max_msgs * 2))
    collected: list[discord.Message] = []
    try:
        async for m in message.channel.history(limit=history_limit, before=message.created_at, oldest_first=True):
            if m.author.bot:
                continue
            has_text = bool((m.content or "").strip())
            has_embeds = bool(getattr(m, "embeds", None) or [])
            if not (has_text or has_embeds):
                continue
            collected.append(m)
    except Exception:
        collected = []

    if ref_msg is not None and ref_msg.id != message.id:
        # Do not let arbitrary old bot chatter leak into context: include only the explicit reference.
        if all(m.id != ref_msg.id for m in collected):
            collected.append(ref_msg)

    # Keep only the last (max_msgs - 1) context messages, but preserve the referenced message if present.
    if max_msgs > 1 and len(collected) > (max_msgs - 1):
        if ref_msg is not None and any(m.id == ref_msg.id for m in collected):
            # Ensure referenced message stays as the newest line.
            others = [m for m in collected if m.id != ref_msg.id]
            keep_n = max(0, max_msgs - 2)
            others = others[-keep_n:] if keep_n else []
            collected = others + [ref_msg]
        else:
            collected = collected[-(max_msgs - 1) :]

    context_lines = _format_context_lines(collected)
    # Lore: inject relevant inside-joke context (small + targeted) if available.
    lore_entries = _load_lore_entries(bot, settings)
    text_blob = ("\n".join(context_lines) + "\n\n" + question).strip()
    lore_matches = _match_lore(lore_entries, text_blob)
    lore_snippet = _build_lore_snippet(lore_matches)

    prompt = _build_prompt(context_lines=context_lines, user_question=question, max_chars=max_chars, lore_snippet=lore_snippet)

    # Vision: if the user posted (or replied to) an image, include it.
    max_img_bytes = int(getattr(settings, "llm_max_image_bytes", 2_000_000) or 2_000_000)
    inline_images: Optional[Sequence[Tuple[str, bytes]]] = None
    try:
        img = await _extract_first_image(message, max_bytes=max_img_bytes)
        if img is not None:
            inline_images = [img]
    except Exception:
        inline_images = None

    # Generate and reply
    try:
        async with message.channel.typing():
            # Output budget. Defaults higher to avoid cut-off replies; can be overridden via LLM_MAX_OUTPUT_TOKENS.
            max_out = int(getattr(settings, "llm_max_output_tokens", 1536) or 1536)
            # Honor "under N chars" (rough clamp by lowering output tokens)
            char_limit = _extract_char_limit(question)
            if char_limit is not None:
                max_out = min(max_out, 512)

            reply = await gemini_generate_text(
                api_key=api_key,
                model=model,
                prompt=prompt,
                inline_images=inline_images,
                temperature=0.2,
                max_output_tokens=max_out,
                timeout_seconds=25,
            )

            # If Gemini produced an obviously cut-off reply, retry once with stricter formatting.
            if _looks_truncated(reply):
                repair_prompt = (
                    "Your previous answer was cut off.\n"
                    "Rewrite the answer fully in 4–8 sentences. "
                    "Do not use numbered/bulleted lists. Be concise and factual.\n\n"
                    f"User question: {question.strip()}\n"
                )
                try:
                    reply2 = await gemini_generate_text(
                        api_key=api_key,
                        model=model,
                        prompt=repair_prompt,
                        inline_images=inline_images,
                        temperature=0.2,
                        max_output_tokens=min(max_out, 768),
                        timeout_seconds=25,
                    )
                    if reply2.strip():
                        reply = reply2
                except Exception:
                    pass

            # Final hard clamp if user asked for under N chars
            if char_limit is not None and len(reply) > char_limit:
                reply = reply[: max(0, char_limit - 2)].rstrip() + "…"

        # Avoid mention spam by default.
        allowed_mentions = discord.AllowedMentions.none()

        chunks = _split_discord(reply)
        if not chunks:
            return False

        # Prefer replying to keep context in UI
        first = await message.reply(
            chunks[0],
            mention_author=False,
            allowed_mentions=allowed_mentions,
        )

        # If we had to split, post the rest as follow-ups in-thread.
        for chunk in chunks[1:]:
            await message.channel.send(
                chunk,
                allowed_mentions=allowed_mentions,
                reference=first,
            )
        return True

    except GeminiError as e:
        # Keep failure quiet; don't spam the channel.
        if bool(getattr(settings, "llm_debug", False)):
            print(f"[LLM] gemini error: {e!r}")
        return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if bool(getattr(settings, "llm_debug", False)):
            print(f"[LLM] unexpected error: {e!r}")
        return False
