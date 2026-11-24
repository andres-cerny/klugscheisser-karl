"""Connections between the players of two teams (2026-10-24): who plays in the same club now, who played together
before ("Welche Spielerinnen aus Finnland und der Schweiz kennen sich aus dem Klub?").

Perplexity needed ~20 calls (one "Bei welchem Klub spielt X?" per player) for this on 24.10.; this is one direct
lookup without a model. The two teams come from the question / the tool's `team` / a game event. The players are
the game's lineup (stored with the official result, e.g. Leijonat) plus the teams' rosters at the game date; no
game named → the latest played game between the two teams (said in the answer). Without any stored game: the
current rosters.

- **aktuell zusammen**: both players' current club is the same (`current_clubs`: roster affiliation of this season
  → Elite Prospects season row of this season → newest open career station).
- **früher zusammen**: overlapping seasons at the same club/college (stored career stations + Elite Prospects
  season rows, `cross.relation_pairs`; season-level overlap).
Every line carries its source links; who was in the game's lineup is marked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.llm.words import fill
from klugscheisser.projects import wording

CONNECT_Q = re.compile(
    r"kennen sich|kennen einander|know each other|knows? \w+ from|verbindung\w*|connections?|connected|"
    r"verbunden|klubkolleg\w*|clubkolleg\w*|vereinskolleg\w*|teamkolleg\w*|mitspieler\w*|teammates?\b|"
    r"zusammen gespielt|zusammengespielt|spiel\w* zusammen|zusammen spiel\w*|gemeinsam\w* gespielt|"
    r"play(ed|s)? together|played (with|alongside)|"
    r"(gleiche[nmr]?|selbe[nmr]?|demselben|denselben|derselben|im gleichen|dem gleichen) "
    r"(verein|team|club|klub|mannschaft)|same (club|team)|"
    r"shared? a (club|team)|share a (club|team)",
    re.I,
)
MAX_PAIRS = 40
LINEUP_MARK = "🏒"


@dataclass
class Club:
    name: str
    key: str  # facts.teams.team_key
    quelle: str
    url: str | None
    since: int | None = None  # start year of the season


@dataclass
class Player:
    id: int
    name: str
    side: int  # 0 / 1
    in_lineup: bool = False
    clubs: list[Club] = field(default_factory=list)


def season_start(today: date | None = None) -> int:
    """Start year of the current season (August–July): 24.10.2026 → 2026 (= 2026/27)."""
    from zoneinfo import ZoneInfo

    today = today or datetime.now(ZoneInfo("Europe/Zurich")).date()
    return today.year if today.month >= 8 else today.year - 1


def _season_label(y: int) -> str:
    return f"{y}/{(y + 1) % 100:02d}"


def _source_name(source: str | None, url: str | None) -> str:
    src = (source or "").strip()
    low = src.lower()
    if low == "eliteprospects":
        return "Elite Prospects"
    if low == "wikidata" or "wikidata.org" in (url or ""):
        return "Wikidata"
    if "wikipedia.org" in (url or ""):
        return "Wikipedia"
    if low in ("web", "news", "") and url:
        host = re.sub(r"^https?://(www\.)?", "", url).split("/", 1)[0]
        return host or "Quelle"
    return src[:40] or "Quelle"


def _roster_seed_clubs(s: Session, ids: list[int], tag: str) -> dict[int, Club]:
    """The club as written on this season's federation roster (seeds/rosters/*.yaml `club_text`, e.g. Leijonat:
    "Kerttu Kuja-Halkola, HIFK"), for persons on such a roster."""
    from klugscheisser import tournament
    from klugscheisser.domain.names import normalize
    from klugscheisser.facts.teams import team_key

    if not ids:
        return {}
    rows = s.execute(
        select(m.EventEntry.athlete_id, m.Event.key, m.Team.key, m.Athlete.full_name)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .join(m.Team, m.Team.id == m.EventEntry.team_id)
        .join(m.Athlete, m.Athlete.id == m.EventEntry.athlete_id)
        .where(m.EventEntry.athlete_id.in_(ids))
    ).all()
    out: dict[int, Club] = {}
    for aid, ev_key, t_key, name in rows:
        if aid in out:
            continue
        seed = tournament._roster_seed(ev_key, t_key)
        if not seed or str(seed.get("season") or "") != tag:
            continue
        hit = next(
            (p for p in seed.get("players") or [] if normalize(p.get("name") or "") == normalize(name)), None
        )
        club = (hit or {}).get("club_text")
        if club:
            url = seed.get("source_url")
            out[aid] = Club(club, team_key(club), _source_name("web", url) if url else "Kader", url)
    return out


def current_clubs(s: Session, ids: list[int], season: int | None = None) -> dict[int, list[Club]]:
    """Current club/college per person, best evidence first, one entry per club:
    1. roster affiliation of this season (SIHF / federation roster pages), then the club written on this
       season's roster seed (club_text),
    2. Elite Prospects (or league) season row of this season (club/college),
    3. the newest open career station (club/college) that started at most this season.
    A person without any of these is missing from the result."""
    from klugscheisser.facts.queries import stints_for
    from klugscheisser.facts.teams import is_national_name, team_key

    if not ids:
        return {}
    y = season or season_start()
    tag = f"{y}-{(y + 1) % 100:02d}"
    out: dict[int, list[Club]] = {}

    def add(aid: int, club: Club) -> None:
        have = out.setdefault(aid, [])
        if not any(c.key == club.key or club.key in c.key or c.key in club.key for c in have):
            have.append(club)

    rows = s.execute(
        select(m.Affiliation, m.Team)
        .join(m.Team, m.Team.id == m.Affiliation.team_id)
        .where(m.Affiliation.athlete_id.in_(ids), m.Affiliation.season == tag, m.Team.kind != "national")
    ).all()
    for af, team in rows:
        if is_national_name(team.name):
            continue
        add(af.athlete_id, Club(team.name, team_key(team.name), af.source or "Kader", af.source_url, y))
    for aid, club in _roster_seed_clubs(s, [i for i in ids if i not in out], tag).items():
        add(aid, club)
    done = set(out)
    stats = s.scalars(
        select(m.SeasonStat).where(
            m.SeasonStat.athlete_id.in_([i for i in ids if i not in done]),
            m.SeasonStat.season == y,
            m.SeasonStat.team_kind.in_(("club", "college")),
            m.SeasonStat.team_name.is_not(None),
        )
    )
    for r in stats:
        if r.team_name.endswith("(all)"):
            continue
        add(
            r.athlete_id,
            Club(r.team_name, team_key(r.team_name), _source_name(r.source, r.source_url), r.source_url, y),
        )
    done = set(out)
    rest = [i for i in ids if i not in done]
    best: dict[int, list] = {}
    for st in stints_for(s, rest):
        if st.kind not in ("club", "college") or st.season_from is None or st.season_from > y:
            continue
        if st.season_to is not None and st.season_to < y:
            continue
        cur = best.get(st.athlete_id)
        if cur is None or st.season_from > cur[0].season_from:
            best[st.athlete_id] = [st]
        elif st.season_from == cur[0].season_from:
            cur.append(st)
    for aid, sts in best.items():
        sts.sort(key=lambda x: x.source != "eliteprospects")
        for st in sts:
            add(
                aid,
                Club(
                    st.team_name,
                    team_key(st.team_name),
                    _source_name(st.source, st.source_url),
                    st.source_url,
                    st.season_from,
                ),
            )
    return out


# --- which teams, which game ----------------------------------------------------------------------------------


@dataclass
class Setup:
    sides: list  # games.Side × 2
    game: m.Event | None = None
    game_note: str = ""
    groups: list[list[int]] = field(default_factory=list)
    lineup: set[int] = field(default_factory=set)
    from_lineup: bool = False


def _two_sides(s: Session, question: str, team: m.Team | None, event: m.Event | None) -> list:
    from klugscheisser.answer.games import Side
    from klugscheisser.answer.lookups import sides

    if event is not None and event.kind == "game" and event.home_team_id and event.away_team_id:
        return [Side(s.get(m.Team, t), {t}) for t in (event.home_team_id, event.away_team_id)]
    found = list(sides(s, question))
    if team is not None and not any(team.id in x.ids for x in found):
        found.insert(0, Side(team, {team.id}))
    return found[:2] if len(found) >= 2 else []


def _pick_game(s: Session, question: str, sides: list, event: m.Event | None) -> tuple[m.Event | None, str]:
    """The game asked about, else the latest played meeting of the two teams (said so)."""
    from klugscheisser.answer import games

    if event is not None and event.kind == "game":
        return event, "genanntes Spiel"
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Zurich")).date()
    tournament = next((t for t in games.tournaments() if t[0].search(question)), None)
    day = games._day_in(question, today)
    if tournament is not None or day is not None:  # "an Olympia 2026", "am 2.10.": that game
        ref = games.GameRef(sides=sides, tournament=tournament, day=day)
        found = [g for g in games.find_games(s, ref) if g.status in ("final", "live")]
        if found:
            return found[0], "genanntes Spiel"
    found = games.find_games(s, games.GameRef(sides=sides, latest=True))
    found = [g for g in found if g.status in ("final", "live")]
    if found:
        named = tournament is not None or day is not None
        return found[0], (
            "genanntes Spiel nicht gespeichert: neuestes gespieltes Duell der beiden Teams"
            if named
            else "kein Spiel genannt: neuestes gespieltes Duell der beiden Teams"
        )
    return None, "kein gespieltes Duell gespeichert: aktuelle Kader"


def setup(
    s: Session, question: str, team: m.Team | None = None, event: m.Event | None = None
) -> Setup | None:
    from klugscheisser.answer import games

    sides = _two_sides(s, question, team, event)
    if len(sides) != 2:
        return None
    game, note = _pick_game(s, question, sides, event)
    st = Setup(sides=sides, game=game, game_note=note)
    if game is not None:
        item = games.result_item(s, game)
        ev_sides = games._event_sides(s, game, games.GameRef(sides=sides))
        # the question's side order
        if ev_sides and ev_sides[0].team.id not in sides[0].ids and len(ev_sides) == 2:
            ev_sides = ev_sides[::-1]
        groups, from_lineup = games.lineup_groups(s, game, item, ev_sides)
        st.from_lineup = from_lineup
        if from_lineup:
            st.lineup = {i for g in groups for i in g}
        rosters = [games.roster_ids(s, side, game.starts_at) for side in ev_sides]
        st.groups = [
            list(dict.fromkeys((groups[i] if i < len(groups) else []) + rosters[i])) for i in range(2)
        ]
    else:
        st.groups = [games.roster_ids(s, side, datetime.now(UTC)) for side in sides]
    # one person on both sides (data error) counts for neither comparison twice
    both = set(st.groups[0]) & set(st.groups[1])
    st.groups = [[i for i in g if i not in both] for g in st.groups]
    return st if all(st.groups) else None


# --- the lookup -------------------------------------------------------------------------------------------------


def wants(question: str) -> bool:
    return bool(CONNECT_Q.search(question))


def _link(name: str, url: str | None) -> str:
    return f"[{name}]({url})" if url else name


def lookup(s: Session, st: Setup) -> dict:
    """{current: [(club name, [Player…], links)], former: [(Player a, Player b, teams text, links)], unknown: [...]}"""
    from klugscheisser.answer.cross import relation_pairs
    from klugscheisser.facts.teams import team_key

    a_ids, b_ids = st.groups
    ids = a_ids + b_ids
    names = dict(s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all())
    players = {
        i: Player(i, names.get(i, "?"), 0 if i in set(a_ids) else 1, in_lineup=i in st.lineup) for i in ids
    }
    clubs = current_clubs(s, ids)
    for aid, cs in clubs.items():
        players[aid].clubs = cs
    # aktuell: clubs with players of both sides
    by_club: dict[str, list[tuple[Player, Club]]] = {}
    for p in players.values():
        for c in p.clubs:
            k = next((k for k in by_club if k == c.key or k in c.key or c.key in k), c.key)
            by_club.setdefault(k, []).append((p, c))
    current = []
    now_pairs: set[tuple[int, int, str]] = set()
    for k, members in by_club.items():
        sides_here = {p.side for p, _ in members}
        if sides_here != {0, 1}:
            continue
        members.sort(key=lambda pc: (pc[0].side, pc[0].name))
        label = max((c.name for _, c in members), key=lambda n: (len(team_key(n)) > 0, -len(n)))
        current.append((label, members))
        for p, _ in members:
            for q, _ in members:
                if p.side == 0 and q.side == 1:
                    now_pairs.add((p.id, q.id, k))
    current.sort(key=lambda x: -len(x[1]))
    now_persons = {(p, q) for p, q, _ in now_pairs}
    y = season_start()
    # früher: overlapping seasons at the same club/college, both sides
    by_pair: dict[tuple[int, int], list] = {}
    for pr in relation_pairs(s, [a_ids, b_ids], ids):
        a, b = (
            (pr.a, pr.b)
            if pr.a.athlete_id in players and players[pr.a.athlete_id].side == 0
            else (pr.b, pr.a)
        )
        if (
            a.athlete_id not in players
            or b.athlete_id not in players
            or players[a.athlete_id].side == players[b.athlete_id].side
        ):
            continue
        k = team_key(pr.team_name)
        reaches_now = pr.season_to is None or pr.season_to >= y
        if reaches_now and (a.athlete_id, b.athlete_id) in now_persons:
            continue  # they play together now (the same club, maybe under another name): listed under "aktuell"
        if any(
            (a.athlete_id, b.athlete_id, ck) in now_pairs and (ck == k or ck in k or k in ck)
            for ck in by_club
        ):
            continue
        by_pair.setdefault((a.athlete_id, b.athlete_id), []).append((pr, a, b))
    former = []
    for (aid, bid), prs in by_pair.items():
        teams, keys, links = [], [], []
        for pr, a, b in sorted(prs, key=lambda x: x[0].season_from, reverse=True):
            k = team_key(pr.team_name)
            if any(k == x or k in x or x in k for x in keys):
                continue
            keys.append(k)
            teams.append(f"{pr.team_name} ({_overlap(pr.season_from, pr.season_to, y)})")
            for x in (a, b):
                if x.source_url and len(links) < 4 and x.source_url not in [u for _, u in links]:
                    links.append((_source_name(x.source, x.source_url), x.source_url))
        newest = max(pr.season_from for pr, _, _ in prs)
        former.append((players[aid], players[bid], teams[:3], links, newest))
    former.sort(key=lambda x: (-x[4], x[0].name, x[1].name))
    unknown = [p.name for p in players.values() if not p.clubs]
    return {"players": players, "current": current, "former": former, "unknown": unknown}


def _overlap(start: int, end: int | None, y: int) -> str:
    """'2025/26', '2022/23–2024/25', 'seit 2025/26' (open or reaching into the future: until now)."""
    if end is None or end >= y:
        return f"seit {_season_label(start)}" if start < y else _season_label(start)
    return _season_label(start) if end == start else f"{_season_label(start)}–{_season_label(end)}"


def _who(p: Player, mark: bool) -> str:
    return f"{p.name}{' ' + LINEUP_MARK if mark and p.in_lineup else ''}"


def render(s: Session, st: Setup, res: dict, datenstand: str) -> str:
    from klugscheisser.answer.games import game_label

    a, b = (x.team.name for x in st.sides)
    out = [
        f"**Datenstand:** {datenstand}",
        "",
        f"**🤝 Verbindungen {a} – {b}** (Klub/College, alle gespeicherten Stationen)",
    ]
    mark = bool(st.lineup)
    if st.game is not None:
        g = st.game
        comp = f", {g.competition.name}" if g.competition else ""
        link = f" {_link(g.source or 'Resultat', g.source_url)}" if g.source_url else ""
        out.append(f"_Spiel: {game_label(g)}{comp}{link} ({st.game_note})_")
        if st.from_lineup:
            out.append(
                f"_Verglichen: {a} {len(st.groups[0])}, {b} {len(st.groups[1])} {wording().players} (Aufstellung des Spiels + Kader); "
                f"{LINEUP_MARK} = stand in der Aufstellung dieses Spiels_"
            )
        else:
            out.append(
                f"_Verglichen: Kader {a} ({len(st.groups[0])}) und {b} ({len(st.groups[1])}); keine Aufstellung gespeichert_"
            )
    else:
        out.append(f"_{st.game_note}: {a} {len(st.groups[0])}, {b} {len(st.groups[1])} {wording().players}_")
    out += ["", f"**Aktuell im selben Klub ({_season_label(season_start())})**"]
    if res["current"]:
        for label, members in res["current"]:
            parts = []
            for side in (0, 1):
                who = [f"{_who(p, mark)}" for p, _ in members if p.side == side]
                if who:
                    parts.append(f"{st.sides[side].team.name}: {', '.join(who)}")
            links = []
            for _, c in members:
                if c.url and c.url not in [u for _, u in links] and len(links) < 4:
                    links.append((c.quelle, c.url))
            out.append(f"- {label}: " + " · ".join(parts) + " — " + ", ".join(_link(n, u) for n, u in links))
    else:
        out.append(fill("- {no_player} beider Teams laut gespeicherten Daten aktuell im selben Klub."))
    out += ["", "**Früher zusammen (gleichzeitig im selben Klub/College, Saisons überlappend)**"]
    former = res["former"]
    if former:
        for pa, pb, teams, links, _ in former[:MAX_PAIRS]:
            out.append(
                f"- {_who(pa, mark)} ({st.sides[0].team.name}) – {_who(pb, mark)} ({st.sides[1].team.name}): "
                f"früher zusammen bei {'; '.join(teams)} — " + ", ".join(_link(n, u) for n, u in links)
            )
        if len(former) > MAX_PAIRS:
            out.append(
                f"- … und {len(former) - MAX_PAIRS} weitere Paare (mehr: frage mit {fill('{a_player_dat}')}, z. B. person='…')."
            )
    else:
        out.append("- Keine überlappenden Stationen in den gespeicherten Daten gefunden.")
    if res["unknown"]:
        unk = res["unknown"]
        out.append(
            f"\n_Ohne gespeicherten aktuellen Klub ({len(unk)}): {', '.join(unk[:25])}{' …' if len(unk) > 25 else ''}_"
        )
    out.append(
        "\n_Quellen: Kaderseiten (SIHF/Verbände), Elite Prospects Saisonzeilen, Wikipedia/Wikidata-Stationen. "
        "«aktuell» = Klub der laufenden Saison; «früher» = mindestens eine gemeinsame Saison. Was fehlt, ist nicht "
        "gespeichert. Antwort mit «Datenstand: …» beginnen, Zeilen mit Links übernehmen._"
    )
    return "\n".join(out)


def answer_text(
    s: Session, question: str, datenstand: str, team: m.Team | None = None, event: m.Event | None = None
) -> str | None:
    """The `frage` tool's text for a connections question between two teams; None: not one."""
    from klugscheisser.answer.cross import _named

    if not wants(question):
        return None
    if _named(s, question):
        return None  # "Wen aus Finnland kennt Alina Müller?": one person's stations (usual way)
    st = setup(s, question, team, event)
    if st is None:
        return None
    return render(s, st, lookup(s, st), datenstand)
