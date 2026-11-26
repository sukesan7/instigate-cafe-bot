"""Integration tests for multi-guild isolation, economy atomicity, privacy controls, and feature workflows.

All tests execute offline against temporary isolated SQLite databases.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from icbot.academics.exams_service import ExamStore, ParsedExamDraft
from icbot.config import Settings
from icbot.db import Database
from icbot.features.admin import audit_cmd, features_cmd, setup_cmd
from icbot.features.blackjack import blackjack
from icbot.features.exams import exams_add_cmd, exams_cmd
from icbot.features.info import botinfo, status_cmd
from icbot.features.notes import notes_cmd
from icbot.features.profile import profile_cmd
from icbot.features.resume import ResumeConfirmView, resume_cmd
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import DailyAlreadyClaimedError, UserStore
from tests.helpers import FakeClient, FakeInteraction, build_fake_member

GUILD_ALPHA = 900_000_000_000_000_001
GUILD_BETA = 900_000_000_000_000_002
ADMIN_ROLE_ID = 888_000_000_000_000_001


@dataclass
class _SyntheticPdfAttachment:
    filename: str = "sample_resume.pdf"
    content: bytes = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\nxref\n0 4\n"
        b"0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n"
        b"0000000115 00000 n\ntrailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n164\n%%EOF"
    )
    content_type: str = "application/pdf"
    url: str = "https://cdn.discordapp.com/attachments/123/456/synthetic.pdf"

    @property
    def size(self) -> int:
        return len(self.content)

    async def read(self) -> bytes:
        return self.content


def _make_interaction(
    user: Any,
    guild: Any,
    settings: Settings,
    db: Database,
    channel: Any = None,
    command_name: str = "ic test",
) -> FakeInteraction:
    client = FakeClient(settings=settings, db=db)
    return FakeInteraction(user=user, client=client, guild=guild, channel=channel, command_name=command_name)


def _last_reply(inter: FakeInteraction) -> Any | None:
    if inter.followup.messages:
        return inter.followup.messages[-1]
    if inter.response.sent_messages:
        return inter.response.sent_messages[-1]
    return None


@pytest.mark.asyncio
async def test_multi_discipline_onboarding_and_setup(tmp_path: Path) -> None:
    """Verify onboarding setup and exam scheduling across distinct guilds."""
    db_path = tmp_path / "gate1_test.db"
    settings = Settings(
        guild_id=GUILD_ALPHA,
        db_path=str(db_path),
        allowed_role_ids=[ADMIN_ROLE_ID],
        allow_gateway=False,
    )
    db = Database(str(db_path))
    await db.init()

    try:
        cs_lead = build_fake_member(
            user_id=101, display_name="CS_Lead", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        guild_cs = SimpleNamespace(id=GUILD_ALPHA, name="Alpha Guild")

        inter_setup = _make_interaction(
            user=cs_lead, guild=guild_cs, settings=settings, db=db, command_name="ic admin setup"
        )
        await setup_cmd.callback(inter_setup)
        assert inter_setup.response.sent_messages or inter_setup.followup.messages

        inter_add_cs = _make_interaction(
            user=cs_lead, guild=guild_cs, settings=settings, db=db, command_name="ic exam add"
        )
        await exams_add_cmd.callback(
            inter_add_cs,
            course_code="CPS209",
            course_name="Computer Science II",
            date="2026-12-18",
            start_time="14:00",
            end_time="16:00",
        )
        reply = _last_reply(inter_add_cs)
        assert reply is not None and "Added exam" in (reply.content or "")

        biz_lead = build_fake_member(
            user_id=202, display_name="Biz_Lead", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        guild_biz = SimpleNamespace(id=GUILD_BETA, name="Beta Guild")

        inter_add_biz = _make_interaction(
            user=biz_lead, guild=guild_biz, settings=settings, db=db, command_name="ic exam add"
        )
        await exams_add_cmd.callback(
            inter_add_biz,
            course_code="FIN300",
            course_name="Managerial Finance I",
            date="2026-12-19",
            start_time="09:00",
            end_time="11:30",
        )
        reply_biz = _last_reply(inter_add_biz)
        assert reply_biz is not None and "Added exam" in (reply_biz.content or "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multi_guild_economy_and_exam_isolation(tmp_path: Path) -> None:
    """Verify strict guild tenancy isolation for exams and economy balances."""
    db_path = tmp_path / "gate2_test.db"
    db = Database(str(db_path))
    await db.init()

    try:
        store = ExamStore(db)
        await store.add_exam(
            guild_id=GUILD_ALPHA,
            draft=ParsedExamDraft(
                course_code="CPS109", course_name="CS I", start_ts=1797600000, end_ts=1797607200
            ),
            created_by=101,
        )
        await store.add_exam(
            guild_id=GUILD_BETA,
            draft=ParsedExamDraft(
                course_code="MTH140", course_name="Calc I", start_ts=1797700000, end_ts=1797707200
            ),
            created_by=202,
        )

        exams_alpha = await store.list_exams(GUILD_ALPHA)
        exams_beta = await store.list_exams(GUILD_BETA)

        assert len(exams_alpha) == 1
        assert exams_alpha[0].course_code == "CPS109"
        assert len(exams_beta) == 1
        assert exams_beta[0].course_code == "MTH140"

        u_store = UserStore(db)
        shared_uid = 999
        await u_store.ensure_user(shared_uid, "DualMember", guild_id=GUILD_ALPHA)
        await u_store.ensure_user(shared_uid, "DualMember", guild_id=GUILD_BETA)

        await u_store.add_balance(shared_uid, 500, category="grant", reference_id="g1", guild_id=GUILD_ALPHA)

        assert await u_store.get_balance(shared_uid, guild_id=GUILD_ALPHA) == 1500
        assert await u_store.get_balance(shared_uid, guild_id=GUILD_BETA) == 1000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_financial_atomicity_and_concurrency_lock(tmp_path: Path) -> None:
    """Verify ACID transaction settlement, market buy, and daily claim cooldown lock."""
    db_path = tmp_path / "gate4_test.db"
    db = Database(str(db_path))
    await db.init()

    try:
        u_store = UserStore(db, guild_id=GUILD_ALPHA)
        uid = 404
        await u_store.ensure_user(uid, "EconomyUser")

        claimed_amt, bal_after = await u_store.claim_daily(uid)
        assert claimed_amt == 100
        assert bal_after == 1100

        await u_store.subtract_balance(uid, 300, category="casino_wager", reference_id="round_1")
        await u_store.add_balance(uid, 600, category="casino_payout", reference_id="round_1")
        assert await u_store.get_balance(uid) == 1400

        m_store = FunMarketStore(db, guild_id=GUILD_ALPHA)
        await m_store.sync_catalog()
        await m_store.buy_long(user_id=uid, symbol="IC", qty=2)

        assert await u_store.get_balance(uid) == 1200

        async def try_claim() -> bool:
            try:
                await u_store.claim_daily(uid)
                return True
            except DailyAlreadyClaimedError:
                return False

        results = await asyncio.gather(*(try_claim() for _ in range(10)))
        assert sum(1 for r in results if r is True) == 0

        ledger = await u_store.get_ledger(uid)
        assert len(ledger) >= 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_interactive_slash_command_latency_bounds(tmp_path: Path) -> None:
    """Verify interactive slash commands defer or respond well within Discord's 3s deadline."""
    db_path = tmp_path / "gate5_test.db"
    settings = Settings(guild_id=GUILD_ALPHA, db_path=str(db_path), allow_gateway=False)
    db = Database(str(db_path))
    await db.init()

    try:
        member = build_fake_member(user_id=505, display_name="SpeedTester")
        guild = SimpleNamespace(id=GUILD_ALPHA, name="Alpha Guild")

        commands_to_test: list[tuple[str, Any, dict[str, Any]]] = [
            ("ic botinfo", botinfo, {}),
            ("ic profile", profile_cmd, {}),
            ("ic exam list", exams_cmd, {}),
        ]

        for name, cmd, kwargs in commands_to_test:
            inter = _make_interaction(user=member, guild=guild, settings=settings, db=db, command_name=name)
            t0 = time.perf_counter()
            await cmd.callback(inter, **kwargs)
            lat_ms = (time.perf_counter() - t0) * 1000.0

            assert lat_ms < 2500.0, f"Command {name} exceeded latency bound: {lat_ms:.2f}ms"
            assert inter.response.is_done() or inter.followup.messages, (
                f"Command {name} did not respond or defer"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_offline_notes_degradation_and_feature_profile_gating(tmp_path: Path) -> None:
    """Verify /ic notes degrades gracefully offline and student_core blocks casino while keeping academics."""
    db_path = tmp_path / "profile_gate.db"
    settings = Settings(
        guild_id=GUILD_ALPHA,
        db_path=str(db_path),
        feature_profile="student_core",
        allow_gateway=False,
        allowed_role_ids=[ADMIN_ROLE_ID],
    )
    db = Database(str(db_path))
    await db.init()

    try:
        member = build_fake_member(
            user_id=707, display_name="FlagTester", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        guild = SimpleNamespace(id=GUILD_ALPHA, name="Alpha Guild")

        inter_casino = _make_interaction(
            user=member, guild=guild, settings=settings, db=db, command_name="ic casino blackjack"
        )
        await blackjack.callback(inter_casino, bet=50)
        casino_msg = _last_reply(inter_casino)
        assert casino_msg is not None
        combined = (casino_msg.content or "") + (casino_msg.embed.description if casino_msg.embed else "")
        assert "disabled" in combined.lower()

        inter_notes = _make_interaction(
            user=member, guild=guild, settings=settings, db=db, command_name="ic notes"
        )
        await notes_cmd.callback(inter_notes, course="CPS109")
        notes_msg = _last_reply(inter_notes)
        assert notes_msg is not None
        assert "notes" in (notes_msg.content or "").lower() or notes_msg.embed is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_privacy_consent_offline_checklist_and_admin_diagnostics(tmp_path: Path) -> None:
    """Verify /ic resume offline checklist button, erasure deletion, admin feature toggle, and status report."""
    db_path = tmp_path / "privacy_admin.db"
    settings = Settings(
        guild_id=GUILD_ALPHA,
        db_path=str(db_path),
        token="",
        gemini_api_key="synth-gemini-key-test",
        llm_enabled=True,
        allow_gateway=False,
    )
    db = Database(str(db_path))
    await db.init()

    try:
        member = build_fake_member(user_id=808, display_name="PrivacySubject")
        admin = build_fake_member(user_id=909, administrator=True, display_name="GuildAdmin")
        guild = SimpleNamespace(id=GUILD_ALPHA, owner_id=909, name="Alpha Guild", me=SimpleNamespace(id=777))

        # 1. /ic resume consent view -> click View No-AI Checklist offline (zero Gemini API call)
        inter_res = _make_interaction(
            user=member, guild=guild, settings=settings, db=db, command_name="ic resume"
        )
        await resume_cmd.callback(
            inter_res,
            file=_SyntheticPdfAttachment(),  # type: ignore[arg-type]
            discipline="SWE",
            role="Junior Backend Engineer",
        )
        consent_reply = _last_reply(inter_res)
        assert consent_reply is not None and consent_reply.view is not None
        view: ResumeConfirmView = consent_reply.view
        labels = [getattr(c, "label", "") for c in view.children]
        assert {"Submit for AI Review", "View No-AI Checklist", "Cancel"}.issubset(set(labels))

        btn_inter = _make_interaction(user=member, guild=guild, settings=settings, db=db)
        await view.checklist_button.callback(btn_inter)
        check_reply = btn_inter.response.edit_messages[0] if btn_inter.response.edit_messages else None
        assert check_reply is not None and check_reply.embed is not None
        assert "Offline Resume Checklist" in (check_reply.embed.title or "")

        # 2. Transactional user data deletion & ledger tombstoning
        u_store = UserStore(db, guild_id=GUILD_ALPHA)
        await u_store.ensure_user(808, "PrivacySubject")
        await u_store.add_balance(808, 500, category="reward", reference_id="grant_1")
        del_counts = await u_store.delete_user_data(808, anonymize_ledger=True)
        assert del_counts["users"] == 1
        assert del_counts["economy_ledger_preserved"] == 1
        assert del_counts["economy_ledger_anonymized"] == 1
        assert await u_store.get_profile(808) is None
        assert await u_store.get_ledger(808) == []
        assert len(await u_store.get_ledger(0)) >= 1

        # 3. Admin feature disable + audit trail + /ic status issue_report=True attachment
        inter_feat = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic admin features"
        )
        await features_cmd.callback(inter_feat, disable="casino")
        assert _last_reply(inter_feat) is not None

        inter_audit = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic admin audit"
        )
        await audit_cmd.callback(inter_audit)
        audit_reply = _last_reply(inter_audit)
        assert audit_reply is not None and audit_reply.embed is not None

        inter_status = _make_interaction(
            user=admin, guild=guild, settings=settings, db=db, command_name="ic status"
        )
        await status_cmd.callback(inter_status, issue_report=True)
        status_reply = _last_reply(inter_status)
        assert status_reply is not None and status_reply.file is not None
    finally:
        await db.close()
