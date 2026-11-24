"""Text helpers for profile documents: HTML fragment -> lines, cap without cutting a sentence."""

from __future__ import annotations

import re

from klugscheisser.collectors.rss import _SENTENCE_END, strip_html

_BLOCK_END = re.compile(r"<(?:br\s*/?|/p|/li|/div|/h[1-6]|/tr|/dd)\s*>", re.I)


def html_to_lines(fragment: str) -> list[str]:
    """Block-level HTML -> non-empty text lines (verbatim, whitespace collapsed)."""
    fragment = re.sub(r"<(script|style|svg|noscript)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    return [line for line in (strip_html(p) for p in _BLOCK_END.split(fragment)) if line]


def cap_text(text: str, max_chars: int) -> str:
    """At most ``max_chars``; cut after the last full sentence (or line) and mark the cut with " […]"."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    ends = [mt.end() for mt in _SENTENCE_END.finditer(cut)]
    line = cut.rfind("\n")
    best = max([*ends[-1:], line])
    if best <= 0:
        best = cut.rfind(" ")
    return cut[:best].rstrip() + " […]"
