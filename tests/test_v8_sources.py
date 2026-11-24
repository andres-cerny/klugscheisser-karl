"""v8 phase 2: handle checks (Jev on the profile, Facebook pages), media policy, Brave options."""

import httpx
import respx
from sqlalchemy.orm import Session

from klugscheisser import tiers
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles import check
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.interfaces import MediaRef, WebResult
from klugscheisser.media.policy import allowed_media
from klugscheisser.seed.loader import load_all_seeds


class FakeJev:
    def __init__(self, p: float):
        self.p, self.calls = p, []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return {
            k: {"noul": self.p >= 0.5, "probabilities": {"true": self.p, "false": 1 - self.p}}
            for k in questions
        }


def _with_handle(s, name="Lara Stalder", handle="lara.test"):
    a = repo.resolve_athlete(s, name)
    h = add_handle_candidate(
        s, a, "instagram", handle, {"kind": "profile_bio", "detail": "Bio nennt Eishockey"}
    )
    from klugscheisser.handles.service import add_evidence

    add_evidence(h, {"kind": "profile_name", "detail": "Profilname"})
    h.profile = {"full_name": name, "biography": "EVZ 🇨🇭 hockey", "followers": 900}
    s.flush()
    return a, h


def test_jev_profile_check_verifies_or_demotes(db):
    with Session(db) as s:
        load_all_seeds(s)
        a, h = _with_handle(s)
        assert h.status == "wahrscheinlich"
        report = check.CheckReport()
        check.check_instagram(s, FakeJev(0.95), [a.id], report)
        assert h.status == "verified" and report.verified == 1
        b, h2 = _with_handle(s, "Alina Müller", "not.alina")
        check.check_instagram(s, FakeJev(0.05), [b.id], report)
        assert h2.status == "candidate" and report.demoted == 1 and not h2.usable


def test_facebook_page_only_own_pages(db):
    assert check.facebook_page("https://www.facebook.com/larastalder7/") == "larastalder7"
    assert check.facebook_page("https://www.facebook.com/brynasif/photos/abc") is None
    assert (
        check.facebook_page("https://de-de.facebook.com/people/Lara-Stalder/100012/")
        == "people/Lara-Stalder/100012"
    )

    class FakeWeb:
        def search(self, q, limit=5, **kw):
            assert kw.get("keep_social") and q.startswith("site:facebook.com")
            return [
                WebResult("https://www.facebook.com/evzug/posts/1", "EVZ – Lara Stalder trifft", ""),
                WebResult(
                    "https://www.facebook.com/larastalder7/", "Lara Stalder | Facebook", "Eishockey EVZ"
                ),
            ]

    with Session(db) as s:
        load_all_seeds(s)
        a = repo.resolve_athlete(s, "Lara Stalder")
        report = check.CheckReport()
        check.find_facebook(s, FakeJev(0.9), FakeWeb(), [a.id], report)
        s.refresh(a)
        fb = [h for h in a.handles if h.platform == "facebook"]
        assert report.facebook_found == 1 and fb[0].handle == "larastalder7" and fb[0].usable


def _post(s, a, kind="athlete_post", source_kind=None, relevant=True, caption="Training heute"):
    item = m.Item(
        source_type=kind,
        platform="instagram",
        url=f"https://instagram.com/p/{caption[:5]}{a.id}{relevant}",
        content_hash=caption,
        text=caption,
        source_kind=source_kind,
    )
    s.add(item)
    s.flush()
    s.add(m.ItemAthlete(item_id=item.id, athlete_id=a.id))
    s.add(
        m.Classification(
            item_id=item.id,
            relevant_p=0.9 if relevant else 0.05,
            relevant=relevant,
            category="other",
            sensitive_p=0.0,
            vorsicht=False,
            newsworthiness=2,
            classifier="jev",
            classifier_version="t",
        )
    )
    s.flush()
    s.refresh(item)
    return item


def test_media_policy_by_relevance_fan_tier_and_interview(db):
    img = [MediaRef("image", "https://cdn/x.jpg")]
    reel = [MediaRef("image", "https://cdn/c.jpg"), MediaRef("video", "https://cdn/v.mp4", 40)]
    with Session(db) as s:
        load_all_seeds(s)
        tiers.recompute(s)
        t1 = repo.resolve_athlete(s, "Lara Stalder")
        t2 = repo.resolve_athlete(s, "John Wroblewski")
        t3 = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Testa Drei", nationality="NOR")
        assert allowed_media(s, _post(s, t1, relevant=False), img) == []  # Jev: not relevant
        assert allowed_media(s, _post(s, t1, source_kind="fan", caption="Fan edit"), img) == []
        assert allowed_media(s, _post(s, t2, caption="Bild"), img) == img
        assert allowed_media(s, _post(s, t2, caption="Reel"), reel) == []  # tier 2: no reels
        assert allowed_media(s, _post(s, t1, caption="Training clip"), reel) == []  # no interview words
        jev = FakeJev(0.9)
        interview = _post(s, t1, caption="Interview: Lara erzählt über ihre Saison")
        assert allowed_media(s, interview, reel, jev) == reel and jev.calls
        assert allowed_media(s, _post(s, t3, caption="Reel drei"), reel) == reel  # tier 3 unchanged


@respx.mock
def test_brave_news_endpoint_country_and_social():
    from klugscheisser.web_search.brave import BraveSearch

    route = respx.get("https://api.search.brave.com/res/v1/news/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://www.instagram.com/p/x",
                        "title": "IG",
                        "description": "",
                        "page_age": "2025-01-02T00:00:00",
                    },
                    {
                        "url": "https://isport.blesk.cz/a",
                        "title": "Rozhovor",
                        "description": "d",
                        "page_age": "2022-03-13T10:00:00",
                    },
                ]
            },
        )
    )
    settings = Settings(_env_file=None, brave_api_key="k", brave_daily_cap=10)

    class Ledger:  # no database: cost recording is a no-op
        def scalar(self, *a, **k):
            return 0

        def add(self, *a):
            pass

        def flush(self):
            pass

    web = BraveSearch(settings, session=Ledger())
    out = web.search('"Klára Peslarová" rozhovor', limit=5, endpoint="news", country="CZ", search_lang="cs")
    params = route.calls[0].request.url.params
    assert "country" not in params and params["search_lang"] == "cs"  # CZ is not accepted by Brave
    assert [r.url for r in out] == ["https://isport.blesk.cz/a"] and out[0].page_age.startswith("2022")
    out = web.search("x", limit=5, endpoint="news", keep_social=True)
    assert len(out) == 2


def test_surnames_and_identity_on_news(db):
    from klugscheisser.classify.jev import QUESTIONS
    from klugscheisser.collectors.rss import Article
    from klugscheisser.domain.matcher import AthleteMatcher
    from klugscheisser.ingest.news import confirmed_links
    from klugscheisser.ingest.runner import build_context

    assert "Privatleben" in QUESTIONS["relevant"]["criteria"]["true"]  # boulevard stories count as relevant
    with Session(db) as s:
        load_all_seeds(s)
        tiers.recompute(s)
        matcher = AthleteMatcher(s)
        index = matcher.surname_index()
        stalder = repo.resolve_athlete(s, "Lara Stalder")
        assert index.get("stalder") == stalder.id and "muller" not in index
        links = matcher.from_surnames("Stalder traf im Powerplay zum 1:2.")
        assert [(x.athlete_id, x.kind) for x in links] == [(stalder.id, "surname")]
        ctx = build_context(s, Settings(_env_file=None))
        art = Article(
            feed="Blick",
            url="https://blick.ch/a",
            title="FC Luzern holt Stalder",
            summary="Tim Stalder unterschreibt",
            published_at=None,
            source_type="other_media",
            source_kind="boulevard",
        )
        ctx.jev_client = FakeJev(0.03)
        assert confirmed_links(ctx, art, links) == []  # namesake: dropped, the article itself stays
        ctx.jev_client = FakeJev(0.85)
        assert confirmed_links(ctx, art, links) == links
