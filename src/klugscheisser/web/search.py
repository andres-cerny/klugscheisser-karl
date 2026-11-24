"""Forgiving search for the web app's `/suche` (ui-loop 2026-10-17). Web only: MCP and the chat keep their own
lookups (`repo.find_athletes`, `tools.resolve_event`), nothing here changes them.

What it forgives:
- typos in names, teams and events (one wrong letter in words of 5–7 letters, two from 8 letters on);
- missing accents/umlauts and case ("frolunda", "geneve");
- country names in German, English, French and Italian ("Switzerland", "Suisse", "Svizzera" → Schweiz);
- word order and partial words while typing ("zsc lio", "Switz").

English-named national-team records from Wikipedia/Elite Prospects imports ("Finland women's national ice
hockey team") are duplicates of the German national teams ("Finnland"): the German team is shown, the
duplicate is folded into its row ("auch erfasst als …", still linked) — no data is deleted.

When nothing is found, `meinten` holds up to three close spellings ("Meinten Sie …?").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser import tiers
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.names import normalize

# IOC code -> names people type (de / en / fr / it + demonyms and short forms), normalised on load
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "SUI": (
        "Schweiz",
        "Switzerland",
        "Suisse",
        "Svizzera",
        "Svizra",
        "Swiss",
        "Schweizer",
        "Schweizerinnen",
        "Nati",
        "Frauen-Nati",
        "CH",
        "SUI",
    ),
    "GER": ("Deutschland", "Germany", "Allemagne", "Germania", "German", "Deutsche", "GER"),
    "AUT": ("Österreich", "Austria", "Autriche", "Austrian", "Österreicherinnen", "Österreicher", "AUT"),
    "FIN": (
        "Finnland",
        "Finland",
        "Finlande",
        "Finlandia",
        "Suomi",
        "Finnish",
        "Finninnen",
        "Finnen",
        "Naisleijonat",
        "FIN",
    ),
    "SWE": (
        "Schweden",
        "Sweden",
        "Suède",
        "Suede",
        "Svezia",
        "Sverige",
        "Swedish",
        "Schwedinnen",
        "Damkronorna",
        "SWE",
    ),
    "CZE": (
        "Tschechien",
        "Czechia",
        "Czech Republic",
        "Tchéquie",
        "Tchequie",
        "Repubblica Ceca",
        "Cechia",
        "Česko",
        "Tschechische Republik",
        "Czech",
        "Tschechen",
        "CZE",
    ),
    "CAN": ("Kanada", "Canada", "Canadian", "Kanadierinnen", "Kanadier", "CAN"),
    "USA": (
        "USA",
        "United States",
        "US",
        "Vereinigte Staaten",
        "États-Unis",
        "Etats Unis",
        "Stati Uniti",
        "America",
        "Amerika",
        "American",
        "Amerikanerinnen",
        "Amerikaner",
    ),
    "JPN": ("Japan", "Japon", "Giappone", "Japanese", "Smile Japan", "JPN"),
    "FRA": ("Frankreich", "France", "Francia", "French", "Französinnen", "Franzosen", "FRA"),
    "ITA": ("Italien", "Italy", "Italie", "Italia", "Italian", "Italienerinnen", "Italiener", "ITA"),
    "DEN": ("Dänemark", "Denmark", "Danemark", "Danimarca", "Danmark", "Danish", "DEN"),
    "SVK": ("Slowakei", "Slovakia", "Slovaquie", "Slovacchia", "Slovensko", "SVK"),
    "HUN": ("Ungarn", "Hungary", "Hongrie", "Ungheria", "HUN"),
    "NOR": ("Norwegen", "Norway", "Norvège", "Norvegia", "Norge", "Norweger", "NOR"),
    "CHN": ("China", "Chine", "Cina", "CHN"),
    "KOR": ("Südkorea", "Korea", "South Korea", "Corée", "Corea", "KOR"),
    "GBR": ("Grossbritannien", "Great Britain", "Grande-Bretagne", "Gran Bretagna", "GBR"),
    "NED": ("Niederlande", "Netherlands", "Holland", "Pays-Bas", "Paesi Bassi", "NED"),
    "POL": ("Polen", "Poland", "Pologne", "Polonia", "POL"),
    "LAT": ("Lettland", "Latvia", "Lettonie", "Lettonia", "LAT"),
    "SLO": ("Slowenien", "Slovenia", "Slovénie", "SLO"),
    "RUS": ("Russland", "Russia", "Russie", "RUS"),
    "KAZ": ("Kasachstan", "Kazakhstan", "KAZ"),
}
COUNTRY_ALIASES: dict[str, str] = {normalize(n): ioc for ioc, names in _COUNTRY_NAMES.items() for n in names}
_SHORT_ALIASES = {a for a in COUNTRY_ALIASES if len(a) <= 3}  # "us", "ch", "sui": whole query only

# event words in other languages -> the word used in our event names
_EVENT_WORDS = {
    "olympia": "olympische",
    "olympics": "olympische",
    "olympic": "olympische",
    "olympiques": "olympische",
    "olimpiadi": "olympische",
    "olympiade": "olympische",
    "jeux": "olympische",
    "weltmeisterschaft": "wm",
    "worlds": "wm",
    "world": "wm",
    "championship": "wm",
    "championships": "wm",
    "mondiaux": "wm",
    "mondiali": "wm",
    "mondial": "wm",
    "mondiale": "wm",
    "championnat": "wm",
    "campionato": "wm",
    "monde": "wm",
    "mondo": "wm",
}
# words in other languages that only say "women's" (ui-loop round 4): «EV Zug Frauen» finds "EV Zug Damen",
# «Davos Damen» "HC Davos Ladies", «Lausanne Frauen» "Lausanne HC Féminin"; teams without such a word do not match
_WOMEN_WORDS = frozenset(
    normalize(w)
    for w in (
        "Frauen", "Damen", "Ladies", "Women", "Women's", "Woman", "Féminin", "Féminine", "Femmes", "Dames",
        "Donne", "Femminile", "Naiset", "Damer", "Girls",
    )
)  # fmt: skip
_WOMEN_BAG = " ".join(sorted(_WOMEN_WORDS))
# filler words dropped from event queries («championnat du monde», «world championship of …»)
_EVENT_STOP = frozenset(
    ("du", "de", "des", "la", "le", "les", "of", "the", "di", "del", "della", "der", "die", "das", "im", "in")
)
# "Finland women's national ice hockey team" (normalised): an English duplicate of a national team
_EN_NATIONAL = re.compile(r"\bwomens national (ice hockey )?team\b")
_EN_NATIONAL_MEN = re.compile(
    r"\bmens national (floorball |ice hockey )?team\b"
)  # 2026-11-24: men's floorball


def _en_national() -> re.Pattern[str]:
    from klugscheisser.sports import current_spec

    return _EN_NATIONAL_MEN if current_spec().men else _EN_NATIONAL


def allowed_typos(word: str) -> int:
    n = len(word)
    return 0 if n <= 4 else 1 if n <= 7 else 2


def edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Damerau-Levenshtein (adjacent swaps count once); stops early above `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def word_match(q: str, words: list[str], last: bool = False) -> int | None:
    """Cost of query word `q` against a list of words: 0 exact/prefix (prefix only for the word being typed or
    from 3 letters), else the edit distance within the allowance; None = no match."""
    best: int | None = None
    for w in words:
        if w == q or (w.startswith(q) and (last or len(q) >= 3)):
            return 0
        k = allowed_typos(q)
        if k:
            d = min(
                edit_distance(q, w, k), edit_distance(q, w[: len(q)], k) + 1 if len(w) > len(q) else k + 1
            )
            if d <= k and (best is None or d < best):
                best = d
    return best


def text_score(q: str, name: str, extra: tuple[str, ...] = ()) -> int:
    """0–95: how well the normalised query fits a normalised name (plus extra words such as key/short name)."""
    if not q or not name:
        return 0
    if q == name or q in extra:
        return 95
    if name.startswith(q):
        return 88
    words = name.split() + [w for x in extra for w in x.split()]
    if f" {q}" in f" {name}":
        return 80
    toks = q.split()
    costs = [word_match(t, words, last=i == len(toks) - 1) for i, t in enumerate(toks)]
    if all(c is not None for c in costs):
        typos = sum(c for c in costs if c)
        return 75 - 12 * typos
    if len(q) >= 4 and q in name:
        return 60
    return 0


def countries_in_query(q: str) -> set[str]:
    """IOC codes named by the (normalised) query: whole query or a word / two-word phrase, typos allowed."""
    if not q:
        return set()
    out = set()
    if q in COUNTRY_ALIASES:
        out.add(COUNTRY_ALIASES[q])
    toks = q.split()
    phrases = toks + [" ".join(toks[i : i + 2]) for i in range(len(toks) - 1)]
    for p in phrases:
        if p in COUNTRY_ALIASES and p not in _SHORT_ALIASES:
            out.add(COUNTRY_ALIASES[p])
            continue
        if len(p) < 4:
            continue
        for alias, ioc in COUNTRY_ALIASES.items():
            if alias in _SHORT_ALIASES:
                continue
            if (alias.startswith(p) and len(toks) == 1 and len(p) >= 4) or (
                allowed_typos(p) and edit_distance(p, alias, allowed_typos(p)) <= allowed_typos(p)
            ):
                out.add(ioc)
    return out


@dataclass
class TeamHit:
    team: m.Team
    score: int
    also: list[m.Team] = field(default_factory=list)  # folded English duplicates


def _team_info(tm: m.Team) -> str:
    if tm.kind == "national":
        return " · ".join(x for x in ("Nationalteam", tm.country) if x)
    return " · ".join(x for x in (tm.league, tm.country) if x)


def find_teams(s: Session, query: str, limit: int = 5) -> list[TeamHit]:
    q = normalize(query or "")
    if not q:
        return []
    teams = list(s.scalars(select(m.Team)))
    national = {tm.country: tm for tm in teams if tm.kind == "national" and tm.country}
    codes = countries_in_query(q)
    hits: dict[int, TeamHit] = {}

    def canonical(tm: m.Team) -> tuple[m.Team, bool]:
        """An English national-team record whose name starts with the same country → the German team."""
        name = normalize(tm.name)
        hit = _en_national().search(name) if tm.kind != "national" and tm.country in national else None
        if hit and COUNTRY_ALIASES.get(name[: hit.start()].strip()) == tm.country:
            return national[tm.country], True
        return tm, False

    for tm in teams:
        name = normalize(tm.name)
        extra = tuple(x for x in (normalize(tm.short_name or ""), normalize(tm.key)) if x)
        if _WOMEN_WORDS.intersection(name.split()):
            extra += (_WOMEN_BAG,)
        score = text_score(q, name, extra)
        if tm.kind == "national" and tm.country in codes:
            score = max(score, 100)
        if not score:
            continue
        target, dup = canonical(tm)
        if dup:
            score = max(score, 100 if target.country in codes else score)
        hit = hits.setdefault(target.id, TeamHit(target, 0))
        hit.score = max(hit.score, score)
    # fold every English duplicate into its German team when that team is listed
    for tm in teams:
        target, dup = canonical(tm)
        if dup and target.id in hits and tm not in hits[target.id].also:
            hits[target.id].also.append(tm)
    ordered = sorted(
        hits.values(), key=lambda h: (-h.score, h.team.kind != "national", normalize(h.team.name))
    )
    return ordered[:limit]


def _event_hay(ev: m.Event) -> list[str]:
    hay = normalize(" ".join(filter(None, [ev.name, ev.key.replace("-", " "), ev.city]))).split()
    # "Women's Euro Hockey Tour" and "Frauen-WM" both answer «Frauen …» / «Women's …»
    return [*hay, "frauen"] if _WOMEN_WORDS.intersection(hay) else hay


def find_events(s: Session, query: str, limit: int = 5) -> list[m.Event]:
    q = normalize(query or "")
    if not q:
        return []
    toks = [
        "frauen" if t in _WOMEN_WORDS else _EVENT_WORDS.get(t, t)
        for t in q.split()
        if t not in _EVENT_STOP or len(q.split()) == 1
    ] or q.split()
    scored = []
    for ev in s.scalars(select(m.Event).where(m.Event.kind != "game").order_by(m.Event.starts_at.desc())):
        hay = _event_hay(ev)
        costs = [word_match(t, hay, last=i == len(toks) - 1) for i, t in enumerate(toks)]
        if all(c is not None for c in costs):
            scored.append((sum(costs), ev))
    scored.sort(key=lambda x: x[0])
    return [ev for _, ev in scored[:limit]]


def _aliases(s: Session) -> list[tuple[int, str]]:
    return [
        (int(i), str(al))
        for i, al in s.execute(select(m.AthleteAlias.athlete_id, m.AthleteAlias.alias_normalized))
    ]


def find_people(s: Session, query: str, limit: int = 8) -> list[m.Athlete]:
    q = normalize(query or "")
    people: dict[int, m.Athlete] = {}
    if len(q) >= 2:  # prefix/word-start match while typing, before the typo matches
        rows = s.scalars(
            select(m.Athlete)
            .join(m.AthleteAlias)
            .where(
                or_(
                    m.AthleteAlias.alias_normalized.like(f"{q}%"),
                    m.AthleteAlias.alias_normalized.like(f"% {q}%"),
                )
            )
            .distinct()
            .limit(limit)
        )
        people.update({a.id: a for a in rows})
    if len(people) < limit and len(q) >= 3:
        for match in repo.find_athletes(s, query, limit=limit):
            people.setdefault(match.athlete.id, match.athlete)
    if len(people) < limit and len(q) >= 4:
        # word-wise typos the trigram match misses (swapped letters: "Stlader", short names: "Enzlr")
        best: dict[int, int] = {}
        for aid, alias in _aliases(s):
            if aid not in people and (sc := text_score(q, alias)) >= 51:
                best[aid] = max(best.get(aid, 0), sc)
        for aid in sorted(best, key=lambda i: -best[i])[: limit - len(people)]:
            if a := s.get(m.Athlete, aid):
                people[aid] = a
    return list(people.values())[:limit]


def did_you_mean(s: Session, query: str, limit: int = 3) -> list[dict[str, str]]:
    """Close spellings when nothing matched (one letter more tolerance than the search, per word), from person
    names, teams and events. [{"label", "href"}] — the link opens the page directly."""
    q = normalize(query or "")
    toks = [t for t in q.split() if len(t) >= 3]
    if not toks or len(q) < 4:
        return []
    cands: dict[str, tuple[str, str]] = {}  # normalised spelling -> (label, href)
    names, rank = {}, {}  # rank: equal spellings → tier 1 before tier 2 before the rest (the Swiss focus)
    for aid, name, tier, manual in s.execute(
        select(m.Athlete.id, m.Athlete.full_name, m.Athlete.tier, m.Athlete.tier_manual)
    ):
        names[aid], rank[f"/person/{aid}"] = name, manual or tier or 3
    for aid, alias in _aliases(s):
        if aid in names:
            cands.setdefault(alias, (names[aid], f"/person/{aid}"))
    en_national = _en_national()
    for tm in s.scalars(select(m.Team)):
        if not en_national.search(normalize(tm.name)):
            cands.setdefault(normalize(tm.name), (tm.name, f"/team/{tm.id}"))
    for ev in s.scalars(select(m.Event).where(m.Event.kind != "game")):
        cands.setdefault(normalize(ev.name), (ev.name, f"/event/{quote(ev.key)}"))
    scored: list[tuple[int, str, str]] = []
    for norm, (label, href) in cands.items():
        words = [w for w in norm.split() if len(w) >= 3]
        cost = 0
        for t in toks:
            k = allowed_typos(t) + 1
            d = min((edit_distance(t, w, k) for w in words), default=k + 1)
            if d > k:
                break
            cost += d
        else:
            scored.append((cost, label, href))
    seen, res = set(), []
    for _cost, _rank, label, href in sorted((c, rank.get(h, 2), lb, h) for c, lb, h in scored):
        if href not in seen:
            seen.add(href)
            res.append({"label": label, "href": href})
    return res[:limit]


def search(s: Session, query: str, limit: int = 8, *, head=None, event_brief=None) -> dict[str, Any]:
    """Persons, teams, events for the query (+ `meinten` when all three are empty)."""
    from klugscheisser.mcp_server.present import athlete_head
    from klugscheisser.web.photo import stored_photo

    head = head or athlete_head
    personen = []
    for a in find_people(s, query, limit):
        h = head(s, a)
        club = h["vereine_laut_quellen"][0]["team"] if h["vereine_laut_quellen"] else None
        badge = tiers.badge(h["tier"], h["quellen"])
        foto = stored_photo(s, a.id) or {}
        personen.append(
            {
                "id": a.id,
                "name": a.full_name,
                "info": " · ".join(x for x in (h["position"], a.nationality, club, badge) if x),
                # ui-loop round 3: result rows show a small photo (or initials) and the fact line apart
                "fakten": " · ".join(x for x in (h["position"], a.nationality, club) if x),
                "badge": badge,
                "foto": foto.get("thumb"),
                "foto_credit": (
                    f"Foto: {foto.get('author')}, {foto.get('licence')}, Wikimedia Commons" if foto else None
                ),
                "tier": h["tier"],
            }
        )
    teams = [
        {
            "id": hit.team.id,
            "name": hit.team.name,
            "info": _team_info(hit.team),
            "auch": [{"id": x.id, "name": x.name} for x in hit.also],
        }
        for hit in find_teams(s, query)
    ]
    anlaesse = [event_brief(ev) for ev in find_events(s, query)] if event_brief else []
    res = {"personen": personen, "teams": teams, "anlaesse": anlaesse, "meinten": []}
    if not (personen or teams or anlaesse):
        res["meinten"] = did_you_mean(s, query)
    return res
