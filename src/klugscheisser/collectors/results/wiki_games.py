"""Games of a past tournament from a Wikipedia page's {{Ice hockey box}} templates (one request per page).

Used for tournaments whose games the IIHF realtime feed does not serve, e.g. the Olympic women's tournament 2026
(IIHF event 852: GetLatestState answers `null` for those games). Backfill only (`klugscheisser results-backfill`),
never in the nightly run.

Each box gives date, local time, teams (IOC codes), score (OT / GWS), period scores, goalies, goals with assists,
venue, attendance and its `reference`: the IIHF game sheet (PDF). That sheet is the game's source link
(label "IIHF via Wikipedia": the data was read from Wikipedia, which transcribes the sheet); the page revision
read is kept in `raw`. Wikipedia: CC BY-SA, read through the MediaWiki API with an identified user agent.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from klugscheisser.collectors.results.base import TEAM_DE, PoliteHttp, ot_suffix, wants
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"
PAGE_URL = "https://en.wikipedia.org/w/index.php?title={title}&oldid={rev}"
SOURCE = "IIHF via Wikipedia"


def template_bodies(text: str, name: str) -> list[tuple[int, str]]:
    """(position, body) of every {{name …}} in the wikitext, nested templates and links included."""
    out = []
    start = 0
    opener = "{{" + name
    while (i := text.find(opener, start)) >= 0:
        depth, j = 0, i
        while j < len(text) - 1:
            pair = text[j : j + 2]
            if pair == "{{":
                depth, j = depth + 1, j + 2
                continue
            if pair == "}}":
                depth, j = depth - 1, j + 2
                if depth == 0:
                    break
                continue
            j += 1
        out.append((i, text[i + len(opener) : j - 2]))
        start = j
    return out


def params(body: str) -> dict[str, str]:
    """'| key = value' pairs split at top level (not inside {{…}} or [[…]])."""
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(body):
        pair = body[i : i + 2]
        if pair in ("{{", "[["):
            depth += 1
            cur.append(pair)
            i += 2
            continue
        if pair in ("}}", "]]"):
            depth -= 1
            cur.append(pair)
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(body[i])
        i += 1
    parts.append("".join(cur))
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
TEAM = re.compile(r"\{\{ihw(?:-rt)?\|([A-Z]{3})", re.I)
START = re.compile(r"\{\{start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})", re.I)
TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
SCORE = re.compile(r"(\d+)\s*[–-]\s*(\d+)")
CLOCK = re.compile(r"\b(\d{1,3}):(\d{2})\b")
CODE_ALIASES = {"SWI": "SUI", "NETH": "NED", "DEU": "GER"}  # non-IOC/IIHF spellings seen in boxes
TAGS = {"PP", "PP1", "PP2", "SH", "SH1", "SH2", "EN", "PS", "EA", "GWS", "OT", "ENG"}


def _target(t: str) -> str:
    return re.sub(r"\s*\((ice hockey|ice hockey player|hockey)\)$", "", t.strip())


def name_map(text: str) -> dict[str, str]:
    """Short labels used in the boxes ('Carpenter') -> full names ('Alex Carpenter'), from all links of the page;
    labels used for two different persons are dropped (no guessing)."""
    seen: dict[str, set[str]] = {}
    for target, label in LINK.findall(text):
        if label:
            seen.setdefault(label.strip(), set()).add(_target(target))
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def plain(text: str, names: dict[str, str]) -> str:
    """Wikitext of one name -> the full name ('[[Alex Carpenter|Carpenter]]' or a bare 'Scamurra')."""
    text = text.replace("'''", "").strip()
    if hit := LINK.search(text):
        return _target(hit.group(1))
    text = re.sub(r"\{\{[^}]*\}\}|<[^>]+>", "", text).strip()
    return names.get(text, text)


def parse_goals(value: str, team: str, names: dict[str, str]) -> list[dict]:
    """'[[A|Carpenter]] ([[B|Keller]], Edwards) (PP) – 15:55 / …' or '28:37 – Scorer (Assist)' -> goals."""
    goals = []
    for part in value.split(" / "):
        part = part.strip()
        clock = CLOCK.search(part)
        if not clock:
            continue
        rest = (part[: clock.start()] + part[clock.end() :]).replace("–", " ").strip()
        groups = re.findall(r"\(([^()]*(?:\[\[[^\]]*\]\][^()]*)*)\)", rest)
        scorer_txt = rest.split("(", 1)[0]
        assists: list[str] = []
        for g in groups:
            if g.strip().upper() in TAGS:
                continue
            assists = [plain(a, names) for a in g.split(",") if a.strip()]
            break
        scorer = plain(scorer_txt, names)
        if not scorer:
            continue
        goals.append(
            {
                "time": f"{clock.group(1)}:{clock.group(2)}",
                "secs": int(clock.group(1)) * 60 + int(clock.group(2)),
                "team": team,
                "scorer": scorer,
                "assists": assists,
            }
        )
    return goals


def _heading_before(text: str, pos: int) -> str | None:
    heads = list(re.finditer(r"^==+\s*([^=]+?)\s*==+\s*$", text[:pos], re.M))
    return heads[-1].group(1) if heads else None


class WikiTournament:
    """One Wikipedia page with the games of one tournament (config entry of `wiki_backfill_pages`)."""

    def __init__(
        self,
        page: str,
        competition: str,
        competition_key: str | None = None,
        tz: str = "UTC",
        tournament: dict | None = None,
        key_prefix: str | None = None,
    ) -> None:
        self.page, self.competition, self.competition_key = page, competition, competition_key
        self.tz, self.tournament = tz, tournament
        self.key_prefix = key_prefix or (competition_key or "wiki")

    @classmethod
    def from_config(cls, cfg: dict) -> WikiTournament:
        return cls(
            page=cfg["page"],
            competition=cfg["competition"],
            competition_key=cfg.get("competition_key"),
            tz=cfg.get("tz", "UTC"),
            tournament=cfg.get("tournament"),
            key_prefix=cfg.get("key_prefix"),
        )


def parse_page(text: str, rev: int | str | None, t: WikiTournament) -> list[GameResult]:
    names = name_map(text)
    page_url = PAGE_URL.format(title=t.page.replace(" ", "_"), rev=rev)
    games = []
    for n, (pos, body) in enumerate(template_bodies(text, "Ice hockey box"), start=1):
        p = params(body)
        codes = [TEAM.search(p.get("team1", "")), TEAM.search(p.get("team2", ""))]
        day = START.search(p.get("date", ""))
        if not all(codes) or not day:
            continue
        home, away = (CODE_ALIASES.get(c.group(1).upper(), c.group(1).upper()) for c in codes)
        hh, mm = 0, 0
        if tm := TIME.search(p.get("time", "")):
            hh, mm = int(tm.group(1)), int(tm.group(2))
        local = datetime(
            int(day.group(1)), int(day.group(2)), int(day.group(3)), hh, mm, tzinfo=ZoneInfo(t.tz)
        )
        sc = SCORE.search(p.get("score", ""))
        raw_score = p.get("score", "")
        shootout = "GWS" in raw_score or "SO" in re.sub(r"\[\[[^\]]*\]\]", "", raw_score).split()
        overtime = "OT" in raw_score
        score = (
            f"{sc.group(1)}:{sc.group(2)}{ot_suffix(0, shootout=shootout, overtime=overtime and not shootout)}"
            if sc
            else None
        )
        ref = (p.get("reference") or "").strip()
        ref_id = re.sub(r"\W", "", ref.rsplit("/", 1)[-1].split("_", 1)[0]).lower() if ref else ""
        external = ref_id or f"{t.key_prefix}-{n}"
        goals = parse_goals(p.get("goals1", ""), home, names) + parse_goals(p.get("goals2", ""), away, names)
        goals.sort(key=lambda g: g["secs"])
        h = a = 0
        for g in goals:
            h, a = (h + 1, a) if g["team"] == home else (h, a + 1)
            g["score"] = f"{h}:{a}"
            del g["secs"]
        parts = []
        for g in goals:
            detail = f"{g['score']}, {g['time']}"
            parts.append(Participant(g["scorer"], "scorer", TEAM_DE.get(g["team"], g["team"]), detail))
            parts += [
                Participant(x, "assist", TEAM_DE.get(g["team"], g["team"]), detail) for x in g["assists"]
            ]
        for key, code in (("goalie1", home), ("goalie2", away)):
            for gl in re.split(r"<br\s*/?>|/", p.get(key, "")):
                if name := plain(gl, names):
                    parts.append(Participant(name, "goalie", TEAM_DE.get(code, code), None))
        periods = [x.replace("–", ":") for x in re.findall(r"\d+\s*–\s*\d+", p.get("periods", ""))]
        stadium = ", ".join(_target(x) for x, _ in LINK.findall(p.get("stadium", ""))) or None
        games.append(
            GameResult(
                source=SOURCE,
                sport="icehockey_women",
                competition=t.competition,
                external_id=external,
                url=ref or f"{page_url}#{n}",
                status="final" if score else "scheduled",
                starts_at=local.astimezone(UTC),
                home=TEAM_DE.get(home, home),
                away=TEAM_DE.get(away, away),
                score=score,
                period_scores=periods,
                venue=stadium,
                participants=parts,
                raw={
                    "home_code": home,
                    "away_code": away,
                    "game_number": n,
                    "phase": _heading_before(text, pos),
                    "goals": goals,
                    "spectators": (p.get("attendance") or "").replace(",", "") or None,
                    "detail": bool(score),
                    "wikipedia": page_url,
                    **({"competition_key": t.competition_key} if t.competition_key else {}),
                    **({"tournament": t.tournament} if t.tournament else {}),
                },
            )
        )
    return games


class WikiGamesCollector:
    """Results collector over Wikipedia tournament pages (backfill)."""

    name = "wiki"
    source_label = SOURCE

    def __init__(
        self, pages: list[WikiTournament], delay_s: float = 1.0, http: PoliteHttp | None = None
    ) -> None:
        self.pages = pages
        self.http = http or PoliteHttp(delay_s)
        self.known_final: set[str] = set()

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        out = []
        for t in self.pages:
            try:
                data = self.http.json(
                    API,
                    params={
                        "action": "parse",
                        "page": t.page,
                        "prop": "wikitext|revid",
                        "format": "json",
                        "formatversion": "2",
                    },
                )
                parsed = data["parse"]
                games = parse_page(parsed["wikitext"], parsed.get("revid"), t)
            except Exception as exc:
                log.warning("wiki games: %s failed: %s", t.page, exc)
                continue
            out += [g for g in games if wants(teams, g.home, g.away, g.raw["home_code"], g.raw["away_code"])]
        return out
