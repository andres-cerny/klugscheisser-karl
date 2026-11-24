"""X posts about a person via the xAI (Grok) Responses API with the server-side ``x_search`` tool.

Skeleton for build plan v5 phase 8 (not wired into ingest). Research: docs/sources-v5-fis-x.md.

Request (docs.x.ai/developers/tools/x-search, as of 2026-10-03): ``POST https://api.x.ai/v1/responses`` with
``tools=[{"type": "x_search", "allowed_x_handles": [...max 20], "from_date": "YYYY-MM-DD"}]``.

What the response gives us (docs.x.ai/developers/tools/citations, /tool-usage-details):
- citations are URLs only (``https://x.com/i/status/<id>`` or ``https://x.com/<handle>/status/<id>``), in
  ``url_citation`` annotations on the ``output_text`` block (and ``citations`` in the xAI SDK);
- "server-side tool call outputs are not returned", so there is no verbatim post text, author or date from the
  tool itself. We ask the model for them via structured output (``text.format`` json_schema) and keep only
  posts whose status id is among the citations. That text is model-reported (``text_verbatim=False``).
- ``fetch_oembed`` can replace it with X's own rendering (publish.x.com/oembed, no key) -> ``text_verbatim=True``.
  Off by default: publish.x.com/robots.txt has ``Disallow: /oembed`` although X documents the endpoint for
  programmatic use. Decide before using it.

Cost: X Search is billed per item ($5 / 1k posts fetched, $10 / 1k profiles, since 2026-09-21) plus tokens.
``usage.cost_in_usd_ticks`` (1 USD = 1e10 ticks) gives the exact cost; otherwise we estimate from the counts.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.ingest import costs

log = logging.getLogger(__name__)

BASE_URL = "https://api.x.ai/v1"
RESPONSES_URL = f"{BASE_URL}/responses"
OEMBED_URL = "https://publish.x.com/oembed"
COST_SOURCE = "xai"

# Cheapest current model with tool calling + structured outputs (docs.x.ai/developers/pricing, 2026-10-03).
# Grok 4.1 Fast is no longer listed; grok-4.3 and grok-4.20-0309-non-reasoning cost the same.
DEFAULT_MODEL = "grok-4.3"
INPUT_USD_PER_TOKEN = 1.25 / 1_000_000
OUTPUT_USD_PER_TOKEN = 2.50 / 1_000_000
X_POST_USD = 5.0 / 1000
X_USER_USD = 10.0 / 1000
USD_TICKS = 10_000_000_000
MAX_HANDLES = 20
MAX_TURNS = 3  # bounds how many searches (and fetched posts) one request can run

STATUS_RE = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")

POSTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "author_handle": {"type": "string"},
                    "text": {"type": "string"},
                    "posted_at": {"type": "string"},
                },
                "required": ["url", "author_handle", "text", "posted_at"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You collect X posts about one athlete for a fact-checked sports database. Use x_search. "
    "Return only posts you actually fetched. For each post give the post URL exactly as cited, the author's "
    "@handle without '@', the full post text copied verbatim (no summary, no translation) and the post time "
    "as ISO 8601. If you found nothing, return an empty list."
)


@dataclass(frozen=True)
class XPost:
    url: str
    author: str | None
    text: str | None
    posted_at: datetime | None
    text_verbatim: bool = False  # True only after fetch_oembed (text as rendered by X)

    @property
    def status_id(self) -> str:
        m = STATUS_RE.match(self.url)
        return m.group(2) if m else ""


def _status(url: str) -> tuple[str | None, str] | None:
    m = STATUS_RE.match(url.strip())
    if not m:
        return None
    handle = None if m.group(1) == "i" else m.group(1)
    return handle, m.group(2)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def cited_urls(data: dict[str, Any]) -> list[str]:
    """All citation URLs in order: ``url_citation`` annotations, then a top-level ``citations`` list."""
    urls: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            for ann in content.get("annotations") or []:
                if ann.get("type") == "url_citation" and ann.get("url"):
                    urls.append(ann["url"])
    urls.extend(u for u in data.get("citations") or [] if isinstance(u, str))
    return urls


def output_text(data: dict[str, Any]) -> str:
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            parts.extend(
                c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"
            )
    return "".join(parts)


def parse_response(data: dict[str, Any], limit: int) -> list[XPost]:
    """Cited X posts, enriched with the model's structured fields when their status id matches.

    Posts the model lists but that are not among the citations are dropped (no invented posts)."""
    reported: dict[str, dict[str, Any]] = {}
    try:
        for p in json.loads(output_text(data) or "{}").get("posts") or []:
            st = _status(str(p.get("url", "")))
            if st:
                reported.setdefault(st[1], p)
    except (json.JSONDecodeError, AttributeError):
        log.info("x_search: output is not the requested JSON; using citations only")

    posts: list[XPost] = []
    seen: set[str] = set()
    for url in cited_urls(data):
        st = _status(url)
        if not st or st[1] in seen:
            continue
        handle, sid = st
        seen.add(sid)
        rep = reported.get(sid, {})
        author = handle or (str(rep.get("author_handle") or "").lstrip("@") or None)
        posts.append(
            XPost(
                url=f"https://x.com/{author or 'i'}/status/{sid}",
                author=author,
                text=(rep.get("text") or None),
                posted_at=_parse_time(rep.get("posted_at")),
            )
        )
        if len(posts) >= limit:
            break
    return posts


def cost_usd(usage: dict[str, Any] | None) -> float:
    """Exact cost from ``cost_in_usd_ticks`` when present, else an estimate from tokens and fetched items."""
    usage = usage or {}
    ticks = usage.get("cost_in_usd_ticks")
    if isinstance(ticks, int):
        return ticks / USD_TICKS
    tools = usage.get("server_side_tool_usage_details") or {}
    return (
        int(usage.get("input_tokens") or 0) * INPUT_USD_PER_TOKEN
        + int(usage.get("output_tokens") or 0) * OUTPUT_USD_PER_TOKEN
        + int(tools.get("x_posts_fetched") or 0) * X_POST_USD
        + int(tools.get("x_users_fetched") or 0) * X_USER_USD
    )


class GrokXSearch:
    name = "x_grok"

    def __init__(
        self, settings: Settings, client: httpx.Client | None = None, model: str = DEFAULT_MODEL
    ) -> None:
        self.settings = settings
        self.model = model
        self.http = client or httpx.Client(timeout=180.0)
        self.last_usage: dict[str, Any] = {}

    def build_request(
        self, name: str, handles: list[str] | None, since: date | None, limit: int
    ) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": "x_search"}
        clean = [h.strip().lstrip("@") for h in handles or [] if h.strip().lstrip("@")]
        if clean:
            tool["allowed_x_handles"] = clean[:MAX_HANDLES]
        if since:
            tool["from_date"] = since.isoformat()
        prompt = f"Find up to {limit} recent X posts by or about the athlete {name}."
        if clean:
            prompt += " Their X account(s): " + ", ".join("@" + h for h in clean[:MAX_HANDLES]) + "."
        return {
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "text": {
                "format": {"type": "json_schema", "name": "x_posts", "schema": POSTS_SCHEMA, "strict": True}
            },
            "max_turns": MAX_TURNS,
            "store": False,
        }

    def search_person(
        self,
        name: str,
        handles: list[str] | None = None,
        since: date | None = None,
        limit: int = 10,
        session: Session | None = None,
    ) -> list[XPost]:
        """``session``: when given, the request cost is recorded in cost_ledger as source "xai" (flush only)."""
        key = self.settings.xai_api_key
        if key is None or not key.get_secret_value():
            raise RuntimeError("XAI_API_KEY is not set")
        r = self.http.post(
            RESPONSES_URL,
            json=self.build_request(name, handles, since, limit),
            headers={"Authorization": f"Bearer {key.get_secret_value()}", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        self.last_usage = data.get("usage") or {}
        if session is not None:
            fetched = int(
                (self.last_usage.get("server_side_tool_usage_details") or {}).get("x_posts_fetched") or 0
            )
            costs.record(
                session, COST_SOURCE, fetched, cost_usd(self.last_usage), note=f"x_search {name}"[:200]
            )
        return parse_response(data, limit)

    def fetch_oembed(self, post: XPost) -> XPost:
        """Replace text/author/date with X's own rendering of the post. See module docstring (robots.txt)."""
        r = self.http.get(OEMBED_URL, params={"url": post.url, "omit_script": "1", "dnt": "true"})
        if r.status_code == 404:
            return post  # deleted or protected: caller should drop it
        r.raise_for_status()
        return parse_oembed(r.json(), post)


_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_DATE_RE = re.compile(r"<a [^>]*>([A-Z][a-z]+ \d{1,2}, \d{4})</a>\s*</blockquote>")


def parse_oembed(data: dict[str, Any], post: XPost) -> XPost:
    markup = str(data.get("html") or "")
    m = _P_RE.search(markup)
    if not m:
        return post
    text = re.sub(r"<br\s*/?>", "\n", m.group(1))
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    author = str(data.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1] or post.author
    posted_at = post.posted_at
    d = _DATE_RE.search(markup)
    if d and posted_at is None:
        posted_at = datetime.strptime(d.group(1), "%B %d, %Y").replace(tzinfo=UTC)  # date only
    url = str(data.get("url") or post.url)
    return replace(post, url=url, author=author, text=text, posted_at=posted_at, text_verbatim=True)
