"""Key players from the Swiss angle (switch TEAM_KEY_PLAYERS, 2026-10-28): a compact block in the MCP `team` tool.

Why: "Vorschau Schweiz gegen Tschechien: Wer sind die Schlüsselspielerinnen?" took Perplexity 3 rounds (team, then
3x person, then person) and it picked players by general stats; it missed what a Swiss broadcast needs (Mlýnková's
hat-trick in the 7:0 against Switzerland, players with Swiss clubs). For a national team other than Switzerland the
`team` answer now carries, for the players of the roster it shows (model-free, every line with date + link):

1. "Gegen die Schweiz": goals and assists in the stored games against Switzerland (official game sheets,
   `items.raw.goals`; shootout goals left out, as in stats/current.py), each game with date, match (home team
   first, as stored), score, competition and link; goalies with their games (saves when the sheet has them).
2. "Schweiz-Bezug": Swiss clubs in the career (facts/german.club_years: seasons, games, now there), with the
   roster's Deutsch label (bestätigt / wahrscheinlich).
3. "Form": last full club season top scorers, current national-team and club season (Stand date), goalies.
4. "Auszeichnungen": awards of the last and current season (All-Star, player of the year … first).
Vorsicht / resolved-injury notes of the roster line follow the name wherever she appears.

Switzerland itself, clubs and other sports get no block (the tool has no opponent argument; for Switzerland the
"Swiss angle" is the team itself, and its players' records against one opponent are a question for suche/events).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

TZ = ZoneInfo("Europe/Zurich")

# 2026-11-24: men's projects (floorball) get the men's words; the women's forms stay the default
_MEN = {
    "Torhüterinnen": "Torhüter",
    "Torhüterin": "Torhüter",
    "Torschützinnen": "Torschützen",
    "Skorerinnen": "Skorer",
    "Schlüsselspielerinnen": "Schlüsselspieler",
}


def _men() -> bool:
    from klugscheisser.sports import current_spec

    return current_spec().men


def _w(word: str) -> str:
    return _MEN.get(word, word) if _men() else word


MAX_GAMES = 8  # newest stored games against Switzerland
MAX_SKATERS = 8
MAX_GOALIES = 3
TOP_SCORERS = 5
TOP_CURRENT = 3
MAX_AWARD_PLAYERS = 6
AWARDS_EACH = 2
SHOOTOUT = re.compile(r"Penalty-Schiessen|\bGWS\b|shootout", re.I)
_SCORE = re.compile(r"(\d+)\s*:\s*(\d+)")
# individual honours before team titles
AWARD_RANK = (
    re.compile(
        r"all-?star|player of the year|spieler(?:in)? des jahres|mvp|most valuable|best |beste|"
        r"of the year|kazmaier|zlatá|top |most |leading|directorate|all-?american|first team",
        re.I,
    ),
    re.compile(r"rookie|team", re.I),
)


def applies(team: m.Team) -> bool:
    return team.kind == "national" and bool(team.country) and team.country != "SUI"


# --- helpers -------------------------------------------------------------------------------------------------


def _day(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y") if dt else "?"


def _link(label: str | None, url: str | None) -> str:
    return f"[{(label or 'Quelle').strip('[] ')}]({url})" if url else ""


def comp_label(g: m.Event) -> str:
    """'WM 2025', 'Olympia 2026', 'EHT Lahti', else the competition name."""
    name = g.competition.name if g.competition is not None else ""
    if (hit := re.search(r"\bWM (\d{4})", name)) and "Division" not in name:
        return f"WM {hit.group(1)}"
    if re.search(r"olympi", name, re.I) and (hit := re.search(r"(\d{4})", name)):
        return f"Olympia {hit.group(1)}"
    if re.search(r"euro hockey tour", name, re.I):
        city = (g.city or "").split(" (")[0].strip() or (
            name.rsplit(",", 1)[-1].strip() if "," in name else ""
        )
        return f"EHT {city}".strip()
    if re.search(r"länderspiel", name, re.I):
        return "Länderspiel"
    return name


def game_text(g: m.Event) -> str:
    label = comp_label(g)
    return f"{_day(g.starts_at)} {g.name} {g.score}" + (f" ({label})" if label else "")


def _n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


class NameMatch:
    """Sheet names ('Natalie Mlynkova', 'Tereza Vanisova') -> roster persons: full names and aliases (maiden names,
    spellings), normalised; else a unique surname with the same first initial. Never guesses between two."""

    def __init__(self, s: Session, athletes: list[m.Athlete]):
        from klugscheisser.domain.names import name_variants, normalize

        self._norm = normalize
        self.full: dict[str, set[int]] = defaultdict(set)
        self.last: dict[str, set[tuple[int, str]]] = defaultdict(set)
        self.surname = {a.id: normalize(a.full_name).split()[-1] for a in athletes if a.full_name.strip()}
        self.spelling: dict[str, str] = {}  # normalised variant -> the alias as stored (with accents)
        self.other_names: dict[int, set[str]] = defaultdict(
            set
        )  # matched under another surname (married name)
        names = [(a.id, a.full_name) for a in athletes]
        ids = [a.id for a in athletes]
        if ids:
            names += [
                (al.athlete_id, al.alias)
                for al in s.scalars(select(m.AthleteAlias).where(m.AthleteAlias.athlete_id.in_(ids)))
            ]
        for aid, name in names:
            for v in name_variants(name):
                self.full[v].add(aid)
                self.spelling.setdefault(v, name)
                parts = v.split()
                if len(parts) >= 2:
                    self.last[parts[-1]].add((aid, parts[0][:1]))

    def __call__(self, name: str | None) -> int | None:
        n = self._norm(name or "")
        if not n:
            return None
        hit = self.full.get(n) or set()
        parts = n.split()
        if not hit and len(parts) >= 3:
            hit = self.full.get(f"{parts[0]} {parts[-1]}") or set()
        if len(hit) == 1:
            aid = next(iter(hit))
            if parts[-1] != self.surname.get(aid) and len(parts) >= 2:
                self.other_names[aid].add(self.spelling.get(n, name or "").split()[-1])
            return aid
        if hit or len(parts) < 2:
            return None
        cands = {aid for aid, ini in self.last.get(parts[-1], set()) if ini == parts[0][:1]}
        return next(iter(cands)) if len(cands) == 1 else None


# --- 1. against Switzerland -------------------------------------------------------------------------------------


def swiss_games(s: Session, team: m.Team) -> tuple[list[m.Event], set[int]]:
    sui = set(s.scalars(select(m.Team.id).where(m.Team.kind == "national", m.Team.country == "SUI")))
    if not sui:
        return [], sui
    own = {team.id}
    cond = or_(
        m.Event.home_team_id.in_(own) & m.Event.away_team_id.in_(sui),
        m.Event.home_team_id.in_(sui) & m.Event.away_team_id.in_(own),
    )
    games = list(
        s.scalars(
            select(m.Event)
            .where(m.Event.kind == "game", m.Event.status == "final", m.Event.score.is_not(None), cond)
            .order_by(m.Event.starts_at.desc().nulls_last(), m.Event.id.desc())
            .limit(MAX_GAMES)
        )
    )
    return games, sui


def against_switzerland(
    s: Session, team: m.Team, match: NameMatch, flag, name_of: dict[int, str], goalie_ids: set[int]
) -> list[str]:
    from klugscheisser.answer.games import result_item
    from klugscheisser.answer.team_games import record

    games, _sui = swiss_games(s, team)
    out = ["**Gegen die Schweiz**"]
    if not games:
        out.append(
            f"- Keine Spiele {team.name} – Schweiz in Karls Daten (gespeichert sind Spiele ab 2025). "
            "Fehlen ist kein Gegenbeweis."
        )
        return out
    rec = record(games, {team.id})
    out.append(
        f"_{_n(len(games), 'gespeichertes Spiel', 'gespeicherte Spiele')} {_day(games[-1].starts_at)}–"
        f"{_day(games[0].starts_at)}"
        + (f"; Bilanz {team.name}: {rec}" if rec else "")
        + ". Resultat Heimteam zuerst; Tore ohne Penalty-Schiessen._"
    )
    pts: dict[int, dict[int, list[int]]] = defaultdict(dict)  # athlete -> game id -> [goals, assists]
    goalie: dict[int, list[tuple[m.Event, str | None, bool]]] = defaultdict(list)
    without_sheet: list[m.Event] = []
    for g in games:
        item = result_item(s, g)
        raw = (item.raw or {}) if item is not None else {}
        goals = raw.get("goals")
        scored = sum(int(x) for x in (_SCORE.search(g.score or "") or re.match("()()", "")).groups() if x)
        if goals is None or (not goals and scored):  # no sheet, or a sheet without scorers (SIHF friendlies)
            without_sheet.append(g)
            continue
        for goal in goals:
            if SHOOTOUT.search(str(goal.get("time") or "")):
                continue
            if (aid := match(goal.get("scorer"))) is not None:
                pts[aid].setdefault(g.id, [0, 0])[0] += 1
            for helper in goal.get("assists") or []:
                if (aid := match(helper)) is not None:
                    pts[aid].setdefault(g.id, [0, 0])[1] += 1
        hit = _SCORE.search(g.score or "")
        against = None
        if hit:
            home, away = int(hit.group(1)), int(hit.group(2))
            against = away if g.home_team_id == team.id else home
        keepers = [p for p in raw.get("participants") or [] if p.get("role") == "goalie"]
        ours = [(p, aid) for p in keepers if (aid := match(p.get("name"))) is not None]
        for p, aid in ours:
            shutout = against == 0 and len(ours) == 1
            goalie[aid].append((g, p.get("detail"), shutout))
    out += [f"- {game_text(g)} {_link(g.source, g.source_url)}".rstrip() for g in games]
    by_id = {g.id: g for g in games}
    rows = []
    for aid, per in pts.items():
        if aid in goalie_ids:
            continue
        ng, na = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
        rows.append((ng + na, ng, aid, per))
    rows.sort(key=lambda r: (-r[0], -r[1], name_of.get(r[2], "")))
    if rows or goalie:
        out.append(
            f"_Punkte (T = Tore, A = Assists) und {_w('Torhüterinnen')} des Kaders in diesen Spielen (Datum = Spiel oben):_"
        )
    for total, ng, aid, per in rows[:MAX_SKATERS]:
        bits = []
        for gid, (gg, aa) in sorted(per.items(), key=lambda kv: by_id[kv[0]].starts_at, reverse=True):
            what = " ".join(x for x in (f"{gg} T" if gg else "", f"{aa} A" if aa else "") if x)
            bits.append(f"{_day(by_id[gid].starts_at)}: {what}" + (" (Hattrick)" if gg >= 3 else ""))
        other = sorted(match.other_names.get(aid, ()))
        aka = f" (auf Spielblättern auch {', '.join(other)})" if other else ""
        out.append(f"- {name_of[aid]}{aka}{flag(aid)}: {ng} T, {total - ng} A – " + "; ".join(bits))
    if len(rows) > MAX_SKATERS:
        more = rows[MAX_SKATERS:]
        rest = ", ".join(f"{name_of[r[2]]} {r[0]} P." for r in more[:5])
        out.append(f"- … weitere mit Punkten: {rest}" + (f" (+{len(more) - 5})" if len(more) > 5 else ""))
    for aid, apps in sorted(goalie.items(), key=lambda kv: -len(kv[1]))[:MAX_GOALIES]:
        bits = []
        for g, detail, shutout in sorted(apps, key=lambda x: x[0].starts_at, reverse=True):
            extra = [x for x in (detail, "ohne Gegentor" if shutout else None) if x]
            bits.append(_day(g.starts_at) + (f" ({', '.join(extra)})" if extra else ""))
        out.append(f"- {_w('Torhüterin')} {name_of[aid]}{flag(aid)} im Tor: " + "; ".join(bits))
    if not rows and not goalie and len(without_sheet) < len(games):
        out.append(
            "- Kein Torschütze, Assistgeber oder Torhüter des aktuellen Kaders auf diesen Spielblättern."
            if _men()
            else "- Keine Torschützin, Assistgeberin oder Torhüterin des aktuellen Kaders auf diesen Spielblättern."
        )
    if without_sheet:
        out.append(
            f"- Ohne {_w('Torschützinnen')} in Karls Daten (Spielblatt fehlt oder ohne Tore): "
            + "; ".join(_day(g.starts_at) for g in without_sheet)
        )
    return out


# --- 2. Swiss clubs ------------------------------------------------------------------------------------------------


def swiss_clubs(
    s: Session, athletes: list[m.Athlete], flag, deutsch: dict[int, str], staff_role: dict[int, str]
) -> list[str]:
    from klugscheisser.facts import german

    years = german.club_years(s, [a.id for a in athletes])
    rows = []
    for a in athletes:
        clubs = [c for c in years.get(a.id, []) if c.place.country == "SUI"]
        if not clubs:
            continue
        newest = max((max(c.seasons) for c in clubs if c.seasons), default=0)
        rows.append((not any(c.current for c in clubs), -newest, a, clubs))
    out = ["**Schweiz-Bezug** (Schweizer Klubs in der Laufbahn)"]
    if not rows:
        out.append("- Keine Schweizer Klubstation in Karls Laufbahndaten.")
        return out
    for _cur, _new, a, clubs in sorted(rows, key=lambda r: (r[0], r[1], r[2].full_name)):
        label = (deutsch.get(a.id) or "").split(" – ")[0]
        de = f" · Deutsch: {label}" if label in ("bestätigt", "wahrscheinlich") else ""
        src = next(((k, v) for c in clubs for k, v in c.sources.items() if v), (None, None))
        role = f" ({staff_role[a.id]})" if a.id in staff_role else ""
        out.append(
            f"- {a.full_name}{role}{flag(a.id)}: "
            + "; ".join(c.long() for c in clubs)
            + de
            + (f" {_link(*src)}" if src[1] else "")
        )
    return out


# --- 3. form -----------------------------------------------------------------------------------------------------


def _stat_line(r: m.SeasonStat, current: bool) -> str:
    from klugscheisser.stats.queries import SOURCE_LABEL

    vals = [
        f"{v} {lbl}"
        for v, lbl in ((r.games, "Sp."), (r.goals, "T"), (r.assists, "A"), (r.points, "P"))
        if v is not None
    ]
    if r.save_pct is not None:
        vals.append(f"Fangquote {r.save_pct:.3f}".replace("0.", "."))
    if r.gaa is not None:
        vals.append(f"GAA {r.gaa:.2f}")
    stand = f", Stand {_day(r.fetched_at)}" if current and r.fetched_at else ""
    team = r.team_name or ""
    return (
        f"{team} ({r.league}) {', '.join(vals)}{stand} "
        f"{_link(SOURCE_LABEL.get(r.source, r.source), r.source_url)}"
    ).rstrip()


def form(s: Session, athletes: list[m.Athlete], flag, goalie_ids: set[int]) -> list[str]:
    from klugscheisser.facts.german import season_start
    from klugscheisser.stats.queries import is_summary, prefer_current, season_label

    now = season_start()
    ids = [a.id for a in athletes]
    name = {a.id: a.full_name for a in athletes}
    rows = list(
        s.scalars(
            select(m.SeasonStat).where(
                m.SeasonStat.athlete_id.in_(ids),
                m.SeasonStat.phase == "regular",
                m.SeasonStat.season >= now - 1,
            )
        )
    )
    rows = [r for r in prefer_current(rows) if not is_summary(r)]

    def best(pred) -> dict[int, m.SeasonStat]:
        out: dict[int, m.SeasonStat] = {}
        for r in rows:
            if pred(r) and (r.athlete_id not in out or (r.points or 0) > (out[r.athlete_id].points or 0)):
                out[r.athlete_id] = r
        return out

    club = ("club", "college")
    out = [f"**Form und {_w('Skorerinnen')}** (Saisonzahlen, Hauptrunde)"]
    last = best(
        lambda r: (
            r.season == now - 1
            and r.team_kind in club
            and r.points is not None
            and r.athlete_id not in goalie_ids
        )
    )
    top = sorted(last.values(), key=lambda r: -(r.points or 0))[:TOP_SCORERS]
    if top:
        out.append(f"_Saison {season_label(now - 1)}, Klub/College, Top {len(top)} nach Punkten:_")
        out += [f"- {name[r.athlete_id]}{flag(r.athlete_id)}: {_stat_line(r, False)}" for r in top]
    nat = best(lambda r: r.season == now and r.team_kind == "national" and (r.points or 0) > 0)
    top = sorted(nat.values(), key=lambda r: (-(r.points or 0), -(r.goals or 0), name[r.athlete_id]))[
        :TOP_CURRENT
    ]
    if top:  # one line: the rows come from the same counted sheets (one link)
        from klugscheisser.stats.queries import SOURCE_LABEL

        r0 = top[0]
        out.append(
            f"- Nationalteam {season_label(now)} (läuft, {r0.league}, Stand {_day(r0.fetched_at)}), Top {len(top)}: "
            + "; ".join(
                f"{name[r.athlete_id]}{flag(r.athlete_id)} "
                + (f"{r.games} Sp., " if r.games is not None else "")  # IIHF sheets: no games played
                + f"{r.goals} T, {r.assists} A"
                for r in top
            )
            + f" {_link(SOURCE_LABEL.get(r0.source, r0.source), r0.source_url)}"
        )
    cur = best(
        lambda r: (
            r.season == now and r.team_kind in club and (r.points or 0) > 0 and r.athlete_id not in goalie_ids
        )
    )
    top = sorted(cur.values(), key=lambda r: (-(r.points or 0), -(r.goals or 0)))[:TOP_CURRENT]
    if top:
        out.append(f"_Saison {season_label(now)} (läuft), Klub/College, Top {len(top)}:_")
        out += [f"- {name[r.athlete_id]}{flag(r.athlete_id)}: {_stat_line(r, True)}" for r in top]
    keepers = best(
        lambda r: r.athlete_id in goalie_ids and r.season == now - 1 and r.team_kind in club and r.games
    )
    gl = sorted(keepers.values(), key=lambda r: -(r.games or 0))[:MAX_GOALIES]
    if gl:
        out.append(f"_{_w('Torhüterinnen')}, Saison {season_label(now - 1)}, Klub/College:_")
        out += [f"- {name[r.athlete_id]}{flag(r.athlete_id)}: {_stat_line(r, False)}" for r in gl]
    return out if len(out) > 1 else []


# --- 4. awards ----------------------------------------------------------------------------------------------------


def awards(s: Session, athletes: list[m.Athlete], flag) -> list[str]:
    from klugscheisser.facts.german import season_start
    from klugscheisser.stats.queries import SOURCE_LABEL

    now = season_start()
    ids = [a.id for a in athletes]
    name = {a.id: a.full_name for a in athletes}
    facts = s.scalars(
        select(m.Fact).where(
            m.Fact.athlete_id.in_(ids),
            m.Fact.kind == "award",
            m.Fact.season >= now - 1,
            m.Fact.status != "conflict",
        )
    )
    per: dict[int, dict[str, m.Fact]] = defaultdict(dict)
    for f in facts:
        per[f.athlete_id].setdefault(f.value_text.strip(), f)

    def rank(text: str) -> int:
        return next((i for i, rx in enumerate(AWARD_RANK) if rx.search(text)), len(AWARD_RANK))

    scored = []
    for aid, fs in per.items():
        items = sorted(fs.values(), key=lambda f: (rank(f.value_text), -(f.season or 0), f.value_text))
        scored.append((rank(items[0].value_text), -len(items), name[aid], aid, items[:AWARDS_EACH]))
    if not scored:
        return []
    out = ["**Auszeichnungen** (letzte und laufende Saison)"]
    for _r, _n2, nm, aid, items in sorted(scored)[:MAX_AWARD_PLAYERS]:
        src = items[0]
        out.append(
            f"- {nm}{flag(aid)}: "
            + "; ".join(f.value_text for f in items)
            + f" {_link(SOURCE_LABEL.get(src.source, src.source), src.source_url)}".rstrip()
        )
    return out


# --- block ---------------------------------------------------------------------------------------------------------


def block(
    s: Session,
    team: m.Team,
    athletes: list[m.Athlete],
    players: dict[int, dict[str, Any]],
    staff_athletes: list[tuple[m.Athlete, str]] | None = None,
) -> list[str]:
    """The markdown lines of the block for `team`'s roster (`players`: get_team_roster's per-player dicts with
    position, Vorsicht reason, status note and Deutsch cell). Empty when the team gets no block."""
    if not applies(team) or not athletes:
        return []

    def flag(aid: int) -> str:
        p = players.get(aid) or {}
        bits = []
        if p.get("vorsicht"):
            bits.append(f"Vorsicht: {p['vorsicht_grund']}" if p.get("vorsicht_grund") else "Vorsicht")
        elif p.get("vorsicht_grund"):
            bits.append(f"Vorsicht: {p['vorsicht_grund']}")
        if p.get("status_hinweis"):
            bits.append(p["status_hinweis"])
        return f" ({'; '.join(bits)})" if bits else ""

    goalie_ids = {
        a.id for a in athletes if (players.get(a.id) or {}).get("position") in ("Torhüterin", "Torhüter", "G")
    }
    name_of = {a.id: a.full_name for a in athletes}
    match = NameMatch(s, athletes)
    deutsch = {aid: p.get("deutsch") or "" for aid, p in players.items()}
    staff_role = {a.id: role for a, role in staff_athletes or []}
    out = [
        f"**{_w('Schlüsselspielerinnen')} aus Schweizer Sicht – {team.name}**",
        "_Aus Karls Spielblättern, Laufbahnen, Saisonstatistiken und Auszeichnungen berechnet (kein Modell); nur "
        "Personen des Kaders unten. Mehr zu einer Person: person(name)._",
    ]
    out += against_switzerland(s, team, match, flag, name_of, goalie_ids)
    out += swiss_clubs(s, [*athletes, *(a for a, _r in staff_athletes or [])], flag, deutsch, staff_role)
    out += form(s, athletes, flag, goalie_ids)
    out += awards(s, athletes, flag)
    return out


__all__ = ["NameMatch", "applies", "block", "comp_label", "swiss_games"]
