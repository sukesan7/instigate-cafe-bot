"""URL validation and SSRF protection for campus academic sources and resource links.

Separates two trust levels:
1. ``validate_resource_url``: validates user-entered external links stored in the manual
   catalog (links are displayed to Discord users, never fetched by the bot server).
2. ``validate_fetch_url``: strict validation for server-side HTTP fetch targets
   (requires HTTPS, blocks private/loopback/link-local/metadata IPs and internal hostnames,
   rejects embedded credentials, and enforces optional domain allowlists).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)

_BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
)

_MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class UrlValidationResult:
    """Result of validating an external URL for storage or server-side fetching."""

    ok: bool
    normalized_url: str = ""
    host: str = ""
    reason: str = ""


def _is_private_or_reserved_ip(host: str) -> bool:
    """Return True if ``host`` parses as a non-globally-routable IP address."""
    cleaned = host.strip("[]")
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_blocked_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if not h:
        return True
    if h in _BLOCKED_HOSTNAMES:
        return True
    if any(h.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return True
    # Reject single-label hostnames (e.g. "redis", "postgres", "intranet")
    if "." not in h and ":" not in h:
        return True
    return _is_private_or_reserved_ip(h)


def _host_matches_allowlist(host: str, allowed_hosts: Sequence[str]) -> bool:
    """Return True if ``host`` equals or is a subdomain of an allowlisted host."""
    h = host.lower().rstrip(".")
    for entry in allowed_hosts:
        candidate = str(entry or "").strip().lower().rstrip(".")
        if not candidate:
            continue
        if "://" in candidate:
            parsed = urlparse(candidate)
            candidate = (parsed.hostname or "").lower().rstrip(".")
        if not candidate:
            continue
        if h == candidate or h.endswith(f".{candidate}"):
            return True
    return False


def validate_resource_url(url: str, *, require_https: bool = False) -> UrlValidationResult:
    """Validate an external academic resource URL stored in the guild catalog."""
    raw = str(url or "").strip()
    if not raw:
        return UrlValidationResult(ok=False, reason="URL must not be empty.")
    if len(raw) > _MAX_URL_LENGTH:
        return UrlValidationResult(ok=False, reason=f"URL exceeds maximum length ({_MAX_URL_LENGTH}).")
    if any(ch.isspace() for ch in raw):
        return UrlValidationResult(ok=False, reason="URL must not contain whitespace.")

    try:
        parsed = urlparse(raw)
    except ValueError:
        return UrlValidationResult(ok=False, reason="Malformed URL.")

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = ("https",) if require_https else ("https", "http")
    if scheme not in allowed_schemes:
        return UrlValidationResult(
            ok=False,
            reason=f"URL scheme {scheme!r} is not allowed (expected {', '.join(allowed_schemes)}).",
        )

    if parsed.username or parsed.password:
        return UrlValidationResult(ok=False, reason="URL must not contain embedded credentials.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return UrlValidationResult(ok=False, reason="URL is missing a valid hostname.")

    if _is_blocked_hostname(host):
        return UrlValidationResult(
            ok=False,
            reason=f"Host {host!r} is private, local, or disallowed.",
        )

    return UrlValidationResult(ok=True, normalized_url=raw, host=host)


def validate_fetch_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
) -> UrlValidationResult:
    """Strict SSRF guard for server-side HTTP fetches by academic providers.

    Requires HTTPS, blocks private/loopback/link-local/metadata addresses and single-label
    internal hostnames, rejects non-standard credentials, and enforces ``allowed_hosts``
    when provided.
    """
    base = validate_resource_url(url, require_https=True)
    if not base.ok:
        return base

    # Reject raw IP addresses entirely for server-side academic scraping
    cleaned_host = base.host.strip("[]")
    try:
        ipaddress.ip_address(cleaned_host)
        return UrlValidationResult(
            ok=False,
            reason="Direct IP addresses are not permitted for server-side academic fetches.",
        )
    except ValueError:
        pass

    if allowed_hosts:
        cleaned_allowlist = [h for h in allowed_hosts if str(h or "").strip()]
        if cleaned_allowlist and not _host_matches_allowlist(base.host, cleaned_allowlist):
            return UrlValidationResult(
                ok=False,
                host=base.host,
                reason=f"Host {base.host!r} is not in the configured academic fetch allowlist.",
            )

    return base
