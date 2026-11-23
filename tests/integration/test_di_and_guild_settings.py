"""Integration tests for ServiceContainer DI, per-guild settings isolation, and error redaction."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from discord import app_commands

from icbot.bot import CampusBot, create_bot
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.errors import ConfigValidationError, ProviderError
from icbot.features import info as info_feat
from icbot.features import notes as notes_feat
from icbot.features import profile as profile_feat
from icbot.features import report as report_feat
from icbot.features import resume as resume_feat
from icbot.features import sports as sports_feat
from icbot.guild_config import (
    PROCESS_ONLY_KEYS,
    SECRET_SETTING_KEYS,
    GuildSettings,
    GuildSettingsStore,
    resolve_effective_settings,
    validate_guild_overrides,
)
from icbot.services.btc_esplora import AddressTx
from icbot.services.crypto_fng import FearGreed
from icbot.services.gemini_client import GeminiError
from icbot.services.yahoo_quotes import Quote
from icbot.tasks import btc_watch as btc_watch_task
from icbot.tasks import llm_assistant as llm_task
from icbot.tasks import market_daily_report as daily_report_task
from icbot.tasks import market_watch as market_watch_task
from tests.helpers import FakeMessage, FakeTextChannel, FrozenClock, build_fake_member

pytestmark = [pytest.mark.discord_mock, pytest.mark.storage]


def _synth_gemini_key() -> str:
    return "synth-gem-" + "secret-di-key-123"


def _synth_odds_key() -> str:
    return "synth-odds-" + "secret-di-key-456"


def _make_base_settings(tmp_path: Path, db_name: str = "di_test.db", **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "token": "synth-bot-" + "secret-tok-001",
        "guild_id": 900000000000000001,
        "stock_channel_id": 900000000000000010,
        "user_command_channel_id": 900000000000000020,
        "allowed_role_ids": [900000000000000090],
        "embed_accent_color": 0xA64DFF,
        "embed_footer_text": "Default Footer",
        "daily_claim_amount": 100,
        "bj_starting_bankroll": 1000,
        "db_path": str(tmp_path / db_name),
        "lore_path": "tests/fixtures/synthetic_lore.json",
        "notes_sites": ["Default Notes|https://notes.default.invalid/"],
        "gemini_api_key": _synth_gemini_key(),
        "odds_api_key": "",
        "llm_enabled": False,
        "allow_gateway": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


async def test_guild_settings_validation_and_crud_isolation(tmp_db: Database, tmp_path: Path):
    clock = FrozenClock(1_760_100_000.0)
    store = GuildSettingsStore(tmp_db, clock=clock)
    guild_a = 900000000000000101
    guild_b = 900000000000000102

    # Secret keys, process-only keys, invalid guild IDs, and unknown keys must be rejected
    for secret_key in SECRET_SETTING_KEYS:
        with pytest.raises(ConfigValidationError, match="Secret key"):
            validate_guild_overrides({secret_key: "secret-val"})

    for proc_key in ("db_path", "grades_db_path", "allow_gateway", "go_ssl_verify"):
        assert proc_key in PROCESS_ONLY_KEYS
        with pytest.raises(ConfigValidationError, match="Process-level key"):
            validate_guild_overrides({proc_key: "override-val"})

    with pytest.raises(ConfigValidationError, match="Unknown or non-overridable"):
        validate_guild_overrides({"not_a_real_setting": 123})

    with pytest.raises(ConfigValidationError, match="guild_id"):
        GuildSettings(guild_id=0, overrides={"daily_claim_amount": 200})

    with pytest.raises(ConfigValidationError, match="embed_accent_color"):
        await store.set_overrides(guild_a, {"embed_accent_color": False})

    # CRUD lifecycle for two distinct guilds
    saved_a = await store.set_overrides(
        guild_a,
        {
            "embed_footer_text": "Alpha Cafe",
            "embed_accent_color": "#11AA22",
            "daily_claim_amount": 250,
            "allowed_role_ids": [900000000000000191],
        },
    )
    assert saved_a.updated_at == 1_760_100_000
    assert saved_a.overrides["embed_accent_color"] == 0x11AA22

    clock.advance(60)
    saved_b = await store.set_overrides(
        guild_b,
        {
            "embed_footer_text": "Beta Cafe",
            "daily_claim_amount": 500,
        },
    )
    assert saved_b.updated_at == 1_760_100_060

    loaded_a = await store.get(guild_a)
    assert loaded_a is not None
    assert loaded_a.overrides["embed_footer_text"] == "Alpha Cafe"

    clock.advance(30)
    updated_a = await store.update_overrides(
        guild_a,
        {"embed_footer_text": None, "bj_starting_bankroll": 3000},
    )
    assert "embed_footer_text" not in updated_a.overrides
    assert updated_a.overrides["bj_starting_bankroll"] == 3000

    base = _make_base_settings(tmp_path)
    eff_a = resolve_effective_settings(base, updated_a)
    eff_b = await store.resolve_for_guild(base, guild_b)
    assert eff_a.daily_claim_amount == 250
    assert eff_a.bj_starting_bankroll == 3000
    assert eff_a.embed_footer_text == "Default Footer"
    assert eff_a.db_path == base.db_path
    assert eff_a.token == base.token
    assert eff_b.daily_claim_amount == 500
    assert eff_b.embed_footer_text == "Beta Cafe"

    await store.clear(guild_a)
    assert await store.get(guild_a) is None

    # Corrupt stored JSON raises ConfigValidationError cleanly
    await tmp_db.execute(
        "INSERT OR REPLACE INTO guild_settings (guild_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (guild_a, "{corrupt json", 1234),
    )
    with pytest.raises(ConfigValidationError, match="Corrupt guild_settings JSON"):
        await store.get(guild_a)


async def test_multi_guild_commands_channel_gate_and_caches_stay_isolated(
    tmp_path: Path, make_interaction, mock_aiohttp
):
    clock = FrozenClock(1_760_200_000.0)
    base = _make_base_settings(tmp_path, user_command_channel_id=900000000000000020)
    db = Database(base.db_path)
    bot = create_bot(
        base,
        db=db,
        clock=clock,
        register_commands=False,
        sync_commands=False,
    )
    await bot.setup_hook()

    guild_a = SimpleNamespace(id=900000000000000101, name="Guild Alpha")
    guild_b = SimpleNamespace(id=900000000000000102, name="Guild Beta")
    assert bot.services.guild_settings is not None

    await bot.services.guild_settings.set_overrides(
        guild_a.id,
        {
            "user_command_channel_id": 900000000000000201,
            "allowed_role_ids": [900000000000000301],
            "embed_footer_text": "Alpha Footer",
            "embed_accent_color": 0x102030,
            "daily_claim_amount": 150,
            "notes_sites": ["AlphaNotes|https://alpha.notes.invalid/"],
            "sports_rss_nba": "https://alpha.rss.invalid/nba.xml",
        },
    )
    await bot.services.guild_settings.set_overrides(
        guild_b.id,
        {
            "user_command_channel_id": 900000000000000202,
            "allowed_role_ids": [900000000000000302],
            "embed_footer_text": "Beta Footer",
            "embed_accent_color": 0x405060,
            "daily_claim_amount": 400,
            "notes_sites": ["BetaNotes|https://beta.notes.invalid/"],
            "sports_rss_nba": "https://beta.rss.invalid/nba.xml",
        },
    )

    mock_aiohttp.get(
        "https://alpha.notes.invalid/",
        text='<a href="https://example.notion.site/CPS530-Alpha">Alpha</a>',
    )
    mock_aiohttp.get(
        "https://beta.notes.invalid/",
        text='<a href="https://example.notion.site/CPS530-Beta">Beta</a>',
    )
    mock_aiohttp.get(
        "https://alpha.rss.invalid/nba.xml",
        text="<rss><channel><item><title>Alpha Hoops</title><link>https://alpha.rss.invalid/1</link></item></channel></rss>",
    )
    mock_aiohttp.get(
        "https://beta.rss.invalid/nba.xml",
        text="<rss><channel><item><title>Beta Hoops</title><link>https://beta.rss.invalid/1</link></item></channel></rss>",
    )

    try:
        # 1. Per-guild channel gate and allowed_role_ids
        ch_a = FakeTextChannel(channel_id=900000000000000201, name="alpha-cmds")
        ch_b = FakeTextChannel(channel_id=900000000000000202, name="beta-cmds")

        def _make_ch_inter(
            *,
            guild: Any,
            channel: Any,
            user: Any,
            command_name: str = "ic test",
        ) -> Any:
            inter = make_interaction(client=bot, guild=guild, user=user, command_name=command_name)
            inter.channel = channel
            inter.channel_id = channel.id
            return inter

        member_plain_a = build_fake_member(user_id=800000000000000101, role_ids=[])
        denied_a = _make_ch_inter(
            guild=guild_a, channel=ch_b, user=member_plain_a, command_name="ic dailyclaim"
        )
        assert await bot.tree.interaction_check(denied_a) is False  # type: ignore[arg-type]
        assert f"<#{ch_a.id}>" in (denied_a.last_reply.content or "")

        member_role_b_in_a = build_fake_member(user_id=800000000000000102, role_ids=[900000000000000302])
        role_mismatch = _make_ch_inter(
            guild=guild_a,
            channel=ch_b,
            user=member_role_b_in_a,
            command_name="ic dailyclaim",
        )
        assert await bot.tree.interaction_check(role_mismatch) is False  # type: ignore[arg-type]

        role_ok_in_b = _make_ch_inter(
            guild=guild_b,
            channel=ch_a,
            user=member_role_b_in_a,
            command_name="ic dailyclaim",
        )
        assert await bot.tree.interaction_check(role_ok_in_b) is True  # type: ignore[arg-type]

        # 2. Per-guild /ic dailyclaim uses isolated daily amounts and branding without mutating base
        user_a = build_fake_member(user_id=800000000000000201, display_name="UserA")
        user_b = build_fake_member(user_id=800000000000000202, display_name="UserB")

        inter_a = _make_ch_inter(guild=guild_a, channel=ch_a, user=user_a)
        await profile_feat.daily_cmd.callback(inter_a)  # type: ignore[arg-type]
        emb_a = inter_a.last_reply.embed
        assert emb_a is not None
        assert "$150" in (emb_a.description or "")
        assert emb_a.color is not None and emb_a.color.value == 0x102030
        assert "Alpha Footer" in (emb_a.footer.text or "")

        inter_b = _make_ch_inter(guild=guild_b, channel=ch_b, user=user_b)
        await profile_feat.daily_cmd.callback(inter_b)  # type: ignore[arg-type]
        emb_b = inter_b.last_reply.embed
        assert emb_b is not None
        assert "$400" in (emb_b.description or "")
        assert emb_b.color is not None and emb_b.color.value == 0x405060
        assert "Beta Footer" in (emb_b.footer.text or "")
        assert bot.settings.daily_claim_amount == 100
        assert bot.settings.embed_footer_text == "Default Footer"

        # 3. NotesCache is partitioned per normalized notes_sites
        notes_inter_a = _make_ch_inter(guild=guild_a, channel=ch_a, user=user_a)
        await notes_feat.notes_cmd.callback(notes_inter_a, "CPS 530")  # type: ignore[arg-type]
        notes_emb_a = notes_inter_a.last_reply.embed
        assert notes_emb_a is not None
        assert any("Alpha" in (f.value or "") for f in notes_emb_a.fields)
        assert not any("Beta" in (f.value or "") for f in notes_emb_a.fields)

        notes_inter_b = _make_ch_inter(guild=guild_b, channel=ch_b, user=user_b)
        await notes_feat.notes_cmd.callback(notes_inter_b, "CPS 530")  # type: ignore[arg-type]
        notes_emb_b = notes_inter_b.last_reply.embed
        assert notes_emb_b is not None
        assert any("Beta" in (f.value or "") for f in notes_emb_b.fields)
        assert not any("Alpha" in (f.value or "") for f in notes_emb_b.fields)

        # 4. Sports degrades cleanly to RSS-only when ODDS_API_KEY="" and isolates RSS per guild
        sports_inter_a = _make_ch_inter(guild=guild_a, channel=ch_a, user=user_a)
        await sports_feat.nba.callback(sports_inter_a)  # type: ignore[arg-type]
        s_emb_a = sports_inter_a.last_reply.embed
        assert s_emb_a is not None
        assert "ODDS_API_KEY is not configured" in (s_emb_a.fields[0].value or "")
        assert "Alpha Hoops" in (s_emb_a.fields[1].value or "")

        sports_inter_b = _make_ch_inter(guild=guild_b, channel=ch_b, user=user_b)
        await sports_feat.nba.callback(sports_inter_b)  # type: ignore[arg-type]
        s_emb_b = sports_inter_b.last_reply.embed
        assert s_emb_b is not None
        assert "Beta Hoops" in (s_emb_b.fields[1].value or "")
        assert "Alpha Hoops" not in (s_emb_b.fields[1].value or "")
        assert not any("the-odds-api.com" in c.url for c in mock_aiohttp.calls)

        # 5. /ic botinfo reports integration readiness cleanly with no secrets
        info_inter = _make_ch_inter(guild=guild_a, channel=ch_a, user=user_a)
        await info_feat.botinfo.callback(info_inter)  # type: ignore[arg-type]
        info_emb = info_inter.last_reply.embed
        assert info_emb is not None
        field_names = [f.name for f in info_emb.fields]
        assert "Integration Readiness" in field_names
        readiness = next(f.value or "" for f in info_emb.fields if f.name == "Integration Readiness")
        assert "Resume Review" in readiness
        assert "Sports Odds" in readiness
        rendered = json.dumps([info_emb.to_dict()])
        for sec in base.secret_values():
            assert sec not in rendered
    finally:
        await bot.close()


async def test_injected_container_providers_power_llm_resume_market_and_btc(tmp_path: Path, make_interaction):
    clock = FrozenClock(1_760_300_000.0)
    monotonic_clock = FrozenClock(500.0)
    gemini_calls: list[dict[str, Any]] = []
    quotes_calls: list[list[str]] = []
    fng_calls = 0
    btc_calls: list[str] = []

    async def _fake_gemini(**kwargs: Any) -> str:
        gemini_calls.append(kwargs)
        if "PASS" in str(kwargs.get("prompt", "")):
            return "PASS"
        return "Strong systems background."

    def _q(sym: str, price: float, chg: float, pct: float) -> Quote:
        return Quote(
            symbol=sym,
            market_state="REGULAR",
            regular_price=price,
            regular_change_pct=pct,
            regular_change=chg,
            pre_price=None,
            pre_change_pct=None,
            pre_change=None,
            post_price=None,
            post_change_pct=None,
            post_change=None,
            previous_close=price - chg,
        )

    async def _fake_quotes(symbols: list[str], *, debug: bool = False) -> dict[str, Quote]:
        quotes_calls.append(list(symbols))
        return {
            "SPY": _q("SPY", 550.0, 15.0, 2.80),
            "QQQ": _q("QQQ", 480.0, 4.0, 0.84),
            "^VIX": _q("^VIX", 24.5, 2.1, 9.3),
            "AAPL": _q("AAPL", 210.0, 5.0, 2.44),
        }

    async def _fake_fng() -> FearGreed:
        nonlocal fng_calls
        fng_calls += 1
        return FearGreed(value=62, value_classification="Greed", timestamp=1_760_300_000)

    async def _fake_btc(addr: str, *, base_url: str, limit: int = 5) -> list[AddressTx]:
        btc_calls.append(addr)
        return [
            AddressTx(
                txid="a" * 64,
                confirmed=True,
                block_time=1_760_299_900,
                value_sats=50_000_000,
            )
        ]

    base = _make_base_settings(
        tmp_path,
        market_watch_enabled=True,
        market_watch_channel_id=900000000000000070,
        market_watch_tickers=["AAPL"],
        market_daily_report_enabled=True,
        market_daily_report_channel_id=900000000000000071,
        market_daily_report_tickers=["AAPL"],
        btc_watch_enabled=True,
        btc_watch_channel_id=900000000000000072,
        btc_watch_addresses=["bc1qsyntheticdiwatch000000000000000"],
        llm_enabled=True,
    )
    db = Database(base.db_path)
    services = ServiceContainer.from_settings(
        base,
        db=db,
        clock=clock,
        monotonic_clock=monotonic_clock,
        gemini_generate_fn=_fake_gemini,
        fetch_quotes_fn=_fake_quotes,
        fetch_fear_greed_fn=_fake_fng,
        fetch_address_txs_fn=_fake_btc,
    )
    bot = create_bot(
        base,
        db=db,
        services=services,
        register_commands=False,
        sync_commands=False,
    )
    await bot.setup_hook()

    @dataclass
    class _StubAttachment:
        filename: str = "resume.pdf"
        content_type: str = "application/pdf"
        size: int = 128
        url: str = ""

        async def read(self) -> bytes:
            return b"%PDF-1.4 synthetic resume"

    try:
        # 1. ResumeReviewer uses container.gemini_generate_fn and guild branding
        guild_a = SimpleNamespace(id=900000000000000101, name="Guild Alpha")
        assert bot.services.guild_settings is not None
        await bot.services.guild_settings.set_overrides(
            guild_a.id,
            {"gemini_model": "gemini-custom-guild", "embed_footer_text": "Resume Guild"},
        )
        member = build_fake_member(user_id=800000000000000301, display_name="Applicant")
        inter = make_interaction(client=bot, guild=guild_a, user=member)
        await inter.response.defer(thinking=True)
        await resume_feat.ResumeReviewer.review(inter, _StubAttachment(), "SWE")  # type: ignore[arg-type]
        rev_emb = inter.last_reply.embed
        assert rev_emb is not None
        assert "Strong systems background." in (rev_emb.description or "")
        assert "Resume Guild" in (rev_emb.footer.text or "")
        assert len(gemini_calls) == 2

        # 2. LLM mention uses guild gemini_model, monotonic_clock cooldown, and lore_cache
        bot_user = SimpleNamespace(id=999000000000000001, display_name="Instigator")
        bot._connection.user = bot_user  # type: ignore[assignment]

        class _LlmChannel(FakeTextChannel):
            class _Typing:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            def typing(self):
                return self._Typing()

            async def history(self, **kwargs: Any):
                if False:
                    yield None

        llm_ch = _LlmChannel(channel_id=900000000000000020, name="chat")
        replies: list[str] = []

        class _LlmMessage(FakeMessage):
            def __init__(self, **kw: Any):
                super().__init__(**kw)
                self.guild = guild_a
                self.mentions = [bot_user]
                self.reference = None
                self.created_at = 0

            async def reply(self, content: str, **kwargs: Any) -> FakeMessage:
                replies.append(content)
                return await llm_ch.send(content)

        llm_msg = _LlmMessage(content=f"<@{bot_user.id}> hello there", channel=llm_ch, author=member)
        llm_msg.author.bot = False
        assert await llm_task.maybe_handle_llm_mention(bot, llm_msg) is True  # type: ignore[arg-type]
        assert replies == ["Strong systems background."]
        assert gemini_calls[-1]["model"] == "gemini-custom-guild"
        assert str(base.lore_path) in bot.services.lore_cache or len(bot.services.lore_cache) == 1

        # Second call inside cooldown returns False without invoking Gemini again
        assert await llm_task.maybe_handle_llm_mention(bot, llm_msg) is False  # type: ignore[arg-type]
        monotonic_clock.advance(10)
        assert await llm_task.maybe_handle_llm_mention(bot, llm_msg) is True  # type: ignore[arg-type]

        # 3. /ic report uses injected quotes, fear&greed, and FrozenClock
        report_inter = make_interaction(client=bot, guild=guild_a, user=member)
        await report_feat.report_cmd.callback(report_inter)  # type: ignore[arg-type]
        report_emb = report_inter.last_reply.embed
        assert report_emb is not None
        assert fng_calls == 1
        assert len(quotes_calls) >= 1

        # 4. Background pollers reuse registered FakeTextChannels via get_channel + injected hooks
        mw_ch = FakeTextChannel(channel_id=900000000000000070, name="mw")
        dr_ch = FakeTextChannel(channel_id=900000000000000071, name="daily")
        btc_ch = FakeTextChannel(channel_id=900000000000000072, name="btc")
        channel_map = {mw_ch.id: mw_ch, dr_ch.id: dr_ch, btc_ch.id: btc_ch}
        bot.get_channel = lambda cid: channel_map.get(int(cid))  # type: ignore[method-assign]

        await market_watch_task.poll_and_post_market_watch(bot)
        assert len(mw_ch.sent) == 1
        mw_emb = mw_ch.sent[0].embed
        assert mw_emb is not None
        assert any(f.name == "Triggered" and "VIX elevated" in (f.value or "") for f in mw_emb.fields)

        await daily_report_task.post_daily_market_report(bot)
        assert len(dr_ch.sent) == 1
        assert dr_ch.sent[0].embed is not None

        await btc_watch_task.poll_and_post_btc_watch(bot)
        assert len(btc_ch.sent) == 1
        assert btc_calls == ["bc1qsyntheticdiwatch000000000000000"]
    finally:
        await bot.close()


async def test_two_concurrent_bots_have_isolated_containers_and_close_independently(tmp_path: Path):
    s1 = _make_base_settings(tmp_path, db_name="bot1.db")
    s2 = _make_base_settings(tmp_path, db_name="bot2.db")
    bot1: CampusBot = create_bot(s1, register_commands=False, sync_commands=False)
    bot2: CampusBot = create_bot(s2, register_commands=False, sync_commands=False)

    await bot1.setup_hook()
    await bot2.setup_hook()

    try:
        assert bot1.services is not bot2.services
        assert bot1.services.http_session is not None
        assert bot2.services.http_session is not None
        assert bot1.services.http_session is not bot2.services.http_session
        assert bot1.services.notes_cache is not bot2.services.notes_cache
        assert bot1.services.rss_cache is not bot2.services.rss_cache
        assert bot1.services.bj_sessions is not bot2.services.bj_sessions
        assert bot1.services.jobs_bad_sources is not bot2.services.jobs_bad_sources

        bot1.services.notes_cache.set(("s1",), now=10.0, data={"S1": [("CPS109", "https://example.invalid")]})
        bot1.services.jobs_bad_sources.add("gh:bad")
        bot1.services.llm_killed = True
        assert bot2.services.notes_cache.get(("s1",), now=10.0, ttl_seconds=60.0) is None
        assert bot2.services.jobs_bad_sources == set()
        assert bot2.services.llm_killed is False

        sess1 = bot1.services.http_session
        sess2 = bot2.services.http_session
        await bot1.close()
        assert sess1.closed is True
        assert sess2.closed is False
        assert bot2.db.is_closed is False
    finally:
        await bot2.close()


async def test_app_command_error_handler_and_resume_redacts_secrets(
    tmp_path: Path, make_interaction, caplog: pytest.LogCaptureFixture
):
    gem_secret = _synth_gemini_key()
    odds_secret = _synth_odds_key()
    base = _make_base_settings(
        tmp_path, llm_enabled=True, gemini_api_key=gem_secret, odds_api_key=odds_secret
    )
    bot = create_bot(base, register_commands=False, sync_commands=False)

    @dataclass
    class _Attachment:
        filename: str = "resume.pdf"
        content_type: str = "application/pdf"
        size: int = 64
        url: str = ""

        async def read(self) -> bytes:
            return b"%PDF-1.4 resume"

    async def _failing_gemini(**kwargs: Any) -> str:
        raise GeminiError(
            message=(
                f"HTTP 500 calling https://generativelanguage.example.invalid/m?key={gem_secret} "
                f"Authorization: Bearer {odds_secret}"
            ),
            status=500,
        )

    bot.services.gemini_generate_fn = _failing_gemini
    await bot.setup_hook()

    try:
        inter_err = make_interaction(client=bot, command_name="ic sports nba")
        provider_err = ProviderError(
            f"API failed url=https://api.example.invalid?apiKey={odds_secret} key={gem_secret}"
        )
        inv_err = app_commands.CommandInvokeError(SimpleNamespace(name="nba"), provider_err)  # type: ignore[arg-type]

        with caplog.at_level(logging.WARNING):
            await bot.tree.on_error(inter_err, inv_err)  # type: ignore[arg-type]

        reply_text = inter_err.last_reply.content or ""
        assert reply_text.startswith("❌")
        assert odds_secret not in reply_text
        assert gem_secret not in reply_text
        assert "[REDACTED]" in reply_text
        for rec in caplog.records:
            assert odds_secret not in rec.getMessage()
            assert gem_secret not in rec.getMessage()

        # ResumeReviewer also redacts secrets returned inside GeminiError
        resume_inter = make_interaction(client=bot, command_name="ic resume")
        await resume_inter.response.defer(thinking=True)
        await resume_feat.ResumeReviewer.review(resume_inter, _Attachment(), "SWE")  # type: ignore[arg-type]
        resume_reply = resume_inter.last_reply.content or ""
        assert "Failed to reach Gemini for ATS scan" in resume_reply
        assert gem_secret not in resume_reply
        assert odds_secret not in resume_reply
        assert "[REDACTED]" in resume_reply
    finally:
        await bot.close()
