"""Integration and contract tests for interaction-state guards, audience matrix, and pagination views."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from icbot.academics.models import Exam
from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.admin import GuildSetupView
from icbot.features.exams import ExamClearConfirmView, ExamPaginationView, build_exam_schedule_embed
from icbot.features.help import (
    GettingStartedView,
    HelpPaginationView,
    build_getting_started_embed,
    build_help_embed,
)
from icbot.features.invest import AdminMarketConfirmView
from icbot.features.jobs import JobPaginationView
from icbot.features.lc_leaderboard import LeaderboardView
from icbot.features.notes import NotesPaginationView, build_notes_page_embed
from icbot.features.resume import ResumeConfirmView, ResumeReviewPaginationView
from icbot.guild_config import GuildSettingsStore
from icbot.services.jobs.models import JobPosting
from icbot.ui import (
    DEFAULT_PAGINATOR_TIMEOUT_SECONDS,
    INTERACTION_AUDIENCE_MATRIX,
    SHARED_INTERACTION_EXCEPTIONS,
    compute_page_slice,
    safe_edit_view_message,
    safe_timeout_disable,
)
from tests.helpers import FakeClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _http_exc(cls: type[discord.HTTPException], status: int, text: str) -> discord.HTTPException:
    resp = SimpleNamespace(status=status, reason=text)
    return cls(resp, text)  # type: ignore[arg-type]


def test_pure_page_slice_calculation_and_headless_embed_serialization() -> None:
    """Verify page calculation is separated from Discord views and handles empty/boundary/out-of-range inputs."""
    # 1. Empty sequence
    empty_slice: Any = compute_page_slice([], page=0, per_page=5)
    assert empty_slice.items == ()
    assert empty_slice.page == 0
    assert empty_slice.total_pages == 1
    assert empty_slice.total_items == 0
    assert empty_slice.has_prev is False
    assert empty_slice.has_next is False

    # 2. Multi-page sequence (12 items, 5 per page -> 3 pages)
    items = list(range(12))
    s0 = compute_page_slice(items, page=-3, per_page=5)
    assert s0.page == 0
    assert s0.total_pages == 3
    assert s0.items == (0, 1, 2, 3, 4)
    assert s0.has_prev is False
    assert s0.has_next is True

    s1 = compute_page_slice(items, page=1, per_page=5)
    assert s1.page == 1
    assert s1.items == (5, 6, 7, 8, 9)
    assert s1.has_prev is True
    assert s1.has_next is True

    s2 = compute_page_slice(items, page=99, per_page=5)
    assert s2.page == 2
    assert s2.items == (10, 11)
    assert s2.has_prev is True
    assert s2.has_next is False

    # 3. Headless embed builders serialize pages without instantiating a Discord view
    settings = Settings()
    notes_embed = build_notes_page_embed(
        settings,
        course_display="CPS109",
        campus_name="TMU",
        all_fields=[(f"Source {i}", f"Link {i}", False) for i in range(10)],
        total_matches=10,
        page=1,
        total_pages=2,
    )
    assert "Page 2/2" in (notes_embed.footer.text or "")
    assert len(notes_embed.fields) == 2

    exams_embed = build_exam_schedule_embed(
        settings,
        [
            Exam(
                id=i + 1,
                guild_id=1,
                course_code=f"CPS{100 + i}",
                course_name="Comp Sci",
                start_ts=1700000000 + i * 3600,
                end_ts=None,
                location="ENG 101",
                notes="",
            )
            for i in range(12)
        ],
        "America/Toronto",
        page=1,
        total_pages=2,
    )
    assert "Page 2/2" in (exams_embed.footer.text or "")
    assert len(exams_embed.fields) == 2

    help_embed = build_help_embed(0, 2, settings)
    gs_embed = build_getting_started_embed(0, settings)
    assert help_embed.title is not None
    assert gs_embed.title is not None


def test_interaction_audience_matrix_and_ui_style_documentation() -> None:
    """Verify the explicit interaction-audience matrix, 180s paginator default, and documented exception list."""
    assert DEFAULT_PAGINATOR_TIMEOUT_SECONDS == 180.0
    expected_views = {
        "NotesPaginationView",
        "ExamPaginationView",
        "ExamClearConfirmView",
        "JobPaginationView",
        "ResumeReviewPaginationView",
        "ResumeConfirmView",
        "HelpPaginationView",
        "GettingStartedView",
        "LeaderboardView",
        "GuildSetupView",
        "AdminMarketConfirmView",
        "BlackjackView",
        "BlackjackTerminalView",
    }
    assert set(INTERACTION_AUDIENCE_MATRIX.keys()) == expected_views

    ordinary_paginators = (
        "NotesPaginationView",
        "ExamPaginationView",
        "JobPaginationView",
        "ResumeReviewPaginationView",
        "HelpPaginationView",
        "GettingStartedView",
        "LeaderboardView",
    )
    for name in ordinary_paginators:
        assert INTERACTION_AUDIENCE_MATRIX[name].default_timeout_seconds == 180.0

    assert len(SHARED_INTERACTION_EXCEPTIONS) == 3
    ui_doc = (PROJECT_ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    assert "Interaction-Audience Matrix" in ui_doc
    assert "SHARED_INTERACTION_EXCEPTIONS" in ui_doc
    assert "NotesPaginationView" in ui_doc


@pytest.mark.asyncio
async def test_shared_vs_invoker_only_navigation_simultaneous_users_and_cross_guild_isolation(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify shared guild navigation on NotesPaginationView vs. invoker-only guards and cross-guild blocks."""
    db_file = tmp_path / "interaction_matrix.db"
    db = Database(str(db_file))
    await db.init()

    guild_a = SimpleNamespace(id=910000000000000001, name="Guild A")
    guild_b = SimpleNamespace(id=910000000000000002, name="Guild B")
    user_a1 = make_member(user_id=810000000000000001)
    user_a2 = make_member(user_id=810000000000000002)
    user_b1 = make_member(user_id=810000000000000003)

    settings = Settings(db_path=str(db_file), guild_id=0)
    container = ServiceContainer.from_settings(settings, db=db)
    client = FakeClient(settings=settings, db=db, services=container)

    try:
        # 1. NotesPaginationView: Deliberate shared-navigation exception within Guild A
        notes_fields = [(f"Group {i}", f"• [Doc {i}](https://example.edu/{i})", False) for i in range(10)]
        notes_view = NotesPaginationView(
            settings=settings,
            course_display="CPS109",
            campus_name="TMU",
            all_fields=notes_fields,
            total_matches=10,
            guild_id=guild_a.id,
        )
        assert notes_view.timeout == 180.0
        assert notes_view.prev_button.disabled is True
        assert notes_view.next_button.disabled is False

        # User A1 advances to page 2 (last page)
        inter_a1 = make_interaction(user=user_a1, guild=guild_a, client=client)
        await notes_view.next_button.callback(inter_a1)
        assert notes_view.current_page == 1
        assert notes_view.prev_button.disabled is False
        assert notes_view.next_button.disabled is True

        # User A2 (another member of the SAME guild) can navigate back to page 1
        inter_a2 = make_interaction(user=user_a2, guild=guild_a, client=client)
        await notes_view.prev_button.callback(inter_a2)
        assert notes_view.current_page == 0
        assert notes_view.prev_button.disabled is True
        assert notes_view.next_button.disabled is False

        # User B1 (from a DIFFERENT guild) is rejected ephemerally
        inter_b1 = make_interaction(user=user_b1, guild=guild_b, client=client)
        await notes_view.next_button.callback(inter_b1)
        assert notes_view.current_page == 0
        assert inter_b1.last_reply.ephemeral is True
        assert "context mismatch" in (inter_b1.last_reply.content or "").lower()

        # 2. JobPaginationView: Invoker-only (User A1 owns it; simultaneous click by User A2 or Guild B fails)
        postings = [
            JobPosting(
                provider="greenhouse",
                employer=f"Employer {i}",
                external_id=f"job-{i}",
                title=f"Software Intern {i}",
                location="Toronto, ON",
                url=f"https://boards.greenhouse.io/emp/jobs/{i}",
                posted_unix=1700000000,
                text_blob="software intern toronto",
            )
            for i in range(7)
        ]
        job_view = JobPaginationView(
            settings=settings,
            postings=postings,
            preset_name="tech_swe",
            total_matched=7,
            last_updated_str="2m ago",
            owner_id=user_a1.id,
            guild_id=guild_a.id,
        )
        assert job_view.timeout == 180.0

        # Simultaneous click by User A2 (wrong user in same guild) -> denied ephemerally once, no double ack
        inter_job_stranger = make_interaction(user=user_a2, guild=guild_a, client=client)
        allowed = await job_view.interaction_check(inter_job_stranger)
        assert allowed is False
        assert len(inter_job_stranger.response.sent_messages) == 1
        assert inter_job_stranger.response.sent_messages[0].ephemeral is True

        # Direct callback invocation by User A2 -> also sends exactly 1 ephemeral denial
        inter_job_stranger2 = make_interaction(user=user_a2, guild=guild_a, client=client)
        await job_view.next_button.callback(inter_job_stranger2)
        assert job_view.current_page == 0
        assert len(inter_job_stranger2.response.sent_messages) == 1

        # Cross-guild click by User A1 from Guild B -> denied ephemerally
        user_a1_in_b = make_member(user_id=user_a1.id)
        inter_job_cross = make_interaction(user=user_a1_in_b, guild=guild_b, client=client)
        await job_view.next_button.callback(inter_job_cross)
        assert job_view.current_page == 0
        assert inter_job_cross.last_reply.ephemeral is True
        assert "context mismatch" in (inter_job_cross.last_reply.content or "").lower()

        # Full Discord.py dispatch (interaction_check -> callback) by Owner User A1 -> 1 edit, 0 sent messages
        inter_job_owner = make_interaction(user=user_a1, guild=guild_a, client=client)
        assert await job_view.interaction_check(inter_job_owner) is True
        await job_view.next_button.callback(inter_job_owner)
        assert job_view.current_page == 1
        assert len(inter_job_owner.response.sent_messages) == 0
        assert len(inter_job_owner.response.edit_messages) == 1
    finally:
        await container.close()
        await db.close()


@pytest.mark.asyncio
async def test_mid_session_feature_disablement_and_permission_revocation(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify disabling a feature or revoking admin permissions mid-session invalidates active views."""
    db_file = tmp_path / "mid_session.db"
    db = Database(str(db_file))
    await db.init()

    guild_id = 920000000000000001
    guild = SimpleNamespace(id=guild_id, name="Dynamic Gate Guild")
    user = make_member(user_id=820000000000000001, administrator=True)

    settings = Settings(db_path=str(db_file), guild_id=guild_id)
    container = ServiceContainer.from_settings(settings, db=db)
    store = GuildSettingsStore(db)
    client = FakeClient(settings=settings, db=db, services=container)

    try:
        # 1. Create active paginators & confirmation views while features are enabled
        notes_view = NotesPaginationView(
            settings=settings,
            course_display="CPS109",
            campus_name="TMU",
            all_fields=[(f"G{i}", f"V{i}", False) for i in range(10)],
            total_matches=10,
            guild_id=guild_id,
        )
        exam_view = ExamPaginationView(
            settings=settings,
            exams=[
                Exam(
                    id=i + 1,
                    guild_id=guild_id,
                    course_code=f"C{i}",
                    course_name="Course",
                    start_ts=1700000000 + i,
                    end_ts=None,
                    location="Room",
                    notes="",
                )
                for i in range(12)
            ],
            tz_name="America/Toronto",
            owner_id=user.id,
            guild_id=guild_id,
        )
        job_view = JobPaginationView(
            settings=settings,
            postings=[
                JobPosting(
                    provider="greenhouse",
                    employer="Stripe",
                    external_id=f"j{i}",
                    title="Software Intern",
                    location="Toronto",
                    url="https://boards.greenhouse.io/stripe/jobs/1",
                    posted_unix=1700000000,
                    text_blob="software intern",
                )
                for i in range(7)
            ],
            preset_name="tech_swe",
            total_matched=7,
            last_updated_str="1m ago",
            owner_id=user.id,
            guild_id=guild_id,
        )
        resume_pag = ResumeReviewPaginationView(
            settings=settings,
            pages=["Page 1 content", "Page 2 content"],
            discipline_title="SWE",
            user_id=user.id,
            guild_id=guild_id,
        )
        lb_view = LeaderboardView(
            owner_id=user.id,
            settings=settings,
            snaps=[
                SimpleNamespace(user_id=user.id, net_worth=1000, cash=1000, long_value=0, short_equity=0)
                for _ in range(12)
            ],
            guild_id=guild_id,
        )
        mkt_confirm = AdminMarketConfirmView(
            admin_user_id=user.id,
            settings=settings,
            guild_id=guild_id,
        )

        # 2. Disable academics, careers, economy, and market mid-session in guild_settings
        await store.set_overrides(
            guild_id,
            {"disabled_features": ["academics", "careers", "economy", "market"]},
        )

        for _active_view, btn in [
            (notes_view, notes_view.next_button),
            (exam_view, exam_view.next_button),
            (job_view, job_view.next_button),
            (resume_pag, resume_pag.next_button),
            (lb_view, lb_view.next_btn),
            (mkt_confirm, mkt_confirm.confirm),
        ]:
            inter = make_interaction(user=user, guild=guild, client=client)
            await btn.callback(inter)
            assert inter.last_reply.ephemeral is True
            assert "disabled" in (inter.last_reply.content or "").lower()

        assert notes_view.current_page == 0
        assert exam_view.current_page == 0
        assert job_view.current_page == 0
        assert resume_pag.current_page == 0
        assert lb_view.state.page == 0
        assert mkt_confirm.confirmed is None

        # 3. Re-enable features, then revoke user's admin permission mid-session on GuildSetupView & ExamClearConfirmView
        await store.set_overrides(guild_id, {"disabled_features": []})
        setup_view = GuildSetupView(guild_id, user.id, container, settings)
        clear_view = ExamClearConfirmView(
            owner_id=user.id,
            guild_id=guild_id,
            settings=settings,
            db=db,
            pending_count=3,
        )

        demoted_user = make_member(user_id=user.id, administrator=False, role_ids=[])
        inter_setup_demoted = make_interaction(user=demoted_user, guild=guild, client=client)
        assert await setup_view.interaction_check(inter_setup_demoted) is False
        assert "permission" in (inter_setup_demoted.last_reply.content or "").lower()

        inter_clear_demoted = make_interaction(user=demoted_user, guild=guild, client=client)
        await clear_view.confirm_button.callback(inter_clear_demoted)
        assert clear_view.confirmed is False
        assert "permission" in (inter_clear_demoted.last_reply.content or "").lower()
    finally:
        await container.close()
        await db.close()


@pytest.mark.asyncio
async def test_post_timeout_expiry_empty_pages_and_discord_403_404_429_resilience(
    make_interaction,
    make_member,
) -> None:
    """Verify empty/single-page button states, post-timeout denial, and 403/404/429 resilience on edit."""
    settings = Settings()
    guild = SimpleNamespace(id=930000000000000001, name="Resilience Guild")
    user = make_member(user_id=830000000000000001, administrator=True)

    # 1. Empty / single-page dataset disables both Previous and Next buttons immediately
    empty_jobs_view = JobPaginationView(
        settings=settings,
        postings=[],
        preset_name="tech_swe",
        total_matched=0,
        last_updated_str="",
        owner_id=user.id,
        guild_id=guild.id,
    )
    assert empty_jobs_view.total_pages == 1
    assert empty_jobs_view.prev_button.disabled is True
    assert empty_jobs_view.next_button.disabled is True

    gs_view = GettingStartedView(current_step=0, settings=settings, author_id=user.id, guild_id=guild.id)
    assert gs_view.timeout == 180.0
    assert gs_view.prev_button.disabled is True
    assert gs_view.next_button.disabled is False

    # 2. Multi-page view: timeout disables buttons, edits message, and rejects subsequent clicks
    multi_view = ResumeReviewPaginationView(
        settings=settings,
        pages=["Page 1", "Page 2"],
        discipline_title="SWE",
        user_id=user.id,
        guild_id=guild.id,
    )
    assert multi_view.timeout == 180.0
    assert multi_view.prev_button.disabled is True
    assert multi_view.next_button.disabled is False

    # Attach a fake message that raises 404 NotFound on timeout edit -> must not raise
    class _DeletedMessage:
        async def edit(self, **kwargs: Any) -> None:
            raise _http_exc(discord.NotFound, 404, "Unknown Message")

    multi_view.message = _DeletedMessage()
    await multi_view.on_timeout()
    assert multi_view.prev_button.disabled is True
    assert multi_view.next_button.disabled is True

    # Clicking Next after timeout is rejected ephemerally
    inter_after_expiry = make_interaction(user=user, guild=guild)
    await multi_view.next_button.callback(inter_after_expiry)
    assert multi_view.current_page == 0
    assert inter_after_expiry.last_reply.ephemeral is True
    assert "expired" in (inter_after_expiry.last_reply.content or "").lower()

    # 3. ResumeConfirmView timeout & post-timeout click rejection
    fake_attachment = SimpleNamespace(
        filename="resume.pdf", size=10240, url="https://cdn.discord.invalid/r.pdf"
    )
    init_inter = make_interaction(user=user, guild=guild)
    resume_confirm = ResumeConfirmView(
        interaction=init_inter,  # type: ignore[arg-type]
        attachment=fake_attachment,  # type: ignore[arg-type]
        discipline_key="swe",
        discipline_title="SWE",
        target_role="Intern",
        settings=settings,
    )
    resume_confirm.message = _DeletedMessage()
    await resume_confirm.on_timeout()
    assert all(isinstance(c, discord.ui.Button) and c.disabled for c in resume_confirm.children)

    inter_confirm_expired = make_interaction(user=user, guild=guild)
    await resume_confirm.confirm_button.callback(inter_confirm_expired)
    assert inter_confirm_expired.last_reply.ephemeral is True
    assert "expired" in (inter_confirm_expired.last_reply.content or "").lower()

    # 4. Discord 403 Forbidden, 404 NotFound, and 429 RateLimited during edit_message never raise tracebacks
    for exc in (
        _http_exc(discord.Forbidden, 403, "Missing Access"),
        _http_exc(discord.NotFound, 404, "Unknown Message"),
        _http_exc(discord.HTTPException, 429, "You are being rate limited"),
    ):
        help_view = HelpPaginationView(
            current_page=0,
            total_pages=3,
            settings=settings,
            author_id=user.id,
            guild_id=guild.id,
        )
        err_inter = make_interaction(user=user, guild=guild)

        async def _fail_edit(*args: Any, _exc: Exception = exc, **kwargs: Any) -> None:
            raise _exc

        err_inter.response.edit_message = _fail_edit  # type: ignore[method-assign]
        # Must complete without raising an exception
        await help_view.next_button.callback(err_inter)
        assert help_view.current_page == 1

        # safe_timeout_disable also absorbs 403/404/429 cleanly
        class _FailingMsg:
            async def edit(self, *args: Any, _exc: Exception = exc, **kwargs: Any) -> None:
                raise _exc

        await safe_timeout_disable(help_view, _FailingMsg())

    # 5. safe_edit_view_message fallback when interaction.response.is_done() is True
    from tests.helpers import FakeMessage

    backing_msg = FakeMessage(content="Initial")
    done_inter = make_interaction(user=user, guild=guild, message=backing_msg)
    await done_inter.response.defer()
    edited_ok = await safe_edit_view_message(done_inter, content="Updated after defer")
    assert edited_ok is True
