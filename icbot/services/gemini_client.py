from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp

from icbot.errors import ProviderError, parse_retry_after, redact_secrets


@dataclass(frozen=True)
class GeminiError(ProviderError):
    """Raised when Gemini returns a non-2xx response or an unusable payload."""

    message: str
    status: int
    details: str = ""
    retry_after: float | None = None

    def __str__(self) -> str:  # pragma: no cover
        base = f"GeminiError(status={self.status}): {self.message}"
        return f"{base} :: {self.details}" if self.details else base


def _extract_text(payload: dict[str, Any]) -> str:
    # Typical response shape:
    # {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
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


async def gemini_generate_text(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_instruction: str | None = None,
    # Optional inline images (mime_type, bytes). If provided, the request becomes multimodal.
    inline_images: Sequence[tuple[str, bytes]] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    timeout_seconds: int = 25,
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Call the Gemini Developer API (REST) and return the model's text output."""

    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not api_key:
        raise GeminiError("missing api key", status=0)
    if not model:
        raise GeminiError("missing model name", status=0)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    parts: list[dict[str, Any]] = []
    if inline_images:
        for mime, b in inline_images:
            mime = (mime or "").strip() or "image/png"
            # Gemini REST expects base64 data
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

    # Prefer the API's canonical camelCase field name.
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

    timeout = aiohttp.ClientTimeout(total=max(5, int(timeout_seconds)))
    secrets = (api_key,)

    status: int = 0
    owns_session = session is None or session.closed
    active_session = session if not owns_session else aiohttp.ClientSession(timeout=timeout)
    assert active_session is not None
    try:
        try:
            async with active_session.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            ) as resp:
                status = int(resp.status)
                text = await resp.text()
                if status < 200 or status >= 300:
                    safe_details = redact_secrets(text[:2000], secrets)
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    raise GeminiError(
                        "non-2xx response",
                        status=status,
                        details=safe_details,
                        retry_after=retry_after,
                    )

                try:
                    payload = await resp.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as err:
                    safe_details = redact_secrets(text[:2000], secrets)
                    raise GeminiError("invalid json response", status=status, details=safe_details) from err
        except GeminiError:
            raise
        except aiohttp.ClientError as err:
            safe_err = redact_secrets(str(err), secrets)
            raise GeminiError(f"network error: {safe_err}", status=0) from err
    finally:
        if owns_session:
            await active_session.close()

    out = _extract_text(payload)
    if out:
        return out

    # Helpful debug when Gemini blocks/finishes early.
    finish_reason = ""
    try:
        cand0 = (payload.get("candidates") or [{}])[0] or {}
        finish_reason = str(cand0.get("finishReason") or "")
    except (AttributeError, IndexError, TypeError):
        finish_reason = ""

    details = redact_secrets(str(payload)[:2000], secrets) if payload else ""
    msg = "empty response" + (f" (finishReason={finish_reason})" if finish_reason else "")
    raise GeminiError(msg, status=status, details=details)
