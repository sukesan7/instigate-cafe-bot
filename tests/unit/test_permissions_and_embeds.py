"""Unit tests for Discord permission checks and embed builders."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from icbot import embeds
from icbot.config import Settings
from icbot.permissions import is_admin_or_allowed

pytestmark = pytest.mark.unit


def test_permissions_requires_discord_member(make_interaction, synthetic_settings: Settings):
    raw_user = SimpleNamespace(
        id=800000000000000001,
        guild_permissions=SimpleNamespace(administrator=True),
        roles=[],
    )
    interaction = make_interaction(user=raw_user, settings=synthetic_settings)
    assert is_admin_or_allowed(interaction, synthetic_settings) is False


def test_permissions_administrator_and_allowed_roles(make_interaction, synthetic_settings: Settings):
    admin_inter = make_interaction(administrator=True, role_ids=[])
    assert is_admin_or_allowed(admin_inter, synthetic_settings) is True

    allowed_role_inter = make_interaction(administrator=False, role_ids=[900000000000000099])
    assert is_admin_or_allowed(allowed_role_inter, synthetic_settings) is True

    unprivileged_inter = make_interaction(administrator=False, role_ids=[123456])
    assert is_admin_or_allowed(unprivileged_inter, synthetic_settings) is False


def test_embed_builders_and_discord_length_limits(synthetic_settings: Settings):
    long_text = "x" * 5000
    sys_no_sub = embeds.system(
        synthetic_settings,
        title="A" * 400,
        summary=long_text,
        fields=[("N" * 400, "V" * 2000, True), ("IgnoreNone", None, False)],  # type: ignore[list-item]
        footer_hint="Unit",
    )
    assert len(sys_no_sub.title) == 256
    assert sys_no_sub.title.endswith("…")
    assert len(sys_no_sub.description) == 4096
    assert sys_no_sub.description.endswith("…")
    assert len(sys_no_sub.fields) == 1
    assert len(sys_no_sub.fields[0].name) == 256
    assert len(sys_no_sub.fields[0].value) == 1024
    assert "System" in sys_no_sub.footer.text

    # Characterization: when subtitle is provided, _base_embed prepends subtitle after truncating summary
    sys_with_sub = embeds.system(
        synthetic_settings,
        title="Subtitled",
        subtitle="Synthetic subtitle",
        summary="Body text",
    )
    assert sys_with_sub.description == "*Synthetic subtitle*\n\nBody text"

    stock_e = embeds.stock(synthetic_settings, title="Market", headline="$IC +5", reason="Earnings beat")
    assert any(f.name == "Reason" and f.value == "Earnings beat" for f in stock_e.fields)

    game_e = embeds.game(synthetic_settings, title="Blackjack", summary="Play", controls="Hit / Stand")
    assert any(f.name == "Controls" for f in game_e.fields)

    job_e = embeds.job_alert(
        synthetic_settings,
        title="Software Engineering Intern",
        source="workday:acme-corp",
        location="Toronto, ON",
        url="https://example.invalid/jobs/1",
        tags=["SWE", "Early Career"],
    )
    assert job_e.url == "https://example.invalid/jobs/1"
    field_map = {f.name: f.value for f in job_e.fields}
    assert field_map["🏢 Company"] == "**Acme Corp**"
    assert "[Apply Here](https://example.invalid/jobs/1)" in field_map["🔗 Apply"]


@pytest.mark.parametrize(
    "builder",
    [
        lambda s: embeds.ttc_alert(s, headline="H" * 500, details="D" * 2000, url="https://example.invalid"),
        lambda s: embeds.go_alert(
            s,
            headline="H" * 500,
            details="D" * 2000,
            meta="Disruption",
            posted_dt="2026-10-21 10:00:00",
            lines=["LW", "LE"],
        ),
        lambda s: embeds.job_alert(
            s,
            title="J" * 500,
            company="acme",
            location="L" * 2000,
            url="https://example.invalid/job",
            tags=[f"tag-{i}" for i in range(15)],
        ),
    ],
)
def test_alert_and_job_embeds_respect_field_and_title_caps(synthetic_settings: Settings, builder):
    emb = builder(synthetic_settings)
    assert len(emb.title or "") <= 256
    assert len(emb.footer.text or "") <= 256
    assert len(emb.author.name or "") <= 256
    for fld in emb.fields:
        assert len(fld.name) <= 256
        assert len(fld.value) <= 1024
    assert len(emb) <= 6000


async def test_interaction_single_response_protocol(make_interaction):
    # Path 1: defer -> followup.send succeeds; double response raises InteractionResponded
    inter1 = make_interaction(command_name="ic profile")
    await inter1.response.defer(thinking=True)
    with pytest.raises(discord.InteractionResponded):
        await inter1.response.send_message("second response")
    with pytest.raises(discord.InteractionResponded):
        await inter1.response.defer()
    await inter1.followup.send("followup ok")
    assert len(inter1.followup.messages) == 1

    # Path 2: followup.send before any initial response is illegal
    inter2 = make_interaction(command_name="ic test")
    with pytest.raises(RuntimeError, match="before response"):
        await inter2.followup.send("premature followup")

    # Path 3: edit_message marks initial response done
    inter3 = make_interaction(command_name="ic leaderboard")
    await inter3.response.edit_message(content="updated")
    with pytest.raises(discord.InteractionResponded):
        await inter3.response.edit_message(content="twice")


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-09: _base_embed prepends subtitle after 4096-char truncation, overflowing Discord 4096-char description cap. Planned fix: Phase 4.",
)
def test_embed_description_with_subtitle_enforces_discord_4096_limit(synthetic_settings: Settings):
    emb = embeds.system(
        synthetic_settings,
        title="Subtitle Overflow Check",
        subtitle="Status notice",
        summary="x" * 5000,
    )
    assert len(emb.description or "") <= 4096


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-09: _add_fields does not cap embeds at Discord's 25-field maximum. Planned fix: Phase 4.",
)
def test_embed_enforces_discord_25_field_limit(synthetic_settings: Settings):
    emb = embeds.system(
        synthetic_settings,
        title="Too Many Fields",
        summary="Check 25-field cap",
        fields=[(f"Field {i}", f"Value {i}", False) for i in range(30)],
    )
    assert len(emb.fields) <= 25


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-09: embed builders do not enforce Discord's 6000 combined character limit. Planned fix: Phase 4.",
)
def test_embed_enforces_discord_6000_total_char_limit(synthetic_settings: Settings):
    emb = embeds.system(
        synthetic_settings,
        title="T" * 250,
        summary="S" * 4000,
        fields=[(f"Name {i} " + "n" * 100, "v" * 900, False) for i in range(6)],
    )
    assert len(emb) <= 6000


