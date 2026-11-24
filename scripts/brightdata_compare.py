"""Bright Data vs Apify, step 1: fetch own posts + profiles for a list of handles via Bright Data and keep the raw
records for the comparison (step 2: scripts/brightdata_compare_report.py compares them with the live DB, read-only;
results: docs/brightdata-vs-apify-2026-10-28.md).

    set -a; . ~/.config/brightdata-pending.env; set +a
    uv run python scripts/brightdata_compare.py --out /tmp/bdcompare --since 2026-09-15 --limit 15 h1 h2 ...

Uses BRIGHTDATA_API_TOKEN from the environment (never printed). Writes nothing to any database. A hard record
budget (--budget, counted across runs in <out>/records.json, booked with the reserved maximum before each
snapshot) stops before it could be passed. Raw records are written to <out> (outside git: they hold CDN links
and third-party personal data).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from klugscheisser.collectors.instagram import brightdata as bd
from klugscheisser.config import Settings


class FileQuota:
    """Records used by this test, persisted so several runs share one budget."""

    def __init__(self, path: Path, budget: int) -> None:
        self.path, self.cap = path, budget
        self.n = json.loads(path.read_text())["records"] if path.exists() else 0

    def used(self) -> int:
        return self.n

    def room(self) -> int:
        return max(0, self.cap - self.n)

    def book(self, records: int, note: str) -> float:
        self.n += records
        self.path.write_text(json.dumps({"records": self.n}))
        print(f"  [records] +{records} ({note}), total {self.n} of budget {self.cap}")
        return 0.0


def progress_meta(source: bd.BrightDataInstagramSource, snapshot: str) -> dict:
    """Snapshot metadata without secrets (records/errors as Bright Data reports them)."""
    try:
        r = source.client.get(f"{bd.API}/progress/{snapshot}", headers=source._headers)
        return r.json() if r.status_code < 400 else {"http": r.status_code}
    except httpx.HTTPError as exc:
        return {"error": type(exc).__name__}


def run(source, dataset, inputs, reserve, quota, params=None, limit=None) -> tuple[list[dict], dict]:
    if reserve > quota.room():
        raise SystemExit(f"budget: {reserve} records reserved but only {quota.room()} left; stopping")
    t = time.monotonic()
    snap: dict = {}
    orig_post = source.client.post

    def post(*a, **kw):  # remember the snapshot id for the metadata check
        r = orig_post(*a, **kw)
        with contextlib.suppress(ValueError):
            snap["id"] = r.json().get("snapshot_id")
        return r

    source.client.post = post
    try:
        rows = source.collect(dataset, inputs, params=params, limit=limit)
    finally:
        source.client.post = orig_post
    secs = time.monotonic() - t
    quota.book(len(rows), f"{len(inputs)} inputs")
    meta = progress_meta(source, snap["id"]) if snap.get("id") else {}
    return rows, {"snapshot_id": snap.get("id"), "seconds": round(secs), "rows": len(rows), "progress": meta}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="+")
    ap.add_argument("--out", default="/tmp/bdcompare")
    ap.add_argument("--since", default="2026-09-15")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--batch", type=int, default=13, help="handles per posts snapshot")
    ap.add_argument("--budget", type=int, default=600)
    ap.add_argument("--skip-profiles", action="store_true")
    ap.add_argument("--skip-posts", action="store_true")
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()
    if not os.environ.get("BRIGHTDATA_API_TOKEN"):
        print("BRIGHTDATA_API_TOKEN is not set")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    quota = FileQuota(out / "records.json", args.budget)
    settings = Settings(_env_file=None, brightdata_poll_s=5, brightdata_timeout_s=900)
    source = bd.BrightDataInstagramSource(settings, quota=quota)
    handles = [h.lstrip("@").lower() for h in args.handles]
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    log: list[dict] = []

    if not args.skip_profiles:
        print(f"profiles: {len(handles)} handles")
        rows, meta = run(
            source,
            bd.PROFILES_DATASET,
            [{"url": f"https://www.instagram.com/{h}/"} for h in handles],
            len(handles),
            quota,
        )
        (out / f"profiles-{args.tag}.json").write_text(json.dumps(rows))
        log.append({"kind": "profiles", "handles": handles, **meta})
        print(f"  {meta['rows']} rows in {meta['seconds']} s; progress: {meta['progress']}")

    if not args.skip_posts:
        for i in range(0, len(handles), args.batch):
            batch = handles[i : i + args.batch]
            print(f"posts: batch {i // args.batch + 1}: {len(batch)} handles")
            rows, meta = run(
                source,
                bd.POSTS_DATASET,
                [source.post_input(h, since, args.limit) for h in batch],
                args.limit * len(batch),
                quota,
                params={"type": "discover_new", "discover_by": "url"},
                limit=args.limit,
            )
            (out / f"posts-{args.tag}-{i // args.batch + 1}.json").write_text(json.dumps(rows))
            log.append({"kind": "posts", "handles": batch, **meta})
            print(f"  {meta['rows']} rows in {meta['seconds']} s; progress: {meta['progress']}")

    with (out / "log.jsonl").open("a") as f:
        for entry in log:
            f.write(json.dumps({"tag": args.tag, "at": datetime.now(UTC).isoformat(), **entry}) + "\n")
    print(f"records used by this test so far: {quota.n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
