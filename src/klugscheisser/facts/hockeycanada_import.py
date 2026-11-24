"""Hockey Canada tournament rosters → person facts (source "hockeycanada"; free profile source, 2026-10-25).

Per event (collectors/hockeycanada.EVENTS, newest first) and team: each roster line is matched to a person we
already have (never creates one), then per person and field the value of the newest event wins:
birth date, height (feet'inches → cm, ±2 cm against other sources), weight (lb → kg), shoots/catches, position,
hometown; for the current event (WCh 2026) also the jersey number and the club as written on the roster.
Link = the roster page of that event and team, data date (`valid_from`) = first day of that tournament.

Matching (the same name index as everywhere, `db.repo.find_athletes`): an exact name/alias hit of a player of the
team's nationality (or without nationality) whose stored birth year is at most one off the roster; also the
reversed name order ("Shiga Akane"). A fuzzy hit is accepted only when the birth date is equal. Two candidates left
→ not matched (counted). The person's own empty `birth_date` / `shoots` / `position` columns are filled from the
roster (never overwritten).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.hockeycanada import EVENTS, SOURCE, HcEvent, HcPlayer, HcRoster
from klugscheisser.db import models as m
from klugscheisser.db.repo import find_athletes
from klugscheisser.facts import source_facts as sf

log = logging.getLogger(__name__)

EXTRACTOR = "hockeycanada-roster"


@dataclass
class HcImport:
    events: list[str] = field(default_factory=list)
    rosters: int = 0
    lines: int = 0
    matched: int = 0
    persons: int = 0
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    empty_rosters: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    counts: Counter = field(default_factory=Counter)

    def out(self) -> dict:
        d = dict(self.__dict__)
        d["counts"] = dict(self.counts)
        return d


def match_person(s: Session, player: HcPlayer, code: str) -> tuple[m.Athlete | None, str]:
    """(person, "exact" | "fuzzy-birth" | "none" | "ambiguous" | "birth-differs")."""
    names = [player.name]
    if player.first_name and player.last_name:
        names.append(f"{player.last_name} {player.first_name}")
    birth = player.birth_date

    def plausible(a: m.Athlete) -> bool:
        return a.role == "athlete" and (a.nationality in (None, code))

    exact: dict[int, m.Athlete] = {}
    fuzzy: dict[int, m.Athlete] = {}
    for name in names:
        for hit in find_athletes(s, name, limit=5):
            if not plausible(hit.athlete):
                continue
            (exact if hit.how == "exact" else fuzzy)[hit.athlete.id] = hit.athlete
    if exact:
        ok = [a for a in exact.values() if _birth_fits(a, birth)]
        if not ok:
            return None, "birth-differs"
        if len(ok) > 1:
            same = [a for a in ok if a.birth_date == birth]
            if len(same) == 1:
                return same[0], "exact"
            return None, "ambiguous"
        return ok[0], "exact"
    if birth is not None:
        same = [a for a in fuzzy.values() if a.birth_date == birth]
        if len(same) == 1:
            return same[0], "fuzzy-birth"
    return None, "none"


def _birth_fits(a: m.Athlete, birth) -> bool:
    """A stored birth date contradicts the roster only when the years are more than one apart: the stored column
    is sometimes year-only (1 January, from Wikidata) or a year off (Wikidata 1995 vs. roster/Wikipedia 1996)."""
    if birth is None or a.birth_date is None or a.birth_date == birth:
        return True
    return abs(a.birth_date.year - birth.year) <= 1


def player_specs(p: HcPlayer, roster: HcRoster) -> list[sf.Spec]:
    e = roster.event
    kw = {"valid_from": e.start, "url": roster.url}
    out: list[sf.Spec] = []
    if p.birth_date:
        out.append(sf.birth_date_spec(p.birth_date, **kw))
    if p.height_cm:
        out.append(sf.height_spec(p.height_cm, extra={"raw": p.height_raw, "event": e.key}, **kw))
    if p.weight_kg:
        out.append(sf.weight_spec(p.weight_kg, extra={"lb": p.weight_lb, "event": e.key}, **kw))
    if p.shoots:
        out.append(sf.shoots_spec(p.shoots, goalie=p.position == "G", **kw))
    if p.position:
        out.append(sf.position_spec(p.position, **kw))
    if p.hometown and len(p.hometown) > 2:
        out.append(sf.Spec("hometown", {"text": p.hometown}, p.hometown, **kw))
    if e.current:
        team = f"{roster.team.name} ({e.label})"
        if p.jersey is not None:
            out.append(
                sf.Spec(
                    "jersey_number",
                    {"number": p.jersey, "team": roster.team.name, "event": e.key},
                    f"Nummer {p.jersey} ({team})",
                    season=e.season,
                    **kw,
                )
            )
        if p.club:
            out.append(
                sf.Spec(
                    "current_team",
                    {"team": p.club, "season": e.season, "text": p.club, "as_of": e.key},
                    f"{p.club} (laut Kader {e.label})",
                    season=e.season,
                    **kw,
                )
            )
    return out


def newest_per_kind(lines: list[tuple[HcPlayer, HcRoster]]) -> list[sf.Spec]:
    """One spec per kind: the newest event's value (lines come newest first)."""
    seen: set[str] = set()
    out: list[sf.Spec] = []
    for p, roster in sorted(lines, key=lambda x: x[1].event.start, reverse=True):
        for spec in player_specs(p, roster):
            if spec.kind not in seen:
                seen.add(spec.kind)
                out.append(spec)
    return out


def _fill_columns(a: m.Athlete, p: HcPlayer, counts: Counter) -> None:
    if a.birth_date is None and p.birth_date:
        a.birth_date = p.birth_date
        counts["column:birth_date"] += 1
    if a.shoots is None and p.shoots:
        a.shoots = p.shoots
        counts["column:shoots"] += 1
    if a.position is None and p.position:
        a.position = p.position
        counts["column:position"] += 1


def import_rosters(
    s: Session,
    rosters: list[HcRoster],
    only_athlete_ids: set[int] | None = None,
    report: HcImport | None = None,
    full: bool = True,
) -> HcImport:
    """Match and store. `full=True`: these rosters are all events this source covers, so a matched person's
    "hockeycanada" rows are brought to exactly what they say (rows of events read before but missing now are
    dropped). `full=False` (nightly: only the current event): a person's rows from other events stay; only
    persons on the given rosters are touched and their rows from the given events replaced."""
    r = report or HcImport()
    by_person: dict[int, list[tuple[HcPlayer, HcRoster]]] = {}
    persons: dict[int, m.Athlete] = {}
    for roster in rosters:
        r.rosters += 1
        if not roster.players:
            r.empty_rosters.append(f"{roster.event.key} {roster.team.code}")
        for p in roster.players:
            r.lines += 1
            a, how = match_person(s, p, roster.team.code)
            label = f"{roster.event.key} {roster.team.code} {p.name}"
            if a is None:
                (r.ambiguous if how == "ambiguous" else r.unmatched).append(f"{label} ({how})")
                continue
            if only_athlete_ids is not None and a.id not in only_athlete_ids:
                continue
            r.matched += 1
            r.counts[f"match:{how}"] += 1
            by_person.setdefault(a.id, []).append((p, roster))
            persons[a.id] = a
    events_read = {ro.event.key for ro in rosters}
    for aid, lines in by_person.items():
        a = persons[aid]
        specs = newest_per_kind(lines)
        if not full:  # keep this person's rows of events not read in this run
            specs += _kept_specs(s, aid, events_read)
        newest = max(lines, key=lambda x: x[1].event.start)
        sf.sync_facts(s, a, SOURCE, EXTRACTOR, newest[1].url, _one_per_kind(specs), r.counts)
        _fill_columns(a, newest[0], r.counts)
        r.persons += 1
    s.flush()
    return r


def event_of(url: str | None) -> str | None:
    for e in EVENTS:
        if f"/{e.path}/" in (url or ""):
            return e.key
    return None


def _kept_specs(s: Session, athlete_id: int, events_read: set[str]) -> list[sf.Spec]:
    rows = s.scalars(
        select(m.Fact).where(
            m.Fact.athlete_id == athlete_id, m.Fact.source == SOURCE, m.Fact.extractor == EXTRACTOR
        )
    ).all()
    return [
        sf.Spec(f.kind, dict(f.value or {}), f.value_text, f.season, f.valid_from, f.source_url)
        for f in rows
        if event_of(f.source_url) not in events_read
    ]


def _one_per_kind(specs: list[sf.Spec]) -> list[sf.Spec]:
    """Per kind the value with the newest data date."""
    best: dict[str, sf.Spec] = {}
    for spec in specs:
        old = best.get(spec.kind)
        if old is None or (spec.valid_from or date.min) > (old.valid_from or date.min):
            best[spec.kind] = spec
    return list(best.values())


def run(
    s: Session,
    client,
    events: list[HcEvent] | None = None,
    codes: set[str] | None = None,
    only_athlete_ids: set[int] | None = None,
) -> HcImport:
    """Fetch the rosters of `events` (default all) and import them."""
    events = list(events or EVENTS)
    report = HcImport(events=[e.key for e in events])
    rosters: list[HcRoster] = []
    for e in events:
        got, errors = client.rosters(e, codes)
        rosters.extend(got)
        report.errors.extend(errors)
    report.requests = getattr(client, "requests", 0)
    full = len(events) == len(EVENTS) and not codes
    return import_rosters(s, rosters, only_athlete_ids, report, full=full)


WATCH_DAYS_BEFORE = 21  # nightly: start reading the current event's rosters three weeks before it


def nightly(s: Session, settings, client=None, today: date | None = None) -> dict:
    """Nightly step "hockeycanada": re-read the rosters of the current event (WCh 2026) from three weeks before
    until the day after its end; ~11 requests. Only persons on those rosters are touched; their rows from older
    events stay. Off: HOCKEYCANADA_NIGHTLY=false."""
    from datetime import timedelta

    from klugscheisser.collectors.hockeycanada import HockeyCanadaClient

    today = today or date.today()
    if not settings.hockeycanada_nightly:
        return {"uebersprungen": "HOCKEYCANADA_NIGHTLY aus"}
    events = [
        e
        for e in EVENTS
        if e.current and e.start - timedelta(days=WATCH_DAYS_BEFORE) <= today <= e.end + timedelta(days=1)
    ]
    if not events:
        return {"uebersprungen": "kein laufendes Turnier"}
    client = client or HockeyCanadaClient(delay_s=settings.hockeycanada_delay_s)
    r = run(s, client, events)
    return {
        "kader": r.rosters,
        "leer": len(r.empty_rosters),
        "personen": r.persons,
        "ohne_person": len(r.unmatched),
        "neu": r.counts.get("facts_added", 0),
        "abrufe": r.requests,
        **({"fehler": r.errors[:5]} if r.errors else {}),
    }
