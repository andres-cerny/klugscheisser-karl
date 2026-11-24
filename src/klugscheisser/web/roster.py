"""v9 team and event pages (design "Variant C"): title, data age, "Provisorisch" once, a dense roster grouped
Tor / Verteidigung / Sturm / Staff, and the team/event chat next to it (phone: Kader | Chat)."""

from __future__ import annotations

import re
from typing import Any

from klugscheisser.web.data import _von_bis
from klugscheisser.web.html import (
    e,
    fact_of,
    icon,
    safe_url,
    short_label,
    stand_line,
    tier_badge,
    view_switch,
    vorsicht_line,
)
from klugscheisser.web.person import deep_button

GROUPS = (("G", "Tor"), ("D", "Verteidigung"), ("F", "Sturm"), ("", "Staff"))
POSITION_GROUP = {"Torhüterin": "G", "Verteidigerin": "D", "Stürmerin": "F"}
POSITION_GROUP |= {"Torhüter": "G", "Verteidiger": "D", "Stürmer": "F"}  # 2026-11-24: men's floorball


def group_of(p: dict[str, Any]) -> str:
    role = p.get("rolle") or ""
    if role in ("G", "D", "F"):
        return role
    return POSITION_GROUP.get(p.get("position") or "", "")


def status_badge(list_type: str | None) -> str:
    if list_type == "confirmed":
        return "<span class='badge-ok'>bestätigt</span>"
    if list_type == "provisional":
        return "<span class='badge-prov'>provisorisch</span>"
    return "<span class='small'>Kader noch nicht erfasst</span>"


def _club(p: dict[str, Any], team_name: str | None) -> str:
    v = p.get("verein")
    name = v.get("team") if isinstance(v, dict) else None
    return "" if not name or name == team_name else name


def player_li(p: dict[str, Any], team_name: str | None = None) -> str:
    num = (
        f"<span class='no'>{int(p['nummer'])}</span>"
        if p.get("nummer")
        else "<span class='no none' aria-label='keine Nummer'>–</span>"
    )
    akt = ""
    if p.get("vorsicht"):
        v = p["vorsicht"][0]
        text = v.get("zusammenfassung_de") or v.get("zusammenfassung") or "Vorsicht"
        akt = (
            f"<a class='v' href='{safe_url(v.get('url'))}' target='_blank' rel='noopener' "
            f"title='{e(short_label(v.get('quelle')))}'>{icon('alert', 13)}<span>Vorsicht: {e(text)}</span></a>"
        )
    elif post := p.get("neuester_post"):
        text = post.get("zusammenfassung_de") or post.get("zusammenfassung") or ""
        akt = (
            f"<a class='lp' href='{safe_url(post.get('url'))}' target='_blank' rel='noopener' "
            f"title='{e(short_label(post.get('quelle')))}'><span>{e(text)}</span>{icon('ext', 13)}</a>"
        )
    club = _club(p, team_name)
    return (
        f"<li data-k='{e(group_of(p) or 'staff')}'>{num}<span class='nm'><a href='/person/{int(p['id'])}'>"
        f"{e(p['name'])}</a>{tier_badge(p.get('tier'), p.get('quellen'), short=True)}</span>"
        f"<span class='ps'>{e(p.get('position') or '–')}</span><span class='cl'>{e(club)}</span>"
        f"<span class='ak'>{akt}</span></li>"
    )


def roster_html(players: list[dict[str, Any]], team_name: str | None = None) -> str:
    """Players grouped Tor / Verteidigung / Sturm, staff (Headcoach …) after."""
    out = []
    head = (
        "<li class='hd' aria-hidden='true'><span>Nr.</span><span>Name</span><span>Position</span>"
        "<span>Verein</span><span>"
        + ("Aktuell" if any("neuester_post" in p for p in players) else "")
        + "</span></li>"
    )
    for key, label in GROUPS:
        rows = [p for p in players if group_of(p) == key]
        if not rows:
            continue
        rows.sort(key=lambda p: (p.get("nummer") is None, p.get("nummer") or 0, p["name"]))
        out.append(
            f"<section aria-label='{label}' data-sec='{e(key or 'staff')}'><div class='tl-m'>{label} "
            f"<span class='n'>{len(rows)}</span></div><ul class='roster'>{head}"
            + "".join(player_li(p, team_name) for p in rows)
            + "</ul></section>"
        )
    return "".join(out)


def _tel(phone: str) -> str:
    """'+00 (0) 12 3456 – 78' → '+0012345678' (tel: target: a leading +, digits only, the "(0)" dropped)."""
    digits = re.sub(r"\(0\)", "", phone)
    return ("+" if digits.strip().startswith("+") else "") + re.sub(r"\D", "", digits)


def contact_li(c: dict[str, Any], team: str | None = None) -> str:
    """One media manager / press officer: name, role, mailto:/tel: links, source (work contact as published)."""
    bits = []
    if c.get("email"):
        bits.append(f"<a href='mailto:{e(c['email'])}'>{e(c['email'])}</a>")
    if c.get("telefon"):
        bits.append(f"<a href='tel:{e(_tel(c['telefon']))}'>{e(c['telefon'])}</a>")
    src = (
        f" <a class='src' href='{safe_url(c['url'])}' target='_blank' rel='noopener'>"
        f"{e(short_label(c.get('quelle')))}</a>"
        if c.get("url")
        else ""
    )
    who = f"{e(team)}: " if team else ""
    return (
        f"<li>{who}<a href='/person/{int(c['id'])}'>{e(c['name'])}</a> <span class='small'>{e(c['rolle'])}</span>"
        f" · {' · '.join(bits)}{src}</li>"
    )


def contacts_html(rows: list[tuple[str | None, dict[str, Any]]]) -> str:
    """2026-11-01: 'Medienkontakt' block (team page: its own staff; event page: one line per team)."""
    if not rows:
        return ""
    return (
        f"<section class='kontakt' aria-label='Medienkontakt'><div class='sh'>{icon('user', 15)}<h2>Medienkontakt"
        f"</h2><span class='n'>{len(rows)}</span></div><ul class='evt'>"
        + "".join(contact_li(c, team) for team, c in rows)
        + "</ul></section>"
    )


def kader_stand(ks: dict[str, Any] | None) -> str:
    if not ks:
        return ""
    src = ks.get("quelle") or ""
    if len(src) > 24 or not src:
        from urllib.parse import urlparse

        src = (urlparse(ks.get("url") or "").hostname or "").removeprefix("www.") or src[:24]
    return f"Kader {e(ks['datum'])}" + (f" ({e(src)})" if src else "")


def prov_note(text: str, src: dict[str, Any] | None) -> str:
    link = ""
    if src and src.get("url"):
        label = src.get("quelle") or "Quelle"
        link = f" <a href='{safe_url(src['url'])}' target='_blank' rel='noopener'>{e(label)}</a>"
    return f"<div class='prov'><span class='tag'>Provisorisch</span><p>{e(text)}{link}</p></div>"


def page_frame(header: str, body: str, aside: str, tab: str, chat: str = "Chat") -> str:
    view = "chat" if tab == "chat" else "kader"
    return (
        f"<div class='pg' data-view='{view}'><div class='pg-main'>{header}"
        f"{view_switch([('kader', 'Kader'), ('chat', chat)], view)}"
        f"<div class='pg-info' id='info' data-f='alle'>{body}</div></div>{aside}</div>"
    )


def prep_form(event_key: str) -> str:
    """v5: event preparation — rosters incl. coaches/staff, deep load, nightly refresh (cost shown first)."""
    hx = 'hx-post="/vorbereitung" hx-target="#prep-result" hx-swap="innerHTML" hx-disabled-elt="find button"'
    return f"""<details class="prep"><summary class="btn small">Vorbereitung für diesen Anlass</summary>
<form method="post" action="/vorbereitung" {hx}><input type="hidden" name="event" value="{e(event_key)}">
<p class="small">Lädt alle Kader inkl. Trainer und Staff, Instagram, Profile und eine Websuche pro Person in die
Datenbank und aktualisiert sie nachts bis zum Ende des Anlasses. Du siehst zuerst den Vorschlag mit Kosten.</p>
<p><label><input type="radio" name="teams" value="alle" checked> alle Teams</label>
<label><input type="radio" name="teams" value="schweiz"> nur Schweiz</label></p>
<p><button class="btn primary" type="submit">Vorschlag holen</button></p></form>
<div id="prep-result"></div></details>"""


def other_teams_html(teams: list[dict[str, Any]], total: int) -> str:
    """ui-loop round 4: teams without a stored roster as one compact block (by group), not one big heading each
    with "Kader noch nicht erfasst" — the rosters we have stay on top."""
    if not teams:
        return ""
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in teams:
        groups.setdefault(t.get("gruppe") or "", []).append(t)

    def li(t: dict[str, Any]) -> str:
        status = (
            f" <span class='st'>{status_badge(t['kader_status'])}</span>" if t.get("kader_status") else ""
        )
        return f"<li><a href='/team/{int(t['id'])}'>{e(t['team'])}</a>{status}</li>"

    rows = "".join(
        f"<div class='evg'>{f'<span class=gl>Gruppe {e(g)}</span>' if g else ''}"
        f"<ul class='evt'>{''.join(li(t) for t in ts)}</ul></div>"
        for g, ts in sorted(groups.items(), key=lambda x: (x[0] == "", x[0]))
    )
    title = "Teams" if len(teams) == total else "Weitere Teams"
    return (
        f"<section class='ev-rest' aria-labelledby='evr-h'><div class='sh'><h2 id='evr-h'>{title}</h2>"
        f"<span class='n'>{len(teams)} · Kader noch nicht erfasst</span></div>{rows}</section>"
    )


def event_body(ev: dict[str, Any], aside: str, tab: str = "kader") -> str:
    a = ev["anlass"]
    dates = _von_bis(a["von"], a.get("bis")) if a.get("von") else ""
    meta = " · ".join(x for x in (dates, a.get("ort")) if x)
    src = (
        f" <a class='src' href='{safe_url(a['url'])}' target='_blank' rel='noopener'>Quelle</a>"
        if a.get("url")
        else ""
    )
    header = f"""<div class="rh"><span class="eyebrow">Anlass</span><h1>{e(a["name"])}</h1>
<div class="rm">{e(meta)}{src}</div>{stand_line(ev["stand"], a["key"], first=kader_stand(ev.get("kader_stand")))}
<div class="acts">{ev.get("brief_form", "")}</div></div>"""
    swiss = next((t for t in ev["teams"] if t["schweiz"]), ev["teams"][0] if ev["teams"] else None)
    body = []
    if "PROVISORISCH" in ev["lineup_status"]:
        body.append(prov_note(ev["lineup_status"], (swiss or {}).get("kader_quelle")))
    else:
        body.append(f"<p class='small'>{e(ev['lineup_status'])}</p>")
    with_roster = [t for t in ev["teams"] if t["spielerinnen"]]
    rest = [t for t in ev["teams"] if not t["spielerinnen"]]
    if len(with_roster) >= 2:
        # ui-loop round 4: several rosters make a very long page; one jump bar to each team (+ the others)
        jumps = [(f"team-{int(t['id'])}", t["team"], len(t["spielerinnen"])) for t in with_roster]
        jumps += [("evr-h", "Weitere Teams", len(rest))] if rest else []
        body.append(
            "<nav class='chips bjump' aria-label='Springen zu'>"
            + "".join(
                f"<a class='fchip' href='#{a}'><span class='in'>{e(label)}<span class='n'>{n}</span></span></a>"
                for a, label, n in jumps
            )
            + "</nav>"
        )
    for team in with_roster:
        group = f" · Gruppe {e(team['gruppe'])}" if team.get("gruppe") else ""
        body.append(
            f"<div class='team-h' id='team-{int(team['id'])}'><h2><a href='/team/{int(team['id'])}'>"
            f"{e(team['team'])}</a></h2>"
            f"<span class='small'>{status_badge(team['kader_status'])}{group}"
            f"{' · ' + str(len(team['spielerinnen'])) if team['spielerinnen'] else ''}</span></div>"
        )
        body.append(roster_html(team["spielerinnen"]))
    body.append(other_teams_html(rest, len(ev["teams"])))
    body.append(contacts_html([(t["team"], c) for t in ev["teams"] for c in t.get("kontakte") or []]))
    body.append(
        "<details class='daten' id='daten'><summary><span class='dt'>Daten &amp; Vorbereitung</span> <span class='n'>Vorbereitung, "
        f"Alles neu laden</span><span class='chev'>{icon('down')}</span></summary><div class='row'>"
        f"{prep_form(a['key'])}{deep_button(f'event:{a["key"]}', 'Alles neu laden (Tier 1/2)')}</div></details>"
    )
    return page_frame(header, "".join(body), aside, tab, "Chat zum Anlass")


def team_body(td: dict[str, Any], aside: str, tab: str = "kader") -> str:
    tm = td["team"]
    src = td.get("kader_quelle") or {}
    meta = " · ".join(x for x in (tm.get("liga"), tm.get("land")) if x)
    header = f"""<div class="rh"><span class="eyebrow">Team</span><h1>{e(tm["name"])}</h1>
<div class="rm">{e(meta)}</div>{stand_line(td["stand"], tm["name"], first=kader_stand(td.get("kader_stand")))}
{vorsicht_line(td["vorsicht"])}</div>"""
    body = []
    if src.get("status") == "provisional":
        body.append(
            prov_note(
                f"Kader {src.get('anlass') or ''}: provisorisch, nicht die offizielle Aufstellung.", src
            )
        )
    elif src:
        bits = " · ".join(x for x in (src.get("anlass"), src.get("quelle")) if x)
        link = (
            f" <a class='src' href='{safe_url(src['url'])}' target='_blank' rel='noopener'>Quelle</a>"
            if src.get("url")
            else ""
        )
        body.append(f"<p class='small'>Kader: {e(bits)}{link}</p>")
    n = len(td["spielerinnen"])
    body.append(
        f"<div class='sh'>{icon('user', 15)}<h2>Kader</h2><span class='n'>{n or ''}</span></div>"
        + (roster_html(td["spielerinnen"], tm["name"]) or "<p class='empty'>Kein Kader erfasst.</p>")
    )
    body.append(contacts_html([(None, c) for c in td.get("kontakte") or []]))
    rows = "".join(
        f"<a class='tl{' warn' if i.get('vorsicht') else ''}' href='{safe_url(i.get('url'))}' target='_blank' "
        f"rel='noopener'><span class='d'>{e((i.get('datum') or '')[:6])}</span><span class='rail'></span>"
        f"<span class='b'><span class='t'>{e(fact_of(i, 160))}</span><span class='s'>"
        f"<span class='src s2'>{e(short_label(i.get('quelle')))}</span></span></span><span class='r'>"
        f"<span class='src'>{e(short_label(i.get('quelle')))}</span>{icon('ext', 14)}</span></a>"
        # newest first (the list comes ranked by relevance; on the page a dated list reads top-down)
        for i in sorted(td["eintraege"], key=lambda i: i.get("published_at") or "", reverse=True)
    )
    body.append(
        f"<div class='sh'>{icon('list', 15)}<h2>Neueste Einträge</h2><span class='n'>{len(td['eintraege'])}</span>"
        f"</div>{rows}"
        if rows
        else "<p class='empty'>Keine Einträge im Zeitraum.</p>"
    )
    return page_frame(header, "".join(body), aside, tab, "Chat zum Team")
