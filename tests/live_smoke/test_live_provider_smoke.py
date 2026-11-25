"""Opt-in, bounded live provider smoke diagnostics (excluded from default offline runs).

Selected ONLY when explicitly invoked with ``pytest -o addopts=--strict-markers -m live_smoke``
and guarded by BOTH ``ICBOT_ALLOW_LIVE_SMOKE=1`` and ``ICBOT_LIVE_SMOKE_OPT_IN=true``.

Deliberate scope & safety invariants:
- Never starts ``main.py``, Discord gateway, or background pollers (``DISCORD_TOKEN=""``).
- Never calls paid AI APIs (``GEMINI_API_KEY=""``) or touches production SQLite DBs.
- Excludes undocumented/ToS-fragile scrapers (Yahoo Finance, RateMyProfessors, TMU Grades,
  course notes, ATS boards), which are verified offline via ``provider_contract`` mocks and
  local ``scripts/manual/`` operator diagnostics.
- Bounds HTTP timeouts (<= 10s), payload byte limits, ``Retry-After`` handling, and secret redaction.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

import aiohttp
import pytest

from icbot.academics.url_safety import validate_fetch_url
from icbot.errors import parse_retry_after, redact_secrets
from icbot.services.crypto_fng import FNG_URL
from icbot.services.go_client import _parse_messages
from icbot.services.sports_client import OddsAPIClient, OddsAPIError
from icbot.services.ttc_client import TTCFetchError, fetch_account_status_items, resolve_account_id

pytestmark = pytest.mark.live_smoke

DEFAULT_GO_API_BASE = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 512 * 1024  # 512 KiB strict upper bound for smoke responses


def _live_opt_in_active(provider_name: str) -> bool:
    if os.getenv("ICBOT_ALLOW_LIVE_SMOKE", "") != "1":
        return False
    if os.getenv("ICBOT_LIVE_SMOKE_OPT_IN", "").lower() != "true":
        return False
    selected = (os.getenv("ICBOT_LIVE_SMOKE_PROVIDER", "all") or "all").strip().lower()
    if selected in ("", "all"):
        return True
    allowed = {p.strip() for p in selected.split(",") if p.strip()}
    return provider_name.lower() in allowed


def _require_live_provider(provider_name: str) -> None:
    if not _live_opt_in_active(provider_name):
        pytest.skip(
            f"Live smoke disabled or provider '{provider_name}' not selected "
            "(requires ICBOT_ALLOW_LIVE_SMOKE=1 and ICBOT_LIVE_SMOKE_OPT_IN=true)."
        )


async def bounded_get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[int, Any, float | None, str]:
    """Perform an SSRF-validated, timeout- and byte-bounded JSON GET request with secret redaction."""
    url_check = validate_fetch_url(url)
    if not url_check.ok:
        raise ValueError(f"Unsafe smoke target URL: {redact_secrets(url_check.reason, secrets)}")
    safe_url = url_check.normalized_url

    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(5.0, timeout_seconds))
    req_headers = {"Accept": "application/json", **(headers or {})}

    async with session.get(safe_url, headers=req_headers, timeout=timeout) as resp:
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        declared_len = resp.headers.get("Content-Length")
        if declared_len and declared_len.isdigit() and int(declared_len) > max_bytes:
            raise ValueError(f"Response Content-Length {declared_len} exceeded max_bytes={max_bytes}")

        content_stream = getattr(resp, "content", None)
        if content_stream is not None and hasattr(content_stream, "read"):
            raw = await content_stream.read(max_bytes + 1)
        else:
            raw = await resp.read()
        if len(raw) > max_bytes:
            raise ValueError(f"Response body exceeded max_bytes={max_bytes}")

        text = raw.decode("utf-8", errors="replace")
        if resp.status != 200:
            snippet = redact_secrets(text[:200], secrets)
            return resp.status, None, retry_after, snippet

        payload = json.loads(text)
        return 200, payload, retry_after, ""


async def test_live_smoke_go_transit() -> None:
    """Verify official GO Transit ServiceAlert API connectivity & schema parsing when opted in."""
    _require_live_provider("go_transit")
    api_key = os.getenv("GO_API_KEY", "").strip()
    if not api_key:
        pytest.skip("GO_API_KEY not configured for live_smoke: go_transit")
    api_base = (os.getenv("GO_API_BASE", DEFAULT_GO_API_BASE) or DEFAULT_GO_API_BASE).strip().rstrip("/")
    url = f"{api_base}/ServiceUpdate/ServiceAlert/All?{urlencode({'key': api_key})}"

    async with aiohttp.ClientSession() as session:
        status, payload, retry_after, note = await bounded_get_json(
            session,
            url,
            headers={"x-api-key": api_key, "X-API-Key": api_key},
            secrets=(api_key,),
            max_bytes=DEFAULT_MAX_BYTES,
        )

    if status == 429:
        pytest.skip(f"GO Transit rate-limited (429; retry_after={retry_after}s)")
    if status == 451:
        pytest.skip("GO Transit unavailable due to regional restriction (451)")
    assert status == 200, f"GO Transit HTTP {status}: {note}"
    msgs = _parse_messages(payload)
    assert isinstance(msgs, list)


async def test_live_smoke_ttc_mastodon() -> None:
    """Verify TTC Mastodon account resolution & bounded status fetch (limit=2) when opted in."""
    _require_live_provider("ttc_mastodon")
    api_base = os.getenv("TTC_MASTODON_BASE", "").strip()
    acct = os.getenv("TTC_MASTODON_ACCT", "").strip()
    if not api_base or not acct:
        pytest.skip("TTC_MASTODON_BASE / TTC_MASTODON_ACCT not configured for live_smoke: ttc_mastodon")

    url_check = validate_fetch_url(api_base)
    if not url_check.ok:
        raise AssertionError(f"Unsafe TTC Mastodon base URL: {url_check.reason}")

    async with aiohttp.ClientSession() as session:
        try:
            acct_id = await resolve_account_id(session, api_base, acct, timeout_seconds=10)
            items = await fetch_account_status_items(
                session,
                api_base,
                acct_id,
                limit=2,
                timeout_seconds=10,
            )
        except TTCFetchError as exc:
            if exc.status == 429:
                pytest.skip(f"TTC Mastodon rate-limited (429; retry_after={exc.retry_after}s)")
            raise AssertionError(redact_secrets(str(exc))) from exc
        assert isinstance(items, list)


async def test_live_smoke_sports_odds() -> None:
    """Verify Odds API zero-quota `/v4/sports` endpoint when ODDS_API_KEY and opt-in are set."""
    _require_live_provider("sports_odds")
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        pytest.skip("ODDS_API_KEY not configured for live_smoke: sports_odds")

    client = OddsAPIClient(api_key, timeout_seconds=10)
    try:
        sports = await client.list_sports()
    except OddsAPIError as exc:
        if exc.status == 429:
            pytest.skip(f"Odds API rate-limited (429; retry_after={exc.retry_after}s)")
        raise AssertionError(redact_secrets(str(exc), (api_key,))) from exc
    assert isinstance(sports, list) and len(sports) >= 1


async def test_live_smoke_crypto_fng() -> None:
    """Verify public Fear & Greed JSON endpoint (`limit=1`) with bounded size when opted in."""
    _require_live_provider("crypto_fng")
    async with aiohttp.ClientSession() as session:
        status, payload, retry_after, note = await bounded_get_json(
            session,
            FNG_URL,
            max_bytes=64 * 1024,
        )
    if status == 429:
        pytest.skip(f"Fear & Greed endpoint rate-limited (429; retry_after={retry_after}s)")
    if status == 451:
        pytest.skip("Fear & Greed endpoint unavailable due to regional restriction (451)")
    assert status == 200, f"Fear & Greed HTTP {status}: {note}"
    items = (payload or {}).get("data") or []
    assert isinstance(items, list) and len(items) == 1
    val = int(items[0]["value"])
    assert 0 <= val <= 100
