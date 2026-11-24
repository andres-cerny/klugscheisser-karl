import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.results import iihf, pwhl, sdhl, sihf
from klugscheisser.collectors.results.base import PoliteHttp, first_last, title_name, wants
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.results import enabled_collectors, ingest_results, name_candidates
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/results")
SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def load(name):
    return json.loads((FIX / name).read_text())


# --- parsing ----------------------------------------------------------------------------------------


def test_name_helpers():
    assert first_last("Rüedi Lisa") == "Lisa Rüedi"
    assert first_last("Thuvik, Hanna") == "Hanna Thuvik"
    assert title_name("Alina", "MULLER") == "Alina Muller"
    assert "Alina Sophie Müller" in name_candidates("Sophie Müller Alina")
    assert wants(["Boston Fleet"], "Boston Fleet", "Ottawa Charge")
    assert wants(["SUI"], "Schweiz", "USA", "SUI", "USA")
    assert not wants(["Boston Fleet"], "Toronto Sceptres", "Ottawa Charge")
    assert wants(None, "x", "y")


def test_sihf_results_and_detail():
    games = sihf.parse_results(load("sihf_results.json"))
    assert len(games) == 3
    g = games[0]
    assert (g.source, g.external_id, g.status) == ("SIHF", "20271185000026", "final")
    assert (g.home, g.away, g.score) == ("ZSC Lions", "Lausanne HC", "4:0")
    assert g.period_scores == ["1:0", "1:0", "2:0"]
    assert g.url == "https://www.sihf.ch/de/game-center/game/20271185000026"
    assert g.starts_at.isoformat() == "2026-09-26T13:15:00+02:00"

    sihf.apply_detail(g, load("sihf_game_20271185000026.json"))
    scorers = [(p.name, p.detail) for p in g.participants if p.role == "scorer"]
    assert scorers[0] == ("Lisa Rüedi", "1:0, 12:07")
    assert [p.name for p in g.participants if p.role == "scorer"].count("Lisa Rüedi") == 2
    assists = {p.name for p in g.participants if p.role == "assist"}
    assert {"Jana Peter", "Anja Trummer", "Gloria Henek"} <= assists
    goalies = [(p.name, p.team, p.detail) for p in g.participants if p.role == "goalie"]
    assert goalies == [
        ("Alisha Berger", "ZSC Lions", "0 Gegentore, 60:00 Min."),
        ("Sofia Bernardasci", "Lausanne HC", "4 Gegentore, 60:00 Min."),
    ]
    assert len([p for p in g.participants if p.role == "lineup"]) == 40
    assert g.venue == "Swiss Life Arena, Zürich ZH"
    assert g.raw["detail"] and "ageGroup" not in json.dumps(g.raw)


def test_iihf_event_games_and_urls():
    games = iihf.parse_event_games(load("iihf_event_773.json"), iihf.IihfEvent())
    assert len(games) == 20
    sui_usa = next(g for g in games if g.external_id == "13547")
    assert (sui_usa.home, sui_usa.away, sui_usa.status) == ("Schweiz", "USA", "scheduled")
    assert sui_usa.url == "https://www.iihf.com/en/events/2026/ww/gamecenter/playbyplay/75141/3-sui-vs-usa"
    assert sui_usa.starts_at == datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    assert sui_usa.score is None and sui_usa.venue == "Hockey Arena Esbjerg"


def test_iihf_game_state():
    game = iihf.parse_event_games(load("iihf_event_773.json"), iihf.IihfEvent())[0]
    game.raw.update(home_code="THA", away_code="PHI")
    iihf.apply_state(game, load("iihf_state_13500.json"))
    assert game.status == "final" and game.score == "8:1"
    assert game.period_scores == ["0:0", "2:0", "6:1"]
    first = next(p for p in game.participants if p.role == "scorer")
    assert (first.name, first.detail) == ("Nicholas Charles Lampson", "1:0, 26:37 (PP)")
    assert {p.name for p in game.participants if p.role == "goalie"} == {
        "Benjamin David Kleineschay",
        "Francois Emmanuel Gautier",
    }
    assert "GG_Athlete_Id" not in json.dumps(load("iihf_state_13500.json"))  # encodes birth dates


def test_pwhl_schedule_and_summary():
    seasons = load("pwhl_seasons.json")["SiteKit"]["Seasons"]
    active = pwhl.active_seasons(seasons, datetime(2026, 5, 1).date(), datetime(2026, 5, 10).date())
    assert [s["season_id"] for s in active] == ["9"]

    games = pwhl.parse_schedule(load("pwhl_schedule_9.json"), "PWHL 2026 Playoffs")
    g = next(x for x in games if x.external_id == "338")
    assert (g.home, g.away, g.score, g.status) == ("Boston Fleet", "Ottawa Charge", "2:1", "final")
    assert g.url == "https://www.thepwhl.com/en/stats/game-center/338"
    pwhl.apply_summary(g, load("pwhl_summary_338.json"))
    assert g.period_scores == ["0:1", "2:0", "0:0"]
    mine = [(p.role, p.detail) for p in g.participants if p.name == "Alina Müller" and p.role != "lineup"]
    assert mine == [("scorer", "1:1, 17:56 2. Drittel"), ("assist", "2:1, 19:27 2. Drittel")]
    goalies = {p.name: p.detail for p in g.participants if p.role == "goalie"}
    assert goalies == {
        "Aerin Frankel": "17 Paraden bei 18 Schüssen",
        "Gwyneth Philips": "26 Paraden bei 28 Schüssen",
    }
    assert any(p.role == "lineup" and p.name == "Alina Müller" for p in g.participants)


def test_sdhl_schedule_and_events():
    games = sdhl.parse_schedule((FIX / "sdhl_schedule_20960.html").read_text(), 20960)
    assert len(games) == 9
    assert [g.status for g in games].count("scheduled") == 3
    ot = next(g for g in games if g.home == "HV 71")
    assert ot.score == "3:2 n.V." and len(ot.period_scores) == 4
    upcoming = next(g for g in games if g.status == "scheduled")
    assert upcoming.url == "https://stats.swehockey.se/ScheduleAndResults/Schedule/20960"
    g = next(g for g in games if g.raw["game_id"] == "1110693")
    assert g.url == "https://stats.swehockey.se/Game/Events/1110693"
    sdhl.apply_events(g, (FIX / "sdhl_events_1110693.html").read_text())
    assert [x["score"] for x in g.raw["goals"]] == ["1:0", "2:0", "3:0", "3:1", "3:2", "4:2"]
    meixner = next(p for p in g.participants if p.name == "Anna Meixner")
    assert (meixner.role, meixner.team, meixner.detail) == ("scorer", "Brynäs IF", "3:0, 41:25")
    goalies = {p.name: p.detail for p in g.participants if p.role == "goalie"}
    assert goalies["Klara Peslarova"] == "16 Paraden bei 18 Schüssen"


# --- collectors with mocked HTTP ------------------------------------------------------------------


def _sihf_routes(results=None):
    respx.get(f"{sihf.API}/cache300").mock(
        return_value=httpx.Response(200, json=results or load("sihf_results.json"))
    )
    detail = load("sihf_game_20271185000026.json")

    def detail_for(request):
        if request.url.params["searchQuery"] == "20271185000026":
            return httpx.Response(200, json=detail)
        return httpx.Response(404)

    return respx.get(f"{sihf.API}/gameoverview").mock(side_effect=detail_for)


def _pwhl_routes():
    def feed(request):
        p = request.url.params
        if p.get("view") == "seasons":
            return httpx.Response(200, json=load("pwhl_seasons.json"))
        if p.get("view") == "schedule":
            name = {"9": "pwhl_schedule_9.json", "11": "pwhl_schedule_11.json"}.get(p["season_id"])
            return httpx.Response(200, json=load(name) if name else {"SiteKit": {"Schedule": []}})
        if p.get("tab") == "gamesummary" and p.get("game_id") == "338":
            return httpx.Response(200, json=load("pwhl_summary_338.json"))
        return httpx.Response(404)

    return respx.get(pwhl.FEED).mock(side_effect=feed)


@respx.mock
def test_sihf_collector_respects_team_filter_and_errors():
    _sihf_routes()
    c = sihf.SihfCollector(http=PoliteHttp(0))
    games = c.collect(since=SINCE, teams=["ZSC Lions"])
    assert [g.external_id for g in games] == ["20271185000026"]
    assert games[0].participants


@respx.mock
def test_collectors_never_raise():
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    for c in (
        sihf.SihfCollector(http=PoliteHttp(0)),
        iihf.IihfCollector(http=PoliteHttp(0)),
        pwhl.PwhlCollector(http=PoliteHttp(0)),
        sdhl.SdhlCollector(http=PoliteHttp(0)),
    ):
        assert c.collect(since=SINCE) == []


@respx.mock
def test_sdhl_collector_fetches_only_new_finals():
    respx.get(f"{sdhl.BASE}/ScheduleAndResults/Schedule/20960").mock(
        return_value=httpx.Response(200, text=(FIX / "sdhl_schedule_20960.html").read_text())
    )
    events = respx.get(url__regex=r".*/Game/Events/\d+").mock(
        return_value=httpx.Response(200, text=(FIX / "sdhl_events_1110693.html").read_text())
    )
    c = sdhl.SdhlCollector(http=PoliteHttp(0), max_details=2)
    c.known_final = {"80001001"}
    games = c.collect(since=SINCE, teams=["Brynäs IF", "Frölunda HC"])
    assert {g.home for g in games} >= {"Brynäs IF", "Frölunda HC"}
    assert events.call_count == 2  # 80001001 known, 80001003 + 80001006 fetched


def test_enabled_collectors_flags():
    on = dict(sihf_data_enabled=True, iihf_realtime_enabled=True, pwhl_enabled=True, sdhl_enabled=True)
    names = [c.name for c in enabled_collectors(Settings(_env_file=None, **on))]
    assert names == ["sihf", "iihf", "pwhl", "sdhl"]
    off = Settings(_env_file=None, **{**on, "iihf_realtime_enabled": False, "sdhl_enabled": False})
    assert [c.name for c in enabled_collectors(off)] == ["sihf", "pwhl"]


# --- ingest -----------------------------------------------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Lisa Rüedi", nationality="SUI")
        s.commit()
    return db


@respx.mock
def test_ingest_sihf_creates_event_and_item_idempotently(world):
    detail_route = _sihf_routes()
    with Session(world) as s:
        ctx = build_context(s, Settings(_env_file=None))
        c = sihf.SihfCollector(http=PoliteHttp(0))
        report = ingest_results(ctx, [c], since=SINCE)
        s.commit()
        assert not report.errors
        assert (report.games, report.events_new, report.items_new) == (3, 3, 1)

        ev = s.scalar(select(m.Event).where(m.Event.key == "sihf-20271185000026"))
        assert (ev.kind, ev.source, ev.status, ev.score) == ("game", "SIHF", "final", "4:0")
        assert ev.source_url == "https://www.sihf.ch/de/game-center/game/20271185000026"
        assert repo.get_team(s, "zsc-frauen").id == ev.home_team_id
        assert repo.get_team(s, "lausanne-feminin").id == ev.away_team_id
        assert ev.competition_id is not None and ev.ends_at is not None

        item = s.scalar(select(m.Item).where(m.Item.url == ev.source_url))
        assert (item.source_type, item.source_kind, item.platform, item.source_name) == (
            "official_result",
            "result",
            "sihf",
            "SIHF",
        )
        assert item.title == "ZSC Lions – Lausanne HC 4:0"
        assert item.external_id == "20271185000026"
        assert "1:0 12:07 Lisa Rüedi (Jana Peter, Anja Trummer)" in item.text
        # Wetli and Rüedi are in the seeded Olympic roster, Nora Harju in the Lahti squad (dressed, no goal)
        assert (
            "Schweizerinnen: Lisa Rüedi (2 Tore); Nora Harju (im Einsatz); Stefanie Wetli (1 Tor)."
            in item.text
        )
        linked = {ia.athlete.full_name for ia in item.athletes}
        assert {"Lisa Rüedi", "Stefanie Wetli"} <= linked
        assert {ia.match_kind for ia in item.athletes} == {"name"}
        assert {t.team.key for t in item.teams} == {"zsc-frauen", "lausanne-feminin"}
        assert item.classification.category == "result"
        assert "ageGroup" not in json.dumps(item.raw)
        assert detail_route.call_count == 3  # one detail answers; the other two 404 -> event but no item

        # second run: no duplicates, no detail re-fetch for games already stored as final
        report2 = ingest_results(build_context(s, Settings(_env_file=None)), [c], since=SINCE)
        s.commit()
        assert (report2.events_new, report2.items_new, report2.items_changed) == (0, 0, 0)
        assert s.scalar(select(func.count()).select_from(m.Event).where(m.Event.source == "SIHF")) == 3
        assert s.scalar(select(func.count()).select_from(m.Item).where(m.Item.platform == "sihf")) == 1
        assert detail_route.call_count == 3 + 2  # only the two games without an item are retried


@respx.mock
def test_ingest_updates_live_game_to_final(world):
    live = copy.deepcopy(load("sihf_results.json"))
    row = live["data"][0]
    row[5] = {"homeTeam": "2", "awayTeam": "0", "type": "result"}
    row[8] = {**row[8], "percent": 60, "name": "3. Drittel"}
    respx.get(f"{sihf.API}/cache300").mock(
        side_effect=[httpx.Response(200, json=live), httpx.Response(200, json=load("sihf_results.json"))]
    )
    respx.get(f"{sihf.API}/gameoverview").mock(
        return_value=httpx.Response(200, json=load("sihf_game_20271185000026.json"))
    )
    with Session(world) as s:
        c = sihf.SihfCollector(http=PoliteHttp(0))
        ingest_results(build_context(s, Settings(_env_file=None)), [c], since=SINCE, teams=["ZSC Lions"])
        ev = s.scalar(select(m.Event).where(m.Event.key == "sihf-20271185000026"))
        assert (ev.status, ev.score) == ("live", "2:0")
        assert s.scalar(select(func.count()).select_from(m.Item)) == 0

        report = ingest_results(
            build_context(s, Settings(_env_file=None)), [c], since=SINCE, teams=["ZSC Lions"]
        )
        assert (report.events_updated, report.items_new) == (1, 1)
        assert (ev.status, ev.score) == ("final", "4:0")


@respx.mock
def test_ingest_pwhl_links_alina_mueller(world):
    _pwhl_routes()
    with Session(world) as s:
        c = pwhl.PwhlCollector(teams=["Boston Fleet"], http=PoliteHttp(0))
        report = ingest_results(
            build_context(s, Settings(_env_file=None)), [c], since=datetime(2026, 4, 1, tzinfo=UTC)
        )
        s.commit()
        assert not report.errors
        item = s.scalar(
            select(m.Item).where(m.Item.url == "https://www.thepwhl.com/en/stats/game-center/338")
        )
        assert item.title == "Boston Fleet – Ottawa Charge 2:1"
        assert "Alina Müller" in {ia.athlete.full_name for ia in item.athletes}
        assert "Alina Müller (1 Tor, 1 Assist)" in item.text
        assert item.published_at is not None
        ev = s.scalar(select(m.Event).where(m.Event.key == "pwhl-338"))
        assert ev.status == "final" and ev.source == "PWHL"


@respx.mock
def test_ingest_iihf_schedule_links_national_teams(world):
    respx.get(f"{iihf.API}/GetLatestScoresStateForEvents/773").mock(
        return_value=httpx.Response(200, json=load("iihf_event_773.json"))
    )
    with Session(world) as s:
        c = iihf.IihfCollector(http=PoliteHttp(0))
        report = ingest_results(build_context(s, Settings(_env_file=None)), [c], since=SINCE, teams=["SUI"])
        # v18: the WM games are seeded with the collector's keys: updated, never duplicated
        assert report.events_new == 0 and report.items_new == 0
        ev = s.scalar(select(m.Event).where(m.Event.key == "iihf-13547"))
        assert ev.status == "scheduled" and ev.name == "Schweiz – USA"
        assert ev.home_team_id == repo.get_team(s, "sui-women").id
        assert ev.away_team_id == repo.get_team(s, "usa-women").id
        assert ev.source_url.endswith("/75141/3-sui-vs-usa")


@pytest.mark.network
def test_live_sihf_results():
    games = sihf.SihfCollector(delay_s=1.0, max_details=1).collect()
    assert games and all(g.url.startswith("https://www.sihf.ch/de/game-center/game/") for g in games)
