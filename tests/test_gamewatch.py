"""Game watch (jobs/gamewatch.py): IIHF games read right after the final buzzer, re-checked, then one follow-up
per game (news + Instagram of both teams). Recorded IIHF JSON only (respx), fake Instagram/news sources, no paid
call: before the game idle, during the game polls, final stored once, re-check, follow-up once after the delay,
caps respected, nothing outside the game windows, a past IIHF event, and what the MCP tools show afterwards."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.instagram.base import FetchResult, InstagramPost
from klugscheisser.collectors.results import iihf
from klugscheisser.collectors.results.base import PoliteHttp
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.ingest import costs
from klugscheisser.jobs import gamewatch as gw
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/results")
START = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)  # game 3, Schweiz – USA, 19:00 Swiss time
KEY = "iihf-13547"
FEED = f"{iihf.API}/GetLatestScoresStateForEvents/773"
STATE = f"{iihf.API}/GetLatestState/13547"


def settings(**kw) -> Settings:
    base = {
        "_env_file": None,
        "iihf_realtime_enabled": True,
        "results_request_delay_s": 0.0,
        "instagram_source": "fixture",
        "instagram_team_accounts": [],
        "llm_daily_cost_cap_usd": 2.0,
        "game_watch_roster_watch": False,  # the kaderwatch step reads federation pages: tests/test_game_rosters.py
    }
    return Settings(**{**base, **kw})


def feed(status: str = "UPCOMING", home: str = "0", away: str = "0", start: datetime | None = None) -> list:
    """The recorded WCh schedule with game 3 in the given state (game 4, played at the same time, is left out so
    the tests follow one game)."""
    data = [g for g in json.loads((FIX / "iihf_event_773.json").read_text()) if g["GameId"] != "13548"]
    for g in data:
        if g["GameId"] == "13547" and start is not None:
            g["GameDateTimeUTC"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        if g["GameId"] == "13547":
            g["Status"], g["HomeTeam"]["Points"], g["GuestTeam"]["Points"] = status, home, away
    return data


def final_state() -> dict:
    return json.loads((FIX / "iihf_state_13547_final.json").read_text())


def live_state() -> dict:
    st = final_state()
    st["IsGameCompleted"], st["Status"] = False, "2nd Period"
    st["CurrentScore"] = {"Home": "1", "Away": "0"}
    st["Periods"] = st["Periods"][:1]
    return st


def incomplete_state() -> dict:
    """Final at the buzzer, but the overtime goal is not in the sheet yet."""
    st = final_state()
    st["Periods"][3]["ScoringActions"] = []
    return st


class Routes:
    """respx routes whose answers a test changes as the game goes on."""

    def __init__(self, mock):
        self.feed = feed()
        self.state = None
        self.feed_route = mock.get(FEED).mock(side_effect=lambda req: httpx.Response(200, json=self.feed))
        self.state_route = mock.get(STATE).mock(side_effect=lambda req: httpx.Response(200, json=self.state))

    @property
    def calls(self) -> tuple[int, int]:
        return self.feed_route.call_count, self.state_route.call_count


class FakeInstagram:
    name = "fixture"
    paid = False

    def __init__(self, paid: bool = False, cost: float = 0.0):
        self.paid, self.cost, self.calls = paid, cost, []
        if paid:
            self.name = "apify"

    def fetch_posts(self, handles, since, limit=12, max_usd=None):
        self.calls.append({"handles": list(handles), "max_usd": max_usd})
        posts = {
            h: [
                InstagramPost(
                    post_id=f"{h}-1",
                    shortcode=f"{h}-1",
                    url=f"https://www.instagram.com/p/{h}-after-game/",
                    owner_username=h,
                    owner_full_name=h,
                    published_at=datetime.now(UTC) - timedelta(minutes=30),
                    caption="Was für ein Spiel! Sieg in der Verlängerung gegen die USA.",
                )
            ]
            for h in handles
        }
        return FetchResult(posts=posts, cost_usd=self.cost, source=self.name)

    def fetch_profiles(self, handles):
        return []

    def fetch_mentions(self, handles, since, limit=12, max_usd=None):
        return FetchResult(posts={}, source=self.name)


class FakeNews:
    def __init__(self):
        self.calls = 0

    def articles(self):
        self.calls += 1
        return []


@pytest.fixture
def wch(db):
    """All seeds (WCh 2026 schedule with the IIHF keys, rosters), plus verified Instagram handles for two players
    and the Swiss team account."""
    with Session(db) as s:
        load_all_seeds(s)
        for name, handle in (("Alina Müller", "alinamueller"), ("Hilary Knight", "hilaryknight")):
            a = repo.resolve_athlete(s, name)
            assert a is not None, name
            s.add(m.Handle(athlete=a, platform="instagram", handle=handle, status="verified"))
        s.delete(s.scalar(select(m.Event).where(m.Event.key == "iihf-13548")))  # see feed()
        s.commit()
    return db


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def run(st: Settings, minutes: float, **kw) -> dict:
    kw.setdefault("instagram_source", FakeInstagram())
    kw.setdefault("news_collector", FakeNews())
    return gw.watch_pass(st, now=at(minutes), http=PoliteHttp(0), **kw)


def game(s) -> m.Event:
    return s.scalar(select(m.Event).where(m.Event.key == KEY))


def marker(s, source: str) -> m.CacheEntry | None:
    return s.get(m.CacheEntry, (source, KEY))


# --- the whole game day ----------------------------------------------------------------------------------


@respx.mock
def test_game_day_idle_poll_final_recheck_followup(wch):
    r = Routes(respx)
    st = settings(game_watch_schedule_hours=0)
    ig, news = FakeInstagram(), FakeNews()

    # before the game: idle, not one request
    assert run(st, -300)["polls"] == [] and r.calls == (0, 0)  # 13:00 UTC: before games 1-3

    # running: the feed says 2nd period, the state has the first goal
    r.feed, r.state = feed("2nd Period", "1", "0"), live_state()
    out = run(st, 30)
    assert r.calls == (1, 1) and out["polls"][0]["live"]
    with session_scope() as s:
        ev = game(s)
        assert (ev.status, ev.score) == ("live", "1:0")
        assert marker(s, gw.SRC_FINAL) is None
    # inside the poll interval: no second request
    run(st, 32)
    assert r.calls == (1, 1)
    run(st, 36)
    assert r.calls == (2, 2)

    # final: stored once, with scorers, assists, goalies, period scores and overtime
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    out = run(st, 152, instagram_source=ig, news_collector=news)
    assert r.calls == (3, 3)
    assert out["polls"][0]["final_neu"] and out["follow_ups"] == []
    with session_scope() as s:
        ev = game(s)
        assert (ev.status, ev.score) == ("final", "3:2 n.V.")
        item = s.scalar(select(m.Item).where(m.Item.platform == "iihf", m.Item.external_id == "13547"))
        assert "Alina Muller" in item.text and "Torhüterinnen" in item.text
        assert "(1:0, 0:1, 1:1, 1:0)" in item.text
        assert "Alina Müller" in {ia.athlete.full_name for ia in item.athletes}
        assert gw.sheet_complete(item)
        assert marker(s, gw.SRC_FINAL).fetched_at == at(152)
        # national-team tournament totals counted at once (stats tool)
        rows = s.scalars(
            select(m.SeasonStat).where(m.SeasonStat.source == "iihf", m.SeasonStat.league == "WC (W)")
        ).all()
        muller = repo.resolve_athlete(s, "Alina Müller")
        mine = [x for x in rows if x.athlete_id == muller.id]
        assert mine and (mine[0].goals, mine[0].assists) == (2, 1)
        # the tournament sheet (MCP events(turnier=…)) shows the result in the Swiss schedule
        from klugscheisser.mcp_server import suche as F

        assert "Fr 06.11. 19:00: Schweiz – USA 3:2 n.V." in F.events(s, turnier="wch-2026")

    # nothing due 5 minutes later (sheet complete, first re-check after 10 minutes)
    run(st, 157)
    assert r.calls == (3, 3)
    # re-check 10 minutes after the final: the state is read again, nothing duplicated
    run(st, 163)
    assert r.calls == (4, 4)
    with session_scope() as s:
        assert marker(s, gw.SRC_RECHECK).fetched_at == at(163)
        assert s.scalar(select(func.count()).select_from(m.Item).where(m.Item.platform == "iihf")) == 1
        assert s.scalar(select(func.count()).select_from(m.Event).where(m.Event.key == KEY)) == 1
    run(st, 170)
    assert r.calls == (4, 4)

    # follow-up 60 minutes after the final: once
    out = run(st, 213, instagram_source=ig, news_collector=news)
    assert len(out["follow_ups"]) == 1
    assert news.calls == 1
    asked = {h for c in ig.calls for h in c["handles"]}
    assert {"alinamueller", "hilaryknight", "swissicehockeywomen"} <= asked
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == gw.JOB_KIND))
        assert job.status == "done" and job.params["spiel"] == KEY
        assert job.result["instagram_personen"]["personen"] >= 2
        assert job.result["instagram_teams"]["team_posts_new"] == 1
        assert marker(s, gw.SRC_FOLLOWUP) is not None
        rows = {row["key"]: row for row in gw.status(s, st)}
        row = rows[KEY]
        assert row["final_gespeichert"] == "06.11. 21:32" and row["spielblatt"] == "vollständig"
        assert row["follow_up_status"] == "done" and "Instagram" in row["follow_up_ergebnis"]
    calls = len(ig.calls)
    out = run(st, 230, instagram_source=ig, news_collector=news)
    assert out["follow_ups"] == [] and len(ig.calls) == calls
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(m.Job).where(m.Job.kind == gw.JOB_KIND)) == 1

    assert r.calls == (5, 5)  # the pass at +61 min also made the 30-minute re-check
    # the last re-check (120 min after the final), then idle for good
    run(st, 152 + 121)
    assert r.calls == (6, 6)
    run(st, 152 + 300)
    run(st, 60 * 30)
    assert r.calls == (6, 6)


@respx.mock
def test_mcp_tools_show_the_game_right_after_the_final(wch):
    from klugscheisser.mcp_server import stats_tools
    from klugscheisser.mcp_server import suche as F
    from klugscheisser.mcp_server.present import render_markdown

    # the tools judge "last games" by the real clock: the game ended a moment ago
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(hours=2, minutes=40)
    with session_scope() as s:
        game(s).starts_at = start
    r = Routes(respx)
    r.feed, r.state = feed("FINAL", "3", "2", start=start), final_state()
    out = gw.watch_pass(settings(), now=now, http=PoliteHttp(0))
    assert out["polls"][0]["final_neu"]
    with session_scope() as s:
        last = F.events(s, team="Schweiz")
        assert "Schweiz – USA 3:2 n.V. (Sieg n.V.)" in last
        found = F.suche(s, settings(), "Schweiz USA Torschützinnen", team="Schweiz", quelle="official_result")
        assert "3:2 63:27 Alina Muller" in found and "(1:0, 0:1, 1:1, 1:0)" in found
        assert "Alina Müller (2 Tore, 1 Assist)" in found and "Torhüterinnen: Andrea Brandli" in found
        usa = F.team(s, "USA")  # key players from the Swiss angle: the new game against Switzerland
        assert "Schweiz – USA 3:2 n.V. (WM 2026)" in usa and "Hilary Knight: 1 T, 1 A" in usa
        assert "None Sp." not in usa and "Hilary Knight 1 T, 1 A" in usa
        st = render_markdown(stats_tools.stats(s, person="Alina Müller", league="WM", phase="alle"))

        assert "Alina Müller, 2026/27 Switzerland (WC (W)): 2 Tore, 1 Assist, 3 Punkte" in st


# --- re-checks of an incomplete sheet ----------------------------------------------------------------------


@respx.mock
def test_incomplete_sheet_is_read_at_every_poll_until_complete(wch):
    r = Routes(respx)
    st = settings(game_watch_schedule_hours=0)
    r.feed, r.state = feed("FINAL", "3", "2"), incomplete_state()
    run(st, 152)
    with session_scope() as s:
        item = s.scalar(select(m.Item).where(m.Item.platform == "iihf", m.Item.external_id == "13547"))
        assert not gw.sheet_complete(item)
        assert marker(s, gw.SRC_FINAL) is not None  # the result itself is there at once
    run(st, 158)
    assert r.calls == (2, 2)
    r.state = final_state()  # IIHF adds the overtime goal
    run(st, 164)
    assert r.calls == (3, 3)
    with session_scope() as s:
        item = s.scalar(select(m.Item).where(m.Item.platform == "iihf", m.Item.external_id == "13547"))
        assert gw.sheet_complete(item) and "63:27 Alina Muller" in item.text
        assert s.scalar(select(func.count()).select_from(m.Item).where(m.Item.platform == "iihf")) == 1
    run(st, 169)  # complete, next re-check at +30 min
    assert r.calls == (3, 3)


def test_sheet_complete_rules():
    def item(score, goals, goalies=("SUI", "USA")):
        parts = [{"role": "goalie", "team": t} for t in goalies]
        return m.Item(raw={"score": score, "goals": goals, "participants": parts})

    g = {"scorer": "X", "period": "1"}
    assert gw.sheet_complete(item("2:1", [g, g, g]))
    assert not gw.sheet_complete(item("2:1", [g, g]))
    assert not gw.sheet_complete(item("2:1", [g, g, g], goalies=("SUI",)))
    # shootout: the winner's extra goal is no scorer goal; shootout attempts are not counted
    so = {"scorer": "Y", "period": "SO"}
    assert gw.sheet_complete(item("3:2 n.P.", [g, g, g, g, so, so]))
    assert not gw.sheet_complete(item("3:2 n.P.", [g, g, g, so]))
    assert not gw.sheet_complete(None)


# --- windows, schedule, collisions ------------------------------------------------------------------------


@respx.mock
def test_not_active_outside_game_windows(wch):
    r = Routes(respx)
    st = settings(game_watch_schedule_hours=0)
    for when in (datetime(2026, 10, 31, 12, tzinfo=UTC), datetime(2026, 11, 6, 12, tzinfo=UTC)):
        assert gw.watch_pass(st, now=when, http=PoliteHttp(0))["polls"] == []
    # a game that never turns final: polling stops after GAME_WATCH_MAX_HOURS
    r.feed = feed("3rd Period", "1", "1")
    r.state = live_state()
    run(st, 60)
    assert r.calls == (1, 1)
    run(st, 4 * 60 + 10)
    assert r.calls == (1, 1)


@respx.mock
def test_schedule_read_while_the_event_runs(wch):
    r = Routes(respx)
    st = settings(game_watch_schedule_hours=6)
    # two weeks before: no game within 2 days, no request
    gw.watch_pass(st, now=datetime(2026, 10, 20, 12, tzinfo=UTC), http=PoliteHttp(0))
    assert r.calls == (0, 0)
    # the morning of the first game day: one schedule read, no game state
    out = gw.watch_pass(st, now=datetime(2026, 11, 6, 8, tzinfo=UTC), http=PoliteHttp(0))
    assert out["polls"][0]["grund"] == "Spielplan" and r.calls == (1, 0)
    gw.watch_pass(st, now=datetime(2026, 11, 6, 11, tzinfo=UTC), http=PoliteHttp(0))
    assert r.calls == (1, 0)
    gw.watch_pass(st, now=datetime(2026, 11, 6, 14, 30, tzinfo=UTC), http=PoliteHttp(0))
    assert r.calls[0] == 2  # games 1 and 2 are in their window now


@respx.mock
def test_off_switch_and_iihf_off(wch):
    r = Routes(respx)
    assert gw.watch_pass(settings(game_watch=False), now=at(30))["status"].startswith("aus")
    assert gw.watch_pass(settings(iihf_realtime_enabled=False), now=at(30))["status"].startswith("aus")
    assert r.calls == (0, 0)


@respx.mock
def test_poll_waits_for_a_running_results_refresh(wch, engine):
    from sqlalchemy import text

    from klugscheisser.ingest.results import RESULTS_LOCK

    r = Routes(respx)
    r.feed, r.state = feed("2nd Period", "1", "0"), live_state()
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": RESULTS_LOCK})
        out = run(settings(game_watch_schedule_hours=0), 30)
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": RESULTS_LOCK})
    assert out["polls"][0]["status"] == "läuft bereits" and r.calls == (0, 0)


@respx.mock
def test_followup_waits_for_a_nightly_run_and_runs_once(wch):
    r = Routes(respx)
    st = settings(game_watch_schedule_hours=0)
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    run(st, 152)
    with session_scope() as s:
        s.add(m.Job(kind="nightly", status="running", started_at=at(200), created_at=at(200), params={}))
    ig = FakeInstagram()
    assert run(st, 213, instagram_source=ig)["follow_ups"] == [] and ig.calls == []
    with session_scope() as s:
        s.scalar(select(m.Job).where(m.Job.kind == "nightly")).status = "done"
    assert len(run(st, 214, instagram_source=ig)["follow_ups"]) == 1 and ig.calls


# --- caps -------------------------------------------------------------------------------------------------


@respx.mock
def test_followup_skipped_when_the_llm_cap_is_used_up(wch):
    from klugscheisser.llm import text as llm_text

    r = Routes(respx)
    st = settings(game_watch_schedule_hours=0, llm_daily_cost_cap_usd=1.0)
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    run(st, 152)
    with session_scope() as s:
        costs.record(s, llm_text.cost_source(st, "answer"), 1, 1.5, "answers today")
    ig, news = FakeInstagram(), FakeNews()
    out = run(st, 213, instagram_source=ig, news_collector=news)
    assert len(out["follow_ups"]) == 1 and ig.calls == [] and news.calls == 0
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == gw.JOB_KIND))
        assert "LLM-Tageslimit" in job.result["uebersprungen"][0]
        assert "übersprungen" in gw.summarize(job.result)
    assert run(st, 240, instagram_source=ig)["follow_ups"] == []  # once per game, also when skipped


@respx.mock
def test_followup_apify_room_respects_daily_and_followup_caps(wch):
    r = Routes(respx)
    st = settings(
        game_watch_schedule_hours=0,
        apify_token="test",  # Apify configured: Instagram may cost money
        instagram_source="apify",
        apify_daily_cost_cap_usd=2.0,
        game_watch_followup_apify_usd=0.30,
    )
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    run(st, 152)
    with session_scope() as s:
        costs.record(s, "apify", 10, 1.95, "earlier today")
        local, room = gw.followup_settings(s, st, at(213))
        assert room == pytest.approx(0.05)
        assert local.apify_daily_cost_cap_usd == pytest.approx(2.0)
        costs.record(s, "apify", 10, 0.10, "more")
    ig = FakeInstagram(paid=True)
    run(st, 213, instagram_source=ig)
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == gw.JOB_KIND))
        assert job.result["apify_rahmen_usd"] == 0
        assert any("Apify-Limite" in x for x in job.result["uebersprungen"])
        # nothing paid past the cap
        assert costs.spent_today(s, "apify") == pytest.approx(2.05)
    assert ig.calls == []  # Apify-only source over its cap: not asked at all


@respx.mock
def test_followup_apify_cycle_guard(wch):
    r = Routes(respx)
    # the billing cycle started before the spend two days ago, whatever today's date is
    start_day = (datetime.now(gw.TZ).date() - timedelta(days=3)).day
    st = settings(
        apify_token="test",
        instagram_source="apify",
        apify_cycle_cap_usd=18.0,
        apify_cycle_start_day=start_day,
    )
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    with session_scope() as s:
        s.add(
            m.CostLedger(
                source="apify", units=1, cost_usd=17.9, created_at=datetime.now(UTC) - timedelta(days=2)
            )
        )
        s.flush()
        _, room = gw.followup_settings(s, st, datetime.now(UTC))
        assert room == pytest.approx(0.1)


# --- a past IIHF event ------------------------------------------------------------------------------------


@respx.mock
def test_past_event_from_a_recorded_game(db):
    """IIHF event 874 (Dec 2025, recorded game 13500) configured as an extra current event: the watch polls it in
    its window and stores the final once; long after it, nothing is asked."""
    state = json.loads((FIX / "iihf_state_13500.json").read_text())
    feed_874 = [
        {
            "GameId": "13500",
            "GameNumber": "5",
            "Status": "FINAL",
            "GameDateTimeUTC": "2025-12-17T13:30:00Z",
            "HomeTeam": {"TeamCode": "THA", "Points": "8"},
            "GuestTeam": {"TeamCode": "PHI", "Points": "1"},
            "Venue": "International Ice Hockey Arena",
            "PhaseId": "PreliminaryRound",
        }
    ]
    route_feed = respx.get(f"{iihf.API}/GetLatestScoresStateForEvents/874").mock(
        return_value=httpx.Response(200, json=feed_874)
    )
    route_state = respx.get(f"{iihf.API}/GetLatestState/13500").mock(
        return_value=httpx.Response(200, json=state)
    )
    respx.get(FEED).mock(return_value=httpx.Response(200, json=[]))
    comp = "IIHF Test-Turnier 2025"
    st = settings(
        game_watch_schedule_hours=0,
        iihf_events=[{"event_id": 874, "path": "", "site_id_offset": None, "competition": comp}],
    )
    with session_scope() as s:
        s.add(
            m.Competition(
                sport_id=repo.get_sport(s, "icehockey_women").id, key="test-874", name=comp, kind="tournament"
            )
        )
        s.add(
            m.Event(
                key="iihf-13500",
                kind="game",
                name="THA – PHI",
                status="scheduled",
                starts_at=datetime(2025, 12, 17, 13, 30, tzinfo=UTC),
                competition_id=s.scalar(select(m.Competition.id).where(m.Competition.name == comp)),
            )
        )
    later = datetime(2026, 11, 6, 12, tzinfo=UTC)
    gw.watch_pass(st, now=later, http=PoliteHttp(0))
    assert route_feed.call_count == 0
    when = datetime(2025, 12, 17, 15, 45, tzinfo=UTC)
    gw.watch_pass(
        st, now=when, http=PoliteHttp(0), instagram_source=FakeInstagram(), news_collector=FakeNews()
    )
    assert (route_feed.call_count, route_state.call_count) == (1, 1)
    with session_scope() as s:
        ev = s.scalar(select(m.Event).where(m.Event.key == "iihf-13500"))
        assert (ev.status, ev.score) == ("final", "8:1")
        assert s.get(m.CacheEntry, (gw.SRC_FINAL, "iihf-13500")) is not None
    gw.watch_pass(st, now=when + timedelta(minutes=5), http=PoliteHttp(0))
    assert route_feed.call_count == 1
    with Session(db) as s:
        rows = gw.status(s, st)
    assert any(r["key"] == "iihf-13500" and r["resultat"] == "8:1" for r in rows)
