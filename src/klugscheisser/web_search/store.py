"""Store a verified (fetched, allowed) web page as a document so the next question needs no search."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.matcher import Link
from klugscheisser.domain.source_kind import ACCOUNTS_SEED
from klugscheisser.ingest.store import IngestContext, ItemDraft, store_item
from klugscheisser.interfaces import WebPage
from klugscheisser.web_search.domains import host_of, registrable_domain

WEB_LINK_CONFIDENCE = 0.8


@lru_cache
def _seed_web_domains(path: Path = ACCOUNTS_SEED) -> dict[str, tuple[str, str]]:
    """domain -> (kind, display name) from the ``web:`` section of seeds/accounts.yaml."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}
    return {
        domain.lower(): (kind, name)
        for kind, entries in (data.get("web") or {}).items()
        for domain, name in (entries or {}).items()
    }


def outlet_for(session: Session, url: str) -> tuple[str, str | None]:
    """(source_kind, outlet name) for a page: known outlets (accounts table / seed, platform "web") keep their
    kind (srf.ch -> srf, blick.ch -> boulevard, nzz.ch -> media, sihf.ch -> official); else ("web", None)."""
    domain = registrable_domain(url)
    row = session.get(m.Account, ("web", domain))
    if row is not None:
        return row.kind, row.display_name
    if domain in _seed_web_domains():
        return _seed_web_domains()[domain]
    return "web", None


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def store_web_page(ctx: IngestContext, page: WebPage, athlete_ids: list[int], query: str) -> m.Item | None:
    """Allowed page with text -> Item (source_type/platform "web"), linked to the athletes. Idempotent by URL:
    the same page again adds no new item (changed text updates it, history kept by store_item)."""
    if not page.allowed or not page.text:
        return None
    url = canonical_url(page.url)
    kind, outlet = outlet_for(ctx.session, url)
    site_name = getattr(page, "site_name", None)
    published_at = getattr(page, "published_at", None)
    host = host_of(url)
    if host.endswith("wikipedia.org"):
        # JSON-LD datePublished of a wiki page is its creation date; the last edit is the useful date
        outlet = outlet or f"Wikipedia ({host.split('.')[0]})"
        published_at = getattr(page, "modified_at", None)
    source_name = (outlet or site_name or host)[:100]
    fetched = page.fetched_at.isoformat() if page.fetched_at else None
    draft = ItemDraft(
        source_type="web",
        platform="web",
        url=url,
        title=page.title if page.title and page.title != page.url else None,
        text=page.text,
        source_name=source_name,
        published_at=published_at,
        raw={
            "query": query,
            "fetched_at": fetched,
            "domain": registrable_domain(url),
            "site_name": site_name,
            "language": getattr(page, "language", None),
        },
        source_kind=kind,
    )
    links = [Link(aid, "web", WEB_LINK_CONFIDENCE) for aid in dict.fromkeys(athlete_ids)]
    return store_item(ctx, draft, links).item
