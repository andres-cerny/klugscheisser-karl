"""2026-11-24: floorball project (men's WFC 2026): sport check, seed loader, tiers, tournament answers."""

import os
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import projects, tiers, tournament
from klugscheisser.db import models as m
from klugscheisser.domain.sport import sport_verdict
from klugscheisser.seed import floorball
from klugscheisser.sports import home_team_key, national_team_key

PROJECTS = """default: eishockey
projects:
  eishockey:
    name: Frauen-Eishockey
  unihockey:
    name: Unihockey
    scheduled_jobs: false
    seeds: false
    seeds_dir: {seeds}
    main_event: wfc-2026
    sport: floorball_men
    wording:
      person: Unihockeyspieler
      persons: Unihockeyspieler
      sport_de: Unihockey
      player: Spieler
      players: Spieler
      teammates: Mitspieler
"""

EVENT = """
event: wfc-2026
name: "Unihockey-WM 2026 der Männer"
aliases: [wfc-2026, unihockey-wm]
starts: "2026-12-05"
ends: "2026-12-13"
host: "Finnland (Tampere)"
city: Tampere
schedule_source: iff
games_source: IFF
sources:
  iff: {name: "IFF, WFC 2026", url: "https://floorball.sport/wfc2026", kind: official, date: "2026-11-24"}
  wiki: {name: "Wikipedia", url: "https://en.wikipedia.org/wiki/2026_Men%27s_World_Floorball_Championships", kind: web}
venues:
  - {city: Tampere, arena: "Nokia Arena", rounds: "alle Spiele", src: iff}
groups:
  A: {teams: [SWE, FIN, SUI, CZE]}
  B: {teams: [GER, AUT, NOR, LAT]}
facts:
  - {topic: [modus, uebersicht], text: "16 Teams in vier Gruppen.", src: iff}
history:
  - {year: 2024, host: "Malmö", gold: SWE, silver: FIN, bronze: CZE, sui: "4.", src: wiki}
games:
  - {key: wfc-2026-1, utc: "2026-12-05T14:00:00Z", home: SUI, away: CZE, venue: "Nokia Arena", city: Tampere,
     url: "https://floorball.sport/wfc2026/game/1"}
"""

ROSTER = """
event: wfc-2026
team: SUI
list_type: confirmed
season: "2026-27"
source: "Swiss Unihockey, WM-Kader (20.11.2026)"
source_url: "https://www.swissunihockey.ch/de/news/wm-kader"
source_date: "2026-11-20"
players:
  - name: "Noah Meier"
    pos: F
    number: 10
    captain: C
    club: "Grasshopper Club Zürich"
    birth: "1998-04-02"
    height_cm: 182
    career:
      - {team: "Grasshopper Club Zürich", league: "UPL", from: 2016, to: 2020}
      - {team: "IBK Dalen", league: "SSL", country: SWE, from: 2020}
    stats:
      - {season: 2025, team: "IBK Dalen", league: "SSL", games: 30, goals: 21, assists: 18, points: 39, pim: 4,
         source: swissunihockey, source_url: "https://api-v2.swissunihockey.ch/api/players/1"}
    facts:
      - {kind: medal, text: "WM-Bronze 2022", date: "2022-12-11", source_url: "https://floorball.sport/x"}
    funfacts:
      - {text: "Spielt nebenbei Alphorn.", source: "SRF, 01.11.2026", date: "2026-11-01",
         source_url: "https://www.srf.ch/sport/unihockey/alphorn"}
  - {name: "Pascal Keller", pos: G, number: 1, club: "SV Wiler-Ersigen"}
staff:
  - {name: "Johan Schönbeck", role: "Head Coach"}
"""

FUNFACTS_MD = """# Fun Facts
## Pascal Keller
- Hat als Kind Eishockey gespielt ([Blick, 03.10.2026](https://www.blick.ch/sport/unihockey/keller))
- Ohne Quelle, wird übersprungen
## Unbekannt Niemand
- Etwas (https://example.org/x, 2026-01-01)
"""


@pytest.fixture
def uni(db, tmp_path):
    seeds = tmp_path / "floorball"
    (seeds / "rosters").mkdir(parents=True)
    (seeds / "event.yaml").write_text(textwrap.dedent(EVENT), encoding="utf-8")
    (seeds / "rosters" / "wfc-2026-sui.yaml").write_text(textwrap.dedent(ROSTER), encoding="utf-8")
    (seeds / "funfacts.md").write_text(FUNFACTS_MD, encoding="utf-8")
    path = tmp_path / "projects.yaml"
    path.write_text(PROJECTS.format(seeds=seeds), encoding="utf-8")
    old = os.environ.get("PROJECTS_FILE")
    os.environ["PROJECTS_FILE"] = str(path)
    try:
        with projects.use("unihockey"):
            yield db, seeds
    finally:
        os.environ["PROJECTS_FILE"] = old or ""


def test_keys_and_wording_follow_the_project(uni):
    assert national_team_key("SUI") == "sui-men"
    assert home_team_key() == "sui-men"
    assert projects.wording().plural(2) == "2 Spieler"
    assert tiers.main_event() == "wfc-2026"


def test_sport_check_has_floorball_as_home():
    with_ice = "Eishockey: Puck, IIHF und NHL"
    floor = "Die Unihockey-Nati gewinnt im Powerplay gegen Schweden (IFF)"
    assert sport_verdict(with_ice).is_hockey  # no project file: ice hockey stays home
    assert not sport_verdict(floor).is_hockey


def test_sport_check_in_the_floorball_project(uni):
    floor = sport_verdict("Die Unihockey-Nati gewinnt im Powerplay gegen Schweden (IFF)")
    assert floor.is_hockey and floor.sport == "unihockey"
    ice = sport_verdict("Eishockey: Puck, IIHF und NHL")
    assert ice.is_other and ice.sport == "icehockey"
    assert sport_verdict("Spiel der Nati", "https://www.srf.ch/sport/unihockey/x").is_hockey


def test_seed_floorball_loads_everything_with_sources(uni):
    db, seeds = uni
    with Session(db) as s:
        stats = floorball.load(s, seeds)
        s.commit()
        assert stats.teams == 8 and stats.games == 1 and stats.rosters == 1
        assert stats.careers == 2 and stats.season_stats == 1 and stats.fun_facts == 2
        ev = s.scalar(select(m.Event).where(m.Event.key == "wfc-2026"))
        assert ev.kind == "tournament" and ev.starts_at.year == 2026
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-men"))
        assert sui.name == "Schweiz" and sui.kind == "national"
        game = s.scalar(select(m.Event).where(m.Event.key == "wfc-2026-1"))
        assert game.home_team_id == sui.id and game.source == "IFF"
        assert game.source_url == "https://floorball.sport/wfc2026/game/1"
        noah = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Noah Meier"))
        assert noah.sport.key == "floorball_men" and noah.nationality == "SUI" and noah.position == "F"
        stat = s.scalar(select(m.SeasonStat).where(m.SeasonStat.athlete_id == noah.id))
        assert (stat.season, stat.points, stat.penalty_minutes, stat.source) == (
            2025,
            39,
            4,
            "swissunihockey",
        )
        stints = s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == noah.id)).all()
        assert {c.team_name for c in stints} == {"Grasshopper Club Zürich", "IBK Dalen"}
        assert all(c.source_url for c in stints)
        fun = s.scalars(select(m.Fact).where(m.Fact.kind == "fun_fact")).all()
        assert {f.value_text for f in fun} == {"Spielt nebenbei Alphorn.", "Hat als Kind Eishockey gespielt"}
        assert all(f.source_url and f.source == "seed" for f in fun)
        keller_fun = next(f for f in fun if "Kind" in f.value_text)
        assert keller_fun.value["date"] == "2026-10-03" and keller_fun.value["quelle"] == "Blick, 03.10.2026"
        assert s.scalar(select(m.Fact).where(m.Fact.kind == "height_cm")).value["number"] == 182
        assert s.scalar(select(m.Fact).where(m.Fact.kind == "captaincy")) is not None
        assert stats.tournament_fun_facts == 1  # "Unbekannt Niemand": no stored person
        assert noah.tier == 1  # Swiss roster of the project's tournament
        # idempotent
        again = floorball.load(s, seeds)
        assert (again.careers, again.season_stats, again.fun_facts, again.teams) == (0, 0, 0, 0)


def test_tournament_answers_from_the_floorball_files(uni):
    db, seeds = uni
    with Session(db) as s:
        floorball.load(s, seeds)
        s.flush()
        assert tournament.events_dir() == Path(seeds) / "events"
        ev = s.scalar(select(m.Event).where(m.Event.key == "wfc-2026"))
        info = yaml_load(seeds / "event.yaml")
        hist = tournament.sec_history(info, "Wer gewann die WM 2024?")
        assert "Gold Schweden" in hist.zeilen[0].text and hist.zeilen[0].url.startswith(
            "https://en.wikipedia"
        )
        kader = tournament.sec_rosters(s, ev, info, ["SUI"], seeds / "events")
        assert any("2 Spieler" in z.text for z in kader.zeilen)
        assert not any("Spielerinnen" in z.text for z in kader.zeilen)


def yaml_load(path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_funfacts_md_parser():
    items, skipped = floorball.parse_funfacts_md(FUNFACTS_MD)
    assert [i["person"] for i in items] == ["Pascal Keller", "Unbekannt Niemand"]
    assert items[1]["url"] == "https://example.org/x" and items[1]["date"] == "2026-01-01"
    assert items[1]["text"] == "Etwas"
    assert len(skipped) == 1


# --- 2026-11-24 (later): end date of schedules, results service, research formats ----------------------------


def test_scheduled_until_stops_jobs_the_day_after(tmp_path):
    from datetime import date

    from klugscheisser.jobs.nightly import scheduled_projects

    path = tmp_path / "p.yaml"
    path.write_text(
        "default: eishockey\nprojects:\n  eishockey:\n    name: E\n  unihockey:\n    name: U\n"
        "    scheduled_jobs: true\n    scheduled_until: 2026-12-14\n    sport: floorball_men\n"
        "  allgemein:\n    name: A\n    scheduled_jobs: false\n",
        encoding="utf-8",
    )
    old = os.environ.get("PROJECTS_FILE")
    os.environ["PROJECTS_FILE"] = str(path)
    try:
        uni = projects.get("unihockey")
        assert uni.scheduled_until == date(2026, 12, 14)
        assert uni.scheduled_on(date(2026, 12, 14)) and not uni.scheduled_on(date(2026, 12, 15))
        assert projects.get("eishockey").scheduled_on(date(2030, 1, 1))
        assert not projects.get("allgemein").scheduled_on(date(2026, 12, 1))
        keys = {p.key for p in scheduled_projects()}
        assert "eishockey" in keys and "allgemein" not in keys
        assert ("unihockey" in keys) == uni.scheduled_on()
    finally:
        os.environ["PROJECTS_FILE"] = old or ""


def test_torneopal_schedule_widget_and_results(uni):
    from datetime import UTC, datetime

    from klugscheisser.collectors.floorball import torneopal

    db, seeds = uni
    games = torneopal.parse(
        (Path(__file__).parent / "fixtures/floorball/torneopal_schedule_group1.txt").read_text()
    )
    assert (
        len(games) == 6
        and games[1].match_id == 967559
        and (games[1].home, games[1].away)
        == (
            "Finland",
            "Switzerland",
        )
    )
    assert games[1].score is None and games[1].starts(2026) == datetime(2026, 12, 5, 17, 0, tzinfo=UTC)
    assert torneopal.score_text("5–4 ja") == "5:4 n.V." and torneopal.score_text("3 – 2") == "3:2"
    with Session(db) as s:
        floorball.load(s, seeds)
        games[1].score = "4–3"
        counts = torneopal.store(s, games, "wfc-2026", now=datetime(2026, 12, 6, tzinfo=UTC))
        ev = s.scalar(select(m.Event).where(m.Event.key == "torneopal-967559"))
        assert (ev.score, ev.status, ev.source) == ("4:3", "final", "Torneopal")
        assert ev.home_team_id == s.scalar(select(m.Team.id).where(m.Team.key == "fin-men"))
        assert counts["resultate"] == 1


RESEARCH_ROSTER = """
team: Switzerland
code: SUI
status: official_wfc_squad
announced: {date: 2026-11-05, source_url: "https://unihockey.ch/a-28693/", source_title: "Mit zehn Debütanten"}
retrieved: 2026-11-24
squad_facts: ["20 players."]
coaches:
  head_coach: {name: Johan Schönbeck, nationality: Sweden}
  assistants: [Armin Brunner]
captain: {name: null}
players:
  - {name: Jan Bürki, birth_date: 1999-05-06, position: defender, club: FBC Kalmarsund, league: "SSL (SWE)",
     wfc_debut: false, notes: "one of 3 players left from WFC 2022"}
  - {name: Matteo Steiner, birth_date: 2002-04-20, position: forward, club: Nykvarns IBF, league: "SSL? (SWE)",
     wfc_debut: true, caps: 12, goals: 3, assists: 4, points: 7}
"""


def test_research_roster_format():
    import yaml

    from klugscheisser.seed.floorball_info import normalize_roster

    d = normalize_roster(yaml.safe_load(RESEARCH_ROSTER), "wfc-2026")
    assert (
        d["team"] == "SUI"
        and d["list_type"] == "confirmed"
        and d["source_url"] == "https://unihockey.ch/a-28693/"
    )
    b, st = d["players"]
    assert (b["pos"], b["league"], b["country"], b["birth"]) == ("D", "SSL", "SWE", "1999-05-06")
    assert b["funfacts"][0]["source_url"] == "https://unihockey.ch/a-28693/"
    assert {f["kind"] for f in st["facts"]} == {"appearances", "national_team_event"}
    assert [(x["name"], x["role"], x.get("nationality")) for x in d["staff"]] == [
        ("Johan Schönbeck", "Head Coach", "SWE"),
        ("Armin Brunner", "Assistant Coach", None),
    ]


def test_funfacts_numbered_items_with_source_lines():
    md = (
        "# Fun facts\n\n## The 2026 Swiss team\n\n"
        "1. **Two brothers: Noël and Elia Seiler.** First game together.\n"
        "   Source: unihockey.ch, 2026-09-05, https://unihockey.ch/x-28527/\n"
        "2. **Do NOT use:** April fools.\n   https://unihockey.ch/joke/\n"
        "3. Schmuki won the cup. **[unverified: German Wikipedia only]** https://de.wikipedia.org/wiki/P\n"
    )
    items, skipped = floorball.parse_funfacts_md(md)
    assert [i["url"] for i in items] == ["https://unihockey.ch/x-28527/", "https://de.wikipedia.org/wiki/P"]
    assert items[0]["date"] == "2026-09-05" and items[0]["text"].startswith("Two brothers")
    assert items[1]["unverified"] and "unverified" not in items[1]["text"]
    assert any("nicht verwenden" in x for x in skipped)


def test_nightly_skips_ice_hockey_steps_in_floorball(uni):
    from klugscheisser.jobs import nightly

    result: dict = {}
    nightly._step(result, "kaderwatch", lambda: 1 / 0)  # would fail if it ran
    nightly._step(result, "profiles", lambda: {"ok": 1})
    assert result["kaderwatch"]["status"].startswith("übersprungen") and result["profiles"] == {"ok": 1}
    assert "fehler" not in result


def test_on_demand_update_runs_no_ice_hockey_results_in_floorball(uni, monkeypatch):
    """2026-11-24: a question's background update wrote women's ice hockey games onto the floorball teams."""
    from klugscheisser.config import Settings
    from klugscheisser.ingest import results

    db, _ = uni
    monkeypatch.setattr(results, "ingest_results", lambda *a, **k: 1 / 0)  # would fail if it ran
    with Session(db) as s:
        out = results.results_for_update(s, Settings(_env_file=None), [])
    assert out["status"].startswith("übersprungen")


def test_the_real_floorball_seeds_load(db, tmp_path):
    """seeds/floorball as committed (research files): every roster loads, no errors, sources on every fact."""
    seeds = Path(__file__).parent.parent / "seeds" / "floorball"
    path = tmp_path / "projects.yaml"
    path.write_text(PROJECTS.format(seeds=seeds), encoding="utf-8")
    old = os.environ.get("PROJECTS_FILE")
    os.environ["PROJECTS_FILE"] = str(path)
    try:
        with projects.use("unihockey"), Session(db) as s:
            stats = floorball.load(s, seeds)
            assert stats.teams == 16 and stats.rosters >= 15 and stats.games >= 24
            assert stats.per_team["sui-men"].startswith("20 Spieler")
            assert not [x for x in stats.skipped if "unknown team" in x or "no team" in x]
            assert s.scalar(select(m.Fact.id).where(m.Fact.source_url.is_(None))) is None
            ev = s.scalar(select(m.Event).where(m.Event.key == "wfc-2026"))
            r = tournament.answer(s, ev, "Wie ist die WM-Bilanz der Schweiz gegen Schweden?")
            assert r["themen"] == ["bilanz"] and "Schweden" in tournament.render(r)
            s.rollback()
    finally:
        os.environ["PROJECTS_FILE"] = old or ""
