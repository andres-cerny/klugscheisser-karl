"""v11: stats search and rankings for the agent (MCP `stats`, `get_athlete`, chat/`frage`).

Built on `stats.queries` (same rules): empty values mean "no data" and are never counted as 0; a total only
sums rows that have the value, and each per-game rate is based only on the games of rows that have that value.
EP's "(all)" summary rows of national teams ("Switzerland (all)", league "International (W)") repeat the
tournament rows and are always left out, so nothing is counted twice. Every row carries its source
(`quelle`, a ready Markdown `quelle_link`, `url`) and data date (`stand`).

Rankings only cover the persons in our database (not a full league table); the result says so.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import german_transliteration, normalize
from klugscheisser.projects import wording
from klugscheisser.stats.queries import COUNT_FIELDS, SOURCE_LABEL, prefer_current, row_dict, season_label

PREFIX = "📊 (Statistik)"
SOURCE_DE = SOURCE_LABEL

# friendly names → league as written by Elite Prospects (keys normalised)
LEAGUE_ALIASES: dict[str, str] = {
    "swhl": "SWHL A (W)",
    "swhl a": "SWHL A (W)",
    "womens league": "SWHL A (W)",
    "women league": "SWHL A (W)",
    "postfinance womens league": "SWHL A (W)",
    "postfinance": "SWHL A (W)",
    "pfwl": "SWHL A (W)",
    "swhl b": "SWHL B (W)",
    "pwhl": "PWHL (W)",
    "sdhl": "SDHL (W)",
    "ncaa": "NCAA (W)",
    "olympia": "OG (W)",
    "olympische spiele": "OG (W)",
    "olympics": "OG (W)",
    "olympic games": "OG (W)",
    "olympischen spielen": "OG (W)",
    "olympischen spiele": "OG (W)",
    "olympic": "OG (W)",
    "og": "OG (W)",
    "wm": "WC (W)",
    "weltmeisterschaft": "WC (W)",
    "weltmeisterschaften": "WC (W)",
    "world championship": "WC (W)",
    "world championships": "WC (W)",
    "wc": "WC (W)",
    "eht": "EHT (W)",
    "euro hockey tour": "EHT (W)",
    "international": "International (W)",
    "u18 wm": "WJC-18 (W)",
    "u18": "WJC-18 (W)",
    "wjc 18": "WJC-18 (W)",
}
# tournaments: few games per season, so per-game rankings need fewer games
TOURNAMENTS = {"OG (W)", "WC (W)", "WC D1 (W)", "EHT (W)", "International (W)", "WJC-18 (W)", "OGQ (W)"}

TEAM_KINDS: dict[str, set[str]] = {
    "club": {"club", "college"},
    "verein": {"club", "college"},
    "klub": {"club", "college"},
    "college": {"college"},
    "national": {"national"},
    "nationalteam": {"national"},
    "nationalmannschaft": {"national"},
    "nati": {"national"},
    "junioren": {"youth"},
    "youth": {"youth"},
    "nachwuchs": {"youth"},
}

# sort key → (German label, descending?)
SORTS: dict[str, tuple[str, bool]] = {
    "points": ("Punkte", True),
    "goals": ("Tore", True),
    "assists": ("Assists", True),
    "points_per_game": ("Punkte pro Spiel", True),
    "goals_per_game": ("Tore pro Spiel", True),
    "assists_per_game": ("Assists pro Spiel", True),
    "games": ("Spiele", True),
    "save_pct": ("Fangquote", True),
    "gaa": ("Gegentorschnitt", False),
    "wins": ("Siege", True),
    "shutouts": ("Shutouts", True),
    "penalty_minutes": ("Strafminuten", True),
    "plus_minus": ("Plus/Minus", True),
}
GOALIE_SORTS = {"save_pct", "gaa", "wins", "shutouts"}
SKATER_SORTS = {
    "points",
    "goals",
    "assists",
    "points_per_game",
    "goals_per_game",
    "assists_per_game",
    "plus_minus",
}
RATE_SORTS = {"points_per_game", "goals_per_game", "assists_per_game", "save_pct", "gaa"}
GROUPS = {"season", "career", "team", "league"}
GROUPS_DE = {
    "season": "pro Saison",
    "career": "Karriere pro Person",
    "team": "pro Team",
    "league": "pro Liga",
}


def current_season(today: date | None = None) -> int:
    """Start year of the running season (August–July): 2026-10-04 → 2026 (= 2026/27)."""
    d = today or date.today()
    return d.year if d.month >= 8 else d.year - 1


# --- parsing of the parameters ---------------------------------------------------------------------------


def parse_season(value: str | int | None, today: date | None = None) -> tuple[int, int] | None:
    """'2026/27', '2026-27', '2026-2027', '26/27', 2026, 'diese Saison', 'letzte Saison', 'seit 2022',
    '2020/21–2025/26', '2022 bis 2025' → (first, last) start years. None = all seasons."""
    if value is None or value == "":
        return None
    cur = current_season(today)
    if isinstance(value, int):
        return value, value
    v = str(value).strip().lower().replace("saison", "").strip()
    if not v or v in {"alle", "all", "karriere", "career", "gesamt", "insgesamt"}:
        return None
    if re.search(r"\b(diese|aktuell\w*|laufend\w*|this|current|heuer)\b", v):
        return cur, cur
    if re.search(r"\b(letzte|vergangene|vorige|last|previous)\b|vorsaison", v):
        return cur - 1, cur - 1
    if mt := re.fullmatch(r"(?:seit|ab|since|from)\s*(\d{4})(?:\s*[/-]\s*\d{2,4})?", v):
        return int(mt.group(1)), cur
    if mt := re.fullmatch(r"(\d{2})\s*/\s*(\d{2})", v):
        y = 2000 + int(mt.group(1))
        return y, y
    if mt := re.fullmatch(r"(\d{4})(?:\s*[/-]\s*(\d{2}|\d{4}))?", v):
        y = int(mt.group(1))
        if not mt.group(2):
            return y, y
        e = int(mt.group(2))
        if e < 100:
            e += y // 100 * 100
            if e < y:
                e += 100
        return (y, y) if e == y + 1 else (min(y, e), max(y, e))
    if mt := re.fullmatch(r"(\d{4})(?:\s*/\s*\d{2})?\s*(?:–|—|-|bis|to)\s*(\d{4})(?:\s*/\s*\d{2})?", v):
        a, b = int(mt.group(1)), int(mt.group(2))
        return min(a, b), max(a, b)
    raise ValueError(
        f"Unbekannte Saison '{value}'. Erlaubt: 2026/27, 2026-27, 2026 (= 2026/27), 'diese Saison', "
        "'letzte Saison', 'seit 2022', '2020/21–2025/26'."
    )


def resolve_leagues(s: Session, value: str | list[str] | None) -> tuple[list[str], list[str]]:
    """Friendly league names → EP league names. Returns (leagues, unknown inputs)."""
    if not value:
        return [], []
    values = value if isinstance(value, list) else re.split(r"\s*(?:,|;|\bvs\.?\b|\bund\b|/)\s*", value)
    known = {lg: lg for lg in s.scalars(select(m.SeasonStat.league).distinct())}
    by_norm = {normalize(lg): lg for lg in known}
    by_norm.update({normalize(re.sub(r"\(w\)", "", lg, flags=re.I)): lg for lg in known})
    out, unknown = [], []
    for v in values:
        if not v or not str(v).strip():
            continue
        n = normalize(str(v))
        lg = LEAGUE_ALIASES.get(n) or by_norm.get(n) or by_norm.get(normalize(f"{v} (W)"))
        if lg is None:
            unknown.append(str(v))
            lg = str(v).strip()  # exact filter: finds nothing, the result says so
        if lg not in out:
            out.append(lg)
    return out, unknown


def resolve_team_kinds(value: str | None) -> set[str] | None:
    if not value:
        return None
    n = normalize(value)
    for key, kinds in TEAM_KINDS.items():
        if n == key or n.startswith(key):
            return kinds
    raise ValueError(f"Unbekannter team_kind '{value}'. Erlaubt: club, college, nationalteam, junioren.")


def resolve_phase(value: str | None) -> str | None:
    """'regular' (default) | 'playoffs' | None for both."""
    if value is None:
        return "regular"
    n = normalize(value)
    if n in {"alle", "all", "beide", "both", ""}:
        return None
    if n.startswith("playoff") or n in {"po", "post", "postseason"}:
        return "playoffs"
    if n.startswith(("regul", "haupt", "quali")):
        return "regular"
    raise ValueError(f"Unbekannte phase '{value}'. Erlaubt: regular, playoffs, alle.")


def resolve_nationality(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"[A-Za-z]{3}", v):
        return v.upper()
    from klugscheisser.adds.wiki import countries_in

    found = countries_in(v)
    if not found:
        raise ValueError(f"Unbekannte Nation '{value}'. Beispiel: SUI, Schweiz, SWE.")
    return found[0]


def _nation_team_word(ioc: str) -> str:
    from klugscheisser.adds.wiki import COUNTRIES

    return normalize(COUNTRIES.get(ioc, (ioc,))[0])


def resolve_person(s: Session, name: str | int) -> m.Athlete:
    """Like get_athlete (all spellings, umlauts, aliases, typos), plus a unique surname ("Stalder")."""
    from klugscheisser.db import repo

    a = repo.resolve_athlete(s, name if isinstance(name, int) else str(name))
    if a is not None:
        return a
    q = normalize(str(name))
    if q and " " not in q:
        hits = [
            x
            for x in s.scalars(select(m.Athlete))
            if q
            in {
                normalize(x.full_name).split()[-1],
                normalize(german_transliteration(x.full_name)).split()[-1],
            }
        ]
        if len(hits) > 1:  # prefer persons with stats
            with_stats = set(
                s.scalars(
                    select(m.SeasonStat.athlete_id)
                    .where(m.SeasonStat.athlete_id.in_([x.id for x in hits]))
                    .distinct()
                )
            )
            hits = [x for x in hits if x.id in with_stats] or hits
        if len(hits) == 1:
            return hits[0]
        if hits:
            names = ", ".join(sorted(x.full_name for x in hits)[:6])
            raise ValueError(f"Mehrere Personen heissen '{name}': {names}. Bitte den vollen Namen angeben.")
    matches = repo.find_athletes(s, str(name), limit=5)
    if matches:
        names = ", ".join(x.athlete.full_name for x in matches)
        raise ValueError(f"Mehrere Treffer für '{name}': {names}. Bitte den vollen Namen angeben.")
    raise ValueError(
        f"Keine Person gefunden für '{name}'. Fehlt sie? hinzufuegen(name='{name}', details=…) schlägt sie vor."
    )


# --- formatting ----------------------------------------------------------------------------------------------


def _num(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}".replace(".", ",")


def _stand(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _stand_de(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y") if iso else "ohne Datum"


def source_fields(source: str, url: str, stand: str | None) -> dict[str, str | None]:
    """`source` may name several sources joined by '+' (a total over EP and current rows)."""
    name = " + ".join(SOURCE_DE.get(x, x) for x in source.split("+"))
    label = f"{name}, Stand {_stand_de(stand)}"
    return {"quelle": name, "quelle_link": f"[{label}]({url})", "url": url, "stand": stand}


def is_goalie_row(r: dict[str, Any]) -> bool:
    return any(
        r.get(f) is not None for f in ("wins", "losses", "shutouts", "goals_against", "saves", "gaa")
    ) or (r.get("save_pct") is not None)


LABELS = {  # field → (one, many)
    "games": ("Spiel", "Spiele"),
    "goals": ("Tor", "Tore"),
    "assists": ("Assist", "Assists"),
    "points": ("Punkt", "Punkte"),
    "penalty_minutes": ("Strafminute", "Strafminuten"),
    "wins": ("Sieg", "Siege"),
    "losses": ("Niederlage", "Niederlagen"),
    "shutouts": ("Shutout", "Shutouts"),
}


def _count(r: dict[str, Any], f: str) -> str:
    one, many = LABELS[f]
    return f"{r[f]} {one if r[f] == 1 else many}"


def stat_text(r: dict[str, Any]) -> str:
    """'24 Spiele, 29 Tore, 34 Assists, 63 Punkte (2,63 pro Spiel), 10 Strafminuten' — only values we have."""
    bits = [_count(r, "games")] if r.get("games") is not None else []
    if is_goalie_row(r):
        bits += [_count(r, f) for f in ("wins", "losses", "shutouts") if r.get(f) is not None]
        if r.get("save_pct") is not None:
            bits.append(f"Fangquote {_num(r['save_pct'] * 100, 1)} %")
        if r.get("gaa") is not None:
            bits.append(f"Gegentorschnitt {_num(r['gaa'])}")
    else:
        bits += [_count(r, f) for f in ("goals", "assists", "points") if r.get(f) is not None]
        if r.get("points_per_game") is not None:
            bits[-1] += f" ({_num(r['points_per_game'])} pro Spiel)"
        if r.get("penalty_minutes") is not None:
            bits.append(_count(r, "penalty_minutes"))
        if r.get("plus_minus") is not None:
            bits.append(f"Plus/Minus {r['plus_minus']:+d}")
    if len(bits) <= 1 and r.get("games") is not None:
        bits.append("weitere Werte fehlen in der Quelle")
    return ", ".join(bits) or "keine Werte in der Quelle"


def ep_quote(r: dict[str, Any]) -> str:
    """The values as Elite Prospects writes them (GP/G/A/PTS …): the "quote" of an answer line."""
    keys = (
        (
            ("games", "GP"),
            ("wins", "W"),
            ("losses", "L"),
            ("shutouts", "SO"),
            ("goals_against", "GA"),
            ("save_pct", "SVP"),
            ("gaa", "GAA"),
        )
        if is_goalie_row(r)
        else (
            ("games", "GP"),
            ("goals", "G"),
            ("assists", "A"),
            ("points", "PTS"),
            ("penalty_minutes", "PIM"),
            ("plus_minus", "+/-"),
        )
    )
    return " · ".join(f"{label} {r[k]}" for k, label in keys if r.get(k) is not None) or "keine Werte"


def _where(r: dict[str, Any]) -> str:
    if r.get("saison"):
        when = r["saison"]
    elif r.get("von"):
        when = r["von"] if r["von"] == r["bis"] else f"{r['von']}–{r['bis']}"
    else:
        when = ""
    team = r.get("team")
    league = r.get("league")
    where = f"{team} ({league})" if team and league else team or league or "alle Ligen"
    po = ", Playoffs" if r.get("phase") == "playoffs" else ""
    seasons = f", {r['saisons']} Saisons" if r.get("saisons", 0) > 1 else ""
    return f"{when} {where}{po}{seasons}".strip()


def sentence(r: dict[str, Any]) -> str:
    return f"{r['person']}, {_where(r)}: {stat_text(r)}."


# --- the query ---------------------------------------------------------------------------------------------


@dataclass
class StatsResult:
    rows: list[dict[str, Any]]
    filters: dict[str, Any]
    hinweise: list[str] = field(default_factory=list)
    total: int = 0
    newest: str | None = None
    person_ids: list[int] = field(default_factory=list)


def _team_match(team_name: str | None, wanted: str) -> bool:
    if not team_name:
        return False
    hay = normalize(team_name)
    words = normalize(wanted).split()
    return all(re.search(rf"\b{re.escape(w)}", hay) for w in words)


def _aggregate(rows: list[m.SeasonStat]) -> dict[str, Any]:
    """Sum of several season rows of one person (None stays None when no row has the value)."""
    t: dict[str, Any] = {f: None for f in COUNT_FIELDS}
    basis: dict[str, int] = defaultdict(int)
    sv_w = sv_g = gaa_w = gaa_g = 0.0
    for r in rows:
        for f in COUNT_FIELDS:
            v = getattr(r, f)
            if v is not None:
                t[f] = (t[f] or 0) + v
        for f in ("points", "goals", "assists"):
            if getattr(r, f) is not None and r.games:
                basis[f] += r.games
        if r.save_pct is not None and r.games:
            sv_w, sv_g = sv_w + r.save_pct * r.games, sv_g + r.games
        if r.gaa is not None and r.games:
            gaa_w, gaa_g = gaa_w + r.gaa * r.games, gaa_g + r.games
    for key, f in (
        ("points_per_game", "points"),
        ("goals_per_game", "goals"),
        ("assists_per_game", "assists"),
    ):
        t[key] = round(t[f] / basis[f], 2) if basis[f] and t[f] is not None else None
    # save % from saves and goals against when every row with goalie data has both, else games-weighted
    sv_rows = [r for r in rows if r.saves is not None and r.goals_against is not None]
    with_sv = [r for r in rows if r.save_pct is not None or r.saves is not None]
    shots = sum(r.saves + r.goals_against for r in sv_rows)
    if shots and len(sv_rows) == len(with_sv):
        t["save_pct"] = round(sum(r.saves for r in sv_rows) / shots, 3)
    else:
        t["save_pct"] = round(sv_w / sv_g, 3) if sv_g else None
    t["gaa"] = round(gaa_w / gaa_g, 2) if gaa_g else None
    seasons = sorted({r.season for r in rows})
    teams = list(dict.fromkeys(r.team_name for r in rows if r.team_name))
    leagues = list(dict.fromkeys(r.league for r in rows))
    oldest_fetch = min((r.fetched_at for r in rows if r.fetched_at), default=None)
    t.update(
        von=season_label(seasons[0]),
        bis=season_label(seasons[-1]),
        saisons=len(seasons),
        team=teams[0] if len(teams) == 1 else None,
        teams=teams[:4] if len(teams) > 1 else None,
        league=leagues[0] if len(leagues) == 1 else None,
        leagues=leagues[:6] if len(leagues) > 1 else None,
        team_kind=rows[0].team_kind if len({r.team_kind for r in rows}) == 1 else None,
        phase=rows[0].phase if len({r.phase for r in rows}) == 1 else None,
        source="+".join(dict.fromkeys(r.source for r in rows)),
        source_url=rows[0].source_url,
        fetched_at=oldest_fetch,
    )
    return t


def _person_fields(a: m.Athlete) -> dict[str, Any]:
    return {"person": a.full_name, "person_id": a.id, "nation": a.nationality, "position": a.position}


def _finish(row: dict[str, Any], source: str, url: str, fetched: datetime | None) -> dict[str, Any]:
    row.update(source_fields(source, url, _stand(fetched)))
    row["zeile_prefix"] = PREFIX
    row["zitat"] = ep_quote(row)
    row["satz"] = sentence(row)
    if row.get("games") is not None and not is_goalie_row(row) and row.get("points") is None:
        row["hinweis"] = "Nur Spiele erfasst (keine Tore/Punkte in der Quelle) – nicht als 0 zählen."
    elif row.get("games") is None and row.get("points") is not None:
        row["hinweis"] = (
            "Aus den Spielberichten gezählt: nur Tore und Assists, Spiele nicht erfasst "
            f"({wording().players} ohne Punkt "
            "fehlen) – nicht als vollständige Turnierstatistik lesen."
        )
    for k in ("source", "source_url", "fetched_at", "season"):
        row.pop(k, None)
    return {k: v for k, v in row.items() if v is not None}


def default_min_games(sort_by: str | None, group: str, leagues: list[str]) -> int:
    if sort_by not in RATE_SORTS:
        return 0
    tournament = bool(leagues) and all(lg in TOURNAMENTS for lg in leagues)
    if group == "season":
        return 3 if tournament else 5
    return 5 if tournament else 10


def query(
    s: Session,
    person: str | int | list[str | int] | None = None,
    team: str | None = None,
    league: str | list[str] | None = None,
    season: str | int | None = None,
    team_kind: str | None = None,
    phase: str | None = "regular",
    nationality: str | None = None,
    position: str | None = None,
    sort_by: str | None = None,
    group: str = "season",
    min_games: int | None = None,
    limit: int = 20,
    today: date | None = None,
    newest_first: bool = False,
) -> StatsResult:
    """`newest_first` (MCP `stats`): unranked season rows newest season first, so the current season heads
    the list and `limit` cuts the oldest seasons; several persons interleave per season (person order inside).
    Off (chat): per person, then chronological."""
    group = (group or "season").strip().lower()
    group = {"saison": "season", "karriere": "career", "liga": "league", "person": "career"}.get(group, group)
    if group not in GROUPS:
        raise ValueError("group muss 'season', 'career', 'team' oder 'league' sein.")
    if sort_by:
        sort_by = sort_by.strip().lower()
        if sort_by not in SORTS:
            raise ValueError(f"Unbekanntes sort_by '{sort_by}'. Erlaubt: {', '.join(SORTS)}.")
    limit = max(1, min(int(limit or 20), 100))
    names = person if isinstance(person, list) else ([person] if person not in (None, "") else [])
    athletes = list(dict.fromkeys(resolve_person(s, n) for n in names))
    leagues, unknown_leagues = resolve_leagues(s, league)
    seasons = parse_season(season, today)
    kinds = resolve_team_kinds(team_kind)
    ph = resolve_phase(phase)
    nat = resolve_nationality(nationality)
    pos = position.strip().upper()[:1] if position else None

    q = select(m.SeasonStat, m.Athlete).join(m.Athlete, m.Athlete.id == m.SeasonStat.athlete_id)
    q = q.where(or_(m.SeasonStat.team_name.is_(None), ~m.SeasonStat.team_name.ilike("%(all)")))
    if athletes:
        q = q.where(m.SeasonStat.athlete_id.in_([a.id for a in athletes]))
    if leagues:
        q = q.where(m.SeasonStat.league.in_(leagues))
    if seasons:
        q = q.where(m.SeasonStat.season.between(*seasons))
    if kinds:
        q = q.where(m.SeasonStat.team_kind.in_(kinds))
    if ph:
        q = q.where(m.SeasonStat.phase == ph)
    if nat:
        q = q.where(m.Athlete.nationality == nat)
    if pos:
        q = q.where(m.Athlete.position == pos)
    pairs = list(s.execute(q.order_by(m.SeasonStat.season, m.SeasonStat.id)).all())
    keep = {id(r) for r in prefer_current([r for r, _ in pairs])}  # newer SIHF/sheet rows instead of EP
    pairs = [(r, a) for r, a in pairs if id(r) in keep]
    replaced = [r for r, _ in pairs if getattr(r, "replaces", None)]
    if team:
        t = normalize(team)
        if t in {"schweiz", "nati", "sui", "swiss", "schweizer nati"}:
            team = "Switzerland"
        pairs = [(r, a) for r, a in pairs if _team_match(r.team_name, team)]

    hinweise: list[str] = []
    if replaced:
        newest_cur = max(r.fetched_at for r in replaced if r.fetched_at)
        ep_day = max((x.fetched_at for r in replaced for x in r.replaces if x.fetched_at), default=None)
        names = " + ".join(dict.fromkeys(SOURCE_DE.get(r.source, r.source) for r in replaced))
        ep = f"Elite Prospects (Stand {ep_day:%d.%m.%Y})" if ep_day else "Elite Prospects"
        hinweise.append(
            f"Aktuelle Werte von {names} (Stand {newest_cur:%d.%m.%Y}) statt {ep} für dieselbe Saison und Liga; "
            "die EP-Werte sind nicht mitgezählt (je Saisonzeile unter 'ersetzt')."
        )
    rows: list[dict[str, Any]] = []
    if group == "season":
        for r, a in pairs:
            d = row_dict(r)
            d.update(_person_fields(a))
            d.pop("quelle_link", None)
            d.pop("stand", None)
            rows.append(_finish(d, r.source, r.source_url, r.fetched_at))
    else:
        buckets: dict[tuple, list[m.SeasonStat]] = defaultdict(list)
        persons: dict[int, m.Athlete] = {}
        for r, a in pairs:
            key = {"career": (a.id,), "team": (a.id, r.team_name, r.league), "league": (a.id, r.league)}[
                group
            ]
            buckets[key].append(r)
            persons[a.id] = a
        for key, rs in buckets.items():
            a = persons[key[0]]
            d = _aggregate(rs)
            d.update(_person_fields(a))
            rows.append(_finish(d, d["source"], d["source_url"], d["fetched_at"]))

    total = len(rows)
    ranking = not athletes or (len(athletes) > 1 and sort_by is not None)
    mg = default_min_games(sort_by, group, leagues) if min_games is None else max(0, int(min_games))
    if sort_by:
        label, desc = SORTS[sort_by]
        if ranking and sort_by in GOALIE_SORTS:  # a goalie ranking lists goalies, a scorer ranking skaters
            rows = [r for r in rows if is_goalie_row(r) or r.get("position") == "G"]
        elif ranking and sort_by in SKATER_SORTS:
            rows = [r for r in rows if not is_goalie_row(r) and r.get("position") != "G"]
        if mg:
            few = [r for r in rows if (r.get("games") or 0) < mg]
            if few:
                hinweise.append(f"{len(few)} Zeile(n) mit weniger als {mg} Spielen nicht gerankt ({label}).")
            rows = [r for r in rows if (r.get("games") or 0) >= mg]
        missing = [r for r in rows if r.get(sort_by) is None]
        if missing:
            hinweise.append(
                f"{len(missing)} Zeile(n) ohne Angabe zu {label} (z. B. ältere Saisons nur mit Spielen): "
                + ("nicht gerankt." if ranking else "am Schluss, nicht als 0 gezählt.")
            )
        have = [r for r in rows if r.get(sort_by) is not None]
        have.sort(key=lambda r: (-(r[sort_by]) if desc else r[sort_by], -(r.get("games") or 0)))
        rows = have if ranking else have + missing
    elif athletes:
        order = {a.id: i for i, a in enumerate(athletes)}
        if group == "season" and newest_first:  # newest season first; persons in the asked order inside
            rows.sort(key=lambda r: order[r["person_id"]])
            rows.sort(key=lambda r: r.get("saison") or "", reverse=True)  # '2026/27' labels sort like years
        elif group == "season":  # rows come by season: person order, then season
            rows.sort(key=lambda r: order[r["person_id"]])
        else:
            rows.sort(key=lambda r: (order[r["person_id"]], -(r.get("games") or 0)))
    else:
        rows.sort(key=lambda r: -(r.get("points") or -1))
    if ranking and rows:
        hinweise.append(
            "Rangliste nur über die Personen in unserer Datenbank (keine vollständige Ligatabelle)."
        )
    for i, r in enumerate(rows[:limit], 1):
        if ranking and sort_by:
            r["rang"] = i
    if any(r.get("hinweis") for r in rows[:limit]) and not any("ohne Angabe" in h for h in hinweise):
        hinweise.append(
            "Ältere Saisons teils nur mit Spielen (Tore/Punkte fehlen dort; nicht als 0 gezählt)."
        )
    if unknown_leagues:
        known = sorted(set(s.scalars(select(m.SeasonStat.league).distinct())))
        hinweise.append(
            f"Liga nicht erkannt: {', '.join(unknown_leagues)}. Bekannte Ligen: {', '.join(known[:25])}."
        )
    if not rows:
        hinweise.append("Keine Statistik in unseren Daten für diese Filter.")
    stands = [r["stand"] for r in rows if r.get("stand")]
    filters = {
        "personen": [a.full_name for a in athletes] or None,
        "team": team,
        "liga": leagues or None,
        "saison": (
            None
            if not seasons
            else season_label(seasons[0])
            if seasons[0] == seasons[1]
            else f"{season_label(seasons[0])}–{season_label(seasons[1])}"
        ),
        "team_kind": sorted(kinds) if kinds else None,
        "phase": ph or "alle",
        "nation": nat,
        "position": pos,
        "gruppierung": GROUPS_DE[group],
        "sortierung": SORTS[sort_by][0] if sort_by else None,
        "min_spiele": mg or None,
    }
    return StatsResult(
        rows=rows[:limit],
        filters={k: v for k, v in filters.items() if v is not None},
        hinweise=hinweise,
        total=total,
        newest=max(stands) if stands else None,
        person_ids=[a.id for a in athletes],
    )


# --- stints from season rows (for "who played together") ----------------------------------------------------


def season_stints(
    s: Session, athlete_ids: list[int] | None = None, team_names: set[str] | None = None
) -> list:
    """Career stations derived from the season rows (one per person, team and run of consecutive seasons), as
    `facts.queries.StintRow` with source and link, so "played together" also sees teams only EP lists."""
    from klugscheisser.facts.queries import StintRow

    q = (
        select(m.SeasonStat, m.Athlete.full_name)
        .join(m.Athlete, m.Athlete.id == m.SeasonStat.athlete_id)
        .where(m.SeasonStat.team_name.is_not(None), ~m.SeasonStat.team_name.ilike("%(all)"))
    )
    if athlete_ids is not None:
        q = q.where(m.SeasonStat.athlete_id.in_(athlete_ids))
    if team_names is not None:
        q = q.where(m.SeasonStat.team_name.in_(team_names))
    by: dict[tuple, list[tuple[m.SeasonStat, str]]] = defaultdict(list)
    pairs = list(s.execute(q))
    keep = {id(r) for r in prefer_current([r for r, _ in pairs])}
    for r, name in pairs:
        if id(r) not in keep:
            continue
        # v12: one station per Elite Prospects team id, so a renamed club stays one station
        by[(r.athlete_id, r.ep_team_id or r.team_name)].append((r, name))
    out = []
    for (aid, _team_key), rs in by.items():
        rs.sort(key=lambda x: x[0].season)
        team = rs[-1][0].team_name  # the newest name
        seasons = sorted({r.season for r, _ in rs})
        runs: list[list[int]] = []
        for y in seasons:
            if runs and y == runs[-1][-1] + 1:
                runs[-1].append(y)
            else:
                runs.append([y])
        r0, name = rs[0]
        for run in runs:
            in_run = [r for r, _ in rs if r.season in run]
            games = sum(r.games or 0 for r in in_run if r.phase == "regular")
            label = (
                season_label(run[0]) if len(run) == 1 else f"{season_label(run[0])}–{season_label(run[-1])}"
            )
            out.append(
                StintRow(
                    athlete_id=aid,
                    athlete_name=name,
                    team_id=None,
                    team_name=team,
                    kind=r0.team_kind if r0.team_kind in ("club", "college", "national", "youth") else "club",
                    league=r0.league,
                    season_from=run[0],
                    season_to=run[-1],
                    role="player",
                    status="confirmed",
                    confidence=1.0,
                    source=r0.source,
                    source_url=r0.source_url,
                    quote=f"{team} ({r0.league}) {label}" + (f": {games} Spiele" if games else ""),
                    ep_team_id=r0.ep_team_id,
                )
            )
    return out


# --- understanding a chat question ("Wie viele Punkte hat Stalder diese Saison?") -----------------------------

STAT_CUE = re.compile(
    r"\b(punkte?n?|pts|tore|toren|treffer|assists?|vorlagen|skorer\w*|scorer\w*|torschützin\w*|fangquote|"
    r"abwehrquote|save ?%|save percent\w*|save pct|sv ?%|saves|paraden|gegentor\w*|gaa|shutouts?|zu null|"
    r"strafminuten|plus ?/? ?minus|statisti\w*|stats|bilanz|einsätze|wie viele spiele|pro spiel|points?|"
    r"goals?|assists?|how many games|games played|per game)\b",
    re.I,
)
SORT_CUES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"(punkte?n?|pts|skorer\w*|scorer\w*) pro spiel|\bppg\b|punkteschnitt", re.I),
        "points_per_game",
    ),
    (re.compile(r"(tore|toren|treffer) pro spiel|torschnitt", re.I), "goals_per_game"),
    (re.compile(r"(assists?|vorlagen) pro spiel", re.I), "assists_per_game"),
    (re.compile(r"(points?|pts) per game|\bppg\b", re.I), "points_per_game"),
    (re.compile(r"goals? per game", re.I), "goals_per_game"),
    (
        re.compile(r"fangquote|abwehrquote|save ?%|save percent\w*|save pct|sv ?%|saves|paraden", re.I),
        "save_pct",
    ),
    (re.compile(r"gegentorschnitt|\bgaa\b|gegentore pro spiel", re.I), "gaa"),
    (re.compile(r"shutouts?|zu null", re.I), "shutouts"),
    (re.compile(r"strafminuten|\bpim\b", re.I), "penalty_minutes"),
    (re.compile(r"plus ?/? ?minus", re.I), "plus_minus"),
    (re.compile(r"\b(tore|toren|treffer|torschützin\w*|goals?)\b", re.I), "goals"),
    (re.compile(r"\b(assists?|vorlagen)\b", re.I), "assists"),
    (re.compile(r"\b(punkte?n?|pts|skorer\w*|scorer\w*|points?)\b", re.I), "points"),
    (re.compile(r"\b(einsätze|spiele|games)\b", re.I), "games"),
]
RANKING = re.compile(
    r"\b(beste[nrs]?|meiste[nr]?|am meisten|top ?\d*|rangliste|ranking|führend\w*|höchste[nr]?|"
    r"topskorer\w*|topscorer\w*)\b",
    re.I,
)
CAREER = re.compile(r"\b(karriere|insgesamt|gesamthaft|total|all ?time|bisher|jemals|ever|career)\b", re.I)
NATIONAL = re.compile(r"(für die schweiz|für die nati\b|\bnati\b|nationalteam|nationalmannschaft)", re.I)
SEASON_IN_TEXT = re.compile(
    r"\b\d{4}\s*[/-]\s*\d{2,4}\b|\b\d{2}/\d{2}\b|\bsaison \d{4}\b|\b(diese[nr]?|aktuelle[nr]?|laufende[nr]?|"
    r"letzte[nr]?|vergangene[nr]?|vorige[nr]?) saison\b|\bvorsaison\b|\bthis season\b|\blast season\b",
    re.I,
)
# a number is asked for (not just a goal mentioned in a story)
QUANT = re.compile(
    r"wie ?viele?|anzahl|statisti|\bstats\b|bilanz|zahlen|pro spiel|quote|schnitt|\bgaa\b|insgesamt|karriere|"
    r"\btotal\b|how many|percent\w*|prozent|\baverage\b",
    re.I,
)
COMPARE = re.compile(
    r"\b(vs\.?|versus|gegen|im vergleich|verglichen|vergleich\w*|oder|compared?|comparison)\b", re.I
)
# "aktuell", "this year": the running season, also when a league is named
CURRENT = re.compile(
    r"\b(aktuell\w*|derzeit\w*|zurzeit|momentan\w*|im moment|dieses jahr|heuer|currently|current|"
    r"this year|right now|so far this)\b",
    re.I,
)
# "Olympia 2026", "WM 2025", "2026 Olympics": a calendar year at a tournament is the season that ENDS in it
_TOURNEY_WORD = (
    r"(?:olympia\w*|olympics?|olympische\w* spiele\w*|olympic games|wm|weltmeisterschaft\w*|"
    r"world championships?|worlds|u18 ?-?wm|eht|euro hockey tour)"
)
TOURNEY_YEAR = re.compile(
    rf"\b{_TOURNEY_WORD}(?:\s+(?:in|von|of|im jahr|in the year))?\s+((?:19|20)\d{{2}})\b"
    rf"|\b((?:19|20)\d{{2}})(?:er)?\s+{_TOURNEY_WORD}",
    re.I,
)
# a follow-up about the persons of the previous answer ("und wie viele Punkte hat sie?")
FOLLOWUP = re.compile(
    r"\b(sie|ihr|ihre[mnrs]?|er|ihm|sein|seine[mnrs]?|her|she|he|him|his|they|them|their|"
    r"die beiden|beide|both)\b|^\s*(und|and|what about|und was ist mit)\b",
    re.I,
)
RELATION_CUE = re.compile(
    r"zusammen gespielt|spielten? zusammen|zusammengespielt|teamkolleg|mitspielerin", re.I
)


def leagues_in(question: str) -> list[str]:
    """Leagues named in a question, in text order ("Olympia vs WM" → OG (W), WC (W))."""
    n = f" {normalize(question)} "
    found: dict[str, int] = {}
    for alias, lg in sorted(LEAGUE_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in {"og", "wc", "international"}:
            continue  # too common as words; the tool parameter accepts them
        pos = n.find(f" {alias} ")
        if pos >= 0 and lg not in found:
            found[lg] = pos
    return sorted(found, key=found.get)


def persons_in(s: Session, question: str, leagues: list[str] | None = None) -> list[int]:
    """Persons named in the question: full names (all spellings) and unique surnames of persons with stats
    ("Stalder", "Brändli"), in text order. See `persons_named` for surnames several persons share."""
    return persons_named(s, question, leagues)[0]


def persons_named(s: Session, question: str, leagues: list[str] | None = None) -> tuple[list[int], list[str]]:
    """Like `persons_in`, plus (ids, notes). A surname several persons with stats share ("Müller") is taken
    as the person with clearly the most games in the named leagues (only when a league is named); the note
    says so ("„Müller“ als Alina Müller verstanden …"). Without a league it stays unresolved."""
    from klugscheisser.domain.matcher import AthleteMatcher

    ids = [link.athlete_id for link in AthleteMatcher(s).from_text(question)]
    notes: list[str] = []
    words = {
        normalize(x): x
        for x in (re.sub(r"['’]s$", "", w) for w in re.findall(r"\b[A-ZÄÖÜÉÈ][\w'’-]{2,}", question))
    }
    if words:
        rows = s.execute(
            select(m.Athlete.id, m.Athlete.full_name).where(
                m.Athlete.id.in_(select(m.SeasonStat.athlete_id).distinct())
            )
        ).all()
        by_surname: dict[str, set[int]] = defaultdict(set)
        full: dict[int, str] = {}
        for aid, name in rows:
            full[aid] = name
            for variant in (normalize(name), normalize(german_transliteration(name))):
                if " " in variant:
                    by_surname[variant.split()[-1]].add(aid)
        for w in sorted(words):
            hits = by_surname.get(w, set())
            if len(hits) == 1:
                ids.append(next(iter(hits)))
            elif len(hits) > 1 and leagues and not hits & set(ids):
                pick = _busiest_in(s, hits, leagues)
                if pick is not None:
                    ids.append(pick)
                    notes.append(
                        f"„{words[w]}“ als {full[pick]} verstanden (meiste Spiele in {', '.join(leagues)})"
                    )
    norm = normalize(question)

    def pos(aid: int) -> int:
        p = norm.find(normalize(s.get(m.Athlete, aid).full_name).split()[-1])
        return p if p >= 0 else len(norm)

    return sorted(dict.fromkeys(ids), key=pos), notes


def _busiest_in(s: Session, ids: set[int], leagues: list[str]) -> int | None:
    """The person with clearly the most games in these leagues (at least twice the next one), else None."""
    games = dict(
        s.execute(
            select(m.SeasonStat.athlete_id, func.coalesce(func.sum(m.SeasonStat.games), 0))
            .where(
                m.SeasonStat.athlete_id.in_(ids),
                m.SeasonStat.league.in_(leagues),
                or_(m.SeasonStat.team_name.is_(None), ~m.SeasonStat.team_name.ilike("%(all)")),
            )
            .group_by(m.SeasonStat.athlete_id)
        ).all()
    )
    ranked = sorted(((int(g or 0), aid) for aid, g in games.items()), reverse=True)
    if not ranked or ranked[0][0] <= 0:
        return None
    if len(ranked) > 1 and ranked[0][0] < 2 * ranked[1][0]:
        return None
    return ranked[0][1]


def _turn_ids(s: Session, turn: dict[str, Any]) -> list[int]:
    ids = [int(i) for i in turn.get("person_ids") or []]
    if not ids and turn.get("personen"):
        from klugscheisser.db import repo

        for name in turn["personen"]:
            a = repo.resolve_athlete(s, str(name))
            if a is not None:
                ids.append(a.id)
    return list(dict.fromkeys(ids))


def followup_person_ids(s: Session, question: str, history: list[dict] | None) -> list[int]:
    """Persons a follow-up question is about ("Und wie viele Punkte hat sie diese Saison?"): only when the
    question names nobody and refers back (sie/ihr/her/she/er/…, or starts with "und"/"and"). Taken from
    the previous turn: the persons its answer sentences (`saetze`) name — so the single "winner" of "Wer ist
    die älteste …?" — else its persons when they are few (at most 5). [] = nothing to resolve."""
    if not history or not FOLLOWUP.search(question):
        return []
    if persons_in(s, question):
        return []
    last = history[-1]
    ids = _turn_ids(s, last)
    text = " ".join(str(x) for x in (last.get("saetze") or [])) or str(last.get("antwort") or "")
    if text:
        if not ids:  # no stored persons: the names in the answer itself
            ids = persons_in(s, text)
        norm = f" {normalize(text)} "
        found: dict[int, int] = {}
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids))) if ids else []:
            for variant in (normalize(a.full_name), normalize(german_transliteration(a.full_name))):
                p = norm.find(f" {variant} ")
                if p >= 0:
                    found[a.id] = min(p, found.get(a.id, p))
        if 0 < len(found) <= 5:
            return sorted(found, key=found.get)
    return ids if 0 < len(ids) <= 5 else []


@dataclass
class StatsQuestion:
    params: dict[str, Any]  # keyword arguments for query()
    annahmen: list[str]  # assumptions to show ("Saison 2026/27 angenommen")


def tournament_season(question: str) -> int | None:
    """'Olympia 2026', 'WM 2025', '2026 Olympics' → start year of the season that ends in that year
    (the 2026 Olympics are in 2025/26). None when no tournament with a year is named."""
    mt = TOURNEY_YEAR.search(question)
    if not mt:
        return None
    return int(mt.group(1) or mt.group(2)) - 1


def understand(
    s: Session,
    question: str,
    scope_ids: list[int] | None = None,
    today: date | None = None,
    history: list[dict] | None = None,
) -> StatsQuestion | None:
    """Rules only (no model): is this a stats question, and with which filters? None = not a stats question.
    `scope_ids`: persons of the chat (person page) used when the question names nobody.
    `history`: previous chat turns; a follow-up ("hat sie …?") takes the persons of the previous answer."""
    from klugscheisser.adds.wiki import countries_in

    if RELATION_CUE.search(question):
        return None  # relations ("Wer spielte zusammen …") are answered from the stations
    leagues = leagues_in(question)
    compare_word = bool(COMPARE.search(question))
    persons: list[int] | None = None
    notes: list[str] = []
    if compare_word and len(leagues) < 2:  # "Stalder vs Müller an Weltmeisterschaften"
        persons, notes = persons_named(s, question, leagues)
    compare = compare_word and (len(leagues) >= 2 or len(persons or []) >= 2)
    if not STAT_CUE.search(question) and not compare:
        return None
    cur = current_season(today)
    sort_by = next((key for rx, key in SORT_CUES if rx.search(question)), None)
    ranking = bool(RANKING.search(question))
    if persons is None:
        persons, notes = persons_named(s, question, leagues)
    if not persons and not ranking and (fu := followup_person_ids(s, question, history)):
        persons = fu
        names = [s.get(m.Athlete, i).full_name for i in fu]
        notes.append(f"Folgefrage: bezogen auf {', '.join(names)} (vorige Antwort)")
    if not persons and not ranking and scope_ids and len(scope_ids) <= 5:
        persons = list(scope_ids)
    if not ranking and not (persons and (QUANT.search(question) or compare)):
        return None  # "Was sagte Stalder nach ihren zwei Toren?" stays a question for the documents
    p: dict[str, Any] = {"person": persons or None, "sort_by": sort_by}
    if leagues:
        p["league"] = leagues
    if re.search(r"playoff", question, re.I):
        p["phase"] = "playoffs"
    nations = countries_in(question)
    tournament = bool(leagues) and all(lg in TOURNAMENTS for lg in leagues)
    if nations and not persons:
        if NATIONAL.search(question) or tournament:  # "für die Schweiz an Olympia": national team rows
            p["team"] = COUNTRY_TEAM.get(nations[0]) or _nation_team_word(nations[0]).title()
            p["team_kind"] = "nationalteam"
        else:  # "beste Schweizerin in der SWHL": nationality of the person
            p["nationality"] = nations[0]
    elif NATIONAL.search(question) and not leagues:
        p["team_kind"] = "nationalteam"
    t_season = tournament_season(question) if leagues else None
    if mt := SEASON_IN_TEXT.search(question):
        p["season"] = mt.group(0)
        p["group"] = "season"
    elif t_season is not None:  # "Olympia 2026" = the tournament in season 2025/26
        p["season"] = t_season
        p["group"] = "career" if ranking else "season"
        notes.append(f"{TOURNEY_YEAR.search(question).group(0)} = Saison {season_label(t_season)}")
    elif CURRENT.search(question) and not CAREER.search(question):  # "aktuell", "this year"
        p["season"] = cur
        p["group"] = "season"
    elif len(leagues) >= 2 and persons:
        p["group"] = "league"  # "Brändli an Olympia vs WM"
    elif CAREER.search(question):
        p["group"] = "career" if ranking or not persons else "league"
    elif tournament or (leagues and persons and not ranking):
        # a league named, no season: the career there ("Spiele in der NCAA" = all her NCAA seasons)
        p["group"] = "career" if ranking or len(persons) != 1 else "league"
    elif len(persons) >= 2:
        p["group"] = "league" if not leagues else "career"
    else:
        p["season"] = cur
        p["group"] = "season"
        notes.append(f"Saison {season_label(cur)} angenommen (laufende Saison)")
    if not p["sort_by"] and ranking:
        p["sort_by"] = "points"
    if not ranking:
        p["sort_by"] = None  # one person's numbers: chronological / per league, not ranked
    p["limit"] = 5 if ranking else 20
    return StatsQuestion(params=p, annahmen=notes)


def try_stats_with_history(s: Session, question: str, athletes: list[m.Athlete], history: list[dict] | None):
    """A stats follow-up ("Und wie viele Punkte hat sie diese Saison?" after "Wer ist die älteste …?"):
    the persons of the previous answer, then the usual `cross.try_stats` (same return shape, with a
    "Folgefrage: bezogen auf …" note). None when there is no follow-up to resolve or it is no stats
    question: the caller then goes on as before."""
    from klugscheisser.answer import cross

    ids = followup_person_ids(s, question, history)
    if not ids or (athletes and [a.id for a in athletes] == ids):
        return None  # nothing to resolve, or the scope already is these persons
    found = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    who = [found[i] for i in ids if i in found]
    cr = cross.try_stats(s, question, who)
    if cr is None:
        return None
    note = f"Folgefrage: bezogen auf {', '.join(a.full_name for a in who)} (vorige Antwort)"
    out = list(cr)
    out[5] = f"{out[5]} · {note}" if out[5] else note
    return tuple(out)


COUNTRY_TEAM = {"SUI": "Switzerland"}
