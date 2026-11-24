"""Seed athletes and their social handles from Wikidata (public SPARQL endpoint, no key).

Handles found here are only candidates: Wikidata is community-edited, so they start as
`wahrscheinlich` and need a second signal (profile check, official link) to become `verified`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import httpx

from klugscheisser.sports import SportSpec

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "klugscheisser-athlete/0.1 (https://github.com/andres-cerny/klugscheisser_athlete)"

PLAYERS_QUERY = """
SELECT ?p ?label ?labelEn ?given ?family ?birth ?ig ?x ?ep ?posLabel
       (GROUP_CONCAT(DISTINCT ?alt; separator="|") AS ?alts) WHERE {
  ?p wdt:P21 wd:%(gender)s; wdt:P27 wd:%(country)s; wdt:P106 wd:%(occupation)s .
  OPTIONAL { ?p rdfs:label ?label FILTER(lang(?label)="de") }
  OPTIONAL { ?p rdfs:label ?labelEn FILTER(lang(?labelEn)="en") }
  OPTIONAL { ?p wdt:P735/rdfs:label ?given FILTER(lang(?given)="en") }
  OPTIONAL { ?p wdt:P734/rdfs:label ?family FILTER(lang(?family)="en") }
  OPTIONAL { ?p wdt:P569 ?birth }
  OPTIONAL { ?p wdt:P2003 ?ig }
  OPTIONAL { ?p wdt:P2002 ?x }
  OPTIONAL { ?p wdt:P2481 ?ep }
  OPTIONAL { ?p wdt:P413 ?pos . ?pos rdfs:label ?posLabel FILTER(lang(?posLabel)="en") }
  OPTIONAL { ?p skos:altLabel ?alt FILTER(lang(?alt) IN ("de","en","fr")) }
} GROUP BY ?p ?label ?labelEn ?given ?family ?birth ?ig ?x ?ep ?posLabel
"""

MEMBERSHIP_QUERY = """
SELECT ?p ?team ?teamLabel ?start ?end ?country WHERE {
  ?p wdt:P21 wd:%(gender)s; wdt:P27 wd:%(country)s; wdt:P106 wd:%(occupation)s .
  ?p p:P54 ?st . ?st ps:P54 ?team .
  OPTIONAL { ?st pq:P580 ?start }
  OPTIONAL { ?st pq:P582 ?end }
  OPTIONAL { ?team wdt:P17 ?c . ?c wdt:P298 ?country }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,de". }
}
"""


# v5 P4 bulk seeding: like PLAYERS_QUERY, plus work period end (P2032), date of death and a birth-year filter
# in the query itself (keeps the result small for big skiing nations); persons without birth date stay in.
BULK_PLAYERS_QUERY = """
SELECT ?p ?label ?labelEn ?given ?family ?birth ?ig ?x ?ep ?posLabel ?workEnd
       (GROUP_CONCAT(DISTINCT ?alt; separator="|") AS ?alts) WHERE {
  VALUES ?country { %(countries)s }
  ?p wdt:P106 wd:%(occupation)s; wdt:P21 wd:%(gender)s; wdt:P27 ?country .
  OPTIONAL { ?p wdt:P569 ?birth }
  FILTER(!BOUND(?birth) || YEAR(?birth) >= %(min_year)d)
  FILTER NOT EXISTS { ?p wdt:P570 ?death }
  OPTIONAL { ?p rdfs:label ?label FILTER(lang(?label)="de") }
  OPTIONAL { ?p rdfs:label ?labelEn FILTER(lang(?labelEn)="en") }
  OPTIONAL { ?p wdt:P735/rdfs:label ?given FILTER(lang(?given)="en") }
  OPTIONAL { ?p wdt:P734/rdfs:label ?family FILTER(lang(?family)="en") }
  OPTIONAL { ?p wdt:P2003 ?ig }
  OPTIONAL { ?p wdt:P2002 ?x }
  OPTIONAL { ?p wdt:P2481 ?ep }
  OPTIONAL { ?p wdt:P2032 ?workEnd }
  OPTIONAL { ?p wdt:P413 ?pos . ?pos rdfs:label ?posLabel FILTER(lang(?posLabel)="en") }
  OPTIONAL { ?p skos:altLabel ?alt FILTER(lang(?alt) IN ("de","en","fr")) }
} GROUP BY ?p ?label ?labelEn ?given ?family ?birth ?ig ?x ?ep ?posLabel ?workEnd
"""

# memberships (P54) of exactly these persons, only teams of the sport (or without a sport): a skier who now
# races bicycles must not get her cycling team as a ski team
BULK_MEMBERSHIP_QUERY = """
SELECT ?p ?team ?teamLabel ?start ?end ?country WHERE {
  VALUES ?p { %(values)s }
  ?p p:P54 ?st . ?st ps:P54 ?team .
  %(sport_filter)s
  OPTIONAL { ?st pq:P580 ?start }
  OPTIONAL { ?st pq:P582 ?end }
  OPTIONAL { ?team wdt:P17 ?c . ?c wdt:P298 ?country }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,de". }
}
"""


@dataclass
class WikidataTeamMembership:
    team_qid: str
    team_label: str
    start: date | None = None
    end: date | None = None
    country: str | None = None


@dataclass
class WikidataPlayer:
    qid: str
    name: str
    nationality: str
    given_name: str | None = None
    family_name: str | None = None
    birth_date: date | None = None
    position: str | None = None
    instagram: list[str] = field(default_factory=list)
    x: list[str] = field(default_factory=list)
    eliteprospects_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    teams: list[WikidataTeamMembership] = field(default_factory=list)
    work_end: date | None = None  # P2032 work period (end): end of the career when set

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"

    def inactive_reason(self, today: date, min_birth_year: int = 1980, retired_years: int = 3) -> str | None:
        """'born_before' / 'retired' when the person is no longer an active athlete, else None.
        Unknown birth date and no known career end count as active (Wikidata often lacks both)."""
        if self.birth_date is not None and self.birth_date.year < min_birth_year:
            return "born_before"
        if self.work_end is not None and self.work_end < date(
            today.year - retired_years, today.month, min(today.day, 28)
        ):
            return "retired"
        return None


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _val(row: dict, key: str) -> str | None:
    return row[key]["value"] if key in row else None


def parse_players(payload: dict, nationality: str, sport: SportSpec) -> dict[str, WikidataPlayer]:
    players: dict[str, WikidataPlayer] = {}
    for row in payload["results"]["bindings"]:
        qid = _qid(row["p"]["value"])
        name = _val(row, "label") or _val(row, "labelEn")
        if not name or name == qid:
            continue
        player = players.get(qid)
        if player is None:
            player = players[qid] = WikidataPlayer(
                qid=qid,
                name=name,
                nationality=nationality,
                given_name=_val(row, "given"),
                family_name=_val(row, "family"),
                birth_date=_date(_val(row, "birth")),
                eliteprospects_id=_val(row, "ep"),
            )
        if (end := _date(_val(row, "workEnd"))) and (player.work_end is None or end > player.work_end):
            player.work_end = end
        if (pos := sport.position_code(_val(row, "posLabel"))) and not player.position:
            player.position = pos
        for key, target in (("ig", player.instagram), ("x", player.x)):
            if (handle := _val(row, key)) and handle not in target:
                target.append(handle)
        en = _val(row, "labelEn")
        for alias in [en, *(_val(row, "alts") or "").split("|")]:
            if alias and alias != player.name and alias not in player.aliases:
                player.aliases.append(alias)
    return players


def parse_memberships(payload: dict, players: dict[str, WikidataPlayer]) -> None:
    seen: set[tuple] = set()
    for row in payload["results"]["bindings"]:
        qid = _qid(row["p"]["value"])
        if qid not in players:
            continue
        m = WikidataTeamMembership(
            team_qid=_qid(row["team"]["value"]),
            team_label=_val(row, "teamLabel") or "",
            start=_date(_val(row, "start")),
            end=_date(_val(row, "end")),
            country=_val(row, "country"),
        )
        key = (qid, m.team_qid, m.start)
        if key not in seen:
            seen.add(key)
            players[qid].teams.append(m)


class WikidataCollector:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT})

    def _query(self, query: str) -> dict:
        r = self.client.get(
            ENDPOINT, params={"query": query}, headers={"Accept": "application/sparql-results+json"}
        )
        r.raise_for_status()
        return r.json()

    def collect(self, sport: SportSpec, nationality: str, country_qid: str) -> list[WikidataPlayer]:
        params = {
            "gender": sport.wikidata_gender,
            "country": country_qid,
            "occupation": sport.wikidata_occupation,
        }
        players = parse_players(self._query(PLAYERS_QUERY % params), nationality, sport)
        parse_memberships(self._query(MEMBERSHIP_QUERY % params), players)
        return list(players.values())

    def _post(self, query: str) -> dict:
        # POST: long VALUES lists exceed what the query service accepts as GET
        r = self.client.post(
            ENDPOINT, data={"query": query}, headers={"Accept": "application/sparql-results+json"}
        )
        r.raise_for_status()
        return r.json()

    def collect_bulk(
        self,
        sport: SportSpec,
        nationality: str,
        country_qids: str | tuple[str, ...] | list[str],
        min_birth_year: int = 1980,
        chunk: int = 150,
    ) -> list[WikidataPlayer]:
        """All persons with the sport's occupation, gender and one of these citizenships (DEN: Denmark and
        Kingdom of Denmark), born >= min_birth_year (or without birth date), alive, with their memberships.
        The active filter (career end) is applied by the caller via `WikidataPlayer.inactive_reason`."""
        if isinstance(country_qids, str):
            country_qids = (country_qids,)
        params = {
            "gender": sport.wikidata_gender,
            "countries": " ".join(f"wd:{q}" for q in country_qids),
            "occupation": sport.wikidata_occupation,
            "min_year": min_birth_year,
        }
        players = parse_players(self._post(BULK_PLAYERS_QUERY % params), nationality, sport)
        qids = list(players)
        for i in range(0, len(qids), chunk):
            values = " ".join(f"wd:{q}" for q in qids[i : i + chunk])
            query = BULK_MEMBERSHIP_QUERY % {"values": values, "sport_filter": sport_filter(sport)}
            parse_memberships(self._post(query), players)
        return list(players.values())


def sport_filter(sport: SportSpec) -> str:
    """SPARQL filter: ?team has no sport (P641) or this sport."""
    if not sport.wikidata_sport:
        return ""
    return (
        "FILTER(NOT EXISTS { ?team wdt:P641 ?anySport } || "
        f"EXISTS {{ ?team wdt:P641 wd:{sport.wikidata_sport} }})"
    )


@dataclass
class BulkCount:
    """Dry-run / load counts per nation."""

    nation: str
    found: int = 0
    active: int = 0
    born_before: int = 0
    retired: int = 0
    no_birth_date: int = 0  # kept (counted among active)
    with_instagram: int = 0
    with_current_team: int = 0


def split_active(
    players: list[WikidataPlayer],
    nation: str,
    today: date,
    min_birth_year: int = 1980,
    retired_years: int = 3,
) -> tuple[list[WikidataPlayer], BulkCount]:
    count = BulkCount(nation=nation, found=len(players))
    active: list[WikidataPlayer] = []
    for p in players:
        reason = p.inactive_reason(today, min_birth_year, retired_years)
        if reason == "born_before":
            count.born_before += 1
            continue
        if reason == "retired":
            count.retired += 1
            continue
        active.append(p)
        count.no_birth_date += p.birth_date is None
        count.with_instagram += bool(p.instagram)
        count.with_current_team += any(t.end is None for t in p.teams)
    count.active = len(active)
    return active, count
