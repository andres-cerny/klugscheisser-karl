"""X as a source (official API): profile check → tiered handle status, incremental timeline, team accounts,
labels, costs. Response shapes as returned by api.x.com on 2026-10-03 (user lookup + user timeline)."""

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.x_api import BASE, XApiClient
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.runner import build_context
from klugscheisser.ingest.x import ingest_x
from klugscheisser.mcp_server.present import item_out

SETTINGS = Settings(_env_file=None, x_bearer_token="test", x_team_accounts=["SwissIceHockey"])

USER = {
    "data": {
        "id": "111",
        "username": "rahelenzler",
        "name": "Rahel Enzler",
        "description": "Eishockey | ZSC Lions | 🇨🇭",
        "verified": False,
        "public_metrics": {"followers_count": 900},
    }
}
TEAM = {"data": {"id": "76612732", "name": "Swiss Ice Hockey", "username": "SwissIceHockey"}}
POSTS = {
    "data": [
        {
            "id": "2106378348775584001",
            "text": "Was für ein Spiel in Lahti! 🏒",
            "created_at": "2026-10-03T14:00:00.000Z",
            "lang": "de",
        },
        {
            "id": "2106378348775584000",
            "text": "Training in Zürich",
            "created_at": "2026-10-02T10:00:00.000Z",
            "lang": "de",
        },
    ]
}
TEAM_POSTS = {
    "data": [
        {
            "id": "2106378348775584999",
            "text": "Sieg nach Penaltyschiessen gegen Tschechien. 🚨: Rahel Enzler, Leeman",
            "created_at": "2026-10-03T15:00:00.000Z",
            "lang": "fr",
        },
    ]
}


@pytest.fixture
def rahel(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Rahel Enzler", nationality="SUI")
        add_handle_candidate(s, a, "x", "rahelenzler", {"kind": "wikidata", "detail": "Wikidata Q1"})
        s.commit()
        return a.id


@respx.mock
def test_profile_check_timeline_team_account_and_costs(db, rahel):
    respx.get(f"{BASE}/users/by/username/rahelenzler").mock(return_value=httpx.Response(200, json=USER))
    respx.get(f"{BASE}/users/by/username/SwissIceHockey").mock(return_value=httpx.Response(200, json=TEAM))
    timeline = respx.get(f"{BASE}/users/111/tweets").mock(return_value=httpx.Response(200, json=POSTS))
    respx.get(f"{BASE}/users/76612732/tweets").mock(return_value=httpx.Response(200, json=TEAM_POSTS))
    with Session(db) as s:
        a = s.get(m.Athlete, rahel)
        report = ingest_x(build_context(s, SETTINGS), XApiClient(SETTINGS), [a])
        s.commit()
        assert report.profiles_checked == 1 and report.posts_new == 2 and report.team_posts_new == 1
        h = a.handles[0]
        assert h.status == "verified" and h.last_seen_post_id == "2106378348775584001"  # wikidata+name+bio
        assert report.cost_usd == pytest.approx(2 * 0.01 + 3 * 0.005)
        items = {i.url: i for i in s.scalars(select(m.Item).where(m.Item.platform == "x"))}
        own = items["https://x.com/rahelenzler/status/2106378348775584001"]
        assert own.source_kind == "self" and own.athletes[0].athlete_id == rahel
        out = item_out(own)
        assert (
            out["zeile_prefix"].startswith("𝕏 (von ihr selbst)")
            and "[X @rahelenzler, 03.10.]" in out["quelle"]
        )
        team = items["https://x.com/SwissIceHockey/status/2106378348775584999"]
        assert team.source_kind == "official" and rahel in {x.athlete_id for x in team.athletes}
        # second run: only newer posts are requested (since_id), team account not again within 3 h
        ingest_x(build_context(s, SETTINGS), XApiClient(SETTINGS), [a])
        assert timeline.calls.last.request.url.params["since_id"] == "2106378348775584001"


@respx.mock
def test_unrelated_profile_stays_unusable(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Testa Namensvetterin")
        add_handle_candidate(s, a, "x", "tnv", {"kind": "search_hit", "detail": "Suche"})
        s.commit()
        respx.get(f"{BASE}/users/by/username/tnv").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "id": "9",
                        "username": "tnv",
                        "name": "Ganz Jemand Anders",
                        "description": "Golf und Reisen",
                    }
                },
            )
        )
        timeline = respx.get(f"{BASE}/users/9/tweets").mock(return_value=httpx.Response(200, json=POSTS))
        report = ingest_x(build_context(s, SETTINGS), XApiClient(SETTINGS), [a], team_accounts=False)
        assert report.profiles_checked == 1 and report.posts_new == 0 and not timeline.called
        assert not a.handles[0].usable


def test_budget_cap_stops_reading(db, rahel):
    tight = SETTINGS.model_copy(update={"x_daily_cost_cap_usd": 0.0})
    with Session(db) as s:
        report = ingest_x(
            build_context(s, tight), XApiClient(tight), [s.get(m.Athlete, rahel)], team_accounts=False
        )
        assert report.skipped_budget == ["rahelenzler"] and report.cost_usd == 0
