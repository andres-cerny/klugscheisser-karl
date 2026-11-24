"""Nightly refresh 2026-10-11: tier 1 every night + 1/7 of tier 2, global sources once, cost estimate and caps,
one-off full night, dry run; plus MCP speed (news/results in the background, own status wait), the events
duplicate-key fix and the per-tool timing log. Offline, no paid call."""

import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import TZ
from klugscheisser.ingest import costs
from klugscheisser.jobs import nightly as nj

BASE = dict(
    _env_file=None, x_bearer_token="test", x_team_accounts=["SwissIceHockey"], instagram_team_accounts=[]
)
SETTINGS = Settings(**BASE)
NIGHT = datetime(2026, 10, 12, 5, 0, tzinfo=TZ)  # a Monday, 05:00 Zurich
FULL_DAY = date(2026, 10, 15)


def person(s, name, tier, ig=None, x=None, ig_seen=None, x_seen=None, x_status="wahrscheinlich"):
    a = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), name)
    a.tier = tier
    if ig:
        s.add(
            m.Handle(
                athlete=a,
                platform="instagram",
                handle=ig,
                status="verified",
                last_scraped_at=ig_seen,
                profile={"fetched_at": datetime.now(UTC).isoformat(), "posts_count": 50},
            )
        )
    if x:
        s.add(
            m.Handle(
                athlete=a,
                platform="x",
                handle=x,
                status=x_status,
                last_scraped_at=x_seen,
                profile={"x_id": "1"} if x_seen else None,
            )
        )
    s.flush()
    return a


@pytest.fixture
def world(db):
    """Tier 1: 3 persons (2 Instagram, 1 X read before); tier 2: 14 persons, each with Instagram and an X
    handle never read; tier 3: 5 persons."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    week = datetime.now(UTC) - timedelta(days=7)
    with Session(db) as s:
        ids = {1: [], 2: [], 3: []}
        ids[1].append(
            person(s, "Eins Alpha", 1, ig="alpha", x="alpha_x", ig_seen=yesterday, x_seen=yesterday).id
        )
        ids[1].append(person(s, "Eins Beta", 1, ig="beta", ig_seen=yesterday).id)
        ids[1].append(person(s, "Eins Gamma", 1).id)
        for i in range(14):
            ids[2].append(person(s, f"Zwei Person{i}", 2, ig=f"zwei{i}", x=f"zwei{i}_x", ig_seen=week).id)
        for i in range(5):
            ids[3].append(person(s, f"Drei Person{i}", 3, ig=f"drei{i}", ig_seen=week).id)
        s.commit()
    return ids


# --- who ---------------------------------------------------------------------------------------------


def test_rotation_is_stable_and_covers_everyone_once_a_week():
    days = [date(2026, 10, 12) + timedelta(days=d) for d in range(7)]
    for pid in range(1, 50):
        hits = [d for d in days if nj.in_rotation(pid, d, 7)]
        assert len(hits) == 1
        assert nj.in_rotation(pid, hits[0] + timedelta(days=7), 7)  # same weekday next week
    assert all(nj.in_rotation(5, d, 1) for d in days)


def test_selection_tier1_every_night_tier2_seventh_tier3_off(world):
    with session_scope() as s:
        groups = nj.select_persons(s, SETTINGS, NIGHT.date())
        assert groups["tier 1"] == world[1]
        assert groups["tier 2 (1/7)"] == [i for i in world[2] if i % 7 == NIGHT.date().toordinal() % 7]
        assert len(groups["tier 2 (1/7)"]) == 2 and not any("tier 3" in k for k in groups)
        # over a week every tier 2 person comes exactly once
        week = [
            i
            for d in range(7)
            for i in nj.select_persons(s, SETTINGS, NIGHT.date() + timedelta(days=d))["tier 2 (1/7)"]
        ]
        assert sorted(week) == sorted(world[2])
        # tier 3 is configurable (e.g. from 2026-11-04 on)
        with3 = SETTINGS.model_copy(update={"nightly_tiers": {1: 1, 2: 7, 3: 1}})
        assert nj.select_persons(s, with3, NIGHT.date())["tier 3"] == world[3]
        # the full night takes all of tier 1 and 2
        full = nj.select_persons(s, SETTINGS, FULL_DAY, full=True)
        assert full["tier 1 (alle, einmalig)"] == world[1] and full["tier 2 (alle, einmalig)"] == world[2]
        assert "tier 2 (1/7)" not in full


def test_prepared_event_persons_stay_in(world):
    with session_scope() as s:
        s.add(
            m.AddRequest(
                kind="event",
                status="done",
                input={"event_key": "x", "nightly_from": "2026-10-10", "nightly_to": "2026-10-20"},
                athlete_ids=[world[3][0], world[1][0]],
            )
        )
    with session_scope() as s:
        groups = nj.select_persons(s, SETTINGS, NIGHT.date())
        assert groups["Anlässe"] == [world[3][0]]  # tier 1 person listed once, in tier 1
        p = nj.plan(s, SETTINGS, NIGHT)
        assert world[3][0] in p.first_read_ids  # prepared events: X first reads as before


def test_env_settings_parse(monkeypatch):
    monkeypatch.setenv("NIGHTLY_TIERS", '{"1": 1, "2": 7, "3": 30}')
    monkeypatch.setenv("NIGHTLY_FULL_ONCE_DATE", "2026-10-15")
    monkeypatch.setenv("NIGHTLY_X_FIRST_READ_TIERS", "[1, 2]")
    st = Settings(_env_file=None)
    assert st.nightly_tiers == {1: 1, 2: 7, 3: 30} and st.nightly_full_once_date == FULL_DAY
    assert st.nightly_x_first_read_tiers == [1, 2]
    d = Settings(_env_file=None)
    monkeypatch.delenv("NIGHTLY_FULL_ONCE_DATE")
    assert Settings(_env_file=None).nightly_full_once_date is None and d.nightly_hour == 5


# --- when --------------------------------------------------------------------------------------------


def test_due_at_five_and_full_night_once(db):
    st = SETTINGS.model_copy(update={"nightly_full_once_date": FULL_DAY})
    with session_scope() as s:
        assert not nj.due(s, st, NIGHT.replace(hour=4)) and nj.due(s, st, NIGHT)
        one = datetime(2026, 10, 15, 1, 0, tzinfo=TZ)
        assert nj.full_due(s, st, one) and nj.due(s, st, one)
        assert not nj.full_due(s, st, one.replace(hour=0))
        assert not nj.full_due(s, st, one + timedelta(days=1))
        assert not nj.full_due(s, SETTINGS, one)  # no date set
        for key in (nj.MARKER, nj.full_marker(FULL_DAY)):  # what run_nightly writes at 01:00
            s.add(m.CacheEntry(source=key[0], target=key[1], fetched_at=one.astimezone(UTC)))
        s.flush()
        assert not nj.due(s, st, one.replace(hour=5))  # the full run replaced Thursday's regular run
        assert nj.due(s, st, one.replace(hour=5) + timedelta(days=1))


# --- how much ----------------------------------------------------------------------------------------


def test_estimate_regular_night(world):
    with session_scope() as s:
        p = nj.plan(s, SETTINGS, NIGHT)
        # tier 1: 2 Instagram accounts, tier 2 slice: 2; X: alpha_x incremental, 2 tier-2 first reads skipped
        assert (p.ig_handles, p.ig_first, p.x_handles, p.x_first, p.x_first_skipped) == (4, 0, 1, 0, 2)
        price = SETTINGS.apify_cost_per_post_usd
        assert p.apify_usd == pytest.approx(4 * price)  # no stored posts: one result per account
        assert p.x_usd == pytest.approx(0.01 + 0.005)  # @SwissIceHockey: lookup + one post
        assert p.ok and p.apify_cap == SETTINGS.nightly_cap_usd and p.total_cap is None
        # grouped by last fetch: tier 1 (yesterday) and tier 2 (a week ago) never share an Apify run
        assert len(p.ig_buckets) == 2 and sorted(p.ig_buckets[1]) == world[1][:2]


def test_estimate_full_night_with_x_first_reads(world):
    with session_scope() as s:
        p = nj.plan(s, SETTINGS, datetime(2026, 10, 15, 1, 0, tzinfo=TZ), full=True)
        assert p.full and p.x_first == 14 and p.x_first_skipped == 0
        assert p.x_usd == pytest.approx(14 * (0.01 + 20 * 0.005) + 0.015)
        assert (p.total_cap, p.apify_cap, p.x_cap) == (8.0, 2.0, 6.0) and p.ok
        assert set(p.first_read_ids) >= set(world[2])


def test_estimate_uses_posting_rate(world):
    with session_scope() as s:
        a = s.get(m.Athlete, world[1][0])
        for d in range(45):  # one post every second day -> 0.5/day over 90 days
            s.add(
                m.Item(
                    source_type="athlete_post",
                    platform="instagram",
                    url=f"https://instagram.com/p/a{d}",
                    author="alpha",
                    content_hash=f"h{d}",
                    published_at=datetime.now(UTC) - timedelta(days=2 * d),
                    athletes=[m.ItemAthlete(athlete_id=a.id)],
                )
            )
        s.flush()
        # the world's handles were read "yesterday" (relative to today), so plan from tonight, not from NIGHT
        tonight = datetime.now(TZ).replace(hour=5, minute=0, second=0, microsecond=0)
        base = nj.plan(s, SETTINGS, tonight).apify_usd
        later = nj.plan(s, SETTINGS, tonight + timedelta(days=10)).apify_usd  # 10 days not fetched
        assert later > base


def test_caps_abort_cleanly(world):
    with session_scope() as s:
        tiny = SETTINGS.model_copy(update={"nightly_cap_usd": 0.001})
        assert any("Apify" in x for x in nj.plan(s, tiny, NIGHT).problems)
        costs.record(s, "x", 1, 0.999, "spent today")
        assert any(x.startswith("X:") for x in nj.plan(s, SETTINGS, NIGHT).problems)
        cycle = SETTINGS.model_copy(update={"apify_cycle_cap_usd": 1.0})
        costs.record(s, "apify", 1, 0.999, "cycle")
        assert any("Apify-Monat" in x for x in nj.plan(s, cycle, NIGHT).problems)
        low = SETTINGS.model_copy(update={"nightly_full_cap_usd": 1.0})
        p = nj.plan(s, low, datetime(2026, 10, 15, 1, 0, tzinfo=TZ), full=True)
        assert any(x.startswith("Total") for x in p.problems)


def test_cycle_start():
    assert nj.cycle_start(date(2026, 10, 11), 4) == date(2026, 10, 4)
    assert nj.cycle_start(date(2026, 11, 3), 4) == date(2026, 10, 4)
    assert nj.cycle_start(date(2026, 11, 4), 4) == date(2026, 11, 4)
    assert nj.cycle_start(date(2026, 1, 2), 4) == date(2025, 12, 4)


# --- the run (paid steps faked) -------------------------------------------------------------------------


@pytest.fixture
def fake_steps(monkeypatch):
    calls = {}

    def ig(p, st):
        calls["ig"] = (p.ig_buckets, st)
        return {"handles": p.ig_handles}

    def x(p, st):
        calls["x"] = (set(p.first_read_ids), st)
        return {"handles_read": p.x_handles}

    monkeypatch.setattr(nj, "_instagram", ig)
    monkeypatch.setattr(nj, "_x", x)
    monkeypatch.setattr(nj, "_news", lambda st: {"status": "aktuell"})
    monkeypatch.setattr(nj, "_results", lambda st: {"status": "aktuell"})
    monkeypatch.setattr(nj, "_roster_watch", lambda: {"neu": []})  # no network in tests
    monkeypatch.setattr(nj, "_season_totals", lambda st: {"gespeichert": 0})
    monkeypatch.setattr(nj, "_hockeycanada", lambda settings: {})
    monkeypatch.setattr(nj, "_profiles", lambda p, st: {"athletes": len(p.ids)})
    monkeypatch.setattr("klugscheisser.tiers.recompute", lambda s: {})
    return calls


def test_regular_run_caps_and_job(world, fake_steps):
    out = nj.run_nightly(SETTINGS, now=NIGHT)
    assert out["status"] == "done" and out["art"] == "regulär" and out["personen"] == 5
    _, st = fake_steps["ig"]
    assert st.apify_daily_cost_cap_usd == pytest.approx(SETTINGS.nightly_cap_usd)
    assert st.x_daily_cost_cap_usd == SETTINGS.x_daily_cost_cap_usd  # X cap unchanged on regular nights
    first, _ = fake_steps["x"]
    assert first == set(world[1])
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == "nightly"))
        assert job.status == "done" and job.params["plan"]["personen"] == 5 and job.result["profiles"]
        assert not nj.due(s, SETTINGS, NIGHT + timedelta(hours=1))
        assert nj.last_runs(s)[0]["job_id"] == job.id


def test_full_run_raises_x_cap_for_that_run_only(world, fake_steps):
    st = SETTINGS.model_copy(update={"nightly_full_once_date": FULL_DAY})
    with session_scope() as s:
        costs.record(s, "x", 1, 0.5, "earlier today")
    out = nj.run_nightly(st, now=datetime(2026, 10, 15, 1, 0, tzinfo=TZ))
    assert out["art"] == "voll (einmalig)" and out["personen"] == 17 and out["status"] == "done"
    _, run_st = fake_steps["x"]
    assert run_st.x_daily_cost_cap_usd == pytest.approx(0.5 + 6.0)
    assert st.x_daily_cost_cap_usd == 1.0  # the settings object itself is not changed
    with session_scope() as s:
        assert not nj.full_due(s, st, datetime(2026, 10, 15, 2, 0, tzinfo=TZ))
        assert not nj.due(s, st, datetime(2026, 10, 15, 5, 0, tzinfo=TZ))


def test_full_run_over_cap_falls_back_to_regular(world, fake_steps):
    st = SETTINGS.model_copy(update={"nightly_full_once_date": FULL_DAY, "nightly_full_cap_usd": 0.5})
    out = nj.run_nightly(st, now=datetime(2026, 10, 15, 1, 0, tzinfo=TZ))
    assert out["art"] == "regulär" and out["abgebrochen"] and out["status"] == "done"
    with session_scope() as s:
        assert not nj.full_due(s, st, datetime(2026, 10, 15, 2, 0, tzinfo=TZ))  # tried once, not again


def test_regular_run_over_cap_pays_nothing(world, fake_steps):
    out = nj.run_nightly(SETTINGS.model_copy(update={"nightly_cap_usd": 0.0}), now=NIGHT)
    assert out["status"] == "abgebrochen" and "ig" not in fake_steps
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == "nightly"))
        assert job.status == "failed" and "Apify" in job.error


def test_profiles_stop_at_end_hour(world, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "klugscheisser.ingest.profiles.profiles_for_update",
        lambda s, st, athletes: seen.extend(a.id for a in athletes) or {"athletes": len(athletes)},
    )
    monkeypatch.setattr(nj, "PROFILE_CHUNK", 2)
    with session_scope() as s:
        p = nj.plan(s, SETTINGS, datetime(2026, 10, 15, 1, 0, tzinfo=TZ), full=True)
    times = iter(
        [datetime(2026, 10, 15, 7, 50, tzinfo=TZ)] * 3 + [datetime(2026, 10, 15, 8, 1, tzinfo=TZ)] * 20
    )
    out = nj._profiles(p, SETTINGS, clock=lambda: next(times))
    assert len(seen) == 4 and "gestoppt" in out
    day = iter([datetime(2026, 10, 15, 14, 0, tzinfo=TZ)] * 50)  # started by hand during the day: no stop
    seen.clear()
    assert "gestoppt" not in nj._profiles(p, SETTINGS, clock=lambda: next(day)) and len(seen) == 17


def test_x_first_reads_only_for_allowed_persons(world):
    from klugscheisser.collectors.x_api import XApiClient
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.ingest.x import ingest_x

    class NoNetwork(XApiClient):
        def user(self, username):
            raise AssertionError("no request expected")

        def posts(self, *a, **kw):
            raise AssertionError("no request expected")

    with session_scope() as s:
        athletes = [s.get(m.Athlete, i) for i in world[2][:3]]
        report = ingest_x(
            build_context(s, SETTINGS),
            NoNetwork(SETTINGS),
            athletes,
            team_accounts=False,
            first_read_ids=set(),
        )
        assert report.skipped_first_read == ["zwei0_x", "zwei1_x", "zwei2_x"] and report.cost_usd == 0


# --- dry run -----------------------------------------------------------------------------------------


def test_cli_dry_run_writes_nothing(world, monkeypatch):
    from klugscheisser import config
    from klugscheisser.cli import app

    monkeypatch.setattr(config, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr("klugscheisser.cli.get_settings", lambda: SETTINGS)
    with session_scope() as s:
        before = (
            s.scalar(select(func.count()).select_from(m.Job)),
            s.scalar(select(func.count()).select_from(m.CacheEntry)),
        )
    r = CliRunner().invoke(app, ["nightly", "--dry-run", "--date", "2026-10-12"])
    assert r.exit_code == 0, r.output
    assert '"personen": 5' in r.output and "Eins Alpha" in r.output and "nichts bezahlt" in r.output
    r = CliRunner().invoke(app, ["nightly", "--dry-run", "--date", "2026-10-15", "--full", "--no-names"])
    assert r.exit_code == 0 and '"personen": 17' in r.output and "voll (einmalig)" in r.output
    with session_scope() as s:
        after = (
            s.scalar(select(func.count()).select_from(m.Job)),
            s.scalar(select(func.count()).select_from(m.CacheEntry)),
        )
    assert before == after


# --- MCP speed, status wait, timing log ------------------------------------------------------------------


def test_mcp_freshen_refreshes_news_and_results_in_background(db, monkeypatch):
    from klugscheisser.jobs import freshness
    from klugscheisser.mcp_server import tools

    got = {}

    def fake(s, settings, athletes, **kw):
        got.update(kw)
        return freshness.Freshness("aktuell")

    monkeypatch.setattr(freshness, "ensure_fresh", fake)
    with session_scope() as s:
        a = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Testa")
        assert tools.freshen(s, [a]) == {"status": "aktuell"}
    # search lab 2026-10-25: MCP_REFRESH_NO_WAIT (default) answers at once, the update runs in the background
    assert got == {"fast_sources_now": False, "wait_s": 0.0}
    monkeypatch.setattr(tools, "get_settings", lambda: Settings(_env_file=None, mcp_refresh_no_wait=False))
    with session_scope() as s:
        a = repo.upsert_athlete(s, repo.get_sport(s, "icehockey_women"), "Testa")
        tools.freshen(s, [a])
    assert got["wait_s"] is None  # switched off: waits auto_refresh_wait_s as before


def test_status_tools_wait_status_wait_s_not_auto_refresh_wait(db, monkeypatch):
    from klugscheisser.jobs import freshness
    from klugscheisser.mcp_server import server

    waits = []

    def fake_wait(s, job_id, wait_s, future=None):
        waits.append(wait_s)
        return None

    monkeypatch.setattr(freshness, "wait_for_job", fake_wait)
    st = Settings(_env_file=None, auto_refresh_wait_s=3, status_wait_s=60)
    monkeypatch.setattr(server, "get_settings", lambda: st)
    monkeypatch.setattr("klugscheisser.config.get_settings", lambda: st)
    with session_scope() as s:
        job = m.Job(kind="instagram_refresh", status="running", params={})
        s.add(job)
        s.flush()
        req = m.AddRequest(kind="person", status="loading", input={}, job_id=job.id, athlete_ids=[])
        s.add(req)
        s.flush()
        job_id, req_id = job.id, req.id
    server.refresh_status(job_id)
    assert "Daten werden geladen" in server.hinzufuegen_status(req_id)
    assert waits == [60, 60]


def test_running_hint_names_status_wait(db, monkeypatch):
    from klugscheisser.jobs import freshness

    st = Settings(_env_file=None, auto_refresh=True, auto_refresh_wait_s=0, status_wait_s=60)
    with session_scope() as s:
        a = person(s, "Testa Hinweis", 1, ig="testa_h")
        s.commit()
        f = freshness.ensure_fresh(s, st, [a], wait_s=0, submit=lambda *a, **k: None)
        assert f.status == "läuft" and "bis zu 60 Sekunden" in f.hinweis


def test_tool_calls_are_logged_with_duration_and_size(db, caplog):
    from klugscheisser.mcp_server import server

    with caplog.at_level(logging.INFO, logger="klugscheisser.tools"):
        server.list_events()
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("tool list_events done"))
    assert " s, " in line and "chars" in line and int(line.split(", ")[1].split()[0]) > 0
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    assert "name" in tools["get_athlete"].input_schema["properties"]  # schemas still from the real signature
    assert server.response_size("abc") == 3 and server.response_size({"a": 1}) == len('{"a": 1}')


# --- events: two writers ------------------------------------------------------------------------------


def _game(gid="g1"):
    from klugscheisser.interfaces import GameResult

    return GameResult(
        source="SIHF",
        sport="icehockey_women",
        competition="Testliga",
        external_id=gid,
        url=f"https://example.org/{gid}",
        status="scheduled",
        home="A",
        away="B",
    )


def test_two_writers_same_event_no_unique_violation(db):
    from klugscheisser.ingest.results import upsert_event

    first = Session(db)
    _, created, _ = upsert_event(first, "sihf", _game(), {})
    assert created
    out = {}

    def second():
        with Session(db) as s:
            try:
                out["r"] = upsert_event(s, "sihf", _game(), {})[1]
                s.commit()
            except Exception as exc:  # pragma: no cover - the bug this test guards against
                out["err"] = exc

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.5)  # the second writer waits for the first transaction
    first.commit()
    first.close()
    t.join(10)
    assert "err" not in out and out["r"] is False
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(m.Event).where(m.Event.key == "sihf-g1")) == 1


def test_one_results_refresh_at_a_time(db):
    from klugscheisser.ingest.results import RESULTS_LOCK, results_for_update

    with Session(db) as holder, Session(db) as other:
        assert holder.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": RESULTS_LOCK})
        assert results_for_update(other, Settings(_env_file=None), []) == {"status": "läuft bereits"}
        holder.rollback()  # lock released with the transaction
        other.rollback()
        assert results_for_update(other, Settings(_env_file=None), []) != {"status": "läuft bereits"}
