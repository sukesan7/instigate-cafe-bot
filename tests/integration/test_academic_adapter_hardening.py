"""Integration tests for hardened academic adapters: caching, negative caching, timeouts, drift, and off-loop charts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from icbot.config import Settings
from icbot.container import ServiceContainer, get_container
from icbot.db import Database
from icbot.features.exams import exams_add_cmd, exams_cmd
from icbot.features.grades import grades_command
from icbot.features.notes import notes_cmd
from icbot.features.prof import prof_cmd
from tests.helpers import FakeAiohttpResponse, load_fixture_json, load_fixture_text

pytestmark = [pytest.mark.storage, pytest.mark.discord_mock, pytest.mark.provider_contract]


async def test_grades_configurable_cache_negative_cache_and_misaligned_fixture_end_to_end(
    make_interaction,
    mock_aiohttp,
    frozen_time,
    tmp_path: Path,
):
    """Grades adapter uses injected DB (no hardcoded tmu_grades.db), negative-caches 404, and serves accessible chart."""
    custom_db_path = tmp_path / "custom_install.db"
    db = Database(str(custom_db_path))
    await db.init()

    settings = Settings(
        db_path=str(custom_db_path),
        grades_db_path=str(tmp_path / "unused_separate_grades.db"),
        guild_id=920000000000000001,
        rmp_university="Toronto Metropolitan University",
    )
    container = ServiceContainer.from_settings(settings, db=db, clock=frozen_time)

    mock_aiohttp.get(
        "https://tmu.grades.zone/course/coe817",
        text=load_fixture_text("tmu_grades_misaligned_v1.html"),
    )
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/ghost999",
        status=404,
        text="Not Found",
    )

    try:
        seed = make_interaction(command_name="ic grades")
        client = seed.client
        client.settings = settings
        client.db = db
        client.services = container

        # 1. First fetch for COE817 hits mocked HTTP once, aligns both professors, renders off-loop PNG with alt text
        inter1 = make_interaction(command_name="ic grades", client=client)
        await grades_command.callback(inter1, course="COE817")
        reply1 = inter1.followup.messages[0]
        assert reply1.file is not None
        assert reply1.file.description is not None
        assert "COE817 - Applied Cryptography" in reply1.file.description
        assert reply1.embed is not None
        assert "Last fetched:" in (reply1.embed.description or "")
        field_map = {f.name: f.value for f in reply1.embed.fields}
        assert "Prof. Ada Lovelace" in field_map
        assert "Prof. Alan Turing" in field_map
        assert "Accessible Summary" in field_map
        assert len(mock_aiohttp.calls) == 1

        # 2. Second fetch within TTL hits cache with ZERO new HTTP calls and marks (cached)
        frozen_time.advance(120)
        inter2 = make_interaction(command_name="ic grades", client=client)
        await grades_command.callback(inter2, course="COE817")
        reply2 = inter2.followup.messages[0]
        assert reply2.embed is not None
        assert "(cached)" in (reply2.embed.description or "")
        assert len(mock_aiohttp.calls) == 1

        # 3. 404 for unsupported/nonexistent course triggers negative cache (repeat within 5m makes 0 new calls)
        inter_404_1 = make_interaction(command_name="ic grades", client=client)
        await grades_command.callback(inter_404_1, course="GHOST999")
        assert "Course Not Found" in (inter_404_1.followup.messages[0].embed.title or "")
        assert len(mock_aiohttp.calls) == 2

        frozen_time.advance(60)
        inter_404_2 = make_interaction(command_name="ic grades", client=client)
        await grades_command.callback(inter_404_2, course="GHOST999")
        assert "Course Not Found" in (inter_404_2.followup.messages[0].embed.title or "")
        assert "(cached)" in (inter_404_2.followup.messages[0].embed.description or "")
        assert len(mock_aiohttp.calls) == 2

        # Ensure data persisted in the injected custom installation DB, NOT a hardcoded tmu_grades.db
        cached_row = await db.fetchone(
            "SELECT course_code, title FROM grades_course_cache WHERE course_code = 'coe817'"
        )
        assert cached_row == ("coe817", "COE817 - Applied Cryptography")
    finally:
        await container.close()
        await db.close()


async def test_rmp_conservative_caching_schema_drift_and_core_academic_isolation(
    make_interaction,
    make_member,
    mock_aiohttp,
    frozen_time,
):
    """RMP caches conservatively, handles schema drift without crashing, and never breaks exams/manual notes."""
    school_payload = load_fixture_json("rmp_school_response.json")
    prof_payload = load_fixture_json("rmp_prof_response.json")
    drift_payload = load_fixture_json("rmp_schema_drift_v1.json")

    def _rmp_handler(call):
        body = call.json_body or {}
        query = body.get("query", "")
        text = ((body.get("variables") or {}).get("query") or {}).get("text", "")
        if "SchoolSearchQuery" in query:
            payload = school_payload
        elif "drifted" in text.lower():
            payload = drift_payload
        else:
            payload = prof_payload
        return FakeAiohttpResponse(
            url=call.url,
            method=call.method,
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    mock_aiohttp.post("https://www.ratemyprofessors.com/graphql", handler=_rmp_handler)

    admin = make_member(user_id=850000000000000001, administrator=True)
    inter1 = make_interaction(user=admin, command_name="ic prof")
    client = inter1.client
    container = get_container(client)

    # 1. First RMP lookup fetches school ID + professor (2 POST calls)
    await prof_cmd.callback(inter1, name="Ada Lovelace")
    emb1 = inter1.followup.messages[0].embed
    assert emb1 is not None
    assert "Ada Lovelace" in (emb1.title or "")
    assert "Last fetched:" in (emb1.description or "")
    assert "Unofficial GraphQL" in (emb1.description or "")
    assert len(mock_aiohttp.calls) == 2

    # 2. Second lookup for same professor hits conservative cache (0 new HTTP calls!)
    frozen_time.advance(600)
    inter2 = make_interaction(user=admin, command_name="ic prof", client=client)
    await prof_cmd.callback(inter2, name="ada lovelace")
    emb2 = inter2.followup.messages[0].embed
    assert emb2 is not None
    assert "(cached)" in (emb2.description or "")
    assert len(mock_aiohttp.calls) == 2

    # 3. Drifted schema query reuses cached school ID (only 1 POST call), surfaces clean drift notice
    inter_drift = make_interaction(user=admin, command_name="ic prof", client=client)
    await prof_cmd.callback(inter_drift, name="Drifted Prof")
    assert "unexpected upstream schema" in (inter_drift.followup.messages[0].content or "")
    assert len(mock_aiohttp.calls) == 3

    # 4. Repeat drifted query within 60s negative error TTL makes 0 extra HTTP calls
    frozen_time.advance(20)
    inter_drift2 = make_interaction(user=admin, command_name="ic prof", client=client)
    await prof_cmd.callback(inter_drift2, name="Drifted Prof")
    assert "unexpected upstream schema" in (inter_drift2.followup.messages[0].content or "")
    assert len(mock_aiohttp.calls) == 3

    # 5. Core academic commands (exam planner & manual catalog) remain completely functional offline
    await container.academic_catalog.add_resource(
        guild_id=inter1.guild_id,
        course_code="COE817",
        title="Manual Crypto Notes",
        url="https://docs.example.invalid/crypto",
        source_attribution="Guild Manual Catalog",
    )
    inter_exam = make_interaction(user=admin, command_name="ic exam add", client=client)
    await exams_add_cmd.callback(
        inter_exam,
        course_code="COE817",
        course_name="Applied Cryptography",
        date="2026-12-20",
        start_time="10:00",
    )
    assert "Added exam COE817" in (inter_exam.last_reply.content or "")

    inter_list = make_interaction(user=admin, command_name="ic exam list", client=client)
    await exams_cmd.callback(inter_list)
    assert len(inter_list.followup.messages[0].embed.fields) == 1


async def test_provider_timeout_isolated_and_negative_cached_across_notes_and_grades(
    make_interaction,
    mock_aiohttp,
    frozen_time,
):
    """Timeouts on external Notes SPA and Grades providers degrade gracefully and negative-cache."""

    def _timeout_handler(call):
        raise TimeoutError("simulated upstream timeout")

    mock_aiohttp.get("https://example.invalid/Notes/", handler=_timeout_handler)
    mock_aiohttp.get("https://tmu.grades.zone/course/ele888", handler=_timeout_handler)

    inter = make_interaction(command_name="ic notes")
    client = inter.client
    container = get_container(client)

    # Seed manual catalog fallback so /ic notes still succeeds despite SPA timeout
    await container.academic_catalog.add_resource(
        guild_id=inter.guild_id,
        course_code="ELE888",
        title="Admin Fallback Notes",
        url="https://fallback.example.invalid/ele888",
        source_attribution="Admin Manual Catalog",
    )

    # 1. /ic notes times out on remote SPA, serves manual fallback + freshness + degraded notice
    await notes_cmd.callback(inter, course="ELE888")
    notes_emb = inter.followup.messages[0].embed
    assert notes_emb is not None
    assert any(f.name == "Admin Manual Catalog" for f in notes_emb.fields)
    assert "Last fetched:" in (notes_emb.description or "")
    assert "unavailable" in (notes_emb.description or "")
    assert len(mock_aiohttp.calls) == 1

    # 2. Second /ic notes within negative error TTL does NOT re-hit timed-out SPA
    frozen_time.advance(15)
    inter_notes2 = make_interaction(command_name="ic notes", client=client)
    await notes_cmd.callback(inter_notes2, course="ELE888")
    assert len(mock_aiohttp.calls) == 1

    # 3. /ic grades timeout returns clean Grades Error embed and negative-caches briefly
    inter_g1 = make_interaction(command_name="ic grades", client=client)
    await grades_command.callback(inter_g1, course="ELE888")
    assert "Grades Error" in (inter_g1.followup.messages[0].embed.title or "")
    assert len(mock_aiohttp.calls) == 2

    frozen_time.advance(15)
    inter_g2 = make_interaction(command_name="ic grades", client=client)
    await grades_command.callback(inter_g2, course="ELE888")
    assert "Grades Error" in (inter_g2.followup.messages[0].embed.title or "")
    assert len(mock_aiohttp.calls) == 2
