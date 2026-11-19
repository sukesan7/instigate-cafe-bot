from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp

from icbot.errors import ProviderError, parse_retry_after, redact_secrets

log = logging.getLogger(__name__)

# Supported contemporary Google Gemini API models (REST Developer API)
SUPPORTED_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-3.1-flash-lite-preview",
)

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    }
)

DEFAULT_MAX_PROMPT_CHARS = 100_000
DEFAULT_MAX_SYSTEM_INSTRUCTION_CHARS = 20_000
DEFAULT_MAX_ATTACHMENTS = 5
DEFAULT_MAX_IMAGE_BYTES = 5_000_000  # 5 MB per attachment
DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES = 10_000_000  # 10 MB total
DEFAULT_MAX_CONCURRENCY = 5


class ModelRole(StrEnum):
    """Role-specific model tiers for workload optimization."""

    DEFAULT = "default"
    CHAT = "chat"
    ASSISTANT = "assistant"
    FAST = "fast"
    RESUME = "resume"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ModelPricing:
    """Pricing matrix per 1,000,000 tokens (in USD)."""

    input_cost_per_million: float
    output_cost_per_million: float


# Baseline pricing matrix for operator cost estimation
DEFAULT_PRICING: dict[str, ModelPricing] = {
    "gemini-2.5-flash": ModelPricing(input_cost_per_million=0.30, output_cost_per_million=2.50),
    "gemini-2.5-pro": ModelPricing(input_cost_per_million=1.25, output_cost_per_million=10.00),
    "gemini-2.0-flash": ModelPricing(input_cost_per_million=0.10, output_cost_per_million=0.40),
    "gemini-2.0-flash-lite": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    "gemini-1.5-flash": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    "gemini-1.5-pro": ModelPricing(input_cost_per_million=1.25, output_cost_per_million=5.00),
    "gemini-3.1-flash-lite-preview": ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
}
FALLBACK_PRICING = ModelPricing(input_cost_per_million=0.30, output_cost_per_million=2.50)


# --- Exceptions Hierarchy ---


class GeminiError(ProviderError):
    """Base exception for Gemini Developer API failures."""

    def __init__(
        self,
        message: str = "",
        *,
        status: int = 0,
        details: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status, retry_after=retry_after)
        self.message = message
        self.status = status
        self.details = details
        self.retry_after = retry_after

    def __str__(self) -> str:
        base = f"{type(self).__name__}(status={self.status}): {self.message}"
        return f"{base} :: {self.details}" if self.details else base


class GeminiMisconfigurationError(GeminiError):
    """Raised when API key or model configuration is missing or invalid (HTTP 400 invalid key, 401, 403)."""


class GeminiRateLimitError(GeminiError):
    """Raised when upstream rate limits or quota are exhausted (HTTP 429)."""


class GeminiTransientError(GeminiError):
    """Raised for transient upstream server outages or network failures (HTTP 500, 502, 503, 504, client errors)."""


class GeminiSafetyBlockError(GeminiError):
    """Raised when prompt or response is blocked by safety or moderation filters."""


class GeminiUnsupportedContentError(GeminiError):
    """Raised when prompt size, attachment size, or attachment MIME type is invalid/unsupported."""


class GeminiBudgetExceededError(GeminiError):
    """Raised when a user, guild, or global token/cost budget is exceeded."""


class GeminiInvalidResponseError(GeminiError):
    """Raised when upstream response is malformed, missing candidates, or unparseable."""


# --- Governance & Budget Tracking ---


@dataclass
class UsageSnapshot:
    """Historical token and cost usage snapshot."""

    timestamp: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    user_id: int | None = None
    guild_id: int | None = None
    model: str = ""


class AIBudgetGovernor:
    """
    Per-guild, per-user, and global token/cost rate-limiting and cost guardrails.
    Never stores raw prompt text, resumes, or secret keys.
    """

    def __init__(
        self,
        *,
        daily_cost_limit_usd: float = 10.0,
        weekly_cost_limit_usd: float = 50.0,
        user_requests_per_minute: int = 15,
        user_requests_per_day: int = 150,
        guild_requests_per_day: int = 1000,
        pricing_matrix: dict[str, ModelPricing] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.daily_cost_limit_usd = float(daily_cost_limit_usd)
        self.weekly_cost_limit_usd = float(weekly_cost_limit_usd)
        self.user_requests_per_minute = int(user_requests_per_minute)
        self.user_requests_per_day = int(user_requests_per_day)
        self.guild_requests_per_day = int(guild_requests_per_day)
        self.pricing_matrix = dict(pricing_matrix or DEFAULT_PRICING)
        self._clock = clock if clock is not None else time.time

        self._user_request_timestamps: dict[int, deque[float]] = {}
        self._guild_request_timestamps: dict[int, deque[float]] = {}
        self._usage_history: deque[UsageSnapshot] = deque(maxlen=10_000)

        # Cumulative totals
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.rate_limit_hits: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cost_usd: float = 0.0

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = self.pricing_matrix.get(model, FALLBACK_PRICING)
        in_cost = (max(0, input_tokens) / 1_000_000.0) * pricing.input_cost_per_million
        out_cost = (max(0, output_tokens) / 1_000_000.0) * pricing.output_cost_per_million
        return in_cost + out_cost

    def _prune_old_records(self, now: float) -> None:
        one_week_ago = now - 7 * 86400
        while self._usage_history and self._usage_history[0].timestamp < one_week_ago:
            self._usage_history.popleft()

    def get_recent_cost(self, window_seconds: float) -> float:
        now = self._clock()
        threshold = now - window_seconds
        return sum(s.cost_usd for s in self._usage_history if s.timestamp >= threshold)

    def check_budget(self, user_id: int | None = None, guild_id: int | None = None) -> None:
        now = self._clock()
        self._prune_old_records(now)

        # 1. Global cost bounds
        daily_cost = self.get_recent_cost(86400)
        if daily_cost >= self.daily_cost_limit_usd:
            raise GeminiBudgetExceededError(
                f"Daily AI cost budget exhausted (${daily_cost:.2f} >= ${self.daily_cost_limit_usd:.2f})",
                status=429,
            )

        weekly_cost = self.get_recent_cost(7 * 86400)
        if weekly_cost >= self.weekly_cost_limit_usd:
            raise GeminiBudgetExceededError(
                f"Weekly AI cost budget exhausted (${weekly_cost:.2f} >= ${self.weekly_cost_limit_usd:.2f})",
                status=429,
            )

        # 2. Per-user rate limits
        if user_id is not None:
            ts_q = self._user_request_timestamps.setdefault(user_id, deque())
            # Prune older than 1 day
            while ts_q and ts_q[0] < now - 86400:
                ts_q.popleft()

            # Check 1 minute bound
            minute_reqs = sum(1 for t in ts_q if t >= now - 60)
            if minute_reqs >= self.user_requests_per_minute:
                raise GeminiBudgetExceededError(
                    f"User AI rate limit exceeded ({minute_reqs}/{self.user_requests_per_minute} req/min)",
                    status=429,
                )

            # Check 1 day bound
            if len(ts_q) >= self.user_requests_per_day:
                raise GeminiBudgetExceededError(
                    f"User daily AI request quota exhausted ({len(ts_q)}/{self.user_requests_per_day})",
                    status=429,
                )

        # 3. Per-guild rate limits
        if guild_id is not None:
            g_q = self._guild_request_timestamps.setdefault(guild_id, deque())
            while g_q and g_q[0] < now - 86400:
                g_q.popleft()
            if len(g_q) >= self.guild_requests_per_day:
                raise GeminiBudgetExceededError(
                    f"Guild daily AI request quota exhausted ({len(g_q)}/{self.guild_requests_per_day})",
                    status=429,
                )

    def record_request_start(self, user_id: int | None = None, guild_id: int | None = None) -> None:
        self.total_requests += 1
        now = self._clock()
        if user_id is not None:
            self._user_request_timestamps.setdefault(user_id, deque()).append(now)
        if guild_id is not None:
            self._guild_request_timestamps.setdefault(guild_id, deque()).append(now)

    def record_usage(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        user_id: int | None = None,
        guild_id: int | None = None,
    ) -> float:
        now = self._clock()
        cost = self.calculate_cost(model, input_tokens, output_tokens)

        self.successful_requests += 1
        self.total_input_tokens += max(0, input_tokens)
        self.total_output_tokens += max(0, output_tokens)
        self.total_cost_usd += cost

        self._usage_history.append(
            UsageSnapshot(
                timestamp=now,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                user_id=user_id,
                guild_id=guild_id,
                model=model,
            )
        )
        return cost

    def record_failure(self, status: int = 0) -> None:
        self.failed_requests += 1
        if status == 429:
            self.rate_limit_hits += 1

    def get_diagnostics(self) -> dict[str, Any]:
        """Return diagnostic metrics without exposing user content or keys."""
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "rate_limit_hits": self.rate_limit_hits,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "daily_cost_usd": round(self.get_recent_cost(86400), 4),
            "daily_cost_limit_usd": self.daily_cost_limit_usd,
            "weekly_cost_usd": round(self.get_recent_cost(7 * 86400), 4),
            "weekly_cost_limit_usd": self.weekly_cost_limit_usd,
            "active_users_tracked": len(self._user_request_timestamps),
            "active_guilds_tracked": len(self._guild_request_timestamps),
        }


# --- Provider Client ---


class GeminiClient:
    """
    Production-hardened Gemini Developer REST API client.
    Handles payload validation, role-specific model mapping, bounded retries,
    concurrency control, quota management, and secret-safe error handling.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        default_model: str = "gemini-2.5-flash",
        resume_model: str = "gemini-2.5-pro",
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_retries: int = 2,
        initial_backoff: float = 1.0,
        max_backoff: float = 10.0,
        governor: AIBudgetGovernor | None = None,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.default_model = (default_model or "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        self.resume_model = (resume_model or "gemini-2.5-pro").strip() or "gemini-2.5-pro"
        self.max_retries = max(0, int(max_retries))
        self.initial_backoff = max(0.1, float(initial_backoff))
        self.max_backoff = max(self.initial_backoff, float(max_backoff))
        self._concurrency_limit = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self._concurrency_limit)
        self.governor = governor if governor is not None else AIBudgetGovernor(clock=clock)
        self._clock = clock if clock is not None else time.time
        self._sleep_fn = sleep_fn if sleep_fn is not None else asyncio.sleep

    @property
    def is_available(self) -> bool:
        """Returns True if an API key is configured."""
        return bool(self.api_key)

    def resolve_model(
        self,
        model: str | None = None,
        role: ModelRole | str | None = None,
    ) -> str:
        """Resolve the target model name from explicit string or role tier."""
        if model and str(model).strip():
            return str(model).strip()

        if role is not None:
            r = role.value if isinstance(role, ModelRole) else str(role).lower().strip()
            if r in (ModelRole.RESUME.value, ModelRole.HEAVY.value):
                return self.resume_model
            if r in (ModelRole.FAST.value,):
                return "gemini-2.5-flash"
            if r in (ModelRole.CHAT.value, ModelRole.ASSISTANT.value, ModelRole.DEFAULT.value):
                return self.default_model

        return self.default_model

    def validate_request_payload(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        inline_images: Sequence[tuple[str, bytes]] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[str, str | None, list[tuple[str, bytes]], float | None, int | None]:
        """Validate payload boundaries and sanitize options against DoS/format limits."""
        if not isinstance(prompt, str):
            raise GeminiUnsupportedContentError("Prompt must be a string", status=400)
        if len(prompt) > DEFAULT_MAX_PROMPT_CHARS:
            raise GeminiUnsupportedContentError(
                f"Prompt length ({len(prompt)} chars) exceeds limit of {DEFAULT_MAX_PROMPT_CHARS}",
                status=400,
            )

        clean_sys: str | None = None
        if system_instruction is not None:
            clean_sys = str(system_instruction).strip()
            if len(clean_sys) > DEFAULT_MAX_SYSTEM_INSTRUCTION_CHARS:
                raise GeminiUnsupportedContentError(
                    f"System instruction ({len(clean_sys)} chars) exceeds limit of {DEFAULT_MAX_SYSTEM_INSTRUCTION_CHARS}",
                    status=400,
                )

        validated_images: list[tuple[str, bytes]] = []
        if inline_images:
            if len(inline_images) > DEFAULT_MAX_ATTACHMENTS:
                raise GeminiUnsupportedContentError(
                    f"Attachment count ({len(inline_images)}) exceeds limit of {DEFAULT_MAX_ATTACHMENTS}",
                    status=400,
                )
            total_bytes = 0
            for idx, item in enumerate(inline_images):
                if not isinstance(item, tuple) or len(item) != 2:
                    raise GeminiUnsupportedContentError(
                        f"Attachment #{idx} must be a (mime_type, bytes) tuple", status=400
                    )
                mime, b = item
                mime_clean = (mime or "").strip().lower() or "image/png"
                if mime_clean not in SUPPORTED_MIME_TYPES:
                    raise GeminiUnsupportedContentError(
                        f"Unsupported attachment MIME type: {mime_clean}", status=400
                    )
                if not isinstance(b, (bytes, bytearray)):
                    raise GeminiUnsupportedContentError(f"Attachment #{idx} data must be bytes", status=400)
                if len(b) > DEFAULT_MAX_IMAGE_BYTES:
                    raise GeminiUnsupportedContentError(
                        f"Attachment #{idx} size ({len(b)} bytes) exceeds per-item limit of {DEFAULT_MAX_IMAGE_BYTES}",
                        status=400,
                    )
                total_bytes += len(b)
                if total_bytes > DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES:
                    raise GeminiUnsupportedContentError(
                        f"Total attachment size ({total_bytes} bytes) exceeds limit of {DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES}",
                        status=400,
                    )
                validated_images.append((mime_clean, bytes(b)))

        clean_temp: float | None = None
        if temperature is not None:
            clean_temp = max(0.0, min(2.0, float(temperature)))

        clean_max_out: int | None = None
        if max_output_tokens is not None:
            clean_max_out = max(1, min(8192, int(max_output_tokens)))

        return prompt, clean_sys, validated_images, clean_temp, clean_max_out

    def build_request_body(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        inline_images: Sequence[tuple[str, bytes]] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if inline_images:
            for mime, b in inline_images:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(b).decode("ascii"),
                        }
                    }
                )

        parts.append({"text": prompt})
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}]}

        if system_instruction and system_instruction.strip():
            body["systemInstruction"] = {
                "parts": [{"text": system_instruction.strip()}],
            }

        gen_cfg: dict[str, Any] = {}
        if temperature is not None:
            gen_cfg["temperature"] = float(temperature)
        if max_output_tokens is not None:
            gen_cfg["maxOutputTokens"] = int(max_output_tokens)
        if gen_cfg:
            body["generationConfig"] = gen_cfg

        return body

    async def generate_text(
        self,
        *,
        prompt: str,
        model: str | None = None,
        role: ModelRole | str | None = None,
        system_instruction: str | None = None,
        inline_images: Sequence[tuple[str, bytes]] | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: int = 25,
        session: aiohttp.ClientSession | None = None,
        user_id: int | None = None,
        guild_id: int | None = None,
        max_retries: int | None = None,
    ) -> str:
        """
        Execute a text generation call through the Gemini Developer REST API.
        Enforces budget, bounds, concurrency semaphore, and secret redaction.
        """
        if not self.is_available:
            raise GeminiMisconfigurationError("missing api key", status=0)

        eff_model = self.resolve_model(model, role=role)
        if not eff_model:
            raise GeminiMisconfigurationError("missing model name", status=0)

        # 1. Validate payload boundaries
        v_prompt, v_sys, v_images, v_temp, v_max_out = self.validate_request_payload(
            prompt,
            system_instruction=system_instruction,
            inline_images=inline_images,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

        # 2. Check token/cost governance budget
        self.governor.check_budget(user_id=user_id, guild_id=guild_id)
        self.governor.record_request_start(user_id=user_id, guild_id=guild_id)

        # 3. Construct payload and metadata
        body = self.build_request_body(
            v_prompt,
            system_instruction=v_sys,
            inline_images=v_images,
            temperature=v_temp,
            max_output_tokens=v_max_out,
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{eff_model}:generateContent?key={self.api_key}"
        timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds)))
        secrets = (self.api_key,)

        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        owns_session = session is None or session.closed
        active_session = session if not owns_session else aiohttp.ClientSession(timeout=timeout)
        assert active_session is not None

        try:
            async with self._semaphore:
                for attempt in range(retries + 1):
                    try:
                        async with active_session.post(
                            url,
                            json=body,
                            headers={"Content-Type": "application/json"},
                            timeout=timeout,
                        ) as resp:
                            status = int(resp.status)
                            raw_text = await resp.text()

                            # 2xx Success path
                            if 200 <= status < 300:
                                try:
                                    payload = await resp.json()
                                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as err:
                                    safe_details = redact_secrets(raw_text[:2000], secrets)
                                    self.governor.record_failure(status=status)
                                    raise GeminiInvalidResponseError(
                                        "invalid json response", status=status, details=safe_details
                                    ) from err

                                # Check safety block at prompt level
                                prompt_feedback = payload.get("promptFeedback") or {}
                                if str(prompt_feedback.get("blockReason") or "").upper() == "SAFETY":
                                    self.governor.record_failure(status=status)
                                    raise GeminiSafetyBlockError(
                                        "prompt blocked by safety filter",
                                        status=status,
                                        details="promptFeedback.blockReason=SAFETY",
                                    )

                                candidates = payload.get("candidates") or []
                                if not candidates:
                                    self.governor.record_failure(status=status)
                                    raise GeminiInvalidResponseError(
                                        "empty response (no candidates)", status=status, details=""
                                    )

                                cand0 = candidates[0] or {}
                                finish_reason = str(cand0.get("finishReason") or "").upper()
                                if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST"):
                                    self.governor.record_failure(status=status)
                                    raise GeminiSafetyBlockError(
                                        f"response blocked by safety filter (finishReason={finish_reason})",
                                        status=status,
                                        details=f"finishReason={finish_reason}",
                                    )

                                text = _extract_text(payload)
                                if not text:
                                    self.governor.record_failure(status=status)
                                    msg = "empty response" + (
                                        f" (finishReason={finish_reason})" if finish_reason else ""
                                    )
                                    raise GeminiInvalidResponseError(msg, status=status, details="")

                                # Record usage & cost
                                usage_meta = payload.get("usageMetadata") or {}
                                in_tokens = int(usage_meta.get("promptTokenCount") or 0)
                                out_tokens = int(usage_meta.get("candidatesTokenCount") or 0)
                                self.governor.record_usage(
                                    model=eff_model,
                                    input_tokens=in_tokens,
                                    output_tokens=out_tokens,
                                    user_id=user_id,
                                    guild_id=guild_id,
                                )
                                return text

                            # Error responses handling
                            safe_details = redact_secrets(raw_text[:2000], secrets)
                            retry_after = parse_retry_after(resp.headers.get("Retry-After"))

                            # Rate limit / quota (429)
                            if status == 429:
                                self.governor.record_failure(status=429)
                                if attempt < retries:
                                    delay = (
                                        retry_after
                                        if retry_after is not None
                                        else min(
                                            self.max_backoff,
                                            self.initial_backoff * (2**attempt) + random.uniform(0.1, 0.5),
                                        )
                                    )
                                    log.warning(
                                        "Gemini 429 rate limit hit; backoff %0.2fs (attempt %d/%d)",
                                        delay,
                                        attempt + 1,
                                        retries,
                                    )
                                    await self._sleep_fn(delay)
                                    continue
                                raise GeminiRateLimitError(
                                    "rate limited",
                                    status=429,
                                    details=safe_details,
                                    retry_after=retry_after,
                                )

                            # Server transient errors (500, 502, 503, 504)
                            if 500 <= status < 600:
                                self.governor.record_failure(status=status)
                                if attempt < retries:
                                    delay = min(
                                        self.max_backoff,
                                        self.initial_backoff * (2**attempt) + random.uniform(0.1, 0.5),
                                    )
                                    log.warning(
                                        "Gemini %d server error; backoff %0.2fs (attempt %d/%d)",
                                        status,
                                        delay,
                                        attempt + 1,
                                        retries,
                                    )
                                    await self._sleep_fn(delay)
                                    continue
                                raise GeminiTransientError(
                                    f"server error ({status})", status=status, details=safe_details
                                )

                            # Auth / misconfiguration errors (401, 403)
                            if status in (401, 403):
                                self.governor.record_failure(status=status)
                                raise GeminiMisconfigurationError(
                                    "authentication or permission denied",
                                    status=status,
                                    details=safe_details,
                                )

                            # Client request errors (400, etc.)
                            self.governor.record_failure(status=status)
                            if status == 400 and (
                                "API_KEY_INVALID" in safe_details or "key" in safe_details.lower()
                            ):
                                raise GeminiMisconfigurationError(
                                    "invalid api key", status=400, details=safe_details
                                )
                            raise GeminiError("non-2xx response", status=status, details=safe_details)

                    except (asyncio.CancelledError, GeminiError):
                        raise
                    except TimeoutError:
                        self.governor.record_failure(status=0)
                        if attempt < retries:
                            delay = min(
                                self.max_backoff,
                                self.initial_backoff * (2**attempt) + random.uniform(0.1, 0.5),
                            )
                            log.warning(
                                "Gemini timeout; backoff %0.2fs (attempt %d/%d)",
                                delay,
                                attempt + 1,
                                retries,
                            )
                            await self._sleep_fn(delay)
                            continue
                        raise
                    except aiohttp.ClientError as err:
                        self.governor.record_failure(status=0)
                        safe_err = redact_secrets(str(err), secrets)
                        if attempt < retries:
                            delay = min(
                                self.max_backoff,
                                self.initial_backoff * (2**attempt) + random.uniform(0.1, 0.5),
                            )
                            log.warning(
                                "Gemini network client error (%s); backoff %0.2fs",
                                safe_err,
                                delay,
                            )
                            await self._sleep_fn(delay)
                            continue
                        raise GeminiTransientError(f"network error: {safe_err}", status=0) from err
        finally:
            if owns_session:
                await active_session.close()

        # Should never reach here unless loop terminates without return/raise
        raise GeminiError("request failed", status=0)

    def get_diagnostics(self) -> dict[str, Any]:
        """Admin diagnostics summary reporting provider status and budget metrics without secrets."""
        diag = self.governor.get_diagnostics()
        diag.update(
            {
                "available": self.is_available,
                "model_default": self.default_model,
                "model_resume": self.resume_model,
                "concurrency_limit": self._concurrency_limit,
                "max_retries": self.max_retries,
                "key_configured": bool(self.api_key),
            }
        )
        return diag

    def format_diagnostics(self) -> str:
        """Formatted human-readable string for admin inspection."""
        diag = self.get_diagnostics()
        status_icon = "🟢" if diag["available"] else "⚪"
        return (
            f"**AI Provider Diagnostics** {status_icon}\n"
            f"• Available: `{diag['available']}` (Key Configured: `{diag['key_configured']}`)\n"
            f"• Default Model: `{diag['model_default']}` | Resume Model: `{diag['model_resume']}`\n"
            f"• Concurrency Limit: `{diag['concurrency_limit']}` | Max Retries: `{diag['max_retries']}`\n"
            f"• Requests: Total `{diag['total_requests']}` | Success `{diag['successful_requests']}` | Fail `{diag['failed_requests']}` | 429 Hits `{diag['rate_limit_hits']}`\n"
            f"• Tokens: Input `{diag['total_input_tokens']:,}` | Output `{diag['total_output_tokens']:,}`\n"
            f"• Cost (USD): Est. Total `${diag['total_cost_usd']:.4f}` | 24h `${diag['daily_cost_usd']:.4f}` / Limit `${diag['daily_cost_limit_usd']:.2f}`"
        )


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    for p in parts:
        t = (p or {}).get("text")
        if isinstance(t, str) and t.strip():
            texts.append(t)
    return "\n".join(texts).strip()


# Backwards compatibility functional wrapper
async def gemini_generate_text(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_instruction: str | None = None,
    inline_images: Sequence[tuple[str, bytes]] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    timeout_seconds: int = 25,
    session: aiohttp.ClientSession | None = None,
    max_retries: int = 0,
    user_id: int | None = None,
    guild_id: int | None = None,
    client: GeminiClient | None = None,
    **kwargs: Any,
) -> str:
    """Call the Gemini Developer API (REST) and return the model's text output."""
    if client is not None:
        return await client.generate_text(
            prompt=prompt,
            model=model,
            system_instruction=system_instruction,
            inline_images=inline_images,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            session=session,
            max_retries=max_retries,
            user_id=user_id,
            guild_id=guild_id,
        )

    temp_client = GeminiClient(
        api_key=api_key,
        default_model=model,
        max_retries=max_retries,
    )
    return await temp_client.generate_text(
        prompt=prompt,
        model=model,
        system_instruction=system_instruction,
        inline_images=inline_images,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        session=session,
        max_retries=max_retries,
        user_id=user_id,
        guild_id=guild_id,
    )
