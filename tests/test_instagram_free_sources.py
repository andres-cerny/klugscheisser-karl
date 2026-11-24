"""Free Instagram sources: Meta Graph Business Discovery, the hybrid (Graph first, Apify for the rest) and the
cheaper apidojo actor. Offline: Graph via httpx.MockTransport, Apify faked; no paid call."""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from klugscheisser.collectors.instagram import sources as src
from klugscheisser.collectors.instagram.base import (
    FetchResult,
    InstagramProfile,
    caption_hashtags,
    caption_mentions,
    parse_apidojo_post,
    shortcode_from_url,
)
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest import costs
from klugscheisser.ingest.instagram import ingest_instagram, ingest_instagram_teams
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

GRAPH_SETTINGS = Settings(
    _env_file=None,
    meta_graph_token="tok",
    meta_ig_business_user_id="17841",
    meta_graph_page_size=2,
    llm_provider="auto",
)


@pytest.fixture(autouse=True)
def fresh_graph_budget():
    src.reset_graph_budget()
    yield
    src.reset_graph_budget()


def media(i: int, day: int, owner: str = "testa.beispiel", **extra) -> dict:
    return {
        "id": f"m{i}",
        "caption": f"Post {i} #hockey @Lara.Stalder7. mit @official_evz",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "media_url": f"https://cdn.example/{i}.jpg",
        "permalink": f"https://www.instagram.com/p/CODE{i}/",
        "timestamp": f"2026-10-{day:02d}T10:00:00+0000",
        "like_count": 10 + i,
        "comments_count": i,
        "username": owner,
        **extra,
    }


class FakeGraph:
    """Business Discovery: `pages[handle]` = list of media pages (newest first); a handle not in `pages` is no
    professional account (400). `fail` = {handle: (status, error code)}."""

    def __init__(self, pages: dict[str, list[list[dict]]], fail: dict | None = None, headers=None):
        self.pages, self.fail, self.headers = pages, fail or {}, headers or {}
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = request.url.params["fields"]
        handle = re.search(r"username\(([^)]+)\)", fields).group(1)
        after = re.search(r"media\.after\(([^)]+)\)", fields)
        limit = re.search(r"\.limit\((\d+)\)", fields)
        self.calls.append({"handle": handle, "after": after and after.group(1), "fields": fields})
        if handle in self.fail:
            status, code = self.fail[handle]
            return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})
        if handle not in self.pages:
            return httpx.Response(
                400, json={"error": {"code": 110, "message": "Invalid user id", "error_subcode": 2207013}}
            )
        page_no = int(after.group(1)) if after else 0
        bd: dict = {"username": handle, "name": "Testa Beispiel", "id": "99"}
        if "biography" in fields:
            bd |= {"biography": "Eishockey 🇨🇭", "followers_count": 1234, "media_count": 77, "website": "x.ch"}
        if "media" in fields:
            pages = self.pages[handle]
            data = pages[page_no][: int(limit.group(1))] if page_no < len(pages) else []
            block: dict = {"data": data}
            if page_no + 1 < len(pages):
                block["paging"] = {"cursors": {"after": str(page_no + 1)}}
            bd["media"] = block
        return httpx.Response(200, json={"business_discovery": bd, "id": "17841"}, headers=self.headers)


def graph_source(fake: FakeGraph, settings: Settings = GRAPH_SETTINGS):
    return src.GraphBusinessDiscoverySource(
        settings, client=httpx.Client(transport=httpx.MockTransport(fake))
    )


class FakeApify:
    name, paid = "apify", True

    def __init__(self, cost: float = 0.01):
        self.cost = cost
        self.calls: list[tuple[str, list[str], float | None]] = []
        self.last_paid_count = None

    def _post(self, handle):
        from klugscheisser.collectors.instagram.base import InstagramPost

        return InstagramPost(
            post_id=f"a-{handle}",
            shortcode=None,
            url=f"https://www.instagram.com/p/APIFY{handle.replace('.', '')}/",
            owner_username=handle,
            owner_full_name=None,
            published_at=datetime(2026, 10, 20, tzinfo=UTC),
            caption=f"Apify-Post von {handle}",
        )

    def fetch_posts(self, handles, since, limit=12, max_usd=None):
        self.calls.append(("posts", list(handles), max_usd))
        return FetchResult(posts={h: [self._post(h)] for h in handles}, cost_usd=self.cost, source="apify")

    def fetch_mentions(self, handles, since, limit=50, max_usd=None):
        self.calls.append(("mentions", list(handles), max_usd))
        return FetchResult(posts={h: [] for h in handles}, cost_usd=self.cost, source="apify")

    def fetch_profiles(self, handles):
        self.calls.append(("profiles", list(handles), None))
        self.last_paid_count = len(handles)
        return [InstagramProfile(username=h, full_name=h, biography=None) for h in handles]


# --- caption parsing -------------------------------------------------------------------------------------


def test_caption_hashtags_mentions_and_shortcode():
    text = "Sieg! #Hockey #fürblauundwiis #hockey mail@x.ch @Lara.Stalder7. und @official_evz, @lara.stalder7"
    assert caption_hashtags(text) == ["Hockey", "fürblauundwiis", "hockey"]
    assert caption_mentions(text) == ["lara.stalder7", "official_evz"]
    assert shortcode_from_url("https://www.instagram.com/reel/DAbc_1-x/") == "DAbc_1-x"
    assert shortcode_from_url("https://www.instagram.com/someone/p/XYZ/") == "XYZ"
    assert shortcode_from_url("https://example.com") is None


# --- Graph ---------------------------------------------------------------------------------------------


def test_graph_parses_posts_media_and_profile():
    carousel = media(
        3,
        12,
        media_type="CAROUSEL_ALBUM",
        media_url=None,
        children={
            "data": [{"media_type": "IMAGE", "media_url": f"https://cdn.example/c{i}.jpg"} for i in range(7)]
        },
    )
    video = media(2, 11, media_type="VIDEO", media_url="https://cdn.example/v.mp4", thumbnail_url="t.jpg")
    silent_video = media(1, 10, media_type="VIDEO", media_url=None, thumbnail_url="https://cdn.example/t.jpg")
    fake = FakeGraph({"testa.beispiel": [[carousel, video], [silent_video]]})
    g = graph_source(fake)
    r = g.fetch_posts(["testa.beispiel"], since=None, limit=12)
    posts = r.posts["testa.beispiel"]
    assert [p.post_id for p in posts] == ["m3", "m2", "m1"] and r.missing == [] and not g.paid
    first = posts[0]
    assert first.shortcode == "CODE3" and first.url == "https://www.instagram.com/p/CODE3/"
    assert first.media_type == "carousel" and len(first.media) == 5  # MAX_CAROUSEL_IMAGES
    assert first.hashtags == ["hockey"] and first.mentions == ["lara.stalder7", "official_evz"]
    assert first.owner_full_name == "Testa Beispiel" and first.likes == 13
    assert first.published_at == datetime(2026, 10, 12, 10, tzinfo=UTC)
    assert "media_url" not in first.raw and "children" not in first.raw  # no CDN links stored
    assert [(x.kind, x.url) for x in posts[1].media] == [("video", "https://cdn.example/v.mp4")]
    assert [(x.kind, x.url) for x in posts[2].media] == [("image", "https://cdn.example/t.jpg")]
    # the profile came with the first page: fetch_profiles makes no extra call
    calls = len(fake.calls)
    (p,) = g.fetch_profiles(["testa.beispiel"])
    assert len(fake.calls) == calls
    assert (p.username, p.is_business, p.followers, p.posts_count, p.external_url, p.source) == (
        "testa.beispiel",
        True,
        1234,
        77,
        "x.ch",
        "graph",
    )
    assert g.api == "https://graph.facebook.com/v24.0" and g.ig_user_id == "17841"


def test_graph_pagination_stops_at_since_and_limit():
    pages = [[media(9, 28), media(8, 27)], [media(7, 26), media(6, 20)], [media(5, 10), media(4, 9)]]
    fake = FakeGraph({"testa.beispiel": pages})
    g = graph_source(fake)
    since = datetime(2026, 10, 21, tzinfo=UTC)
    posts = g.fetch_posts(["testa.beispiel"], since=since, limit=50).posts["testa.beispiel"]
    assert [p.post_id for p in posts] == ["m9", "m8", "m7"]
    assert [c["after"] for c in fake.calls] == [None, "1"]  # page 3 is never asked for
    assert "biography" in fake.calls[0]["fields"] and "biography" not in fake.calls[1]["fields"]
    fake.calls.clear()
    posts = graph_source(fake).fetch_posts(["testa.beispiel"], since=None, limit=3).posts["testa.beispiel"]
    assert [p.post_id for p in posts] == ["m9", "m8", "m7"] and len(fake.calls) == 2
    assert ".limit(1)" in fake.calls[1]["fields"]  # second page only asks for what is still missing


def test_graph_pinned_old_post_does_not_stop_paging():
    pinned = media(1, 1)
    fake = FakeGraph({"testa.beispiel": [[pinned, media(9, 28)], [media(8, 27), media(2, 2)]]})
    posts = graph_source(fake).fetch_posts(["testa.beispiel"], datetime(2026, 10, 21, tzinfo=UTC), 50)
    assert [p.post_id for p in posts.posts["testa.beispiel"]] == ["m9", "m8"]


def test_graph_400_means_not_available():
    fake = FakeGraph({"testa.beispiel": [[media(1, 10)]]})
    g = graph_source(fake)
    r = g.fetch_posts(["private.person", "testa.beispiel"], None, 12)
    assert list(r.posts) == ["testa.beispiel"] and r.missing == ["private.person"]
    assert g.status == {"private.person": False, "testa.beispiel": True}
    calls = len(fake.calls)
    g.fetch_posts(["private.person"], None, 12)  # known 'no': not asked twice
    assert g.fetch_profiles(["private.person"]) == [] and len(fake.calls) == calls


@pytest.mark.parametrize("code", [4, 17, 32, 613, 190])
def test_graph_rate_limit_or_token_stops_graph(code):
    fake = FakeGraph(
        {"a": [[media(1, 10)]], "b": [[media(2, 10)]], "c": [[media(3, 10)]]}, fail={"b": (400, code)}
    )
    g = graph_source(fake)
    r = g.fetch_posts(["a", "b", "c"], None, 12)
    assert list(r.posts) == ["a"] and r.missing == ["b", "c"] and g.stopped
    assert [c["handle"] for c in fake.calls] == ["a", "b"]
    assert "b" not in g.status  # a rate limit says nothing about the account
    # the block holds for the whole process: a new source (next nightly bucket) does not call Graph either
    g2 = graph_source(fake)
    assert g2.fetch_posts(["c"], None, 12).missing == ["c"] and len(fake.calls) == 2


def test_graph_call_cap_per_run():
    fake = FakeGraph({h: [[media(i, 10, owner=h)]] for i, h in enumerate("abcde")})
    settings = GRAPH_SETTINGS.model_copy(update={"meta_graph_max_calls_per_run": 3})
    r = graph_source(fake, settings).fetch_posts(list("abcde"), None, 1)
    assert list(r.posts) == ["a", "b", "c"] and r.missing == ["d", "e"] and len(fake.calls) == 3


def test_graph_usage_header_near_limit_stops():
    fake = FakeGraph(
        {"a": [[media(1, 10, owner="a")]], "b": [[media(2, 10, owner="b")]]},
        headers={"x-app-usage": json.dumps({"call_count": 97, "total_time": 3, "total_cputime": 2})},
    )
    g = graph_source(fake)
    r = g.fetch_posts(["a", "b"], None, 1)
    assert list(r.posts) == ["a"] and r.missing == ["b"] and len(fake.calls) == 1


def test_graph_server_error_falls_back_for_that_handle_only():
    fake = FakeGraph(
        {"a": [[media(1, 10, owner="a")]], "c": [[media(3, 10, owner="c")]]}, fail={"b": (500, 2)}
    )
    g = graph_source(fake)
    r = g.fetch_posts(["a", "b", "c"], None, 1)
    assert list(r.posts) == ["a", "c"] and r.missing == ["b"] and not g.stopped


# --- hybrid ---------------------------------------------------------------------------------------------


def hybrid(fake: FakeGraph, apify: FakeApify | None = None):
    return src.HybridInstagramSource(GRAPH_SETTINGS, graph=graph_source(fake), apify=apify or FakeApify())


def test_hybrid_graph_first_one_apify_call_for_the_rest():
    fake = FakeGraph({"a": [[media(1, 10, owner="a")]], "c": [[media(3, 10, owner="c")]]})
    apify = FakeApify(cost=0.02)
    h = hybrid(fake, apify)
    r = h.fetch_posts(["a", "b", "c", "d"], None, 12, max_usd=1.0)
    assert apify.calls == [("posts", ["b", "d"], 1.0)]
    assert [p.post_id for p in r.posts["a"]] == ["m1"] and r.posts["b"][0].post_id == "a-b"
    assert r.cost_usd == 0.02 and r.paid_handles == 2 and r.missing == []
    # same run: b and d are known 'no', Graph is not asked again; profiles a/c come from Graph
    calls = len(fake.calls)
    profiles = h.fetch_profiles(["a", "b", "c", "d"])
    assert len(fake.calls) == calls and apify.calls[-1] == ("profiles", ["b", "d"], None)
    assert {p.username for p in profiles} == {"a", "b", "c", "d"} and h.last_paid_count == 2
    assert src.paid_profile_count(h, 4) == 2


def test_hybrid_without_apify_budget_reads_only_graph():
    fake = FakeGraph({"a": [[media(1, 10, owner="a")]]})
    apify = FakeApify()
    r = hybrid(fake, apify).fetch_posts(["a", "b"], None, 12, max_usd=0.0)
    assert list(r.posts) == ["a"] and r.missing == ["b"] and apify.calls == [] and r.cost_usd == 0


def test_hybrid_all_on_graph_makes_no_apify_call():
    apify = FakeApify()
    r = hybrid(FakeGraph({"a": [[media(1, 10, owner="a")]]}), apify).fetch_posts(["a"], None, 12, max_usd=1)
    assert apify.calls == [] and r.paid_handles == 0 and r.cost_usd == 0


def test_hybrid_mentions_go_to_apify_only():
    fake = FakeGraph({"a": [[media(1, 10, owner="a")]]})
    apify = FakeApify()
    r = hybrid(fake, apify).fetch_mentions(["a"], None, 50, max_usd=0.5)
    assert fake.calls == [] and apify.calls == [("mentions", ["a"], 0.5)] and r.paid_handles == 1


def test_hybrid_hints_skip_graph_and_shape_estimate():
    fake = FakeGraph({"a": [[media(1, 10, owner="a")]]})
    h = hybrid(fake)
    h.set_graph_hints({"a": True, "b": False})
    assert src.likely_paid(h, ["a", "b", "c"]) == ["b", "c"]  # unknown c counts as paid
    h.fetch_posts(["a", "b"], None, 12, max_usd=1)
    assert [c["handle"] for c in fake.calls] == ["a"]


def test_source_choice(monkeypatch):
    monkeypatch.setattr(src.ApifyInstagramSource, "__init__", lambda self, settings: None)
    both = GRAPH_SETTINGS.model_copy(update={"apify_token": "x", "instagram_source": "auto"})
    assert isinstance(src.get_instagram_source(both), src.HybridInstagramSource)
    assert isinstance(
        src.get_instagram_source(both.model_copy(update={"instagram_source": "apify"})),
        src.ApifyInstagramSource,
    )
    graph_only = GRAPH_SETTINGS.model_copy(update={"instagram_source": "hybrid"})
    assert isinstance(src.get_instagram_source(graph_only), src.GraphBusinessDiscoverySource)
    apify_only = Settings(_env_file=None, apify_token="x", instagram_source="auto")
    assert isinstance(src.get_instagram_source(apify_only), src.ApifyInstagramSource)
    assert src.graph_first(both) and not src.graph_first(apify_only)
    assert src.is_paid(src.HybridInstagramSource(both, graph=object(), apify=object()))


# --- apidojo actor -------------------------------------------------------------------------------------

APIDOJO_POST = {
    "id": "3970912334131520660",
    "code": "Dcbg7qmIGSU",
    "url": "https://www.instagram.com/p/Dcbg7qmIGSU/",
    "createdAt": "2026-08-24T16:40:59.000Z",
    "likeCount": 240,
    "commentCount": 1,
    "isPaidPartnership": True,
    "caption": "Interview mit @Andrea.Braendli #olympics",
    "owner": {
        "id": "1",
        "username": "ItsSydneyWingfield",
        "fullName": "Sydney",
        "isPrivate": False,
        "isVerified": True,
        "followerCount": 5000,
        "postCount": 300,
        "profilePicUrl": "https://cdn.example/pic.jpg",
    },
    "coowners": [{"username": "andrea.braendli", "fullName": "Andrea Brändli"}],
    "isCarousel": False,
    "isVideo": True,
    "video": {"url": "https://cdn.example/v.mp4", "duration": 12.5},
    "image": {"url": "https://cdn.example/i.jpg"},
}


def test_parse_apidojo_post():
    p = parse_apidojo_post(APIDOJO_POST)
    assert (p.post_id, p.shortcode, p.owner_username, p.owner_full_name) == (
        "3970912334131520660",
        "Dcbg7qmIGSU",
        "itssydneywingfield",
        "Sydney",
    )
    assert p.published_at == datetime(2026, 8, 24, 16, 40, 59, tzinfo=UTC)
    assert (p.likes, p.comments, p.paid_partnership, p.media_type) == (240, 1, True, "video")
    assert (
        p.mentions == ["andrea.braendli"]
        and p.hashtags == ["olympics"]
        and p.coauthors == ["andrea.braendli"]
    )
    assert p.tagged == [] and p.alt is None
    assert [(x.kind, x.url, x.duration_s) for x in p.media] == [("video", "https://cdn.example/v.mp4", 12.5)]
    assert "video" not in p.raw and "image" not in p.raw and "cdn.example" not in json.dumps(p.raw)
    assert p.involves("andrea.braendli")


def test_apidojo_inputs(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "apify_client", types.SimpleNamespace(ApifyClient=lambda token: None))
    settings = Settings(_env_file=None, apify_token="x", apify_instagram_actor="apidojo/instagram-scraper")
    a = src.ApifyInstagramSource(settings)
    runs = []

    def fake_run(run_input, max_usd, actor=None):
        runs.append((actor or a.actor, run_input))
        if "startUrls" in run_input and run_input["startUrls"][0].endswith("/tagged/"):
            return [APIDOJO_POST | {"caption": "Team!", "coowners": []}], 0.0005
        return [APIDOJO_POST | {"owner": {"username": "andrea.braendli"}}], 0.0005

    monkeypatch.setattr(a, "_run", fake_run)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    r = a.fetch_posts(["andrea.braendli", "lara.stalder7"], since, 10)
    assert runs[0] == (
        "apidojo/instagram-scraper",
        {
            "startUrls": [
                "https://www.instagram.com/andrea.braendli/",
                "https://www.instagram.com/lara.stalder7/",
            ],
            "maxItems": 20,
            "until": "2026-10-01",
        },
    )
    assert len(r.posts["andrea.braendli"]) == 1 and r.posts["lara.stalder7"] == []
    m_ = a.fetch_mentions(["andrea.braendli", "lara.stalder7"], since, 5)
    assert [x[1]["startUrls"] for x in runs[1:]] == [
        ["https://www.instagram.com/andrea.braendli/tagged/"],
        ["https://www.instagram.com/lara.stalder7/tagged/"],
    ]
    assert "lara.stalder7" in m_.posts["lara.stalder7"][0].tagged and m_.cost_usd == pytest.approx(0.001)
    a.fetch_profiles(["andrea.braendli"])  # profiles stay on the actor with bios
    assert runs[-1][0] == "apify/instagram-scraper" and runs[-1][1]["resultsType"] == "details"


# --- ingest: budget only for the paid part ----------------------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        testa = repo.upsert_athlete(s, sport, "Testa Beispiel", nationality="SUI", position="F")
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata", "detail": "test"})
        for name, handle in [("Andrea Brändli", "andrea.braendli"), ("Lara Stalder", "lara.stalder7")]:
            add_handle_candidate(
                s, repo.resolve_athlete(s, name), "instagram", handle, {"kind": "wikidata", "detail": "test"}
            )
        s.commit()
    return db


NAMES = ["Testa Beispiel", "Andrea Brändli", "Lara Stalder"]


def set_profiles(db, graph_ok: dict[str, bool]):
    now = datetime.now(UTC).isoformat()
    with Session(db) as s:
        for h in s.query(m.Handle).filter(m.Handle.platform == "instagram"):
            h.profile = {"fetched_at": now} | (
                {"graph_ok": graph_ok[h.handle], "graph_checked_at": now} if h.handle in graph_ok else {}
            )
        s.commit()


def run_ingest(db, source, settings, **kw):
    with Session(db) as s:
        athletes = [repo.resolve_athlete(s, n) for n in NAMES]
        report = ingest_instagram(build_context(s, settings), source, athletes, **kw)
        s.commit()
    return report


def test_ingest_budget_counts_only_apify_handles(world):
    set_profiles(world, {"testa.beispiel": True})
    per_handle = 12 * 0.0027 * 2  # estimate of one handle without post count
    with Session(world) as s:
        costs.record(s, "apify", 1, 2.0 - 2.5 * per_handle, "earlier today")  # room for 2.5 handles
        s.commit()
    fake = FakeGraph({"testa.beispiel": [[media(1, 10)]]})
    apify = FakeApify(cost=0.03)
    settings = GRAPH_SETTINGS.model_copy(update={"apify_daily_cost_cap_usd": 2.0})
    r = run_ingest(world, hybrid(fake, apify), settings)
    assert r.skipped_budget == [] and r.items_new == 3 and r.cost_usd == pytest.approx(0.03)
    assert apify.calls == [("posts", ["andrea.braendli", "lara.stalder7"], pytest.approx(2.5 * per_handle))]
    with Session(world) as s:
        led = s.query(m.CostLedger).order_by(m.CostLedger.id.desc()).first()
        assert (led.source, led.units, led.cost_usd) == ("apify", 2, pytest.approx(0.03))
        handles = {h.handle: h for h in s.query(m.Handle).filter(m.Handle.platform == "instagram")}
        # what Graph said is remembered for the next run
        assert handles["testa.beispiel"].profile["graph_ok"] is True
        assert handles["andrea.braendli"].profile["graph_ok"] is False
        assert handles["testa.beispiel"].last_scraped_at is not None


def test_ingest_without_apify_budget_still_reads_graph(world):
    set_profiles(world, {})
    with Session(world) as s:
        costs.record(s, "apify", 1, 2.0, "cap used up")
        s.commit()
    fake = FakeGraph({"testa.beispiel": [[media(1, 10)]]})
    apify = FakeApify()
    settings = GRAPH_SETTINGS.model_copy(update={"apify_daily_cost_cap_usd": 2.0})
    r = run_ingest(world, hybrid(fake, apify), settings, refresh_profiles=False)
    assert apify.calls == [] and r.items_new == 1
    assert sorted(r.skipped_budget) == ["andrea.braendli", "lara.stalder7"]
    with Session(world) as s:
        h = s.query(m.Handle).filter(m.Handle.handle == "andrea.braendli").one()
        assert h.last_scraped_at is None  # not read: next run starts from the old point


def test_ingest_profiles_charge_only_apify_part(world):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with Session(world) as s:
        for h in s.query(m.Handle).filter(m.Handle.platform == "instagram"):
            h.profile = {"fetched_at": old, "category": "Athlete", "verified_badge": True}
        s.commit()
    fake = FakeGraph({"testa.beispiel": [[media(1, 10)]]})
    apify = FakeApify(cost=0.0)
    run_ingest(world, hybrid(fake, apify), GRAPH_SETTINGS)
    assert apify.calls[-1] == ("profiles", ["andrea.braendli", "lara.stalder7"], None)
    with Session(world) as s:
        led = s.query(m.CostLedger).filter(m.CostLedger.note == "profiles").one()
        assert led.units == 2
        testa = s.query(m.Handle).filter(m.Handle.handle == "testa.beispiel").one()
        # Graph profile: new numbers, but category and badge from the earlier Apify profile stay
        assert testa.profile["followers"] == 1234 and testa.profile["category"] == "Athlete"
        assert testa.profile["verified_badge"] is True and testa.profile["graph_ok"] is True


def test_ingest_mentions_go_to_apify(world):
    set_profiles(world, {"testa.beispiel": True})
    fake = FakeGraph({"testa.beispiel": [[media(1, 10)]]})
    apify = FakeApify(cost=0.01)
    run_ingest(world, hybrid(fake, apify), GRAPH_SETTINGS, mentions=True, refresh_profiles=False)
    assert fake.calls == [] and [c[0] for c in apify.calls] == ["mentions"]


def test_team_account_via_graph_costs_nothing(world):
    fake = FakeGraph({"swissicehockeywomen": [[media(1, 10, owner="swissicehockeywomen")]]})
    apify = FakeApify()
    h = hybrid(fake, apify)
    h.set_graph_hints({"swissicehockeywomen": True})
    with Session(world) as s:
        costs.record(s, "apify", 1, 2.0, "cap used up")
        r = ingest_instagram_teams(build_context(s, GRAPH_SETTINGS), h, ["swissicehockeywomen"])
        s.commit()
    assert apify.calls == [] and r.team_posts_new == 1 and r.skipped_budget == [] and r.cost_usd == 0
