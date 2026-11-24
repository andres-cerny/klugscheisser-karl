"""Web search via the Brave Search API, plus fetching of allowed result pages.

Search: ``GET https://api.search.brave.com/res/v1/web/search`` with header ``X-Subscription-Token`` and
params ``q``, ``count`` (max 20), ``country`` (2-letter, e.g. CH), ``search_lang`` (e.g. de); results are in
``web.results[]`` with ``title``, ``url``, ``description`` (may contain <strong> highlights). Every request is
recorded in cost_ledger as source "brave"; at ``BRAVE_DAILY_CAP`` requests per rolling 24 h searching stops.

Fetch: only http(s) pages that are not on the domain blocklist, not on an outlet whose terms forbid automated
reading (seeds/feeds.yaml ``fetch_article: false``) and allowed by robots.txt for our identified user agent.
Redirects are followed by hand so every hop is checked the same way. Otherwise ``WebPage(allowed=False)``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import USER_AGENT, strip_html
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.ingest import costs
from klugscheisser.interfaces import WebPage, WebResult
from klugscheisser.web_search.domains import is_blocked, registrable_domain, terms_restricted_domains
from klugscheisser.web_search.extract import extract_main_text, extract_meta

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_ENDPOINTS = {
    "web": BRAVE_URL,
    "news": "https://api.search.brave.com/res/v1/news/search",
    "videos": "https://api.search.brave.com/res/v1/videos/search",
}
# Brave accepts only some country codes (CZ is rejected); unknown ones are left out
BRAVE_COUNTRIES = {"CH", "DE", "AT", "US", "CA", "FI", "SE", "DK", "JP", "FR", "IT", "GB", "NO", "ALL"}
COST_SOURCE = "brave"
MAX_REDIRECTS = 5
HTML_TYPES = ("text/html", "application/xhtml+xml")

NOTE_BLOCKED = "Domain gesperrt (Blocklist)"
NOTE_TERMS = "Nutzungsbedingungen erlauben kein automatisches Lesen"
NOTE_ROBOTS = "robots.txt verbietet das Lesen"
NOTE_ROBOTS_UNREACHABLE = "robots.txt nicht erreichbar"


@dataclass
class FetchedPage(WebPage):
    """WebPage plus metadata found on the page (used by store_web_page)."""

    site_name: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    language: str | None = None


class BraveSearch:
    name = "brave"

    def __init__(
        self,
        settings: Settings,
        session: Session | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """``session``: where cost is recorded (flushed only; the caller commits). Without it each search uses
        its own short transaction."""
        self.settings = settings
        self.session = session
        self.http = client or httpx.Client(
            timeout=settings.web_fetch_timeout_s, headers={"User-Agent": USER_AGENT}
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self.last_note: str | None = None  # why the last search returned nothing, e.g. daily cap

    # --- search -----------------------------------------------------------------------------------

    @contextmanager
    def _db(self) -> Iterator[Session]:
        if self.session is not None:
            yield self.session
            return
        from klugscheisser.db.session import session_scope

        with session_scope() as s:  # current project's database
            yield s

    def requests_today(self, session: Session) -> int:
        since = datetime.now(UTC) - timedelta(hours=24)
        return int(
            session.scalar(
                select(func.coalesce(func.sum(m.CostLedger.units), 0)).where(
                    m.CostLedger.source == COST_SOURCE, m.CostLedger.created_at >= since
                )
            )
        )

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        endpoint: str = "web",
        country: str | None = None,
        search_lang: str | None = None,
        keep_social: bool = False,
    ) -> list[WebResult]:
        """v8: `endpoint` web | news | videos; `country`/`search_lang` override the settings for one query
        (e.g. Czech words without a country); `keep_social` keeps instagram/facebook/x links (handle checks,
        podcast/video links), which are otherwise dropped as collected elsewhere."""
        self.last_note = None
        key = self.settings.brave_api_key
        if key is None:
            raise RuntimeError("BRAVE_API_KEY is not set")
        with self._db() as s:
            used = self.requests_today(s)
            if used >= self.settings.brave_daily_cap:
                self.last_note = f"Brave-Tageslimit erreicht ({used}/{self.settings.brave_daily_cap})"
                log.warning(self.last_note)
                return []
            params: dict[str, str | int] = {"q": query, "count": min(20, max(1, limit + 5))}
            cc = (country if country is not None else self.settings.brave_country or "").upper()
            if cc in BRAVE_COUNTRIES:
                params["country"] = cc
            lang = search_lang if search_lang is not None else self.settings.brave_search_lang
            if lang:
                params["search_lang"] = lang
            r = self.http.get(
                BRAVE_ENDPOINTS.get(endpoint, BRAVE_URL),
                params=params,
                headers={"Accept": "application/json", "X-Subscription-Token": key.get_secret_value()},
            )
            if r.status_code == 200:
                costs.record(s, COST_SOURCE, 1, self.settings.brave_cost_per_request_usd, note=query[:200])
            r.raise_for_status()
            blocked = self.settings.web_blocked_domains
            if keep_social:
                social = (
                    "instagram.com",
                    "facebook.com",
                    "x.com",
                    "twitter.com",
                    "youtube.com",
                    "tiktok.com",
                )
                blocked = [d for d in blocked if d not in social]
            return parse_brave(r.json(), blocked, limit)

    # --- fetch ------------------------------------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser(origin + "/robots.txt")
            try:
                r = self.http.get(origin + "/robots.txt", follow_redirects=True)
            except httpx.HTTPError as exc:
                log.info("robots.txt %s: %s", origin, exc)
                self._robots[origin] = None
                return None
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif 400 <= r.status_code < 500:
                rp.allow_all = True
            elif r.status_code >= 500:
                self._robots[origin] = None
                return None
            else:
                rp.parse(r.text.splitlines())
            self._robots[origin] = rp
        return self._robots[origin]

    def check_allowed(self, url: str) -> str | None:
        """None when the page may be fetched, else the reason."""
        if urlsplit(url).scheme not in ("http", "https"):
            return "kein http(s)-Link"
        if is_blocked(url, self.settings.web_blocked_domains):
            return NOTE_BLOCKED
        if (
            registrable_domain(url) in terms_restricted_domains()
            and not self.settings.news_fetch_all_articles
        ):
            return NOTE_TERMS
        rp = self._robots_for(url)
        if rp is None:
            return NOTE_ROBOTS_UNREACHABLE
        if not rp.can_fetch(USER_AGENT, url):
            return NOTE_ROBOTS
        return None

    def fetch(self, url: str) -> WebPage:
        now = datetime.now(UTC)
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            reason = self.check_allowed(current)
            if reason:
                return FetchedPage(
                    url=current, title=current, text=None, allowed=False, fetched_at=now, note=reason
                )
            try:
                body, status, headers = self._get(current)
            except httpx.HTTPError as exc:
                return FetchedPage(
                    url=current, title=current, text=None, allowed=True, fetched_at=now, note=str(exc)
                )
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                current = urljoin(current, headers["location"])
                continue
            break
        else:
            return FetchedPage(
                url=current,
                title=current,
                text=None,
                allowed=True,
                fetched_at=now,
                note="zu viele Weiterleitungen",
            )
        if status != 200:
            return FetchedPage(
                url=current, title=current, text=None, allowed=True, fetched_at=now, note=f"HTTP {status}"
            )
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and ctype not in HTML_TYPES:
            return FetchedPage(
                url=current,
                title=current,
                text=None,
                allowed=True,
                fetched_at=now,
                note=f"kein HTML ({ctype})",
            )
        page = body.decode(_charset(headers.get("content-type", "")) or "utf-8", errors="replace")
        meta = extract_meta(page)
        text = extract_main_text(page, self.settings.web_text_max_chars)
        return FetchedPage(
            url=current,
            title=meta.title or current,
            text=text,
            allowed=True,
            fetched_at=now,
            note=None if text else "kein Haupttext gefunden",
            site_name=meta.site_name,
            published_at=meta.published_at,
            modified_at=meta.modified_at,
            language=meta.language,
        )

    def _get(self, url: str) -> tuple[bytes, int, httpx.Headers]:
        """GET without following redirects; reads at most ``web_fetch_max_bytes``."""
        limit = self.settings.web_fetch_max_bytes
        with self.http.stream("GET", url, follow_redirects=False) as r:
            chunks, size = [], 0
            if r.status_code == 200:
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        break
            return b"".join(chunks)[:limit], r.status_code, r.headers


def _charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"' ")
    return None


def parse_brave(data: dict, blocked: list[str], limit: int) -> list[WebResult]:
    out: list[WebResult] = []
    seen: set[str] = set()
    rows = (data.get("web") or {}).get("results") or data.get("results") or []  # web | news, videos
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url or url in seen or is_blocked(url, blocked):
            continue
        seen.add(url)
        video = row.get("video") or {}
        extra = {k: v for k, v in (("dauer", video.get("duration")), ("kanal", video.get("creator"))) if v}
        out.append(
            WebResult(
                url=url,
                title=strip_html(row.get("title") or ""),
                snippet=strip_html(row.get("description") or ""),
                page_age=row.get("page_age") or None,
                extra=extra or None,
            )
        )
        if len(out) >= limit:
            break
    return out
