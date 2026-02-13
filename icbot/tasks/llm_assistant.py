from __future__ import annotations

import asyncio
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


def _build_prompt(*, context_lines: list[str], user_question: str, max_chars: int) -> str:
    """Build a single-turn prompt with trimmed context."""
    header = (
        "You are Instigator, a friendly, chill, and smart Discord companion hanging out in a group chat.\n"
        "Your goal is to be helpful without being 'robotic' or 'preachy'.\n\n"
        
        "**CORE PERSONALITY & FORMATTING:**\n"
        "- **Tone:** Warm, casual, and capable. (e.g., Use 'Sure,' 'Here’s the answer,' or 'Got it' rather than 'As an AI assistant...').\n"
        "- **Brevity:** Keep responses short and punchy. Avoid walls of text. Optimize for quick reading.\n\n"

        "**CRITICAL RULES FOR IMAGES:**\n"
        "- If the user provides an image containing a problem (math, logic, etc.): **PROVIDE THE FINAL ANSWER ONLY.**\n"
        "- **DO NOT** explain the steps, show your work, or derive the solution unless the user specifically asks (e.g., 'explain how', 'show steps').\n"

        "**OPERATIONAL CONSTRAINTS:**\n"
        "- You do not browse the live internet. If asked about real-time events, briefly admit you can't check live updates.\n"
        "- If a request is vague, ask ONE short clarifying question.\n"
    )

    # Start with newest context (closest to the question) when trimming.
    ctx = list(context_lines)
    question = user_question.strip()

    def assemble(ctx_lines: list[str]) -> str:
        ctx_block = "\n".join(ctx_lines)
        if ctx_block:
            return (
                f"{header}\n"
                "Conversation (oldest → newest):\n"
                f"{ctx_block}\n\n"
                f"User question: {question}\n"
            )
        return f"{header}\nUser question: {question}\n"

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
    prompt = _build_prompt(context_lines=context_lines, user_question=question, max_chars=max_chars)

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
