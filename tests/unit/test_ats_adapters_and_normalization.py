from __future__ import annotations

import aiohttp
import pytest

from icbot.errors import ProviderError
from icbot.services.jobs.adapters import (
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
)
from icbot.services.jobs.classification import (
    canonicalize_company_name,
    canonicalize_title,
    classify_career_level,
    classify_discipline_tags,
    detect_is_remote,
)
from icbot.services.jobs.models import JobPosting, SourceHealth

pytestmark = pytest.mark.unit


def test_job_posting_model_defaults_and_properties():
    job = JobPosting(
        provider="greenhouse",
        employer="Stripe",
        external_id="gh-1234",
        title="Software Engineer Intern",
        career_level="intern",
        discipline_tags=("software", "cs"),
        location="Toronto, ON",
        is_remote=False,
        url="https://boards.greenhouse.io/stripe/jobs/1234",
        posted_unix=1700000000,
        updated_unix=1700001000,
        fetched_unix=1700002000,
        text_blob="Software Engineer Intern Toronto",
    )
    assert job.provider == "greenhouse"
    assert job.employer == "Stripe"
    assert job.company == "Stripe"
    assert job.source == "greenhouse:Stripe"
    assert job.source_key == "greenhouse:Stripe"
    assert job.dedupe_key == ("greenhouse", "Stripe", "gh-1234")
    assert job.career_level == "intern"
    assert job.is_remote is False
    assert job.license == "ATS Public Listing"


def test_career_level_classification():
    assert classify_career_level("Software Engineering Intern - Summer 2027") == "intern"
    assert classify_career_level("Firmware Engineering Co-op") == "intern"
    assert classify_career_level("Student Software Developer") == "intern"
    assert classify_career_level("New Grad Software Engineer (2027)") == "new_grad"
    assert classify_career_level("Recent Graduate Data Scientist") == "new_grad"
    assert classify_career_level("Junior Backend Developer") == "entry"
    assert classify_career_level("Associate Systems Engineer") == "entry"
    assert classify_career_level("Senior Staff Platform Architect") == "experienced"
    assert classify_career_level("Director of Engineering") == "experienced"
    assert classify_career_level("Principal Hardware Engineer") == "experienced"
    assert classify_career_level("Software Engineer") == "unknown"


def test_discipline_tags_classification():
    assert "software" in classify_discipline_tags("Backend Software Engineer (Go/Rust)")
    assert "hardware" in classify_discipline_tags("Hardware PCB Bring-up Engineer")
    assert "cs" in classify_discipline_tags("Machine Learning AI Research Scientist")
    assert "electrical" in classify_discipline_tags("Embedded Firmware FPGA Engineer")
    assert "mechanical" in classify_discipline_tags("Mechatronics Robotics CAD Engineer")
    assert "consulting" in classify_discipline_tags("Solutions Engineer / Consultant")
    assert classify_discipline_tags("Office Coordinator") == ()


def test_remote_detection():
    assert detect_is_remote("Remote - Canada") is True
    assert detect_is_remote("Toronto, ON", text_blob="This is a fully remote role") is True
    assert detect_is_remote("Toronto, ON", text_blob="Work from home 2 days/wk") is True
    assert detect_is_remote("Toronto, ON", text_blob="On-site only") is False


def test_canonicalization():
    assert canonicalize_company_name("stripe") == "Stripe"
    assert canonicalize_company_name("acme-corp") == "Acme Corp"
    assert canonicalize_company_name("  palantir_tech  ") == "Palantir Tech"
    assert canonicalize_title("  Senior   Software   Engineer  \n\t") == "Senior Software Engineer"


def test_source_health_lifecycle():
    health = SourceHealth(source_key="greenhouse:stripe", provider="greenhouse", target="stripe")
    assert health.status == "healthy"
    assert health.is_cooling_down(100.0) is False

    # Mark cooldown
    health.mark_cooldown(30.0, status_code=429, error="Rate limited", now=100.0)
    assert health.status == "cooling_down"
    assert health.consecutive_failures == 1
    assert health.cooldown_until == 130.0
    assert health.is_cooling_down(120.0) is True
    assert health.is_cooling_down(131.0) is False

    # Mark success
    health.mark_success(now=140.0)
    assert health.status == "healthy"
    assert health.consecutive_failures == 0
    assert health.last_success_unix == 140
    assert health.is_cooling_down(140.0) is False

    # Mark blacklisted
    health.mark_blacklisted("HTTP 404 Not Found", status_code=404)
    assert health.status == "blacklisted"
    assert health.consecutive_failures == 1


def test_workday_derive_cxs_info_tenant_shapes():
    adapter = WorkdayAdapter()

    # Shape 1: Standard subdomain
    api, tenant, site = adapter.derive_cxs_info("https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers")
    assert tenant == "acme"
    assert site == "AcmeCareers"
    assert api == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs?locale=en-US"

    # Shape 2: Simple subdomain without wdX
    api, tenant, site = adapter.derive_cxs_info("https://stripe.myworkdayjobs.com/en-US/stripe_jobs")
    assert tenant == "stripe"
    assert site == "stripe_jobs"
    assert api == "https://stripe.myworkdayjobs.com/wday/cxs/stripe/stripe_jobs/jobs?locale=en-US"

    # Shape 3: Path-based tenant
    api, tenant, site = adapter.derive_cxs_info("https://myworkdayjobs.com/rbc/en-US/RBC_Careers")
    assert tenant == "rbc"
    assert site == "RBC_Careers"
    assert api == "https://myworkdayjobs.com/wday/cxs/rbc/RBC_Careers/jobs?locale=en-US"

    # Shape 4: Invalid URL fails with ProviderError
    with pytest.raises(ProviderError, match="Workday heuristic failed"):
        adapter.derive_cxs_info("https://example.com/")


def test_workday_careers_prefix_from_url():
    adapter = WorkdayAdapter()
    assert (
        adapter.careers_prefix_from_url("https://cppib.wd10.myworkdayjobs.com/en-US/cppinvestments")
        == "/en-US/cppinvestments"
    )
    assert (
        adapter.careers_prefix_from_url("https://acme.wd3.myworkdayjobs.com/en-CA/corp_jobs", locale="en-CA")
        == "/en-CA/corp_jobs"
    )


async def test_greenhouse_adapter_success_and_rate_limit(mock_aiohttp):
    adapter = GreenhouseAdapter()
    board = "synthco"
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    feed = {
        "jobs": [
            {
                "id": 901,
                "title": "Software Engineer Intern",
                "location": {"name": "Toronto, ON"},
                "absolute_url": "https://boards.greenhouse.io/synthco/jobs/901",
                "departments": [{"name": "Engineering"}],
                "created_at": "2026-11-01T12:00:00Z",
                "updated_at": "2026-11-02T15:00:00Z",
            }
        ]
    }
    mock_aiohttp.get(url, json_data=feed)

    async with aiohttp.ClientSession() as session:
        jobs = await adapter.fetch_postings(session, board)
        assert len(jobs) == 1
        j = jobs[0]
        assert j.provider == "greenhouse"
        assert j.employer == "synthco"
        assert j.external_id == "901"
        assert j.title == "Software Engineer Intern"
        assert j.career_level == "intern"
        assert "software" in j.discipline_tags
        assert j.location == "Toronto, ON"
        assert j.url == "https://boards.greenhouse.io/synthco/jobs/901"
        assert j.updated_unix is not None

    # Test HTTP 429 Retry-After handling
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(url, status=429, headers={"Retry-After": "45"}, text="Rate limit exceeded")

    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProviderError) as exc_info:
            await adapter.fetch_postings(session, board)
        assert exc_info.value.status == 429
        assert exc_info.value.retry_after == 45.0


async def test_lever_adapter_success_and_eu_instance(mock_aiohttp):
    adapter = LeverAdapter()
    site = "palantir"
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"

    feed = [
        {
            "id": "lev-77",
            "text": "New Grad Systems Engineer",
            "hostedUrl": "https://jobs.lever.co/palantir/lev-77",
            "categories": {
                "location": "Toronto, ON",
                "team": "Infrastructure",
                "department": "Engineering",
                "commitment": "Full Time",
            },
            "createdAt": 1700000000000,
        }
    ]
    mock_aiohttp.get(url, json_data=feed)

    async with aiohttp.ClientSession() as session:
        jobs = await adapter.fetch_postings(session, site)
        assert len(jobs) == 1
        j = jobs[0]
        assert j.provider == "lever"
        assert j.employer == "palantir"
        assert j.external_id == "lev-77"
        assert j.title == "New Grad Systems Engineer"
        assert j.career_level == "new_grad"
        assert "software" in j.discipline_tags
        assert j.location == "Toronto, ON"


async def test_workday_adapter_pagination_and_csrf_extraction(mock_aiohttp):
    adapter = WorkdayAdapter()
    careers_url = "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers"
    cxs_url = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"

    html = """
    <html>
      <head>
        <script>
          window.workdayConfig = {"csrfToken": "synthetic-csrf-token-1234"};
        </script>
      </head>
      <body>Careers</body>
    </html>
    """
    page1 = {
        "total": 3,
        "jobPostings": [
            {
                "jobPostingId": "P1",
                "title": "Software Engineering Co-op",
                "externalPath": "/job/Toronto/P1",
                "locationsText": "Toronto, ON",
            },
            {
                "jobPostingId": "P2",
                "title": "Hardware Verification Intern",
                "externalPath": "/job/Ottawa/P2",
                "locationsText": "Ottawa, ON",
            },
        ],
    }
    mock_aiohttp.get(careers_url, text=html)
    mock_aiohttp.post(cxs_url, prefix=True, json_data=page1)

    async with aiohttp.ClientSession() as session:
        jobs, _api_used, tenant, site = await adapter.fetch_postings(
            session, careers_url, employer_label="Acme", limit=50
        )
        assert len(jobs) == 2
        assert tenant == "acme"
        assert site == "AcmeCareers"
        assert jobs[0].external_id == "P1"
        assert jobs[0].career_level == "intern"
        assert jobs[0].url == "https://acme.wd3.myworkdayjobs.com/en-US/AcmeCareers/job/Toronto/P1"
