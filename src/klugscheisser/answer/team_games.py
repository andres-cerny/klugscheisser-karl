"""Last games of a team and head-to-head meetings, from all stored games (every competition and source).

Used by list_events(team=…, last=…, since=…, opponent=…) and by the direct `frage` lookup for questions like
"Form von Tschechien", "letzte 3 Spiele der USA", "Resultate von Österreich", "Wann haben die Schweiz und
Österreich zuletzt gegeneinander gespielt?". No model: rows straight from the `events` table, each with date,
competition, score, the result from the team's view and the source link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

DEFAULT_LAST = 5
DEFAULT_H2H = 10
MAX_LAST = 30

NUMBER_WORDS = {
    "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
# "letzten 3 Spiele", "last five games", "letzte drei Länderspiele"
LAST_N = re.compile(
    r"\b(?:letzte[nms]?|vergangene[nms]?|last|past|recent)\s+" + _NUM + r"\s+(?:\w+\s+)?"
    r"(?:spiel\w*|länderspiel\w*|partie\w*|match\w*|games?|resultat\w*|ergebnis\w*|results?)",
    re.I,
)
# questions about a team's recent games / results / form
RESULTS_Q = re.compile(
    r"\b(?:letzte[nms]?|vergangene[nms]?|jüngste[nms]?|bisherige[nms]?|last|recent|latest|past)\s+(?:\w+\s+)?"
    r"(?:spiele|länderspiele|partien|matches|games|resultate|ergebnisse|results|scores|auftritte)\b"
    r"|\b(?:form|formkurve|formstand|bilanz|record)\b"
    r"|\b(?:resultate|ergebnisse|results)\s+(?:von|der|des|vom|of|for)\b"
    r"|\bwie\s+(?:hat|haben|spielte|spielten)\b.*\bzuletzt\b"
    r"|\bzuletzt\b.*\b(?:gespielt|gewonnen|verloren|resultat\w*|ergebnis\w*)\b"
    r"|\bhead.?to.?head\b|\bh2h\b|\bdirekt(?:e[nms]?)?\s*(?:duell\w*|vergleich\w*|begegnung\w*)\b"
    r"|\bgegeneinander\b|\bletzte[nms]?\s+(?:duell|aufeinandertreffen|begegnung|meeting)\b",
    re.I,
)
# two teams in the question: head-to-head only with one of these words (else: each team's own games)
H2H = re.compile(
    r"\b(gegen|gegeneinander|vs\.?|versus|against|head.?to.?head|h2h|duell\w*|direkt\w*|zwischen|between|"
    r"aufeinandertreffen|begegnung\w*|meeting|meetings)\b",
    re.I,
)


def wants_team_games(question: str) -> bool:
    return bool(RESULTS_Q.search(question) or LAST_N.search(question))


def last_n(question: str, default: int = DEFAULT_LAST) -> int:
    if hit := LAST_N.search(question):
        word = hit.group(1).lower()
        n = int(word) if word.isdigit() else NUMBER_WORDS.get(word, default)
        return max(1, min(n, MAX_LAST))
    return default


@dataclass
class TeamGames:
    team: m.Team
    games: list[m.Event] = field(default_factory=list)  # newest first
    opponent: m.Team | None = None
    upcoming: m.Event | None = None  # next scheduled game (head-to-head: next meeting)
    stored_total: int = 0  # all final games of the team (or of the pair) in the database
    first_stored: datetime | None = None
    competitions: list[str] = field(default_factory=list)  # competitions of all stored games


def _plays(ids: set[int]):
    return or_(m.Event.home_team_id.in_(ids), m.Event.away_team_id.in_(ids))


def _between(a: set[int], b: set[int]):
    return or_(
        m.Event.home_team_id.in_(a) & m.Event.away_team_id.in_(b),
        m.Event.home_team_id.in_(b) & m.Event.away_team_id.in_(a),
    )


def team_games(
    s: Session,
    team: m.Team,
    team_ids: set[int] | None = None,
    opponent: m.Team | None = None,
    opponent_ids: set[int] | None = None,
    last: int | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
) -> TeamGames:
    """Played games (final or live) of `team`, newest first: the last `last` (default 5, head-to-head 10) or all
    since `since`; with `opponent` only their meetings. All competitions and sources."""
    now = now or datetime.now(UTC)
    ids = team_ids or {team.id}
    cond = _plays(ids)
    if opponent is not None:
        cond = _between(ids, opponent_ids or {opponent.id})
    base = select(m.Event).where(m.Event.kind == "game", cond)
    played = base.where(m.Event.status.in_(("final", "live")), m.Event.starts_at <= now)
    all_played = list(s.scalars(played.order_by(m.Event.starts_at.desc().nulls_last(), m.Event.id.desc())))
    games = [g for g in all_played if since is None or (g.starts_at and g.starts_at >= since)]
    n = last or (None if since is not None else (DEFAULT_H2H if opponent is not None else DEFAULT_LAST))
    if n:
        games = games[: min(n, MAX_LAST)]
    upcoming = s.scalar(
        base.where(m.Event.status != "final", m.Event.starts_at > now).order_by(m.Event.starts_at).limit(1)
    )
    comps = list(
        dict.fromkeys(
            g.competition.name for g in all_played if g.competition is not None and g.competition.name
        )
    )
    return TeamGames(
        team=team,
        games=games,
        opponent=opponent,
        upcoming=upcoming,
        stored_total=len(all_played),
        first_stored=min((g.starts_at for g in all_played if g.starts_at), default=None),
        competitions=comps,
    )


_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")


def outcome(g: m.Event, ids: set[int]) -> str | None:
    """'Sieg', 'Niederlage n.P.', 'Unentschieden' … from the view of the team `ids`; None without a score."""
    hit = _SCORE.search(g.score or "")
    if not hit or g.status != "final":
        return None
    home, away = int(hit.group(1)), int(hit.group(2))
    own, other = (home, away) if g.home_team_id in ids else (away, home)
    extra = " n.P." if "n.P." in (g.score or "") else (" n.V." if "n.V." in (g.score or "") else "")
    if own == other:
        return "Unentschieden" + extra
    return ("Sieg" if own > other else "Niederlage") + extra


def record(games: list[m.Event], ids: set[int]) -> str | None:
    """'5 Spiele: 3 Siege (1 n.P.), 2 Niederlagen; Tore 12:8'."""
    wins = losses = draws = extra_w = extra_l = gf = ga = counted = 0
    for g in games:
        res = outcome(g, ids)
        hit = _SCORE.search(g.score or "")
        if res is None or not hit:
            continue
        counted += 1
        home, away = int(hit.group(1)), int(hit.group(2))
        own, other = (home, away) if g.home_team_id in ids else (away, home)
        gf, ga = gf + own, ga + other
        late = "n.P." in res or "n.V." in res
        if res.startswith("Sieg"):
            wins, extra_w = wins + 1, extra_w + late
        elif res.startswith("Niederlage"):
            losses, extra_l = losses + 1, extra_l + late
        else:
            draws += 1
    if not counted:
        return None
    bits = [
        f"{wins} {'Sieg' if wins == 1 else 'Siege'}" + (f" ({extra_w} n.V./n.P.)" if extra_w else ""),
        f"{losses} {'Niederlage' if losses == 1 else 'Niederlagen'}"
        + (f" ({extra_l} n.V./n.P.)" if extra_l else ""),
    ]
    if draws:
        bits.append(f"{draws} Unentschieden")
    return f"{counted} {'Spiel' if counted == 1 else 'Spiele'}: " + ", ".join(bits) + f"; Tore {gf}:{ga}"


def game_row(g: m.Event, ids: set[int] | None = None) -> dict:
    """One game for the tools: date (Swiss time), match, score, result from the team's view, competition, link."""
    from klugscheisser.answer.games import game_when

    out = {
        "key": g.key,
        "datum": game_when(g),
        "name": g.name,
        "resultat": g.score if g.score and g.status in ("final", "live") else None,
        "ausgang": outcome(g, ids) if ids else None,
        "status": "live" if g.status == "live" else None,
        "ort": g.city or ((g.venue or "").split(",")[0].strip() or None),
        "wettbewerb": g.competition.name if g.competition else None,
        "quelle": g.source,
        "url": g.source_url,
    }
    return {k: v for k, v in out.items() if v}


def coverage_note(tg: TeamGames) -> str:
    """What is stored for the team (or the pair): how many games since when, from which competitions."""
    who = f"{tg.team.name} – {tg.opponent.name}" if tg.opponent is not None else tg.team.name
    if not tg.stored_total:
        return f"Keine gespielten Spiele von {who} gespeichert."
    since = f" seit {tg.first_stored:%d.%m.%Y}" if tg.first_stored else ""
    comps = (
        f" ({' · '.join(tg.competitions[:6])}{' …' if len(tg.competitions) > 6 else ''})"
        if tg.competitions
        else ""
    )
    return (
        f"Gespeichert: {tg.stored_total} {'gespieltes Spiel' if tg.stored_total == 1 else 'gespielte Spiele'} von {who}{since}{comps}. "
        "Spiele ausserhalb dieser Wettbewerbe sind nicht gespeichert."
    )


def render(tg: TeamGames, ids: set[int] | None = None) -> list[str]:
    """Markdown lines (frage / list_events): heading, one line per game, record, next game, coverage."""
    from klugscheisser.answer.games import game_when

    ids = ids or {tg.team.id}
    if tg.opponent is not None:
        head = (
            f"**🏒 Direktvergleich {tg.team.name} – {tg.opponent.name}** (neueste zuerst, alle Wettbewerbe)"
        )
    else:
        head = f"**🏒 Letzte Spiele {tg.team.name}** (neueste zuerst, alle Wettbewerbe)"
    out = [head]
    for g in tg.games:
        r = game_row(g, ids)
        res = f" {r['resultat']}" if r.get("resultat") else ""
        aus = (
            f" ({r['ausgang']} {tg.team.name})"
            if r.get("ausgang")
            else (" (live)" if r.get("status") else "")
        )
        comp = f" – {r['wettbewerb']}" if r.get("wettbewerb") else ""
        where = (
            f", {r['ort']}" if r.get("ort") and r["ort"].split()[0] not in (r.get("wettbewerb") or "") else ""
        )
        link = f" [{r.get('quelle') or 'Quelle'}]({r['url']})" if r.get("url") else ""
        out.append(f"- 🏒 (Resultat) {r['datum']}: {r['name']}{res}{aus}{comp}{where}{link}")
    if not tg.games:
        out.append("- Keine gespielten Spiele im gefragten Zeitraum gespeichert.")
    if rec := record(tg.games, ids):
        out.append(f"- Bilanz {tg.team.name} in diesen Spielen: {rec}")
    if (nx := tg.upcoming) is not None:
        what = "Nächstes Duell" if tg.opponent is not None else "Nächstes Spiel"
        comp = f" – {nx.competition.name}" if nx.competition else ""
        link = f" [{nx.source or 'Quelle'}]({nx.source_url})" if nx.source_url else ""
        out.append(f"- 📅 {what}: {game_when(nx)}: {nx.name}{comp}{link}")
    out.append(f"_{coverage_note(tg)}_")
    return out


# --- questions --------------------------------------------------------------------------------------------


def for_question(
    s: Session,
    question: str,
    team: m.Team | None = None,
    event: m.Event | None = None,
    now: datetime | None = None,
) -> list[tuple[TeamGames, set[int]]] | None:
    """A question about recent games / results / form / head-to-head of one or more teams → their stored games.
    `team` (the tool's team parameter) counts as named first. None: not such a question, or no team named."""
    from klugscheisser.answer.games import Side, tournament_games
    from klugscheisser.answer.lookups import sides

    if not wants_team_games(question):
        return None
    found = sides(s, question)
    if team is not None:
        found = [x for x in found if team.id not in x.ids]
        found.insert(0, Side(team, {team.id}))
    if not found:
        return None
    now = now or datetime.now(UTC)
    explicit = bool(LAST_N.search(question))
    n = last_n(question)
    keep: set[int] | None = None
    if event is not None and event.kind != "game" and event.starts_at and event.starts_at <= now:
        keep = {g.id for g in tournament_games(s, event)}  # a played tournament named: only its games
    out = []
    if len(found) >= 2 and H2H.search(question):
        a, b = found[0], found[1]
        tg = team_games(s, a.team, a.ids, b.team, b.ids, last=n if explicit else None, now=now)
        out.append((tg, a.ids))
    else:
        for side in found[:4]:
            out.append((team_games(s, side.team, side.ids, last=n, now=now), side.ids))
    if keep is not None:
        for tg, _ in out:
            tg.games = [g for g in tg.games if g.id in keep]
    return out


def answer_text(
    s: Session, question: str, datenstand: str, team: m.Team | None = None, event: m.Event | None = None
) -> str | None:
    """The `frage` tool's text for a team-games question (None: not one)."""
    found = for_question(s, question, team, event)
    if not found:
        return None
    if not any(tg.games for tg, _ in found):
        from klugscheisser import tournament

        # 2026-11-24: "Bilanz Schweiz gegen Schweden an Weltmeisterschaften" with no stored games: the tournament
        # sheet's head-to-head (past WMs) answers, not "keine Spiele gespeichert"
        if tournament.event_for_question(s, question, event.id if event is not None else None) is not None:
            return None
    out = [f"**Datenstand:** {datenstand}"]
    for tg, ids in found:
        out += ["", *render(tg, ids)]
    out.append(
        "\n_Antwort mit «Datenstand: …» beginnen. Zeilen unverändert übernehmen (Resultat + Wettbewerb + Link); "
        "was hier fehlt, ist nicht gespeichert. Mehr Spiele: list_events(team='…', last=N) oder "
        "list_events(team='…', opponent='…') für alle Duelle._"
    )
    return "\n".join(out)
