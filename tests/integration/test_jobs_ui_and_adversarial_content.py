"""Integration tests for jobs UI refactoring, per-guild theme semantics, and adversarial ATS content."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.db import Database
from icbot.features.jobs import (
    JobPaginationView,
    job_preferences_cmd,
    job_preview_cmd,
    job_subscribe_cmd,
    jobs_cmd,
)
from icbot.guild_config import GuildSettingsStore
from icbot.services.jobs.models import JobPosting
from tests.helpers import FakeClient, load_fixture_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _assert_discord_embed_limits(embed: discord.Embed) -> None:
    """Assert that a Discord embed strictly satisfies all Discord API character and field limits."""
    assert len(embed) <= 6000
    if embed.title:
        assert len(embed.title) <= 256
    if embed.description:
        assert len(embed.description) <= 4096
    if embed.author and embed.author.name:
        assert len(embed.author.name) <= 256
    if embed.footer and embed.footer.text:
        assert len(embed.footer.text) <= 256
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.name or "") <= 256
        assert len(f.value or "") <= 1024


def test_zero_raw_discord_embed_constructors_in_feature_modules() -> None:
    """Verify no feature module in icbot/features/ directly calls discord.Embed(...)."""
    features_dir = PROJECT_ROOT / "icbot" / "features"
    violations: list[str] = []

    for py_file in sorted(features_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_discord_embed = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "Embed"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "discord"
                ) or (isinstance(func, ast.Name) and func.id == "Embed")
                if is_discord_embed:
                    violations.append(f"{py_file.name}:{node.lineno}")

    assert violations == [], f"Raw discord.Embed constructors found in feature modules: {violations}"


@pytest.mark.asyncio
async def test_all_four_jobs_display_paths_use_effective_guild_theme_and_consistent_footers(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify /ic jobs, /ic job_subscribe, /ic job_preferences, and /ic job_preview resolve per-guild themes."""
    db_file = tmp_path / "jobs_themes.db"
    db = Database(str(db_file))
    await db.init()

    guild_id = 910000000000000001
    guild = SimpleNamespace(id=guild_id, name="TMU Engineering Guild", me=SimpleNamespace())
    admin_member = make_member(user_id=870000000000000001, administrator=True)

    # Global bot settings use default instigate_cafe purple (0xA64DFF),
    # while guild override uses custom blue (0x004C97) and custom brand footer/display name.
    base_settings = Settings(
        db_path=str(db_file),
        guild_id=guild_id,
        embed_accent_color=0xA64DFF,
        embed_footer_text="Instigate Cafe",
        bot_display_name="InstigateAI",
    )
    await GuildSettingsStore(db).set_overrides(
        guild_id,
        {
            "bot_display_name": "TMU Careers Hub",
            "embed_footer_text": "TMU Co-op Portal",
            "embed_accent_color": 0x004C97,
        },
    )

    container = ServiceContainer.from_settings(base_settings, db=db)
    container.clock = lambda: 1700003600.0
    client = FakeClient(settings=base_settings, db=db, services=container)

    # Seed realistic postings loaded from offline provider fixtures
    gh_payload = load_fixture_json("greenhouse_jobs.json")
    lv_payload = load_fixture_json("lever_jobs.json")

    fixture_rows: list[tuple[str, str, str, str, str, str, int, int]] = []
    for item in gh_payload.get("jobs", []):
        loc_obj = item.get("location") or {}
        fixture_rows.append(
            (
                "greenhouse:examplecorp",
                str(item["id"]),
                str(item["absolute_url"]),
                str(item["title"]),
                "ExampleCorp",
                str(loc_obj.get("name") or "Remote"),
                1700000000,
                1700003000,
            )
        )
    for item in lv_payload:
        cats = item.get("categories") or {}
        fixture_rows.append(
            (
                "lever:examplesite",
                str(item["id"]),
                str(item["hostedUrl"]),
                str(item["text"]),
                "ExampleSite",
                str(cats.get("location") or "Remote"),
                1700000000,
                1700003000,
            )
        )

    for row in fixture_rows:
        await db.execute(
            """
            INSERT OR REPLACE INTO job_seen (
                source, external_id, url, title, company, location, posted_unix, first_seen_unix
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    try:
        # 1. /ic jobs (healthy state -> guild theme accent 0x004C97)
        inter_jobs = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic jobs",
            client=client,
        )
        await jobs_cmd.callback(inter_jobs, limit=10, preset="tech_swe")
        jobs_embed = inter_jobs.followup.messages[0].embed
        assert jobs_embed is not None
        _assert_discord_embed_limits(jobs_embed)
        assert jobs_embed.color.value == 0x004C97
        assert jobs_embed.author.name == "TMU Careers Hub"
        assert jobs_embed.footer.text == "TMU Co-op Portal • Jobs • Page 1/1"
        assert "Displaying cached postings ingested from configured" in (jobs_embed.description or "")
        assert "`10m ago`" in (jobs_embed.description or "")

        # 1b. /ic jobs (degraded provider state -> semantic warning 0xF39C12)
        container.jobs_bad_sources.add("greenhouse:stripe")
        inter_jobs_deg = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic jobs",
            client=client,
        )
        await jobs_cmd.callback(inter_jobs_deg, limit=10, preset="tech_swe")
        deg_embed = inter_jobs_deg.followup.messages[0].embed
        assert deg_embed is not None
        assert deg_embed.color.value == 0xF39C12
        assert "⚠️ *ATS provider feeds are cooling down or unreachable" in (deg_embed.description or "")
        container.jobs_bad_sources.clear()

        # 2. /ic job_subscribe (list action -> guild theme accent 0x004C97)
        await db.set_guild_job_subscription(
            guild_id,
            920000000000000001,
            preset="tech_swe",
            role_id=930000000000000001,
            enabled=True,
        )
        inter_sub = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic job_subscribe",
            client=client,
        )
        await job_subscribe_cmd.callback(inter_sub, action="list")
        sub_embed = inter_sub.followup.messages[0].embed
        assert sub_embed is not None
        _assert_discord_embed_limits(sub_embed)
        assert sub_embed.color.value == 0x004C97
        assert sub_embed.author.name == "TMU Careers Hub"
        assert sub_embed.footer.text == "TMU Co-op Portal • Jobs • Subscriptions"

        # 3. /ic job_preferences (read -> guild theme accent 0x004C97; update -> semantic success 0x2ECC71)
        inter_pref_read = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic job_preferences",
            client=client,
        )
        await job_preferences_cmd.callback(inter_pref_read)
        pref_read_embed = inter_pref_read.followup.messages[0].embed
        assert pref_read_embed is not None
        _assert_discord_embed_limits(pref_read_embed)
        assert pref_read_embed.color.value == 0x004C97
        assert pref_read_embed.author.name == "TMU Careers Hub"
        assert (
            pref_read_embed.footer.text
            == "TMU Co-op Portal • Jobs • Local SQLite Storage (Plaintext on Host Disk)"
        )

        inter_pref_upd = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic job_preferences",
            client=client,
        )
        await job_preferences_cmd.callback(
            inter_pref_upd,
            preset="tech_swe",
            locations="Toronto, Waterloo",
            keywords="python, backend",
            remote_only=False,
        )
        pref_upd_embed = inter_pref_upd.followup.messages[0].embed
        assert pref_upd_embed is not None
        _assert_discord_embed_limits(pref_upd_embed)
        assert pref_upd_embed.color.value == 0x2ECC71
        assert pref_upd_embed.author.name == "TMU Careers Hub"
        assert (
            pref_upd_embed.footer.text
            == "TMU Co-op Portal • Jobs • Local SQLite Storage (Plaintext on Host Disk)"
        )

        # 4. /ic job_preview (matched -> semantic success 0x2ECC71; rejected -> semantic error 0xE74C3C)
        inter_prev_ok = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic job_preview",
            client=client,
        )
        await job_preview_cmd.callback(
            inter_prev_ok,
            title="Software Engineer Intern",
            employer="Stripe",
            location="Toronto, ON",
            preset="tech_swe",
        )
        prev_ok_embed = inter_prev_ok.followup.messages[0].embed
        assert prev_ok_embed is not None
        _assert_discord_embed_limits(prev_ok_embed)
        assert prev_ok_embed.color.value == 0x2ECC71
        assert prev_ok_embed.author.name == "TMU Careers Hub"
        assert prev_ok_embed.footer.text == "TMU Co-op Portal • Jobs • Deterministic Rule Engine Preview"

        inter_prev_rej = make_interaction(
            user=admin_member,
            guild=guild,
            command_name="ic job_preview",
            client=client,
        )
        await job_preview_cmd.callback(
            inter_prev_rej,
            title="Senior Director of Engineering",
            employer="Stripe",
            location="London, UK",
            preset="tech_swe",
        )
        prev_rej_embed = inter_prev_rej.followup.messages[0].embed
        assert prev_rej_embed is not None
        _assert_discord_embed_limits(prev_rej_embed)
        assert prev_rej_embed.color.value == 0xE74C3C
        assert prev_rej_embed.author.name == "TMU Careers Hub"
        assert prev_rej_embed.footer.text == "TMU Co-op Portal • Jobs • Deterministic Rule Engine Preview"
    finally:
        await container.close()
        await db.close()


@pytest.mark.asyncio
async def test_adversarial_ats_content_https_validation_date_semantics_and_degraded_empty_states(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify adversarial ATS fields, HTTPS link checks, unknown date semantics, and empty/stale cards."""
    s = Settings(
        embed_accent_color=0x38BDF8,
        embed_footer_text="Cyber Campus",
        bot_display_name="CyberBot",
    )

    # 1. Adversarial postings in JobPaginationView:
    #    - Mention injection (@everyone, @here, <@&999>, <@123>)
    #    - Markdown spoofing (**bold**, [fake](https://evil.invalid), backticks)
    #    - Unsafe URL schemes (http://, javascript:, data:, missing, unbalanced parens)
    #    - Unknown timestamps (posted_unix=0, last_updated_str="") -> never invents "Recently"
    #    - Giant strings (3,000+ chars)
    adversarial_postings = [
        JobPosting(
            provider="greenhouse",
            employer="**EvilCorp** @everyone <@&999888777> [Spoof](https://phish.invalid)",
            external_id="adv-1",
            title="Software Intern `injection` @here <@123456789> " + ("A" * 500),
            location="Toronto `ON` @everyone " + ("L" * 400),
            url="javascript:alert(document.cookie)",
            posted_unix=0,
            text_blob="software intern toronto",
            career_level="intern",
        ),
        JobPosting(
            provider="lever",
            employer="PlainHTTP Corp",
            external_id="adv-2",
            title="Backend Engineering Intern",
            location="Remote, Canada",
            url="http://insecure.example.com/apply/123",
            posted_unix=0,
            text_blob="backend engineering intern remote",
            career_level="intern",
        ),
        JobPosting(
            provider="workday",
            employer="ParenBreak [Corp]",
            external_id="adv-3",
            title="Data Science Co-op",
            location="Waterloo, ON",
            url="https://valid.example.com/job) [ClickMe](https://evil.invalid",
            posted_unix=1700000000,
            text_blob="data science co-op waterloo",
            career_level="intern",
        ),
        JobPosting(
            provider="greenhouse",
            employer="Verified [Employer]",
            external_id="adv-4",
            title="Platform Engineering Intern",
            location="Toronto, ON",
            url="https://boards.greenhouse.io/verified/jobs/42",
            posted_unix=1700000000,
            text_blob="platform engineering intern toronto",
            career_level="intern",
        ),
    ]

    view = JobPaginationView(
        settings=s,
        postings=adversarial_postings,
        preset_name="tech_swe",
        total_matched=len(adversarial_postings),
        last_updated_str="",  # Absent cache timestamp -> must not invent 'Recently'
        is_degraded=False,
        query="@everyone `query`",
        location_filter="<@&888777666> Toronto",
        owner_id=42,
    )
    embed = view.build_current_embed()
    _assert_discord_embed_limits(embed)

    # No 'Recently' invented anywhere when timestamps are absent
    full_text = (embed.description or "") + "\n" + "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    assert "Recently" not in full_text
    assert "🕒 **Cached**" not in (embed.description or "")

    # Mentions are neutralized with zero-width space across description and fields
    assert "@everyone" not in full_text
    assert "@here" not in full_text
    assert "<@&999888777>" not in full_text
    assert "<@&888777666>" not in full_text
    assert "<@123456789>" not in full_text

    # Posting 1 (javascript: URL and posted_unix=0): link rejected, date says "Date not listed"
    f0 = embed.fields[0]
    assert "\\*\\*EvilCorp\\*\\*" in f0.name
    assert "Date not listed" in f0.value
    assert "Link unavailable" in f0.value
    assert "javascript:" not in f0.value

    # Posting 2 (http:// non-HTTPS URL): rejected by HTTPS-only job link validator
    f1 = embed.fields[1]
    assert "Link unavailable" in f1.value
    assert "http://insecure.example.com" not in f1.value

    # Posting 3 (parenthesis injection URL + valid posted_unix): link rejected, timestamp rendered
    f2 = embed.fields[2]
    assert "<t:1700000000:R>" in f2.value
    assert "Link unavailable" in f2.value
    assert "evil.invalid" not in f2.value

    # Posting 4 (valid HTTPS URL with brackets in company name): brackets escaped, HTTPS link rendered
    f3 = embed.fields[3]
    assert "[Apply on Verified \\[Employer\\]](https://boards.greenhouse.io/verified/jobs/42)" in f3.value

    # 2. Empty cache & degraded provider states in /ic jobs produce intelligible empty_state_embed cards
    db_file = tmp_path / "jobs_empty.db"
    db = Database(str(db_file))
    await db.init()
    guild = SimpleNamespace(id=910000000000000099, name="Empty State Guild")
    member = make_member(user_id=870000000000000099, administrator=False)
    container = ServiceContainer.from_settings(s, db=db)
    container.jobs_bad_sources.add("greenhouse:outage")
    client = FakeClient(settings=s, db=db, services=container)

    try:
        # Case A: job_seen is empty while providers are degraded -> warning-colored empty_state_embed card
        inter_empty = make_interaction(user=member, guild=guild, command_name="ic jobs", client=client)
        await jobs_cmd.callback(inter_empty)
        empty_msg = inter_empty.followup.messages[0]
        assert empty_msg.embed is not None
        _assert_discord_embed_limits(empty_msg.embed)
        assert empty_msg.embed.color.value == 0xF39C12  # warning semantic color
        assert "No Cached Listings" in (empty_msg.embed.title or "")
        assert "cooling down or unreachable" in (empty_msg.embed.description or "")
        assert empty_msg.embed.footer.text == "Cyber Campus • Jobs"

        # Case B: job_seen has a non-matching row while degraded -> warning-colored no-matches card
        await db.execute(
            """
            INSERT INTO job_seen (source, external_id, url, title, company, location, posted_unix, first_seen_unix)
            VALUES ('greenhouse:stripe', '1', 'https://boards.greenhouse.io/stripe/jobs/1',
                    'Registered Nurse Intern', 'Hospital', 'Vancouver, BC', 0, 0)
            """
        )
        inter_nomatch = make_interaction(user=member, guild=guild, command_name="ic jobs", client=client)
        await jobs_cmd.callback(inter_nomatch, preset="tech_swe", location="Toronto")
        nomatch_msg = inter_nomatch.followup.messages[0]
        assert nomatch_msg.embed is not None
        _assert_discord_embed_limits(nomatch_msg.embed)
        assert nomatch_msg.embed.color.value == 0xF39C12
        assert "No Matching Roles" in (nomatch_msg.embed.title or "")
        assert "rate-limited or unreachable" in (nomatch_msg.embed.description or "")
        assert nomatch_msg.embed.footer.text == "Cyber Campus • Jobs"

        # Case C: Adversarial user input in /ic job_preferences and /ic job_preview is neutralized
        inter_adv_pref = make_interaction(
            user=member, guild=guild, command_name="ic job_preferences", client=client
        )
        await job_preferences_cmd.callback(
            inter_adv_pref,
            preset="tech_swe",
            locations="@everyone `Toronto`, <@&999888777>",
            keywords="**spoof**, @here `python`",
        )
        adv_pref_embed = inter_adv_pref.followup.messages[0].embed
        assert adv_pref_embed is not None
        _assert_discord_embed_limits(adv_pref_embed)
        assert "@everyone" not in (adv_pref_embed.description or "")
        assert "@here" not in (adv_pref_embed.description or "")
        assert "<@&999888777>" not in (adv_pref_embed.description or "")

        inter_adv_prev = make_interaction(
            user=member, guild=guild, command_name="ic job_preview", client=client
        )
        await job_preview_cmd.callback(
            inter_adv_prev,
            title="Software Intern @everyone `inject` " + ("T" * 400),
            employer="**FakeEmployer** <@&999888777>",
            location="Toronto @here",
        )
        adv_prev_embed = inter_adv_prev.followup.messages[0].embed
        assert adv_prev_embed is not None
        _assert_discord_embed_limits(adv_prev_embed)
        prev_blob = (adv_prev_embed.description or "") + "\n".join(
            f"{f.name}\n{f.value}" for f in adv_prev_embed.fields
        )
        assert "@everyone" not in prev_blob
        assert "@here" not in prev_blob
        assert "<@&999888777>" not in prev_blob
    finally:
        await container.close()
        await db.close()


@pytest.mark.asyncio
async def test_jobs_explicit_query_location_override_and_degraded_ratio_threshold(
    tmp_path: Path,
    make_interaction,
    make_member,
) -> None:
    """Verify explicit query/location filters narrow results over broad env allowlists and 1 dead board of many is not degraded."""
    db_file = tmp_path / "jobs_filters.db"
    db = Database(str(db_file))
    await db.init()

    s = Settings(
        db_path=str(db_file),
        greenhouse_boards=["stripe", "shopify", "figma", "databricks", "cloudflare", "airbnb"],
        jobs_location_allowlist=["toronto", "new york", "seattle", "remote"],
        jobs_keywords_allowlist=["software", "developer", "backend", "python"],
    )
    container = ServiceContainer.from_settings(s, db=db, clock=lambda: 1_700_000_600.0)
    client = FakeClient(settings=s, db=db, services=container)
    guild = SimpleNamespace(id=910000000000000077, name="Jobs Filter Guild")
    member = make_member(user_id=870000000000000077, administrator=False)

    await db.execute(
        """
        INSERT INTO job_seen (source, external_id, url, title, company, location, posted_unix, first_seen_unix, text_blob)
        VALUES
            ('greenhouse:stripe', '1', 'https://boards.greenhouse.io/stripe/jobs/1',
             'Python Backend Intern', 'Stripe', 'Toronto, ON', 1700000000, 1700000000,
             'python backend intern stripe toronto'),
            ('greenhouse:shopify', '2', 'https://boards.greenhouse.io/shopify/jobs/2',
             'Software Developer Intern', 'Shopify', 'Seattle, WA', 1700000000, 1700000000,
             'software developer intern shopify seattle')
        """
    )

    try:
        # 1 bad source out of 6 configured boards must NOT flag the entire job board degraded
        container.jobs_bad_sources.add("greenhouse:airbnb")
        inter_q = make_interaction(user=member, guild=guild, command_name="ic jobs", client=client)
        await jobs_cmd.callback(inter_q, preset="tech_swe", query="python")
        emb_q = inter_q.followup.messages[0].embed
        assert emb_q is not None
        assert emb_q.color.value != 0xF39C12
        assert len(emb_q.fields) == 1
        assert "Python Backend Intern" in emb_q.fields[0].name

        # Explicit location filter narrows results instead of being drowned out by env allowlist
        inter_loc = make_interaction(user=member, guild=guild, command_name="ic jobs", client=client)
        await jobs_cmd.callback(inter_loc, preset="tech_swe", location="Seattle")
        emb_loc = inter_loc.followup.messages[0].embed
        assert emb_loc is not None
        assert len(emb_loc.fields) == 1
        assert "Shopify" in emb_loc.fields[0].name

        # Systemic failure (>= 50% of configured sources bad) DOES trigger degraded warning banner
        container.jobs_bad_sources.update({"greenhouse:figma", "greenhouse:cloudflare"})
        inter_sys = make_interaction(user=member, guild=guild, command_name="ic jobs", client=client)
        await jobs_cmd.callback(inter_sys, preset="tech_swe")
        emb_sys = inter_sys.followup.messages[0].embed
        assert emb_sys is not None
        assert emb_sys.color.value == 0xF39C12
        assert "cooling down or unreachable" in (emb_sys.description or "")
    finally:
        await container.close()
        await db.close()
