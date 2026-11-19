from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

ProviderType = Literal["greenhouse", "lever", "workday"]
CareerLevel = Literal["intern", "new_grad", "entry", "experienced", "unknown"]


@dataclass
class SourceHealth:
    """Tracks operational health, cooldown, and error state for a single ATS source."""

    source_key: str
    provider: ProviderType
    target: str
    status: Literal["healthy", "cooling_down", "degraded", "blacklisted"] = "healthy"
    consecutive_failures: int = 0
    last_success_unix: int | None = None
    last_error: str | None = None
    last_status_code: int | None = None
    cooldown_until: float = 0.0

    def is_cooling_down(self, now: float | None = None) -> bool:
        cur = now if now is not None else time.time()
        return cur < self.cooldown_until

    def mark_success(self, now: float | None = None) -> None:
        cur = now if now is not None else time.time()
        self.status = "healthy"
        self.consecutive_failures = 0
        self.last_success_unix = int(cur)
        self.last_error = None
        self.last_status_code = 200
        self.cooldown_until = 0.0

    def mark_cooldown(
        self, seconds: float, *, status_code: int | None = None, error: str = "", now: float | None = None
    ) -> None:
        cur = now if now is not None else time.time()
        self.consecutive_failures += 1
        self.status = "cooling_down"
        self.cooldown_until = cur + max(1.0, float(seconds))
        self.last_status_code = status_code
        self.last_error = error or f"Cooldown {seconds}s"

    def mark_blacklisted(self, error: str = "", *, status_code: int | None = None) -> None:
        self.consecutive_failures += 1
        self.status = "blacklisted"
        self.last_status_code = status_code
        self.last_error = error or "Blacklisted due to permanent error"


@dataclass(frozen=True)
class JobPosting:
    """Normalized job posting across Greenhouse, Lever, and Workday."""

    provider: ProviderType
    employer: str
    external_id: str
    title: str
    career_level: CareerLevel = "unknown"
    discipline_tags: tuple[str, ...] = ()
    location: str = ""
    is_remote: bool = False
    url: str = ""
    posted_unix: int | None = None
    updated_unix: int | None = None
    fetched_unix: int = 0
    source_health: str = "healthy"
    text_blob: str = ""
    source_key: str = ""
    license: str = "ATS Public Listing"

    def __post_init__(self) -> None:
        # Default source_key to legacy format if not explicitly set
        if not self.source_key:
            if self.provider == "greenhouse":
                object.__setattr__(self, "source_key", f"greenhouse:{self.employer}")
            elif self.provider == "lever":
                object.__setattr__(self, "source_key", self.employer)
            elif self.provider == "workday":
                object.__setattr__(self, "source_key", f"workday:{self.employer}")
            else:
                object.__setattr__(self, "source_key", f"{self.provider}:{self.employer}")

    # Backwards compatibility properties matching legacy JobPosting and JobItem
    @property
    def source(self) -> str:
        return self.source_key

    @property
    def company(self) -> str:
        return self.employer

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        """Triple (provider, employer, external_id) for precise deduplication."""
        return (self.provider, self.employer, self.external_id)


@dataclass(frozen=True)
class MatchTraceItem:
    """Individual rule evaluation result in a match trace."""

    rule: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class MatchDecision:
    """Explainable verdict and trace returned by the rule engine."""

    matched: bool
    preset: str
    career_level: CareerLevel
    disciplines: tuple[str, ...]
    trace: tuple[MatchTraceItem, ...]
    explanation: str


@dataclass(frozen=True)
class JobFilterCriteria:
    """Filter criteria for matching candidate job postings."""

    location_allowlist: tuple[str, ...] = ()
    keywords_allowlist: tuple[str, ...] = ()
    keywords_denylist: tuple[str, ...] = ()
    require_early_career: bool = False
    disciplines: tuple[str, ...] = ()
    career_levels: tuple[CareerLevel, ...] = ()
    remote_only: bool = False
    preset: str = "tech_swe"
    employer_allowlist: tuple[str, ...] = ()
