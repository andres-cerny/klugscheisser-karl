"""Phase D: background image/reel analysis (offline: FakeMediaAnalyzer, fake Gemini client, respx)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from google.genai import errors
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.matcher import Link
from klugscheisser.embed.providers import GeminiEmbedder
from klugscheisser.ingest import costs
from klugscheisser.ingest.runner import build_context
from klugscheisser.ingest.store import ItemDraft, store_item
from klugscheisser.interfaces import MediaRef
from klugscheisser.media.analyzer import (
    TOO_LONG,
    ClaudeMediaAnalyzer,
    FakeMediaAnalyzer,
    GeminiMediaAnalyzer,
    MediaDownloadError,
    MediaRateLimited,
    plan_media,
)
from klugscheisser.media.worker import MAX_ATTEMPTS, process_media_queue, purge_stale
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, media_min_interval_s=0)  # no keys: rules classifier, fake embedder
IMG = "https://cdn.example/p1.jpg"
IMG2 = "https://cdn.example/p2.jpg"
VID = "https://cdn.example/reel.mp4"
CRUTCHES = "Eine Spielerin im Schweizer Trikot steht mit Krücken und verletztem Knie vor einem Spital."


def add_post(s: Session, url: str, caption: str, media: list[MediaRef]) -> m.Item:
    ctx = replace(build_context(s, SETTINGS), media_analysis=True)
    athlete = repo.upsert_athlete(
        s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI"
    )
    draft = ItemDraft(
        source_type="athlete_post",
        platform="instagram",
        url=url,
        text=caption,
        source_name="@testa.beispiel",
        author="testa.beispiel",
        published_at=datetime.now(UTC),
        media=media,
        media_type="image",
        source_kind="self",
    )
    item = store_item(ctx, draft, [Link(athlete_id=athlete.id, kind="author", confidence=1.0)]).item
    s.commit()
    return item


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def queue_count(s: Session) -> int:
    return s.scalar(select(func.count()).select_from(m.MediaQueue))


def test_queue_drain_updates_item_and_reclassifies(world):
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/MEDIA1/", "🤍", [MediaRef("image", IMG)])
        assert item.media_pending and queue_count(s) == 1
        assert item.classification.vorsicht is False  # an emoji alone says nothing
        emb_before = list(s.scalar(select(m.Embedding.vector).where(m.Embedding.item_id == item.id)))

        fake = FakeMediaAnalyzer(SETTINGS, descriptions={IMG: CRUTCHES}, visible_text="Comeback 2027")
        report = process_media_queue(s, SETTINGS, fake)

        assert report.analyzed == 1 and not report.errors
        s.refresh(item)
        assert "Krücken" in item.image_description
        assert "Sichtbarer Text: «Comeback 2027»" in item.image_description
        assert item.raw["media"] == {
            "spoken_quotes": [],
            "visible_text": "Comeback 2027",
            "model": "fake-media-1",
            "analyzed": ["image"],
        }
        assert item.media_pending is False
        assert queue_count(s) == 0
        assert item.classification.vorsicht is True  # picked up from the image
        assert item.classification.category == "injury"
        emb_after = list(s.scalar(select(m.Embedding.vector).where(m.Embedding.item_id == item.id)))
        assert emb_after != emb_before
        assert costs.spent_today(s, "llm:fake") == 0.0
        units = s.scalar(select(func.sum(m.CostLedger.units)).where(m.CostLedger.source == "llm:fake"))
        assert units == 1


def test_spoken_quotes_are_stored(world):
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/MEDIA2/", "🏒", [MediaRef("video", VID, 30.0)])
        fake = FakeMediaAnalyzer(SETTINGS, quotes={VID: ["Mir händ hüt guet gspilt."]})
        process_media_queue(s, SETTINGS, fake)
        s.refresh(item)
        assert item.raw["media"]["spoken_quotes"] == ["Mir händ hüt guet gspilt."]
        assert item.raw["media"]["analyzed"] == ["video"]


def test_failures_count_attempts_then_give_up(world):
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/MEDIA3/", "🤍", [MediaRef("image", IMG)])
        fake = FakeMediaAnalyzer(SETTINGS, fail=RuntimeError("boom"))
        for n in range(1, MAX_ATTEMPTS):
            r = process_media_queue(s, SETTINGS, fake)
            assert r.failed == 1
            assert s.get(m.MediaQueue, item.id).attempts == n
            assert s.get(m.Item, item.id).media_pending is True
        r = process_media_queue(s, SETTINGS, fake)
        assert r.dropped == 1 and queue_count(s) == 0
        item = s.get(m.Item, item.id)
        assert item.media_pending is False
        assert item.raw["media"] == {"error": "boom"}


def test_rate_limit_does_not_burn_attempts(world):
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/MEDIA4/", "🤍", [MediaRef("image", IMG)])
        r = process_media_queue(s, SETTINGS, FakeMediaAnalyzer(SETTINGS, fail=MediaRateLimited("429")))
        assert r.rate_limited and r.analyzed == 0
        assert s.get(m.MediaQueue, item.id).attempts == 0


def test_daily_limit(world):
    settings = Settings(_env_file=None, media_min_interval_s=0, media_daily_limit=2)
    with Session(world) as s:
        add_post(s, "https://www.instagram.com/p/MEDIA5/", "🤍", [MediaRef("image", IMG)])
        costs.record(s, "llm:fake", 2, 0.0, "earlier analyses")
        s.commit()
        fake = FakeMediaAnalyzer(settings)
        r = process_media_queue(s, settings, fake)
        assert r.limit_reached and r.analyzed == 0 and not fake.calls
        assert queue_count(s) == 1


def test_purge_stale(world):
    with Session(world) as s:
        old = add_post(s, "https://www.instagram.com/p/MEDIA6/", "🤍", [MediaRef("image", IMG)])
        new = add_post(s, "https://www.instagram.com/p/MEDIA7/", "🤍", [MediaRef("image", IMG2)])
        s.get(m.MediaQueue, old.id).created_at = datetime.now(UTC) - timedelta(hours=49)
        s.commit()
        assert purge_stale(s, hours=48) == 1
        s.commit()
        assert s.get(m.MediaQueue, old.id) is None and s.get(m.MediaQueue, new.id) is not None
        assert s.get(m.Item, old.id).media_pending is False
        assert s.get(m.Item, new.id).media_pending is True


def test_video_length_rule():
    media = [
        MediaRef("video", VID, 181.0),
        MediaRef("video", "https://cdn.example/short.mp4", 60.0),
        *[MediaRef("image", f"https://cdn.example/{i}.jpg") for i in range(7)],
    ]
    chosen, notes = plan_media(media, SETTINGS)
    assert [r.kind for r in chosen] == ["video"] + ["image"] * 5
    assert notes == [f"video: {TOO_LONG}"]


def test_long_reel_only_is_not_sent(world):
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/MEDIA8/", "🏒", [MediaRef("video", VID, 600.0)])
        fake = FakeMediaAnalyzer(SETTINGS)
        r = process_media_queue(s, SETTINGS, fake)
        s.refresh(item)
        assert r.analyzed == 1 and item.media_pending is False
        assert item.raw["media"]["analyzed"] == ["video: nicht analysiert (zu lang)"]
        assert item.image_description is None
        assert s.scalar(select(func.count()).select_from(m.CostLedger)) == 0  # no model call


# --- Gemini analyzer with a fake client -----------------------------------------------------------


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out)


OK_JSON = (
    '{"description": "Zwei Spielerinnen jubeln.", "spoken_quotes": ["We did it!"], "visible_text": null}'
)


def gemini(outcomes, **kw):
    settings = Settings(_env_file=None, gemini_model="m1", media_fallback_models=["m2"], **kw)
    client = SimpleNamespace(models=FakeModels(outcomes))
    sleeps = []
    return GeminiMediaAnalyzer(settings, client=client, http=httpx.Client(), sleep=sleeps.append), sleeps


def busy():
    return errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})


def daily_quota():
    return errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "message": "quota",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "39885s"},
                ],
            }
        },
    )


@respx.mock
def test_gemini_sends_images_and_short_reel_in_one_request():
    respx.get(IMG).mock(
        return_value=httpx.Response(200, content=b"img1", headers={"content-type": "image/jpeg"})
    )
    respx.get(IMG2).mock(
        return_value=httpx.Response(200, content=b"img2", headers={"content-type": "image/png"})
    )
    respx.get(VID).mock(
        return_value=httpx.Response(200, content=b"vid", headers={"content-type": "video/mp4"})
    )
    long_route = respx.get("https://cdn.example/long.mp4").mock(return_value=httpx.Response(200))
    a, _ = gemini([OK_JSON])
    out = a.analyze(
        [
            MediaRef("image", IMG),
            MediaRef("image", IMG2),
            MediaRef("video", VID, 45.0),
            MediaRef("video", "https://cdn.example/long.mp4", 300.0),
        ],
        "🥳",
    )
    assert len(a.client.models.calls) == 1
    model, contents = a.client.models.calls[0]
    assert model == "m1"
    mimes = [p.inline_data.mime_type for p in contents[:-1]]
    assert mimes == ["image/jpeg", "image/png", "video/mp4"]
    assert "🥳" in contents[-1]
    assert not long_route.called
    assert out.description == "Zwei Spielerinnen jubeln."
    assert out.spoken_quotes == ["We did it!"] and out.visible_text is None
    assert out.analyzed == ["image", "image", "video", "video: nicht analysiert (zu lang)"]


@respx.mock
def test_gemini_retries_with_backoff_then_falls_back():
    respx.get(IMG).mock(
        return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})
    )
    a, sleeps = gemini([busy(), busy(), OK_JSON])
    assert a.analyze([MediaRef("image", IMG)], "").model == "m1"
    assert sleeps == [4.0, 8.0]

    a, sleeps = gemini([busy(), busy(), busy(), OK_JSON])
    assert a.analyze([MediaRef("image", IMG)], "").model == "m2"  # m1 overloaded -> next model

    a, sleeps = gemini([daily_quota(), OK_JSON])
    assert a.analyze([MediaRef("image", IMG)], "").model == "m2"
    assert sleeps == [] and a.available_models() == ["m2"]  # m1 skipped until its quota resets

    a, _ = gemini([busy()] * 6)
    with pytest.raises(MediaRateLimited):
        a.analyze([MediaRef("image", IMG)], "")


@respx.mock
def test_gemini_expired_cdn_link_and_oversized_video():
    respx.get(IMG).mock(return_value=httpx.Response(403))
    a, _ = gemini([])
    with pytest.raises(MediaDownloadError):
        a.analyze([MediaRef("image", IMG)], "")

    respx.get(VID).mock(
        return_value=httpx.Response(200, content=b"v" * 2000, headers={"content-type": "video/mp4"})
    )
    a, _ = gemini([], media_max_upload_mb=0.001)
    out = a.analyze([MediaRef("video", VID, 30.0)], "")
    assert out.analyzed == ["video: nicht analysiert (zu gross)"] and not a.client.models.calls


@respx.mock
def test_claude_analyzer_images_only():
    respx.get(IMG).mock(
        return_value=httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})
    )
    sent = {}

    class FakeProvider:
        model = "claude-test"

        def __init__(self):
            self.last_usage = {"input": 1200, "output": 80}

        def message(self, messages, max_tokens, schema=None, system=None):
            sent.update(messages=messages, schema=schema)
            return OK_JSON

    a = ClaudeMediaAnalyzer(SETTINGS, provider=FakeProvider(), http=httpx.Client())
    out = a.analyze([MediaRef("image", IMG), MediaRef("video", VID, 20.0)], "")
    assert [c["type"] for c in sent["messages"][0]["content"]] == ["image", "text"]
    assert out.analyzed == ["image", "video: nicht analysiert (Video vom Modell nicht unterstützt)"]
    assert sent["schema"] is not None and (out.input_tokens, out.output_tokens) == (1200, 80)


def test_gemini_embedder_truncates_and_normalises():
    calls = []

    def embed_content(model, contents, config):
        calls.append((model, config.task_type, config.output_dimensionality))
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=[3.0, 4.0] + [0.0] * 382) for _ in contents]
        )

    client = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
    e = GeminiEmbedder(Settings(_env_file=None), client=client)
    assert e.model == "gemini-embedding-001-384"
    v = e.embed_documents(["a", "b"])
    assert len(v) == 2 and v[0][:2] == [0.6, 0.8]
    e.embed_query("q")
    assert calls == [
        ("gemini-embedding-001", "RETRIEVAL_DOCUMENT", 384),
        ("gemini-embedding-001", "RETRIEVAL_QUERY", 384),
    ]


# --- live ----------------------------------------------------------------------------------------

WIKI_IMG = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1d/"
    "Switzerland_women%27s_national_ice_hockey_team_after_winning_bronze_at_2014_Winter_Olympics.JPG/"
    "1280px-Switzerland_women%27s_national_ice_hockey_team_after_winning_bronze_at_2014_Winter_Olympics.JPG"
)


@pytest.mark.network
def test_live_gemini_describes_ice_hockey_photo():
    from dotenv import dotenv_values  # conftest blanks key env vars; read the real key from .env

    key = dotenv_values(".env").get("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY not set in .env")
    settings = Settings(_env_file=None, gemini_api_key=key)
    out = GeminiMediaAnalyzer(settings).analyze([MediaRef("image", WIKI_IMG)], "🥉🇨🇭")
    print(out)
    assert out.description and out.analyzed == ["image"]
    assert any(w in out.description.lower() for w in ("eishockey", "trikot", "eis", "spielerinnen"))


def test_post_without_description_is_queued_again_with_fresh_links(world):
    """v15: a stored post whose media were never analysed (link expired) is queued again when it comes in
    again; a described post is not."""
    with Session(world) as s:
        item = add_post(s, "https://www.instagram.com/p/AGAIN1/", "🤍", [MediaRef("video", VID, 20.0)])
        s.delete(s.get(m.MediaQueue, item.id))
        item.media_pending = False
        s.commit()
        assert queue_count(s) == 0
        add_post(s, "https://www.instagram.com/p/AGAIN1/", "🤍", [MediaRef("video", VID, 20.0)])
        s.commit()
        assert queue_count(s) == 1
        s.delete(s.get(m.MediaQueue, item.id))
        item = s.get(m.Item, item.id)
        item.media_pending, item.image_description = False, "Ein Jubelbild."
        s.commit()
        add_post(s, "https://www.instagram.com/p/AGAIN1/", "🤍", [MediaRef("video", VID, 20.0)])
        s.commit()
        assert queue_count(s) == 0
