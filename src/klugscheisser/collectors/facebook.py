"""v8: posts of a person's own public Facebook page (tier 1 only), text only, via apify/facebook-posts-scraper
($0.005 per post + $0.001 per run, Oct 2026). Images and videos are not analysed (decided in plan v8)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from klugscheisser.config import Settings

log = logging.getLogger(__name__)
ACTOR = "apify/facebook-posts-scraper"
PRICE_PER_POST = 0.005
PRICE_PER_RUN = 0.001


@dataclass
class FacebookPost:
    url: str
    text: str
    published_at: datetime | None
    page_name: str | None
    author: str | None


def page_url(handle: str) -> str:
    return f"https://www.facebook.com/{handle.strip('/')}/"


def parse_post(d: dict) -> FacebookPost | None:
    url = d.get("url") or d.get("postUrl") or d.get("link")
    text = (d.get("text") or d.get("message") or d.get("postText") or "").strip()
    if not url or not text:
        return None
    when = None
    if d.get("timestamp"):
        when = datetime.fromtimestamp(int(d["timestamp"]), tz=UTC)
    elif d.get("time"):
        try:
            when = datetime.fromisoformat(str(d["time"]).replace("Z", "+00:00"))
        except ValueError:
            when = None
    user = d.get("user") or {}
    return FacebookPost(
        url=url, text=text, published_at=when, page_name=d.get("pageName"), author=user.get("name")
    )


class ApifyFacebookSource:
    name = "apify"

    def __init__(self, settings: Settings) -> None:
        from apify_client import ApifyClient

        self.client = ApifyClient(settings.apify_token.get_secret_value())

    def fetch_posts(
        self, handle: str, limit: int = 30, max_usd: float = 0.5
    ) -> tuple[list[FacebookPost], float]:
        run = self.client.actor(ACTOR).call(
            run_input={"startUrls": [{"url": page_url(handle)}], "resultsLimit": limit},
            run_timeout=timedelta(minutes=10),
            max_total_charge_usd=Decimal(str(max_usd)),
            logger=None,
        )
        if run is None:
            return [], 0.0
        r = run.model_dump() if hasattr(run, "model_dump") else dict(run)
        dataset = r.get("default_dataset_id") or r.get("defaultDatasetId")
        items = list(self.client.dataset(dataset).iterate_items())
        posts = [p for p in (parse_post(d) for d in items) if p is not None]
        return posts, PRICE_PER_RUN + len(items) * PRICE_PER_POST
