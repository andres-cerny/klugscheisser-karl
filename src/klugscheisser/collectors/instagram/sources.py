"""Instagram sources: Apify (scraper), Meta Graph Business Discovery (official API, free), Bright Data
(brightdata.py, free records per month), the hybrid of them (Graph, then Bright Data, Apify for the rest),
fixtures. See docs/instagram-sources.md."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from klugscheisser.collectors.instagram.base import (
    MAX_CAROUSEL_IMAGES,
    FetchResult,
    InstagramPost,
    InstagramProfile,
    caption_hashtags,
    caption_mentions,
    group_by_handle,
    parse_apidojo_post,
    parse_apify_post,
    parse_apify_profile,
    shortcode_from_url,
)
from klugscheisser.collectors.instagram.brightdata import BrightDataInstagramSource
from klugscheisser.config import Settings
from klugscheisser.interfaces import MediaRef

log = logging.getLogger(__name__)

_DEFAULT = object()  # hybrid: "build this part from the settings if configured"


class FixtureInstagramSource:
    """Reads Apify-format JSON from tests/fixtures/instagram. Free, offline."""

    name = "fixture"
    paid = False

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _load(self, pattern: str) -> list[dict]:
        rows: list[dict] = []
        for path in sorted(self.dir.glob(pattern)):
            rows.extend(json.loads(path.read_text()))
        return rows

    def fetch_posts(self, handles: list[str], since: datetime | None, limit: int = 12) -> FetchResult:
        posts = [parse_apify_post(d) for d in self._load("posts*.json")]
        if since:
            posts = [p for p in posts if p.published_at and p.published_at > since]
        grouped = group_by_handle(posts, handles)
        return FetchResult(posts={h: ps[:limit] for h, ps in grouped.items()}, source=self.name)

    def fetch_mentions(self, handles: list[str], since: datetime | None, limit: int = 50) -> FetchResult:
        """Posts of other accounts (fixture files mentions*.json) that tag these handles."""
        posts = [parse_apify_post(d) for d in self._load("mentions*.json")]
        if since:
            posts = [p for p in posts if p.published_at and p.published_at > since]
        out: dict[str, list] = {h: [] for h in handles}
        for p in posts:
            tagged = {x.lower() for x in (*p.tagged, *p.mentions, *p.coauthors)}
            for h in handles:
                if h.lower() in tagged:
                    out[h].append(p)
        return FetchResult(posts={h: ps[:limit] for h, ps in out.items()}, source=self.name)

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        wanted = {h.lower() for h in handles}
        return [p for p in map(parse_apify_profile, self._load("profiles*.json")) if p.username in wanted]


APIDOJO = "apidojo/instagram-scraper"


class ApifyInstagramSource:
    """apify/instagram-scraper (default): for a profile URL it returns the profile's posts, including
    collaboration posts of others. With APIFY_INSTAGRAM_ACTOR=apidojo/instagram-scraper posts and mentions come
    from that cheaper actor (no tagged users, no alt text, no carousel children); profiles always come from
    APIFY_INSTAGRAM_PROFILE_ACTOR (it has bios)."""

    name = "apify"
    paid = True

    def __init__(self, settings: Settings) -> None:
        from apify_client import ApifyClient

        self.client = ApifyClient(settings.apify_token.get_secret_value())
        self.actor = settings.apify_instagram_actor
        self.profile_actor = settings.apify_instagram_profile_actor
        self.max_run_usd = Decimal(str(settings.apify_daily_cost_cap_usd))
        self.price_per_result = settings.apify_cost_per_post_usd
        self.last_paid_count: int | None = None  # handles of the last fetch_profiles (all of them are paid)

    @property
    def apidojo(self) -> bool:
        return self.actor.strip().lower() == APIDOJO

    def _cap(self, max_usd: float | None) -> Decimal:
        return Decimal(str(max_usd)) if max_usd else self.max_run_usd

    def _run(self, run_input: dict, max_usd: Decimal, actor: str | None = None) -> tuple[list[dict], float]:
        run = self.client.actor(actor or self.actor).call(
            run_input=run_input, run_timeout=timedelta(minutes=10), max_total_charge_usd=max_usd, logger=None
        )
        if run is None:
            return [], 0.0
        r = run.model_dump() if hasattr(run, "model_dump") else dict(run)
        dataset = r.get("default_dataset_id") or r.get("defaultDatasetId")
        items = list(self.client.dataset(dataset).iterate_items())
        # usage_total_usd lags behind for pay-per-event actors; derive cost from the result price
        reported = float(r.get("usage_total_usd") or r.get("usageTotalUsd") or 0.0)
        events = ((r.get("pricing_info") or {}).get("pricing_per_event") or {}).get(
            "actor_charge_events"
        ) or {}
        price = float((events.get("result") or {}).get("event_price_usd") or self.price_per_result)
        return items, max(reported, len(items) * price)

    @staticmethod
    def apidojo_input(urls: list[str], since: datetime | None, max_items: int) -> dict:
        run_input: dict = {"startUrls": urls, "maxItems": max_items}
        if since:
            run_input["until"] = since.date().isoformat()  # "posts newer than this date"
        return run_input

    def fetch_posts(
        self, handles: list[str], since: datetime | None, limit: int = 12, max_usd: float | None = None
    ) -> FetchResult:
        urls = [f"https://www.instagram.com/{h}/" for h in handles]
        if self.apidojo:  # maxItems counts for the whole run, not per profile
            items, cost = self._run(self.apidojo_input(urls, since, limit * len(handles)), self._cap(max_usd))
            posts = [parse_apidojo_post(d) for d in items if d.get("url") or d.get("code")]
            return FetchResult(posts=group_by_handle(posts, handles), cost_usd=cost, source=self.name)
        run_input = {"directUrls": urls, "resultsType": "posts", "resultsLimit": limit}
        if since:
            run_input["onlyPostsNewerThan"] = since.date().isoformat()
        items, cost = self._run(run_input, self._cap(max_usd))
        posts = [parse_apify_post(d) for d in items if d.get("url") or d.get("shortCode")]
        return FetchResult(posts=group_by_handle(posts, handles), cost_usd=cost, source=self.name)

    def fetch_mentions(
        self, handles: list[str], since: datetime | None, limit: int = 50, max_usd: float | None = None
    ) -> FetchResult:
        """v8: posts of other accounts that tag/mention these handles (friends, team, sponsors, fans)."""
        if self.apidojo:
            return self._apidojo_mentions(handles, since, limit, max_usd)
        run_input = {
            "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
            "resultsType": "mentions",
            "resultsLimit": limit,
        }
        if since:
            run_input["onlyPostsNewerThan"] = since.date().isoformat()
        items, cost = self._run(run_input, self._cap(max_usd))
        posts = [parse_apify_post(d) for d in items if d.get("url") or d.get("shortCode")]
        by_handle: dict[str, list] = {h: [] for h in handles}
        for p in posts:  # a mention belongs to the handle it tags or mentions
            tagged = {x.lower() for x in (*p.tagged, *p.mentions, *p.coauthors)}
            for h in handles:
                if h.lower() in tagged or len(handles) == 1:
                    by_handle[h].append(p)
        return FetchResult(posts=by_handle, cost_usd=cost, source=self.name)

    def _apidojo_mentions(
        self, handles: list[str], since: datetime | None, limit: int, max_usd: float | None
    ) -> FetchResult:
        """apidojo lists no tagged users: one run per handle on its /tagged/ page, so every post found there
        belongs to that handle (added to `tagged`, so the matcher links it)."""
        by_handle: dict[str, list] = {h: [] for h in handles}
        total = 0.0
        for h in handles:
            left = None if max_usd is None else max_usd - total
            if left is not None and left <= 0:
                break
            items, cost = self._run(
                self.apidojo_input([f"https://www.instagram.com/{h}/tagged/"], since, limit), self._cap(left)
            )
            total += cost
            for d in items:
                if not (d.get("url") or d.get("code")):
                    continue
                post = parse_apidojo_post(d)
                if h.lower() not in post.tagged:
                    post.tagged.append(h.lower())
                by_handle[h].append(post)
        return FetchResult(posts=by_handle, cost_usd=total, source=self.name)

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        self.last_paid_count = len(handles)
        items, _ = self._run(
            {"directUrls": [f"https://www.instagram.com/{h}/" for h in handles], "resultsType": "details"},
            Decimal("0.20"),
            actor=self.profile_actor,
        )
        return [parse_apify_profile(d) for d in items if d.get("username")]


# --- Meta Graph Business Discovery ----------------------------------------------------------------------

GRAPH_RATE_LIMIT_CODES = {4, 17, 32, 613}
GRAPH_TOKEN_CODES = {190}
GRAPH_PERMISSION_CODES = {10, *range(200, 300)}
GRAPH_USAGE_STOP_PCT = 95  # stop before Meta blocks us (x-app-usage / x-business-use-case-usage)
_GRAPH_MEDIA = {"IMAGE": "image", "VIDEO": "video", "CAROUSEL_ALBUM": "carousel"}


class GraphError(Exception):
    """A Graph call failed for this handle (network, 5xx): try another source for it."""


class GraphStopped(GraphError):
    """No more Graph calls in this run (rate limit, call cap, token expired, missing permission)."""


class GraphCallBudget:
    """Graph calls in the last hour, shared by every Graph source of this process (the nightly run builds a
    new source per bucket; Meta's limit is ~200 calls per rolling hour per app). After a rate-limit or token
    error no further call is made for an hour."""

    def __init__(self) -> None:
        self.calls: deque[float] = deque()
        self.blocked_until = 0.0
        self.reason: str | None = None

    def _purge(self, now: float) -> None:
        while self.calls and self.calls[0] < now - 3600:
            self.calls.popleft()

    def check(self, cap: int) -> str | None:
        """Why no call may be made now (None: go ahead)."""
        now = time.monotonic()
        if now < self.blocked_until:
            return self.reason
        self._purge(now)
        if len(self.calls) >= cap:
            return f"Graph-Aufrufe: Limite {cap} pro Stunde erreicht"
        return None

    def add(self) -> None:
        self.calls.append(time.monotonic())

    def block(self, reason: str, seconds: float = 3600) -> None:
        self.blocked_until, self.reason = time.monotonic() + seconds, reason


GRAPH_BUDGET = GraphCallBudget()


def reset_graph_budget() -> None:
    """Tests: forget earlier calls and blocks."""
    global GRAPH_BUDGET
    GRAPH_BUDGET = GraphCallBudget()


def _usage_pct(headers: httpx.Headers) -> float:
    """Highest usage percentage Meta reports in the rate-limit headers (0 when absent)."""
    pct = 0.0
    for name in ("x-app-usage", "x-business-use-case-usage", "x-ad-account-usage"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        entries = [data] if isinstance(data, dict) and "call_count" in data else []
        if isinstance(data, dict) and not entries:
            for v in data.values():
                entries += v if isinstance(v, list) else [v]
        for e in entries:
            if isinstance(e, dict):
                for key in ("call_count", "total_time", "total_cputime"):
                    if isinstance(e.get(key), int | float):
                        pct = max(pct, float(e[key]))
    return pct


def _graph_ref(x: dict) -> MediaRef | None:
    if x.get("media_type") == "VIDEO":
        if x.get("media_url"):
            return MediaRef(kind="video", url=x["media_url"])
        if x.get("thumbnail_url"):  # videos with licensed music come without media_url
            return MediaRef(kind="image", url=x["thumbnail_url"])
        return None
    if x.get("media_url"):
        return MediaRef(kind="image", url=x["media_url"])
    return None


def graph_media_refs(m: dict) -> list[MediaRef]:
    """Images (up to MAX_CAROUSEL_IMAGES of a carousel) and videos of a Graph media object."""
    children = [r for c in ((m.get("children") or {}).get("data") or []) if (r := _graph_ref(c))]
    if children:
        return children[:MAX_CAROUSEL_IMAGES]
    ref = _graph_ref(m)
    return [ref] if ref else []


def parse_graph_post(m: dict, handle: str, full_name: str | None) -> InstagramPost:
    caption = m.get("caption") or ""
    permalink = m.get("permalink") or ""
    ts = m.get("timestamp")
    return InstagramPost(
        post_id=str(m["id"]),
        shortcode=shortcode_from_url(permalink),
        url=permalink,
        owner_username=(m.get("username") or handle).lower(),
        owner_full_name=full_name,
        published_at=datetime.fromisoformat(ts.replace("+0000", "+00:00")) if ts else None,
        caption=caption,
        media_type=_GRAPH_MEDIA.get(m.get("media_type") or ""),
        hashtags=caption_hashtags(caption),
        mentions=caption_mentions(caption),
        likes=m.get("like_count"),
        comments=m.get("comments_count"),
        raw={k: v for k, v in m.items() if k not in {"media_url", "thumbnail_url", "children"}}
        | {"via": "graph"},
        media=graph_media_refs(m),
    )


def parse_graph_profile(d: dict) -> InstagramProfile:
    return InstagramProfile(
        username=(d.get("username") or "").lower(),
        full_name=d.get("name"),
        biography=d.get("biography"),
        is_business=True,  # Business Discovery only sees Business/Creator accounts
        followers=d.get("followers_count"),
        posts_count=d.get("media_count"),
        private=False,  # professional accounts cannot be private
        external_url=d.get("website"),
        source="graph",
    )


class GraphBusinessDiscoverySource:
    """Official Meta Graph API, free. Needs our own IG professional account + token; sees only Business/Creator
    accounts and only their own posts (no tagged posts, no mentions). Handles it cannot serve end up in
    `FetchResult.missing`; `status` remembers per handle whether Graph can read it (False: not professional or
    not found); `stopped` says why no more calls are made in this run."""

    name = "graph"
    paid = False
    PROFILE_FIELDS = "username,name,biography,followers_count,media_count,website"
    MEDIA_FIELDS = (
        "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
        "like_count,comments_count,username,children{media_type,media_url,thumbnail_url}"
    )

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.token = settings.meta_graph_token.get_secret_value()
        self.ig_user_id = settings.meta_ig_business_user_id
        self.api = f"https://graph.facebook.com/{settings.meta_graph_api_version.strip('/')}"
        self.max_calls = settings.meta_graph_max_calls_per_run
        self.page_size = max(1, settings.meta_graph_page_size)
        self.client = client or httpx.Client(timeout=30)
        self.status: dict[str, bool] = {}
        self.profiles: dict[str, InstagramProfile] = {}  # profiles seen while fetching posts
        self.stopped: str | None = None
        self.calls = 0
        self.last_paid_count = 0

    def _stop(self, reason: str, block_s: float = 3600) -> GraphStopped:
        self.stopped = reason
        GRAPH_BUDGET.block(reason, block_s)
        return GraphStopped(reason)

    def _call(self, handle: str, fields: str) -> dict | None:
        """business_discovery of one handle; None when Graph cannot read it (not professional, not found)."""
        if self.stopped:
            raise GraphStopped(self.stopped)
        if reason := GRAPH_BUDGET.check(self.max_calls):
            self.stopped = reason
            raise GraphStopped(reason)
        try:
            r = self.client.get(
                f"{self.api}/{self.ig_user_id}",
                params={
                    "fields": f"business_discovery.username({handle}){{{fields}}}",
                    "access_token": self.token,
                },
            )
        except httpx.HTTPError as exc:
            raise GraphError(f"@{handle}: {type(exc).__name__}") from exc
        GRAPH_BUDGET.add()
        self.calls += 1
        if r.status_code >= 400:
            try:
                err = (r.json() or {}).get("error") or {}
            except ValueError:
                err = {}
            code, msg = err.get("code"), str(err.get("message") or "")[:200]
            if code in GRAPH_RATE_LIMIT_CODES:
                log.warning("Graph rate limit (code %s): rest of this run via Apify. %s", code, msg)
                raise self._stop(f"Graph-Ratenlimit (Code {code})")
            if code in GRAPH_TOKEN_CODES:
                log.error("Graph token invalid or expired (code 190): renew META_GRAPH_TOKEN. %s", msg)
                raise self._stop("Graph-Token abgelaufen/ungültig (Code 190)")
            if code in GRAPH_PERMISSION_CODES:
                log.error("Graph permission missing (code %s): check the app's permissions. %s", code, msg)
                raise self._stop(f"Graph-Berechtigung fehlt (Code {code})")
            if r.status_code == 400:  # not a Business/Creator account, or no such account
                self.status[handle.lower()] = False
                return None
            raise GraphError(f"@{handle}: HTTP {r.status_code} {msg}")
        if _usage_pct(r.headers) >= GRAPH_USAGE_STOP_PCT:
            log.warning("Graph usage near the limit: no more Graph calls for an hour")
            self._stop("Graph-Nutzung fast am Limit", 3600)
        data = (r.json() or {}).get("business_discovery")
        self.status[handle.lower()] = data is not None
        return data

    def discover_posts(self, handle: str, since: datetime | None, limit: int) -> list[InstagramPost] | None:
        """Own posts newer than `since`, newest first, up to `limit` (several pages if needed). None: Graph
        cannot read this account. Raises GraphStopped/GraphError (a half-read history is not returned)."""
        posts: list[InstagramPost] = []
        after: str | None = None
        while len(posts) < limit:
            n = min(self.page_size, limit - len(posts))
            media = f"media.after({after}).limit({n})" if after else f"media.limit({n})"
            fields = f"{self.PROFILE_FIELDS if after is None else 'username'},{media}{{{self.MEDIA_FIELDS}}}"
            data = self._call(handle, fields)
            if data is None:
                return None
            if after is None:
                self.profiles[handle.lower()] = parse_graph_profile(data)
            block = data.get("media") or {}
            page = block.get("data") or []
            for m in page:
                post = parse_graph_post(m, handle, data.get("name") or self._full_name(handle))
                if since and post.published_at and post.published_at <= since:
                    continue  # older than wanted (a pinned old post may come first: don't stop on it)
                posts.append(post)
                if len(posts) >= limit:
                    break
            last = parse_graph_post(page[-1], handle, None).published_at if page else None
            after = ((block.get("paging") or {}).get("cursors") or {}).get("after")
            if not page or not after or (since is not None and last is not None and last <= since):
                break
        return posts[:limit]

    def _full_name(self, handle: str) -> str | None:
        p = self.profiles.get(handle.lower())
        return p.full_name if p else None

    def fetch_posts(self, handles: list[str], since: datetime | None, limit: int = 12) -> FetchResult:
        out: dict[str, list[InstagramPost]] = {}
        missing: list[str] = []
        for i, h in enumerate(handles):
            if self.status.get(h.lower()) is False:
                missing.append(h)
                continue
            try:
                posts = self.discover_posts(h, since, limit)
            except GraphStopped:
                missing += handles[i:]
                break
            except GraphError as exc:
                log.warning("Graph posts failed for @%s: %s", h, exc)
                missing.append(h)
                continue
            if posts is None:
                missing.append(h)
            else:
                out[h] = posts
        return FetchResult(posts=out, source=self.name, missing=missing, paid_handles=0)

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        profiles: list[InstagramProfile] = []
        for h in handles:
            key = h.lower()
            if key in self.profiles:
                profiles.append(self.profiles[key])
                continue
            if self.status.get(key) is False or self.stopped:
                continue
            try:
                data = self._call(h, self.PROFILE_FIELDS)
            except GraphStopped:
                break
            except GraphError as exc:
                log.warning("Graph profile failed for @%s: %s", h, exc)
                continue
            if data is not None:
                self.profiles[key] = parse_graph_profile(data)
                profiles.append(self.profiles[key])
        return profiles


# --- hybrid: Graph first, then Bright Data, Apify for the rest ---------------------------------------------


class HybridInstagramSource:
    """Own posts and profiles in this order, each part optional: Graph (free) for every handle it can read,
    then Bright Data (free records per month) for the rest, then Apify (paid) in ONE batched call for what is
    still missing. Mentions (posts of others tagging a handle) only exist on Apify; without Apify they are
    skipped (all handles `missing`). Remembers per run which handles Graph cannot read; `hints` (from
    handle.profile["graph_ok"], set by the ingest) skip handles known not to work and make the budget estimate
    count only handles that will go to Apify."""

    name = "hybrid"

    def __init__(
        self,
        settings: Settings,
        graph: GraphBusinessDiscoverySource | None = _DEFAULT,
        apify: ApifyInstagramSource | None = _DEFAULT,
        brightdata: BrightDataInstagramSource | None = _DEFAULT,
    ) -> None:
        f = settings.features
        if graph is _DEFAULT:
            graph = GraphBusinessDiscoverySource(settings) if f["graph_business_discovery"] else None
        if apify is _DEFAULT:
            apify = ApifyInstagramSource(settings) if f["apify"] else None
        if brightdata is _DEFAULT:
            brightdata = BrightDataInstagramSource(settings) if f["brightdata"] else None
        self.graph, self.apify, self.brightdata = graph, apify, brightdata
        self.hints: dict[str, bool] = {}
        self.last_paid_count: int | None = None

    @property
    def paid(self) -> bool:
        """Only the Apify part costs money: without Apify the hybrid is free."""
        return self.apify is not None

    @property
    def graph_status(self) -> dict[str, bool]:
        return self.graph.status if self.graph is not None else {}

    @property
    def page_states(self) -> dict[str, str]:
        """Bright Data's "private" / "unavailable" per handle (dead_page records), for the ingest."""
        return self.brightdata.page_states if self.brightdata is not None else {}

    def set_graph_hints(self, hints: dict[str, bool]) -> None:
        self.hints.update({h.lower(): ok for h, ok in hints.items()})

    def _try_graph(self, handle: str) -> bool:
        if self.graph is None or self.graph.stopped:
            return False
        key = handle.lower()
        return self.graph.status.get(key, self.hints.get(key)) is not False

    def _graph_ok(self, handle: str) -> bool:
        key = handle.lower()
        return (
            self.graph is not None
            and not self.graph.stopped
            and self.graph.status.get(key, self.hints.get(key)) is True
        )

    def _brightdata_open(self) -> bool:
        bd = self.brightdata
        return bd is not None and not bd.stopped and bd.quota.room() > 0

    def likely_paid(self, handles: list[str]) -> list[str]:
        """Handles that will probably go to Apify: none without Apify or while Bright Data has records left
        this month (if it fails after all, Apify still runs inside the caller's max_usd); else all not known
        to work on Graph (unknown ones count as paid, the estimate errs on the safe side)."""
        if self.apify is None or self._brightdata_open():
            return []
        return [h for h in handles if not self._graph_ok(h)]

    def fetch_posts(
        self, handles: list[str], since: datetime | None, limit: int = 12, max_usd: float | None = None
    ) -> FetchResult:
        via_graph = [h for h in handles if self._try_graph(h)]
        g = self.graph.fetch_posts(via_graph, since, limit) if via_graph else FetchResult(posts={})
        posts = dict(g.posts)
        rest = [h for h in handles if h not in posts]
        if rest and self.brightdata is not None:
            b = self.brightdata.fetch_posts(rest, since, limit)
            posts.update(b.posts)
            rest = [h for h in rest if h not in posts]
        if not rest:
            return FetchResult(posts=posts, source=self.name, paid_handles=0)
        if self.apify is None or (max_usd is not None and max_usd <= 0):  # no Apify (budget): rest unread
            return FetchResult(posts=posts, source=self.name, missing=rest, paid_handles=0)
        a = self.apify.fetch_posts(rest, since, limit, max_usd=max_usd)
        posts.update({h: a.posts.get(h, []) for h in rest})
        return FetchResult(posts=posts, cost_usd=a.cost_usd, source=self.name, paid_handles=len(rest))

    def fetch_mentions(
        self, handles: list[str], since: datetime | None, limit: int = 50, max_usd: float | None = None
    ) -> FetchResult:
        if self.apify is None or (max_usd is not None and max_usd <= 0):
            return FetchResult(posts={}, source=self.name, missing=list(handles), paid_handles=0)
        r = self.apify.fetch_mentions(handles, since, limit, max_usd=max_usd)
        r.source, r.paid_handles = self.name, len(handles)
        return r

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        via_graph = [h for h in handles if self._try_graph(h)]
        found = self.graph.fetch_profiles(via_graph) if via_graph else []
        got = {p.username for p in found}
        rest = [h for h in handles if h.lower() not in got]
        if rest and self.brightdata is not None:
            more = self.brightdata.fetch_profiles(rest)
            found += more
            got |= {p.username for p in more}
            rest = [h for h in rest if h.lower() not in got]
        if self.apify is None:
            self.last_paid_count = 0
            return found
        self.last_paid_count = len(rest)
        return found + (self.apify.fetch_profiles(rest) if rest else [])


# --- helpers for callers ----------------------------------------------------------------------------------


def is_paid(source) -> bool:
    """Does this source cost money? (Fakes in tests may only set name = "apify".)"""
    return bool(getattr(source, "paid", False)) or getattr(source, "name", "") == "apify"


def likely_paid(source, handles: list[str]) -> list[str]:
    """The handles of a fetch that will probably be charged (for budget estimates)."""
    if hasattr(source, "likely_paid"):
        return source.likely_paid(handles)
    return list(handles) if is_paid(source) else []


def paid_profile_count(source, asked: int) -> int:
    """How many profiles of the last fetch_profiles went to Apify."""
    n = getattr(source, "last_paid_count", None)
    return int(n) if n is not None else (asked if is_paid(source) else 0)


def graph_first(settings: Settings) -> bool:
    """Will get_instagram_source pick a source that asks Graph first (hybrid or graph)?"""
    choice = (settings.instagram_source or "auto").strip().lower()
    return settings.features["graph_business_discovery"] and choice in ("auto", "hybrid", "graph")


def _configured(settings: Settings) -> dict[str, bool]:
    f = settings.features
    return {"graph": f["graph_business_discovery"], "brightdata": f["brightdata"], "apify": f["apify"]}


def instagram_configured(settings: Settings) -> bool:
    """Is any real Instagram source (Graph, Bright Data, Apify) configured?"""
    return any(_configured(settings).values())


def apify_used(settings: Settings) -> bool:
    """For cost estimates: may Instagram cost Apify money? False only when the chosen source is free for sure
    (a free source like Graph or Bright Data is configured and Apify is not part of the choice)."""
    choice = (settings.instagram_source or "auto").strip().lower()
    have = _configured(settings)
    if choice == "apify":
        return True
    if choice in ("graph", "brightdata"):
        return not have[choice]
    if choice in ("auto", "hybrid"):
        return have["apify"] or not (have["graph"] or have["brightdata"])
    return True


def get_instagram_source(
    settings: Settings,
) -> (
    FixtureInstagramSource
    | ApifyInstagramSource
    | GraphBusinessDiscoverySource
    | BrightDataInstagramSource
    | HybridInstagramSource
):
    """INSTAGRAM_SOURCE=apify/graph/brightdata: only that one (if configured). auto/hybrid: the hybrid when two
    or more are configured, else the one that is, else the fixtures."""
    choice = (settings.instagram_source or "auto").strip().lower()
    have = _configured(settings)
    if choice == "apify" and have["apify"]:
        return ApifyInstagramSource(settings)
    if choice == "graph" and have["graph"]:
        return GraphBusinessDiscoverySource(settings)
    if choice == "brightdata" and have["brightdata"]:
        return BrightDataInstagramSource(settings)
    if choice in ("auto", "hybrid"):
        if sum(have.values()) >= 2:
            return HybridInstagramSource(settings)
        if have["apify"]:
            return ApifyInstagramSource(settings)
        if have["graph"]:
            return GraphBusinessDiscoverySource(settings)
        if have["brightdata"]:
            return BrightDataInstagramSource(settings)
    return FixtureInstagramSource(Path(settings.fixtures_dir) / "instagram")
