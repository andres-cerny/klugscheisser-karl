"""Team games: last games / form / head-to-head from all stored games (list_events, frage), and the backfill of
past tournaments (IIHF realtime game states by id range, Wikipedia ice hockey boxes). Fixtures only, no network."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.answer import cross, lookups, team_games
from klugscheisser.collectors.results import iihf, wiki_games
from klugscheisser.collectors.results.base import PoliteHttp
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.results import backfill_collectors, enabled_collectors, ingest_results
from klugscheisser.ingest.runner import build_context
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.present import render_markdown
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/results")
D1A = iihf.IihfEvent.from_config(
    {
        "event_id": 793,
        "path": "2025/wwia",
        "competition": "IIHF Frauen-WM 2025 Division I Gruppe A",
        "competition_key": "wchd1a-2025",
        "game_ids": [12331, 12333],
        "tz": "Asia/Shanghai",
        "tournament": {"prefix": "wchd1a", "key": "wchd1a-2025", "name": "Div I A 2025", "city": "Shenzhen"},
    }
)
OLY = wiki_games.WikiTournament(
    "Ice hockey at the 2026 Winter Olympics – Women's tournament",
    "Olympische Winterspiele 2026 Frauen-Eishockey",
    "oly-2026",
    "Europe/Rome",
    {"prefix": "oly", "key": "oly-2026", "name": "Olympia 2026", "city": "Mailand (ITA)"},
)


def state():
    return json.loads((FIX / "iihf_state_12332.json").read_text())


def wikitext():
    return (FIX / "wiki_oly2026_boxes.txt").read_text()


# --- IIHF backfill (past events) ----------------------------------------------------------------------


def test_iihf_game_from_state_local_time_and_link():
    g = iihf.game_from_state(state(), D1A)
    assert (g.home, g.away, g.score, g.status) == ("Österreich", "Niederlande", "3:2 n.V.", "final")
    assert g.starts_at == datetime(2025, 4, 17, 8, 30, tzinfo=UTC)  # 16:30 Shenzhen
    assert g.url == "https://realtime.iihf.com/gamestate/GetLatestState/12332"  # iihf.com page id unknown
    assert g.raw["competition_key"] == "wchd1a-2025" and g.raw["tournament"]["key"] == "wchd1a-2025"
    assert g.competition == "IIHF Frauen-WM 2025 Division I Gruppe A"
    assert any(p.role == "scorer" for p in g.participants) and g.raw["detail"]
    assert "Athlete_Id" not in json.dumps(state())  # birth data never in fixtures
    other = iihf.IihfEvent(event_id=712, game_ids=(1, 2))
    assert iihf.game_from_state(state(), other) is None and iihf.game_from_state(None, D1A) is None


@respx.mock
def test_iihf_collector_scans_the_id_range_once():
    respx.get(f"{iihf.API}/GetLatestState/12331").mock(return_value=httpx.Response(200, json=None))
    respx.get(f"{iihf.API}/GetLatestState/12332").mock(return_value=httpx.Response(200, json=state()))
    other = {**state(), "EventId": "796", "GameId": "12333"}
    respx.get(f"{iihf.API}/GetLatestState/12333").mock(return_value=httpx.Response(200, json=other))
    c = iihf.IihfCollector([D1A], http=PoliteHttp(0))
    games = c.collect()
    assert [g.external_id for g in games] == ["12332"]
    c.known_final = {"12332"}
    calls = respx.calls.call_count
    assert c.collect() == [] and respx.calls.call_count == calls + 2  # stored final: no request


def test_backfill_config_and_nightly_stay_apart():
    s = Settings(_env_file=None, iihf_realtime_enabled=True)
    nightly = next(c for c in enabled_collectors(s) if c.name == "iihf")
    assert [e.event_id for e in nightly.events] == [773]  # past events never in the regular run
    names = [c.name for c in backfill_collectors(s)]
    assert names == ["iihf", "wiki"]
    iihf_c = backfill_collectors(s)[0]
    assert {e.event_id for e in iihf_c.events} == {712, 793} and all(e.game_ids for e in iihf_c.events)
    only = backfill_collectors(s, ["oly-2026"])
    assert [c.name for c in only] == ["wiki"]


# --- Wikipedia boxes (Olympics) -----------------------------------------------------------------------


def test_wiki_boxes_parse_games_goals_and_names():
    games = wiki_games.parse_page(wikitext(), 1367628736, OLY)
    assert len(games) == 4
    usa_cze, cze_sui, sui_usa, fin_sui = games
    assert (usa_cze.home, usa_cze.away, usa_cze.score) == ("USA", "Tschechien", "5:1")
    assert usa_cze.starts_at == datetime(2026, 2, 5, 15, 40, tzinfo=UTC)  # 16:40 Milan
    assert (
        usa_cze.url == "https://www.iihf.com/pdf/852/ihw852a01_74_5_1" and usa_cze.external_id == "ihw852a01"
    )
    assert usa_cze.source == "IIHF via Wikipedia" and usa_cze.raw["phase"] == "Group A"
    goals = usa_cze.raw["goals"]
    assert [g["score"] for g in goals] == ["1:0", "2:0", "3:0", "3:1", "4:1", "5:1"]
    assert goals[2]["scorer"] == "Hayley Scamurra"  # bare short name resolved from the page's links
    assert goals[0]["assists"] == ["Megan Keller", "Laila Edwards"]
    assert ("Aerin Frankel", "goalie") in [(p.name, p.role) for p in usa_cze.participants]
    assert cze_sui.score == "3:4 n.P." and cze_sui.away == "Schweiz"
    assert fin_sui.away == "Schweiz" and fin_sui.raw["away_code"] == "SUI"  # 'SWI' in the box
    assert fin_sui.raw["phase"] == "Quarter-finals" and fin_sui.score == "0:1"
    assert sui_usa.raw["tournament"]["key"] == "oly-2026"


# --- ingest + answers ---------------------------------------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _game(s, key, home, away, when, score, comp=None, source="Leijonat"):
    ev = m.Event(
        key=key,
        kind="game",
        name=f"{home.name} – {away.name}",
        starts_at=when,
        status="final" if score else "scheduled",
        score=score,
        home_team_id=home.id,
        away_team_id=away.id,
        competition_id=comp.id if comp else None,
        source=source,
        source_url=f"https://example.org/{key}",
    )
    s.add(ev)
    s.flush()
    return ev


@pytest.fixture
def games(world):
    """Czechia: 2 EHT games in August (one older than 14 days, won vs SUI) + 1 old WCh game; USA: 1 Olympic game."""
    with Session(world) as s:
        sui, cze, usa, aut = (
            repo.get_team(s, k) for k in ("sui-women", "cze-women", "usa-women", "aut-women")
        )
        eht = m.Competition(
            sport_id=repo.get_sport(s, "icehockey_women").id,
            key="eht-x",
            name="Women's Euro Hockey Tour 2026/27, Kloten",
            kind="tournament",
        )
        oly = m.Competition(
            sport_id=eht.sport_id, key="oly-x", name="Olympische Winterspiele 2026", kind="tournament"
        )
        s.add_all([eht, oly])
        s.flush()
        now = datetime.now(UTC)
        _game(s, "g1", sui, cze, now - timedelta(days=58), "1:2", eht)
        _game(s, "g2", sui, cze, now - timedelta(days=16), "3:2 n.P.", eht)
        _game(s, "g3", cze, usa, now - timedelta(days=400), "0:4", None, "IIHF")
        _game(s, "g4", sui, usa, now - timedelta(days=250), "0:5", oly, "IIHF via Wikipedia")
        _game(s, "g5", sui, aut, now + timedelta(days=21), None, None, "IIHF")
        s.commit()
    return world


def test_team_games_all_competitions_record_and_coverage(games):
    with Session(games) as s:
        cze = repo.get_team(s, "cze-women")
        tg = team_games.team_games(s, cze, last=5)
        assert [g.key for g in tg.games] == ["g2", "g1", "g3"]  # newest first, all sources/competitions
        assert team_games.outcome(tg.games[1], {cze.id}) == "Sieg"
        assert team_games.outcome(tg.games[0], {cze.id}) == "Niederlage n.P."
        assert team_games.record(tg.games, {cze.id}) == (
            "3 Spiele: 1 Sieg, 2 Niederlagen (1 n.V./n.P.); Tore 4:8"
        )
        assert "Gespeichert: 3 gespielte Spiele von Tschechien" in team_games.coverage_note(tg)
        assert len(team_games.team_games(s, cze, last=1).games) == 1


def test_list_events_team_last_and_head_to_head(games):
    with Session(games) as s:
        out = t.list_events(s, team="Tschechien")
        assert [g["key"] for g in out["spiele_letzte"]] == ["g2", "g1", "g3"]  # not only the last 14 days
        assert out["spiele_letzte"][1]["ausgang"] == "Sieg"
        assert out["spiele_letzte"][1]["wettbewerb"] == "Women's Euro Hockey Tour 2026/27, Kloten"
        md = render_markdown(out)
        assert "1:2 (Sieg)" in md and "Bilanz in diesen Spielen: 3 Spiele" in md and "Gespeichert:" in md
        usa = t.list_events(s, team="USA", last=1)
        assert [g["key"] for g in usa["spiele_letzte"]] == ["g4"]
        h2h = t.list_events(s, team="Schweiz", opponent="Tschechien")
        assert [g["key"] for g in h2h["spiele_letzte"]] == ["g2", "g1"]
        aut = t.list_events(s, team="Schweiz", opponent="Österreich")
        assert aut["spiele_letzte"] == []
        # the next SUI–AUT meetings: g5, and the seeded WCh 2026 game (10.11.2026) while it is within `days`
        nxt = [g["key"] for g in aut["spiele_naechste"]]
        assert "g5" in nxt and set(nxt) <= {"g5", "iihf-13559"}
        assert "Keine gespielten Spiele von Schweiz – Österreich" in render_markdown(aut)
        with pytest.raises(ValueError):
            t.list_events(s, opponent="Österreich")


def test_questions_route_to_team_games(games):
    with Session(games) as s:
        assert team_games.wants_team_games("Wie ist die Form von Tschechien?")
        assert team_games.last_n("Letzte drei Länderspiele der USA") == 3
        assert not team_games.wants_team_games("Wer ist Captain von Tschechien?")
        form = team_games.for_question(s, "Wie ist die Form von Tschechien?")
        assert [tg.team.key for tg, _ in form] == ["cze-women"] and len(form[0][0].games) == 3
        two = team_games.for_question(s, "Letzte drei Länderspiele der USA und von Tschechien")
        assert [tg.team.key for tg, _ in two] == ["usa-women", "cze-women"] and two[0][0].opponent is None
        h2h = team_games.for_question(
            s, "Wann haben die Schweiz und Tschechien zuletzt gegeneinander gespielt?"
        )
        assert h2h[0][0].opponent.key == "cze-women" and [g.key for g in h2h[0][0].games] == ["g2", "g1"]
        aut = team_games.answer_text(s, "Direktvergleich Schweiz gegen Österreich", "x")
        assert "Nächstes Duell" in aut and "Keine gespielten Spiele" in aut
        # team parameter of the tool counts as named
        res = team_games.answer_text(s, "Letzte Resultate?", "x", team=repo.get_team(s, "usa-women"))
        assert "Schweiz – USA 0:5 (Sieg USA)" in res and "Olympische Winterspiele 2026" in res
        # web chat: direct answer without a model, and no "Welches Spiel meinst du?"
        hit = lookups.direct_lookup(s, "Form von Tschechien?", [])
        assert hit.docs[0].extra["direct"]["spalten"][-1] == "Wettbewerb"
        assert len(hit.docs[0].extra["direct"]["rows"]) == 3
        assert cross.ask_back(s, "Bilanz Schweiz gegen Tschechien", []) is None


def test_mcp_frage_answers_team_games_before_the_wm_sheet(games, monkeypatch, frozen_now):
    from klugscheisser.mcp_server import server

    frozen_now(datetime(2026, 11, 4, 12, tzinfo=UTC), t, team_games, server)  # fixture games at fixed dates

    monkeypatch.setattr(server, "session_scope", lambda: Session(games))
    out = server.frage.__wrapped__("Letzte Resultate und Form?", team="Tschechien", event="WM 2026")
    text = out.content[0].text
    assert "Letzte Spiele Tschechien" in text and "1:2 (Sieg Tschechien)" in text


@respx.mock
def test_backfill_ingest_adopts_swiss_rows_and_builds_containers(world):
    page = {"parse": {"revid": 1367628736, "wikitext": wikitext()}}
    respx.get(wiki_games.API).mock(return_value=httpx.Response(200, json=page))
    respx.get(f"{iihf.API}/GetLatestState/12331").mock(return_value=httpx.Response(200, json=None))
    respx.get(f"{iihf.API}/GetLatestState/12332").mock(return_value=httpx.Response(200, json=state()))
    respx.get(f"{iihf.API}/GetLatestState/12333").mock(return_value=httpx.Response(200, json=None))
    with Session(world) as s:
        sui, usa = repo.get_team(s, "sui-women"), repo.get_team(s, "usa-women")
        # the SIHF team page stored SUI-USA first (fallback source, no time, no scorers)
        old = m.Event(
            key="sihfnt-sui-2026-02-09-sui-usa",
            kind="game",
            name="Schweiz – USA",
            starts_at=datetime(2026, 2, 8, 23, 0, tzinfo=UTC),
            status="final",
            score="0:5",
            home_team_id=sui.id,
            away_team_id=usa.id,
            source="SIHF",
        )
        s.add(old)
        s.flush()
        ctx = build_context(s, Settings(_env_file=None))
        collectors = [
            iihf.IihfCollector([D1A], http=PoliteHttp(0)),
            wiki_games.WikiGamesCollector([OLY], http=PoliteHttp(0)),
        ]
        report = ingest_results(ctx, collectors)
        assert not report.errors, report.errors
        assert report.per_source == {"IIHF": 1, "IIHF via Wikipedia": 4} and report.adopted == 1
        assert old.key == "wiki-ihw852a07" and old.source == "IIHF via Wikipedia"
        aut = s.scalar(select(m.Event).where(m.Event.key == "iihf-12332"))
        assert aut.competition.key == "wchd1a-2025" and aut.home_team_id == repo.get_team(s, "aut-women").id
        cont = s.scalar(select(m.Event).where(m.Event.key == "wchd1a-2025"))
        assert cont is not None and cont.kind == "tournament"
        n_items = s.scalar(
            select(func.count()).select_from(m.Item).where(m.Item.source_type == "official_result")
        )
        assert n_items == 5
        again = ingest_results(ctx, collectors)  # re-run: known finals, nothing new
        assert again.events_new == 0 and again.items_new == 0
