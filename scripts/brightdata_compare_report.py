"""Bright Data vs Apify, step 2: compare the raw Bright Data records of scripts/brightdata_compare.py with what Apify
stored in a Karl database for the same handles and window. Read-only (the connection is opened with
default_transaction_read_only=on); prints a plain-text report.

    uv run python scripts/brightdata_compare_report.py --out /tmp/bdcompare --db klugscheisser --since 2026-09-15
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from klugscheisser.collectors.instagram import brightdata as bd
from klugscheisser.collectors.instagram.base import parse_apify_post


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def handle_of(d: dict) -> str | None:
    return bd._handle_of(((d.get("discovery_input") or d.get("input") or {}) or {}).get("url"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/bdcompare")
    ap.add_argument("--db", default="klugscheisser")
    ap.add_argument("--since", default="2026-09-15")
    ap.add_argument("--tag", default="p1")
    args = ap.parse_args()
    out = Path(args.out)
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)

    rows = [
        d
        for f in sorted(glob.glob(str(out / f"posts-{args.tag}-*.json")))
        for d in json.loads(Path(f).read_text())
    ]
    profiles = json.loads((out / f"profiles-{args.tag}.json").read_text())
    handles: list[str] = []
    for e in map(json.loads, (out / "log.jsonl").read_text().splitlines()):
        if e["kind"] == "posts" and e["tag"] == args.tag:
            handles += e["handles"]

    bd_posts: dict[str, dict[str, tuple]] = collections.defaultdict(
        dict
    )  # handle -> shortcode -> (post, raw)
    errors: dict[str, str] = {}
    outside = 0
    for d in rows:
        h = handle_of(d)
        if bd._is_error(d):
            errors[h] = f"{d.get('error_code')}: {d.get('error')}"
            continue
        p = bd.parse_brightdata_post(d)
        if p.published_at and p.published_at < since:
            outside += 1
        bd_posts[h][p.shortcode] = (p, d)

    conn = psycopg.connect(f"dbname={args.db}", options="-c default_transaction_read_only=on")
    cur = conn.cursor()
    cur.execute(
        "select distinct on (handle) handle, last_scraped_at, profile from handles where platform='instagram' "
        "and handle = any(%s) order by handle, updated_at desc",
        (handles,),
    )
    hrow = {h: (ls, prof or {}) for h, ls, prof in cur.fetchall()}
    cur.execute(
        "select url, raw, published_at, source_type, source_kind, scraped_at, image_description from items "
        "where platform='instagram' and raw is not null and published_at >= %s",
        (since,),
    )
    db_items = cur.fetchall()
    by_code: dict[str, tuple] = {}
    for url, raw, pub, st, kind, scraped, imgdesc in db_items:
        code = raw.get("shortCode") or bd.shortcode_from_url(url)
        by_code[code] = (url, raw, pub, st, kind, scraped, imgdesc)

    def apify_set(h: str) -> dict[str, tuple]:
        """Posts Apify stored that belong to h's own feed: owner h, or h a coauthor (collab)."""
        res = {}
        for code, t in by_code.items():
            raw = t[1]
            owner = (raw.get("ownerUsername") or "").lower()
            coauth = {(c.get("username") or "").lower() for c in raw.get("coauthorProducers") or []}
            if owner == h or h in coauth:
                res[code] = t
        return res

    tot = collections.Counter()
    field = collections.Counter()
    notes: list[str] = []
    print(f"window: published >= {since.date()}; BD records outside window: {outside}\n")
    print(f"{'handle':22} {'last apify':11} both apify bd | BD-only reasons | Apify-only reasons | BD error")
    for h in handles:
        ls, _prof = hrow.get(h, (None, {}))
        a = apify_set(h)
        b = bd_posts.get(h, {})
        both = set(a) & set(b)
        a_only = set(a) - set(b)
        b_only = set(b) - set(a)
        reasons_b = collections.Counter()
        for code in b_only:
            p, d = b[code]
            if code in by_code:
                reasons_b["in DB via other route"] += 1
            elif ls and p.published_at and p.published_at > ls:
                reasons_b["newer than last Apify scrape"] += 1
            elif p.owner_username != h:
                reasons_b["collab (other owner)"] += 1
            else:
                reasons_b[f"own {d.get('content_type')}/{d.get('product_type')}"] += 1
        reasons_a = collections.Counter()
        for code in a_only:
            raw = a[code][1]
            if len(b) >= 15:
                reasons_a["BD limit 15 reached"] += 1
            elif h in errors:
                reasons_a["BD error"] += 1
            elif raw.get("isPinned"):
                reasons_a["pinned"] += 1
            else:
                reasons_a[
                    f"{raw.get('type')}/{raw.get('productType')} owner={'self' if (raw.get('ownerUsername') or '').lower() == h else 'other'}"
                ] += 1
        tot["both"] += len(both)
        tot["apify_only"] += len(a_only)
        tot["bd_only"] += len(b_only)
        for k, v in reasons_b.items():
            tot[f"bd_only: {k}"] += v
        for k, v in reasons_a.items():
            tot[f"apify_only: {k}"] += v
        print(
            f"{h:22} {ls.strftime('%m-%d %H:%M') if ls else '-':11} {len(both):4} {len(a_only):5} {len(b_only):2} | "
            f"{dict(reasons_b) or ''} | {dict(reasons_a) or ''} | {errors.get(h, '')[:70]}"
        )
        # field quality on matched posts
        for code in both:
            p, d = b[code]
            raw = a[code][1]
            ap_ = parse_apify_post(raw)
            field["matched"] += 1
            field["caption equal"] += norm(p.caption) == norm(ap_.caption)
            if norm(p.caption) != norm(ap_.caption):
                notes.append(f"caption differs {code}: BD {len(p.caption)} chars vs Apify {len(ap_.caption)}")
            if p.published_at and ap_.published_at:
                field["date equal (±60 s)"] += abs((p.published_at - ap_.published_at).total_seconds()) <= 60
            field["tagged equal"] += set(p.tagged) == set(ap_.tagged)
            field["tagged BD ⊇ Apify"] += set(p.tagged) >= set(ap_.tagged)
            field["tagged Apify>0"] += bool(ap_.tagged)
            field["tagged BD>0"] += bool(p.tagged)
            field["coauthors equal"] += set(p.coauthors) == set(ap_.coauthors)
            field["coauthors Apify>0"] += bool(ap_.coauthors)
            field["coauthors BD>0"] += bool(p.coauthors)
            field["alt Apify (informative)"] += bool(ap_.informative_alt)
            field["alt BD (informative)"] += bool(p.informative_alt)
            field["alt BD any"] += bool(p.alt)
            atype = (raw.get("type") or "").lower()
            amap = {"sidecar": "carousel", "image": "image", "video": "video"}.get(atype, atype)
            field["media type equal"] += amap == p.media_type
            if amap != p.media_type:
                notes.append(
                    f"media type {code}: Apify {atype}/{raw.get('productType')} vs BD {d.get('content_type')}/{d.get('product_type')}"
                )
            field["BD media refs>0"] += bool(p.media)
            n_an = len((raw.get("media") or {}).get("analyzed") or [])
            if n_an:
                field["carousel/media count comparable"] += 1
                field["BD media count == Apify analyzed"] += min(len(p.media), 5) == n_an
            if p.likes is not None and ap_.likes is not None and ap_.likes > 0:
                field["likes within 10%"] += abs(p.likes - ap_.likes) <= 0.1 * ap_.likes + 3
            if p.likes is None:
                field["likes missing in BD"] += 1
            if p.comments is not None and ap_.comments is not None:
                field["comments within 10%"] += abs(p.comments - ap_.comments) <= 0.1 * ap_.comments + 3
            field["paid partnership Apify"] += bool(ap_.paid_partnership)
            field["paid partnership BD"] += bool(p.paid_partnership)
            field["paid partnership equal"] += bool(ap_.paid_partnership) == bool(p.paid_partnership)
    print("\nTotals:", dict(tot))
    print("\nField quality on matched posts:")
    for k, v in field.items():
        print(f"  {k}: {v}")
    print("\nNotes:")
    for n in notes[:40]:
        print("  " + n)

    print("\nProfiles (BD vs stored):")
    for d in profiles:
        if bd._is_error(d):
            h = handle_of(d) or bd._handle_of((d.get("input") or {}).get("url"))
            print(
                f"  {h}: ERROR {d.get('error_code')}: {d.get('error')}; stored: {hrow.get(h, (None, {}))[1]}"
            )
            continue
        p = bd.parse_brightdata_profile(d)
        s = hrow.get(p.username, (None, {}))[1]
        fs = s.get("followers")
        diff = f"{(p.followers - fs) / fs:+.1%}" if fs and p.followers is not None else "-"
        print(
            f"  {p.username:20} followers BD {p.followers} vs {fs} ({diff}); posts {p.posts_count} vs "
            f"{s.get('posts_count')}; bio equal {norm(p.biography) == norm(s.get('biography'))}; private "
            f"{p.private}/{s.get('private')}; category BD {p.category!r} vs {s.get('category')!r}; business BD "
            f"{d.get('is_business_account')!r}; verified {p.verified_badge}/{s.get('verified_badge')}; "
            f"ext_url {bool(p.external_url)}/{bool(s.get('external_url'))}; posts in profile {len(d.get('posts') or [])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
