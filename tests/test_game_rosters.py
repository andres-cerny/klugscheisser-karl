"""Rosters around WCh games (jobs/game_rosters.py + the game watch follow-up): players who dressed but are not on
Karl's tournament roster are added from the stored IIHF sheet (confirmed, source = game sheet + date), nobody is
removed, unclear cases are only logged; the follow-up runs the roster watcher for the two teams of the game.
Recorded IIHF JSON (respx) and fakes only, no network, no paid call."""

import copy
from datetime import date

import respx
from sqlalchemy import func, select
from test_gamewatch import (  # noqa: F401  (wch is a fixture)
    KEY,
    FakeInstagram,
    FakeNews,
    Routes,
    feed,
    final_state,
    run,
    settings,
    wch,
)

from klugscheisser.collectors.results import iihf
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.jobs import game_rosters as gr
from klugscheisser.jobs import gamewatch as gw
from klugscheisser.jobs import roster_watch
from klugscheisser.seed.loader import GAME_SHEET_SOURCE

GAME_URL = "https://www.iihf.com/en/events/2026/ww/gamecenter/playbyplay/75141/3-sui-vs-usa"


def _actor(given: str, family: str, number: str, pos: str = "F", noc: str = "SUI") -> dict:
    return {
        "NOC_Code": noc,
        "Number": number,
        "Position": pos,
        "ReportingName": f"{family.upper()} {given}",
        "FamilyName": family.upper(),
        "GivenName": given,
    }


def state_with(*actors: dict, period: int = 0) -> dict:
    """The recorded final with the first goal's assists replaced by `actors` (who dressed for that team)."""
    st = copy.deepcopy(final_state())
    goal = st["Periods"][period]["ScoringActions"][0]
    goal["Assistant1"] = actors[0] if actors else None
    goal["Assistant2"] = actors[1] if len(actors) > 1 else None
    for extra in actors[2:]:  # more names: further goals of the same team would carry them; enough as assists
        st["Periods"][period].setdefault("ScoringActions", []).append(
            {**copy.deepcopy(goal), "Scorer": extra, "Assistant1": None, "Assistant2": None}
        )
    return st


def wch_entry(s, athlete_id: int) -> m.EventEntry | None:
    ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
    return s.scalar(
        select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.athlete_id == athlete_id)
    )


def team_entries(s, code: str) -> list[m.EventEntry]:
    ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
    team = s.scalar(select(m.Team).where(m.Team.key == f"{code.lower()}-women"))
    return list(
        s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id))
    )


def test_sheet_players_from_the_iihf_state():
    ev = iihf.IihfEvent(event_id=773, path="2026/ww", site_id_offset=75138)
    game = iihf.game_from_state(final_state(), ev)
    rows = {(p["team"], p["name"]): p for p in game.raw["sheet_players"]}
    muller = rows[("SUI", "Alina Muller")]
    assert muller["number"] == 25 and muller["pos"] == "F" and "scorer" in muller["roles"]
    goalies = [p for p in rows.values() if "goalie" in p["roles"]]
    assert {p["team"] for p in goalies} == {"SUI", "USA"} and all(p["pos"] == "G" for p in goalies)


@respx.mock
def test_the_recorded_sheet_changes_no_roster(wch):  # noqa: F811
    """Everyone on the recorded sheet is on the seeded official lists (spellings without umlauts included)."""
    r = Routes(respx)
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    with session_scope() as s:
        before = {c: len(team_entries(s, c)) for c in ("SUI", "USA")}
    out = run(settings(game_watch_schedule_hours=0), 152)
    assert "kader_spielbericht" in out["polls"][0] and out["polls"][0]["kader_spielbericht"] == {}
    with session_scope() as s:
        assert {c: len(team_entries(s, c)) for c in ("SUI", "USA")} == before
        state = s.get(m.SourceState, gr.STATE).state[KEY]
        assert state["SUI"]["dazu"] == [] and state["SUI"]["unklar"] == []
        assert state["SUI"]["nicht_eingesetzt"] is None  # no line-ups in the IIHF feed: unknown, not "all"


@respx.mock
def test_late_addition_added_once_with_game_sheet_source(wch):  # noqa: F811
    with session_scope() as s:
        sport = repo.get_sport(s, "icehockey_women")
        known = repo.upsert_athlete(s, sport, "Lea Testhofer", nationality="SUI", position="D")
        known_id = known.id
        assert wch_entry(s, known_id) is None
    r = Routes(respx)
    r.feed = feed("FINAL", "3", "2")
    r.state = state_with(_actor("Lea", "Testhofer", "88", "D"), _actor("Nina", "Neuhaus", "77", "F"))
    st = settings(game_watch_schedule_hours=0)
    out = run(st, 152)
    added = out["polls"][0]["kader_spielbericht"][KEY]["SUI"]["dazu"]
    assert added == ["Lea Testhofer (Nr. 88)", "Nina Neuhaus (Nr. 77) – neue Person"]
    with session_scope() as s:
        e = wch_entry(s, known_id)
        assert e.list_type == "confirmed" and e.role == "D" and e.jersey_number == 88
        assert e.source == f"{GAME_SHEET_SOURCE}: Schweiz – USA (06.11.2026)" and e.source_url == GAME_URL
        assert e.team.key == "sui-women"
        new = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Nina Neuhaus"))
        assert new.nationality == "SUI" and new.position == "F"
        assert wch_entry(s, new.id).list_type == "confirmed"
        assert (
            s.scalar(
                select(func.count()).select_from(m.Athlete).where(m.Athlete.full_name == "Lea Testhofer")
            )
            == 1
        )
        n = len(team_entries(s, "SUI"))
        # the tournament sheet names the late additions with source and date; the list's source stays the official one
        from klugscheisser import tournament

        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        team = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        status = tournament.roster_status(s, ev, team)
        assert not status["quelle"].startswith(GAME_SHEET_SOURCE)
        assert any(
            "Lea Testhofer (Nr. 88, D): nachgemeldet" in x for x in status["laut_spielbericht_nachgemeldet"]
        )
        # the CLI status shows it per game
        row = {x["key"]: x for x in gw.status(s, st)}[KEY]
        assert row["kader_spielbericht"]["SUI"]["dazu"][0].startswith("Lea Testhofer")
    # the re-check reads the same sheet: nothing added twice
    run(st, 163)
    with session_scope() as s:
        assert len(team_entries(s, "SUI")) == n


@respx.mock
def test_unclear_cases_are_logged_not_added(wch):  # noqa: F811
    with session_scope() as s:
        sui = team_entries(s, "SUI")
        taken = next(e for e in sui if e.jersey_number)
        usa_player = next(e for e in team_entries(s, "USA") if e.role == "F").athlete
        sport = repo.get_sport(s, "icehockey_women")
        for nation in ("SUI", "SUI"):  # two Swiss persons of the same name
            s.add(
                m.Athlete(
                    sport_id=sport.id,
                    full_name="Sara Doppel",
                    name_normalized="sara doppel",
                    nationality=nation,
                )
            )
        s.flush()
        for a in s.scalars(select(m.Athlete).where(m.Athlete.full_name == "Sara Doppel")):
            repo.add_aliases(s, a, ["Sara Doppel"])
        number, holder, n = taken.jersey_number, taken.athlete.full_name, len(sui)
        given, family = usa_player.full_name.split(" ", 1)
    r = Routes(respx)
    r.feed = feed("FINAL", "3", "2")
    r.state = state_with(
        _actor("Petra", "Nummer", str(number)),  # number held by another listed player
        _actor("Sara", "Doppel", "95"),  # two persons of that name
        _actor(given, family, "96"),  # on the USA list
    )
    out = run(settings(game_watch_schedule_hours=0), 152)
    sui_out = out["polls"][0]["kader_spielbericht"][KEY]["SUI"]
    assert sui_out["dazu"] == []
    text = " | ".join(sui_out["unklar"])
    assert f"Nummer hat laut Kader {holder}" in text and "2 Personen dieses Namens" in text
    assert "anderen Teams" in text
    with session_scope() as s:
        assert len(team_entries(s, "SUI")) == n


@respx.mock
def test_too_many_new_names_are_not_applied(wch):  # noqa: F811
    r = Routes(respx)
    r.feed = feed("FINAL", "3", "2")
    r.state = state_with(*[_actor(f"Neu{i}", f"Spielerin{i}", str(60 + i)) for i in range(gr.MAX_ADDS + 1)])
    with session_scope() as s:
        n = len(team_entries(s, "SUI"))
    out = run(settings(game_watch_schedule_hours=0), 152)
    sui_out = out["polls"][0]["kader_spielbericht"][KEY]["SUI"]
    assert sui_out["dazu"] == [] and "nicht übernommen" in sui_out["unklar"][0]
    with session_scope() as s:
        assert len(team_entries(s, "SUI")) == n


@respx.mock
def test_switch_off_leaves_rosters_alone(wch):  # noqa: F811
    r = Routes(respx)
    r.feed, r.state = feed("FINAL", "3", "2"), state_with(_actor("Nina", "Neuhaus", "77"))
    out = run(settings(game_watch_schedule_hours=0, game_watch_sheet_rosters=False), 152)
    assert "kader_spielbericht" not in out["polls"][0]
    with session_scope() as s:
        assert s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Nina Neuhaus")) is None


def test_full_lineup_logs_not_dressed_and_removes_nobody(wch):  # noqa: F811
    with session_scope() as s:
        game = s.scalar(select(m.Event).where(m.Event.key == KEY))
        game.status = "final"
        sui = team_entries(s, "SUI")
        players = [e for e in sui if e.role in ("G", "D", "F")]
        dressed, absent = players[:-1], players[-1]
        item = m.Item(
            source_type="official_result",
            platform="iihf",
            external_id="13547",
            url=GAME_URL,
            title="Schweiz – USA",
            text="Schweiz – USA 3:2",
            content_hash="x-lineup",
            raw={"lineup": {"Schweiz": [e.athlete.full_name for e in dressed]}},
        )
        s.add(item)
        s.flush()
        report = gr.reconcile_game(s, game, item, date(2026, 11, 6))
        assert report["SUI"]["dazu"] == [] and report["SUI"]["nicht_eingesetzt"] == [absent.athlete.full_name]
        assert wch_entry(s, absent.athlete_id) is not None  # logged only, still on the list
        assert report["USA"]["nicht_eingesetzt"] is None  # no USA line-up on this sheet


def test_official_list_never_drops_a_game_sheet_addition(wch):  # noqa: F811
    """The next kaderwatch run with the official list (which does not name her yet) keeps the late addition."""
    with session_scope() as s:
        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        team = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        sport = repo.get_sport(s, "icehockey_women")
        late = repo.upsert_athlete(s, sport, "Nina Neuhaus", nationality="SUI", position="F")
        official = [
            {
                "athlete_id": e.athlete_id,
                "name": e.athlete.full_name,
                "pos": e.role,
                "number": e.jersey_number,
            }
            for e in team_entries(s, "SUI")
            if e.role in ("G", "D", "F")
        ]
        s.add(
            m.EventEntry(
                event_id=ev.id,
                athlete_id=late.id,
                team_id=team.id,
                role="F",
                jersey_number=77,
                list_type="confirmed",
                source=f"{GAME_SHEET_SOURCE}: Schweiz – USA (06.11.2026)",
                source_url=GAME_URL,
            )
        )
        s.flush()
        official[0] = {**official[0], "number": 98}  # a changed number: the list is loaded again
        change = roster_watch.apply_official(
            s,
            ev,
            team,
            "SUI",
            official,
            "Kaderwatch: SIHF, offizieller WM-Kader (07.11.2026)",
            "https://www.sihf.ch/de/news/x",
            date(2026, 11, 7),
            {},
        )
        assert change["status"] == "übernommen" and change["weg"] == []
        e = wch_entry(s, late.id)
        assert e is not None and e.source.startswith(GAME_SHEET_SOURCE)


class FakeRosterWatch:
    def __init__(self):
        self.calls = []

    def __call__(self, s, codes):
        self.calls.append(set(codes))
        return {
            "geprueft": sorted(codes),
            "kader": {
                "SUI": {
                    "status": "übernommen",
                    "dazu": ["Nina Neuhaus"],
                    "weg": [],
                    "quelle": "Kaderwatch: SIHF",
                }
            },
            "fehler": {"USA": "HTTP 403, Bot-Schutz der Seite (nicht umgangen)"},
            "neu": [],
        }


@respx.mock
def test_followup_runs_the_roster_watcher_for_both_teams(wch):  # noqa: F811
    r = Routes(respx)
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    st = settings(game_watch_schedule_hours=0, game_watch_roster_watch=True)
    rw = FakeRosterWatch()
    run(st, 152, roster_watch=rw)
    assert rw.calls == []  # not at the final, only with the follow-up
    out = run(st, 213, roster_watch=rw, instagram_source=FakeInstagram(), news_collector=FakeNews())
    assert len(out["follow_ups"]) == 1 and rw.calls == [{"SUI", "USA"}]
    with session_scope() as s:
        job = s.scalar(select(m.Job).where(m.Job.kind == gw.JOB_KIND))
        assert job.status == "done"  # a federation page behind bot protection is no failure
        kw = job.result["kaderwatch"]
        assert kw["SUI"]["dazu"] == ["Nina Neuhaus"] and "Bot-Schutz" in kw["fehler_seiten"]["USA"]
        assert "Kaderwatch SUI/USA: SUI +1/-0" in gw.summarize(job.result)
    run(st, 240, roster_watch=rw)
    assert len(rw.calls) == 1  # once per game


@respx.mock
def test_roster_watcher_runs_also_when_the_llm_cap_is_used_up(wch):  # noqa: F811
    from klugscheisser.ingest import costs
    from klugscheisser.llm import text as llm_text

    r = Routes(respx)
    r.feed, r.state = feed("FINAL", "3", "2"), final_state()
    st = settings(game_watch_schedule_hours=0, game_watch_roster_watch=True, llm_daily_cost_cap_usd=1.0)
    run(st, 152)
    with session_scope() as s:
        costs.record(s, llm_text.cost_source(st, "answer"), 1, 1.5, "answers today")
    rw, ig = FakeRosterWatch(), FakeInstagram()
    run(st, 213, roster_watch=rw, instagram_source=ig)
    assert rw.calls == [{"SUI", "USA"}] and ig.calls == []
    # off switch
    r2 = settings(game_watch_schedule_hours=0, game_watch_roster_watch=False)
    with session_scope() as s:
        s.query(m.CacheEntry).filter(m.CacheEntry.source == gw.SRC_FOLLOWUP).delete()
    run(r2, 214, roster_watch=rw)
    assert len(rw.calls) == 1


def test_roster_watcher_only_checks_the_given_teams(wch):  # noqa: F811
    class Down:
        def get(self, url):
            raise RuntimeError("offline in tests")

    class NoMirror:
        def rosters(self, event):
            return [], []

    with session_scope() as s:
        report = roster_watch.run(
            s, client=Down(), mirror_client=NoMirror(), only={"SUI", "USA"}, today=date(2026, 11, 4)
        )  # fixed date: the WCh roster watch ends on 2026-11-16 (seeds/events/wch-2026.yaml)
        assert sorted(report["geprueft"]) == ["SUI", "USA"]
