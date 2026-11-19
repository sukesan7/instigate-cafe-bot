from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp

from icbot.errors import parse_retry_after, redact_secrets
from icbot.services.http_transport import ProviderProvenance, ProviderResult, TransportError

log = logging.getLogger(__name__)

_TORONTO_TZ = ZoneInfo("America/Toronto")


class GOFetchError(TransportError):
    pass


@dataclass(frozen=True)
class GOMessage:
    code: str
    status: str | None
    posted_dt: str | None
    subject_en: str | None
    body_en: str | None
    category: str | None
    subcategory: str | None
    lines: list[str]
    stops: list[str]
    trips: list[str]


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _extract_list_codes(items: Any, key: str) -> list[str]:
    out: list[str] = []
    for it in _as_list(items):
        if isinstance(it, dict):
            v = it.get(key)
            if v:
                out.append(str(v))
        else:
            try:
                v = getattr(it, key, None)
                if v:
                    out.append(str(v))
            except (AttributeError, TypeError) as exc:
                log.debug("Skipping GO alert item attribute %s: %s", key, exc)
    return out


def _parse_messages(payload: Any) -> list[GOMessage]:
    if not isinstance(payload, dict):
        return []

    # expected: {"Metadata": {...}, "Messages": {"Message":[...]}}
    messages = payload.get("Messages") or {}
    raw = messages.get("Message") if isinstance(messages, dict) and "Message" in messages else messages

    out: list[GOMessage] = []
    for m in _as_list(raw):
        if not isinstance(m, dict):
            continue
        out.append(
            GOMessage(
                code=str(m.get("Code") or "").strip(),
                status=(str(m.get("Status")).strip() if m.get("Status") is not None else None),
                posted_dt=(
                    str(m.get("PostedDateTime")).strip() if m.get("PostedDateTime") is not None else None
                ),
                subject_en=(
                    str(m.get("SubjectEnglish")).strip() if m.get("SubjectEnglish") is not None else None
                ),
                body_en=(str(m.get("BodyEnglish")).strip() if m.get("BodyEnglish") is not None else None),
                category=(str(m.get("Category")).strip() if m.get("Category") is not None else None),
                subcategory=(str(m.get("SubCategory")).strip() if m.get("SubCategory") is not None else None),
                lines=_extract_list_codes(m.get("Lines"), "Code"),
                stops=_extract_list_codes(m.get("Stops"), "Name"),
                trips=_extract_list_codes(m.get("Trips"), "TripNumber"),
            )
        )
    # drop empties
    return [x for x in out if x.code]


async def fetch_service_alerts(
    session: aiohttp.ClientSession,
    api_base: str,
    access_key: str,
    timeout_seconds: int = 18,
) -> list[GOMessage]:
    """Fetch GO Transit service alerts via the official GO API.

    The GO API requires an Access Key for every call. Many deployments accept the key as the
    query parameter `key`. We send it as a query param and also include common header names
    for compatibility.
    """
    base = (api_base or "").rstrip("/")
    if not base:
        raise GOFetchError("missing api_base")

    params = {"key": access_key}
    url = f"{base}/ServiceUpdate/ServiceAlert/All?{urlencode(params)}"

    headers = {
        "Accept": "application/json",
        # Some API gateways accept the key as a header; harmless if ignored.
        "x-api-key": access_key,
        "X-API-Key": access_key,
    }

    secrets = (access_key,)
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as resp:
            # Some GO endpoints return 200 + ErrorCode in Metadata, but also can return non-200.
            text = await resp.text()
            if resp.status != 200:
                safe_url = redact_secrets(url, secrets)
                safe_text = redact_secrets(text[:300], secrets)
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                raise GOFetchError(
                    f"HTTP {resp.status} via GET {safe_url} :: {safe_text}",
                    status=resp.status,
                    retry_after=retry_after,
                )
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as e:
                safe_text = redact_secrets(text[:300], secrets)
                safe_err = redact_secrets(repr(e), secrets)
                raise GOFetchError(f"invalid json: {safe_err} :: {safe_text}") from e
    except (TimeoutError, aiohttp.ClientError) as e:
        safe_err = redact_secrets(repr(e), secrets)
        raise GOFetchError(f"network error: {safe_err}") from e

    msgs = _parse_messages(payload)
    return msgs


def parse_toronto_timestamp(dt_str: str | None) -> float | None:
    """Parse a GO Transit datetime string into a UTC epoch timestamp in America/Toronto."""
    if not dt_str:
        return None
    raw = dt_str.strip()
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TORONTO_TZ)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=_TORONTO_TZ)
            return dt.timestamp()
        except ValueError:
            pass
    return None


async def fetch_service_alerts_provenance(
    session: aiohttp.ClientSession,
    api_base: str,
    access_key: str,
    *,
    timeout_seconds: int = 18,
    clock: Callable[[], float] | None = None,
) -> ProviderResult[list[GOMessage]]:
    """Fetch service alerts and return them wrapped in a ProviderResult with provenance."""
    msgs = await fetch_service_alerts(
        session,
        api_base,
        access_key,
        timeout_seconds=timeout_seconds,
    )
    now = clock() if clock is not None else time.time()
    source_ts: float | None = None
    if msgs:
        for m in msgs:
            ts = parse_toronto_timestamp(m.posted_dt)
            if ts is not None and (source_ts is None or ts > source_ts):
                source_ts = ts

    clean_base = (api_base or "").rstrip("/")
    prov = ProviderProvenance(
        source="go_transit",
        source_url=f"{clean_base}/ServiceUpdate/ServiceAlert/All",
        fetched_at=now,
        source_timestamp=source_ts,
        status_code=200,
        content_length=len(msgs),
    )
    return ProviderResult(data=msgs, provenance=prov)
