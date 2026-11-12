from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoreEntry:
    id: str
    triggers: tuple[str, ...]
    title: str
    summary: str
    response_rules: tuple[str, ...]


def _candidate_paths() -> list[Path]:
    """Find lore pack file paths in a few common locations."""
    env = (os.getenv("LORE_PATH") or "").strip()
    paths: list[Path] = []
    if env:
        paths.append(Path(env))

    # Common defaults (repo root)
    cwd = Path.cwd()
    paths.extend(
        [
            cwd / "tools" / "lore.tmu_files.json",
            cwd / "tools" / "lore.json",
            cwd / "tools" / "lore.example.json",
            cwd / "lore.tmu_files.json",
            cwd / "lore.json",
            cwd / "lore.example.json",
        ]
    )

    # Also relative to this file
    here = Path(__file__).resolve()
    paths.extend(
        [
            here.parent / "lore.tmu_files.json",
            here.parent / "lore.json",
            here.parent / "lore.example.json",
        ]
    )

    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


_LORE_CACHE: list[LoreEntry] | None = None


def load_lore() -> list[LoreEntry]:
    global _LORE_CACHE
    if _LORE_CACHE is not None:
        return _LORE_CACHE

    pack_path = None
    for p in _candidate_paths():
        if p.exists() and p.is_file():
            pack_path = p
            break

    if pack_path is None:
        _LORE_CACHE = []
        return _LORE_CACHE

    try:
        data = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _LORE_CACHE = []
        return _LORE_CACHE

    entries: list[LoreEntry] = []
    for e in data.get("entries") or []:
        triggers = tuple(str(t).strip().lower() for t in (e.get("triggers") or []) if str(t).strip())
        if not triggers:
            continue
        entries.append(
            LoreEntry(
                id=str(e.get("id") or "").strip() or "unknown",
                triggers=triggers,
                title=str(e.get("title") or "").strip() or "Lore",
                summary=str(e.get("summary") or "").strip(),
                response_rules=tuple(
                    str(r).strip() for r in (e.get("response_rules") or []) if str(r).strip()
                ),
            )
        )

    _LORE_CACHE = entries
    return _LORE_CACHE


def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_lore(texts: Iterable[str], *, max_entries: int = 5, max_chars: int = 1400) -> str:
    """Return a compact lore snippet for the prompt based on trigger matches."""
    lore = load_lore()
    if not lore:
        return ""

    blob = _normalize_text("\n".join(t for t in texts if t))
    if not blob:
        return ""

    scored: list[tuple[int, LoreEntry]] = []
    for entry in lore:
        hits = 0
        for trig in entry.triggers:
            if trig and trig in blob:
                hits += 1
        if hits:
            scored.append((hits, entry))

    if not scored:
        return ""

    scored.sort(key=lambda x: (-x[0], x[1].id))
    picked = [e for _, e in scored[:max_entries]]

    lines: list[str] = []
    lines.append("Server lore:")
    for e in picked:
        rules = ""
        if e.response_rules:
            rules = " Rules: " + "; ".join(e.response_rules[:2])
        lines.append(f"- {e.title}: {e.summary}{rules}")

    out = "\n".join(lines).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out
