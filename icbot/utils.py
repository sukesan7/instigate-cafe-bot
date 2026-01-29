from __future__ import annotations
import re
from typing import Optional


def clamp_nonnegative(x: int) -> int:
    return max(0, x)


def parse_signed_int(s: str) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if not re.fullmatch(r"[+-]?\d+", s):
        return None
    try:
        return int(s)
    except ValueError:
        return None
