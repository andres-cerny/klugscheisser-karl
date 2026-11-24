"""Profile photo from Wikimedia Commons (Wikidata P18), with author and licence. Never Instagram pictures.

Fetched lazily (the person page loads it via htmx, so the page itself never waits) with a hard 5 s budget.
The result is stored as a Fact of kind "photo" (source "wikimedia", link = Commons file page). A miss is
remembered in CacheEntry so a person without a photo is asked again only after RETRY_DAYS.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.wikidata import USER_AGENT
from klugscheisser.db import models as m

log = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PHOTO_KIND = "photo"
SOURCE = "wikimedia"
CACHE_SOURCE = "wikimedia-photo"
TIMEOUT_S = 5.0
THUMB_WIDTH = 320
RETRY_DAYS = 30


def _plain(value: Any) -> str:
    """extmetadata values are HTML snippets ('<a href=…>Jenn G</a>'): text only, whitespace collapsed."""
    text = unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _clean_url(url: str | None) -> str | None:
    """Drop the utm tracking query the API appends to upload/thumb URLs."""
    if not url:
        return None
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def parse_p18(payload: dict) -> str | None:
    """File name of the first non-deprecated P18 claim of a wbgetclaims response."""
    claims = (payload.get("claims") or {}).get("P18") or []
    for c in sorted(claims, key=lambda c: c.get("rank") != "preferred"):
        if c.get("rank") == "deprecated":
            continue
        value = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(value, str) and value:
            return value
    return None


def parse_imageinfo(payload: dict) -> dict[str, Any] | None:
    """Commons `prop=imageinfo&iiprop=url|extmetadata&iiurlwidth=…` → photo metadata, or None when the file
    is missing or author/licence/file page are unknown (no photo without attribution)."""
    pages = (payload.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):  # formatversion=1
        pages = list(pages.values())
    if not pages or pages[0].get("missing"):
        return None
    infos = pages[0].get("imageinfo") or []
    if not infos:
        return None
    ii = infos[0]
    meta = ii.get("extmetadata") or {}
    author = _plain((meta.get("Artist") or {}).get("value"))
    licence = _plain((meta.get("LicenseShortName") or {}).get("value"))
    file_page = ii.get("descriptionurl")
    thumb = _clean_url(ii.get("thumburl") or ii.get("url"))
    if not (author and licence and file_page and thumb):
        return None
    out = {
        "url": _clean_url(ii.get("url")),
        "thumb": thumb,
        "width": ii.get("thumbwidth"),
        "height": ii.get("thumbheight"),
        "author": author[:200],
        "licence": licence[:80],
        "file_page": file_page,
    }
    if lic_url := _plain((meta.get("LicenseUrl") or {}).get("value")):
        out["licence_url"] = lic_url
    return out


def fetch_photo(qid: str, client: httpx.Client | None = None, budget_s: float = TIMEOUT_S) -> dict | None:
    """Wikidata P18 → Commons image info, both calls together within `budget_s`."""
    deadline = time.monotonic() + budget_s
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT})

    def get(url: str, params: dict) -> dict:
        left = deadline - time.monotonic()
        if left <= 0.2:
            raise TimeoutError("photo budget used up")
        r = client.get(url, params=params, timeout=left)
        r.raise_for_status()
        return r.json()

    try:
        claims = get(
            WIKIDATA_API, {"action": "wbgetclaims", "entity": qid, "property": "P18", "format": "json"}
        )
        name = parse_p18(claims)
        if not name:
            return None
        info = get(
            COMMONS_API,
            {
                "action": "query",
                "titles": f"File:{name}",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": THUMB_WIDTH,
                "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl",
                "format": "json",
                "formatversion": 2,
            },
        )
        return parse_imageinfo(info)
    finally:
        if own:
            client.close()


def stored_photo(session: Session, athlete_id: int) -> dict | None:
    f = session.scalar(
        select(m.Fact)
        .where(m.Fact.athlete_id == athlete_id, m.Fact.kind == PHOTO_KIND, m.Fact.status != "unconfirmed")
        .order_by(m.Fact.id.desc())
        .limit(1)
    )
    return dict(f.value) if f is not None and f.value and f.source_url else None


def _target(a: m.Athlete) -> str:
    return f"athlete:{a.id}:{a.wikidata_qid}"


def photo_due(session: Session, a: m.Athlete) -> bool:
    """True when a lookup makes sense: QID known, nothing stored, no recent miss."""
    if not a.wikidata_qid or stored_photo(session, a.id) is not None:
        return False
    entry = session.get(m.CacheEntry, (CACHE_SOURCE, _target(a)))
    return entry is None or entry.fetched_at < datetime.now(UTC) - timedelta(days=RETRY_DAYS)


def store_photo(session: Session, a: m.Athlete, meta: dict) -> None:
    session.add(
        m.Fact(
            athlete_id=a.id,
            kind=PHOTO_KIND,
            value=meta,
            value_text=f"Foto: {meta['author']}, {meta['licence']}, Wikimedia Commons",
            source=SOURCE,
            source_url=meta["file_page"],
            quote=None,
            confidence=1.0,
            status="confirmed",
            extractor=SOURCE,
        )
    )
    session.flush()


def ensure_photo(session: Session, a: m.Athlete, fetch=None) -> dict | None:
    """Stored photo, or one lookup (≤ 5 s) when due. Failures mean "no photo", never an error page."""
    fetch = fetch or fetch_photo  # looked up per call (a default argument would be fixed at import time)
    if (meta := stored_photo(session, a.id)) is not None:
        return meta
    if not photo_due(session, a):
        return None
    now = datetime.now(UTC)
    try:
        meta = fetch(a.wikidata_qid)
    except Exception as exc:  # network, timeout, odd JSON: no photo this time, ask again in a day
        log.info("photo lookup for %s failed: %s", a.wikidata_qid, exc)
        meta = None
        now -= timedelta(days=RETRY_DAYS - 1)
    session.merge(m.CacheEntry(source=CACHE_SOURCE, target=_target(a), fetched_at=now))
    if meta:
        store_photo(session, a, meta)
    session.flush()
    return meta
