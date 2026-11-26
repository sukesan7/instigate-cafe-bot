"""Regression tests for SSRF boundaries, connector DNS validation, redirects, and streaming bounds."""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest

from icbot.academics.models import Campus
from icbot.academics.parsers import MAX_GRADES_HTML_BYTES, MAX_SPA_HTML_BYTES, MAX_SPA_JS_BYTES
from icbot.academics.providers import NotesSpaProvider, fetch_notes_spa_links
from icbot.academics.url_safety import (
    SSRFPolicyError,
    create_ssrf_safe_connector,
    is_public_routable_ip,
    validate_fetch_url,
    validate_resource_url,
)
from icbot.config import Settings
from icbot.errors import ProviderError
from icbot.features.grades import _fetch_grades_html
from icbot.services.http_transport import BoundedHttpTransport, SSRFBlockedError
from icbot.services.jobs.adapters import WorkdayAdapter
from tests.helpers import FakeAiohttpResponse

pytestmark = pytest.mark.unit


class _StreamingFakeContent:
    """Async chunked stream lacking Content-Length trust."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def iter_chunked(self, n: int):
        for chunk in self._chunks:
            yield chunk


class _StreamingFakeResponse(FakeAiohttpResponse):
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        status: int = 200,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(url=url, method=method, status=status, body=b"".join(chunks or []), headers=headers)
        self.content = _StreamingFakeContent(chunks or [])
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeInnerResolver(aiohttp.abc.AbstractResolver):
    """Deterministic fake DNS resolver returning scripted responses per call."""

    def __init__(self, script: list[list[str]]) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
        self.calls.append(host)
        ips = self._script.pop(0) if self._script else []
        out: list[aiohttp.abc.ResolveResult] = []
        for ip in ips:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return out

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("ip_str", "expected_public"),
    [
        ("93.184.216.34", True),
        ("1.1.1.1", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.10", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),  # CGNAT RFC 6598
        ("100.127.255.254", False),
        ("224.0.0.1", False),  # Multicast
        ("240.0.0.1", False),  # Reserved
        ("::1", False),
        ("::", False),
        ("fe80::1%eth0", False),
        ("fd00:ec2::254", False),  # AWS IPv6 metadata
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped IPv6
        ("::ffff:10.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("::ffff:100.64.0.1", False),
        ("2002:7f00:0001::", False),  # 6to4 encoding 127.0.0.1
        ("2130706433", False),  # Decimal 127.0.0.1
        ("0x7f000001", False),  # Hex 127.0.0.1
        ("0177.0.0.1", False),  # Octal 127.0.0.1
        ("127.1", False),  # Shorthand 127.0.0.1
    ],
)
def test_is_public_routable_ip_handles_ipv4_ipv6_mapped_cgnat_and_alt_forms(
    ip_str: str, expected_public: bool
) -> None:
    assert is_public_routable_ip(ip_str) is expected_public


def test_url_validation_ports_alt_ips_and_display_vs_fetch_distinction() -> None:
    # Displayed catalog links allow ordinary http:// URLs, while rejecting malformed ports and alt private IPs
    assert validate_resource_url("http://ocw.example.invalid/lecture1.pdf", require_https=False).ok is True
    assert validate_resource_url("https://2130706433/secret", require_https=False).ok is False
    assert validate_resource_url("https://[::ffff:169.254.169.254]/latest", require_https=False).ok is False
    assert validate_resource_url("https://example.invalid:99999/bad", require_https=False).ok is False

    # Server-side fetches require HTTPS, domain names (no alt IP literals), and safe ports
    assert validate_fetch_url("https://notes.example.invalid/courses/").ok is True
    assert validate_fetch_url("https://notes.example.invalid:443/courses/").ok is True
    assert validate_fetch_url("https://notes.example.invalid:8443/courses/").ok is False
    assert validate_fetch_url("https://notes.example.invalid:22/courses/").ok is False
    assert validate_fetch_url("https://2130706433/courses/").ok is False
    assert validate_fetch_url("https://0x7f000001/courses/").ok is False
    assert validate_fetch_url("https://[::ffff:127.0.0.1]/courses/").ok is False


async def test_connector_validating_dns_resolver_blocks_private_and_rebinding() -> None:
    """Verify aiohttp.TCPConnector uses ValidatingDnsResolver once per connect and blocks DNS rebinding."""
    fake_inner = _FakeInnerResolver(
        [
            ["93.184.216.34"],  # first lookup: public
            ["127.0.0.1"],  # simulated DNS rebinding on second lookup
            ["169.254.169.254"],  # metadata
            ["::ffff:10.0.0.5"],  # IPv4-mapped IPv6
            ["100.64.0.12"],  # CGNAT
        ]
    )
    connector = create_ssrf_safe_connector(inner_resolver=fake_inner)
    try:
        resolved = await connector._resolve_host("notes.example.invalid", 443)
        assert [r["host"] for r in resolved] == ["93.184.216.34"]

        for _ in range(4):
            with pytest.raises(SSRFPolicyError):
                await connector._resolve_host("notes.example.invalid", 443)

        # Connector with use_dns_cache=False resolved each time (no stale bypass)
        assert len(fake_inner.calls) == 5
    finally:
        await connector.close()


async def test_bounded_http_transport_follows_safe_redirects_and_blocks_unsafe_hops(mock_aiohttp) -> None:
    """Verify BoundedHttpTransport disables aiohttp auto-redirects and enforces SSRF policy per hop."""
    mock_aiohttp.get(
        "https://notes.example.invalid/start?secret=abc123",
        status=302,
        headers={"Location": "/v2/data.json"},
    )
    mock_aiohttp.get(
        "https://notes.example.invalid/v2/data.json",
        status=200,
        json_data={"ok": True},
    )

    async with BoundedHttpTransport(
        allowed_hosts=("notes.example.invalid",),
        max_retries=0,
        enforce_ssrf_protection=True,
    ) as transport:
        resp = await transport.get("https://notes.example.invalid/start?secret=abc123")
        assert resp.json() == {"ok": True}
        assert len(mock_aiohttp.calls) == 2

    # Downgrade, metadata redirect, credential redirect, disallowed host, loop
    unsafe_targets = [
        "http://notes.example.invalid/downgrade",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::ffff:127.0.0.1]/admin",
        "https://user:pass@notes.example.invalid/leak",
        "https://evil.invalid/steal",
        "https://notes.example.invalid:8443/alt-port",
    ]
    for bad_location in unsafe_targets:
        mock_aiohttp._routes.clear()
        mock_aiohttp.calls.clear()
        mock_aiohttp.get(
            "https://notes.example.invalid/entry?api_key=TOPSECRET",
            status=302,
            headers={"Location": bad_location},
        )
        async with BoundedHttpTransport(
            allowed_hosts=("notes.example.invalid",),
            max_retries=0,
            enforce_ssrf_protection=True,
        ) as transport:
            with pytest.raises(SSRFBlockedError) as exc_info:
                await transport.get("https://notes.example.invalid/entry?api_key=TOPSECRET")
            assert "TOPSECRET" not in str(exc_info.value)
            assert len(mock_aiohttp.calls) == 1

    # Redirect loop must fail closed cleanly
    mock_aiohttp._routes.clear()
    mock_aiohttp.calls.clear()
    mock_aiohttp.get("https://notes.example.invalid/a", status=302, headers={"Location": "/b"})
    mock_aiohttp.get("https://notes.example.invalid/b", status=302, headers={"Location": "/a"})
    async with BoundedHttpTransport(
        allowed_hosts=("notes.example.invalid",),
        max_retries=0,
        enforce_ssrf_protection=True,
    ) as transport:
        with pytest.raises(SSRFBlockedError, match=r"loop|redirects"):
            await transport.get("https://notes.example.invalid/a")


async def test_notes_spa_streaming_limits_redirects_and_cancellation(mock_aiohttp) -> None:
    """Verify fetch_notes_spa_links enforces streaming bounds (even without Content-Length) & redirect SSRF."""
    # 1. Valid chunked HTML + valid bundle without Content-Length
    html_bytes = (
        b'<html><body><script src="/assets/index-abc123.js"></script>'
        b'<a href="https://example-student.notion.site/CPS109-1234">CPS109</a></body></html>'
    )
    js_bytes = b'const x = "https://example-student.notion.site/ELE888-5678";'
    mock_aiohttp.get(
        "https://notes.example.invalid/",
        handler=lambda call: _StreamingFakeResponse(
            url=call.url, status=200, chunks=[html_bytes[:40], html_bytes[40:]]
        ),
    )
    mock_aiohttp.get(
        "https://notes.example.invalid/assets/index-abc123.js",
        handler=lambda call: _StreamingFakeResponse(
            url=call.url, status=200, chunks=[js_bytes[:20], js_bytes[20:]]
        ),
    )

    async with aiohttp.ClientSession() as session:
        links, err = await fetch_notes_spa_links(
            session, "Notes", "https://notes.example.invalid/", allowed_hosts=("notes.example.invalid",)
        )
    assert err == ""
    assert dict(links)["CPS109"] == "https://example-student.notion.site/CPS109-1234"
    assert dict(links)["ELE888"] == "https://example-student.notion.site/ELE888-5678"

    # 2. Oversized HTML streamed without Content-Length (or lying Content-Length) must abort mid-stream
    oversized_chunk = b"a" * 65536
    chunk_count = (MAX_SPA_HTML_BYTES // len(oversized_chunk)) + 2
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        "https://notes.example.invalid/",
        handler=lambda call: _StreamingFakeResponse(
            url=call.url,
            status=200,
            headers={"Content-Length": "128"},  # lying header
            chunks=[oversized_chunk] * chunk_count,
        ),
    )
    async with aiohttp.ClientSession() as session:
        links2, err2 = await fetch_notes_spa_links(
            session, "Notes", "https://notes.example.invalid/", allowed_hosts=("notes.example.invalid",)
        )
    assert links2 == []
    assert "exceed" in err2.lower()

    # 3. Oversized JS bundle streamed without Content-Length must abort mid-stream
    js_chunk_count = (MAX_SPA_JS_BYTES // len(oversized_chunk)) + 2
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        "https://notes.example.invalid/",
        handler=lambda call: _StreamingFakeResponse(url=call.url, status=200, chunks=[html_bytes]),
    )
    mock_aiohttp.get(
        "https://notes.example.invalid/assets/index-abc123.js",
        handler=lambda call: _StreamingFakeResponse(
            url=call.url, status=200, chunks=[oversized_chunk] * js_chunk_count
        ),
    )
    async with aiohttp.ClientSession() as session:
        links3, err3 = await fetch_notes_spa_links(
            session, "Notes", "https://notes.example.invalid/", allowed_hosts=("notes.example.invalid",)
        )
    assert links3 == []
    assert "exceed" in err3.lower()

    # 4. Redirect from valid notes page OR valid bundle URL to private IP returns unsafe_url on NotesSpaProvider
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        "https://notes.example.invalid/",
        prefix=True,
        status=302,
        headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )
    provider = NotesSpaProvider()
    settings = Settings(
        notes_sites=["Student Notes|https://notes.example.invalid/?secret=hidden"],
        academic_allowed_hosts=["notes.example.invalid"],
    )
    campus = Campus(campus_id="tmu", name="Toronto Metropolitan University")
    async with aiohttp.ClientSession() as session:
        res = await provider.search("CPS109", campus=campus, settings=settings, session=session)
    assert res.status == "unsafe_url"
    assert "hidden" not in (res.error_message or "")

    # 5. Cancellation propagates cleanly
    mock_aiohttp._routes.clear()
    mock_aiohttp.get("https://notes.example.invalid/", exc=asyncio.CancelledError)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(asyncio.CancelledError):
            await fetch_notes_spa_links(session, "Notes", "https://notes.example.invalid/")


async def test_grades_and_workday_reject_unsafe_urls_redirects_and_oversized_bodies(mock_aiohttp) -> None:
    """Verify grades HTML fetch and WorkdayAdapter reject SSRF redirects and oversized streams."""
    # Grades redirect to disallowed host must fail closed
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/cps109",
        status=302,
        headers={"Location": "https://evil.invalid/steal"},
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises((ProviderError, ValueError)):
            await _fetch_grades_html("https://tmu.grades.zone/course/cps109", {}, session)

    # Grades oversized stream without Content-Length aborts cleanly
    mock_aiohttp._routes.clear()
    big_chunk = b"x" * 65536
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/cps109",
        handler=lambda call: _StreamingFakeResponse(
            url=call.url,
            status=200,
            chunks=[big_chunk] * ((MAX_GRADES_HTML_BYTES // len(big_chunk)) + 2),
        ),
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProviderError):
            await _fetch_grades_html("https://tmu.grades.zone/course/cps109", {}, session)

    # Workday rejects unsafe careers_url / api_url_override before issuing HTTP
    adapter = WorkdayAdapter()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProviderError):
            await adapter.fetch_postings(session, "https://127.0.0.1/en-US/Careers")
        with pytest.raises(ProviderError):
            await adapter.fetch_postings(
                session,
                "https://acme.wd3.myworkdayjobs.com/en-US/Careers",
                api_url_override="http://169.254.169.254/latest/meta-data/",
            )
