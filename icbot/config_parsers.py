"""Pure configuration value coercion, format validation, and SQLite path normalization primitives.

This module intentionally has zero Discord, database, or async dependencies so
features, background pollers, and ``icbot.config`` can share a single canonical
implementation of scalar parsing and input validation without import cycles.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icbot.errors import ConfigValidationError

# Discord "snowflake" IDs fit in signed int64 for most libs
MAX_SNOWFLAKE = 9223372036854775807

_HEX_COLOR_RE = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")
_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
_TRUE_VALUES = ("1", "true", "yes", "y", "on")
_FALSE_VALUES = ("0", "false", "no", "n", "off")


def parse_int(value: Any, default: int = 0, *, clamp_snowflake: bool = False) -> int:
    """Best-effort int parsing with optional Discord snowflake bounds clamping."""
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        n = int(s)
        if clamp_snowflake and (n < 0 or n > MAX_SNOWFLAKE):
            return default
        return n
    except (TypeError, ValueError):
        return default


def parse_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float parsing returning ``default`` on empty or malformed values."""
    try:
        if value is None:
            return default
        s = str(value).strip()
        if not s:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse common boolean strings (``1/true/yes/y/on`` vs ``0/false/no/n/off``)."""
    try:
        if value is None:
            return default
        s = str(value).strip().lower()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
            return False
        return default
    except (TypeError, ValueError):
        return default


def parse_color(value: Any, default: int = 0xA64DFF) -> int:
    """Parse embed color from '#RRGGBB', '0xRRGGBB', 'RRGGBB', or int."""
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return default

        # Already numeric
        if s.isdigit():
            return int(s)

        m = _HEX_COLOR_RE.match(s)
        if m:
            return int(m.group(1), 16)

        return default
    except (TypeError, ValueError):
        return default


def parse_str_list(raw: Any) -> list[str]:
    """Parse comma-separated env lists, stripping junk tokens (e.g., '\\') and deduplicating."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = [str(x).strip() for x in raw]
    else:
        s = str(raw).strip()
        if not s:
            return []
        tokens = [tok.strip() for tok in s.split(",")]

    out: list[str] = []
    for t in tokens:
        if not t or t in ("\\", "\\\\"):
            continue
        out.append(t)

    # de-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq


def is_valid_http_url(raw: str) -> bool:
    """Return True if ``raw`` is a whitespace-free HTTP or HTTPS URL with a network location."""
    s = (raw or "").strip()
    if not s or any(ch.isspace() for ch in s):
        return False
    parsed = urlparse(s)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def is_valid_hhmm(raw: str) -> bool:
    """Return True if ``raw`` is a valid 24-hour ``HH:MM`` clock string."""
    m = _HHMM_RE.fullmatch((raw or "").strip())
    if not m:
        return False
    hh, mm = int(m.group(1)), int(m.group(2))
    return 0 <= hh <= 23 and 0 <= mm <= 59


def is_valid_tz(tz_name: str) -> bool:
    """Return True if ``tz_name`` resolves to a valid IANA timezone."""
    name = (tz_name or "").strip()
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False


def is_valid_snowflake(val: int, *, allow_zero: bool = True) -> bool:
    """Return True if ``val`` is a non-boolean int within Discord snowflake bounds."""
    if isinstance(val, bool) or not isinstance(val, int):
        return False
    lower = 0 if allow_zero else 1
    return lower <= val <= MAX_SNOWFLAKE


def is_memory_sqlite_path(raw: str) -> bool:
    """Return True if ``raw`` designates an in-memory SQLite database URI."""
    s = (raw or "").strip()
    return s == ":memory:" or s.startswith("file::memory:") or "mode=memory" in s


def normalize_sqlite_path(raw: str, *, allow_empty: bool = False) -> str:
    """Normalize a configured SQLite database file path while preserving special memory URIs."""
    s = (raw or "").strip()
    if not s:
        if allow_empty:
            return ""
        raise ConfigValidationError("SQLite database path must not be empty.")
    if "\x00" in s:
        raise ConfigValidationError("SQLite database path must not contain null bytes.")
    if is_memory_sqlite_path(s):
        return s
    if s in (".", "./", "..", "../") or s.endswith(("/", "\\")):
        raise ConfigValidationError(f"SQLite database path must point to a file, not a directory: {raw!r}")
    expanded = os.path.expanduser(s)
    normed = os.path.normpath(expanded)
    if normed in ("", "."):
        raise ConfigValidationError(f"SQLite database path must point to a file, not a directory: {raw!r}")
    return normed


__all__ = [
    "MAX_SNOWFLAKE",
    "_FALSE_VALUES",
    "_HEX_COLOR_RE",
    "_HHMM_RE",
    "_TRUE_VALUES",
    "is_memory_sqlite_path",
    "is_valid_hhmm",
    "is_valid_http_url",
    "is_valid_snowflake",
    "is_valid_tz",
    "normalize_sqlite_path",
    "parse_bool",
    "parse_color",
    "parse_float",
    "parse_int",
    "parse_str_list",
]
