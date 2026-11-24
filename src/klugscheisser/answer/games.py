"""v12 Part F: recognise a specific game in a question ("das letzte Spiel Schweiz–Finnland an der EHT").

Rules only (no model): two teams (nations or clubs) + a game word (Spiel, Match, game, gegen, vs, "–") +
optional tournament / date words. The game is looked up in the stored results (events kind=game from the
results collectors, their `official_result` item carries the lineups). One game → its players, a date window
(game day ±2) and its documents first; several → the app asks back; none → the latest stored meeting or the
named tournament, labelled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ
from klugscheisser.projects import wording
from klugscheisser.sports import home_team_key, national_team_key

WINDOW_DAYS = 2  # documents from game day −2 to +2
PER_PERSON = 2  # documents per player in the window
MAX_PERSON_DOCS = 40
MAX_REPORTS = 10
RECENT_DAYS = 30  # no stored game at all: joint reports of the two teams from the last 30 days

GAME_WORD = re.compile(
    r"\b(spiel|spiels|match|matchs|partie|game|gegen|vs\.?|versus|against|duell|begegnung)\b|\s[–—-]\s|[–—]",
    re.I,
)
AGAINST = re.compile(r"\b(gegen|vs\.?|versus|against)\b", re.I)
LATEST = re.compile(
    r"\b(letzte[nms]?|vergangene[nms]?|recent|last|latest|neueste[nms]?|zuletzt|most recent)\b", re.I
)
NEXT = re.compile(r"\b(nächste[nms]?|kommende[nms]?|bevorstehende[nms]?|next|upcoming)\b", re.I)
# tournament words → (competition name pattern, tournament event key prefix, label)
TOURNAMENTS = [
    (
        re.compile(r"euro.?hockey(.?tour)?|\bw?eht\b", re.I),
        r"euro hockey tour|\beht\b",
        "eht-",
        "Euro Hockey Tour",
    ),
    (
        re.compile(r"\b(wm|weltmeisterschaft|world championships?|wch)\b", re.I),
        r"\bwm\b|world championship|weltmeister",
        "wch-",
        "WM",
    ),
    (re.compile(r"olymp", re.I), r"olymp", "oly-", "Olympia"),
]
MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "märz": 3, "march": 3,
    "april": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "juli": 7, "july": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}  # fmt: skip
DATE_NUM = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.(\d{2,4})?")
DATE_WORD = re.compile(r"\b(\d{1,2})\.?\s+(" + "|".join(MONTHS) + r")\b\.?(?:\s+(\d{4}))?", re.I)
# names in other languages of the match reports (French/Italian outlets), on top of adds.wiki.COUNTRIES
EXTRA_NATION_WORDS = {"SUI": r"suisse|svizzer|schweizerinnen", "GER": r"allemagne|germania"}


def tournaments() -> list[tuple]:
    """TOURNAMENTS of the current project: the WM entry points at the project's main tournament (floorball:
    "wfc-2026" → events "wfc-…", competition "World Floorball Championships"); ice hockey unchanged."""
    from klugscheisser import projects

    prefix = projects.current().main_event.split("-", 1)[0] + "-"
    if prefix == "wch-":
        return TOURNAMENTS
    return [
        (rx, comp + r"|world floorball championship|\bwfc\b", prefix, label)
        if p == "wch-"
        else (rx, comp, p, label)
        for rx, comp, p, label in TOURNAMENTS
    ]


@dataclass
class Side:
    """One team of a game as named in the question. `ids`: all stored team rows that mean it (the results
    feed and the rosters sometimes use different rows for one club, e.g. "EV Zug" and "EVZ Women's Team")."""

    team: m.Team
    ids: set[int]


@dataclass
class GameRef:
    sides: list[Side]
    tournament: tuple | None = None  # one entry of TOURNAMENTS
    day: date | None = None
    latest: bool = False
    upcoming: bool = False
    assumed_swiss: bool = False  # "gegen Finnland": the Swiss team is the other side

    @property
    def teams(self) -> list[m.Team]:
        return [x.team for x in self.sides]


@dataclass
class GameScope:
    """What a recognised (or not found) game gives the answer step."""

    note: str
    ids: list[int] = field(default_factory=list)
    groups: list[list[int]] = field(default_factory=list)  # players per team (relations across teams)
    since: datetime | None = None
    until: datetime | None = None
    event: m.Event | None = None
    items: list[m.Item] = field(default_factory=list)  # game documents: official result + match reports
    found: bool = False
    sides: list[Side] = field(default_factory=list)
    label: str = ""  # short name for the head of a computed document ("Spiel Schweiz–Finnland, …")


def _players(s: Session, ids) -> list[int]:
    """Only players (no coaches/staff), in the given order."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    keep = set(s.scalars(select(m.Athlete.id).where(m.Athlete.id.in_(ids), m.Athlete.role == "athlete")))
    return [i for i in ids if i in keep]


def sides_in(s: Session, question: str) -> list[Side]:
    """National teams (country words) and clubs (names/aliases) named in the question, one Side per team."""
    from klugscheisser.adds.wiki import countries_in
    from klugscheisser.domain.names import normalize
    from klugscheisser.domain.team_matcher import TeamMatcher

    out: list[Side] = []
    for ioc in countries_in(question):
        team = s.scalar(select(m.Team).where(m.Team.key == national_team_key(ioc)))
        if team is not None:
            out.append(Side(team, {team.id}))
    tm = TeamMatcher(s)
    if tm._rx is None:
        return out
    clubs = [t for t in s.scalars(select(m.Team).where(m.Team.kind != "national"))]
    # a question is always about the project's sport (women's hockey): clubs count without the article checks
    # (women/hockey context); national-team aliases are covered by the country words above
    for mt in tm._rx.finditer(normalize(question)):
        alias = mt.group(1)
        found = [s.get(m.Team, e.team_id) for e in tm.by_alias[alias]]
        found = [t for t in found if t is not None and t.kind != "national"]
        if not found or len(alias) < 3:
            continue
        ids = {t.id for t in found} | {t.id for t in clubs if alias in normalize(t.name)}
        for side in out:
            if side.ids & ids:
                side.ids |= ids
                break
        else:
            out.append(Side(found[0], ids))
    return out


def teams_in(s: Session, question: str) -> list[m.Team]:
    return [x.team for x in sides_in(s, question)]


def _day_in(question: str, today: date) -> date | None:
    q = question.lower()
    if re.search(r"\bvorgestern\b", q):
        return today - timedelta(days=2)
    if re.search(r"\b(gestern|yesterday)\b", q):
        return today - timedelta(days=1)
    if re.search(r"\b(heute|today|tonight)\b", q):
        return today
    if re.search(r"\b(morgen|tomorrow)\b", q):
        return today + timedelta(days=1)
    for rx, month_of in ((DATE_NUM, int), (DATE_WORD, lambda g: MONTHS[g.lower()])):
        if mt := rx.search(question):
            try:
                year = int(mt.group(3)) if mt.group(3) else today.year
                year += 2000 if year < 100 else 0
                return date(year, month_of(mt.group(2)), int(mt.group(1)))
            except (ValueError, KeyError):
                continue
    return None


def game_ref(s: Session, question: str, today: date | None = None) -> GameRef | None:
    """Two teams + a game word → GameRef. One team with "gegen/vs" (not the Swiss team) → the Swiss team is
    assumed as the other side ("Wer hat gegen Finnland getroffen?")."""
    if not GAME_WORD.search(question):
        return None
    sides = sides_in(s, question)
    assumed = False
    if len(sides) == 1 and AGAINST.search(question) and sides[0].team.key != home_team_key():
        sui = s.scalar(select(m.Team).where(m.Team.key == home_team_key()))
        if sui is not None:
            sides, assumed = [Side(sui, {sui.id}), sides[0]], True
    if len(sides) != 2:
        return None
    today = today or datetime.now(TZ).date()
    return GameRef(
        sides=sides,
        tournament=next((t for t in tournaments() if t[0].search(question)), None),
        day=_day_in(question, today),
        latest=bool(LATEST.search(question)),
        upcoming=bool(NEXT.search(question)),
        assumed_swiss=assumed,
    )


def _local_day(dt: datetime | None) -> date | None:
    return dt.astimezone(TZ).date() if dt else None


def _tournament_events(s: Session, ref: GameRef) -> list[m.Event]:
    """Stored tournaments of the named kind: newest that has started first (an upcoming one only for "next"
    or when none has started)."""
    if ref.tournament is None:
        return []
    events = list(
        s.scalars(
            select(m.Event)
            .where(m.Event.key.startswith(ref.tournament[2]), m.Event.kind != "game")
            .order_by(m.Event.starts_at.desc().nulls_last())
        )
    )
    now = datetime.now(UTC)
    started = [e for e in events if e.starts_at and e.starts_at <= now]
    rest = [e for e in events if e not in started]
    return rest[::-1] + started if ref.upcoming else started + rest[::-1]


def _span(ev: m.Event) -> tuple[datetime | None, datetime | None]:
    if ev.starts_at is None:
        return None, None
    return ev.starts_at, ev.ends_at or ev.starts_at + timedelta(days=7)


def tournament_games(s: Session, ev: m.Event) -> list[m.Event]:
    """Stored games of a tournament container, oldest first: games in its date span (one day of slack each side)
    between two of its teams (any of its teams when fewer than two are stored; its competition when none)."""
    if ev.starts_at is None:
        return []
    lo, hi = _span(ev)
    teams = set(s.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == ev.id)))
    q = select(m.Event).where(
        m.Event.kind == "game",
        m.Event.starts_at >= lo - timedelta(days=1),
        m.Event.starts_at <= hi + timedelta(days=1),
    )
    if len(teams) >= 2:
        q = q.where(m.Event.home_team_id.in_(teams), m.Event.away_team_id.in_(teams))
    elif teams:
        q = q.where(or_(m.Event.home_team_id.in_(teams), m.Event.away_team_id.in_(teams)))
    elif ev.competition_id:
        q = q.where(m.Event.competition_id == ev.competition_id)
    else:
        return []
    return list(s.scalars(q.order_by(m.Event.starts_at, m.Event.id)))


def game_when(ev: m.Event) -> str:
    """'30.09.2026 11:30' in Swiss time; the date only when the source gave no time (00:00)."""
    if ev.starts_at is None:
        return "?"
    local = ev.starts_at.astimezone(TZ)
    return f"{local:%d.%m.%Y}" if (local.hour, local.minute) == (0, 0) else f"{local:%d.%m.%Y %H:%M}"


def _meetings(s: Session, ref: GameRef) -> list[m.Event]:
    """All stored games between the two teams, newest first."""
    a, b = (x.ids for x in ref.sides)
    return list(
        s.scalars(
            select(m.Event)
            .where(
                m.Event.kind == "game",
                or_(
                    m.Event.home_team_id.in_(a) & m.Event.away_team_id.in_(b),
                    m.Event.home_team_id.in_(b) & m.Event.away_team_id.in_(a),
                ),
            )
            .order_by(m.Event.starts_at.desc().nulls_last())
        )
    )


def find_games(s: Session, ref: GameRef, now: datetime | None = None) -> list[m.Event]:
    """Candidate games for the reference, best first. More than one → the caller asks back."""
    now = now or datetime.now(UTC)
    games = _meetings(s, ref)
    if ref.tournament is not None:
        comp_rx = re.compile(ref.tournament[1], re.I)
        spans = [_span(ev) for ev in _tournament_events(s, ref)]

        def in_tournament(g: m.Event) -> bool:
            if g.competition and comp_rx.search(g.competition.name or ""):
                return True
            return any(lo and g.starts_at and lo - timedelta(days=1) <= g.starts_at <= hi for lo, hi in spans)

        games = [g for g in games if in_tournament(g)]
    if ref.day is not None:
        games = [g for g in games if (d := _local_day(g.starts_at)) and abs((d - ref.day).days) <= 1]
        return sorted(games, key=lambda g: abs((_local_day(g.starts_at) - ref.day).days))
    started = [g for g in games if g.status in ("final", "live") or (g.starts_at and g.starts_at <= now)]
    coming = sorted((g for g in games if g not in started and g.starts_at), key=lambda g: g.starts_at)
    if ref.upcoming:
        return coming[:1]
    if ref.latest:
        return started[:1]
    return started + coming


def game_label(ev: m.Event, with_score: bool = True) -> str:
    """'Schweiz–Finnland, Lahti 03.10.2026, 2:3'."""
    place = ev.city or (ev.venue or "").split(",")[0].strip()
    when = _local_day(ev.starts_at)
    bits = [ev.name.replace(" – ", "–")]
    where = " ".join(filter(None, [place, f"{when:%d.%m.%Y}" if when else None]))
    if where:
        bits.append(where)
    if with_score and ev.status == "final" and ev.score:
        bits.append(ev.score)
    return ", ".join(bits)


def suggestion_label(ev: m.Event) -> str:
    """Short form for a tap-to-send suggestion: 'Schweiz–Finnland, Lahti 3.10.'."""
    when = _local_day(ev.starts_at)
    place = ev.city or (ev.venue or "").split(",")[0].strip()
    day = f"{when.day}.{when.month}." if when else ""
    return ", ".join(filter(None, [ev.name.replace(" – ", "–"), " ".join(filter(None, [place, day]))]))


def result_item(s: Session, ev: m.Event) -> m.Item | None:
    if not ev.source_url:
        return None
    return s.scalar(
        select(m.Item).where(m.Item.url == ev.source_url, m.Item.source_type == "official_result").limit(1)
    )


def _event_sides(s: Session, ev: m.Event, ref: GameRef | None) -> list[Side]:
    """The game's two teams, with the other rows the question's side stands for."""
    out = []
    for tid in (ev.home_team_id, ev.away_team_id):
        team = s.get(m.Team, tid) if tid else None
        if team is None:
            continue
        ids = next((x.ids for x in (ref.sides if ref else []) if tid in x.ids), {tid})
        out.append(Side(team, set(ids) | {tid}))
    return out or (ref.sides if ref else [])


def lineup_groups(
    s: Session, ev: m.Event, item: m.Item | None, sides: list[Side]
) -> tuple[list[list[int]], bool]:
    """Players of both teams in that game: the lineup stored with the official result (names matched to the
    persons linked to the item), else the roster of a tournament around the game day, else the teams'
    rosters / affiliations. One list per team (home, away); bool: from the lineup."""
    from klugscheisser.domain.names import normalize

    if item is not None and (lineup := (item.raw or {}).get("lineup")):
        by_name = {}
        for ia in item.athletes:
            if ia.athlete:
                parts = normalize(ia.athlete.full_name).split()
                by_name[" ".join(parts)] = ia.athlete_id
                by_name[" ".join(parts[1:] + parts[:1])] = ia.athlete_id
        groups = []
        for key in _lineup_keys(list(lineup), sides):
            names = lineup.get(key) or [] if key else []
            groups.append(_players(s, [by_name[normalize(n)] for n in names if normalize(n) in by_name]))
        if sum(len(g) for g in groups) >= 4:
            return groups, True
    return [roster_ids(s, side, ev.starts_at) for side in sides], False


def _lineup_keys(keys: list[str], sides: list[Side]) -> list[str | None]:
    """Which lineup key belongs to which side: the source writes "SC Bern" where our team is "SC Bern Frauen".
    Exact name, then one name inside the other, then the one key left over."""
    from klugscheisser.domain.names import normalize

    out: list[str | None] = []
    for side in sides:
        name = normalize(side.team.name)
        hit = next((k for k in keys if normalize(k) == name), None) or next(
            (k for k in keys if normalize(k) in name or name in normalize(k)), None
        )
        out.append(hit)
    left = [k for k in keys if k not in out]
    if out.count(None) == 1 and len(left) == 1:
        out[out.index(None)] = left[0]
    return out


def roster_ids(s: Session, side: Side, when: datetime | None) -> list[int]:
    """Players of a team around a date: the entries of a tournament running then, else the team's newest
    roster / affiliations (national team: players of that nation when no roster is stored)."""
    from klugscheisser.retrieval.search import athlete_team_filter

    if when is not None:
        for ev in s.scalars(select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None))):
            lo, hi = _span(ev)
            if lo - timedelta(days=1) <= when <= hi:
                entries = select(m.EventEntry.athlete_id).where(
                    m.EventEntry.event_id == ev.id, m.EventEntry.team_id.in_(side.ids)
                )
                if ids := _players(s, s.scalars(entries)):
                    return ids
    ids = []
    for tid in sorted(side.ids):
        ids += athlete_team_filter(s, s.get(m.Team, tid)).athlete_ids or []
    ids = _players(s, ids)
    team = side.team
    if len(ids) < 10 and team.kind == "national" and team.country:
        ids = _players(s, s.scalars(select(m.Athlete.id).where(m.Athlete.nationality == team.country)))
    return ids


def _nation_rx(team: m.Team) -> re.Pattern | None:
    from klugscheisser.adds.wiki import COUNTRIES

    if team.kind != "national" or not team.country or team.country not in COUNTRIES:
        return None
    extra = EXTRA_NATION_WORDS.get(team.country)
    return re.compile(COUNTRIES[team.country][2] + (f"|{extra}" if extra else ""), re.I)


def _mentions(team: m.Team, text: str) -> bool:
    from klugscheisser.domain.names import normalize

    if rx := _nation_rx(team):
        return bool(rx.search(text))
    words = [
        w
        for w in normalize(team.name).split()
        if len(w) >= 3 and w not in ("frauen", "women", "damen", "herren", "men")
    ]
    return bool(words) and all(w in normalize(text) for w in words)


def window_items(
    s: Session, sides: list[Side], ids: list[int], since: datetime, until: datetime
) -> tuple[list[m.Item], list[m.Item]]:
    """(match reports, per-person documents) published in the window. Reports: both teams in the title first,
    then linked to both teams, then both named in the lead; one per title. Per-person documents only for
    players with documents in the window (PER_PERSON each)."""
    base = (
        select(m.Item)
        .outerjoin(m.Classification, m.Classification.item_id == m.Item.id)
        .where(
            m.Item.level == "full",
            m.Item.published_at >= since,
            m.Item.published_at <= until,
            or_(m.Classification.id.is_(None), m.Classification.relevant),
        )
        .order_by(m.Item.published_at.desc())
    )
    from klugscheisser.adds.wiki import countries_in

    own = {x.team.country for x in sides if x.team.kind == "national"}
    scored = []
    for item in s.scalars(base.where(m.Item.source_type != "official_result")):
        linked = {it.team_id for it in item.teams}
        title = item.title or ""
        lead = f"{title} {(item.text or '')[:400]}"
        if all(_mentions(x.team, title) for x in sides):
            # a third nation in the title ("in Finnland: … gegen Tschechien") is most likely another game
            scored.append((0.5 if own and set(countries_in(title)) - own else 0, item))
        elif all(x.ids & linked for x in sides):
            scored.append((1, item))
        elif all(_mentions(x.team, lead) for x in sides):
            scored.append((2, item))
    reports, titles = [], set()
    for _, item in sorted(scored, key=lambda r: r[0]):  # stable: newest first within a rank
        if (item.title or item.url) in titles:
            continue
        titles.add(item.title or item.url)
        reports.append(item)
        if len(reports) >= MAX_REPORTS:
            break
    seen = {i.id for i in reports}
    per_person: list[m.Item] = []
    if ids:
        count: dict[int, int] = {}
        q = base.join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id).where(
            m.ItemAthlete.athlete_id.in_(ids)
        )
        for item in s.scalars(q):
            if item.id in seen:
                continue
            who = [ia.athlete_id for ia in item.athletes if ia.athlete_id in ids]
            if who and all(count.get(a, 0) >= PER_PERSON for a in who):
                continue
            for a in who:
                count[a] = count.get(a, 0) + 1
            seen.add(item.id)
            per_person.append(item)
            if len(per_person) >= MAX_PERSON_DOCS:
                break
    return reports, per_person


def game_window(ev: m.Event, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Game day ±2 days; a game that has not been played yet: the last 14 days before it (previews)."""
    now = now or datetime.now(UTC)
    if ev.starts_at and ev.starts_at > now + timedelta(days=WINDOW_DAYS):
        return now - timedelta(days=14), now
    day = _local_day(ev.starts_at) or now.astimezone(TZ).date()
    lo = datetime.combine(day - timedelta(days=WINDOW_DAYS), time.min, TZ)
    hi = datetime.combine(day + timedelta(days=WINDOW_DAYS), time.max, TZ)
    return lo, hi


def scope_for_game(s: Session, ref: GameRef | None, ev: m.Event) -> GameScope:
    item = result_item(s, ev)
    sides = _event_sides(s, ev, ref)
    groups, from_lineup = lineup_groups(s, ev, item, sides)
    groups = [g for g in groups if g]
    ids = list(dict.fromkeys(i for g in groups for i in g))
    lo, hi = game_window(ev)
    reports, _ = window_items(s, sides, [], lo, hi)
    note = f"Spiel {game_label(ev)}"
    if from_lineup:
        note += f" · Aufstellung ({len(ids)} {wording().players} in unserer Datenbank)"
    else:
        note += f" · Kader beider Teams ({len(ids)} {wording().players}; keine Aufstellung gespeichert)"
    if ref is not None and ref.assumed_swiss:
        note += " · Schweiz als Gegner angenommen"
    return GameScope(
        note=note,
        ids=ids,
        groups=groups,
        since=lo,
        until=hi,
        event=ev,
        items=([item] if item else []) + [r for r in reports if not item or r.id != item.id],
        found=True,
        sides=sides,
        label=f"Spiel {game_label(ev)}",
    )


def scope_without_game(s: Session, ref: GameRef) -> GameScope:
    """No stored game matches: the named tournament's window and rosters, else the latest stored meeting
    (labelled), else both teams' rosters with their joint reports of the last RECENT_DAYS days."""
    names = "–".join(t.name for t in ref.teams)
    where = f" ({ref.tournament[3]})" if ref.tournament else ""
    missing = f"Spiel {names}{where} nicht in den gespeicherten Resultaten"
    for ev in _tournament_events(s, ref):
        lo, hi = _span(ev)
        if lo is None:
            continue
        groups = [g for g in (roster_ids(s, x, ev.starts_at) for x in ref.sides) if g]
        ids = list(dict.fromkeys(i for g in groups for i in g))
        span = f"{_local_day(lo):%d.%m.}–{_local_day(hi):%d.%m.%Y}"
        lo, hi = lo - timedelta(days=1), hi + timedelta(days=1)
        reports, _ = window_items(s, ref.sides, [], lo, hi)
        return GameScope(
            note=f"{missing}; Berichte und Kader aus {ev.name} ({span})",
            ids=ids,
            groups=groups,
            since=lo,
            until=hi,
            items=reports,
            sides=ref.sides,
            label=f"Kader {' und '.join(ref_names(ref))} ({ev.name})",
        )
    meetings = [g for g in _meetings(s, ref) if g.status in ("final", "live")]
    if meetings:
        sc = scope_for_game(s, ref, meetings[0])
        sc.note = f"{missing}; stattdessen letztes gespeichertes Spiel: {sc.note}"
        sc.found = False
        return sc
    groups = [g for g in (roster_ids(s, x, None) for x in ref.sides) if g]
    ids = list(dict.fromkeys(i for g in groups for i in g))
    now = datetime.now(UTC)
    reports, _ = window_items(s, ref.sides, [], now - timedelta(days=RECENT_DAYS), now)
    note = f"{missing}; keine gespeicherte Begegnung, Kader beider Teams"
    if reports:
        note += f" und neueste Berichte über beide Teams (letzte {RECENT_DAYS} Tage)"
    return GameScope(
        note=note,
        ids=ids,
        groups=groups,
        items=reports,
        sides=ref.sides,
        label=f"Kader {' und '.join(ref_names(ref))}",
    )


def ref_names(ref: GameRef) -> list[str]:
    return [t.name for t in ref.teams]
