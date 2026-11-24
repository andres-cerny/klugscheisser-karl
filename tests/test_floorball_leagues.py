"""2026-11-24: SSL (ssl.se API) and F-Liiga (fliiga.com) league adapters, matching, writing and the "newest dated
source wins" stint fix. Offline: recorded, trimmed answers under tests/fixtures/floorball/."""

from datetime import date
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball import fliiga, leagues, run, ssl_se
from klugscheisser.collectors.floorball.common import CappedHttp
from klugscheisser.db import models as m

FX = Path(__file__).parent / "fixtures" / "floorball"
TODAY = date(2026, 11, 24)


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def handler(request: httpx.Request) -> httpx.Response:
    url, q = request.url, request.url.params
    if url.host == "www.ssl.se":
        if url.path.endswith("/layout-info"):
            return httpx.Response(200, text=fx("ssl_layout.json"))
        goalies = url.path.endswith("/goalkeepers_summary")
        if q.get("ssgtUuid") == "qqing3frfp":
            return httpx.Response(
                200, text=fx("ssl_goalies_2026.json" if goalies else "ssl_players_2026.json")
            )
        if q.get("ssgtUuid") == "ov02ysucwb" and not goalies:
            return httpx.Response(200, text=fx("ssl_players_2025.json"))
        return httpx.Response(200, text="[]")
    if url.host == "fliiga.com":
        if url.path.startswith("/wp-json/"):
            return httpx.Response(200, text=fx("fliiga_list.json"), headers={"x-wp-totalpages": "1"})
        for slug in ("juuso-ahola", "markus-laakso"):
            if slug in url.path:
                return httpx.Response(200, text=fx(f"fliiga_{slug.split('-')[1]}.html"))
    return httpx.Response(404)


def fake_http(max_requests: int = 50) -> CappedHttp:
    return CappedHttp(
        delay_s=0, max_requests=max_requests, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _athlete(s: Session, name: str, nationality: str | None = "SWE", **kw) -> m.Athlete:
    sport = s.scalar(select(m.Sport).where(m.Sport.key == "floorball_men"))
    if sport is None:
        sport = m.Sport(key="floorball_men", name="Unihockey Männer")
        s.add(sport)
        s.flush()
    first, last = name.split(" ", 1)
    a = m.Athlete(
        sport_id=sport.id,
        full_name=name,
        first_name=first,
        last_name=last,
        name_normalized=name.lower(),
        nationality=nationality,
        **kw,
    )
    s.add(a)
    s.flush()
    return a


# --- parsing --------------------------------------------------------------------------------------------------


def test_ssl_seasons_and_stats():
    import json

    seasons = ssl_se.parse_seasons(json.loads(fx("ssl_layout.json")))
    assert (2026, "regular", "qqing3frfp") in seasons and all(
        s[2] != "ujdsndgskt" for s in seasons
    )  # no women
    players, report = ssl_se.fetch_players(fake_http(), today=TODAY)
    assert report["saisons"] == 2
    by = {p.name: p for p in players}
    ase = by["Arvid Äse"]
    assert (ase.birth_date, ase.nationality, ase.position) == (date(2004, 3, 17), "SWE", "F")
    r26 = next(r for r in ase.rows if r.season == 2026)
    assert (r26.team, r26.games, r26.goals, r26.assists, r26.points) == ("Pixbo IBK", 10, 20, 12, 32)
    assert {r.season for r in ase.rows} == {2025, 2026}
    assert by["Jan Bürki"].birth_date is None and by["Jan Bürki"].nationality is None  # "N/A"
    gk = by["Miroslav Kovarik"]
    assert gk.position == "G" and gk.rows[0].goals_against == 29 and gk.rows[0].save_pct == 0.8092
    assert gk.rows[0].gaa is None  # API writes 0 for "no value"


def test_fliiga_player_page_field_player_and_goalie():
    p = fliiga.parse_player(fx("fliiga_ahola.html"), "https://fliiga.com/pelaajat/miehet/juuso-ahola/")
    assert (p.name, p.birth_year, p.team, p.position) == ("Juuso Ahola", 2003, "Nokian KrP", None)
    cur = [r for r in p.rows if r.season == 2026]
    assert [(r.phase, r.games, r.goals, r.assists, r.points, r.plus_minus) for r in cur] == [
        ("regular", 14, 20, 18, 38, 28)
    ]
    assert ("playoffs", 15, 32) in [(r.phase, r.games, r.points) for r in p.rows if r.season == 2025]
    assert next(r for r in p.rows if r.season == 2021).team == "Karhut"
    g = fliiga.parse_player(fx("fliiga_laakso.html"), "u")
    assert g.position == "G" and g.birth_year == 1995
    r = next(x for x in g.rows if x.season == 2026)
    assert (r.games, r.saves, r.goals_against, r.save_pct, r.gaa) == (9, 154, 25, 0.8603, 2.78)


def test_fliiga_list_only_men():
    import json

    refs = fliiga.parse_list(json.loads(fx("fliiga_list.json")))
    assert refs and all("/miehet/" in r.url for r in refs)
    assert "Juuso Ahola" in {r.name for r in refs}


# --- matching + writing ---------------------------------------------------------------------------------------


def test_match_by_name_birth_year_and_nationality(db):
    a = leagues.LeaguePlayer("1", "Erik Berg", "u1", birth_date=date(2000, 5, 1), nationality="SWE")
    b = leagues.LeaguePlayer("2", "Erik Berg", "u2", birth_date=date(1995, 5, 1), nationality="SWE")
    c = leagues.LeaguePlayer("3", "Jan Bürki", "u3")
    idx = leagues.Index([a, b, c])
    with Session(db) as s:
        assert idx.match(_athlete(s, "Erik Berg", birth_date=date(1995, 2, 2)))[0] is b
        assert idx.match(_athlete(s, "Erik Berg"))[1].startswith("mehrdeutig")
        assert idx.match(_athlete(s, "Erik Berg", birth_date=date(1990, 1, 2)))[1] == "Jahrgang passt nicht"
        assert idx.match(_athlete(s, "Jan Buerki", nationality="SUI"))[0] is c  # transliteration


def test_import_writes_stats_stints_profile_and_is_idempotent(db):
    with Session(db) as s:
        a = _athlete(s, "Arvid Äse")
        b = _athlete(s, "Jan Bürki", nationality="SUI", birth_date=date(1999, 5, 6))
        players, _ = ssl_se.fetch_players(fake_http(), today=TODAY)
        kw = dict(source="ssl", league="SSL", country="SWE", today=TODAY)
        dry = leagues.import_players(s, [a, b], players, dry_run=True, **kw)
        assert dry["players"] == 2 and s.scalar(select(m.SeasonStat.id)) is None
        res = leagues.import_players(s, [a, b], players, dry_run=False, **kw)
        s.commit()
        assert res["season_stats_added"] == 3 and res["matched"]["Jan Bürki"][0] == "FBC Kalmarsund"
        assert a.birth_date == date(2004, 3, 17) and a.position == "F" and b.position == "D"
        rows = list(s.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == a.id)))
        assert {(r.season, r.league, r.team_name, r.phase, r.source) for r in rows} == {
            (2025, "SSL", "Pixbo IBK", "regular", "ssl"),
            (2026, "SSL", "Pixbo IBK", "regular", "ssl"),
        }
        assert rows[0].source_url == "https://www.ssl.se/athlete-profile/qbR-5fZbIbltt"
        st = s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == b.id)).one()
        assert (st.team_name, st.season_from, st.season_to, st.source, st.league) == (
            "FBC Kalmarsund",
            2026,
            None,
            "league",
            "SSL",
        )
        assert st.quote == "SSL-Statistik: 2026/27 FBC Kalmarsund"
        again = leagues.import_players(s, [a, b], players, dry_run=False, **kw)
        assert not again.get("season_stats_added") and not again.get("stints_added")
        assert not again.get("season_stats_updated")


def test_fliiga_import_via_run_enrich(db):
    with Session(db) as s:
        a = _athlete(s, "Juuso Ahola", nationality="FIN", birth_date=date(2003, 8, 30))
        g = _athlete(s, "Markus Laakso", nationality="FIN", birth_date=date(1994, 4, 1))  # wrong year
        coach = _athlete(s, "Markus Laakso", nationality="FIN", role="coach")
        out = run.enrich(s, [a.id, g.id, coach.id], sources=("fliiga",), dry_run=False, http=fake_http())
        assert out["fliiga_players"] == 1 and out["fliiga_quelle"]["seiten"] == 2
        assert "Markus Laakso: Jahrgang passt nicht" in out["fliiga_skipped"]
        rows = list(s.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == a.id)))
        assert len(rows) == 10 and {r.source for r in rows} == {"fliiga"}
        stints = list(s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == a.id)))
        assert {(x.team_name, x.season_from) for x in stints} == {("Karhut", 2021), ("Nokian KrP", 2022)}
        assert out["stints_beendet"] == []


def test_outdated_open_stints_are_ended_not_deleted(db):
    with Session(db) as s:
        a = _athlete(s, "Jan Zaugg", nationality="SUI")

        def stint(team, frm, to, source):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_name=team,
                    kind="club",
                    season_from=frm,
                    season_to=to,
                    source=source,
                    source_url=f"https://example.org/{team}/{frm}",
                    quote="x",
                )
            )

        stint("Mullsjö AIS", 2019, None, "web")  # old article: "now at Mullsjö"
        stint("Floorball Köniz", 2021, 2022, "federation")
        stint("Floorball Köniz", 2021, None, "web")  # same club as the later "Köniz Bern": stays open
        stint("Floorball Köniz Bern", 2023, None, "federation")
        stint("Storvreta IBK", 2025, None, "web")  # newer than every structured station: stays
        s.flush()
        lines = leagues.close_outdated_open_stints(s, [a.id], dry_run=True)
        assert len(lines) == 1 and "Mullsjö AIS" in lines[0] and "endet 2020" in lines[0]
        assert (
            s.scalar(select(m.CareerStint.season_to).where(m.CareerStint.team_name == "Mullsjö AIS")) is None
        )
        leagues.close_outdated_open_stints(s, [a.id], dry_run=False)
        sts = {(x.team_name, x.season_from, x.source): x.season_to for x in s.scalars(select(m.CareerStint))}
        assert sts[("Mullsjö AIS", 2019, "web")] == 2020
        assert sts[("Floorball Köniz", 2021, "web")] is None
        assert sts[("Storvreta IBK", 2025, "web")] is None
        assert len(sts) == 5


def test_roster_club_overrides_nationality_and_placeholder_birth_dates(db, tmp_path):
    (tmp_path / "rosters").mkdir()
    (tmp_path / "rosters" / "DEN.yaml").write_text(
        "players:\n  - {name: Gustav Hansen, club: Nykvarns IF}\n", encoding="utf-8"
    )
    clubs = leagues.roster_clubs(tmp_path)
    assert clubs == {"gustav hansen": {"Nykvarns IF"}}
    p = leagues.LeaguePlayer("1", "Gustav Hansen", "u", nationality="SWE")
    p.rows.append(leagues.LeagueRow(2026, "Nykvarns IBF"))
    idx = leagues.Index([p])
    with Session(db) as s:
        a = _athlete(s, "Gustav Hansen", nationality="DEN")
        assert idx.match(a)[0] is None
        assert idx.match(a, clubs["gustav hansen"])[0] is p
    assert ssl_se._date("2025-01-01") is None and ssl_se._date("2004-03-17") == date(2004, 3, 17)


def test_structured_open_stints_are_not_ended(db):
    with Session(db) as s:
        a = _athlete(s, "Peteris Trekse", nationality="LAT")
        for team, frm, source in (("Classic", 2024, "league"), ("SV Wiler-Ersigen", 2026, "federation")):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_name=team,
                    kind="club",
                    season_from=frm,
                    source=source,
                    source_url=f"https://example.org/{frm}",
                    quote="x",
                )
            )
        s.flush()
        assert leagues.close_outdated_open_stints(s, [a.id], dry_run=False) == []
