"""v9: the person page's one dated timeline (Instagram, news, results, web, podcasts/videos, events) and the
"Nächste Fragen" hints for the chat. Pure functions over the page data (web.data.person_data); no model call."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

from klugscheisser.projects import wording
from klugscheisser.web.html import e, fact_of, icon, meaning, safe_url, short_label

MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]
# filter key, chip label
# ui-loop round 4: "Steckbrief" added; the chips follow the page order, like a table of contents
FILTERS = (
    ("alle", "Alles"),
    ("steckbrief", "Steckbrief"),
    ("statistik", "Statistik"),
    ("erfolge", "Erfolge"),
    ("funfacts", "Fun Facts"),
    ("vorsicht", "Vorsicht"),
    ("social", "Social"),
    ("news", "News"),
    ("podcasts", "Podcasts"),
)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _url_key(url: str | None) -> str:
    return unquote(url or "").rstrip("/").lower()


def _kind(item: dict[str, Any]) -> tuple[str, str]:
    """(filter key, label) of a stored item."""
    st = item.get("source_type")
    quelle = item.get("quelle") or ""
    if item.get("account") or quelle.startswith("[Instagram"):
        return "social", "Instagram"
    if quelle.startswith("[X "):
        return "social", "X"
    if st == "media_link":
        url = (item.get("url") or "").lower()
        podcast = any(x in url for x in ("spotify", "podcast", "anchor.fm", "soundcloud"))
        return "podcasts", "Podcast" if podcast or "podcast" in quelle.lower() else "Video"
    if st == "official_result":
        return "news", "Resultat"
    if "wikipedia" in (item.get("url") or ""):
        return "news", "Wikipedia"
    if st in ("profile", "interview", "web"):  # the site itself is in "src"; no bare "Web" tag (2026-11-01)
        return "news", {"profile": "Profil", "interview": "Interview"}.get(st, "Webseite")
    return "news", "News"


def item_entry(item: dict[str, Any]) -> dict[str, Any]:
    key, label = _kind(item)
    title = fact_of(item, 180)
    bits = [meaning(item.get("zeile_prefix"))]
    if de := item.get("titel_de") or item.get("zusammenfassung_de"):
        bits.append(f"Deutsch: {de}")
    elif item.get("bildbeschreibung"):
        bits.append(f"Bild: {item['bildbeschreibung']}")
    elif item.get("titel") and item.get("zusammenfassung") and item["zusammenfassung"] != item["titel"]:
        bits.append(item["zusammenfassung"])
    return {
        "k": key + (" vorsicht" if item.get("vorsicht") else ""),
        "label": label,
        "vorsicht": bool(item.get("vorsicht")),
        "title": title or item.get("url"),
        "sub": " · ".join(b for b in bits if b),
        "url": item.get("url"),
        "src": short_label(item.get("quelle")) if key != "podcasts" else _host_label(item),
        "when": _when(item.get("published_at")),
        "extern": True,
    }


def _host_label(item: dict[str, Any]) -> str:
    label = short_label(item.get("quelle"))
    if label and label.upper() != "VIDEO":
        return label
    return (urlparse(item.get("url") or "").hostname or "").removeprefix("www.")


def event_entry(ev: dict[str, Any]) -> dict[str, Any]:
    status = {"confirmed": "bestätigt", "provisional": "provisorisch"}.get(ev.get("kader_status") or "", "")
    src = ev.get("quelle") or ""
    return {
        "k": "event",
        "label": "Anlass",
        "vorsicht": False,
        "title": ev["name"],
        "sub": f"Im Kader – {status}" if status else "",
        "url": ev.get("url") or f"/event/{ev['key']}",
        "src": src if len(src) <= 24 else (urlparse(ev.get("url") or "").hostname or "").removeprefix("www."),
        "when": _when(ev.get("start")),
        "extern": bool(ev.get("url")),
    }


TERM_DE = {
    "year": "Jahr",
    "years": "Jahre",
    "month": "Monat",
    "months": "Monate",
    "day": "Tag",
    "days": "Tage",
}
TRANSFER_DE = {
    "extension": "Vertragsverlängerung",
    "loan": "Leihe",
    "retired": "Rücktritt",
    "retirement": "Rücktritt",
    "trade": "Tausch",
}


def _term(extra: str | None) -> str:
    """EP's 'extra' ('2 years', '1 year', 'PTO') in German where it is a contract length."""
    words = (extra or "").strip().split()
    if len(words) == 2 and words[0].isdigit() and words[1].lower() in TERM_DE:
        return f"{words[0]} {TERM_DE[words[1].lower()]}"
    return (extra or "").strip()


def _team_name(x: Any) -> str:
    return (x.get("name") or "") if isinstance(x, dict) else (x or "")


def transfer_entry(t: dict[str, Any], ep_url: str | None = None) -> dict[str, Any]:
    """v11: one transfer from ep_profiles.data["transfers"]: 'Wechsel zu PWHL Detroit, 3 Jahre', linked to its
    source (often the club's press release), else to her Elite Prospects page."""
    to, frm = _team_name(t.get("to")), _team_name(t.get("from"))
    kind = TRANSFER_DE.get(str(t.get("type") or "").lower())
    if not kind and to and to == frm:
        kind = "Vertragsverlängerung"
    if kind == "Vertragsverlängerung":
        title = f"Vertragsverlängerung bei {to}" if to else kind
    elif kind and to:
        title = f"{kind}: {to}"
    else:
        title = f"Wechsel zu {to}" if to else (kind or "Wechsel")
    term = _term(t.get("extra"))
    if term:
        title += f", {term}"
    url = t.get("source_url") or ep_url
    host = (urlparse(t.get("source_url") or "").hostname or "").removeprefix("www.")
    return {
        "k": "wechsel",
        "label": "Wechsel",
        "vorsicht": False,
        "title": title,
        "sub": f"von {frm}" if frm and frm != to else "",
        "url": url,
        "src": host or "Elite Prospects",
        "when": _when(t.get("date")),
        "extern": bool(url),
    }


def entries(p: dict[str, Any]) -> list[dict[str, Any]]:
    """All dated things about her, each once (same link = same entry), Vorsicht items marked."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    lists = ("vorsicht", "instagram", "x", "news", "resultate", "profil", "medien")
    for name in lists:
        for item in p.get(name) or []:
            key = _url_key(item.get("url"))
            if key and key in seen:
                continue
            seen.add(key)
            out.append(item_entry(item))
    out += [event_entry(ev) for ev in p.get("anlaesse") or []]
    ep = p.get("ep") or {}
    for t in ep.get("transfers") or []:
        entry = transfer_entry(t, ep.get("url"))
        key = _url_key(t.get("source_url"))
        if key and key in seen:  # the club's press release is already a news / web entry: say what it is
            if any(x["vorsicht"] and _url_key(x.get("url")) == key for x in out):
                continue  # a Vorsicht report stays one
            out = [x for x in out if _url_key(x.get("url")) != key]
        seen.add(key)
        out.append(entry)
    return out


def grouped(items: list[dict[str, Any]], now: datetime | None = None) -> list[tuple[str, list[dict]]]:
    """'Demnächst' (upcoming, soonest first), then one group per month (newest first), undated last."""
    now = now or datetime.now(UTC)
    upcoming = sorted((x for x in items if x["when"] and x["when"] > now), key=lambda x: x["when"])
    past = sorted((x for x in items if x["when"] and x["when"] <= now), key=lambda x: x["when"], reverse=True)
    groups: list[tuple[str, list[dict]]] = []
    if upcoming:
        for x in upcoming:
            x["up"] = True
        groups.append(("Demnächst", upcoming))
    for x in past:
        label = f"{MONTHS[x['when'].month - 1]} {x['when'].year}"
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(x)
    if undated := [x for x in items if not x["when"]]:
        groups.append(("Ohne Datum", undated))
    return groups


def row_html(x: dict[str, Any]) -> str:
    from klugscheisser.domain.labels import local

    day = local(x["when"]).strftime("%d.%m.") if x["when"] else "–"
    cls = "tl" + (" up" if x.get("up") else "") + (" warn" if x["vorsicht"] else "")
    target = ' target="_blank" rel="noopener"' if x["extern"] else ""
    href = safe_url(x["url"]) if x["extern"] else e(x["url"])
    kind = (
        "<span class='k v'>Vorsicht</span>" if x["vorsicht"] else ""
    ) + f"<span class='k'>{e(x['label'])}</span>"
    sub = f"<span class='x'>{e(x['sub'])}</span>" if x["sub"] else ""
    src = f"<span class='src'>{e(x['src'])}</span>" if x["src"] else ""
    # phone: the source chip sits in the second line (more room for the title); wide screens: on the right
    src2 = f"<span class='src s2'>{e(x['src'])}</span>" if x["src"] else ""
    ext = icon("ext", 14) if x["extern"] else icon("right", 14)
    return (
        f'<a class="{cls}" href="{href}"{target} data-k="{e(x["k"])}" data-url="{e(x["url"])}">'
        f'<span class="d">{e(day)}</span><span class="rail"></span>'
        f'<span class="b"><span class="t">{e(x["title"])}</span><span class="s">{kind}{src2}{sub}</span></span>'
        f'<span class="r">{src}{ext}</span></a>'
    )


def timeline_html(items: list[dict[str, Any]], now: datetime | None = None, keep: int | None = None) -> str:
    """Month groups of rows. `keep` (person page): whole months are shown until at least `keep` rows, the older
    months sit collapsed behind "Ältere Einträge anzeigen (n)" (only when at least 5 more rows are hidden)."""
    groups = grouped(items, now)
    shown, cut = 0, len(groups)
    if keep:
        for i, (_, rows) in enumerate(groups):
            if shown >= keep:
                cut = i
                break
            shown += len(rows)
        if sum(len(r) for _, r in groups[cut:]) < 5:
            cut = len(groups)

    def part(gs: list[tuple[str, list[dict]]]) -> str:
        out = []
        for label, rows in gs:
            out.append(f"<div class='tl-m'>{e(label)}</div>")
            out += [row_html(x) for x in rows]
        return "".join(out)

    if cut >= len(groups):
        return part(groups)
    rest = sum(len(r) for _, r in groups[cut:])
    return (
        f"<div class='tlw cut'>{part(groups[:cut])}<div class='tl-old'>{part(groups[cut:])}</div>"
        f"<button type='button' class='more-btn tl-more' onclick='tlMore(this)'>Ältere Einträge anzeigen "
        f"<span class='n'>({rest})</span>{icon('down', 16)}</button></div>"
    )


def counts(
    items: list[dict[str, Any]], erfolge: int, funfacts: int, seasons: int = 0, facts: int = 0
) -> list[tuple[str, str, int]]:
    """Chip counts; `seasons` (v11) = seasons in the Statistik section, `facts` = sourced Steckbrief facts (both
    not part of "Alles")."""
    n = {key: sum(1 for x in items if key in x["k"].split()) for key, _ in FILTERS}
    n["erfolge"], n["funfacts"], n["statistik"], n["steckbrief"] = erfolge, funfacts, seasons, facts
    n["alle"] = len(items) + erfolge + funfacts
    return [(key, label, n[key]) for key, label in FILTERS]


# --- "Nächste Fragen" -----------------------------------------------------------------------------------


def _short(text: str, words: int = 6) -> str:
    parts = text.split()
    return " ".join(parts[:words]).rstrip(".,;:") + (" …" if len(parts) > words else "")


def person_questions(p: dict[str, Any], fun_facts: list, now: datetime | None = None) -> list[str]:
    """Three hints from her own data: the newest Vorsicht topic, a fun fact, the latest/next event."""
    now = now or datetime.now(UTC)
    out = []
    if p.get("vorsicht"):
        v = max(p["vorsicht"], key=lambda i: i.get("published_at") or "")
        topic = v.get("titel_de") or v.get("titel") or v.get("zusammenfassung") or ""
        if topic:
            out.append(f"Was ist bekannt zu «{_short(topic)}»?")
    if fun_facts:
        out.append(f"Mehr dazu: «{_short(fun_facts[0].value_text)}»")
    events = [x for x in p.get("anlaesse") or [] if _when(x.get("start"))]
    upcoming = sorted((x for x in events if _when(x["start"]) > now), key=lambda x: _when(x["start"]))
    past = sorted(
        (x for x in events if _when(x["start"]) <= now), key=lambda x: _when(x["start"]), reverse=True
    )
    if upcoming:
        out.append(f"Wie ist der Stand vor {upcoming[0]['name']}?")
    elif past:
        out.append(f"Wie lief {past[0]['name']}?")
    for extra in ("Was gibt es Neues?", wording().example_question):
        if len(out) < 3 and extra not in out:
            out.append(extra)
    return out[:3]


TEAM_QUESTIONS = ["Wer hat die meisten Länderspiele?", "Wer spielt im Ausland?", "Wer ist verletzt?"]
