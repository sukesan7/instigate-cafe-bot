"""Regression tests for release claim accuracy, entrypoint permission boundaries, and error log sanitization."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.admin import llm_start_cmd, llm_stop_cmd
from icbot.features.blackjack import blackjack
from icbot.features.info import botinfo
from icbot.features.invest import (
    admin_move,
    admin_set,
    buy,
    cover,
    history,
    market,
    portfolio,
    quote,
    sell,
    short,
    stock,
    sync_catalog_cmd,
    trades,
    weekroll_cmd,
)
from icbot.features.jobs import (
    JobPaginationView,
    job_preferences_cmd,
    job_preview_cmd,
    job_subscribe_cmd,
    jobs_cmd,
)
from icbot.features.profile import daily_cmd, profile_cmd
from icbot.features.report import report_cmd
from icbot.features.resume import ResumeConfirmView
from icbot.features.roulette import roulette_cmd
from icbot.features.slots import slots_cmd, slots_payouts_cmd
from icbot.logging import RedactingLogFilter, StructuredLogFormatter
from icbot.services.jobs.models import JobPosting
from tests.helpers import FakeClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_public_copy_and_documentation_claims_are_accurate() -> None:
    """Verify overstated encryption, WCAG AA, provably fair, and live certification claims are absent."""
    jobs_src = (PROJECT_ROOT / "icbot/features/jobs.py").read_text(encoding="utf-8")
    assert "Local Encrypted Storage" not in jobs_src
    assert "Local SQLite Storage (Plaintext" in jobs_src
    assert "portable across servers on this bot instance" in jobs_src

    theme_src = (PROJECT_ROOT / "icbot/theme.py").read_text(encoding="utf-8")
    assert "WCAG AA compliant" not in theme_src
    assert "relative luminance heuristic" in theme_src

    readme_src = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "provably fair" not in readme_src.lower()
    assert "unit-tested PRNG game engines" in readme_src
    assert "LLM_CHANNEL_HISTORY_ENABLED=false" in readme_src
    assert "docs/release-evidence.md" in readme_src

    launch_src = (PROJECT_ROOT / "docs/launch-assets.md").read_text(encoding="utf-8")
    assert "GDPR-compliant" not in launch_src

    evidence_path = PROJECT_ROOT / "docs/release-evidence.md"
    assert evidence_path.is_file()
    evidence_src = evidence_path.read_text(encoding="utf-8")
    assert "Claim-to-Evidence Traceability Matrix" in evidence_src
    assert "Not Yet Verified" in evidence_src


@pytest.mark.asyncio
async def test_job_preferences_embed_discloses_plaintext_sqlite_and_portable_user_scope(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify /ic job_preferences responds ephemerally and honestly states plaintext SQLite + portable user scope."""
    db_file = tmp_path / "jobs_claim.db"
    db = Database(str(db_file))
    await db.init()
    settings = Settings(db_path=str(db_file), guild_id=900000000000000001)
    container = ServiceContainer.from_settings(settings, db=db)
    client = FakeClient(settings=settings, db=db, services=container)
    member = make_member(user_id=860000000000000001, administrator=False)

    try:
        inter = make_interaction(user=member, command_name="ic job_preferences", client=client)
        await job_preferences_cmd.callback(
            inter,
            preset="tech_swe",
            locations="Toronto, Remote",
            keywords="python, backend",
            remote_only=False,
        )
        assert inter.response.ephemeral is True
        msg = inter.followup.messages[0]
        assert msg.ephemeral is True
        assert msg.embed is not None
        footer_text = msg.embed.footer.text or ""
        assert "Local Encrypted Storage" not in footer_text
        assert "Plaintext" in footer_text
        assert "portable across servers on this bot instance" in (msg.embed.description or "")
    finally:
        await container.close()
        await db.close()


@pytest.mark.asyncio
async def test_resume_and_botinfo_disclose_platform_and_third_party_retention_boundaries(
    make_interaction,
    make_member,
) -> None:
    """Verify /ic resume disclosure and /ic botinfo distinguish local zero-disk storage from Discord/Gemini policies."""
    settings = Settings(guild_id=900000000000000001)
    member = make_member(user_id=860000000000000002, administrator=False)
    inter = make_interaction(user=member, command_name="ic botinfo")

    await botinfo.callback(inter)
    botinfo_embed = inter.last_reply.embed
    assert botinfo_embed is not None
    privacy_field = next(f for f in botinfo_embed.fields if "Privacy" in (f.name or ""))
    assert "LLM_CHANNEL_HISTORY_ENABLED=false" in (privacy_field.value or "")
    assert "Discord CDN and Google Gemini API retention policies apply" in (privacy_field.value or "")

    fake_attachment = SimpleNamespace(
        filename="resume.pdf", size=24_576, url="https://cdn.discord.invalid/resume.pdf"
    )
    view = ResumeConfirmView(
        interaction=inter,  # type: ignore[arg-type]
        attachment=fake_attachment,  # type: ignore[arg-type]
        discipline_key="swe",
        discipline_title="Software Engineering & Tech",
        target_role="Backend Intern",
        settings=settings,
    )
    disc_embed = view.build_disclosure_embed()
    desc = disc_embed.description or ""
    assert "Google Cloud / Gemini API terms" in desc
    assert "Discord's Privacy Policy" in desc
    assert "local SQLite database or host disk" in desc


@pytest.mark.asyncio
async def test_admin_economy_casino_market_and_career_entrypoints_enforce_guild_and_feature_gates(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify DM rejection, non-admin rejection, and disabled-feature gates across all command entrypoints."""
    db_file = tmp_path / "entrypoints.db"
    db = Database(str(db_file))
    await db.init()

    guild = SimpleNamespace(id=900000000000000001, name="Gate Test Guild")
    admin_member = make_member(user_id=861000000000000001, administrator=True)
    regular_member = make_member(user_id=861000000000000002, administrator=False, role_ids=[])

    # 1. DM context (interaction.guild = None) rejected across admin, economy, casino, and market commands
    settings_full = Settings(db_path=str(db_file), guild_id=guild.id, feature_profile="community_full")
    container_full = ServiceContainer.from_settings(settings_full, db=db)
    client_full = FakeClient(settings=settings_full, db=db, services=container_full)

    try:
        dm_commands = [
            (llm_stop_cmd, {}),
            (llm_start_cmd, {}),
            (profile_cmd, {}),
            (daily_cmd, {}),
            (blackjack, {"bet": 10}),
            (roulette_cmd, {"bet": 10, "bet_type": "red"}),
            (slots_cmd, {"bet": 10}),
            (slots_payouts_cmd, {}),
            (market, {}),
            (quote, {"symbol": "IC"}),
            (portfolio, {}),
            (buy, {"symbol": "IC", "qty": 1}),
            (sell, {"symbol": "IC", "qty": 1}),
            (short, {"symbol": "IC", "qty": 1}),
            (cover, {"symbol": "IC", "qty": 1}),
            (trades, {}),
            (history, {}),
            (stock, {}),
            (admin_move, {"symbol": "IC", "change": "+5"}),
            (admin_set, {"symbol": "IC", "value": 100}),
            (sync_catalog_cmd, {}),
            (weekroll_cmd, {}),
        ]
        for cmd, kwargs in dm_commands:
            dm_inter = make_interaction(user=admin_member, guild=guild, client=client_full)
            dm_inter.guild = None
            await cmd.callback(dm_inter, **kwargs)
            assert dm_inter.last_reply.ephemeral is True, f"{cmd.name} did not reject DM ephemerally"
            assert "within a server" in (dm_inter.last_reply.content or ""), (
                f"{cmd.name} allowed DM execution"
            )

        # 2. Non-admin member rejected on admin market and LLM killswitch commands
        admin_only_cmds = [
            (llm_stop_cmd, {}),
            (llm_start_cmd, {}),
            (admin_move, {"symbol": "IC", "change": "+5"}),
            (admin_set, {"symbol": "IC", "value": 100}),
            (sync_catalog_cmd, {}),
            (weekroll_cmd, {}),
        ]
        for cmd, kwargs in admin_only_cmds:
            unauth = make_interaction(user=regular_member, guild=guild, client=client_full)
            await cmd.callback(unauth, **kwargs)
            assert unauth.last_reply.ephemeral is True
            assert "permission" in (unauth.last_reply.content or "").lower()

        # 3. Feature gate enforcement when market, casino, economy, and careers are disabled
        settings_disabled = Settings(
            db_path=str(db_file),
            guild_id=guild.id,
            disabled_features=["market", "casino", "economy", "careers"],
        )
        container_disabled = ServiceContainer.from_settings(settings_disabled, db=db)
        client_disabled = FakeClient(settings=settings_disabled, db=db, services=container_disabled)
        try:
            gated_commands = [
                (market, {}),
                (quote, {"symbol": "IC"}),
                (portfolio, {}),
                (trades, {}),
                (history, {}),
                (stock, {}),
                (report_cmd, {}),
                (slots_payouts_cmd, {}),
                (jobs_cmd, {}),
                (job_subscribe_cmd, {}),
                (job_preferences_cmd, {}),
                (job_preview_cmd, {"title": "Software Intern"}),
            ]
            for cmd, kwargs in gated_commands:
                gated_inter = make_interaction(user=admin_member, guild=guild, client=client_disabled)
                await cmd.callback(gated_inter, **kwargs)
                assert gated_inter.last_reply.ephemeral is True, f"{cmd.name} bypassed feature gate"
                assert (
                    "disabled" in (gated_inter.last_reply.content or "").lower()
                    or "unavailable" in (gated_inter.last_reply.content or "").lower()
                )
        finally:
            await container_disabled.close()

        # 4. JobPaginationView rejects page navigation by another user
        sample_postings = [
            JobPosting(
                provider="greenhouse",
                employer="Corp",
                external_id=f"id-{i}",
                title=f"Software Engineer Intern {i}",
                location="Toronto",
                url=f"https://example.invalid/{i}",
                posted_unix=1_760_000_000,
                text_blob="swe intern",
            )
            for i in range(7)
        ]
        job_view = JobPaginationView(
            settings=settings_full,
            postings=sample_postings,
            preset_name="tech_swe",
            total_matched=7,
            last_updated_str="1m ago",
            owner_id=regular_member.id,
        )
        hijack_inter = make_interaction(user=admin_member, guild=guild, client=client_full)
        await job_view.next_button.callback(hijack_inter)
        assert hijack_inter.last_reply.ephemeral is True
        assert "only the user who ran" in (hijack_inter.last_reply.content or "").lower()
        assert job_view.current_page == 0
    finally:
        await container_full.close()
        await db.close()


def test_log_redaction_scrubs_exception_args_and_exc_info_tracebacks() -> None:
    """Verify RedactingLogFilter + StructuredLogFormatter scrub secrets in %s Exception args and exc_info tracebacks."""
    # Construct synthetic test key dynamically so static file scanner (scan_secrets.py) does not flag source literal
    raw_gemini_key = "AIza" + "SyD-9876543210abcdefghijklmnopqrstu"
    raw_custom_secret = "super-secret-metrolinx-token-998877"
    redactor = RedactingLogFilter(secrets=(raw_custom_secret,))
    formatter = StructuredLogFormatter("%(levelname)s:%(name)s:%(message)s")

    # Case 1: Exception passed as %s arg in record.args (not a plain str)
    exc_arg = RuntimeError(
        f"GET https://api.example.invalid/v1?api_key={raw_gemini_key} failed with token {raw_custom_secret}"
    )
    record1 = logging.LogRecord(
        name="icbot.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=100,
        msg="Upstream fetch error: %s",
        args=(exc_arg,),
        exc_info=None,
    )
    assert redactor.filter(record1) is True
    formatted1 = formatter.format(record1)
    assert raw_gemini_key not in formatted1
    assert raw_custom_secret not in formatted1
    assert "[REDACTED]" in formatted1

    # Case 2: Exception logged via exc_info=True (traceback formatted by Formatter)
    try:
        raise ValueError(f"Authorization: Bearer {raw_custom_secret} key={raw_gemini_key}")
    except ValueError as err:
        import sys

        record2 = logging.LogRecord(
            name="icbot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=120,
            msg="Unhandled provider exception",
            args=(),
            exc_info=sys.exc_info(),
        )
        assert err is not None

    assert redactor.filter(record2) is True
    formatted2 = formatter.format(record2)
    assert raw_gemini_key not in formatted2
    assert raw_custom_secret not in formatted2
    assert "[REDACTED]" in formatted2
