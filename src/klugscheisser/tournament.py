"""A tournament as a complete object (WCh 2026): format, groups, schedule, pre-tournament games, history, rosters.

The facts live in `seeds/events/<event>.yaml`, each with its source (and a verbatim quote where we have one); the
games are rows in the events table (seed/loader.load_event_info), so results from the results pipeline show up here
as soon as they are stored. Answers are built without a model: a tournament question ("Wie funktioniert der Modus?",
"Gegen wen spielt die Schweiz?", "Wo fand die WM früher statt?") gets the matching sections in a few lines, each line
with its source link. Used by the MCP tools `frage` / `get_event` and by the web chat (answer/lookups.py).
"""

from __future__ import annotations

import contextlib
import functools
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

INFO_DIR = Path("seeds/events")  # eishockey; the current project's directory: events_dir()


def projects_wording():
    from klugscheisser import projects

    return projects.wording()


TZ = ZoneInfo("Europe/Zurich")

NATION_DE = {
    "SUI": "Schweiz",
    "USA": "USA",
    "CZE": "Tschechien",
    "GER": "Deutschland",
    "AUT": "Österreich",
    "CAN": "Kanada",
    "FIN": "Finnland",
    "SWE": "Schweden",
    "JPN": "Japan",
    "DEN": "Dänemark",
    "RUS": "Russland",
    "CHN": "China",
    "KAZ": "Kasachstan",
    "KOR": "Südkorea",
    # 2026-11-24: floorball nations (Unihockey-WM)
    "NOR": "Norwegen",
    "LAT": "Lettland",
    "SVK": "Slowakei",
    "EST": "Estland",
    "POL": "Polen",
    "THA": "Thailand",
    "SGP": "Singapur",
    "PHI": "Philippinen",
    "AUS": "Australien",
    "HUN": "Ungarn",
    "ITA": "Italien",
    "SLO": "Slowenien",
    "ESP": "Spanien",
    "NED": "Niederlande",
    "FRA": "Frankreich",
    "BEL": "Belgien",
    "GBR": "Grossbritannien",
    "IRL": "Irland",
    "UKR": "Ukraine",
    "NZL": "Neuseeland",
    "MAS": "Malaysia",
}
NATION_RX = {
    "SUI": r"schweiz\w*|swiss|switzerland|nati\b",
    "USA": r"usa|us-amerikan\w*|united states|amerikaner\w*",
    "CZE": r"tschech\w*|czech\w*",
    "GER": r"deutschland\w*|deutsche[nrs]?\b|germany|german\b",
    "AUT": r"österreich\w*|oesterreich\w*|austria\w*",
    "CAN": r"kanad\w*|canad\w*",
    "FIN": r"finn\w*|finland",
    "SWE": r"schwed\w*|swed\w*",
    "JPN": r"japan\w*",
    "DEN": r"dän\w*|daen\w*|denmark|danish",
    "NOR": r"norweg\w*|norway",
    "LAT": r"lett\w*|latvia\w*",
    "SVK": r"slowak\w*|slovak\w*",
    "EST": r"estland|estnisch\w*|eston\w*|esten\b",
    "POL": r"polen|polnisch\w*|poland|polish",
    "THA": r"thailand\w*|thai\b",
    "SGP": r"singapur\w*|singapore\w*",
    "PHI": r"philippin\w*",
    "AUS": r"australi\w*",
    "HUN": r"ungarn|ungarisch\w*|hungar\w*",
    "ITA": r"itali\w*",
    "SLO": r"slowen\w*|sloven\w*",
    "ESP": r"spanien|spanisch\w*|spain|spanish",
    "NED": r"niederland\w*|holland\w*|netherlands|dutch",
}

WM_WORDS = re.compile(
    r"\b(wms?|frauen-?wms?|unihockey-?wms?|wm-\w+|weltmeisterschaft\w*|world championships?|women'?s worlds|worlds|wch|wwc"
    r"|wfc|world floorball championships?)\b",
    re.I,
)
TOURNAMENT = re.compile(WM_WORDS.pattern[:-3] + r"|turnier\w*)\b", re.I)
TOPICS: dict[str, re.Pattern] = {
    "modus": re.compile(
        r"modus|format|system|regeln?|setzliste|seeding|punktgleich\w*|gleichstand|tie-?break\w*|tordifferenz"
        r"|rangierung|klassierung|punktesystem|wie viele punkte",
        re.I,
    ),
    "gruppen": re.compile(r"gruppe\w*|group\w*|gegner\w*|opponent\w*|gegen wen|wer spielt gegen", re.I),
    "spielplan": re.compile(
        r"spielplan|schedule|wann|uhrzeit|um wie viel uhr|termin\w*|datum|daten|\bspiele\b|\bspielt\b|games?\b"
        r"|anspiel|beginn|start|\bwhen\b|what time|kick-?off|face-?off|fixtures?|\bplays?\b|\bplaying\b",
        re.I,
    ),
    "viertelfinal": re.compile(
        r"viertel\w*|quarter\w*|k\.?-?o\b|k\.-o\.|ko-phase|playoff\w*|halbfinal\w*|semi\w*|\bfinal\w*|medaill\w*",
        re.I,
    ),
    "abstieg": re.compile(r"abstieg\w*|absteig\w*|steig\w*\b.{0,30}\bab\b|relegation\w*|relegated", re.I),
    "geschichte": re.compile(
        r"geschichte|histor\w*|früher\w*|frueher\w*|bisher\w*|gastgeber\w*|\bhosts?\b|austragung\w*|bestes?\b"
        r"|platzierung\w*|\bränge?\b|rangierung\w*|jemals|seit 19\d\d|\b19\d\d\b|\b20[01]\d\b|\b202[0-5]\b|letzte[nmr]? wm|vergangene"
        r"|weltmeister(in|innen)?\b|titel",
        re.I,
    ),
    "november": re.compile(
        r"november|verschoben|verschieb\w*|verleg\w*|herbst|warum (jetzt|nicht im april)|\bapril\b"
        r"|why\b.{0,60}\b(november|autumn|fall|moved|now)\b|\bmoved\b",
        re.I,
    ),
    "vorbereitung": re.compile(
        r"vorbereitung\w*|testspiel\w*|vorbereitungsspiel\w*|euro hockey tour|\beht\b|weht|södertälje|"
        r"soedertaelje|lahti|hauptprobe|vor der wm|before the",
        re.I,
    ),
    "kader": re.compile(
        r"kader|aufgebot\w*|roster|squad|selektion\w*|nominier\w*|aufgeboten|verletz\w*|fehlt|fehlen|abwesend",
        re.I,
    ),
    "teams": re.compile(
        r"\bteams?\b|nationen|teilnehmer\w*|wer (ist|spielt) (dabei|mit)|welche länder|participants?|countries"
        r"|who(?:'s| is) (?:in|playing)",
        re.I,
    ),
    "ort": re.compile(
        r"\bwo\b|where|venue|stadion|arena|halle|esbjerg|herning|dänemark|daenemark|tampere|hakamets|kauppi",
        re.I,
    ),
    "bilanz": re.compile(
        r"bilanz\w*|head.?to.?head|direkte?n? (duell|begegnung)\w*|duell\w*|gegeneinander|"
        r"(bisher|früher|frueher|je|schon) gegen|gegen \w+ (bisher|früher|je)",
        re.I,
    ),
    "rekorde": re.compile(r"rekord\w*|records?|all.?time|ewige\w*|meiste\w* (punkte|tore|assists)", re.I),
    "funfacts": re.compile(
        r"fun.?facts?|kurios\w*|anekdote\w*|wusstest|interessante?s? fakt\w*|trivia", re.I
    ),
    "schiedsrichter": re.compile(
        r"schiedsricht\w*|schiri\w*|referee\w*|\bpfeif\w*|\bgepfiffen|unparteiisch\w*|\bofficiat\w*|\bumpire\w*",
        re.I,
    ),
    "resultate": re.compile(
        r"resultat\w*|ergebnis\w*|results?|score|tabelle|standings?|gewonnen|verloren|\bwon\b|\blost\b|\bbeat\b",
        re.I,
    ),
}
OVERVIEW = re.compile(
    r"\b(infos?|überblick|ueberblick|alles über|was weisst du|erzähl|erzaehl|fakten)\b", re.I
)
# questions about players' doings belong to the normal answer engine, not to the tournament sheet
PERSONAL_WORDS = re.compile(
    r"\b(instagram|post(s|et)?|gossip|studier\w*|freund\w*|privat\w*|interview\w*|gesagt|zitat\w*|news|"
    r"neuigkeit\w*)\b",
    re.I,
)
# goals/points are about players ("Wer hat die meisten Tore?") — unless the question is about the format
# ("Wie viele Punkte braucht die Schweiz für den Viertelfinal?", "Zählt die Tordifferenz?")
STAT_WORDS = re.compile(
    r"\b(tor(e|en)?|getroffen|skorer\w*|scorer\w*|assists?|punkte\w*|goals?|points?)\b", re.I
)
PLAYER_ASK = re.compile(
    r"\bwer\b|\bwho\b|spieler\w*|torschütz\w*|torschuetz\w*|topskorer\w*|skorer\w*|player\w*|"
    r"\bam meisten\b|\bmost\b|\bbeste\w*\b|\bbest\b",
    re.I,
)
FORMAT_TOPICS = {"modus", "viertelfinal", "abstieg", "gruppen"}
# a question about the format needs no "WM" in it when no other competition is named
FORMAT_WORDS = re.compile(
    r"viertelfinal\w*|quarter-?final\w*|relegation\w*|abstieg\w*|absteig\w*|\bmodus\b|setzliste", re.I
)
OTHER_COMPETITION = re.compile(
    r"olymp\w*|pwhl|ncaa|sdhl|swhl|women'?s league|\bliga\b|league|euro.?hockey.?tour|\bw?eht\b|playoff\w*|"
    r"meisterschaft(?<!weltmeisterschaft)|cup\b|u18|junior\w*",
    re.I,
)
# the current tournament is meant (not only a past one) when the question names it like this
CURRENT_CUE = re.compile(
    r"\b2026\b|dänemark|daenemark|denmark|herning|esbjerg|november|diese[snr]? (jahr|wm)|kommende\w*|"
    r"nächste\w*|naechste\w*|bevorstehende\w*|aktuelle\w*|this year|upcoming|next\b",
    re.I,
)


def about_players(question: str, topics: list[str]) -> bool:
    """True for questions about players' own doings (posts, quotes, private life) or their goals/points, which
    the answer engine handles; goal/point words in a question about the format stay with the tournament sheet."""
    if PERSONAL_WORDS.search(question):
        return True
    if STAT_WORDS.search(question):
        return bool(PLAYER_ASK.search(question)) or not (set(topics) & FORMAT_TOPICS)
    return False


@dataclass
class Line:
    text: str
    quelle: str
    url: str
    zitat: str | None = None
    kind: str = "web"  # official | media | web | result

    def out(self) -> dict[str, Any]:
        d = {"text": self.text, "quelle": self.quelle, "url": self.url}
        if self.zitat:
            d["zitat"] = self.zitat
        return d


@dataclass
class Section:
    thema: str
    titel: str
    zeilen: list[Line] = field(default_factory=list)


# --- info file ---------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _read(path: str, mtime: float) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if isinstance(
        data.get("event"), dict
    ):  # 2026-11-24: the floorball research format (seed/floorball_info.py)
        from klugscheisser import projects
        from klugscheisser.seed.floorball_info import normalize

        checked = datetime.fromtimestamp(mtime, UTC).date().isoformat()
        data = normalize(data, projects.current().main_event, checked)
        detail = Path(path).parent / "wfc_history_detail.yaml"
        if detail.exists():  # Swiss WFC games, head-to-head, MVPs, IFF records
            from klugscheisser.seed.floorball_info import add_history_detail

            add_history_detail(data, yaml.safe_load(detail.read_text(encoding="utf-8")) or {})
        fun = Path(path).parent / "funfacts.md"
        if fun.exists():  # the tournament's fun facts (each with its source) as topic "funfacts"
            from klugscheisser.seed.floorball import parse_funfacts_md

            items, _skipped = parse_funfacts_md(fun.read_text(encoding="utf-8"))
            for n, f in enumerate(items):
                key = f"fun{n}"
                data["sources"][key] = {
                    "name": f.get("source") or _host(f["url"]),
                    "url": f["url"],
                    "kind": "web",
                }
                if f.get("date"):
                    data["sources"][key]["date"] = f["date"]
                text = f["text"] + (
                    " (nur eine Sekundärquelle, vor Sendung prüfen)" if f.get("unverified") else ""
                )
                data["facts"].append({"topic": ["funfacts"], "text": text, "src": key})
    return data


def events_dir() -> Path:
    """The current project's tournament files (seeds/events; seeds/floorball/events for Unihockey)."""
    from klugscheisser import projects

    return Path(projects.current().seeds_dir) / "events"


def _single_event_file(d: Path) -> tuple[str, Path] | None:
    """A project with one tournament may keep it as <seeds_dir>/event.yaml (floorball) instead of events/<key>.yaml."""
    path = d.parent / "event.yaml"
    if not path.exists():
        return None
    key = _read(str(path.resolve()), path.stat().st_mtime).get("event")
    return (key, path) if key else None


def load_info(event_key: str, info_dir: Path | None = None) -> dict | None:
    d = info_dir or events_dir()
    path = d / f"{event_key}.yaml"
    if not path.exists():
        single = _single_event_file(d)
        if not single or single[0] != event_key:
            return None
        path = single[1]
    return _read(str(path.resolve()), path.stat().st_mtime)


def info_events(info_dir: Path | None = None) -> list[str]:
    d = info_dir or events_dir()
    keys = {p.stem for p in d.glob("*.yaml")} if d.exists() else set()
    if single := _single_event_file(d):
        keys.add(single[0])
    return sorted(keys)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.")


def _opponents() -> str:
    from klugscheisser.sports import current_spec

    return "Gegner" if current_spec().men else "Gegnerinnen"


def _venue_text(v: dict) -> str:
    official = f"bei der IIHF «{v['iihf_name']}», " if v.get("iihf_name") else ""
    places = f"{v['capacity']} Plätze" if v.get("capacity") else "Kapazität unbekannt"
    return f"{v['city']}: {v['arena']} ({official}{places}) – {v.get('rounds', '')}"


def _src(info: dict, key: str) -> tuple[str, str, str]:
    sources = info.get("sources") or {}
    # 2026-11-24: a tournament file names its schedule source (`schedule_source`, e.g. the IFF for floorball);
    # the WCh file uses "iihf_feed"/"iihf_site"
    if key not in sources and key in ("iihf_feed", "iihf_site") and info.get("schedule_source") in sources:
        key = info["schedule_source"]
    elif key not in sources and info.get("default_source") in sources:
        key = info["default_source"]
    s = sources.get(key) or {}
    return s.get("name", key), s.get("url", ""), s.get("kind", "web")


def _line(info: dict, text: str, src: str, quote: str | None = None) -> Line:
    name, url, kind = _src(info, src)
    return Line(text, name, url, quote, kind)


def nations_in(question: str) -> list[str]:
    """Teams a question names. A country after 'in'/'nach' is a place ('WM in Dänemark', 'WM 2025 in
    Tschechien'), not a team."""
    out = []
    for code, rx in NATION_RX.items():
        matches = [*re.finditer(rf"\b({rx})", question, re.I), *re.finditer(rf"\b{code}\b", question)]
        if any(not re.search(r"\b(in|nach)\s+$", question[: x.start()], re.I) for x in matches):
            out.append(code)
    return out


GENERIC_HOW = re.compile(r"wie funktioniert|ablauf|wie läuft|how does", re.I)


def topics_in(question: str) -> list[str]:
    found = [t for t, rx in TOPICS.items() if rx.search(question)]
    if not found and GENERIC_HOW.search(question):
        found = ["modus"]  # "Wie funktioniert die WM?" without a narrower topic
    if not found and DATE_WORDS.search(question):
        found = ["spielplan"]  # "Was läuft am 13.11.?"
    return found


MONTHS = {
    "jan": 1, "feb": 2, "mär": 3, "maer": 3, "mar": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12,
}  # fmt: skip
WEEKDAYS = {
    "montag": 0, "monday": 0, "dienstag": 1, "tuesday": 1, "mittwoch": 2, "wednesday": 2, "donnerstag": 3,
    "thursday": 3, "freitag": 4, "friday": 4, "samstag": 5, "saturday": 5, "sonntag": 6, "sunday": 6,
}  # fmt: skip
_NUM_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.(?:(\d{2,4}))?(?!\d)")
_WORD_DATE = re.compile(
    r"(?<![\d.])(\d{1,2})\.?\s*(januar|jan|februar|feb|märz|maerz|march|mar|april|apr|mai|may|juni|june|jun|juli|"
    r"july|jul|august|aug|september|sept?|oktober|october|okt|oct|november|nov|dezember|december|dez|dec)\b"
    r"|\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
    re.I,
)
_FIRST_DAY = re.compile(
    r"erste[nmr]? (turnier|spiel|wm-)?tag\w*|eröffnung\w*|eroeffnung\w*|opening (day|game)|first day|zum auftakt|auftakt",
    re.I,
)
_LAST_DAY = re.compile(r"letzte[nmr]? (turnier|wm-)?tag\b|final ?tag|last day", re.I)
_REL_DAY = re.compile(r"\b(heute|today|übermorgen|uebermorgen|morgen|tomorrow)\b", re.I)
DATE_WORDS = re.compile(
    "|".join(p.pattern for p in (_NUM_DATE, _WORD_DATE, _FIRST_DAY, _LAST_DAY, _REL_DAY))
    + r"|\b(am|on) ("
    + "|".join(WEEKDAYS)
    + r")\b",
    re.I,
)


def days_in(question: str, start: date, end: date, today: date | None = None) -> list[date]:
    """Days of the tournament (start..end) a question names: '8.11.', '8. November', 'November 8', 'am
    Samstag', 'am ersten Tag', 'heute'/'morgen'. Empty list = no day named (all days)."""
    today = today or datetime.now(TZ).date()
    out: set[date] = set()

    def add(y: int | None, mo: int, d: int) -> None:
        for year in [y] if y else sorted({start.year, end.year}):
            with contextlib.suppress(ValueError):
                out.add(date(year, mo, d))

    for d_, mo, y in _NUM_DATE.findall(question):
        if 1 <= int(mo) <= 12:
            yy = int(y) + 2000 if y and len(y) == 2 else (int(y) if y else None)
            add(yy, int(mo), int(d_))
    for d1, mon1, mon2, d2 in _WORD_DATE.findall(question):
        name, day = (mon1, d1) if mon1 else (mon2, d2)
        mo = next((v for k, v in MONTHS.items() if name.lower().startswith(k)), None)
        if mo:
            add(None, mo, int(day))
    if _FIRST_DAY.search(question):
        out.add(start)
    if _LAST_DAY.search(question):
        out.add(end)
    for word in _REL_DAY.findall(question):
        w = word.lower()
        out.add(
            today
            + timedelta(
                days=2
                if w.startswith("über") or w.startswith("ueber")
                else (0 if w in ("heute", "today") else 1)
            )
        )
    for wd in re.findall(r"\b(?:am|on) (" + "|".join(WEEKDAYS) + r")\b", question, re.I):
        n = (end - start).days
        out |= {
            start + timedelta(days=i)
            for i in range(n + 1)
            if (start + timedelta(days=i)).weekday() == WEEKDAYS[wd.lower()]
        }
    return sorted(d for d in out if start <= d <= end)


# --- recognising a tournament question -------------------------------------------------------------------------


def event_for_question(
    s: Session, question: str, event_id: int | None = None, info_dir: Path | None = None
) -> m.Event | None:
    """The tournament (with an info file) a question is about, or None. With `event_id` (the tool's event argument)
    that event is used; otherwise the question must name the WM/tournament. Questions about players' own doings
    (goals, posts, quotes …) or naming a stored person stay with the normal answer engine."""
    topics = topics_in(question)
    if about_players(question, topics):
        return None
    if event_id is None and not WM_WORDS.search(question):
        # "Gegen wen spielt die Schweiz im Viertelfinal?", "Wie läuft das Turnier?": format words or "Turnier" name
        # the tournament, unless another competition is named (Olympia, PWHL playoffs, Euro Hockey Tour …)
        named = TOURNAMENT.search(question) or FORMAT_WORDS.search(question)
        if not named or OTHER_COMPETITION.search(question):
            return None
    if not topics and not OVERVIEW.search(question):
        return None  # "Wer hatte ein Comeback?" with event=WM: a question about players, not the tournament
    if event_id is None and not WM_WORDS.search(question) and _names_other_tournament(s, question):
        return None  # 24.10.: "WEHT-Turnier in Lahti … Södertälje" got the WCh sheet
    if _names_person(s, question):
        return None
    if event_id is not None:
        ev = s.get(m.Event, event_id)
        return ev if ev is not None and load_info(ev.key, info_dir) else None
    keys = info_events(info_dir)
    if not keys:
        return None
    now = datetime.now(UTC)
    evs = list(s.scalars(select(m.Event).where(m.Event.key.in_(keys))))
    # the current/next one first (an info file for a past tournament stays usable by its key)
    evs.sort(key=lambda e: (e.ends_at is not None and e.ends_at < now, e.starts_at or now))
    return evs[0] if evs else None


def _names_other_tournament(s: Session, question: str) -> bool:
    """A stored tournament other than the WCh named by its city or key ("Lahti", "Södertälje")."""
    from klugscheisser import projects
    from klugscheisser.answer.event_games import named_events

    main = projects.current().main_event
    return any(not ev.key.startswith("wch-") and ev.key != main for ev in named_events(s, question))


def _names_person(s: Session, question: str) -> bool:
    try:
        from klugscheisser.domain.matcher import AthleteMatcher

        return bool(AthleteMatcher(s).from_text(question))
    except Exception:  # matcher problems never block the tournament answer
        return False


# --- sections ------------------------------------------------------------------------------------------------


def _games(s: Session, ev: m.Event, info: dict) -> list[m.Event]:
    keys = [g["key"] for g in info.get("games") or []]
    q = select(m.Event).where(m.Event.kind == "game")
    cond = [m.Event.key.in_(keys)] if keys else []
    if ev.competition_id is not None:
        cond.append(m.Event.competition_id == ev.competition_id)
    if not cond:
        return []
    games = list(s.scalars(q.where(or_(*cond)).order_by(m.Event.starts_at, m.Event.key)))
    lo, hi = ev.starts_at, ev.ends_at
    if lo is not None:  # a competition may hold other years' games
        games = [g for g in games if g.starts_at is None or g.starts_at.date() >= lo.date()]
    if hi is not None:
        games = [g for g in games if g.starts_at is None or g.starts_at.date() <= hi.date()]
    return games


def _team_codes(s: Session) -> dict[int, str]:
    return {
        t.id: (t.country or t.short_name or t.name)
        for t in s.scalars(select(m.Team).where(m.Team.kind == "national"))
    }


def _when(dt: datetime | None) -> str:
    if dt is None:
        return "Zeit offen"
    d = dt.astimezone(TZ)
    return f"{['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][d.weekday()]} {d:%d.%m.} {d:%H:%M}"


def _of(nat: str) -> str:
    return "der Schweiz" if nat == "SUI" else f"von {NATION_DE.get(nat, nat)}"


def _game_text(g: m.Event, codes: dict[int, str], perspective: str | None = None) -> str:
    home, away = codes.get(g.home_team_id or 0, "?"), codes.get(g.away_team_id or 0, "?")
    match = f"{NATION_DE.get(home, home)} – {NATION_DE.get(away, away)}"
    where = g.city or g.venue or ""
    if g.status == "final" and g.score:
        res = f" {g.score}"
    elif g.status == "live":
        res = f" (läuft{', ' + g.score if g.score else ''})"
    else:
        res = ""
    return f"{_when(g.starts_at)}: {match}{res}" + (f" ({where})" if where else "")


def _game_line(g: m.Event, codes: dict[int, str]) -> Line:
    kind = "result" if g.status in ("final", "live") else "official"
    return Line(_game_text(g, codes), g.source or "IIHF", g.source_url or "", None, kind)


def sec_overview(s: Session, ev: m.Event, info: dict) -> Section:
    sec = Section("uebersicht", "Überblick")
    start, end = info.get("starts"), info.get("ends")
    d1, d2 = date.fromisoformat(start), date.fromisoformat(end)
    venues = "; ".join(f"{v['city']}: {v['arena']}" for v in info.get("venues") or [])
    sec.zeilen.append(
        _line(
            info,
            f"{info.get('name', ev.name)}: {d1:%d.%m.}–{d2:%d.%m.%Y} in {info.get('host')} ({venues}).",
            "wiki_2026",
        )
    )
    sec.zeilen += sec_groups(s, ev, info).zeilen[:2]
    for f in info.get("facts") or []:
        if "uebersicht" in f.get("topic", []):
            sec.zeilen.append(_line(info, f["text"], f["src"], f.get("quote")))
    return sec


def sec_groups(s: Session, ev: m.Event, info: dict, nations: list[str] | None = None) -> Section:
    sec = Section("gruppen", "Gruppen")
    seeds = info.get("seeding") or {}
    for g, spec in (info.get("groups") or {}).items():
        teams = ", ".join(
            f"{NATION_DE.get(c, c)} ({seeds[c]})" if c in seeds else NATION_DE.get(c, c)
            for c in spec["teams"]
        )
        sec.zeilen.append(
            _line(
                info,
                f"Gruppe {g} ({spec.get('city')}): {teams}"
                + (" – in Klammern die Setzliste." if seeds else "."),
                "wiki_2026",
            )
        )
    codes = _team_codes(s)
    for nat in nations or ["SUI"]:
        games = [
            g
            for g in _games(s, ev, info)
            if nat in (codes.get(g.home_team_id or 0), codes.get(g.away_team_id or 0))
        ]
        group_keys = {x["key"] for x in info.get("games") or [] if x.get("group")}
        prelim = [g for g in games if g.key.startswith("iihf-") or g.key in group_keys][:4]
        if prelim:
            opp = []
            for g in prelim:
                h, a = codes.get(g.home_team_id or 0), codes.get(g.away_team_id or 0)
                other = a if h == nat else h
                opp.append(f"{NATION_DE.get(other, other)} ({g.starts_at.astimezone(TZ):%d.%m.})")
            sec.zeilen.append(
                Line(
                    f"{_opponents()} {_of(nat)} in der Vorrunde: " + ", ".join(opp) + ".",
                    _src(info, "iihf_feed")[0],
                    _src(info, "iihf_feed")[1],
                    kind="official",
                )
            )
    return sec


def sec_schedule(
    s: Session, ev: m.Event, info: dict, nations: list[str], days: list[date] | None = None
) -> Section:
    codes = _team_codes(s)
    games = _games(s, ev, info)
    if days:  # "Wer spielt am ersten Tag?", "am 8.11.": only these days (group games and knock-out rounds)
        games = [g for g in games if g.starts_at and g.starts_at.astimezone(TZ).date() in days]
    if len(nations) >= 2:  # "Wann spielt die Schweiz gegen Deutschland?": their meeting(s)
        games = [
            g
            for g in games
            if {codes.get(g.home_team_id or 0), codes.get(g.away_team_id or 0)} <= set(nations)
        ]
        title = "Spiel " + " – ".join(NATION_DE.get(n, n) for n in nations)
    elif nations:
        games = [
            g
            for g in games
            if {codes.get(g.home_team_id or 0), codes.get(g.away_team_id or 0)} & set(nations)
        ]
        title = "Spielplan " + ", ".join(NATION_DE.get(n, n) for n in nations)
    else:
        title = "Spielplan Vorrunde"
    if days:
        title = (
            "Spielplan "
            + ", ".join(f"{d:%d.%m.}" for d in days)
            + (f" ({title.removeprefix('Spielplan ')})" if nations else "")
        )
    sec = Section("spielplan", title + f" ({info.get('time_note') or 'Schweizer Zeit = Ortszeit Dänemark'})")
    sec.zeilen += [_game_line(g, codes) for g in games]
    prelim = {x["key"] for x in info.get("games") or []}
    if len(nations) < 2 and not any(g.key not in prelim for g in _games(s, ev, info)):
        playoffs = [(p, "") for p in info.get("playoffs") or []]
        if len(nations) == 1 and not days:
            # 2026-11-24: "Wann spielt die Schweiz?": its own possible way to the quarterfinal, not the whole bracket
            playoffs = ko_path(info, nations[0])
        for p, how in playoffs:  # knock-out games until the federation lists them with teams
            d = date.fromisoformat(p["day"])
            if days and d not in days:
                continue
            t = f" {p['time']}" if p.get("time") else ""
            sec.zeilen.append(_line(info, f"{how}{d:%d.%m.}{t}: {p['what']} ({p['arena']})", p["src"]))
    if len(nations) >= 2 and not games:  # "Wann spielt die Schweiz gegen Kanada?" across the groups
        groups = info.get("groups") or {}
        where = {n: next((g for g, v in groups.items() if n in v["teams"]), None) for n in nations}
        names = " – ".join(NATION_DE.get(n, n) for n in nations)
        text = f"Kein Vorrundenspiel {names} gespeichert"
        if len(set(where.values())) > 1 and None not in where.values():
            text += (
                " (" + ", ".join(f"{NATION_DE.get(n, n)} in Gruppe {g}" for n, g in where.items()) + "); "
                f"ein Aufeinandertreffen ist erst in der K.-o.-Phase möglich ({info.get('ko_start_note') or 'ab Viertelfinal, 13.11.'})"
            )
        sec.zeilen.append(_line(info, text + ".", "wiki_2026"))
    for d in days or []:
        if not any(g.starts_at.astimezone(TZ).date() == d for g in games) and not any(
            p["day"] == d.isoformat() for p in info.get("playoffs") or []
        ):
            name, url, _kind = _src(info, "iihf_feed")
            sec.zeilen.append(Line(f"{d:%d.%m.}: keine Spiele gespeichert (spielfreier Tag).", name, url))
    return sec


_WINNER = re.compile(r"^(?:winner|sieger(?: spiel)?)\s*(\d+)$", re.I)


def ko_path(info: dict, nation: str) -> list[tuple[dict, str]]:
    """The knock-out games a team can reach up to the quarterfinal, from its group: the games with a slot of
    its group ("A3", "A4" → playoff round; "A1", "A2" → quarterfinal) and the quarterfinals their winners go to.
    Each with how it gets there ("Als A3: "). Without slot data (an older file) all knock-out games."""
    playoffs = info.get("playoffs") or []
    if not any(p.get("slots") for p in playoffs):
        return [(p, "") for p in playoffs]
    group = next((g for g, v in (info.get("groups") or {}).items() if nation in v.get("teams", [])), None)
    if group is None:
        return []
    seed = re.compile(rf"^{re.escape(str(group))}(\d)$", re.I)
    first, out = {}, []
    for p in playoffs:
        mine = [x for x in p.get("slots") or [] if seed.match(x.strip())]
        if mine:
            out.append((p, f"Als {mine[0].strip().upper()}: "))
            if p.get("no") is not None:
                first[int(p["no"])] = mine[0].strip().upper()
    for p in playoffs:
        if not re.search(r"quarter|viertel", f"{p.get('stage', '')} {p['what']}", re.I):
            continue
        for x in p.get("slots") or []:
            mt = _WINNER.match(x.strip())
            if mt and int(mt.group(1)) in first and all(q is not p for q, _ in out):
                out.append((p, f"Als {first[int(mt.group(1))]} nach Sieg in Spiel {mt.group(1)}: "))
    out.sort(key=lambda x: (x[0]["day"], x[0].get("time") or ""))
    if out:  # after the quarterfinal: only the days of the medal round, no more bracket lines
        from klugscheisser.seed.floorball_info import stage_de

        for label, stages in (
            ("Halbfinals (bei Sieg im Viertelfinal)", ("Halbfinal",)),
            ("Final und Spiel um Bronze", ("Final", "Spiel um Bronze")),
        ):
            games = [p for p in playoffs if stage_de(p.get("stage", "")) in stages]
            if games:
                note = {**games[0], "what": label, "time": None}
                out.append((note, ""))
    return out


def sec_facts(info: dict, topic: str, title: str) -> Section:
    sec = Section(topic, title)
    for f in info.get("facts") or []:
        if topic in f.get("topic", []):
            sec.zeilen.append(_line(info, f["text"], f["src"], f.get("quote")))
    return sec


def sec_bilanz(info: dict, question: str, nations: list[str]) -> Section:
    """Head-to-head at WMs: only the opponent(s) the question names ("Bilanz Schweiz gegen Schweden"); all
    opponents met (never a "0 Spiele" line) when none is named. The per-edition game lists only for a named
    year or when no head-to-head line exists."""
    from klugscheisser import projects

    home = projects.current().home_nation
    opp = [n for n in nations if n != home]
    years = {int(y) for y in re.findall(r"\b(19[89]\d|20[0-2]\d)\b", question)}
    facts = [f for f in info.get("facts") or [] if "bilanz" in f.get("topic", [])]
    h2h = [f for f in facts if f.get("opponent")]
    per_year = [f for f in facts if not f.get("opponent")]
    if opp:
        title = "Bilanz der Schweiz gegen " + ", ".join(NATION_DE.get(n, n) for n in opp) + " an WMs"
        lines = [f for f in h2h if f["opponent"] in opp]
        rows = [f for f in per_year if (f.get("year") in years) and set(f.get("opponents") or []) & set(opp)]
    else:
        title = "Bilanz der Schweiz an WMs"
        lines = [f for f in h2h if f.get("played", 1)]
        rows = [f for f in per_year if f.get("year") in years]
    if not lines and not opp:
        rows = rows or per_year
    sec = Section("bilanz", title)
    for f in [*lines, *rows]:
        sec.zeilen.append(_line(info, f["text"], f["src"], f.get("quote")))
    if opp and not lines:
        for n in opp:
            sec.zeilen.append(
                _line(info, f"Schweiz gegen {NATION_DE.get(n, n)}: keine WM-Bilanz gespeichert.", "wiki_2026")
            )
    return sec


def sec_quarterfinal(s: Session, ev: m.Event, info: dict, nations: list[str]) -> Section:
    sec = sec_facts(info, "viertelfinal", "Viertelfinals und Medaillenspiele")
    groups = info.get("groups") or {}
    for nat in nations or ["SUI"]:
        g = next((k for k, v in groups.items() if nat in v["teams"]), None)
        if g is None or len(groups) != 2:  # the cross-over A1–B4 … only with two groups (floorball: 4, facts)
            continue
        other = next(k for k in groups if k != g)
        pairs = ", ".join(f"als {g}{i} gegen {other}{5 - i}" for i in range(1, 5))
        sec.zeilen.append(
            _line(
                info,
                f"Für {'die Schweiz' if nat == 'SUI' else NATION_DE.get(nat, nat)} (Gruppe {g}) heisst das im "
                f"Viertelfinal: {pairs} "
                f"(Gegnerin aus Gruppe {other}: {', '.join(NATION_DE.get(c, c) for c in groups[other]['teams'])}).",
                "wiki_2026",
            )
        )
    for p in info.get("playoffs") or []:
        if "Relegation" not in p["what"]:
            d = date.fromisoformat(p["day"])
            t = f" {p['time']}" if p.get("time") else ""
            sec.zeilen.append(_line(info, f"{d:%d.%m.}{t}: {p['what']} ({p['arena']})", p["src"]))
    return sec


def sec_relegation(info: dict) -> Section:
    return sec_facts(info, "abstieg", "Abstieg")


def sec_history(info: dict, question: str) -> Section:
    sec = Section("geschichte", "Frühere WMs (Gastgeber, Medaillen, Rang der Schweiz)")
    years = {int(y) for y in re.findall(r"\b(19[89]\d|20[0-2]\d)\b", question)}
    rows = info.get("history") or []
    if years:
        rows = [r for r in rows if r["year"] in years] or rows
    for r in rows:
        if r.get("note"):
            host = "" if r["host"] in ("–", "") else f" {r['host']}"
            text = f"{r['year']}{host}: {r['note']}"
        else:
            text = (
                f"{r['year']} {r['host']}: Gold {NATION_DE.get(r['gold'], r['gold'])}, Silber "
                f"{NATION_DE.get(r['silver'], r['silver'])}, Bronze {NATION_DE.get(r['bronze'], r['bronze'])}; "
                f"Schweiz {r['sui']}"
            )
            if r.get("extra"):  # floorball: final score, 4th place, Swiss coach, notes
                text += f" ({r['extra']})"
        sec.zeilen.append(_line(info, text, r.get("src") or info.get("history_source") or "wiki_wwc"))
    for n in info.get("history_notes") or []:
        sec.zeilen.append(_line(info, n["text"], n["src"], n.get("quote")))
    sec.zeilen += sec_facts(info, "zukunft", "").zeilen
    if not years and "wiki_sui" in (info.get("sources") or {}):
        sec.zeilen.append(
            _line(
                info,
                "Ränge der Schweiz: Wikipedia-Teamseite (1990–2025); Gastgeber/Medaillen: Wikipedia-WM-Seite.",
                "wiki_sui",
            )
        )
    return sec


def sec_preparation(s: Session, info: dict, nations: list[str]) -> Section:
    want = set(nations or ["SUI"])
    sec = Section("vorbereitung", "Spiele vor der WM (Schweizer Zeit)")
    today = datetime.now(TZ).date()
    for g in sorted(info.get("pre_games") or [], key=lambda x: (x["date"], x.get("time", ""))):
        if not {g["home"], g["away"]} & want:
            continue
        d = date.fromisoformat(g["date"])
        t = f" {g['time']}" if g.get("time") else ""
        sep = " gegen " if g.get("neutral") else " – "
        match = f"{NATION_DE.get(g['home'], g['home'])}{sep}{NATION_DE.get(g['away'], g['away'])}"
        res = (
            f" {g['score']}"
            if g.get("score")
            else (" (gespielt, Resultat nicht gespeichert)" if d < today else "")
        )
        sec.zeilen.append(_line(info, f"{d:%d.%m.}{t}: {match}{res} – {g['what']}", g["src"], g.get("quote")))
    if "SUI" in want and info.get("swiss_preparation"):
        p = info["swiss_preparation"]
        sec.zeilen.append(_line(info, p["text"], p["src"], p.get("quote")))
    return sec


def roster_status(s: Session, ev: m.Event, team: m.Team, info_dir: Path | None = None) -> dict[str, Any]:
    """Stored roster of one team: count, provisional/confirmed, source + date, absences, watcher finding."""
    from klugscheisser.jobs.roster_watch import watch_state
    from klugscheisser.seed.loader import roster_history

    entries = list(
        s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id))
    )
    from klugscheisser.seed.loader import GAME_SHEET_SOURCE

    players = [e for e in entries if e.role in (None, "G", "D", "F")]
    # 2026-11-04: late additions from a game sheet are named apart; the list's source is the official list's
    listed = [e for e in players if not (e.source or "").startswith(GAME_SHEET_SOURCE)] or players
    seed = _roster_seed(ev.key, team.key, info_dir)
    out: dict[str, Any] = {
        "team": team.name,
        "anzahl": len(players),
        "status": (
            "offiziell"
            if players and all(e.list_type == "confirmed" for e in players)
            else ("provisorisch" if players else "kein Kader gespeichert")
        ),
        "quelle": listed[0].source if listed else None,
        "url": listed[0].source_url if listed else None,
        "datum": seed.get("source_date"),
        "fehlen": [{k: a.get(k) for k in ("name", "reason", "quote")} for a in seed.get("absent") or []],
    }
    state = watch_state(s)
    applied = (state.get("applied") or {}).get(team.country or "")
    if len(listed) < len(players):
        from klugscheisser.jobs.game_rosters import added_lines

        out["laut_spielbericht_nachgemeldet"] = added_lines(s, ev, team)
    if applied and listed and (listed[0].source or "") == applied.get("source"):
        out["datum"] = applied.get("date")  # list loaded by the roster watcher: its date, not the old seed's
    # 2026-10-28: players dropped when the official list replaced the earlier one (roster history, not deleted)
    current = {e.athlete_id for e in players}
    gone = [
        h
        for h in roster_history(s, ev, team)
        if h.get("new_list_type") == "confirmed"
        and h.get("athlete_id") not in current
        and h.get("role") in (None, "G", "D", "F")
    ]
    if gone and out["status"] == "offiziell":
        camp = sorted({h["name"] for h in gone if h.get("name") and h.get("list_type") != "confirmed"})
        named = {a.get("name") for a in out["fehlen"]}  # already said with the seed's reason
        late = [
            h for h in gone if h.get("name") and h.get("list_type") == "confirmed" and h["name"] not in named
        ]
        if camp:
            out["nicht_im_offiziellen_kader"] = (
                f"{len(camp)} {projects_wording().players} der früheren provisorischen Liste sind nicht im offiziellen Kader: "
                + ", ".join(camp)
            )
        if late:
            out["aus_offiziellem_kader_gestrichen"] = [
                f"{h['name']}: stand im offiziellen Kader ({h.get('source')}), nicht mehr in der Liste vom "
                f"{_date_de(h.get('replaced_by_date') or h.get('removed'))} ({h.get('replaced_by_url') or ''})"
                for h in late
            ]
    found = state.get("found", {}).get(team.country or "")
    if found and out["status"] != "offiziell":
        out["offizieller_kader"] = (
            f"Offizieller WM-Kader publiziert am {found.get('date_de') or found.get('seen')}: {found.get('url')}"
            + (" (noch nicht eingelesen)" if not found.get("loaded") else "")
        )
    return out


def _date_de(iso: str | None) -> str:
    try:
        return f"{date.fromisoformat(str(iso)[:10]):%d.%m.%Y}"
    except ValueError:
        return str(iso or "?")


@functools.lru_cache(maxsize=64)
def _roster_seed_cached(path: str, mtime: float) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _roster_seed(event_key: str, team_key: str, info_dir: Path | None = None) -> dict:
    rosters = (info_dir or events_dir()).parent / "rosters"
    nat = team_key.split("-")[0]
    path = rosters / f"{event_key}-{nat}.yaml"
    if not path.exists():
        path = rosters / f"{nat.upper()}.yaml"  # floorball research format (rosters/SUI.yaml)
        if not path.exists():
            return {}
    data = _roster_seed_cached(str(path.resolve()), path.stat().st_mtime)
    from klugscheisser.seed.floorball_info import is_research_roster, normalize_roster

    return normalize_roster(data, event_key) if is_research_roster(data) else data


def _site(url: str | None) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.") if url else "Kader"


def sec_rosters(
    s: Session, ev: m.Event, info: dict, nations: list[str], info_dir: Path | None = None
) -> Section:
    sec = Section("kader", "Kader")
    teams = list(s.scalars(select(m.Team).join(m.EventTeam).where(m.EventTeam.event_id == ev.id)))
    teams.sort(key=lambda t: (t.country != "SUI", t.name))
    focus = set(nations or ["SUI"])
    for t in teams:
        st = roster_status(s, ev, t, info_dir)
        when = f", Stand {date.fromisoformat(st['datum']):%d.%m.%Y}" if st.get("datum") else ""
        text = f"{t.name}: {st['status']}, {st['anzahl']} {projects_wording().players}{when}"
        if st.get("quelle"):
            text += f" – {st['quelle']}"
        if st.get("offizieller_kader"):
            text += f". {st['offizieller_kader']}"
        for gone in st.get("aus_offiziellem_kader_gestrichen") or []:
            text += f". Gestrichen: {gone}"
        for late in st.get("laut_spielbericht_nachgemeldet") or []:
            text += f". {late}"
        label = _site(st.get("url"))
        sec.zeilen.append(Line(text, label, st.get("url") or "", kind="official"))
        if t.country in focus:
            entries = [
                e
                for e in s.scalars(
                    select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == t.id)
                )
                if e.role in (None, "G", "D", "F")
            ]
            order = {"G": 0, "D": 1, "F": 2}
            entries.sort(key=lambda e: (order.get(e.role or "", 3), e.athlete.full_name))
            names = "; ".join(
                f"{e.athlete.full_name} ({e.role or '–'}{', #' + str(e.jersey_number) if e.jersey_number else ''})"
                for e in entries
            )
            if names:
                sec.zeilen.append(
                    Line(
                        f"Kader {t.name}: {names}",
                        label,
                        st.get("url") or "",
                        kind="official",
                    )
                )
            from klugscheisser import staff  # v19: the staff named on this roster

            if crew := staff.event_staff(s, ev, t):
                sec.zeilen.append(
                    Line(
                        f"Staff {t.name}: "
                        + "; ".join(
                            f"{staff.LABEL.get(staff.role_of(e.role), e.role)} {e.athlete.full_name}"
                            for e in crew
                        ),
                        _site(crew[0].source_url),
                        crew[0].source_url or "",
                        kind="official",
                    )
                )
            seed = _roster_seed(ev.key, t.key, info_dir)
            for fact in (
                seed.get("squad_facts") or []
            ):  # floorball: facts about the squad, from the announcement
                sec.zeilen.append(
                    Line(fact, seed.get("source") or label, seed.get("source_url") or "", None, "official")
                )
            for a in st["fehlen"]:
                sec.zeilen.append(
                    Line(
                        f"{t.name}: {a['name']} fehlt – {a.get('reason') or 'Grund nicht angegeben'}",
                        label,
                        st.get("url") or "",
                        a.get("quote"),
                        "official",
                    )
                )
    return sec


def sec_results(s: Session, ev: m.Event, info: dict, nations: list[str]) -> Section:
    codes = _team_codes(s)
    games = [g for g in _games(s, ev, info) if g.status in ("final", "live")]
    if nations:
        games = [
            g
            for g in games
            if {codes.get(g.home_team_id or 0), codes.get(g.away_team_id or 0)} & set(nations)
        ]
    sec = Section("resultate", "Resultate")
    sec.zeilen = [_game_line(g, codes) for g in games]
    if not sec.zeilen:
        start = date.fromisoformat(info["starts"])
        sec.zeilen.append(
            _line(info, f"Noch keine WM-Resultate gespeichert (Turnierstart {start:%d.%m.%Y}).", "iihf_site")
        )
    return sec


# --- answer ---------------------------------------------------------------------------------------------------


def answer(
    s: Session,
    ev: m.Event,
    question: str = "",
    info_dir: Path | None = None,
    max_lines: int = 40,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    """The sections that fit the question (overview when none fits). Compact: one line per fact, each with source.
    `topics` overrides what is read from the question (get_event's short sheet)."""
    info = load_info(ev.key, info_dir)
    if info is None:
        raise ValueError(f"Keine Turnierdaten für {ev.key}")
    d1, d2 = date.fromisoformat(info["starts"]), date.fromisoformat(info["ends"])
    days = days_in(question, d1, d2)
    topics = list(topics) if topics is not None else topics_in(question)
    if "spielplan" in topics and len(topics) > 1 and not nations_in(question) and not days:
        topics.remove("spielplan")  # "Wann ist der Final?": the final, not all 20 group games
    nations = nations_in(question)
    sections: list[Section] = []
    if "geschichte" in topics and not CURRENT_CUE.search(question):
        # only about past WMs ("Wo fand die WM 2024 statt?"); with the current one named as well (p01: "WM in
        # Dänemark … Vergleich zur WM 2025") all topics stay and the history comes last
        topics = ["geschichte"] + [t for t in topics if t in ("november",)]
    elif "geschichte" in topics:
        topics = [t for t in topics if t != "geschichte"] + ["geschichte"]
    for t in topics:
        if t == "uebersicht":
            sections.append(sec_overview(s, ev, info))
        elif t == "modus":
            sections += [sec_facts(info, "modus", "Modus"), sec_quarterfinal(s, ev, info, nations)]
        elif t == "gruppen":
            sections.append(sec_groups(s, ev, info, nations))
        elif t == "spielplan":
            sections.append(
                sec_schedule(
                    s,
                    ev,
                    info,
                    nations or (["SUI"] if "gegen" in question.lower() and not days else []),
                    days,
                )
            )
        elif t == "viertelfinal":
            sections.append(sec_quarterfinal(s, ev, info, nations))
        elif t == "abstieg":
            sections.append(sec_relegation(info))
        elif t == "geschichte":
            sections.append(sec_history(info, question))
        elif t == "november":
            sections.append(sec_facts(info, "november", "Warum im November"))
        elif t == "vorbereitung":
            sections.append(sec_preparation(s, info, nations))
        elif t == "kader":
            sections.append(sec_rosters(s, ev, info, nations, info_dir))
        elif t == "teams":
            sections += [sec_groups(s, ev, info, nations), sec_facts(info, "teams", "Teilnehmer")]
        elif t == "ort":
            sec = Section("ort", "Spielorte")
            for v in info.get("venues") or []:
                sec.zeilen.append(
                    _line(
                        info,
                        _venue_text(v),
                        v["src"],
                    )
                )
            sections.append(sec)
        elif t == "bilanz":
            sections.append(sec_bilanz(info, question, nations))
        elif t == "rekorde":
            sections.append(sec_facts(info, "rekorde", "WM-Rekorde (IFF)"))
        elif t == "funfacts":
            sections.append(sec_facts(info, "funfacts", "Fun Facts zur WM"))
        elif t == "schiedsrichter":
            sections.append(sec_facts(info, "schiedsrichter", "Schiedsrichter"))
        elif t == "resultate":
            sections.append(sec_results(s, ev, info, nations))
    if not any(sec.zeilen for sec in sections):
        sections = [sec_overview(s, ev, info)]
    # one section per kind, lines deduplicated, capped
    seen_titles, seen_text, out_secs, n = set(), set(), [], 0
    for sec in sections:
        if sec.titel in seen_titles or not sec.zeilen:
            continue
        seen_titles.add(sec.titel)
        lines = []
        for ln in sec.zeilen:
            if ln.text in seen_text or n >= max_lines:
                continue
            seen_text.add(ln.text)
            lines.append(ln)
            n += 1
        if lines:
            out_secs.append(
                {"thema": sec.thema, "titel": sec.titel, "zeilen": [x.out() for x in lines], "_lines": lines}
            )
    urls = {ln.url for sec in out_secs for ln in sec["_lines"] if ln.url}
    result = {
        "turnier": info.get("name", ev.name),
        "kopf": f"{d1:%d.%m.}–{d2:%d.%m.%Y}, {info.get('host')}",
        "key": ev.key,
        "frage": question,
        "themen": topics or ["uebersicht"],
        "geprueft": info.get("checked"),
        "abschnitte": [{k: v for k, v in sec.items() if k != "_lines"} for sec in out_secs],
        "quellen": len(urls),
        "hinweis": "Gespeicherte, belegte Turnierdaten (Karl). Jede Zeile mit ihrem Link übernehmen; "
        "nichts ergänzen, was hier nicht steht.",
    }
    result["_render"] = [(sec["titel"], sec["_lines"]) for sec in out_secs]
    return result


def render(result: dict[str, Any], datenstand: str | None = None) -> str:
    from klugscheisser.mcp_server.present import zeile_prefix

    out = []
    if datenstand:
        out.append(f"**Datenstand:** {datenstand} · Turnierdaten geprüft am {result.get('geprueft')}")
    out.append(f"**{result['turnier']}** – {result.get('kopf', '')}".rstrip(" –"))
    for titel, lines in result.get("_render") or []:
        out.append(f"\n**{titel}**")
        for ln in lines:
            link = f" [{ln.quelle}]({ln.url})" if ln.url else ""
            quote = f" «{ln.zitat}»" if ln.zitat else ""
            out.append(
                f"- {zeile_prefix(ln.kind if ln.kind != 'web' else 'web', None)} {ln.text}{quote}{link}"
            )
    out.append("\n_Nur diese belegten Angaben; was fehlt, ist nicht gespeichert (kein Gegenbeweis)._")
    from klugscheisser.mcp_server.present import one_source_per_block

    return one_source_per_block("\n".join(out))


def public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def answer_rows(result: dict[str, Any]) -> list[list[str]]:
    """Rows for the web chat's direct answer: [Angabe, Quelle, sentence]."""
    rows = []
    for _titel, lines in result.get("_render") or []:
        for ln in lines:
            rows.append([ln.text, ln.quelle, ln.text])
    return rows
