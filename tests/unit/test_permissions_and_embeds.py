"""Unit tests for Discord permission checks and embed builders."""

from __future__ import annotations

from types import SimpleNamespace

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

