from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)


class GOFetchError(RuntimeError):
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

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_seconds)
        ) as resp:
            # Some GO endpoints return 200 + ErrorCode in Metadata, but also can return non-200.
            text = await resp.text()
            if resp.status != 200:
                raise GOFetchError(f"HTTP {resp.status} via GET {url} :: {text[:300]}")
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as e:
                raise GOFetchError(f"invalid json: {e!r} :: {text[:300]}") from e
    except (TimeoutError, aiohttp.ClientError) as e:
        raise GOFetchError(f"network error: {e!r}") from e

    msgs = _parse_messages(payload)
    return msgs
