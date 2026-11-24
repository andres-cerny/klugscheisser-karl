"""Structured facts from Wikidata: one batched SPARQL query per ~50 persons (POST), stored as facts and career
stints with source "wikidata", the person's Wikidata URL as link, no quote (structured statement), confidence 1.

Identity is the QID stored on the person, so a fact never lands on a namesake. Teams of another sport
(P641 set and not ice hockey, e.g. a football club with the same town name) are skipped. Idempotent: values
already stored for the person are not added again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds.wiki import ioc_code
from klugscheisser.collectors.wikidata import ENDPOINT, USER_AGENT
from klugscheisser.db import models as m
from klugscheisser.facts.teams import TeamResolver, is_national_name, is_youth_name

log = logging.getLogger(__name__)

SOURCE = "wikidata"
CHUNK = 50
ICE_HOCKEY = "Q41466"
# kinds this importer writes that are not (yet) in facts.catalog.FACT_KINDS
EXTRA_KINDS = {"appearances": {"de": "Einsätze", "value": ["number", "team"]}}

FACTS_QUERY = """
SELECT ?p ?prop ?st ?val ?valLabel (SAMPLE(?en) AS ?valEn) (SAMPLE(?prec) AS ?precision)
       (SAMPLE(?start_) AS ?start) (SAMPLE(?startPrec_) AS ?startPrec)
       (SAMPLE(?end_) AS ?end) (SAMPLE(?endPrec_) AS ?endPrec)
       (SAMPLE(?num_) AS ?matches) (SAMPLE(?when_) AS ?when) (SAMPLE(?jersey_) AS ?jersey)
       (SAMPLE(?leagueLabel_) AS ?league) (SAMPLE(?majorLabel_) AS ?major) (SAMPLE(?sc_) AS ?sportCountry)
       (SAMPLE(?tc_) AS ?teamCountry) (SAMPLE(?rank_) AS ?ranking) (SAMPLE(?evLabel_) AS ?eventLabel)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?type_), "entity/"); separator="|") AS ?types)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?sport_), "entity/"); separator="|") AS ?sports) WHERE {
  VALUES ?p { %(values)s }
  {
    VALUES ?prop { "P569" "P19" "P413" "P423" "P1412" "P1449" "P166" "P647" "P69" "P54" "P1344" }
    ?pr wikibase:claim ?claim; wikibase:statementProperty ?ps .
    FILTER(STRAFTER(STR(?pr), "entity/") = ?prop)
    ?p ?claim ?st . ?st ?ps ?val .
    OPTIONAL { ?pr wikibase:statementValue ?psv . ?st ?psv ?node . ?node wikibase:timePrecision ?prec }
  }
  UNION { ?p p:P2048 ?st . ?st psn:P2048/wikibase:quantityAmount ?val . BIND("P2048" AS ?prop) }
  UNION { ?p p:P2067 ?st . ?st psn:P2067/wikibase:quantityAmount ?val . BIND("P2067" AS ?prop) }
  FILTER NOT EXISTS { ?st wikibase:rank wikibase:DeprecatedRank }
  OPTIONAL { ?val rdfs:label ?en FILTER(lang(?en) = "en") }
  OPTIONAL { ?st pqv:P580 ?sn . ?sn wikibase:timeValue ?start_; wikibase:timePrecision ?startPrec_ }
  OPTIONAL { ?st pqv:P582 ?enode . ?enode wikibase:timeValue ?end_; wikibase:timePrecision ?endPrec_ }
  OPTIONAL { ?st pq:P1350 ?num_ }
  OPTIONAL { ?st pq:P585 ?when_ }
  OPTIONAL { ?st pq:P1618 ?jersey_ }
  OPTIONAL { ?st pq:P118 ?lg . ?lg rdfs:label ?leagueLabel_ FILTER(lang(?leagueLabel_) = "en") }
  OPTIONAL { ?st pq:P812 ?mj . ?mj rdfs:label ?majorLabel_ FILTER(lang(?majorLabel_) IN ("de", "en")) }
  OPTIONAL { ?st pq:P1532/wdt:P298 ?sc_ }
  OPTIONAL { ?st pq:P1352 ?rank_ }
  OPTIONAL { ?st pq:P1344|pq:P805 ?ev_ . ?ev_ rdfs:label ?evLabel_ FILTER(lang(?evLabel_) = "de") }
  OPTIONAL { ?val wdt:P31 ?type_ }
  OPTIONAL { ?val wdt:P17/wdt:P298 ?tc_ }
  OPTIONAL { ?val wdt:P641 ?sport_ }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de,en,fr,it,sv,fi,cs,mul". }
}
GROUP BY ?p ?prop ?st ?val ?valLabel
"""

NATIONAL_TYPES = {"Q135747780", "Q1194951", "Q6979593"}  # women's national ice hockey team, national team
COLLEGE_TYPES = {"Q18558301", "Q2367225"}  # college sports team, university sports club
HANDED = {"Q10927615": "L", "Q10927630": "R"}
POSITION_DE = {"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"}
# v9: medals. "award received" (P166) of a medal item, or "participant in" (P1344) an Olympics / world
# championship with ranking (P1352) 1–3. Nothing else counts; a person without such a statement has no medal.
MEDAL_RX = re.compile(r"\b(gold|silver|bronze)\b.*\bmedal\b|\b(gold|silber|bronze)medaille\b", re.IGNORECASE)
METALS = {"gold": "gold", "silver": "silver", "silber": "silver", "bronze": "bronze"}
RANK_METAL = {1: "gold", 2: "silver", 3: "bronze"}
MEDAL_EVENT_RX = re.compile(r"olymp|world championship|weltmeisterschaft|championnat du monde", re.IGNORECASE)


@dataclass
class WikidataStats:
    persons: int = 0
    with_data: int = 0
    facts: int = 0
    stints: int = 0
    skipped_other_sport: int = 0
    errors: list[str] = field(default_factory=list)


def _v(row: dict, key: str) -> str | None:
    """Value of a SPARQL binding: raw JSON rows ({"value": …}) and WikiClient's flat rows both work."""
    value = row.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return value if value not in (None, "") else None


def _qid(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[-1] if uri else None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.lstrip("+")[:10])
    except ValueError:
        return None


def _label(row: dict) -> str | None:
    label = _v(row, "valLabel")
    if label and label == _qid(_v(row, "val")):
        return None  # no label in any of our languages
    return label


def medal_metal(*labels: str | None) -> str | None:
    """'Olympic bronze medal' / 'Olympische Bronzemedaille' -> 'bronze' (None for any other award)."""
    for label in labels:
        if label and (hit := MEDAL_RX.search(label)):
            return METALS[(hit.group(1) or hit.group(2)).lower()]
    return None


def season_from_date(d: date | None, precision: int | None) -> int | None:
    """Start of a stint → start year of its first season (a date from August on starts that year's season)."""
    if d is None:
        return None
    if precision is not None and precision < 10:  # year only: "2019" = from 2019/20
        return d.year
    return d.year if d.month >= 7 else d.year - 1


def season_to_date(d: date | None, precision: int | None) -> int | None:
    """End of a stint → start year of its last season ("2019" or April 2019 = last season 2018/19)."""
    if d is None:
        return None
    if precision is not None and precision < 10:
        return d.year - 1
    return d.year if d.month >= 9 else d.year - 1


def fetch_rows(wiki, qids: list[str]) -> list[dict]:
    """SPARQL rows for these QIDs. `wiki`: adds.wiki.WikiClient (POST, flat rows) or None for a plain POST
    to the public endpoint (raw bindings)."""
    query = FACTS_QUERY % {"values": " ".join(f"wd:{q}" for q in qids)}
    if wiki is not None:
        return wiki.sparql(query)
    else:
        import httpx

        r = httpx.post(
            ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT},
            timeout=120,
        )
        r.raise_for_status()
        payload = r.json()
    return payload["results"]["bindings"]


def import_wikidata_facts(session: Session, athletes: list[m.Athlete], wiki=None, rows=None) -> dict:
    """Import Wikidata facts and stints for these persons (those with a QID). `rows`: pre-fetched SPARQL
    bindings (tests / fixtures); otherwise fetched in chunks of CHUNK QIDs."""
    stats = WikidataStats()
    by_qid = {a.wikidata_qid: a for a in athletes if a.wikidata_qid}
    stats.persons = len(by_qid)
    if not by_qid:
        return stats.__dict__
    if rows is None:
        rows = []
        qids = list(by_qid)
        for i in range(0, len(qids), CHUNK):
            try:
                rows.extend(fetch_rows(wiki, qids[i : i + CHUNK]))
            except Exception as exc:  # one failed chunk must not stop the rest
                log.warning("wikidata facts chunk %d failed: %s", i // CHUNK, exc)
                stats.errors.append(str(exc)[:200])
    resolver = TeamResolver(session)
    seen_persons: set[str] = set()
    for row in rows:
        a = by_qid.get(_qid(_v(row, "p")))
        if a is None:
            continue
        seen_persons.add(a.wikidata_qid)
        _import_row(session, a, row, resolver, stats)
    stats.with_data = len(seen_persons)
    session.flush()
    return stats.__dict__


# --- one row --------------------------------------------------------------------------------------------


def _url(a: m.Athlete) -> str:
    return f"https://www.wikidata.org/wiki/{a.wikidata_qid}"


def _add_fact(
    session: Session,
    a: m.Athlete,
    kind: str,
    value: dict,
    value_text: str,
    stats: WikidataStats,
    season: int | None = None,
    valid_from: date | None = None,
) -> None:
    value = {k: v for k, v in value.items() if v not in (None, "")}
    existing = session.scalars(
        select(m.Fact).where(m.Fact.athlete_id == a.id, m.Fact.kind == kind, m.Fact.source == SOURCE)
    ).all()
    if any(f.value == value for f in existing):
        return
    session.add(
        m.Fact(
            athlete_id=a.id,
            kind=kind,
            value=value,
            value_text=value_text,
            season=season,
            valid_from=valid_from,
            source=SOURCE,
            source_url=_url(a),
            quote=None,
            confidence=1.0,
            status="confirmed",
            extractor=SOURCE,
        )
    )
    session.flush()
    stats.facts += 1


def _import_row(session: Session, a: m.Athlete, row: dict, resolver: TeamResolver, stats) -> None:
    from klugscheisser.sports import get_sport

    prop = _v(row, "prop")
    val = _v(row, "val")
    label = _label(row)
    en = _v(row, "valEn")
    precision = int(_v(row, "precision") or 0) or None
    when = _date(_v(row, "when"))
    if prop == "P569":
        d = _date(val)
        if d is None:
            return
        if precision is not None and precision < 11:  # year or month only
            _add_fact(session, a, "birth_date", {"year": d.year}, str(d.year), stats)
        else:
            _add_fact(session, a, "birth_date", {"date": d.isoformat()}, f"{d:%d.%m.%Y}", stats)
    elif prop == "P19" and label:
        _add_fact(session, a, "birth_place", {"text": label, "qid": _qid(val)}, label, stats)
    elif prop == "P2048" and val:
        try:
            cm = round(float(val) * 100)  # normalised value is in metres
        except ValueError:
            return
        if 120 <= cm <= 220:
            _add_fact(session, a, "height_cm", {"number": cm}, f"{cm} cm", stats)
    elif prop == "P2067" and val:  # mass, normalised to kilograms (2026-10-25)
        try:
            kg = round(float(val))
        except ValueError:
            return
        if 35 <= kg <= 130:
            _add_fact(session, a, "weight_kg", {"number": kg}, f"{kg} kg", stats)
    elif prop == "P413":
        try:
            spec = get_sport(a.sport.key) if a.sport else None
        except KeyError:
            spec = None
        code = spec.position_code(en or label) if spec else None
        labels = (spec.position_labels_de if spec else None) or POSITION_DE  # "Stürmer" for men's sports
        text = labels.get(code or "", label or en)
        if text:
            _add_fact(session, a, "position", {"text": text, "code": code}, text, stats)
    elif prop == "P423":
        hand = HANDED.get(_qid(val) or "")
        if hand:
            _add_fact(session, a, "shoots", {"text": hand}, "links" if hand == "L" else "rechts", stats)
    elif prop == "P1412" and label:
        _add_fact(session, a, "languages", {"text": label}, label, stats)
    elif prop == "P1449" and val:
        _add_fact(session, a, "nickname", {"text": val}, val, stats)
    elif prop == "P166" and label and (metal := medal_metal(en, label)):
        year = when.year if when else None
        _add_medal(session, a, metal, label, year, _v(row, "eventLabel"), stats)
    elif prop == "P166" and label:
        year = when.year if when else None
        text = f"{label} ({year})" if year else label
        _add_fact(session, a, "award", {"text": label, "year": year}, text, stats, season=None)
    elif prop == "P1344" and label:
        try:
            rank = int(float(_v(row, "ranking") or 0))
        except ValueError:
            rank = 0
        if rank in RANK_METAL and MEDAL_EVENT_RX.search(f"{en or ''} {label}"):
            year = when.year if when else _year_in(en or label)
            _add_medal(session, a, RANK_METAL[rank], label, year, label, stats)
    elif prop == "P647" and label:
        year = when.year if when else None
        team_id = resolver.resolve(en, label)
        text = f"Gedraftet von {label}" + (f" ({year})" if year else "")
        _add_fact(session, a, "draft", {"team": label, "year": year, "team_id": team_id}, text, stats)
    elif prop == "P69" and label:
        start, end = _date(_v(row, "start")), _date(_v(row, "end"))
        years = "–".join(str(d.year) for d in (start, end) if d) or None
        major = _v(row, "major")
        text = label + (f", {major}" if major else "") + (f" ({years})" if years else "")
        _add_fact(session, a, "college", {"text": label, "major": major, "years": years}, text, stats)
    elif prop == "P54" and (en or label):
        _import_stint(session, a, row, resolver, stats)


def _year_in(text: str | None) -> int | None:
    hit = re.search(r"\b(19|20)\d{2}\b", text or "")
    return int(hit.group(0)) if hit else None


def _add_medal(
    session: Session, a: m.Athlete, metal: str, label: str, year: int | None, event: str | None, stats
) -> None:
    text = event or label
    if year and str(year) not in text:
        text = f"{text} ({year})"
    value = {"text": text, "metal": metal, "year": year, "event": event}
    _add_fact(session, a, "medal", value, text, stats)


def _import_stint(session: Session, a: m.Athlete, row: dict, resolver: TeamResolver, stats) -> None:
    sports = set(filter(None, (_v(row, "sports") or "").split("|")))
    if sports and ICE_HOCKEY not in sports:
        stats.skipped_other_sport += 1  # e.g. a football club of the same town
        return
    types = set(filter(None, (_v(row, "types") or "").split("|")))
    en, label = _v(row, "valEn"), _label(row)
    name = en or label
    country = ioc_code(None, _v(row, "teamCountry"))
    sport_country = ioc_code(None, _v(row, "sportCountry"))
    if is_youth_name(name) or (label and is_youth_name(label)):
        kind = "youth"
    elif types & NATIONAL_TYPES or is_national_name(name) or (label and is_national_name(label)):
        kind = "national"
    elif types & COLLEGE_TYPES:
        kind = "college"
    else:
        kind = "club"
    start, end = _date(_v(row, "start")), _date(_v(row, "end"))
    season_from = season_from_date(start, int(_v(row, "startPrec") or 0) or None)
    season_to = season_to_date(end, int(_v(row, "endPrec") or 0) or None)
    if season_from is not None and season_to is not None and season_to < season_from:
        season_to = season_from  # joined and left within one season
    team_id = resolver.resolve(en, label, national=kind == "national", country=sport_country or country)
    league = _v(row, "league")
    stint = session.scalar(
        select(m.CareerStint).where(
            m.CareerStint.athlete_id == a.id,
            m.CareerStint.source == SOURCE,
            m.CareerStint.team_name == name,
            m.CareerStint.season_from.is_(None)
            if season_from is None
            else m.CareerStint.season_from == season_from,
        )
    )
    if stint is not None:  # rerun: take over corrected end / team link
        stint.season_to, stint.kind, stint.league = season_to, kind, league or stint.league
        stint.team_id = team_id or stint.team_id
    else:
        session.add(
            m.CareerStint(
                athlete_id=a.id,
                team_id=team_id,
                team_name=name,
                kind=kind,
                league=league,
                season_from=season_from,
                season_to=season_to,
                role="player",
                source=SOURCE,
                source_url=_url(a),
                quote=None,
                confidence=1.0,
                status="confirmed",
            )
        )
        session.flush()
        stats.stints += 1
    if jersey := _v(row, "jersey"):
        _add_fact(
            session,
            a,
            "jersey_number",
            {"number": jersey, "team": name},
            f"Nummer {jersey} ({name})",
            stats,
            season=season_from,
        )
    if matches := _v(row, "matches"):
        try:
            n = int(float(matches))
        except ValueError:
            return
        _add_fact(session, a, "appearances", {"number": n, "team": name}, f"{n} Spiele für {name}", stats)
