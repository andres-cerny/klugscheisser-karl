"""2026-10-28: league season tables of other countries (SDHL/NDHL from stats.swehockey.se, Auroraliiga from
tulospalvelu.leijonat.fi) in the nightly `saisonwerte` step, with the match check (stats/current.plausible).
No network: trimmed copies of the real pages (27.10.2026) in tests/fixtures/results."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.results import leijonat_stats, swehockey_stats
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.stats import current, queries

FIX = Path(__file__).parent / "fixtures" / "results"
PBT = (FIX / "swehockey_playersbyteam_20960.html").read_text()
LEADERS = (FIX / "swehockey_leaders_20960.html").read_text()
PLAYERS = json.loads((FIX / "leijonat_players_3706.json").read_text())
GOALIES = json.loads((FIX / "leijonat_goalies_3706.json").read_text())
SUBSERIE = json.loads((FIX / "leijonat_subserie_3706.json").read_text())

SETTINGS = Settings(
    _env_file=None,
    auto_refresh=False,
    sihf_data_enabled=False,
    season_totals_college_enabled=False,
    leijonat_season=2027,
    season_totals_jihf_enabled=False,
    season_totals_swehockey_leagues=[{"schedule_id": 20960, "league": "SDHL (W)", "name": "SDHL"}],
)
EP_DAY = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self):
        self.urls = []

    def text(self, url, params=None):
        self.urls.append(url)
        return PBT if "PlayersByTeam" in url else LEADERS

    def json(self, url, params=None, headers=None):
        self.urls.append(url)
        if url.endswith("getsubseries"):
            return [{"subSerieId": 3706, "subSerieName": "Auroraliiga"}]
        if url.endswith("getsubserie"):
            return SUBSERIE
        if url.endswith("getplayers"):
            return PLAYERS
        if url.endswith("getgoalkeepers"):
            return GOALIES
        raise AssertionError(url)


def test_swehockey_players_by_team():
    rows = swehockey_stats.parse_players_by_team(PBT)
    tulus = next(r for r in rows if r["name"] == "Noora Tulus")
    assert tulus == {
        "name": "Noora Tulus",
        "team": "Brynäs IF",
        "goalie": False,
        "games": 9,
        "goals": 5,
        "assists": 10,
        "points": 15,
        "penalty_minutes": 0,
        "plus_minus": 12,
    }
    goalies = [r for r in rows if r["goalie"]]
    pes = next(r for r in goalies if r["name"] == "Klara Peslarova")
    assert (
        pes["games"],
        pes["goals_against"],
        pes["saves"],
        pes["save_pct"],
        pes["gaa"],
        pes["shutouts"],
    ) == (
        6,
        5,
        129,
        0.963,
        0.83,
        2,
    )
    # a goalie's line in the skater table (dressed games) is not a skater row; dressed-only goalies are left out
    assert not any(r["name"] == "Klara Peslarova" and not r["goalie"] for r in rows)
    assert all(r["games"] for r in goalies)
    assert swehockey_stats.last_update(LEADERS) == datetime(2026, 10, 27, 13, 2, tzinfo=swehockey_stats.TZ)


def test_leijonat_league_rows():
    rows = leijonat_stats.skater_rows(PLAYERS["Players"])
    assert {r["name"] for r in rows} == {"Kiira Yrjänen", "Emma Vilén"}  # the goalie (MV) is not a skater
    vilen = sorted((r["team"], r["games"], r["points"]) for r in rows if r["name"] == "Emma Vilén")
    assert vilen == [("HIFK", 8, 3), ("Kärpät", 4, 1)]  # one row per team after a move
    yrj = next(r for r in rows if r["name"] == "Kiira Yrjänen")
    assert (yrj["team"], yrj["games"], yrj["goals"], yrj["assists"], yrj["birth_year"]) == (
        "Kiekko-Espoo",
        13,
        15,
        8,
        2002,
    )
    g = leijonat_stats.goalie_rows(GOALIES["Players"])[0]
    assert (g["name"], g["games"], g["saves"], g["goals_against"], g["save_pct"]) == (
        "Minja Drufva",
        3,
        40,
        2,
        0.952,
    )
    assert "gaa" not in g  # the feed has no time on ice: its GAA 0,00 is not taken
    assert leijonat_stats.stand(SUBSERIE).date() == date(2026, 9, 13)  # last final day, not the scheduled one


def test_same_club():
    assert current.same_club("Malmö (SWE)", "IF Malmö Redhawks")
    assert current.same_club("HV71", "HV 71")
    assert current.same_club("Brynäs IF Dam", "Brynäs IF")
    assert not current.same_club("AIK", "Leksands IF")
    assert not current.same_club("Luleå HF/MSSK", "Frölunda HC")


def _ep(aid, league, team, **v):
    return m.SeasonStat(
        athlete_id=aid,
        season=2026,
        league=league,
        team_name=team,
        team_kind="club",
        phase="regular",
        source="eliteprospects",
        source_url="https://www.eliteprospects.com/player/1/x",
        fetched_at=EP_DAY,
        **v,
    )


@pytest.fixture
def world(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        p = {
            # EP has her in the SDHL this season: the table row replaces EP's frozen row
            "tulus": repo.upsert_athlete(s, sport, "Noora Tulus", nationality="FIN", position="F"),
            # goalie with diacritics in Karl, none in the table
            "pes": repo.upsert_athlete(s, sport, "Klára Peslarová", nationality="CZE", position="G"),
            # namesake: EP has her in the PWHL this season -> not stored
            "svensson": repo.upsert_athlete(s, sport, "Elin Svensson", nationality="SWE", position="F"),
            # Karl knows nothing about her club: stored, listed as unconfirmed
            "blom": repo.upsert_athlete(s, sport, "Lova Blom", nationality="SWE", position="D"),
            # Leijonat gives the birth year: another year -> not stored
            "yrj": repo.upsert_athlete(s, sport, "Kiira Yrjänen", nationality="FIN", position="F"),
            "vilen": repo.upsert_athlete(s, sport, "Emma Vilén", nationality="FIN", position="F"),
        }
        p["yrj"].birth_date = date(1999, 5, 1)
        p["vilen"].birth_date = date(2008, 3, 1)
        s.flush()
        s.add_all(
            [
                _ep(p["tulus"].id, "SDHL (W)", "Brynäs IF", games=2, goals=1, assists=1, points=2),
                _ep(p["svensson"].id, "PWHL (W)", "Boston Fleet", games=3, goals=0, assists=1, points=1),
            ]
        )
        s.commit()
        yield s, {k: v.id for k, v in p.items()}


def test_refresh_stores_league_tables_with_match_check(world):
    s, ids = world
    http = FakeHttp()
    out = current.refresh(s, SETTINGS, http=http)
    assert out["fehler"] is None
    assert (
        len(http.urls) == 2 + 4
    )  # swehockey: table + leaders; Leijonat: subseries, series, skaters, goalies

    def rows(aid, source):
        return list(
            s.scalars(
                select(m.SeasonStat).where(m.SeasonStat.athlete_id == aid, m.SeasonStat.source == source)
            )
        )

    tulus = rows(ids["tulus"], "swehockey")
    assert [(r.season, r.league, r.team_name, r.games, r.points) for r in tulus] == [
        (2026, "SDHL (W)", "Brynäs IF", 9, 15)
    ]
    assert tulus[0].fetched_at == datetime(2026, 10, 27, 13, 2, tzinfo=swehockey_stats.TZ)
    shown = queries.season_rows(s, ids["tulus"])
    assert [r.source for r in shown] == ["swehockey"]  # EP's frozen SDHL row is replaced, not added
    assert "statt Elite Prospects" in queries.row_dict(shown[0])["ersetzt"]

    pes = rows(ids["pes"], "swehockey")
    assert [(r.games, r.save_pct, r.shutouts) for r in pes] == [(6, 0.963, 2)]

    assert rows(ids["svensson"], "swehockey") == []  # EP: PWHL this season
    assert any("Elin Svensson" in x for x in out["zweifel_beispiele"])
    assert len(rows(ids["blom"], "swehockey")) == 1
    assert any("Lova Blom" in x for x in out["ohne_vereinsbeleg_beispiele"])

    assert rows(ids["yrj"], "leijonat_liiga") == []  # born 2002 per Leijonat, 1999 in Karl
    vilen = rows(ids["vilen"], "leijonat_liiga")
    assert sorted((r.league, r.team_name, r.games) for r in vilen) == [
        ("Auroraliiga (W)", "HIFK", 8),
        ("Auroraliiga (W)", "Kärpät", 4),
    ]

    # second night: same rows, nothing doubled
    current.refresh(s, SETTINGS, http=FakeHttp())
    assert len(rows(ids["tulus"], "swehockey")) == 1


def test_switches_off_make_no_requests(world):
    s, _ = world
    http = FakeHttp()
    off = SETTINGS.model_copy(
        update={"season_totals_swehockey_enabled": False, "season_totals_leijonat_leagues_enabled": False}
    )
    current.refresh(s, off, http=http)
    assert http.urls == []


# --- Japan: totals counted from the JIHF game sheets ------------------------------------------------------

SHEET = (FIX / "jihf_gamesheet_903_5.html").read_text()


def test_jihf_sheet_and_totals():
    from klugscheisser.collectors.results import jihf_stats

    sh = jihf_stats.parse_sheet(SHEET)
    assert sh["teams"] == ["Seibu Princess Rabbits", "Daishin"]
    assert sh["date"].startswith("2026-09-21T16:50")
    noro = next(p for p in sh["players"] if p["key"] == "野呂里桜")  # "(BP)" mark removed
    assert (noro["team"], noro["g"], noro["a"]) == ("Daishin", 1, 1)
    gk = {g["key"]: g for g in sh["goalies"]}
    assert (gk["川口莉子"]["ga"], gk["川口莉子"]["saves"], gk["川口莉子"]["minutes"]) == (1, 25, 60.0)
    assert (gk["竹中梨々奈"]["ga"], gk["竹中梨々奈"]["saves"]) == (3, 12)  # pulled for the empty net at 58:26
    assert not any(p["pos"] == "GK" for p in sh["players"])  # goalies are not skaters, backups not counted
    rows = {r["key"]: r for r in jihf_stats.totals([sh, sh])}
    assert (rows["野呂里桜"]["games"], rows["野呂里桜"]["points"]) == (2, 4)
    kaw = rows["川口莉子"]
    assert (kaw["games"], kaw["saves"], kaw["goals_against"], kaw["save_pct"], kaw["gaa"]) == (
        2,
        50,
        2,
        0.962,
        1.0,
    )
    assert jihf_stats.team_name("ＴＯＹＯＴＡ ＣＹＧＮＵＳ（トヨタシグナス）") == "Toyota Cygnus"


class JihfHttp:
    requests = 0

    def text(self, url, params=None):
        self.requests += 1
        if "detail.php" in url:
            return '<a href="./game/gamesheet.php?meet_id=903&game_no=5">x</a>' if "903" in url else ""
        return SHEET


def test_jihf_rows_match_only_through_the_seed_spelling(db, monkeypatch):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        rio = repo.upsert_athlete(s, sport, "Rio Noro", nationality="JPN", position="F")
        riko = repo.upsert_athlete(s, sport, "Riko Kawaguchi", nationality="JPN", position="G")
        s.commit()
        monkeypatch.setattr(
            current, "native_names", lambda s: {"野呂里桜": "Rio Noro", "川口莉子": "Riko Kawaguchi"}
        )
        jp = SETTINGS.model_copy(
            update={
                "season_totals_swehockey_enabled": False,
                "season_totals_leijonat_leagues_enabled": False,
                "season_totals_jihf_enabled": True,
                "season_totals_jihf_meets": [903, 904],
            }
        )
        http = JihfHttp()
        out = current.refresh(s, jp, http=http)
        assert out["pro_quelle"]["jihf_anfragen"] == 3  # two round pages + one new sheet
        row = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == rio.id))
        assert (row.league, row.team_name, row.games, row.goals, row.assists, row.source) == (
            "Japan (W)",
            "Daishin",
            1,
            1,
            1,
            "jihf",
        )
        g = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == riko.id))
        assert (g.games, g.saves, g.goals_against) == (1, 25, 1)
        assert out["pro_quelle"]["jihf_2026_ohne_lateinischen_namen"] > 20  # everyone else: not guessed
        http2 = JihfHttp()
        current.refresh(s, jp, http=http2)
        assert http2.requests == 2  # the finished sheet is kept: only the round pages again


# --- youth: U18 WCh from Leijonat ------------------------------------------------------------------------

U18 = json.loads((FIX / "leijonat_u18_players_4432.json").read_text())


def test_youth_parts_counted_once_and_checked(db):
    from klugscheisser.stats import youth

    acc = {}
    youth.add_part(acc, U18["Players"], [])
    manetsch = acc[("SUI", "Jael Manetsch")]
    assert (manetsch["games"], manetsch["goals"], manetsch["assists"]) == (3, 1, 2)

    class U18Http:
        requests = 0

        def json(self, url, params=None, headers=None):
            self.requests += 1
            if url.endswith("getsubseries"):
                if params["season"] != 2025:
                    return []
                return [
                    {
                        "subSerieId": 4431,
                        "subSerieName": "W18 World Championship 4.-12.1.2025 Vantaa, FIN, Group A",
                    },
                    {
                        "subSerieId": 4432,
                        "subSerieName": "W18 World Championship 4.-12.1.2025 Vantaa, FIN, Group B",
                    },
                    {"subSerieId": 4590, "subSerieName": "W18 EHT: 4 Nations Tournament 13.-15.12.2024"},
                ]
            if url.endswith("getplayers"):
                return U18  # both parts answer the same: counted once (older seasons do this)
            return {"Players": []}

    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        jael = repo.upsert_athlete(s, sport, "Jael Manetsch", nationality="SUI", position="F")
        jael.birth_date = date(2007, 1, 1)  # year-only date in Karl: the year decides
        norina = repo.upsert_athlete(s, sport, "Norina Müller", nationality="SUI", position="F")
        norina.birth_date = date(2004, 3, 3)  # another person of the same name
        sonja = repo.upsert_athlete(s, sport, "Sonja Inkamp", nationality="GER", position="F")  # other nation
        s.add(_ep(sonja.id, "WJC-18 (W)", "Switzerland U18", games=3))  # and EP has the row anyway
        s.add(
            m.SeasonStat(
                athlete_id=sonja.id,
                season=2024,
                league="WJC-18 (W)",
                team_name="Switzerland U18",
                team_kind="national",
                phase="regular",
                source="eliteprospects",
                source_url="https://www.eliteprospects.com/player/2/x",
                fetched_at=EP_DAY,
                games=3,
            )
        )
        s.commit()
        http = U18Http()
        out = youth.backfill(s, http, store=True)
        rows = list(s.scalars(select(m.SeasonStat).where(m.SeasonStat.source == "leijonat_u18")))
        assert [(r.athlete_id, r.season, r.league, r.team_name, r.games, r.points) for r in rows] == [
            (jael.id, 2024, "WJC-18 (W)", "Switzerland U18", 3, 3)
        ]
        assert any("Norina" in x for x in out["zweifel_beispiele"])
        assert out["pro_quelle"]["ep_vorhanden"] == 1
        assert (
            http.requests == 10 + 2 * 2
        )  # 10 seasons listed, 2 WCh parts x (skaters, goalies); EHT not asked
        youth.backfill(s, U18Http(), store=True)  # idempotent
        assert len(list(s.scalars(select(m.SeasonStat).where(m.SeasonStat.source == "leijonat_u18")))) == 1
