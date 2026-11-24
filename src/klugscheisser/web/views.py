"""v4 page parts: home, chat, add proposals, sources. Pure HTML builders (data in, escaped HTML out)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import object_session

from klugscheisser.adds.base import Proposal
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ
from klugscheisser.interfaces import AnswerResult, Statement
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec
from klugscheisser.web.html import copy_button, e, icon, initials, meaning, safe_url, search_box, short_label


def when(dt: datetime | None) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(TZ)
    if local.date() == datetime.now(TZ).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m.")


def ref_href(ref: str | None) -> str | None:
    if not ref or ":" not in ref:
        return None
    kind, ident = ref.split(":", 1)
    return f"/{kind}/{quote(ident)}" if kind in ("person", "team", "event") else None


def chat_context(c: m.Chat) -> str:
    """Name of the person/team the chat is about (so «Was gibt es Neues bei ihr?» says who), or ''."""
    s = object_session(c)
    if s is None:
        return ""
    if c.athlete_id:
        obj = s.get(m.Athlete, c.athlete_id)
        return obj.full_name if obj else ""
    if c.team_id:
        obj = s.get(m.Team, c.team_id)
        return obj.name if obj else ""
    if c.event_id:
        obj = s.get(m.Event, c.event_id)
        return obj.name if obj else ""
    return ""


def chat_li(c: m.Chat) -> str:
    ctx = chat_context(c)
    ctx_html = f"<span class='sub'>zu {e(ctx)}</span>" if ctx else ""
    return (
        f"<li><a href='/chat/{c.id}'>{icon('chat', 16)}<span class='nm'><span class='t'>{e(c.title)}</span>"
        f"{ctx_html}</span></a><span class='when'>{when(c.updated_at)}</span></li>"
    )


# --- home ----------------------------------------------------------------------------------------------

_QWORDS = re.compile(
    r"^(wer|was|wie|wo|wann|warum|wieso|weshalb|welche[rsnm]?|hat|haben|ist|sind|gibt|gab|kann|spielt|zeig|"
    r"nenn|erzähl|vergleich|füge|who|what|how|when|where|which|is|are|does|did)\b",
    re.I,
)


def looks_like_question(text: str) -> bool:
    """'Wer spielt im Ausland?' / 'wer ist verletzt bei Kanada' → True (offered to the chat, never sent)."""
    v = (text or "").strip()
    return v.endswith("?") or (len(v.split()) >= 3 and bool(_QWORDS.match(v)))


def ask_form(placeholder: str = "Frage an Karl …", value: str = "") -> str:
    """A question box that starts a new chat (POST /chat). The textarea gets the dictation button (JS)."""
    return f"""<form class="askbox" method="post" action="/chat">
<div class="ask"><label class="sr" for="ask-home">Frage an Karl</label><textarea id="ask-home" name="frage" required
 rows="1" onkeydown="sendOnEnter(event)" placeholder="{e(placeholder)}">{e(value)}</textarea>
<button class="send" type="submit" aria-label="Frage senden" title="Frage senden">{icon("send")}</button></div>
</form>"""


def search_li(row: m.SearchLog) -> str:
    href = ref_href(row.result_ref) or f"/suche?q={quote(row.query)}"
    label = row.result_label or row.query
    kind = (row.result_ref or "").split(":", 1)[0]
    sub = {"person": "Person", "team": "Team", "event": "Anlass"}.get(kind)
    sub_html = f"<span class='sub'>{sub}</span>" if sub else ""
    return (
        f"<li><a href='{e(href)}'>{icon('search', 16)}<span class='nm'><span class='t'>{e(label)}</span>"
        f"{sub_html}</span></a><span class='when'>{when(row.created_at)}</span></li>"
    )


def event_li(x: dict[str, Any]) -> str:
    return (
        f'<li><a href="/event/{quote(x["key"])}">{icon("right", 16)}<span class="nm"><span class="t">'
        f'{e(x["name"])}</span><span class="sub">{e(x["info"])}</span></span></a></li>'
    )


def home_body(searches: list[m.SearchLog], chats: list[m.Chat], events: list[dict[str, Any]]) -> str:
    s_rows = "".join(search_li(r) for r in searches)
    c_rows = "".join(chat_li(c) for c in chats)
    ev_rows = "".join(event_li(x) for x in events)
    return f"""<div class="start">
<section class="card card-s" aria-labelledby="h-suche">
<h1 id="h-suche">{icon("search", 22)}Suche</h1>
<p class="meta">Person, Team oder Anlass finden und die Seite mit Steckbrief, Kader und Quellen öffnen.
Tippfehler, fehlende Umlaute und Ländernamen auf Englisch oder Französisch gehen auch.</p>
{search_box(autofocus=True)}
<div class="acts"><button class="btn ghost small" type="button" onclick="openAdd('person')">{icon("plus", 14)}
Person hinzufügen</button><button class="btn ghost small" type="button" onclick="openAdd('team')">{icon("plus", 14)}
Team hinzufügen</button></div>
</section>
<section class="card card-c" aria-labelledby="h-chat">
<h2 id="h-chat" class="card-h">{icon("chat", 22)}Chat: Karl fragen</h2>
<p class="meta">Eine Frage in ganzen Sätzen, z. B. «{e(wording().example_question)}». Karl antwortet nur aus
den gesammelten Daten, jede Aussage mit Quelle, Vorsicht zuerst.</p>
{ask_form()}
</section>
</div>
<div class="cols">
<section class="recent"><h2>Letzte Suchen</h2>
<ul>{s_rows or "<li class='small'>Noch keine Suchen.</li>"}</ul></section>
<section class="recent"><h2>Letzte Chats</h2>
<ul>{c_rows or "<li class='small'>Noch keine Chats.</li>"}</ul>
{f"<a class='btn small allc' href='/chats'>{icon('list', 14)}Alle Chats</a>" if chats else ""}</section>
{f"<section class='recent'><h2>Aktuelle Anlässe</h2><ul>{ev_rows}</ul></section>" if ev_rows else ""}
</div>"""


def avatar(p: dict[str, Any]) -> str:
    """Small round photo (Wikimedia, credit as tooltip; the full credit is on the person page) or initials."""
    if p.get("foto"):
        return (
            f"<span class='av'><img src='{safe_url(p['foto'])}' alt='' loading='lazy' decoding='async' "
            f"width='40' height='40' title='{e(p.get('foto_credit') or '')}'></span>"
        )
    return f"<span class='av ini' aria-hidden='true'>{e(initials(p.get('name') or ''))}</span>"


_KIND_HEAD = {"personen": "Personen", "teams": "Teams", "anlaesse": "Anlässe"}


def _result_rows(res: dict[str, Any], q: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {"personen": [], "teams": [], "anlaesse": []}
    for p in res["personen"]:
        fakten = p.get("fakten", p["info"])
        badge = f"<span class='rb'>{e(p['badge'])}</span>" if p.get("badge") else ""
        rows["personen"].append(
            f'<a class="pr" href="/person/{p["id"]}?q={q}"><span class="kind">Person</span>{avatar(p)}'
            f'<span class="nm"><span class="t">{e(p["name"])}</span> <span class="k">{e(fakten)}</span></span>'
            f"{badge}</a>"
        )
    for tm in res["teams"]:
        also = tm.get("auch") or []
        rows["teams"].append(
            f'<a class="pr" href="/team/{tm["id"]}?q={q}"><span class="kind">Team</span>'
            f'<span class="av ic" aria-hidden="true">{icon("shield", 18)}</span><span class="nm">'
            f'<span class="t">{e(tm["name"])}</span> <span class="k">{e(tm["info"])}</span></span></a>'
            + (
                "<div class='also'>Auch erfasst als: "
                + ", ".join(f"<a href='/team/{x['id']}?q={q}'>{e(x['name'])}</a>" for x in also)
                + "</div>"
                if also
                else ""
            )
        )
    for ev in res["anlaesse"]:
        rows["anlaesse"].append(
            f'<a class="pr" href="/event/{quote(ev["key"])}?q={q}"><span class="kind">Anlass</span>'
            f'<span class="av ic" aria-hidden="true">{icon("calendar", 18)}</span><span class="nm">'
            f'<span class="t">{e(ev["name"])}</span> <span class="k">{e(ev["info"])}</span></span></a>'
        )
    return rows


def suggest_html(res: dict[str, Any], query: str) -> str:
    """Suggestions while typing (flat list under the field)."""
    q = quote(query)
    rows = [r for group in _result_rows(res, q).values() for r in group]
    text = query.strip()
    if len(rows) >= 2:  # the full result page (grouped, with counts) is one step away
        rows.append(
            f'<a class="all" href="/suche?q={q}"><span class="kind">Suche</span><span class="nm">'
            f"Alle Treffer für «{e(text)}»</span>{icon('right', 16)}</a>"
        )
    if len(text.split()) >= 2:  # a sentence: offer the chat (never sent by itself)
        rows.append(
            f'<button type="button" class="ask-q" onclick="askChat({e(json.dumps(text))})">'
            f'<span class="kind">Chat</span><span class="nm">«{e(text)}» Karl fragen</span>{icon("send", 16)}'
            "</button>"
        )
    if not rows and res.get("meinten"):
        rows += [
            f'<a href="{e(x["href"])}"><span class="kind">Meinten</span><span class="nm">'
            f"{e(x['label'])}?</span></a>"
            for x in res["meinten"]
        ]
    if len(text) >= 3 and not (res["personen"] or res["teams"] or res["anlaesse"]):
        rows.append(
            f'<button type="button" class="add" onclick="openAdd(\'person\',{e(json.dumps(text))})">'
            f'<span class="kind">Neu</span><span class="nm">«{e(text)}» hinzufügen</span></button>'
        )
    return "".join(rows)


def search_page(res: dict[str, Any], query: str, searches: list[m.SearchLog] | None = None) -> str:
    """Full result page: grouped by kind with counts; a question is offered to the chat; no hit → «Meinten
    Sie …?», tips, add / ask. Without a query: the field, recent searches and current events."""
    text = query.strip()
    q = quote(query)
    head = f"<h1>Suche</h1>{search_box(value=query, autofocus=not text)}"
    if not text:
        recent = "".join(search_li(r) for r in (searches or []))
        events = "".join(event_li(x) for x in res.get("anlaesse", []))
        return (
            f"<div class='results'>{head}<p class='meta hint'>Person, Team oder Anlass – ein Teil des Namens "
            "reicht. Fragen in ganzen Sätzen gehören in den <a href='/chat'>Chat</a>.</p>"
            + (f"<section class='recent'><h2>Letzte Suchen</h2><ul>{recent}</ul></section>" if recent else "")
            + (
                f"<section class='recent'><h2>Aktuelle Anlässe</h2><ul>{events}</ul></section>"
                if events
                else ""
            )
            + "</div>"
        )
    ask = (
        f"<div class='askcard'><p><b>Das ist eine Frage?</b> Fragen beantwortet Karl im Chat, mit Quellen.</p>"
        f"<button type='button' class='btn primary' onclick=\"askChat({e(json.dumps(text))})\">{icon('chat', 16)}"
        f"Im Chat fragen</button></div>"
        if looks_like_question(text)
        else ""
    )
    groups = _result_rows(res, q)
    n = sum(len(v) for v in groups.values())
    if n:
        body = "".join(
            f"<section class='rgroup'><h2>{_KIND_HEAD[k]} <span class='n'>{len(v)}</span></h2>"
            f"<div class='suggest-list'>{''.join(v)}</div></section>"
            for k, v in groups.items()
            if v
        )
        return f"<div class='results'>{head}{ask}{body}</div>"
    meinten = res.get("meinten") or []
    dym = (
        "<p class='dym'>Meinten Sie "
        + " oder ".join(f"<a href='{e(x['href'])}'>{e(x['label'])}</a>" for x in meinten)
        + "?</p>"
        if meinten
        else ""
    )
    return f"""<div class='results'>{head}{ask}
<div class='noresult'><h2>Keine Treffer für «{e(text)}»</h2>{dym}
<ul class='tips'><li>Ein Teil des Namens reicht, z. B. nur der Nachname.</li>
<li>Tippfehler und fehlende Umlaute sind kein Problem, Länder gehen auch auf Englisch oder Französisch.</li>
<li>Noch nicht erfasst? Dann hinzufügen – oder Karl im Chat fragen.</li></ul>
<div class='acts'><button class='btn' type='button' onclick="openAdd('person',{e(json.dumps(text))})">{icon("plus", 16)}
«{e(text)}» hinzufügen</button><button class='btn' type='button' onclick="askChat({e(json.dumps(text))})">
{icon("chat", 16)}Im Chat fragen</button></div></div></div>"""


# --- answers -------------------------------------------------------------------------------------------


def statement_li(st: Statement) -> str:
    md = f"{st.zeile_prefix} {st.aussage} «{st.zitat}» {st.quelle_link}"
    label = st.quelle.strip("[]")
    link = (
        f"<a class='src' href='{safe_url(st.url)}' target='_blank' rel='noopener' title='{e(label)}'>"
        f"{e(short_label(label))}</a>"
    )
    de = (
        f"<div class='small translated'>Automatisch übersetzt: «{e(st.zitat_de)}»</div>"
        if getattr(st, "zitat_de", None)
        else ""
    )
    kind = meaning(st.zeile_prefix)
    kind_html = f"<span class='small'>{e(kind)}:</span> " if kind else ""
    return f"<li>{kind_html}{e(st.aussage)} <em>«{e(st.zitat)}»</em> {link}{copy_button(md)}{de}</li>"


def _unique(statements: list[Statement]) -> list[Statement]:
    """Drop exact repeats (same statement, quote and link), keep order."""
    seen: set[tuple[str, str, str]] = set()
    out = []
    for st in statements:
        key = (st.aussage, st.zitat, st.url or "")
        if key not in seen:
            seen.add(key)
            out.append(st)
    return out


def answer_html(res: AnswerResult, uid: str | None = None) -> str:
    if getattr(res, "stil", "liste") == "prosa":
        return prosa_answer_html(res, uid)
    out = ["<div class='answer'>", f"<div class='small'>Datenstand: {e(res.datenstand)}</div>"]
    if getattr(res, "aktualisierung", None):
        out.append(f"<div class='small'>{e(res.aktualisierung)}</div>")
    if getattr(res, "scope_hinweis", None):
        out.append(f"<div class='small'>Verstanden als: {e(res.scope_hinweis)}</div>")
    if getattr(res, "aus_cache", None):
        out.append(f"<div class='small'>Aus dem Zwischenspeicher (Stand {e(when_iso(res.aus_cache))})</div>")
    if res.personen:
        if len(res.personen) == len(res.person_ids) and len(res.personen) <= 8:
            names = ", ".join(
                f"<a href='/person/{pid}?tab=chat'>{e(n)}</a>"
                for n, pid in zip(res.personen, res.person_ids, strict=True)
            )
        else:
            names = e(", ".join(res.personen[:8]) + (" …" if len(res.personen) > 8 else ""))
        out.append(f"<div class='small apers'>Personen: {names}</div>")
    vorsicht = [x for x in _unique(res.statements) if x.vorsicht]
    rest = [x for x in _unique(res.statements) if not x.vorsicht]
    if vorsicht:
        out.append(
            "<section class='vor'><h2>Vorsicht</h2><ul class='lines'>"
            + "".join(statement_li(x) for x in vorsicht)
            + "</ul></section>"
        )
    if rest:
        out.append("<ul class='lines'>" + "".join(statement_li(x) for x in rest) + "</ul>")
    if not res.statements:
        out.append("<p><strong>Keine belegte Angabe zu genau dieser Frage.</strong></p>")
        if getattr(res, "naechste_quellen", None):
            out.append(
                "<div class='small'>Am nächsten dran (beantwortet die Frage nicht direkt):</div><ul class='lines'>"
                + "".join(
                    f"<li>{e(x.get('titel'))} "
                    f"<a class='src' href='{safe_url(x.get('url'))}' target='_blank' rel='noopener'>"
                    f"{e(short_label(x.get('quelle')))}</a></li>"
                    for x in res.naechste_quellen
                )
                + "</ul>"
            )
    if unsure := _unique(res.unbestaetigt):
        out.append(
            f"<details><summary>Unbestätigt ({len(unsure)}): Zitat gefunden, stützt die Aussage aber "
            "nicht sicher</summary><ul class='lines'>"
            + "".join(statement_li(x) for x in unsure)
            + "</ul></details>"
        )
    if res.luecken:
        out.append(
            "<h2>Lücken</h2><ul class='lines'>" + "".join(f"<li>{e(x)}</li>" for x in res.luecken) + "</ul>"
        )
    foot = []
    if res.verworfen:
        foot.append(f"{res.verworfen} Aussagen verworfen (nicht belegbar)")
    if getattr(res, "durchsucht", None):
        foot.append(res.durchsucht)
    foot.append(f"Kosten ${res.kosten_usd:.3f}")
    out.append(f"<p class='small'>{' · '.join(foot)}</p></div>")
    return "".join(out)


# --- v12 prosa answer ----------------------------------------------------------------------------------


def _markers(nums: list[int], uid: str, total: int) -> str:
    """Clickable [1] [2] that jump to the numbered source."""
    return "".join(
        f"<a class='qref' href='#{uid}-q{n}' aria-label='Quelle {n}'>[{n}]</a>"
        for n in nums
        if 1 <= n <= total
    )


def _source_li(n: int, st: Statement, uid: str) -> str:
    label = st.quelle.strip("[]") or "Quelle"
    bits = [f"<span class='qn'>[{n}]</span>"]
    if st.berechnet:
        bits.append("<span class='chip'>Berechnet aus gespeicherten Daten</span>")
    if st.vorsicht:
        bits.append("<span class='chip vchip'>Vorsicht</span>")
    if st.url:
        bits.append(f"<a href='{safe_url(st.url)}' target='_blank' rel='noopener'>{e(label)}</a>")
    else:
        bits.append(e(label))
    if kind := meaning(st.zeile_prefix):
        bits.append(f"<span class='small'>{e(kind)}</span>")
    quote_md = f"«{st.zitat}» {st.quelle_link}"
    body = f"<div class='qz'>«{e(st.zitat)}»{copy_button(quote_md)}</div>"
    if getattr(st, "zitat_de", None):
        body += f"<div class='small translated'>Automatisch übersetzt: «{e(st.zitat_de)}»</div>"
    if st.berechnet and st.basis:
        links = " · ".join(
            f"<a href='{safe_url(b.get('url'))}' target='_blank' rel='noopener'>{e(b.get('quelle') or 'Quelle')}</a>"
            if b.get("url")
            else e(b.get("quelle") or "Quelle")
            for b in st.basis[:12]
        )
        more = f" · … und {len(st.basis) - 12} weitere" if len(st.basis) > 12 else ""
        body += f"<div class='small'>Grundlage: {links}{more}</div>"
    return f"<li id='{uid}-q{n}'><div class='qhead'>{' '.join(bits)}</div>{body}</li>"


def _prosa_block(block: Any, uid: str, total: int) -> str:
    if block.typ == "tabelle":
        head = "".join(f"<th scope='col'>{e(c)}</th>" for c in block.spalten)
        rows = []
        for row in block.zeilen:
            satz = row["satz"]
            cells = "".join(f"<td>{e(c)}</td>" for c in row["zellen"])
            vor = "<span class='chip vchip'>Vorsicht</span> " if satz.ton == "vorsicht" else ""
            rows.append(f"<tr>{cells}<td class='qc'>{vor}{_markers(satz.quellen, uid, total)}</td></tr>")
        return (
            "<div class='atab'><table><thead><tr>"
            f"{head}<th class='qc' scope='col'><span class='sr'>Quellen</span></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    text = " ".join(
        f"<span class='as ton-{e(x.ton)}'>{e(x.text)}{_markers(x.quellen, uid, total)}</span>"
        for x in block.saetze
    )
    return f"<p class='ap'>{text}</p>"


def _closest_sources(res: AnswerResult) -> str:
    if not getattr(res, "naechste_quellen", None):
        return ""
    return (
        "<div class='small'>Am nächsten dran (beantwortet die Frage nicht direkt):</div><ul class='lines'>"
        + "".join(
            f"<li>{e(x.get('titel'))} <a class='src' href='{safe_url(x.get('url'))}' target='_blank' "
            f"rel='noopener'>{e(short_label(x.get('quelle')))}</a></li>"
            for x in res.naechste_quellen
        )
        + "</ul>"
    )


def rueckfrage_html(text: str, suggestions: list[str]) -> str:
    """Part D: the app asks back; a suggestion sends itself like a "Nächste Fragen" chip (askNext)."""
    chips = "".join(
        f"<button type='button' class='nq' data-q='{e(q)}' onclick='askNext(this)'><span>{e(q)}</span></button>"
        for q in suggestions
    )
    return (
        f"<div class='rueckfrage'><p>{e(text)}</p>"
        + (f"<div class='qs'>{chips}</div>" if chips else "")
        + "</div>"
    )


def prosa_answer_html(res: AnswerResult, uid: str | None = None) -> str:
    """v12 web chat answer: written text with clickable [n] markers (tables as real tables), the numbered
    sources, then collapsed: unsure sentences, gaps, what was searched. Everything is escaped: the model's
    JSON is rendered by us, never as HTML."""
    if uid is None:
        import hashlib

        uid = "a" + hashlib.sha1(f"{res.frage}|{res.datenstand}|{len(res.quellen)}".encode()).hexdigest()[:8]
    total = len(res.quellen)
    top = [f"Datenstand: {e(res.datenstand)}"]
    if getattr(res, "scope_hinweis", None):
        top.append(f"Verstanden als: {e(res.scope_hinweis)}")
    out = ["<div class='answer prosa'>", f"<div class='small ameta'>{' · '.join(top)}</div>"]
    if getattr(res, "aktualisierung", None):
        out.append(f"<div class='small'>{e(res.aktualisierung)}</div>")
    if getattr(res, "aus_cache", None):
        out.append(f"<div class='small'>Aus dem Zwischenspeicher (Stand {e(when_iso(res.aus_cache))})</div>")
    if res.rueckfrage and not res.antwort:
        out.append(rueckfrage_html(res.rueckfrage, res.rueckfrage_vorschlaege))
    elif res.antwort:
        out.extend(_prosa_block(b, uid, total) for b in res.antwort)
    else:
        out.append("<p><strong>Dazu habe ich nichts gefunden.</strong></p>")
        out.append(_closest_sources(res))
    cited = {n for b in res.antwort for x in b.saetze + [r["satz"] for r in b.zeilen] for n in x.quellen}
    if res.quellen:
        shown = [(n, st) for n, st in enumerate(res.quellen, 1) if n in cited]
        if shown:
            out.append(
                "<h2 class='qh'>Quellen</h2><ol class='qlist'>"
                + "".join(_source_li(n, st, uid) for n, st in shown)
                + "</ol>"
            )
    if res.nicht_sicher:
        unsure_src = [(n, st) for n, st in enumerate(res.quellen, 1) if n not in cited]
        out.append(
            f"<details class='amore'><summary>Nicht sicher belegt ({len(res.nicht_sicher)})</summary>"
            "<p class='small'>Zitat gefunden, stützt den Satz aber nicht sicher.</p><ul class='lines'>"
            + "".join(f"<li>{e(x.text)}{_markers(x.quellen, uid, total)}</li>" for x in res.nicht_sicher)
            + "</ul><ol class='qlist'>"
            + "".join(_source_li(n, st, uid) for n, st in unsure_src)
            + "</ol></details>"
        )
    if res.luecken:
        out.append(
            f"<details class='amore'><summary>Was die Quellen nicht sagen ({len(res.luecken)})</summary>"
            "<ul class='lines'>" + "".join(f"<li>{e(x)}</li>" for x in res.luecken) + "</ul></details>"
        )
    foot = []
    if res.durchsucht:
        foot.append(e(res.durchsucht))
    if res.verworfen:
        foot.append(f"{res.verworfen} Sätze verworfen (nicht belegbar)")
    foot.append(f"Kosten ${res.kosten_usd:.3f}")
    names = ""
    if res.personen:
        if len(res.personen) == len(res.person_ids) and len(res.personen) <= 8:
            names = ", ".join(
                f"<a href='/person/{pid}?tab=chat'>{e(n)}</a>"
                for n, pid in zip(res.personen, res.person_ids, strict=True)
            )
        else:
            names = e(", ".join(res.personen[:8]) + (" …" if len(res.personen) > 8 else ""))
        names = f"<div class='small apers'>Personen: {names}</div>"
    out.append(
        f"<details class='amore'><summary>Durchsucht</summary><p class='small'>{' · '.join(foot)}</p>"
        f"{names}</details>"
    )
    if res.rueckfrage and res.antwort:
        out.append(rueckfrage_html(res.rueckfrage, res.rueckfrage_vorschlaege))
    if res.naechste_fragen:
        out.append(next_questions(res.naechste_fragen))
    out.append("</div>")
    return "".join(out)


# --- chat ----------------------------------------------------------------------------------------------


def user_bubble(text: str) -> str:
    return f"<div class='msg user'>{e(text)}</div>"


def when_iso(value: str) -> str:
    try:
        return when(datetime.fromisoformat(value))
    except ValueError:
        return value


RATINGS = (("gut", "gut"), ("okay", "okay"), ("schlecht", "schlecht"))


def rating_widget(msg: m.ChatMessage) -> str:
    """v6: Fabi rates every answer (gut / okay / schlecht) and may say why."""
    wid = f"rate-{msg.id}"
    target = f'hx-target="#{wid}" hx-swap="outerHTML"'
    buttons = "".join(
        f"<button class='btn small{' on' if msg.rating == value else ''}' name='rating' value='{value}' "
        f"type='submit' aria-pressed='{'true' if msg.rating == value else 'false'}'>{label}</button>"
        for value, label in RATINGS
    )
    reason = e(msg.rating_reason or "")
    saved = "<span class='small'>gespeichert</span>" if msg.rating else ""
    return f"""<form class="rating" id="{wid}" method="post" action="/bewertung/{msg.id}" hx-post="/bewertung/{msg.id}"
 hx-trigger="submit, change" {target}>
<span class="small">Wie gut war die Antwort?</span> {buttons} {saved}
<input type="text" name="grund" value="{reason}" placeholder="Warum? (optional)" aria-label="Grund"></form>"""


def pending_answer(chat_id: int, delay: str = "", fresh: bool = False) -> str:
    trigger = f"load delay:{delay}" if delay else "load"
    url = f"/chat/{chat_id}/antwort" + ("?frisch=1" if fresh else "")
    return (
        f"<div class='msg bot pending' hx-post='{url}' hx-trigger='{trigger}' "
        "hx-swap='outerHTML'><div class='progress' role='status'><span class='ptext'>Suche Quellen …</span> "
        "<span class='small'>(meist 5–30 s; mit Tages-Update bis 90 s)</span></div></div>"
    )


def bot_message(msg: m.ChatMessage, settings: Settings, s=None) -> str:
    from klugscheisser.chat.service import result_from_json

    if msg.add_request_id is not None:
        if s is None:
            from klugscheisser.db.session import session_scope

            with session_scope() as s2:
                return bot_message(msg, settings, s2)
        req = s.get(m.AddRequest, msg.add_request_id)
        # a refined request is shown as its newest successor
        while req is not None and req.status == "refined":
            nxt = s.scalar(select(m.AddRequest).where(m.AddRequest.parent_id == req.id))
            if nxt is None:
                break
            req = nxt
        return f"<div class='msg bot'>{proposal_card(req, settings, s) if req else e(msg.text)}</div>"
    if msg.answer:
        res = result_from_json(msg.answer)
        again = ""
        if res.aus_cache:
            again = (
                f"<form method='post' action='/chat/{msg.chat_id}/nachricht' hx-post='/chat/{msg.chat_id}/nachricht' "
                f"hx-target='#msgs' hx-swap='beforeend'><input type='hidden' name='frage' value='{e(res.frage)}'>"
                "<input type='hidden' name='frisch' value='1'><button class='btn ghost small' type='submit'>"
                f"{icon('refresh', 14)}Neu beantworten (ohne Zwischenspeicher)</button></form>"
            )
        return f"<div class='msg bot'>{answer_html(res, uid=f'm{msg.id}')}{again}{rating_widget(msg)}</div>"
    return f"<div class='msg bot'><p>{e(msg.text)}</p></div>"


def chat_messages(chat: m.Chat | None, settings: Settings) -> str:
    if chat is None or not chat.messages:
        return ""
    out = []
    for msg in chat.messages:
        out.append(user_bubble(msg.text) if msg.role == "user" else bot_message(msg, settings))
    if chat.messages[-1].role == "user":
        out.append(pending_answer(chat.id))
    return "".join(out)


def next_questions(questions: list[str]) -> str:
    """v9 "Nächste Fragen": chips under the latest answer (phone: right above the input); a tap asks."""
    if not questions:
        return ""
    chips = "".join(
        f"<button type='button' class='nq' data-q='{e(q)}' onclick='askNext(this)'><span>{e(q)}</span></button>"
        for q in questions
    )
    return f"<div class='nextq'><span class='lbl'>Nächste Fragen</span><div class='qs'>{chips}</div></div>"


def chat_panel(
    chat: m.Chat | None,
    settings: Settings,
    context: dict[str, Any] | None = None,
    embedded: bool = False,
    questions: list[str] | None = None,
    below_empty: str = "",
) -> str:
    """Messages + input. Without a chat the form creates one (with the page's person/team/event context).
    `questions`: "Nächste Fragen" hints; they travel with the form so a new chat keeps them."""
    context = context or {}
    questions = questions or []
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{e(v)}">'
        for k, v in context.items()
        if k in ("person", "team", "event") and v is not None
    )
    if embedded:
        hidden += '<input type="hidden" name="eingebettet" value="1">'
    hidden += "".join(f'<input type="hidden" name="vorschlag" value="{e(q)}">' for q in questions)
    label = context.get("label")
    # ui-loop round 4: the field next to the chat is narrow (~190 px): a long event/team name becomes "zum Anlass"
    kind = next((k for k in ("event", "team") if context.get(k) is not None), None)
    if not label:
        placeholder = "Frage stellen …"
    elif len(label) <= 20 or kind is None:
        placeholder = f"Frage zu {label if len(label) <= 26 else label[:24].rsplit(' ', 1)[0].rstrip(',')} …"
    else:
        placeholder = "Frage zum Anlass …" if kind == "event" else "Frage zum Team …"
    if chat is None:
        action = "/chat"
        hx = 'hx-post="/chat" hx-target="#chatpanel" hx-swap="outerHTML"'
        about = f" zu {label}" if label else ""
        empty = (
            f"<div class='chat-empty'>Antworten nur aus den gesammelten Daten{e(about)}, mit Quellen. "
            f"Rückfragen wie «was hat {wording().pronoun_de} dazu gesagt?» verstehen sich im Verlauf."
            f"{below_empty}</div>"
        )
    else:
        action = f"/chat/{chat.id}/nachricht"
        hx = f'hx-post="{action}" hx-target="#msgs" hx-swap="beforeend"'
        empty = ""
    return f"""<section class="chat" id="chatpanel">
<div class="msgs" id="msgs" aria-live="polite">{chat_messages(chat, settings) or empty}</div>
<div class="chatfoot">{next_questions(questions)}
<form class="chatform" method="post" action="{action}" {hx} hx-on::after-request="if(event.detail.successful)this.reset()">
{hidden}<div class="ask"><label class="sr" for="frage-in">{e(placeholder)}</label><textarea id="frage-in" name="frage"
 required rows="1" onkeydown="sendOnEnter(event)" placeholder="{e(placeholder)}"></textarea>
<button class="send" type="submit" aria-label="Senden" title="Senden">{icon("send")}</button></div>
<p class="small hint">Enter sendet · Shift+Enter neue Zeile · «Füge … hinzu» schlägt neue Personen vor</p>
</form></div></section>"""


def chat_page(chat: m.Chat | None, settings: Settings, recent: list[m.Chat]) -> str:
    title = e(chat.title) if chat else "Chat: Karl fragen"
    ctx = ""
    if chat is not None:
        bits = []
        if chat.athlete_id:
            bits.append(f"<a href='/person/{chat.athlete_id}?tab=chat'>Personenseite</a>")
        if chat.team_id:
            bits.append(f"<a href='/team/{chat.team_id}'>Teamseite</a>")
        bits.append("<a href='/chat'>Neuer Chat</a>")
        ctx = f"<div class='small'>{' · '.join(bits)}</div>"
    others = "".join(chat_li(c) for c in recent if chat is None or c.id != chat.id)
    # a new chat looks like an open one: messages area on top (here: the recent chats), input at the bottom
    more = (
        f"<section class='recent' id='recentchats'><h2>Letzte Chats</h2><ul>{others}</ul>"
        f"<a class='btn small allc' href='/chats'>{icon('list', 14)}Alle Chats</a></section>"
        if others and chat is None
        else ""
    )
    return (
        f"<div class='results chatpage'><h1 id='chattitle'>{title}</h1>{ctx}"
        f"{chat_panel(chat, settings, below_empty=more)}</div>"
    )


def chats_list(chats: list[m.Chat]) -> str:
    rows = "".join(chat_li(c) for c in chats)
    return (
        f"<div class='results'><h1>Alle Chats</h1><p><a class='btn' href='/chat'>{icon('plus', 14)}Neuer Chat</a></p>"
        f"<section class='recent'><ul>{rows or '<li class=small>Noch keine Chats.</li>'}</ul></section></div>"
    )


# --- add proposals -------------------------------------------------------------------------------------


def _src_links(sources: list[Any]) -> str:
    out = []
    for src in sources[:3]:
        label = getattr(src, "label", None) or "Quelle"
        url = getattr(src, "url", None)
        out.append(
            f"<a href='{safe_url(url)}' target='_blank' rel='noopener'>[{e(label)}]</a>" if url else e(label)
        )
    return " ".join(out)


def _person_li(p: Any, skip_url: str | None = None) -> str:
    bits = [f"<strong>{e(p.name)}</strong>"]
    if p.function_de:
        bits.append(e(p.function_de))
    elif p.position:
        bits.append(
            e(
                {
                    "G": "Torhüterin",
                    "D": "Verteidigerin",
                    "F": "Stürmerin",
                    **current_spec().position_labels_de,
                }.get(p.position, p.position)
            )
        )
    if p.existing_id:
        bits.append("<span class='chip'>schon erfasst</span>")
    if p.instagram:
        check = f" ({e(p.instagram_check)})" if p.instagram_check else ""
        src = " von Fabi" if p.instagram_source == "fabi" else " laut Wikidata"
        bits.append(f"Instagram @{e(p.instagram)}<span class='small'>{src}{check}</span>")
    own = [x for x in p.sources if not skip_url or getattr(x, "url", None) != skip_url]
    return f"<li>{' · '.join(bits)} {_src_links(own)}</li>"


def proposal_card(req: m.AddRequest, settings: Settings, s=None) -> str:
    cid = f"add-{req.id}"
    target = f'hx-target="#{cid}" hx-swap="outerHTML"'
    if req.status == "resolving":
        return (
            f"<div class='proposal' id='{cid}'><div class='progress' hx-get='/hinzufuegen/{req.id}' "
            f"hx-trigger='load delay:3s' hx-target='#{cid}' hx-swap='outerHTML'>Stelle die Vorbereitung "
            "zusammen: Kader, Trainer und Staff aller Teams (ca. 1 Minute) …</div></div>"
        )
    if req.proposal is None:
        return f"<div class='proposal' id='{cid}'><p class='err'>Kein Vorschlag ({e(req.error)}).</p></div>"
    p = Proposal.from_json(req.proposal)
    parts = [f"<div class='proposal' id='{cid}'>"]
    head = {"team": "Team", "event": "Vorbereitung"}.get(p.kind, "Person")
    if req.refine_text:
        head += f" <span class='small'>· präzisiert: «{e(req.refine_text)}»</span>"
    parts.append(f"<div class='small'>{head}</div><p class='sum'>{e(p.summary)}</p>")
    if p.team and p.team.roster_source:
        parts.append(f"<div class='small'>Kader: {_src_links([p.team.roster_source])}</div>")
    roster_url = getattr(p.team.roster_source, "url", None) if p.team and p.team.roster_source else None
    if p.persons:
        shown = p.persons if p.kind == "person" else p.persons[:8]
        rest = len(p.persons) - len(shown)
        parts.append("<ul>" + "".join(_person_li(x, roster_url) for x in shown) + "</ul>")
        if rest > 0:
            parts.append(
                f"<details><summary class='small'>… und {rest} weitere</summary><ul>"
                + "".join(_person_li(x, roster_url) for x in p.persons[8:])
                + "</ul></details>"
            )
    for note in p.notes:
        parts.append(f"<div class='small'>Hinweis: {e(note)}</div>")
    if p.alternatives:
        alts = "".join(f"<li>{e(x)}</li>" for x in p.alternatives[:5])
        parts.append(
            f"<details class='small'{'' if p.found else ' open'}><summary>Andere Treffer, nicht gewählt ({len(p.alternatives[:5])})</summary>"
            f"<ul>{alts}</ul></details>"
        )
    cap = settings.event_prep_cap_chf if p.kind == "event" else settings.add_cost_cap_chf
    cost = f"Geschätzte Kosten: ~{p.cost_estimate_chf:.2f} CHF (Limit {cap:.0f} CHF pro Aktion)"
    if p.cost_estimate_chf > cap:
        cost += ". Über dem Limit: Instagram wird nur für so viele Personen geladen, wie das Limit erlaubt."
    if req.status == "proposed":
        parts.append(f"<div class='small'>{e(cost)}</div>")
        yes = (
            f"<button class='btn primary' hx-post='/hinzufuegen/{req.id}/ja' {target} hx-disabled-elt='this'>"
            + ("Ja, vorbereiten" if p.kind == "event" else "Ja, hinzufügen")
            + "</button>"
            if p.found
            else ""
        )
        parts.append(
            f"""<div class="yesno">{yes}
<button class="btn" hx-post="/hinzufuegen/{req.id}/nein" {target}>Nein</button>
<button class="btn" type="button" onclick="this.parentElement.nextElementSibling.hidden=false;
this.parentElement.nextElementSibling.querySelector('textarea').focus()">Präzisieren</button></div>
<form hidden method="post" action="/hinzufuegen/{req.id}/praezisieren" hx-post="/hinzufuegen/{req.id}/praezisieren"
 {target} hx-disabled-elt="find button"><label class="small">Was stimmt nicht / was genau?</label>
<textarea name="text" required placeholder="z. B. die Schwedin, Jahrgang 2001, spielt bei Luleå"></textarea>
<p><button class="btn primary" type="submit">Neuer Vorschlag</button></p>
<div class="indicator progress">Suche erneut …</div></form>"""
        )
    elif req.status == "rejected":
        parts.append("<p>Verworfen. Nichts hinzugefügt.</p>")
    elif req.status == "loading":
        parts.append(load_status(req, s))
    elif req.status == "done":
        parts.append(done_links(req, s))
    elif req.status == "failed":
        parts.append(f"<p class='err'>Laden fehlgeschlagen: {e(req.error)}</p>{done_links(req, s)}")
    parts.append("</div>")
    return "".join(parts)


def load_status(req: m.AddRequest, s) -> str:
    step = "startet"
    if s is not None and req.job_id:
        job = s.get(m.Job, req.job_id)
        step = ((job.result or {}).get("schritt") if job else None) or "startet"
    return (
        f"<div class='progress' hx-get='/hinzufuegen/{req.id}' hx-trigger='load delay:3s' "
        f"hx-target='#add-{req.id}' hx-swap='outerHTML'>Hinzugefügt. Lade Daten im Hintergrund: "
        f"{e(step)} …</div>"
    )


def done_links(req: m.AddRequest, s) -> str:
    links = []
    if s is not None:
        for aid in (req.athlete_ids or [])[:12]:
            a = s.get(m.Athlete, aid)
            if a is not None:
                links.append(f"<a href='/person/{a.id}'>{e(a.full_name)}</a>")
        more = len(req.athlete_ids or []) - len(links)
        if more > 0:
            links.append(f"… und {more} weitere")
    team = f" · Team: <a href='/team/{req.team_id}'>öffnen</a>" if req.team_id else ""
    later = ""
    job = s.get(m.Job, req.job_id) if s is not None and req.job_id else None
    if job is not None and (skipped := (job.result or {}).get("instagram_spaeter")):
        later = (
            f"<div class='small'>Instagram wegen Kostenlimit noch nicht geladen für {len(skipped)} Personen "
            "(lädt beim ersten Aufruf).</div>"
        )
    status = "Fertig" if req.status == "done" else "Teilweise"
    return f"<p>{status}: {', '.join(links) or 'keine Personen'}{team}</p>{later}"


# --- sources -------------------------------------------------------------------------------------------


def sources_page(sources: list[Any]) -> str:
    """ui-loop round 3: a jump bar per kind (with how many are on), sources as compact cards (two columns on wide
    screens), on/off as a clear label."""
    import re as _re

    groups: dict[str, list[Any]] = {}
    for src in sources:
        groups.setdefault(src.group, []).append(src)

    def anchor(group: str) -> str:
        return "q-" + (_re.sub(r"[^a-z0-9]+", "-", group.lower()).strip("-") or "x")

    jumps = "".join(
        f"<a class='fchip' href='#{anchor(g)}'><span class='in'>{e(g)}<span class='n'>"
        f"{sum(1 for x in rows if x.active)}/{len(rows)}</span></span></a>"
        for g, rows in groups.items()
    )
    out = [
        "<div class='bhead'><h1>Quellen</h1>",
        "<p class='meta'>Woher die Daten kommen, was wir davon nehmen und was die Nutzungsbedingungen sagen. "
        "Jede Aussage in einer Antwort verlinkt auf ihre Quelle.</p></div>",
        f"<nav class='chips bjump' aria-label='Springen zu'>{jumps}</nav>",
    ]
    for group, rows in groups.items():
        items = []
        for src in rows:
            state = "<span class='qon'>aktiv</span>" if src.active else "<span class='qoff'>aus</span>"
            items.append(
                f"<li><div class='qh'><a href='{safe_url(src.url)}' target='_blank' rel='noopener'>"
                f"{e(src.name)}</a>{state}</div>"
                f"<div class='qt'>{e(src.takes)}</div><div class='small'>{e(src.terms)}</div></li>"
            )
        n_on = sum(1 for x in rows if x.active)
        out.append(
            f"<section id='{anchor(group)}' aria-label='{e(group)}'><div class='sh'><h2>{e(group)}</h2>"
            f"<span class='n'>{n_on} von {len(rows)} aktiv</span></div><ul class='qlist'>{''.join(items)}</ul>"
            "</section>"
        )
    return "".join(out)


def person_sources_box(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<h2>Datenquellen</h2><p class='small'>Noch keine Quellen zu dieser Person.</p>"
    items = []
    for r in rows:
        name = (
            f"<a href='{safe_url(r['url'])}' target='_blank' rel='noopener'>{e(r['name'])}</a>"
            if r.get("url")
            else e(r["name"])
        )
        items.append(f"<li><span>{name}</span><span class='small'>{e(r.get('info'))}</span></li>")
    return (
        "<h2>Datenquellen</h2><ul class='lines sources'>"
        + "".join(items)
        + "</ul><p class='small'><a href='/quellen'>Alle Quellen</a></p>"
    )


def ratings_page(rows: list[tuple[m.ChatMessage, str]], counts: dict[str, int]) -> str:
    """Review list for Ondřej: every answer with its question, rating and reason (newest first)."""
    head = " · ".join(f"{label}: {counts.get(value, 0)}" for value, label in RATINGS)
    items = []
    for msg, question in rows:
        badge = dict(RATINGS).get(msg.rating or "", "nicht bewertet")
        reason = f"<div class='small'>Grund: {e(msg.rating_reason)}</div>" if msg.rating_reason else ""
        zeiten = (msg.answer or {}).get("zeiten") or {}
        z = " · ".join(f"{k} {v}s" for k, v in zeiten.items())
        items.append(
            f"<li><div><strong>{e(badge)}</strong> · <a href='/chat/{msg.chat_id}'>{e(question)}</a> "
            f"<span class='when'>{when(msg.created_at)}</span></div>{reason}"
            f"<div class='small'>{e(msg.text[:200])}</div><div class='small'>{e(z)}</div></li>"
        )
    return (
        f"<h1>Bewertungen</h1><p class='meta'>{e(head)} · <a href='/bewertungen.json'>Export (JSON)</a></p>"
        f"<ul class='lines'>{''.join(items) or '<li class=small>Noch keine Antworten.</li>'}</ul>"
    )
