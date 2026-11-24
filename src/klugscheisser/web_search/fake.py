"""Deterministic WebSearch for tests and offline runs: results and pages come from a fixture directory.

Layout: ``<dir>/search.json`` = ``{"<query>": [{"url", "title", "snippet"}, ...], "*": [...]}`` (``*`` is the
fallback for unknown queries); pages are ``<dir>/pages/<name>.html`` mapped by ``<dir>/pages.json`` =
``{"<url>": "<name>.html" | null}`` (null = not allowed). Pages run through the same extraction as Brave.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from klugscheisser.interfaces import WebPage, WebResult
from klugscheisser.web_search.brave import FetchedPage
from klugscheisser.web_search.extract import TEXT_MAX, extract_main_text, extract_meta


class FakeWebSearch:
    name = "fake"

    def __init__(
        self, fixtures: Path | str = "tests/fixtures/web", fetched_at: datetime | None = None
    ) -> None:
        self.dir = Path(fixtures)
        self.results = json.loads((self.dir / "search.json").read_text())
        pages = self.dir / "pages.json"
        self.pages: dict[str, str | None] = json.loads(pages.read_text()) if pages.exists() else {}
        self.fetched_at = fetched_at or datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 5) -> list[WebResult]:
        self.queries.append(query)
        rows = self.results.get(query, self.results.get("*", []))
        return [WebResult(url=r["url"], title=r["title"], snippet=r.get("snippet", "")) for r in rows[:limit]]

    def fetch(self, url: str) -> WebPage:
        name = self.pages.get(url)
        if name is None:
            note = "robots.txt verbietet das Lesen" if url in self.pages else "nicht in den Testdaten"
            return WebPage(
                url=url, title=url, text=None, allowed=False, fetched_at=self.fetched_at, note=note
            )
        page = (self.dir / "pages" / name).read_text()
        meta = extract_meta(page)
        return FetchedPage(
            url=url,
            title=meta.title or url,
            text=extract_main_text(page, TEXT_MAX),
            allowed=True,
            fetched_at=self.fetched_at,
            site_name=meta.site_name,
            published_at=meta.published_at,
            modified_at=meta.modified_at,
            language=meta.language,
        )
