"""v13: direct answers from stored data for question types that had no rule: the coach of a team, a player's
form in her last games, the next/last game of one team. Each returns `cross.ProsaInputs` with one computed
document whose `extra["direct"]` the engine shows without a model (summary, table, unknown line), or None.

These are reached by the question templates the interpreter writes (answer/interpret.py) and by the same
German phrasings typed directly."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.facts.source_site import cite_label, item_info, needs_site, row_site
from klugscheisser.projects import wording

COACH = re.compile(
    r"\b(trainer(in)?|coach(es|ed)?|head ?coach|headcoach|cheftrainer(in)?|nationaltrainer(in)?|wer trainiert)\b",
    re.I,
)
FORM = re.compile(
    r"\b(form|letzten?\s+(\d+|fünf|zehn)\s+spiel\w*|last\s+(\d+|five|ten)\s+games|zuletzt|lately|in letzter zeit)\b",
    re.I,
)
TEN = re.compile(r"\b(10|zehn|ten)\b", re.I)
NEXT = re.compile(r"\b(nächste[snm]?|kommende[snm]?|next|upcoming)\b|\bwann spielt\b", re.I)
LAST = re.compile(r"\b(letzte[snm]?|last|vorige[snm]?|latest)\b", re.I)
GAME = re.compile(r"\b(spiel\w*|match\w*|game|partie|wann spielt)\b", re.I)
STAFF_ROLES = ("coach", "staff")
TZ = ZoneInfo("Europe/Zurich")


AWARDS = re.compile(
    r"\b(auszeichnung\w*|awards?|preis\w*|trophä\w*|titel|medaille\w*|erfolge|ehrung\w*|honou?rs?|"
    r"gewonnen|won)\b",
    re.I,
)
SEASON = re.compile(r"\b(20\d\d)\s*[/-]\s*(\d{2,4})\b")


def sides(s: Session, question: str) -> list:
    """Teams named in the question; v13: also a club named by its main word only ("Frölunda" → "Frölunda HC",
    "Brynäs" → "Brynäs IF"), when exactly one stored club has that word."""
    from klugscheisser.answer.games import Side, sides_in
    from klugscheisser.domain.names import normalize
    from klugscheisser.facts.teams import team_key

    found = sides_in(s, question)
    if found:
        return found
    words = {w for w in normalize(question).split() if len(w) >= 5}
    if not words:
        return []
    hits: dict[str, list[m.Team]] = {}
    for t in s.scalars(select(m.Team).where(m.Team.kind != "national")):
        for w in set(team_key(t.name).split()) & words:
            hits.setdefault(w, []).append(t)
    out = []
    for teams in hits.values():
        names = {team_key(t.name) for t in teams}
        if len(names) == 1:  # one club (possibly several rows of it), never a guess between clubs
            out.append(Side(teams[0], {t.id for t in teams}))
    return out if len(out) == 1 else []


def _ep_url(a: m.Athlete) -> str:
    return f"https://www.eliteprospects.com/player/{a.eliteprospects_id}" if a.eliteprospects_id else ""


def _inputs(
    body: str, label: str, names: list[str], basis: list[dict], direct: dict, ids: list[int], note: str
):
    from klugscheisser.answer import cross

    doc = cross._computed_doc(body, label, names, basis)
    doc.extra["direct"] = direct
    return cross.ProsaInputs(docs=cross._numbered([doc]), athlete_ids=ids, scope_note=note)


# --- coach ------------------------------------------------------------------------------------------------


def coach_inputs(s: Session, question: str):
    """ "Wer ist der Trainer von Finnland?" / "Who coaches Finland?" → persons with role coach/staff stored
    for that team (affiliations, newest first), with the roster/affiliation source."""
    if not COACH.search(question):
        return None
    if direct := staff_inputs(s, question):  # v19: stored team staff with since/until first
        return direct
    found = sides(s, question)
    if len(found) != 1:
        return None
    side = found[0]
    rows = s.execute(
        select(m.Athlete, m.Affiliation)
        .join(m.Affiliation, m.Affiliation.athlete_id == m.Athlete.id)
        .where(m.Affiliation.team_id.in_(side.ids), m.Athlete.role.in_(STAFF_ROLES))
        .order_by(m.Affiliation.season.desc().nulls_last(), m.Affiliation.id.desc())
    ).all()
    seen, found = set(), []
    for a, af in rows:
        if a.id not in seen:
            seen.add(a.id)
            found.append((a, af))
    if not found:
        return None
    team = side.team.name
    summary = f"{'Trainer' if len(found) == 1 else 'Trainer/Staff'} von {team}: " + ", ".join(
        a.full_name for a, _ in found[:3]
    )
    table = []
    for a, af in found:
        when = f"Saison {af.season}" if af.season else "ohne Saison gespeichert"
        table.append([a.full_name, a.role, when, f"{a.full_name}: {a.role} {team} ({when})"])
    basis = [{"quelle": af.source or "Kader", "url": af.source_url or _ep_url(a)} for a, af in found]
    body = f"{summary} (gespeicherte Kader/Zugehörigkeiten). " + "; ".join(r[-1] for r in table) + "."
    direct = {"summary": summary, "rows": table, "unknown": None, "spalten": ["Person", "Rolle", "Saison"]}
    return _inputs(
        body,
        f"Trainer {team}",
        [a.full_name for a, _ in found],
        basis,
        direct,
        [a.id for a, _ in found],
        f"Trainer von {team}",
    )


def staff_inputs(s: Session, question: str):
    """v19: the team's stored staff (klugscheisser/staff.py): current coach and staff, changes, event roster."""
    from klugscheisser import staff

    res = staff.answer(s, question)
    if res is None:
        return None
    rows = [[z["text"], z["quelle"], z["text"]] for sec in res["abschnitte"] for z in sec["zeilen"]]
    names = res["aktuell"][:3]
    who = "Trainer" if res["nur_trainer"] and len(names) == 1 else "Trainer/Staff"
    summary = f"{who} von {res['team']}: " + (
        ", ".join(names) or "; ".join(sec["titel"] for sec in res["abschnitte"])
    )
    basis = [
        {"quelle": z["quelle"], "url": z["url"]}
        for sec in res["abschnitte"]
        for z in sec["zeilen"]
        if z["url"]
    ]
    body = summary + ". " + " ".join(r[0] + "." for r in rows)
    direct = {"summary": summary, "rows": rows, "unknown": None, "spalten": ["Angabe", "Quelle"]}
    return _inputs(
        body, f"Staff {res['team']}", [], basis, direct, res["personen_ids"], f"Staff {res['team']}"
    )


# --- form -------------------------------------------------------------------------------------------------


def _form_line(f: dict) -> str:
    if f.get("save_pct") is not None or f.get("goals_against") is not None:  # goalie
        bits = [f"{f.get('games')} Spiele"]
        if f.get("save_pct") is not None:
            bits.append(f"Fangquote {f['save_pct'] * 100:.1f} %".replace(".", ","))
        if f.get("goals_against") is not None:
            bits.append(f"{f['goals_against']} Gegentore")
        if f.get("saves") is not None:
            bits.append(f"{f['saves']} Paraden")
        return ", ".join(bits)
    bits = [f"{f.get('games')} Spiele"]
    for key, word in (("goals", "Tore"), ("assists", "Assists"), ("points", "Punkte")):
        if f.get(key) is not None:
            bits.append(f"{f[key]} {word}")
    if f.get("plus_minus") is not None:
        bits.append(f"+/- {f['plus_minus']:+d}")
    return ", ".join(bits)


def form_inputs(s: Session, question: str, athletes: list):
    """ "Form von Lara Stalder in den letzten 5 Spielen" → Elite Prospects' totals over her last 5 (or 10)
    games, per named person (at most 5), with the data date. No model."""
    if not FORM.search(question) or not athletes or len(athletes) > 5:
        return None
    window = "last10" if TEN.search(question) else "last5"
    n = 10 if window == "last10" else 5
    table, basis, names, missing, stand = [], [], [], [], None
    for a in athletes:
        ep = s.get(m.EpProfile, a.id)
        f = ((ep.data or {}).get("form") or {}).get(window) if ep else None
        if not f or not f.get("games"):
            missing.append(a.full_name)
            continue
        line = _form_line(f)
        when = f"{ep.fetched_at:%d.%m.%Y}" if ep.fetched_at else "?"
        stand = stand or when
        table.append([a.full_name, line, f"{a.full_name}, letzte {n} Spiele (Stand {when}): {line}"])
        basis.append({"quelle": "Elite Prospects", "url": ep.url})
        names.append(a.full_name)
    if not table:
        return None
    summary = f"Form in den letzten {n} Spielen (Elite Prospects, Stand {stand}): " + "; ".join(
        f"{r[0]}: {r[1]}" for r in table
    )
    unknown = f"Keine Formdaten gespeichert für: {', '.join(missing)}" if missing else None
    body = summary + "." + (f" {unknown}." if unknown else "")
    direct = {
        "summary": summary,
        "rows": table,
        "unknown": unknown,
        "spalten": [wording().player, f"Letzte {n} Spiele"],
    }
    return _inputs(body, f"Form, letzte {n} Spiele", names, basis, direct, [a.id for a in athletes], None)


# --- next / last game of one team -------------------------------------------------------------------------


def team_game_inputs(s: Session, question: str, now: datetime | None = None):
    """ "Wann spielt Frölunda das nächste Mal?" / "Wie ist das letzte Spiel von Frölunda ausgegangen?" — one
    team named: its next stored game (date, opponent, venue) or its last final game (score)."""
    if not GAME.search(question):
        return None
    upcoming, latest = bool(NEXT.search(question)), bool(LAST.search(question))
    if upcoming == latest:
        return None
    found = sides(s, question)
    if len(found) != 1:
        return None  # two teams: the game recognition of answer/games.py
    side, now = found[0], now or datetime.now(UTC)
    q = select(m.Event).where(
        m.Event.kind == "game", or_(m.Event.home_team_id.in_(side.ids), m.Event.away_team_id.in_(side.ids))
    )
    if upcoming:
        q = q.where(m.Event.starts_at > now).order_by(m.Event.starts_at)
    else:
        q = q.where(m.Event.status == "final", m.Event.starts_at <= now).order_by(m.Event.starts_at.desc())
    games = list(s.scalars(q.limit(3)))
    if not games:
        return None

    def team_name(tid):
        t = s.get(m.Team, tid) if tid else None
        return t.name if t else "?"

    from klugscheisser.answer.games import game_when

    table = []
    for ev in games:
        when = game_when(ev)
        match = f"{team_name(ev.home_team_id)} – {team_name(ev.away_team_id)}"
        where = ", ".join(x for x in (ev.venue, ev.city) if x) or "–"
        cells = [when, match, ev.score or ("geplant" if upcoming else "–"), where]
        table.append([*cells, f"{when}: {match}" + (f" {ev.score}" if ev.score else "") + f" ({where})"])
    what = "Nächstes Spiel" if upcoming else "Letztes Spiel"
    summary = f"{what} von {side.team.name}: {table[0][-1]}"
    basis = [{"quelle": ev.source or "Resultate", "url": ev.source_url or ""} for ev in games]
    body = (
        f"{summary}. Weitere: " + "; ".join(r[-1] for r in table[1:]) + "."
        if len(table) > 1
        else summary + "."
    )
    direct = {
        "summary": summary,
        "rows": table,
        "unknown": None,
        "spalten": ["Datum", "Spiel", "Resultat", "Ort"],
    }
    note = f"{what} von {side.team.name} (gespeicherte Resultate/Spielpläne)"
    return _inputs(body, f"{what} {side.team.name}", [], basis, direct, [], note)


# --- games of a tournament ---------------------------------------------------------------------------------

RESULTS_WORD = re.compile(
    r"\b(spiel\w*|match\w*|games?|partie\w*|resultat\w*|ergebnis\w*|results?|scores?|spielplan|schedule|"
    r"fixtures?|programm|gegner\w*|gespielt|abgeschnitten|gewonnen|verloren|played|won|lost)\b",
    re.I,
)


def named_tournament(s: Session, question: str, upcoming: bool = False) -> m.Event | None:
    """A stored tournament container named in the question by its city ("Lahti", "Södertälje"): the newest that
    has started (the next one for "nächste"/"next", or when none has started)."""
    from klugscheisser.domain.names import normalize

    words = set(normalize(question).split())
    hits = []
    for ev in s.scalars(select(m.Event).where(m.Event.kind != "game", m.Event.starts_at.is_not(None))):
        names = {w for w in normalize(ev.city or "").split() if len(w) >= 4}
        names.add(ev.key.rsplit("-", 1)[-1])
        if names & words:
            hits.append(ev)
    if not hits:
        return None
    now = datetime.now(UTC)
    started = sorted((e for e in hits if e.starts_at <= now), key=lambda e: e.starts_at, reverse=True)
    coming = sorted((e for e in hits if e.starts_at > now), key=lambda e: e.starts_at)
    order = coming + started if upcoming else started + coming
    return order[0]


def tournament_games_inputs(s: Session, question: str, athletes: list):
    """ "Wie hat die Schweiz in Lahti gespielt?" / "Spielplan Södertälje" — the stored games of a tournament named by
    its city (date, time, score, venue), only the named team's games when one team is named."""
    from klugscheisser.answer.games import game_when, tournament_games
    from klugscheisser.domain.names import normalize

    if athletes or not RESULTS_WORD.search(question):
        return None
    ev = named_tournament(s, question, upcoming=bool(NEXT.search(question)))
    if ev is None:
        return None
    from klugscheisser import tournament

    if tournament.load_info(ev.key) is not None:
        return None  # a tournament with a fact sheet (WM): tournament_inputs answers from it
    games = tournament_games(s, ev)
    city_words = set(normalize(ev.city or "").split())
    # "Spielplan Södertälje": the city names the tournament, not the club Södertälje SK
    found = [x for x in sides(s, question) if not set(normalize(x.team.name).split()) & city_words]
    team = None
    if len(found) == 1:
        team = found[0].team
        games = [g for g in games if {g.home_team_id, g.away_team_id} & found[0].ids]
    if not games:
        return None
    table = []
    for g in games:
        when, score = game_when(g), g.score or ("live" if g.status == "live" else "geplant")
        where = g.venue or ev.city or "–"
        table.append([when, g.name, score, where, f"{when}: {g.name} {score} ({where})"])
    played = [g for g in games if g.status == "final"]
    who = f" ({team.name})" if team else ""
    summary = f"{ev.name}{who}: {len(games)} Spiele" + (
        f", {len(played)} gespielt" if played and len(played) < len(games) else ""
    )
    basis = [{"quelle": g.source or "Resultate", "url": g.source_url or ""} for g in games]
    body = f"{summary}. " + "; ".join(r[-1] for r in table) + "."
    direct = {
        "summary": summary,
        "rows": table,
        "unknown": None,
        "spalten": ["Datum", "Spiel", "Resultat", "Ort"],
    }
    return _inputs(body, f"Spiele {ev.name}", [], basis, direct, [], f"Spiele {ev.name}{who}")


# --- awards ------------------------------------------------------------------------------------------------


def award_inputs(s: Session, question: str, athletes: list):
    """ "Welche Auszeichnungen hat Andrea Brändli 2025/26 gewonnen?" → her stored medals and awards (all
    sources, medals first), optionally of one season, each with its source. At most 3 persons."""
    if not AWARDS.search(question) or not athletes or len(athletes) > 3:
        return None
    season = None
    if hit := SEASON.search(question):
        season = int(hit.group(1))
    rows, basis, seen = [], [], set()
    for a in athletes:
        q = select(m.Fact).where(
            m.Fact.athlete_id == a.id, m.Fact.kind.in_(("medal", "award")), m.Fact.status != "unconfirmed"
        )
        facts = list(s.scalars(q))
        if season is not None:
            facts = [f for f in facts if f.season == season or (f.value or {}).get("season") == season]
        facts.sort(key=lambda f: (f.kind != "medal", -(f.season or (f.value or {}).get("season") or 0)))
        info = item_info(s, {f.source_item_id for f in facts if needs_site(f.source)})
        for f in facts:
            key = (a.id, f.value_text.lower())
            if key in seen:
                continue
            seen.add(key)
            src = cite_label(f.source, f.source_url, row_site(f, info))  # web: "EV Zug, 16.12.2025"
            rows.append([a.full_name, f.value_text, src, f"{a.full_name}: {f.value_text} ({src})"])
            basis.append({"quelle": src, "url": f.source_url})
    if not rows:
        return None
    when = f" in der Saison {season}/{(season + 1) % 100:02d}" if season is not None else ""
    names = list(dict.fromkeys(r[0] for r in rows))
    summary = f"{len(rows)} gespeicherte Auszeichnungen{when} für {', '.join(names)} (Medaillen zuerst)"
    body = summary + ": " + "; ".join(r[-1] for r in rows[:40]) + "."
    direct = {
        "summary": summary,
        "rows": rows,
        "unknown": None,
        "spalten": [wording().player, "Auszeichnung", "Quelle"],
    }
    return _inputs(body, "Auszeichnungen", names, basis, direct, [a.id for a in athletes], None)


# --- two players together (line, duo) ----------------------------------------------------------------------

TOGETHER = re.compile(
    r"\b(linie|linien|line|block|sturmlinie|duo|zusammen|gemeinsam|together|chemie|chemistry|"
    r"miteinander|kombinieren|kombiniert)\b",
    re.I,
)


def pair_goal_inputs(s: Session, question: str, athletes: list):
    """ "Spielen Enzler und Stalder in der gleichen Linie?" — exactly two named persons: lines are not stored,
    so the answer says that and shows the goals both were involved in (stored official match reports)."""
    if not TOGETHER.search(question):
        return None
    if len(athletes) != 2:  # on a team page: the two persons named in the question win over the page
        from klugscheisser.answer.cross import _named

        named = _named(s, question)
        if len(named) != 2:
            return None
        athletes = [s.get(m.Athlete, i) for i in named]
    a, b = athletes
    names = {a.full_name, b.full_name}
    rows, basis, games = [], [], set()
    items = s.scalars(
        select(m.Item).where(m.Item.source_type == "official_result").order_by(m.Item.published_at.desc())
    )
    for it in items:
        raw = it.raw or {}
        for g in raw.get("goals") or []:
            people = {g.get("scorer"), *(g.get("assists") or [])}
            if names <= people:
                when = f"{it.published_at:%d.%m.%Y}" if it.published_at else "?"
                game = f"{raw.get('home', '?')} – {raw.get('away', '?')} {raw.get('score', '')}".strip()
                assists = ", ".join(g.get("assists") or []) or "–"
                cells = [
                    when,
                    game,
                    f"{g.get('time', '?')} ({g.get('score', '')})",
                    g.get("scorer") or "?",
                    assists,
                ]
                rows.append(
                    [*cells, f"{when} {game}: {g.get('time')} Tor {g.get('scorer')} (Assists: {assists})"]
                )
                basis.append({"quelle": "Spielbericht", "url": it.url})
                games.add(it.id)
    pair = f"{a.full_name} und {b.full_name}"
    if rows:
        summary = (
            f"Linien-Aufstellungen sind nicht gespeichert. In {len(games)} gespeicherten Spielbericht"
            f"{'en' if len(games) != 1 else ''} waren {pair} an {len(rows)} gemeinsamen Tor"
            f"{'en' if len(rows) != 1 else ''} beteiligt"
        )
    else:
        summary = (
            f"Linien-Aufstellungen sind nicht gespeichert, und in den gespeicherten Spielberichten gibt es kein "
            f"Tor, an dem {pair} beide beteiligt waren"
        )
    body = summary + "." + (" " + "; ".join(r[-1] for r in rows) + "." if rows else "")
    direct = {
        "summary": summary,
        "rows": rows,
        "unknown": None,
        "spalten": ["Datum", "Spiel", "Zeit (Stand)", "Tor", "Assists"],
    }
    return _inputs(body, f"Gemeinsame Tore {pair}", sorted(names), basis, direct, [a.id, b.id], None)


# --- Länderspiele (caps) ---------------------------------------------------------------------------------

CAPS = re.compile(
    r"\b(länderspiel\w*|laenderspiel\w*|caps|international (games|appearances)|nationalteam-?spiele|"
    r"einsätze für die (schweiz|nati|nationalmannschaft))\b",
    re.I,
)


def caps_inputs(s: Session, question: str, athletes: list):
    """ "Anzahl Länderspiele (Liste)" — per player the stored number (with its source) and, separately, the
    national-team games Elite Prospects lists (only the tournaments it records: incomplete, never the official
    number). Players without either are named."""
    if not CAPS.search(question) or not athletes:
        return None
    from klugscheisser.answer.agent import _national_games
    from klugscheisser.facts.queries import facts_for

    players = [a for a in athletes if a.role == "athlete"][:60]
    ids = [a.id for a in players]
    stored = {r.athlete_id: r for r in facts_for(s, ids, "appearances")}
    ep = _national_games(s, ids)
    rows, basis, missing = [], [], []
    for a in sorted(players, key=lambda x: -(ep.get(x.id) or 0)):
        f = stored.get(a.id)
        if f is None and a.id not in ep:
            missing.append(a.full_name)
            continue
        official = f"{f.value_text} ({cite_label(f.source, f.source_url, f.site)})" if f else "–"
        ep_text = f"{ep[a.id]} (unvollständig)" if a.id in ep else "–"
        rows.append(
            [
                a.full_name,
                official,
                ep_text,
                f"{a.full_name}: gespeichert {official}; Elite Prospects {ep_text}",
            ]
        )
        if f is not None and f.source_url:
            basis.append({"quelle": f.source, "url": f.source_url})
    if not rows:
        return None
    eps = s.scalars(select(m.EpProfile.url).where(m.EpProfile.athlete_id.in_(list(ep)))).all()
    basis += [{"quelle": "Elite Prospects", "url": u} for u in eps if u]
    summary = (
        f"Länderspiele von {wording().plural(len(players))}: eine gespeicherte Zahl "
        f"(Wikipedia/Artikel) gibt es für {len(stored)}; Elite Prospects zählt nur die dort erfassten Turniere und "
        f"Saisons – unvollständig, keine offizielle Länderspielzahl"
    )
    unknown = f"Keine Angabe für: {', '.join(missing)}" if missing else None
    body = summary + ". " + "; ".join(r[-1] for r in rows) + "." + (f" {unknown}." if unknown else "")
    direct = {
        "summary": summary,
        "rows": rows,
        "unknown": unknown,
        "spalten": [
            wording().player,
            "Länderspiele (gespeichert, Quelle)",
            "Nationalteam-Spiele laut Elite Prospects",
        ],
    }
    return _inputs(body, "Länderspiele", [r[0] for r in rows], basis, direct, ids, None)


def tournament_inputs(s: Session, question: str, athletes: list):
    """ "Wie funktioniert der WM-Modus?" / "Gegen wen spielt die Schweiz an der WM?" — the stored tournament sheet
    (klugscheisser/tournament.py), only when no person is asked about."""
    from klugscheisser import tournament

    if athletes and len(athletes) <= 5:
        return None
    if NEXT.search(question) or LAST.search(question):
        return None  # "Wann ist die nächste WM?": cross.tournament_inputs (date, place, roster) answers it
    ev = tournament.event_for_question(s, question)
    if ev is None:
        return None
    res = tournament.answer(s, ev, question)
    rows = tournament.answer_rows(res)
    if not rows:
        return None
    summary = f"{res['turnier']}: " + "; ".join(sec["titel"] for sec in res["abschnitte"])
    basis = [
        {"quelle": z["quelle"], "url": z["url"]}
        for sec in res["abschnitte"]
        for z in sec["zeilen"]
        if z["url"]
    ]
    body = summary + ". " + " ".join(r[-1] for r in rows)
    direct = {"summary": summary, "rows": rows, "unknown": None, "spalten": ["Angabe", "Quelle"]}
    return _inputs(body, res["turnier"], [], basis, direct, [], res["turnier"])


# --- recent games / head-to-head of teams (all stored games) ------------------------------------------------


def team_results_inputs(s: Session, question: str, athletes: list):
    """ "Form von Tschechien", "letzte 3 Spiele der USA", "Wann spielten die Schweiz und Österreich zuletzt
    gegeneinander?" — the stored games of the named team(s), all competitions, with result, record and link."""
    from klugscheisser.answer import team_games as tgm

    if athletes:
        return None
    found = tgm.for_question(s, question)
    if not found or not any(tg.games for tg, _ in found):
        return None
    table, basis, summaries = [], [], []
    for tg, ids in found:
        label = f"{tg.team.name} – {tg.opponent.name}" if tg.opponent is not None else tg.team.name
        rec = tgm.record(tg.games, ids)
        summaries.append(f"{label}: {rec}" if rec else f"{label}: {len(tg.games)} Spiele")
        for g in tg.games:
            r = tgm.game_row(g, ids)
            score = " ".join(
                x for x in (r.get("resultat"), f"({r['ausgang']})" if r.get("ausgang") else None) if x
            )
            comp = r.get("wettbewerb") or "–"
            table.append(
                [r["datum"], r["name"], score or "–", comp, f"{r['datum']}: {r['name']} {score} ({comp})"]
            )
            basis.append({"quelle": g.source or "Resultate", "url": g.source_url or ""})
    what = "Direktvergleich" if found[0][0].opponent is not None else "Letzte Spiele"
    summary = f"{what} (gespeicherte Resultate, alle Wettbewerbe): " + "; ".join(summaries)
    body = (
        f"{summary}. "
        + "; ".join(r[-1] for r in table)
        + ". "
        + " ".join(tgm.coverage_note(tg) for tg, _ in found)
    )
    direct = {
        "summary": summary,
        "rows": table,
        "unknown": None,
        "spalten": ["Datum", "Spiel", "Resultat", "Wettbewerb"],
    }
    return _inputs(body, what, [], basis, direct, [], summary)


def direct_lookup(s: Session, question: str, athletes: list):
    """The first of the lookups that understands the question (or None)."""
    return (
        tournament_games_inputs(s, question, athletes)  # a tournament without a fact sheet, named by its city
        or team_results_inputs(s, question, athletes)  # recent games / form / head-to-head of teams
        or tournament_inputs(s, question, athletes)
        or caps_inputs(s, question, athletes)
        or pair_goal_inputs(s, question, athletes)
        or coach_inputs(s, question)
        or team_game_inputs(s, question)
        or form_inputs(s, question, athletes)
        or award_inputs(s, question, athletes)
    )
