"""2026-11-01: history for players without Elite Prospects data (stats/history.py, stats/ep_ids.py, ep_import
html_to_raw): SIHF past seasons, stations with seasons, audit, EP ids from free sources. No network."""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.stats import ep_ids, history

TODAY = date(2026, 11, 1)
STAND = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ZUG = {"id": 1, "name": "EV Zug", "acronym": "EVZ"}
BERN = {"id": 2, "name": "SC Bern", "acronym": "SCB"}
SKATER_COLS = [
    "rank",
    "player",
    "team",
    "position",
    "gamesPlayed",
    "goals",
    "assists",
    "points",
    "ppg",
    "pimTotal",
]
GOALIE_COLS = [
    "rank",
    "player",
    "team",
    "gamesPlayed",
    "firstKeeper",
    "goalsAgainst",
    "goalsAgainstAverage",
    "secondsPlayed",
    "penaltyInMinutes",
    "goals",
    "assists",
]
PHASES = [
    ("11", "Regular Season"),
    ("12", "Playout Final"),
    ("13", "Playoff 1/2 Final"),
    ("14", "Playoff Final"),
    ("15", "Kleiner Final"),
]


def _table(cols, rows, season, phase, phases=PHASES):
    return {
        "filters": [
            {"alias": "Season", "selected": str(season)},
            {"alias": "Phase", "selected": phase, "entries": [{"alias": a, "name": n} for a, n in phases]},
        ],
        "header": [{"alias": c} for c in cols],
        "data": rows,
    }


def _sk(name, team, gp, g, a, pim=0):
    return ["1", name, team, "Stürmer", str(gp), str(g), str(a), str(g + a), "0", str(pim)]


def _gk(name, team, gp, ga, gaa, toi, pim=0, g=0, a=0):
    return ["1", name, team, str(gp), str(gp), str(ga), str(gaa), toi, str(pim), str(g), str(a)]


class FakeHttp:
    """Answers like data.sihf.ch: an unknown phase -> the first phase; an unknown season -> the current one."""

    def __init__(self, seasons: dict):
        self.seasons = seasons  # end year -> {(alias, phase): rows}
        self.requests = 0

    def json(self, url, params=None, headers=None):
        self.requests += 1
        season, phase = params["filterQuery"].split("/")
        if int(season) not in self.seasons:
            return _table(SKATER_COLS, [], 2027, "99", [("99", "Regular Season")])
        tables = self.seasons[int(season)]
        phases = tables.get("phases", PHASES)
        if phase not in {p for p, _ in phases}:
            phase = phases[0][0]
        alias = params["alias"]
        cols = GOALIE_COLS if alias == "goalkeeper" else SKATER_COLS
        rows = tables.get((alias, phase)) or [["Keine Spieler gefunden!"]]
        return _table(cols, rows, season, phase, phases)


def test_phase_rules():
    assert history.phase_kind("Playout Final") == "regular"
    assert history.phase_kind("Masterround ") == "regular"
    assert history.phase_kind("Liga Qualifikation") == "regular"
    assert history.phase_kind("Aufstiegspiele") == "regular"
    assert history.phase_kind("Kleiner Final") == "playoffs"
    assert history.phase_kind("Bronce Medal Games") == "playoffs"
    assert history.phase_kind("Playoff Overall") == "overall"
    plan = history.plan_phases([("1", "Regular Season"), ("2", "Playoff Final"), ("3", "Playoff Overall")])
    assert plan == {"regular": [("1", "Regular Season")], "playoffs": [("3", "Playoff Overall")]}
    plan = history.plan_phases(PHASES)
    assert [p for p, _ in plan["playoffs"]] == ["13", "14", "15"]
    assert [p for p, _ in plan["regular"]] == ["11", "12"]


def test_fetch_sums_phases_goalie_once():
    http = FakeHttp(
        {
            2025: {
                ("player", "11"): [_sk("Muster Anna", ZUG, 20, 5, 4, 2), _sk("Wand Gabi", ZUG, 18, 0, 1)],
                ("goalkeeper", "11"): [_gk("Wand Gabi", ZUG, 18, 30, 1.8, "1000:00", pim=2, a=1)],
                ("player", "12"): [_sk("Muster Anna", ZUG, 2, 1, 0)],
                ("player", "13"): [_sk("Muster Anna", ZUG, 3, 0, 1)],
                ("player", "14"): [_sk("Muster Anna", ZUG, 3, 1, 1)],
                ("goalkeeper", "14"): [_gk("Wand Gabi", ZUG, 3, 6, 2.0, "180:00")],
            }
        }
    )
    out = history.fetch_league_season(http, 42, 2025)
    reg = {r["name"]: r for r in out["regular"]}
    po = {r["name"]: r for r in out["playoffs"]}
    assert reg["Anna Muster"]["games"] == 22 and reg["Anna Muster"]["points"] == 10
    assert reg["Wand Gabi" if "Wand Gabi" in reg else "Gabi Wand"]["games"] == 18  # not 36: goalie table only
    gabi = reg.get("Gabi Wand") or reg["Wand Gabi"]
    assert gabi["goalie"] and gabi["gaa"] == 1.8 and gabi["penalty_minutes"] == 2 and gabi["assists"] == 1
    assert po["Anna Muster"]["games"] == 6 and po["Anna Muster"]["goals"] == 1
    assert "filterQuery=2025/11" in reg["Anna Muster"]["url"]
    assert http.requests == 1 + 2 * 5 - 1  # the first answer is the first phase's skater table


def test_fetch_season_not_served():
    assert history.fetch_league_season(FakeHttp({}), 42, 2013) is None


def test_overall_used_alone():
    phases = [("1", "Regular Season"), ("2", "Playoff Final"), ("3", "Playoff Overall")]
    http = FakeHttp(
        {
            2026: {
                "phases": phases,
                ("player", "1"): [_sk("Muster Anna", ZUG, 20, 1, 1)],
                ("player", "2"): [_sk("Muster Anna", ZUG, 6, 3, 1)],  # = Overall (SIHF 2025/26 oddity)
                ("player", "3"): [_sk("Muster Anna", ZUG, 6, 3, 1)],
            }
        }
    )
    out = history.fetch_league_season(http, 42, 2026)
    assert out["playoffs"][0]["games"] == 6


def test_gaa_recomputed_over_phases():
    rows = history.sum_phases(
        [
            (
                None,
                [
                    {
                        "name": "G",
                        "team": "T",
                        "goalie": True,
                        "games": 2,
                        "goals_against": 4,
                        "seconds": 7200,
                        "gaa": 2.0,
                    }
                ],
            ),
            (
                None,
                [
                    {
                        "name": "G",
                        "team": "T",
                        "goalie": True,
                        "games": 1,
                        "goals_against": 0,
                        "seconds": 3600,
                        "gaa": 0.0,
                    }
                ],
            ),
        ]
    )
    assert rows[0]["games"] == 3 and rows[0]["gaa"] == 1.33


@pytest.fixture
def people(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        anna = repo.upsert_athlete(
            s, sport, "Anna Muster", nationality="SUI", position="F", birth_date=date(2000, 5, 1)
        )
        gabi = repo.upsert_athlete(s, sport, "Gabi Wand", nationality="SUI", position="G")
        kim = repo.upsert_athlete(s, sport, "Kim Fremd", nationality="CAN", position="F")
        lea = repo.upsert_athlete(s, sport, "Lea Import", nationality="AUT", position="F")
        epp = repo.upsert_athlete(
            s, sport, "Eva Prospect", nationality="SUI", position="F", eliteprospects_id="9"
        )
        kid = repo.upsert_athlete(
            s, sport, "Jung Kind", nationality="SUI", position="F", birth_date=date(2020, 1, 1)
        )
        s.add(m.EpProfile(athlete_id=epp.id, ep_id="9", url="u", fetched_at=STAND, raw={}, data={}))
        # Lea's club is known from an article station (no season): her SIHF rows are plausible
        s.add(
            m.CareerStint(
                athlete_id=lea.id,
                team_name="SC Bern Frauen",
                kind="club",
                source="web",
                source_url="https://www.scb.ch/x",
                status="confirmed",
            )
        )
        # Anna's current season row (nightly table, EP-style team name)
        s.add(
            m.SeasonStat(
                athlete_id=anna.id,
                season=2026,
                league="SWHL A (W)",
                team_name="EVZ Women's Team",
                team_kind="club",
                phase="regular",
                games=8,
                source="sihf",
                source_url="https://www.sihf.ch/de/ligen/postfinance-womens-league/",
                fetched_at=STAND,
            )
        )
        s.commit()
        return {a.full_name: a.id for a in (anna, gabi, kim, lea, epp, kid)}


def _fetched():
    reg = [
        {
            "name": "Anna Muster",
            "team": "EV Zug",
            "games": 20,
            "goals": 5,
            "assists": 4,
            "points": 9,
            "penalty_minutes": 2,
            "url": "https://data.sihf.ch/statistic/api/cms/table?alias=player&searchQuery=1//42&language=de&filterBy=Season,Phase&filterQuery=2026/11",
        },
        {
            "name": "Gabi Wand",
            "team": "EV Zug",
            "goalie": True,
            "games": 18,
            "goals": 0,
            "assists": 1,
            "points": 1,
            "penalty_minutes": 2,
            "goals_against": 30,
            "gaa": 1.8,
            "url": "https://data.sihf.ch/statistic/api/cms/table?alias=goalkeeper&searchQuery=1//42&language=de&filterBy=Season,Phase&filterQuery=2026/11",
        },
        {
            "name": "Kim Fremd",
            "team": "EV Zug",
            "games": 20,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "penalty_minutes": 0,
            "url": "u",
        },
        {
            "name": "Lea Import",
            "team": "SC Bern",
            "games": 20,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "penalty_minutes": 0,
            "url": "u",
        },
        {
            "name": "Eva Prospect",
            "team": "SC Bern",
            "games": 20,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "penalty_minutes": 0,
            "url": "u",
        },
        {
            "name": "Jung Kind",
            "team": "SC Bern",
            "games": 20,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "penalty_minutes": 0,
            "url": "u",
        },
        {
            "name": "Niemand Bekannt",
            "team": "SC Bern",
            "games": 20,
            "goals": 1,
            "assists": 1,
            "points": 2,
            "url": "u",
        },
    ]
    older = [dict(r, url=r["url"].replace("2026/11", "2025/7")) for r in reg[:1]]
    return [(42, 2026, {"regular": reg, "playoffs": []}), (42, 2025, {"regular": older, "playoffs": []})]


def test_store_history_namesake_rules_and_idempotent(db, people):
    with Session(db) as s:
        rep = history.HistoryReport()
        history.store_history(s, _fetched(), rep, STAND)
        s.commit()
        rows = {
            (r.athlete_id, r.season)
            for r in s.scalars(select(m.SeasonStat).where(m.SeasonStat.season < 2026))
        }
        assert (people["Anna Muster"], 2025) in rows and (people["Anna Muster"], 2024) in rows
        assert (people["Gabi Wand"], 2025) in rows
        assert (people["Lea Import"], 2025) in rows  # foreign, club known
        assert (people["Kim Fremd"], 2025) not in rows  # foreign, no club link: reported
        assert (people["Eva Prospect"], 2025) not in rows  # EP data: EP is her history
        assert (people["Jung Kind"], 2025) not in rows  # age 5
        assert people["Kim Fremd"] in rep.doubtful and people["Jung Kind"] in rep.doubtful
        assert rep.with_ep == 1 and rep.unmatched == 1 and rep.stored == 4
        gabi = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == people["Gabi Wand"]))
        assert (
            gabi.team_name == "EV Zug" and gabi.goals_against == 30 and gabi.gaa == 1.8 and gabi.assists == 1
        )
        anna = s.scalar(
            select(m.SeasonStat).where(
                m.SeasonStat.athlete_id == people["Anna Muster"], m.SeasonStat.season == 2025
            )
        )
        assert anna.goals_against is None and anna.points == 9 and "alias=player" in anna.source_url
        first_id = anna.id

        rep2 = history.HistoryReport()
        history.store_history(s, _fetched(), rep2, STAND)
        s.commit()
        assert rep2.stored == 0 and rep2.updated == 0 and rep2.unchanged == 4
        assert s.get(m.SeasonStat, first_id) is not None


def test_stints_from_rows_open_end_and_idempotent(db, people):
    with Session(db) as s:
        history.store_history(s, _fetched(), history.HistoryReport(), STAND)
        counts = history.derive_stints(s, today=TODAY)
        s.commit()
        anna = list(
            s.scalars(
                select(m.CareerStint).where(
                    m.CareerStint.athlete_id == people["Anna Muster"], m.CareerStint.source == "sihf"
                )
            )
        )
        assert [(x.team_name, x.league, x.season_from, x.season_to) for x in anna] == [
            ("EV Zug", "SWHL A (W)", 2024, None)  # continues: EVZ Women's Team in the 2026/27 table
        ]
        assert anna[0].confidence == 0.95 and anna[0].status == "confirmed"
        assert anna[0].source_url.endswith("filterBy=Season&filterQuery=2025&language=de")
        gabi = s.scalar(select(m.CareerStint).where(m.CareerStint.athlete_id == people["Gabi Wand"]))
        assert (gabi.season_from, gabi.season_to) == (2025, 2025) and "alias=goalkeeper" in gabi.source_url
        assert counts["neu"] >= 3
        again = history.derive_stints(s, today=TODAY)
        assert again["neu"] == 0 and again["entfernt"] == 0 and again["unveraendert"] == counts["neu"]


def test_stint_specs_new_club_this_season():
    def row(season, team, league="SWHL A (W)"):
        return m.SeasonStat(
            season=season, team_name=team, league=league, phase="regular", source_url="x?searchQuery=1//42"
        )

    specs = history.stint_specs(
        [row(2023, "SC Bern"), row(2024, "SC Bern"), row(2025, "EV Zug"), row(2026, "ZSC Lions Frauen")], 2026
    )
    assert [(x.team, x.season_from, x.season_to) for x in specs] == [
        ("SC Bern", 2023, 2024),
        ("EV Zug", 2025, 2025),
        ("ZSC Lions Frauen", 2026, None),
    ]


def test_audit_and_scope(db, people):
    with Session(db) as s:
        scope = history.scope_ids(s, [], [3])
        gaps = {g.athlete_id: g for g in history.audit(s, scope, TODAY)}
        assert gaps[people["Anna Muster"]].thin and "keine_ep_id" in gaps[people["Anna Muster"]].flags
        assert not gaps[people["Eva Prospect"]].thin
        history.store_history(s, _fetched(), history.HistoryReport(), STAND)
        history.derive_stints(s, today=TODAY)
        gaps = {g.athlete_id: g for g in history.audit(s, scope, TODAY)}
        assert not gaps[people["Anna Muster"]].thin
        out = history.summary(list(gaps.values()))
        assert out["tier3"]["personen"] == 6
        urls = history.ep_url_list(s, list(gaps.values()))
        assert all("eliteprospects.com/search/player?q=" in u["url"] for u in urls)
        s.rollback()


def test_ep_id_decisions():
    a = m.Athlete(id=1, full_name="Anna Muster", birth_date=date(2000, 5, 1), nationality="SUI")
    own = ep_ids.Candidate("111", "wikidata_eigenes_item")
    assert ep_ids.decide(a, [own]).ep_id == "111"
    same = ep_ids.Candidate("222", "wikidata_name", date(2000, 5, 1), "SUI")
    other = ep_ids.Candidate("333", "wikidata_name", date(1999, 1, 1), "SUI")
    r = ep_ids.decide(a, [same, other])
    assert r.ep_id == "222" and r.how == "geburtsdatum_gleich"
    assert ep_ids.decide(a, [other]).ep_id is None
    b = m.Athlete(id=2, full_name="Kim Fremd", nationality="CAN")
    assert ep_ids.decide(b, [ep_ids.Candidate("4", "wikidata_name", None, "CAN")]).ep_id == "4"
    assert ep_ids.decide(b, [ep_ids.Candidate("4", "wikidata_name", None, "USA")]).ep_id is None
    link = ep_ids.Candidate("5", "gespeicherter_link", url="https://www.srf.ch/x")
    assert ep_ids.decide(b, [link]).ep_id is None  # an article link alone is never enough


def test_html_to_raw(db, people, tmp_path):
    from klugscheisser.ep_import import html_to_raw

    page = {"props": {"pageProps": {"playerData": {"player": {"id": 4242, "name": "Kim Fremd"}}}}}
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script></html>'
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / f"{people['Kim Fremd']}.html").write_text(html)
    (tmp_path / "in" / f"{people['Anna Muster']}.html").write_text(html)  # someone else's page
    (tmp_path / "in" / f"{people['Lea Import']}.html").write_text("<html>Just a moment...</html>")
    with Session(db) as s:
        counts = html_to_raw(s, tmp_path / "in", tmp_path / "out")
    assert counts["written"] == 1 and counts["other_person"] == 1
    raw = json.loads((tmp_path / "out" / f"{people['Kim Fremd']}.json").read_text())
    assert raw["eliteprospects_id"] == "4242" and raw["athlete_id"] == people["Kim Fremd"]


def test_club_patterns_venue_cities():
    def hit(team, text):
        from klugscheisser.domain.names import normalize

        return any(p.search(normalize(text)) for p in history.club_patterns(team))

    assert not hit("SC Bern", "Österreich verliert das Länderspiel in Bern 1:3")
    assert hit("SC Bern", "Sie wechselt zum SC Bern Frauen")
    assert hit("Hockey Club Ladies Lugano", "Neu bei den Lugano Ladies")
    assert not hit("Hockey Club Ladies Lugano", "Turnier in Lugano")
    assert hit("HC Ambri-Piotta", "Stürmerin von Ambrì")
    assert hit("ZSC Lions", "ZSC Lions Frauen gewinnen")
    assert not hit("ZSC Lions", "GCK Lions gewinnen")
