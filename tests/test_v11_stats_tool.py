"""v11 part C: MCP `stats`, richer `get_athlete`, stats questions in the chat / `frage` (no model calls)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import cross
from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import server
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.stats_tools import stats as stats_tool
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.stats import search

SETTINGS = Settings(_env_file=None, auto_refresh=False)
CUR = search.current_season()
FETCHED = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
EP = "https://www.eliteprospects.com/player/{}/x"


def _row(aid, season, league, team, kind="club", phase="regular", **stats):
    return m.SeasonStat(
        athlete_id=aid,
        season=season,
        league=league,
        team_name=team,
        team_kind=kind,
        phase=phase,
        source="eliteprospects",
        source_url=EP.format(aid),
        fetched_at=FETCHED,
        **stats,
    )


def _sk(gp, g, a, pim=None, pm=None):
    return {"games": gp, "goals": g, "assists": a, "points": g + a, "penalty_minutes": pim, "plus_minus": pm}


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        lara = repo.resolve_athlete(s, "Lara Stalder")
        alina = repo.resolve_athlete(s, "Alina Müller")
        andrea = repo.resolve_athlete(s, "Andrea Brändli")
        andrea.position = "G"
        probe = repo.upsert_athlete(s, sport, "Nina Probewerth", nationality="SUI", position="F")
        kurz = repo.upsert_athlete(s, sport, "Lena Kurzeinsatz", nationality="SUI", position="F")
        swede = repo.upsert_athlete(s, sport, "Svea Svenssonova", nationality="SWE", position="F")
        husky = repo.upsert_athlete(s, sport, "Testa Huskyfrau", nationality="USA", position="D")
        s.flush()
        rows = [
            # Stalder: current season, last season (+ playoffs), old season with games only, Olympics, (all)
            _row(lara.id, CUR, "SWHL A (W)", "EV Zug", **_sk(6, 5, 6, pim=2)),
            _row(lara.id, CUR - 1, "SWHL A (W)", "EV Zug", **_sk(28, 30, 35, pim=10, pm=40)),
            _row(lara.id, CUR - 1, "SWHL A (W)", "EV Zug", phase="playoffs", **_sk(6, 4, 3)),
            _row(lara.id, 2012, "SWHL A (W)", "ZSC Lions Frauen", games=20),
            _row(lara.id, 2025, "OG (W)", "Switzerland Olympics", kind="national", **_sk(7, 3, 2)),
            _row(lara.id, 2021, "OG (W)", "Switzerland Olympics", kind="national", **_sk(7, 2, 3)),
            _row(lara.id, 2025, "International (W)", "Switzerland (all)", kind="national", **_sk(20, 10, 10)),
            # Müller: Olympics (most goals), Northeastern
            _row(alina.id, 2017, "OG (W)", "Switzerland Olympics", kind="national", **_sk(6, 7, 3)),
            _row(alina.id, 2021, "OG (W)", "Switzerland Olympics", kind="national", **_sk(7, 4, 5)),
            _row(
                alina.id, 2021, "International (W)", "Switzerland (all)", kind="national", **_sk(30, 40, 40)
            ),
            *[
                _row(alina.id, y, "NCAA (W)", "Northeastern Univ.", kind="college", **_sk(30, 20, 30))
                for y in range(2017, 2022)
            ],
            _row(husky.id, 2019, "NCAA (W)", "Northeastern Univ.", kind="college", **_sk(30, 2, 10)),
            _row(husky.id, 2020, "NCAA (W)", "Northeastern Univ.", kind="college", **_sk(25, 1, 8)),
            # SWHL this season: Swiss and not Swiss, few games
            _row(probe.id, CUR, "SWHL A (W)", "HC Fribourg-Gottéron", **_sk(6, 2, 2)),
            _row(kurz.id, CUR, "SWHL A (W)", "HC Fribourg-Gottéron", **_sk(2, 3, 3)),
            _row(swede.id, CUR, "SWHL A (W)", "ZSC Lions Frauen", **_sk(6, 8, 8)),
            # Brändli: goalie at WC and Olympics; one WC without games
            _row(andrea.id, 2015, "WC (W)", "Switzerland", kind="national", games=0),
            _row(
                andrea.id,
                2022,
                "WC (W)",
                "Switzerland",
                kind="national",
                games=5,
                wins=3,
                losses=2,
                shutouts=1,
                goals_against=10,
                saves=140,
                save_pct=0.933,
                gaa=2.0,
            ),
            _row(
                andrea.id,
                2023,
                "WC (W)",
                "Switzerland",
                kind="national",
                games=5,
                wins=2,
                losses=3,
                goals_against=11,
                saves=126,
                save_pct=0.92,
                gaa=2.1,
            ),
            _row(
                andrea.id,
                2021,
                "OG (W)",
                "Switzerland Olympics",
                kind="national",
                games=7,
                wins=2,
                losses=5,
                goals_against=24,
                saves=181,
                save_pct=0.883,
                gaa=3.5,
            ),
            _row(
                andrea.id,
                2025,
                "OG (W)",
                "Switzerland Olympics",
                kind="national",
                games=6,
                wins=4,
                losses=2,
                goals_against=15,
                saves=200,
                save_pct=0.930,
                gaa=1.9,
            ),
        ]
        s.add_all(rows)
        url = EP.format(lara.id)
        for kind, value, text in [
            ("weight_kg", {"number": 62}, "62 kg"),
            ("height_cm", {"number": 167}, "167 cm"),
            ("youth_team", {"text": "HC Luzern"}, "HC Luzern"),
            (
                "current_team",
                {"team": "EV Zug", "league": "SWHL A (W)", "season": CUR, "jersey": 9, "status": "aktiv"},
                "EV Zug (SWHL A (W)), #9",
            ),
            ("contract", {"text": "27/28", "season_to": 2027}, "27/28"),
            ("agency", {"text": "Testagentur"}, "Testagentur"),
            ("draft", {"league": "NWHL", "year": 2016, "round": 5, "team": "Boston Pride"}, "2016, Runde 5"),
            (
                "award",
                {"text": "SDHL Most Points", "season": "2019-20", "number": "89", "league": "SDHL"},
                "SDHL Most Points",
            ),
            ("award", {"text": "SDHL Most Points", "season": "2019-20", "number": "89"}, "SDHL Most Points"),
            ("medal", {"text": "Olympia Bronze", "metal": "bronze", "year": 2014}, "Olympia Bronze 2014"),
        ]:
            s.add(
                m.Fact(
                    athlete_id=lara.id,
                    kind=kind,
                    value=value,
                    value_text=text,
                    source="eliteprospects",
                    source_url=url,
                    confidence=1.0,
                    status="confirmed",
                    extractor="eliteprospects",
                )
            )
        s.add(
            m.Fact(
                athlete_id=lara.id,
                kind="height_cm",
                value={"number": 176},
                value_text="176 cm",
                source="wikidata",
                source_url="https://www.wikidata.org/wiki/Q1",
                status="conflict",
            )
        )
        s.add(
            m.EpProfile(
                athlete_id=lara.id,
                ep_id="369459",
                url=url,
                fetched_at=FETCHED,
                raw={},
                data={"form": {"last5": {"GP": 5, "G": 3, "A": 4, "PTS": 7, "PM": 2}}},
            )
        )
        s.commit()
        ids = {
            "lara": lara.id,
            "alina": alina.id,
            "andrea": andrea.id,
            "probe": probe.id,
            "kurz": kurz.id,
            "swede": swede.id,
            "husky": husky.id,
        }
    return db, ids


# --- parsing ---------------------------------------------------------------------------------------------


def test_season_parsing():
    today = date(2026, 10, 4)
    assert search.parse_season("2026/27", today) == (2026, 2026)
    assert search.parse_season("2026-27", today) == (2026, 2026)
    assert search.parse_season("2026-2027", today) == (2026, 2026)
    assert search.parse_season("26/27", today) == (2026, 2026)
    assert search.parse_season(2026, today) == (2026, 2026)
    assert search.parse_season("diese Saison", today) == (2026, 2026)
    assert search.parse_season("letzte Saison", today) == (2025, 2025)
    assert search.parse_season("seit 2022", today) == (2022, 2026)
    assert search.parse_season("2020/21–2025/26", today) == (2020, 2025)
    assert search.parse_season(None) is None and search.parse_season("Karriere") is None
    assert search.current_season(date(2027, 3, 1)) == 2026 and search.current_season(date(2027, 8, 1)) == 2027
    with pytest.raises(ValueError):
        search.parse_season("irgendwann")


def test_league_aliases(world):
    db, _ = world
    with Session(db) as s:
        for name in ("SWHL", "Women's League", "PostFinance Women's League", "swhl a"):
            assert search.resolve_leagues(s, name) == (["SWHL A (W)"], [])
        assert search.resolve_leagues(s, "Olympia")[0] == ["OG (W)"]
        assert search.resolve_leagues(s, ["WM", "PWHL", "SDHL", "NCAA"])[0] == [
            "WC (W)",
            "PWHL (W)",
            "SDHL (W)",
            "NCAA (W)",
        ]
        assert search.resolve_leagues(s, "Olympia vs WM")[0] == ["OG (W)", "WC (W)"]
        assert search.resolve_leagues(s, "NCAA (W)")[0] == ["NCAA (W)"]  # EP name as stored
        _, unknown = search.resolve_leagues(s, "Mondliga")
        assert unknown == ["Mondliga"]
        r = stats_tool(s, league="Mondliga")
        assert r["zeilen"] == [] and any("Liga nicht erkannt" in h for h in r["hinweise"])


# --- the stats tool -----------------------------------------------------------------------------------------


def test_filters_and_source_on_every_row(world):
    db, _ = world
    with Session(db) as s:
        r = stats_tool(s, person="Stalder", season="diese Saison")
        assert [z["team"] for z in r["zeilen"]] == ["EV Zug"]
        z = r["zeilen"][0]
        assert (z["points"], z["games"], z["points_per_game"]) == (11, 6, 1.83)
        assert z["saison"] == f"{CUR}/{(CUR + 1) % 100:02d}" and "11 Punkte" in z["satz"]
        assert r["datenstand"] == "Statistik: Elite Prospects Stand 04.10.2026"
        everything = stats_tool(s, limit=100)
        assert everything["zeilen"]
        for z in everything["zeilen"]:
            assert z["quelle"] == "Elite Prospects" and z["stand"] == "2026-10-04"
            assert z["quelle_link"].startswith(
                "[Elite Prospects, Stand 04.10.2026](https://www.eliteprospects"
            )
            assert z["zeile_prefix"] == "📊 (Statistik)" and z["satz"]
        # phase, team_kind, team, nationality, umlauts in names
        po = stats_tool(s, person="Lara Stalder", phase="playoffs")["zeilen"]
        assert len(po) == 1 and po[0]["phase"] == "playoffs" and po[0]["points"] == 7
        nat = stats_tool(s, person="Stalder", team_kind="nationalteam")["zeilen"]
        assert {z["league"] for z in nat} == {"OG (W)"}
        assert {z["person"] for z in stats_tool(s, team="Northeastern")["zeilen"]} == {
            "Alina Müller",
            "Testa Huskyfrau",
        }
        assert stats_tool(s, person="Alina Mueller", league="Olympia")["anzahl"] == 2
        assert stats_tool(s, person="Andrea Braendli", league="WM")["anzahl"] == 3
        swiss = stats_tool(s, league="SWHL", season=CUR, nationality="Schweiz")["zeilen"]
        assert "Svea Svenssonova" not in {z["person"] for z in swiss}


def test_all_rows_excluded_and_none_not_zero(world):
    db, _ = world
    with Session(db) as s:
        rows = stats_tool(s, person="Stalder", limit=100)["zeilen"]
        assert not any("(all)" in z.get("team", "") for z in rows)
        old = next(z for z in rows if z["saison"] == "2012/13")
        assert old["games"] == 20 and "points" not in old and "goals" not in old  # no data, not 0
        assert "Nur Spiele" in old["hinweis"]
        career = stats_tool(s, person="Stalder", group="career")["zeilen"][0]
        # 6+28+20+7+7 games; points only from rows that have them, rate basis excludes the 20 games-only
        assert career["games"] == 68 and career["points"] == 11 + 65 + 5 + 5
        assert career["points_per_game"] == round(86 / 48, 2)
        r = stats_tool(s, person="Stalder", sort_by="goals")
        assert any("ohne Angabe zu Tore" in h for h in r["hinweise"])
        assert r["zeilen"][-1]["saison"] == "2012/13"  # without value: last, never ranked as 0


def test_sorting_per_game_with_min_games(world):
    db, _ = world
    with Session(db) as s:
        r = stats_tool(s, league="SWHL", season=CUR, sort_by="points_per_game", nationality="SUI")
        names = [z["person"] for z in r["zeilen"]]
        assert names == ["Lara Stalder", "Nina Probewerth"]  # Kurzeinsatz: 2 games < 5
        assert r["zeilen"][0]["rang"] == 1 and r["filter"]["min_spiele"] == 5
        assert any("weniger als 5 Spielen" in h for h in r["hinweise"])
        assert any("nur über die Personen in unserer Datenbank" in h for h in r["hinweise"])
        r0 = stats_tool(s, league="SWHL", season=CUR, sort_by="points_per_game", min_games=0)
        assert r0["zeilen"][0]["person"] == "Lena Kurzeinsatz"
        gaa = stats_tool(s, person="Brändli", sort_by="gaa")["zeilen"]
        assert gaa[0]["gaa"] == 1.9  # lower is better
        sv = stats_tool(s, league="WM", sort_by="save_pct")["zeilen"]
        assert [z["save_pct"] for z in sv] == [0.933, 0.92]  # 2015 without games not ranked
        og = stats_tool(s, league="Olympia", sort_by="save_pct", group="career")
        assert [z["person"] for z in og["zeilen"]] == ["Andrea Brändli"]  # skaters are no goalies
        assert not any("ohne Angabe" in h for h in og["hinweise"])


def test_career_grouping_and_compare(world):
    db, _ = world
    with Session(db) as s:
        r = stats_tool(s, league="Olympia", team="Schweiz", group="career", sort_by="goals")
        top = r["zeilen"][0]
        assert top["person"] == "Alina Müller" and top["goals"] == 11 and top["saisons"] == 2
        assert top["von"] == "2017/18" and top["bis"] == "2021/22"
        cmp = stats_tool(s, person=["Stalder", "Müller"], league="Olympia", group="career")
        assert [z["person"] for z in cmp["zeilen"]] == ["Lara Stalder", "Alina Müller"]
        assert stats_tool(s, person="Stalder vs Müller", league="Olympia", group="career")["anzahl"] == 2
        goalie = stats_tool(s, person="Brändli", league=["Olympia", "WM"], group="league")["zeilen"]
        by = {z["league"]: z for z in goalie}
        assert by["OG (W)"]["games"] == 13 and by["OG (W)"]["save_pct"] == round(381 / (381 + 39), 3)
        assert by["WC (W)"]["wins"] == 5 and "Fangquote" in by["WC (W)"]["satz"]
        teams = stats_tool(s, person="Müller", group="team")["zeilen"]
        assert {z["team"] for z in teams} == {"Switzerland Olympics", "Northeastern Univ."}


def test_season_rows_newest_first(world):
    """MCP stats without sort_by: current season at the top, `limit` cuts the oldest seasons; a comparison
    interleaves per season (asked person order inside). The chat keeps per person, chronological."""
    from klugscheisser.mcp_server.stats_tools import datenstand_stats
    from klugscheisser.stats.queries import season_label as L

    db, _ = world
    with Session(db) as s:
        rows = stats_tool(s, person="Lara Stalder", phase="alle")["zeilen"]
        seasons = [z["saison"] for z in rows]
        assert seasons[0] == L(CUR) and seasons == sorted(seasons, reverse=True) and seasons[-1] == L(2012)
        two = stats_tool(s, person="Lara Stalder", limit=2)["zeilen"]
        assert [z["saison"] for z in two] == [L(CUR), L(CUR - 1)]
        cmp = stats_tool(s, person=["Müller", "Stalder"], league="Olympia")["zeilen"]
        assert [(z["saison"], z["person"]) for z in cmp] == [
            (L(2025), "Lara Stalder"),
            (L(2021), "Alina Müller"),
            (L(2021), "Lara Stalder"),
            (L(2017), "Alina Müller"),
        ]
        chat = search.query(s, person=["Lara Stalder"], phase="alle").rows
        assert [z["saison"] for z in chat] == sorted(z["saison"] for z in chat)
    line = datenstand_stats(
        None,
        [
            {"quelle": "Elite Prospects", "stand": "2026-10-04T19:00:00"},
            {"quelle": "SIHF", "stand": "2026-10-25T06:00:00"},
        ],
    )
    assert line == "Statistik: SIHF Stand 25.10.2026; Elite Prospects Stand 04.10.2026"


def test_bad_parameters_are_german_errors(world):
    db, _ = world
    with Session(db) as s:
        with pytest.raises(ValueError, match="sort_by"):
            stats_tool(s, sort_by="elo")
        with pytest.raises(ValueError, match="Keine Person"):
            stats_tool(s, person="Niemand Unbekannt")
    res = server.stats(person="Lara Stalder", season=str(CUR))
    text = res.content[0].text
    assert "**Datenstand:** Statistik: Elite Prospects Stand" in text and "📊 (Statistik)" in text
    assert "](https://www.eliteprospects.com/player/" in text
    assert "Fehler" in server.stats(sort_by="elo").content[0].text


# --- get_athlete --------------------------------------------------------------------------------------------


def test_get_athlete_new_fields(world):
    db, _ = world
    with Session(db) as s:
        r = t.get_athlete(s, "Lara Stalder")
    prof = r["profil"]
    assert prof["weight_kg"]["wert"] == "62 kg" and prof["youth_team"]["wert"] == "HC Luzern"
    assert prof["current_team"]["nummer"] == 9 and prof["current_team"]["status"] == "aktiv"
    assert prof["contract"]["wert"] == "27/28" and prof["agency"]["wert"] == "Testagentur"
    assert prof["draft"]["quelle"] == "Elite Prospects" and prof["draft"]["stand"] == "2026-10-04"
    assert prof["height_cm"]["wert"] == "167 cm" and "176 cm" in prof["height_cm"]["abweichend"]
    st = r["statistik"]
    season_key = f"saison_{CUR}_{(CUR + 1) % 100:02d}"
    assert st[season_key][0]["points"] == 11 and st[season_key][0]["quelle_link"].startswith("[Elite")
    leagues = {z["league"] for z in st["karriere_pro_liga"]}
    assert "International (W)" not in leagues and "SWHL A (W)" in leagues
    assert st["form"]["letzte_5"]["satz"] == "Letzte 5 Spiele: 3 Tore, 4 Assists, 7 Punkte, 2 Plus/Minus."
    aw = r["auszeichnungen"]
    assert aw["liste"][0]["art"] == "Medaille" and aw["anzahl"] == 2  # duplicate award removed
    assert aw["liste"][0]["saison"] == "2014"  # a year stays a year
    award = aw["liste"][1]
    assert (award["saison"], award["nummer"], award["liga"]) == ("2019-20", "89", "SDHL")
    assert len(str(r)) < 20000  # compact


# --- chat / frage --------------------------------------------------------------------------------------------


def _no_llm(system, prompt, schema):
    raise AssertionError("stats questions must not call the model")


def _engine():
    return eng.GeminiAnswerEngine(SETTINGS, llm=_no_llm, support_checker=lambda p: [1.0] * len(p))


def test_understand_stats_questions(world):
    db, ids = world
    with Session(db) as s:
        q = search.understand(s, "Wie viele Punkte hat Stalder diese Saison?")
        assert q.params["person"] == [ids["lara"]] and q.params["group"] == "season"
        q = search.understand(s, "Beste Schweizerin nach Punkten pro Spiel in der SWHL")
        assert q.params["sort_by"] == "points_per_game" and q.params["league"] == ["SWHL A (W)"]
        assert q.params["nationality"] == "SUI" and q.params["season"] == CUR
        q = search.understand(s, "Brändli an Olympia vs WM")
        assert q.params["person"] == [ids["andrea"]] and q.params["league"] == ["OG (W)", "WC (W)"]
        assert q.params["group"] == "league"
        q = search.understand(s, "Wer hat am meisten Tore für die Schweiz an Olympia?")
        assert q.params["sort_by"] == "goals" and q.params["team"] == "Switzerland"
        assert q.params["group"] == "career"
        assert search.understand(s, "Wer spielte zusammen in Northeastern?") is None
        assert search.understand(s, "Was postet Stalder auf Instagram?") is None
        assert search.understand(s, "Was sagte Stalder nach ihren zwei Toren?") is None
        assert search.understand(s, "Wer hat gestern Tore geschossen?") is None
        assert cross.understand("Wer spielte zusammen in Northeastern?").typ == "beziehung"


def test_chat_questions_answered_from_stats(world):
    _, ids = world
    e = _engine()
    r = e.answer("Wie viele Punkte hat Stalder diese Saison?", use_cache=False)
    assert len(r.statements) == 1 and "11 Punkte" in r.statements[0].aussage
    assert r.statements[0].quelle_link.startswith("[Elite Prospects, Stand 04.10.2026](https://")
    assert r.personen == ["Lara Stalder"] and "Statistik" in r.scope_hinweis

    r = e.answer("Beste Schweizerin nach Punkten pro Spiel in der SWHL", use_cache=False)
    assert r.statements[0].aussage.startswith("1. Lara Stalder")
    assert all("Svenssonova" not in st.aussage for st in r.statements)
    assert "Datenbank" in r.scope_hinweis

    r = e.answer("Brändli an Olympia vs WM", use_cache=False)
    assert len(r.statements) == 2 and {"OG (W)" in st.aussage for st in r.statements} == {True, False}
    assert all(st.url.startswith("https://www.eliteprospects.com/") for st in r.statements)

    r = e.answer("Wer hat am meisten Tore für die Schweiz an Olympia?", use_cache=False)
    assert r.statements[0].aussage.startswith("1. Alina Müller") and "11 Tore" in r.statements[0].aussage

    # follow-up in a chat on the person's page: still from the stats, no model call
    r = e.answer(
        "Und wie viele Tore diese Saison?",
        person_ids=[ids["lara"]],
        history=[{"frage": "Wer ist sie?", "antwort": "…", "personen": ["Lara Stalder"]}],
        use_cache=False,
    )
    assert r.statements and "5 Tore" in r.statements[0].aussage


def test_played_together_uses_ep_season_rows(world):
    r = _engine().answer("Wer spielte zusammen in Northeastern?", use_cache=False)
    assert len(r.statements) == 1
    st = r.statements[0]
    assert "Alina Müller" in st.aussage and "Testa Huskyfrau" in st.aussage and "Northeastern" in st.aussage
    assert "2019/20–2020/21" in st.aussage and st.url.startswith("https://www.eliteprospects.com/")


def test_frage_tool_routes_stats(world, monkeypatch):
    monkeypatch.setattr(eng, "build_answer_engine", lambda settings: _engine())
    res = server.frage("Wie viele Punkte hat Stalder diese Saison?", web=False)
    text = res.content[0].text
    assert (
        "11 Punkte" in text and "[Elite Prospects, Stand 04.10.2026](https://www.eliteprospects.com" in text
    )
