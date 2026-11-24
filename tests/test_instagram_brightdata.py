"""Bright Data as an Instagram source: mapping, trigger → poll → download, the monthly free-record guard, the
hybrid order Graph → Bright Data → Apify and the hybrid without Apify. Offline (httpx.MockTransport)."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_instagram_free_sources import (  # noqa: F401  (fixtures)
    GRAPH_SETTINGS,
    NAMES,
    FakeApify,
    FakeGraph,
    fresh_graph_budget,
    graph_source,
    media,
    run_ingest,
    set_profiles,
    world,
)

from klugscheisser.collectors.instagram import brightdata as bd
from klugscheisser.collectors.instagram import sources as src
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.ingest import costs

BD_SETTINGS = Settings(
    _env_file=None, brightdata_api_token="bd-secret", brightdata_poll_s=0, brightdata_timeout_s=5
)


def bd_post(handle: str, i: int, day: int = 20, **extra) -> dict:
    """A record of the posts dataset, shaped like the docs' example."""
    return {
        "url": f"https://www.instagram.com/p/BD{handle.replace('.', '')}{i}",
        "user_posted": handle,
        "description": f"Sieg #{i}! mit @Lara.Stalder7 #hockey",
        "hashtags": ["#hockey", "#Sieg"],
        "num_comments": 3,
        "date_posted": f"2026-10-{day:02d}T13:03:14.000Z",
        "likes": 51,
        "photos": ["https://scontent.cdninstagram.com/a.jpg", "https://scontent.cdninstagram.com/b.jpg"],
        "videos": None,
        "latest_comments": [{"comments": "toll", "user_commenting": "someone"}],
        "post_id": f"36091{i}",
        "discovery_input": {"url": f"https://www.instagram.com/{handle}/"},
        "shortcode": f"BD{handle.replace('.', '')}{i}",
        "content_type": "Carousel",
        "coauthor_producers": None,
        "tagged_users": None,
        "is_paid_partnership": False,
        "partnership_details": {"profile_id": None, "profile_url": None, "username": None},
        "post_content": [
            {
                "alt_text": "Photo by X on October 20, 2026.",
                "index": 0,
                "type": "Photo",
                "url": "https://scontent.cdninstagram.com/a.jpg",
            },
            {"alt_text": None, "index": 1, "type": "Video", "url": "https://scontent.cdninstagram.com/v.mp4"},
        ],
        "alt_text": "Photo by X on October 20, 2026.",
        **extra,
    }


def bd_profile(handle: str, **extra) -> dict:
    return {
        "account": handle,
        "id": "5087052934",
        "followers": 7938,
        "posts_count": 835,
        "is_business_account": False,
        "is_professional_account": True,
        "is_verified": True,
        "external_url": ["https://example.ch"],
        "biography": "Eishockey 🇨🇭 @evzwomen",
        "category_name": "Athlete",
        "full_name": "Testa Beispiel",
        "is_private": False,
        "profile_image_link": "https://scontent.cdninstagram.com/p.jpg",
        "url": f"https://www.instagram.com/{handle}/",
        "posts": [],
        **extra,
    }


class FakeBrightData:
    """trigger → progress → snapshot. `records(dataset, body)` builds a snapshot's records; `progress` is the
    list of statuses the progress endpoint returns in turn (last one repeats); `fail_trigger` = (status, text)."""

    def __init__(self, records, progress=("running", "ready"), fail_trigger=None, error_message=None):
        self.records, self.progress, self.fail_trigger = records, list(progress), fail_trigger
        self.error_message = error_message
        self.calls: list[tuple[str, str, dict | None]] = []
        self.snapshots: dict[str, list[dict]] = {}
        self.polls: dict[str, int] = {}
        self.auth: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        path = request.url.path
        if path.endswith("/trigger"):
            body = json.loads(request.content)
            self.calls.append(("trigger", dict(request.url.params), body))
            if self.fail_trigger:
                return httpx.Response(self.fail_trigger[0], text=self.fail_trigger[1])
            sid = f"sd_{len(self.snapshots)}"
            self.snapshots[sid] = self.records(request.url.params["dataset_id"], body)
            return httpx.Response(200, json={"snapshot_id": sid})
        sid = path.rsplit("/", 1)[1]
        if "/progress/" in path:
            n = self.polls.get(sid, 0)
            self.polls[sid] = n + 1
            status = self.progress[min(n, len(self.progress) - 1)]
            self.calls.append(("progress", sid, None))
            body = {"snapshot_id": sid, "status": status}
            if status == "failed":
                body["error_message"] = self.error_message
            return httpx.Response(200, json=body)
        self.calls.append(("snapshot", sid, dict(request.url.params)))
        return httpx.Response(200, json=self.snapshots[sid])


class MemQuota:
    """The guard without a database (unit tests); the DB version is tested separately."""

    def __init__(self, used=0, cap=4500, free=5000, price=0.0015):
        self.used_n, self.cap, self.free, self.price = used, cap, free, price
        self.booked: list[tuple[int, str]] = []

    def used(self):
        return self.used_n

    def room(self):
        return max(0, self.cap - self.used_n)

    def book(self, records, note):
        self.booked.append((records, note))
        self.used_n += records
        return 0.0


def posts_for_inputs(dataset, body):
    """Two posts per profile URL in the input (posts dataset) or one profile record (profiles dataset)."""
    out = []
    for inp in body["input"]:
        handle = inp["url"].rstrip("/").rsplit("/", 1)[1]
        if dataset == bd.PROFILES_DATASET:
            out.append(bd_profile(handle))
        else:
            out += [bd_post(handle, 1, 21), bd_post(handle, 2, 20)]
    return out


def bd_source(fake, quota=None, settings=BD_SETTINGS):
    return bd.BrightDataInstagramSource(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        quota=quota if quota is not None else MemQuota(),
        sleep=lambda s: None,
    )


# --- mapping ----------------------------------------------------------------------------------------------


def test_parse_brightdata_post():
    d = bd_post(
        "testa.beispiel",
        1,
        tagged_users=[
            {"username": "Andrea.Braendli", "full_name": "Andrea"},
            "https://www.instagram.com/x.y/",
        ],
        coauthor_producers=["Lara.Stalder7"],
        is_paid_partnership=True,
    )
    p = bd.parse_brightdata_post(d)
    assert (p.post_id, p.shortcode, p.owner_username, p.url) == (
        "360911",
        "BDtestabeispiel1",
        "testa.beispiel",
        "https://www.instagram.com/p/BDtestabeispiel1",
    )
    assert p.published_at == datetime(2026, 10, 20, 13, 3, 14, tzinfo=UTC)
    assert p.caption.startswith("Sieg #1!") and p.hashtags == ["hockey", "Sieg"]
    assert p.mentions == ["lara.stalder7"] and p.tagged == ["andrea.braendli", "x.y"]
    assert p.coauthors == ["lara.stalder7"] and p.paid_partnership and p.media_type == "carousel"
    assert (p.likes, p.comments) == (51, 3)
    assert p.alt == "Photo by X on October 20, 2026." and p.informative_alt is None  # generic alt
    assert [(x.kind, x.url) for x in p.media] == [
        ("image", "https://scontent.cdninstagram.com/a.jpg"),
        ("video", "https://scontent.cdninstagram.com/v.mp4"),
    ]
    assert "cdninstagram" not in json.dumps(p.raw) and "latest_comments" not in p.raw
    assert p.raw["via"] == "brightdata" and p.involves("andrea.braendli")


def test_parse_brightdata_post_fallbacks():
    d = bd_post("a", 1, post_content=None, photos=None, hashtags=None, content_type="Reel")
    d |= {"videos": ["https://cdn/v1.mp4"], "videos_duration": [12.5], "shortcode": None, "post_id": None}
    d |= {"partnership_details": {"username": "brand"}}
    p = bd.parse_brightdata_post(d)
    assert p.shortcode == "BDa1" and p.post_id == "BDa1" and p.media_type == "video"
    assert p.hashtags == ["1", "hockey"] and p.paid_partnership  # from the caption / partnership_details
    assert [(x.kind, x.url, x.duration_s) for x in p.media] == [("video", "https://cdn/v1.mp4", 12.5)]
    many = bd_post("a", 2, post_content=[{"type": "Photo", "url": f"https://cdn/{i}.jpg"} for i in range(8)])
    assert len(bd.parse_brightdata_post(many).media) == 5


def test_parse_brightdata_profile():
    p = bd.parse_brightdata_profile(bd_profile("Testa.Beispiel"))
    assert (p.username, p.full_name, p.biography, p.category, p.followers, p.posts_count) == (
        "testa.beispiel",
        "Testa Beispiel",
        "Eishockey 🇨🇭 @evzwomen",
        "Athlete",
        7938,
        835,
    )
    assert p.verified_badge and p.is_business is False and not p.private
    assert p.external_url == "https://example.ch" and p.source == "brightdata"
    q = bd.parse_brightdata_profile(
        bd_profile("x", account=None, is_business_account=None, external_url=None, category_name=None)
        | {"business_category_name": "Sportler", "external_urls": [{"url": "https://y.ch"}]}
    )
    assert (q.username, q.is_business, q.category, q.external_url) == ("x", True, "Sportler", "https://y.ch")


# --- trigger / poll / download -------------------------------------------------------------------------------


def test_fetch_posts_trigger_poll_download():
    fake = FakeBrightData(posts_for_inputs, progress=("starting", "running", "ready"))
    quota = MemQuota()
    s = bd_source(fake, quota)
    since = datetime(2026, 10, 1, 12, tzinfo=UTC)
    r = s.fetch_posts(["testa.beispiel", "andrea.braendli"], since, limit=3)
    kind, params, body = fake.calls[0]
    assert kind == "trigger" and params["dataset_id"] == bd.POSTS_DATASET
    assert (params["type"], params["discover_by"], params["format"], params["include_errors"]) == (
        "discover_new",
        "url",
        "json",
        "true",
    )
    assert body["limit_per_input"] == 3
    first = body["input"][0]
    assert first["url"] == "https://www.instagram.com/testa.beispiel/" and first["num_of_posts"] == 3
    assert first["start_date"] == "10-01-2026" and len(first["end_date"]) == 10
    assert [c[0] for c in fake.calls] == ["trigger", "progress", "progress", "progress", "snapshot"]
    assert fake.calls[-1][2] == {"format": "json"} and fake.auth == {"Bearer bd-secret"}
    assert r.missing == [] and r.source == "brightdata" and r.cost_usd == 0 and r.paid_handles == 0
    assert [p.post_id for p in r.posts["testa.beispiel"]] == ["360911", "360912"]  # newest first
    assert quota.booked == [(4, "posts for 2 handles")] and s.records == 4
    assert s.fetch_mentions(["testa.beispiel"], None).missing == ["testa.beispiel"]


def test_fetch_posts_batches_and_since_filter():
    fake = FakeBrightData(posts_for_inputs)
    settings = BD_SETTINGS.model_copy(update={"brightdata_batch_size": 2})
    r = bd_source(fake, settings=settings).fetch_posts(
        ["a", "b", "c"], datetime(2026, 10, 20, 23, tzinfo=UTC), 12
    )
    triggers = [c for c in fake.calls if c[0] == "trigger"]
    assert [len(t[2]["input"]) for t in triggers] == [2, 1]
    assert {h: [p.post_id for p in ps] for h, ps in r.posts.items()} == {
        "a": ["360911"],
        "b": ["360911"],
        "c": ["360911"],
    }


def test_collab_post_belongs_to_the_handle_that_found_it():
    def records(dataset, body):
        return [bd_post("evzwomen", 1, discovery_input={"url": "https://www.instagram.com/testa.beispiel/"})]

    r = bd_source(FakeBrightData(records)).fetch_posts(["testa.beispiel"], None, 12)
    assert [p.owner_username for p in r.posts["testa.beispiel"]] == ["evzwomen"]


def test_error_records_and_empty_snapshot():
    def records(dataset, body):
        return [
            {"input": {"url": "https://www.instagram.com/gone/"}, "error": "dead", "error_code": "dead_page"},
            {
                "input": {"url": "https://www.instagram.com/busy/"},
                "error": "rl",
                "error_code": "bucket_rate_limit",
            },
            bd_post("ok", 1),
        ]

    quota = MemQuota()
    r = bd_source(FakeBrightData(records), quota).fetch_posts(["gone", "busy", "ok"], None, 12)
    assert r.posts["gone"] == [] and r.missing == ["busy"] and len(r.posts["ok"]) == 1
    assert quota.booked[0][0] == 3  # error records are counted too (safe side)
    empty = FakeBrightData(lambda d, b: [], progress=("failed",), error_message="No data found in discovery")
    r = bd_source(empty).fetch_posts(["x"], None, 12)
    assert r.posts == {"x": []} and r.missing == []


def test_fetch_profiles():
    fake = FakeBrightData(posts_for_inputs)
    quota = MemQuota()
    s = bd_source(fake, quota)
    profiles = s.fetch_profiles(["testa.beispiel", "lara.stalder7"])
    trigger = fake.calls[0]
    assert trigger[1]["dataset_id"] == bd.PROFILES_DATASET and "type" not in trigger[1]
    assert trigger[2]["input"] == [
        {"url": "https://www.instagram.com/testa.beispiel/"},
        {"url": "https://www.instagram.com/lara.stalder7/"},
    ]
    assert [p.username for p in profiles] == ["testa.beispiel", "lara.stalder7"]
    assert quota.booked == [(2, "profiles for 2 handles")] and src.paid_profile_count(s, 2) == 0


@pytest.mark.parametrize(
    "status,text",
    [
        (401, "Invalid credentials"),
        (402, "Payment required"),
        (403, "Forbidden"),
        (400, "Your free credits are used up, add funds to your balance"),
        (429, "You have too many running jobs for this dataset"),
    ],
)
def test_quota_or_auth_error_stops_for_the_run(status, text):
    fake = FakeBrightData(posts_for_inputs, fail_trigger=(status, text))
    s = bd_source(fake, settings=BD_SETTINGS.model_copy(update={"brightdata_batch_size": 1}))
    r = s.fetch_posts(["a", "b"], None, 12)
    assert r.posts == {} and r.missing == ["a", "b"] and s.stopped
    assert len([c for c in fake.calls if c[0] == "trigger"]) == 1  # no second call after the stop
    assert s.fetch_profiles(["a"]) == [] and len(fake.calls) == 1
    assert "bd-secret" not in s.stopped


def test_failed_snapshot_only_loses_that_batch():
    fake = FakeBrightData(posts_for_inputs, progress=("failed",), error_message="Internal Server Error")
    s = bd_source(fake)
    r = s.fetch_posts(["a"], None, 12)
    assert r.missing == ["a"] and not s.stopped
    suspended = FakeBrightData(posts_for_inputs, progress=("failed",), error_message="Account is suspended")
    s = bd_source(suspended)
    assert s.fetch_posts(["a"], None, 12).missing == ["a"] and s.stopped


def test_timeout_books_the_reserved_records():
    fake = FakeBrightData(posts_for_inputs, progress=("running",))
    quota = MemQuota()
    s = bd_source(fake, quota, BD_SETTINGS.model_copy(update={"brightdata_timeout_s": 0}))
    r = s.fetch_posts(["a", "b"], None, 5)
    assert r.missing == ["a", "b"] and quota.booked == [(10, "posts for 2 handles (Timeout, geschätzt)")]


def test_monthly_cap_limits_handles():
    quota = MemQuota(used=4470, cap=4500)  # room for 30 records = 2 handles with limit 12
    fake = FakeBrightData(posts_for_inputs)
    s = bd_source(fake, quota)
    r = s.fetch_posts(["a", "b", "c"], None, 12)
    assert list(r.posts) == ["a", "b"] and r.missing == ["c"]
    assert [i["url"] for i in fake.calls[0][2]["input"]] == [
        "https://www.instagram.com/a/",
        "https://www.instagram.com/b/",
    ]
    full = MemQuota(used=4500, cap=4500)
    fake2 = FakeBrightData(posts_for_inputs)
    assert bd_source(fake2, full).fetch_posts(["a"], None, 1).missing == ["a"] and fake2.calls == []


# --- the ledger guard (database) ---------------------------------------------------------------------------


def test_quota_counts_records_per_month_in_ledger(db):
    settings = BD_SETTINGS.model_copy(
        update={"brightdata_free_records_per_month": 100, "brightdata_monthly_record_cap": 120}
    )
    q = bd.BrightDataQuota(settings, engine=db)
    with Session(db) as s:
        old = m.CostLedger(source="brightdata", units=999, cost_usd=0.0, note="last month")
        s.add(old)
        s.flush()
        old.created_at = datetime(2020, 1, 31, tzinfo=UTC)
        s.add(m.CostLedger(source="apify", units=50, cost_usd=1.0, note="other source"))
        s.commit()
    assert q.used() == 0 and q.room() == 120
    assert q.book(90, "posts") == 0.0  # inside the free records
    assert q.book(20, "posts") == pytest.approx(10 * 0.0015)  # 10 free, 10 paid
    assert q.used() == 110 and q.room() == 10
    with Session(db) as s:
        rows = s.query(m.CostLedger).filter(m.CostLedger.source == "brightdata").order_by(m.CostLedger.id)
        assert [(r.units, r.cost_usd) for r in rows][-2:] == [(90, 0.0), (20, pytest.approx(0.015))]
    with Session(db) as s:
        assert costs.spent_today(s, "apify") == 1.0  # Bright Data never counts as Apify spend


def test_source_books_in_ledger(db):
    fake = FakeBrightData(posts_for_inputs)
    s = bd.BrightDataInstagramSource(
        BD_SETTINGS, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda x: None
    )
    s.quota.engine = db
    s.fetch_posts(["a"], None, 12)
    s.fetch_profiles(["a"])
    assert s.quota.used() == 3


# --- hybrid order --------------------------------------------------------------------------------------------


def hybrid(graph=None, brightdata=None, apify=None):
    return src.HybridInstagramSource(GRAPH_SETTINGS, graph=graph, apify=apify, brightdata=brightdata)


def test_hybrid_graph_then_brightdata_then_apify():
    graph = graph_source(FakeGraph({"a": [[media(1, 10, owner="a")]]}))

    def records(dataset, body):  # Bright Data knows b, not c (rate limited)
        out = []
        for inp in body["input"]:
            h = inp["url"].rstrip("/").rsplit("/", 1)[1]
            if dataset == bd.PROFILES_DATASET:
                out.append(
                    bd_profile(h)
                    if h == "b"
                    else {"input": inp, "error_code": "bucket_rate_limit", "error": "x"}
                )
            elif h == "b":
                out.append(bd_post("b", 1))
            else:
                out.append({"input": inp, "error_code": "bucket_rate_limit", "error": "x"})
        return out

    fake = FakeBrightData(records)
    apify = FakeApify(cost=0.02)
    h = hybrid(graph, bd_source(fake), apify)
    r = h.fetch_posts(["a", "b", "c"], None, 12, max_usd=1.0)
    assert [i["url"] for i in fake.calls[0][2]["input"]] == [
        "https://www.instagram.com/b/",
        "https://www.instagram.com/c/",
    ]
    assert apify.calls == [("posts", ["c"], 1.0)] and r.paid_handles == 1 and r.cost_usd == 0.02
    assert r.posts["a"][0].post_id == "m1" and r.posts["b"][0].raw["via"] == "brightdata"
    assert r.posts["c"][0].post_id == "a-c"
    profiles = h.fetch_profiles(["a", "b", "c"])
    assert {p.username: p.source for p in profiles} == {"a": "graph", "b": "brightdata", "c": ""}
    assert apify.calls[-1] == ("profiles", ["c"], None) and h.last_paid_count == 1


def test_hybrid_brightdata_used_up_falls_back_to_apify():
    fake = FakeBrightData(posts_for_inputs, fail_trigger=(402, "insufficient balance"))
    apify = FakeApify()
    h = hybrid(None, bd_source(fake), apify)
    assert src.likely_paid(h, ["a"]) == []  # Bright Data still looks open before the first call
    r = h.fetch_posts(["a", "b"], None, 12, max_usd=0.5)
    assert apify.calls == [("posts", ["a", "b"], 0.5)] and r.paid_handles == 2
    assert src.likely_paid(h, ["a"]) == ["a"]  # stopped: Apify again in the estimate
    h.fetch_posts(["c"], None, 12, max_usd=0.5)
    assert len([c for c in fake.calls if c[0] == "trigger"]) == 1


def test_hybrid_graph_and_brightdata_without_apify_is_free():
    graph = graph_source(FakeGraph({"a": [[media(1, 10, owner="a")]]}))
    fake = FakeBrightData(lambda d, b: [])  # Bright Data finds nothing for b: still "read", no Apify
    h = hybrid(graph, bd_source(fake), None)
    assert not h.paid and not src.is_paid(h) and src.likely_paid(h, ["a", "b"]) == []
    r = h.fetch_posts(["a", "b"], None, 12)
    assert list(r.posts) == ["a", "b"] and r.missing == [] and r.cost_usd == 0
    full = hybrid(graph, bd_source(FakeBrightData(posts_for_inputs), MemQuota(used=4500)), None)
    r = full.fetch_posts(["a", "z"], None, 12)
    assert r.missing == ["z"] and r.paid_handles == 0  # cap reached, no Apify: left for next time
    assert full.fetch_profiles(["z"]) == [] and full.last_paid_count == 0


def test_hybrid_mentions_without_apify_are_skipped():
    h = hybrid(None, bd_source(FakeBrightData(posts_for_inputs)), None)
    r = h.fetch_mentions(["a", "b"], None, 50)
    assert r.posts == {} and r.missing == ["a", "b"] and r.cost_usd == 0


def test_source_choice_with_brightdata(monkeypatch):
    monkeypatch.setattr(src.ApifyInstagramSource, "__init__", lambda self, settings: None)
    bd_only = Settings(_env_file=None, brightdata_api_token="t")
    assert isinstance(src.get_instagram_source(bd_only), bd.BrightDataInstagramSource)
    graph_bd = GRAPH_SETTINGS.model_copy(update={"brightdata_api_token": SecretStr("t")})
    h = src.get_instagram_source(graph_bd)
    assert isinstance(h, src.HybridInstagramSource) and h.apify is None and h.graph and h.brightdata
    assert not src.apify_used(graph_bd) and src.instagram_configured(graph_bd)
    all3 = graph_bd.model_copy(update={"apify_token": "x"})
    h = src.get_instagram_source(all3)
    assert h.apify is not None and h.paid and src.apify_used(all3)
    bd_apify = bd_only.model_copy(update={"apify_token": "x"})
    h = src.get_instagram_source(bd_apify)
    assert isinstance(h, src.HybridInstagramSource) and h.graph is None and h.graph_status == {}
    old = all3.model_copy(update={"instagram_source": "apify"})  # INSTAGRAM_SOURCE=apify: as before
    assert type(src.get_instagram_source(old)) is src.ApifyInstagramSource
    only = all3.model_copy(update={"instagram_source": "brightdata"})
    assert isinstance(src.get_instagram_source(only), bd.BrightDataInstagramSource)
    assert not src.instagram_configured(Settings(_env_file=None))


# --- ingest --------------------------------------------------------------------------------------------------


def test_ingest_graph_plus_brightdata_without_apify(world):  # noqa: F811
    set_profiles(world, {})
    graph = graph_source(FakeGraph({"testa.beispiel": [[media(1, 10)]]}))
    fake = FakeBrightData(posts_for_inputs)
    s = bd.BrightDataInstagramSource(
        BD_SETTINGS, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda x: None
    )
    s.quota.engine = world
    r = run_ingest(world, hybrid(graph, s, None), GRAPH_SETTINGS, refresh_profiles=False)
    assert r.errors == [] and r.skipped_budget == [] and r.cost_usd == 0
    assert r.items_new == 1 + 4  # Graph post + 2 Bright Data posts for each of the other two
    with Session(world) as db:
        led = db.query(m.CostLedger).all()
        assert [(x.source, x.units, x.cost_usd) for x in led] == [("brightdata", 4, 0.0)]
        h = db.query(m.Handle).filter(m.Handle.handle == "andrea.braendli").one()
        assert h.last_scraped_at is not None
    assert len(NAMES) == 3


# --- 2026-10-30: profile fields kept, history cap, dead_page texts, team accounts, paid marker ----------------


def bd_world_source(world, records, settings=BD_SETTINGS):  # noqa: F811
    """A Bright Data source that books in the test database's ledger."""
    s = bd.BrightDataInstagramSource(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(FakeBrightData(records))),
        sleep=lambda x: None,
    )
    s.quota.engine = world
    return s


def test_apply_profile_brightdata_keeps_category_badge_and_business_flag(world):  # noqa: F811
    from klugscheisser.handles.profile import apply_profile

    with Session(world) as s:
        h = s.query(m.Handle).filter(m.Handle.handle == "testa.beispiel").one()
        h.profile = {"category": "Athlete", "verified_badge": True, "is_business": True, "graph_ok": False}
        empty = bd_profile("testa.beispiel", category_name=None, is_verified=None)
        empty |= {"is_business_account": None, "is_professional_account": None, "followers": 9000}
        apply_profile(h, bd.parse_brightdata_profile(empty))
        p = h.profile
        assert (p["category"], p["verified_badge"], p["is_business"], p["followers"]) == (
            "Athlete",
            True,
            True,
            9000,
        )
        assert p["graph_ok"] is False  # the ingest's hint stays
        # a Bright Data profile that has the fields wins
        apply_profile(h, bd.parse_brightdata_profile(bd_profile("testa.beispiel", category_name="Coach")))
        assert (h.profile["category"], h.profile["is_business"]) == ("Coach", False)
        # Apify knows category and badge: an empty one there still means "none"
        from klugscheisser.collectors.instagram.base import InstagramProfile

        apply_profile(h, InstagramProfile(username="testa.beispiel", full_name=None, biography=None))
        assert h.profile["category"] is None and h.profile["verified_badge"] is False
        assert h.profile["is_business"] is False  # unknown business flag: the stored one stays
        s.rollback()


def test_history_fetch_capped_for_brightdata():
    fake = FakeBrightData(posts_for_inputs)
    quota = MemQuota(used=4400, cap=4500)  # room 100 = 2 handles at the cap of 50, not 400
    r = bd_source(fake, quota).fetch_posts(["a", "b", "c"], None, 400)
    body = fake.calls[0][2]
    assert body["limit_per_input"] == 50 and {i["num_of_posts"] for i in body["input"]} == {50}
    assert [i["url"] for i in body["input"]] == [
        "https://www.instagram.com/a/",
        "https://www.instagram.com/b/",
    ]
    assert r.missing == ["c"] and len(r.posts["a"]) == 2
    small = FakeBrightData(posts_for_inputs)
    s = bd_source(small, settings=BD_SETTINGS.model_copy(update={"brightdata_history_max_posts": 1}))
    r = s.fetch_posts(["a"], None, 400)
    assert small.calls[0][2]["limit_per_input"] == 1 and len(r.posts["a"]) == 1  # newest only
    nightly = FakeBrightData(posts_for_inputs)
    bd_source(nightly).fetch_posts(["a"], None, 12)  # below the cap: unchanged
    assert nightly.calls[0][2]["limit_per_input"] == 12


def test_hybrid_history_cap_does_not_send_the_rest_to_apify():
    apify = FakeApify()
    h = hybrid(None, bd_source(FakeBrightData(posts_for_inputs)), apify)
    r = h.fetch_posts(["a"], None, 400, max_usd=1.0)
    assert apify.calls == [] and len(r.posts["a"]) == 2 and r.paid_handles == 0


def dead(handle: str, text: str, code: str = "dead_page") -> dict:
    return {"input": {"url": f"https://www.instagram.com/{handle}/"}, "error": text, "error_code": code}


def test_dead_page_texts():
    assert bd.dead_page_state(dead("a", "This profile is private")) == "private"
    assert bd.dead_page_state(dead("a", "Profile isn't available")) == "unavailable"
    assert bd.dead_page_state(dead("a", "Page not found")) == "unavailable"
    no_posts = "There are no public posts in the profile for the specified period"
    assert bd.dead_page_state(dead("a", no_posts)) == "empty"
    assert bd.dead_page_state(dead("a", "rate limit", code="bucket_rate_limit")) is None

    def records(dataset, body):
        return [
            dead("priv", "This profile is private"),
            dead("gone", "Profile isn't available"),
            dead("quiet", no_posts),
            bd_post("ok", 1),
        ]

    s = bd_source(FakeBrightData(records))
    r = s.fetch_posts(["priv", "gone", "quiet", "ok"], None, 12)
    assert r.missing == [] and r.posts["priv"] == [] and r.posts["quiet"] == []  # read, nothing to Apify
    assert s.page_states == {"priv": "private", "gone": "unavailable"}
    p = bd_source(FakeBrightData(lambda d, b: [dead("gone2", "Profile isn't available"), bd_profile("ok")]))
    assert [x.username for x in p.fetch_profiles(["gone2", "ok"])] == ["ok"]
    assert p.page_states == {"gone2": "unavailable"}
    assert hybrid(None, p, None).page_states == {"gone2": "unavailable"} and hybrid().page_states == {}


def test_ingest_applies_dead_page_states(world):  # noqa: F811
    set_profiles(world, {})
    no_posts = "There are no public posts in the profile for the specified period"

    def records(dataset, body):
        return [
            dead("testa.beispiel", "This profile is private"),
            dead("andrea.braendli", "Profile isn't available"),
            dead("lara.stalder7", no_posts),
        ]

    r = run_ingest(world, hybrid(None, bd_world_source(world, records), None), GRAPH_SETTINGS)
    assert r.private == ["testa.beispiel"] and r.review == ["andrea.braendli"] and r.errors == []
    with Session(world) as s:
        hs = {h.handle: h for h in s.query(m.Handle).filter(m.Handle.platform == "instagram")}
        assert hs["testa.beispiel"].profile["private"] is True and hs["testa.beispiel"].usable
        gone = hs["andrea.braendli"]
        assert gone.status == "candidate" and not gone.usable
        assert [e["kind"] for e in gone.evidence][-1] == "ig_unavailable"
        from klugscheisser.handles.service import reason_de

        assert "nicht verfügbar" in reason_de(gone)
        quiet = hs["lara.stalder7"]
        assert quiet.usable and not (quiet.profile or {}).get("private") and quiet.last_scraped_at
    # next run: the private and the flagged handle are no longer read
    calls: list[list[str]] = []

    def seen(dataset, body):
        calls.append([i["url"] for i in body["input"]])
        return []

    r = run_ingest(world, hybrid(None, bd_world_source(world, seen), None), GRAPH_SETTINGS)
    assert r.handles == 1 and calls[0] == ["https://www.instagram.com/lara.stalder7/"]


def test_team_accounts_via_brightdata_before_apify(world):  # noqa: F811
    from klugscheisser.ingest.instagram import ingest_instagram_teams, team_for_account
    from klugscheisser.ingest.runner import build_context

    apify = FakeApify()
    h = hybrid(None, bd_world_source(world, posts_for_inputs), apify)
    with Session(world) as s:
        costs.record(s, "apify", 1, GRAPH_SETTINGS.apify_daily_cost_cap_usd, "cap used up")
        assert team_for_account(s, "lionsfrauen").key == "zsc-frauen"
        assert team_for_account(s, "scb_frauen_official").key == "scb-frauen"
        assert team_for_account(s, "fr_gotteron_ladies").key == "fribourg-ladies"
        assert team_for_account(s, "postfinance_womensleague") is None  # a league, no team
        r = ingest_instagram_teams(
            build_context(s, GRAPH_SETTINGS), h, ["lionsfrauen", "postfinance_womensleague"]
        )
        s.commit()
    assert apify.calls == [] and r.cost_usd == 0 and r.skipped_budget == [] and r.errors == []
    assert r.team_posts_new == 4
    with Session(world) as s:
        led = s.query(m.CostLedger).filter(m.CostLedger.source == "brightdata").all()
        assert [(x.units, x.cost_usd) for x in led] == [(2, 0.0), (2, 0.0)]  # 1 handle, limit 12 each
        zsc = s.query(m.Team).filter(m.Team.key == "zsc-frauen").one()
        lions = s.query(m.Item).filter(m.Item.author == "lionsfrauen").all()
        assert len(lions) == 2 and all(zsc.id in {t.team_id for t in i.teams} for i in lions)
        assert {i.source_kind for i in lions} == {"official"}
        acc = s.get(m.Account, ("instagram", "postfinance_womensleague"))
        assert acc.kind == "official"


def test_paid_partnership_from_caption():
    from klugscheisser.collectors.instagram.base import caption_paid_partnership as paid

    for yes in (
        "Neue Schuhe #ad",
        "Werbung | danke @brand",
        "ANZEIGE: Mein Lieblingsstock",
        "Ready! #sponsored",
        "Paid partnership with @brand",
        "Bezahlte Partnerschaft mit @x",
        "#Werbung #hockey",
    ):
        assert paid(yes), yes
    for no in ("#adidas Schuhe", "Anzeigetafel zeigt 3:2", "#advent", "Werbungsfrei", "", None):
        assert not paid(no), no
    p = bd.parse_brightdata_post(bd_post("a", 1, description="Mein Stock #ad"))
    assert p.paid_partnership and not bd.parse_brightdata_post(bd_post("a", 2)).paid_partnership
