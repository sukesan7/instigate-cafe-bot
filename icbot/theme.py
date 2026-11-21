"""Generic theming, branding presets, and visual accessibility contracts.

Provides reusable campus and community branding configurations so forks or other
universities can deploy the bot without source modifications, while preserving the
original Instigate Cafe brand as the default community preset.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

EmbedStatus = Literal["normal", "info", "success", "warning", "error", "disabled"]

# Named semantic embed colors (all within the relative luminance heuristic range 0.03 <= L <= 0.95)
SEMANTIC_COLORS: Mapping[str, int] = MappingProxyType(
    {
        "success": 0x2ECC71,
        "warning": 0xF39C12,
        "error": 0xE74C3C,
        "disabled": 0x64748B,
    }
)


def resolve_semantic_color(
    accent_color: int,
    status: EmbedStatus = "normal",
    *,
    override_color: int | None = None,
) -> int:
    """Resolve an embed's 24-bit RGB color from the active theme accent and semantic status."""
    if override_color is not None:
        return max(0, min(0xFFFFFF, int(override_color)))
    st = str(status or "normal").strip().lower()
    if st in SEMANTIC_COLORS:
        return SEMANTIC_COLORS[st]
    return max(0, min(0xFFFFFF, int(accent_color)))


@dataclass(frozen=True)
class ThemePreset:
    """Branding theme preset defining color palettes, names, and copy."""

    name: str
    display_name: str
    accent_color: int
    footer_text: str
    logo_url: str
    stock_exchange_name: str
    currency_name: str
    currency_symbol: str
    primary_stock_symbol: str
    primary_stock_name: str
    source_attribution: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "accent_color": self.accent_color,
            "footer_text": self.footer_text,
            "logo_url": self.logo_url,
            "stock_exchange_name": self.stock_exchange_name,
            "currency_name": self.currency_name,
            "currency_symbol": self.currency_symbol,
            "primary_stock_symbol": self.primary_stock_symbol,
            "primary_stock_name": self.primary_stock_name,
            "source_attribution": self.source_attribution,
            "description": self.description,
        }


THEME_PRESETS: dict[str, ThemePreset] = {
    "instigate_cafe": ThemePreset(
        name="instigate_cafe",
        display_name="InstigateAI",
        accent_color=0xA64DFF,  # Vibrant purple
        footer_text="Instigate Cafe",
        logo_url="",
        stock_exchange_name="Instigate Cafe Stock Exchange",
        currency_name="IC Cash",
        currency_symbol="$",
        primary_stock_symbol="IC",
        primary_stock_name="Instigate Cafe",
        source_attribution="Internal Instigate Cafe Repository",
        description="Original Instigate Cafe community branding with signature purple accent.",
    ),
    "campus_generic": ThemePreset(
        name="campus_generic",
        display_name="Campus Assistant",
        accent_color=0x2563EB,  # Royal Blue (within relative luminance range heuristic)
        footer_text="Campus Utility",
        logo_url="",
        stock_exchange_name="Campus Stock Exchange",
        currency_name="Campus Coins",
        currency_symbol="$",
        primary_stock_symbol="CAMP",
        primary_stock_name="Campus Flagship",
        source_attribution="Campus Utility Bot",
        description="Neutral university theme with high-contrast royal blue accent.",
    ),
    "student_clean": ThemePreset(
        name="student_clean",
        display_name="Student Bot",
        accent_color=0x0284C7,  # Sky / Cyan Blue
        footer_text="Student Portal",
        logo_url="",
        stock_exchange_name="Student Exchange",
        currency_name="Credits",
        currency_symbol="$",
        primary_stock_symbol="STUD",
        primary_stock_name="Student Commons",
        source_attribution="Student Community Bot",
        description="Clean, modern student union theme with sky blue styling.",
    ),
    "toronto_tmu": ThemePreset(
        name="toronto_tmu",
        display_name="TMU Campus Bot",
        accent_color=0x004C97,  # TMU Official Blue
        footer_text="TMU Student Hub",
        logo_url="",
        stock_exchange_name="TMU Student Stock Exchange",
        currency_name="TMU Bucks",
        currency_symbol="$",
        primary_stock_symbol="TMU",
        primary_stock_name="Toronto Metropolitan University",
        source_attribution="TMU Student Community",
        description="Toronto Metropolitan University student hub preset.",
    ),
    "emerald_academic": ThemePreset(
        name="emerald_academic",
        display_name="Academic Bot",
        accent_color=0x059669,  # Academic Emerald Green
        footer_text="Academic Commons",
        logo_url="",
        stock_exchange_name="Academic Exchange",
        currency_name="Credits",
        currency_symbol="¢",
        primary_stock_symbol="ACAD",
        primary_stock_name="Academic Commons",
        source_attribution="Academic Commons Repository",
        description="Scholarly theme with rich academic emerald green accents.",
    ),
}

DEFAULT_THEME_NAME = "instigate_cafe"
KNOWN_THEME_NAMES: frozenset[str] = frozenset(THEME_PRESETS.keys())
THEME_PRESETS_VIEW: Mapping[str, ThemePreset] = MappingProxyType(THEME_PRESETS)


def get_theme_preset(name: str | None) -> ThemePreset:
    """Retrieve theme preset by name, defaulting safely to ``instigate_cafe``."""
    key = str(name or "").strip().lower()
    return THEME_PRESETS.get(key, THEME_PRESETS[DEFAULT_THEME_NAME])


def list_theme_presets() -> list[ThemePreset]:
    """Return all available theme presets ordered alphabetically."""
    return [THEME_PRESETS[k] for k in sorted(THEME_PRESETS.keys())]


def calculate_luminance(color_int: int) -> float:
    """Calculate relative luminance for a 24-bit RGB color per WCAG 2.1.

    Returns a value in [0.0, 1.0], where 0.0 is black and 1.0 is white.
    """
    c = max(0, min(0xFFFFFF, int(color_int)))
    r_val = ((c >> 16) & 0xFF) / 255.0
    g_val = ((c >> 8) & 0xFF) / 255.0
    b_val = (c & 0xFF) / 255.0

    def _lin(val: float) -> float:
        if val <= 0.04045:
            return val / 12.92
        return math.pow((val + 0.055) / 1.055, 2.4)

    return 0.2126 * _lin(r_val) + 0.7152 * _lin(g_val) + 0.0722 * _lin(b_val)


def is_accessible_accent(color_int: int) -> bool:
    """Check if an embed sidebar accent color falls within the relative luminance heuristic range.

    Discord dark theme background is approximately #2B2D31 (L ~ 0.03).
    Discord light theme background is #FFFFFF (L = 1.0).
    This check enforces a relative luminance heuristic bound (0.03 <= L <= 0.95) so
    embed sidebar accents avoid near-black or near-white extremes; it is not a
    measured WCAG AA text contrast ratio certification.
    """
    lum = calculate_luminance(color_int)
    return 0.03 <= lum <= 0.95
