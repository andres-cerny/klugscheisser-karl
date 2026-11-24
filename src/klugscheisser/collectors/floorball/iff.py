"""IFF World Floorball Championships (men): games of WFC 2026 and past WFCs (checked 2026-11-24).

What is public and free:
- IFF results and player stats live in the IFF app (app.floorball.sport, data from iff-api.azurewebsites.net).
  Every API call needs a bearer token of the app (anonymous: HTTP 401); floorball.sport/player/ says
  "All player statistics are now on the IFF App". Not used: no logins, no token tricks.
- The English Wikipedia page of each WFC lists every game in {{Ice hockey box}} templates (date, local time,
  IOC codes, score, periods, arena, attendance) and links each one to its IFF game page
  (`reference` = https://app.floorball.sport/game-view/8674, older: …/#/magazinegameview/7167). Read once per
  page through the MediaWiki API (CC BY-SA, identified user agent), parsed with the existing
  template helpers of `collectors.results.wiki_games` (its own box parser is for {{ihw}} women's pages). The IFF game page is the source link (label "IFF via Wikipedia").

Outputs:
- `fetch_wfc_games(year)` -> GameResult list (sport "floorball_men"), for `ingest_results` like the ice hockey
  Wikipedia backfill; knockout games whose teams are still placeholders ("A2 v WPO2") are skipped.
- `seed_games(games)` -> the `games:` entries of the floorball event.yaml ({key, utc, home, away, venue, city, url,
  source}, plus score/status/phase for the record).

Gaps: no rosters and no player tournament stats (games/goals/assists/points/PIM) from a free official source; the
2024 and 2026 Wikipedia boxes carry no scorers. National team stats for Swiss players: see
`swissunihockey.national_totals_for` (career totals only).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from klugscheisser.collectors.floorball.common import CappedHttp
from klugscheisser.collectors.results import wiki_games
from klugscheisser.collectors.results.base import ot_suffix
from klugscheisser.interfaces import GameResult

log = logging.getLogger(__name__)

SOURCE = "IFF via Wikipedia"
SPORT = "floorball_men"


@dataclass(frozen=True)
class Wfc:
    year: int
    page: str
    tz: str
    city: str

    @property
    def key(self) -> str:
        return f"wfc-{self.year}"

    @property
    def competition(self) -> str:
        return f"Unihockey-WM {self.year} der Männer"

    @property
    def league(self) -> str:
        """SeasonStat.league for national team rows of this tournament."""
        return f"WFC {self.year}"


WFCS = {
    w.year: w
    for w in (
        Wfc(2026, "2026 Men's World Floorball Championships", "Europe/Helsinki", "Tampere"),
        Wfc(2024, "2024 Men's World Floorball Championships", "Europe/Stockholm", "Malmö"),
        Wfc(2022, "2022 Men's World Floorball Championships", "Europe/Zurich", "Zürich"),
        Wfc(2020, "2020 Men's World Floorball Championships", "Europe/Helsinki", "Helsinki"),
        Wfc(2018, "2018 Men's World Floorball Championships", "Europe/Prague", "Prag"),
    )
}

# German names of floorball nations missing from the ice hockey table (results.base.TEAM_DE)
EXTRA_DE = {
    "EST": "Estland",
    "SGP": "Singapur",
    "SIN": "Singapur",
    "PHI": "Philippinen",
    "THA": "Thailand",
    "AUS": "Australien",
    "NZL": "Neuseeland",
    "ESP": "Spanien",
    "SLO": "Slowenien",
    "LAT": "Lettland",
    "CAN": "Kanada",
    "JPN": "Japan",
    "SVK": "Slowakei",
}
CODE_FIX = {"SIN": "SGP"}  # Wikipedia uses both for Singapore


def _de(code: str) -> str:
    from klugscheisser.collectors.results.base import TEAM_DE

    return TEAM_DE.get(code) or EXTRA_DE.get(code) or code


TEAM = re.compile(r"\{\{(?:fl|flb|fl-rt|flagicon|fb|fb-rt)\|([A-Z]{3})", re.I)
DAY = re.compile(r"(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})")
ISO_DAY = re.compile(r"\{\{start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})", re.I)
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


def _wikiplain(text: str) -> str:
    """'[[Tampere Ice Stadium|Hakametsä Ice Hall]], [[Tampere]]' -> 'Hakametsä Ice Hall, Tampere'."""
    text = wiki_games.LINK.sub(lambda h: h.group(2) or h.group(1), text)
    text = re.sub(r"\{\{[^}]*\}\}|<[^>]+>", "", text).replace("'''", "")
    return " ".join(text.split())


def _day(value: str) -> tuple[int, int, int] | None:
    if hit := ISO_DAY.search(value):
        return int(hit.group(1)), int(hit.group(2)), int(hit.group(3))
    if (hit := DAY.search(value)) and hit.group(2) in MONTHS:
        return int(hit.group(3)), MONTHS[hit.group(2)], int(hit.group(1))
    return None


def parse_wfc_page(text: str, rev: int | str | None, wfc: Wfc) -> list[GameResult]:
    """Wikipedia wikitext -> GameResults (floorball, IFF game page as url). Boxes whose teams are placeholders
    (knockout games not yet drawn) are skipped."""
    page_url = wiki_games.PAGE_URL.format(title=wfc.page.replace(" ", "_"), rev=rev)
    out = []
    for n, (pos, body) in enumerate(wiki_games.template_bodies(text, "Ice hockey box"), start=1):
        p = wiki_games.params(body)
        codes = [TEAM.search(p.get("team1", "")), TEAM.search(p.get("team2", ""))]
        day = _day(p.get("date", ""))
        if not all(codes) or not day:
            continue
        home, away = (CODE_FIX.get(c.group(1).upper(), c.group(1).upper()) for c in codes)
        hh, mm = 0, 0
        if tm := wiki_games.TIME.search(p.get("time", "")):
            hh, mm = int(tm.group(1)), int(tm.group(2))
        local = datetime(*day, hh, mm, tzinfo=ZoneInfo(wfc.tz))
        raw_score = p.get("score", "")
        sc = wiki_games.SCORE.search(raw_score)
        plain_score = re.sub(r"\[\[[^\]]*\]\]", "", raw_score)
        shootout = "PS" in plain_score.split() or "SO" in plain_score.split() or "penalt" in raw_score.lower()
        overtime = "OT" in plain_score or "overtime" in raw_score.lower()
        score = (
            f"{sc.group(1)}:{sc.group(2)}{ot_suffix(0, shootout=shootout, overtime=overtime and not shootout)}"
            if sc
            else None
        )
        ref = (p.get("reference") or "").strip()
        game_id = re.search(r"(?:game-view|magazinegameview|game)/(\d+)", ref)
        external = f"iff-{game_id.group(1)}" if game_id else f"{wfc.key}-{n}"
        periods = [x.replace("–", ":") for x in re.findall(r"\d+\s*–\s*\d+", p.get("periods", ""))]
        stadium = _wikiplain(p.get("stadium", "")) or None
        attendance = re.sub(r"\D", "", p.get("attendance") or "") or None
        out.append(
            GameResult(
                source=SOURCE,
                sport=SPORT,
                competition=wfc.competition,
                external_id=external,
                url=ref if "floorball.sport" in ref else f"{page_url}#{n}",
                status="final" if score else "scheduled",
                starts_at=local.astimezone(UTC),
                home=_de(home),
                away=_de(away),
                score=score,
                period_scores=periods if score else [],
                venue=stadium,
                raw={
                    "home_code": home,
                    "away_code": away,
                    "game_number": n,
                    "phase": wiki_games._heading_before(text, pos),
                    "spectators": attendance,
                    "wikipedia": page_url,
                    "iff_game": ref or None,
                    "competition_key": wfc.key,
                },
            )
        )
    return out


def fetch_wfc_games(year: int, http: CappedHttp | None = None) -> list[GameResult]:
    """One MediaWiki request. [] on errors (logged)."""
    wfc = WFCS[year]
    http = http or CappedHttp(delay_s=1.0, max_requests=10)
    try:
        data = http.json(
            wiki_games.API,
            params={
                "action": "parse",
                "page": wfc.page,
                "prop": "wikitext|revid",
                "format": "json",
                "formatversion": "2",
            },
        )
        parsed = data["parse"]
        return parse_wfc_page(parsed["wikitext"], parsed.get("revid"), wfc)
    except Exception as exc:
        log.warning("iff: %s failed: %s", wfc.page, exc)
        return []


def seed_games(games: list[GameResult], year: int) -> list[dict]:
    """GameResults -> `games:` entries of the floorball event.yaml (key "wfc-<year>-<IFF game id>")."""
    wfc = WFCS[year]
    out = []
    for g in games:
        venue = (g.venue or "").split(",")[0].strip() or None  # "Kauppi Sports Center, Tampere" -> arena
        out.append(
            {
                "key": f"{wfc.key}-{g.external_id.removeprefix('iff-')}",
                "utc": g.starts_at.strftime("%Y-%m-%dT%H:%M:%SZ") if g.starts_at else None,
                "home": g.raw["home_code"],
                "away": g.raw["away_code"],
                "venue": venue,
                "city": wfc.city,
                "url": g.url,
                "source": "IFF",
                "score": g.score,
                "status": g.status,
                "phase": g.raw.get("phase"),
                "wikipedia": g.raw.get("wikipedia"),
            }
        )
    return out


def import_for_athletes(session, athletes, *, dry_run: bool = True, **_) -> dict:
    """No free per-player IFF data (see module doc): nothing to write per athlete."""
    return {"iff_players": 0, "iff_note": "IFF-Spielerstatistik nur in der IFF-App (Login); nicht frei"}
