"""Citable source names for facts and career stations from web articles (2026-11-01).

`source` "web" (also "news", "federation", "college" …) is only the internal source type. Fabi has to cite the
real site: the article's site name (items.source_name, else the domain of `source_url`) and its date
(items.published_at), e.g. "EV Zug, 16.12.2025" or "frauen-eishockey.com, 07.02.2024".
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de

# structured sources that already name themselves (label stays as before)
NAMED_SOURCES = {
    "wikidata",
    "wikipedia",
    "eliteprospects",
    "hockeycanada",
    "wikimedia",
    "roster",
    "sihf",
    "leijonat",
    "leijonat_u18",
    "leijonat_liiga",
    "swehockey",
    "iihf",
    "wiki",
    "jihf",
}
_GENERIC = {"", "web", "rss", "news", "google news", "website"}
_CUT = re.compile(r"\s+[-|–—:]\s+")
_DATE_TAIL = re.compile(r",\s*\d{2}\.\d{2}\.\d{4}$")
# one spelling of the named sources everywhere (web, MCP, chat); 'sihf' is cited as the site "sihf.ch"
NAMED_DE = {
    "eliteprospects": "Elite Prospects",
    "wikidata": "Wikidata",
    "wikipedia": "Wikipedia",
    "wikimedia": "Wikimedia Commons",
    "hockeycanada": "Hockey Canada",
    "sihf": "sihf.ch",
    "roster": "Kader",
    "iihf": "IIHF",
}


def domain(url: str | None) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_name(url: str | None, source_name: str | None = None, platform: str | None = None) -> str:
    """'EV Zug' (feed / page name), 'frauen-eishockey.com' (name cut at ' - '), else the domain."""
    name = _CUT.split((source_name or "").strip(), maxsplit=1)[0].strip()
    if platform in ("instagram", "x") and name:
        return f"{'Instagram' if platform == 'instagram' else 'X'} {name}"
    if name.lower() in _GENERIC or len(name) > 60:
        return domain(url) or name
    return name


def needs_site(source: str | None) -> bool:
    return (source or "") not in NAMED_SOURCES


def site_label(
    source: str | None,
    url: str | None,
    source_name: str | None = None,
    published_at: datetime | None = None,
    platform: str | None = None,
) -> str | None:
    """'EV Zug, 16.12.2025' for a web-type source; None for named sources (Wikidata, Elite Prospects …)."""
    if not needs_site(source):
        return None
    name = site_name(url, source_name, platform)
    if not name:
        return None
    return f"{name}, {date_de(published_at, True)}" if published_at else name


def cite_label(
    source: str | None, url: str | None, site: str | None = None, names: dict | None = None
) -> str:
    """The label to cite: 'EV Zug, 16.12.2025' (`site`, from `item_sites`) or at least the domain for a web-type
    source, never a bare "Web"/"News"; the named source's name otherwise ('Elite Prospects', 'sihf.ch')."""
    if needs_site(source):
        label = site or site_label(source, url)
        if label:
            return label
    return (names or NAMED_DE).get(source or "", NAMED_DE.get(source or "", source or "Quelle"))


def cite_name(source: str | None, url: str | None, site: str | None = None, names: dict | None = None) -> str:
    """Short name for a 'Quellen: …' line, without the date: 'EV Zug', 'srf.ch', 'Elite Prospects'."""
    if needs_site(source):
        name = _DATE_TAIL.sub("", (site or "").strip("[] ")) or site_name(url)
        if name:
            return name
    return cite_label(source, url, None, names)


def item_info(session: Session, ids) -> dict[int, tuple]:
    """items.id -> (source_name, published_at, platform), one query."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        i: (name, pub, plat)
        for i, name, pub, plat in session.execute(
            select(m.Item.id, m.Item.source_name, m.Item.published_at, m.Item.platform).where(
                m.Item.id.in_(ids)
            )
        )
    }


def row_site(row, info: dict[int, tuple]) -> str | None:
    """site_label of one fact/stint row (model or query row) with `info` from item_info."""
    if not needs_site(row.source) or not row.source_url:
        return None
    name, pub, plat = info.get(getattr(row, "source_item_id", None) or 0, (None, None, None))
    return site_label(row.source, row.source_url, name, pub, plat)


def item_sites(session: Session, rows: list) -> None:
    """Set `row.site` on fact/stint rows (FactRow, StintRow) of web-type sources, one query for all."""
    todo = [r for r in rows if needs_site(r.source) and r.source_url]
    info = item_info(session, {r.source_item_id for r in todo})
    for r in todo:
        r.site = row_site(r, info)
