from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiohttp


@dataclass(frozen=True)
class GeminiError(RuntimeError):
    """Raised when Gemini returns a non-2xx response or an unusable payload."""

    message: str
    status: int
    details: str = ""

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
    system_instruction: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
    timeout_seconds: int = 25,
) -> str:
    """Call the Gemini Developer API (REST) and return the model's text output."""

    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not api_key:
        raise GeminiError("missing api key", status=0)
    if not model:
        raise GeminiError("missing model name", status=0)

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )

    body: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ]
    }

    # The API reference uses camelCase `systemInstruction`, but the official curl sample
    # uses snake_case `system_instruction`. The endpoint accepts the curl form.
    if system_instruction and system_instruction.strip():
        body["system_instruction"] = {
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

    status: int = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body, headers={"Content-Type": "application/json"}) as resp:
            status = int(resp.status)
            text = await resp.text()
            if status < 200 or status >= 300:
                raise GeminiError("non-2xx response", status=status, details=text[:2000])

            try:
                payload = await resp.json()
            except Exception:
                # Some proxies or errors still yield text.
                raise GeminiError("invalid json response", status=status, details=text[:2000])

    out = _extract_text(payload)
    if out:
        return out

    # Helpful debug when Gemini blocks/finishes early.
    finish_reason = ""
    try:
        cand0 = (payload.get("candidates") or [{}])[0] or {}
        finish_reason = str(cand0.get("finishReason") or "")
    except Exception:
        finish_reason = ""

    details = (str(payload)[:2000] if payload else "")
    msg = "empty response" + (f" (finishReason={finish_reason})" if finish_reason else "")
    raise GeminiError(msg, status=status, details=details)
