"""Game questions about a tournament ("Wie hat die Schweiz in Lahti gespielt?", "Wer stand in Lahti im Tor?",
"Torschützinnen an der Euro Hockey Tour") answered from the stored games first.

The tournament comes from the tool's `event` argument or is named in the question (its city, its key, or the
tournament word: Euro Hockey Tour / WM / Olympia → the newest that has started). Its played games are looked up in
the stored results (events kind=game in the tournament's dates, between its teams; only the named team's games when
one is named). The writer then reads one computed overview (date, game, score, venue) + each game's official
result (goals, assists, goalies, lineups) + a few match reports — instead of every player's documents (the cross
route read all 39–135 persons of the event: ~190 Jev calls and 17 writer calls for one question).

Rules only, no model. None → the question goes the usual way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.sports import current_spec, home_team_key

GAME_DETAIL = re.compile(
    r"\b(resultat\w*|ergebnis\w*|results?|scores?|spielstand|tore|tor|goals?|torschütz\w*|scorer\w*|"
    r"torhüter\w*|goalies?|goaltend\w*|keeper|paraden|saves?|assists?|vorlagen|penalty\w*|penalties|"
    r"shootout|gespielt|abgeschnitten|gewonnen|verloren|niederlage\w*|sieg\w*|played|won|lost|"
    r"spiele|spielen|spielte|spielten|games?|matches?|partien?|punkte|points|rang|platz|placed|finished)\b",
    re.I,
)
NOT_GAMES = re.compile(
    r"\b(kader|roster|aufgebot|nominiert|spielplan|schedule|nächste[nms]?|next|upcoming)\b", re.I
)
MAX_GAMES = 12
MAX_REPORTS = 6


@dataclass
class EventGames:
    event: m.Event
    games: list[m.Event]
    docs: list = field(default_factory=list)  # engine.Doc: overview first, then official results, reports
    team: m.Team | None = None

    @property
    def note(self) -> str:
        who = f" ({self.team.name})" if self.team else ""
        return f"Spiele {self.event.name}{who}: gespeicherte Resultate und Spielberichte"

    @property
    def searched(self) -> str:
        return f"{len(self.games)} Spiele · {len(self.docs)} Dokumente aus gespeicherten Resultaten"


def _span(ev: m.Event) -> tuple[datetime, datetime]:
    lo = ev.starts_at
    hi = ev.ends_at or ev.starts_at + timedelta(days=7)
    return lo - timedelta(days=1), hi + timedelta(days=1)


def named_event(s: Session, question: str, event_id: int | None = None) -> m.Event | None:
    """The tool's event, else a tournament named by its city or key ("Lahti", "eht-2026-lahti"), else the newest
    started tournament of a named kind (Euro Hockey Tour, WM, Olympia)."""
    from klugscheisser.answer.games import tournaments
    from klugscheisser.domain.names import normalize

    if event_id is not None:
        return s.get(m.Event, event_id)
    words = set(normalize(question).split())
    now = datetime.now(UTC)
    containers = list(
        s.scalars(select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None)))
    )
    hits = []
    for ev in containers:
        names = {w for w in normalize(ev.city or "").split() if len(w) >= 4}
        if not (tail := ev.key.rsplit("-", 1)[-1]).isdigit():  # "lahti", never "2026" (oly-2026, wch-2026)
            names.add(tail)
        if names & words or ev.key.lower() in question.lower():
            hits.append(ev)
    if not hits:
        for rx, _comp, prefix, _label in tournaments():
            if rx.search(question):
                hits = [e for e in containers if e.key.startswith(prefix)]
                break
    started = sorted((e for e in hits if e.starts_at <= now), key=lambda e: e.starts_at, reverse=True)
    return started[0] if started else None


def event_games(s: Session, ev: m.Event) -> list[m.Event]:
    """Stored games of a tournament (or the game itself), oldest first."""
    if ev.kind == "game":
        return [ev]
    if ev.starts_at is None:
        return []
    lo, hi = _span(ev)
    teams = set(s.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == ev.id)))
    q = select(m.Event).where(m.Event.kind == "game", m.Event.starts_at >= lo, m.Event.starts_at <= hi)
    if len(teams) >= 2:
        q = q.where(m.Event.home_team_id.in_(teams), m.Event.away_team_id.in_(teams))
    elif teams:
        q = q.where(or_(m.Event.home_team_id.in_(teams), m.Event.away_team_id.in_(teams)))
    elif ev.competition_id:
        q = q.where(m.Event.competition_id == ev.competition_id)
    else:
        return []
    return list(s.scalars(q.order_by(m.Event.starts_at, m.Event.id)))


def _when(ev: m.Event) -> str:
    from klugscheisser.domain.labels import TZ

    local = ev.starts_at.astimezone(TZ)
    return f"{local:%d.%m.%Y}" if (local.hour, local.minute) == (0, 0) else f"{local:%d.%m.%Y %H:%M}"


def find(
    s: Session, question: str, event_id: int | None = None, team_id: int | None = None
) -> EventGames | None:
    """The tournament's played games for a game question, with the documents for the writer; None → usual way."""
    from klugscheisser.answer.cross import _computed_doc, _named
    from klugscheisser.answer.engine import item_doc, search_terms
    from klugscheisser.answer.games import Side, _mentions, result_item, sides_in, window_items
    from klugscheisser.domain.names import normalize

    if not GAME_DETAIL.search(question) or NOT_GAMES.search(question):
        return None
    if _named(s, question):
        return None  # "Wie hat Stalder in Lahti gespielt?": her own documents (the usual way)
    ev = named_event(s, question, event_id)
    if ev is None:
        return None
    games = [g for g in event_games(s, ev) if g.status in ("final", "live") and g.score]
    if not games:
        return None
    city = set(normalize(ev.city or "").split())
    # "in Södertälje" names the tournament, not the club Södertälje SK
    found = [x for x in sides_in(s, question) if not set(normalize(x.team.name).split()) & city]
    side = None
    if team_id is not None and (t := s.get(m.Team, team_id)) is not None:
        side = next((x for x in found if team_id in x.ids), Side(t, {t.id}))
    elif len(found) == 1:
        side = found[0]
    elif len(found) == 2:  # "Schweiz gegen Finnland in Lahti": their game(s), when stored
        a, b = (x.ids for x in found)
        both = [
            g for g in games if {g.home_team_id, g.away_team_id} & a and {g.home_team_id, g.away_team_id} & b
        ]
        games = both or games
    if side is not None:
        games = [g for g in games if {g.home_team_id, g.away_team_id} & side.ids]
        if not games:
            return None
    games = games[-MAX_GAMES:]
    terms = search_terms(question)
    rows = [
        f"{_when(g)}: {g.name} {g.score}" + (f" ({g.venue or g.city})" if g.venue or g.city else "")
        for g in games
    ]
    who = f" ({side.team.name})" if side else ""
    basis = [{"quelle": g.source or "Resultate", "url": g.source_url} for g in games if g.source_url]
    body = f"Gespielte Spiele {ev.name}{who}: " + "; ".join(rows) + "."
    overview = _computed_doc(body, f"Spiele {ev.name}{who}", [], basis)
    docs = [overview]
    seen: set[int] = set()
    for g in games:
        item = result_item(s, g)
        if item is not None and item.id not in seen:
            seen.add(item.id)
            docs.append(item_doc(item, terms))
    if side is not None:  # match reports of the named team in the tournament's days
        lo, hi = _span(ev)
        reports, _ = window_items(s, [side], [], lo, hi)
        for item in reports[:MAX_REPORTS]:
            if item.id not in seen and _mentions(side.team, f"{item.title or ''} {(item.text or '')[:400]}"):
                seen.add(item.id)
                docs.append(item_doc(item, terms))
    return EventGames(event=ev, games=games, docs=docs, team=side.team if side else None)


# --- 24.10.: direct lookup for the MCP `frage` tool (no model) ----------------------------------------------------

ASKS_GAMES = re.compile(
    r"\b(resultat\w*|ergebnis\w*|results?|scores?|gespielt|abgeschnitten|gewonnen|verloren|played|won|lost|"
    r"spiele|spielt|spielen|games?|matches?|partien?|gegen wen|gegner\w*|opponents?|spielplan|schedule|"
    r"anspiel\w*|wann|when|uhrzeit|face-?off|termin\w*|daten|datum)\b",
    re.I,
)
# who scored / kept goal: the official results' details and the writer (find() above)
PLAYER_DETAIL = re.compile(
    r"\b(tor(e|en)?|getroffen|torschütz\w*|scorer\w*|skorer\w*|assists?|vorlagen|torhüter\w*|goalies?|keeper|"
    r"paraden|saves?|aufstellung\w*|lineups?)\b",
    re.I,
)
SWISS = re.compile(r"\b(nati|schweiz\w*|swiss|switzerland|suisse|svizzera)\b", re.I)


def named_events(s: Session, question: str) -> list[m.Event]:
    """Tournaments named by their city or key ("Lahti", "Södertälje", "eht-2026-lahti"), oldest first; the WCh /
    Olympics stay with their own sheets unless named the same way."""
    from klugscheisser.domain.names import normalize

    words = set(normalize(question).split())
    out = []
    for ev in s.scalars(select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None))):
        names = {w for w in normalize(ev.city or "").split() if len(w) >= 4}
        if not (tail := ev.key.rsplit("-", 1)[-1]).isdigit():  # "lahti", never "2026" (oly-2026, wch-2026)
            names.add(tail)
        if names & words or ev.key.lower() in question.lower():
            out.append(ev)
    return sorted(out, key=lambda e: e.starts_at)


def _status(ev: m.Event, games: list[m.Event], now: datetime) -> str:
    end = effective_end(ev, games)
    if ev.starts_at > now:
        return "bevorstehend"
    if end is not None and end < now:
        return "beendet"
    return "läuft"


MAX_TOURNAMENT_DAYS = 14


def effective_end(ev: m.Event, games: list[m.Event] | None = None) -> datetime | None:
    """The tournament's end: its stored end, else the time of its last game when all are played, else (no games
    stored) MAX_TOURNAMENT_DAYS after its start — so a tournament without an end date never stays "läuft"."""
    if ev.ends_at is not None:
        return ev.ends_at
    games = [g for g in games or [] if g.starts_at]
    if games:
        if all(g.status == "final" for g in games):
            return max(g.starts_at for g in games) + timedelta(hours=4)
        return None
    if ev.starts_at is not None:
        return ev.starts_at + timedelta(days=MAX_TOURNAMENT_DAYS)
    return None


def answer_text(
    s: Session,
    question: str,
    datenstand: str,
    team: m.Team | None = None,
    event: m.Event | None = None,
    now: datetime | None = None,
) -> str | None:
    """The games of the tournament(s) named in the question (by city/key, or the tool's non-WCh event): played games
    with score, result and link, upcoming ones with date and Swiss time. None: not such a question."""
    from klugscheisser.answer.cross import _named
    from klugscheisser.answer.games import game_when, sides_in
    from klugscheisser.answer.team_games import outcome, record
    from klugscheisser.domain.names import normalize

    now = now or datetime.now(UTC)
    if not ASKS_GAMES.search(question) or PLAYER_DETAIL.search(question) or _named(s, question):
        return None
    evs = named_events(s, question)
    if event is not None and event.kind != "game" and event not in evs:
        if not evs and event.key.startswith(("wch-", "oly-")):
            return None  # the WCh / Olympics sheet answers its own schedule questions
        evs = [event, *evs]
    if not evs:
        return None
    cities = {w for ev in evs for w in normalize(ev.city or "").split()}
    found = [x for x in sides_in(s, question) if not set(normalize(x.team.name).split()) & cities]
    focus: set[int] | None = None
    label = None
    if team is not None:
        focus, label = {team.id}, team.name
    elif len(found) == 1:
        focus, label = found[0].ids, found[0].team.name
    elif (sui := next((x for x in found if x.team.key == home_team_key()), None)) and SWISS.search(question):
        focus, label = sui.ids, sui.team.name  # "die Nati … (nicht finnische Zeit)": the home team's games
    elif not found and SWISS.search(question):
        sui = s.scalar(select(m.Team).where(m.Team.key == home_team_key()))
        if sui is not None:
            focus, label = {sui.id}, sui.name
    out = [f"**Datenstand:** {datenstand}"]
    any_games = False
    for ev in evs:
        games = event_games(s, ev)
        status = _status(ev, games, now)
        mine = [g for g in games if focus is None or {g.home_team_id, g.away_team_id} & focus]
        span = _when(ev).split(" ")[0]
        end = effective_end(ev, games)
        if end is not None:
            span += f"–{end.astimezone(_tz()):%d.%m.%Y}"
        who = f" – Spiele {label}" if label else ""
        out += ["", f"**🏒 {ev.name}{who}** ({span}, {status}; Zeiten Schweizer Zeit)"]
        for g in mine:
            any_games = True
            link = f" [{g.source or 'Quelle'}]({g.source_url})" if g.source_url else ""
            if g.status in ("final", "live") and g.score:
                res = outcome(g, focus) if focus else None
                aus = f" ({res} {label})" if res else (" (live)" if g.status == "live" else "")
                out.append(f"- {game_when(g)}: {g.name} {g.score}{aus}{link}")
            else:
                out.append(f"- 📅 {_weekday(g)} {game_when(g)}: {g.name}{link}")
        if not mine:
            out.append("- Keine Spiele dieses Turniers gespeichert" + (f" für {label}." if label else "."))
        played = [g for g in mine if g.status == "final"]
        if focus and played and (rec := record(played, focus)):
            out.append(f"- Bilanz {label}: {rec}")
    if not any_games:
        return None
    scorers = "Torschützen" if current_spec().men else "Torschützinnen"
    out.append(
        "\n_Antwort mit «Datenstand: …» beginnen. Zeilen unverändert übernehmen (Datum, Schweizer Zeit, Resultat, "
        f"Link); was fehlt, ist nicht gespeichert. {scorers}/Aufstellung: frage('Wer hat … getroffen?') oder "
        "get_event(key)._"
    )
    return "\n".join(out)


def _tz():
    from klugscheisser.domain.labels import TZ

    return TZ


WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _weekday(g: m.Event) -> str:
    return WEEKDAYS_DE[g.starts_at.astimezone(_tz()).weekday()] if g.starts_at else ""
