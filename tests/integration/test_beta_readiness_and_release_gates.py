# tests/integration/test_beta_readiness_and_release_gates.py
"""Automated Integration & Release Gate Verification Tests for v1.0.0-rc1.

Evaluates the 8 Mandatory Release Gates for private alpha and public beta readiness:
  - Gate 1: Multi-Discipline Onboarding & Autonomous Setup (CS/Eng vs. Biz/Commerce)
  - Gate 2: Multi-Guild Data, Settings & Economy Isolation
  - Gate 3: Security, Clean Secret Scan & Log Redaction
  - Gate 4: Financial Economy Integrity & ACID Settlement
  - Gate 5: Bounded Interactive Response Latency (< 2500ms)
  - Gate 6: Predictable Provider Failure & Graceful Degradation
  - Gate 7: Cost Controls, Quota Budgets & Feature-Flag Toggles
  - Gate 8: Privacy-Preserving Feedback & RTBF Data Deletion Compliance

All tests execute strictly offline using temporary, synthetic SQLite databases.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from icbot.academics.exams_service import ExamStore, ParsedExamDraft
from icbot.config import Settings
from icbot.db import Database
from icbot.features.admin import setup_cmd
from icbot.features.exams import exams_add_cmd, exams_cmd
from icbot.features.info import botinfo
from icbot.features.profile import profile_cmd
from icbot.features.resume import ResumeConfirmView
from icbot.logging import BOT_VERSION
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import DailyAlreadyClaimedError, UserStore
from tests.helpers import FakeClient, FakeInteraction, build_fake_member
from tools.verify_beta_readiness import BetaReadinessVerifier

STAGING_GUILD_ALPHA = 900_000_000_000_000_001  # CS & Software Eng cohort
STAGING_GUILD_BETA = 900_000_000_000_000_002  # Business & Commerce cohort
ADMIN_ROLE_ID = 888_000_000_000_000_001


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


@pytest.mark.asyncio
async def test_all_8_beta_readiness_release_gates(tmp_path: Path) -> None:
    """Run the complete BetaReadinessVerifier harness and assert all 8 gates pass."""
    verifier = BetaReadinessVerifier(tmp_path)
    all_passed = await verifier.run_all()

    assert all_passed is True, "Expected all 8 beta readiness release gates to pass."
    assert len(verifier.results) == 8
    assert all(r.passed for r in verifier.results)
    assert BOT_VERSION == "v1.0.0-rc1"


@pytest.mark.asyncio
async def test_multi_discipline_onboarding_and_setup(tmp_path: Path) -> None:
    """Verify autonomous onboarding and setup across CS and Business cohorts (Gate 1)."""
    db_path = tmp_path / "gate1_test.db"
    settings = Settings(
        guild_id=STAGING_GUILD_ALPHA,
        db_path=str(db_path),
        allowed_role_ids=[ADMIN_ROLE_ID],
        allow_gateway=False,
    )
    db = Database(str(db_path))
    await db.init()

    try:
        # Cohort A: Computer Science (CPS209)
        cs_lead = build_fake_member(
            user_id=101, display_name="CS_Lead", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        guild_cs = SimpleNamespace(id=STAGING_GUILD_ALPHA, name="CS Staging")

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
        reply = (
            inter_add_cs.followup.messages[-1]
            if inter_add_cs.followup.messages
            else inter_add_cs.response.sent_messages[-1]
        )
        assert "Added exam" in (reply.content or "")

        # Cohort B: Business / Commerce (FIN300)
        biz_lead = build_fake_member(
            user_id=202, display_name="Biz_Lead", role_ids=[ADMIN_ROLE_ID], administrator=True
        )
        guild_biz = SimpleNamespace(id=STAGING_GUILD_BETA, name="Biz Staging")

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
        reply_biz = (
            inter_add_biz.followup.messages[-1]
            if inter_add_biz.followup.messages
            else inter_add_biz.response.sent_messages[-1]
        )
        assert "Added exam" in (reply_biz.content or "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multi_guild_economy_and_exam_isolation(tmp_path: Path) -> None:
    """Verify strict tenancy isolation for exams and economy balances (Gate 2)."""
    db_path = tmp_path / "gate2_test.db"
    db = Database(str(db_path))
    await db.init()

    try:
        # 1. Exam isolation
        store = ExamStore(db)
        await store.add_exam(
            guild_id=STAGING_GUILD_ALPHA,
            draft=ParsedExamDraft(
                course_code="CPS109", course_name="CS I", start_ts=1797600000, end_ts=1797607200
            ),
            created_by=101,
        )
        await store.add_exam(
            guild_id=STAGING_GUILD_BETA,
            draft=ParsedExamDraft(
                course_code="MTH140", course_name="Calc I", start_ts=1797700000, end_ts=1797707200
            ),
            created_by=202,
        )

        exams_alpha = await store.list_exams(STAGING_GUILD_ALPHA)
        exams_beta = await store.list_exams(STAGING_GUILD_BETA)

        assert len(exams_alpha) == 1
        assert exams_alpha[0].course_code == "CPS109"
        assert len(exams_beta) == 1
        assert exams_beta[0].course_code == "MTH140"

        # 2. Economy isolation: Same user ID in Guild Alpha and Beta
        u_store = UserStore(db)
        shared_uid = 999
        await u_store.ensure_user(shared_uid, "DualMember", guild_id=STAGING_GUILD_ALPHA)
        await u_store.ensure_user(shared_uid, "DualMember", guild_id=STAGING_GUILD_BETA)

        await u_store.add_balance(
            shared_uid, 500, category="grant", reference_id="g1", guild_id=STAGING_GUILD_ALPHA
        )

        bal_alpha = await u_store.get_balance(shared_uid, guild_id=STAGING_GUILD_ALPHA)
        bal_beta = await u_store.get_balance(shared_uid, guild_id=STAGING_GUILD_BETA)

        assert bal_alpha == 1500  # Default 1000 + 500
        assert bal_beta == 1000  # Untouched baseline
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_financial_atomicity_and_concurrency_lock(tmp_path: Path) -> None:
    """Verify ACID transaction settlement and double-click concurrency lock (Gate 4)."""
    db_path = tmp_path / "gate4_test.db"
    db = Database(str(db_path))
    await db.init()

    try:
        u_store = UserStore(db, guild_id=STAGING_GUILD_ALPHA)
        uid = 404
        await u_store.ensure_user(uid, "EconomyUser")

        # Daily claim (+100) -> 1100
        claimed_amt, bal_after = await u_store.claim_daily(uid)
        assert claimed_amt == 100
        assert bal_after == 1100

        # Wager and payout
        await u_store.subtract_balance(uid, 300, category="casino_wager", reference_id="round_1")
        await u_store.add_balance(uid, 600, category="casino_payout", reference_id="round_1")
        assert await u_store.get_balance(uid) == 1400

        # Equity trade: buy 2 IC @ $100 = $200
        m_store = FunMarketStore(db, guild_id=STAGING_GUILD_ALPHA)
        await m_store.sync_catalog()
        await m_store.buy_long(user_id=uid, symbol="IC", qty=2)

        final_bal = await u_store.get_balance(uid)
        assert final_bal == 1200

        # Concurrency race: 10 parallel claims must result in 0 additional claims
        async def try_claim():
            try:
                await u_store.claim_daily(uid)
                return True
            except DailyAlreadyClaimedError:
                return False

        results = await asyncio.gather(*(try_claim() for _ in range(10)))
        assert sum(1 for r in results if r is True) == 0

        # Audit ledger reconciliation
        ledger = await u_store.get_ledger(uid)
        assert len(ledger) >= 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_interactive_slash_command_latency_bounds(tmp_path: Path) -> None:
    """Verify interactive slash commands defer or return well within 2500ms (Gate 5)."""
    db_path = tmp_path / "gate5_test.db"
    settings = Settings(guild_id=STAGING_GUILD_ALPHA, db_path=str(db_path), allow_gateway=False)
    db = Database(str(db_path))
    await db.init()

    try:
        member = build_fake_member(user_id=505, display_name="SpeedTester")
        guild = SimpleNamespace(id=STAGING_GUILD_ALPHA, name="Staging Alpha")

        commands_to_test = [
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
async def test_privacy_preserving_consent_and_rtbf_deletion(tmp_path: Path) -> None:
    """Verify resume consent barrier, user data purge, and immutable audit ledger (Gate 8)."""
    db_path = tmp_path / "gate8_test.db"
    settings = Settings(guild_id=STAGING_GUILD_ALPHA, db_path=str(db_path), allow_gateway=False)
    db = Database(str(db_path))
    await db.init()

    try:
        # 1. Resume Review Consent Barrier
        mock_attachment = SimpleNamespace(filename="student_resume.pdf", size=18000)
        member = build_fake_member(user_id=808, display_name="PrivacySubject")
        inter = _make_interaction(
            user=member, guild=SimpleNamespace(id=STAGING_GUILD_ALPHA), settings=settings, db=db
        )
        confirm_view = ResumeConfirmView(
            interaction=inter,
            attachment=mock_attachment,
            discipline_key="swe",
            discipline_title="Software Engineering",
            target_role="Junior Developer",
            settings=settings,
        )
        labels = [getattr(c, "label", "") for c in confirm_view.children]
        assert "Submit for AI Review" in labels
        assert "View No-AI Checklist" in labels
        assert "Cancel" in labels

        # 2. Right-to-be-Forgotten Data Deletion
        u_store = UserStore(db, guild_id=STAGING_GUILD_ALPHA)
        uid = 808
        await u_store.ensure_user(uid, "PrivacySubject")
        await u_store.add_balance(uid, 500, category="reward", reference_id="grant_1")

        del_counts = await u_store.delete_user_data(uid, anonymize_ledger=True)
        assert del_counts["users"] == 1
        assert del_counts["economy_ledger_preserved"] == 1

        # Profile deleted
        profile_after = await u_store.get_profile(uid)
        assert profile_after is None

        # Ledger records preserved for financial audit integrity
        ledger_after = await u_store.get_ledger(uid)
        assert len(ledger_after) >= 1
    finally:
        await db.close()
