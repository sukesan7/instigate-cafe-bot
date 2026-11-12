"""Unified background/event task supervision, capability gating, backoff, and DST-safe scheduling."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icbot.config import POLL_INTERVAL_BOUNDS, Settings
from icbot.errors import extract_http_error_info, format_exception_safe, redact_secrets

log = logging.getLogger(__name__)

TaskKind = Literal["periodic", "daily_clock", "event_driven"]
TaskState = Literal[
    "disabled",
    "waiting_ready",
    "sleeping",
    "running",
    "backing_off",
    "idle",
    "paused",
    "stopped",
]


def parse_hhmm(raw: str, default_h: int = 16, default_m: int = 30) -> tuple[int, int]:
    """Parse ``HH:MM`` or ``HHMM`` clock string into bounded ``(hour, minute)``."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return default_h, default_m
    try:
        if ":" in cleaned:
            h_raw, m_raw = cleaned.split(":", 1)
            hh, mm = int(h_raw), int(m_raw)
        elif len(cleaned) == 4 and cleaned.isdigit():
            hh, mm = int(cleaned[:2]), int(cleaned[2:])
        else:
            return default_h, default_m
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (TypeError, ValueError) as exc:
        log.debug("Invalid HHMM value %r: %s", raw, exc)
    return default_h, default_m


def _resolve_zone(tz_name: str) -> tzinfo:
    try:
        name = (tz_name or "").strip() or "UTC"
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return UTC


def _resolve_local_wall_time_utc(local_date: date, hh: int, mm: int, tz: tzinfo) -> datetime:
    """Resolve local calendar ``(date, hh, mm)`` in ``tz`` into a UTC ``datetime``.

    Handles PEP 495 DST transitions rigorously:
    - Fall-back ambiguous hour (``fold=0`` < ``fold=1`` in UTC): picks the first occurrence
      (``fold=0``) so scheduled tasks never wait an extra hour or fire twice on the repeated wall time.
    - Spring-forward nonexistent gap hour (``fold=1`` < ``fold=0`` in UTC, e.g. ``02:30`` when clocks
      jump ``01:59:59 -> 03:00:00``): binary-searches the transition second so execution fires at the
      exact first valid instant after the gap (``03:00:00``) rather than drifting into ``03:30``.
    """
    cand0 = datetime(local_date.year, local_date.month, local_date.day, hh, mm, tzinfo=tz, fold=0)
    cand1 = datetime(local_date.year, local_date.month, local_date.day, hh, mm, tzinfo=tz, fold=1)
    u0 = cand0.astimezone(UTC)
    u1 = cand1.astimezone(UTC)

    if u1 < u0:
        lo = int(u1.timestamp())
        hi = int(u0.timestamp()) + 1
        base_offset = datetime.fromtimestamp(lo, tz=tz).utcoffset()
        while lo < hi:
            mid = (lo + hi) // 2
            if datetime.fromtimestamp(mid, tz=tz).utcoffset() != base_offset:
                hi = mid
            else:
                lo = mid + 1
        return datetime.fromtimestamp(lo, tz=UTC)

    return u0


def next_local_run_timestamp(
    hh: int,
    mm: int,
    tz_name: str,
    *,
    now_unix: float | None = None,
) -> float:
    """Return the next UTC epoch timestamp matching local ``(hh, mm)`` strictly after ``now_unix``."""
    now_ts = float(time.time() if now_unix is None else now_unix)
    tz = _resolve_zone(tz_name)
    now_utc = datetime.fromtimestamp(now_ts, tz=UTC)
    local_date = now_utc.astimezone(tz).date()

    target_utc = _resolve_local_wall_time_utc(local_date, hh, mm, tz)
    if target_utc <= now_utc:
        target_utc = _resolve_local_wall_time_utc(local_date + timedelta(days=1), hh, mm, tz)
    return float(target_utc.timestamp())


def seconds_until_next_local_time(
    hh: int,
    mm: int,
    tz_name: str,
    *,
    now_unix: float | None = None,
    min_delay: float = 1.0,
) -> float:
    """Return DST-accurate seconds from ``now_unix`` until next local ``(hh, mm)`` in ``tz_name``."""
    now_ts = float(time.time() if now_unix is None else now_unix)
    target_ts = next_local_run_timestamp(hh, mm, tz_name, now_unix=now_ts)
    return max(float(min_delay), target_ts - now_ts)


async def invoke_poller_callable(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    raise_on_error: bool = True,
) -> Any:
    """Invoke a poller/handler passing ``raise_on_error`` only when accepted by its signature."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return await fn(*args)

    params = sig.parameters
    accepts_raise = "raise_on_error" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_raise:
        return await fn(*args, raise_on_error=raise_on_error)
    return await fn(*args)


@dataclass(frozen=True)
class BackoffPolicy:
    """Bounded exponential backoff policy honoring upstream ``Retry-After``."""

    base_seconds: float = 5.0
    factor: float = 2.0
    max_seconds: float = 1800.0

    def compute_delay(
        self,
        failures: int,
        *,
        retry_after: float | None = None,
        max_override: float | None = None,
    ) -> float:
        cap = max(1.0, float(max_override if max_override is not None else self.max_seconds))
        n = max(1, int(failures))
        exp = float(self.base_seconds) * (float(self.factor) ** (n - 1))
        delay = min(cap, max(0.0, exp))
        if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0.0:
            delay = min(cap, max(delay, float(retry_after)))
        return delay


@dataclass
class TaskHealth:
    """Redacted per-task operational health and schedule snapshot."""

    name: str
    kind: TaskKind
    capability: str
    enabled: bool = False
    state: TaskState = "disabled"
    reason: str = ""
    run_count: int = 0
    success_count: int = 0
    error_count: int = 0
    consecutive_failures: int = 0
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error_summary: str | None = None
    last_http_status: int | None = None
    last_retry_after: float | None = None
    next_run_at: float | None = None
    interval_seconds: int | None = None
    paused: bool = False

    def copy(self) -> TaskHealth:
        return TaskHealth(
            name=self.name,
            kind=self.kind,
            capability=self.capability,
            enabled=self.enabled,
            state=self.state,
            reason=self.reason,
            run_count=self.run_count,
            success_count=self.success_count,
            error_count=self.error_count,
            consecutive_failures=self.consecutive_failures,
            last_started_at=self.last_started_at,
            last_finished_at=self.last_finished_at,
            last_success_at=self.last_success_at,
            last_error_at=self.last_error_at,
            last_error_summary=self.last_error_summary,
            last_http_status=self.last_http_status,
            last_retry_after=self.last_retry_after,
            next_run_at=self.next_run_at,
            interval_seconds=self.interval_seconds,
            paused=self.paused,
        )

    def to_summary_line(self) -> str:
        badge = {
            "disabled": "⚪",
            "paused": "🛑",
            "waiting_ready": "⏳",
            "sleeping": "✅",
            "idle": "✅",
            "running": "🔄",
            "backing_off": "⚠️",
            "stopped": "⏹️",
        }.get(self.state, "•")
        extra = ""
        if self.state == "backing_off" and self.last_http_status is not None:
            extra = f" (HTTP {self.last_http_status}, failures={self.consecutive_failures})"
        elif self.error_count > 0 and self.consecutive_failures > 0:
            extra = f" (failures={self.consecutive_failures})"
        elif not self.enabled and self.reason:
            extra = f" ({self.reason[:60]})"
        return f"{badge} `{self.name}`: **{self.state}**{extra}"


@dataclass(frozen=True)
class TaskSpec:
    """Declarative specification for one supervised periodic, daily, or event-driven task."""

    name: str
    kind: TaskKind
    capability: str
    is_enabled: Callable[[Settings, bool], tuple[bool, str]]
    run_cycle: Callable[[], Awaitable[Any]] | None = None
    interval_seconds: Callable[[Settings], int] | None = None
    daily_schedule: Callable[[Settings], tuple[int, int, str]] | None = None
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)


def _jobs_channel(settings: Settings) -> int:
    if isinstance(settings, Settings):
        return int(settings.job_alerts_channel_id or 0)
    return int(getattr(settings, "job_alerts_channel_id", 0) or getattr(settings, "jobs_channel_id", 0) or 0)


def _go_channel(settings: Settings) -> int:
    return int(
        getattr(settings, "go_alerts_channel_id", 0) or getattr(settings, "ttc_alerts_channel_id", 0) or 0
    )


def _mw_channel(settings: Settings) -> int:
    return int(
        getattr(settings, "market_watch_channel_id", 0) or getattr(settings, "stock_channel_id", 0) or 0
    )


def _dr_channel(settings: Settings) -> int:
    return int(
        getattr(settings, "market_daily_report_channel_id", 0)
        or getattr(settings, "market_watch_channel_id", 0)
        or getattr(settings, "stock_channel_id", 0)
        or 0
    )


def _btc_channel(settings: Settings) -> int:
    return int(
        getattr(settings, "btc_watch_channel_id", 0)
        or getattr(settings, "market_watch_channel_id", 0)
        or getattr(settings, "stock_channel_id", 0)
        or 0
    )


def build_default_task_specs(
    *,
    run_ttc: Callable[[], Awaitable[Any]],
    run_go: Callable[[], Awaitable[Any]],
    run_jobs: Callable[[], Awaitable[Any]],
    run_workday: Callable[[], Awaitable[Any]],
    run_market_watch: Callable[[], Awaitable[Any]],
    run_btc_watch: Callable[[], Awaitable[Any]],
    run_daily_report: Callable[[], Awaitable[Any]],
) -> tuple[TaskSpec, ...]:
    """Build the canonical registry of all 8 background/triggered bot tasks."""

    def _ttc_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not int(getattr(s, "ttc_alerts_channel_id", 0) or 0):
            return False, "TTC_ALERTS_CHANNEL_ID not configured"
        if not (getattr(s, "ttc_mastodon_base", "") or "").strip():
            return False, "Missing TTC_MASTODON_BASE"
        if not (getattr(s, "ttc_mastodon_acct", "") or "").strip():
            return False, "Missing TTC_MASTODON_ACCT"
        if int(getattr(s, "ttc_poll_seconds", 0) or 0) <= 0:
            return False, "TTC_POLL_SECONDS disabled"
        return True, ""

    def _go_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not _go_channel(s):
            return False, "GO alert channel not configured"
        if not (getattr(s, "go_api_key", "") or "").strip():
            return False, "Missing GO_API_KEY"
        if int(getattr(s, "go_poll_seconds", 0) or 0) <= 0:
            return False, "GO_POLL_SECONDS disabled"
        return True, ""

    def _jobs_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not _jobs_channel(s):
            return False, "JOB_ALERTS_CHANNEL_ID not configured"
        if int(getattr(s, "jobs_poll_seconds", 0) or 0) <= 0:
            return False, "JOBS_POLL_SECONDS=0"
        gh = (
            getattr(s, "greenhouse_boards", None)
            if isinstance(s, Settings)
            else (getattr(s, "greenhouse_boards", None) or getattr(s, "greenhouse_board_tokens", None))
        )
        lv = (
            getattr(s, "lever_sites", None)
            if isinstance(s, Settings)
            else (getattr(s, "lever_sites", None) or getattr(s, "lever_site_tokens", None))
        )
        if not (gh or lv):
            return False, "No Greenhouse or Lever boards configured"
        return True, ""

    def _workday_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not _jobs_channel(s):
            return False, "JOB_ALERTS_CHANNEL_ID not configured"
        wd = getattr(s, "workday_sites", None) or []
        if not wd:
            return False, "No WORKDAY_SITES configured"
        eff_poll = int(getattr(s, "workday_poll_seconds", 0) or 0) or int(
            getattr(s, "jobs_poll_seconds", 0) or 0
        )
        if eff_poll <= 0:
            return False, "Workday polling disabled (interval 0)"
        return True, ""

    def _market_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not bool(getattr(s, "market_watch_enabled", False)):
            return False, "MARKET_WATCH_ENABLED=false"
        if not _mw_channel(s):
            return False, "Market watch channel not configured"
        if int(getattr(s, "market_watch_poll_seconds", 0) or 0) <= 0:
            return False, "MARKET_WATCH_POLL_SECONDS disabled"
        return True, ""

    def _btc_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not bool(getattr(s, "btc_watch_enabled", False)):
            return False, "BTC_WATCH_ENABLED=false"
        if not _btc_channel(s):
            return False, "BTC watch channel not configured"
        if not (getattr(s, "btc_watch_addresses", None) or []):
            return False, "No BTC_WATCH_ADDRESSES configured"
        if int(getattr(s, "btc_watch_poll_seconds", 0) or 0) <= 0:
            return False, "BTC_WATCH_POLL_SECONDS disabled"
        return True, ""

    def _daily_enabled(s: Settings, _killed: bool) -> tuple[bool, str]:
        if not bool(getattr(s, "market_daily_report_enabled", False)):
            return False, "MARKET_DAILY_REPORT_ENABLED=false"
        if not _dr_channel(s):
            return False, "Daily market report channel not configured"
        return True, ""

    def _llm_enabled(s: Settings, llm_killed: bool) -> tuple[bool, str]:
        if not bool(getattr(s, "llm_enabled", False)):
            return False, "LLM_ENABLED=false"
        if not (getattr(s, "gemini_api_key", "") or "").strip():
            return False, "Missing GEMINI_API_KEY"
        if llm_killed:
            return False, "Paused via /ic admin llm_stop"
        return True, ""

    return (
        TaskSpec(
            name="ttc",
            kind="periodic",
            capability="ttc_alerts",
            is_enabled=_ttc_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["ttc_poll_seconds"].clamp(
                int(getattr(s, "ttc_poll_seconds", 120) or 120)
            ),
            run_cycle=run_ttc,
        ),
        TaskSpec(
            name="go",
            kind="periodic",
            capability="go_alerts",
            is_enabled=_go_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["go_poll_seconds"].clamp(
                int(getattr(s, "go_poll_seconds", 180) or 180)
            ),
            run_cycle=run_go,
        ),
        TaskSpec(
            name="jobs",
            kind="periodic",
            capability="job_alerts",
            is_enabled=_jobs_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["jobs_poll_seconds"].clamp(
                int(getattr(s, "jobs_poll_seconds", 600) or 0)
            ),
            run_cycle=run_jobs,
        ),
        TaskSpec(
            name="workday",
            kind="periodic",
            capability="job_alerts",
            is_enabled=_workday_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["workday_poll_seconds"].clamp(
                int(getattr(s, "workday_poll_seconds", 0) or 0),
                inherited=int(getattr(s, "jobs_poll_seconds", 0) or 0),
            ),
            run_cycle=run_workday,
        ),
        TaskSpec(
            name="market_watch",
            kind="periodic",
            capability="market_watch",
            is_enabled=_market_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["market_watch_poll_seconds"].clamp(
                int(getattr(s, "market_watch_poll_seconds", 60) or 60)
            ),
            run_cycle=run_market_watch,
        ),
        TaskSpec(
            name="daily_market_report",
            kind="daily_clock",
            capability="market_daily_report",
            is_enabled=_daily_enabled,
            daily_schedule=lambda s: (
                *parse_hhmm(str(getattr(s, "market_daily_report_time", "16:30") or "16:30")),
                str(getattr(s, "market_daily_report_tz", "America/Toronto") or "America/Toronto"),
            ),
            run_cycle=run_daily_report,
        ),
        TaskSpec(
            name="btc_watch",
            kind="periodic",
            capability="btc_watch",
            is_enabled=_btc_enabled,
            interval_seconds=lambda s: POLL_INTERVAL_BOUNDS["btc_watch_poll_seconds"].clamp(
                int(getattr(s, "btc_watch_poll_seconds", 300) or 300)
            ),
            run_cycle=run_btc_watch,
        ),
        TaskSpec(
            name="llm_assistant",
            kind="event_driven",
            capability="llm_assistant",
            is_enabled=_llm_enabled,
        ),
    )


class TaskSupervisor:
    """Single lifecycle supervisor managing periodic, daily-clock, and event-driven bot tasks."""

    def __init__(
        self,
        *,
        settings_getter: Callable[[], Settings],
        is_closed: Callable[[], bool],
        wait_until_ready: Callable[[], Awaitable[None]],
        secrets_getter: Callable[[], Iterable[str]] | None = None,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        jitter_rng: random.Random | None = None,
        llm_killed_getter: Callable[[], bool] | None = None,
    ) -> None:
        self._settings_getter = settings_getter
        self._is_closed = is_closed
        self._wait_until_ready = wait_until_ready
        self._secrets_getter = secrets_getter or (lambda: ())
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._sleep_fn = sleep_fn
        self._rng = jitter_rng if jitter_rng is not None else random.Random()
        self._llm_killed_getter = llm_killed_getter or (lambda: False)

        self._specs: dict[str, TaskSpec] = {}
        self._health: dict[str, TaskHealth] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._draining_tasks: set[asyncio.Task[None]] = set()
        self._schedule_sigs: dict[str, Any] = {}
        self._started: bool = False
        self._override_settings: Settings | None = None

    def _secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets_getter())

    def _current_settings(self) -> Settings:
        if self._override_settings is not None:
            return self._override_settings
        return self._settings_getter()

    async def _sleep(self, delay: float) -> None:
        if self._sleep_fn is not None:
            await self._sleep_fn(delay)
        else:
            await asyncio.sleep(delay)

    def register(self, spec: TaskSpec) -> None:
        self._specs[spec.name] = spec
        if spec.name not in self._health:
            self._health[spec.name] = TaskHealth(
                name=spec.name,
                kind=spec.kind,
                capability=spec.capability,
            )

    def register_many(self, specs: Iterable[TaskSpec]) -> None:
        for spec in specs:
            self.register(spec)

    @property
    def active_tasks(self) -> dict[str, asyncio.Task[None]]:
        return dict(self._tasks)

    def health_snapshot(self) -> dict[str, TaskHealth]:
        """Return redacted copies of all registered task health records."""
        self.sync_event_states()
        return {name: h.copy() for name, h in self._health.items()}

    def sync_event_states(self, settings: Settings | None = None) -> None:
        """Refresh enablement/pause status for event-driven tasks without mutating running loops."""
        if settings is not None:
            self._override_settings = settings
        eff = self._current_settings()
        llm_killed = bool(self._llm_killed_getter())
        secrets = self._secrets()

        for spec in self._specs.values():
            if spec.kind != "event_driven":
                continue
            h = self._health[spec.name]
            enabled, reason = spec.is_enabled(eff, llm_killed)
            h.enabled = enabled
            h.reason = redact_secrets(reason, secrets)
            configured_llm = bool(
                getattr(eff, "llm_enabled", False) and (getattr(eff, "gemini_api_key", "") or "").strip()
            )
            h.paused = bool(not enabled and llm_killed and configured_llm)
            if not enabled:
                h.state = "paused" if h.paused else "disabled"
            elif h.state in ("disabled", "paused", "stopped"):
                h.state = "idle"

    def record_event_start(self, name: str) -> None:
        h = self._health.get(name)
        if h is None:
            return
        h.state = "running"
        h.last_started_at = self._clock()
        h.run_count += 1

    def record_event_success(self, name: str) -> None:
        h = self._health.get(name)
        if h is None:
            return
        now = self._clock()
        h.last_finished_at = now
        h.last_success_at = now
        h.success_count += 1
        h.consecutive_failures = 0
        h.last_error_summary = None
        h.last_http_status = None
        h.last_retry_after = None
        h.state = "idle" if h.enabled else ("paused" if h.paused else "disabled")

    def record_event_error(self, name: str, exc: BaseException) -> None:
        h = self._health.get(name)
        if h is None:
            return
        now = self._clock()
        status, retry_after = extract_http_error_info(exc, now=now)
        h.last_finished_at = now
        h.last_error_at = now
        h.error_count += 1
        h.consecutive_failures += 1
        h.last_http_status = status
        h.last_retry_after = retry_after
        h.last_error_summary = format_exception_safe(exc, self._secrets(), max_len=240)
        h.state = "idle" if h.enabled else ("paused" if h.paused else "disabled")

    def start(self, settings: Settings | None = None) -> dict[str, asyncio.Task[None]]:
        """Start all enabled background loop tasks idempotently."""
        self._started = True
        return self.reconcile(settings)

    def _spawn_loop_task(self, spec: TaskSpec) -> asyncio.Task[None]:
        existing = self._tasks.get(spec.name)
        if existing is not None and not existing.done():
            return existing

        h = self._health[spec.name]
        h.state = "waiting_ready"
        coro = (
            self._run_daily_clock_loop(spec) if spec.kind == "daily_clock" else self._run_periodic_loop(spec)
        )
        task = asyncio.create_task(coro, name=f"icbot:{spec.name}")
        self._tasks[spec.name] = task

        def _on_done(finished: asyncio.Task[None]) -> None:
            self._draining_tasks.discard(finished)
            if self._tasks.get(spec.name) is finished:
                self._tasks.pop(spec.name, None)
            if finished.cancelled():
                if h.state not in ("disabled", "paused"):
                    h.state = "stopped"
                h.next_run_at = None
                return
            exc = finished.exception()
            if exc is not None:
                now = self._clock()
                status, retry_after = extract_http_error_info(exc, now=now)
                h.last_error_at = now
                h.error_count += 1
                h.consecutive_failures += 1
                h.last_http_status = status
                h.last_retry_after = retry_after
                h.last_error_summary = format_exception_safe(exc, self._secrets(), max_len=240)
                h.state = "stopped"
                h.next_run_at = None
                log.error(
                    "Supervised task %s terminated unexpectedly: %s",
                    spec.name,
                    h.last_error_summary,
                )
            else:
                if h.state not in ("disabled", "paused"):
                    h.state = "stopped"
                h.next_run_at = None

        task.add_done_callback(_on_done)
        return task

    def _cancel_task_nowait(self, name: str) -> asyncio.Task[None] | None:
        task = self._tasks.pop(name, None)
        h = self._health.get(name)
        if h is not None:
            h.next_run_at = None
            h.state = "disabled"
        if task is not None and not task.done():
            self._draining_tasks.add(task)
            task.cancel()
            return task
        return None

    def reconcile(self, settings: Settings | None = None) -> dict[str, asyncio.Task[None]]:
        """Reconcile active tasks against current settings and capability guards."""
        if settings is not None:
            self._override_settings = settings
        eff = self._current_settings()
        llm_killed = bool(self._llm_killed_getter())
        secrets = self._secrets()

        self.sync_event_states(eff)

        for spec in self._specs.values():
            if spec.kind == "event_driven":
                continue

            h = self._health[spec.name]
            enabled, reason = spec.is_enabled(eff, llm_killed)
            h.enabled = enabled
            h.reason = redact_secrets(reason, secrets)
            h.interval_seconds = spec.interval_seconds(eff) if (enabled and spec.interval_seconds) else None

            if not self._started or self._is_closed():
                h.state = "stopped" if enabled else "disabled"
                h.next_run_at = None
                continue

            cur_sig: Any = None
            if enabled:
                if spec.kind == "daily_clock" and spec.daily_schedule is not None:
                    cur_sig = ("daily", spec.daily_schedule(eff))
                elif spec.interval_seconds is not None:
                    cur_sig = ("periodic", h.interval_seconds)

            existing = self._tasks.get(spec.name)
            if enabled:
                prev_sig = self._schedule_sigs.get(spec.name)
                if (
                    existing is not None
                    and not existing.done()
                    and prev_sig is not None
                    and prev_sig != cur_sig
                    and h.state == "sleeping"
                ):
                    self._cancel_task_nowait(spec.name)
                    existing = None
                self._schedule_sigs[spec.name] = cur_sig
                if existing is None or existing.done():
                    self._spawn_loop_task(spec)
            else:
                self._schedule_sigs.pop(spec.name, None)
                self._cancel_task_nowait(spec.name)

        return dict(self._tasks)

    async def reconcile_async(self, settings: Settings | None = None) -> dict[str, asyncio.Task[None]]:
        """Reconcile against settings and await cancellation of any disabled tasks."""
        active = self.reconcile(settings)
        await self._drain_cancelled()
        return active

    async def _drain_cancelled(self) -> None:
        draining = [t for t in self._draining_tasks if not t.done()]
        if draining:
            await asyncio.gather(*draining, return_exceptions=True)
        self._draining_tasks.clear()

    async def stop_all(self) -> None:
        """Cancel and await all running and draining supervised tasks cleanly."""
        self._started = False
        tasks = list(self._tasks.values()) + list(self._draining_tasks)
        self._tasks.clear()
        self._draining_tasks.clear()
        for name, h in self._health.items():
            if h.enabled:
                h.state = "stopped"
            h.next_run_at = None
            _ = name
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_periodic_loop(self, spec: TaskSpec) -> None:
        h = self._health[spec.name]
        h.state = "waiting_ready"
        await self._wait_until_ready()
        if self._is_closed():
            return

        eff = self._current_settings()
        enabled, reason = spec.is_enabled(eff, self._llm_killed_getter())
        if not enabled:
            h.enabled = False
            h.reason = redact_secrets(reason, self._secrets())
            h.state = "disabled"
            return

        jitter_max = max(0.0, float(getattr(eff, "task_startup_jitter_seconds", 15.0) or 0.0))
        if jitter_max > 0.0:
            jitter = self._rng.uniform(0.0, jitter_max)
            if jitter > 0.0:
                h.state = "sleeping"
                h.next_run_at = self._clock() + jitter
                await self._sleep(jitter)

        while not self._is_closed():
            eff = self._current_settings()
            enabled, reason = spec.is_enabled(eff, self._llm_killed_getter())
            if not enabled:
                h.enabled = False
                h.reason = redact_secrets(reason, self._secrets())
                h.state = "disabled"
                h.next_run_at = None
                break

            h.state = "running"
            h.last_started_at = self._clock()
            h.run_count += 1
            delay: float
            try:
                assert spec.run_cycle is not None
                await spec.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor isolates any cycle error for backoff
                now = self._clock()
                status, retry_after = extract_http_error_info(exc, now=now)
                h.last_finished_at = now
                h.last_error_at = now
                h.error_count += 1
                h.consecutive_failures += 1
                h.last_http_status = status
                h.last_retry_after = retry_after
                h.last_error_summary = format_exception_safe(exc, self._secrets(), max_len=240)
                eff = self._current_settings()
                max_backoff = float(
                    getattr(eff, "task_max_backoff_seconds", spec.backoff.max_seconds)
                    or spec.backoff.max_seconds
                )
                delay = spec.backoff.compute_delay(
                    h.consecutive_failures,
                    retry_after=retry_after,
                    max_override=max_backoff,
                )
                h.state = "backing_off"
                log.warning(
                    "[%s] task error (failures=%d retry_in=%.1fs status=%s): %s",
                    spec.name.upper(),
                    h.consecutive_failures,
                    delay,
                    status,
                    h.last_error_summary,
                )
            else:
                now = self._clock()
                h.last_finished_at = now
                h.last_success_at = now
                h.success_count += 1
                h.consecutive_failures = 0
                h.last_error_summary = None
                h.last_http_status = None
                h.last_retry_after = None
                eff = self._current_settings()
                interval = spec.interval_seconds(eff) if spec.interval_seconds is not None else 120
                h.interval_seconds = interval
                if interval <= 0:
                    h.enabled = False
                    h.state = "disabled"
                    h.next_run_at = None
                    break
                delay = float(interval)
                h.state = "sleeping"

            if self._is_closed():
                break
            h.next_run_at = self._clock() + delay
            await self._sleep(delay)

    async def _run_daily_clock_loop(self, spec: TaskSpec) -> None:
        h = self._health[spec.name]
        h.state = "waiting_ready"
        await self._wait_until_ready()
        in_backoff = False
        last_target_ts: float | None = None

        while not self._is_closed():
            eff = self._current_settings()
            enabled, reason = spec.is_enabled(eff, self._llm_killed_getter())
            if not enabled:
                h.enabled = False
                h.reason = redact_secrets(reason, self._secrets())
                h.state = "disabled"
                h.next_run_at = None
                break

            if not in_backoff:
                assert spec.daily_schedule is not None
                hh, mm, tz_name = spec.daily_schedule(eff)
                now = self._clock()
                ref_now = max(now, last_target_ts) if last_target_ts is not None else now
                target_ts = next_local_run_timestamp(hh, mm, tz_name, now_unix=ref_now)
                wait_s = max(0.0, target_ts - now)
                h.state = "sleeping"
                h.next_run_at = target_ts
                await self._sleep(wait_s)
                if self._is_closed():
                    break
                eff = self._current_settings()
                enabled, reason = spec.is_enabled(eff, self._llm_killed_getter())
                if not enabled:
                    h.enabled = False
                    h.reason = redact_secrets(reason, self._secrets())
                    h.state = "disabled"
                    h.next_run_at = None
                    break
                last_target_ts = target_ts

            h.state = "running"
            h.last_started_at = self._clock()
            h.run_count += 1
            try:
                assert spec.run_cycle is not None
                await spec.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor isolates any daily cycle error for backoff
                last_target_ts = None
                now = self._clock()
                status, retry_after = extract_http_error_info(exc, now=now)
                h.last_finished_at = now
                h.last_error_at = now
                h.error_count += 1
                h.consecutive_failures += 1
                h.last_http_status = status
                h.last_retry_after = retry_after
                h.last_error_summary = format_exception_safe(exc, self._secrets(), max_len=240)
                eff = self._current_settings()
                max_backoff = float(
                    getattr(eff, "task_max_backoff_seconds", spec.backoff.max_seconds)
                    or spec.backoff.max_seconds
                )
                delay = spec.backoff.compute_delay(
                    h.consecutive_failures,
                    retry_after=retry_after,
                    max_override=max_backoff,
                )
                h.state = "backing_off"
                h.next_run_at = now + delay
                in_backoff = True
                log.warning(
                    "[%s] daily task error (failures=%d retry_in=%.1fs status=%s): %s",
                    spec.name.upper(),
                    h.consecutive_failures,
                    delay,
                    status,
                    h.last_error_summary,
                )
                if self._is_closed():
                    break
                await self._sleep(delay)
            else:
                now = self._clock()
                h.last_finished_at = now
                h.last_success_at = now
                h.success_count += 1
                h.consecutive_failures = 0
                h.last_error_summary = None
                h.last_http_status = None
                h.last_retry_after = None
                in_backoff = False
