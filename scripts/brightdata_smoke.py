"""Live smoke test of the Bright Data Instagram source: profile + a few posts for some handles.

    with-secrets brightdata -- uv run python scripts/brightdata_smoke.py lara.stalder7 swissicehockeywomen

Uses BRIGHTDATA_API_TOKEN from the environment (never printed). Costs ~ (handles + handles × posts) free
records (default 2 + 6 = 8 of the 5000 per month). Writes nothing to any database: the record count is kept
in memory only (so a live/preview DB is never touched). Prints which fields came back, so the mapping in
collectors/instagram/brightdata.py can be checked against real output.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from klugscheisser.collectors.instagram import brightdata as bd
from klugscheisser.config import Settings


class MemoryQuota:
    def __init__(self, cap: int) -> None:
        self.cap, self.n = cap, 0

    def used(self) -> int:
        return self.n

    def room(self) -> int:
        return max(0, self.cap - self.n)

    def book(self, records: int, note: str) -> float:
        self.n += records
        print(f"  [records] +{records} ({note}), total {self.n}")
        return 0.0


def _shape(value) -> str:
    """Type/shape of a field without printing CDN links or personal data."""
    if value is None:
        return "null"
    if isinstance(value, list):
        inner = _shape(value[0]) if value else "-"
        return f"list[{len(value)}] of {inner}"
    if isinstance(value, dict):
        return "{" + ",".join(sorted(value)) + "}"
    return type(value).__name__


def _filled(obj, names: list[str]) -> list[str]:
    return [n for n in names if getattr(obj, n) not in (None, "", [], False)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="+", help="Instagram handles (2 suggested)")
    ap.add_argument("--posts", type=int, default=3, help="posts per handle (default 3)")
    ap.add_argument("--max-records", type=int, default=20, help="stop before more records (default 20)")
    args = ap.parse_args()
    if not os.environ.get("BRIGHTDATA_API_TOKEN"):
        print("BRIGHTDATA_API_TOKEN is not set (run with: with-secrets brightdata -- ...)")
        return 2
    settings = Settings(_env_file=None, brightdata_poll_s=5)
    quota = MemoryQuota(args.max_records)
    source = bd.BrightDataInstagramSource(settings, quota=quota)
    handles = [h.lstrip("@").lower() for h in args.handles]
    ok = True

    print(f"Profiles for {handles} …")
    t = time.monotonic()
    try:
        rows = source._collect_booked(
            bd.PROFILES_DATASET,
            [{"url": f"https://www.instagram.com/{h}/"} for h in handles],
            reserved=len(handles),
            note="smoke profiles",
        )
    except bd.BrightDataError as exc:
        print(f"  FAILED: {exc}")
        return 1
    print(f"  {len(rows)} records in {time.monotonic() - t:.0f} s")
    for d in rows:
        if bd._is_error(d):
            print(f"  error record: {d.get('error_code')} {str(d.get('error'))[:120]}")
            ok = False
            continue
        p = bd.parse_brightdata_profile(d)
        print(f"  @{p.username}: followers={p.followers} posts={p.posts_count} verified={p.verified_badge}")
        print(f"    business={p.is_business} category={p.category!r} private={p.private}")
        print(f"    filled: {_filled(p, ['full_name', 'biography', 'category', 'external_url'])}")
        print(f"    raw fields: {sorted(d)}")
        for key in (
            "is_business_account",
            "is_professional_account",
            "category_name",
            "external_url",
            "posts",
        ):
            print(f"    {key}: {_shape(d.get(key))}")

    print(f"\nPosts ({args.posts} per handle) …")
    t = time.monotonic()
    try:
        rows = source._collect_booked(
            bd.POSTS_DATASET,
            [source.post_input(h, None, args.posts) for h in handles],
            reserved=args.posts * len(handles),
            note="smoke posts",
            params={"type": "discover_new", "discover_by": "url"},
            limit=args.posts,
        )
    except bd.BrightDataError as exc:
        print(f"  FAILED: {exc}")
        return 1
    print(f"  {len(rows)} records in {time.monotonic() - t:.0f} s")
    keys: set[str] = set()
    for d in rows:
        if bd._is_error(d):
            print(f"  error record: {d.get('error_code')} {str(d.get('error'))[:120]}")
            ok = False
            continue
        keys |= set(d)
        p = bd.parse_brightdata_post(d)
        filled = _filled(
            p, ["shortcode", "published_at", "caption", "alt", "media_type", "hashtags", "mentions", "tagged"]
        ) + _filled(p, ["coauthors", "paid_partnership", "likes", "comments", "media"])
        print(
            f"  @{p.owner_username} {p.url} {p.published_at:%Y-%m-%d} type={p.media_type} media={len(p.media)}"
        )
        print(f"    filled: {filled}")
    for key in (
        "tagged_users",
        "coauthor_producers",
        "partnership_details",
        "post_content",
        "discovery_input",
    ):
        sample = next((d.get(key) for d in rows if d.get(key)), None)
        print(f"  {key}: {_shape(sample)}")
    print(f"  raw fields seen: {sorted(keys)}")
    missing: list[str] = []
    grouped = source._group(rows, handles, None, args.posts, missing)
    print(f"  per handle: { {h: len(ps) for h, ps in grouped.items()} } missing={missing}")
    print(f"\nRecords used: {quota.n} (of the free 5000 this month)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
