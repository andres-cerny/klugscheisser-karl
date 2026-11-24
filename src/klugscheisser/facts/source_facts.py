"""Shared writer for the free structured profile sources (2026-10-25): Hockey Canada rosters, Wikipedia infoboxes.
Each source owns its rows (`source` + `extractor`); a run brings them to exactly what the source says
now (unchanged rows keep their id, stale ones are deleted), so a second run changes nothing.

Other sources' rows are never edited. Trust for profile facts (height, weight, shoots, position, birth date/place):
Elite Prospects > Hockey Canada (official tournament roster) > Wikidata > Wikipedia infobox > others
(birth date: Wikidata first, as in `facts.queries._rank`).
A new row that disagrees with a confirmed row of a *more trusted* source is stored with status "conflict" (the
trusted value is shown first, this one next to it, like Elite Prospects conflicts). A small tolerance applies
where sources round differently (feet/inches and pounds → ±2 cm / ±2 kg).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

TRUST = {"eliteprospects": 0, "hockeycanada": 1, "wikidata": 2, "wikipedia": 4}
# birth date (not a hockey field, see facts.queries._rank): Wikidata first, then the roster
TRUST_BIRTH = {"wikidata": 0, "hockeycanada": 1, "eliteprospects": 2, "wikipedia": 4}
TOLERANCE = {"height_cm": 2, "weight_kg": 2}
COMPARED = ("height_cm", "weight_kg", "shoots", "position", "birth_date", "birth_place")


def trust(source: str, kind: str = "") -> int:
    return (TRUST_BIRTH if kind == "birth_date" else TRUST).get(source, 9)


@dataclass
class Spec:
    kind: str
    value: dict
    value_text: str
    season: int | None = None
    valid_from: date | None = None
    url: str | None = None  # per-fact link (default: the import's url)

    def __post_init__(self) -> None:
        self.value = {k: v for k, v in self.value.items() if v not in (None, "")}

    @property
    def key(self) -> tuple:
        return (self.kind, tuple(sorted((k, str(v)) for k, v in self.value.items())), self.season)


def _key(f: m.Fact) -> tuple:
    return (f.kind, tuple(sorted((k, str(v)) for k, v in (f.value or {}).items())), f.season)


def comparable(kind: str, value: dict, value_text: str = "") -> str | float | None:
    """Value to compare across sources (None: cannot be compared)."""
    from klugscheisser.ep_import import value_key

    v = value or {}
    if kind in TOLERANCE:
        try:
            return float(v.get("number"))
        except (TypeError, ValueError):
            return None
    if kind == "birth_date":
        return v.get("date") or (str(v["year"]) if v.get("year") else None)
    return value_key(kind, v, value_text)


def agrees(kind: str, a, b) -> bool:
    if kind in TOLERANCE:
        return abs(float(a) - float(b)) <= TOLERANCE[kind]
    if kind == "birth_date":  # '1994' and '1994-05-15' agree (one is less precise)
        return str(a).startswith(str(b)) or str(b).startswith(str(a))
    return a == b


def status_for(s: Session, athlete_id: int, source: str, kind: str, value: dict, value_text: str = "") -> str:
    """'conflict' when a confirmed row of a more trusted source says something else (and none agrees)."""
    if kind not in COMPARED:
        return "confirmed"
    mine = comparable(kind, value, value_text)
    if mine is None:
        return "confirmed"
    better = [
        f
        for f in s.scalars(
            select(m.Fact).where(
                m.Fact.athlete_id == athlete_id, m.Fact.kind == kind, m.Fact.status == "confirmed"
            )
        )
        if trust(f.source, kind) < trust(source, kind)
    ]
    values = [x for f in better if (x := comparable(kind, f.value or {}, f.value_text)) is not None]
    if not values or any(agrees(kind, mine, x) for x in values):
        return "confirmed"
    return "conflict"


def sync_facts(
    s: Session,
    a: m.Athlete,
    source: str,
    extractor: str,
    url: str,
    specs: list[Spec],
    counts: Counter | None = None,
) -> list[m.Fact]:
    """Bring this source's rows (source + extractor) of one person to exactly `specs`; statuses recomputed."""
    counts = counts if counts is not None else Counter()
    existing = s.scalars(
        select(m.Fact)
        .where(m.Fact.athlete_id == a.id, m.Fact.source == source, m.Fact.extractor == extractor)
        .order_by(m.Fact.id)
    ).all()
    wanted: dict[tuple, Spec] = {}
    for spec in specs:
        wanted.setdefault(spec.key, spec)
    kept: dict[tuple, m.Fact] = {}
    for f in existing:
        k = _key(f)
        spec = wanted.get(k)
        if spec is not None and k not in kept and f.source_url == (spec.url or url):
            kept[k] = f
            f.valid_from = spec.valid_from
        else:
            s.delete(f)
            counts["facts_removed"] += 1
    s.flush()
    for k, spec in wanted.items():
        if k in kept:
            continue
        f = m.Fact(
            athlete_id=a.id,
            kind=spec.kind,
            value=spec.value,
            value_text=spec.value_text,
            season=spec.season,
            valid_from=spec.valid_from,
            source=source,
            source_url=spec.url or url,
            quote=None,
            confidence=1.0,
            status="confirmed",
            extractor=extractor,
        )
        s.add(f)
        kept[k] = f
        counts["facts_added"] += 1
    s.flush()
    for f in kept.values():
        f.status = status_for(s, a.id, source, f.kind, f.value or {}, f.value_text)
        counts[f"fact:{f.kind}"] += 1
        if f.status == "conflict":
            counts["conflicts"] += 1
    s.flush()
    return list(kept.values())


# --- German value texts (same wording as the Elite Prospects / Wikidata imports) ------------------------------

POSITION_DE = {"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"}


def height_spec(cm: int, **kw) -> Spec:
    return Spec("height_cm", {"number": cm, **kw.pop("extra", {})}, f"{cm} cm", **kw)


def weight_spec(kg: int, **kw) -> Spec:
    return Spec("weight_kg", {"number": kg, **kw.pop("extra", {})}, f"{kg} kg", **kw)


def shoots_spec(hand: str, goalie: bool = False, **kw) -> Spec:
    side = "links" if hand == "L" else "rechts"
    value = {"text": hand, "catches": True if goalie else None}
    return Spec("shoots", value, f"{side} (Fanghand)" if goalie else side, **kw)


def position_spec(code: str, **kw) -> Spec:
    from klugscheisser.llm.words import position_labels_de

    text = position_labels_de().get(code) or POSITION_DE[code]  # the project's sport ("Stürmer" for men)
    return Spec("position", {"code": code, "text": text}, text, **kw)


def birth_date_spec(d: date, **kw) -> Spec:
    return Spec("birth_date", {"date": d.isoformat()}, f"{d:%d.%m.%Y}", **kw)
