"""Bright Data Web Scraper API as an Instagram source: own posts (dataset "Instagram - Posts", discover by
profile URL) and profiles (dataset "Instagram - Profiles", collect by URL). Free tier: 5000 records per
calendar month; without funds on the account a call over it just fails. Every record is counted in the cost
ledger (source "brightdata", units = records) and no snapshot is started that could pass
BRIGHTDATA_MONTHLY_RECORD_CAP. See docs/instagram-sources.md (what was verified in the Bright Data docs)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, func, select

from klugscheisser.collectors.instagram.base import (
    MAX_CAROUSEL_IMAGES,
    FetchResult,
    InstagramPost,
    InstagramProfile,
    _parse_dt,
    caption_hashtags,
    caption_mentions,
    caption_paid_partnership,
    group_by_handle,
    shortcode_from_url,
)
from klugscheisser.config import Settings
from klugscheisser.interfaces import MediaRef

log = logging.getLogger(__name__)

API = "https://api.brightdata.com/datasets/v3"
POSTS_DATASET = "gd_lk5ns7kz21pck8jpis"  # "Instagram - Posts" (collect by URL, discover by profile URL)
PROFILES_DATASET = "gd_l1vikfch901nx3by4"  # "Instagram - Profiles" (collect by URL)
LEDGER_SOURCE = "brightdata"

# progress `error_message` values (docs: Monitor progress) that mean "nothing found", not "broken"
EMPTY_MESSAGES = ("no data found", "snapshot is empty")
# words in an error that mean the free records are used up / no funds (the exact answer is not documented)
QUOTA_WORDS = ("balance", "funds", "credit", "payment", "quota", "insufficient")
# ... and progress messages that mean no further call can work in this run
ACCOUNT_MESSAGES = ("suspended", "activate", *QUOTA_WORDS)
# record error codes for an input that does not exist (a dead page): Apify cannot read it either
DEAD_CODES = {"dead_page"}
# what a dead_page record's text says (seen 2026-10-28): "This profile is private", "Profile isn't available"
# (renamed or deleted), "There are no public posts in the profile for the specified period" (just empty)
UNAVAILABLE_WORDS = (
    "isn't available",
    "isn’t available",
    "not available",
    "unavailable",
    "not found",
    "does not exist",
    "doesn't exist",
)


class BrightDataError(Exception):
    """One snapshot failed (network, 5xx, timeout, failed job): its handles may go to another source."""


class BrightDataStopped(BrightDataError):
    """No more Bright Data calls in this run (token wrong, free records used up, account blocked, 429)."""


# --- free-record guard -------------------------------------------------------------------------------------


def month_start(now: datetime | None = None) -> datetime:
    """First instant of the current calendar month (UTC; Bright Data renews free credits on the 1st)."""
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BrightDataQuota:
    """Records booked this month in the cost ledger (source "brightdata"). Booked in its own transaction, so
    the count holds even when the caller's transaction is rolled back (Bright Data charged them anyway)."""

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.free = settings.brightdata_free_records_per_month
        self.cap = settings.brightdata_monthly_record_cap
        self.price = settings.brightdata_cost_per_record_usd
        self.engine = engine

    def _session(self):
        from klugscheisser.db.session import session_scope

        return session_scope(self.engine)

    def used(self) -> int:
        from klugscheisser.db import models as m

        with self._session() as s:
            return int(
                s.scalar(
                    select(func.coalesce(func.sum(m.CostLedger.units), 0)).where(
                        m.CostLedger.source == LEDGER_SOURCE, m.CostLedger.created_at >= month_start()
                    )
                )
            )

    def room(self) -> int:
        """Records that may still be requested this month."""
        return max(0, self.cap - self.used())

    def cost(self, used_before: int, records: int) -> float:
        """$ for `records` more records: 0 while inside the free records, the PAYG price beyond."""
        free_left = max(0, self.free - used_before)
        return max(0, records - free_left) * self.price

    def book(self, records: int, note: str) -> float:
        if records <= 0:
            return 0.0
        from klugscheisser.ingest import costs

        usd = self.cost(self.used(), records)
        with self._session() as s:
            costs.record(s, LEDGER_SOURCE, records, usd, note)
        return usd


# --- parsing ------------------------------------------------------------------------------------------------

_CONTENT = {"carousel": "carousel", "image": "image", "photo": "image", "video": "video", "reel": "video"}
# never stored: CDN links (expire after 24 h), comments (third-party personal data), echo of the input
DROP_KEYS = {
    "photos",
    "videos",
    "images",
    "post_content",
    "thumbnail",
    "display_url",
    "profile_image_link",
    "audio_url",
    "audio",
    "latest_comments",
    "top_comments",
    "discovery_input",
    "input",
}


def _username(x) -> str | None:
    """A user from tagged_users / coauthor_producers: a string (name or profile URL) or an object."""
    if isinstance(x, dict):
        x = x.get("username") or x.get("user_name") or x.get("handle") or x.get("profile_url") or x.get("url")
    if not isinstance(x, str) or not x.strip():
        return None
    x = x.strip().rstrip("/")
    if "instagram.com/" in x:
        x = x.split("instagram.com/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return x.lstrip("@").lower() or None


def _users(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(u for u in map(_username, value) if u))


def brightdata_media_refs(d: dict) -> list[MediaRef]:
    """Images/videos of a post: from `post_content` (one entry per carousel item) when it carries URLs, else
    from `videos` (with `videos_duration`) and `photos`. At most MAX_CAROUSEL_IMAGES."""
    refs: list[MediaRef] = []
    for item in d.get("post_content") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        kind = "video" if str(item.get("type") or "").lower() in ("video", "reel") else "image"
        refs.append(MediaRef(kind=kind, url=item["url"]))
    if not refs:
        durations = d.get("videos_duration") or []
        for i, url in enumerate(d.get("videos") or []):
            if isinstance(url, str) and url:
                dur = durations[i] if i < len(durations) and isinstance(durations[i], int | float) else None
                refs.append(MediaRef(kind="video", url=url, duration_s=dur))
        if not refs:
            refs += [MediaRef(kind="image", url=u) for u in d.get("photos") or [] if isinstance(u, str) and u]
    return refs[:MAX_CAROUSEL_IMAGES]


def parse_brightdata_post(d: dict) -> InstagramPost:
    """One record of the posts dataset (output fields as in the Bright Data docs, Oct 2026)."""
    url = d.get("url") or ""
    code = d.get("shortcode") or d.get("content_id") or shortcode_from_url(url)
    if not url and code:
        url = f"https://www.instagram.com/p/{code}/"
    caption = d.get("description") or ""
    tags = [str(t).lstrip("#") for t in d.get("hashtags") or [] if str(t).lstrip("#")]
    alt = d.get("alt_text") or next(
        (c.get("alt_text") for c in d.get("post_content") or [] if isinstance(c, dict) and c.get("alt_text")),
        None,
    )
    partner = d.get("partnership_details") or {}
    return InstagramPost(
        post_id=str(d.get("post_id") or d.get("pk") or code),
        shortcode=code,
        url=url,
        owner_username=(d.get("user_posted") or "").lower(),
        owner_full_name=None,  # the posts dataset has no display name of the owner
        published_at=_parse_dt(d.get("date_posted")),
        caption=caption,
        alt=alt,
        media_type=_CONTENT.get(str(d.get("content_type") or "").lower()),
        hashtags=list(dict.fromkeys(tags)) or caption_hashtags(caption),
        mentions=caption_mentions(caption),
        tagged=_users(d.get("tagged_users")),
        coauthors=_users(d.get("coauthor_producers")),
        paid_partnership=bool(d.get("is_paid_partnership") or partner.get("username"))
        or caption_paid_partnership(caption),  # Bright Data never filled the flag in the 2026-10-28 test
        likes=d.get("likes"),
        comments=d.get("num_comments"),
        raw={k: v for k, v in d.items() if k not in DROP_KEYS} | {"via": "brightdata"},
        media=brightdata_media_refs(d),
    )


def _first_url(value) -> str | None:
    """external_url is a string in the examples but an array in the schema."""
    if isinstance(value, str):
        return value or None
    for x in value or []:
        if isinstance(x, str) and x:
            return x
        if isinstance(x, dict) and (u := x.get("url") or x.get("link")):
            return u
    return None


def parse_brightdata_profile(d: dict) -> InstagramProfile:
    """One record of the profiles dataset."""
    username = d.get("account") or _username(d.get("url") or d.get("profile_url")) or ""
    business = d.get("is_business_account")
    if business is None:
        business = d.get("is_professional_account")
    return InstagramProfile(
        username=username.lower(),
        full_name=d.get("full_name") or d.get("profile_name"),
        biography=d.get("biography"),
        is_business=business if isinstance(business, bool) else None,
        category=d.get("category_name") or d.get("business_category_name"),
        verified_badge=bool(d.get("is_verified")),
        followers=d.get("followers"),
        posts_count=d.get("posts_count"),
        private=bool(d.get("is_private")),
        external_url=_first_url(d.get("external_url")) or _first_url(d.get("external_urls")),
        source="brightdata",
    )


def _handle_of(url: str | None) -> str | None:
    return _username(url) if url else None


def _is_error(d: dict) -> bool:
    return bool(d.get("error") or d.get("error_code")) and not (d.get("post_id") or d.get("account"))


def dead_page_state(d: dict) -> str | None:
    """For a dead_page error record: "private", "unavailable" (renamed/deleted: check the handle) or "empty"
    (no public posts in the period). None for other records."""
    if d.get("error_code") not in DEAD_CODES:
        return None
    msg = str(d.get("error") or d.get("error_message") or "").lower()
    if "private" in msg:
        return "private"
    if any(w in msg for w in UNAVAILABLE_WORDS):
        return "unavailable"
    return "empty"


# --- the source ------------------------------------------------------------------------------------------


class BrightDataInstagramSource:
    """Bright Data datasets via the async API: trigger → poll progress → download snapshot. Own posts and
    profiles only (no tagged-posts dataset). Handles it cannot serve (monthly cap, errors, stopped) end up in
    `FetchResult.missing`; `stopped` says why no more calls are made in this run."""

    name = "brightdata"
    paid = False  # inside the free records; the record cap keeps it there (own ledger source, not "apify")

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        quota: BrightDataQuota | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = settings.brightdata_api_token.get_secret_value()
        self.client = client or httpx.Client(timeout=60)
        self.quota = quota or BrightDataQuota(settings)
        self.timeout_s = settings.brightdata_timeout_s
        self.poll_s = settings.brightdata_poll_s
        self.batch_size = max(1, settings.brightdata_batch_size)
        self.history_max = max(1, settings.brightdata_history_max_posts)
        self.sleep = sleep
        self.stopped: str | None = None
        self.last_paid_count = 0  # nothing of it goes to Apify
        self.records = 0  # booked in this source's lifetime
        # handle -> "private" | "unavailable" from dead_page records (the ingest sets profile.private or flags
        # the handle for review); "empty" (no posts in the period) is not kept
        self.page_states: dict[str, str] = {}

    # --- HTTP ---------------------------------------------------------------------------------------------

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _stop(self, reason: str) -> BrightDataStopped:
        self.stopped = reason
        log.error("Bright Data stopped for this run: %s", reason)
        return BrightDataStopped(reason)

    def _check(self, r: httpx.Response, what: str) -> None:
        if r.status_code < 400:
            return
        body = r.text[:300]
        low = body.lower()
        if r.status_code == 401:
            raise self._stop("Bright-Data-Token ungültig (401): BRIGHTDATA_API_TOKEN prüfen")
        if r.status_code == 429:  # 25 × 429 in 5 min blocks the IP: no retry in this run
            raise self._stop("Bright Data: zu viele laufende Jobs (429)")
        if r.status_code in (402, 403) or any(k in low for k in QUOTA_WORDS):
            raise self._stop(
                f"Bright Data: Gratis-Records aufgebraucht oder Konto gesperrt (HTTP {r.status_code}): {body}"
            )
        raise BrightDataError(f"{what}: HTTP {r.status_code} {body}")

    def _get(self, url: str, **kw) -> httpx.Response:
        try:
            return self.client.get(url, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise BrightDataError(f"{url.rsplit('/', 2)[-2]}: {type(exc).__name__}") from exc

    def collect(self, dataset: str, inputs: list[dict], params: dict | None = None, limit: int | None = None):
        """Trigger one snapshot and return its records (data and error records)."""
        if self.stopped:
            raise BrightDataStopped(self.stopped)
        query = {"dataset_id": dataset, "include_errors": "true", "format": "json", **(params or {})}
        body: dict = {"input": inputs}
        if limit is not None:
            body["limit_per_input"] = limit
        try:
            r = self.client.post(f"{API}/trigger", params=query, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise BrightDataError(f"trigger: {type(exc).__name__}") from exc
        self._check(r, "trigger")
        snapshot = (r.json() or {}).get("snapshot_id")
        if not snapshot:
            raise BrightDataError(f"trigger: no snapshot_id in {r.text[:200]}")
        deadline = time.monotonic() + self.timeout_s
        while True:
            p = self._get(f"{API}/progress/{snapshot}")
            self._check(p, "progress")
            info = p.json() or {}
            status = str(info.get("status") or "").lower()
            if status == "ready":
                break
            if status in ("failed", "canceled"):
                msg = str(info.get("error_message") or status)
                if any(k in msg.lower() for k in EMPTY_MESSAGES):
                    return []
                if any(k in msg.lower() for k in ACCOUNT_MESSAGES):
                    raise self._stop(f"Bright Data: {msg}")
                raise BrightDataError(f"snapshot {snapshot} {status}: {msg}")
            if time.monotonic() >= deadline:
                raise TimeoutError(snapshot)
            self.sleep(self.poll_s)
        while True:
            d = self._get(f"{API}/snapshot/{snapshot}", params={"format": "json"})
            if d.status_code == 202:  # "Snapshot is building, try again in 10s"
                if time.monotonic() >= deadline:
                    raise TimeoutError(snapshot)
                self.sleep(self.poll_s)
                continue
            if d.status_code == 400 and "empty" in d.text.lower():
                return []
            self._check(d, "download")
            return _records(d)

    def _collect_booked(self, dataset: str, inputs: list[dict], reserved: int, note: str, **kw) -> list[dict]:
        """collect() and book what it delivered in the ledger. A snapshot that timed out may still be charged
        later: then the reserved maximum is booked (the cap stays on the safe side)."""
        try:
            rows = self.collect(dataset, inputs, **kw)
        except TimeoutError as exc:
            self.records += reserved
            self.quota.book(reserved, f"{note} (Timeout, geschätzt)")
            raise BrightDataError(f"snapshot {exc} nicht fertig nach {self.timeout_s} s") from exc
        self.records += len(rows)  # error records too: the docs disagree on whether they are billed
        self.quota.book(len(rows), note)
        return rows

    # --- posts and profiles -------------------------------------------------------------------------------

    @staticmethod
    def post_input(handle: str, since: datetime | None, limit: int) -> dict:
        inp: dict = {"url": f"https://www.instagram.com/{handle}/", "num_of_posts": limit}
        if since:  # MM-DD-YYYY; the end date is tomorrow so today's posts are in
            inp["start_date"] = since.strftime("%m-%d-%Y")
            inp["end_date"] = (datetime.now(UTC) + timedelta(days=1)).strftime("%m-%d-%Y")
        return inp

    def _fit(self, handles: list[str], per_handle: int, what: str) -> tuple[list[str], list[str]]:
        """Split into handles that fit in this month's record cap and the rest."""
        if self.stopped:
            return [], list(handles)
        n = self.quota.room() // max(1, per_handle)
        if n < len(handles):
            log.warning(
                "Bright Data: Monatslimite %s Records erreicht, %s von %s %s nicht via Bright Data",
                self.quota.cap,
                len(handles) - n,
                len(handles),
                what,
            )
        return list(handles[:n]), list(handles[n:])

    def fetch_posts(self, handles: list[str], since: datetime | None, limit: int = 12) -> FetchResult:
        """Own posts (incl. collaboration posts) newer than `since`, up to `limit` per handle. One snapshot per
        batch of handles; at most `limit` records per handle are reserved against the monthly cap. `limit` is
        capped at BRIGHTDATA_HISTORY_MAX_POSTS (history loads ask for up to 400)."""
        limit = min(limit, self.history_max)
        fit, missing = self._fit(handles, limit, "Handles")
        out: dict[str, list[InstagramPost]] = {}
        for start in range(0, len(fit), self.batch_size):
            batch = fit[start : start + self.batch_size]
            try:
                rows = self._collect_booked(
                    POSTS_DATASET,
                    [self.post_input(h, since, limit) for h in batch],
                    reserved=limit * len(batch),
                    note=f"posts for {len(batch)} handles",
                    params={"type": "discover_new", "discover_by": "url"},
                    limit=limit,
                )
            except BrightDataStopped:
                missing += fit[start:]
                break
            except BrightDataError as exc:
                log.warning("Bright Data posts failed for %s: %s", batch, exc)
                missing += batch
                continue
            out.update(self._group(rows, batch, since, limit, missing))
        return FetchResult(posts=out, source=self.name, missing=missing, paid_handles=0)

    def _group(
        self, rows: list[dict], batch: list[str], since: datetime | None, limit: int, missing: list[str]
    ) -> dict[str, list[InstagramPost]]:
        failed: set[str] = set()
        posts: list[InstagramPost] = []
        origin: dict[str, str] = {}  # post url -> handle whose input found it
        for d in rows:
            src = _handle_of(((d.get("discovery_input") or d.get("input") or {}) or {}).get("url"))
            if _is_error(d):
                if src and d.get("error_code") not in DEAD_CODES:
                    failed.add(src)
                self._note_dead(src, d)
                continue
            if not (d.get("url") or d.get("shortcode")):
                continue
            post = parse_brightdata_post(d)
            if since and post.published_at and post.published_at <= since:
                continue
            posts.append(post)
            if src:
                origin[post.url] = src
        grouped = group_by_handle(posts, batch)
        lower = {h.lower(): h for h in batch}
        for post in posts:  # a post found via a handle's profile belongs to it even if no field names it
            h = lower.get(origin.get(post.url, ""))
            if h and post not in grouped[h]:
                grouped[h].append(post)
        for h in batch:
            if h.lower() in failed and not grouped[h]:
                missing.append(h)
                del grouped[h]
            else:
                grouped[h].sort(
                    key=lambda p: p.published_at or datetime.min.replace(tzinfo=UTC), reverse=True
                )
                grouped[h] = grouped[h][:limit]
        return grouped

    def fetch_mentions(self, handles: list[str], since: datetime | None, limit: int = 50) -> FetchResult:
        """Bright Data has no tagged-posts dataset: nothing is fetched, every handle is reported missing."""
        return FetchResult(posts={}, source=self.name, missing=list(handles), paid_handles=0)

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        fit, _ = self._fit(handles, 1, "Profile")
        profiles: list[InstagramProfile] = []
        for start in range(0, len(fit), self.batch_size):
            batch = fit[start : start + self.batch_size]
            try:
                rows = self._collect_booked(
                    PROFILES_DATASET,
                    [{"url": f"https://www.instagram.com/{h}/"} for h in batch],
                    reserved=len(batch),
                    note=f"profiles for {len(batch)} handles",
                )
            except BrightDataStopped:
                break
            except BrightDataError as exc:
                log.warning("Bright Data profiles failed for %s: %s", batch, exc)
                continue
            for d in rows:
                if _is_error(d):
                    self._note_dead(
                        _handle_of(((d.get("input") or d.get("discovery_input")) or {}).get("url")), d
                    )
                elif (p := parse_brightdata_profile(d)).username:
                    profiles.append(p)
        return profiles

    def _note_dead(self, handle: str | None, d: dict) -> None:
        state = dead_page_state(d)
        if handle and state in ("private", "unavailable"):
            self.page_states[handle.lower()] = state
            log.info("Bright Data: @%s %s (%s)", handle, state, str(d.get("error") or "")[:80])


def _records(r: httpx.Response) -> list[dict]:
    """JSON array (format=json); NDJSON as a fallback (the API's default format)."""
    try:
        data = r.json()
    except ValueError:
        data = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    if isinstance(data, dict):
        data = [data]
    return [d for d in data or [] if isinstance(d, dict)]
