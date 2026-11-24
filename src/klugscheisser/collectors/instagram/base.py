"""Instagram data model shared by all sources (Apify, Graph Business Discovery, fixtures)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from klugscheisser.interfaces import MediaRef

GENERIC_ALT = re.compile(r"^(Photo|Video|Foto) (by|von) .+ (on|am) .+\.?$", re.IGNORECASE)

# fields never persisted: comments are third-party personal data, media URLs are CDN links
DROP_RAW_KEYS = {
    "latestComments",
    "firstComment",
    "images",
    "displayUrl",
    "childPosts",
    "ownerId",
    "videoUrl",
    "audioUrl",
    "musicInfo",
    "dimensionsHeight",
    "dimensionsWidth",
    "originalHeight",
    "originalWidth",
}


@dataclass
class InstagramPost:
    post_id: str
    shortcode: str | None
    url: str  # permalink, never a CDN URL
    owner_username: str
    owner_full_name: str | None
    published_at: datetime | None
    caption: str
    alt: str | None = None
    media_type: str | None = None  # image | video | carousel
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    tagged: list[str] = field(default_factory=list)
    coauthors: list[str] = field(default_factory=list)
    paid_partnership: bool = False
    likes: int | None = None
    comments: int | None = None
    raw: dict = field(default_factory=dict)
    media: list[MediaRef] = field(default_factory=list)  # transient CDN URLs, only for the media queue

    @property
    def informative_alt(self) -> str | None:
        """Instagram's auto alt text, unless it is the generic 'Photo by X on DATE'."""
        if not self.alt or GENERIC_ALT.match(self.alt.strip()):
            return None
        return self.alt.strip()

    def involves(self, handle: str) -> bool:
        h = handle.lower()
        return h == self.owner_username.lower() or h in {
            *map(str.lower, self.tagged),
            *map(str.lower, self.mentions),
            *map(str.lower, self.coauthors),
        }


@dataclass
class InstagramProfile:
    username: str
    full_name: str | None
    biography: str | None
    is_business: bool | None = None
    category: str | None = None
    verified_badge: bool = False
    followers: int | None = None
    posts_count: int | None = None
    private: bool = False
    external_url: str | None = None
    source: str = ""  # "graph": no category / verified badge known (apply_profile keeps the old values)


@dataclass
class FetchResult:
    posts: dict[str, list[InstagramPost]]  # handle -> posts involving that handle
    cost_usd: float = 0.0
    source: str = ""
    missing: list[str] = field(default_factory=list)  # handles this source could not serve (hybrid: to Apify)
    paid_handles: int | None = None  # handles that went to a paid source (None: all of them, if paid)


class InstagramSource(Protocol):
    name: str
    paid: bool  # costs money per result (Apify, or hybrid with its Apify part)

    def fetch_posts(self, handles: list[str], since: datetime | None, limit: int = 12) -> FetchResult: ...

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]: ...


def _parse_dt(value: str | int | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        from datetime import UTC

        return datetime.fromtimestamp(value, UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_MEDIA = {
    "Image": "image",
    "Video": "video",
    "Sidecar": "carousel",
    "GraphImage": "image",
    "GraphVideo": "video",
    "GraphSidecar": "carousel",
}


def parse_apify_post(d: dict) -> InstagramPost:
    url = d.get("url") or f"https://www.instagram.com/p/{d.get('shortCode')}/"
    return InstagramPost(
        post_id=str(d.get("id") or d.get("shortCode")),
        shortcode=d.get("shortCode"),
        url=url,
        owner_username=(d.get("ownerUsername") or "").lower(),
        owner_full_name=d.get("ownerFullName"),
        published_at=_parse_dt(d.get("timestamp")),
        caption=d.get("caption") or "",
        alt=d.get("alt"),
        media_type=_MEDIA.get(d.get("type") or "", (d.get("type") or "").lower() or None),
        hashtags=list(d.get("hashtags") or []),
        mentions=[m.lower() for m in d.get("mentions") or []],
        tagged=[(t.get("username") or "").lower() for t in d.get("taggedUsers") or [] if t.get("username")],
        coauthors=[
            (c.get("username") or "").lower() for c in d.get("coauthorProducers") or [] if c.get("username")
        ],
        paid_partnership=bool(d.get("paidPartnership")),
        likes=d.get("likesCount"),
        comments=d.get("commentsCount"),
        raw={k: v for k, v in d.items() if k not in DROP_RAW_KEYS},
        media=media_refs(d),
    )


MAX_CAROUSEL_IMAGES = 5


def media_refs(d: dict) -> list[MediaRef]:
    """Images (up to 5 of a carousel) and videos with duration, from an Apify post record."""

    def one(x: dict) -> MediaRef | None:
        if x.get("videoUrl"):
            return MediaRef(kind="video", url=x["videoUrl"], duration_s=x.get("videoDuration"))
        if x.get("displayUrl"):
            return MediaRef(kind="image", url=x["displayUrl"])
        return None

    children = [r for c in (d.get("childPosts") or []) if (r := one(c))]
    if children:
        return children[:MAX_CAROUSEL_IMAGES]
    ref = one(d)
    return [ref] if ref else []


def parse_apify_profile(d: dict) -> InstagramProfile:
    return InstagramProfile(
        username=(d.get("username") or "").lower(),
        full_name=d.get("fullName"),
        biography=d.get("biography"),
        is_business=d.get("isBusinessAccount"),
        category=d.get("businessCategoryName"),
        verified_badge=bool(d.get("verified")),
        followers=d.get("followersCount"),
        posts_count=d.get("postsCount"),
        private=bool(d.get("private")),
        external_url=d.get("externalUrl"),
    )


def group_by_handle(posts: list[InstagramPost], handles: list[str]) -> dict[str, list[InstagramPost]]:
    out: dict[str, list[InstagramPost]] = {h: [] for h in handles}
    for post in posts:
        for h in handles:
            if post.involves(h):
                out[h].append(post)
    return out


# --- captions (Graph and apidojo give no hashtag/mention lists) ----------------------------------------

_HASHTAG = re.compile(r"(?<![\w&])#(\w+)")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._]{1,30})")
_SHORTCODE = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def caption_hashtags(caption: str | None) -> list[str]:
    """Hashtags without '#', in order, no duplicates (like Apify's `hashtags`)."""
    return list(dict.fromkeys(_HASHTAG.findall(caption or "")))


def caption_mentions(caption: str | None) -> list[str]:
    """@mentions without '@', lower case, trailing dots dropped (end of a sentence), no duplicates."""
    out = [m.rstrip(".").lower() for m in _MENTION.findall(caption or "")]
    return list(dict.fromkeys(m for m in out if m))


# caption markers of paid posts (Swiss/German law asks for "Werbung"/"Anzeige"; Instagram's own label is
# "Paid partnership" / "Bezahlte Partnerschaft"). Only for sources that do not fill the flag (Bright Data).
_PAID = re.compile(
    r"(?<![\w#])(?:#(?:ad|anzeige|werbung|sponsored)|anzeige|werbung|paid partnership|bezahlte partnerschaft)"
    r"(?!\w)",
    re.IGNORECASE,
)


def caption_paid_partnership(caption: str | None) -> bool:
    """Does the caption mark the post as advertising (#ad, #sponsored, Anzeige, Werbung, paid partnership)?"""
    return bool(_PAID.search(caption or ""))


def shortcode_from_url(url: str | None) -> str | None:
    """The post code of a permalink (/p/CODE/, /reel/CODE/, /tv/CODE/)."""
    match = _SHORTCODE.search(url or "")
    return match.group(1) if match else None


# --- apidojo/instagram-scraper (cheaper actor, other output format) ----------------------------------


def parse_apidojo_post(d: dict) -> InstagramPost:
    """One post of apidojo/instagram-scraper. It has no tagged users, no alt text and no carousel children:
    those stay empty; hashtags and mentions come from the caption."""
    owner = d.get("owner") or {}
    code = d.get("code") or shortcode_from_url(d.get("url"))
    url = d.get("url") or f"https://www.instagram.com/p/{code}/"
    caption = d.get("caption") or ""
    media: list[MediaRef] = []
    video, image = d.get("video") or {}, d.get("image") or {}
    if d.get("isVideo") and video.get("url"):
        media.append(MediaRef(kind="video", url=video["url"], duration_s=video.get("duration")))
    elif image.get("url"):
        media.append(MediaRef(kind="image", url=image["url"]))
    return InstagramPost(
        post_id=str(d.get("id") or code),
        shortcode=code,
        url=url,
        owner_username=(owner.get("username") or "").lower(),
        owner_full_name=owner.get("fullName"),
        published_at=_parse_dt(d.get("createdAt")),
        caption=caption,
        media_type="carousel" if d.get("isCarousel") else "video" if d.get("isVideo") else "image",
        hashtags=caption_hashtags(caption),
        mentions=caption_mentions(caption),
        coauthors=[(c.get("username") or "").lower() for c in d.get("coowners") or [] if c.get("username")],
        paid_partnership=bool(d.get("isPaidPartnership")),
        likes=d.get("likeCount"),
        comments=d.get("commentCount"),
        raw={k: v for k, v in d.items() if k not in DROP_RAW_KEYS | {"video", "image", "owner", "coowners"}}
        | {"owner": owner.get("username"), "coowners": [c.get("username") for c in d.get("coowners") or []]},
        media=media,
    )


def parse_apidojo_profile(owner: dict) -> InstagramProfile:
    """Profile fields that apidojo repeats in every post's `owner` (no bio, no category)."""
    return InstagramProfile(
        username=(owner.get("username") or "").lower(),
        full_name=owner.get("fullName"),
        biography=None,
        verified_badge=bool(owner.get("isVerified")),
        followers=owner.get("followerCount"),
        posts_count=owner.get("postCount"),
        private=bool(owner.get("isPrivate")),
    )
