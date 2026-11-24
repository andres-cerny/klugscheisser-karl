"""v9 person page (design "Variant C"): compact header with data age and Vorsicht line, Info | Chat on the
phone, filter chips, Steckbrief drawer, Erfolge, pinned fun facts, one dated timeline; the chat stays next to it
on wide screens. Admin things (tier, "Alles neu laden", data sources) sit collapsed at the very bottom."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from klugscheisser.pronounce import offered
from klugscheisser.web import card as sb
from klugscheisser.web import glance as gl
from klugscheisser.web import timeline as tl
from klugscheisser.web.html import (
    e,
    filter_chips,
    icon,
    initials,
    poll_fragment,
    safe_url,
    say_buttons,
    stand_line,
    tier_badge,
    view_switch,
    vorsicht_line,
)


def update_status_html(status: dict[str, Any] | None) -> str:
    if not status:
        return ""
    if status.get("job_id") and status.get("status") == "läuft":
        return poll_fragment(status["job_id"], "Tägliche Aktualisierung läuft im Hintergrund …")
    if status.get("hinweis"):
        return f"<div class='small'>{e(status['hinweis'])}</div>"
    return ""


def _fact(card: dict[str, Any], label: str) -> str | None:
    return next((f["value"] for f in card["fakten"] if f["label"] == label), None)


def fact_line(a: dict[str, Any], card: dict[str, Any]) -> str:
    """'Stürmerin · SUI · #7 · 32 J. · EV Zug' from the sourced key facts (sources are in the Steckbrief)."""
    number = re.search(r"#\d+", _fact(card, "Nummer") or "")
    club = _fact(card, "Aktueller Verein")
    if club:
        # v11: the EP value is "Team (Liga) · #7 · ausgeliehen"; the line shows the team only
        club = re.sub(r"\s*\(.*\)$", "", club.split(" · ")[0])
    elif a.get("vereine_laut_quellen"):
        club = a["vereine_laut_quellen"][0]["team"]
    bits = [
        a.get("position") or _fact(card, "Position"),
        a.get("nation"),
        number.group(0) if number else None,
        f"{a['alter']} J." if a.get("alter") else None,
        club,
    ]
    return " · ".join(x for x in bits if x)


LINKS = (("wikipedia", "Wikipedia", "book"), ("eliteprospects", "Elite Prospects", "stats"))


def links_row(a: dict[str, Any], card: dict[str, Any]) -> str:
    """v11: Instagram · Wikipedia · Elite Prospects at the top, each only when known. Instagram shows the handle
    and whether it is verified or only probable."""
    out = []
    if a.get("instagram"):
        h = a["instagram"][0]
        out.append(
            f"<a class='pl ig' data-l='instagram' href='{safe_url(h['url'])}' target='_blank' rel='noopener'>"
            f"{icon('instagram', 16)}{e(h['handle'])}<span class='v'>· {e(h['status'])}</span></a>"
        )
    links = card.get("links") or {}
    out += [
        f"<a class='pl' data-l='{key}' href='{safe_url(links[key])}' target='_blank' rel='noopener'>"
        f"{icon(ic, 16)}{e(label)}</a>"
        for key, label, ic in LINKS
        if links.get(key)
    ]
    return f"<nav class='plinks' aria-label='Profile'>{''.join(out)}</nav>" if out else ""


def header_html(p: dict[str, Any], card: dict[str, Any], update: dict[str, Any] | None) -> str:
    a = p["athletin"]
    ini = e(initials(a["name"]))
    # no photo: her initials (also when the lazy lookup finds none and leaves the slot empty)
    photo = sb.photo_slot(card) or f"<div class='ph-ph' role='img' aria-label='Kein Foto'>{ini}</div>"
    # v11: a real recording (Wikimedia, Elite Prospects) plays for every name; a computer voice only for names
    # that are not German-language
    recorded = bool(card.get("aufnahme"))
    say = (
        f"<div class='aussprache' id='aussprache-{int(a['id'])}' hx-get='/person/{int(a['id'])}/aussprache' "
        "hx-trigger='load'></div>"
        if recorded or offered(a.get("nation"))
        else ""
    )
    return f"""<div class="ph-stack"><div class="ph"><div class="ph-photo" data-ini="{ini}">{photo}</div><div class="ph-body">
<h1>{e(a["name"])}{say_buttons(a["id"], a.get("nation"), recorded)}</h1>{say}
<div class="ph-facts"><span class="ph-line">{e(fact_line(a, card))}</span></div>
{links_row(a, card)}</div></div>
{stand_line(p["stand"], str(a["id"]), update_status_html(update))}
{vorsicht_line(p["vorsicht"])}</div>"""


def drawer_html(p: dict[str, Any], card: dict[str, Any]) -> str:
    """Steckbrief & Karriere: a drawer on the phone, the left column on wide screens."""
    a = p["athletin"]
    sub = ["Kurzbio (Wikipedia)" if card.get("intro") else None]
    sub += [f["value"] for f in card["fakten"] if f["label"] in ("Grösse", "Schusshand", "Fanghand")]
    if card["karriere"]:
        sub.append(f"{len(card['karriere'])} Stationen")
    bio = ""
    if b := a.get("instagram_bio"):
        text = " · ".join(x.strip() for x in b["text"].splitlines() if x.strip())
        bio = (
            f"<h2>Instagram-Bio</h2><p class='bio'>«{e(text)}» <a class='src' href='{safe_url(b['url'])}' "
            f"target='_blank' rel='noopener' title='{e(b['quelle'].strip('[]'))}'>{e(b['quelle'].strip('[]'))}</a>"
            f"</p><p class='small'>{e(b.get('hinweis'))}</p>"
        )
    # ui-loop round 5: dates and roster status of the Anlässe are in "Auf einen Blick" and the Zeitleiste
    # ("Demnächst" / by month); here only one compact line of links to their pages remains
    events = ""
    if p["anlaesse"]:
        links = " · ".join(f"<a href='/event/{quote(x['key'])}'>{e(x['name'])}</a>" for x in p["anlaesse"])
        events = f"<p class='small ev-line'>Anlässe: {links}</p>"
    return f"""<div class="drawer" data-sec="steckbrief"><button type="button" class="drawer-btn" aria-expanded="false"
 aria-controls="drawer-body" onclick="toggleOpen(this,'.drawer')"><span class="t"><b>Steckbrief &amp; Karriere</b>
<span>{e(" · ".join(x for x in sub if x) or ("Fakten mit Quelle" if card["fakten"] else "Noch keine Angaben mit Quelle"))}</span></span><span class="chev">{icon("down")}</span>
</button><div class="drawer-body" id="drawer-body">{sb.profile_card(card, photo=False)}{sb.photo_credit(card.get("foto"))}
{bio}{events}</div></div>"""


ERFOLGE_PHONE = 4
ERFOLGE_WIDE = 6  # ui-loop round 5: two columns of three on wide screens (round 4: 8; before: all)


def erfolge_html(items: list[dict[str, Any]], shown: int = ERFOLGE_PHONE, wide: int = ERFOLGE_WIDE) -> str:
    """Medals first (quiet gold / silver / bronze label), each linked to its source; the phone shows 4, wide
    screens 6, then "Alle n Erfolge" (a filter chip "Erfolge" opens all, too)."""
    if not items:
        return ""
    rows = []
    for i, x in enumerate(items):
        if x["metal"]:
            tag = (
                f"<span class='medal {e(x['metal'])}'>{icon('medal', 16)}"
                f"<span class='ml {e(x['metal'])}'>{e(x['label'])}</span></span>"
            )
        else:
            tag = f"<span class='kl'>{e(x['label'])}</span>"
        # v11: one row per award, its seasons (with EP's number, e.g. points) below: "2020/21 (89) · 2019/20"
        detail = f"<span class='y'>{e(x['detail'])}</span>" if x.get("detail") else ""
        rows.append(
            f"<a class='{'x' if i >= wide else 'xp' if i >= shown else ''}' href='{safe_url(x['url'])}' target='_blank' rel='noopener' "
            f"data-k='erfolge'><span class='t'>{e(x['text'])}{detail}</span><span class='r'>{tag}"
            f"<span class='src'>{e(x['quelle'])}</span>{icon('ext', 14)}</span></a>"
        )
    more = (
        f"<button type='button' class='more-btn erf-all{' erf-w' if len(items) > wide else ''}' "
        f"onclick='showAll(this)'>Alle {len(items)} Erfolge"
        f"{icon('down', 16)}</button>"
        if len(items) > shown
        else ""
    )
    return (
        f"<section aria-labelledby='erf-h' data-sec='erfolge'><div class='sh'>{icon('trophy', 15)}"
        f"<h2 id='erf-h'>Erfolge</h2><span class='n'>{len(items)}</span></div>"
        f"<div class='erf'>{''.join(rows)}</div>{more}</section>"
    )


def more_button(athlete_id: int, offset: int | None) -> str:
    if offset is None:
        return ""
    return (
        f"<p><a class='btn' href='/person/{athlete_id}/posts?offset={offset}' "
        f"hx-get='/person/{athlete_id}/posts?offset={offset}' hx-target='this' hx-swap='outerHTML'>"
        "Ältere Instagram-Posts laden</a></p>"
    )


def daten_html(p: dict[str, Any], sources_box: str) -> str:
    """Admin things out of the main flow: search depth (tier), reload everything, profiles, data sources."""
    a = p["athletin"]
    current = int(a.get("tier") or 3)
    options = [("auto", "automatisch")] + [(str(t), f"Tier {t}") for t in (1, 2, 3)]
    chosen = str(current) if a.get("tier_manuell") else "auto"
    opts = "".join(
        f"<option value='{v}'{' selected' if v == chosen else ''}>{label}</option>" for v, label in options
    )
    tier = (
        f"<form method='post' action='/person/{int(a['id'])}/tier' class='row'>"
        f"<label for='tier-sel'>Tiefe der Suche</label> <select id='tier-sel' name='tier' "
        f"onchange='this.form.submit()'>{opts}</select> {tier_badge(a.get('tier'), a.get('quellen'))}"
        "<noscript><button class='btn small' type='submit'>OK</button></noscript>"
        "<span class='small'>Tier 1 = Schweizer Kader, viele Quellen · Tier 2 = andere WM-Teams · "
        "Tier 3 = Rest</span></form>"
    )
    deep = deep_button(f"person:{int(a['id'])}") if current in (1, 2) else ""
    lines = []
    for v in a.get("vereine_laut_quellen", [])[:2]:
        note = f" <span class='small'>({e(v['hinweis'])})</span>" if v.get("hinweis") else ""
        src = (
            f" <a class='src' href='{safe_url(v.get('url'))}' target='_blank' rel='noopener'>{e(v.get('quelle'))}</a>"
            if v.get("url")
            else ""
        )
        lines.append(f"<li>Verein laut Quellen: {e(v['team'])} {e(v.get('saison') or '')}{note}{src}</li>")
    for h in a.get("instagram", []):
        cls = "badge-ok" if h["verified"] else "badge-prov"
        lines.append(
            f"<li>Instagram: <a href='{safe_url(h['url'])}' target='_blank' rel='noopener'>{e(h['handle'])}</a> "
            f"<span class='{cls}'>{e(h['status'])}</span></li>"
        )
    if not a.get("instagram"):
        lines.append("<li class='small'>Kein bestätigter Instagram-Account bekannt.</li>")
    # v11: Instagram, Wikipedia and Elite Prospects are linked at the top of the page; Wikidata stays here
    if a.get("wikidata"):
        lines.append(
            f"<li class='small'>Profile: <a href='{safe_url(a['wikidata'])}' target='_blank' rel='noopener'>"
            "Wikidata</a></li>"
        )
    return (
        f"<details class='daten' id='daten'><summary><span class='dt'>Daten &amp; Einstellungen</span> <span class='n'>Tiefe der Suche, "
        f"Alles neu laden, Datenquellen</span><span class='chev'>{icon('down')}</span></summary>"
        f"{tier}<div class='row'>{deep}</div><ul class='lines'>{''.join(lines)}</ul>{sources_box}</details>"
    )


def deep_button(target: str, label: str = "Alles neu laden") -> str:
    """v8: reload everything for tier 1/2 persons (web, news, Instagram, Facebook …) in the background."""
    return (
        f"<form class='inline' method='post' action='/alles-neu' hx-post='/alles-neu' hx-target='#refresh-status' "
        f"hx-swap='innerHTML' hx-disabled-elt='find button'><input type='hidden' name='target' value='{e(target)}'>"
        f"<button class='btn small' type='submit' title='Websuche, News, Instagram, Facebook – nach Tier'>"
        f"{icon('refresh', 14)}{e(label)}</button></form>"
    )


def chat_aside(title: str, context: str, panel: str, new_href: str | None) -> str:
    """The chat next to a person / team / event: always visible on wide screens, enlarge button."""
    new = (
        f"<a class='ib' href='{e(new_href)}' title='Neuer Chat' aria-label='Neuer Chat'>{icon('plus')}<span class='ibl'>Neuer Chat</span></a>"
        if new_href
        else ""
    )
    return f"""<aside class="pg-chat" aria-label="Chat"><div class="ch-head"><span class="acc">{icon("chat", 16)}</span>
<h2>{e(title)}</h2><span class="ctx">{e(context)}</span>{new}
<button type="button" class="ib ch-big" aria-pressed="false" aria-label="Chat vergrössern" title="Vergrössern"
 onclick="chatBig(this)">{icon("expand")}</button></div>{panel}</aside>"""


TIMELINE_KEEP = 12  # rows shown before older months collapse


def chat_label(name: str) -> str:
    """Phone switch: 'Chat zu Saskia' (first name; the whole name when it is one word)."""
    first = (name or "").split()
    return f"Chat zu {first[0]}" if first else "Chat"


def person_page(
    p: dict[str, Any],
    update: dict[str, Any] | None,
    card: dict[str, Any],
    funfacts: str,
    n_funfacts: int,
    erfolge: list[dict[str, Any]],
    chat_panel: str,
    sources_box: str,
    tab: str = "info",
    has_chat: bool = False,
    statistik: str = "",
    n_seasons: int = 0,
    stats: dict[str, Any] | None = None,
) -> str:
    a = p["athletin"]
    items = tl.entries(p)
    glance = gl.glance_html(p, card, items, erfolge, stats)
    n_facts = len(card.get("fakten") or []) or len(card.get("karriere") or [])
    chips = filter_chips(tl.counts(items, len(erfolge), n_funfacts, n_seasons, n_facts))
    timeline = (
        tl.timeline_html(items, keep=TIMELINE_KEEP) or "<p class='empty'>Noch keine Einträge im Zeitraum.</p>"
    )
    more = more_button(a["id"], p["instagram_mehr"])
    n_items = f"<span class='n'>{len(items)}</span>" if items else ""
    # ui-loop round 3: the 10-second view first, then the Steckbrief (a drawer on the phone, an open section with a
    # fact grid on wide screens; was a long narrow left column)
    info = f"""<div class="pg-info" id="info" data-f="alle"><div class="info-grid">
<div class="info-main"><div class="pbar">{chips}</div>{glance}{drawer_html(p, card)}{statistik}{erfolge_html(erfolge)}{funfacts}
<section aria-labelledby="tl-h" data-sec="timeline"><div class="sh">{icon("list", 15)}<h2 id="tl-h">Zeitleiste</h2>
{n_items}<span class="n">News, Posts, Resultate, Anlässe</span></div>{timeline}<div id="ig">{more}</div>
</section></div></div>{daten_html(p, sources_box)}</div>"""
    aside = chat_aside(
        "Chat", f"zu {a['name']}", chat_panel, f"/person/{a['id']}?tab=chat&neu=1" if has_chat else None
    )
    return (
        f"<div class='pg' data-view='{'chat' if tab == 'chat' else 'info'}'><div class='pg-main'>"
        f"{header_html(p, card, update)}{view_switch([('info', 'Profil'), ('chat', chat_label(a['name']))], tab)}"
        f"{info}</div>"
        f"{aside}</div>"
    )
