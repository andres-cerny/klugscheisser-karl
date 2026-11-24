"""Data for the "Steckbrief" profile card on the person page (v6 step 4).

Only stored, sourced values: every key fact carries at least one source link; values without a link are left
out (no guesses). Facts come from the fact layer (facts.queries), the number from the newest roster, the
career from career stints, the intro verbatim from the stored Wikipedia article.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds.wiki import COUNTRIES
from klugscheisser.collectors.profiles.wikipedia import home_languages
from klugscheisser.config import get_settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de
from klugscheisser.facts.queries import FactRow, StintRow, facts_for, stints_for
from klugscheisser.facts.source_site import item_info, needs_site, row_site, site_label
from klugscheisser.facts.teams import team_key
from klugscheisser.mcp_server.present import age
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec
from klugscheisser.web import photo

INTRO_MAX = 700
SOURCE_DE = {
    "wikidata": "Wikidata",
    "college": "College-Profil",
    "federation": "Verband",
    "wikimedia": "Wikimedia Commons",
    "eliteprospects": "Elite Prospects",
    "hockeycanada": "Hockey Canada",
    "sihf": "sihf.ch",
}
SIHF_SITE = {"SIHF": "sihf.ch"}  # affiliations name their source in words ("SIHF", "Wikidata" …)
KIND_DE = {"national": "Nationalteam", "college": "College", "youth": "Nachwuchs"}


def source_label(source: str, url: str | None) -> str:
    if source == "wikipedia":
        host = urlparse(url or "").hostname or ""
        lang = host.split(".")[0] if host.endswith("wikipedia.org") else ""
        return f"Wikipedia {lang}".strip()
    if source == "hockeycanada":  # 'Hockey Canada, Kader IIHF Frauen-WM 2025': the tournament dates the value
        from klugscheisser.collectors.hockeycanada import roster_label

        return roster_label(url)
    if source not in SOURCE_DE and needs_site(source):  # web-type: the site, never a bare "Web"/"News"
        return site_label(source, url) or "Webseite"
    return SOURCE_DE.get(source, source or "Quelle")


def _src(
    source: str, url: str | None, quote: str | None = None, site: str | None = None
) -> dict[str, Any] | None:
    """`site`: the citable site + date of a web-type source ("EV Zug, 16.12.2025"); else the domain."""
    if not url:
        return None
    if site is None and needs_site(source):
        site = site_label(source, url)
    return {"label": site or source_label(source, url), "url": url, "quote": quote}


def _row_src(r: FactRow | StintRow) -> dict[str, Any] | None:
    return _src(r.source, r.source_url, r.quote, r.site)


def _model_src(s: Session, st: m.CareerStint | m.Fact) -> dict[str, Any] | None:
    item = s.get(m.Item, st.source_item_id) if st.source_item_id else None
    site = (
        site_label(st.source, st.source_url, item.source_name, item.published_at, item.platform)
        if item is not None
        else None
    )
    return _src(st.source, st.source_url, st.quote, site)


def _fact(label: str, value: str, sources: list[dict | None], note: str | None = None, **kw) -> dict | None:
    sources = [x for x in sources if x]
    if not value or not sources:
        return None
    return {"label": label, "value": value, "sources": sources, "note": note, "abweichend": [], **kw}


def _with_conflicts(item: dict | None, row: FactRow, text=lambda r: r.value_text) -> dict | None:
    if item is not None:
        for c in row.conflicts:
            if c.source_url:
                item["abweichend"].append({"value": text(c), "source": _row_src(c)})
    return item


def _one(s: Session, aid: int, kind: str) -> FactRow | None:
    rows = facts_for(s, [aid], kind)
    return rows[0] if rows else None


def _all(s: Session, aid: int, kind: str) -> list[m.Fact]:
    """All shown facts of a multi-valued kind (languages, college), one per value, best source first."""
    facts = s.scalars(
        select(m.Fact)
        .where(m.Fact.athlete_id == aid, m.Fact.kind == kind, m.Fact.status != "unconfirmed")
        .order_by(m.Fact.source != "wikidata", m.Fact.confidence.desc(), m.Fact.id)
    ).all()
    seen: dict[str, m.Fact] = {}
    for f in facts:
        if f.source_url:
            seen.setdefault(f.value_text.strip().lower(), f)
    return list(seen.values())


# --- key facts ------------------------------------------------------------------------------------------


def _number(s: Session, a: m.Athlete) -> dict | None:
    """v8: national-team number (newest roster with a number) and club number (newest fact), both sourced:
    "#7 (Schweiz) · #7 (EVZ)". One of them alone when only one is known or both say the same team."""
    parts: list[str] = []
    sources: list[dict | None] = []
    row = s.execute(
        select(m.EventEntry, m.Event)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.athlete_id == a.id, m.EventEntry.jersey_number.is_not(None))
        .order_by(m.Event.starts_at.desc().nulls_last(), m.EventEntry.fetched_at.desc())
        .limit(1)
    ).first()
    roster_team = None
    if row is not None:
        en, ev = row
        url = en.source_url or ev.source_url
        label = f"Kader {ev.name}" + (" (provisorisch)" if en.list_type == "provisional" else "")
        roster_team = en.team.name if en.team else None
        parts.append(f"#{en.jersey_number}" + (f" ({roster_team})" if roster_team else ""))
        sources.append({"label": label, "url": url, "quote": None} if url else None)
    f = s.scalar(
        select(m.Fact)
        .where(m.Fact.athlete_id == a.id, m.Fact.kind == "jersey_number", m.Fact.status != "unconfirmed")
        .order_by(m.Fact.season.desc().nulls_last(), m.Fact.id.desc())
        .limit(1)
    )
    if f is not None and (f.value or {}).get("number"):
        team = (f.value or {}).get("team")
        national = team and roster_team and (roster_team in team or "national" in team.lower())
        if not national or not parts:
            parts.append(f"#{f.value['number']}" + (f" ({team})" if team else ""))
            sources.append(_model_src(s, f))
    if not parts:
        return None
    return _fact("Nummer", " · ".join(parts), sources)


def _position(s: Session, a: m.Athlete) -> dict | None:
    if row := _one(s, a.id, "position"):
        return _with_conflicts(
            _fact("Position", _position_text(row), [_row_src(row)]), row, text=_position_text
        )
    en = s.scalar(
        select(m.EventEntry)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.athlete_id == a.id, m.EventEntry.role.in_(("G", "D", "F")))
        .order_by(m.Event.starts_at.desc().nulls_last())
        .limit(1)
    )
    if en is None:
        return None
    label = current_spec().position_labels_de.get(en.role or "")
    ev = s.get(m.Event, en.event_id)
    url = en.source_url or (ev.source_url if ev else None)
    return _fact(
        "Position", label or "", [{"label": f"Kader {ev.name}", "url": url, "quote": None} if url else None]
    )


def _birth_text(r: FactRow) -> str:
    v = r.value or {}
    if v.get("date"):
        born = date.fromisoformat(v["date"])
        return f"{age(born)} Jahre (geb. {born:%d.%m.%Y})"
    if v.get("year"):
        return f"Jahrgang {v['year']}"
    return r.value_text


def _birth(s: Session, a: m.Athlete) -> dict | None:
    row = _one(s, a.id, "birth_date")
    if row is None:
        return None
    return _with_conflicts(_fact("Alter", _birth_text(row), [_row_src(row)]), row, text=_birth_text)


def _nation(s: Session, a: m.Athlete) -> dict | None:
    if not a.nationality:
        return None
    name = COUNTRIES.get(a.nationality, (None, a.nationality))[1]
    row = s.execute(
        select(m.EventEntry, m.Event)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .join(m.Team, m.Team.id == m.EventEntry.team_id)
        .where(m.EventEntry.athlete_id == a.id, m.Team.kind == "national", m.Team.country == a.nationality)
        .order_by(m.Event.starts_at.desc().nulls_last())
        .limit(1)
    ).first()
    sources = []
    if row is not None and (url := row[0].source_url or row[1].source_url):
        sources.append({"label": f"Kader {row[1].name}", "url": url, "quote": None})
    elif a.wikidata_qid:
        sources.append(_src("wikidata", f"https://www.wikidata.org/wiki/{a.wikidata_qid}"))
    return _fact("Nation", name, sources)


def _simple(s: Session, a: m.Athlete, kind: str, label: str, text=lambda r: r.value_text) -> dict | None:
    row = _one(s, a.id, kind)
    if row is None:
        return None
    return _with_conflicts(_fact(label, text(row), [_row_src(row)]), row, text=text)


# --- v11: Elite Prospects profile fields ----------------------------------------------------------------

POSITION_DETAIL_DE = {
    "C": "Center",
    "LW": "linker Flügel",
    "RW": "rechter Flügel",
    "W": "Flügel",
    "F": "Stürmerin",
    "D": "Verteidigerin",
    "LD": "linke Verteidigerin",
    "RD": "rechte Verteidigerin",
    "G": "Torhüterin",
}
HAND_DE = {"L": "links", "R": "rechts"}
TEAM_STATUS_DE = {
    "loan": "ausgeliehen",
    "on loan": "ausgeliehen",
    "injured": "verletzt",
    "retired": "Karriere beendet",
    "unsigned": "ohne Vertrag",
    "free agent": "ohne Vertrag",
    "inactive": "inaktiv",
}
QUIET_STATUS = {"", "active", "rostered", "aktiv", "signed"}


def _season(y: Any) -> str | None:
    return f"{int(y)}/{(int(y) + 1) % 100:02d}" if str(y or "").isdigit() else None


def _with_unit(unit: str):
    """'63' → '63 kg'; a text that already has its unit stays."""
    return lambda r: f"{r.value_text} {unit}" if r.value_text.strip().isdigit() else r.value_text


def _position_text(r: FactRow) -> str:
    """'Stürmerin (Center)': the role plus EP's detailed position when the text does not name it yet."""
    detail = (r.value or {}).get("detail")
    details = detail if isinstance(detail, list) else [detail] if detail else []
    extra = [POSITION_DETAIL_DE.get(str(d).upper(), str(d)) for d in details]
    extra = [x for x in extra if x and x.lower() not in r.value_text.lower()]
    return r.value_text + (f" ({', '.join(extra)})" if extra else "")


def _hand_text(r: FactRow) -> str:
    return HAND_DE.get(r.value_text.strip().upper(), r.value_text)


def _stand_note(stand: datetime | None) -> str | None:
    return f"Stand {date_de(stand, True)}" if stand else None


def _current_team(s: Session, a: m.Athlete, stand: datetime | None) -> dict | None:
    """v11: Elite Prospects' current team (team, league, jersey, status, season) wins over career stints; a
    differing current team from the stints stays visible as "abweichend"."""
    row = _one(s, a.id, "current_team")
    stints = _club(s, a)
    if row is None or row.source != "eliteprospects":
        return stints
    v = row.value or {}
    team = v.get("team") or row.value_text
    value = team + (f" ({v['league']})" if v.get("league") else "")
    if v.get("jersey"):
        value += f" · #{v['jersey']}"
    status = str(v.get("status") or "").strip()
    if status.lower() not in QUIET_STATUS:
        value += f" · {TEAM_STATUS_DE.get(status.lower(), status)}"
    bits = [f"Saison {_season(v['season'])}" if _season(v.get("season")) else None, _stand_note(stand)]
    item = _with_conflicts(
        _fact("Aktueller Verein", value, [_row_src(row)], " · ".join(b for b in bits if b) or None), row
    )
    # another source's open stint of the newest start season (e.g. a club release about her next team) stays
    # visible when its team differs; older open stints are often only "at the time of writing"
    ranked = _open_stints(s, a)
    newest = max((x.season_from or 0 for x in ranked), default=None)
    st = next(
        (
            x
            for x in ranked
            if x.season_from == newest
            and x.source_url
            and team_key(x.team_name) != team_key(team)
            and not _same_club(x, [(None, _loose_key(team), _words(team))])
        ),
        None,
    )
    if item is not None and st is not None:
        value = st.team_name + (f" ({st.league})" if st.league else "")
        item["abweichend"].append({"value": value, "source": _model_src(s, st)})
    return item


def _contract(s: Session, a: m.Athlete, stand: datetime | None) -> dict | None:
    row = _one(s, a.id, "contract")
    if row is None:
        return None
    to = _season((row.value or {}).get("season_to"))
    item = _fact("Vertrag", f"bis {to}" if to else row.value_text, [_row_src(row)], _stand_note(stand))
    return _with_conflicts(item, row)


def _draft_text(r: FactRow) -> str:
    if r.value_text:
        return r.value_text
    v = r.value or {}
    bits = [str(v.get("year") or ""), v.get("league") or "", f"Runde {v['round']}" if v.get("round") else ""]
    text = " ".join(b for b in bits if b)
    return text + (f" durch {v['team']}" if v.get("team") else "")


def ep_profile(s: Session, a: m.Athlete) -> dict[str, Any] | None:
    """Elite Prospects page and its fetch time (= data age of every EP value on the page)."""
    ep = s.get(m.EpProfile, a.id)
    if ep is None:
        return None
    return {"url": ep.url, "stand": ep.fetched_at, "data": ep.data or {}}


def wikipedia_url(s: Session, a: m.Athlete) -> str | None:
    """Her Wikipedia article as stored (items, else a fact's source): German first, then English, then the
    home language."""
    order = ["de", "en", *home_languages(a.nationality)]
    urls = list(
        s.scalars(
            select(m.Item.url)
            .join(m.ItemAthlete)
            .where(m.ItemAthlete.athlete_id == a.id, m.Item.platform == "wikipedia")
            .order_by(m.Item.id.desc())
        )
    )
    urls += list(
        s.scalars(
            select(m.Fact.source_url)
            .where(m.Fact.athlete_id == a.id, m.Fact.source == "wikipedia", m.Fact.status != "unconfirmed")
            .distinct()
        )
    )

    def lang(url: str) -> str:
        host = urlparse(url).hostname or ""
        return host.split(".")[0] if host.endswith(".wikipedia.org") else ""

    urls = [u for u in urls if u and lang(u) and "/wiki/" in u]
    if not urls:
        return None
    return min(urls, key=lambda u: order.index(lang(u)) if lang(u) in order else len(order))


def links(s: Session, a: m.Athlete) -> dict[str, str]:
    """v11 link row at the top of the person page: Instagram · Wikipedia · Elite Prospects (only known ones)."""
    out = {}
    handles = sorted(
        (h for h in a.handles if h.platform == "instagram" and h.usable), key=lambda h: h.status != "verified"
    )
    if handles:
        out["instagram"] = f"https://www.instagram.com/{handles[0].handle}/"
    if url := wikipedia_url(s, a):
        out["wikipedia"] = url
    if a.eliteprospects_id:
        out["eliteprospects"] = f"https://www.eliteprospects.com/player/{a.eliteprospects_id}"
    return out


def recording(s: Session, a: m.Athlete) -> str | None:
    """Source of a stored real recording of her name (Wikimedia / Elite Prospects), else None."""
    row = s.get(m.NamePronunciation, a.id)
    if row is not None and row.audio and row.source in ("wikimedia", "eliteprospects"):
        return row.source
    return None


def _stand(s: Session, st: m.CareerStint) -> str:
    when: datetime | None = None
    if st.source_item_id and (item := s.get(m.Item, st.source_item_id)) is not None:
        when = item.published_at or item.scraped_at
    return date_de(when or st.created_at, True)


# 2026-11-01: on equal start season the season-based sources beat article/wiki stations
CLUB_SOURCE_RANK = {"eliteprospects": 0, "sihf": 1}


def _plays_now(s: Session, aid: int) -> list[tuple[str | None, str]]:
    """(EP team id, loose name key) of the club/college teams with games in the running season's statistics."""
    from klugscheisser.web.statistik import current_season

    rows = s.execute(
        select(m.SeasonStat.ep_team_id, m.SeasonStat.team_name).where(
            m.SeasonStat.athlete_id == aid,
            m.SeasonStat.season == current_season(),
            m.SeasonStat.team_kind.in_(("club", "college")),
            m.SeasonStat.games > 0,
        )
    ).all()
    return [(ep_id, _loose_key(name or "")) for ep_id, name in rows]


def _same_club(st: m.CareerStint, teams: list[tuple[str | None, str]]) -> bool:
    """The stint's team is one of `teams` (EP team id, else the loose name key: "EVZ Women's Team" = "EV Zug")."""
    k1 = _loose_key(st.team_name)
    w1 = _words(st.team_name)
    for ep_id, k2, *rest in teams:
        if st.ep_team_id and ep_id:
            if st.ep_team_id == ep_id:
                return True
        elif len(k1) >= 3 and len(k2) >= 3 and (k1 == k2 or k1 in k2 or k2 in k1):
            return True
        elif rest and len(w1) >= 2 and len(rest[0]) >= 2 and (w1 <= rest[0] or rest[0] <= w1):
            return True  # word order: "Victoire de Montréal" = "Montréal Victoire"
    return False


def _words(name: str | None) -> set[str]:
    return {w for w in team_key(name or "").split() if len(w) > 2}


def _open_stints(s: Session, a: m.Athlete) -> list[m.CareerStint]:
    """Open club stints, best first: a team with games in the running season (SIHF table, EP …), then the newest
    start season, then confirmed, then Elite Prospects / SIHF, Wikidata, Wikipedia/web."""
    stints = s.scalars(
        select(m.CareerStint).where(
            m.CareerStint.athlete_id == a.id,
            m.CareerStint.kind.in_(("club", "college")),
            m.CareerStint.season_to.is_(None),
            m.CareerStint.season_from.is_not(None),
            m.CareerStint.status != "unconfirmed",
        )
    ).all()
    now = _plays_now(s, a.id) if stints else []
    return sorted(
        stints,
        key=lambda x: (
            not _same_club(x, now),
            -(x.season_from or 0),
            x.status != "confirmed",
            CLUB_SOURCE_RANK.get(x.source, 2),
            x.source != "wikidata",  # as before: Wikidata's statement before Wikipedia/web text
            x.id,
        ),
    )


def _club(s: Session, a: m.Athlete) -> dict | None:
    """Open club stint, best first: a team with games in the running season (SIHF table, EP …), then the newest
    start season, then confirmed, then Elite Prospects / SIHF, Wikidata, Wikipedia/web. Another open
    stint at a different team stays visible as "abweichend"."""
    stints = _open_stints(s, a)
    if stints:
        st = stints[0]
        value = st.team_name + (f" ({st.league})" if st.league else "")
        note = f"laut Quelle, Stand {_stand(s, st)}"
        item = _fact("Aktueller Verein", value, [_model_src(s, st)], note)
        if item is not None:
            same = [(st.ep_team_id, _loose_key(st.team_name), _words(st.team_name))]
            for other in stints[1:]:
                if (
                    team_key(other.team_name) != team_key(st.team_name)
                    and not _same_club(other, same)
                    and other.source_url
                ):
                    item["abweichend"].append(
                        {
                            "value": other.team_name,
                            "source": _model_src(s, other),
                        }
                    )
        return item
    affs = [x for x in a.affiliations if x.team.kind == "club" and x.source_url and x.valid_to is None]
    if not affs:
        return None
    aff = sorted(affs, key=lambda x: (x.season or "", x.valid_from or date.min), reverse=True)[0]
    stand = f"Saison {aff.season}" if aff.season else "ohne Saisonangabe"
    return _fact(
        "Aktueller Verein",
        aff.team.name,
        [
            {
                "label": SIHF_SITE.get(aff.source or "", aff.source or "Quelle"),
                "url": aff.source_url,
                "quote": None,
            }
        ],
        f"laut Quelle, Stand {stand}",
    )


def _multi(s: Session, a: m.Athlete, kinds: tuple[str, ...], label: str) -> dict | None:
    facts = [f for k in kinds for f in _all(s, a.id, k)]
    if not facts:
        return None
    parts = [{"value": f.value_text, "source": _model_src(s, f)} for f in facts]
    return {
        "label": label,
        "value": ", ".join(p["value"] for p in parts),
        "sources": [],
        "parts": parts,
        "note": None,
        "abweichend": [],
    }


def key_facts(s: Session, a: m.Athlete, stand: datetime | None = None) -> list[dict]:
    """`stand`: fetch time of her Elite Prospects page (data age of the time-sensitive EP values)."""
    pos = _one(s, a.id, "position")
    goalie = (a.position or "").upper() == "G" or bool(
        pos and pos.value_text.strip().upper() in ("G", "TORHÜTERIN")
    )
    items = [
        _number(s, a),
        _position(s, a),
        _birth(s, a),
        _simple(s, a, "birth_place", "Geburtsort"),
        _nation(s, a),
        _simple(s, a, "height_cm", "Grösse", _with_unit("cm")),
        _simple(s, a, "weight_kg", "Gewicht", _with_unit("kg")),
        _simple(s, a, "shoots", "Fanghand" if goalie else "Schusshand", _hand_text),
        _current_team(s, a, stand),
        _contract(s, a, stand),
        _simple(s, a, "youth_team", "Juniorenklub"),
        _simple(s, a, "draft", "Draft", _draft_text),
        _simple(s, a, "agency", "Agentur"),
        _multi(s, a, ("family_in_sport",), f"Familie im {wording().sport_de}"),
        _multi(s, a, ("languages",), "Sprachen"),
        _multi(s, a, ("college", "studies"), "College / Studium"),
    ]
    return [x for x in items if x]


# --- career ---------------------------------------------------------------------------------------------


def _stint_key(st: StintRow) -> tuple:
    return (st.team_id or team_key(st.team_name), st.season_from, st.season_to)


def _career_rows(stints: list[StintRow], keyed: dict | None = None) -> list[dict]:
    """One row per team and seasons; the same stint from several sources becomes one row with several links.
    `keyed` (optional) receives the rows by stint key."""
    rows: dict[tuple, dict] = {} if keyed is None else keyed
    for st in stints:
        if not st.source_url:
            continue
        key = _stint_key(st)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "saison": st.seasons,
                "saison_bekannt": st.season_known,
                "team": st.team_name,
                "team_id": st.team_id,
                "art": KIND_DE.get(st.kind),
                "liga": st.league,
                "status": st.status,
                "sources": [],
            }
        row["liga"] = row["liga"] or st.league
        src = _row_src(st)
        if src and src["url"] not in {x["url"] for x in row["sources"]}:
            row["sources"].append(src)
    return list(rows.values())


def _loose_key(name: str) -> str:
    """team_key without spaces and one-letter rests ("EVZ Women’s Team" -> "evz", "EV Zug" -> "evzug")."""
    return "".join(w for w in team_key(name).split() if len(w) > 1)


def _same_stint(ep: StintRow, other: StintRow, loose: bool = False) -> bool:
    """Another source's stint describes this Elite Prospects stint: same team (id or name key, one name may be
    longer: "EV Zug" / "EVZ Women's Team" only via team_id) and overlapping seasons (or no seasons given)."""
    if ep.team_id and other.team_id:
        same = ep.team_id == other.team_id
    elif loose:  # SIHF list: spaces dropped, "EVZ" (from "EVZ Women's Team") = "EV Zug"
        k1, k2 = _loose_key(ep.team_name), _loose_key(other.team_name)
        same = len(k1) >= 3 and len(k2) >= 3 and (k1 == k2 or k1 in k2 or k2 in k1)
    else:
        k1, k2 = team_key(ep.team_name), team_key(other.team_name)
        same = bool(k1 and k2) and (k1 == k2 or k1 in k2 or k2 in k1)
    if not same:
        return False
    if other.season_from is None and other.season_to is None:
        return True
    lo1, hi1 = ep.season_from or 0, ep.season_to if ep.season_to is not None else 9999
    lo2 = other.season_from if other.season_from is not None else 0
    hi2 = other.season_to if other.season_to is not None else 9999
    return lo1 <= hi2 and lo2 <= hi1


# season-based career lists, best first: Elite Prospects; without it the SIHF league tables (2026-11-01)
LIST_SOURCES = ("eliteprospects", "sihf")

# --- one row per station for persons without EP (2026-11-01) -----------------------------------------------
# Whose seasons win when several sources describe the same station: season lists, then official/club pages,
# then Wikidata, Wikipedia, and last web/media articles (often "1999–2015" with the end year as a season).
OFFICIAL_STINT_SOURCES = {
    "hockeycanada",
    "usahockey",
    "swehockey",
    "jihf",
    "ceskyhokej",
    "leijonat_liiga",
    "federation",
    "college",
    "club",
    "oehv",
    "deb",
    "diu",
}
_AGE_RX = re.compile(r"\bu ?-?(\d{2})\b|under[- ]?(\d{2})|junior\w*|youth|nachwuchs|girls|boys", re.I)
_MEN_RX = re.compile(r"\b(men|mens|men's|herren|männer|maenner)\b", re.I)


def _station_rank(source: str) -> int:
    if source in LIST_SOURCES:
        return 0
    if source in OFFICIAL_STINT_SOURCES:
        return 1
    if source == "wikidata":
        return 2
    if source.startswith("wikipedia") or source == "wiki":
        return 3
    return 4


def _precision(st: StintRow) -> int:
    return (st.season_from is not None) + (st.season_to is not None)


def _team_variant(name: str) -> tuple[frozenset[str], bool]:
    """Age group ("u18", "junior") and men's marker of a team name: "Switzerland U18" ≠ "Switzerland" ≠
    "Switzerland (men)", although the loose keys contain each other."""
    ages = set()
    for mt in _AGE_RX.finditer(name or ""):
        num = mt.group(1) or mt.group(2)
        ages.add(f"u{num}" if num else mt.group(0).lower()[:5])
    return frozenset(ages), bool(_MEN_RX.search(name or ""))


def _same_station(a: StintRow, b: StintRow) -> bool:
    """Two sources describe the same station: same kind and role, same team (EP id, team id, else the loose name
    key, one may contain the other: "Calgary" / "Calgary Inferno"), and seasons that overlap, differ by at most
    one season at both ends, or are missing on one side."""
    if a.kind != b.kind or a.role != b.role:
        return False
    if a.ep_team_id and b.ep_team_id:
        same = a.ep_team_id == b.ep_team_id
    elif a.team_id and b.team_id:
        same = a.team_id == b.team_id
    else:
        k1, k2 = _loose_key(a.team_name), _loose_key(b.team_name)
        same = (
            len(k1) >= 3
            and len(k2) >= 3
            and (k1 == k2 or k1 in k2 or k2 in k1)
            and _team_variant(a.team_name) == _team_variant(b.team_name)
        )
    if not same:
        return False
    if not a.season_known or not b.season_known:
        return True
    lo1, hi1 = a.season_from or 0, a.season_to if a.season_to is not None else 9999
    lo2, hi2 = b.season_from or 0, b.season_to if b.season_to is not None else 9999
    if lo1 <= hi2 and lo2 <= hi1:
        return True
    return (
        None not in (a.season_from, b.season_from, a.season_to, b.season_to)
        and abs(a.season_from - b.season_from) <= 1
        and abs(a.season_to - b.season_to) <= 1
    )


def _stations(stints: list[StintRow]) -> list[tuple[StintRow, list[StintRow]]]:
    """Group stints that describe one station (each must match every other one of its group, so a vague
    "SC Bern seit 2010" cannot chain two separate Bern stints together). The row shown: the most precise
    seasons, then the better source (season list > official > Wikidata > Wikipedia > web/media)."""
    groups: list[list[StintRow]] = []
    for st in stints:
        if not st.source_url:
            continue
        group = next((g for g in groups if all(_same_station(x, st) for x in g)), None)
        if group is None:
            groups.append([st])
        else:
            group.append(st)
    out = []
    for g in groups:
        best = min(range(len(g)), key=lambda i: (-_precision(g[i]), _station_rank(g[i].source), i))
        out.append((g[best], g[:best] + g[best + 1 :]))
    return out


def _station_rows(
    stints: list[StintRow],
    fixed: list[StintRow] | None = None,
    keyed: dict | None = None,
    order: dict[int, int] | None = None,
) -> list[dict]:
    """Career rows with one row per station; `fixed` stints (the SIHF list) stay rows of their own. `order`:
    position of each stint (by id()) in stints_for's order; default: the order given."""
    keyed = {} if keyed is None else keyed
    stations = [(x, []) for x in fixed or []] + _stations(stints)
    order = order or {id(x): i for i, x in enumerate([*(fixed or []), *stints])}
    stations.sort(key=lambda p: order[id(p[0])])
    rows = _career_rows([best for best, _ in stations], keyed)
    for best, others in stations:
        row = keyed.get(_stint_key(best))
        if row is None:
            continue
        for other in others:
            row["liga"] = row["liga"] or other.league
            src = _row_src(other)
            if src and src["url"] not in {x["url"] for x in row["sources"]}:
                row["sources"].append(src)
    return rows


def career(s: Session, a: m.Athlete) -> tuple[list[dict], list[dict]]:
    """Shown rows and the "weitere Angaben" rows. v11: with Elite Prospects stints, EP's season-by-season career
    is the list; other sources' stints of the same team become extra source links on that row, the rest goes
    to "weitere Angaben" (Wikipedia/web stints are vaguer and would repeat every team).
    2026-11-01: without EP stints, the SIHF stints (seasons from the league tables) are the list the same way,
    but stations of other sources that match no SIHF row stay shown (youth clubs, leagues abroad)."""
    stints = stints_for(s, [a.id], include_unconfirmed=True)
    base_source = next((src for src in LIST_SOURCES if any(x.source == src for x in stints)), None)
    if base_source is None:  # 2026-11-01: one row per station, all sources as links
        shown = _station_rows([x for x in stints if x.status != "unconfirmed"])
        unconfirmed = _station_rows([x for x in stints if x.status == "unconfirmed"])
        return shown, unconfirmed
    base = [x for x in stints if x.source == base_source]
    rest, shown, links = [], list(base), []
    for other in (x for x in stints if x.source != base_source):
        match = (
            next((st for st in base if _same_stint(st, other, base_source != "eliteprospects")), None)
            if other.status != "unconfirmed"
            else None
        )
        if match is not None:
            links.append((match, other))
        elif base_source != "eliteprospects" and other.status != "unconfirmed":
            shown.append(other)  # SIHF players: stations elsewhere (youth, abroad) stay in the list
        else:
            rest.append(other)  # EP players: unchanged, everything else under "weitere Angaben"
    order = {id(x): i for i, x in enumerate(stints)}
    keyed: dict[tuple, dict] = {}
    if base_source == "eliteprospects":
        rows = _career_rows(sorted(shown, key=lambda x: order[id(x)]), keyed)
    else:  # SIHF list: its rows stay; the other stations shown are merged per station (2026-11-01)
        rows = _station_rows(shown[len(base) :], fixed=base, keyed=keyed, order=order)
    for match, other in links:
        row, src = keyed.get(_stint_key(match)), _row_src(other)
        if row is not None and src and src["url"] not in {x["url"] for x in row["sources"]}:
            row["sources"].append(src)
    return rows, (_career_rows(rest) if base_source == "eliteprospects" else _station_rows(rest))


# --- Wikipedia intro ------------------------------------------------------------------------------------


def first_paragraph(text: str | None, limit: int = INTRO_MAX) -> str | None:
    """First paragraph of a plain-text Wikipedia extract, verbatim; cut at a sentence end when very long."""
    for line in (text or "").split("\n"):
        line = line.strip()
        if len(line) >= 40:
            if len(line) <= limit:
                return line
            cut = line[:limit]
            end = cut.rfind(". ")
            return (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0]) + " […]"
    return None


def wikipedia_intro(s: Session, a: m.Athlete) -> dict | None:
    items = s.scalars(
        select(m.Item)
        .join(m.ItemAthlete)
        .where(m.ItemAthlete.athlete_id == a.id, m.Item.platform == "wikipedia", m.Item.text.is_not(None))
    ).all()
    if not items:
        return None
    # v8: German or English first (Fabi reads both); the home language only when there is nothing else
    order = ["de", "en", *home_languages(a.nationality)]

    def lang(i: m.Item) -> str:
        if i.language:
            return i.language
        host = urlparse(i.url).hostname or ""
        return host.split(".")[0]

    def rank(i: m.Item) -> tuple:
        lg = lang(i)
        return (order.index(lg) if lg in order else len(order), -(i.id or 0))

    for item in sorted(items, key=rank):
        if text := first_paragraph(item.text):
            out = {"text": text, "url": item.url, "label": f"Wikipedia {lang(item)}".strip()}
            from klugscheisser import translate

            if translate.needs(lang(item)):
                try:
                    out["text_de"] = translate.german(s, get_settings(), text, lang(item))
                except Exception:  # the card never fails because of a translation
                    out["text_de"] = None
                out["sprache"] = translate.label(lang(item))
            return out
    return None


# --- Erfolge (v9) --------------------------------------------------------------------------------------

ACHIEVEMENT_KINDS = ("medal", "award", "milestone", "captaincy")
ACHIEVEMENT_DE = {"award": "Auszeichnung", "milestone": "Meilenstein", "captaincy": "Rolle"}
METAL_DE = {"gold": "Gold", "silver": "Silber", "bronze": "Bronze"}
METAL_ORDER = {"gold": 0, "silver": 1, "bronze": 2}


def short_source(source: str, url: str | None, site: str | None = None) -> str:
    """Quiet source label: 'Wikidata', 'Wikipedia en', 'Kader' or the site + date ('EV Zug, 16.12.2025', else
    'evz.ch') for web finds."""
    if site:
        return site
    if source in ("wikidata", "wikipedia", "wikimedia"):
        return source_label(source, url)
    if source == "roster":
        return "Kader"
    if source == "hockeycanada":
        return "Hockey Canada"
    if source == "eliteprospects":
        return "Elite Prospects"
    host = (urlparse(url or "").hostname or "").removeprefix("www.")
    return host or source_label(source, url)


def _year(f: m.Fact) -> int | None:
    v = f.value or {}
    for raw in (v.get("year"), str(v.get("date") or "")[:4], f.season):
        if raw and str(raw).isdigit():
            return int(raw)
    return None


def _award_season(v: dict) -> str | None:
    """'2019/20' from an award's season: start year (2019) or EP's slug ('2019-2020')."""
    raw = str(v.get("season") or "")
    if raw.isdigit():
        return _season(raw)
    if re.fullmatch(r"\d{4}[-/]\d{2,4}", raw):
        return _season(raw[:4])
    return None


def _base_text(text: str) -> str:
    """Award name without '(2020)', '(89)' and spacing differences, for matching across sources."""
    return " ".join(re.sub(r"\([^)]*\)", " ", text.lower()).split())


def achievements(s: Session, a: m.Athlete) -> list[dict]:
    """Medals, awards, milestones and captaincies with a source link: medals first, near-identical facts
    (same text) once. A medal is a "medal" fact (Wikidata) or an award whose text names one."""
    from klugscheisser.facts.wikidata_import import medal_metal

    facts = s.scalars(
        select(m.Fact)
        .where(
            m.Fact.athlete_id == a.id,
            m.Fact.kind.in_(ACHIEVEMENT_KINDS),
            m.Fact.status != "unconfirmed",
            m.Fact.source_url.is_not(None),
        )
        .order_by(m.Fact.source != "wikidata", m.Fact.confidence.desc(), m.Fact.id)
    ).all()
    info = item_info(s, {f.source_item_id for f in facts if needs_site(f.source)})
    seen: set = set()
    out = []
    by_name: dict[tuple, dict] = {}  # v11: the same award in several seasons is one row ("2020/21 (89) · …")
    for f in facts:
        v = f.value or {}
        metal = v.get("metal") if f.kind == "medal" else medal_metal(f.value_text)
        year = _year(f)
        season = _award_season(v)
        text = " ".join(f.value_text.lower().split())
        key = ("medal", metal, year) if metal and year else text + (f"|{season}" if season else "")
        alt = (_base_text(f.value_text), year) if year else None  # same award, text with or without "(2020)"
        if key in seen or (alt and alt in seen):
            continue
        seen.add(key)
        if alt:
            seen.add(alt)
        row = {
            "text": f.value_text,
            "kind": "medal" if metal else f.kind,
            "label": METAL_DE.get(metal or "", "") or ACHIEVEMENT_DE.get(f.kind, f.kind),
            "metal": metal,
            "year": year or (int(season[:4]) + 1 if season else None),  # a season's award comes at its end
            "url": f.source_url,
            "quelle": short_source(f.source, f.source_url, row_site(f, info)),
        }
        if season and not metal and f.kind == "award":
            number = v.get("number")
            name = v.get("text") or f.value_text
            if number not in (None, ""):
                name = name.removesuffix(f"({number})").strip()
            when = season + (f" ({number})" if number not in (None, "") else "")
            group = by_name.get((_base_text(name), f.source))
            if group is not None:
                group["seasons"].append((season, when))
                group["year"] = max(group["year"] or 0, row["year"] or 0) or None
                continue
            row.update(text=name, seasons=[(season, when)])
            by_name[(_base_text(name), f.source)] = row
        out.append(row)
    for x in out:
        if x.get("seasons"):
            x["detail"] = " · ".join(w for _, w in sorted(x.pop("seasons"), reverse=True))
    kinds = {k: i for i, k in enumerate(ACHIEVEMENT_KINDS)}
    return sorted(
        out,
        key=lambda x: (kinds.get(x["kind"], 9), -(x["year"] or 0), METAL_ORDER.get(x["metal"] or "", 3)),
    )


# --- the card -------------------------------------------------------------------------------------------


def card_data(s: Session, a: m.Athlete) -> dict[str, Any]:
    shown, unconfirmed = career(s, a)
    ep = ep_profile(s, a)
    return {
        "id": a.id,
        "name": a.full_name,
        "foto": photo.stored_photo(s, a.id),
        "foto_laden": photo.photo_due(s, a),
        "fakten": key_facts(s, a, ep["stand"] if ep else None),
        "karriere": shown,
        "karriere_unbestaetigt": unconfirmed,
        "intro": wikipedia_intro(s, a),
        "ep": ep,
        "links": links(s, a),
        "aufnahme": recording(s, a),
    }
