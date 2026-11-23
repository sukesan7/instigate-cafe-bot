from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from icbot.config import Settings
from icbot.container import ServiceContainer
from icbot.features.resume import (
    MAX_FILE_SIZE,
    MAX_PDF_PAGES,
    ResumeConfirmView,
    ResumeReviewer,
    ResumeReviewPaginationView,
    build_resume_system_instruction,
    normalize_discipline,
    validate_resume_bytes,
)
from tests.helpers import (
    FakeClient,
    FakeInteraction,
    RecordedMessage,
    build_fake_member,
)


@dataclass
class _SyntheticAttachment:
    filename: str
    content: bytes
    content_type: str = "application/pdf"
    url: str = "https://cdn.discordapp.com/attachments/123/456/synthetic.pdf"

    @property
    def size(self) -> int:
        return len(self.content)

    async def read(self) -> bytes:
        return self.content


def _build_minimal_pdf(pages: int = 1, active_tag: bytes = b"", scanned: bool = False) -> bytes:
    """Construct an offline, synthetic PDF byte stream with controlled structural elements."""
    body = [b"%PDF-1.4\n"]
    body.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    kids = b" ".join([f"{i + 3} 0 R".encode("ascii") for i in range(pages)])
    body.append(
        f"2 0 obj\n<< /Type /Pages /Kids [{kids.decode('ascii')}] /Count {pages} >>\nendobj\n".encode("ascii")
    )

    for i in range(pages):
        obj_id = i + 3
        if scanned:
            # Scanned page: has image resource but zero font/text definitions
            content = f"{obj_id} 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Im1 << /Type /XObject /Subtype /Image >> >> >> >>\nendobj\n"
        else:
            # Normal page: contains font and text streams
            content = f"{obj_id} 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 100 0 R >> >> >>\nendobj\n"
        body.append(content.encode("ascii"))

    if active_tag:
        body.append(
            f"99 0 obj\n<< /Type /Action {active_tag.decode('ascii', errors='ignore')} >>\nendobj\n".encode(
                "ascii"
            )
        )

    body.append(b"xref\n0 10\ntrailer\n<< /Root 1 0 R >>\nstartxref\n500\n%%EOF\n")
    return b"".join(body)


def _make_bot(settings: Settings, generate_fn: Any) -> FakeClient:
    db = MagicMock()
    container = ServiceContainer.from_settings(settings, db=db, gemini_generate_fn=generate_fn)
    bot = FakeClient(settings=settings, db=db, services=container)
    return bot


# ==============================================================================
# 1. Document Content Signature and Validation Tests
# ==============================================================================


def test_validate_resume_bytes_valid_formats():
    """Authentic PDF, PNG, and JPEG documents pass content signature validation."""
    valid_pdf = _build_minimal_pdf(pages=2)
    res_pdf = validate_resume_bytes(valid_pdf, "resume.pdf", "application/pdf")
    assert res_pdf.valid is True
    assert res_pdf.mime_type == "application/pdf"
    assert res_pdf.page_count == 2
    assert res_pdf.is_scanned is False

    valid_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    res_png = validate_resume_bytes(valid_png, "resume.png", "image/png")
    assert res_png.valid is True
    assert res_png.mime_type == "image/png"

    valid_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    res_jpg = validate_resume_bytes(valid_jpg, "resume.jpg", "image/jpeg")
    assert res_jpg.valid is True
    assert res_jpg.mime_type == "image/jpeg"


def test_validate_resume_bytes_mislabeled_and_spoofed():
    """Reject empty files, oversized files, and spoofed extensions."""
    # 0-byte file
    res_empty = validate_resume_bytes(b"", "empty.pdf")
    assert res_empty.valid is False
    assert "empty" in (res_empty.error or "").lower()

    # Oversized file > 5MB
    oversized = b"%PDF-1.4\n" + b"X" * (MAX_FILE_SIZE + 100)
    res_oversized = validate_resume_bytes(oversized, "big.pdf")
    assert res_oversized.valid is False
    assert "too large" in (res_oversized.error or "").lower()

    # Mislabeled content: text/HTML masquerading as PDF
    html_masquerade = b"<!DOCTYPE html><html><body>Resume</body></html>"
    res_html = validate_resume_bytes(html_masquerade, "fake.pdf")
    assert res_html.valid is False
    assert "invalid content signature" in (res_html.error or "").lower()

    # Extension mismatch: PDF content in .exe or .png
    valid_pdf = _build_minimal_pdf(pages=1)
    res_ext = validate_resume_bytes(valid_pdf, "resume.exe")
    assert res_ext.valid is False
    assert "extension does not match" in (res_ext.error or "").lower()


def test_validate_resume_bytes_malicious_pdf_actions():
    """Reject PDFs containing dangerous script or launch actions without executing them."""
    for tag in (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFiles", b"/SubmitForm"):
        malicious_pdf = _build_minimal_pdf(pages=1, active_tag=tag)
        res = validate_resume_bytes(malicious_pdf, "candidate.pdf")
        assert res.valid is False
        assert "forbidden active content" in (res.error or "")


def test_validate_resume_bytes_pdf_page_ceiling():
    """Enforce the 5-page ceiling on student resumes."""
    # 2 pages: allowed
    pdf_2 = _build_minimal_pdf(pages=2)
    assert validate_resume_bytes(pdf_2, "resume.pdf").valid is True

    # 7 pages: rejected
    pdf_7 = _build_minimal_pdf(pages=7)
    res_7 = validate_resume_bytes(pdf_7, "resume.pdf")
    assert res_7.valid is False
    assert (
        f"exceeds page limit (7 pages detected; student resumes must be {MAX_PDF_PAGES} pages or fewer)"
        in (res_7.error or "")
    )


def test_validate_resume_bytes_scanned_pdf_detection():
    """Detect scanned PDFs lacking selectable text and emit an advisory warning."""
    scanned_pdf = _build_minimal_pdf(pages=1, scanned=True)
    res = validate_resume_bytes(scanned_pdf, "scanned.pdf")
    assert res.valid is True
    assert res.is_scanned is True
    assert "Scanned Image Warning" in (res.warning or "")


# ==============================================================================
# 2. Career Discipline Presets and Rubric Customization Tests
# ==============================================================================


def test_normalize_discipline():
    """Discipline aliases resolve cleanly for engineering and non-engineering faculties."""
    assert normalize_discipline("swe") == ("swe", "Software Engineering & Tech")
    assert normalize_discipline("CS") == ("swe", "Software Engineering & Tech")
    assert normalize_discipline("hw") == ("hw", "Hardware & Embedded Systems")
    assert normalize_discipline("ele") == ("ele", "Electrical Systems & Power")
    assert normalize_discipline("business") == ("business", "Business, Consulting & Finance")
    assert normalize_discipline("accounting") == ("accounting", "Accounting & Auditing")
    assert normalize_discipline("audit") == ("accounting", "Accounting & Auditing")
    assert normalize_discipline("law") == ("law", "Legal, Compliance & Policy")
    assert normalize_discipline("marketing") == ("marketing", "Marketing, Communications & Design")
    assert normalize_discipline("science") == ("science", "Science, Healthcare & Research")
    assert normalize_discipline("general") == ("general", "General Campus & New-Graduate")
    assert normalize_discipline("Product Manager") == ("custom", "Product Manager")


def test_build_resume_system_instruction_dimensions():
    """Verify the 7 evaluation dimensions, opinion labeling, and target role customization."""
    settings = Settings(token="", gemini_api_key="test-key")
    prompt = build_resume_system_instruction(
        discipline_key="accounting",
        discipline_title="Accounting & Auditing",
        target_role="Senior Forensic Auditor",
        settings=settings,
        is_scanned=True,
    )

    # Contains target role and discipline
    assert "Senior Forensic Auditor" in prompt
    assert "Accounting & Auditing" in prompt
    assert "GAAP" in prompt or "IFRS" in prompt

    # Contains 7 core dimensions
    assert "Formatting & Visual Readability" in prompt
    assert "Measurable Accomplishments" in prompt
    assert "Google XYZ formula" in prompt
    assert "Relevant Skills" in prompt
    assert "Relevance to Target Role" in prompt
    assert "Clarity, Precision & Tone" in prompt
    assert "ATS-Friendly Organization" in prompt
    assert "Actionable Edits & Concrete Suggestions" in prompt

    # Mandates advisory recommendation labeling
    assert "evaluative recommendations and best practices" in prompt
    assert "NEVER as definitive ATS pass/fail verdicts" in prompt

    # Includes scanned notice
    assert "CRITICAL NOTE ON SCANNED PDF" in prompt


# ==============================================================================
# 3. End-to-End Ephemeral Lifecycle & Provider Interaction Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_resume_review_lifecycle_end_to_end_ephemeral():
    """Verify full review lifecycle: validation -> ATS check ('PASS') -> senior review -> ephemeral delivery."""
    calls: list[dict[str, Any]] = []

    async def _mock_gemini(**kwargs: Any) -> str:
        calls.append(kwargs)
        if "PASS" in str(kwargs.get("prompt", "")):
            return "PASS"
        return (
            "## Executive Summary & Overall Score\n"
            "Score: 8/10. Strong technical depth and clear project outcomes.\n\n"
            "## Dimension Ratings & Analysis\n"
            "Formatting is clean single-column. Metrics are well-quantified.\n\n"
            "## Key Strengths\n"
            "- Excellent XYZ accomplishment statements.\n\n"
            "## Priority Areas for Improvement\n"
            "- Add more cloud deployment metrics.\n\n"
            "## Actionable Before & After Rewrites\n"
            "Before: Built backend API.\n"
            "After: Architected async FastAPI backend serving 10k req/sec with <50ms latency."
        )

    settings = Settings(
        token="", llm_enabled=True, gemini_api_key="AIzaSyValidTestKey", bot_display_name="InstigateBot"
    )
    bot = _make_bot(settings, _mock_gemini)
    member = build_fake_member(user_id=123456789, display_name="StudentApplicant")
    inter = FakeInteraction(user=member, client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="student_swe.pdf", content=pdf_bytes)

    await ResumeReviewer.review(
        inter,
        attachment,  # type: ignore[arg-type]
        discipline="swe",
        role="Junior Backend Engineer",
        skip_confirm=True,
    )

    # 2 calls: ATS scan + Senior review
    assert len(calls) == 2
    assert "PASS" in calls[0]["prompt"]
    assert calls[0]["task"] == "resume"
    assert "Junior Backend Engineer" in calls[1]["system_instruction"]

    # Ephemeral delivery
    assert len(inter.followup.messages) == 1
    reply: RecordedMessage = inter.followup.messages[0]
    assert reply.ephemeral is True
    assert reply.embed is not None
    assert "Score: 8/10" in (reply.embed.description or "")
    assert "Powered by InstigateBot" in (reply.embed.footer.text or "")


@pytest.mark.asyncio
async def test_resume_review_ats_rejection_reason():
    """When the initial ATS scan rejects an invalid document, return a clear ephemeral explanation."""
    calls: list[dict[str, Any]] = []

    async def _mock_ats_fail(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "FAIL Document appears to be a class syllabus, not a candidate resume."

    settings = Settings(token="", llm_enabled=True, gemini_api_key="AIzaSyValidTestKey")
    bot = _make_bot(settings, _mock_ats_fail)
    inter = FakeInteraction(user=build_fake_member(), client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="syllabus.pdf", content=pdf_bytes)

    await ResumeReviewer.review(inter, attachment, discipline="business", skip_confirm=True)  # type: ignore[arg-type]

    assert len(calls) == 1
    assert len(inter.followup.messages) == 1
    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert "ATS Scan Failed" in (msg.content or "")
    assert "class syllabus" in (msg.content or "")


@pytest.mark.asyncio
async def test_resume_review_oversized_output_pagination_and_file():
    """Provider output exceeding 3900 characters produces a paginator view and attached .md file."""

    async def _mock_long_review(**kwargs: Any) -> str:
        if "PASS" in str(kwargs.get("prompt", "")):
            return "PASS"
        # Generate ~5,000 chars of detailed review content
        section = (
            "## Dimension Analysis\n"
            "This is a detailed analysis of the candidate's achievements and metrics across multiple faculties.\n\n"
        )
        return section * 45

    settings = Settings(token="", llm_enabled=True, gemini_api_key="AIzaSyValidTestKey")
    bot = _make_bot(settings, _mock_long_review)
    inter = FakeInteraction(user=build_fake_member(user_id=987654321), client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=2)
    attachment = _SyntheticAttachment(filename="detailed_law.pdf", content=pdf_bytes)

    await ResumeReviewer.review(
        inter, attachment, discipline="law", role="Compliance Specialist", skip_confirm=True
    )  # type: ignore[arg-type]

    assert len(inter.followup.messages) == 1
    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert msg.view is not None
    assert isinstance(msg.view, ResumeReviewPaginationView)
    assert len(msg.view.pages) >= 2

    # Attached file verification
    assert msg.file is not None
    assert msg.file.filename == "law_resume_review.md"

    # Test pagination interaction
    paginator: ResumeReviewPaginationView = msg.view
    assert paginator.current_page == 0

    # Unauthorized user clicking page button
    unauth_inter = FakeInteraction(user=build_fake_member(user_id=111111111), client=bot)
    await paginator.next_button.callback(unauth_inter)  # type: ignore[arg-type]
    assert "belongs to another user" in (unauth_inter.last_reply.content or "")

    # Authorized user clicking Next
    auth_inter = FakeInteraction(user=build_fake_member(user_id=987654321), client=bot)
    await paginator.next_button.callback(auth_inter)  # type: ignore[arg-type]
    assert paginator.current_page == 1

    # Authorized user clicking Previous
    auth_inter_prev = FakeInteraction(user=build_fake_member(user_id=987654321), client=bot)
    await paginator.prev_button.callback(auth_inter_prev)  # type: ignore[arg-type]
    assert paginator.current_page == 0


@pytest.mark.asyncio
async def test_resume_review_provider_timeout_and_error_handling():
    """Provider timeout or GeminiError returns safe ephemeral feedback with secret redaction."""

    async def _mock_timeout(**kwargs: Any) -> str:
        raise TimeoutError("Deadline exceeded connecting to Gemini endpoint")

    gem_secret = "AIzaSySecretApiKey999"
    settings = Settings(token="", llm_enabled=True, gemini_api_key=gem_secret)
    bot = _make_bot(settings, _mock_timeout)
    inter = FakeInteraction(user=build_fake_member(), client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="resume.pdf", content=pdf_bytes)

    await ResumeReviewer.review(inter, attachment, discipline="swe", skip_confirm=True)  # type: ignore[arg-type]

    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert "Failed to reach Gemini for ATS scan (TimeoutError)" in (msg.content or "")
    assert gem_secret not in (msg.content or "")


# ==============================================================================
# 4. Privacy Disclosure & No-AI Fallback View Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_resume_confirm_view_buttons():
    """Test interactive disclosure view: confirm review, view offline checklist, and cancel."""
    settings = Settings(token="", gemini_api_key="AIzaSyValidTestKey", bot_display_name="Instigate")
    bot = _make_bot(settings, MagicMock())
    owner = build_fake_member(user_id=444555666)
    inter = FakeInteraction(user=owner, client=bot)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="my_resume.pdf", content=pdf_bytes)

    view = ResumeConfirmView(
        interaction=inter,  # type: ignore[arg-type]
        attachment=attachment,  # type: ignore[arg-type]
        discipline_key="accounting",
        discipline_title="Accounting & Auditing",
        target_role="Staff Accountant",
        settings=settings,
    )

    disclosure_embed = view.build_disclosure_embed()
    assert "Third-Party AI Privacy Disclosure" in (disclosure_embed.description or "")
    assert "Google Gemini" in (disclosure_embed.description or "")
    assert "Zero Local Retention" in (disclosure_embed.description or "")
    assert "Staff Accountant" in (disclosure_embed.description or "")

    # Non-owner clicking button
    stranger = build_fake_member(user_id=999999999)
    stranger_inter = FakeInteraction(user=stranger, client=bot)
    await view.checklist_button.callback(stranger_inter)  # type: ignore[arg-type]
    assert "belongs to another user" in (stranger_inter.last_reply.content or "")

    # Owner clicking checklist button -> renders offline checklist
    checklist_inter = FakeInteraction(user=owner, client=bot)
    await view.checklist_button.callback(checklist_inter)  # type: ignore[arg-type]
    last_reply = checklist_inter.last_reply
    assert last_reply.embed is not None
    assert "Self-Review Resume Checklist" in (last_reply.embed.description or "")

    # Owner clicking cancel button -> aborts cleanly
    cancel_inter = FakeInteraction(user=owner, client=bot)
    await view.cancel_button.callback(cancel_inter)  # type: ignore[arg-type]
    assert "Resume review cancelled" in (cancel_inter.last_reply.content or "")


@pytest.mark.asyncio
async def test_unconfigured_gemini_falls_back_to_checklist():
    """If Gemini is not configured, automatically provide the offline checklist."""
    settings = Settings(token="", llm_enabled=True, gemini_api_key="")  # Unconfigured API key
    bot = _make_bot(settings, MagicMock())
    inter = FakeInteraction(user=build_fake_member(), client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="resume.pdf", content=pdf_bytes)

    await ResumeReviewer.review(
        inter, attachment, discipline="marketing", role="Brand Strategist", skip_confirm=True
    )  # type: ignore[arg-type]

    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert "Gemini API is not configured" in (msg.content or "")
    assert msg.embed is not None
    assert "Offline Resume Checklist" in (msg.embed.title or "")
    assert "Brand Strategist" in (msg.embed.description or "")


@pytest.mark.asyncio
async def test_admin_killswitch_blocks_resume_review():
    """If LLM features are administratively paused, block execution immediately."""
    settings = Settings(token="", llm_enabled=True, gemini_api_key="AIzaSyValidKey")
    bot = _make_bot(settings, MagicMock())
    bot.services.llm_killed = True  # Administrative killswitch activated

    inter = FakeInteraction(user=build_fake_member(), client=bot)
    await inter.response.defer(thinking=True, ephemeral=True)

    pdf_bytes = _build_minimal_pdf(pages=1)
    attachment = _SyntheticAttachment(filename="resume.pdf", content=pdf_bytes)

    await ResumeReviewer.review(inter, attachment, discipline="swe", skip_confirm=True)  # type: ignore[arg-type]

    msg = inter.followup.messages[0]
    assert msg.ephemeral is True
    assert "paused by an administrator" in (msg.content or "")
