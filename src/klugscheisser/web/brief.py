"""v6 step 5: game brief ("Tschechien – Schweiz"): both rosters with one sourced line per player, connections
between the teams, recent games and news about both. Deterministic from stored data — no model writes it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ, date_de
from klugscheisser.mcp_server.present import item_out
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec, national_team_key
from klugscheisser.web.html import e, icon, meaning, safe_url, say_buttons, short_label

POS = {"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"}


def _pos() -> dict[str, str]:
    """Position labels of the project's sport (2026-11-24: Torhüter … for men's floorball)."""
    return {**POS, **current_spec().position_labels_de}


RECENT_DAYS = 30
NEWS_PLATFORMS = ("rss", "x", "instagram", "sihf", "iihf", "pwhl", "sdhl")  # not profiles / Wikipedia


def team_for(s: Session, ioc: str) -> m.Team | None:
    return s.scalar(select(m.Team).where(m.Team.key == national_team_key(ioc)))


def roster(s: Session, team: m.Team, event: m.Event | None) -> tuple[list[m.Athlete], str]:
    """Newest stored roster: the event's entries, else affiliations, else players of that nation."""
    if event is not None:
        entries = list(
            s.scalars(
                select(m.EventEntry).where(m.EventEntry.event_id == event.id, m.EventEntry.team_id == team.id)
            )
        )
        entries = [x for x in entries if x.athlete.role == "athlete"]  # v8: coaches/staff are on rosters too
        if entries:
            status = "bestätigt" if all(x.list_type == "confirmed" for x in entries) else "provisorisch"
            return [x.athlete for x in entries], f"Kader {event.name} ({status})"
    from klugscheisser.retrieval.search import athlete_team_filter

    ids = athlete_team_filter(s, team).athlete_ids or []
    players = [a for a in (s.get(m.Athlete, i) for i in ids) if a.role == "athlete"]
    if len(players) >= 10:
        return players, "Nationalteam laut gespeicherten Kadern"
    athletes = list(
        s.scalars(select(m.Athlete).where(m.Athlete.nationality == team.country, m.Athlete.role == "athlete"))
    )
    return athletes[
        :40
    ], f"kein Kader gespeichert: {wording().players} dieser Nation in unserer Datenbank (Auswahl)"


def player_line(s: Session, a: m.Athlete, event: m.Event | None) -> dict[str, Any]:
    from klugscheisser.facts.queries import facts_for, stints_for

    number = None
    if event is not None:
        number = s.scalar(
            select(m.EventEntry.jersey_number).where(
                m.EventEntry.event_id == event.id, m.EventEntry.athlete_id == a.id
            )
        )
    if number is None:  # v8: newest national-team number from any stored roster (e.g. Olympia)
        number = s.scalar(
            select(m.EventEntry.jersey_number)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .join(m.Team, m.Team.id == m.EventEntry.team_id)
            .where(
                m.EventEntry.athlete_id == a.id,
                m.EventEntry.jersey_number.is_not(None),
                m.Team.kind == "national",
            )
            .order_by(m.Event.starts_at.desc().nulls_last())
            .limit(1)
        )
    today = datetime.now(TZ).date()
    birth = facts_for(s, [a.id], "birth_date")
    age = None
    if a.birth_date:
        age = (
            today.year
            - a.birth_date.year
            - ((today.month, today.day) < (a.birth_date.month, a.birth_date.day))
        )
    club = next((st for st in reversed(stints_for(s, [a.id])) if st.kind == "club"), None)
    since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    items = list(
        s.scalars(
            select(m.Item)
            .join(m.ItemAthlete)
            .where(
                m.ItemAthlete.athlete_id == a.id,
                m.Item.level == "full",
                m.Item.published_at >= since,
                m.Item.platform.in_(NEWS_PLATFORMS),
            )
            .order_by(m.Item.published_at.desc())
            .limit(5)
        )
    )
    vorsicht = [item_out(i) for i in items if i.classification and i.classification.vorsicht][:1]
    latest = [item_out(i) for i in items if not (i.classification and i.classification.vorsicht)][:1]
    return {
        "id": a.id,
        "name": a.full_name,
        "nation": a.nationality,
        "nummer": number,
        "position": _pos().get(a.position or "", a.position),
        "alter": age,
        "alter_quelle": birth[0].source_url if birth else None,
        "verein": club,
        "neu": latest[0] if latest else None,
        "vorsicht": vorsicht[0] if vorsicht else None,
    }


def brief_data(s: Session, iocs: list[str], event: m.Event | None) -> dict[str, Any]:
    from klugscheisser.facts.queries import played_together

    teams = [t for t in (team_for(s, x) for x in iocs) if t is not None]
    sides = []
    for team in teams:
        players, roster_note = roster(s, team, event)
        order = {"G": 0, "D": 1, "F": 2}
        players.sort(key=lambda a: (order.get(a.position or "", 3), a.full_name))
        sides.append(
            {"team": team, "note": roster_note, "players": [player_line(s, a, event) for a in players]}
        )
    links = []
    if len(sides) == 2:
        a_ids = [p["id"] for p in sides[0]["players"]]
        b_ids = [p["id"] for p in sides[1]["players"]]
        for p in played_together(s, a_ids, b_ids):
            if "national" in (p.a.kind, p.b.kind):
                continue
            links.append(p)
    games = []
    if len(teams) == 2:
        t1, t2 = teams[0].id, teams[1].id
        games = list(
            s.scalars(
                select(m.Event)
                .where(
                    m.Event.kind == "game",
                    or_(
                        (m.Event.home_team_id == t1) & (m.Event.away_team_id == t2),
                        (m.Event.home_team_id == t2) & (m.Event.away_team_id == t1),
                    ),
                )
                .order_by(m.Event.starts_at.desc())
                .limit(5)
            )
        )
    news = []
    if len(teams) == 2:
        since = datetime.now(UTC) - timedelta(days=60)
        both = (
            select(m.ItemTeam.item_id)
            .where(m.ItemTeam.team_id.in_([teams[0].id, teams[1].id]))
            .group_by(m.ItemTeam.item_id)
            .having(func.count(func.distinct(m.ItemTeam.team_id)) == 2)
        )
        news = [
            item_out(i)
            for i in s.scalars(
                select(m.Item)
                .where(
                    m.Item.id.in_(both),
                    m.Item.published_at >= since,
                    m.Item.level == "full",
                    m.Item.platform.in_(NEWS_PLATFORMS),
                )
                .order_by(m.Item.published_at.desc())
                .limit(6)
            )
        ]
    return {"sides": sides, "links": links, "games": games, "news": news, "event": event}


HOSTS = {
    "eliteprospects.com": "Elite Prospects",
    "wikidata.org": "Wikidata",
    "wikipedia.org": "Wikipedia",
    "sihf.ch": "SIHF",
    "iihf.com": "IIHF",
}
LINKS_SHOWN = 6  # connections before "Alle n Verbindungen"
POS_GROUP = (("Torhüterin", "Tor"), ("Verteidigerin", "Verteidigung"), ("Stürmerin", "Sturm"))


def _pos_group() -> tuple[tuple[str, str], ...]:
    pos = _pos()
    return tuple((pos[c], head) for c, (_l, head) in zip("GDF", POS_GROUP, strict=True))


def host_label(url: str | None, fallback: str = "Quelle") -> str:
    host = (urlparse(url or "").hostname or "").removeprefix("www.")
    for key, label in HOSTS.items():
        if host == key or host.endswith("." + key):
            return label
    return host or fallback


def chip(url: str | None, label: str | None = None, title: str | None = None) -> str:
    """The quiet source chip (same look as everywhere else in the app)."""
    if not url:
        return ""
    label = label or host_label(url)
    return (
        f"<a class='src' href='{safe_url(url)}' target='_blank' rel='noopener' title='{e(title or label)}'>"
        f"{e(label)}</a>"
    )


def _short_source(label: str) -> str:
    """'Elite Prospects' -> 'EP' (initials of a multi-word name), one word stays as it is."""
    words = label.split()
    return "".join(w[0] for w in words).upper() if len(words) > 1 else label


def pair_chips(a_url: str | None, a_name: str, b_url: str | None, b_name: str) -> str:
    """ui-loop round 5: two chips of a connection; from the same source they read 'EP · Müller' / 'EP · Meier'
    (player surname) so the two links can be told apart."""
    la, lb = host_label(a_url), host_label(b_url)
    if a_url and b_url and la == lb:
        sa, sb = (
            f"{_short_source(la)} · {(n or '').split()[-1] if n else ''}".rstrip(" ·")
            for n in (a_name, b_name)
        )
        return f"{chip(a_url, sa, f'{la}: {a_name}')} {chip(b_url, sb, f'{lb}: {b_name}')}"
    return f"{chip(a_url)} {chip(b_url)}"


def _item_row(n: dict[str, Any], warn: bool = False) -> str:
    """One stored item as a linked line: (Vorsicht) kind words · text · source chip."""
    kind = meaning(n.get("zeile_prefix"))
    text = n.get("zusammenfassung") or n.get("titel") or ""
    v = "<span class='k v'>Vorsicht</span>" if warn else ""
    k = f"<span class='k'>{e(kind)}</span>" if kind else ""
    label = short_label(n.get("quelle"))
    return (
        f"<a class='bi{' warn' if warn else ''}' href='{safe_url(n.get('url'))}' target='_blank' rel='noopener'>"
        f"<span class='t'>{v}{k}{e(text)}</span><span class='src'>{e(label)}</span></a>"
    )


def _player(p: dict[str, Any]) -> str:
    bits = []
    if p["nummer"]:
        bits.append(f"#{e(p['nummer'])}")
    if p["position"]:
        bits.append(e(p["position"]))
    if p["alter"] is not None:
        bits.append(f"{p['alter']} J. {chip(p['alter_quelle'])}".strip())
    if p["verein"] is not None:
        v = p["verein"]
        seasons = f" ({e(v.seasons)})" if v.season_known else ""
        bits.append(f"{e(v.team_name)}{seasons} {chip(v.source_url)}".strip())
    rows = ""
    if p["vorsicht"]:
        rows += _item_row(p["vorsicht"], warn=True)
    if p["neu"]:
        rows += _item_row(p["neu"])
    return (
        f"<li class='bp'><div class='l1'><a class='nm' href='/person/{p['id']}?tab=chat'>{e(p['name'])}</a>"
        f"{say_buttons(p['id'], p['nation'])}</div><div class='l2'>{' · '.join(bits)}</div>{rows}</li>"
    )


def _side(side: dict[str, Any], anchor: str) -> str:
    players = side["players"]
    groups: list[str] = []
    pos_group = _pos_group()
    for pos, head in pos_group:
        mine = [p for p in players if p["position"] == pos]
        if mine:
            groups.append(f"<li class='bg'>{e(head)} <span class='n'>{len(mine)}</span></li>")
            groups += [_player(p) for p in mine]
    rest = [p for p in players if p["position"] not in {x for x, _ in pos_group}]
    if rest:
        groups.append("<li class='bg'>Ohne Position</li>")
        groups += [_player(p) for p in rest]
    body = "".join(groups) or "<li class='small'>Kein Kader.</li>"
    return (
        f"<section class='bside' id='{anchor}' aria-labelledby='{anchor}-h'><div class='sh'>"
        f"<h2 id='{anchor}-h'>{e(side['team'].name)}</h2><span class='n'>{len(players)}</span></div>"
        f"<p class='small'>{e(side['note'])}</p><ul class='bplayers'>{body}</ul></section>"
    )


def brief_html(d: dict[str, Any]) -> str:
    """ui-loop round 3: header like the team pages, a jump bar (connections, games, news, both teams), source
    chips instead of '[Quelle]', both rosters side by side on wide screens (grouped Tor / Verteidigung / Sturm)."""
    sides = d["sides"]
    title = " – ".join(x["team"].name for x in sides) or "Spiel-Brief"
    event = f"<div class='rm'>{e(d['event'].name)}</div>" if d["event"] is not None else ""
    out = [
        f"<div class='bhead'><div class='eyebrow'>Spiel-Brief</div><h1>{e(title)}</h1>{event}"
        "<p class='small'>Aus gespeicherten Daten zusammengestellt (keine KI-Texte). Jede Angabe mit Quelle; "
        "Namen öffnen die Seite der Person mit Chat.</p></div>"
    ]
    now = datetime.now(UTC)
    planned = [g for g in d["games"] if g.starts_at and g.starts_at > now]
    played = [g for g in d["games"] if g not in planned]
    jumps = [("b-links", "Verbindungen", len(d["links"][:20]))]
    if planned or played:
        jumps.append(("b-games", "Spiele", len(planned) + len(played)))
    if d["news"]:
        jumps.append(("b-news", "Aktuell", len(d["news"])))
    jumps += [(f"b-team{i}", x["team"].name, len(x["players"])) for i, x in enumerate(sides)]
    out.append(
        "<nav class='chips bjump' aria-label='Springen zu'>"
        + "".join(
            f"<a class='fchip' href='#{a}'><span class='in'>{e(label)}<span class='n'>{n}</span></span></a>"
            for a, label, n in jumps
        )
        + "</nav>"
    )
    links = d["links"][:20]
    if links:
        rows = "".join(
            f"<li{' class=x' if i >= LINKS_SHOWN else ''}><span class='t'>"
            f"<a href='/person/{p.a.athlete_id}?tab=chat'>{e(p.a.athlete_name)}</a> und "
            f"<a href='/person/{p.b.athlete_id}?tab=chat'>{e(p.b.athlete_name)}</a></span>"
            f"<span class='x2'>zusammen bei {e(p.team_name)} · {e(p.a.seasons)} / {e(p.b.seasons)}</span>"
            f"<span class='srcs'>{pair_chips(p.a.source_url, p.a.athlete_name, p.b.source_url, p.b.athlete_name)}"
            "</span></li>"
            for i, p in enumerate(links)
        )
        more = (
            f"<button type='button' class='more-btn' onclick='moreRows(this)'>Alle {len(links)} Verbindungen"
            f"{icon('down', 16)}</button>"
            if len(links) > LINKS_SHOWN
            else ""
        )
        body = f"<ul class='blinks'>{rows}</ul>{more}"
    else:
        body = "<p class='small'>Keine gemeinsamen Stationen gespeichert.</p>"
    out.append(
        f"<section id='b-links' aria-labelledby='b-links-h'><div class='sh'><h2 id='b-links-h'>Verbindungen "
        f"zwischen den Teams</h2><span class='n'>{len(links)}</span></div>{body}</section>"
    )

    def game_li(g) -> str:
        src = chip(g.source_url, g.source or None) if g.source_url else ""
        score = f"<b>{e(g.score)}</b>" if g.score else ""
        return (
            f"<li><span class='d'>{e(date_de(g.starts_at, True))}</span><span class='t'>{e(g.name)} {score}</span>"
            f"{src}</li>"
        )

    games = ""
    if planned:
        games += f"<h3>Geplant</h3><ul class='bgames'>{''.join(game_li(g) for g in planned)}</ul>"
    if played:
        games += (
            f"<h3>Letzte Direktbegegnungen</h3><ul class='bgames'>{''.join(game_li(g) for g in played)}</ul>"
        )
    if games:
        out.append(
            f"<section id='b-games' aria-labelledby='b-games-h'><div class='sh'><h2 id='b-games-h'>Spiele</h2>"
            f"</div>{games}</section>"
        )
    if d["news"]:
        out.append(
            "<section id='b-news' aria-labelledby='b-news-h'><div class='sh'><h2 id='b-news-h'>Aktuell zu beiden "
            f"Teams</h2></div><div class='bnews'>{''.join(_item_row(n) for n in d['news'])}</div></section>"
        )
    out.append(f"<div class='bteams'>{''.join(_side(x, f'b-team{i}') for i, x in enumerate(sides))}</div>")
    return "".join(out)


def brief_form(event_key: str | None, teams: list[m.Team]) -> str:
    opts = "".join(f"<option value='{e(t.country)}'>{e(t.name)}</option>" for t in teams)
    hidden = f"<input type='hidden' name='event' value='{e(event_key)}'>" if event_key else ""
    return (
        f"<details class='prep'><summary class='btn small'>Spiel-Brief</summary>"
        f"<form method='get' action='/brief'>{hidden}<p><select name='a'>{opts}</select> – "
        f"<select name='b'>{opts}</select> <button class='btn primary' type='submit'>Brief öffnen</button></p>"
        "</form></details>"
    )


def brief_url(iocs: list[str], event_key: str | None) -> str:
    q = f"/brief?a={quote(iocs[0])}&b={quote(iocs[1])}"
    return q + (f"&event={quote(event_key)}" if event_key else "")
