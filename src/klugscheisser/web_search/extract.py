"""Main text and metadata of an arbitrary web page (articles, player profiles, college roster pages).

Order: JSON-LD ``articleBody``; else the paragraphs of the page's ``<article>``; else the paragraphs of
``<main>`` (or the whole body); when that yields too little (profile pages are mostly lists and tables), the
block text of ``<main>``/body without navigation, header, footer and forms. Reuses the news excerpt helpers
of ``collectors.rss`` without changing them.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from klugscheisser.collectors.rss import (
    _BOILERPLATE,
    _SENTENCE_END,
    _article_container,
    _json_ld_body,
    strip_html,
)

TEXT_MAX = 8000
MIN_PARAGRAPH = 40  # shorter <p> are captions, bylines, buttons
MIN_TEXT = 300  # below this the paragraph route found too little -> block text

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|nav|header|footer|aside|form|iframe|button|select)\b.*?</\1\s*>",
    re.S | re.I,
)
_BLOCK_TAGS = re.compile(
    r"</?(?:p|div|section|article|li|ul|ol|dl|dt|dd|tr|table|h[1-6]|br|blockquote|figure|figcaption)\b[^>]*>",
    re.I,
)
_COOKIE = re.compile(r"(cookie|javascript|datenschutz|privacy policy|newsletter|abonnieren|subscribe)", re.I)


_INLINE_TAGS = re.compile(
    r"</?(?:a|span|b|i|em|strong|sup|sub|small|abbr|cite|code|mark|u|s|q|font|bdi)\b[^>]*>", re.I
)


def _clean(fragment: str) -> str:
    """Like strip_html, but inline tags vanish without a space: «Lengnau AG)» not «Lengnau AG )»."""
    return strip_html(_INLINE_TAGS.sub("", fragment))


@dataclass
class PageMeta:
    title: str | None = None
    site_name: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    language: str | None = None


def _meta(page: str, *names: str) -> str | None:
    for name in names:
        for pattern in (
            rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(name)}["\']',
        ):
            m = re.search(pattern, page, re.I)
            if m and m.group(1).strip():
                return htmllib.unescape(m.group(1)).strip()
    return None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if not m:
            return None
        dt = datetime.fromisoformat(m.group(1))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _json_ld_nodes(page: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            stack = [json.loads(m.group(1))]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def extract_meta(page: str) -> PageMeta:
    meta = PageMeta()
    meta.title = _meta(page, "og:title", "twitter:title")
    if not meta.title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        meta.title = strip_html(m.group(1)) if m else None
    meta.site_name = _meta(page, "og:site_name", "application-name")
    published = _meta(page, "article:published_time", "datePublished", "date", "dc.date")
    nodes = _json_ld_nodes(page)
    for node in nodes:
        if not published and isinstance(node.get("datePublished"), str):
            published = node["datePublished"]
        publisher = node.get("publisher")
        if not meta.site_name and isinstance(publisher, dict) and isinstance(publisher.get("name"), str):
            meta.site_name = publisher["name"].strip() or None
    if not published:
        m = re.search(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", page, re.I)
        published = m.group(1) if m else None
    meta.published_at = parse_date(published)
    modified = _meta(page, "article:modified_time", "dateModified")
    if not modified:
        modified = next((n["dateModified"] for n in nodes if isinstance(n.get("dateModified"), str)), None)
    meta.modified_at = parse_date(modified)
    m = re.search(r"<html[^>]*\blang=[\"']([A-Za-z-]+)[\"']", page, re.I)
    meta.language = m.group(1).split("-")[0].lower() if m else None
    return meta


def truncate_sentences(text: str, limit: int = TEXT_MAX) -> str:
    """Cut at the last sentence end before ``limit``; never in the middle of a sentence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(_SENTENCE_END.finditer(cut))
    if ends:
        return cut[: ends[-1].end()].rstrip() + " […]"
    newline = cut.rfind("\n")
    if newline > 0:
        return cut[:newline].rstrip() + " […]"
    return cut[: cut.rfind(" ")] + " […]"


def _keep(paragraph: str, min_len: int) -> bool:
    if len(paragraph) < min_len or _BOILERPLATE.match(paragraph):
        return False
    return len(paragraph) > 200 or not _COOKIE.search(paragraph)  # short consent/newsletter notices


def _paragraphs(fragment: str) -> list[str]:
    return [_clean(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", fragment, re.S | re.I)]


def _main(page: str) -> str:
    m = re.search(r"<main\b.*?</main>", page, re.S | re.I)
    if m:
        return m.group(0)
    m = re.search(r"<body\b.*?</body>", page, re.S | re.I)
    return m.group(0) if m else page


def _block_text(fragment: str) -> list[str]:
    fragment = _DROP_BLOCKS.sub(" ", fragment)
    fragment = re.sub(r"</t[dh]>", " · ", fragment, flags=re.I)
    lines = [_clean(x) for x in _BLOCK_TAGS.split(fragment)]
    return [x.strip(" ·") for x in lines if x.strip(" ·")]


def _dedup(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def extract_main_text(page: str, limit: int = TEXT_MAX) -> str | None:
    """Verbatim main text of a page (paragraphs joined by newlines), capped at ``limit`` characters."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    paras = [p for p in (_json_ld_body(page) or []) if _keep(p, MIN_PARAGRAPH)]
    if not paras:
        container = _article_container(page)
        if container:
            paras = [p for p in _paragraphs(_DROP_BLOCKS.sub(" ", container)) if _keep(p, MIN_PARAGRAPH)]
    if not paras:
        main = _DROP_BLOCKS.sub(" ", _main(page))
        paras = [p for p in _paragraphs(main) if _keep(p, MIN_PARAGRAPH)]
        if sum(map(len, paras)) < MIN_TEXT:
            blocks = [b for b in _block_text(_main(page)) if _keep(b, 3)]
            if sum(map(len, blocks)) > sum(map(len, paras)):
                paras = blocks
    text = "\n".join(_dedup(paras)).strip()
    return truncate_sentences(text, limit) if text else None
