"""Bounded HTTP transport abstraction, error typing, and data provenance for external providers.

Provides:
- Explicit socket connect, socket read, and total timeouts.
- Response payload size limits with streaming enforcement (PayloadTooLargeError).
- Structured error typing (TransportError hierarchy) derived from ProviderError.
- TLS verification enabled by default with custom CA support and insecure-TLS warnings.
- SSRF protection rejecting loopback, link-local, private, and metadata IP ranges.
- Bounded retries with exponential backoff and jitter for idempotent requests (respecting Retry-After).
- In-memory conditional ETag (If-None-Match) and Last-Modified (If-Modified-Since) caching (304 Not Modified).
- Data provenance and freshness tracking (ProviderProvenance, ProviderResult[T]).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import aiohttp

from icbot.academics.url_safety import validate_fetch_url
from icbot.errors import ProviderError, parse_retry_after, redact_secrets

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_USER_AGENT = "InstigateCafeBot/0.2.3 (+https://github.com/)"


# ---------------------------------------------------------------------------
# Structured Transport Error Hierarchy
# ---------------------------------------------------------------------------


class TransportError(ProviderError, RuntimeError):
    """Base exception for all HTTP transport and external provider failures.

    Inherits from ProviderError and RuntimeError for backwards compatibility
    with existing exception handlers and tests.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status, retry_after=retry_after)


class TransportTimeoutError(TransportError, TimeoutError):
    """Raised when connect, read, or total socket/HTTP timeout occurs."""


class TransportNetworkError(TransportError):
    """Raised for DNS resolution failures, connection resets, or TCP transport drops."""


class TransportHTTPError(TransportError):
    """Raised when an HTTP response returns an unexpected error status code (>= 400)."""

    def __init__(
        self,
        message: str = "",
        *,
        status: int,
        retry_after: float | None = None,
        response_body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message, status=status, retry_after=retry_after)
        self.status_code = status
        self.response_body = response_body
        self.headers = headers or {}


class TransportAuthError(TransportHTTPError):
    """Raised on 401 Unauthorized or 403 Forbidden responses."""


class TransportNotFoundError(TransportHTTPError):
    """Raised on 404 Not Found responses."""


class TransportRateLimitError(TransportHTTPError):
    """Raised on 429 Too Many Requests responses."""


class TransportServerError(TransportHTTPError):
    """Raised on 5xx server error responses (500, 502, 503, 504)."""


class PayloadTooLargeError(TransportError):
    """Raised when an upstream response body exceeds the configured byte limit."""


class SSRFBlockedError(TransportError):
    """Raised when a request target violates Server-Side Request Forgery safety policies."""


# ---------------------------------------------------------------------------
# Data Provenance and Caching Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderProvenance:
    """Provenance and freshness metadata for an external provider response."""

    source: str
    source_url: str
    fetched_at: float
    source_timestamp: float | None = None
    status_code: int = 200
    from_cache: bool = False
    is_stale: bool = False
    etag: str | None = None
    last_modified: str | None = None
    content_length: int = 0


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """Generic wrapper combining typed domain data with provider provenance."""

    data: T
    provenance: ProviderProvenance


@dataclass
class HttpCacheEntry:
    """Cached response representation for conditional HTTP requests."""

    data: bytes
    headers: dict[str, str]
    etag: str | None = None
    last_modified: str | None = None
    cached_at: float = 0.0
    source_timestamp: float | None = None


class HttpTransportCache:
    """In-memory cache supporting TTL and conditional ETag / Last-Modified caching."""

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._entries: dict[str, HttpCacheEntry] = {}

    def get(self, url: str) -> HttpCacheEntry | None:
        return self._entries.get(url)

    def set(self, url: str, entry: HttpCacheEntry) -> None:
        if len(self._entries) >= self.max_entries and url not in self._entries:
            oldest_key = next(iter(self._entries))
            self._entries.pop(oldest_key, None)
        self._entries[url] = entry

    def clear(self) -> None:
        self._entries.clear()


@dataclass
class TransportResponse:
    """Low-level HTTP response object returned by BoundedHttpTransport."""

    status: int
    headers: dict[str, str]
    body: bytes
    provenance: ProviderProvenance
    _text: str | None = None
    _json: Any = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = self.body.decode("utf-8", errors="replace")
        return self._text

    def json(self) -> Any:
        if self._json is None:
            self._json = json.loads(self.text)
        return self._json


# ---------------------------------------------------------------------------
# TLS Verification Helper
# ---------------------------------------------------------------------------


def build_ssl_context(
    *,
    verify: bool = True,
    ca_bundle_path: str | None = None,
) -> ssl.SSLContext | bool:
    """Build a secure SSL context for aiohttp.TCPConnector or HTTPS client.

    If verify is False, logs a security warning and returns False (aiohttp ssl=False).
    Insecure TLS is never recommended as a convenient public default.
    If ca_bundle_path is given and valid, loads the specified PEM CA bundle.
    Otherwise, attempts to use certifi.where() if installed, falling back to system defaults.
    """
    if not verify:
        log.warning(
            "Insecure TLS verification is enabled (verify=False). "
            "This disables certificate checks and is not recommended for production."
        )
        return False

    cafile = str(ca_bundle_path or "").strip()
    if cafile:
        try:
            return ssl.create_default_context(cafile=cafile)
        except (OSError, ssl.SSLError, ValueError) as exc:
            log.warning(
                "Failed to load CA bundle from %s (%s); falling back to system defaults",
                cafile,
                exc,
            )

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError, ValueError):
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Bounded HTTP Transport
# ---------------------------------------------------------------------------


class BoundedHttpTransport:
    """Bounded, resilient HTTP transport abstraction for external providers."""

    def __init__(
        self,
        *,
        source_name: str = "generic",
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        total_timeout: float = 15.0,
        max_response_bytes: int = 5_000_000,
        max_retries: int = 2,
        base_retry_delay: float = 0.5,
        max_retry_delay: float = 10.0,
        max_retry_after: float = 30.0,
        retry_jitter: float = 0.2,
        user_agent: str = DEFAULT_USER_AGENT,
        ssl_verify: bool = True,
        ca_bundle_path: str | None = None,
        allowed_hosts: Sequence[str] | None = None,
        enforce_ssrf_protection: bool = True,
        session: aiohttp.ClientSession | None = None,
        cache: HttpTransportCache | None = None,
        cache_ttl_seconds: float = 0.0,
        clock: Callable[[], float] | None = None,
        sleep_func: Callable[[float], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.source_name = source_name
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.total_timeout = float(total_timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.max_retries = max(0, int(max_retries))
        self.base_retry_delay = float(base_retry_delay)
        self.max_retry_delay = float(max_retry_delay)
        self.max_retry_after = float(max_retry_after)
        self.retry_jitter = float(retry_jitter)
        self.user_agent = user_agent
        self.ssl_verify = bool(ssl_verify)
        self.ca_bundle_path = ca_bundle_path
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts else None
        self.enforce_ssrf_protection = bool(enforce_ssrf_protection)
        self._session = session
        self._owned_session: aiohttp.ClientSession | None = None
        self.cache = cache
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._clock = clock
        self._sleep_func = sleep_func

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.time()

    async def _sleep(self, seconds: float) -> None:
        if self._sleep_func is not None:
            await self._sleep_func(seconds)
        else:
            await asyncio.sleep(seconds)

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._owned_session is None or self._owned_session.closed:
            ssl_ctx = build_ssl_context(
                verify=self.ssl_verify,
                ca_bundle_path=self.ca_bundle_path,
            )
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._owned_session = aiohttp.ClientSession(connector=connector)
        return self._owned_session

    async def close(self) -> None:
        if self._owned_session is not None and not self._owned_session.closed:
            await self._owned_session.close()
            self._owned_session = None

    async def __aenter__(self) -> BoundedHttpTransport:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    def _calculate_backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return float(min(retry_after, self.max_retry_after))
        exp = min(self.max_retry_delay, self.base_retry_delay * (2 ** (attempt - 1)))
        jitter = random.uniform(0, self.retry_jitter)
        return float(min(self.max_retry_delay, exp + jitter))

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_data: Any = None,
        secrets: Sequence[str] | None = None,
        source_timestamp_extractor: Callable[[Any, dict[str, str]], float | None] | None = None,
    ) -> TransportResponse:
        """Execute an HTTP request with bounds, retries, SSRF safety, and conditional caching."""
        clean_url = str(url or "").strip()
        method_upper = method.strip().upper()
        is_idempotent = method_upper in ("GET", "HEAD", "OPTIONS")
        max_attempts = 1 + self.max_retries if is_idempotent else 1

        # SSRF Protection
        if self.enforce_ssrf_protection:
            val_res = validate_fetch_url(clean_url, allowed_hosts=self.allowed_hosts)
            if not val_res.ok:
                raise SSRFBlockedError(f"SSRF policy blocked request to {clean_url!r}: {val_res.reason}")

        now = self._now()
        cache_entry: HttpCacheEntry | None = None
        if is_idempotent and self.cache is not None:
            cache_entry = self.cache.get(clean_url)
            # Fast return if cached entry is fresh within TTL
            if (
                cache_entry is not None
                and self.cache_ttl_seconds > 0
                and (now - cache_entry.cached_at) <= self.cache_ttl_seconds
            ):
                prov = ProviderProvenance(
                    source=self.source_name,
                    source_url=clean_url,
                    fetched_at=now,
                    source_timestamp=cache_entry.source_timestamp,
                    status_code=200,
                    from_cache=True,
                    etag=cache_entry.etag,
                    last_modified=cache_entry.last_modified,
                    content_length=len(cache_entry.data),
                )
                return TransportResponse(
                    status=200,
                    headers=dict(cache_entry.headers),
                    body=cache_entry.data,
                    provenance=prov,
                )

        req_headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        if headers:
            for k, v in headers.items():
                req_headers[str(k)] = str(v)

        if is_idempotent and cache_entry is not None:
            if cache_entry.etag:
                req_headers["If-None-Match"] = cache_entry.etag
            if cache_entry.last_modified:
                req_headers["If-Modified-Since"] = cache_entry.last_modified

        client_timeout = aiohttp.ClientTimeout(
            sock_connect=self.connect_timeout,
            sock_read=self.read_timeout,
            total=self.total_timeout,
        )

        session = await self.get_session()

        for attempt in range(1, max_attempts + 1):
            try:
                async with session.request(
                    method_upper,
                    clean_url,
                    params=params,
                    headers=req_headers,
                    data=data,
                    json=json_data,
                    timeout=client_timeout,
                ) as resp:
                    status = resp.status
                    resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
                    retry_after = parse_retry_after(resp_headers.get("Retry-After"))

                    # Handle 304 Not Modified
                    if status == 304 and cache_entry is not None:
                        curr_now = self._now()
                        cache_entry.cached_at = curr_now
                        prov = ProviderProvenance(
                            source=self.source_name,
                            source_url=clean_url,
                            fetched_at=curr_now,
                            source_timestamp=cache_entry.source_timestamp,
                            status_code=304,
                            from_cache=True,
                            etag=cache_entry.etag,
                            last_modified=cache_entry.last_modified,
                            content_length=len(cache_entry.data),
                        )
                        return TransportResponse(
                            status=200,
                            headers=dict(cache_entry.headers),
                            body=cache_entry.data,
                            provenance=prov,
                        )

                    # Enforce Content-Length before downloading if provided
                    cl_str = resp_headers.get("Content-Length")
                    if cl_str:
                        try:
                            if int(cl_str) > self.max_response_bytes:
                                raise PayloadTooLargeError(
                                    f"Response Content-Length {cl_str} exceeds limit of "
                                    f"{self.max_response_bytes} bytes"
                                )
                        except ValueError:
                            pass

                    # Read payload with streaming chunk size enforcement
                    if hasattr(resp, "content") and hasattr(resp.content, "iter_chunked"):
                        chunks = bytearray()
                        async for chunk in resp.content.iter_chunked(65536):
                            chunks.extend(chunk)
                            if len(chunks) > self.max_response_bytes:
                                if hasattr(resp, "close"):
                                    resp.close()
                                raise PayloadTooLargeError(
                                    f"Response payload exceeded limit of {self.max_response_bytes} "
                                    f"bytes during streaming"
                                )
                        raw_body = bytes(chunks)
                    else:
                        raw_body = await resp.read()
                        if len(raw_body) > self.max_response_bytes:
                            raise PayloadTooLargeError(
                                f"Response payload length {len(raw_body)} exceeds limit of "
                                f"{self.max_response_bytes} bytes"
                            )

                    if status >= 400:
                        safe_url = redact_secrets(clean_url, secrets)
                        safe_body = redact_secrets(raw_body[:300].decode("utf-8", errors="replace"), secrets)
                        err_msg = f"HTTP {status} from {safe_url}: {safe_body}"

                        can_retry = (
                            is_idempotent and attempt < max_attempts and (status in (429, 500, 502, 503, 504))
                        )
                        if can_retry:
                            if retry_after is not None and retry_after > self.max_retry_after:
                                pass
                            else:
                                delay = self._calculate_backoff(attempt, retry_after)
                                log.warning(
                                    "Retrying request to %s after HTTP %s (attempt %d/%d, delay %.2fs)",
                                    safe_url,
                                    status,
                                    attempt,
                                    max_attempts,
                                    delay,
                                )
                                await self._sleep(delay)
                                continue

                        if status in (401, 403):
                            raise TransportAuthError(
                                err_msg,
                                status=status,
                                retry_after=retry_after,
                                response_body=safe_body,
                                headers=resp_headers,
                            )
                        elif status == 404:
                            raise TransportNotFoundError(
                                err_msg,
                                status=status,
                                retry_after=retry_after,
                                response_body=safe_body,
                                headers=resp_headers,
                            )
                        elif status == 429:
                            raise TransportRateLimitError(
                                err_msg,
                                status=status,
                                retry_after=retry_after,
                                response_body=safe_body,
                                headers=resp_headers,
                            )
                        elif status >= 500:
                            raise TransportServerError(
                                err_msg,
                                status=status,
                                retry_after=retry_after,
                                response_body=safe_body,
                                headers=resp_headers,
                            )
                        else:
                            raise TransportHTTPError(
                                err_msg,
                                status=status,
                                retry_after=retry_after,
                                response_body=safe_body,
                                headers=resp_headers,
                            )

                    # Successful response (status < 400)
                    curr_now = self._now()
                    etag = resp_headers.get("ETag")
                    last_mod = resp_headers.get("Last-Modified")
                    source_ts: float | None = None
                    if source_timestamp_extractor:
                        try:
                            parsed_obj = json.loads(raw_body.decode("utf-8")) if raw_body else None
                            source_ts = source_timestamp_extractor(parsed_obj, resp_headers)
                        except (ValueError, TypeError, KeyError, AttributeError) as exc:
                            log.debug("Failed extracting source timestamp: %s", exc)

                    if (
                        is_idempotent
                        and self.cache is not None
                        and (etag or last_mod or self.cache_ttl_seconds > 0)
                    ):
                        self.cache.set(
                            clean_url,
                            HttpCacheEntry(
                                data=raw_body,
                                headers=resp_headers,
                                etag=etag,
                                last_modified=last_mod,
                                cached_at=curr_now,
                                source_timestamp=source_ts,
                            ),
                        )

                    prov = ProviderProvenance(
                        source=self.source_name,
                        source_url=clean_url,
                        fetched_at=curr_now,
                        source_timestamp=source_ts,
                        status_code=status,
                        from_cache=False,
                        etag=etag,
                        last_modified=last_mod,
                        content_length=len(raw_body),
                    )
                    return TransportResponse(
                        status=status,
                        headers=resp_headers,
                        body=raw_body,
                        provenance=prov,
                    )

            except TimeoutError as exc:
                safe_url = redact_secrets(clean_url, secrets)
                if is_idempotent and attempt < max_attempts:
                    delay = self._calculate_backoff(attempt, None)
                    log.warning(
                        "Retrying request to %s after timeout (attempt %d/%d, delay %.2fs)",
                        safe_url,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    await self._sleep(delay)
                    continue
                raise TransportTimeoutError(f"Request to {safe_url} timed out: {exc}") from exc
            except aiohttp.ClientError as exc:
                safe_url = redact_secrets(clean_url, secrets)
                if is_idempotent and attempt < max_attempts:
                    delay = self._calculate_backoff(attempt, None)
                    log.warning(
                        "Retrying request to %s after network error (attempt %d/%d, delay %.2fs): %s",
                        safe_url,
                        attempt,
                        max_attempts,
                        delay,
                        exc,
                    )
                    await self._sleep(delay)
                    continue
                raise TransportNetworkError(f"Network error requesting {safe_url}: {exc}") from exc
            except asyncio.CancelledError:
                raise

        raise TransportError(f"Failed to execute request to {clean_url}")

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        secrets: Sequence[str] | None = None,
        source_timestamp_extractor: Callable[[Any, dict[str, str]], float | None] | None = None,
    ) -> TransportResponse:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            secrets=secrets,
            source_timestamp_extractor=source_timestamp_extractor,
        )

    async def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_data: Any = None,
        secrets: Sequence[str] | None = None,
    ) -> TransportResponse:
        return await self.request(
            "POST",
            url,
            params=params,
            headers=headers,
            data=data,
            json_data=json_data,
            secrets=secrets,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        secrets: Sequence[str] | None = None,
        source_timestamp_extractor: Callable[[Any, dict[str, str]], float | None] | None = None,
    ) -> ProviderResult[Any]:
        resp = await self.get(
            url,
            params=params,
            headers=headers,
            secrets=secrets,
            source_timestamp_extractor=source_timestamp_extractor,
        )
        return ProviderResult(data=resp.json(), provenance=resp.provenance)
