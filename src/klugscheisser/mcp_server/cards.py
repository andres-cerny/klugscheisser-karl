"""Evidence cards for the MCP `search` tool (switch MCP_SEARCH_CARDS, search lab 2026-10-25) and the
`dokument(id)` tool.

One card per document, most relevant first, plain words (no emoji legend):

    **[1] Titel**
    Typ: Medienbericht · Quelle: SRF · 22.09.2026 (vor 33 Tagen) · über: Lara Stalder
    <excerpt: the full text if it is short, else the passage Jev judged>
    Auch berichtet von: RTS, Blick (+2) – dokument(…)   (story groups, SEARCH_GROUP_STORIES: same story elsewhere)
    Volltext: dokument(12345)            (only when the excerpt is not the full text)
    Link: https://…                      (exactly one canonical link, always the last line)

The excerpt is the same text Jev scored (best passage of the chunk vectors, else the window with the question's
words), cleaned (whitespace, emojis). Short documents (<= FULL_MAX characters, most posts and result lines) are
shown in full. The whole answer aims at TARGET_CHARS; over it, excerpts shrink around their best sentence before
any card is dropped. A last line says what was searched and how to get more."""

from __future__ import annotations

import re
from typing import Any

from klugscheisser.db import models as m
from klugscheisser.retrieval.docs import age_de, passage

FULL_MAX = 800  # cleaned text up to this length goes on the card in full
EXCERPT = 700  # passage length of long documents (shrinks to fit TARGET_CHARS)
EXCERPT_MIN = 300
TARGET_CHARS = 8000
DOC_PART = 10000  # dokument(id): characters per part (+ header stays under the 12k tool cap, MAX_TOOL_CHARS)

_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff‍️⭐⬆⤴⤵\U0001d400-\U0001d7ff]+"
)

KIND_WORDS = {
    "self": "eigener Account",
    "official": "offizielle Quelle",
    "srf": "Medienbericht (SRF)",
    "public": "Medienbericht (öffentlich-rechtlich)",
    "media": "Medienbericht",
    "boulevard": "Boulevard",
    "fan": "Fan-Account – Vorsicht",
    "brand": "Werbung",
    "result": "Resultat",
    "web": "Webseite",
    "profile": "Profil/Biografie",
    "other": "andere Quelle",
}
PLATFORM_WORDS = {"instagram": "Instagram", "x": "X", "rss": "", "web": ""}


_URL = re.compile(r"https?://\S+|www\.\S+")


def clean(text: str | None) -> str:
    """No emojis or decorative unicode letters, no URLs (the card has exactly one link), single spaces."""
    t = _URL.sub(" ", _EMOJI.sub(" ", text or ""))
    return re.sub(r"\s+", " ", t).strip()


def full_text(item: m.Item) -> str:
    body = clean(item.text)
    if item.image_description:
        body = f"{body} Bild: {clean(item.image_description)}".strip()
    return body


def kind_words(item: m.Item) -> str:
    kind = item.source_kind or ("web" if item.source_type == "web" else "other")
    if item.source_type == "profile":
        kind = "profile"
    word = KIND_WORDS.get(kind, KIND_WORDS["other"])
    plat = PLATFORM_WORDS.get(item.platform or "", "")
    if plat and kind == "self":
        return f"{plat}, eigener Account"
    if plat and item.source_type == "tagged_post":
        return f"{plat}, markiert/erwähnt ({word})"
    return f"{plat}, {word}" if plat else word


def _shrink(text: str, terms: list[str], width: int) -> str:
    """Cut a passage to `width` around the sentence with most question words (not just its start)."""
    if len(text) <= width:
        return text
    cut = passage(text, terms, width)
    return clean(cut) + " […]"


def card(n: int, item: m.Item, scores: dict | None, terms: list[str], width: int = EXCERPT) -> dict[str, Any]:
    from klugscheisser.mcp_server import caution

    body = full_text(item)
    best = (scores or {}).get("passage")
    if len(body) <= FULL_MAX:
        excerpt, is_full = body, True
    else:
        excerpt, is_full = _shrink(clean(best) if best else body, terms, width), False
    names = sorted(ia.athlete.full_name for ia in item.athletes if ia.athlete)
    if not names and item.teams:
        names = ["Team " + ", ".join(sorted(t.team.name for t in item.teams if t.team))]
    from urllib.parse import urlsplit

    src = clean(item.source_name) or urlsplit(item.url).netloc.removeprefix("www.") or "-"
    meta = [f"Typ: {kind_words(item)}", f"Quelle: {src}"]
    meta.append(age_de(item.published_at))
    if names:
        meta.append("über: " + ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else ""))
    out = {
        "n": n,
        "id": item.id,
        "titel": clean(item.title) or clean(item.text)[:90] or "(ohne Titel)",
        "meta": " · ".join(meta),
        "auszug": excerpt,
        "volltext": not is_full,
        "url": item.url,
    }
    if (scores or {}).get("auch"):  # story groups (retrieval/stories.py): same story, other sources
        out["auch"] = scores["auch"]
    if caution.is_caution(item):
        from klugscheisser.classify.base import vorsicht_reason

        out["vorsicht"] = vorsicht_reason(item.classification) or "heikel"
    return out


def render_card(c: dict[str, Any]) -> str:
    lines = [f"**[{c['n']}] {c['titel'][:160]}**", c["meta"]]
    if c.get("vorsicht"):
        lines.append(f"Vorsicht: {c['vorsicht']}")
    lines.append(c["auszug"])
    if c.get("auch"):
        from klugscheisser.retrieval.stories import also_line

        lines.append(also_line(c["auch"]))
    if c["volltext"]:
        lines.append(f"Volltext: dokument({c['id']})")
    lines.append(f"Link: {c['url']}")
    return "\n".join(lines)


def build(hits: list, terms: list[str], target: int | None = None) -> list[dict[str, Any]]:
    """Cards for the hits; long excerpts shrink (700 -> 300 chars) until the cards fit `target` (TARGET_CHARS)."""
    target = target or TARGET_CHARS
    width = EXCERPT
    while True:
        cards = [card(i, h.item, h.scores, terms, width) for i, h in enumerate(hits, 1)]
        size = sum(len(render_card(c)) + 2 for c in cards)
        if size <= target or width <= EXCERPT_MIN:
            return cards
        width -= 100


def render(result: dict[str, Any]) -> list[str]:
    cards = result.get("karten") or []
    head = f"_Suche: «{result.get('frage', '')}»"
    filt = result.get("filter")
    if filt:
        head += f" ({filt})"
    head += f" · {len(cards)} Dokumente, nach Relevanz sortiert_"
    out = [head, ""]
    out += [render_card(c) + "\n" for c in cards]
    if not cards:
        out.append("Keine passenden Dokumente gefunden.")
    out.append(result.get("fuss") or "")
    return out


def footer(n_cards: int, n_scored: int, limit: int, since_note: str | None) -> str:
    parts = [f"Gezeigt: {n_cards} beste Dokumente" + (f" von {n_scored} geprüften." if n_scored else ".")]
    if since_note:
        parts.append(since_note)
    parts.append(
        "Mehr: dokument(id) für den ganzen Text; mit athlete/team/since eingrenzen oder limit erhöhen "
        f"(jetzt {limit}); für Zahlen stats, für Spiele list_events, für eine Person get_athlete."
    )
    return "_" + " ".join(parts) + "_"


def doc_parts(body: str, size: int = DOC_PART) -> list[str]:
    """`body` in parts of at most `size` characters, each cut after a sentence end (else at a space) in its last
    third, so no word or sentence is split between two dokument() calls when it can be avoided."""
    out: list[str] = []
    while len(body) > size:
        cut = max(body.rfind(". ", size * 2 // 3, size), body.rfind("! ", size * 2 // 3, size))
        cut = cut + 1 if cut > 0 else body.rfind(" ", size * 2 // 3, size)
        if cut <= 0:
            cut = size
        out.append(body[:cut].strip())
        body = body[cut:].strip()
    out.append(body)
    return out


def dokument(session, item_id: int, teil: int = 1) -> dict[str, Any]:
    """The stored, cleaned full text of one document (text + image description), DOC_PART characters per part.
    Many source pages are behind a login (Instagram, X, paywalls), so this is often the only way to read it."""
    from klugscheisser.mcp_server.tools import NotFound

    item = session.get(m.Item, int(item_id))
    if item is None:
        raise NotFound(f"Kein Dokument mit id {item_id}.")
    pieces = doc_parts(full_text(item))
    parts = len(pieces)
    teil = min(max(1, int(teil)), parts)
    c = card(1, item, None, [])
    text = pieces[teil - 1]
    return {
        "dokument_text": "\n".join(
            [
                f"**{c['titel'][:200]}**",
                c["meta"],
                *([f"Vorsicht: {c['vorsicht']}"] if c.get("vorsicht") else []),
                f"Teil {teil}/{parts}"
                + (f" – weiter mit dokument({item.id}, teil={teil + 1})" if teil < parts else ""),
                "",
                text,
                "",
                f"Link: {item.url}",
            ]
        )
    }


__all__ = ["build", "card", "clean", "doc_parts", "dokument", "footer", "render", "render_card"]
