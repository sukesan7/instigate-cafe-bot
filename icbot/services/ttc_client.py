from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import List, Optional
import html
import re
import xml.etree.ElementTree as ET

import aiohttp

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = _TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass(frozen=True)
class TTCItem:
    guid: str
    title: str
    link: str
    summary: str
    published_iso: Optional[str]


async def fetch_rss_items(
    session: aiohttp.ClientSession,
    feed_url: str,
    *,
    limit: int = 20,
    timeout_seconds: int = 12,
) -> List[TTCItem]:
    """Fetch and parse an RSS feed into TTCItem objects."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "User-Agent": "InstigateCafeBot/0.1 (+discord bot; personal project)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    async with session.get(feed_url, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        xml_text = await resp.text()

    root = ET.fromstring(xml_text)
    items: List[TTCItem] = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip() or title

        desc = (item.findtext("description") or "").strip()
        summary = _strip_html(desc)

        pub_raw = (item.findtext("pubDate") or "").strip()
        published_iso = None
        if pub_raw:
            try:
                published_iso = parsedate_to_datetime(pub_raw).isoformat()
            except Exception:
                published_iso = pub_raw

        if title or summary:
            items.append(
                TTCItem(
                    guid=guid,
                    title=title,
                    link=link,
                    summary=summary,
                    published_iso=published_iso,
                )
            )

        if len(items) >= limit:
            break

    return items
