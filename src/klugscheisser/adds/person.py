"""v4 P2: turn "add this person" (name, optional Instagram handle, Fabi's details) into a sourced proposal.

Candidates come from Wikidata (search in de + en, then one SPARQL query for all candidates: human, gender,
occupation, sport, citizenship, birth date, current team, position, Instagram, EliteProspects, sitelinks) and
the Wikipedia summary of the top candidates. Jev decides which candidate is the person Fabi means; without
Jev only a single exact-name candidate that does not contradict her details is accepted. Namesakes are never
guessed: they end up in `alternatives` and Fabi is asked to be more precise.

Brave web search runs only when Wikidata has nothing fitting; its titles are hints in `notes`, never facts.
Fabi's Instagram handle is described (Apify profile check when configured), not verified: the loader adds it
as a candidate with evidence "von Fabi angegeben".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds.base import Proposal, ProposedPerson, SourceRef, estimate_load_cost_chf
from klugscheisser.adds.wiki import (
    COUNTRIES,
    SPORTS,
    SportInfo,
    WikiClient,
    countries_in,
    date_de,
    ioc_code,
    is_coach_label,
    parse_date,
    sport_fields,
    sport_from_labels,
    sports_in,
    wiki_title,
    year_in,
)
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.names import name_variants, normalize
from klugscheisser.ingest import costs

log = logging.getLogger(__name__)

FIT_THRESHOLD = 0.7  # Jev probability needed to accept a candidate
JEV_USD_PER_M_TOKENS = 0.042  # same rate as the answer engine
HUMAN = "Q5"
FEMALE = "Q6581072"
MALE = "Q6581097"
FABI_ONLY = "Keine unabhängige Quelle gefunden – nur Angaben von Fabi"
NONE_OPTION = "keiner"

ENTITY_QUERY = """
SELECT ?p ?labelDe ?labelEn ?descDe ?descEn ?gender ?birth ?birthPrec ?ep ?dewiki ?enwiki
       (GROUP_CONCAT(DISTINCT STR(?inst); separator="|") AS ?instances)
       (GROUP_CONCAT(DISTINCT ?occL; separator="|") AS ?occupations)
       (GROUP_CONCAT(DISTINCT ?sportL; separator="|") AS ?sports)
       (GROUP_CONCAT(DISTINCT CONCAT(COALESCE(?ioc, ""), ":", COALESCE(?iso, "")); separator="|") AS ?nations)
       (GROUP_CONCAT(DISTINCT ?posL; separator="|") AS ?positions)
       (GROUP_CONCAT(DISTINCT ?posDe; separator="|") AS ?positionsDe)
       (GROUP_CONCAT(DISTINCT ?ig; separator="|") AS ?instagram)
       (GROUP_CONCAT(DISTINCT ?teamInfo; separator="|") AS ?teams) WHERE {
  VALUES ?p { %(values)s }
  OPTIONAL { ?p rdfs:label ?labelDe FILTER(lang(?labelDe)="de") }
  OPTIONAL { ?p rdfs:label ?labelEn FILTER(lang(?labelEn)="en") }
  OPTIONAL { ?p schema:description ?descDe FILTER(lang(?descDe)="de") }
  OPTIONAL { ?p schema:description ?descEn FILTER(lang(?descEn)="en") }
  OPTIONAL { ?p wdt:P31 ?inst }
  OPTIONAL { ?p wdt:P21 ?gender }
  OPTIONAL { ?p wdt:P106 ?occ . ?occ rdfs:label ?occL FILTER(lang(?occL)="en") }
  OPTIONAL { ?p wdt:P641 ?sport . ?sport rdfs:label ?sportL FILTER(lang(?sportL)="en") }
  OPTIONAL { ?p wdt:P27 ?cit . OPTIONAL { ?cit wdt:P984 ?ioc } OPTIONAL { ?cit wdt:P298 ?iso } }
  OPTIONAL { ?p p:P569/psv:P569 ?bn . ?bn wikibase:timeValue ?birth ; wikibase:timePrecision ?birthPrec }
  OPTIONAL { ?p wdt:P413 ?pos .
             OPTIONAL { ?pos rdfs:label ?posL FILTER(lang(?posL)="en") }
             OPTIONAL { ?pos rdfs:label ?posDe FILTER(lang(?posDe)="de") } }
  OPTIONAL { ?p wdt:P2003 ?ig }
  OPTIONAL { ?p wdt:P2481 ?ep }
  OPTIONAL { ?p p:P54 ?st . ?st ps:P54 ?team . FILTER NOT EXISTS { ?st pq:P582 ?end }
             OPTIONAL { ?st pq:P580 ?start }
             OPTIONAL { ?team rdfs:label ?teamDe FILTER(lang(?teamDe)="de") }
             OPTIONAL { ?team rdfs:label ?teamEn FILTER(lang(?teamEn)="en") }
             BIND(CONCAT(STRAFTER(STR(?team), "entity/"), "::", COALESCE(?teamDe, ?teamEn, ""), "::",
                         COALESCE(STR(?start), "")) AS ?teamInfo) }
  OPTIONAL { ?dewiki schema:about ?p ; schema:isPartOf <https://de.wikipedia.org/> }
  OPTIONAL { ?enwiki schema:about ?p ; schema:isPartOf <https://en.wikipedia.org/> }
} GROUP BY ?p ?labelDe ?labelEn ?descDe ?descEn ?gender ?birth ?birthPrec ?ep ?dewiki ?enwiki
"""

# v5 P4: teams a person coaches. Three ways Wikidata records it: head coach (P286) on the team without end
# date; "coach of sports team" (P6087) on the person without end date; "participant in" (P1344) an event with
# qualifiers team (P54) and role (P2868) "... coach" (how Colin Muller is linked to the Swiss women's team).
COACHED_QUERY = """
SELECT ?p ?team ?teamEn ?teamDe ?cls ?natInst ?ioc ?role ?when WHERE {
  VALUES ?p { %(values)s }
  { ?team p:P286 ?s . ?s ps:P286 ?p . FILTER NOT EXISTS { ?s pq:P582 ?e } BIND("head coach" AS ?role)
    OPTIONAL { ?s pq:P580 ?when } }
  UNION
  { ?p p:P6087 ?s . ?s ps:P6087 ?team . FILTER NOT EXISTS { ?s pq:P582 ?e }
    OPTIONAL { ?s pq:P39|pq:P2868 ?r . ?r rdfs:label ?role FILTER(lang(?role)="en") }
    OPTIONAL { ?s pq:P580 ?when } }
  UNION
  { ?p p:P1344 ?s . ?s ps:P1344 ?ev ; pq:P54 ?team ; pq:P2868 ?r .
    ?r rdfs:label ?role FILTER(lang(?role)="en" && CONTAINS(LCASE(?role), "coach"))
    OPTIONAL { ?ev wdt:P585|wdt:P580 ?when } }
  OPTIONAL { ?team rdfs:label ?teamEn FILTER(lang(?teamEn)="en") }
  OPTIONAL { ?team rdfs:label ?teamDe FILTER(lang(?teamDe)="de") }
  OPTIONAL { ?team wdt:P2094 ?c . ?c rdfs:label ?cls FILTER(lang(?cls)="en") }
  OPTIONAL { ?team wdt:P31 ?i . ?i rdfs:label ?natInst
             FILTER(lang(?natInst)="en" && CONTAINS(?natInst, "national")) }
  OPTIONAL { ?team wdt:P17 ?cc . ?cc wdt:P984 ?ioc }
}
"""

_DISAMBIGUATION = re.compile(r"begriffskl|disambiguation|wikimedia", re.I)
_REJECT = re.compile(r"\b(nein|nicht|falsch|andere[nrs]?|no|not|wrong)\b", re.I)
# words about the person herself ("die Schwedin", "Spielerin"), not about a team ("Trainer der Frauen")
_FEMALE_WORDS = re.compile(
    r"\b(sie|she|her|frau|woman)\b|\w+(?:erin|istin|etin|edin|nnin|länderin|ländin)\b", re.I
)
_MALE_WORDS = re.compile(
    r"\b(er|he|his|mann|man)\b|\w+(?:spieler|fahrer|läufer|trainer|schwede|kanadier)\b", re.I
)
_WOMEN_TEAM = re.compile(r"frauen|damen|women|ladies|female", re.I)
_NATIONAL_TEAM = re.compile(r"national|nationalmannschaft|nationalteam", re.I)
_HANDLE = re.compile(r"^(?:https?://(?:www\.)?instagram\.com/)?@?([A-Za-z0-9._]{1,30})/?$")


def clean_handle(raw: str | None) -> str | None:
    """'@hanna.olsson', 'instagram.com/hanna.olsson/' -> 'hanna.olsson'."""
    if not raw or not raw.strip():
        return None
    m_ = _HANDLE.match(raw.strip())
    return m_.group(1).lower() if m_ else None


@dataclass
class CoachedTeam:
    """A team the person coaches (or coached at an event), from Wikidata."""

    qid: str
    label: str  # English label
    label_de: str | None = None
    role: str = "coach"  # English role label: "head coach", "assistant coach"
    when: str = ""  # ISO date of the statement / event, "" when unknown
    women: bool = False
    national: bool = False
    nation: str | None = None  # IOC code of the team's country

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"

    def function_de(self, female: bool | None) -> str:
        """'Cheftrainer Frauen-Nationalteam Schweiz', 'Assistenztrainer/in Frauen ZSC Lions Frauen'."""
        low = self.role.lower()
        stem = "Cheftrainer" if "head" in low else "Assistenztrainer" if "assist" in low else "Trainer"
        stem += {True: "in", False: ""}.get(female, "/in")
        if self.national and self.nation:
            land = COUNTRIES.get(self.nation, (None, self.nation))[1]
            team = f"{'Frauen-' if self.women else ''}Nationalteam {land}"
        else:
            team = (("Frauen " if self.women else "") + (self.label_de or self.label)).strip()
        return f"{stem} {team}"


_WOMEN_LABEL = re.compile(
    r"women|frauen|damen|female|ladies|\bdam\b|naisten|ženy|ženská|femminile|féminin", re.I
)


def parse_coached(rows: list[dict[str, str]]) -> dict[str, list[CoachedTeam]]:
    """COACHED_QUERY rows -> {person QID: [teams, newest first]}; rows without a team are ignored."""
    out: dict[str, dict[str, CoachedTeam]] = {}
    for row in rows:
        if "team" not in row or "p" not in row:
            continue
        pqid, tqid = row["p"].rsplit("/", 1)[-1], row["team"].rsplit("/", 1)[-1]
        teams = out.setdefault(pqid, {})
        t = teams.get(tqid)
        if t is None:
            t = teams[tqid] = CoachedTeam(qid=tqid, label=row.get("teamEn") or row.get("teamDe") or tqid)
        t.label_de = t.label_de or row.get("teamDe")
        if row.get("role") and (t.role == "coach" or "head" in row["role"].lower()):
            t.role = row["role"]
        t.when = max(t.when, (row.get("when") or "")[:10])
        t.women = t.women or bool(
            _WOMEN_LABEL.search(" ".join([t.label, t.label_de or "", row.get("cls") or ""]))
        )
        t.national = t.national or bool(row.get("natInst"))
        t.nation = t.nation or row.get("ioc")
    return {p: sorted(ts.values(), key=lambda t: t.when, reverse=True) for p, ts in out.items()}


@dataclass
class Candidate:
    """One Wikidata person with the facts we need for the proposal and the fit check."""

    qid: str
    name: str
    labels: set[str] = field(default_factory=set)
    description: str | None = None
    human: bool = False
    gender: str | None = None
    occupations: list[str] = field(default_factory=list)
    sports: list[str] = field(default_factory=list)
    nations: list[str] = field(default_factory=list)  # IOC codes
    birth_date: date | None = None  # only with day precision
    birth_year: int | None = None
    positions: list[str] = field(default_factory=list)
    positions_de: list[str] = field(default_factory=list)
    instagram: list[str] = field(default_factory=list)
    eliteprospects_id: str | None = None
    teams: list[tuple[str, str, str]] = field(default_factory=list)  # (qid, label, start)
    dewiki: str | None = None
    enwiki: str | None = None
    wiki_extract: str | None = None
    wiki_url: str | None = None
    wiki_lang: str | None = None
    coached: list[CoachedTeam] = field(default_factory=list)  # newest first

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"

    @property
    def female(self) -> bool | None:
        return {FEMALE: True, MALE: False}.get(self.gender or "")

    @property
    def sport(self) -> SportInfo | None:
        return sport_from_labels(self.occupations + self.sports)

    @property
    def is_coach(self) -> bool:
        return bool(self.coached) or any(is_coach_label(o) for o in self.occupations)

    @property
    def coached_team(self) -> CoachedTeam | None:
        """The newest coached team. A women's team coached currently or within the last 4 years wins, so the
        sport category follows that team (a man coaching a women's team belongs to women's ice hockey)."""
        if not self.coached:
            return None
        recent = str(date.today().year - 4)
        return next(
            (t for t in self.coached if t.women and (not t.when or t.when >= recent)), self.coached[0]
        )

    @property
    def is_player(self) -> bool:
        sport = self.sport
        return sport is not None and any(
            re.search(sport.label_pattern, o, re.I) and not is_coach_label(o) for o in self.occupations
        )

    def current_team(self) -> str | None:
        """Newest current club; a national team only when there is no club."""
        teams = sorted(self.teams, key=lambda t: t[2], reverse=True)
        clubs = [t for t in teams if t[1] and not _NATIONAL_TEAM.search(t[1])]
        pick = (clubs or [t for t in teams if t[1]] or [None])[0]
        return pick[1] if pick else None

    def exact_name(self, name: str) -> bool:
        wanted = name_variants(name)
        return any(normalize(label) in wanted or wanted & name_variants(label) for label in self.labels)

    def short_de(self) -> str:
        """'Hanna Olsson (SWE), Eishockeyspielerin, geb. 1999, Frölunda HC – Wikidata Q19972549'."""
        _, _, noun = sport_fields(self.sport, self.female is not False)
        parts = [f"{self.name} ({'/'.join(self.nations)})" if self.nations else self.name]
        coached = self.coached_team
        if coached:
            parts.append(coached.function_de(self.female))
        elif noun:
            parts.append(("Trainer/in " if self.is_coach and not self.is_player else "") + noun)
        elif self.description:
            parts.append(self.description)
        if self.birth_year:
            parts.append(f"geb. {self.birth_year}")
        # P54 lists teams played for: for a coach that is the playing career, not a current team
        if not coached and (team := self.current_team()):
            parts.append(team)
        return f"{', '.join(parts)} – Wikidata {self.qid}"

    def long_de(self) -> str:
        text = self.short_de()
        if self.description:
            text += f". Wikidata-Beschreibung: {self.description}"
        if self.wiki_extract:
            text += f". Wikipedia: {self.wiki_extract[:600]}"
        return text


def parse_entities(rows: list[dict[str, str]]) -> dict[str, Candidate]:
    """SPARQL rows (one or more per person) -> candidates by QID."""
    out: dict[str, Candidate] = {}
    for row in rows:
        qid = row["p"].rsplit("/", 1)[-1]
        name = row.get("labelDe") or row.get("labelEn") or qid
        c = out.get(qid)
        if c is None:
            c = out[qid] = Candidate(qid=qid, name=name)
        c.labels |= {v for v in (row.get("labelDe"), row.get("labelEn")) if v}
        c.description = c.description or row.get("descDe") or row.get("descEn")
        instances = [i.rsplit("/", 1)[-1] for i in (row.get("instances") or "").split("|") if i]
        c.human = c.human or HUMAN in instances
        c.gender = c.gender or (row.get("gender") or "").rsplit("/", 1)[-1] or None
        if row.get("birth") and c.birth_year is None:
            born = parse_date(row["birth"])
            if born:
                c.birth_year = born.year
                if int(row.get("birthPrec") or 11) >= 11:
                    c.birth_date = born
        c.eliteprospects_id = c.eliteprospects_id or row.get("ep")
        c.dewiki = c.dewiki or row.get("dewiki")
        c.enwiki = c.enwiki or row.get("enwiki")
        for key, target in (
            ("occupations", c.occupations),
            ("sports", c.sports),
            ("positions", c.positions),
            ("positionsDe", c.positions_de),
            ("instagram", c.instagram),
        ):
            for value in (row.get(key) or "").split("|"):
                if value and value not in target:
                    target.append(value)
        for value in (row.get("nations") or "").split("|"):
            ioc, _, iso = value.partition(":")
            if (code := ioc_code(ioc or None, iso or None)) and code not in c.nations:
                c.nations.append(code)
        for value in (row.get("teams") or "").split("|"):
            team_qid, _, rest = value.partition("::")
            label, _, start = rest.partition("::")
            if team_qid and (team_qid, label, start) not in c.teams:
                c.teams.append((team_qid, label, start))
    return out


def contradiction(c: Candidate, *texts: str) -> str | None:
    """German reason why the candidate cannot be the person described in Fabi's texts, else None.
    Each text is checked on its own, so a correction ("nein, die Eishockeyspielerin") overrides the details."""
    for text in texts:
        if text and (reason := _contradiction(c, text)):
            return reason
    return None


def first_found(finder, texts: tuple[str, ...]) -> list:
    """Matches from the first text that has any (refine before details)."""
    return next((found for t in texts if t and (found := finder(t))), [])


def _contradiction(c: Candidate, context: str) -> str | None:
    nations = countries_in(context)
    if nations and c.nations and not set(nations) & set(c.nations):
        return f"Nation {'/'.join(c.nations)} statt {'/'.join(nations)}"
    sports = sports_in(context)
    if sports and c.sport and c.sport not in sports:
        return f"anderer Sport ({c.sport.name_de})"
    if sports and not c.sport and c.occupations:
        return f"kein Sportbezug ({', '.join(c.occupations[:2])})"
    year = year_in(context)
    if year and c.birth_year and year != c.birth_year:
        return f"Jahrgang {c.birth_year} statt {year}"
    if c.female is False and _FEMALE_WORDS.search(context) and not _MALE_WORDS.search(context):
        return "männlich"
    return None


_UNSET: Any = object()


class WikiPersonResolver:
    """PersonResolver: Wikidata + Wikipedia candidates, Jev fit check, Brave hints, Apify profile check."""

    def __init__(
        self,
        settings: Settings,
        wiki: WikiClient | None = None,
        web_search: Any = _UNSET,
        jev: Any = _UNSET,
        instagram_source: Any = _UNSET,
        max_candidates: int = 6,
        wiki_summaries: int = 3,
    ) -> None:
        self.settings = settings
        self.wiki = wiki or WikiClient()
        self._web_search = web_search
        self._instagram = instagram_source
        if jev is _UNSET:
            jev = None
            if settings.features["jev"]:
                from klugscheisser.classify.jev import JevClient

                jev = JevClient(settings)
        self.jev = jev
        self.max_candidates = max_candidates
        self.wiki_summaries = wiki_summaries

    # --- sources ----------------------------------------------------------------------------------------

    def candidates(self, name: str, notes: list[str]) -> list[Candidate]:
        try:
            hits = self.wiki.search_entities(name)
        except Exception as exc:
            log.warning("wikidata search failed for %r: %s", name, exc)
            notes.append("Wikidata war nicht erreichbar.")
            return []
        qids = [h["id"] for h in hits if not _DISAMBIGUATION.search(h.get("description") or "")]
        qids = qids[: self.max_candidates * 2]
        if not qids:
            return []
        try:
            rows = self.wiki.sparql(ENTITY_QUERY % {"values": " ".join(f"wd:{q}" for q in qids)})
        except Exception as exc:
            log.warning("wikidata query failed: %s", exc)
            notes.append("Wikidata war nicht erreichbar.")
            return []
        found = parse_entities(rows)
        self.add_coached(found)
        labels = {h["id"]: h.get("label") for h in hits}
        for qid, c in found.items():
            if c.name == qid and labels.get(qid):  # no de/en label: use the label search matched
                c.name = labels[qid]
                c.labels.add(labels[qid])
        return [found[q] for q in qids if q in found and found[q].human]

    def add_coached(self, found: dict[str, Candidate]) -> None:
        """Teams the human candidates coach (one extra query; a failure only loses the coach info)."""
        qids = [q for q, c in found.items() if c.human]
        if not qids:
            return
        try:
            rows = self.wiki.sparql(COACHED_QUERY % {"values": " ".join(f"wd:{q}" for q in qids)})
        except Exception as exc:
            log.warning("wikidata coached-teams query failed: %s", exc)
            return
        for qid, teams in parse_coached(rows).items():
            if qid in found:
                found[qid].coached = teams

    def add_wikipedia(self, c: Candidate) -> None:
        for lang, url in (("de", c.dewiki), ("en", c.enwiki)):
            if title := wiki_title(url):
                summary = self.wiki.summary(lang, title)
                extract = (summary.get("extract") or "").strip()
                if extract:
                    c.wiki_extract = extract
                    c.wiki_url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page") or url
                    c.wiki_lang = lang
                    return
                c.wiki_url, c.wiki_lang = c.wiki_url or url, c.wiki_lang or lang

    def web_search(self, session: Session):
        if self._web_search is _UNSET:
            from klugscheisser.web_search import get_web_search

            return get_web_search(self.settings, session)
        return self._web_search

    def instagram_source(self):
        if self._instagram is _UNSET:
            from klugscheisser.collectors.instagram.sources import get_instagram_source, instagram_configured

            if not instagram_configured(self.settings):
                return None

            return get_instagram_source(self.settings)
        return self._instagram

    # --- decision ---------------------------------------------------------------------------------------

    def ask_jev(
        self, session: Session, name: str, context: str, cands: list[Candidate]
    ) -> tuple[Candidate | None, float, float]:
        """(chosen candidate or None, probability, cost in USD)."""
        from klugscheisser.classify.jev import choice_answer

        options = [c.qid for c in cands] + [NONE_OPTION]
        criteria = {c.qid: c.short_de() for c in cands}
        criteria[NONE_OPTION] = (
            "keiner der Kandidaten passt sicher zu Fabis Angaben (Namensvetter oder unklar)"
        )
        wish = f"Fabi möchte die Person «{name}» hinzufügen." + (
            f" Ihre Angaben: {context}." if context else ""
        )
        question = {
            "fit": {
                "type": "choice",
                "instructions": f"{wish} Welcher Kandidat ist genau diese Person? Bei Namensvettern mit anderem "
                "Sport, anderer Nation oder anderem Jahrgang «keiner» wählen.",
                "criteria": criteria,
            }
        }
        state = wish + "\n\n" + "\n\n".join(f"Kandidat {c.qid}: {c.long_de()}" for c in cands)
        before = dict(getattr(self.jev, "usage", {}) or {})
        answer = self.jev.ask(state, question)["fit"]
        after = getattr(self.jev, "usage", {}) or {}
        tokens = sum(
            float(v) - float(before.get(k, 0)) for k, v in after.items() if isinstance(v, int | float)
        )
        cost = round(tokens / 1e6 * JEV_USD_PER_M_TOKENS, 6)
        costs.record(session, "jev", 1, cost, f"add person fit: {name}"[:200])
        choice, p = choice_answer(answer, options)
        chosen = next((c for c in cands if c.qid == choice), None)
        return chosen, p, cost

    def propose(
        self,
        session: Session,
        name: str,
        instagram: str | None = None,
        details: str = "",
        refine: str | None = None,
        previous: Proposal | None = None,
    ) -> Proposal:
        name = " ".join(name.split())
        notes: list[str] = []
        spent = 0.0
        handle = clean_handle(instagram)
        if instagram and not handle:
            notes.append(f"«{instagram}» ist kein gültiger Instagram-Name und wurde ignoriert.")
        prev = previous.persons[0] if previous and previous.persons else None
        if handle is None and prev and prev.instagram_source == "fabi":
            handle = prev.instagram
        context = " ".join(x for x in (details.strip(), (refine or "").strip()) if x)
        excluded: set[str] = set()
        if previous and refine and _REJECT.search(refine):
            excluded = {p.wikidata_qid for p in previous.persons if p.wikidata_qid}

        cands = [c for c in self.candidates(name, notes) if c.qid not in excluded]
        # plausible first: exact name, no contradiction
        texts = (refine or "", details)
        cands.sort(key=lambda c: (not c.exact_name(name), contradiction(c, *texts) is not None))
        cands = cands[: self.max_candidates]
        for c in cands[: self.wiki_summaries]:
            if c.dewiki or c.enwiki:
                self.add_wikipedia(c)

        match: Candidate | None = None
        fit: float | None = None
        ambiguous = False
        if cands and self.jev is not None:
            try:
                match, fit, cost = self.ask_jev(session, name, context, cands)
                spent += cost
                if match is not None and fit < FIT_THRESHOLD:
                    notes.append(
                        f"Jev ist unsicher, ob {match.name} ({match.qid}) passt ({fit:.0%}) – nicht übernommen."
                    )
                    match = None
            except Exception as exc:
                log.warning("jev fit check failed: %s", exc)
                notes.append("Jev-Prüfung fehlgeschlagen – Kandidaten nur nach Name verglichen.")
                match, ambiguous = self._without_jev(name, texts, cands)
        elif cands:
            match, ambiguous = self._without_jev(name, texts, cands)

        alternatives = [c.short_de() for c in cands if c is not match]
        if ambiguous:
            listing = "; ".join(alternatives[:5])
            return Proposal(
                kind="person",
                found=False,
                summary=f"Mehrere Personen heissen «{name}»: {listing}. Bitte präzisieren "
                "(Sport, Nation, Jahrgang oder Verein), damit keine Namensvetterin übernommen wird.",
                alternatives=alternatives,
                notes=notes,
                resolver_cost_usd=round(spent, 4),
            )

        hints: list[str] = []
        if match is None:
            hints, cost = self._web_hints(session, name, details)
            spent += cost
        if match is None and not context and not handle:
            summary = f"Zu «{name}» wurde nichts Verlässliches gefunden."
            if alternatives:
                summary += (
                    f" Kandidaten mit ähnlichem Namen passen nicht sicher: {'; '.join(alternatives[:3])}."
                )
            summary += " Bitte Details angeben (Sport, Nation, Verein oder Instagram)."
            return Proposal(
                kind="person",
                found=False,
                summary=summary,
                alternatives=alternatives,
                notes=notes + hints,
                resolver_cost_usd=round(spent, 4),
            )

        person = self._from_candidate(match, fit, texts) if match else self._from_fabi(name, texts)
        if match is None:
            notes.insert(0, FABI_ONLY)
        elif person.role == "coach" and not match.is_coach:
            notes.append("Rolle Trainer/in nur von Fabi angegeben – Wikidata nennt keinen Trainerberuf.")
        notes.extend(hints)

        if handle:
            person.instagram, person.instagram_source = handle, "fabi"
            person.instagram_check, cost = self._check_instagram(session, handle, person)
            spent += cost
            person.sources.append(SourceRef(f"Instagram @{handle} von Fabi angegeben"))
            if match and match.instagram and handle not in {h.lower() for h in match.instagram}:
                notes.append(f"Wikidata nennt einen anderen Instagram-Namen: @{match.instagram[0]}.")
        elif match and match.instagram:
            person.instagram, person.instagram_source = match.instagram[0], "wikidata"
            person.instagram_check = "nicht geprüft"

        existing = find_existing(session, person.wikidata_qid, person.name, person.birth_date)
        if existing is not None:
            person.existing_id = existing.id

        with_ig = 1 if person.instagram else 0
        return Proposal(
            kind="person",
            found=True,
            summary=person_summary(person, existing, fabi_only=match is None, details=context),
            persons=[person],
            alternatives=alternatives,
            notes=notes,
            cost_estimate_chf=estimate_load_cost_chf(self.settings, 1, with_ig),
            resolver_cost_usd=round(spent, 4),
        )

    def _without_jev(
        self, name: str, texts: tuple[str, ...], cands: list[Candidate]
    ) -> tuple[Candidate | None, bool]:
        """(match, ambiguous). Only one exact-name candidate that fits the details is accepted."""
        exact = [c for c in cands if c.exact_name(name)]
        if len(exact) > 1:
            return None, True
        if len(exact) == 1 and contradiction(exact[0], *texts) is None:
            return exact[0], False
        return None, False

    def _web_hints(self, session: Session, name: str, details: str) -> tuple[list[str], float]:
        ws = self.web_search(session)
        if ws is None:
            return [], 0.0
        query = " ".join(f"{name} {details}".split())[:200]
        try:
            results = ws.search(query, limit=5)
        except Exception as exc:
            log.warning("web search failed: %s", exc)
            return [], 0.0
        # BraveSearch records its own cost in the ledger; here it only counts for the proposal
        cost = self.settings.brave_cost_per_request_usd if getattr(ws, "name", "") == "brave" else 0.0
        hints = [f"Hinweis aus der Websuche (nicht geprüft): {r.title} – {r.url}" for r in results[:3]]
        return hints, cost

    def _check_instagram(self, session: Session, handle: str, person: ProposedPerson) -> tuple[str, float]:
        source = self.instagram_source()
        if source is None:
            return "nicht geprüft", 0.0
        try:
            profiles = source.fetch_profiles([handle])
        except Exception as exc:
            log.warning("instagram profile check failed for %s: %s", handle, exc)
            return "nicht geprüft (Profilabruf fehlgeschlagen)", 0.0
        from klugscheisser.collectors.instagram.sources import paid_profile_count

        cost = self.settings.apify_cost_per_post_usd * min(1, paid_profile_count(source, 1))
        if cost:
            costs.record(session, "apify", 1, cost, f"add person profile @{handle}")
        profile = next((p for p in profiles if p.username.lower() == handle), None)
        return describe_profile(profile, person), cost

    # --- building -----------------------------------------------------------------------------------------

    def _from_candidate(self, c: Candidate, fit: float | None, texts: tuple[str, ...]) -> ProposedPerson:
        context = " ".join(t for t in texts if t)
        female = c.female is not False
        coached = c.coached_team
        # Fabi saying "Trainer" decides the role; Wikidata often lists coaches only as former players, but a
        # coached team (P286 / P6087 / P1344 role "coach") makes the person a coach too
        coach = (
            re.search(r"trainer|coach", context, re.I) is not None
            or coached is not None
            or (c.is_coach and not c.is_player)
        )
        # a coach of a women's team belongs to the women's sport, whatever the coach's gender
        women_team = bool(coach and ((coached and coached.women) or _WOMEN_TEAM.search(context)))
        key, sport_name, noun = sport_fields(c.sport, female or women_team)
        wanted = first_found(countries_in, texts) or countries_in(c.description or "")
        nation = next((n for n in c.nations if n in wanted), c.nations[0] if c.nations else None)
        position = None
        if c.positions:
            position = _position_code(key, c.positions[0]) or (c.positions_de or c.positions)[0]
        sources = [SourceRef(f"Wikidata {c.qid}", c.url)]
        if coached is not None:
            sources.append(SourceRef(f"Wikidata {coached.qid} ({coached.label})", coached.url))
        if c.wiki_url:
            sources.append(SourceRef(f"Wikipedia ({c.wiki_lang})", c.wiki_url))
        function = None
        if coach:
            function = coached.function_de(c.female) if coached else ("Trainerin" if female else "Trainer")
        return ProposedPerson(
            name=c.name,
            role="coach" if coach else "athlete",
            function_de=function,
            sport_key=key,
            sport_name_de=sport_name,
            person_de=noun,
            nationality=nation,
            birth_date=c.birth_date,
            team=None if coach else c.current_team(),  # P54 = teams played for
            position=None if coach else position,
            wikidata_qid=c.qid,
            wikipedia_url=c.wiki_url,
            eliteprospects_id=c.eliteprospects_id,
            sources=sources,
            fit=round(fit, 3) if fit is not None else None,
        )

    def _from_fabi(self, name: str, texts: tuple[str, ...]) -> ProposedPerson:
        context = " ".join(t for t in texts if t)
        sports = first_found(sports_in, texts)
        female = bool(_FEMALE_WORDS.search(context)) or not _MALE_WORDS.search(context)
        from klugscheisser.sports import DEFAULT_SPORT, current_spec

        spec = current_spec()
        if spec.key != DEFAULT_SPORT:  # 2026-11-24: other projects (men's floorball): their sport and gender
            female = bool(_FEMALE_WORDS.search(context)) if spec.men else female
            base = spec.key.removesuffix("_women").removesuffix("_men")
            sports = sports or [x for x in SPORTS if x.base == base]
        women_sport = female or bool(_WOMEN_TEAM.search(context))
        key, sport_name, noun = sport_fields(sports[0] if sports else None, women_sport)
        nations = first_found(countries_in, texts)
        year = year_in(context)
        born = None
        if m_ := re.search(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*((?:19|20)\d\d)\b", context):
            try:
                born = date(int(m_.group(3)), int(m_.group(2)), int(m_.group(1)))
            except ValueError:
                born = None
        person = ProposedPerson(
            name=name,
            role="coach" if re.search(r"trainer|coach", context, re.I) else "athlete",
            sport_key=key,
            sport_name_de=sport_name,
            person_de=noun,
            nationality=nations[0] if len(nations) == 1 else None,
            birth_date=born,
            sources=[SourceRef("von Fabi angegeben")],
        )
        if person.role == "coach":
            gender_known = _FEMALE_WORDS.search(context) or _MALE_WORDS.search(context)
            person.function_de = ("Trainerin" if female else "Trainer") if gender_known else "Trainer/in"
        if year and not born:
            person.sources[0] = SourceRef(f"von Fabi angegeben (Jahrgang {year})")
        return person


def _position_code(sport_key: str | None, label: str) -> str | None:
    if not sport_key:
        return None
    try:
        from klugscheisser.sports import get_sport

        return get_sport(sport_key).position_code(label)
    except KeyError:
        return None


def position_de(sport_key: str | None, position: str | None) -> str | None:
    if not position:
        return None
    try:
        from klugscheisser.sports import get_sport

        return get_sport(sport_key or "").position_labels_de.get(position, position)
    except KeyError:
        return position


def describe_profile(profile, person: ProposedPerson) -> str:
    """German description of the Instagram profile check (evidence is added later by the loader)."""
    if profile is None:
        return "Profil nicht gefunden"
    parts: list[str] = []
    if profile.full_name and name_variants(profile.full_name) & name_variants(person.name):
        parts.append("Profilname stimmt")
    elif profile.full_name:
        parts.append(f"Profilname «{profile.full_name}» weicht ab")
    else:
        parts.append("kein Profilname")
    bio = profile.biography or ""
    words = [w for w in (person.team, (person.sport_name_de or "").split(" ")[0]) if w]
    hit = next((w for w in words if normalize(w) and normalize(w) in normalize(bio)), None)
    if hit is None and re.search(r"hockey|floorball|innebandy|salibandy|ski|sport|athlet", bio, re.I):
        hit = "Sport"
    parts.append(f"Bio nennt {hit}" if hit else "Bio ohne Sport-/Teambezug")
    if profile.private:
        parts.append("privates Profil")
    if profile.followers is not None:
        parts.append(f"{profile.followers:,} Follower".replace(",", "'"))
    return ", ".join(parts)


def find_existing(session: Session, qid: str | None, name: str, birth: date | None) -> m.Athlete | None:
    """Same Wikidata QID, or exactly one exact-name match whose birth date and QID do not contradict."""
    if qid:
        athlete = session.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == qid))
        if athlete is not None:
            return athlete
    fits = [
        x.athlete
        for x in repo.find_athletes(session, name)
        if x.how == "exact"
        and (birth is None or x.athlete.birth_date in (None, birth))
        and (not qid or x.athlete.wikidata_qid in (None, qid))
    ]
    return fits[0] if len(fits) == 1 else None


def person_summary(p: ProposedPerson, existing: m.Athlete | None, fabi_only: bool, details: str = "") -> str:
    """'Hanna Olsson (SWE), Eishockeyspielerin, geb. 12.03.1999, Stürmerin, aktuell Frölunda HC (laut
    Wikidata). Instagram @hannaolsson (von Fabi angegeben; Profilname stimmt).'"""
    head = f"{p.name} ({p.nationality})" if p.nationality else p.name
    parts = [head]
    if p.role in ("coach", "staff"):
        parts.append(
            f"{p.function_de or 'Trainer/in'}" + (f" ({p.sport_name_de})" if p.sport_name_de else "")
        )
    elif p.person_de:
        parts.append(p.person_de)
    if p.birth_date:
        parts.append(f"geb. {date_de(p.birth_date)}")
    if pos := position_de(p.sport_key, p.position):
        parts.append(pos)
    if p.team:
        parts.append(f"aktuell {p.team}")
    text = ", ".join(parts)
    if fabi_only:
        text += f" – nur Angaben von Fabi{f': «{details}»' if details else ''}."
    else:
        text += " (laut Wikidata)."
    if p.instagram:
        how = "von Fabi angegeben" if p.instagram_source == "fabi" else "laut Wikidata"
        text += f" Instagram @{p.instagram} ({how}; {p.instagram_check or 'nicht geprüft'})."
    if existing is not None:
        text += f" Bereits in unserer Datenbank (ID {existing.id}) – es werden nur fehlende Daten ergänzt."
    if p.sources:
        text += " Quellen: " + ", ".join(s.label for s in p.sources) + "."
    return text
