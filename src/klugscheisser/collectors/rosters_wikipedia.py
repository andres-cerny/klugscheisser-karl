"""v8: tournament rosters from a Wikipedia roster page (e.g. "Ice hockey at the 2026 Winter Olympics – Women's team
rosters"): per team the head coach and every player with jersey number, position, birth date and club.

The page cites the official IIHF roster PDFs (www.iihf.com is behind bot protection, so we never fetch them); the
PDF link is kept as the team's source next to the Wikipedia page. Output is written as reviewable YAML seeds
(seeds/rosters/<event>-<nat>.yaml) and loaded by seed.loader.load_roster.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import httpx

from klugscheisser.collectors.wikidata import USER_AGENT

API = "https://en.wikipedia.org/w/api.php"

COUNTRY_CODES = {
    "Canada": "CAN",
    "Czech Republic": "CZE",
    "Czechia": "CZE",
    "Finland": "FIN",
    "Switzerland": "SUI",
    "United States": "USA",
    "France": "FRA",
    "Germany": "GER",
    "Italy": "ITA",
    "Japan": "JPN",
    "Sweden": "SWE",
    "Denmark": "DEN",
    "Austria": "AUT",
    "Hungary": "HUN",
    "Norway": "NOR",
    "Slovakia": "SVK",
    "China": "CHN",
}


@dataclass
class RosterPlayer:
    name: str
    number: int | None
    pos: str | None
    birth: date | None
    club: str | None
    club_country: str | None
    captain: str | None = None  # "C" | "A"
    wiki: str | None = None  # Wikipedia article title, if linked


@dataclass
class TeamRoster:
    country: str  # IOC code
    section: str  # heading on the page
    head_coach: str | None = None
    head_coach_wiki: str | None = None
    head_coach_country: str | None = None
    official_url: str | None = None  # IIHF roster PDF cited by the page
    players: list[RosterPlayer] = field(default_factory=list)


def fetch_wikitext(page: str, client: httpx.Client | None = None) -> str:
    http = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    r = http.get(
        API,
        params={"action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": 2},
    )
    r.raise_for_status()
    return r.json()["parse"]["wikitext"]


def page_url(page: str) -> str:
    return "https://en.wikipedia.org/wiki/" + page.replace(" ", "_")


_SORTNAME = re.compile(r"\{\{\s*sortname\s*\|([^}]*)\}\}", re.I)
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_BIRTH = re.compile(r"birth date and age2?\s*\|[^}]*?\|(\d{4})\|(\d{1,2})\|(\d{1,2})\s*(?:\||\}\})", re.I)
_FLAG = re.compile(r"\{\{\s*flagicon\s*\|\s*([A-Z]{3})", re.I)
_CAPTAIN = re.compile(r"\[\[Captain \(ice hockey\)[^|\]]*\|([CA])\]\]")
_HEAD_COACH = re.compile(
    r"Head coach:\s*(?:\{\{flagicon\|([A-Z]{3})\}\}\s*)?\[\[([^\]|]+)(?:\|([^\]]+))?\]\]"
)
_REF_URL = re.compile(r"url=(https?://[^|}\s]+)")


def _name(cell: str) -> tuple[str, str | None] | None:
    m = _SORTNAME.search(cell)
    if m:
        args = [a.strip() for a in m.group(1).split("|")]
        pos = [a for a in args if "=" not in a]
        named = dict(a.split("=", 1) for a in args if "=" in a)
        if len(pos) < 2:
            return None
        name = f"{pos[0]} {pos[1]}".strip()
        if named.get("nolink"):
            return name, None
        if len(pos) >= 3 and pos[2]:
            return name, pos[2]
        dab = named.get("dab")
        return name, f"{name} ({dab})" if dab else name
    m = _LINK.search(cell)
    if m:
        return (m.group(2) or m.group(1)).strip(), m.group(1).strip()
    text = re.sub(r"<[^>]+>|'''?|align=\"?\w+\"?|style=\"[^\"]*\"", "", cell).strip(" |")
    return (text, None) if text else None


def _club(cell: str) -> tuple[str | None, str | None]:
    flag = _FLAG.search(cell)
    link = _LINK.search(cell)
    club = (link.group(2) or link.group(1)).strip() if link else None
    return club, flag.group(1).upper() if flag else None


def _int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def parse_row(row: str) -> RosterPlayer | None:
    body = row.strip()
    if not body.startswith("|"):
        return None
    cells = [c.strip() for c in body.lstrip("|").split("||")]
    if len(cells) < 4:
        return None
    named = _name(cells[2])
    if named is None:
        return None
    name, wiki = named
    birth = None
    for cell in cells[3:]:
        m = _BIRTH.search(cell)
        if m:
            try:
                birth = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                birth = None
            break
    club, club_country = _club(cells[-1])
    club_country = {"SWI": "SUI"}.get(club_country or "", club_country)
    captain = _CAPTAIN.search(cells[2])
    pos = re.sub(r"[^GDF]", "", cells[1].upper())[:1] or None
    return RosterPlayer(
        name=name,
        number=_int(cells[0]),
        pos=pos,
        birth=birth,
        club=club,
        club_country=club_country,
        captain=captain.group(1) if captain else None,
        wiki=wiki,
    )


def parse(wikitext: str) -> list[TeamRoster]:
    """All team sections (=== Country ===) with a roster table."""
    out = []
    parts = re.split(r"^===\s*([^=]+?)\s*===\s*$", wikitext, flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2], strict=False):
        code = COUNTRY_CODES.get(heading.strip())
        if code is None or "{|" not in body:
            continue
        team = TeamRoster(country=code, section=heading.strip())
        coach = _HEAD_COACH.search(body)
        if coach:
            team.head_coach_country = coach.group(1)
            team.head_coach_wiki = coach.group(2).strip()
            team.head_coach = (coach.group(3) or coach.group(2)).strip()
            ref = _REF_URL.search(body[coach.end() : coach.end() + 600])
            team.official_url = ref.group(1) if ref else None
        table = body[body.index("{|") : body.index("|}", body.index("{|"))]
        for row in table.split("|-")[1:]:
            player = parse_row(row)
            if player is not None:
                team.players.append(player)
        out.append(team)
    return out


def to_seed(
    team: TeamRoster,
    event_key: str,
    page: str,
    season: str,
    list_type: str = "confirmed",
    qids: dict[str, str] | None = None,
) -> dict:
    """One roster seed (format of seeds/rosters/*.yaml)."""
    qids = qids or {}
    players = []
    for p in team.players:
        row: dict = {"name": p.name, "pos": p.pos, "number": p.number}
        if p.birth:
            row["birth"] = p.birth.isoformat()
        if p.captain:
            row["captain"] = p.captain
        if p.club:
            row["club"] = p.club
            if p.club_country:
                row["country"] = p.club_country
        if p.wiki:
            row["wiki"] = p.wiki
            if qids.get(p.wiki):
                row["qid"] = qids[p.wiki]
        players.append(row)
    staff = []
    if team.head_coach:
        coach: dict = {"name": team.head_coach, "role": "Head coach"}
        # the page shows a flag only for a coach from another country
        coach["nationality"] = team.head_coach_country or team.country
        if team.head_coach_wiki:
            coach["wiki"] = team.head_coach_wiki
            if qids.get(team.head_coach_wiki):
                coach["qid"] = qids[team.head_coach_wiki]
        staff.append(coach)
    return {
        "event": event_key,
        "team": f"{team.country.lower()}-women",
        "list_type": list_type,
        "season": season,
        "source": "Wikipedia (Kaderliste nach IIHF)",
        "source_url": page_url(page),
        "official_url": team.official_url,
        "players": players,
        "staff": staff,
    }


def resolve_qids(titles: list[str], client: httpx.Client | None = None) -> dict[str, str]:
    """Wikipedia title -> Wikidata QID (pageprops, redirects followed). Missing pages are left out."""
    http = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    out: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        r = http.get(
            API,
            params={
                "action": "query",
                "titles": "|".join(batch),
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "redirects": 1,
                "format": "json",
                "formatversion": 2,
            },
        )
        r.raise_for_status()
        q = r.json().get("query", {})
        back = {x["to"]: x["from"] for x in q.get("normalized", [])}
        back.update({x["to"]: back.get(x["from"], x["from"]) for x in q.get("redirects", [])})
        for page in q.get("pages", []):
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                out[back.get(page["title"], page["title"])] = qid
    return out
