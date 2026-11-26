"""URL validation and SSRF protection for campus academic sources and resource links.

Separates two trust levels:
1. ``validate_resource_url``: validates user-entered external links stored in the manual
   catalog (links are displayed to Discord users, never fetched by the bot server).
2. ``validate_fetch_url``: strict validation for server-side HTTP fetch targets
   (requires HTTPS, blocks private/loopback/link-local/metadata/CGNAT/alt-encoded IPs and
   internal hostnames, restricts ports, rejects embedded credentials, and enforces
   optional domain allowlists).

Also provides connector-level DNS validation (``ValidatingDnsResolver`` and
``create_ssrf_safe_connector``), bounded per-hop redirect handling
(``request_with_safe_redirects``), and chunk-by-chunk response size enforcement
(``read_response_bytes_limited``, ``read_response_text_limited``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from icbot.errors import ProviderError, redact_secrets

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
_DEFAULT_SAFE_PORTS: tuple[int, ...] = (443,)
_DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_DISCARD_V6_NETWORK = ipaddress.IPv6Network("100::/64")
_NAT64_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


class SSRFPolicyError(ProviderError, ValueError):
    """Raised when an outbound URL, redirect hop, port, or resolved IP violates SSRF policy."""


class StreamingPayloadLimitError(ProviderError, ValueError):
    """Raised when a streamed HTTP response exceeds its byte limit."""


@dataclass(frozen=True)
class UrlValidationResult:
    """Result of validating an external URL for storage or server-side fetching."""

    ok: bool
    normalized_url: str = ""
    host: str = ""
    reason: str = ""


def sanitize_url_for_log(url: str) -> str:
    """Return a URL with credentials, query strings, fragments, and secrets stripped."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return redact_secrets(raw.split("?", 1)[0][:200])
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        host_disp = f"[{host}]" if ":" in host else host
        try:
            port_part = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port_part = ""
        path_part = parsed.path or ""
        return redact_secrets(f"{parsed.scheme.lower()}://{host_disp}{port_part}{path_part}")
    return redact_secrets(raw.split("?", 1)[0].split("#", 1)[0][:200])


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse canonical or alternative IPv4/IPv6 representations; return None for domain names."""
    cleaned = str(host or "").strip().strip("[]")
    if not cleaned:
        return None

    # Strip any IPv6 zone ID only to identify whether it's an IP literal (zone IDs themselves are non-public).
    base_part, sep, _zone = cleaned.partition("%")
    candidate = base_part if sep else cleaned

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass

    # Detect alternative IPv4 encodings (decimal integer, hex, octal, shorthand like 127.1).
    # Reject empty/whitespace and guard against domain names (inet_aton rejects them with OSError).
    if ":" not in cleaned and not any(ch.isspace() for ch in cleaned):
        try:
            packed = socket.inet_aton(candidate)
            return ipaddress.IPv4Address(packed)
        except OSError:
            return None
    return None


def _is_non_public_ip_obj(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IPv4/IPv6 address object is private, reserved, or non-globally-routable."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return True
        if ip.ipv4_mapped is not None:
            return _is_non_public_ip_obj(ip.ipv4_mapped)
        # Transition mechanisms encapsulate arbitrary IPv4 networks and must not be trusted.
        if ip.sixtofour is not None or ip.teredo is not None:
            return True
        if ip in _DISCARD_V6_NETWORK or any(ip in net for net in _NAT64_NETWORKS):
            return True
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_site_local
            or not ip.is_global
        )

    if ip in _CGNAT_NETWORK:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def is_public_routable_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only when ``value`` is a valid, globally publicly routable IP address."""
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return not _is_non_public_ip_obj(value)

    raw = str(value or "").strip().strip("[]")
    if not raw or "%" in raw:
        return False
    ip = _parse_ip_literal(raw)
    if ip is None:
        return False
    return not _is_non_public_ip_obj(ip)


def _is_private_or_reserved_ip(host: str) -> bool:
    """Return True if ``host`` parses (canonically or in alt IPv4 form) as a non-public IP."""
    cleaned = str(host or "").strip().strip("[]")
    if not cleaned:
        return False
    if "%" in cleaned:
        # Zone IDs only apply to scoped/link-local interfaces and never identify public endpoints.
        return _parse_ip_literal(cleaned) is not None or True
    ip = _parse_ip_literal(cleaned)
    if ip is None:
        return False
    return _is_non_public_ip_obj(ip)


def _is_blocked_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if not h or "\\" in h or "%" in h:
        return True
    if h in _BLOCKED_HOSTNAMES:
        return True
    if any(h.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        return True
    if _is_private_or_reserved_ip(h):
        return True
    # Reject single-label hostnames (e.g. "redis", "postgres", "intranet")
    return bool("." not in h and ":" not in h)


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
    if "\\" in raw:
        return UrlValidationResult(ok=False, reason="URL must not contain backslashes.")

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

    try:
        _ = parsed.port
    except ValueError:
        return UrlValidationResult(ok=False, reason="Malformed port in URL.")

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
    allowed_ports: Sequence[int] = _DEFAULT_SAFE_PORTS,
) -> UrlValidationResult:
    """Strict SSRF guard for server-side HTTP fetches by academic and outbound providers.

    Requires HTTPS, blocks private/loopback/link-local/metadata/CGNAT/alt-encoded IP
    addresses and single-label internal hostnames, rejects non-standard ports and embedded
    credentials, and enforces ``allowed_hosts`` when provided.
    """
    base = validate_resource_url(url, require_https=True)
    if not base.ok:
        return base

    try:
        port = urlparse(base.normalized_url).port
    except ValueError:
        return UrlValidationResult(ok=False, reason="Malformed port in URL.")

    if port is not None and port not in allowed_ports:
        return UrlValidationResult(
            ok=False,
            host=base.host,
            reason=f"Port {port} is not permitted for server-side fetches.",
        )

    # Reject raw IP addresses (canonical or alternative numeric encodings) for server-side fetches.
    if _parse_ip_literal(base.host) is not None:
        return UrlValidationResult(
            ok=False,
            reason="Direct IP addresses are not permitted for server-side HTTP fetches.",
        )

    if allowed_hosts:
        cleaned_allowlist = [h for h in allowed_hosts if str(h or "").strip()]
        if cleaned_allowlist and not _host_matches_allowlist(base.host, cleaned_allowlist):
            return UrlValidationResult(
                ok=False,
                host=base.host,
                reason=f"Host {base.host!r} is not in the configured fetch allowlist.",
            )

    return base


class ValidatingDnsResolver(aiohttp.abc.AbstractResolver):
    """aiohttp DNS resolver that rejects any resolution yielding non-public IPs."""

    def __init__(
        self,
        inner_resolver: aiohttp.abc.AbstractResolver | None = None,
        *,
        resolve_timeout: float = 5.0,
    ) -> None:
        self._inner = inner_resolver if inner_resolver is not None else aiohttp.DefaultResolver()
        self._resolve_timeout = float(resolve_timeout)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
        clean_host = str(host or "").strip()
        if _is_blocked_hostname(clean_host):
            raise SSRFPolicyError(f"Blocked DNS resolution for disallowed hostname {clean_host!r}.")

        results = await asyncio.wait_for(
            self._inner.resolve(clean_host, port, family=family),
            timeout=self._resolve_timeout,
        )
        for entry in results:
            ip_str = str(entry.get("host") or "").strip()
            if not is_public_routable_ip(ip_str):
                raise SSRFPolicyError(f"DNS resolution for {clean_host!r} returned non-public IP address.")
        return results

    async def close(self) -> None:
        await self._inner.close()


def create_ssrf_safe_connector(
    *,
    ssl_ctx: ssl.SSLContext | bool = True,
    inner_resolver: aiohttp.abc.AbstractResolver | None = None,
    resolve_timeout: float = 5.0,
    **connector_kwargs: Any,
) -> aiohttp.TCPConnector:
    """Create an ``aiohttp.TCPConnector`` that validates resolved IPs on every connection."""
    resolver = ValidatingDnsResolver(inner_resolver=inner_resolver, resolve_timeout=resolve_timeout)
    return aiohttp.TCPConnector(
        ssl=ssl_ctx,
        resolver=resolver,
        use_dns_cache=False,
        **connector_kwargs,
    )


async def read_response_bytes_limited(
    resp: Any,
    max_bytes: int,
    *,
    label: str = "response",
) -> bytes:
    """Read an HTTP response body while enforcing ``max_bytes`` during chunk streaming."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")

    headers = getattr(resp, "headers", None)
    if headers is not None:
        cl_raw = headers.get("Content-Length")
        if cl_raw:
            try:
                if int(cl_raw) > max_bytes:
                    if hasattr(resp, "close"):
                        resp.close()
                    raise StreamingPayloadLimitError(
                        f"{label} Content-Length ({cl_raw}) exceeds limit of {max_bytes} bytes."
                    )
            except ValueError:
                pass

    content = getattr(resp, "content", None)
    if content is not None and hasattr(content, "iter_chunked"):
        buf = bytearray()
        async for chunk in content.iter_chunked(65536):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                if hasattr(resp, "close"):
                    resp.close()
                raise StreamingPayloadLimitError(
                    f"{label} exceeded limit of {max_bytes} bytes during streaming."
                )
        return bytes(buf)

    if hasattr(resp, "read"):
        raw = bytes(await resp.read())
    else:
        raw = str(await resp.text(errors="replace")).encode("utf-8")

    if len(raw) > max_bytes:
        if hasattr(resp, "close"):
            resp.close()
        raise StreamingPayloadLimitError(
            f"{label} payload ({len(raw)} bytes) exceeds limit of {max_bytes} bytes."
        )
    return raw


async def read_response_text_limited(
    resp: Any,
    max_bytes: int,
    *,
    label: str = "response",
    errors: str = "replace",
) -> str:
    """Read and decode an HTTP response body while enforcing ``max_bytes``."""
    raw = await read_response_bytes_limited(resp, max_bytes, label=label)
    charset = getattr(resp, "charset", None) or "utf-8"
    try:
        return raw.decode(charset, errors=errors)
    except LookupError:
        return raw.decode("utf-8", errors=errors)


@asynccontextmanager
async def request_with_safe_redirects(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    allowed_ports: Sequence[int] = _DEFAULT_SAFE_PORTS,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    **request_kwargs: Any,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Execute ``session.request`` with ``allow_redirects=False`` and validate every redirect hop."""
    check = validate_fetch_url(url, allowed_hosts=allowed_hosts, allowed_ports=allowed_ports)
    if not check.ok:
        raise SSRFPolicyError(f"Refused unsafe fetch URL ({sanitize_url_for_log(url)}): {check.reason}")

    current_url = check.normalized_url
    visited: set[str] = {current_url}
    kwargs = dict(request_kwargs)
    kwargs.pop("allow_redirects", None)

    for hop in range(max_redirects + 1):
        async with session.request(method, current_url, allow_redirects=False, **kwargs) as resp:
            if resp.status not in _REDIRECT_STATUSES:
                yield resp
                return

            location = (resp.headers.get("Location") or "").strip() if getattr(resp, "headers", None) else ""
            if not location:
                yield resp
                return

            if hop >= max_redirects:
                raise SSRFPolicyError(
                    f"Exceeded maximum redirects ({max_redirects}) fetching {sanitize_url_for_log(current_url)}."
                )

            next_url = urljoin(current_url, location)
            next_check = validate_fetch_url(
                next_url,
                allowed_hosts=allowed_hosts,
                allowed_ports=allowed_ports,
            )
            if not next_check.ok:
                raise SSRFPolicyError(
                    f"Blocked unsafe redirect from {sanitize_url_for_log(current_url)} "
                    f"to {sanitize_url_for_log(next_url)}: {next_check.reason}"
                )

            if next_check.normalized_url in visited:
                raise SSRFPolicyError(
                    f"Detected redirect loop involving {sanitize_url_for_log(next_check.normalized_url)}."
                )

            visited.add(next_check.normalized_url)
            current_url = next_check.normalized_url

    raise SSRFPolicyError(f"Exceeded maximum redirects ({max_redirects}).")
