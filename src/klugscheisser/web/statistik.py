"""v11: the "Statistik" section of the person page (season stats from Elite Prospects and other sources).

Tabs Klub | Nationalteam | Junioren (team_kind), a Playoffs toggle (phase), career totals per league and per team
with points per game, goalies with save % and GAA, and the form line ("Letzte 5 Spiele: 3 T, 4 A, 7 P") from
ep_profiles.data. Empty values show "–", never 0. EP's national "(all)" summary rows ("International (W)") are
left out everywhere, so no game is counted twice. Every number block names its source and data age.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de
from klugscheisser.sports import current_spec
from klugscheisser.stats.queries import SOURCE_LABEL, row_dict, season_rows, totals
from klugscheisser.web.html import e, icon, safe_url

# tab key, label, team kinds in it (college players count as club players: NCAA is their league)
GROUPS = (
    ("club", "Klub", ("club", "college")),
    ("national", "Nationalteam", ("national",)),
    ("youth", "Junioren", ("youth",)),
)
PHASES = (("regular", "Saison"), ("playoffs", "Playoffs"))
SUMMARY_LEAGUES = {"International (W)"}  # EP's national-team "(all)" rows: sums of the tournament rows
SHOWN_SEASONS = 6  # rows before "Alle … Saisons"
GOALIE_FIELDS = ("wins", "losses", "shutouts", "saves", "goals_against", "save_pct", "gaa")
SOURCE_DE = SOURCE_LABEL


def is_summary(league: str | None, team: str | None) -> bool:
    return (league or "") in SUMMARY_LEAGUES or "(all)" in (team or "")


def current_season(now: datetime | None = None) -> int:
    """Start year of the running season (a season starts in August)."""
    now = now or datetime.now(UTC)
    return now.year if now.month >= 8 else now.year - 1


# --- formatting ------------------------------------------------------------------------------------------


def num(v: Any) -> str:
    return "–" if v is None else str(v)


def signed(v: Any) -> str:
    return "–" if v is None else (f"+{v}" if v > 0 else str(v))


def dec(v: Any, places: int = 2) -> str:
    return "–" if v is None else f"{v:.{places}f}".replace(".", ",")


def pct(v: Any) -> str:
    """Save percentage: 0.918 → '91,8 %' (a source that writes 91.8 is taken as is)."""
    if v is None:
        return "–"
    return f"{(v * 100 if v <= 1 else v):.1f} %".replace(".", ",")


# --- data ------------------------------------------------------------------------------------------------


def _source_line(ep: m.EpProfile | None, rows: list[m.SeasonStat]) -> dict[str, Any] | None:
    """'Quelle: Elite Prospects · Stand 04.10.2026' — EP's fetch time is the data age of all its values. None when
    no shown row is from EP (the current sources get their own lines, `_current_sources`)."""
    if ep is not None:
        return {"label": "Elite Prospects", "url": ep.url, "stand": ep.fetched_at}
    eps = [r for r in rows if r.source == "eliteprospects"]
    if not eps:
        return None
    r = max(eps, key=lambda x: x.fetched_at or datetime.min.replace(tzinfo=UTC))
    return {"label": SOURCE_DE.get(r.source, r.source), "url": r.source_url, "stand": r.fetched_at}


def _current_sources(rows: list[m.SeasonStat]) -> list[dict[str, Any]]:
    """Non-EP sources of the shown rows (SIHF season table, counted game sheets), newest Stand each."""
    best: dict[str, m.SeasonStat] = {}
    for r in rows:
        if r.source == "eliteprospects":
            continue
        if r.source not in best or (r.fetched_at and best[r.source].fetched_at < r.fetched_at):
            best[r.source] = r
    return [
        {"label": SOURCE_DE.get(src, src), "url": r.source_url, "stand": r.fetched_at}
        for src, r in best.items()
    ]


def _is_goalie(a: m.Athlete, rows: list[m.SeasonStat]) -> bool:
    if a.position:
        return a.position.upper() == "G"
    return any(getattr(r, f) is not None for r in rows for f in GOALIE_FIELDS)


def _totals(s: Session, aid: int, by: str, phase: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    out = [
        t
        for k in kinds
        for t in totals(s, aid, by=by, phase=phase, team_kind=k)
        if not is_summary(t.get("league"), t.get("team"))
    ]
    return sorted(out, key=lambda t: -(t.get("games") or 0))


def form(ep: m.EpProfile | None) -> list[dict[str, Any]]:
    """Last 5 / last 10 games from ep_profiles.data["form"] (EP's free totals; no per-game stats)."""
    data = ((ep.data if ep else None) or {}).get("form") or {}
    out = []
    for key, n in (("last5", 5), ("last10", 10)):
        f = data.get(key) or {}
        if any(v is not None for v in f.values()):
            out.append({"n": n, **f})
    return out


def stats_data(s: Session, a: m.Athlete, now: datetime | None = None) -> dict[str, Any] | None:
    """Everything the Statistik section shows, or None when no season stats are stored."""
    rows = [r for r in season_rows(s, a.id, phase=None) if not is_summary(r.league, r.team_name)]
    if not rows:
        return None
    ep = s.get(m.EpProfile, a.id)
    goalie = _is_goalie(a, rows)
    panes = []
    for key, label, kinds in GROUPS:
        for phase, phase_label in PHASES:
            mine = [r for r in rows if r.team_kind in kinds and r.phase == phase]
            if not mine:
                continue
            mine.sort(key=lambda r: (-r.season, r.team_name or "", r.league))
            panes.append(
                {
                    "group": key,
                    "label": label,
                    "phase": phase,
                    "phase_label": phase_label,
                    "rows": [row_dict(r) for r in mine],
                    "liga": _totals(s, a.id, "league", phase, kinds),
                    "team": _totals(s, a.id, "team", phase, kinds),
                }
            )
    return {
        "goalie": goalie,
        "panes": panes,
        "form": form(ep),
        "quelle": _source_line(ep, rows),
        "weitere_quellen": _current_sources(rows),
        "current": current_season(now),
        "n_seasons": len({r.season for r in rows}),
    }


# --- HTML ------------------------------------------------------------------------------------------------

SKATER_COLS = (
    ("Sp", "games", num),
    ("T", "goals", num),
    ("A", "assists", num),
    ("P", "points", num),
    ("PIM", "penalty_minutes", num),
    ("+/-", "plus_minus", signed),
)
GOALIE_COLS = (
    ("Sp", "games", num),
    ("W", "wins", num),
    ("L", "losses", num),
    ("SO", "shutouts", num),
    ("Sv%", "save_pct", pct),
    ("GAA", "gaa", dec),
)
TITLES = {
    "Sp": "Spiele",
    "T": "Tore",
    "A": "Assists",
    "P": "Punkte",
    "PIM": "Strafminuten",
    "+/-": "Plus/Minus",
    "W": "Siege",
    "L": "Niederlagen",
    "SO": "Shutouts",
    "Sv%": "Fangquote",
    "GAA": "Gegentore pro Spiel",
    "P/Sp": "Punkte pro Spiel",
}


def _th(label: str) -> str:
    return f"<th scope='col' title='{e(TITLES.get(label, label))}'>{e(label)}</th>"


def source_line(q: dict[str, Any]) -> str:
    label = f"Quelle: {e(q['label'])}"
    if q.get("url"):
        label = f"<a href='{safe_url(q['url'])}' target='_blank' rel='noopener'>{label}{icon('ext', 13)}</a>"
    stand = f" · Stand {e(date_de(q['stand'], True))}" if q.get("stand") else ""
    return f"<p class='st-src'>{label}{stand}</p>"


def form_line(items: list[dict[str, Any]], goalie: bool) -> str:
    """'Letzte 5 Spiele: 3 T, 4 A, 7 P, +/- +2' (goalies: 'Sv% 93,1 %, 6 Gegentore')."""
    parts = []
    for f in items:
        if goalie:
            bits = [
                f"Sv% {pct(f['SVP'])}" if f.get("SVP") is not None else None,
                f"{f['GA']} Gegentore" if f.get("GA") is not None else None,
                f"{f['SV']} Paraden" if f.get("SV") is not None else None,
            ]
        else:
            bits = [
                f"{f[k]} {lbl}" if f.get(k) is not None else None
                for k, lbl in (("G", "T"), ("A", "A"), ("PTS", "P"))
            ]
            if f.get("PM") is not None:
                bits.append(f"+/- {signed(f['PM'])}")
        bits = [b for b in bits if b]
        if bits:
            games = f" ({f['GP']} Sp.)" if f.get("GP") is not None and f["GP"] != f["n"] else ""
            parts.append(f"<span><b>Letzte {f['n']} Spiele{e(games)}:</b> {e(', '.join(bits))}</span>")
    return f"<p class='st-form'>{''.join(parts)}</p>" if parts else ""


def _row_source(r: dict[str, Any]) -> str:
    """Rows not from Elite Prospects (SIHF table, counted game sheets) name their source and Stand in the cell;
    when they replace an EP row, its old values are the tooltip."""
    if r.get("source") in (None, "eliteprospects"):
        return ""
    label = SOURCE_DE.get(r["source"], r["source"])
    stand = f" · Stand {date_de(datetime.fromisoformat(r['stand']), True)}" if r.get("stand") else ""
    title = f" title='{e(r['ersetzt'])}'" if r.get("ersetzt") else ""
    text = f"{e(label)}{e(stand)}"
    if r.get("quelle_link"):
        text = f"<a href='{safe_url(r['quelle_link'])}' target='_blank' rel='noopener'>{text}</a>"
    return f"<span class='rs'{title}>{text}</span>"


def _season_table(rows: list[dict[str, Any]], cols: tuple, current: int) -> str:
    head = "".join(_th(c[0]) for c in cols)
    body = []
    for i, r in enumerate(rows):
        cls = " ".join(
            x for x in ("cur" if r["season"] == current else "", "x" if i >= SHOWN_SEASONS else "") if x
        )
        cells = "".join(f"<td>{e(fmt(r.get(f)))}</td>" for _, f, fmt in cols)
        body.append(
            f"<tr class='{cls}'><td class='s'>{e(r['saison'])}</td><td class='tm'>{e(r['team'] or '–')}"
            f"<span class='lg2'>{e(r['league'])}</span>{_row_source(r)}</td><td class='lg c-lg'>{e(r['league'])}</td>{cells}</tr>"
        )
    more = (
        f"<button type='button' class='more-btn' onclick='statAll(this)'>Alle Saisons anzeigen"
        f"{icon('down', 16)}</button>"
        if len(rows) > SHOWN_SEASONS
        else ""
    )
    return (
        "<div class='st-scroll'><table class='st'><thead><tr><th class='l' scope='col'>Saison</th>"
        "<th class='l' scope='col'>Team</th><th class='l c-lg' scope='col'>Liga</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>{more}"
    )


def _totals_table(rows: list[dict[str, Any]], cols: tuple, by: str, goalie: bool) -> str:
    cols = cols[:5] if goalie else cols  # goalies: no GAA in totals (it needs the time on ice)
    extra = "" if goalie else _th("P/Sp")
    first = "Liga" if by == "league" else "Team"
    body = []
    for t in rows:
        name = t["league"] if by == "league" else t["team"] or "–"
        sub = t.get("league") if by == "team" else None
        seasons = t["von"] if t["von"] == t["bis"] else f"{t['von']}–{t['bis']}"
        cells = "".join(f"<td>{e(fmt(t.get(f)))}</td>" for _, f, fmt in cols)
        rate = "" if goalie else f"<td>{e(dec(t.get('points_per_game')))}</td>"
        sub_html = f"<span class='lg'>{e(sub)}</span>" if sub else ""
        body.append(
            f"<tr><td class='tm'>{e(name)}{sub_html}<span class='lg'>{e(seasons)}</span></td>{cells}{rate}</tr>"
        )
    head = "".join(_th(c[0]) for c in cols)
    return (
        f"<div class='st-scroll'><table class='st st-tot'><thead><tr><th class='l' scope='col'>{first}</th>"
        f"{head}{extra}</tr>"
        f"</thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _pane(p: dict[str, Any], cols: tuple, current: int, goalie: bool, shown: bool) -> str:
    hidden = "" if shown else " hidden"
    teams = (
        f"<details class='st-more'><summary>Total pro Team <span class='chev'>{icon('down', 16)}</span></summary>"
        f"{_totals_table(p['team'], cols, 'team', goalie)}</details>"
        if p["team"]
        else ""
    )
    return (
        f"<div class='st-pane' data-g='{e(p['group'])}' data-p='{e(p['phase'])}'{hidden}>"
        f"{_season_table(p['rows'], cols, current)}"
        f"<h3>Karriere total pro Liga</h3>{_totals_table(p['liga'], cols, 'league', goalie)}{teams}</div>"
    )


def statistik_html(d: dict[str, Any] | None) -> str:
    if not d or not d["panes"]:
        return ""
    cols = GOALIE_COLS if d["goalie"] else SKATER_COLS
    groups = list(dict.fromkeys((p["group"], p["label"]) for p in d["panes"]))
    first = groups[0][0]
    has_playoffs = any(p["phase"] == "playoffs" for p in d["panes"])
    tabs = "".join(
        f"<button type='button' aria-pressed='{'true' if g == first else 'false'}' data-g='{e(g)}' "
        f"onclick=\"statTab(this,'{e(g)}')\">{e(label)}</button>"
        for g, label in groups
    )
    if len(groups) == 1:  # one kind of team only: its name, no tab to press
        tabs = f"<span class='st-one'>{e(groups[0][1])}</span>"
    start = d["panes"][0]  # first group, regular season (or its playoffs when there is no regular season)
    toggle = (
        f"<label class='st-po'><input type='checkbox' onchange='statPhase(this)'"
        f"{' checked' if start['phase'] == 'playoffs' else ''}> Playoffs</label>"
        if has_playoffs
        else ""
    )
    panes = "".join(_pane(p, cols, d["current"], d["goalie"], p is start) for p in d["panes"])
    kind = f" · {current_spec().position_labels_de.get('G', 'Torhüterin')}" if d["goalie"] else ""
    return (
        f"<section class='stats' id='statistik' aria-labelledby='st-h' data-sec='statistik' data-g='{e(first)}' "
        f"data-p='{e(start['phase'])}'><div class='sh'>{icon('stats', 15)}<h2 id='st-h'>Statistik</h2>"
        f"<span class='n'>{d['n_seasons']} Saisons{kind}</span></div>{form_line(d['form'], d['goalie'])}"
        f"<div class='st-bar'><div class='st-tabs' role='group' aria-label='Teamart'>{tabs}</div>{toggle}</div>"
        f"{panes}<p class='st-empty small' hidden>Keine Spiele in dieser Auswahl gespeichert.</p>"
        f"{source_line(d['quelle']) if d.get('quelle') else ''}"
        f"{''.join(source_line(q) for q in d.get('weitere_quellen') or [])}</section>"
    )
