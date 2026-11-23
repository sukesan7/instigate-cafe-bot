"""Exhaustive parameterized regression tests for single-source-of-truth AI feature control.

Verifies deny-first precedence across:
- GEMINI_API_KEY present vs. absent (key presence NEVER equals operator consent)
- LLM_ENABLED master switch on vs. off (process-level global master disable)
- Feature profiles (default, student_core, toronto_tmu, community_full)
- enabled_features / disabled_features lists (deny-first)
- Administrative killswitch (/ic admin llm_stop -> llm_killed=True)
- Per-guild overrides (cannot bypass process LLM_ENABLED=false; can disable locally)
- DM / channel / role restrictions for @mention assistant
- Direct callback entrypoints: resume_cmd (confirm=False & confirm=True),
  ResumeConfirmView.confirm_button, ResumeReviewer.review, maybe_handle_llm_mention,
  FeatureRegistry, Settings.capabilities(), TaskSupervisor, /ic status, /ic botinfo.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from icbot.config import AIAccessDecision, Settings, resolve_ai_access
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.info import botinfo, status_cmd
from icbot.features.resume import ResumeConfirmView, ResumeReviewer, resume_cmd
from icbot.guild_config import GuildSettings, GuildSettingsStore, resolve_effective_settings
from icbot.registry import check_command_gate, get_feature_registry, guard_feature, is_feature_enabled
from icbot.scheduler import TaskSupervisor, build_default_task_specs
from icbot.tasks import llm_assistant as llm_task
from tests.helpers import (
    FakeClient,
    FakeInteraction,
    FakeMessage,
    FakeTextChannel,
    FrozenClock,
    build_fake_member,
)

pytestmark = pytest.mark.unit


@dataclass
class _SpyAttachment:
    filename: str = "candidate_resume.pdf"
    content_type: str = "application/pdf"
    content: bytes = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    url: str = "https://cdn.discordapp.invalid/attachments/1/2/candidate_resume.pdf"
    read_calls: int = 0

    @property
    def size(self) -> int:
        return len(self.content)

    async def read(self) -> bytes:
        self.read_calls += 1
        return self.content


class _SpyChannel(FakeTextChannel):
    def __init__(self, *, channel_id: int, name: str = "general") -> None:
        super().__init__(channel_id=channel_id, name=name)
        self.history_calls: int = 0

    class _Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

    def typing(self) -> _Typing:
        return self._Typing()

    async def history(self, **kwargs: Any):
        self.history_calls += 1
        yield FakeMessage(content="Prior channel message")


class _MentionMessage(FakeMessage):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        if self.author is not None:
            self.author.bot = False
        self.reply_calls: list[str] = []

    async def reply(self, content: str, **kwargs: Any) -> FakeMessage:
        self.reply_calls.append(content)
        return self


# ---------------------------------------------------------------------------
# 1. Exhaustive Parameterized Policy Matrix (Single Source of Truth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "case_id",
        "llm_enabled",
        "api_key",
        "profile",
        "enabled_features",
        "disabled_features",
        "llm_killed",
        "expect_allowed",
        "expect_enabled",
        "expect_status",
    ),
    [
        # Key presence alone NEVER enables AI under any profile or allowlist
        ("default_all_off", False, "", "default", [], [], False, False, False, "disabled"),
        ("key_only_default_off", False, "AIzaSyKey", "default", [], [], False, False, False, "disabled"),
        (
            "key_only_community_full_off",
            False,
            "AIzaSyKey",
            "community_full",
            [],
            [],
            False,
            False,
            False,
            "disabled",
        ),
        (
            "key_only_explicit_ai_off",
            False,
            "AIzaSyKey",
            "default",
            ["ai"],
            [],
            False,
            False,
            False,
            "disabled",
        ),
        (
            "key_only_student_core_off",
            False,
            "AIzaSyKey",
            "student_core",
            ["ai"],
            [],
            False,
            False,
            False,
            "disabled",
        ),
        # Master switch ON + profile/allowlist matrix (without key -> missing_config)
        ("master_on_default_no_key", True, "", "default", [], [], False, False, True, "missing_config"),
        (
            "master_on_community_full_no_key",
            True,
            "",
            "community_full",
            [],
            [],
            False,
            False,
            True,
            "missing_config",
        ),
        (
            "master_on_explicit_ai_no_key",
            True,
            "",
            "student_core",
            ["ai"],
            [],
            False,
            False,
            True,
            "missing_config",
        ),
        # Feature-profile / denylist deny-first precedence over LLM_ENABLED=true
        (
            "master_on_student_core_denied",
            True,
            "AIzaSyKey",
            "student_core",
            [],
            [],
            False,
            False,
            False,
            "disabled",
        ),
        (
            "master_on_toronto_tmu_denied",
            True,
            "AIzaSyKey",
            "toronto_tmu",
            [],
            [],
            False,
            False,
            False,
            "disabled",
        ),
        (
            "master_on_disabled_list_denied",
            True,
            "AIzaSyKey",
            "default",
            [],
            ["ai"],
            False,
            False,
            False,
            "disabled",
        ),
        (
            "disabled_overrides_enabled_and_full",
            True,
            "AIzaSyKey",
            "community_full",
            ["ai"],
            ["ai"],
            False,
            False,
            False,
            "disabled",
        ),
        # Admin killswitch deny-first precedence
        ("killed_when_ready", True, "AIzaSyKey", "default", [], [], True, False, True, "paused"),
        (
            "killed_when_community_full",
            True,
            "AIzaSyKey",
            "community_full",
            [],
            [],
            True,
            False,
            True,
            "paused",
        ),
        ("killed_when_missing_key", True, "", "default", [], [], True, False, True, "paused"),
        (
            "killed_when_master_off_stays_disabled",
            False,
            "AIzaSyKey",
            "default",
            [],
            [],
            True,
            False,
            False,
            "disabled",
        ),
        (
            "killed_when_module_disabled_stays_disabled",
            True,
            "AIzaSyKey",
            "default",
            [],
            ["ai"],
            True,
            False,
            False,
            "disabled",
        ),
        # Allowed states (master ON + module permitted + not killed + key present)
        ("ready_default_master_on", True, "AIzaSyKey", "default", [], [], False, True, True, "ready"),
        (
            "ready_community_full_master_on",
            True,
            "AIzaSyKey",
            "community_full",
            [],
            [],
            False,
            True,
            True,
            "ready",
        ),
        (
            "ready_student_core_explicit_ai",
            True,
            "AIzaSyKey",
            "student_core",
            ["ai"],
            [],
            False,
            True,
            True,
            "ready",
        ),
    ],
)
def test_ai_policy_matrix_single_source_of_truth(
    case_id: str,
    llm_enabled: bool,
    api_key: str,
    profile: str,
    enabled_features: list[str],
    disabled_features: list[str],
    llm_killed: bool,
    expect_allowed: bool,
    expect_enabled: bool,
    expect_status: str,
) -> None:
    """resolve_ai_access, FeatureRegistry, Settings.capabilities(), and TaskSupervisor all agree 100%."""
    settings = Settings(
        db_path=":memory:",
        llm_enabled=llm_enabled,
        gemini_api_key=api_key,
        feature_profile=profile,
        enabled_features=enabled_features,
        disabled_features=disabled_features,
    )

    # 1. Central typed helper
    decision = resolve_ai_access(settings, llm_killed=llm_killed)
    assert isinstance(decision, AIAccessDecision), case_id
    assert decision.allowed is expect_allowed, case_id
    assert decision.enabled is expect_enabled, case_id
    assert decision.status == expect_status, case_id

    # 2. FeatureRegistry agrees with central helper
    reg = get_feature_registry()
    assert is_feature_enabled("ai", settings) is expect_enabled, case_id
    health = reg.resolve_feature_health("ai", settings, llm_killed=llm_killed)
    assert health.enabled is expect_enabled, case_id
    assert health.available is expect_allowed, case_id
    assert health.status == expect_status, case_id

    # 3. Settings.capabilities() agrees for BOTH llm_assistant and resume_review
    caps = settings.capabilities(llm_killed=llm_killed)
    for cap_name in ("llm_assistant", "resume_review"):
        cap = caps[cap_name]
        assert cap.configured is expect_enabled, f"{case_id}:{cap_name}"
        assert cap.available is expect_allowed, f"{case_id}:{cap_name}"

    # 4. TaskSupervisor agrees on enabled/paused/disabled/idle state
    async def _noop() -> None:
        return None

    sup = TaskSupervisor(
        settings_getter=lambda: settings,
        is_closed=lambda: False,
        wait_until_ready=_noop,
        llm_killed_getter=lambda: llm_killed,
    )
    sup.register_many(
        build_default_task_specs(
            run_ttc=_noop,
            run_go=_noop,
            run_jobs=_noop,
            run_workday=_noop,
            run_market_watch=_noop,
            run_btc_watch=_noop,
            run_daily_report=_noop,
        )
    )
    sup.reconcile(settings)
    llm_health = sup.health_snapshot()["llm_assistant"]
    assert llm_health.enabled is expect_allowed, case_id
    if expect_allowed:
        assert llm_health.state == "idle", case_id
        assert llm_health.paused is False, case_id
    elif expect_status == "paused":
        assert llm_health.state == "paused", case_id
        assert llm_health.paused is True, case_id
    else:
        assert llm_health.state == "disabled", case_id
        assert llm_health.paused is False, case_id


# ---------------------------------------------------------------------------
# 2. Denied Operations Must Not Read Attachments/History or Call Gemini
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "settings_kwargs", "llm_killed", "expect_checklist"),
    [
        ("key_only_master_off", {"llm_enabled": False, "gemini_api_key": "AIzaSyKey"}, False, False),
        (
            "community_full_master_off",
            {"llm_enabled": False, "gemini_api_key": "AIzaSyKey", "feature_profile": "community_full"},
            False,
            False,
        ),
        (
            "disabled_features_ai",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "disabled_features": ["ai"]},
            False,
            False,
        ),
        (
            "student_core_profile",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "feature_profile": "student_core"},
            False,
            False,
        ),
        (
            "admin_llm_stop",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey"},
            True,
            False,
        ),
        (
            "missing_key_when_enabled",
            {"llm_enabled": True, "gemini_api_key": ""},
            False,
            True,
        ),
    ],
)
async def test_resume_all_entrypoints_never_read_attachment_or_call_gemini_when_denied(
    case_id: str,
    settings_kwargs: dict[str, Any],
    llm_killed: bool,
    expect_checklist: bool,
) -> None:
    """resume_cmd (confirm=False/True), ResumeConfirmView.confirm_button, and ResumeReviewer.review block safely."""
    gemini_calls: list[dict[str, Any]] = []

    async def _forbidden_gemini(**kwargs: Any) -> str:
        gemini_calls.append(kwargs)
        raise AssertionError(f"Gemini must NEVER be called for denied case {case_id}")

    settings = Settings(db_path=":memory:", **settings_kwargs)
    container = ServiceContainer(
        settings=settings,
        clock=FrozenClock(1_700_000_000.0),
        monotonic_clock=FrozenClock(100.0),
        gemini_generate_fn=_forbidden_gemini,
    )
    container.llm_killed = llm_killed
    bot = FakeClient(settings=settings, services=container)
    bot._llm_killed = llm_killed  # type: ignore[attr-defined]
    member = build_fake_member(user_id=800000000000000010, display_name="Applicant")
    guild = SimpleNamespace(id=900000000000000010, name="Test Guild")

    # Entrypoint A: /ic resume confirm=False
    att_a = _SpyAttachment()
    inter_a = FakeInteraction(user=member, client=bot, guild=guild, command_name="ic resume")
    await resume_cmd.callback(inter_a, file=att_a, discipline="swe", confirm=False)  # type: ignore[arg-type]
    assert att_a.read_calls == 0, f"{case_id}: confirm=False read attachment"
    assert gemini_calls == [], f"{case_id}: confirm=False called Gemini"
    assert inter_a.last_reply is not None and inter_a.last_reply.ephemeral is True
    if expect_checklist:
        assert inter_a.last_reply.embed is not None
        assert "Offline Resume Checklist" in (inter_a.last_reply.embed.title or "")

    # Entrypoint B: /ic resume confirm=True (direct bypass attempt)
    att_b = _SpyAttachment()
    inter_b = FakeInteraction(user=member, client=bot, guild=guild, command_name="ic resume")
    await resume_cmd.callback(inter_b, file=att_b, discipline="swe", confirm=True)  # type: ignore[arg-type]
    assert att_b.read_calls == 0, f"{case_id}: confirm=True read attachment"
    assert gemini_calls == [], f"{case_id}: confirm=True called Gemini"
    assert inter_b.last_reply is not None and inter_b.last_reply.ephemeral is True

    # Entrypoint C: ResumeConfirmView.confirm_button (e.g. opened earlier, then disabled/stopped)
    att_c = _SpyAttachment()
    init_inter = FakeInteraction(user=member, client=bot, guild=guild, command_name="ic resume")
    view = ResumeConfirmView(
        interaction=init_inter,  # type: ignore[arg-type]
        attachment=att_c,  # type: ignore[arg-type]
        discipline_key="swe",
        discipline_title="Software Engineering & Tech",
        target_role="Intern",
        settings=settings,
    )
    click_inter = FakeInteraction(user=member, client=bot, guild=guild, command_name="ic resume")
    await view.confirm_button.callback(click_inter)  # type: ignore[arg-type]
    assert att_c.read_calls == 0, f"{case_id}: confirm_button read attachment"
    assert gemini_calls == [], f"{case_id}: confirm_button called Gemini"
    assert click_inter.last_reply is not None
    assert "Analyzing resume with Gemini" not in (click_inter.last_reply.content or "")

    # Entrypoint D: Direct ResumeReviewer.review() call
    att_d = _SpyAttachment()
    inter_d = FakeInteraction(user=member, client=bot, guild=guild, command_name="ic resume")
    await inter_d.response.defer(thinking=True, ephemeral=True)
    await ResumeReviewer.review(inter_d, att_d, discipline="swe", skip_confirm=True)  # type: ignore[arg-type]
    assert att_d.read_calls == 0, f"{case_id}: ResumeReviewer.review read attachment"
    assert gemini_calls == [], f"{case_id}: ResumeReviewer.review called Gemini"
    assert len(inter_d.followup.messages) == 1
    assert inter_d.followup.messages[0].ephemeral is True
    if expect_checklist:
        assert inter_d.followup.messages[0].embed is not None
        assert "Offline Resume Checklist" in (inter_d.followup.messages[0].embed.title or "")


@pytest.mark.parametrize(
    ("case_id", "settings_kwargs", "llm_killed", "in_dm", "channel_id", "role_ids"),
    [
        ("master_off_with_key", {"llm_enabled": False, "gemini_api_key": "AIzaSyKey"}, False, False, 200, []),
        (
            "disabled_features_ai",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "disabled_features": ["ai"]},
            False,
            False,
            200,
            [],
        ),
        (
            "student_core_profile",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "feature_profile": "student_core"},
            False,
            False,
            200,
            [],
        ),
        (
            "admin_llm_stop",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey"},
            True,
            False,
            200,
            [],
        ),
        (
            "missing_key",
            {"llm_enabled": True, "gemini_api_key": ""},
            False,
            False,
            200,
            [],
        ),
        (
            "dm_disallowed_by_default",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "llm_allow_dm": False},
            False,
            True,
            200,
            [],
        ),
        (
            "channel_not_in_allowlist",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "llm_allowed_channel_ids": [999]},
            False,
            False,
            200,
            [],
        ),
        (
            "role_not_in_allowlist",
            {"llm_enabled": True, "gemini_api_key": "AIzaSyKey", "llm_allowed_role_ids": [777]},
            False,
            False,
            200,
            [111],
        ),
    ],
)
async def test_mention_assistant_never_reads_history_or_calls_gemini_when_denied(
    case_id: str,
    settings_kwargs: dict[str, Any],
    llm_killed: bool,
    in_dm: bool,
    channel_id: int,
    role_ids: list[int],
) -> None:
    """Denied @mention invocations must not read channel history, read attachments, or call Gemini."""
    gemini_calls: list[dict[str, Any]] = []

    async def _forbidden_gemini(**kwargs: Any) -> str:
        gemini_calls.append(kwargs)
        raise AssertionError(f"Gemini must NEVER be called for denied mention case {case_id}")

    settings = Settings(
        db_path=":memory:",
        llm_channel_history_enabled=True,
        **settings_kwargs,
    )
    container = ServiceContainer(
        settings=settings,
        clock=FrozenClock(1_700_000_000.0),
        monotonic_clock=FrozenClock(100.0),
        gemini_generate_fn=_forbidden_gemini,
    )
    container.llm_killed = llm_killed
    bot = FakeClient(settings=settings, services=container)
    bot._llm_killed = llm_killed  # type: ignore[attr-defined]

    ch = _SpyChannel(channel_id=channel_id, name="general")
    author = build_fake_member(user_id=800000000000000010, display_name="Alice", role_ids=role_ids)
    att = _SpyAttachment(filename="diagram.png", content_type="image/png", content=b"\x89PNG\r\n\x1a\n")
    msg = _MentionMessage(
        content="<@999000000000000001> help me with this",
        channel=ch,
        author=author,
    )
    msg.guild = None if in_dm else discord.Object(id=900000000000000010)  # type: ignore[assignment]
    msg.mentions = [bot.user]  # type: ignore[assignment]
    msg.attachments = [att]  # type: ignore[assignment]
    msg.reference = None

    handled = await llm_task.maybe_handle_llm_mention(bot, msg)  # type: ignore[arg-type]
    assert handled is False, case_id
    assert ch.history_calls == 0, f"{case_id}: read channel history while denied"
    assert att.read_calls == 0, f"{case_id}: read attachment while denied"
    assert gemini_calls == [], f"{case_id}: called Gemini while denied"
    assert msg.reply_calls == [], f"{case_id}: posted reply while denied"


# ---------------------------------------------------------------------------
# 3. Per-Guild Override Deny-First Enforcement & UI Status Consistency
# ---------------------------------------------------------------------------


def test_per_guild_override_cannot_bypass_process_master_disable() -> None:
    """A guild override of llm_enabled=True cannot override process-level LLM_ENABLED=false."""
    base_off = Settings(db_path=":memory:", llm_enabled=False, gemini_api_key="AIzaSyKey")
    guild_cfg = GuildSettings.from_overrides(
        900000000000000101,
        {"llm_enabled": True, "feature_profile": "community_full", "enabled_features": ["ai"]},
    )
    eff = resolve_effective_settings(base_off, guild_cfg)
    assert eff.llm_enabled is False
    assert resolve_ai_access(eff).allowed is False
    assert resolve_ai_access(eff).status == "disabled"

    # Conversely, when process LLM_ENABLED=true, a guild CAN opt out locally
    base_on = Settings(db_path=":memory:", llm_enabled=True, gemini_api_key="AIzaSyKey")
    guild_opt_out = GuildSettings.from_overrides(
        900000000000000102,
        {"llm_enabled": False},
    )
    eff_opt_out = resolve_effective_settings(base_on, guild_opt_out)
    assert eff_opt_out.llm_enabled is False
    assert resolve_ai_access(eff_opt_out).allowed is False
    assert resolve_ai_access(eff_opt_out).status == "disabled"


async def test_status_and_botinfo_never_report_working_when_ai_disabled_or_paused(
    tmp_db: Database,
    make_interaction,
    make_member,
) -> None:
    """/ic status and /ic botinfo reflect disabled/paused states accurately (never 'Working'/'Ready')."""
    admin = make_member(user_id=800000000000000099, administrator=True)
    guild = SimpleNamespace(
        id=900000000000000101,
        name="Campus Guild",
        get_channel=lambda _cid: None,
        me=None,
    )

    # Case 1: Key present but LLM_ENABLED=false -> must NOT show Working or Ready
    settings_off = Settings(
        db_path=tmp_db.path,
        guild_id=guild.id,
        llm_enabled=False,
        gemini_api_key="AIzaSyKey",
        feature_profile="community_full",
    )
    bot_off = FakeClient(
        settings=settings_off, db=tmp_db, services=ServiceContainer.from_settings(settings_off, db=tmp_db)
    )
    inter_status_off = make_interaction(user=admin, guild=guild, client=bot_off, command_name="ic status")
    await status_cmd.callback(inter_status_off)
    status_fields_off = {f.name: f.value for f in (inter_status_off.last_reply.embed.fields or [])}
    assert "**Instigate AI Assistant**: ⚪ **Disabled**" in status_fields_off["Feature Modules Status"]

    inter_info_off = make_interaction(user=admin, guild=guild, client=bot_off, command_name="ic botinfo")
    await botinfo.callback(inter_info_off)
    info_fields_off = {f.name: f.value for f in (inter_info_off.last_reply.embed.fields or [])}
    assert "**LLM Assistant:** ⚪ Disabled" in info_fields_off["Integration Readiness"]
    assert "**Resume Review:** ⚪ Disabled" in info_fields_off["Integration Readiness"]

    # Case 2: LLM_ENABLED=true + key present + /ic admin llm_stop active -> must show Paused (never Working)
    settings_on = Settings(
        db_path=tmp_db.path,
        guild_id=guild.id,
        llm_enabled=True,
        gemini_api_key="AIzaSyKey",
    )
    container_paused = ServiceContainer.from_settings(settings_on, db=tmp_db)
    container_paused.llm_killed = True
    bot_paused = FakeClient(settings=settings_on, db=tmp_db, services=container_paused)
    bot_paused._llm_killed = True  # type: ignore[attr-defined]

    inter_status_paused = make_interaction(
        user=admin, guild=guild, client=bot_paused, command_name="ic status"
    )
    await status_cmd.callback(inter_status_paused)
    status_fields_paused = {f.name: f.value for f in (inter_status_paused.last_reply.embed.fields or [])}
    assert "**Instigate AI Assistant**: 🛑 **Paused**" in status_fields_paused["Feature Modules Status"]
    assert "llm_start" in status_fields_paused["Actionable Diagnostics & Fixes"]

    inter_info_paused = make_interaction(
        user=admin, guild=guild, client=bot_paused, command_name="ic botinfo"
    )
    await botinfo.callback(inter_info_paused)
    info_fields_paused = {f.name: f.value for f in (inter_info_paused.last_reply.embed.fields or [])}
    assert "✅ Ready" not in info_fields_paused["Integration Readiness"]
    assert "llm_stop" in info_fields_paused["Integration Readiness"]


async def test_guard_feature_and_command_gate_respect_master_switch_and_killswitch(
    tmp_db: Database,
    make_interaction,
    make_member,
) -> None:
    """check_command_gate and guard_feature block /ic resume when master-off or paused, and allow when ready."""
    member = make_member(user_id=800000000000000010)
    guild = SimpleNamespace(id=900000000000000101, name="Campus Guild")

    # 1. Key present, LLM_ENABLED=false -> blocked
    s_off = Settings(db_path=tmp_db.path, llm_enabled=False, gemini_api_key="AIzaSyKey")
    inter_off = make_interaction(user=member, guild=guild, settings=s_off, command_name="ic resume")
    assert await check_command_gate(inter_off, s_off) is False
    assert inter_off.last_reply.ephemeral is True

    # 2. LLM_ENABLED=true + key + llm_killed=True -> blocked with paused notice
    s_on = Settings(db_path=tmp_db.path, llm_enabled=True, gemini_api_key="AIzaSyKey")
    container = ServiceContainer.from_settings(s_on, db=tmp_db)
    container.llm_killed = True
    bot = FakeClient(settings=s_on, db=tmp_db, services=container)
    bot._llm_killed = True  # type: ignore[attr-defined]
    inter_paused = make_interaction(user=member, guild=guild, client=bot, command_name="ic resume")
    assert await guard_feature(inter_paused, "ai") is False
    assert "paused" in (inter_paused.last_reply.content or "").lower()

    # 3. LLM_ENABLED=true + missing key -> check_command_gate allows ic resume through to render offline checklist
    s_nokey = Settings(db_path=tmp_db.path, llm_enabled=True, gemini_api_key="")
    inter_nokey = make_interaction(user=member, guild=guild, settings=s_nokey, command_name="ic resume")
    assert await check_command_gate(inter_nokey, s_nokey) is True

    # 4. Normal consented flow still works when LLM_ENABLED=true + key + not killed
    gemini_calls: list[dict[str, Any]] = []

    async def _ok_gemini(**kwargs: Any) -> str:
        gemini_calls.append(kwargs)
        return "PASS" if "PASS" in str(kwargs.get("prompt", "")) else "## Executive Summary\nScore: 9/10"

    c_ok = ServiceContainer.from_settings(s_on, db=tmp_db, gemini_generate_fn=_ok_gemini)
    bot_ok = FakeClient(settings=s_on, db=tmp_db, services=c_ok)
    att = _SpyAttachment()
    inter_cmd = make_interaction(user=member, guild=guild, client=bot_ok, command_name="ic resume")
    await resume_cmd.callback(inter_cmd, file=att, discipline="swe", confirm=False)  # type: ignore[arg-type]
    assert att.read_calls == 0  # Disclosure shown before reading attachment!
    assert isinstance(inter_cmd.last_reply.view, ResumeConfirmView)

    click_inter = make_interaction(user=member, guild=guild, client=bot_ok, command_name="ic resume")
    await inter_cmd.last_reply.view.confirm_button.callback(click_inter)  # type: ignore[arg-type]
    assert att.read_calls == 1
    assert len(gemini_calls) == 2

    # 5. Guild-level disable overrides host LLM_ENABLED=true
    store = GuildSettingsStore(tmp_db)
    await store.set_overrides(guild.id, {"disabled_features": ["ai"]})
    c_ok.guild_settings = store
    att_denied = _SpyAttachment()
    inter_guild_denied = make_interaction(user=member, guild=guild, client=bot_ok, command_name="ic resume")
    await resume_cmd.callback(inter_guild_denied, file=att_denied, discipline="swe", confirm=True)  # type: ignore[arg-type]
    assert att_denied.read_calls == 0
    assert len(gemini_calls) == 2  # No additional Gemini calls
