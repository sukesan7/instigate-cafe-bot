from __future__ import annotations

import asyncio
import re
import time
from typing import Iterable

import discord

from icbot.config import Settings
from icbot.services.gemini_client import GeminiError, gemini_generate_text


_MENTION_RE_TEMPLATE = r"<@!?{bot_id}>"


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
        content = (m.content or "").strip()
        if not content:
            continue
        # Keep each line reasonably short; long walls make the model worse.
        if len(content) > 800:
            content = content[:800] + " …"
        lines.append(f"{author}: {content}")
    return lines


def _build_prompt(*, context_lines: list[str], user_question: str, max_chars: int) -> str:
    """Build a single-turn prompt with trimmed context."""
    header = (
        "You are Instigator, a helpful Discord assistant for the Instigate Cafe server.\n"
        "Use recent chat context only when it is relevant or when the user asks about what was said.\n"
        "You may answer using general knowledge even if the recent chat context doesn't mention the topic.\n"
        "Be concise, correct, and practical. If there is missing context, ask 1 short clarifying question.\n"
        "You do NOT browse the internet\n."
        "For time-sensitive questions (latest/so far/today), say you cannot verify live updates and offer a general explanation or ask for a source link.\n"
        "Do NOT invent details that aren't in the chat.\n"
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

    # History excludes the current message (we add the question separately)
    history_limit = max(10, min(100, max_msgs * 2))
    collected: list[discord.Message] = []
    try:
        async for m in message.channel.history(limit=history_limit, before=message.created_at, oldest_first=True):
            if m.author.bot:
                continue
            if not (m.content or "").strip():
                continue
            collected.append(m)
    except Exception:
        collected = []

    # Keep only the last (max_msgs - 1) context messages
    if max_msgs > 1 and len(collected) > (max_msgs - 1):
        collected = collected[-(max_msgs - 1) :]

    context_lines = _format_context_lines(collected)
    prompt = _build_prompt(context_lines=context_lines, user_question=question, max_chars=max_chars)

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
