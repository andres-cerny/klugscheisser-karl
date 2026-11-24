"""Who of nation X plays at clubs in country Y ("Welche Deutschen spielen in der Schweiz?", "Welche Schweizer
spielen in Schweden?", "Finnen in der Schweiz", "Welche Schweizerinnen spielen im Ausland?").

Any project and sport: the persons come from the stored nationality (or a named group), the club from what we
store about each person, newest first:

1. a dated club affiliation (the federation's tournament roster: "club" + "league" per player),
2. the stored current-team fact (Elite Prospects, Wikipedia, club sites …),
3. the newest career stint at a club.

The club's country comes from its league ("L-UPL", "SSL", "SDHL", "PWHL" with the team's city …), else from the
stored team, else from a country named in the club or league text. Former clubs only count when the question asks
for the past ("Wer hat schon in Schweden gespielt?"). Direct answer, no model: count, list with club, league,
season and source, and for whom no club country is stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize

# --- countries ------------------------------------------------------------------------------------------------

# place nouns after "in (der|den)" → IOC code (the country as a place, not as a team or nationality)
PLACE_NOUNS: dict[str, str] = {
    "SUI": r"schweiz|switzerland",
    "SWE": r"schweden|sweden",
    "FIN": r"finnland|finland",
    "GER": r"deutschland|germany",
    "CZE": r"tschechien|tschechei|czechia|czech republic",
    "SVK": r"slowakei|slovakia",
    "NOR": r"norwegen|norway",
    "DEN": r"dänemark|daenemark|denmark",
    "AUT": r"österreich|oesterreich|austria",
    "USA": r"usa|amerika|america|united states|vereinigten staaten|us",
    "CAN": r"kanada|canada",
    "LAT": r"lettland|latvia",
    "EST": r"estland|estonia",
    "JPN": r"japan",
    "ITA": r"italien|italy",
    "FRA": r"frankreich|france",
    "SLO": r"slowenien|slovenia",
    "NED": r"niederlanden?|holland|netherlands",
    "HUN": r"ungarn|hungary",
    "POL": r"polen|poland",
    "CHN": r"china",
    "KOR": r"korea|südkorea",
    "GBR": r"grossbritannien|großbritannien|england|great britain|uk",
    "SGP": r"singapur|singapore",
    "THA": r"thailand",
    "PHI": r"philippinen|philippines",
    "RUS": r"russland|russia",
    "KAZ": r"kasachstan|kazakhstan",
}
_PLACE_NOUN_RX = {ioc: re.compile(rf"^(?:{rx})$", re.I) for ioc, rx in PLACE_NOUNS.items()}
CLUB_WORDS = (
    r"(?:clubs?|klubs?|vereinen?|vereins?|teams?|mannschaften|ligen|liga|league|leagues|meisterschaft|"
    r"topliga|top-?liga|profiliga)"
)
# "in der Schweiz", "in Schweden", "in the US", "nach Finnland" (wechseln)
_IN_PLACE = re.compile(r"\b(?:in|nach|to)\s+(?:der|den|dem|the)?\s*([a-zäöüé]+(?: [a-zäöü]+)?)", re.I)
# "bei Schweizer Clubs", "in schwedischen Klubs", "in der finnischen Liga", "at Swedish clubs", "for Swiss teams"
_ADJ_PLACE = re.compile(
    rf"\b(?:in|bei|für|fuer|at|for|with|im)\s+(?:der|den|dem|einem|einer|einen|the|a)?\s*([a-zäöüé-]+)\s+{CLUB_WORDS}\b",
    re.I,
)
ABROAD = re.compile(
    r"\b(im ausland|ins ausland|abroad|overseas|legionär\w*|legionaer\w*|ausländische[nrms]? "
    rf"{CLUB_WORDS}|foreign {CLUB_WORDS}|ausserhalb (?:der|von) \w+|outside (?:of )?\w+)\b",
    re.I,
)
# "in" + this word is never a place: "in der Saison", "in den letzten Wochen"
NOT_PLACE_WORDS = re.compile(r"^(saison|season|letzten|vergangenen|nati|nationalmannschaft|wm|world)\b", re.I)

PLAYS = re.compile(
    r"\b(spielen|spielt|spielend|spielte|spielten|gespielt|play|plays|playing|played|aktiv|engagiert|"
    r"unter vertrag|stehen|steht|sind|ist|is|are|were|waren|war)\b",
    re.I,
)
WHO = re.compile(
    r"\b(wer|welche\w*|who|which|wie viele|wieviele|how many|liste\w*|list|alle|all|gibt es|are there)\b",
    re.I,
)
PAST = re.compile(
    r"\b(gespielt|spielte|spielten|played|früher\w*|frueher\w*|ehemalig\w*|former\w*|karriere|career|jemals|"
    r"ever|schon (?:mal|einmal)|bereits|bisher)\b",
    re.I,
)
NOT_THIS = re.compile(
    r"\b(gegen|against|vs\.?|versus|tor(e|en)?|punkte|goals|points|assists|spiele[ns]? (?:am|um)|"
    r"wann|when|wo spielt|where)\b",
    re.I,
)

# league (as stored: "L-UPL", "SSL (SWE)", "SDHL (W)", "Naisten Liiga") → country. None: several countries
# (the team decides: PWHL, NCAA is USA only). Any project/sport; order matters (first match wins).
LEAGUE_COUNTRY: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"\(([A-Z]{3})\)"), "CODE"),  # "L-UPL (SUI)", "SSL? (SWE)": the research rosters' own code
    (
        re.compile(
            r"\b(l-?upl|unihockey prime league|nla|nlb|nationalliga|swhl|women'?s league|swiss league|"
            r"national league|mysports league|unihockey)\b",
            re.I,
        ),
        "SUI",
    ),
    (
        re.compile(
            r"\b(ssl|svenska superligan|sdhl|shl|hockeyallsvenskan|allsvenskan|riksserien|nda|innebandy)\b",
            re.I,
        ),
        "SWE",
    ),
    (
        re.compile(
            r"\b(f-?liiga|salibandyliiga|auroraliiga|naisten liiga|naisten sm-sarja|sm-sarja|liiga|mestis|"
            r"divari|salibandy)\b",
            re.I,
        ),
        "FIN",
    ),
    (re.compile(r"\b(floorball-?bundesliga|dfel|del2?|frauen-?bundesliga|fraueneishockey)\b", re.I), "GER"),
    (re.compile(r"\b(livesport superliga|tipsport extraliga|chance liga|extraliga žen)\b", re.I), "CZE"),
    (re.compile(r"\b(ncaa|ushs|nwhl|phf|wcha|ecac|hockey east|college)\b", re.I), "USA"),
    (re.compile(r"\b(u ?sports|cwhl)\b", re.I), "CAN"),
    (re.compile(r"\b(eliteserien|fjordkraftligaen)\b", re.I), "NOR"),
    (re.compile(r"\b(metal ligaen|unihockeyligaen|floorballligaen)\b", re.I), "DEN"),
]
# country words of other languages in league names (Finnish Wikipedia: "Sveitsin liiga" = Swiss league)
FOREIGN_WORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsveitsi\w*", re.I), "SUI"),
    (re.compile(r"\bruotsi\w*", re.I), "SWE"),
    (re.compile(r"\bsaksa\w*", re.I), "GER"),
    (re.compile(r"\btšeki\w*|\btsheki\w*", re.I), "CZE"),
    (re.compile(r"\bnorja\w*", re.I), "NOR"),
    (re.compile(r"\btanska\w*", re.I), "DEN"),
    (re.compile(r"\bsuomen\b", re.I), "FIN"),
]
# PWHL / LPHF: the team's city decides
PWHL_CITY = {
    "boston": "USA",
    "minnesota": "USA",
    "new york": "USA",
    "seattle": "USA",
    "toronto": "CAN",
    "montreal": "CAN",
    "montréal": "CAN",
    "ottawa": "CAN",
    "vancouver": "CAN",
    "hamilton": "CAN",
    "detroit": "USA",
    "las vegas": "USA",
    "san jose": "USA",
}


def place_phrase(ioc: str) -> str:
    """'in der Schweiz', 'in Schweden', 'in den USA'."""
    from klugscheisser.tournament import NATION_DE

    special = {
        "SUI": "in der Schweiz",
        "SVK": "in der Slowakei",
        "USA": "in den USA",
        "NED": "in den Niederlanden",
        "PHI": "auf den Philippinen",
        "CZE": "in Tschechien",
    }
    return special.get(ioc, f"in {NATION_DE.get(ioc, ioc)}")


def from_phrase(ioc: str) -> str:
    """'der Schweiz', 'Schweden', 'den USA' (after 'aus')."""
    return place_phrase(ioc).split(" ", 1)[1]


def _code_of_words(text: str) -> str | None:
    """A country named in a short text: place noun or adjective ('Schweden', 'schwedischen', 'Swiss')."""
    from klugscheisser.tournament import nations_in

    t = (text or "").strip()
    for ioc, rx in _PLACE_NOUN_RX.items():
        if rx.match(t):
            return ioc
    found = nations_in(t)
    return found[0] if len(found) == 1 else None


def league_country(league: str | None, team: str | None = None) -> str | None:
    """Country of a club from its league text (and, for PWHL, the team's city); None when unknown."""
    lg = (league or "").strip()
    if not lg:
        return None
    if hit := LEAGUE_COUNTRY[0][0].search(lg):
        return hit.group(1)
    from klugscheisser.tournament import nations_in

    plain = re.sub(r"\s*\(w\)\s*$", "", lg, flags=re.I)
    named = nations_in(plain) or [c for rx, c in FOREIGN_WORDS if rx.search(plain)]
    if len(set(named)) == 1:  # "Czech league", "Czechia (W)", "Finnische höchste Liga", "Sveitsin liiga"
        return named[0]
    for rx, ioc in LEAGUE_COUNTRY[1:]:
        if rx.search(lg):
            return ioc
    if re.search(r"\b(pwhl|lphf)\b", lg, re.I):
        low = (team or "").lower()
        return next((c for city, c in PWHL_CITY.items() if city in low), None)
    return None


# --- understanding the question -------------------------------------------------------------------------------


@dataclass
class Ref:
    place: str | None  # IOC code of the clubs' country; None with abroad=True
    abroad: bool = False
    past: bool = False
    rest: str = ""  # the question without the place words (the nation / group is read from it)
    nations: list[str] | None = None  # the players' nations


def parse(question: str) -> Ref | None:
    """The place a question asks about, or None. Needs a 'who' question (or a short 'Finnen in der Schweiz')
    and no game words ('gegen', 'wann', goals …)."""
    from klugscheisser.tournament import nations_in

    q = (question or "").strip()
    if not q or NOT_THIS.search(q):
        return None
    words = len(re.findall(r"\w+", q))
    place, spans = None, []
    for mt in _ADJ_PLACE.finditer(q):
        if code := _code_of_words(mt.group(1)):
            place, spans = code, [mt.span()]
            break
    if place is None:
        for mt in _IN_PLACE.finditer(q):
            cand = mt.group(1)
            if NOT_PLACE_WORDS.match(cand):
                continue
            first = cand.split()[0]
            code = next(
                (
                    ioc
                    for ioc, rx in _PLACE_NOUN_RX.items()
                    if rx.match(cand) or rx.match(first) or (ioc == "USA" and first.lower() == "us")
                ),
                None,
            )
            if code:
                place, spans = code, [mt.span()]
                break
    abroad = place is None and bool(ABROAD.search(q))
    if place is None and not abroad:
        return None
    if abroad:
        spans = [ABROAD.search(q).span()]
    rest = q
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    nations = nations_in(rest)
    short = words <= 5 and bool(nations)
    if not (WHO.search(q) or short):
        return None
    if not (PLAYS.search(q) or short or re.search(CLUB_WORDS, q, re.I)):
        return None
    return Ref(place=place, abroad=abroad, past=bool(PAST.search(q)), rest=rest.strip(), nations=nations)


# --- each person's club ---------------------------------------------------------------------------------------


@dataclass
class Club:
    name: str
    country: str | None
    league: str | None
    season: int | None  # start year
    source: str
    url: str
    prio: float  # shared current club 4 (data-source fact of this season 4.5), roster 3, league row 3, fact 2/1, stint 0
    kind: str  # "kader" | "fakt" | "station"
    item_id: int | None = None

    def rank(self) -> tuple:
        return (self.season or 0, self.prio)


def _season_year(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    mt = re.match(r"(\d{4})", str(value))
    return int(mt.group(1)) if mt else None


def current_season(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 8 else today.year - 1  # as connections.season_start


_CLEAN_TEAM = re.compile(r"\s*\((?:wechsel|ab |seit |from |since |bis |until )[^)]*\)\s*$", re.I)
DATA_SOURCES = {"eliteprospects", "sihf", "hockeycanada", "torneopal", "swissunihockey", "seed", "iihf"}


def _team_lookup(s: Session, name: str, cache: dict) -> m.Team | None:
    from klugscheisser.db import repo

    key = normalize(name)
    if key not in cache:
        team = repo.find_team(s, name, fuzzy=False)
        if team is None and len(key) >= 5:  # "Falun" → "IBF Falun": one club only
            hits = [
                t
                for t in cache.setdefault("_clubs", list(s.scalars(select(m.Team))))
                if key in normalize(t.name)
            ]
            team = hits[0] if len({t.country for t in hits}) == 1 and hits else None
        cache[key] = team
    return cache[key]


def _country(league: str | None, team: m.Team | None, name: str) -> str | None:
    if not league and (inner := re.search(r"\(([^)]+)\)\s*$", name or "")):
        league = inner.group(1)  # "Montréal (PWHL)", "Ohio State University (WCHA)"
    if c := league_country(league, name):
        return c
    if team is not None and team.kind != "national":
        return league_country(team.league, team.name) or team.country
    return None


def clubs_of(
    s: Session, ids: list[int], past: bool = False, today: date | None = None
) -> dict[int, list[Club]]:
    """Per person the club candidates, best first (newest season, then roster > data source > other).
    `past=False`: only what can be current (a dated affiliation of last/this season, the current-team fact, a
    career stint not ended before last season)."""
    from klugscheisser.answer.cross import _source_label

    cur = current_season(today)
    out: dict[int, list[Club]] = {i: [] for i in ids}
    if not ids:
        return out
    cache: dict = {}
    rows = s.execute(
        select(m.Affiliation, m.Team)
        .join(m.Team, m.Team.id == m.Affiliation.team_id)
        .where(m.Affiliation.athlete_id.in_(ids), m.Team.kind != "national")
    ).all()
    for aff, team in rows:
        season = _season_year(aff.season)
        if season is not None and season > cur:
            season = cur
        if season is None and not past:
            continue  # undated (Wikidata "member of"): a career, not the current club
        if not past and season is not None and season < cur - 1:
            continue
        out[aff.athlete_id].append(
            Club(
                team.name,
                _country(team.league, team, team.name),
                team.league,
                season,
                _source_label(aff.source or "Kader", aff.source_url or "").strip("[]")
                if aff.source_url
                else (aff.source or "Kader")[:80],
                aff.source_url or "",
                3,
                "kader",
            )
        )
    facts = s.scalars(
        select(m.Fact).where(
            m.Fact.athlete_id.in_(ids), m.Fact.kind == "current_team", m.Fact.status != "unconfirmed"
        )
    )
    for f in facts:
        v = f.value or {}
        if str(v.get("status") or "").lower() in ("retired", "zurückgetreten"):
            continue
        name = _CLEAN_TEAM.sub("", str(v.get("team") or v.get("text") or f.value_text or "")).strip()
        if not name:
            continue
        season = _season_year(v.get("season"))
        if season is not None and season > cur:
            season = cur  # "2029/30": a contract until then, the club is the current one
        if not past and season is not None and season < cur - 1:
            continue  # "AIK IF (2007/08)": an old Wikipedia infobox, not the current club
        team = _team_lookup(s, name, cache)
        league = v.get("league")
        out[f.athlete_id].append(
            Club(
                team.name
                if team is not None and normalize(team.name) != normalize(name) and len(name) < 8
                else name,
                _country(league, team, name),
                re.sub(r"\s*\(w\)\s*$", "", league or "", flags=re.I) or (team.league if team else None),
                season,
                _source_label(f.source, f.source_url).strip("[]"),
                f.source_url,
                # a data source's row of this season (Elite Prospects …) stays first, as before; news only after
                # the dated roster/league rows
                (4.5 if season is not None and season >= cur else 2) if f.source in DATA_SOURCES else 1,
                "fakt",
                f.source_item_id,
            )
        )
    stints = s.scalars(
        select(m.CareerStint).where(
            m.CareerStint.athlete_id.in_(ids), m.CareerStint.kind == "club", m.CareerStint.role == "player"
        )
    )
    for st in stints:
        end = st.season_to if st.season_to is not None else st.season_from
        if not past and (
            end is None
            or (st.season_to is not None and st.season_to < cur - 1)
            or (st.season_to is None and st.season_from < cur - 2)  # open since 2007: an old infobox, not now
        ):
            continue
        if end is not None and end > cur:
            end = cur  # a contract until 2029: the club is the current one
        team = s.get(m.Team, st.team_id) if st.team_id else _team_lookup(s, st.team_name, cache)
        out[st.athlete_id].append(
            Club(
                st.team_name,
                _country(st.league, team, st.team_name),
                st.league,
                end,
                _source_label(st.source, st.source_url).strip("[]"),
                st.source_url,
                0,
                "station",
                st.source_item_id,
            )
        )
    if not past:
        # dated league rows of this/last season (Swiss Unihockey, SSL, F-Liiga, Elite Prospects …)
        stat_rows = s.scalars(
            select(m.SeasonStat).where(
                m.SeasonStat.athlete_id.in_(ids),
                m.SeasonStat.season >= cur - 1,
                m.SeasonStat.team_kind.in_(("club", "college")),
                m.SeasonStat.team_name.is_not(None),
            )
        )
        for r in stat_rows:
            if r.team_name.endswith("(all)"):
                continue
            team = _team_lookup(s, r.team_name, cache)
            out[r.athlete_id].append(
                Club(
                    r.team_name,
                    _country(r.league, team, r.team_name),
                    r.league,
                    min(r.season, cur),
                    _source_label(r.source, r.source_url or "").strip("[]"),
                    r.source_url or "",
                    3,
                    "statistik",
                )
            )
        # the shared current-club rule (connections.current_clubs: this season's roster, league row, open
        # station), the same as the person card and the connections answer
        from klugscheisser.answer.connections import current_clubs

        for aid, clubs in current_clubs(s, ids, season=cur).items():
            for c in clubs[:1]:
                team = _team_lookup(s, c.name, cache)
                quelle = c.quelle
                if len(quelle) > 40 and c.url:  # a roster's long title: the site is enough in a table cell
                    quelle = urlsplit(c.url).netloc.removeprefix("www.")
                out[aid].append(
                    Club(
                        c.name,
                        _country(None, team, c.name),
                        team.league if team else None,
                        min(c.since or cur, cur),
                        quelle,
                        c.url or "",
                        4,
                        "aktuell",
                    )
                )
    for aid in out:
        out[aid].sort(key=lambda c: c.rank(), reverse=True)
    return out


WHERE_PLAYS = re.compile(
    r"^\W*(wo|bei wem|für wen|fuer wen|where|who does|for whom)\s+(spielt|spielen|plays?|does)\b"
    r"(?!.*\b(gegen|against|vs|wann|when|heute|morgen|today|tomorrow|um \d|spiel\b|game\b|wm\b|turnier))",
    re.I,
)


def person_club_inputs(s: Session, ids: list[int], label: str = ""):
    """'Wo spielt Jan Bürki?' / 'Bei welchem Verein spielt er?': the newest stored club per named person (dated
    roster or league row first, never an old news article); direct answer with league, season and source."""
    from klugscheisser.answer.cross import ProsaInputs, _basis, _computed_doc, _numbered, _source_doc
    from klugscheisser.projects import wording
    from klugscheisser.tournament import NATION_DE

    people = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    cands = clubs_of(s, list(people))
    rows, docs, basis, unknown = [], [], [], []
    for aid in ids:
        a = people.get(aid)
        if a is None:
            continue
        club = current_club(cands.get(aid) or [])
        if club is None:
            unknown.append(a.full_name)
            continue
        abroad = club.country if club.country and club.country != a.nationality else None
        extra = ", ".join(
            x
            for x in (club.league, NATION_DE.get(abroad or "", abroad or ""), _season_label(club.season))
            if x
        )
        txt = f"{club.name} ({extra})" if extra else club.name
        rows.append([a.full_name, txt, club.source, f"{a.full_name}: aktueller Klub {txt}"])
        basis.append((club.source, club.url))
        docs.append(
            _source_doc(
                f"{a.full_name}: aktueller Klub {txt}.", club.source, club.url, a.full_name, club.item_id
            )
        )
    if not rows:
        return None
    if len(rows) == 1:
        summary = (
            f"{rows[0][0]} spielt aktuell bei {rows[0][1]} (laut {rows[0][2]}, neuester gespeicherter Klub)"
        )
    else:
        summary = (
            f"Aktueller Klub von {len(rows)} {wording().players} (neuester gespeicherter Klub, mit Quelle)"
        )
    unk = f"Ohne gespeicherten aktuellen Klub: {', '.join(unknown)}" if unknown else None
    body = summary + ". " + "; ".join(r[-1] for r in rows) + "." + (f" {unk}." if unk else "")
    computed = _computed_doc(body, label or "Aktueller Klub", [r[0] for r in rows], _basis(basis))
    computed.extra["direct"] = {
        "summary": summary,
        "rows": rows,
        "unknown": unk,
        "spalten": [wording().player, "Klub (Liga, Land, Saison)", "Quelle"],
    }
    return ProsaInputs(docs=_numbered([computed, *docs]), athlete_ids=list(ids))


def current_club(cands: list[Club]) -> Club | None:
    """The newest candidate; its country from another candidate of the same club when it has none."""
    if not cands:
        return None
    best = cands[0]
    if best.country is None:
        b = normalize(best.name)
        same = next(
            (c for c in cands[1:] if c.country and (normalize(c.name) in b or b in normalize(c.name))),
            None,
        )
        if same is not None:
            best = Club(**{**best.__dict__, "country": same.country})
    return best


# --- the answer -----------------------------------------------------------------------------------------------


def _scope(s: Session, ref: Ref, athletes: list, history) -> tuple[list[int], str | None]:
    from klugscheisser import projects
    from klugscheisser.answer.cross import _group_scope, _players, understand
    from klugscheisser.projects import wording

    form = understand(ref.rest)
    form.nations = list(ref.nations or [])
    if ref.abroad and not form.nations:
        form.nations = [projects.current().home_nation]  # "Legionäre" without a nation: the home nation
        ref.nations = form.nations
    ids, note = _group_scope(s, ref.rest, form, athletes, history)
    if not ids:
        ids = _players(s, s.scalars(select(m.Athlete.id).where(m.Athlete.role == "athlete")))
        note = f"alle {wording().players} in unserer Datenbank ({len(ids)})"
    return ids, note


def inputs(s: Session, question: str, ref: Ref, athletes: list, history=None, today: date | None = None):
    """ProsaInputs with one computed document (direct answer) + one source document per listed person."""
    from klugscheisser.answer.cross import (
        MAX_LISTED,
        MAX_SOURCE_DOCS,
        ProsaInputs,
        _basis,
        _computed_doc,
        _numbered,
        _scope_label,
        _source_doc,
        _today,
    )
    from klugscheisser.projects import wording
    from klugscheisser.tournament import NATION_DE

    ids, note = _scope(s, ref, athletes, history)
    label = _scope_label(note, len(ids))
    people = {
        a.id: a
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids), m.Athlete.role == "athlete"))
    }
    cands = clubs_of(s, list(people), past=ref.past, today=today)
    hits, unknown = [], []
    for aid, a in people.items():
        if ref.past:
            here = [c for c in cands[aid] if c.country and _matches(c.country, a, ref)]
            seen, uniq = set(), []
            for c in here:
                if normalize(c.name) not in seen:
                    seen.add(normalize(c.name))
                    uniq.append(c)
            if uniq:
                hits.append((a, uniq))
            elif not any(c.country for c in cands[aid]):
                unknown.append(a.full_name)
            continue
        club = current_club(cands[aid])
        if club is None or club.country is None:
            unknown.append(a.full_name)
        elif _matches(club.country, a, ref):
            hits.append((a, [club]))
    hits.sort(key=lambda h: ((h[0].nationality or ""), normalize(h[1][0].name), h[0].full_name))
    where = "im Ausland" if ref.abroad else place_phrase(ref.place or "")
    who = (
        f"{wording().players} aus {' + '.join(from_phrase(n) for n in ref.nations)}"
        if ref.nations
        else wording().players
    )
    rows, docs, basis = [], [], []
    for a, clubs in hits:
        bits = []
        for c in clubs[: 4 if ref.past else 1]:
            extra = ", ".join(
                x
                for x in (
                    c.league,
                    NATION_DE.get(c.country or "", c.country or "") if ref.abroad or not ref.place else None,
                    _season_label(c.season),
                )
                if x
            )
            bits.append(f"{c.name} ({extra})" if extra else c.name)
        club_txt = "; ".join(bits)
        src = clubs[0].source
        nat = NATION_DE.get(a.nationality or "", a.nationality or "")
        cells = (
            [a.full_name, club_txt, src]
            if ref.nations and len(ref.nations) == 1
            else [a.full_name, nat, club_txt, src]
        )
        rows.append([*cells, f"{a.full_name} ({nat}): {club_txt}"])
        basis.append((src, clubs[0].url))
        docs.append(
            _source_doc(
                f"{a.full_name} ({nat}): Klub {club_txt}.",
                src,
                clubs[0].url,
                a.full_name,
                clubs[0].item_id,
            )
        )
    n = len(hits)
    verb = ("hat" if n == 1 else "haben") if ref.past else ("spielt" if n == 1 else "spielen")
    when = " schon" if ref.past else " aktuell"
    tail = "gespielt" if ref.past else ""
    summary = f"{n} {who} {verb}{when} bei Klubs {where}" + (f" {tail}" if tail else "")
    sources = "/".join(sorted({c[0].source for _, c in hits[:12]})) or "gespeicherte Daten"
    summary += (
        f" (Auswahl: {label}; Klub laut {sources}, Stand {_today():%d.%m.%Y}; "
        + (
            "alle gespeicherten Stationen"
            if ref.past
            else "nur der neueste gespeicherte Klub, frühere zählen nicht"
        )
        + ")"
    )
    unk = None
    if unknown:
        unk = (
            f"Ohne gespeicherten Klub mit Land (für sie unbekannt): {len(unknown)}: "
            + ", ".join(sorted(unknown)[:20])
            + (" …" if len(unknown) > 20 else "")
        )
    body = f"{label}, Klubs {where}: {summary}."
    if rows:
        body += " " + "; ".join(r[-1] for r in rows[:MAX_LISTED]) + "."
    if unk:
        body += f" {unk}."
    computed = _computed_doc(body, label, [a.full_name for a, _ in hits], _basis(basis))
    spalten = [wording().player, "Klub (Liga, Saison)", "Quelle"]
    if not (ref.nations and len(ref.nations) == 1):
        spalten.insert(1, "Nation")
    computed.extra["direct"] = {"summary": summary, "rows": rows, "unknown": unk, "spalten": spalten}
    return ProsaInputs(
        docs=_numbered([computed, *docs[:MAX_SOURCE_DOCS]]),
        athlete_ids=[a.id for a, _ in hits] or list(people),
        scope_note=note,
    )


def _matches(country: str, a: m.Athlete, ref: Ref) -> bool:
    if ref.abroad:
        return bool(a.nationality) and country != a.nationality
    return country == ref.place


def _season_label(y: int | None) -> str:
    return f"{y}/{(y + 1) % 100:02d}" if y else ""
