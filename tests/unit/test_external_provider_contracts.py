"""Unit and characterization tests for unified external provider contracts and bounded HTTP transport.

Tests:
- Bounded HTTP transport timeouts (connect, read, total).
- Maximum response payload size enforcement (Content-Length and streaming bytes).
- Structured error typing (TransportError hierarchy, ProviderError backwards compatibility).
- SSRF protection against loopback, metadata, link-local, and unauthorized hosts.
- Insecure TLS detection and security logging (GO_SSL_VERIFY, verify=False).
- Conditional HTTP caching with ETag and Last-Modified (304 Not Modified).
- Idempotent request retry with backoff and jitter respecting Retry-After.
- Provider data provenance, source timestamp extraction, and DST handling across:
  * TTC Mastodon client
  * GO Transit alerts client
  * Yahoo Finance quote client
  * Esplora Bitcoin address transactions
  * Crypto Fear & Greed Index
  * The Odds API sports client
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp
import pytest

from icbot.errors import ProviderError
from icbot.services.btc_esplora import fetch_address_txs_provenance
from icbot.services.crypto_fng import fetch_fear_greed_provenance
from icbot.services.go_client import (
    fetch_service_alerts_provenance,
    parse_toronto_timestamp,
)
from icbot.services.http_transport import (
    BoundedHttpTransport,
    HttpTransportCache,
    PayloadTooLargeError,
    ProviderResult,
    SSRFBlockedError,
    TransportError,
    TransportNetworkError,
    TransportRateLimitError,
    TransportServerError,
    TransportTimeoutError,
    build_ssl_context,
)
from icbot.services.sports_client import (
    ODDS_BASE_URL,
    OddsAPIClient,
)
from icbot.services.ttc_client import fetch_account_status_items_provenance
from icbot.services.yahoo_quotes import fetch_quotes_provenance
from icbot.tasks.go_alerts import _make_go_connector

# ---------------------------------------------------------------------------
# Bounded HTTP Transport Tests
# ---------------------------------------------------------------------------


async def test_bounded_http_transport_timeout_hierarchy(mock_aiohttp):
    """Verify that timeout raises TransportTimeoutError which inherits from TimeoutError and ProviderError."""
    url = "https://example.invalid/slow-api"
    mock_aiohttp.get(url, exc=TimeoutError())

    async with BoundedHttpTransport(
        source_name="test_timeout",
        total_timeout=0.1,
        max_retries=0,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.get(url)

    err = exc_info.value
    assert isinstance(err, TimeoutError)
    assert isinstance(err, TransportError)
    assert isinstance(err, ProviderError)
    assert isinstance(err, RuntimeError)


async def test_bounded_http_transport_network_error(mock_aiohttp):
    """Verify network connection errors map to TransportNetworkError."""
    url = "https://example.invalid/conn-drop"
    mock_aiohttp.get(url, exc=aiohttp.ClientConnectionError("Connection reset by peer"))

    async with BoundedHttpTransport(
        source_name="test_network",
        max_retries=0,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(TransportNetworkError) as exc_info:
            await transport.get(url)

    err = exc_info.value
    assert isinstance(err, TransportError)
    assert "Connection reset by peer" in str(err)


async def test_bounded_http_transport_size_limit_content_length(mock_aiohttp):
    """Verify response with Content-Length exceeding limit is rejected immediately."""
    url = "https://example.invalid/large-download"
    mock_aiohttp.get(
        url,
        status=200,
        headers={"Content-Length": "10000000"},
        body=b"header-only-too-large",
    )

    async with BoundedHttpTransport(
        source_name="test_size",
        max_response_bytes=1_000_000,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(PayloadTooLargeError, match="exceeds limit of 1000000 bytes"):
            await transport.get(url)


async def test_bounded_http_transport_size_limit_stream_body(mock_aiohttp):
    """Verify response payload exceeding max_response_bytes raises PayloadTooLargeError."""
    url = "https://example.invalid/stream-large"
    oversized_data = b"x" * 2000
    mock_aiohttp.get(url, status=200, body=oversized_data)

    async with BoundedHttpTransport(
        source_name="test_stream_size",
        max_response_bytes=1000,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(PayloadTooLargeError, match="exceeds limit of 1000 bytes"):
            await transport.get(url)
    with pytest.raises(PayloadTooLargeError, match="exceeds limit of 1000 bytes"):
        await transport.get(url)


async def test_bounded_http_transport_conditional_etag_caching(mock_aiohttp):
    """Verify conditional ETag request (304 Not Modified) returns cached payload with from_cache=True."""
    url = "https://example.invalid/cacheable-resource"
    cache = HttpTransportCache()
    transport = BoundedHttpTransport(
        source_name="test_cache",
        cache=cache,
        enforce_ssrf_protection=False,
        clock=lambda: 1700000000.0,
    )

    # First request: 200 OK with ETag
    mock_aiohttp.get(
        url,
        status=200,
        headers={"ETag": '"v1.0"'},
        json_data={"version": 1},
    )
    resp1 = await transport.get(url)
    assert resp1.status == 200
    assert resp1.json() == {"version": 1}
    assert resp1.provenance.from_cache is False
    assert resp1.provenance.etag == '"v1.0"'

    # Verify cache entry was populated
    cached_entry = cache.get(url)
    assert cached_entry is not None
    assert cached_entry.etag == '"v1.0"'

    # Second request: server returns 304 Not Modified
    mock_aiohttp._routes.clear()
    mock_aiohttp.get(
        url,
        status=304,
        headers={"ETag": '"v1.0"'},
        body=b"",
    )
    resp2 = await transport.get(url)
    assert resp2.status == 200
    assert resp2.json() == {"version": 1}
    assert resp2.provenance.from_cache is True
    assert resp2.provenance.status_code == 304
    assert resp2.provenance.etag == '"v1.0"'

    # Verify If-None-Match header was sent upstream
    assert len(mock_aiohttp.calls) == 2
    second_call_headers = mock_aiohttp.calls[1].headers
    assert second_call_headers.get("If-None-Match") == '"v1.0"'
    await transport.close()


async def test_bounded_http_transport_idempotent_retry_with_jitter(mock_aiohttp):
    """Verify idempotent GET retries on 503 and succeeds, invoking sleep_func for backoff."""
    url = "https://example.invalid/retry-endpoint"
    mock_aiohttp.get(url, status=503, text="Unavailable", repeat=False)
    mock_aiohttp.get(url, status=200, json_data={"status": "recovered"})

    slept_delays: list[float] = []

    async def _mock_sleep(sec: float) -> None:
        slept_delays.append(sec)

    async with BoundedHttpTransport(
        source_name="test_retry",
        max_retries=2,
        base_retry_delay=0.1,
        retry_jitter=0.05,
        sleep_func=_mock_sleep,
        enforce_ssrf_protection=False,
    ) as transport:
        resp = await transport.get(url)
        assert resp.status == 200
        assert resp.json() == {"status": "recovered"}
        assert len(slept_delays) == 1
        assert 0.1 <= slept_delays[0] <= 0.2


async def test_bounded_http_transport_retry_respects_retry_after(mock_aiohttp):
    """Verify 429 response honors Retry-After header for backoff duration."""
    url = "https://example.invalid/rate-limited"
    mock_aiohttp.get(
        url,
        status=429,
        headers={"Retry-After": "5"},
        text="Quota exceeded",
        repeat=False,
    )
    mock_aiohttp.get(url, status=200, json_data={"ok": True})

    slept_delays: list[float] = []

    async def _mock_sleep(sec: float) -> None:
        slept_delays.append(sec)

    async with BoundedHttpTransport(
        source_name="test_rate_limit",
        max_retries=1,
        sleep_func=_mock_sleep,
        enforce_ssrf_protection=False,
    ) as transport:
        resp = await transport.get(url)
        assert resp.status == 200
        assert len(slept_delays) == 1
        assert slept_delays[0] == 5.0


async def test_bounded_http_transport_non_idempotent_no_retry(mock_aiohttp):
    """Verify POST operations fail immediately on 500 without automatic retry."""
    url = "https://example.invalid/submit-order"
    mock_aiohttp.post(url, status=500, text="Internal Server Error")

    async with BoundedHttpTransport(
        source_name="test_post",
        max_retries=3,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(TransportServerError) as exc_info:
            await transport.post(url, json_data={"amount": 100})

    assert exc_info.value.status_code == 500
    assert len(mock_aiohttp.calls) == 1


async def test_bounded_http_transport_retry_after_exceeds_max_fails_fast(mock_aiohttp):
    """Verify Retry-After exceeding max_retry_after aborts immediately."""
    url = "https://example.invalid/heavy-throttle"
    mock_aiohttp.get(
        url,
        status=429,
        headers={"Retry-After": "120"},
        text="Too many requests",
    )

    async with BoundedHttpTransport(
        source_name="test_max_retry_after",
        max_retries=2,
        max_retry_after=30.0,
        enforce_ssrf_protection=False,
    ) as transport:
        with pytest.raises(TransportRateLimitError) as exc_info:
            await transport.get(url)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 120.0
    assert len(mock_aiohttp.calls) == 1


async def test_bounded_http_transport_ssrf_rejection():
    """Verify SSRF validation rejects loopback, RFC1918 private ranges, AWS/GCP metadata, and internal domains."""
    transport = BoundedHttpTransport(enforce_ssrf_protection=True)

    blocked_targets = [
        "https://127.0.0.1/admin",
        "https://localhost/api",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/metrics",
        "https://192.168.1.1/router",
        "https://internal-server.local/debug",
        "https://metadata.google.internal/computeMetadata/v1/",
    ]

    for target in blocked_targets:
        with pytest.raises(SSRFBlockedError, match="SSRF policy blocked request"):
            await transport.get(target)


async def test_bounded_http_transport_ssrf_host_allowlist():
    """Verify allowed_hosts restricts fetches to authorized domains and subdomains only."""
    transport = BoundedHttpTransport(
        allowed_hosts=["api.openmetrolinx.com", "blockstream.info"],
        enforce_ssrf_protection=True,
    )

    # Unauthorized external host blocked
    with pytest.raises(SSRFBlockedError, match="not in the configured"):
        await transport.get("https://untrusted-api.org/data")


# ---------------------------------------------------------------------------
# TLS & Certificate Verification Tests
# ---------------------------------------------------------------------------


def test_build_ssl_context_insecure_warning(caplog):
    """Verify verify=False logs an explicit security warning and returns False."""
    with caplog.at_level(logging.WARNING):
        ctx = build_ssl_context(verify=False)
    assert ctx is False
    assert any("Insecure TLS verification is enabled" in record.message for record in caplog.records)


def test_build_ssl_context_secure_defaults():
    """Verify verify=True returns a valid SSLContext with standard certificate verification."""
    ctx = build_ssl_context(verify=True)
    assert isinstance(ctx, aiohttp.TCPConnector.__init__.__annotations__.get("ssl", object) or object)
    assert hasattr(ctx, "verify_mode")


async def test_go_connector_security_warning_on_insecure(caplog):
    """Verify _make_go_connector logs warning when go_ssl_verify=False."""

    class DummySettings:
        go_ssl_verify = False
        go_ca_bundle_path = ""

    with caplog.at_level(logging.WARNING):
        connector = _make_go_connector(DummySettings())

    try:
        assert connector._ssl is False
        assert any("Insecure TLS verification is enabled" in record.message for record in caplog.records)
    finally:
        await connector.close()


# ---------------------------------------------------------------------------
# Provider Contract & Provenance Tests
# ---------------------------------------------------------------------------


async def test_ttc_client_provenance_and_html_cleaning(mock_aiohttp):
    """Verify TTC client cleans HTML, parses timestamps, and returns typed ProviderResult with provenance."""
    base = "https://mastodon.example.invalid"
    statuses_url = f"{base}/api/v1/accounts/42001/statuses"

    mock_aiohttp.get(
        statuses_url,
        prefix=True,
        json_data=[
            {
                "id": "9001",
                "created_at": "2026-11-19T14:30:00.000Z",
                "spoiler_text": "<b>Line 1 Alert</b>",
                "content": "<p>Service suspended between Bloor and Eglinton due to signal repairs.</p>",
                "url": "https://mastodon.example.invalid/@ttc/9001",
            },
            {
                "id": "9000",
                "created_at": "2026-11-19T13:00:00.000Z",
                "content": "<p>Normal operations on Line 2.</p>",
                "url": "https://mastodon.example.invalid/@ttc/9000",
            },
        ],
    )

    async with aiohttp.ClientSession() as session:
        result = await fetch_account_status_items_provenance(
            session,
            base,
            "42001",
            clock=lambda: 1700000500.0,
        )

    assert isinstance(result, ProviderResult)
    items = result.data
    assert len(items) == 2

    # Clean HTML verification
    assert items[0].guid == "9001"
    assert items[0].title == "Line 1 Alert"
    assert "Service suspended between Bloor and Eglinton" in items[0].summary
    assert "<p>" not in items[0].summary

    # Provenance verification
    prov = result.provenance
    assert prov.source == "ttc_mastodon"
    assert prov.fetched_at == 1700000500.0
    assert prov.source_timestamp == datetime(2026, 11, 19, 14, 30, tzinfo=UTC).timestamp()
    assert prov.status_code == 200
    assert prov.content_length == 2


def test_go_transit_toronto_dst_timestamp_parsing():
    """Verify Toronto timezone parsing correctly handles winter (EST, UTC-5) and summer (EDT, UTC-4) DST shifts."""
    # Winter date (EST, UTC-5)
    winter_str = "2026-01-15 10:00:00"
    winter_ts = parse_toronto_timestamp(winter_str)
    assert winter_ts is not None
    winter_dt = datetime.fromtimestamp(winter_ts, tz=UTC)
    assert winter_dt.hour == 15  # 10:00 EST + 5 hours = 15:00 UTC

    # Summer date (EDT, UTC-4)
    summer_str = "2026-07-15 10:00:00"
    summer_ts = parse_toronto_timestamp(summer_str)
    assert summer_ts is not None
    summer_dt = datetime.fromtimestamp(summer_ts, tz=UTC)
    assert summer_dt.hour == 14  # 10:00 EDT + 4 hours = 14:00 UTC


async def test_go_transit_provenance_and_skips_malformed(mock_aiohttp):
    """Verify GO Transit client handles missing attributes and returns ProviderResult with provenance."""
    base = "https://api.openmetrolinx.com/OpenDataAPI/api/V1"
    url = f"{base}/ServiceUpdate/ServiceAlert/All?key=secret-key"

    mock_aiohttp.get(
        url,
        json_data={
            "Metadata": {"TimeStamp": "2026-11-19 12:00:00"},
            "Messages": {
                "Message": [
                    {
                        "Code": "AL001",
                        "Status": "Active",
                        "PostedDateTime": "2026-11-19 11:45:00",
                        "SubjectEnglish": "Lakeshore West Train Delay",
                        "BodyEnglish": "15 minute delay due to mechanical inspection.",
                        "Lines": [{"Code": "LW"}],
                        "Stops": [{"Name": "Union Station"}],
                    },
                    # Malformed entry without Code must be skipped
                    {
                        "Code": "",
                        "SubjectEnglish": "Ghost notification",
                    },
                ]
            },
        },
    )

    async with aiohttp.ClientSession() as session:
        result = await fetch_service_alerts_provenance(
            session,
            base,
            "secret-key",
            clock=lambda: 1700000000.0,
        )

    assert isinstance(result, ProviderResult)
    msgs = result.data
    assert len(msgs) == 1
    assert msgs[0].code == "AL001"
    assert msgs[0].lines == ["LW"]
    assert msgs[0].stops == ["Union Station"]

    prov = result.provenance
    assert prov.source == "go_transit"
    assert prov.fetched_at == 1700000000.0
    expected_ts = parse_toronto_timestamp("2026-11-19 11:45:00")
    assert prov.source_timestamp == expected_ts


async def test_yahoo_quotes_provenance_and_market_sessions(mock_aiohttp):
    """Verify Yahoo Finance returns ProviderResult and best_session accurately evaluates pre/reg/post."""
    from icbot.services.yahoo_quotes import YF_CRUMB_URL, YF_FC_URL

    mock_aiohttp.get(YF_FC_URL, text="cookie-set")
    mock_aiohttp.get(YF_CRUMB_URL, text="crumb123")
    mock_aiohttp.get(
        "https://query2.finance.yahoo.com/v7/finance/quote",
        prefix=True,
        json_data={
            "quoteResponse": {
                "result": [
                    {
                        "symbol": "SPY",
                        "marketState": "PRE",
                        "regularMarketPrice": 500.0,
                        "regularMarketChangePercent": 0.5,
                        "preMarketPrice": 505.0,
                        "preMarketChangePercent": 1.0,
                        "preMarketChange": 5.0,
                    },
                    {
                        "symbol": "QQQ",
                        "marketState": "POST",
                        "regularMarketPrice": 400.0,
                        "regularMarketChangePercent": -0.2,
                        "postMarketPrice": 402.0,
                        "postMarketChangePercent": 0.5,
                        "postMarketChange": 2.0,
                    },
                    {
                        "symbol": "AAPL",
                        "marketState": "REGULAR",
                        "regularMarketPrice": 220.0,
                        "regularMarketChangePercent": 1.5,
                        "regularMarketChange": 3.3,
                    },
                ]
            }
        },
    )

    result = await fetch_quotes_provenance(["SPY", "QQQ", "AAPL"], clock=lambda: 1700000000.0)
    assert isinstance(result, ProviderResult)
    quotes = result.data
    assert len(quotes) == 3

    # Session selection logic
    spy_session, spy_price, spy_pct, _ = quotes["SPY"].best_session()
    assert spy_session == "PRE"
    assert spy_price == 505.0
    assert spy_pct == 1.0

    qqq_session, qqq_price, _qqq_pct, _ = quotes["QQQ"].best_session()
    assert qqq_session == "POST"
    assert qqq_price == 402.0

    aapl_session, aapl_price, _aapl_pct, _ = quotes["AAPL"].best_session()
    assert aapl_session == "REG"
    assert aapl_price == 220.0

    # Provenance
    assert result.provenance.source == "yahoo_finance"
    assert result.provenance.fetched_at == 1700000000.0


async def test_btc_esplora_provenance_and_net_sats(mock_aiohttp):
    """Verify Esplora calculates net sats and returns ProviderResult with block_time provenance."""
    address = "bc1qsyntheticaddress123"
    url = f"https://blockstream.info/api/address/{address}/txs"

    mock_aiohttp.get(
        url,
        json_data=[
            {
                "txid": "tx001",
                "status": {"confirmed": True, "block_time": 1700001000},
                "vin": [
                    {
                        "prevout": {
                            "scriptpubkey_address": "other_addr",
                            "value": 50000,
                        }
                    }
                ],
                "vout": [
                    {
                        "scriptpubkey_address": address,
                        "value": 30000,
                    },
                    {
                        "scriptpubkey_address": "change_addr",
                        "value": 20000,
                    },
                ],
            }
        ],
    )

    async with aiohttp.ClientSession() as session:
        result = await fetch_address_txs_provenance(
            address,
            session=session,
            clock=lambda: 1700002000.0,
        )

    assert isinstance(result, ProviderResult)
    txs = result.data
    assert len(txs) == 1
    assert txs[0].txid == "tx001"
    assert txs[0].confirmed is True
    assert txs[0].value_sats == 30000

    prov = result.provenance
    assert prov.source == "esplora_btc"
    assert prov.fetched_at == 1700002000.0
    assert prov.source_timestamp == 1700001000.0


async def test_crypto_fng_provenance_and_classification(mock_aiohttp):
    """Verify Fear & Greed index parser returns ProviderResult with classification and timestamp."""
    from icbot.services.crypto_fng import FNG_URL

    mock_aiohttp.get(
        FNG_URL,
        json_data={
            "data": [
                {
                    "value": "78",
                    "value_classification": "Extreme Greed",
                    "timestamp": "1700003000",
                }
            ]
        },
    )

    async with aiohttp.ClientSession() as session:
        result = await fetch_fear_greed_provenance(
            session=session,
            clock=lambda: 1700003500.0,
        )

    assert isinstance(result, ProviderResult)
    fng = result.data
    assert fng is not None
    assert fng.value == 78
    assert fng.value_classification == "Extreme Greed"
    assert fng.timestamp == 1700003000

    prov = result.provenance
    assert prov.source == "crypto_fng"
    assert prov.fetched_at == 1700003500.0
    assert prov.source_timestamp == 1700003000.0


async def test_sports_client_provenance_and_cache_awareness(mock_aiohttp):
    """Verify OddsAPIClient provenance tracks cache hits and sorts UTC event commence times."""
    key = "test-odds-key"
    client = OddsAPIClient(key, cache_ttl_seconds=300, clock=lambda: 1700000000.0)

    url = f"{ODDS_BASE_URL}/sports/basketball_nba/events"
    mock_aiohttp.get(
        url,
        prefix=True,
        json_data=[
            {
                "id": "game2",
                "sport_key": "basketball_nba",
                "commence_time": "2026-11-20T01:00:00Z",
                "home_team": "Raptors",
                "away_team": "Celtics",
            },
            {
                "id": "game1",
                "sport_key": "basketball_nba",
                "commence_time": "2026-11-19T23:30:00Z",
                "home_team": "Knicks",
                "away_team": "Bulls",
            },
        ],
    )

    # First call: fresh fetch
    res1 = await client.get_events_provenance("basketball_nba")
    assert isinstance(res1, ProviderResult)
    assert len(res1.data) == 2
    # Sorted by commence_time ascending
    assert res1.data[0].id == "game1"
    assert res1.data[1].id == "game2"
    assert res1.provenance.from_cache is False
    assert res1.provenance.source == "the_odds_api"

    # Second call: served from TTL cache without additional network call
    res2 = await client.get_events_provenance("basketball_nba")
    assert res2.provenance.from_cache is True
    assert len(mock_aiohttp.calls) == 1
