from __future__ import annotations

import asyncio
import re
import time
from typing import Iterable

import discord

from icbot.config import Settings
from icbot.services.gemini_client import GeminiError, gemini_generate_text


_MENTION_RE_TEMPLATE = r"<@!?{bot_id}>"


# Heuristics for special intents. We keep these lightweight on purpose.
_SUMMARY_INTENT_RE = re.compile(
    r"\b(summarize|summary|recap|tl;dr|tldr|catch\s+me\s+up|what\s+did\s+we\s+talk\s+about)\b",
    re.IGNORECASE,
)
_LAST_N_RE = re.compile(r"\blast\s+(\d{1,3})\s+messages\b", re.IGNORECASE)


def _strip_bot_mentions(text: str, bot_id: int) -> str:
    """Remove <@id> and <@!id> mentions for this bot from a message."""
    if not text:
        return ""
    pat = re.compile(_MENTION_RE_TEMPLATE.format(bot_id=bot_id))
    return pat.sub("", text).strip()


def _embed_to_text(embed: discord.Embed, *, max_chars: int = 3000) -> str:
    """Convert a Discord embed into plain text suitable for LLM context."""
    parts: list[str] = []
    title = (getattr(embed, "title", None) or "").strip()
    if title:
        parts.append(f"Title: {title}")

    desc = (getattr(embed, "description", None) or "").strip()
    if desc:
        parts.append(desc)

    for f in (getattr(embed, "fields", None) or []):
        name = (getattr(f, "name", None) or "").strip()
        value = (getattr(f, "value", None) or "").strip()
        if not (name or value):
            continue
        if name and value:
            parts.append(f"{name}:\n{value}")
        elif name:
            parts.append(name)
        else:
            parts.append(value)

    footer_obj = getattr(embed, "footer", None)
    footer_text = (getattr(footer_obj, "text", None) or "").strip() if footer_obj else ""
    if footer_text:
        parts.append(f"Footer: {footer_text}")

    out = "\n\n".join([p for p in parts if p]).strip()
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars] + " …"
    return out


def _message_to_text(msg: discord.Message, *, max_chars: int = 3500) -> str:
    """Convert a Discord message (content + embeds) into plain text."""
    parts: list[str] = []
    content = (msg.content or "").strip()
    if content:
        parts.append(content)

    embeds = getattr(msg, "embeds", None) or []
    for e in embeds:
        try:
            et = _embed_to_text(e, max_chars=max_chars)
        except Exception:
            et = ""
        if et:
            parts.append(et)

    out = "\n\n".join([p for p in parts if p]).strip()
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars] + " …"
    return out


def _collapse_ws(text: str) -> str:
    """Collapse whitespace/newlines for compact context lines."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text, flags=re.UNICODE).strip()


def _parse_summary_target(question: str, *, default_n: int = 10, max_n: int = 25) -> int | None:
    """Return N if the question appears to ask for a summary/recap, else None."""
    if not question:
        return None
    if not _SUMMARY_INTENT_RE.search(question):
        return None
    n = default_n
    m = _LAST_N_RE.search(question)
    if m:
        try:
            n = int(m.group(1))
        except Exception:
            n = default_n
    n = max(1, min(max_n, n))
    return n


async def _get_referenced_message(message: discord.Message) -> discord.Message | None:
    """Fetch the message that `message` is replying to, if any."""
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


def _format_context_lines(msgs: Iterable[discord.Message], *, per_msg_max_chars: int = 900) -> list[str]:
    """Format a list of Discord messages into compact one-line context entries.

    Notes:
    - We include embeds (e.g., /ic report) by converting them to text.
    - We include bot messages so follow-ups like "what was the answer?" have continuity.
    """

    lines: list[str] = []
    for m in msgs:
        author = (m.author.display_name if m.author else "Unknown").strip() or "Unknown"
        text = _message_to_text(m, max_chars=per_msg_max_chars)
        text = _collapse_ws(text)
        if not text:
            continue
        if per_msg_max_chars > 0 and len(text) > per_msg_max_chars:
            text = text[:per_msg_max_chars] + " …"
        lines.append(f"{author}: {text}")
    return lines


def _build_prompt(*, context_lines: list[str], user_question: str, max_chars: int, summary_n: int | None = None) -> str:
    """Build a single-turn prompt with trimmed context."""
    if summary_n is not None:
        header = (
            "You are Instigator, a Discord assistant.\n"
            "You are given exactly the last N messages from this channel (N is provided below).\n"
            "Summarize ONLY what is in those messages. Do NOT add facts, assumptions, or outside knowledge.\n"
            "If the user asks for information that is not present in the provided messages, say so.\n"
            "Keep the summary compact and structured.\n"
        )
    else:
        header = (
            "You are Instigator, a Discord assistant.\n"
            "Use recent chat context when it is relevant, and answer direct questions using general knowledge when needed.\n"
            "Never claim something was said unless it appears in the Conversation block.\n"
            "Be concise, correct, and practical. If missing context is required, ask 1 short clarifying question.\n"
            "You do NOT browse the internet.\n"
            "For time-sensitive questions (latest/so far/today), say you cannot verify live updates and offer a general explanation or ask for a source link.\n"
            "Explain reports and metrics; do not provide personalized financial advice.\n"
            "When asked to compute/encode/convert something, provide the final result (not just a description).\n"
        )

    # Start with newest context (closest to the question) when trimming.
    ctx = list(context_lines)
    question = user_question.strip()

    def assemble(ctx_lines: list[str]) -> str:
        ctx_block = "\n".join(ctx_lines)
        if ctx_block:
            n_line = f"N={summary_n}\n" if summary_n is not None else ""
            return (
                f"{header}"
                f"{n_line}"
                "<CONVERSATION>\n"
                f"{ctx_block}\n"
                "</CONVERSATION>\n\n"
                "<QUESTION>\n"
                f"{question}\n"
                "</QUESTION>\n"
            )
        return f"{header}<QUESTION>\n{question}\n</QUESTION>\n"

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
    model = str(getattr(settings, "gemini_model", "") or "").strip() or "gemini-2.0-flash"
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

    # Special handling for “summarize last N messages” style requests.
    summary_n = _parse_summary_target(question, default_n=10, max_n=max_msgs)
    target_n = summary_n if summary_n is not None else max(1, max_msgs - 1)

    # History excludes the current message (we add the question separately)
    history_limit = min(250, max(30, target_n * 4))
    collected: list[discord.Message] = []
    try:
        async for m in message.channel.history(limit=history_limit, before=message.created_at, oldest_first=True):
            # Include bot messages and embeds to preserve continuity (e.g., /ic report, follow-ups).
            if not _message_to_text(m, max_chars=1):
                continue
            collected.append(m)
    except Exception:
        collected = []

    # Keep only the last target_n context messages
    if len(collected) > target_n:
        collected = collected[-target_n:]

    # If the user is replying to a message (e.g., an /ic report embed), include that referenced
    # message in the LLM context so the bot can explain it when explicitly mentioned.
    # For summary requests, we skip this extra line to avoid “N messages” mismatches.
    ref_line = ""
    if summary_n is None:
        try:
            ref_msg = await _get_referenced_message(message)
            if ref_msg is not None:
                ref_author = (ref_msg.author.display_name if ref_msg.author else "Unknown").strip() or "Unknown"
                ref_text = _collapse_ws(_message_to_text(ref_msg, max_chars=1200))
                if ref_text:
                    ref_line = f"Referenced message (user replied to) — {ref_author}: {ref_text}"
        except Exception:
            ref_line = ""

    context_lines = _format_context_lines(collected)
    if ref_line:
        context_lines = [ref_line] + context_lines
    prompt = _build_prompt(context_lines=context_lines, user_question=question, max_chars=max_chars, summary_n=summary_n)

    # Generate and reply
    try:
        async with message.channel.typing():
            reply = await gemini_generate_text(
                api_key=api_key,
                model=model,
                prompt=prompt,
                temperature=0.2,
                max_output_tokens=512,
                timeout_seconds=25,
            )

        # Avoid mention spam by default.
        allowed_mentions = discord.AllowedMentions.none()

        # Prefer replying to keep context in UI
        await message.reply(
            reply,
            mention_author=False,
            allowed_mentions=allowed_mentions,
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
