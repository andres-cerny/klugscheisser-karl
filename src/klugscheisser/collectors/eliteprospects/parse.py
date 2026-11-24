"""Parse one cached Elite Prospects player page (the data blocks of the page) into plain dataclasses.

Pure functions: no database, no network. Input is the format of `data/eliteprospects/raw/<athlete_id>.json`
and of `ep_profiles.raw` (+ athlete_id, eliteprospects_id, url, fetched_at): `playerData`,
`initialLeagueStats`, `initialCupStats`, `initialCareerTotals`, `initialAwards`, `initialTransactions`,
`initialGameLog`; any block may be missing or null.

Rules:
- empty values stay None (never 0); "0" stays 0 (a goalie who dressed but did not play has GP 0).
- rows without a team name are projected stats (EP's own forecast), not facts: dropped.
- never read: photos/image URLs, views, scouting reports, per-game player stats.
- seasons are start years ("2026-2027" -> 2026), like season_stats and career_stints.
- team kind: national | college | youth | club (see `team_kind`). "(all)" rows ("Switzerland (all)",
  league "International (W)") are EP's own sum of all national-team games of a season: kept with kind
  "national" and `aggregate=True`; they repeat the other national rows and must not be added to them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

BASE_URL = "https://www.eliteprospects.com"

# --- team kinds ---------------------------------------------------------------------------------------------

# EP country names (as used for national teams) -> IOC code
COUNTRIES = {
    "Switzerland": "SUI",
    "Sweden": "SWE",
    "Finland": "FIN",
    "Canada": "CAN",
    "USA": "USA",
    "Czechia": "CZE",
    "Czech Republic": "CZE",
    "Germany": "GER",
    "Austria": "AUT",
    "Japan": "JPN",
    "Denmark": "DEN",
    "Norway": "NOR",
    "France": "FRA",
    "Italy": "ITA",
    "Slovakia": "SVK",
    "Hungary": "HUN",
    "China": "CHN",
    "South Korea": "KOR",
    "Korea": "KOR",
    "Great Britain": "GBR",
    "Netherlands": "NED",
    "Poland": "POL",
    "Latvia": "LAT",
    "Slovenia": "SLO",
    "Russia": "RUS",
    "Kazakhstan": "KAZ",
    "Iceland": "ISL",
    "Australia": "AUS",
    "New Zealand": "NZL",
    "Mexico": "MEX",
    "Spain": "ESP",
    "Turkey": "TUR",
    "Ukraine": "UKR",
    "Belarus": "BLR",
    "Estonia": "EST",
    "Lithuania": "LTU",
    "Croatia": "CRO",
    "Romania": "ROU",
    "Belgium": "BEL",
    "Hong Kong": "HKG",
    "Chinese Taipei": "TPE",
    "North Korea": "PRK",
    "Bulgaria": "BUL",
    "Serbia": "SRB",
    "Israel": "ISR",
    "Ireland": "IRL",
    "South Africa": "RSA",
    "Thailand": "THA",
    "Mongolia": "MGL",
    "Singapore": "SGP",
    "Philippines": "PHI",
    "Puerto Rico": "PUR",
}
_COUNTRY_ALT = "|".join(re.escape(c) for c in sorted(COUNTRIES, key=len, reverse=True))
# "Switzerland", "Switzerland U18", "Switzerland Olympics", "Switzerland (all)", "Czechia U18 (women) (all)"
NATIONAL_TEAM_RX = re.compile(
    rf"^(?P<country>{_COUNTRY_ALT})(?P<age> U\d{{2}})?(?: Olympics)?(?: \(women\))?(?P<all> \(all\))?$"
)
# tournaments of national teams (Olympics + qualification, world championships of all ages and divisions,
# Euro Hockey Tour, EP's season sums, youth Olympics, European championships, Universiade …)
NATIONAL_LEAGUE_RX = re.compile(
    r"^((OGQ?|WCQ?|WC D\w+|EC)(\s|$)|WJC|EHT|International|EYOF|YOG|EWIHC|EC-\d|U16 EC|Asian WG"
    r"|Deutschland Cup|Universiade|4 Nations|Rivalry Series|Four Nations|Latam Cup|Dream Nations Cup)",
    re.IGNORECASE,
)
COLLEGE_LEAGUE_RX = re.compile(r"^(NCAA|U ?Sports|CIS\b|ACHA|QCHL|CCHA|NJCAA)", re.IGNORECASE)
COLLEGE_TEAM_RX = re.compile(
    r"\bUniv\.|\bUniversity\b", re.IGNORECASE
)  # not "HC Université Neuchâtel" (club)
# junior / youth leagues: age classes (U15, 19U, J20), Swiss Mini/Novizen/Elit, German Schüler/Jugend/Junioren,
# North American midget/bantam/prep/high-school leagues, Swedish district tournaments, select camps
YOUTH_LEAGUE_RX = re.compile(
    r"\bU ?-?\d{2}\b|\b\d{2}U\b|^J\d{2}\b|^Mini\b|Novizen|Elit\b|Elite Jr|Junior|Junioren|Jugend|Schüler"
    r"|Midget|Bantam|Peewee|\bPW\b|^USHS|^CAHS|^CSS[HV]HL|^JWHL|^AM[BM]?A*HL|^BCEHL|^AFHL|^MFMHL|^FMAAA"
    r"|^SFMAAA|^LLFHL|^Provincial WHL|^OWHL|^NWC-|^Esso Cup|^Brick Invitational|^Challenge Cup"
    r"|^TV-Pucken|^Stålbucklan|^WSI\b|^DNL\b|^Damjunior|^LHD|^QMA|^QMEAA|^HEO\b|^PCBHL|^WAAA|^NAPHL"
    r"|^HPHL|^T1E|^UMHSEHL|^EWHC|^MNHP|^MMCR|^USA-|^US 19U|^WHC U|JHL\b|MAAA|Region \(W\)|^U\d{2}",
    re.IGNORECASE,
)
# not "Junior": "Eisbären Juniors Berlin" plays in the women's top league
YOUTH_TEAM_RX = re.compile(r"\bU ?\d{2}\b|\b\d{2}U\b|\bJ\d{2}\b|\bMidget|\bBantam|\bPeewee", re.I)


def national_team(team: str | None) -> re.Match | None:
    return NATIONAL_TEAM_RX.match((team or "").strip())


def team_kind(team: str | None, league: str | None) -> str:
    """national | college | youth | club for one season row (team name as on EP, league name)."""
    team, league = (team or "").strip(), (league or "").strip()
    if national_team(team) or NATIONAL_LEAGUE_RX.match(league):
        return "national"
    if COLLEGE_LEAGUE_RX.match(league):
        return "college"
    if YOUTH_LEAGUE_RX.search(league):
        return "youth"
    if COLLEGE_TEAM_RX.search(team):
        return "college"
    if YOUTH_TEAM_RX.search(team):
        return "youth"
    return "club"


def stint_team_name(team: str) -> str:
    """Team name for career stints: national rows of one team under one name ("Switzerland Olympics" and
    "Switzerland (all)" -> "Switzerland"; "Switzerland U18 (all)" -> "Switzerland U18")."""
    hit = national_team(team)
    if hit:
        return hit.group("country") + (hit.group("age") or "")
    return team


def short_league(league: str | None) -> str:
    """'SWHL A (W)' -> 'SWHL A', 'World Championship (D1|W)' -> 'World Championship (D1)'."""
    text = re.sub(r"\((\w+)\|W\)", r"(\1)", league or "")
    text = re.sub(r"\s*\(W\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


# --- dataclasses --------------------------------------------------------------------------------------------


@dataclass
class EpSeasonRow:
    season: int  # start year
    team: str
    league: str
    team_kind: str  # national | college | youth | club
    phase: str  # regular | playoffs
    games: int | None = None
    goals: int | None = None
    assists: int | None = None
    points: int | None = None
    penalty_minutes: int | None = None
    plus_minus: int | None = None
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    shutouts: int | None = None
    goals_against: int | None = None
    saves: int | None = None
    save_pct: float | None = None  # 0.918
    gaa: float | None = None
    toi: str | None = None
    cup: bool = False  # from the cup / tournament block (initialCupStats)
    postseason_type: str | None = (
        None  # playoffs | playoffs-qc | relegation | qualification (phase "playoffs")
    )
    team_path: str | None = None  # "/team/19495/zsc-lions-frauen"
    aggregate: bool = False  # EP's "(all)" season sum of a national team

    @property
    def has_stats(self) -> bool:
        return any(getattr(self, f) is not None for f in STAT_FIELDS)


STAT_FIELDS = (
    "games",
    "goals",
    "assists",
    "points",
    "penalty_minutes",
    "plus_minus",
    "wins",
    "losses",
    "ties",
    "shutouts",
    "goals_against",
    "saves",
    "save_pct",
    "gaa",
    "toi",
)


@dataclass
class EpAward:
    season: int
    name: str  # "SDHL (W) Most Points"
    extra: str | None  # "89", ".945", "ZSC Lions Frauen", "Top-3"
    number: str | None  # extra when it is a value ("89", "+63", ".945", "23 pts", "Top-3")
    detail: str | None  # extra when it is not a value (team, month)
    league: str | None
    medal: str | None = None  # gold | silver | bronze for Olympic / world championship (incl. U18) medals
    event: str | None = None  # "Olympische Spiele" | "WM" | "U18-WM" (only for medals)


@dataclass
class EpTransfer:
    date: date | None
    from_team: str | None
    to_team: str | None
    to_team_path: str | None
    from_info: str | None  # "no-team" …
    to_info: str | None  # "retired" | "unknown" | "time-out" …
    extra: str | None  # "3 years", "For 2023/24", "College Commitment"
    source: str | None  # first source link (club press release, league news, tweet)
    type: str | None  # confirmed | extension | loan
    url: str | None  # EP transfer page


@dataclass
class EpDraft:
    league: str  # "PWHL", "PWHL Expansion", "NWHL", "CWHL"
    year: int
    round: int | None
    overall: int | None
    team: str | None


@dataclass
class EpRelative:
    relation: str  # German: "Bruder", "Vater", "Cousin" …
    name: str
    relation_en: str  # as written on EP ("Brother", "Twin Sister")
    path: str | None  # "/player.php?player=95044"


@dataclass
class EpProfileData:
    date_of_birth: date | None = None
    place_of_birth: str | None = None  # city only: "Winterthur"
    place_of_birth_raw: str | None = None  # "Winterthur, SUI" / "Toronto, ON, CAN"
    birth_region: str | None = None  # "ON"
    birth_country: str | None = None  # "SUI"
    height_cm: int | None = None
    weight_kg: int | None = None
    position: str | None = None  # G | D | F
    detailed_position: list[str] = field(default_factory=list)  # ["C"], ["LW", "RW"]
    shoots: str | None = None  # L | R (goalies: catches)
    catches: bool = False  # True: `shoots` is the catching hand of a goalie
    youth_team: str | None = None
    status: str | None = None  # active | retired
    contract: str | None = None  # "28/29", "Junior"
    contract_season_to: int | None = None  # "28/29" -> 2028
    agencies: list[str] = field(default_factory=list)
    nationality: str | None = None  # "Switzerland"
    secondary_nationality: str | None = None
    pronunciation_url: str | None = None


@dataclass
class EpRecord:
    athlete_id: int | None
    ep_id: str | None
    name: str | None
    url: str | None
    fetched_at: datetime | None
    profile: EpProfileData
    seasons: list[EpSeasonRow] = field(default_factory=list)
    career_totals: list[dict] = field(default_factory=list)
    awards: list[EpAward] = field(default_factory=list)
    transfers: list[EpTransfer] = field(default_factory=list)
    drafts: list[EpDraft] = field(default_factory=list)
    relatives: list[EpRelative] = field(default_factory=list)
    highlights: list[dict] = field(default_factory=list)  # [{"name": "Olympic (W) Bronze Medal", "count": 2}]
    links: dict[str, str] = field(default_factory=dict)  # instagram | x | facebook | youtube | linkedin
    latest: dict[str, Any] = field(default_factory=dict)  # team, league, season, jersey_number, status
    form: dict[str, Any] = field(default_factory=dict)  # {"last5": {...}, "last10": {...}}
    projected_dropped: int = 0

    @property
    def is_goalie(self) -> bool:
        return self.profile.position == "G"


# --- small helpers ------------------------------------------------------------------------------------------


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _edges(block: Any, *path: str) -> list[dict]:
    edges = _get(block, *path, "edges") or []
    return [e.get("node", e) for e in edges if isinstance(e, dict)]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace("+", "").strip()))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def save_pct(value: Any) -> float | None:
    """'.918' -> 0.918, '1.000' -> 1.0, '91.84' (form, in percent) -> 0.9184."""
    v = _float(value)
    if v is None:
        return None
    return round(v / 100, 4) if v > 1.0 else v


def season_start(slug: Any) -> int | None:
    """'2026-2027' -> 2026."""
    hit = re.match(r"(\d{4})", str(slug or ""))
    return int(hit.group(1)) if hit else None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# --- profile ------------------------------------------------------------------------------------------------


def contract_season_to(text: str | None) -> int | None:
    """'28/29' -> 2028, '26/27+1' -> 2026; 'Junior', 'Oct. 31' -> None."""
    hit = re.match(r"^(\d{2})/(\d{2})", text or "")
    if not hit:
        return None
    start = 2000 + int(hit.group(1))
    return start if (int(hit.group(1)) + 1) % 100 == int(hit.group(2)) else None


def _place(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """'Toronto, ON, CAN' -> ('Toronto', 'ON', 'CAN'); 'Winterthur, SUI' -> ('Winterthur', None, 'SUI')."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not parts:
        return None, None, None
    country = parts[-1] if len(parts) > 1 and re.fullmatch(r"[A-Z]{3}", parts[-1]) else None
    region = parts[1] if len(parts) > 2 else None
    return parts[0], region, country


def _profile(p: dict) -> EpProfileData:
    city, region, country = _place(_text(p.get("placeOfBirth")))
    shoots, catches = _text(p.get("shoots")), False
    if p.get("position") == "G" or (not shoots and _text(p.get("catches"))):
        shoots, catches = _text(p.get("catches")) or shoots, bool(_text(p.get("catches")))
    agencies = []
    for a in p.get("agencies") or []:
        name = _text(a.get("name") if isinstance(a, dict) else a)
        if name:
            agencies.append(name)
    contract = _text(p.get("contract"))
    return EpProfileData(
        date_of_birth=_date(p.get("dateOfBirth")),
        place_of_birth=city,
        place_of_birth_raw=_text(p.get("placeOfBirth")),
        birth_region=region,
        birth_country=country,
        height_cm=_int(_get(p, "height", "metrics")),
        weight_kg=_int(_get(p, "weight", "metrics")),
        position=_text(p.get("position")),
        detailed_position=[x for x in (p.get("detailedPosition") or []) if x],
        shoots=shoots.upper() if shoots else None,
        catches=catches,
        youth_team=_text(p.get("youthTeam")),
        status=_text(p.get("status")),
        contract=contract,
        contract_season_to=contract_season_to(contract),
        agencies=agencies,
        nationality=_text(_get(p, "nationality", "name")),
        secondary_nationality=_text(_get(p, "secondaryNationality", "name")),
        pronunciation_url=_text(p.get("pronunciationUrl")),
    )


LINK_KEYS = ("instagram", "x", "facebook", "youtube", "linkedin")


def _links(p: dict) -> dict[str, str]:
    links = p.get("links") or {}
    return {k: links[k].strip() for k in LINK_KEYS if isinstance(links.get(k), str) and links[k].strip()}


def _latest(p: dict) -> dict[str, Any]:
    ls = p.get("latestStats") or {}
    if not ls:
        return {}
    return {
        "team": _text(ls.get("teamName")) or _text(_get(ls, "team", "name")),
        "league": _text(ls.get("leagueName")),
        "season": season_start(_get(ls, "season", "slug")),
        "jersey_number": _int(ls.get("jerseyNumber")),
        "status": _text(ls.get("status")),
        "team_path": _text(_get(ls, "team", "eliteprospectsUrlPath")),
    }


FORM_FIELDS = {
    "GP": "games",
    "G": "goals",
    "A": "assists",
    "PTS": "points",
    "PM": "plus_minus",
    "GA": "goals_against",
    "SV": "saves",
}


def _form(p: dict) -> dict[str, Any]:
    """Free last-5 / last-10 game totals of the current season (EP's `latestGameLogsAggregated`)."""
    out: dict[str, Any] = {}
    for agg in p.get("latestGameLogsAggregated") or []:
        limit, stats = agg.get("limit"), agg.get("stats") or {}
        if not limit or not stats:
            continue
        row = {name: _int(stats.get(key)) for key, name in FORM_FIELDS.items()}
        row["save_pct"] = save_pct(stats.get("SVP"))
        out[f"last{limit}"] = row
    return out


RELATIONS_DE = {
    "brother": "Bruder",
    "brothers": "Bruder",
    "sister": "Schwester",
    "sisters": "Schwester",
    "father": "Vater",
    "mother": "Mutter",
    "son": "Sohn",
    "daughter": "Tochter",
    "twin brother": "Zwillingsbruder",
    "twin-brother": "Zwillingsbruder",
    "twin sister": "Zwillingsschwester",
    "twin-sister": "Zwillingsschwester",
    "uncle": "Onkel",
    "aunt": "Tante",
    "cousin": "Cousin",
    "cousins": "Cousin",
    "grandfather": "Grossvater",
    "grandmother": "Grossmutter",
    "husband": "Ehemann",
    "wife": "Ehefrau",
    "nephew": "Neffe",
    "niece": "Nichte",
    "brother-in-law": "Schwager",
    "sister-in-law": "Schwägerin",
    "father-in-law": "Schwiegervater",
    "stepfather": "Stiefvater",
    "stepbrother": "Stiefbruder",
    "half-brother": "Halbbruder",
    "half-sister": "Halbschwester",
    "partner": "Partnerin",
    "fiance": "Verlobter",
    "girlfriend": "Partnerin",
    "boyfriend": "Partner",
}
_LINK_RX = re.compile(r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def relatives(description: str | None) -> list[EpRelative]:
    """'Brother: <a …>Mirco Müller</a>' / 'Brothers: <a>A</a>, <a>B</a>' -> one EpRelative per person.
    Lines that are not a family relation ('Goalie profile: …', 'Skater profile', free text) are ignored."""
    out: list[EpRelative] = []
    for line in re.split(r"<br\s*/?>|\n", description or "", flags=re.IGNORECASE):
        hit = re.match(r"\s*([A-Za-z][A-Za-z -]*?)\s*:\s*(.+)$", line.strip(), re.DOTALL)
        if not hit:
            continue
        relation_en = hit.group(1).strip()
        relation = RELATIONS_DE.get(relation_en.lower())
        if relation is None:
            continue
        links = _LINK_RX.findall(hit.group(2))
        if links:
            people = [(path, _strip_tags(name)) for path, name in links]
        else:
            people = [(None, n.strip()) for n in re.split(r",| & | and ", _strip_tags(hit.group(2)))]
        for path, name in people:
            if name and len(name) <= 100:
                out.append(
                    EpRelative(relation=relation, name=name, relation_en=relation_en, path=path or None)
                )
    return out


_DRAFT_RX = re.compile(
    r"Drafted\s*<a[^>]*>\s*(\d{4}),\s*(\d+)\s*#(\d+)\s*by\s*([^<]+?)\s*</a>\s*in the\s*([^<]+?)\s*Draft",
    re.IGNORECASE,
)


def drafts(player_data: dict) -> list[EpDraft]:
    """Draft picks from `pwhlDraftSelections` / `playerDraftSelections` and the biography lines
    ('Drafted 2023, 1 #3 by Boston Fleet in the PWHL Draft'); one per league and year."""
    found: dict[tuple, EpDraft] = {}
    for key, league in (("pwhlDraftSelections", "PWHL"), ("playerDraftSelections", None)):
        for e in _edges(player_data.get(key)):
            year = _int(e.get("year"))
            if not year:
                continue
            path = e.get("eliteprospectsUrlPath") or ""
            name = league or _draft_league_from_path(path) or "Draft"
            found[(name, year)] = EpDraft(
                league=name,
                year=year,
                round=_int(e.get("round")),
                overall=_int(e.get("overall")),
                team=_text(e.get("teamName")) or _text(_get(e, "team", "name")),
            )
    bio = _get(player_data, "player", "biographyAsHTML") or ""
    for year, rnd, overall, team, league in _DRAFT_RX.findall(bio):
        key = (league.strip(), int(year))
        if key not in found:
            found[key] = EpDraft(
                league=key[0],
                year=key[1],
                round=int(rnd),
                overall=int(overall),
                team=html.unescape(team).strip(),
            )
    return sorted(found.values(), key=lambda d: (d.year, d.league))


def _draft_league_from_path(path: str) -> str | None:
    """'/draft/nwhl-draft/2020' -> 'NWHL'."""
    hit = re.match(r"/draft/([a-z0-9-]+)-draft/", path)
    return hit.group(1).replace("-", " ").upper() if hit else None


# --- seasons ------------------------------------------------------------------------------------------------

_STAT_KEYS = {
    "GP": "games",
    "G": "goals",
    "A": "assists",
    "PTS": "points",
    "PIM": "penalty_minutes",
    "PM": "plus_minus",
    "W": "wins",
    "L": "losses",
    "T": "ties",
    "SO": "shutouts",
    "GA": "goals_against",
    "SVS": "saves",
}


def _stats(stats: dict) -> dict[str, Any]:
    out: dict[str, Any] = {name: _int(stats.get(key)) for key, name in _STAT_KEYS.items()}
    out["save_pct"] = save_pct(stats.get("SVP"))
    out["gaa"] = _float(stats.get("GAA"))
    out["toi"] = _text(stats.get("TOI"))
    return out


def season_rows(raw: dict) -> tuple[list[EpSeasonRow], int]:
    """Season rows of the league and cup blocks (regular season; postseason with games as phase
    'playoffs'), and the number of dropped projected rows. One row per season/league/team/phase."""
    rows: dict[tuple, EpSeasonRow] = {}
    dropped = 0
    for block, cup in (("initialLeagueStats", False), ("initialCupStats", True)):
        for e in _edges(raw.get(block), "playerStats"):
            team = _text(e.get("teamName")) or _text(_get(e, "team", "name"))
            season = season_start(_get(e, "season", "slug")) or _int(_get(e, "season", "startYear"))
            if not team or e.get("statsType") == "projected":
                dropped += 1  # EP's forecast for the season, not a fact
                continue
            if season is None:
                continue
            league = (_text(e.get("leagueName")) or "")[:50]
            kind = team_kind(team, league)
            hit = national_team(team)
            base = dict(
                season=season,
                team=team[:200],
                league=league,
                team_kind=kind,
                cup=cup,
                team_path=_text(_get(e, "team", "eliteprospectsUrlPath")),
                aggregate=bool(hit and hit.group("all")),
            )
            phases = [("regular", e.get("regularStats") or {}, None)]
            post = e.get("postseasonStats") or {}
            if _int(post.get("GP")) is not None:
                phases.append(("playoffs", post, _text(e.get("postseasonType"))))
            for phase, stats, ptype in phases:
                row = EpSeasonRow(**base, phase=phase, postseason_type=ptype, **_stats(stats))
                key = (row.season, row.league, row.team, row.phase)
                old = rows.get(key)
                if old is None or (row.games or 0) > (old.games or 0):  # one double row in 238 pages
                    rows[key] = row
    out = sorted(rows.values(), key=lambda r: (r.season, r.phase, r.cup, r.team_kind, r.team))
    return out, dropped


def _career_totals(raw: dict) -> list[dict]:
    out = []
    for e in _edges(raw.get("initialCareerTotals"), "playerStatsAllTimeLeague"):
        league = _text(e.get("leagueName"))
        if not league:
            continue
        reg, post = e.get("regularStats") or {}, e.get("postseasonStats") or {}
        incomplete = sorted(
            _STAT_KEYS.get(k, k.lower())
            for k, v in (e.get("incompleteRegularStats") or {}).items()
            if v is True
        )
        row = {
            "league": league,
            "team_kind": team_kind(None, league),
            "seasons": _int(e.get("numberOfRegularSeasons")),
            "postseason_seasons": _int(e.get("numberOfPostseasonSeasons")),
            "regular": {**_stats(reg), "points_per_game": _float(reg.get("PPG"))} if reg else None,
            "playoffs": {**_stats(post), "points_per_game": _float(post.get("PPG"))} if post else None,
            "incomplete": incomplete,  # EP marks totals that miss some seasons
        }
        out.append(row)
    return out


# --- awards and transfers -----------------------------------------------------------------------------------

_MEDAL_RX = re.compile(r"^(Olympic|World Championship|U18 WJC) \(W\) (Gold|Silver|Bronze) Medal$")
MEDAL_EVENT_DE = {"Olympic": "Olympische Spiele", "World Championship": "WM", "U18 WJC": "U18-WM"}
_VALUE_RX = re.compile(r"^([+-]?\d*[.,]?\d+\s*(%|pts|min|gp)?|Top-\d+)$", re.IGNORECASE)


def _awards(raw: dict) -> list[EpAward]:
    out: dict[tuple, EpAward] = {}
    for e in _edges(raw.get("initialAwards"), "awards"):
        name = _text(e.get("awardTypeName")) or _text(_get(e, "awardType", "name"))
        season = season_start(_get(e, "season", "slug"))
        if not name or season is None:
            continue
        extra = _text(e.get("extra"))
        is_value = bool(extra and _VALUE_RX.match(extra))
        medal = _MEDAL_RX.match(name)
        award = EpAward(
            season=season,
            name=name,
            extra=extra,
            number=extra if is_value else None,
            detail=extra if extra and not is_value else None,
            league=_text(_get(e, "awardType", "league")) or _text(e.get("awardTypeLeague")),
            medal=medal.group(2).lower() if medal else None,
            event=MEDAL_EVENT_DE[medal.group(1)] if medal else None,
        )
        out.setdefault((season, name, extra), award)
    return sorted(out.values(), key=lambda a: (a.season, a.name))


def _transfers(raw: dict) -> list[EpTransfer]:
    out = []
    for e in _edges(raw.get("initialTransactions"), "transfers"):
        sources = [s for s in (e.get("sources") or []) if isinstance(s, str) and s.strip()]
        path = _text(e.get("eliteprospectsUrlPath"))
        out.append(
            EpTransfer(
                date=_date(e.get("date")),
                from_team=_text(_get(e, "fromTeam", "name")),
                to_team=_text(_get(e, "toTeam", "name")),
                to_team_path=_text(_get(e, "toTeam", "eliteprospectsUrlPath")),
                from_info=_text(e.get("fromInfo")),
                to_info=_text(e.get("toInfo")),
                extra=_text(e.get("extra")),
                source=sources[0].strip() if sources else None,
                type=_text(e.get("type")),
                url=BASE_URL + path if path else None,
            )
        )
    return sorted(out, key=lambda t: t.date or date.min)


# --- public -------------------------------------------------------------------------------------------------


def parse(raw: dict) -> EpRecord:
    """One cached player page -> EpRecord. Never raises on missing blocks."""
    player_data = raw.get("playerData") or {}
    p = player_data.get("player") or {}
    seasons, dropped = season_rows(raw)
    highlights = [
        {"name": h["name"], "count": _int(h.get("count"))}
        for h in player_data.get("playerHighlights") or []
        if isinstance(h, dict) and h.get("name")
    ]
    path = _text(p.get("eliteprospectsUrlPath"))
    return EpRecord(
        athlete_id=_int(raw.get("athlete_id")),
        ep_id=_text(raw.get("eliteprospects_id")) or _text(p.get("id")),
        name=_text(raw.get("name")) or _text(p.get("name")),
        url=_text(raw.get("url")) or (BASE_URL + path if path else None),
        fetched_at=_datetime(raw.get("fetched_at")),
        profile=_profile(p),
        seasons=seasons,
        career_totals=_career_totals(raw),
        awards=_awards(raw),
        transfers=_transfers(raw),
        drafts=drafts(player_data),
        relatives=relatives(p.get("profileDescriptionAsHTML")),
        highlights=highlights,
        links=_links(p),
        latest=_latest(p),
        form=_form(p),
        projected_dropped=dropped,
    )
