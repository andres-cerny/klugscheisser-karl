"""Wikipedia infobox → person facts (free profile source, 2026-10-25).

Karl stores each person's Wikipedia articles as plain text (items, platform "wikipedia"); the infobox is not in that
text. This reads the wikitext of the stored en/de/fr articles through the MediaWiki API (50 titles per request,
identified User-Agent, 1 s apart) and takes height, weight, shoots/catches, position and birth place from the
infobox ("Infobox ice hockey biography/player", "Infobox Eishockeyspieler", "Infobox Personnalité du hockey sur
glace"). Stored with source "wikipedia", extractor "wikipedia-infobox" and the article as link; the facts the
model extracted from the article text (extractor = model name) are not touched. Elite Prospects, Hockey Canada,
and Wikidata rank above it (facts/source_facts.py): a differing value is stored as "conflict".

Not built on purpose: the "Career statistics" tables. Measured 2026-10-25 on the WCh field: 0 of the 36 focus
players would gain a season row (every focus player with such a table already has Elite Prospects seasons), 28
other players would; the tables are hand-made, often years out of date (Stalder: last row 2023-24), and season
rows of a second source would be counted next to Elite Prospects by the stats queries. See docs/sources.md.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.profiles.http import USER_AGENT
from klugscheisser.db import models as m
from klugscheisser.facts import source_facts as sf

log = logging.getLogger(__name__)

SOURCE = "wikipedia"
EXTRACTOR = "wikipedia-infobox"
LANGS = ("en", "de", "fr")
INFOBOX = re.compile(
    r"\{\{\s*(Infobox ice hockey (?:biography|player)|Infobox Eishockeyspieler|"
    r"Infobox Personnalit[ée] du hockey sur glace)\s*\|",
    re.I,
)


def _template_body(text: str, start: int) -> str:
    """The template starting at `start` ('{{…}}'), nested templates included."""
    depth, i = 0, start
    while i < len(text) - 1:
        two = text[i : i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return text[start:]


def infobox_fields(wikitext: str) -> dict[str, str]:
    """Top-level `| key = value` pairs of the first hockey infobox (keys lower case)."""
    hit = INFOBOX.search(wikitext or "")
    if not hit:
        return {}
    body = _template_body(wikitext, hit.start())[2:-2]
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(body):
        two = body[i : i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            cur.append(two)
            i += 2
            continue
        if body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(body[i])
        i += 1
    parts.append("".join(cur))
    out: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def plain(value: str | None) -> str:
    """'[[Lucerne]], [[Switzerland]]' → 'Lucerne, Switzerland'; refs, comments, templates and markup removed."""
    v = value or ""
    v = re.sub(r"<!--.*?-->", "", v, flags=re.S)
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    v = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"<[^>]+>", " ", v)
    v = v.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", v).strip(" ,;")


def _num(v: str | None) -> float | None:
    hit = re.search(r"\d+(?:[.,]\d+)?", plain(v))
    return float(hit.group(0).replace(",", ".")) if hit else None


def height_cm(f: dict[str, str]) -> int | None:
    cm = None
    if (x := _num(f.get("height_cm"))) is not None:
        cm = x
    elif (x := _num(f.get("height_m"))) is not None:
        cm = x * 100 if x < 3 else x
    elif (ft := _num(f.get("height_ft"))) is not None:
        cm = (ft * 12 + (_num(f.get("height_in")) or 0)) * 2.54
    else:
        for key in ("größe", "grösse", "taille"):
            raw = plain(f.get(key))
            if (x := _num(raw)) is not None:
                cm = x * 100 if x < 3 else x  # "1,67 m" or "167 cm"
                break
    if cm is None:
        return None
    cm = round(cm)
    return cm if 140 <= cm <= 205 else None


def weight_kg(f: dict[str, str]) -> int | None:
    kg = None
    if (x := _num(f.get("weight_kg"))) is not None:
        kg = x
    elif (x := _num(f.get("weight_lb"))) is not None:
        kg = x * 0.45359237
    else:
        for key in ("gewicht", "poids"):
            if (x := _num(f.get(key))) is not None:
                kg = x
                break
    if kg is None:
        return None
    kg = round(kg)
    return kg if 40 <= kg <= 120 else None


_LEFT = re.compile(r"^(left|links|gauche)", re.I)
_RIGHT = re.compile(r"^(right|rechts|droite)", re.I)


def hand(f: dict[str, str]) -> tuple[str | None, bool]:
    """('L'|'R'|None, goalie?) from shoots/catches (en), Schusshand/Fanghand (de), tire/attrape (fr)."""
    for key, goalie in (
        ("catches", True),
        ("fanghand", True),
        ("attrape", True),
        ("shoots", False),
        ("schusshand", False),
        ("tire", False),
    ):
        v = plain(f.get(key))
        if _LEFT.match(v):
            return "L", goalie
        if _RIGHT.match(v):
            return "R", goalie
    return None, False


def position_code(f: dict[str, str]) -> str | None:
    v = plain(f.get("position")).lower()
    if re.search(r"goal|torh|gardien", v):
        return "G"
    if re.search(r"defen|verteid|défense|defense", v):
        return "D"
    if re.search(r"forward|centre|center|wing|stürm|sturm|attaq|ailier", v):
        return "F"
    return None


def birth_place(f: dict[str, str]) -> str | None:
    for key in ("birth_place", "geburtsort", "lieu de naissance"):
        v = plain(f.get(key))
        if v:
            v = re.sub(r"\s*\(([^)]*)\)\s*$", r", \1", v)  # 'San Francisco (États-Unis)'
            return v[:200]
    return None


def specs_from(fields: dict[str, str], url: str) -> list[sf.Spec]:
    out: list[sf.Spec] = []
    kw = {"url": url}
    if cm := height_cm(fields):
        out.append(sf.height_spec(cm, **kw))
    if kg := weight_kg(fields):
        out.append(sf.weight_spec(kg, **kw))
    h, goalie = hand(fields)
    code = position_code(fields)
    if h:
        out.append(sf.shoots_spec(h, goalie=goalie or code == "G", **kw))
    if code:
        out.append(sf.position_spec(code, **kw))
    if place := birth_place(fields):
        out.append(sf.Spec("birth_place", {"text": place.split(",")[0].strip(), "raw": place}, place, **kw))
    return out


@dataclass
class InfoboxImport:
    articles: int = 0
    fetched: int = 0
    with_infobox: int = 0
    persons: int = 0
    requests: int = 0
    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


def stored_articles(s: Session, athlete_ids: list[int] | None) -> dict[int, list[tuple[str, str, str]]]:
    """Person → [(lang, title, url)] of her stored en/de/fr Wikipedia profiles, in LANGS order."""
    q = (
        select(m.ItemAthlete.athlete_id, m.Item.url)
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .where(m.Item.platform == "wikipedia", m.Item.source_type == "profile")
    )
    if athlete_ids is not None:
        q = q.where(m.ItemAthlete.athlete_id.in_(athlete_ids))
    out: dict[int, list[tuple[str, str, str]]] = {}
    for aid, url in s.execute(q):
        parts = urlsplit(url or "")
        lang = parts.netloc.split(".")[0]
        if lang not in LANGS or "/wiki/" not in parts.path:
            continue
        title = unquote(parts.path.split("/wiki/", 1)[1]).replace("_", " ")
        out.setdefault(aid, []).append((lang, title, url))
    for v in out.values():
        v.sort(key=lambda x: LANGS.index(x[0]))
    return out


def fetch_wikitexts(titles_by_lang: dict[str, set[str]], http=None, pause_s: float = 1.0, sleep=time.sleep):
    """{(lang, title): wikitext} via action=query&prop=revisions (redirects followed; keyed by the asked title)."""
    import httpx

    client = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
    out: dict[tuple[str, str], str] = {}
    requests = 0
    for lang, titles in titles_by_lang.items():
        titles_l = sorted(titles)
        for i in range(0, len(titles_l), 50):
            chunk = titles_l[i : i + 50]
            r = client.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "titles": "|".join(chunk),
                    "format": "json",
                    "formatversion": 2,
                    "redirects": 1,
                },
            )
            requests += 1
            r.raise_for_status()
            q = r.json().get("query") or {}
            back: dict[str, str] = {}
            for x in (q.get("normalized") or []) + (q.get("redirects") or []):
                back[x["to"]] = back.get(x["from"], x["from"])
            for pg in q.get("pages") or []:
                revs = pg.get("revisions") or []
                if revs:
                    content = revs[0]["slots"]["main"]["content"]
                    out[(lang, pg["title"])] = content
                    out[(lang, back.get(pg["title"], pg["title"]))] = content
            sleep(pause_s)
    return out, requests


def run(s: Session, athlete_ids: list[int] | None, http=None, sleep=time.sleep) -> InfoboxImport:
    r = InfoboxImport()
    arts = stored_articles(s, athlete_ids)
    titles: dict[str, set[str]] = {}
    for lst in arts.values():
        for lang, title, _ in lst:
            titles.setdefault(lang, set()).add(title)
            r.articles += 1
    texts, r.requests = fetch_wikitexts(titles, http=http, sleep=sleep)
    r.fetched = sum(1 for lst in arts.values() for lang, title, _ in lst if (lang, title) in texts)
    for aid, lst in arts.items():
        a = s.get(m.Athlete, aid)
        if a is None:
            continue
        specs: list[sf.Spec] = []
        seen: set[str] = set()
        for lang, title, url in lst:  # en first, then de, fr: one value per kind
            fields = infobox_fields(texts.get((lang, title)) or "")
            if not fields:
                continue
            r.with_infobox += 1
            for spec in specs_from(fields, url):
                if spec.kind not in seen:
                    seen.add(spec.kind)
                    specs.append(spec)
        url = lst[0][2]
        sf.sync_facts(s, a, SOURCE, EXTRACTOR, url, specs, r.counts)
        r.persons += bool(specs)
    s.flush()
    return r


def article_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
