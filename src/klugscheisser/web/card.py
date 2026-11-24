"""HTML of the "Steckbrief" profile card (data from web.card_data). Every value is shown with its source link."""

from __future__ import annotations

from typing import Any

from klugscheisser.web.html import e, icon, safe_url


def src_link(src: dict[str, Any] | None) -> str:
    """A quiet source chip; the full label (or the quote) shows on hover."""
    if not src or not src.get("url"):
        return ""
    label = src.get("label") or "Quelle"
    title = f"«{src['quote']}»" if src.get("quote") else label
    return (
        f'<a class="src" href="{safe_url(src["url"])}" target="_blank" rel="noopener" title="{e(title)}">'
        f"{e(label)}</a>"
    )


def photo_html(meta: dict[str, Any] | None, name: str) -> str:
    """The photo with its required attribution, or nothing."""
    if not meta:
        return ""
    w, h = meta.get("width"), meta.get("height")
    size = f' width="{int(w)}" height="{int(h)}"' if isinstance(w, int) and isinstance(h, int) else ""
    licence = e(meta["licence"])
    if meta.get("licence_url"):
        licence = f'<a href="{safe_url(meta["licence_url"])}" target="_blank" rel="noopener">{licence}</a>'
    return (
        f'<figure class="sb-photo"><a href="{safe_url(meta["file_page"])}" target="_blank" rel="noopener">'
        f'<img src="{safe_url(meta["thumb"])}" alt="{e(name)}" loading="lazy" decoding="async"{size}></a>'
        f"<figcaption>Foto: {e(meta['author'])}, {licence}, "
        f'<a href="{safe_url(meta["file_page"])}" target="_blank" rel="noopener">Wikimedia Commons</a>'
        "</figcaption></figure>"
    )


def photo_credit(meta: dict[str, Any] | None) -> str:
    """v9: the photo sits small in the page header; its attribution is repeated visibly in the Steckbrief."""
    if not meta:
        return ""
    return (
        f"<p class='small'>Foto: {e(meta['author'])}, {e(meta['licence'])}, "
        f"<a href='{safe_url(meta['file_page'])}' target='_blank' rel='noopener'>Wikimedia Commons</a></p>"
    )


def photo_slot(card: dict[str, Any]) -> str:
    if card.get("foto"):
        return photo_html(card["foto"], card["name"])
    if card.get("foto_laden"):  # looked up after the page is shown (≤ 5 s), replaced by the photo or nothing
        return (
            f'<div class="sb-photo sb-wait" hx-get="/person/{int(card["id"])}/foto" hx-trigger="load" '
            'hx-swap="outerHTML" aria-hidden="true"></div>'
        )
    return ""


def _fact_dd(f: dict[str, Any]) -> str:
    if f.get("parts"):
        value = ", ".join(f"{e(p['value'])} {src_link(p['source'])}" for p in f["parts"])
    else:
        value = e(f["value"]) + "".join(f" {src_link(x)}" for x in f["sources"])
    if f.get("note"):
        value += f' <span class="small">({e(f["note"])})</span>'
    if f.get("abweichend"):
        other = "; ".join(f"{e(x['value'])} {src_link(x['source'])}" for x in f["abweichend"])
        value += f'<div class="sb-diff small">abweichend: {other}</div>'
    return value


CAREER_SHOWN = 6  # newest stations shown before "Frühere Stationen"


def _career_table(rows: list[dict[str, Any]], newest_first: bool = True) -> str:
    """Season | team (league below) | source chips. ui-loop round 3: newest station first (like the timeline),
    older ones behind "Frühere Stationen anzeigen (n)"; on narrow screens the chips sit under the team."""
    unknown = [r for r in rows if r.get("saison_bekannt") is False]
    rows = [r for r in rows if r.get("saison_bekannt") is not False]
    rows = list(reversed(rows)) if newest_first else rows
    body = []
    for i, r in enumerate(rows):
        team = e(r["team"])
        if r.get("team_id"):
            team = f"<a href='/team/{int(r['team_id'])}'>{team}</a>"
        if r.get("art"):
            team += f' <span class="chip">{e(r["art"])}</span>'
        if r.get("status") == "conflict":
            team += ' <span class="chip">abweichende Quellen</span>'
        srcs = " ".join(src_link(x) for x in r["sources"])
        liga = f"<span class='lg'>{e(r['liga'])}</span>" if r.get("liga") else ""
        cls = " class='x'" if newest_first and i >= CAREER_SHOWN else ""
        body.append(
            f"<tr{cls}><td class='sb-season'>{e(r['saison'])}</td><td class='sb-team'>{team}{liga}</td>"
            f"<td class='sb-srcs'>{srcs}</td></tr>"
        )
    hidden = len(rows) - CAREER_SHOWN if newest_first else 0
    more = (
        f"<button type='button' class='more-btn' onclick='moreRows(this)'>Frühere Stationen anzeigen "
        f"<span class='n'>({hidden})</span>{icon('down', 16)}</button>"
        if hidden >= 2
        else ""
    )
    if 0 < hidden < 2:  # one hidden row is not worth a button
        body = [b.replace("<tr class='x'>", "<tr>") for b in body]
    table = (
        '<div class="sb-scroll"><table class="sb-career"><thead><tr><th>Saison</th><th>Team</th><th>Quellen</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table></div>{more}"
        if rows
        else ""
    )
    return table + _unknown_seasons(unknown)


def _unknown_seasons(rows: list[dict[str, Any]]) -> str:
    """2026-11-01: stations whose source names no season (articles): a plain list instead of "?" rows."""
    if not rows:
        return ""
    items = []
    for r in rows:
        team = e(r["team"])
        if r.get("team_id"):
            team = f"<a href='/team/{int(r['team_id'])}'>{team}</a>"
        liga = f" ({e(r['liga'])})" if r.get("liga") else ""
        art = f' <span class="chip">{e(r["art"])}</span>' if r.get("art") else ""
        srcs = " ".join(src_link(x) for x in r["sources"])
        items.append(f"<li>{team}{liga}{art} {srcs}</li>")
    return f'<div class="sb-unknown small"><b>Saison unbekannt</b><ul>{"".join(items)}</ul></div>'


def profile_card(card: dict[str, Any], photo: bool = True) -> str:
    """Key facts, intro and career. `photo=False` on the person page, where the photo sits in the header."""
    facts = "".join(f"<div><dt>{e(f['label'])}</dt><dd>{_fact_dd(f)}</dd></div>" for f in card["fakten"])
    top = photo_slot(card) if photo else ""
    if facts:
        top += f'<dl class="sb-facts">{facts}</dl>'
    used = [
        x for f in card["fakten"] for x in f["sources"] + [p["source"] for p in f.get("parts") or []] if x
    ]
    if (ep := card.get("ep")) and any(x["label"] == "Elite Prospects" for x in used):
        from klugscheisser.domain.labels import date_de

        # v11: one data age for all Elite Prospects values (they come from one page fetch)
        top += (
            f'<p class="small">Angaben von <a href="{safe_url(ep["url"])}" target="_blank" rel="noopener">'
            f"Elite Prospects</a>: Stand {e(date_de(ep['stand'], True))}</p>"
        )
    parts = [
        '<section class="steckbrief" id="steckbrief" aria-label="Steckbrief"><h2>Steckbrief</h2>'
        f'<div class="sb-top">{top}</div>'
    ]
    if intro := card.get("intro"):
        de = (
            f"<div class='small translated'>Automatisch übersetzt (aus dem {e(intro.get('sprache'))}en): "
            f"«{e(intro['text_de'])}»</div>"
            if intro.get("text_de")
            else ""
        )
        parts.append(
            f'<blockquote class="sb-intro">«{e(intro["text"])}» '
            f"{src_link({'label': intro['label'], 'url': intro['url']})}{de}</blockquote>"
        )
    if card["karriere"]:
        parts.append("<h2>Karriere</h2>" + _career_table(card["karriere"]))
    if card["karriere_unbestaetigt"]:
        parts.append(
            "<details class='sb-more'><summary>weitere Angaben (unbestätigt)</summary>"
            + _career_table(card["karriere_unbestaetigt"], newest_first=False)
            + "</details>"
        )
    if not (
        card["fakten"]
        or (photo and (card.get("foto") or card.get("foto_laden")))
        or card.get("intro")
        or card["karriere"]
    ):
        parts.append('<p class="small">Noch keine Steckbrief-Angaben mit Quelle gespeichert.</p>')
    parts.append("</section>")
    return "".join(parts)


def fun_facts_html(athlete_id: int, facts: list, generated, translations: dict | None = None) -> str:
    """v6 step 6: sourced fun facts with their date and a manual "Neu erstellen" button. v8: German below a
    quote that is neither German nor English (`translations`: quote -> German). v9: one sentence each; the
    verbatim quote, its source (with date) and the translation open on tap."""
    from klugscheisser.domain.labels import date_de

    translations = translations or {}

    def one(f) -> str:
        label = ((f.value or {}).get("quelle") or "Quelle").strip("[]")
        de = translations.get(f.quote or "")
        quote = f"<blockquote>«{e(f.quote)}»</blockquote>" if f.quote else ""
        de_html = f"<div class='de'>Übersetzung: «{e(de)}»</div>" if de else ""
        src = (
            f"<a class='srcl' href='{safe_url(f.source_url)}' target='_blank' rel='noopener'>{e(label)}"
            f"{icon('ext', 13)}</a>"
            if f.source_url
            else f"<span class='small'>{e(label)} [Link fehlt]</span>"
        )
        return (
            f"<details class='ff' data-k='funfacts'><summary><span class='t'>{e(f.value_text)}</span>"
            f"<span class='chev'>{icon('down')}</span></summary><div class='body'>{quote}{de_html}{src}</div>"
            "</details>"
        )

    rows = "".join(one(f) for f in facts)
    when = f"erstellt am {date_de(generated, True)}" if generated else "noch nicht erstellt"
    body = rows or (
        "<p class='empty' data-k='funfacts'>Keine belegten Fun Facts gefunden.</p>" if generated else ""
    )
    label = "Neu erstellen" if generated else "Fun Facts erstellen"
    return (
        "<section class='funfacts' id='funfacts' data-sec='funfacts' aria-labelledby='ff-h'>"
        f"<div class='sh'><span class='acc'>{icon('pin', 15)}</span><h2 id='ff-h'>Fun Facts</h2>"
        f"<span class='n'>Zitat auf Tippen</span></div>{body}"
        f"<div class='ff-foot'>{e(when)} · "
        f"<button class='lnk' hx-post='/person/{athlete_id}/funfacts' hx-target='#funfacts' "
        f"hx-swap='outerHTML' hx-disabled-elt='this'>{icon('refresh', 13)}{label}</button>"
        "<span class='indicator small'>ca. 5–15 s</span></div></section>"
    )
