"""v11 part D: polite Elite Prospects fetcher (fake HTTP client and clock, never the network)."""

import json
import random
import sys
import types
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from klugscheisser.collectors.eliteprospects import fetch as ep
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.jobs import nightly

FIXTURE = json.loads(Path("tests/fixtures/eliteprospects/stalder_skater.json").read_text())
BLOCKS = {k: FIXTURE[k] for k in ep.KEEP}
T0 = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)  # 03:00 in Zurich


def page(blocks=BLOCKS, extra=None) -> str:
    props = {**blocks, "seoData": {"title": "x"}, "initialProjections": [1], **(extra or {})}
    data = json.dumps({"props": {"pageProps": props}, "page": "/player/[id]/[slug]"})
    return f'<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">{data}</script></body></html>'


CAPTCHA = "<html><title>Just a moment...</title><div class='cf-chl'>Please complete the captcha</div></html>"


class Clock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


class FakeHttp:
    """url -> queued answers (status, text, headers) or exceptions; unknown urls answer with a player page."""

    def __init__(self, answers=None, default=200):
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.default = default
        self.calls: list[tuple[str, dict]] = []
        self.cookies = httpx.Cookies({"session": "x"})

    def get(self, url, headers=None):
        assert not self.cookies, "cookies must be cleared before every request"
        self.calls.append((url, dict(headers or {})))
        queue = self.answers.get(url)
        answer = queue.pop(0) if queue else (self.default, page(), {})
        if isinstance(answer, Exception):
            raise answer
        status, text, hdrs = answer
        self.cookies.set("session", "y")  # the site sets a cookie; it must never be sent back
        return httpx.Response(status, text=text, headers=hdrs, request=httpx.Request("GET", url))


def settings(**kw) -> Settings:
    return Settings(ep_enabled=True, **kw)


def person(s, name, ep_id, tier=1, fetched_at=None, role="athlete"):
    sport = s.query(m.Sport).first() or m.Sport(key="icehockey_women", name="Eishockey")
    a = m.Athlete(
        sport=sport,
        full_name=name,
        name_normalized=name.lower(),
        eliteprospects_id=ep_id,
        tier=tier,
        role=role,
    )
    s.add(a)
    s.flush()
    if fetched_at is not None:
        s.add(m.EpProfile(athlete_id=a.id, ep_id=ep_id, url="old", fetched_at=fetched_at, raw={"old": 1}))
    s.commit()
    return a.id


def run(s, st, http, clock, **kw):
    return ep.fetch_profiles(s, st, http=http, clock=clock, sleep=clock.sleep, rng=random.Random(1), **kw)


def url(ep_id):
    return f"https://www.eliteprospects.com/player/{ep_id}"


# --- happy path ---------------------------------------------------------------------------------------


def test_fetch_stores_only_page_blocks_follows_player_redirect_and_keeps_import_data(db):
    with Session(db) as s:
        a = person(s, "Lara Stalder", "369459")
        b = person(s, "Alina Müller", "222", tier=2, fetched_at=T0 - timedelta(days=8))
        prof = s.get(m.EpProfile, b)
        prof.data, prof.imported_at = {"form": {"last5": 1}}, T0 - timedelta(days=8)
        s.commit()
        clock = Clock()
        http = FakeHttp(
            {url("369459"): [(301, "", {"location": "/player/369459/lara-stalder"})]},
        )
        report = run(s, settings(), http, clock)
        assert report.fetched == [a, b] and not report.failed and report.stopped is None
        assert [c[0] for c in http.calls] == [url("369459"), url("369459") + "/lara-stalder", url("222")]
        for _, headers in http.calls:
            assert headers["User-Agent"].startswith("Mozilla/5.0") and headers["Accept-Language"] == "en"
            assert "Cookie" not in headers
        s.expire_all()
        new = s.get(m.EpProfile, a)
        assert set(new.raw) == set(ep.KEEP) and new.raw["playerData"] == BLOCKS["playerData"]
        assert new.url == url("369459") + "/lara-stalder" and new.ep_id == "369459"
        assert new.fetched_at == T0 and new.data == {} and new.imported_at is None
        old = s.get(m.EpProfile, b)  # upsert: raw replaced, the importer's part untouched
        assert set(old.raw) == set(ep.KEEP) and old.url == url("222") and old.fetched_at > T0
        assert old.data == {"form": {"last5": 1}} and old.imported_at is not None
        # one gap between the two persons (redirect hop: none), 6–9 s
        assert len(clock.sleeps) == 1 and 6 <= clock.sleeps[0] <= 9
        row = s.get(m.SourceState, "eliteprospects")
        assert row.state["pages"] == 3 and row.state["day"] == "2026-10-05" and row.state["running"] is False


def test_gaps_between_requests_are_random_6_to_9_s_also_across_runs(db):
    with Session(db) as s:
        for i in range(5):
            person(s, f"P{i}", str(100 + i))
        clock = Clock()
        run(s, settings(), FakeHttp(), clock, limit=3)
        assert len(clock.sleeps) == 2 and all(6 <= x <= 9 for x in clock.sleeps)
        clock.sleep(2)  # next run starts 2 s after the last request: waits the rest of the gap
        clock.sleeps.clear()
        run(s, settings(), FakeHttp(), clock)
        assert len(clock.sleeps) == 2 and 4 <= clock.sleeps[0] <= 7 and 6 <= clock.sleeps[1] <= 9


def test_redirect_away_from_player_pages_is_not_followed(db):
    with Session(db) as s:
        a = person(s, "Lara Stalder", "1")
        http = FakeHttp({url("1"): [(302, "", {"location": "https://www.eliteprospects.com/login"})]})
        report = run(s, settings(), http, Clock())
        assert len(http.calls) == 1 and report.failed == [
            (a, "Lara Stalder", "Weiterleitung ausserhalb von /player/")
        ]
        assert s.get(m.EpProfile, a) is None
    assert ep.allowed_url(url("1")) and not ep.allowed_url("https://www.eliteprospects.com/team/1")
    assert not ep.allowed_url("http://www.eliteprospects.com/player/1")


# --- blocks -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, reason",
    [((403, "Forbidden", {}), "HTTP 403"), ((429, "", {}), "HTTP 429"), ((200, CAPTCHA, {}), "Captcha")],
)
def test_block_stops_at_once_and_pauses_seven_days(db, answer, reason):
    with Session(db) as s:
        person(s, "A", "1")
        person(s, "B", "2")
        clock = Clock()
        http = FakeHttp({url("1"): [answer]})
        report = run(s, settings(), http, clock)
        assert len(http.calls) == 1 and report.fetched == [] and reason in report.stopped
        row = s.get(m.SourceState, "eliteprospects")
        assert row.blocked_until == T0 + timedelta(days=7) and reason in row.state["block_reason"]
        assert "pausiert bis 12.10.2026" in ep.status_line(s, settings(), T0)
        # every later run inside the pause does nothing and says why
        for days in (1, 6):
            clock.t = T0 + timedelta(days=days)
            http2 = FakeHttp()
            again = run(s, settings(), http2, clock, force=True)
            assert http2.calls == [] and again.stopped.startswith("pausiert bis 12.10.2026")
        clock.t = T0 + timedelta(days=7, minutes=1)  # pause over
        http3 = FakeHttp()
        assert run(s, settings(), http3, clock).fetched and http3.calls


def test_429_with_long_retry_after_pauses_longer(db):
    with Session(db) as s:
        person(s, "A", "1")
        http = FakeHttp({url("1"): [(429, "", {"Retry-After": str(10 * 86400)})]})
        run(s, settings(), http, Clock())
        assert s.get(m.SourceState, "eliteprospects").blocked_until == T0 + timedelta(days=10)


def test_page_without_next_data_but_not_a_captcha_is_a_person_failure(db):
    with Session(db) as s:
        a = person(s, "A", "1")
        person(s, "B", "2")
        report = run(
            s, settings(), FakeHttp({url("1"): [(200, "<html>Player not found</html>", {})]}), Clock()
        )
        assert report.failed == [(a, "A", "keine Spielerdaten auf der Seite")] and len(report.fetched) == 1
        assert s.get(m.SourceState, "eliteprospects").blocked_until is None


# --- 5xx, timeouts, Retry-After -------------------------------------------------------------------------


def test_server_errors_back_off_30_60_120_then_stop_for_the_day(db):
    with Session(db) as s:
        person(s, "A", "1")
        person(s, "B", "2")
        clock = Clock()
        http = FakeHttp({url("1"): [(503, "", {})] * 4})
        report = run(s, settings(), http, clock)
        assert report.waits == [30, 60, 120] and len(http.calls) == 4 and report.fetched == []
        assert "für heute gestoppt" in report.stopped
        assert s.get(m.SourceState, "eliteprospects").state["stopped"]
        clock.t = T0 + timedelta(hours=1)  # same local day: nothing
        http2 = FakeHttp()
        assert "für heute gestoppt" in run(s, settings(), http2, clock).stopped and http2.calls == []
        clock.t = T0 + timedelta(days=1)  # next day: normal again
        assert len(run(s, settings(), FakeHttp(), clock).fetched) == 2


def test_retry_after_seconds_and_http_date_and_timeouts(db):
    with Session(db) as s:
        a = person(s, "A", "1")
        b = person(s, "B", "2")
        c = person(s, "C", "3")
        clock = Clock()
        when = format_datetime(T0 + timedelta(seconds=300), usegmt=True)
        http = FakeHttp(
            {
                url("1"): [(503, "", {"Retry-After": "90"})],
                url("2"): [(502, "", {"Retry-After": when})],
                url("3"): [httpx.ReadTimeout("slow"), httpx.ConnectError("down")],
            }
        )
        report = run(s, settings(), http, clock)
        assert report.fetched == [a, b, c]
        assert report.waits[0] == 90  # max(30, Retry-After)
        assert 200 <= report.waits[1] <= 300  # HTTP date, relative to the clock at that moment
        assert report.waits[2:] == [30, 60]


def test_too_long_retry_after_stops_for_the_day_instead_of_waiting(db):
    with Session(db) as s:
        person(s, "A", "1")
        report = run(s, settings(), FakeHttp({url("1"): [(503, "", {"Retry-After": "3600"})]}), Clock())
        assert report.waits == [] and "3600 s" in report.stopped and "für heute gestoppt" in report.stopped


def test_retry_after_parsing():
    assert ep.retry_after_s("120", T0) == 120
    assert ep.retry_after_s(format_datetime(T0 + timedelta(minutes=2), usegmt=True), T0) == 120
    assert ep.retry_after_s(format_datetime(T0 - timedelta(minutes=2), usegmt=True), T0) == 0
    assert ep.retry_after_s("soon", T0) is None and ep.retry_after_s(None, T0) is None


# --- 404 streak ----------------------------------------------------------------------------------------


def test_404_is_noted_and_five_in_a_row_stop_for_the_day(db):
    with Session(db) as s:
        ids = [person(s, f"P{i}", str(i)) for i in range(1, 8)]
        clock = Clock()
        http = FakeHttp({url(str(i)): [(404, "", {})] for i in range(1, 7)})
        report = run(s, settings(), http, clock)
        assert len(http.calls) == 5 and len(report.failed) == 5 and report.fetched == []
        assert "5 Seiten ohne Spielerdaten" in report.stopped
        assert all(s.get(m.EpProfile, i) is None for i in ids)
        # next day: the five failed ones wait EP_FAILED_RETRY_DAYS, the others go on
        clock.t = T0 + timedelta(days=1)
        http2 = FakeHttp({url("6"): [(404, "", {})]})
        report2 = run(s, settings(), http2, clock)
        assert [c[0] for c in http2.calls] == [url("6"), url("7")] and report2.fetched == [ids[6]]
        clock.t = T0 + timedelta(days=8)
        assert ids[0] in [d.athlete_id for d in run(s, settings(), FakeHttp(), clock, dry_run=True).due]


def test_single_404_does_not_stop_the_run(db):
    with Session(db) as s:
        person(s, "A", "1")
        b = person(s, "B", "2")
        report = run(s, settings(), FakeHttp({url("1"): [(404, "", {})]}), Clock())
        assert report.failed[0][2] == "nicht gefunden (HTTP 404)" and report.fetched == [b]


# --- daily cap -------------------------------------------------------------------------------------------


def test_daily_cap_and_reset_per_local_day(db):
    with Session(db) as s:
        for i in range(1, 5):
            person(s, f"P{i}", str(i))
        clock = Clock()
        report = run(s, settings(ep_daily_page_cap=2), FakeHttp(), clock)
        assert len(report.fetched) == 2 and "Tageslimit 2" in report.stopped and report.pages_today == 2
        http = FakeHttp()
        clock.t = T0 + timedelta(hours=20)  # 23:00 local, same day
        assert "Tageslimit" in run(s, settings(ep_daily_page_cap=2), http, clock).stopped and not http.calls
        clock.t = T0 + timedelta(hours=23)  # 02:00 next local day
        report = run(s, settings(ep_daily_page_cap=2), FakeHttp(), clock)
        assert len(report.fetched) == 2
        assert s.get(m.SourceState, "eliteprospects").state["day"] == "2026-10-06"


# --- schedule ------------------------------------------------------------------------------------------


def test_due_per_tier_new_first_then_oldest(db):
    with Session(db) as s:
        t1_yesterday = person(s, "T1 gestern", "11", 1, T0 - timedelta(days=1))
        person(s, "T1 heute", "12", 1, T0 - timedelta(minutes=30))
        t1_new = person(s, "T1 neu", "13", 1)
        person(s, "T2 vor 3 Tagen", "21", 2, T0 - timedelta(days=3))
        t2_week = person(s, "T2 vor 7 Tagen", "22", 2, T0 - timedelta(days=7))
        t2_old = person(s, "T2 vor 20 Tagen", "23", 2, T0 - timedelta(days=20))
        t3_month = person(s, "T3 vor 31 Tagen", "31", 3, T0 - timedelta(days=31))
        person(s, "T3 vor 10 Tagen", "32", 3, T0 - timedelta(days=10))
        person(s, "Coach", "41", 1, role="coach")  # staff pages are not /player/
        person(s, "Ohne EP", None, 1)
        due = ep.due_persons(s, settings(), now=T0)
        assert [d.athlete_id for d in due] == [t1_new, t1_yesterday, t2_old, t2_week]
        assert [d.reason for d in due] == [
            "neu",
            "Tier 1: täglich",
            "Tier 2: wöchentlich",
            "Tier 2: wöchentlich",
        ]
        due3 = ep.due_persons(s, settings(ep_tier3=True), now=T0)
        assert due3[-1].athlete_id == t3_month and due3[-1].reason == "Tier 3: monatlich"
        assert [d.athlete_id for d in ep.due_persons(s, settings(), tiers=[2], now=T0)] == [t2_old, t2_week]
        forced = ep.due_persons(s, settings(), tiers=[1], force=True, now=T0)
        assert len(forced) == 3 and forced[-1].reason == "erzwungen"
        # explicit persons ignore the tier filter (but not "due" unless forced)
        assert [d.athlete_id for d in ep.due_persons(s, settings(), athlete_ids=[t3_month], now=T0)] == [
            t3_month
        ]


def test_due_after_a_game_of_her_team(db):
    with Session(db) as s:
        a = person(s, "Spielerin", "1", 2, T0 - timedelta(days=2))
        b = person(s, "Andere", "2", 2, T0 - timedelta(days=2))
        c = person(s, "Aufstellung", "3", 2, T0 - timedelta(days=2))
        sport = s.query(m.Sport).first()
        zsc, evz, hcl = (m.Team(sport_id=sport.id, key=k, name=k) for k in ("zsc", "evz", "hcl"))
        s.add_all([zsc, evz, hcl])
        s.flush()
        s.add(m.Affiliation(athlete_id=a, team_id=zsc.id, season="2026-27"))
        s.add(m.Affiliation(athlete_id=b, team_id=hcl.id, season="2024-25"))  # old club: does not count
        s.add(
            m.Event(
                key="sihf-1",
                name="ZSC – EVZ",
                kind="game",
                status="final",
                starts_at=T0 - timedelta(hours=8),
                home_team_id=zsc.id,
                away_team_id=evz.id,
            )
        )
        s.add(
            m.Event(
                key="sihf-0",
                name="HCL – EVZ",
                kind="game",
                status="final",
                starts_at=T0 - timedelta(hours=9),
                home_team_id=hcl.id,
                away_team_id=evz.id,
            )
        )
        item = m.Item(
            source_type="official_result",
            platform="sdhl",
            url="https://stats.swehockey.se/Game/Events/1",
            content_hash="h",
            published_at=T0 - timedelta(hours=10),
        )
        s.add(item)
        s.flush()
        s.add(m.ItemAthlete(item_id=item.id, athlete_id=c))
        s.commit()
        due = ep.due_persons(s, settings(), now=T0)
        assert [d.athlete_id for d in due] == [a, c] and due[0].reason == "Spiel am 04.10."
        assert ep.due_persons(s, settings(ep_after_game=False), now=T0) == []
        # fetched after the game: not due any more
        s.get(m.EpProfile, a).fetched_at = T0 - timedelta(hours=1)
        s.commit()
        assert [d.athlete_id for d in ep.due_persons(s, settings(), now=T0)] == [c]


# --- dry-run, flag, force ---------------------------------------------------------------------------------


def test_dry_run_lists_due_and_makes_no_request_nor_state(db):
    with Session(db) as s:
        a = person(s, "A", "1")
        http = FakeHttp()
        report = run(s, settings(), http, Clock(), dry_run=True)
        assert http.calls == [] and [d.athlete_id for d in report.due] == [a]
        assert s.get(m.SourceState, "eliteprospects") is None and s.get(m.EpProfile, a) is None
        assert "Probelauf" in report.lines()[0] and "A (Tier 1): neu" in report.lines()[1]


def test_flag_off_does_nothing(db):
    with Session(db) as s:
        person(s, "A", "1")
        http = FakeHttp()
        report = ep.fetch_profiles(
            s, Settings(ep_enabled=False), http=http, clock=Clock(), sleep=Clock().sleep
        )
        assert http.calls == [] and report.stopped == "EP_ENABLED ist aus"
        assert ep.status_line(s, Settings(ep_enabled=False), T0) == "Elite Prospects: aus (EP_ENABLED)"
        assert Settings(ep_enabled=False).features["eliteprospects"] is False


def test_force_ignores_due_but_not_the_cap(db):
    with Session(db) as s:
        a = person(s, "A", "1", 1, T0 - timedelta(minutes=5))
        person(s, "B", "2", 1, T0 - timedelta(minutes=5))
        assert run(s, settings(), FakeHttp(), Clock()).due == []
        report = run(s, settings(ep_daily_page_cap=1), FakeHttp(), Clock(), force=True)
        assert report.fetched == [a] and "Tageslimit" in report.stopped


def test_a_second_run_while_one_is_running_does_nothing(db):
    with Session(db) as s:
        person(s, "A", "1")
        s.add(m.SourceState(source="eliteprospects", state={"running": True, "heartbeat": T0.isoformat()}))
        s.commit()
        http = FakeHttp()
        assert "anderer Lauf" in run(s, settings(), http, Clock()).stopped and not http.calls


# --- nightly + sources --------------------------------------------------------------------------------------


def test_nightly_window_once_per_day_and_importer_seam(db, monkeypatch):
    from klugscheisser.db.session import session_scope

    st = settings(ep_nightly_start_hour=3, ep_nightly_end_hour=5)  # the old early window (before the refresh)
    with Session(db) as s:
        a = person(s, "A", "1")
        assert nightly.ep_due(s, st, T0)  # 03:00 local, no refresh needed before
        assert not nightly.ep_due(s, st, T0 + timedelta(hours=2, minutes=30))  # 05:30
        assert not nightly.ep_due(s, st, T0 - timedelta(hours=2))  # 01:00
        assert not nightly.ep_due(s, Settings(ep_enabled=False), T0)
    calls = []
    fake = types.ModuleType("klugscheisser.ep_import")
    fake.import_profiles = lambda s, athlete_ids: calls.append(athlete_ids) or {"personen": len(athlete_ids)}
    monkeypatch.setitem(sys.modules, "klugscheisser.ep_import", fake)
    clock = Clock()
    out = nightly.run_ep_nightly(st, now=T0, http=FakeHttp(), clock=clock, sleep=clock.sleep)
    assert out["geholt"] == 1 and out["import"] == {"personen": 1} and calls == [[a]]
    with session_scope() as s:
        assert not nightly.ep_due(s, st, T0 + timedelta(minutes=10))  # done for today
        assert nightly.ep_due(s, st, T0 + timedelta(days=1))
    # the window ends the run: at 05:00 local nothing more is fetched
    with Session(db) as s:
        person(s, "B", "2")
    late = Clock(T0 + timedelta(hours=2))
    out = nightly.run_ep_nightly(st, now=T0, http=FakeHttp(), clock=late, sleep=late.sleep)
    assert out["geholt"] == 0 and out["gestoppt"] == "Nachtfenster vorbei"
    # pages not imported yet (the fake importer never sets imported_at) are retried with the next run
    assert nightly.import_fetched(st, []) == {"personen": 1} and calls[-1] == [a]
    with session_scope() as s:
        s.execute(update(m.EpProfile).values(imported_at=m.EpProfile.fetched_at + timedelta(hours=1)))
    assert nightly.import_fetched(st, []) is None


def test_sources_registry_says_what_is_read(db):
    from klugscheisser.sources_registry import general_sources, person_sources

    st = settings()
    info = next(x for x in general_sources(st) if x.name == "EliteProspects")
    assert (
        info.active
        and "6–9 s" in info.terms
        and "300/Tag" in info.terms
        and "robots.txt erlaubt" in info.terms
    )
    with Session(db) as s:
        a = person(s, "A", "1")
        row = next(r for r in person_sources(s, s.get(m.Athlete, a)) if r["name"] == "EliteProspects")
        assert row["info"] == "Profilseite noch nicht gelesen" and not row["aktiv"]
        run(s, st, FakeHttp(), Clock())
        s.expire_all()
        row = next(r for r in person_sources(s, s.get(m.Athlete, a)) if r["name"] == "EliteProspects")
        assert row["info"] == "Profilseite gelesen am 05.10.2026" and row["aktiv"]
        s.get(m.SourceState, "eliteprospects").blocked_until = datetime.now(UTC) + timedelta(days=7)
        s.commit()
    info = next(x for x in general_sources(st) if x.name == "EliteProspects")
    assert not info.active and "pausiert bis" in info.terms


def test_local_day_and_season():
    assert ep.local_day(datetime(2026, 10, 4, 22, 30, tzinfo=UTC)) == date(2026, 10, 5)
    assert ep.current_season(T0) == (2026, "2026-27")
    assert ep.current_season(datetime(2027, 3, 1, tzinfo=UTC)) == (2026, "2026-27")


# --- 2026-10-25: EP_SCOPE=event:<key> (only the WCh 2026 rosters), right after the nightly refresh ----------


def event(
    s, key="wch-2026", starts=datetime(2026, 11, 6, tzinfo=UTC), ends=datetime(2026, 11, 16, tzinfo=UTC)
):
    ev = m.Event(key=key, name="IIHF Frauen-WM 2026", kind="tournament", starts_at=starts, ends_at=ends)
    s.add(ev)
    s.flush()
    return ev.id


def entry(s, event_id, athlete_id, list_type="provisional", role="F"):
    s.add(m.EventEntry(event_id=event_id, athlete_id=athlete_id, list_type=list_type, role=role))
    s.commit()


WCH = settings(ep_scope="event:wch-2026")


def test_event_scope_reads_only_the_roster_each_page_once_per_day(db):
    with Session(db) as s:
        ev = event(s)
        a = person(s, "A", "1", tier=2, fetched_at=T0 - timedelta(days=21))  # oldest first after the new ones
        b = person(s, "B (ohne EP)", None)
        person(s, "C nicht im Kader", "3", tier=1)
        coach = person(s, "Coach", "4", role="coach")
        today = person(s, "D heute schon", "5", fetched_at=T0 - timedelta(minutes=30))
        new = person(s, "E neu", "6", tier=3)
        old = person(s, "F gestern", "7", tier=1, fetched_at=T0 - timedelta(hours=5))  # 22:00 local yesterday
        for aid, lt in [(a, "provisional"), (b, "confirmed"), (coach, "confirmed"), (today, "confirmed")]:
            entry(s, ev, aid, lt)
        entry(s, ev, new)
        entry(s, ev, old, "confirmed")
        dry = run(s, WCH, FakeHttp(), Clock(), dry_run=True)
        assert [d.athlete_id for d in dry.due] == [new, a, old]  # never fetched first, then oldest; no tiers
        assert dry.without_ep_id == [(b, "B (ohne EP)")] and "Kader wch-2026" in dry.lines()[0]
        assert "Ohne EP-ID (nicht geholt): 1" in dry.lines()
        assert ep.resolve_scope(s, "tiers", T0).kind == "tiers"
        http = FakeHttp()
        report = run(s, WCH, http, Clock())
        assert sorted(report.fetched) == sorted([new, a, old]) and len(http.calls) == 3
        assert report.out()["ohne_ep_id"] == 1 and report.out()["umfang"] == "Kader wch-2026"
        assert any("ohne EP-ID" in line for line in report.lines(300))
        later = Clock(T0 + timedelta(hours=3))  # same local day: nobody is due again
        assert run(s, WCH, FakeHttp(), later).due == []
        tomorrow = Clock(T0 + timedelta(days=1))
        assert len(run(s, WCH, FakeHttp(), tomorrow, dry_run=True).due) == 4  # incl. "D heute schon"
        assert "nur Kader wch-2026" in ep.status_line(s, WCH, T0)


def test_event_scope_keeps_the_daily_cap_and_the_block(db):
    with Session(db) as s:
        ev = event(s)
        ids = [person(s, f"P{i}", str(10 + i)) for i in range(4)]
        for aid in ids:
            entry(s, ev, aid)
        report = run(s, settings(ep_scope="event:wch-2026", ep_daily_page_cap=2), FakeHttp(), Clock())
        assert report.fetched == ids[:2] and "Tageslimit 2" in report.stopped
        dry = run(
            s, settings(ep_scope="event:wch-2026", ep_daily_page_cap=2), FakeHttp(), Clock(), dry_run=True
        )
        assert "Tageslimit: heute nur noch 0 Seiten" in dry.lines(2)[1]
        nxt = Clock(T0 + timedelta(days=1))
        http = FakeHttp(
            {url("12"): [(403, "<html><title>Just a moment...</title></html>", {"server": "cf"})]}
        )
        blocked = run(s, WCH, http, nxt)
        assert len(http.calls) == 1 and blocked.fetched == [] and "HTTP 403" in blocked.stopped
        row = s.get(m.SourceState, "eliteprospects")
        assert row.blocked_until == nxt.t + timedelta(days=7)
        assert row.state["block_detail"] == {
            "status": 403,
            "bytes": 44,
            "title": "Just a moment...",
            "captcha": True,
            "server": "cf",
        }


def test_event_scope_unknown_or_over_reads_nothing(db):
    with Session(db) as s:
        a = person(s, "A", "1")
        http = FakeHttp()
        assert "nicht gefunden" in run(s, WCH, http, Clock()).stopped
        assert "unbekannt" in run(s, settings(ep_scope="wm"), http, Clock()).stopped
        ev = event(s, ends=datetime(2026, 10, 3, 12, tzinfo=UTC))  # ended 03.10., T0 is 05.10.
        entry(s, ev, a)
        over = run(s, WCH, http, Clock())
        assert "ist vorbei (03.10.2026)" in over.stopped and http.calls == []
        assert run(s, WCH, http, Clock(), athlete_ids=[a]).fetched == [a]  # by hand (--person) still works
        s.execute(update(m.Event).values(ends_at=datetime(2026, 10, 4, 12, tzinfo=UTC)))
        s.commit()
        tomorrow = Clock(T0 + timedelta(days=1))  # 06.10.: the night after 04.10.+1 day is the last one
        assert "vorbei" in run(s, WCH, FakeHttp(), tomorrow).stopped
        assert run(s, WCH, FakeHttp(), Clock(), force=True).fetched == [a]  # 05.10. still read


def test_nightly_ep_runs_right_after_the_refresh(db):
    st = settings()  # defaults: 05:00–09:00, start = NIGHTLY_HOUR
    at = T0 + timedelta(hours=2, minutes=20)  # 05:20 local
    with Session(db) as s:
        assert (st.ep_nightly_start_hour, st.ep_nightly_end_hour, st.nightly_hour) == (5, 9, 5)
        assert not nightly.ep_due(s, st, at)  # refresh not run yet today
        s.add(
            m.CacheEntry(
                source=nightly.MARKER[0], target=nightly.MARKER[1], fetched_at=at - timedelta(minutes=15)
            )
        )
        job = m.Job(kind="nightly", status="running", started_at=at - timedelta(minutes=15))
        s.add(job)
        s.commit()
        assert not nightly.ep_due(s, st, at)  # still running: never in parallel
        job.started_at = at - timedelta(hours=7)  # a crashed run does not block EP for good
        s.commit()
        assert nightly.ep_due(s, st, at)
        job.status, job.started_at = "done", at - timedelta(minutes=15)
        s.commit()
        assert nightly.ep_due(s, st, at)
        assert not nightly.ep_due(s, st, T0 + timedelta(hours=7))  # 10:00: window over
        start, end = nightly.ep_window(st, at)
        assert (start.hour, end.hour) == (5, 9)


def test_stored_final_url_saves_the_redirect_hop_and_falls_back_to_the_id(db):
    with Session(db) as s:
        a = person(s, "A", "1", fetched_at=T0 - timedelta(days=2))
        b = person(s, "B", "2", fetched_at=T0 - timedelta(days=2))
        s.get(m.EpProfile, a).url = url("1") + "/a-name"
        s.get(m.EpProfile, b).url = url("2") + "/old-slug"
        s.commit()
        http = FakeHttp(
            {
                url("2") + "/old-slug": [(404, "", {})],
                url("2"): [(301, "", {"location": "/player/2/new-slug"})],
            }
        )
        report = run(s, settings(), http, Clock())
        assert report.fetched == [a, b] and not report.failed
        assert [c[0] for c in http.calls] == [
            url("1") + "/a-name",  # one request, no redirect
            url("2") + "/old-slug",
            url("2"),
            url("2") + "/new-slug",
        ]
        s.expire_all()
        assert s.get(m.EpProfile, b).url == url("2") + "/new-slug"
        assert ep.page_url("1", url("9") + "/other") == url("1")  # another id's page is never used
