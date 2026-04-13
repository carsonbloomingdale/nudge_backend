"""Map emoji in journal/task strings to readable aliases for LLM prompts."""

from __future__ import annotations

import re
from typing import List

import emoji as emoji_lib

_ALIAS_RE = re.compile(r":([a-zA-Z0-9_+-]+):")


def emoji_meaning_hints(text: str, *, max_hints: int = 24) -> List[str]:
    """Return unique English-style labels for emoji in *text* (order preserved)."""
    if not text:
        return []
    if not emoji_lib.emoji_count(text):
        return []
    dem = emoji_lib.demojize(text)
    out: List[str] = []
    seen: set[str] = set()
    for m in _ALIAS_RE.finditer(dem):
        raw = m.group(1)
        label = raw.replace("_", " ").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= max_hints:
            break
    return out
