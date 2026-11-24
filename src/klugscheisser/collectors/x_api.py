"""Official X API v2 (pay-per-use since Feb 2026): user lookup and user timeline.

Costs (docs.x.com, 2026-10): $0.010 per user read, $0.005 per post read; billed from prepaid credits.
We read only what we store: one lookup per handle (cached in the handle profile), then the timeline with
`since_id` so only new posts are paid for. Reposts are skipped. X display rules: keep @handle, unmodified text,
date and permalink together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from klugscheisser.config import Settings

BASE = "https://api.x.com/2"


@dataclass
class XUser:
    id: str
    username: str
    name: str | None = None
    description: str | None = None
    verified: bool = False
    url: str | None = None
    followers: int | None = None


@dataclass
class XPost:
    id: str
    author: str  # username without @
    text: str
    created_at: datetime | None
    lang: str | None = None
    quoted_url: str | None = None
    mentions: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.id}"


class XApiError(Exception):
    pass


class XApiClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        token = settings.x_bearer_token.get_secret_value() if settings.x_bearer_token else ""
        self.http = client or httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"})
        self.posts_read = 0
        self.users_read = 0

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.http.get(f"{BASE}{path}", params=params)
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise XApiError(f"X API {r.status_code}: {r.text[:200]}")
        return r.json()

    def user(self, username: str) -> XUser | None:
        data = self._get(
            f"/users/by/username/{username}",
            {"user.fields": "description,verified,url,public_metrics,name"},
        ).get("data")
        if not data:
            return None
        self.users_read += 1
        return XUser(
            id=data["id"],
            username=data["username"],
            name=data.get("name"),
            description=data.get("description"),
            verified=bool(data.get("verified")),
            url=data.get("url"),
            followers=(data.get("public_metrics") or {}).get("followers_count"),
        )

    def posts(self, user: XUser, since_id: str | None = None, max_results: int = 20) -> list[XPost]:
        params = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "created_at,lang,referenced_tweets,entities",
            "exclude": "retweets",
        }
        if since_id:
            params["since_id"] = since_id
        data = self._get(f"/users/{user.id}/tweets", params)
        out = []
        for t in data.get("data") or []:
            self.posts_read += 1
            quoted = next(
                (r["id"] for r in t.get("referenced_tweets") or [] if r.get("type") == "quoted"), None
            )
            mentions = [m["username"] for m in (t.get("entities") or {}).get("mentions") or []]
            out.append(
                XPost(
                    id=t["id"],
                    author=user.username,
                    text=t.get("text", ""),
                    created_at=datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                    if t.get("created_at")
                    else None,
                    lang=t.get("lang"),
                    quoted_url=f"https://x.com/i/status/{quoted}" if quoted else None,
                    mentions=mentions,
                )
            )
        return out

    def search_recent(self, query: str, max_results: int = 10) -> list[XPost]:
        """v8: posts of the last 7 days matching `query` (e.g. '"Lara Stalder" -is:retweet'), any author."""
        data = self._get(
            "/tweets/search/recent",
            {
                "query": query,
                "max_results": max(10, min(max_results, 100)),
                "tweet.fields": "created_at,lang,author_id,entities",
                "expansions": "author_id",
                "user.fields": "username",
            },
        )
        users = {u["id"]: u["username"] for u in (data.get("includes") or {}).get("users") or []}
        out = []
        for t in data.get("data") or []:
            self.posts_read += 1
            out.append(
                XPost(
                    id=t["id"],
                    author=users.get(t.get("author_id"), "i"),
                    text=t.get("text", ""),
                    created_at=datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                    if t.get("created_at")
                    else None,
                    lang=t.get("lang"),
                    mentions=[m["username"] for m in (t.get("entities") or {}).get("mentions") or []],
                )
            )
        return out
