"""v4 P2: turn "add this team" (free text) into a sourced roster proposal incl. coaches and staff.

Order: interpret the request (LLM, else simple rules) -> our DB (team + stored event rosters, e.g. WCH 2026)
-> Wikidata team entity (search, P31/P641/P17, head coach P286, sitelinks) -> current roster section of the
Wikipedia article (en, then de): names, positions, numbers, birth dates, coaches/staff. Every person carries the
roster page (or Wikidata) as source. Rosters on Wikipedia can be outdated; the proposal says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.adds.base import (
    Proposal,
    ProposedPerson,
    ProposedTeam,
    SourceRef,
    estimate_load_cost_chf,
)
from klugscheisser.adds.person import find_existing
from klugscheisser.adds.wiki import (
    COUNTRIES,
    POSITION_ABBR,
    SPORTS,
    WikiClient,
    countries_in,
    ioc_code,
    parse_roster_html,
    pick_roster_section,
    sport_fields,
    sport_from_labels,
    sports_in,
    wiki_title,
    wiki_url,
)
from klugscheisser.collectors.profiles.wikipedia import home_languages
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo

log = logging.getLogger(__name__)

TEAM_QUERY = """
SELECT ?t ?labelEn ?labelDe ?descEn ?descDe ?enwiki ?dewiki
       (GROUP_CONCAT(DISTINCT ?instL; separator="|") AS ?instances)
       (GROUP_CONCAT(DISTINCT ?sportL; separator="|") AS ?sports)
       (GROUP_CONCAT(DISTINCT CONCAT(COALESCE(?ioc, ""), ":", COALESCE(?iso, "")); separator="|") AS ?nations)
       (GROUP_CONCAT(DISTINCT ?leagueL; separator="|") AS ?leagues)
       (GROUP_CONCAT(DISTINCT CONCAT(STRAFTER(STR(?coach), "entity/"), "::", COALESCE(?coachL, "")); separator="|")
        AS ?coaches) WHERE {
  VALUES ?t { %(values)s }
  OPTIONAL { ?t rdfs:label ?labelEn FILTER(lang(?labelEn)="en") }
  OPTIONAL { ?t rdfs:label ?labelDe FILTER(lang(?labelDe)="de") }
  OPTIONAL { ?t schema:description ?descEn FILTER(lang(?descEn)="en") }
  OPTIONAL { ?t schema:description ?descDe FILTER(lang(?descDe)="de") }
  OPTIONAL { ?t wdt:P31 ?inst . ?inst rdfs:label ?instL FILTER(lang(?instL)="en") }
  OPTIONAL { ?t wdt:P641 ?sp . ?sp rdfs:label ?sportL FILTER(lang(?sportL)="en") }
  OPTIONAL { ?t wdt:P17 ?c . OPTIONAL { ?c wdt:P984 ?ioc } OPTIONAL { ?c wdt:P298 ?iso } }
  OPTIONAL { ?t wdt:P118 ?league . ?league rdfs:label ?leagueL FILTER(lang(?leagueL)="en") }
  OPTIONAL { ?t p:P286 ?cs . ?cs ps:P286 ?coach . FILTER NOT EXISTS { ?cs pq:P582 ?ce }
             OPTIONAL { ?coach rdfs:label ?coachL FILTER(lang(?coachL)="en") } }
  OPTIONAL { ?enwiki schema:about ?t ; schema:isPartOf <https://en.wikipedia.org/> }
  OPTIONAL { ?dewiki schema:about ?t ; schema:isPartOf <https://de.wikipedia.org/> }
} GROUP BY ?t ?labelEn ?labelDe ?descEn ?descDe ?enwiki ?dewiki
"""

SPORT_EN = {
    "icehockey": "ice hockey",
    "ski_alpine": "alpine skiing",
    "floorball": "floorball",
    "football": "association football",
    "handball": "handball",
    "volleyball": "volleyball",
    "basketball": "basketball",
    "curling": "curling",
}

INTERPRET_SYSTEM = (
    "Du hilfst einer Sportmoderatorin, ein ganzes Team in eine Datenbank aufzunehmen. Lies ihre Anfrage "
    "(Deutsch oder Englisch) und gib zurück: query = Name des Teams/Vereins ohne Füllwörter (bei "
    "Nationalteams leer), kind = national oder club, country = IOC-Code (SUI, SWE, CAN, USA, …) oder leer, "
    "gender = female oder male (ohne Angabe female), sport = Sportart auf Englisch (ohne Angabe ice hockey)."
)


def _project_defaults() -> tuple[str, str]:
    """(gender, sport base) when a request names none: the project's sport (2026-11-24: men's floorball)."""
    from klugscheisser.sports import current_spec

    spec = current_spec()
    base = spec.key.removesuffix("_women").removesuffix("_men")
    return ("male" if spec.men else "female"), (base if base in SPORT_EN else "icehockey")


def _interpret_system() -> str:
    gender, base = _project_defaults()
    return INTERPRET_SYSTEM.replace("(ohne Angabe female)", f"(ohne Angabe {gender})").replace(
        "(ohne Angabe ice hockey)", f"(ohne Angabe {SPORT_EN[base]})"
    )


INTERPRET_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "kind": {"type": "string", "enum": ["national", "club"]},
        "country": {"type": "string"},
        "gender": {"type": "string", "enum": ["female", "male"]},
        "sport": {"type": "string"},
    },
    "required": ["query", "kind", "country", "gender", "sport"],
    "additionalProperties": False,
}

_NATIONAL = re.compile(r"national|\bnati\b|landesauswahl|nationalmannschaft", re.I)
_MALE = re.compile(r"\b(herren|männer|maenner|men'?s?|male)\b", re.I)
_FEMALE = re.compile(r"frauen|damen|women|ladies|female|\bdam\b", re.I)
_WOMEN_TEAM = re.compile(r"women|frauen|damen|\bdam\b|female|ladies|\(w\)", re.I)
_FILLER = re.compile(
    r"\b(füge|fuege|hinzu|hinzufügen|add|bitte|please|lade|load|das|die|den|der|dem|des|the|a|an|team|teams|"
    r"mannschaft|kader|roster|squad|frauen|damen|women'?s?|ladies|herren|männer|men'?s?|zu|to|und|and|alle|all|"
    r"spielerinnen|spieler|players|mit|with|inkl|inklusive|including|trainer\w*|staff|coaches|von|of|vom|"
    r"\w*nationalteam|\w*nationalmannschaft|national|nati|eishockey|ice|hockey)\b",
    re.I,
)
_REJECT = re.compile(r"\b(nein|nicht|falsch|andere[nrs]?|no|not|wrong)\b", re.I)


@dataclass
class TeamRequest:
    query: str
    kind: str = "club"  # national | club
    country: str | None = None
    gender: str = "female"
    sport: str = "ice hockey"


@dataclass
class TeamEntity:
    qid: str
    label: str
    label_de: str | None = None
    description: str | None = None
    instances: list[str] = field(default_factory=list)
    sports: list[str] = field(default_factory=list)
    nations: list[str] = field(default_factory=list)
    leagues: list[str] = field(default_factory=list)
    coaches: list[tuple[str, str]] = field(default_factory=list)  # (qid, label)
    enwiki: str | None = None
    dewiki: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"

    @property
    def name(self) -> str:
        return self.label_de or self.label

    @property
    def is_team(self) -> bool:
        return any(re.search(r"team|club", i, re.I) for i in self.instances)

    @property
    def is_national(self) -> bool:
        return any("national" in i for i in self.instances)

    @property
    def is_women(self) -> bool:
        return bool(_WOMEN_TEAM.search(" ".join([self.label, self.label_de or "", *self.instances])))

    def short_de(self) -> str:
        return f"{self.name} ({self.description or ', '.join(self.instances[:1])}) – Wikidata {self.qid}"


def parse_teams(rows: list[dict[str, str]]) -> dict[str, TeamEntity]:
    out: dict[str, TeamEntity] = {}
    for row in rows:
        qid = row["t"].rsplit("/", 1)[-1]
        t = out.get(qid)
        if t is None:
            t = out[qid] = TeamEntity(qid=qid, label=row.get("labelEn") or row.get("labelDe") or qid)
        t.label_de = t.label_de or row.get("labelDe")
        t.description = t.description or row.get("descDe") or row.get("descEn")
        t.enwiki = t.enwiki or row.get("enwiki")
        t.dewiki = t.dewiki or row.get("dewiki")
        for key, target in (("instances", t.instances), ("sports", t.sports), ("leagues", t.leagues)):
            for value in (row.get(key) or "").split("|"):
                if value and value not in target:
                    target.append(value)
        for value in (row.get("nations") or "").split("|"):
            ioc, _, iso = value.partition(":")
            if (code := ioc_code(ioc or None, iso or None)) and code not in t.nations:
                t.nations.append(code)
        for value in (row.get("coaches") or "").split("|"):
            cq, _, label = value.partition("::")
            if cq and label and (cq, label) not in t.coaches:
                t.coaches.append((cq, label))
    return out


def interpret_rules(text: str) -> TeamRequest:
    """Fallback without LLM: keywords for kind, country, gender, sport; the rest is the team name."""
    countries = countries_in(text)
    sports = sports_in(text)
    national = bool(_NATIONAL.search(text))
    query = text
    if national:
        for ioc in countries:
            query = re.sub(rf"\b\w*(?:{COUNTRIES[ioc][2]})\w*", " ", query, flags=re.I)
    query = _FILLER.sub(" ", query)
    query = " ".join(re.sub(r"[^\w\s.\-']", " ", query).split()).strip(" -.")
    default_gender, default_base = _project_defaults()
    base = next((s.base for s in sports if s.base in SPORT_EN), default_base)
    if default_gender == "male":  # men's project: female only when the text says so
        gender = "female" if _FEMALE.search(text) and not _MALE.search(text) else "male"
    else:
        gender = "male" if _MALE.search(text) and not _FEMALE.search(text) else "female"
    return TeamRequest(
        query="" if national else query,
        kind="national" if national else "club",
        country=countries[0] if countries else None,
        gender=gender,
        sport=SPORT_EN[base],
    )


def interpret(text: str, llm: Any = None) -> TeamRequest:
    if llm is not None:
        try:
            data = llm.complete_json(_interpret_system(), text, INTERPRET_SCHEMA)
            if data.get("kind") in ("national", "club"):
                country = (data.get("country") or "").strip().upper() or None
                return TeamRequest(
                    query=(data.get("query") or "").strip(),
                    kind=data["kind"],
                    country=country if country and len(country) == 3 else None,
                    gender="male" if data.get("gender") == "male" else "female",
                    sport=(data.get("sport") or SPORT_EN[_project_defaults()[1]]).strip().lower(),
                )
        except Exception as exc:
            log.warning("team request interpretation failed, using rules: %s", exc)
    return interpret_rules(text)


def _position(code: str | None, lang: str = "en") -> str | None:
    if not code:
        return None
    up = code.strip().upper()
    if up in ("G", "D", "F"):
        return up
    if up in POSITION_ABBR.get(lang, {}):
        return POSITION_ABBR[lang][up]
    if up in ("C", "LW", "RW", "W", "F/D"):
        return "F"
    if up in ("GK", "GOALIE"):
        return "G"
    from klugscheisser.sports import current_spec

    return current_spec().position_code(code)  # 2026-11-24: the project's sport


_UNSET: Any = object()


class TeamRosterResolver:
    """TeamResolver: DB rosters, Wikidata team entity, Wikipedia roster section."""

    def __init__(self, settings: Settings, wiki: WikiClient | None = None, llm: Any = _UNSET) -> None:
        self.settings = settings
        self.wiki = wiki or WikiClient()
        if llm is _UNSET:
            from klugscheisser.llm.providers import get_llm

            llm = get_llm(settings)
        self.llm = llm

    # --- finding the team ---------------------------------------------------------------------------------

    def db_team(self, session: Session, req: TeamRequest) -> m.Team | None:
        if req.kind == "national" and req.country:
            return session.scalar(
                select(m.Team)
                .where(m.Team.kind == "national", m.Team.country == req.country)
                .order_by(m.Team.id)
            )
        return repo.find_team(session, req.query) if req.query else None

    def db_roster(self, session: Session, team: m.Team) -> tuple[list[m.EventEntry], m.Event | None]:
        """Entries of the newest event with a stored roster for this team."""
        row = session.execute(
            select(m.EventEntry.event_id, func.max(m.EventEntry.fetched_at).label("at"))
            .where(m.EventEntry.team_id == team.id)
            .group_by(m.EventEntry.event_id)
            .order_by(func.max(m.EventEntry.fetched_at).desc())
            .limit(1)
        ).first()
        if row is None:
            return [], None
        entries = session.scalars(
            select(m.EventEntry)
            .where(m.EventEntry.team_id == team.id, m.EventEntry.event_id == row.event_id)
            .order_by(m.EventEntry.jersey_number)
        ).all()
        return list(entries), session.get(m.Event, row.event_id)

    def wikidata_team(
        self, req: TeamRequest, excluded: set[str], notes: list[str]
    ) -> tuple[TeamEntity | None, list]:
        if req.kind == "national":
            if not req.country or req.country not in COUNTRIES:
                return None, []
            gender = "women's" if req.gender == "female" else "men's"
            queries = [(f"{COUNTRIES[req.country][0]} {gender} national {req.sport} team", ("en",))]
        else:
            if not req.query:
                return None, []
            queries = [(req.query, ("de", "en"))]
        try:
            teams = self._search_teams(queries)
            best = self._best_team(req, teams, excluded)
            if best is not None and req.kind == "club" and req.gender == "female" and not best.is_women:
                # the women's section is often a separate item found only with the club's full name
                for qid, t in self._search_teams([(best.label, ("en",))]).items():
                    teams.setdefault(qid, t)
                best = self._best_team(req, teams, excluded)
        except Exception as exc:
            log.warning("wikidata team lookup failed: %s", exc)
            notes.append("Wikidata war nicht erreichbar.")
            return None, []
        others = [t for t in teams.values() if t is not best and t.is_team]
        return best, others

    def _search_teams(self, queries: list[tuple[str, tuple[str, ...]]]) -> dict[str, TeamEntity]:
        qids: list[str] = []
        for query, langs in queries:
            for hit in self.wiki.search_entities(query, languages=langs, limit=8):
                if hit["id"] not in qids:
                    qids.append(hit["id"])
        if not qids:
            return {}
        rows = self.wiki.sparql(TEAM_QUERY % {"values": " ".join(f"wd:{q}" for q in qids[:12])})
        found = parse_teams(rows)
        return {q: found[q] for q in qids if q in found}

    def _best_team(
        self, req: TeamRequest, teams: dict[str, TeamEntity], excluded: set[str]
    ) -> TeamEntity | None:
        best, best_score = None, 0
        for t in teams.values():
            if t.qid in excluded or not t.is_team:
                continue
            if t.sports and not any(req.sport in s.lower() for s in t.sports):
                continue
            if req.country and t.nations and req.country not in t.nations:
                continue
            if (req.kind == "national") != t.is_national:
                continue
            score = 10
            if req.gender == "female":
                score += 5 if t.is_women else 0
                if re.search(r"\bmen'?s\b|herr", " ".join([t.label, *t.instances]), re.I):
                    continue
            if t.enwiki or t.dewiki:
                score += 2
            if score > best_score:
                best, best_score = t, score
        return best

    # --- roster -------------------------------------------------------------------------------------------

    def roster_languages(self, country: str | None) -> list[str]:
        """Home language(s) of the team's country first, then en, de (a Swiss team: de, fr, it, en)."""
        order: list[str] = []
        for lang in (*home_languages(country), "en", "de"):
            if lang not in order:
                order.append(lang)
        return order

    def wikipedia_roster(self, team: TeamEntity, country: str | None = None):
        """(roster, SourceRef, section title, language): the most current roster section among the team's
        articles in the home language(s), en and de. Equal currency -> home language first; an undated
        home-language section loses against an English 'Current roster'."""
        country = country or (team.nations[0] if team.nations else None)
        languages = self.roster_languages(country)
        titles = {"en": wiki_title(team.enwiki), "de": wiki_title(team.dewiki)}
        missing = [lang for lang in languages if not titles.get(lang)]
        if missing:
            try:
                titles.update(self.wiki.sitelinks(team.qid, missing))
            except Exception as exc:
                log.warning("wikidata sitelinks of %s failed: %s", team.qid, exc)
        found = []  # (score, -language rank, lang, resolved title, section)
        for rank, lang in enumerate(languages):
            title = titles.get(lang)
            if not title:
                continue
            try:
                resolved, sections = self.wiki.sections(lang, title)
            except Exception as exc:
                log.warning("wikipedia sections %s:%s failed: %s", lang, title, exc)
                continue
            if (section := pick_roster_section(sections)) is not None:
                found.append((section["score"], -rank, lang, resolved, section))
        found.sort(key=lambda f: (f[0], f[1]), reverse=True)
        for _, _, lang, resolved, section in found:
            try:
                roster = parse_roster_html(
                    self.wiki.section_html(lang, resolved, section["index"]),
                    lambda label, lang=lang: _position(label, lang),
                )
            except Exception as exc:
                log.warning("wikipedia roster %s:%s failed: %s", lang, resolved, exc)
                continue
            if roster.rows:
                anchor = section.get("anchor") or section["line"].replace(" ", "_")
                source = SourceRef(
                    f"Wikipedia ({lang}): {resolved} – {section['line']}",
                    f"{wiki_url(lang, resolved)}#{anchor}",
                )
                return roster, source, section["line"], lang
        return None, None, None, None

    # --- propose ------------------------------------------------------------------------------------------

    def propose(
        self,
        session: Session,
        request: str,
        refine: str | None = None,
        previous: Proposal | None = None,
        include_staff: bool = True,
    ) -> Proposal:
        text = " ".join(x for x in (request.strip(), (refine or "").strip()) if x)
        req = interpret(text, self.llm)
        excluded: set[str] = set()
        if previous and previous.team and previous.team.wikidata_qid and refine and _REJECT.search(refine):
            excluded.add(previous.team.wikidata_qid)
        notes: list[str] = []
        female = req.gender == "female"
        base = _project_defaults()[1]
        sport_info = sport_from_labels([req.sport]) or next(s for s in SPORTS if s.base == base)
        sport_key, sport_name, noun = sport_fields(sport_info, female)

        db_team = self.db_team(session, req)
        if db_team is not None:
            entries, event = self.db_roster(session, db_team)
            if entries and (not previous or not excluded):
                return self._from_db(db_team, entries, event, sport_key, sport_name, noun, include_staff)

        entity, others = self.wikidata_team(req, excluded, notes)
        alternatives = [t.short_de() for t in others[:5]]
        what = self._describe_request(req)
        if entity is None or (req.kind == "club" and female and not entity.is_women):
            summary = f"Kein passendes Team gefunden für «{request.strip()}» ({what})."
            if entity is not None:
                summary += f" Gefunden wurde nur {entity.short_de()} – ohne eigenes Frauenteam."
            summary += " Bitte präzisieren (Vereinsname, Land, Frauen/Männer)."
            return Proposal(kind="team", found=False, summary=summary, alternatives=alternatives, notes=notes)

        team = ProposedTeam(
            name=entity.name,
            kind="national" if entity.is_national else "club",
            country=req.country or (entity.nations[0] if entity.nations else None),
            league=entity.leagues[0] if entity.leagues else None,
            sport_key=sport_key,
            wikidata_qid=entity.qid,
            existing_id=db_team.id if db_team is not None else None,
        )
        roster, source, section, lang = self.wikipedia_roster(entity, team.country)
        if roster is None:
            return Proposal(
                kind="team",
                found=False,
                summary=f"Team gefunden: {entity.short_de()}, aber kein Kader auf Wikipedia "
                f"({', '.join(self.roster_languages(team.country))}). "
                "Ohne Kader kann ich niemanden vorschlagen.",
                team=team,
                alternatives=alternatives,
                notes=notes,
            )
        team.roster_source = source
        team_source = SourceRef(f"Wikidata {entity.qid}", entity.url)
        persons: list[ProposedPerson] = []
        for row in roster.rows:
            if row.role != "athlete" and not include_staff:
                continue
            persons.append(
                ProposedPerson(
                    name=row.name,
                    role=row.role,
                    function_de=_function(row.function_de, female) if row.role != "athlete" else None,
                    sport_key=sport_key,
                    sport_name_de=sport_name,
                    person_de=noun,
                    nationality=row.nationality or (team.country if team.kind == "national" else None),
                    birth_date=row.birth_date,
                    team=row.team if team.kind == "national" and row.team else team.name,
                    position=row.position if row.role == "athlete" else None,
                    jersey_number=row.number,
                    wikipedia_url=wiki_url(lang, row.wikipedia_title) if row.wikipedia_title else None,
                    sources=[source],
                )
            )
        if include_staff:
            known = {p.name.lower() for p in persons}
            for qid, label in entity.coaches:
                if label.lower() not in known:
                    persons.append(
                        ProposedPerson(
                            name=label,
                            role="coach",
                            function_de="Cheftrainer/in",
                            sport_key=sport_key,
                            sport_name_de=sport_name,
                            person_de=noun,
                            team=team.name,
                            wikidata_qid=qid,
                            sources=[team_source],
                        )
                    )
                else:  # roster names the coach too: add Wikidata as second source
                    for p in persons:
                        if p.name.lower() == label.lower():
                            p.wikidata_qid = p.wikidata_qid or qid
                            p.sources.append(team_source)
        cap = self.settings.add_max_team_persons
        if len(persons) > cap:
            notes.append(f"Nur die ersten {cap} von {len(persons)} Personen werden übernommen (Obergrenze).")
            persons = persons[:cap]
        for p in persons:
            existing = find_existing(session, p.wikidata_qid, p.name, p.birth_date)
            if existing is not None:
                p.existing_id = existing.id
        stand = f"Stand {roster.as_of}" if roster.as_of else "Stand unklar"
        notes.insert(0, f"Kader von Wikipedia («{section}»), {stand} – kann veraltet sein.")
        if not include_staff:
            notes.append("Trainer und Staff nicht berücksichtigt.")
        return self._proposal(team, persons, source, section, stand, notes, alternatives, female)

    def _describe_request(self, req: TeamRequest) -> str:
        who = "Frauen" if req.gender == "female" else "Männer"
        if req.kind == "national":
            land = COUNTRIES.get(req.country or "", (None, req.country or "Land unklar"))[1]
            return f"Nationalteam {land}, {who}, {req.sport}"
        return f"Verein «{req.query}», {who}, {req.sport}"

    def _from_db(self, team, entries, event, sport_key, sport_name, noun, include_staff) -> Proposal:
        persons = []
        for e in entries:
            a = e.athlete
            if a.role != "athlete" and not include_staff:
                continue
            persons.append(
                ProposedPerson(
                    name=a.full_name,
                    role=a.role or "athlete",
                    sport_key=a.sport.key if a.sport else sport_key,
                    sport_name_de=sport_name,
                    person_de=noun,
                    nationality=a.nationality,
                    birth_date=a.birth_date,
                    team=team.name,
                    position=a.position or e.role,
                    jersey_number=e.jersey_number,
                    wikidata_qid=a.wikidata_qid,
                    sources=[SourceRef(e.source or "Kader in unserer Datenbank", e.source_url)],
                    existing_id=a.id,
                )
            )
        source = SourceRef(entries[0].source or "Kader in unserer Datenbank", entries[0].source_url)
        proposed = ProposedTeam(
            name=team.name,
            kind=team.kind,
            country=team.country,
            league=team.league,
            sport_key=sport_key,
            existing_id=team.id,
            roster_source=source,
        )
        section = event.name if event else "gespeicherter Kader"
        notes = [
            f"Kader aus unserer Datenbank ({section}); alle Personen sind schon erfasst – es werden nur fehlende Daten ergänzt."
        ]
        female = not (sport_key or "").endswith("_men")  # 2026-11-24: men's teams say "Spieler"
        return self._proposal(proposed, persons, source, section, "bereits geladen", notes, [], female)

    def _proposal(self, team, persons, source, section, stand, notes, alternatives, female) -> Proposal:
        players = [p for p in persons if p.role == "athlete"]
        staff = [p for p in persons if p.role != "athlete"]
        new = [p for p in persons if p.existing_id is None]
        estimate = estimate_load_cost_chf(self.settings, len(new), len(new))
        player_word = (
            ("Spielerin" if female else "Spieler")
            if len(players) == 1
            else ("Spielerinnen" if female else "Spieler")
        )
        sample = ", ".join(p.name for p in players[:5])
        coach = next((p for p in staff if p.role == "coach"), None)
        summary = (
            f"{team.name}"
            + (f" ({team.country})" if team.country else "")
            + f": Kader aus {source.label} ({stand}). {len(players)} {player_word}, {len(staff)} Trainer/Staff."
        )
        if sample:
            summary += f" Zum Beispiel: {sample}."
        if coach:
            summary += f" {coach.function_de or 'Trainer/in'}: {coach.name}."
        if len(new) < len(persons):
            summary += f" {len(persons) - len(new)} davon sind schon in unserer Datenbank."
        summary += f" Geschätzte Ladekosten: höchstens CHF {estimate:.2f}"
        summary += " (falls alle Instagram haben)." if new else "."
        if estimate > self.settings.add_cost_cap_chf:
            notes.append(
                f"Schätzung über dem Kostendeckel von CHF {self.settings.add_cost_cap_chf:.2f} – "
                "das Laden stoppt beim Deckel."
            )
        return Proposal(
            kind="team",
            found=bool(persons),
            summary=summary,
            persons=persons,
            team=team,
            alternatives=alternatives,
            notes=notes,
            cost_estimate_chf=estimate,
        )


def _function(function_de: str | None, female: bool) -> str | None:
    """'Cheftrainer/in' -> 'Cheftrainer' for men's teams; for women's teams the staff gender is unknown."""
    if function_de and not female:
        return function_de.replace("/in", "")
    return function_de
