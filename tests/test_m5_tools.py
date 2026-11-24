from pathlib import Path

import httpx
import pytest
import respx
import yaml
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.collectors.rss import DEFAULT_FEEDS, RssCollector
from klugscheisser.config import Settings
from klugscheisser.db import repo
from klugscheisser.embed.providers import FakeEmbedder
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.news import ingest_news
from klugscheisser.ingest.runner import build_context
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.server import build_app
from klugscheisser.retrieval.search import FakeReranker, Filters, search
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures")
SRF_ONLY = DEFAULT_FEEDS[:1]  # other feeds are not mocked here
SETTINGS = Settings(_env_file=None)


@pytest.fixture(scope="module")
def data(engine):
    """Seeds + fixture Instagram + synthetic SRF news, ingested once for the module."""
    from sqlalchemy import text

    from klugscheisser.db.models import Base

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE "
                + ", ".join(t.name for t in Base.metadata.sorted_tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        testa = repo.upsert_athlete(s, sport, "Testa Beispiel", nationality="SUI", position="F")
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        wch = s.scalar(
            __import__("sqlalchemy")
            .select(__import__("klugscheisser.db.models", fromlist=["Event"]).Event)
            .where(__import__("klugscheisser.db.models", fromlist=["Event"]).Event.key == "wch-2026")
        )
        from klugscheisser.db.models import EventEntry, Team

        sui = s.scalar(__import__("sqlalchemy").select(Team).where(Team.key == "sui-women"))
        s.add(
            EventEntry(
                event_id=wch.id,
                athlete_id=testa.id,
                team_id=sui.id,
                role="F",
                list_type="provisional",
                source="Test",
            )
        )
        for name, handle in [
            ("Alina Müller", "alinamueller_"),
            ("Andrea Brändli", "andrea.braendli"),
            ("Lara Stalder", "lara.stalder7"),
        ]:
            add_handle_candidate(s, repo.resolve_athlete(s, name), "instagram", handle, {"kind": "wikidata"})
        ctx = build_context(s, SETTINGS)
        athletes = [
            repo.resolve_athlete(s, n)
            for n in ["Testa Beispiel", "Alina Müller", "Andrea Brändli", "Lara Stalder"]
        ]
        ingest_instagram(ctx, FixtureInstagramSource(FIX / "instagram"), athletes)
        with respx.mock:
            respx.get(DEFAULT_FEEDS[0].url).mock(
                return_value=httpx.Response(200, content=(FIX / "rss/srf_sport.xml").read_bytes())
            )
            respx.get(url__regex=r".*test-beispiel-trifft-doppelt").mock(
                return_value=httpx.Response(200, text=(FIX / "rss/article_beispiel.html").read_text())
            )
            ingest_news(build_context(s, SETTINGS), RssCollector(SRF_ONLY, delay_s=0))
        s.commit()
    return engine


def test_retrieval_quality_recall_at_3(data):
    questions = yaml.safe_load((FIX / "retrieval_questions.yaml").read_text())
    hits_at_3 = 0
    misses = []
    with Session(data) as s:
        for case in questions:
            urls = [
                h.item.url
                for h in search(s, FakeEmbedder(), FakeReranker(), case["q"], Filters(), top_n=3, min_score=0)
            ]
            if any(u in urls for u in case["expect"]):
                hits_at_3 += 1
            else:
                misses.append((case["q"], urls))
    recall = hits_at_3 / len(questions)
    assert recall >= 0.8, f"recall@3={recall:.2f}, misses={misses}"


def test_retrieval_quality_recall_at_3_shared_path(data):
    """exp/search-jev-path: the same labelled set through the shared path (no vectors, word-overlap scorer)."""
    from klugscheisser.retrieval.docs import find_documents

    questions = yaml.safe_load((FIX / "retrieval_questions.yaml").read_text())
    misses = []
    with Session(data) as s:
        for case in questions:
            hits = find_documents(
                s, SETTINGS, case["q"], Filters(), top_n=3, min_score=0, reranker=FakeReranker()
            )
            urls = [h.item.url for h in hits]
            if not any(u in urls for u in case["expect"]):
                misses.append((case["q"], urls))
    recall = 1 - len(misses) / len(questions)
    assert recall >= 0.8, f"recall@3={recall:.2f}, misses={misses}"


def test_get_event_marks_provisional_and_swiss_first(data):
    with Session(data) as s:
        out = t.get_event(s, "Frauen WM 2026")
    assert out["quelle"].startswith("klugscheisser_athlete") and "hinweis" in out
    assert "PROVISORISCH" in out["lineup_status"]
    sui = next(b for b in out["teams"] if b["schweiz"])
    assert (
        sui["kader_status"] == "provisorisch" and len(sui["spielerinnen"]) == 26
    )  # official WM list (25, SIHF 01.11.2026) + Testa, added as provisional: the team is provisional again
    testa = next(p for p in sui["spielerinnen"] if p["name"] == "Testa Beispiel")
    assert testa["vorsicht"] and testa["vorsicht"][0]["kategorie"] == "Verletzung/Gesundheit"
    assert "neueste_posts" not in testa and "instagram" not in testa  # compact roster by default
    usa = next(b for b in out["teams"] if b["team"] == "USA")
    # other teams show status only (players via get_team_roster); USA: official list (31.10.2026)
    assert usa["status"] == "offiziell" and usa["anzahl"] == 25 and "spielerinnen" not in usa


def test_every_item_is_self_citing(data):
    with Session(data) as s:
        out = t.get_athlete_posts(s, "Lara Stalder")
    assert out["posts"]
    for item in out["posts"]:
        assert item["url"].startswith("https://www.instagram.com/p/")
        assert item["quelle"].startswith("[Instagram @") and item["verified"] is True
        assert item["published_at"] and item["source_type"] in {"athlete_post", "tagged_post", "srf"}


def test_get_athlete_spelling_and_sections(data):
    with Session(data) as s:
        out = t.get_athlete(s, "andrea braendli")
    a = out["athletin"]
    assert a["name"] == "Andrea Brändli" and a["position"] == "Torhüterin"
    assert a["instagram"][0]["handle"] == "@andrea.braendli"
    assert a["instagram_bio"]["quelle"].startswith("[Instagram-Bio @andrea.braendli")
    assert any("Detroit" in (p.get("text") or "") for p in out["neueste_instagram_posts"])
    assert all("hinweis" in v for v in a["vereine_laut_quellen"] if v["saison"] == "2025-26")


def test_vorsicht_listed_first_and_quotes_verbatim(data):
    with Session(data) as s:
        athlete = t.get_athlete(s, "Testa Beispiel")
        news = t.whats_new(s, "Testa Beispiel", since="3650d")
    assert athlete["vorsicht"][0]["url"] == "https://www.instagram.com/p/SYN1/"
    srf = next(i for i in news["eintraege"] if i["source_type"] == "srf")
    assert srf["quelle"] == "[SRF, 02.10.]" or srf["quelle"].startswith("[SRF, 02.10.")
    assert {"text": "Wir haben heute als Team gespielt, das freut mich am meisten", "is_quote": True} in srf[
        "zitate"
    ]


def test_handles_only_usable(data):
    with Session(data) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        add_handle_candidate(s, a, "instagram", "fake.fan.account", {"kind": "search_hit"})
        s.flush()
        out = t.get_ig_handles(s, athletes=["Lara Stalder", "Niemand Unbekannt"])
        s.rollback()
    handles = [h["handle"] for h in out["athletinnen"][0]["handles"]]
    assert handles == ["@lara.stalder7"] and out["nicht_gefunden"] == ["Niemand Unbekannt"]


def test_whats_new_for_team_includes_team_level_news(data):
    with Session(data) as s:
        out = t.whats_new(s, "Schweiz", since="3650d")
    assert any(i["source_type"] == "srf" for i in out["eintraege"])


def test_search_tool_filters_by_source_type(data):
    with Session(data) as s:
        out = t.search_tool(s, SETTINGS, "Detroit", source_type="srf")
    assert out["treffer"] and all(i["source_type"] == "srf" for i in out["treffer"])


def test_unknown_inputs_give_clear_errors(data):
    with Session(data) as s:
        with pytest.raises(t.NotFound):
            t.get_event(s, "Tour de France")
        with pytest.raises(ValueError):
            t.parse_since("letzten Dienstag", 7)


def test_mcp_lists_all_tools_over_http(data):
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    headers = {
        "Authorization": "Bearer secret",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        tools = c.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
        ).json()
        names = {x["name"] for x in tools["result"]["tools"]}
        assert {
            "get_event",
            "get_athlete",
            "get_athlete_posts",
            "get_team_roster",
            "get_ig_handles",
            "whats_new",
            "search",
            "list_events",
            "status",
        } <= names
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_athlete", "arguments": {"name": "Alina Müller"}},
        }
        result = c.post("/mcp", json=call, headers=headers).json()["result"]
        assert "structuredContent" not in result  # one representation: the text (no JSON copy)
        text = result["content"][0]["text"]
        assert "**Alina Müller**" in text and "```json" not in text
        missing = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_event", "arguments": {"event": "Tour de France"}},
        }
        assert (
            "Fehler" in c.post("/mcp", json=missing, headers=headers).json()["result"]["content"][0]["text"]
        )


def test_every_item_has_ready_markdown_link(data):
    with Session(data) as s:
        out = t.get_event(s, "WM", posts_per_player=1)  # detail view: posts per player
    sui = next(b for b in out["teams"] if b["schweiz"])
    posts = [p for player in sui["spielerinnen"] for p in player["neueste_posts"]]
    assert posts
    for p in posts:
        assert p["quelle_link"] == f"[{p['quelle'].strip('[]')}]({p['url']})"
    assert "quelle_link" in out["hinweis"]
