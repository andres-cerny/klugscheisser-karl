from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.classify.base import ClassifyInput
from klugscheisser.classify.jev import JevClassifier, JevClient, noul_probability, score_answer
from klugscheisser.classify.router import ClassifierRouter
from klugscheisser.classify.rules import RuleBasedClassifier
from klugscheisser.collectors.instagram.base import parse_apify_post
from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.labels import source_label
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest import costs
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.runner import build_context, purge_irrelevant
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/instagram")
SETTINGS = Settings(_env_file=None, instagram_source="fixture", llm_provider="auto")


@pytest.fixture
def world(db):
    """Seeds + fictional athlete with a Wikidata-only handle + real players' handles."""
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        testa = repo.upsert_athlete(s, sport, "Testa Beispiel", nationality="SUI", position="F")
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata", "detail": "test"})
        for name, handle in [
            ("Alina Müller", "alinamueller_"),
            ("Andrea Brändli", "andrea.braendli"),
            ("Lara Stalder", "lara.stalder7"),
        ]:
            add_handle_candidate(
                s, repo.resolve_athlete(s, name), "instagram", handle, {"kind": "wikidata", "detail": "test"}
            )
        s.commit()
    return db


def run(db, names, source=None, settings=SETTINGS):
    with Session(db) as s:
        athletes = [repo.resolve_athlete(s, n) for n in names]
        ctx = build_context(s, settings)
        report = ingest_instagram(ctx, source or FixtureInstagramSource(FIX), athletes)
        s.commit()
    return report


def item_by_url(s, url):
    return s.scalar(select(m.Item).where(m.Item.url == url))


def test_parse_drops_comments_and_media_urls():
    post = parse_apify_post(
        {
            "id": "1",
            "url": "https://www.instagram.com/p/X/",
            "ownerUsername": "A",
            "caption": "hi",
            "displayUrl": "https://cdn/x.jpg",
            "latestComments": [{"text": "x"}],
            "type": "Image",
        }
    )
    assert "displayUrl" not in post.raw and "latestComments" not in post.raw
    assert post.media[0].url == "https://cdn/x.jpg" and post.media[0].kind == "image"
    assert post.owner_username == "a"


def test_generic_alt_is_ignored():
    post = parse_apify_post(
        {"id": "1", "url": "u", "ownerUsername": "a", "alt": "Photo by X on May 1, 2026."}
    )
    assert post.informative_alt is None


def test_ingest_end_to_end_and_idempotent(world):
    r1 = run(world, ["Testa Beispiel", "Alina Müller", "Andrea Brändli", "Lara Stalder"])
    assert r1.items_new >= 15 and not r1.errors
    r2 = run(world, ["Testa Beispiel", "Alina Müller", "Andrea Brändli", "Lara Stalder"])
    assert r2.items_new == 0
    with Session(world) as s:
        assert s.scalar(select(func.count()).select_from(m.Embedding)) == s.scalar(
            select(func.count()).select_from(m.Item)
        )
        srf = item_by_url(s, "https://www.instagram.com/p/DcB4G44s6jK/")
        assert srf.source_type == "srf" and srf.source_name == "@srfsport"
        assert [ia.athlete.full_name for ia in srf.athletes] == ["Andrea Brändli"]
        assert srf.classification.category == "team_coach" and srf.classification.storyline == "transfer"
        assert "displayUrl" not in (srf.raw or {})


def test_injury_post_is_flagged_vorsicht(world):
    run(world, ["Testa Beispiel"])
    with Session(world) as s:
        injury = item_by_url(s, "https://www.instagram.com/p/SYN1/")
        assert injury.source_type == "athlete_post"
        assert injury.classification.category == "injury"
        assert injury.classification.vorsicht and injury.classification.relevant


def test_sponsor_and_informative_alt(world):
    run(world, ["Testa Beispiel"])
    with Session(world) as s:
        ad = item_by_url(s, "https://www.instagram.com/p/SYN3/")
        assert ad.source_type == "tagged_post" and ad.classification.category == "sponsor"
        win = item_by_url(s, "https://www.instagram.com/p/SYN2/")
        assert win.image_description.startswith("May be an image")
        assert win.classification.category == "result"


def test_profile_signals_verify_handle(world):
    run(world, ["Testa Beispiel"])
    with Session(world) as s:
        h = s.scalar(select(m.Handle).where(m.Handle.handle == "testa.beispiel"))
        assert h.status == "verified" and h.profile["full_name"] == "Testa Beispiel"


def test_edited_caption_updates_item(world, tmp_path):
    run(world, ["Testa Beispiel"])
    edited = (FIX / "posts_synthetic.json").read_text().replace("3:1 Sieg", "3:1-Sieg (korrigiert)")
    (tmp_path / "posts.json").write_text(edited)
    with Session(world) as s:  # force a re-fetch window
        s.execute(m.Handle.__table__.update().values(last_scraped_at=None))
        s.commit()
    r = run(world, ["Testa Beispiel"], source=FixtureInstagramSource(tmp_path))
    assert r.items_changed == 1
    with Session(world) as s:
        win = item_by_url(s, "https://www.instagram.com/p/SYN2/")
        assert "korrigiert" in win.text and "3:1 Sieg" in win.raw["previous_texts"][0]


class PaidFixture(FixtureInstagramSource):
    name = "apify"

    def fetch_posts(self, handles, since, limit=12, max_usd=None):
        result = super().fetch_posts(handles, since, limit)
        result.cost_usd = 0.01
        return result


def test_cost_cap_skips_paid_fetch(world):
    with Session(world) as s:
        costs.record(s, "apify", 100, 1.99, "earlier today")
        s.commit()
    capped = Settings(_env_file=None, apify_daily_cost_cap_usd=2.0)
    r = run(world, ["Testa Beispiel"], source=PaidFixture(FIX), settings=capped)
    assert r.skipped_budget == ["testa.beispiel"] and r.items_new == 0


def test_cost_is_recorded(world):
    r = run(world, ["Testa Beispiel"], source=PaidFixture(FIX))
    assert r.cost_usd == pytest.approx(0.01)
    with Session(world) as s:
        assert costs.spent_today(s, "apify") >= 0.01


def test_retention_purges_only_old_irrelevant(world):
    run(world, ["Testa Beispiel", "Lara Stalder"])
    with Session(world) as s:
        item = s.scalars(select(m.Item)).first()
        item.classification.relevant, item.classification.vorsicht = False, False
        item.scraped_at = datetime.now(UTC) - timedelta(days=40)
        s.commit()
        assert purge_irrelevant(s, 30) == 1
        s.commit()
        s.refresh(item)
        assert item.text is None and item.text_purged_at is not None


def test_label_format():
    assert (
        source_label("instagram", "@srfsport", datetime(2026, 8, 14, 17, tzinfo=UTC))
        == "[Instagram @srfsport, 14.08.]"
    )
    assert source_label("rss", "SRF", datetime(2025, 11, 28, 9, tzinfo=UTC)) == "[SRF, 28.11.2025]"


# --- Jev adapter against recorded/mocked HTTP responses ---

JEV_RESPONSE = {  # format per api.typesafe.ai/openapi.json (2026-10)
    "model": "jev-latest",
    "answers": {
        "relevant": {"type": "noul", "noul": 0.93},
        "category": {
            "type": "choice",
            "choice": "injury",
            "confidence": 0.88,
            "probabilities": {"injury": 0.88, "training": 0.05, "other": 0.07},
        },
        "sensitive": {"type": "noul", "noul": 0.91},
        "newsworthiness": {"type": "score", "score": 3.48, "confidence": 0.7},
        "storyline": {
            "type": "choice",
            "choice": "none",
            "confidence": 0.8,
            "probabilities": {"none": 0.8, "comeback": 0.2},
        },
        "mood": {
            "type": "choice",
            "choice": "trauer",
            "confidence": 0.7,
            "probabilities": {"trauer": 0.7, "verletzung": 0.25, "neutral": 0.05},
        },
    },
    "usage": {"input_tokens": 420, "output_tokens": 12},
}


@respx.mock
def test_jev_classifier_parses_answers():
    from klugscheisser.classify.base import TOPICS

    topics = {f"topic_{k}": {"noul": k == "verletzung", "confidence": 0.9} for k in TOPICS}  # v5 topic tags
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(
        return_value=httpx.Response(
            200, json={**JEV_RESPONSE, "answers": {**JEV_RESPONSE["answers"], **topics}}
        )
    )
    settings = Settings(_env_file=None, typesafe_api_key="sk-test")
    result = JevClassifier(settings).classify(
        ClassifyInput(text="Kreuzband gerissen", source_type="athlete_post")
    )
    body = route.calls.last.request.read().decode()
    assert (
        '"model":"jev-latest"' in body.replace(" ", "")
        and "Bearer sk-test" in route.calls.last.request.headers["authorization"]
    )
    assert result.category == "injury" and result.relevant_p == 0.93 and result.sensitive_p == 0.91
    assert (
        result.newsworthiness == 4 and result.storyline is None and result.mood == "trauer"
    )  # E[level]=3.48 -> 4 of 5
    assert result.topics["verletzung"] == 0.9 and result.topics["studium"] == pytest.approx(0.1)
    assert result.main_persons is None  # one person: no "Hauptperson" questions


def test_jev_answer_shapes():
    assert noul_probability({"noul": True, "confidence": 0.8}) == 0.8
    assert noul_probability({"noul": False, "confidence": 0.8}) == pytest.approx(0.2)
    assert score_answer({"score": 2}, 5) == 2


@respx.mock
def test_router_falls_back_and_breaks_circuit_on_auth_error():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(return_value=httpx.Response(401, json={}))
    settings = Settings(_env_file=None, typesafe_api_key="sk-bad")
    router = ClassifierRouter(JevClassifier(settings, JevClient(settings)), RuleBasedClassifier(), settings)
    for _ in range(3):
        result = router.classify(ClassifyInput(text="3:1 Sieg, zwei Tore 🏒", source_type="athlete_post"))
        assert result.category == "result"
    assert route.call_count == 1
