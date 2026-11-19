from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class JobIngestionMetrics:
    """Structured metrics for job retrieval, matching, deduplication, and Discord alert dispatch."""

    source_success_count: int = 0
    source_latency_seconds: dict[str, float] = field(default_factory=dict)
    new_matching_jobs: int = 0
    suppressed_duplicates: int = 0
    http_failures: int = 0
    discord_delivery_successes: int = 0
    discord_delivery_failures: dict[str, int] = field(
        default_factory=lambda: {
            "rate_limited_429": 0,
            "missing_permissions": 0,
            "channel_not_found": 0,
            "transient": 0,
        }
    )
    outbox_pending_count: int = 0
    outbox_dead_letter_count: int = 0

    def record_source_success(self, source_key: str, latency: float) -> None:
        self.source_success_count += 1
        self.source_latency_seconds[source_key] = round(float(latency), 3)

    def record_http_failure(self, source_key: str, error: str) -> None:
        self.http_failures += 1
        log.warning("[JOB_METRICS] HTTP fetch failure on source %s: %s", source_key, error)

    def record_matching_jobs(self, count: int) -> None:
        self.new_matching_jobs += max(0, int(count))

    def record_suppressed_duplicates(self, count: int) -> None:
        self.suppressed_duplicates += max(0, int(count))

    def record_discord_delivery_success(self, count: int = 1) -> None:
        self.discord_delivery_successes += max(0, int(count))

    def record_discord_delivery_failure(self, reason: str, count: int = 1) -> None:
        key = reason if reason in self.discord_delivery_failures else "transient"
        self.discord_delivery_failures[key] = self.discord_delivery_failures.get(key, 0) + max(0, int(count))
        log.warning("[JOB_METRICS] Discord delivery failure [%s]: count=%d", key, count)

    def update_outbox_counts(self, pending: int, dead_letter: int) -> None:
        self.outbox_pending_count = max(0, int(pending))
        self.outbox_dead_letter_count = max(0, int(dead_letter))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_success_count": self.source_success_count,
            "source_latency_seconds": dict(self.source_latency_seconds),
            "new_matching_jobs": self.new_matching_jobs,
            "suppressed_duplicates": self.suppressed_duplicates,
            "http_failures": self.http_failures,
            "discord_delivery_successes": self.discord_delivery_successes,
            "discord_delivery_failures": dict(self.discord_delivery_failures),
            "outbox_pending_count": self.outbox_pending_count,
            "outbox_dead_letter_count": self.outbox_dead_letter_count,
        }
