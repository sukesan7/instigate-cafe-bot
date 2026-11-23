"""Unit tests for least-privilege startup Gateway intent policy and graceful degradation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

import icbot.bot as bot_mod
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.help import ic_help
from icbot.guild_config import GuildSettingsStore
from icbot.tasks import llm_assistant as llm_task
from tests.helpers import FakeMessage, FakeTextChannel, FrozenClock, build_fake_member

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("label", "settings_kwargs", "expect_message_content"),
    [
        ("slash_only_defaults", {}, False),
        (
            "api_key_without_llm_enabled",
            {"gemini_api_key": "synthetic-key-only", "llm_enabled": False},
            False,
        ),
        (
            "nondefault_profile_student_core",
            {"feature_profile": "student_core"},
            False,
        ),
        (
            "nondefault_profile_community_full",
            {"feature_profile": "community_full", "gemini_api_key": "synthetic-key"},
            False,
        ),
        (
            "enabled_ai_module_without_channel_history",
            {"enabled_features": ["ai"], "gemini_api_key": "synthetic-key"},
            False,
        ),
        (
            "assistant_enabled_direct_mentions_only",
            {
                "llm_enabled": True,
                "gemini_api_key": "synthetic-key",
                "llm_channel_history_enabled": False,
            },
            False,
        ),
        (
            "channel_history_flag_while_assistant_disabled",
            {
                "llm_enabled": False,
                "llm_channel_history_enabled": True,
            },
            False,
        ),
        (
            "assistant_and_channel_history_startup_opt_in",
            {
                "llm_enabled": True,
                "llm_channel_history_enabled": True,
            },
            True,
        ),
    ],
)
def test_startup_intent_policy_matrix(
    label: str,
    settings_kwargs: dict[str, Any],
    expect_message_content: bool,
) -> None:
    """Startup intent policy defaults to least privilege and never enables privileged intents silently."""
    settings = Settings(db_path=":memory:", **settings_kwargs)
    policy = bot_mod.resolve_intent_policy(settings)
    intents = bot_mod.build_intents(settings)

    assert isinstance(policy, bot_mod.IntentPolicy), label
    assert policy.guilds is True
    assert policy.messages is True
    assert policy.members is False
    assert policy.presences is False
    assert policy.message_content is expect_message_content
    assert policy.explicit_override is False

    # Intents object matches policy exactly
    assert intents.guilds is True
    assert intents.messages is True
    assert intents.guild_messages is True
    assert intents.dm_messages is True
    assert intents.members is False
    assert intents.presences is False
    assert intents.message_content is expect_message_content

    expected_priv = ("message_content",) if expect_message_content else ()
    assert policy.privileged_intents == expected_priv


def test_build_intents_zero_arg_defaults_to_zero_privileged_intents() -> None:
    """Calling build_intents() with no Settings defaults strictly to least privilege."""
    intents = bot_mod.build_intents()
    assert intents.guilds is True
    assert intents.messages is True
    assert intents.message_content is False
    assert intents.members is False
    assert intents.presences is False


async def test_campus_bot_applies_startup_settings_intent_policy_and_explicit_override(
    tmp_path: Path,
) -> None:
    """CampusBot resolves intents from startup Settings before super().__init__ and honors explicit intents."""
    default_settings = Settings(db_path=str(tmp_path / "default.db"))
    bot_default = bot_mod.create_bot(default_settings, register_commands=False, sync_commands=False)
    try:
        assert bot_default.intents.guilds is True
        assert bot_default.intents.messages is True
        assert bot_default.intents.message_content is False
        assert bot_default.intents.members is False
        assert bot_default.intents.presences is False
        assert bot_default.intent_policy.privileged_intents == ()
        assert bot_default.intent_policy.explicit_override is False
    finally:
        await bot_default.close()

    history_settings = Settings(
        db_path=str(tmp_path / "history.db"),
        llm_enabled=True,
        llm_channel_history_enabled=True,
    )
    bot_history = bot_mod.create_bot(history_settings, register_commands=False, sync_commands=False)
    try:
        assert bot_history.intents.message_content is True
        assert bot_history.intents.members is False
        assert bot_history.intents.presences is False
        assert bot_history.intent_policy.privileged_intents == ("message_content",)
        assert bot_history.intent_policy.explicit_override is False
    finally:
        await bot_history.close()

    # Explicit injected intents override startup inference cleanly
    custom_intents = discord.Intents.none()
    custom_intents.guilds = True
    bot_custom = bot_mod.create_bot(
        history_settings,
        intents=custom_intents,
        register_commands=False,
        sync_commands=False,
    )
    try:
        assert bot_custom.intents.guilds is True
        assert bot_custom.intents.messages is False
        assert bot_custom.intents.message_content is False
        assert bot_custom.intent_policy.explicit_override is True
        assert bot_custom.intent_policy.privileged_intents == ()
    finally:
        await bot_custom.close()


async def test_on_message_has_no_message_counter_and_routes_mentions_without_message_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary messages do not increment counters or run prefix commands; @mentions route without message_content."""
    settings = Settings(
        db_path=str(tmp_path / "mentions.db"),
        llm_enabled=True,
        gemini_api_key="synthetic-key",
        llm_channel_history_enabled=False,
    )
    bot = bot_mod.create_bot(settings, register_commands=False, sync_commands=False)
    assert bot.intents.message_content is False
    assert bot.intents.messages is True

    mention_calls: list[Any] = []
    process_calls = 0

    async def _spy_mention(client: Any, message: Any) -> bool:
        mention_calls.append(message)
        return True

    async def _spy_process(message: Any) -> None:
        nonlocal process_calls
        process_calls += 1

    monkeypatch.setattr("icbot.bot.maybe_handle_llm_mention", _spy_mention)
    monkeypatch.setattr(bot, "process_commands", _spy_process)

    fake_bot_user = SimpleNamespace(id=999000000000000001, display_name="InstigateAI")
    bot._connection.user = fake_bot_user  # type: ignore[assignment]
    channel = FakeTextChannel(channel_id=900000000000000020, name="general")
    author = build_fake_member(user_id=800000000000000001, display_name="Alice")
    author.bot = False

    # 1. Ordinary non-mention chat message: no message counter, no prefix dispatch, no LLM call
    plain_msg: Any = FakeMessage(content="just chatting", channel=channel, author=author)
    plain_msg.mentions = []
    plain_msg.reference = None
    await bot.on_message(plain_msg)
    assert mention_calls == []
    assert process_calls == 0

    # 2. Explicit @mention message: routed even with intents.message_content=False (mention exception)
    mention_msg: Any = FakeMessage(
        content="<@999000000000000001> hello there",
        channel=channel,
        author=author,
    )
    mention_msg.mentions = [fake_bot_user]
    mention_msg.reference = None
    await bot.on_message(mention_msg)
    assert len(mention_calls) == 1
    assert process_calls == 0

    await bot.close()


async def test_per_guild_channel_history_override_degrades_gracefully_without_message_content_intent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When process started without MESSAGE_CONTENT, per-guild channel history degrades cleanly while @mention works."""
    db_file = tmp_path / "guild_override_history.db"
    db = Database(str(db_file))
    await db.init()

    captured_prompts: list[str] = []

    async def _mock_gemini(**kwargs: Any) -> str:
        captured_prompts.append(kwargs.get("prompt", ""))
        return "Direct mention reply works without channel history."

    # Process startup settings: least privilege (no channel history -> message_content=False)
    startup_settings = Settings(
        db_path=str(db_file),
        gemini_api_key="synthetic-key",
        llm_enabled=True,
        llm_channel_history_enabled=False,
    )
    guild_store = GuildSettingsStore(db)
    await guild_store.set_overrides(
        900000000000000111,
        {"llm_enabled": True, "llm_channel_history_enabled": True},
    )

    container = ServiceContainer(
        settings=startup_settings,
        db=db,
        clock=FrozenClock(1_700_000_000.0),
        monotonic_clock=FrozenClock(100.0),
        gemini_generate_fn=_mock_gemini,
    )
    container.guild_settings = guild_store
    bot = bot_mod.create_bot(
        startup_settings,
        db=db,
        services=container,
        register_commands=False,
        sync_commands=False,
    )
    assert bot.intents.message_content is False

    history_called = False

    class _SpyChannel(FakeTextChannel):
        class _Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: Any):
                return False

        def typing(self):
            return self._Typing()

        async def history(self, **kwargs: Any):
            nonlocal history_called
            history_called = True
            yield FakeMessage(content="Should not be read without MESSAGE_CONTENT intent")

    class _MentionMsg(FakeMessage):
        def __init__(self, **kw: Any):
            super().__init__(**kw)
            self.reply_calls: list[str] = []

        async def reply(self, content: str, **kwargs: Any) -> FakeMessage:
            self.reply_calls.append(content)
            return self

    fake_bot_user = SimpleNamespace(id=999000000000000001, display_name="InstigateAI")
    bot._connection.user = fake_bot_user  # type: ignore[assignment]
    channel = _SpyChannel(channel_id=900000000000000222, name="general")
    author = build_fake_member(user_id=800000000000000001, display_name="Alice")
    author.bot = False
    msg: Any = _MentionMsg(
        content="<@999000000000000001> What is our exam timezone?",
        channel=channel,
        author=author,
    )
    msg.guild = discord.Object(id=900000000000000111)
    msg.mentions = [fake_bot_user]
    msg.reference = None

    try:
        with caplog.at_level(logging.WARNING, logger="icbot.tasks.llm_assistant"):
            handled = await llm_task.maybe_handle_llm_mention(bot, msg)

        assert handled is True
        assert history_called is False
        assert len(captured_prompts) == 1
        assert "<channel_history>" not in captured_prompts[0]
        assert "What is our exam timezone?" in captured_prompts[0]
        assert len(msg.reply_calls) == 1
        assert any(
            "MESSAGE_CONTENT" in rec.message and "restart" in rec.message.lower() for rec in caplog.records
        )
    finally:
        await bot.close()


async def test_channel_history_works_when_startup_enabled_message_content(tmp_path: Path) -> None:
    """When process started with LLM_ENABLED=true and LLM_CHANNEL_HISTORY_ENABLED=true, history is read."""
    captured_prompts: list[str] = []

    async def _mock_gemini(**kwargs: Any) -> str:
        captured_prompts.append(kwargs.get("prompt", ""))
        return "Reply with channel context."

    startup_settings = Settings(
        db_path=str(tmp_path / "with_history.db"),
        gemini_api_key="synthetic-key",
        llm_enabled=True,
        llm_channel_history_enabled=True,
    )
    container = ServiceContainer(
        settings=startup_settings,
        clock=FrozenClock(1_700_000_000.0),
        monotonic_clock=FrozenClock(100.0),
        gemini_generate_fn=_mock_gemini,
    )
    bot = bot_mod.create_bot(
        startup_settings,
        services=container,
        register_commands=False,
        sync_commands=False,
    )
    assert bot.intents.message_content is True

    class _HistoryChannel(FakeTextChannel):
        class _Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: Any):
                return False

        def typing(self):
            return self._Typing()

        async def history(self, **kwargs: Any):
            bob = build_fake_member(user_id=800000000000000002, display_name="Bob")
            yield FakeMessage(content="Prior context line", author=bob)

    class _MentionMsg(FakeMessage):
        async def reply(self, content: str, **kwargs: Any) -> FakeMessage:
            return self

    fake_bot_user = SimpleNamespace(id=999000000000000001, display_name="InstigateAI")
    bot._connection.user = fake_bot_user  # type: ignore[assignment]
    author = build_fake_member(user_id=800000000000000001, display_name="Alice")
    author.bot = False
    msg: Any = _MentionMsg(
        content="<@999000000000000001> summarize above",
        channel=_HistoryChannel(channel_id=900000000000000222, name="general"),
        author=author,
    )
    msg.guild = discord.Object(id=900000000000000111)
    msg.mentions = [fake_bot_user]
    msg.reference = None

    try:
        handled = await llm_task.maybe_handle_llm_mention(bot, msg)
        assert handled is True
        assert "<channel_history>" in captured_prompts[0]
        assert "Bob: Prior context line" in captured_prompts[0]
    finally:
        await bot.close()


def test_missing_developer_portal_approval_fails_early_with_precise_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing Discord Developer Portal approval raises actionable SystemExit without losing gateway safeguard."""
    import main as main_mod

    monkeypatch.setenv("DISCORD_TOKEN", "synthetic-token-value")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "portal.db"))
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_CHANNEL_HISTORY_ENABLED", "true")
    monkeypatch.setenv("ICBOT_ALLOW_GATEWAY", "false")

    # 1. Gateway safeguard still blocks before bot.run()
    with pytest.raises(SystemExit, match="ICBOT_ALLOW_GATEWAY=true"):
        main_mod.main([])

    # 2. With --allow-gateway, PrivilegedIntentsRequired yields clear operator guidance
    created_bots: list[Any] = []

    def _raise_privileged(self: Any, token: str, **kwargs: Any) -> None:
        created_bots.append(self)
        raise discord.PrivilegedIntentsRequired(shard_id=0)

    monkeypatch.setattr(bot_mod.CampusBot, "run", _raise_privileged)

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main(["--allow-gateway"])

    msg = str(exc_info.value)
    assert "Message Content Intent" in msg
    assert "Developer Portal" in msg
    assert "LLM_CHANNEL_HISTORY_ENABLED=false" in msg
    assert "restart" in msg.lower()
    assert len(created_bots) == 1
    assert created_bots[0].intents.message_content is True
    asyncio.run(created_bots[0].close())

    # Also verify helper directly
    direct = bot_mod.format_privileged_intents_error(bot_mod.resolve_intent_policy(Settings()))
    assert "Developer Portal" in direct


def test_channel_history_without_llm_enabled_warns_in_config_validation() -> None:
    """Settings.validate() warns when LLM_CHANNEL_HISTORY_ENABLED=true while LLM_ENABLED=false."""
    s = Settings(db_path=":memory:", llm_enabled=False, llm_channel_history_enabled=True)
    report = s.validate(strict=False)
    assert report.is_valid is True
    assert any(
        w.field == "llm_channel_history_enabled" and "LLM_ENABLED" in w.message for w in report.warnings
    )


async def test_slash_commands_help_and_no_double_startup_without_message_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_interaction,
) -> None:
    """Slash commands (/ic help) and idempotent setup_hook/on_ready work with zero privileged intents."""
    settings = Settings(
        db_path=str(tmp_path / "slash_only.db"),
        guild_id=900000000000000001,
        task_startup_jitter_seconds=0.0,
    )
    bot = bot_mod.create_bot(settings, register_commands=True, sync_commands=True)
    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False

    sync_calls = 0

    async def _fake_sync(*, guild=None):
        nonlocal sync_calls
        sync_calls += 1
        return []

    monkeypatch.setattr(bot.tree, "sync", _fake_sync)

    try:
        # Idempotent startup: repeated setup_hook & on_ready must never double-init or double-sync
        await bot.setup_hook()
        await bot.setup_hook()
        bot._handle_ready()
        await bot.on_ready()
        await bot.on_ready()

        assert bot.setup_complete is True
        assert sync_calls == 1
        assert bot.ready_count == 2
        guild_obj = discord.Object(id=settings.guild_id)
        assert [c.name for c in bot.tree.get_commands(guild=guild_obj)] == ["ic"]

        # /ic help works cleanly through interaction with intents.message_content=False
        inter = make_interaction(command_name="ic help", settings=settings, client=bot)
        await ic_help.callback(inter)
        assert inter.last_reply is not None
        assert inter.last_reply.embed is not None
        assert "Main Utilities" in (inter.last_reply.embed.title or "")
    finally:
        await bot.close()


def test_security_and_deployment_docs_match_actual_intent_policy() -> None:
    """Verify SECURITY.md, docs/deployment.md, and docs/configuration.md accurately describe startup intents."""
    security = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    deployment = (PROJECT_ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    configuration = (PROJECT_ROOT / "docs/configuration.md").read_text(encoding="utf-8")

    assert "zero privileged intents" in security.lower()
    assert "LLM_CHANNEL_HISTORY_ENABLED=true" in security
    assert "LLM_CHANNEL_HISTORY_ENABLED=true" in deployment
    assert "restart" in deployment.lower()
    assert "Message Content Intent" in configuration
