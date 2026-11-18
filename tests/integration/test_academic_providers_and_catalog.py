"""Integration & contract tests for multi-campus academic providers, manual catalog, and isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from icbot.academics import (
    CatalogValidationError,
    Course,
    ManualResourceCatalogStore,
    campus_from_settings,
)
from icbot.config import Settings
from icbot.container import ServiceContainer, get_container
from icbot.db import Database
from icbot.features.exams import ExamPaginationView, exams_add_cmd, exams_cmd
from icbot.features.grades import grades_command
from icbot.features.notes import NotesPaginationView, notes_cmd
from icbot.features.prof import prof_cmd
from tests.helpers import FakeAiohttpResponse, load_fixture_json, load_fixture_text

pytestmark = [pytest.mark.storage, pytest.mark.discord_mock, pytest.mark.provider_contract]


async def test_manual_catalog_offline_crud_guild_isolation_and_url_safety(tmp_path: Path):
    """Per-guild manual catalog works 100% offline, isolates guilds, and enforces URL safety."""
    db = Database(str(tmp_path / "catalog.db"))
    await db.init()
    store = ManualResourceCatalogStore(db, clock=lambda: 1760000000.0)

    try:
        # 1. Store course with aliases in Guild A (unrelated campus: Humanities)
        await store.upsert_course(
            1001,
            Course.from_code(
                "HIST-2010",
                title="Modern Canadian History",
                campus_id="maple_leaf_u",
                department_code="HIST",
                aliases=("HST 2010", "HIST2010A"),
                term="Fall 2026",
            ),
        )
        added = await store.add_resource(
            guild_id=1001,
            course_code="HIST-2010",
            title="Open Reading List & Primary Sources",
            url="https://ocw.mapleleaf.edu.invalid/hist-2010/syllabus",
            resource_type="syllabus",
            source_attribution="History Student Society",
            added_by=555,
        )
        assert added.course_key == "HIST2010"
        assert added.resource_id.startswith("manual:")

        # 2. Alias lookup resolves cleanly in Guild A
        found = await store.search_resources(1001, "hst 2010")
        assert len(found) == 1
        assert found[0].title == "Open Reading List & Primary Sources"
        assert "rights remain" in found[0].license_note.lower()

        # 3. Guild B is strictly isolated from Guild A
        assert await store.search_resources(2002, "HIST-2010") == []

        # 4. Unsafe URLs are rejected before persistence
        with pytest.raises(CatalogValidationError, match="Unsafe or invalid"):
            await store.add_resource(
                guild_id=1001,
                course_code="HIST-2010",
                title="Bad SSRF Link",
                url="https://169.254.169.254/latest/meta-data/",
            )
        with pytest.raises(CatalogValidationError, match="Unsafe or invalid"):
            await store.add_resource(
                guild_id=1001,
                course_code="HIST-2010",
                title="XSS Link",
                url="javascript:alert(1)",
            )

        # 5. Remove resource by numeric ID
        numeric_id = int(added.resource_id.split(":")[1])
        assert await store.remove_resource(2002, numeric_id) is False
        assert await store.remove_resource(1001, numeric_id) is True
        assert await store.search_resources(1001, "HIST-2010") == []

        await db.check_integrity()
    finally:
        await db.close()


async def test_tmu_engineering_course_end_to_end_across_all_providers(
    make_interaction,
    mock_aiohttp,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Contract test for TMU engineering course (ELE888) across catalog, Notes SPA, RMP, and TMU Grades."""
    from icbot.features import grades as grades_mod

    monkeypatch.setattr(grades_mod, "DB_PATH", str(tmp_path / "tmu_grades_e2e.db"))

    # Mock Notes SPA, RMP GraphQL, and TMU Grades Zone
    mock_aiohttp.get("https://example.invalid/Notes/", text=load_fixture_text("notes_spa_index.html"))
    mock_aiohttp.get(
        "https://example.invalid/Notes/assets/index-deadbeef.js",
        text=load_fixture_text("notes_spa_bundle.js"),
    )
    school_payload = load_fixture_json("rmp_school_response.json")
    prof_payload = load_fixture_json("rmp_prof_response.json")

    def _rmp_handler(call):
        query_text = (call.json_body or {}).get("query", "")
        payload = school_payload if "SchoolSearchQuery" in query_text else prof_payload
        return FakeAiohttpResponse(
            url=call.url,
            method=call.method,
            status=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    mock_aiohttp.post("https://www.ratemyprofessors.com/graphql", handler=_rmp_handler)
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/ele888",
        text=load_fixture_text("tmu_grades_course.html"),
    )

    inter_notes = make_interaction(command_name="ic notes")
    client = inter_notes.client
    container = get_container(client)

    # Add a manual catalog link for ELE888 in the same guild alongside Notes SPA
    await container.academic_catalog.add_resource(
        guild_id=inter_notes.guild_id,
        course_code="ELE888",
        title="ELE888 Lab Prep Guide",
        url="https://docs.example.invalid/ele888-guide",
        resource_type="guide",
        source_attribution="ECE Study Group",
    )

    # 1. /ic notes aggregates both manual catalog + Notes SPA
    await notes_cmd.callback(inter_notes, course="ele 888")
    notes_embed = inter_notes.followup.messages[0].embed
    assert notes_embed is not None
    field_names = [f.name for f in notes_embed.fields]
    assert "ECE Study Group" in field_names
    assert "Synthetic Notes" in field_names
    assert "External links only" in (notes_embed.description or "")

    # 2. /ic prof uses RmpProfessorProvider via registry
    inter_prof = make_interaction(command_name="ic prof", client=client)
    await prof_cmd.callback(inter_prof, name="Ada Lovelace")
    prof_embed = inter_prof.followup.messages[0].embed
    assert prof_embed is not None
    assert "Ada Lovelace" in (prof_embed.title or "")
    assert "RateMyProfessors" in (prof_embed.footer.text or "")

    # 3. /ic grades uses TmuGradesZoneProvider via registry for TMU campus
    inter_grades = make_interaction(command_name="ic grades", client=client)
    await grades_command.callback(inter_grades, course="ELE888")
    grades_reply = inter_grades.followup.messages[0]
    assert grades_reply.file is not None
    assert grades_reply.embed is not None
    assert "ELE888 - Intelligent Systems" in (grades_reply.embed.title or "")
    assert "TMU Grades Zone" in (grades_reply.embed.footer.text or "")


async def test_unrelated_campus_and_discipline_offline_baseline_and_provider_isolation(
    make_interaction,
    make_member,
    mock_aiohttp,
    tmp_path: Path,
):
    """Unrelated campus (Maple Leaf University) works offline without TMU scrapers; remote failures isolated."""
    db = Database(str(tmp_path / "mlu.db"))
    await db.init()
    mlu_settings = Settings(
        db_path=str(tmp_path / "mlu.db"),
        guild_id=900000000000000055,
        rmp_university="Maple Leaf University",
        campus_id="maple_leaf_u",
        campus_url="https://www.mapleleaf.edu.invalid",
        exams_tz="America/Vancouver",
        # Configure a failing / SSRF-unsafe remote source to verify strict failure isolation
        notes_sites=[
            "Unsafe Internal|https://169.254.169.254/latest/meta-data/",
            "Broken Remote|https://broken.example.invalid/notes/",
        ],
        academic_course_aliases=["CSCI*->CS*"],
    )
    mock_aiohttp.get("https://broken.example.invalid/notes/", status=500, text="Server Error")

    container = ServiceContainer.from_settings(mlu_settings, db=db)
    admin_user = make_member(user_id=800000000000000099, administrator=True)

    try:
        # Build a fake client bound to Maple Leaf University settings
        bootstrap_inter = make_interaction(user=admin_user, command_name="ic exam add")
        client = bootstrap_inter.client
        client.settings = mlu_settings
        client.db = db
        client.services = container

        inter_seed = make_interaction(user=admin_user, command_name="ic exam add", client=client)

        # Seed manual catalog with CS 135 (Computer Science) and HIST-2010 (History)
        await container.academic_catalog.add_resource(
            guild_id=inter_seed.guild_id,
            course_code="CS 135",
            title="Designing Functional Programs Notes",
            url="https://cs.mapleleaf.edu.invalid/cs135",
            resource_type="notes",
            source_attribution="CS Club Catalog",
        )

        # 1. /ic grades for non-TMU campus returns unsupported_campus with ZERO HTTP calls to tmu.grades.zone
        inter_grades = make_interaction(user=admin_user, command_name="ic grades", client=client)
        await grades_command.callback(inter_grades, course="CS 135")
        grades_emb = inter_grades.followup.messages[0].embed
        assert grades_emb is not None
        assert "Grade Provider Not Available" in (grades_emb.title or "")
        assert "Maple Leaf University" in (grades_emb.description or "")
        assert not any("tmu.grades.zone" in c.url for c in mock_aiohttp.calls)

        # 2. /ic notes still serves manual catalog via alias (CSCI 135 -> CS135) despite unsafe/failing remotes
        inter_notes = make_interaction(user=admin_user, command_name="ic notes", client=client)
        await notes_cmd.callback(inter_notes, course="CSCI 135")
        notes_emb = inter_notes.followup.messages[0].embed
        assert notes_emb is not None
        assert any(f.name == "CS Club Catalog" for f in notes_emb.fields)
        assert any("cs.mapleleaf.edu.invalid/cs135" in f.value for f in notes_emb.fields)
        # SSRF target must NEVER have been contacted
        assert not any("169.254.169.254" in c.url for c in mock_aiohttp.calls)

        # 3. Core exam planner works completely offline with organizer/visibility and campus timezone
        await exams_add_cmd.callback(
            inter_seed,
            course_code="HIST-2010",
            course_name="Modern Canadian History",
            date="2026-12-14",
            start_time="14:00",
            end_time="16:00",
            location="Arts Hall 204",
            notes="Open book essays",
            organizer="History Dept",
            visibility="guild",
        )
        assert "Added exam HIST-2010" in (inter_seed.last_reply.content or "")

        inter_list = make_interaction(user=admin_user, command_name="ic exam list", client=client)
        await exams_cmd.callback(inter_list)
        list_emb = inter_list.followup.messages[0].embed
        assert list_emb is not None
        assert "America/Vancouver" in (list_emb.description or "")
        assert "HIST-2010 — Modern Canadian History" == list_emb.fields[0].name
        assert "History Dept" in list_emb.fields[0].value

        # Verify provenance metadata directly from registry
        campus = campus_from_settings(mlu_settings)
        grade_res = await container.academic_registry.get_grade_distribution(
            "HIST-2010", campus=campus, settings=mlu_settings
        )
        assert grade_res.status == "unsupported_campus"
        assert grade_res.provenance.fetched_at > 0
    finally:
        await container.close()
        await db.close()


async def test_exam_and_notes_pagination_respect_discord_embed_limits(
    make_interaction,
    make_member,
):
    """Verify pagination views activate cleanly and respect Discord field/length limits."""
    admin_user = make_member(user_id=800000000000000091, administrator=True)
    seed_inter = make_interaction(user=admin_user, command_name="ic exam add")
    client = seed_inter.client
    container = get_container(client)

    # 1. Insert 15 exams to trigger ExamPaginationView (10 per page)
    for i in range(1, 16):
        inter_add = make_interaction(user=admin_user, command_name="ic exam add", client=client)
        await exams_add_cmd.callback(
            inter_add,
            course_code=f"CRS{100 + i}",
            course_name=f"Course Subject {i}",
            date="2026-12-15",
            start_time=f"{8 + (i % 10):02d}:00",
            location=f"Hall {i}",
        )

    inter_list = make_interaction(user=admin_user, command_name="ic exam list", client=client)
    await exams_cmd.callback(inter_list)
    reply_exam = inter_list.followup.messages[0]
    assert isinstance(reply_exam.view, ExamPaginationView)
    assert len(reply_exam.embed.fields) == 10
    assert len(reply_exam.embed) <= 6000
    assert "Page 1/2" in (reply_exam.embed.footer.text or "")

    # Navigate to page 2
    next_exam_inter = make_interaction(user=admin_user, client=client)
    await reply_exam.view.next_button.callback(next_exam_inter)
    assert reply_exam.view.current_page == 1
    page2_embed = reply_exam.view.current_embed()
    assert len(page2_embed.fields) == 5
    assert "Page 2/2" in (page2_embed.footer.text or "")

    # 2. Insert 10 distinct manual resource groups for the same course to trigger NotesPaginationView (8 per page)
    for idx in range(1, 11):
        await container.academic_catalog.add_resource(
            guild_id=seed_inter.guild_id,
            course_code="MATH-200",
            title=f"Lecture Pack {idx}",
            url=f"https://ocw.example.invalid/math200/pack-{idx}",
            source_attribution=f"Contributor Group {idx:02d}",
        )

    # Clear notes_sites so only manual catalog runs (offline)
    client.settings = client.settings.replace(notes_sites=[])
    inter_notes = make_interaction(user=admin_user, command_name="ic notes", client=client)
    await notes_cmd.callback(inter_notes, course="MATH-200")
    reply_notes = inter_notes.followup.messages[0]
    assert isinstance(reply_notes.view, NotesPaginationView)
    assert len(reply_notes.embed.fields) == 8
    assert len(reply_notes.embed) <= 6000
    assert "Page 1/2" in (reply_notes.embed.footer.text or "")

    next_notes_inter = make_interaction(user=admin_user, client=client)
    await reply_notes.view.next_button.callback(next_notes_inter)
    assert reply_notes.view.current_page == 1
    notes_p2 = reply_notes.view.current_embed()
    assert len(notes_p2.fields) == 2
    assert "Page 2/2" in (notes_p2.footer.text or "")
