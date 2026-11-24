"""Passages for the chunk vectors (switch RETRIEVAL_CHUNKS, search lab 2026-10-25).

A full document of up to SINGLE_MAX characters is one passage. Longer ones (web articles, Wikipedia / college
bios) are split on paragraph and sentence ends into ~SIZE-character passages; each one starts with the last
~OVERLAP characters of the one before, so a fact on a boundary is in one piece somewhere.

What gets embedded is `embed_text`: gemini-embedding-2's document format `title: … | text: …`, with a short
header (source, date, persons) before the passage, so a paragraph of a long bio still "knows" whom and when
it is about. The header is not part of the stored passage text (that is what Jev and the reader see)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SINGLE_MAX = 2000
SIZE = 1800
OVERLAP = 200
_SENT = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass
class Passage:
    ord: int
    start: int
    text: str


def doc_body(item) -> str:
    return " ".join(filter(None, [item.text, item.image_description]))


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def split(body: str, size: int = SIZE, overlap: int = OVERLAP, single_max: int = SINGLE_MAX) -> list[Passage]:
    body = body or ""
    if len(body) <= single_max:
        return [Passage(0, 0, body)]
    # cut points: sentence / line ends; a passage ends at the last cut point before `size`
    cuts = sorted({m.end() for m in _SENT.finditer(body)} | {len(body)})
    out: list[Passage] = []
    start = 0
    while start < len(body):
        limit = start + size
        end = max((c for c in cuts if start < c <= limit), default=None)
        if end is None or end - start < size // 3:  # no sentence end in reach: cut at a space
            end = min(len(body), limit)
            sp = body.rfind(" ", start + size // 2, end)
            if end < len(body) and sp > 0:
                end = sp
        out.append(Passage(len(out), start, body[start:end].strip()))
        if end >= len(body):
            break
        # next passage starts ~overlap chars earlier, at a sentence start if there is one
        back = max(start + 1, end - overlap)
        nxt = min((c for c in cuts if back <= c < end), default=back)
        start = nxt
    # a tiny tail is merged into the passage before
    if len(out) > 1 and len(out[-1].text) < 300:
        last = out.pop()
        prev = out[-1]
        out[-1] = Passage(prev.ord, prev.start, body[prev.start : last.start + len(last.text)].strip())
    return [p for p in out if p.text]


def header(item, names: list[str]) -> str:
    date = item.published_at.date().isoformat() if item.published_at else "unbekannt"
    src = item.source_name or item.platform or ""
    kind = item.source_kind or item.source_type or ""
    who = ", ".join(names[:6]) or "-"
    return f"Quelle: {src} ({kind}) | Datum: {date} | Personen: {who}"


def item_names(item) -> list[str]:
    names = [ia.athlete.full_name for ia in item.athletes if ia.athlete]
    if not names and item.teams:
        names = [it.team.name for it in item.teams if it.team]
    return sorted(names)


def embed_text(item, passage: Passage, names: list[str] | None = None) -> str:
    title = (item.title or "").strip() or "none"
    names = item_names(item) if names is None else names
    return f"title: {title[:200]} | text: {header(item, names)}\n{passage.text}"


def store_chunks(session, embedder, item) -> int:
    """(Re)compute the passage vectors of one full document if its text changed. Returns passages written."""
    from sqlalchemy import delete, select

    from klugscheisser.db import models as m

    body = doc_body(item)
    if item.level != "full" or not item.text:
        return 0
    h = body_hash(body)
    old = session.scalar(
        select(m.ItemChunk.text_hash).where(
            m.ItemChunk.item_id == item.id, m.ItemChunk.model == embedder.model, m.ItemChunk.ord == 0
        )
    )
    if old == h:
        return 0
    names = item_names(item)
    parts = split(body)
    vecs = embedder.embed_documents([embed_text(item, p, names) for p in parts])
    session.execute(
        delete(m.ItemChunk).where(m.ItemChunk.item_id == item.id, m.ItemChunk.model == embedder.model)
    )
    for p, v in zip(parts, vecs, strict=True):
        session.add(
            m.ItemChunk(
                item_id=item.id,
                model=embedder.model,
                ord=p.ord,
                start=p.start,
                text=p.text,
                text_hash=h,
                vector=v,
            )
        )
    session.flush()
    return len(parts)


__all__ = ["Passage", "body_hash", "doc_body", "embed_text", "header", "item_names", "split", "store_chunks"]
