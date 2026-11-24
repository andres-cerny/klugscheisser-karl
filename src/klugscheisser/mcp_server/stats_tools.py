"""v11: MCP `stats` tool and the Elite Prospects part of `get_athlete` (profile fields, statistics, awards).

Every line carries its source: `quelle`, a ready Markdown `quelle_link` and `stand` (data date).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.facts.source_site import cite_label
from klugscheisser.mcp_server.present import HINWEIS, QUELLE
from klugscheisser.stats import search
from klugscheisser.stats.queries import season_label

STATS_HINWEIS = (
    HINWEIS + " Statistik: 'satz' + 'quelle_link' je Zeile übernehmen; leere Werte heissen 'keine Angabe', "
    "nie 0. Ranglisten gelten nur für die Personen in unserer Datenbank; 'hinweise' immer nennen."
)
FACT_SOURCE_DE = {
    "eliteprospects": "Elite Prospects",
    "wikidata": "Wikidata",
    "wikipedia": "Wikipedia",
    "hockeycanada": "Hockey Canada",
}
PROFILE_KINDS = (
    "current_team",
    "position",
    "height_cm",
    "weight_kg",
    "shoots",
    "birth_place",
    "youth_team",
    "contract",
    "agency",
    "draft",
)
MAX_AWARDS = 12


def _split_persons(person: str | list[str] | None) -> list[str] | None:
    if person is None or person == "" or person == []:
        return None
    if isinstance(person, list):
        return [p for p in person if str(p).strip()]
    parts = [
        p.strip() for p in re.split(r"\s*(?:,|;|\bvs\.?\b|\bversus\b|\bgegen\b)\s*", person) if p.strip()
    ]
    return parts or None


def datenstand_stats(stand: str | None, rows: list[dict[str, Any]] | None = None) -> str:
    """'Statistik: SIHF Stand 25.10.2026; Elite Prospects Stand 04.10.2026' — the newest Stand per source."""
    per: dict[str, str] = {}
    for r in rows or []:
        if r.get("quelle") and r.get("stand"):
            per[r["quelle"]] = max(per.get(r["quelle"], ""), r["stand"])
    if per:
        bits = sorted(per.items(), key=lambda kv: kv[1], reverse=True)
        return "Statistik: " + "; ".join(
            f"{q} Stand {datetime.fromisoformat(d).strftime('%d.%m.%Y')}" for q, d in bits
        )
    when = datetime.fromisoformat(stand).strftime("%d.%m.%Y") if stand else "keine Daten"
    return f"Statistik: Stand {when}"


def stats(
    s: Session,
    person: str | list[str] | None = None,
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
) -> dict[str, Any]:
    res = search.query(
        s,
        person=_split_persons(person),
        team=team,
        league=league,
        season=season,
        team_kind=team_kind,
        phase=phase,
        nationality=nationality,
        position=position,
        sort_by=sort_by,
        group=group,
        min_games=min_games,
        limit=limit,
        newest_first=True,
    )
    return {
        "datenstand": datenstand_stats(res.newest, res.rows),
        "quelle": QUELLE,
        "hinweis": STATS_HINWEIS,
        "filter": res.filters,
        "anzahl": len(res.rows),
        "anzahl_gesamt": res.total,
        "hinweise": res.hinweise or None,
        "zeilen": res.rows,
    }


# --- get_athlete: Elite Prospects profile, statistics, awards ------------------------------------------------


def _fact_source(source: str, url: str, stand: str | None, site: str | None = None) -> dict[str, Any]:
    """`site`: citable site + date of a web-type source ("EV Zug, 16.12.2025"), never a bare "web"."""
    name = cite_label(source, url, site, FACT_SOURCE_DE)
    label = f"{name}, Stand {datetime.fromisoformat(stand):%d.%m.%Y}" if stand else name
    if source == "hockeycanada":  # the tournament roster dates the value
        from klugscheisser.collectors.hockeycanada import roster_label

        label = roster_label(url)
    out = {"quelle": name, "quelle_link": f"[{label}]({url})"}
    if stand:
        out["stand"] = stand
    return out


def profile_fields(s: Session, a: m.Athlete, ep_stand: str | None) -> dict[str, Any]:
    """New profile fields (EP first for hockey data), each with source; differing values are named."""
    from klugscheisser.facts.catalog import FACT_KINDS
    from klugscheisser.facts.queries import facts_for

    out: dict[str, Any] = {}
    for kind in PROFILE_KINDS:
        rows = facts_for(s, [a.id], kind)
        if not rows:
            continue
        r = rows[0]
        entry: dict[str, Any] = {
            "feld": FACT_KINDS.get(kind, {}).get("de", kind),
            "wert": r.value_text,
            **_fact_source(
                r.source, r.source_url, ep_stand if r.source == "eliteprospects" else None, r.site
            ),
        }
        if kind == "current_team":
            v = r.value or {}
            for src, dst in (
                ("team", "team"),
                ("league", "liga"),
                ("season", "saison"),
                ("jersey", "nummer"),
                ("status", "status"),
            ):
                if v.get(src) not in (None, ""):
                    entry[dst] = v[src]
        if r.conflicts:
            c = r.conflicts[0]
            name = cite_label(c.source, c.source_url, c.site, FACT_SOURCE_DE)
            if c.source == "hockeycanada":
                from klugscheisser.collectors.hockeycanada import roster_label

                name = roster_label(c.source_url)
            entry["abweichend"] = f"{c.value_text} ({name})"
        out[kind] = entry
    return out


def _season_text(v: Any) -> str | None:
    if isinstance(v, int):
        return season_label(v)
    return str(v) if v not in (None, "") else None


def awards(s: Session, a: m.Athlete, ep_stand: str | None) -> dict[str, Any] | None:
    """Medals first, then awards newest first; deduplicated by text and season."""
    facts = list(
        s.scalars(
            select(m.Fact).where(
                m.Fact.athlete_id == a.id, m.Fact.kind.in_(("medal", "award")), m.Fact.status != "unconfirmed"
            )
        )
    )

    def season_key(f: m.Fact) -> str:
        v = f.value or {}
        return str(v.get("season") or v.get("year") or f.season or "")

    facts.sort(key=lambda f: (f.kind != "medal", f.source != "eliteprospects"))
    facts.sort(key=season_key, reverse=True)
    facts.sort(key=lambda f: f.kind != "medal")
    seen, items = set(), []
    for f in facts:
        v = f.value or {}
        key = (f.kind, (v.get("text") or f.value_text).casefold(), season_key(f))
        if key in seen:
            continue
        seen.add(key)
        item = {
            "art": "Medaille" if f.kind == "medal" else "Auszeichnung",
            "text": v.get("text") or f.value_text,
            "saison": _season_text(v.get("season") or f.season)
            or (str(v["year"]) if v.get("year") else None),
            "nummer": v.get("number"),
            "liga": v.get("league"),
            **_fact_source(f.source, f.source_url, ep_stand if f.source == "eliteprospects" else None),
        }
        items.append({k: x for k, x in item.items() if x not in (None, "")})
    if not items:
        return None
    return {
        "anzahl": len(items),
        "liste": items[:MAX_AWARDS],
        "weitere": max(0, len(items) - MAX_AWARDS) or None,
    }


FORM_LABELS = (
    ("G", "Tore"),
    ("A", "Assists"),
    ("PTS", "Punkte"),
    ("PM", "Plus/Minus"),
    ("GA", "Gegentore"),
    ("SV", "Paraden"),
)


def form_block(ep: m.EpProfile | None) -> dict[str, Any] | None:
    form = ((ep.data or {}).get("form") or {}) if ep else {}
    if not form:
        return None
    out: dict[str, Any] = {}
    for key, label in (("last5", "letzte_5"), ("last10", "letzte_10")):
        f = form.get(key)
        if not f:
            continue
        n = key.removeprefix("last")
        bits = [f"{f[k]} {lab}" for k, lab in FORM_LABELS if f.get(k) is not None]
        if f.get("SVP") is not None:
            bits.append(f"Fangquote {f['SVP']}")
        out[label] = {**f, "satz": f"Letzte {n} Spiele: " + (", ".join(bits) or "keine Werte") + "."}
    if not out:
        return None
    stand = ep.fetched_at.date().isoformat() if ep.fetched_at else None
    return {**out, **search.source_fields("eliteprospects", ep.url, stand)}


COMPACT_DROP = {"person", "person_id", "nation", "position", "zitat", "zeile_prefix", "url", "quelle"}


def _compact(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k not in COMPACT_DROP} for r in rows]


def statistik(s: Session, a: m.Athlete, ep: m.EpProfile | None) -> dict[str, Any] | None:
    """Current season (regular season + playoffs), career totals per league (most games first), form."""
    cur = search.current_season()
    season_rows = search.query(s, person=[a.id], season=cur, phase="alle").rows
    label = f"saison_{season_label(cur).replace('/', '_')}"
    out: dict[str, Any] = {}
    if not season_rows:
        season_rows = search.query(s, person=[a.id], season=cur - 1, phase="alle").rows
        label = f"saison_{season_label(cur - 1).replace('/', '_')}"
    if season_rows:
        out[label] = _compact(season_rows)
    per_league = search.query(s, person=[a.id], group="league", limit=100)
    if per_league.rows:
        out["karriere_pro_liga"] = _compact(sorted(per_league.rows, key=lambda r: -(r.get("games") or 0))[:8])
        if len(per_league.rows) > 8:
            out["weitere_ligen"] = len(per_league.rows) - 8
        if per_league.hinweise:
            out["hinweise"] = [h for h in per_league.hinweise if "Rangliste" not in h] or None
    if form := form_block(ep):
        out["form"] = form
    if not out:
        return None
    out["details"] = "Mehr mit stats(person=…, group='season'|'team'|'league', league=…, season=…)."
    return {k: v for k, v in out.items() if v is not None}


def ep_block(s: Session, a: m.Athlete) -> dict[str, Any]:
    ep = s.get(m.EpProfile, a.id)
    ep_stand = ep.fetched_at.date().isoformat() if ep and ep.fetched_at else None
    out: dict[str, Any] = {}
    if prof := profile_fields(s, a, ep_stand):
        out["profil"] = prof
    if st := statistik(s, a, ep):
        out["statistik"] = st
    if aw := awards(s, a, ep_stand):
        out["auszeichnungen"] = aw
    if ep is not None:
        out["elite_prospects"] = search.source_fields("eliteprospects", ep.url, ep_stand)
    return out
