"""Story groups at query time (2026-10-25): many outlets report the same news (a coach appointment, a transfer, a
retirement), so a ranked list showed the same story up to 10 times. After Jev has scored the candidates, documents
of one story are folded into one entry; the freed places go to the next distinct stories.

Same story = all of:
- both documents may be grouped: not a profile/biography (Wikipedia & co.), not a result sheet or stats page, not
  a multi-topic news digest or ticker ("Sportnews-Ticker", "Die wichtigsten News", "Tagesticker" …);
- same person (a shared linked athlete), or, when neither names a person, the same team;
- published within STORY_HOURS of each other;
- their passage vectors (`item_chunks`, gemini-embedding-2) are at least STORY_SIM similar (best passage pair).

Threshold test (2026-10-25, docs/story-groups.md): 713 candidate pairs from the top 40 of 48 searches; Sonnet labelled a
stratified sample of 150 (checked by hand), Haiku all 713. With the guards above and 0.91: est. merge precision
0.985 (Sonnet) / 0.980 (Haiku), recall 0.70 / 0.67. Wider windows (72 h) and lower thresholds merged follow-ups
and the next game of a tournament.

A group's main entry: the best Jev score; on a tie the primary source first (club/league/federation/the player's
own post > SRF and public broadcasters > other media > fan/brand accounts). The others stay reachable by id.
Nothing is stored; one SQL query per search (only for pairs that pass the cheap checks)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m

STORY_SIM = 0.91  # min. cosine similarity of the best passage pair
STORY_HOURS = 48  # max. time between the two publications

NO_GROUP_TYPES = {"profile", "official_result"}
NO_GROUP_KINDS = {"profile", "result"}
# reference/stats pages that look like web documents
NO_GROUP_SOURCES = re.compile(
    r"wikipedia|wikidata|grokipedia|ice ?hockey ?wiki|hockeydb|eliteprospects|olympedia|quanthockey|"
    r"hockey-reference|statistik|stats",
    re.I,
)
# one document, several unrelated news items: never folded (it may carry another story)
DIGEST_TITLES = re.compile(
    r"ticker|die wichtigsten news|news aus der|newsletter|briefing|round-?up|recap|highlights|best of|"
    r"wochenrückblick|rückblick",
    re.I,
)
# primary sources first on a score tie (lower = shown first)
PRIMACY = {"official": 0, "self": 0, "srf": 1, "public": 1, "media": 2, "web": 2, "boulevard": 3}


def groupable(item: m.Item) -> bool:
    if item.published_at is None:
        return False
    if item.source_type in NO_GROUP_TYPES or (item.source_kind or "") in NO_GROUP_KINDS:
        return False
    if NO_GROUP_SOURCES.search(f"{item.source_name or ''} {item.url or ''}"):
        return False
    return not DIGEST_TITLES.search(item.title or "")


def primacy(item: m.Item) -> int:
    return PRIMACY.get(item.source_kind or "", 4)


def _persons(item: m.Item) -> set[int]:
    return {ia.athlete_id for ia in item.athletes}


def _teams(item: m.Item) -> set[int]:
    return {it.team_id for it in item.teams}


def related(a: m.Item, b: m.Item, hours: float = STORY_HOURS) -> bool:
    """The cheap checks: both groupable, close in time, same person (or same team when neither names a person)."""
    if not (groupable(a) and groupable(b)):
        return False
    if abs((a.published_at - b.published_at).total_seconds()) > hours * 3600:
        return False
    pa, pb = _persons(a), _persons(b)
    if pa or pb:
        return bool(pa & pb)
    return bool(_teams(a) & _teams(b))


def similarities(
    session: Session, settings: Settings, pairs: list[tuple[int, int]]
) -> dict[tuple[int, int], float]:
    """Best passage-pair cosine similarity per document pair (pairs without passage vectors are missing)."""
    if not pairs:
        return {}
    model = f"{settings.chunk_embedding_model}-{settings.chunk_embedding_dim}"
    with session.begin_nested():  # a failure must not break the caller's transaction
        rows = session.execute(
            text(
                "SELECT p.a, p.b, max(1 - (ca.vector <=> cb.vector)) "
                "FROM unnest(CAST(:a AS int[]), CAST(:b AS int[])) AS p(a, b) "
                "JOIN item_chunks ca ON ca.item_id = p.a AND ca.model = :model "
                "JOIN item_chunks cb ON cb.item_id = p.b AND cb.model = :model "
                "GROUP BY p.a, p.b"
            ),
            {"a": [x for x, _ in pairs], "b": [y for _, y in pairs], "model": model},
        ).all()
    return {(int(a), int(b)): float(s) for a, b, s in rows}


def group(
    session: Session,
    settings: Settings,
    items: Sequence[m.Item | None],
    scores: Sequence[float],
    *,
    sim: float = STORY_SIM,
    hours: float = STORY_HOURS,
) -> list[list[int]]:
    """Story groups over a ranked list (best first). Returns lists of indices into `items`, in rank order of each
    group's best member; the first index of a group is its main entry (best score, on a tie the primary source).
    A document joins the first earlier group whose first member it matches (no chains: A~B and B~C does not put
    C with A). `None` items (computed documents) stay alone."""
    n = len(items)
    # the cheap checks once per document (relationships are preloaded by the caller; ~1 ms for 120 documents)
    keys = [
        (it.published_at.timestamp(), _persons(it), _teams(it)) if it is not None and groupable(it) else None
        for it in items
    ]
    pairs: set[tuple[int, int]] = set()
    span = hours * 3600
    for i in range(n):
        ka = keys[i]
        if ka is None:
            continue
        for j in range(i):
            kb = keys[j]
            if kb is None or abs(ka[0] - kb[0]) > span:
                continue
            if (ka[1] & kb[1]) if (ka[1] or kb[1]) else (ka[2] & kb[2]):
                a, b = items[i].id, items[j].id
                pairs.add((min(a, b), max(a, b)))
    sims = similarities(session, settings, sorted(pairs)) if pairs else {}
    groups: list[list[int]] = []
    for i in range(n):
        a = items[i]
        home = None
        if a is not None and sims:
            for g in groups:
                b = items[g[0]]
                if b is not None and sims.get((min(a.id, b.id), max(a.id, b.id)), 0.0) >= sim:
                    home = g
                    break
        if home is None:
            groups.append([i])
        else:
            home.append(i)
    for g in groups:  # main entry: best score, then primary source, then rank
        if len(g) > 1:
            g.sort(key=lambda i: (-round(scores[i], 3), primacy(items[i]) if items[i] is not None else 9, i))
    return groups


def also_entry(item: m.Item) -> dict:
    """A folded document as plain data (JSON-safe): id, source, link, date."""
    from urllib.parse import urlsplit

    src = (item.source_name or urlsplit(item.url or "").netloc.removeprefix("www.") or "?").strip()
    return {
        "id": item.id,
        "quelle": src,
        "url": item.url,
        "datum": item.published_at.date().isoformat() if item.published_at else None,
    }


def also_line(entries: Sequence[dict]) -> str:
    """'Auch berichtet von: SRF, RTS, Blick (+5) – dokument(1, 2, …)' for the folded documents of one group
    (every id listed, so each article stays reachable)."""
    names: list[str] = []
    for e in entries:
        if e["quelle"] not in names:
            names.append(e["quelle"])
    shown = ", ".join(names[:3]) + (f" (+{len(names) - 3})" if len(names) > 3 else "")
    return f"Auch berichtet von: {shown} – dokument({', '.join(str(e['id']) for e in entries)})"


__all__ = [
    "STORY_HOURS",
    "STORY_SIM",
    "also_entry",
    "also_line",
    "group",
    "groupable",
    "primacy",
    "related",
    "similarities",
]
