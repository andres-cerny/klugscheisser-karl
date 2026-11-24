"""2026-10-25 (docs/search-lab/timing.md quick wins): bot page views start no paid update, the media worker backs
off exponentially after rate limits, Wikipedia lookups for never-seen persons leave the request path, and every MCP
tool call gets one `mcp_call` log line."""

import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser import calllog
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.seed.loader import load_all_seeds

# --- bots: no paid update from a page view -------------------------------------------------------------------

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.6 Mobile/15E148 Safari/604.1"
)
HEADLESS = CHROME.replace("Chrome/", "HeadlessChrome/")


@pytest.mark.parametrize(
    "ua,bot",
    [
        (CHROME, False),
        (IPHONE, False),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", False),
        (HEADLESS, True),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", True),
        ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0)", True),
        ("curl/8.14.1", True),
        ("python-httpx/0.28.1", True),
        ("Perplexity-MCP/1.0", True),
        ("facebookexternalhit/1.1", True),
        ("", True),
        (None, True),
    ],
)
def test_bot_user_agents(ua, bot):
    from klugscheisser.web.bots import is_bot_user_agent

    assert is_bot_user_agent(ua) is bot


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        pid = s.scalars(select(m.Athlete.id).order_by(m.Athlete.id)).first()
    return db, pid


def _person_page_updates(monkeypatch, settings, seeded, headers) -> int:
    pid = seeded[1]
    from klugscheisser.web import build_web_app
    from klugscheisser.web import data as d

    calls = []
    monkeypatch.setattr(d, "start_background_update", lambda s, st, athletes: calls.append(athletes) or None)
    with TestClient(build_web_app(settings)) as c:
        assert c.get(f"/person/{pid}", headers=headers).status_code == 200
    return len(calls)


SETTINGS = Settings(_env_file=None, auto_refresh=True, web_answer_engine="fake")


def test_real_browser_page_view_still_starts_the_update(seeded, monkeypatch):
    assert _person_page_updates(monkeypatch, SETTINGS, seeded, {"User-Agent": CHROME}) == 1


def test_headless_and_bot_page_views_start_no_update(seeded, monkeypatch):
    assert _person_page_updates(monkeypatch, SETTINGS, seeded, {"User-Agent": HEADLESS}) == 0
    assert _person_page_updates(monkeypatch, SETTINGS, seeded, {"User-Agent": "Googlebot/2.1"}) == 0
    hints = {"User-Agent": CHROME, "Sec-CH-UA": '"HeadlessChrome";v="141", "Chromium";v="141"'}
    assert _person_page_updates(monkeypatch, SETTINGS, seeded, hints) == 0


def test_bot_switch_restores_the_old_behaviour(seeded, monkeypatch):
    on = Settings(_env_file=None, auto_refresh=True, web_answer_engine="fake", web_refresh_for_bots=True)
    assert _person_page_updates(monkeypatch, on, seeded, {"User-Agent": HEADLESS}) == 1


# --- media worker: exponential backoff ----------------------------------------------------------------------


def test_backoff_doubles_up_to_the_cap_and_resets():
    from klugscheisser.media.worker import RateLimitBackoff

    b = RateLimitBackoff(max_s=300)
    assert [b.hit(60, now=0) for _ in range(5)] == [60, 120, 240, 300, 300]
    assert b.paused(now=299) and not b.paused(now=301)
    b.reset()
    assert b.hits == 0 and not b.paused(now=0)
    assert b.hit(60, now=0) == 60
    # a base above the cap (30 min for the Claude subscription) is never shortened by the cap
    assert RateLimitBackoff(max_s=60).hit(1800, now=0) == 1800


@dataclass
class _P:
    key: str


def test_rate_limit_pauses_all_projects_despite_short_idle(db, monkeypatch):
    """The bug of 22.-23.10.: the other project's 10 s idle wait cut the pause short (6,032 retries)."""
    from klugscheisser import projects
    from klugscheisser.media import worker
    from klugscheisser.media.analyzer import FakeMediaAnalyzer

    calls = []

    def fake_queue(s, settings, analyzer, **kw):
        calls.append(time.monotonic())
        return worker.MediaReport(rate_limited=True)

    monkeypatch.setattr(worker, "process_media_queue", fake_queue)
    monkeypatch.setattr(projects, "active_projects", lambda: [_P("eishockey"), _P("allgemein")])
    monkeypatch.setattr(projects, "use", lambda p: nullcontext(p))
    stop = threading.Event()
    t = worker.start_background_worker(
        Settings(_env_file=None), analyzer=FakeMediaAnalyzer(), idle_s=0.01, stop=stop
    )
    time.sleep(1.0)
    stop.set()
    t.join(timeout=5)
    assert len(calls) == 1  # paused 60 s after the first rate limit; no retry of either project


def test_success_resets_and_idle_projects_keep_their_own_wait(db, monkeypatch):
    from klugscheisser import projects
    from klugscheisser.media import worker
    from klugscheisser.media.analyzer import FakeMediaAnalyzer

    calls: dict[str, int] = {}
    current = {"key": None}

    def fake_queue(s, settings, analyzer, **kw):
        calls[current["key"]] = calls.get(current["key"], 0) + 1
        return worker.MediaReport()  # nothing queued: idle wait

    def use(p):
        current["key"] = p.key
        return nullcontext(p)

    monkeypatch.setattr(worker, "process_media_queue", fake_queue)
    monkeypatch.setattr(projects, "active_projects", lambda: [_P("a"), _P("b")])
    monkeypatch.setattr(projects, "use", use)
    stop = threading.Event()
    t = worker.start_background_worker(
        Settings(_env_file=None), analyzer=FakeMediaAnalyzer(), idle_s=0.2, stop=stop
    )
    time.sleep(1.1)
    stop.set()
    t.join(timeout=5)
    assert 2 <= calls["a"] <= 8 and 2 <= calls["b"] <= 8  # every ~0.2 s, not in a tight loop


# --- ensure_profiles off the request path -------------------------------------------------------------------


class _Pool:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))


def test_profiles_lookup_runs_in_background_once(seeded, monkeypatch):
    db, pid = seeded
    from klugscheisser.ingest import profiles
    from klugscheisser.jobs import refresh

    pool = _Pool()
    monkeypatch.setattr(refresh, "executor", lambda: pool)
    looked_up = []
    monkeypatch.setattr(profiles, "_lookup", lambda s, st, todo: looked_up.append([a.id for a in todo]))
    st = Settings(_env_file=None, profiles_enabled=True)
    assert st.profiles_in_background
    with Session(db) as s:
        a = s.get(m.Athlete, pid)
        assert profiles.ensure_profiles(s, st, [a]) == 1
        assert profiles.ensure_profiles(s, st, [a]) == 0  # already being looked up (parallel fan-out)
    assert looked_up == [] and len(pool.jobs) == 1  # the request did not fetch anything itself
    fn, args = pool.jobs[0]
    fn(*args)  # the pool thread
    assert looked_up == [[pid]]
    with Session(db) as s:  # done: a later question may start it again (the real lookup sets the mark)
        assert profiles.ensure_profiles(s, st, [s.get(m.Athlete, pid)]) == 1


def test_profiles_lookup_in_request_with_switch_off(seeded, monkeypatch):
    db, pid = seeded
    from klugscheisser.ingest import profiles
    from klugscheisser.jobs import refresh

    pool = _Pool()
    monkeypatch.setattr(refresh, "executor", lambda: pool)
    looked_up = []
    monkeypatch.setattr(profiles, "_lookup", lambda s, st, todo: looked_up.append([a.id for a in todo]))
    st = Settings(_env_file=None, profiles_enabled=True, profiles_in_background=False)
    with Session(db) as s:
        assert profiles.ensure_profiles(s, st, [s.get(m.Athlete, pid)]) == 1
    assert looked_up == [[pid]] and pool.jobs == []


# --- one MCP call-log line per tool call --------------------------------------------------------------------


def test_call_notes_collect_cost_cache_refresh(db):
    from klugscheisser.ingest import costs
    from klugscheisser.jobs.freshness import ensure_fresh

    calllog.note("cache", "hit")  # outside a call: no-op
    notes, token = calllog.start()
    try:
        with Session(db) as s:
            costs.record(s, "llm:claude", 1, 0.0123, "test")
            costs.record(s, "jev", 1, 0.0005, "test")
            ensure_fresh(s, Settings(_env_file=None, auto_refresh=False), [])
            s.rollback()
        calllog.note("cache", "miss")
        calllog.note("cache", "hit")  # the first word counts
    finally:
        calllog.end(token)
    assert calllog.current() is None
    assert notes.cost_rows == 2 and abs(notes.cost_usd - 0.0128) < 1e-9
    line = calllog.format_line("frage", 2.345, 1985, False, False, notes, "question='Wer?'")
    assert line == (
        "mcp_call tool=frage dur=2.35s chars=1985 capped=no err=no ua=- cost=$0.0128 cache=miss "
        "refresh=aus:0.0s args=question='Wer?'"
    )


def test_args_summary_and_short_ua():
    s = calllog.summarize_args(
        ("x" * 200,), {"person": "Lara  Stalder", "days": 7, "team": None}, ["question"]
    )
    assert s.startswith("question='" + "x" * 79 + "…'") and "person='Lara Stalder' days=7" in s
    assert "team" not in s
    assert calllog.short_ua("Perplexity-MCP/1.0") == "Perplexity-MCP/1.0"
    assert calllog.short_ua(HEADLESS) == "HeadlessChrome/141.0.0.0"
    assert calllog.short_ua(None) == "-"


def test_mcp_tool_call_logs_one_line_with_user_agent(db, caplog):
    from klugscheisser.mcp_server.server import build_app

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
        "Authorization": "Bearer secret",
        "User-Agent": "Perplexity-MCP/1.0",
    }
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_events", "arguments": {"days": 30}},
    }
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with (
        caplog.at_level(logging.INFO, logger="klugscheisser.tools"),
        TestClient(app, base_url="http://127.0.0.1:8000") as c,
    ):
        assert c.post("/mcp", json=call, headers=headers).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("mcp_call ")]
    assert len(lines) == 1
    line = lines[0]
    assert "tool=list_events " in line and "ua=Perplexity-MCP/1.0 " in line and "args=days=30" in line
    assert "err=no" in line and "cost=$0" in line and "secret" not in line
