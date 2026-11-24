"""Turn DB rows into self-citing tool output (German labels, Vorsicht flag, verbatim quotes)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser import tiers
from klugscheisser.classify.base import (
    CATEGORY_DE,
    MOOD_EMOJI,
    SOURCE_KIND_EMOJI,
    STORYLINE_DE,
    is_vorsicht,
    vorsicht_reason,
)
from klugscheisser.db import models as m
from klugscheisser.domain.labels import SOURCE_TYPE_DE, TZ, date_de, local, source_label
from klugscheisser.handles.service import reason_de, usable_handles
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec

QUELLE = "klugscheisser_athlete pipeline (verifiziert)"
HINWEIS = (
    "Verifizierte Daten aus der klugscheisser_athlete-Pipeline, getrennt von eigener Websuche halten "
    "('Verifiziert' vs. 'Zusätzlich aus dem Web'). PFLICHT: (1) Antwort beginnt mit 'Datenstand: ' + Feld "
    "'datenstand'. (2) Jede Zeile = 'zeile_prefix' + eine Aussage + 'quelle_link' (unverändert als Markdown-Link); "
    "keine Zeile ohne Link. (3) Einträge mit vorsicht=true zuerst und vollständig. (4) Zitate (is_quote=true) "
    "wörtlich in Anführungszeichen; automatisch transkribierte als solche kennzeichnen. (5) Keine Instagram-Handles "
    "verwenden, die nicht aus diesen Tools stammen."
)
SUMMARY_MAX = 220
QUOTE_RX = re.compile(r"«([^»]{8,600})»|„([^“”]{8,600})[“”]|\"([^\"]{8,600})\"|“([^”]{8,600})”")


def relative_de(dt: datetime | None) -> str:
    if dt is None:
        return "noch nie"
    d = local(dt)
    today = datetime.now(TZ).date()
    if d.date() == today:
        return f"heute {d:%H:%M}"
    if (today - d.date()).days == 1:
        return f"gestern {d:%H:%M}"
    return d.strftime("%d.%m.%Y %H:%M")


def datenstand_times(session: Session, athlete_ids: list[int] | None = None) -> dict[str, datetime | None]:
    """Last fetch of Instagram (for these athletes), news and results; the web app shows the age of each."""
    hq = select(func.max(m.Handle.last_scraped_at)).where(m.Handle.status.in_(["verified", "wahrscheinlich"]))
    if athlete_ids:
        hq = hq.where(m.Handle.athlete_id.in_(athlete_ids))
    instagram = session.scalar(hq)
    news = session.scalar(
        select(func.max(m.CacheEntry.fetched_at)).where(m.CacheEntry.source == "news")
    ) or session.scalar(select(func.max(m.Item.scraped_at)).where(m.Item.platform == "rss"))
    results = session.scalar(
        select(func.max(m.CacheEntry.fetched_at)).where(m.CacheEntry.source.like("results%"))
    ) or session.scalar(select(func.max(m.Item.scraped_at)).where(m.Item.source_type == "official_result"))
    return {"Instagram": instagram, "News": news, "Resultate": results}


def datenstand(session: Session, athlete_ids: list[int] | None = None) -> str:
    """'Instagram: heute 09:12 · News: heute 09:10 · Resultate: gestern 21:40' for the athletes in the answer."""
    times = datenstand_times(session, athlete_ids)
    return " · ".join(f"{label}: {relative_de(dt)}" for label, dt in times.items())


def envelope(session: Session, athlete_ids: list[int] | None = None, **payload: Any) -> dict[str, Any]:
    return {
        "datenstand": datenstand(session, athlete_ids),
        "quelle": QUELLE,
        "hinweis": HINWEIS,
        **payload,
    }


def zeile_prefix(source_kind: str | None, mood: str | None, platform: str | None = None) -> str:
    emoji, meaning = SOURCE_KIND_EMOJI.get(source_kind or "other", SOURCE_KIND_EMOJI["other"])
    if platform == "x":
        emoji = (
            "𝕏"  # X posts: platform mark instead of the camera, meaning stays (von ihr selbst, offiziell …)
        )
    prefix = f"{emoji} ({meaning})"
    if mood in MOOD_EMOJI:
        m_emoji, m_meaning = MOOD_EMOJI[mood]
        prefix += f" {m_emoji} ({m_meaning})"
    return prefix


SENTENCE_MIN = 25  # a shorter first sentence is joined with the next one
# Sentence end: . ! ? before a space or the end – but not after an ordinal / date ("In der 56. Minute",
# "vom 28. bis 31. Oktober", "am 13.10. in Bern"; a score "3:2." does end a sentence), a single letter
# ("z. B.", "A. Müller") or a common abbreviation (gap loop round 2: "Zu beachten" showed "In der 56.").
_ABBREV_WORDS = (
    "bzw ca dr nr st mio mrd inkl evtl vs usw resp prof hr fr jh jg geb std min sek ggf abs art bd tel ehem jun "
    "sen mr mrs ms jr no vgl ff u.a z.b d.h u.s.w"
)
_ABBREV = set(_ABBREV_WORDS.split())
_END = re.compile(r"[.!?](?=\s|$)")
_SCORE = re.compile(r"\d+:\d+$")


def _sentence_end(t: str, pos: int) -> bool:
    """Is the . ! ? at `pos` the end of a sentence?"""
    if t[pos] in "!?":
        return True
    word = t[:pos].rsplit(" ", 1)[-1].strip("([«\"'")
    if not word:
        return True
    if word[-1].isdigit():  # ordinal / date ("56.", "13.10.") – a score ("3:2.") ends the sentence
        return bool(_SCORE.search(word))
    if len(word) == 1 and word.isalpha():  # initial or "z. B."
        return False
    return word.casefold() not in _ABBREV


def _sentence_ends(t: str):
    return (m_.end() for m_ in _END.finditer(t) if _sentence_end(t, m_.start()))


def first_sentence(text: str | None, limit: int = SUMMARY_MAX) -> str:
    """Factual one-liner from the caption itself (no interpretation): first sentence or line."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text.strip())
    s = t
    for end in _sentence_ends(t):
        head = t[:end].strip()
        if len(head) >= SENTENCE_MIN or end >= len(t):  # "Tor!" alone says nothing: take the next one too
            s = head
            break
    if len(s) <= limit:
        return s
    cut = s[:limit]
    return cut[: cut.rfind(" ")] + " […]"


def quotes(text: str | None) -> list[dict[str, Any]]:
    out = []
    for match in QUOTE_RX.finditer(text or ""):
        q = next(g for g in match.groups() if g)
        if len(q.split()) >= 4:  # skip short scare quotes / titles
            out.append({"text": q.strip(), "is_quote": True})
    return out


COMPACT_KEYS = (
    "zeile_prefix",
    "quelle",
    "quelle_link",
    "url",
    "vorsicht",
    "kategorie",
    "storyline",
    "zusammenfassung",
)


def item_compact(item: m.Item) -> dict[str, Any]:
    """Short form for list views (events): no full text; details via get_athlete / get_athlete_posts."""
    full = item_out(item)
    out = {k: full[k] for k in COMPACT_KEYS if full.get(k) not in (None, False)}
    out["zusammenfassung"] = first_sentence(item.text, limit=140)
    _add_german(item, out)
    return out


def _add_german(item: m.Item, out: dict[str, Any]) -> None:
    """v8: stored German translation of title/summary for documents neither in German nor English (lookup only;
    translations are made by the backfill, never while a page renders)."""
    from sqlalchemy.orm import object_session

    from klugscheisser import translate

    lang = translate.item_lang(item)
    s = object_session(item)
    if s is None or not translate.needs(lang):
        return
    if de := translate.cached(s, first_sentence(item.text)):
        out["zusammenfassung_de"] = de
    if item.title and (de := translate.cached(s, item.title)):
        out["titel_de"] = de
    out["sprache"] = translate.label(lang)


def item_out(item: m.Item, scores: dict | None = None) -> dict[str, Any]:
    c = item.classification
    own = item.source_type == "athlete_post"
    label = source_label(item.platform, item.source_name, item.published_at)
    out: dict[str, Any] = {
        "zeile_prefix": zeile_prefix(item.source_kind, c.mood if c else None, item.platform),
        "quelle": label,
        "quelle_link": f"[{label.strip('[]')}]({item.url})",
        "url": item.url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "datum": date_de(item.published_at, with_year=True),
        "source_type": item.source_type,
        "art": SOURCE_TYPE_DE.get(item.source_type, item.source_type),
        "verified": True,
        "vorsicht": is_vorsicht(c),
        "vorsicht_grund": vorsicht_reason(c) if is_vorsicht(c) else None,
        "kategorie": CATEGORY_DE.get(c.category, c.category) if c else None,
        "storyline": STORYLINE_DE.get(c.storyline) if c and c.storyline else None,
        "relevanz": c.newsworthiness if c else None,
        "athletinnen": sorted(ia.athlete.full_name for ia in item.athletes if ia.athlete),
    }
    if item.title:
        out["titel"] = item.title
    out["zusammenfassung"] = first_sentence(item.text)
    out["text"] = item.text
    _add_german(item, out)
    if own:
        out["eigene_worte"] = True  # caption written by the athlete herself; quote verbatim if quoted
    if item.image_description:
        out["bildbeschreibung"] = item.image_description
    q = quotes(item.text)
    media = (item.raw or {}).get("media") or {}
    q += [
        {"text": t, "is_quote": True, "automatisch_transkribiert": True}
        for t in media.get("spoken_quotes") or []
    ]
    if q:
        out["zitate"] = q
    if media.get("visible_text"):
        out["text_im_bild"] = media["visible_text"]
    if item.media_pending:
        out["medien_analyse"] = "Bild/Video wird noch analysiert; Vorsicht-Einstufung kann sich noch ändern."
    elif media.get("error"):
        out["medien_analyse"] = f"Bild/Video nicht analysiert ({media['error']})."
    elif any("nicht analysiert" in str(a) for a in media.get("analyzed") or []):
        out["medien_analyse"] = "Teilweise analysiert: " + ", ".join(map(str, media["analyzed"]))
    if item.platform == "instagram":
        out["account"] = item.source_name
        if item.raw and item.raw.get("paidPartnership"):
            out["bezahlte_partnerschaft"] = True
    if scores:
        out["_scores"] = scores
    return out


def sort_items(items: list[m.Item]) -> list[m.Item]:
    """Vorsicht first, then newsworthiness, then recency."""

    def key(i: m.Item):
        c = i.classification
        ts = i.published_at.timestamp() if i.published_at else 0
        return (not is_vorsicht(c), -(c.newsworthiness if c else 0), -ts)

    return sorted(items, key=key)


def handle_out(h: m.Handle) -> dict[str, Any]:
    out = {
        "plattform": h.platform,
        "handle": f"@{h.handle}",
        "url": f"https://www.instagram.com/{h.handle}/"
        if h.platform == "instagram"
        else f"https://x.com/{h.handle}",
        "status": "verifiziert" if h.status == "verified" else "wahrscheinlich",
        "begruendung": reason_de(h),
        "verified": h.status == "verified",
    }
    if h.status != "verified":
        out["hinweis"] = "Nicht abschliessend bestätigt – im Gespräch nur mit Hinweis verwenden."
    return out


def age(born: date | None) -> int | None:
    if not born:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def current_season(today: date | None = None) -> str:
    d = today or date.today()
    start = d.year if d.month >= 8 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def affiliation_out(a: m.Affiliation) -> dict[str, Any]:
    out = {
        "team": a.team.name,
        "liga": a.team.league,
        "saison": a.season,
        "seit": a.valid_from.isoformat() if a.valid_from else None,
        "quelle": "sihf.ch" if a.source == "SIHF" else a.source,
        "url": a.source_url,
    }
    if a.season and a.season != current_season():
        out["hinweis"] = f"Stand Saison {a.season} – möglicherweise veraltet; Instagram-Bio/News prüfen."
    elif not a.season and a.source == "Wikidata":
        out["hinweis"] = "Laut Wikidata, ohne Saisonangabe – möglicherweise veraltet."
    return out


def athlete_head(session: Session, a: m.Athlete) -> dict[str, Any]:
    sport = current_spec()  # 2026-11-24: the project's sport (Torhüterin / Torhüter)
    affs = sorted(a.affiliations, key=lambda x: (x.season or "", x.valid_from or date.min), reverse=True)
    affs = [x for x in affs if x.team.kind == "club" and (x.valid_to is None or x.valid_to >= date.today())]
    out: dict[str, Any] = {
        "id": a.id,
        "name": a.full_name,
        "position": sport.position_labels_de.get(a.position or "", None),
        "nation": a.nationality,
        "geburtsdatum": a.birth_date.isoformat() if a.birth_date else None,
        "alter": age(a.birth_date),
        "vereine_laut_quellen": [affiliation_out(x) for x in affs[:3]],
        "instagram": [handle_out(h) for h in usable_handles(a)],
        # v8: how deep we look for this person (1 Swiss pool, 2 other WM teams, 3 rest) and how much we hold
        "tier": tiers.effective(a),
        "tier_manuell": a.tier_manual is not None,
        "quellen": tiers.sources_count(session, a.id),
    }
    ig = next((h for h in usable_handles(a) if h.profile and h.profile.get("biography")), None)
    if ig:
        fetched = ig.profile.get("fetched_at")
        when = datetime.fromisoformat(fetched) if fetched else None
        out["instagram_bio"] = {
            "text": ig.profile["biography"],
            "quelle": f"[Instagram-Bio @{ig.handle}, Stand {date_de(when)}]",
            "quelle_link": f"[Instagram-Bio @{ig.handle}, Stand {date_de(when)}](https://www.instagram.com/{ig.handle}/)",
            "url": f"https://www.instagram.com/{ig.handle}/",
            "hinweis": "Bio ist oft aktueller als Kaderlisten (z. B. neuer Verein).",
        }
    if a.wikidata_qid:
        out["wikidata"] = f"https://www.wikidata.org/wiki/{a.wikidata_qid}"
    if a.eliteprospects_id:
        out["eliteprospects"] = f"https://www.eliteprospects.com/player/{a.eliteprospects_id}"
    return out


# --- the tool's text (the only representation the client gets: compact Markdown, every fact with its link) ------

# (result key, section title, detail): detail = the list is what the tool is about (search hits, what's new,
# posts) and gets a short excerpt per entry; otherwise one line per entry (overviews like get_athlete)
SECTION_TITLES = [
    ("vorsicht", "⚠️ Vorsicht", True),
    ("zu_beachten", "⭐ Zu beachten", False),
    ("neueste_instagram_posts", "📸 Instagram", False),
    ("posts", "📸 Instagram", True),
    ("neue_eintraege", "🆕 Neu seit der Aktualisierung", True),
    ("neu_seit_aufruf", "🆕 Neu seit der Aktualisierung", True),
    ("eintraege", "🆕 Neu", True),
    ("treffer", "🔎 Treffer", True),
    ("news", "📰 News", False),
    ("resultate", "🏒 Resultate", False),
]
FACT_MAX = 220
EXCERPT_MAX = 320
SECTION_MAX = 12  # entries per section in the text; the rest is counted with a hint how to get them
# one short line per call; the full rules (Datenstand first, links unchanged, ⚠️ first, quotes verbatim, handles
# only from these tools) are in the server instructions once (mcp_server/server.py INSTRUCTIONS)
RULES = "_Jede Aussage mit ihrem Link übernehmen._"
MORE_HINT = {
    "eintraege": "whats_new mit kürzerem since oder search(query, …) für ein Thema",
    "treffer": "search mit genauerem query oder Filtern (athlete, team, since, source_type)",
    "posts": "get_athlete_posts mit since/limit",
    "vorsicht": "get_athlete_posts / whats_new für die Person",
}


# a bullet line ending in one Markdown link: "- … [label](https://…)"
_TRAILING_LINK = re.compile(r" (\[[^\[\]\n]+\]\(https?://\S+\))$")
SHARED_SOURCE_MIN = 3  # lines in a block with the same link before it is named once for the block


def one_source_per_block(text: str) -> str:
    """When every linked line of a block (heading to next heading) ends in the same link, name it once.

    get_athlete / get_team_roster repeated one link on every line (9 identical Elite Prospects links in the
    profile, 13 identical DEB links for a staff list): 8-12k characters of mostly links (gap loop round 2).
    The block then gets one line "_Quelle für alle Zeilen: [..](..)_" under its heading and the lines lose
    their copy. Blocks whose lines have different links keep them per line; a block with a bullet that has no
    link (other than a "… und N weitere" hint) is left alone, so no line gets a source it did not have."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("**")]
    bounds = list(zip([0, *starts], [*starts, len(lines)], strict=True))
    out: list[str] = []
    for a, b in bounds:
        block = lines[a:b]
        bullets = [i for i, ln in enumerate(block) if ln.startswith("- ")]
        links = [_TRAILING_LINK.search(block[i]) for i in bullets]
        linked = [mt.group(1) for mt in links if mt]
        bare = [block[i] for i, mt in zip(bullets, links, strict=True) if not mt]
        if (
            len(linked) >= SHARED_SOURCE_MIN
            and len(set(linked)) == 1
            and all(ln.startswith("- …") for ln in bare)
        ):
            link = linked[0]
            for i, mt in zip(bullets, links, strict=True):
                if mt:
                    block[i] = block[i][: mt.start()]
            at = 1 if block and block[0].startswith("**") else bullets[0]
            block.insert(at, f"_Quelle für alle Zeilen: {link}_")
        out += block
    return "\n".join(out)


def _flat(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " […]"


def excerpt(item: dict[str, Any], fact: str, limit: int = EXCERPT_MAX) -> str:
    """The item's text after what the line already says (title / first sentence), cut at `limit`."""
    text = _flat(item.get("text"))
    if not text:
        return ""
    head = _flat(fact).removesuffix(" […]")
    if head and text.startswith(head):
        text = text[len(head) :].strip(" .:–-")
    elif head and head in text[: len(head) + 40]:
        text = text[text.index(head) + len(head) :].strip(" .:–-")
    return _cut(text, limit) if len(text) >= 20 else ""


def line_for(item: dict[str, Any], detail: bool = False) -> str:
    base = _flat(item.get("titel") or item.get("zusammenfassung") or "")
    fact = base
    if len(fact) < 25 and item.get("bildbeschreibung"):  # emoji-only captions: say what the picture shows
        fact = f"{fact} – Bild: {_flat(item['bildbeschreibung'])}".strip(" –")
    fact = _cut(fact, FACT_MAX)
    if de := (item.get("titel_de") or item.get("zusammenfassung_de")):
        fact += f" (übersetzt: {_cut(_flat(de), 160)})"
    line = f"- {item.get('zeile_prefix', '')} {fact} {item.get('quelle_link', '')}".replace("  ", " ")
    if item.get("hinweis_alt"):  # older injury report shown for lack of news (mcp_server/caution.py)
        line += f" _({item['hinweis_alt']})_"
    if detail:
        about = item.get("athletinnen") or []
        extra = []
        if item.get("kategorie"):
            extra.append(item["kategorie"])
        if about:
            extra.append("über " + ", ".join(about[:3]) + (f" +{len(about) - 3}" if len(about) > 3 else ""))
        if extra:
            line += f" ({' · '.join(extra)})"
        if ex := excerpt(item, _cut(base, FACT_MAX)):
            line += f"\n  {ex}"
    seen: set[str] = set()
    for q in item.get("zitate") or []:
        if len(seen) == 2 or q["text"] in seen or q["text"] in fact:
            continue
        seen.add(q["text"])
        tag = " (automatisch transkribiert)" if q.get("automatisch_transkribiert") else ""
        line += f"\n  > «{q['text']}»{tag}"
    if item.get("medien_analyse") and detail:
        line += f"\n  _{item['medien_analyse']}_"
    return line


def _link(label: str | None, url: str | None) -> str:
    if not url:
        return f"({label})" if label else ""
    return f"[{(label or _site(url)).strip('[]')}]({url})"


def _site(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.") or "Quelle"


def _handle_md(h: dict[str, Any]) -> str:
    note = "" if h.get("verified") or h.get("status") == "verifiziert" else f" ({h.get('status')})"
    return f"[{h['handle']}]({h['url']}){note}"


STAT_PREFIX = "📊 (Statistik)"
ROSTER_STATUS_DE = {"confirmed": "bestätigt", "provisional": "provisorisch"}


def render_stats(result: dict[str, Any]) -> list[str]:
    """v11: stats rows (tool `stats`) and the Elite Prospects part of get_athlete, one sourced line each."""
    out: list[str] = []
    if result.get("zeilen") is not None:
        f = result.get("filter") or {}
        out.append("_Filter: " + ", ".join(f"{k}: {v}" for k, v in f.items()) + "_")
        for h in result.get("hinweise") or []:
            out.append(f"_{h}_")
        out.append("\n**📊 Statistik**")
        for r in result["zeilen"]:
            rank = f"{r['rang']}. " if r.get("rang") else ""
            note = f" ({r['hinweis']})" if r.get("hinweis") else ""
            out.append(f"- {STAT_PREFIX} {rank}{r['satz']}{note} {r['quelle_link']}")
        if not result["zeilen"]:
            out.append("- Keine Statistik in unseren Daten für diese Filter.")
    if prof := result.get("profil"):
        out.append("\n**🪪 Profil**")
        for p in prof.values():
            bits = [f"#{p['nummer']}" if p.get("nummer") else None, p.get("status")]
            extra = " · ".join(str(b) for b in bits if b and str(b) not in p["wert"])
            diff = f" (abweichend: {p['abweichend']})" if p.get("abweichend") else ""
            out.append(
                f"- 🪪 (Profil) {p['feld']}: {p['wert']}{' · ' + extra if extra else ''}{diff} {p['quelle_link']}"
            )
    if st := result.get("statistik"):
        out.append("\n**📊 Statistik**")
        for key, rows in st.items():
            if key.startswith("saison_") or key == "karriere_pro_liga":
                title = (
                    "Karriere pro Liga"
                    if key == "karriere_pro_liga"
                    else "Saison " + key[7:].replace("_", "/")
                )
                out.append(f"_{title}_")
                for r in rows:
                    out.append(f"- {STAT_PREFIX} {r['satz']} {r['quelle_link']}")
        if form := st.get("form"):
            for k in ("letzte_5", "letzte_10"):
                if form.get(k):
                    out.append(f"- {STAT_PREFIX} {form[k]['satz']} {form['quelle_link']}")
        if st.get("weitere_ligen"):
            out.append(f"- … und {st['weitere_ligen']} weitere Ligen")
        out += [f"_{h}_" for h in st.get("hinweise") or []]
        if st.get("details"):
            out.append(f"_{st['details']}_")
    if aw := result.get("auszeichnungen"):
        out.append("\n**🏆 Erfolge**")
        for x in aw["liste"]:
            bits = [x["text"], x.get("saison"), f"({x['nummer']})" if x.get("nummer") else None]
            out.append(f"- 🏆 ({x['art']}) {' '.join(b for b in bits if b)} {x['quelle_link']}")
        if aw.get("weitere"):
            out.append(f"- … und {aw['weitere']} weitere")
    return out


def _items_section(
    out: list[str], result: dict[str, Any], key: str, title: str, detail: bool, seen: set
) -> None:
    items = [i for i in result.get(key) or [] if isinstance(i, dict) and i.get("url") not in seen]
    if not items:
        return
    out.append(f"\n**{title}**")
    shown = items[:SECTION_MAX]
    for item in shown:
        seen.add(item.get("url"))
        out.append(line_for(item, detail))
    if rest := len(items) - len(shown):
        how = MORE_HINT.get(key)
        out.append(f"- … und {rest} weitere" + (f" (mehr: {how})" if how else ""))


def _render_events(result: dict[str, Any]) -> list[str]:
    """list_events: tournaments as containers, single games only in the time window, club games per league."""
    out: list[str] = []
    if evs := result.get("anlaesse"):
        out.append("\n**🏆 Turniere und Anlässe**")
        for e in evs:
            span = e["von"] if e.get("bis") in (None, "ohne Datum", e["von"]) else f"{e['von']}–{e['bis']}"
            bits = [span, e.get("ort"), e.get("stand")]
            line = f"- {e['name']} (`{e['key']}`): " + ", ".join(b for b in bits if b)
            if sp := e.get("spiele"):
                line += f"; {sp}"
            if e.get("url"):
                line += f" {_link(e.get('quelle'), e['url'])}"
            out.append(line)
    for key, title in (("spiele_letzte", "🏒 Letzte Spiele"), ("spiele_naechste", "📅 Nächste Spiele")):
        if games := result.get(key):
            out.append(f"\n**{title}**")
            for g in games:
                res = f" {g['resultat']}" if g.get("resultat") else ""
                res += f" ({g['ausgang']})" if g.get("ausgang") else ""
                where = f" ({g['ort']})" if g.get("ort") else ""
                comp = f" – {g['wettbewerb']}" if g.get("wettbewerb") else ""
                link = f" {_link(g.get('quelle'), g.get('url'))}" if g.get("url") else ""
                out.append(f"- {g['datum']}: {g['name']}{res}{where}{comp} (`{g['key']}`){link}")
            if key == "spiele_letzte" and result.get("bilanz"):
                out.append(f"- Bilanz in diesen Spielen: {result['bilanz']}")
            if key == "spiele_letzte" and result.get("gespeichert"):
                out.append(f"_{result['gespeichert']}_")
        elif key == "spiele_letzte" and result.get("gespeichert"):
            out.append(f"\n_{result['gespeichert']}_")
    if leagues := result.get("klubspiele"):
        out.append("\n**Klubspiele im Zeitraum (nicht einzeln aufgeführt)**")
        out += [f"- {x['liga']}: {x['anzahl']} Spiele, {x['von']}–{x['bis']}" for x in leagues]
    return out


def _roster_flag(p: dict[str, Any]) -> str:
    """' ⚠️ Verletzung 19.09.' (Vorsicht with its reason) and/or ' · Verletzung 22.09., seither: …' (resolved)."""
    flag = ""
    if p.get("vorsicht"):
        flag = f" ⚠️ {p['vorsicht_grund']}" if p.get("vorsicht_grund") else " ⚠️"
    if p.get("status_hinweis"):
        flag += f" · {p['status_hinweis']}"
    return flag


def _render_status(st: dict[str, Any]) -> list[str]:
    """get_athlete: newest injury report with what happened since, and her newest nomination / game."""
    out = ["\n**🩺 Aktueller Stand**"]
    if inj := st.get("verletzung"):
        out.append(f"- {inj['text']} {_link(inj.get('quelle'), inj.get('url'))} – {inj['seither']}")
    if last := st.get("zuletzt"):
        out.append(f"- {last['text']} {_link(last.get('quelle'), last.get('url'))}")
    return out


def _render_roster(result: dict[str, Any]) -> list[str]:
    """get_team_roster: one line per player."""
    out = []
    src = result.get("kader_quelle") or {}
    head = f"**{result['team']}**" + (f" ({result['liga']})" if result.get("liga") else "")
    status = {"provisional": "provisorisch", "confirmed": "bestätigt"}.get(
        src.get("status"), src.get("status")
    )
    bits = [src.get("anlass"), status]
    head += " – Kader " + ", ".join(b for b in bits if b) if any(bits) else ""
    out.append(head + (f" {_link(src.get('quelle'), src.get('url'))}" if src.get("url") else ""))
    if src.get("quelle") and not src.get("url"):
        out.append(f"_Quelle: {src['quelle']}_")
    if block := result.get("schluesselspielerinnen"):  # TEAM_KEY_PLAYERS (mcp_server/key_players.py)
        out += ["", *block]
    players = result.get("spielerinnen") or []
    out.append(f"\n**{wording().players} ({len(players)})**")
    for p in players:
        ig = p.get("instagram") or []
        handle = f" {_handle_md(ig[0])}" if ig else ""
        bits = [p.get("position"), p.get("nation")]
        de = f" · Deutsch: {p['deutsch']}" if p.get("deutsch") else ""
        out.append(f"- {p['name']} ({', '.join(b for b in bits if b) or '–'}){handle}{_roster_flag(p)}{de}")
    return out


def _render_handles(result: dict[str, Any]) -> list[str]:
    out = ["\n**Instagram-Handles (nur diese verwenden)**"]
    for a in result.get("athletinnen") or []:
        hs = a.get("handles")
        if isinstance(hs, list):
            out.append(f"- {a['name']}: " + ", ".join(_handle_md(h) for h in hs))
        else:
            out.append(f"- {a['name']}: {hs}")
    if result.get("nicht_gefunden"):
        out.append("_Nicht gefunden: " + ", ".join(result["nicht_gefunden"]) + "_")
    return out


def _render_teams(result: dict[str, Any]) -> list[str]:
    """get_event: roster status per team, then the players of the teams we show in full (Switzerland)."""
    out: list[str] = []
    teams = result.get("teams") or []
    status_lines = [t for t in teams if t.get("anzahl") is not None]
    if status_lines:  # v18: tournament sheet, roster status of the teams
        out.append("\n**Kader der Teams**")
        for t in status_lines:
            when = f", Stand {t['datum']}" if t.get("datum") else ""
            extra = f" – {t['offizieller_kader']}" if t.get("offizieller_kader") else ""
            gone = "; fehlen: " + ", ".join(a["name"] for a in t["fehlen"]) if t.get("fehlen") else ""
            if t.get("nicht_im_offiziellen_kader"):  # 2026-10-28: dropped from the provisional / camp list
                gone += f"; {t['nicht_im_offiziellen_kader']}"
            for g in t.get("aus_offiziellem_kader_gestrichen") or []:
                gone += f"; gestrichen: {g}"
            for g in t.get("laut_spielbericht_nachgemeldet") or []:  # 2026-11-04: late additions (game sheet)
                gone += f"; {g}"
            out.append(
                f"- {t['team']} (Gruppe {t.get('gruppe')}): {t['status']}, {t['anzahl']}{when}{gone}"
                f" {_link(t.get('quelle'), t.get('url'))}{extra}".rstrip()
            )
    media = [t for t in teams if t.get("medien")]
    if media:  # 2026-11-01: who Fabi contacts per team (work contact as the federation publishes it)
        out.append("\n**Medienkontakte der Teams**")
        out += [f"- {t['team']}: {t['medien']}" for t in media]
    missing = [t for t in teams if t.get("anzahl") is None and not t.get("spielerinnen")]
    if missing:
        out.append("\n_Kader noch nicht erfasst: " + ", ".join(t["team"] for t in missing) + "._")
    for team in teams:
        players = team.get("spielerinnen") or []
        if not players:
            continue
        src = team.get("kader_quelle") or {}
        out.append(
            f"\n**{team['team']}** ({team.get('kader_status', '')}, {len(players)})"
            + (f" {_link(src.get('quelle'), src.get('url'))}" if src.get("url") else "")
        )
        if team.get("staff"):
            out.append(f"- Staff: {team['staff']}")
        for p in players:
            ig = p.get("instagram") or {}
            handle = f" {_handle_md(ig)}" if ig else ""
            bits = [p.get("position") or "–", f"#{p['nummer']}" if p.get("nummer") else None]
            if club := (p.get("verein") or {}).get("team"):
                bits.append(club)
            flag = _roster_flag(p)
            post = p.get("neueste_posts") or []
            last = (
                f" · {post[0]['zeile_prefix']} {_cut(_flat(post[0].get('zusammenfassung')), 80)} "
                f"{post[0]['quelle_link']}"
                if post
                else ""
            )
            out.append(f"- {p['name']} ({', '.join(b for b in bits if b)}){handle}{flag}{last}")
    return out


# keys the renderer shows (or deliberately leaves out: the rules are in RULES and the server instructions)
_SHOWN = {
    "datenstand",
    "quelle",
    "hinweis",
    "fehler",
    "aktualisierung",
    "erweitert",
    "athletin",
    "aktueller_stand",
    "anlass",
    "lineup_status",
    "details",
    "turnier",
    "spiele",
    "bilanz",
    "gespeichert",
    "teams",
    "zeilen",
    "filter",
    "hinweise",
    "anzahl",
    "anzahl_gesamt",
    "profil",
    "statistik",
    "auszeichnungen",
    "anlaesse",
    "spiele_letzte",
    "spiele_naechste",
    "klubspiele",
    "mehr",
    "team",
    "liga",
    "kader_quelle",
    "spielerinnen",
    "athletinnen",
    "nicht_gefunden",
    "mehrdeutig",
    "frage",
    "kandidatinnen",
    "ziel",
    "seit",
    "erkannt",
    "instagram",
    "zuletzt_abgerufen",
    "hinweis_posts",
    "job_id",
    "status",
    "ergebnis",
    "fertig_um",
    "staff",
    "staff_im_aufgebot",
    "deutsch",
    "deutsch_staff",
    "deutsch_regel",
    "schluesselspielerinnen",  # inside the roster (_render_roster)
    "elite_prospects",  # get_athlete: its link is on the profile/statistics lines already
    *(k for k, _t, _d in SECTION_TITLES),
}


def _generic(key: str, value: Any) -> str | None:
    """Fallback for a key without its own rendering: nothing the tool returns gets lost."""
    if value in (None, "", [], {}, False):
        return None
    if isinstance(value, dict):
        return f"- {key}: " + "; ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", []))
    if isinstance(value, list):
        parts = [
            ", ".join(f"{v}" for v in x.values() if v not in (None, "", []))
            if isinstance(x, dict)
            else str(x)
            for x in value[:SECTION_MAX]
        ]
        more = f" … und {len(value) - SECTION_MAX} weitere" if len(value) > SECTION_MAX else ""
        return f"- {key}: " + " | ".join(parts) + more
    return f"- {key}: {value}"


def render_markdown(result: dict[str, Any]) -> str:
    """The tool's text: compact German Markdown, one fact per line with its link (no second copy as JSON)."""
    out = [f"**Datenstand:** {result['datenstand']}"] if result.get("datenstand") else []
    if result.get("fehler") and not result.get("job_id"):
        return "\n".join([*out, f"Fehler: {result['fehler']}"])
    if isinstance(akt := result.get("aktualisierung"), dict) and (akt.get("hinweis") or akt.get("job_id")):
        job = f" (refresh_status({akt['job_id']}) für die neuen Einträge)" if akt.get("job_id") else ""
        out.append(f"_{akt.get('hinweis') or 'Aktualisierung läuft im Hintergrund.'}{job}_")
    if result.get("erweitert"):
        out.append(f"_{result['erweitert']}_")
    if "karten" in result or "dokument_text" in result:  # search cards / dokument(id) (MCP_SEARCH_CARDS)
        from klugscheisser.mcp_server import cards

        body = cards.render(result) if "karten" in result else [result["dokument_text"]]
        return "\n".join([*out, *body, "", RULES])
    if result.get("mehrdeutig"):
        out.append(result.get("frage") or "Mehrere Treffer.")
        out += [
            f"- {k['name']}" + (f" (geb. {k['geburtsdatum']})" if k.get("geburtsdatum") else "")
            for k in result.get("kandidatinnen") or []
        ]
        out.append("_Mit dem vollen Namen erneut fragen._")
        return "\n".join(out)
    if isinstance(a := result.get("athletin"), dict):
        details = [a.get("position"), a.get("nation"), f"{a['alter']} J." if a.get("alter") else None]
        out.append(f"**{a['name']}** – " + ", ".join(x for x in details if x))
        for v in a.get("vereine_laut_quellen", [])[:2]:
            note = f" ({v['hinweis']})" if v.get("hinweis") else ""
            out.append(f"- Verein laut Quellen: {v['team']} {v.get('saison') or ''}{note}".rstrip())
        for h in a.get("instagram", []):
            out.append(f"- Instagram: [{h['handle']}]({h['url']}) – {h['status']}")
        if bio := a.get("instagram_bio"):
            bio_text = " · ".join(x.strip() for x in bio["text"].splitlines() if x.strip())
            out.append(f"- Bio: «{bio_text}» {bio.get('quelle_link', '')}")
        links = [
            f"[{n}]({a[k]})"
            for k, n in (("eliteprospects", "Elite Prospects"), ("wikidata", "Wikidata"))
            if a.get(k)
        ]
        if links:
            out.append("- Profile: " + " · ".join(links))
        if isinstance(de := result.get("deutsch"), dict) and de.get("text"):
            src = (de.get("quellen") or [{}])[0]
            out.append(f"- {de['text']} {_link(src.get('quelle'), src.get('url'))}".rstrip())
        if st := result.get("aktueller_stand"):
            out += _render_status(st)
    elif isinstance(a, str):  # get_athlete_posts
        hs = result.get("instagram") or []
        out.append(
            f"**{a}** – Instagram: "
            + (", ".join(_handle_md(h) for h in hs) or "kein Account bekannt")
            + f" (zuletzt abgerufen {result.get('zuletzt_abgerufen', '?')})"
        )
        if result.get("hinweis_posts"):
            out.append(f"_{result['hinweis_posts']}_")
    if a := result.get("anlass"):
        out.append(f"**{a['name']}** ({a['von']}–{a['bis']}, {a.get('ort') or ''})")
        if result.get("lineup_status"):
            out.append(f"_{result['lineup_status']}_")
    if result.get("ziel"):  # whats_new / refresh
        head = f"**{result['ziel']}**"
        if result.get("seit"):
            head += f" – seit {result['seit']}, {result.get('anzahl', 0)} Einträge"
        out.append(head)
    if result.get("frage") and "treffer" in result:  # search
        rec = result.get("erkannt")
        out.append(
            f"_Suche: «{result['frage']}», {result.get('anzahl', 0)} Treffer"
            + (f"; erkannt: {', '.join(f'{k}={v}' for k, v in rec.items())}" if rec else "")
            + "_"
        )
    out += render_stats(result)
    if result.get("zeilen") is not None and result.get("anzahl_gesamt", 0) > result.get("anzahl", 0):
        out.append(
            f"- … {result['anzahl']} von {result['anzahl_gesamt']} Zeilen gezeigt (mehr: limit erhöhen)."
        )
    for sec in result.get("turnier") or []:  # tournament fact sheet (klugscheisser/tournament.py)
        out.append(f"\n**{sec['titel']}**")
        for z in sec["zeilen"]:
            link = f" [{z['quelle']}]({z['url']})" if z.get("url") else ""
            out.append(f"- {z['text']}{link}")
    seen: set[str] = set()
    for key, title, detail in SECTION_TITLES:
        _items_section(out, result, key, title, detail, seen)
    if result.get("staff"):  # v19: team staff (get_team_roster)
        out.append("\n**Trainer und Staff**")
        out += [
            (f"- {z['text']} [{z['quelle']}]({z['url']})" if z.get("url") else f"- {z['text']}")
            + (f" · Deutsch: {z['deutsch']}" if z.get("deutsch") else "")
            for z in result["staff"]
        ]
    if result.get("staff_im_aufgebot"):
        out.append(f"_Staff im Aufgebot: {'; '.join(result['staff_im_aufgebot'])}_")
    for z in result.get("deutsch_staff") or []:
        out.append(f"- {z}")
    if result.get("deutsch_regel"):
        out.append(f"_{result['deutsch_regel']}_")
    for key, label in (("news", "News"), ("instagram", "Instagram")):  # refresh: status strings
        if isinstance(result.get(key), str):
            out.append(f"- {label}: {result[key]}")
    if "job_id" in result:  # refresh / refresh_status
        bits = [f"Auftrag {result['job_id']}"]
        if isinstance(result.get("status"), str):
            bits.append(result["status"])
        if result.get("fertig_um"):
            bits.append(f"fertig {result['fertig_um']}")
        if erg := result.get("ergebnis"):
            bits.append(f"{erg.get('neue_posts', 0)} neue Posts aus {erg.get('accounts', 0)} Accounts")
        out.append("- " + ", ".join(bits))
        if result.get("hinweis") and result["hinweis"] != HINWEIS:
            out.append(f"_{result['hinweis']}_")
        if result.get("fehler"):
            out.append(f"- Fehler: {result['fehler']}")
    if games := result.get("spiele"):
        out.append("\n**Spiele** (Schweizer Zeit)")
        for g in games:
            score = g.get("resultat") or ("live" if g.get("status") == "live" else "geplant")
            link = f" [{g['quelle']}]({g['url']})" if g.get("url") else ""
            where = f", {g['ort']}" if g.get("ort") else ""
            out.append(f"- {g['datum']}: {g['spiel']} {score}{where}{link}")
    out += _render_teams(result)
    if result.get("kader_quelle") is not None and "spielerinnen" in result:
        out += _render_roster(result)
    if "athletinnen" in result and isinstance(result.get("athletinnen"), list) and "treffer" not in result:
        out += _render_handles(result)
    if isinstance(result.get("anlaesse"), list) and result.get(
        "athletin"
    ):  # get_athlete: events of the person
        out.append("\n**🏆 Anlässe**")
        for e in result["anlaesse"][:8]:
            status = ROSTER_STATUS_DE.get(e["kader_status"], e["kader_status"])
            when = "" if re.search(r"\([^)]*\d{4}\)", e["anlass"]) else f" ({e['datum']})"  # date in name
            out.append(
                f"- {e['anlass']}{when}: im Aufgebot ({status}) {_link(e.get('quelle'), e.get('url'))}".rstrip()
            )
        if len(result["anlaesse"]) > 8:
            out.append(f"- … und {len(result['anlaesse']) - 8} frühere")
    elif "anlaesse" in result or "spiele_naechste" in result:
        out += _render_events(result)
    if result.get("details"):
        out.append(f"\n_{result['details']}_")
    if result.get("mehr"):
        out.append(f"\n_{result['mehr']}_")
    rest = [
        g for k, v in result.items() if k not in _SHOWN and not k.startswith("_") and (g := _generic(k, v))
    ]
    if rest:
        out += ["\n**Weitere Angaben**", *rest]
    out.append("\n" + RULES)
    return one_source_per_block("\n".join(out))


def render_answer(result: dict[str, Any]) -> str:
    """The `frage` tool's text (its only representation): every statement with its verbatim quote and link."""
    out = [f"**Datenstand:** {result['datenstand']}"]
    if result.get("aktualisierung"):
        out.append(f"_{result['aktualisierung']}_")
    if result.get("scope_hinweis"):
        out.append(f"_Verstanden als: {result['scope_hinweis']}_")
    if result.get("personen") and len(result["personen"]) <= 8:
        out.append(f"_Personen: {', '.join(result['personen'])}_")
    if result.get("durchsucht"):
        out.append(f"_{result['durchsucht']}_")
    if result.get("rueckfrage"):
        out.append(f"\n**Rückfrage:** {result['rueckfrage']}")
        out += [f"- {x}" for x in result.get("rueckfrage_vorschlaege") or []]

    def line(st: dict[str, Any]) -> str:
        de = f" (automatisch übersetzt: «{st['zitat_de']}»)" if st.get("zitat_de") else ""
        text = f"- {st['zeile_prefix']} {st['aussage']} «{st['zitat']}»{de} {st['quelle_link']}"
        basis = [b for b in st.get("basis") or [] if b.get("url") and b["url"] != st.get("url")]
        if st.get("berechnet") and basis:  # computed from stored data: the underlying sources
            links = ", ".join(_link(b.get("quelle"), b["url"]) for b in basis[:4])
            text += f"\n  Berechnet aus: {links}" + (f" +{len(basis) - 4}" if len(basis) > 4 else "")
        return text

    statements = result.get("statements") or []
    vorsicht = [st for st in statements if st.get("vorsicht")]
    rest = [st for st in statements if not st.get("vorsicht")]
    if vorsicht:
        out += ["\n**⚠️ Vorsicht**", *map(line, vorsicht)]
    if rest:
        out += ["\n**✔️ Belegt**", *map(line, rest)]
    if not statements and not result.get("rueckfrage"):
        out.append("\nKeine belegten Angaben in unseren Quellen zu genau dieser Frage.")
        if result.get("naechste_quellen"):
            out.append("**Am nächsten dran (beantwortet die Frage nicht direkt):**")
            out += [
                f"- {x['zeile_prefix']} {_cut(_flat(x['titel']), 160)} [{x['quelle'].strip('[]')}]({x['url']})"
                for x in result["naechste_quellen"]
            ]
    if result.get("unbestaetigt"):
        out += [
            "\n**❔ Unbestätigt (Zitat stützt die Aussage nicht sicher)**",
            *map(line, result["unbestaetigt"]),
        ]
    if result.get("nicht_geprueft"):
        out.append("\n**🌐 Weitere Web-Treffer (nicht geprüft, nicht gelesen)**")
        out += [
            f"- [{_cut(_flat(x['titel']), 140)}]({x['url']}) – {x['hinweis']}"
            for x in result["nicht_geprueft"][:6]
        ]
    if result.get("luecken"):
        out.append("\n**Nicht beantwortet:** " + "; ".join(result["luecken"]))
    if result.get("verworfen"):
        out.append(
            f"_{result['verworfen']} Aussage(n) verworfen, weil kein wörtlicher Beleg gefunden wurde._"
        )
    if result.get("naechste_fragen"):
        out.append("_Weitere mögliche Fragen: " + " · ".join(result["naechste_fragen"][:3]) + "_")
    out.append("\n_Aussage, «Zitat» und Link gehören zusammen._")
    return one_source_per_block("\n".join(out))
