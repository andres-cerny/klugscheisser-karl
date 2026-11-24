from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.classify.base import ClassificationResult, ClassifyInput
from klugscheisser.classify.router import ClassifierRouter
from klugscheisser.classify.rules import RuleBasedClassifier
from klugscheisser.collectors.instagram.base import media_refs
from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.runner import build_context
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import zeile_prefix
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/instagram")
SETTINGS = Settings(_env_file=None)


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        testa = repo.upsert_athlete(
            s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI"
        )
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        for name, handle in [("Andrea Brändli", "andrea.braendli"), ("Lara Stalder", "lara.stalder7")]:
            add_handle_candidate(s, repo.resolve_athlete(s, name), "instagram", handle, {"kind": "wikidata"})
        athletes = [repo.resolve_athlete(s, n) for n in ("Testa Beispiel", "Andrea Brändli", "Lara Stalder")]
        ingest_instagram(build_context(s, SETTINGS), FixtureInstagramSource(FIX), athletes)
        s.commit()
    return db


def kind_of(s, url):
    return s.scalar(select(m.Item.source_kind).where(m.Item.url == url))


def test_source_kinds_are_decided(world):
    with Session(world) as s:
        assert kind_of(s, "https://www.instagram.com/p/SYN1/") == "self"
        assert kind_of(s, "https://www.instagram.com/p/DcB4G44s6jK/") == "srf"  # @srfsport
        assert kind_of(s, "https://www.instagram.com/p/DcBYRfmkTQ6/") == "official"  # @pwhl__detroit
        assert kind_of(s, "https://www.instagram.com/p/SYN3/") == "other"  # unknown brand, Jev off in tests


def test_line_prefix_and_meaning():
    assert zeile_prefix("boulevard", "liebe") == "🍿 (Gossip) ❤️ (Liebe/Privatleben)"
    assert zeile_prefix("self", "neutral") == "📸 (von ihr selbst)"
    assert zeile_prefix(None, None) == "🔗 (andere Quelle)"


def test_datenstand_is_first_and_items_carry_prefix(world):
    with Session(world) as s:
        out = t.get_athlete_posts(s, "Testa Beispiel")
    assert next(iter(out)) == "datenstand"
    assert out["datenstand"].startswith("Instagram: heute")
    injury = next(p for p in out["posts"] if p["url"].endswith("/SYN1/"))
    assert injury["zeile_prefix"].startswith("📸 (von ihr selbst) 🤕")
    assert "datenstand" in out["hinweis"] and "zeile_prefix" in out["hinweis"]


def test_mood_is_stored(world):
    with Session(world) as s:
        win = s.scalar(select(m.Item).where(m.Item.url == "https://www.instagram.com/p/SYN2/"))
        assert win.classification.mood == "freude"


def test_vorsicht_category_shortcut_only_for_rules():
    s = Settings(_env_file=None, sensitive_threshold=0.4)

    class FakeJev:
        name, version = "jev", "x"

        def classify(self, item):
            return ClassificationResult(
                relevant_p=0.9, category="private_life", sensitive_p=0.05, newsworthiness=2, classifier="jev"
            )

    fan = ClassifyInput(text="Even the pros need a fan girl moment 🥹", source_type="tagged_post")
    assert ClassifierRouter(FakeJev(), None, s).classify(fan).vorsicht is False
    rules = ClassifierRouter(RuleBasedClassifier(), None, s).classify(
        ClassifyInput(text="Hochzeit am Wochenende 💍", source_type="athlete_post")
    )
    assert rules.category == "private_life" and rules.vorsicht is True


def test_media_refs_from_carousel_and_reel():
    carousel = {"childPosts": [{"displayUrl": f"https://cdn/{i}.jpg"} for i in range(8)]}
    assert [r.url for r in media_refs(carousel)] == [f"https://cdn/{i}.jpg" for i in range(5)]
    reel = {"videoUrl": "https://cdn/v.mp4", "videoDuration": 42.5, "displayUrl": "https://cdn/thumb.jpg"}
    assert media_refs(reel)[0].kind == "video" and media_refs(reel)[0].duration_s == 42.5


def test_media_is_queued_only_when_analysis_enabled(world):
    with Session(world) as s:
        assert s.scalar(select(m.MediaQueue)) is None  # fixtures carry no media URLs; and analysis is off


def test_vorsicht_threshold_matches_calibration():
    import yaml

    labels = yaml.safe_load(Path("tests/fixtures/vorsicht_labels.yaml").read_text())
    threshold = Settings(_env_file=None).sensitive_threshold
    wrong = [x["note"] for x in labels if (x["p"] >= threshold) != x["sensitive"]]
    assert not wrong, f"threshold {threshold} misclassifies: {wrong}"


def test_jev_has_no_text_second_opinion_but_outage_fallback(monkeypatch):
    from klugscheisser.classify.router import build_classifier
    from klugscheisser.llm.providers import FakeLlmProvider

    s = Settings(_env_file=None, typesafe_api_key="sk-test")
    router = build_classifier(s, FakeLlmProvider())
    assert router.fallback is None and router.outage is not None

    class Boom:
        name, version = "jev", "x"

        def classify(self, item):
            raise RuntimeError("Jev HTTP 500")

    router.primary = Boom()
    out = router.classify(ClassifyInput(text="3:1 Sieg 🏒", source_type="athlete_post"))
    assert out.classifier == "llm"  # fake LLM took over during the outage


def test_namesakes_are_disambiguated_by_jev(db):
    from klugscheisser.domain.matcher import AthleteMatcher

    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        ski = repo.get_sport(s, "ski_alpine_women", "Ski alpin Frauen")
        hockey = m.Athlete(
            sport_id=sport.id, full_name="Lea Muster", name_normalized="lea muster", role="athlete"
        )
        skier = m.Athlete(
            sport_id=ski.id, full_name="Lea Muster", name_normalized="lea muster", role="athlete"
        )
        s.add_all([hockey, skier])
        s.flush()
        repo.add_aliases(s, hockey, ["Lea Muster"])
        repo.add_aliases(s, skier, ["Lea Muster"])
        s.commit()
        text = "Lea Muster gewinnt die Abfahrt in Lake Louise"
        assert AthleteMatcher(s).from_text(text) == []  # without Jev: no guessing

        class FakeJev:
            def ask(self, state, questions):
                key = next(k for k, v in questions["wer"]["criteria"].items() if "Ski" in v)
                return {"wer": {"type": "choice", "choice": key, "confidence": 0.93}}

        links = AthleteMatcher(s, jev_client=FakeJev()).from_text(text)
        assert [link.athlete_id for link in links] == [skier.id]
