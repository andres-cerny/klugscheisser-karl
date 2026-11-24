"""2026-11-24 (after the Unihockey go-live): answers that went wrong, fixed generally (any project/sport).

- "Welche Deutschen spielen in der Schweiz?" / "Finnen in der Schweiz": nation X at clubs of country Y, from
  the stored clubs (no ask-back); the club country from the league, the team or the roster's country
- head-to-head at WMs: only the asked opponent; German stage names
- funfacts.md back-references ("(same article)"), research notes are no fun facts, seeding is idempotent
- deep: the translation step counts against the run's cap
- schedule of one team: its own knock-out path; "Wer pfeift?" → referees
"""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import tournament
from klugscheisser.answer import club_country, cross
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed import floorball
from klugscheisser.seed.floorball_info import normalize_roster, note_fun_fact, stage_de, stages_de

# --- nation X at clubs of country Y --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q, place, nations, abroad, past",
    [
        ("Welche Deutschen spielen in der Schweiz?", "SUI", ["GER"], False, False),
        ("Welche Schweizer spielen in Schweden?", "SWE", ["SUI"], False, False),
        ("Finnen in der Schweiz", "SUI", ["FIN"], False, False),
        ("Welche Japaner spielen bei Schweizer Klubs?", "SUI", ["JPN"], False, False),
        ("Which Swedes play in Finland?", "FIN", ["SWE"], False, False),
        ("Welche Schweizerinnen spielen im Ausland?", None, ["SUI"], True, False),
        ("Welche Schweizerinnen haben schon in Schweden gespielt?", "SWE", ["SUI"], False, True),
        ("Welche Esten spielen in der finnischen Liga?", "FIN", ["EST"], False, False),
        ("Wer spielt in Tschechien?", "CZE", [], False, False),
    ],
)
def test_parse_nation_and_place(q, place, nations, abroad, past):
    ref = club_country.parse(q)
    assert ref is not None
    assert (ref.place, ref.nations, ref.abroad, ref.past) == (place, nations, abroad, past)


@pytest.mark.parametrize(
    "q",
    [
        "Wann spielt die Schweiz in Finnland?",
        "Schweiz gegen Schweden in Tampere",
        "Wie viele Tore hat er in der Schweiz geschossen?",
        "Wie war das Wetter in der Schweiz?",
        "Welche Schweizerinnen spielen in der PWHL?",  # a league: the league answer (cross.league_ref)
    ],
)
def test_parse_leaves_other_questions_alone(q):
    assert club_country.parse(q) is None or cross.league_ref(q) is not None


@pytest.mark.parametrize(
    "league, team, ioc",
    [
        ("L-UPL (SUI)", None, "SUI"),
        ("Unihockey Prime League (L-UPL)", None, "SUI"),
        ("SSL", None, "SWE"),
        ("F-liiga", None, "FIN"),
        ("Sveitsin liiga", None, "SUI"),  # Finnish Wikipedia: "Swiss league"
        ("1. Floorball-Bundesliga", None, "GER"),
        ("Czech league (level unverified)", None, "CZE"),
        ("SDHL (W)", None, "SWE"),
        ("SWHL A (W)", None, "SUI"),
        ("NCAA (W)", None, "USA"),
        ("PWHL (W)", "Toronto Sceptres", "CAN"),
        ("PWHL (W)", "Boston Fleet", "USA"),
        ("Superliga", None, None),  # several countries: the team decides
    ],
)
def test_league_country(league, team, ioc):
    assert club_country.league_country(league, team) == ioc


@pytest.fixture
def clubs(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "floorball_men", "Unihockey")
        teams = {}
        for name, country, league in (
            ("UHC Uster", "SUI", "L-UPL"),
            ("MFBC Leipzig", "SUI", None),  # stored wrongly as Swiss by the old loader
            ("IBF Falun", "SWE", "SSL"),
            ("Floorball Thurgau", "SUI", None),
        ):
            teams[name] = m.Team(
                sport_id=sport.id,
                key=name.lower().replace(" ", "-"),
                name=name,
                kind="club",
                country=country,
                league=league,
            )
            s.add(teams[name])
        s.flush()
        people = {}
        for name, nat in (
            ("Adam Testlundgren", "GER"),
            ("Svenson Testhoppe", "GER"),
            ("Jan Testbürki", "SUI"),
            ("Noël Testseiler", "SUI"),
            ("Elia Testseiler", "SUI"),
            ("Ohne Klubangabe", "GER"),
        ):
            people[name] = repo.upsert_athlete(s, sport, name, nationality=nat)
        s.flush()

        def aff(who, team, season="2026-27"):
            s.add(
                m.Affiliation(
                    athlete_id=people[who].id,
                    team_id=teams[team].id,
                    season=season,
                    source="WM-Kader",
                    source_url="https://example.org/kader",
                )
            )

        aff("Adam Testlundgren", "UHC Uster")
        aff("Svenson Testhoppe", "MFBC Leipzig")
        aff("Noël Testseiler", "IBF Falun")
        aff("Elia Testseiler", "Floorball Thurgau")
        # Jan: only an old Wikipedia infobox (Swiss club 2019) and a newer current-team fact (Sweden)
        s.add(
            m.Fact(
                athlete_id=people["Jan Testbürki"].id,
                kind="current_team",
                value={"text": "Kalmarsund (ab kommender Saison)", "league": "SSL", "season": "2025-12-15"},
                value_text="Kalmarsund",
                source="web",
                source_url="https://example.org/buerki",
                confidence=1.0,
                status="confirmed",
            )
        )
        s.add(
            m.CareerStint(
                athlete_id=people["Jan Testbürki"].id,
                team_name="SV Wiler-Ersigen",
                kind="club",
                league="NLA",
                season_from=2015,
                season_to=2019,
                role="player",
                source="wikipedia",
                source_url="https://de.wikipedia.org/wiki/Jan",
                status="confirmed",
                confidence=1.0,
            )
        )
        s.commit()
    return db


def _direct(pi):
    assert pi is not None and pi.docs[0].computed
    return pi.docs[0].extra["direct"]


def test_nation_at_clubs_of_a_country_is_answered_without_ask_back(clubs):
    with Session(clubs) as s:
        q = "Welche Deutschen spielen in der Schweiz?"
        assert cross.ask_back(s, q, [], None) is None
        d = _direct(cross.prosa_inputs(s, q, [], None))
        names = [r[0] for r in d["rows"]]
        # the league beats the wrong stored country (MFBC Leipzig has none: its team says SUI, so it counts until
        # the club countries are repaired, see test_repair_club_countries_from_the_rosters)
        assert "Adam Testlundgren" in names
        assert "UHC Uster (L-UPL, 2026/27)" in d["rows"][0][1] or any("UHC Uster" in r[1] for r in d["rows"])
        assert "Ohne Klubangabe" in (d["unknown"] or "")
        assert d["summary"].startswith(f"{len(names)} ") and "in der Schweiz" in d["summary"]


def test_newest_club_counts_and_former_ones_only_for_the_past(clubs):
    with Session(clubs) as s:
        now = _direct(cross.prosa_inputs(s, "Welche Schweizer spielen in Schweden?", [], None))
        assert sorted(r[0] for r in now["rows"]) == ["Jan Testbürki", "Noël Testseiler"]
        home = _direct(cross.prosa_inputs(s, "Welche Schweizer spielen in der Schweiz?", [], None))
        assert [r[0] for r in home["rows"]] == ["Elia Testseiler"]  # Jan's 2015-2019 club is no current club
        past = _direct(
            cross.prosa_inputs(s, "Welche Schweizer haben schon in der Schweiz gespielt?", [], None)
        )
        assert "Jan Testbürki" in [r[0] for r in past["rows"]]
        abroad = _direct(cross.prosa_inputs(s, "Welche Schweizer spielen im Ausland?", [], None))
        assert sorted(r[0] for r in abroad["rows"]) == ["Jan Testbürki", "Noël Testseiler"]


def test_research_roster_gives_every_club_its_country():
    raw = {
        "code": "GER",
        "team": "Germany",
        "announced": {"date": "2026-11-03", "source_url": "https://example.org/ger"},
        "players": [
            {"name": "A", "club": "MFBC Leipzig", "league": "1. Floorball-Bundesliga (GER)"},
            {"name": "B", "club": "UHC Uster", "league": "L-UPL (SUI)"},
            {"name": "C", "club": "Berlin Rockets"},
            {"name": "D", "club": "Slevik IBK", "country_of_club": "Norway"},
        ],
    }
    players = {p["name"]: p for p in normalize_roster(raw, "wfc-2026")["players"]}
    assert (players["A"]["country"], players["A"]["country_guess"]) == ("GER", False)
    assert players["B"]["country"] == "SUI"
    assert (players["C"]["country"], players["C"]["country_guess"]) == ("GER", True)
    assert players["D"]["country"] == "NOR"


def test_repair_club_countries_from_the_rosters(clubs, tmp_path):
    (tmp_path / "rosters").mkdir()
    (tmp_path / "rosters" / "GER.yaml").write_text(
        "code: GER\nteam: Germany\nannounced: {date: 2026-11-03, source_url: 'https://example.org/ger'}\n"
        "players:\n  - {name: Svenson Testhoppe, club: MFBC Leipzig, league: '1. Floorball-Bundesliga (GER)'}\n",
        encoding="utf-8",
    )
    with Session(clubs) as s:
        assert floorball.repair_club_countries(s, tmp_path, "wfc-2026", apply=False) == [
            ("MFBC Leipzig", "SUI", "GER")
        ]
        floorball.repair_club_countries(s, tmp_path, "wfc-2026")
        assert floorball.repair_club_countries(s, tmp_path, "wfc-2026") == []  # idempotent
        d = _direct(cross.prosa_inputs(s, "Welche Deutschen spielen in der Schweiz?", [], None))
        assert [r[0] for r in d["rows"]] == ["Adam Testlundgren"]
        s.rollback()


# --- head-to-head and stage names ---------------------------------------------------------------------------


def test_stage_names_in_german():
    assert stage_de("Semi-final") == "Halbfinal"
    assert stage_de("Quarterfinal (A1 vs W35)") == "Viertelfinal"
    assert stage_de("5th-8th place semi-final") == "Platzierungsspiel 5–8"
    assert stage_de("Bronze medal match") == "Spiel um Bronze"
    assert stage_de("Group A") == "Gruppe A"
    assert stage_de("Gold medal game (final)") == "Final"
    assert stages_de("1998 Final: SUI 3–10 SWE; 2000 Semi-final: SUI 2–8 SWE; 1996 Group A: SUI 0–9 SWE") == (
        "1998 Final: SUI 3–10 SWE; 2000 Halbfinal: SUI 2–8 SWE; 1996 Gruppe A: SUI 0–9 SWE"
    )


INFO = {
    "sources": {"w": {"name": "Wikipedia", "url": "https://en.wikipedia.org/wiki/WFC", "kind": "web"}},
    "default_source": "w",
    "facts": [
        {
            "topic": ["bilanz"],
            "text": "Schweiz gegen Schweden: 11 Spiele",
            "src": "w",
            "opponent": "SWE",
            "played": 11,
        },
        {
            "topic": ["bilanz"],
            "text": "Schweiz gegen Finnland: 12 Spiele",
            "src": "w",
            "opponent": "FIN",
            "played": 12,
        },
        {
            "topic": ["bilanz"],
            "text": "Schweiz gegen Österreich: nie",
            "src": "w",
            "opponent": "AUT",
            "played": 0,
        },
        {
            "topic": ["bilanz"],
            "text": "WM 2018, Spiele der Schweiz: …",
            "src": "w",
            "year": 2018,
            "opponents": ["SWE"],
        },
    ],
}


def test_head_to_head_shows_only_the_asked_opponent():
    one = tournament.sec_bilanz(INFO, "Bilanz Schweiz gegen Schweden an Weltmeisterschaften", ["SUI", "SWE"])
    assert [z.text for z in one.zeilen] == ["Schweiz gegen Schweden: 11 Spiele"]
    assert "gegen Schweden" in one.titel
    every = tournament.sec_bilanz(INFO, "Bilanz der Schweiz an WMs", ["SUI"])
    assert [z.text for z in every.zeilen] == [
        "Schweiz gegen Schweden: 11 Spiele",
        "Schweiz gegen Finnland: 12 Spiele",
    ]
    year = tournament.sec_bilanz(INFO, "Bilanz gegen Schweden an der WM 2018", ["SWE"])
    assert len(year.zeilen) == 2


# --- the schedule of one team and the referees --------------------------------------------------------------


def test_one_teams_path_to_the_quarterfinal_only():
    info = {
        "groups": {"A": {"teams": ["FIN", "SUI", "LAT", "NOR"]}, "B": {"teams": ["SWE"]}},
        "playoffs": [
            {
                "day": "2026-12-09",
                "time": "09:00",
                "what": "Playoff: A4 – D1 (Spiel 33)",
                "arena": "H",
                "src": "x",
                "slots": ["A4", "D1"],
                "no": 33,
                "stage": "Playoff 1",
            },
            {
                "day": "2026-12-09",
                "time": "15:00",
                "what": "Playoff: B3 – C2 (Spiel 35)",
                "arena": "H",
                "src": "x",
                "slots": ["B3", "C2"],
                "no": 35,
                "stage": "Playoff 3",
            },
            {
                "day": "2026-12-09",
                "time": "08:30",
                "what": "Platzierung: D3 – C4",
                "arena": "K",
                "src": "x",
                "slots": ["D3", "C4"],
                "no": 31,
                "stage": "13-16 placement",
            },
            {
                "day": "2026-12-10",
                "time": "18:30",
                "what": "Viertelfinal: A1 – Sieger Spiel 35",
                "arena": "N",
                "src": "x",
                "slots": ["A1", "Winner 35"],
                "no": 42,
                "stage": "Quarterfinal (A1 vs W35)",
            },
            {
                "day": "2026-12-11",
                "time": "15:00",
                "what": "Viertelfinal: B2 – Sieger Spiel 33",
                "arena": "N",
                "src": "x",
                "slots": ["B2", "Winner 33"],
                "no": 45,
                "stage": "Quarterfinal (B2 vs W33)",
            },
            {
                "day": "2026-12-12",
                "time": "16:45",
                "what": "Halbfinal",
                "arena": "N",
                "src": "x",
                "slots": ["Winner 42", "Winner 45"],
                "no": 49,
                "stage": "Semifinal 1",
            },
        ],
    }
    path = tournament.ko_path(info, "SUI")
    whats = [p["what"] for p, _ in path]
    assert "Platzierung: D3 – C4" not in whats and "Playoff: B3 – C2 (Spiel 35)" not in whats
    assert [how for _, how in path][:3] == ["Als A4: ", "Als A1: ", "Als A4 nach Sieg in Spiel 33: "]
    assert whats[-1].startswith("Halbfinals")


def test_who_referees_is_a_referee_question():
    assert "schiedsrichter" in tournament.topics_in("Wer pfeift an der WM?")
    assert "schiedsrichter" in tournament.topics_in("Who referees the final?")


# --- fun facts ------------------------------------------------------------------------------------------------


def test_research_notes_are_no_fun_facts():
    assert (
        note_fun_fact("not among the 4 named debutants; profile rows start at World Games 2025 (incomplete)")
        is None
    )
    assert note_fun_fact("spelled 'Phillip' on floorball.de, 'PHILIPP' in federation post") is None
    assert note_fun_fact("plays in Switzerland") is None
    assert note_fun_fact("same surname as Yu Nagata - relationship unknown") is None
    assert (
        note_fun_fact("oldest verified squad player (33); plays in Switzerland")
        == "oldest verified squad player (33)"
    )
    assert note_fun_fact("SWISS CLUB. Scored 1-1 v Switzerland in the historic 4-3 win") == (
        "Scored 1-1 v Switzerland in the historic 4-3 win"
    )
    assert note_fun_fact("abroad since 2020 (from 1. SC Vítkovice); second-oldest (33)") == (
        "abroad since 2020 (from 1. SC Vítkovice); second-oldest (33)"
    )


def test_funfacts_md_back_references_get_the_earlier_link():
    md = """# Facts
## History
1. **First title.** Sweden won.
   Source: unihockey.ch, 2025-09, https://unihockey.ch/news/mnner-wm-2028-in-prag-27714/
2. **Spectator record.** 181,571 in Prague.
   Source: unihockey.ch, 2025-09 (same article).
3. **Points leader.** Hofbauer, 97 points.
   Sources: IFF Men's WFC Records, https://floorball.sport/wfc-records/ (retrieved 2026-11-24)
4. **Most goals.** Nilsson, 58.
   Source: IFF Men's WFC Records.
5. **Only three remain.** Zaugg, Seiler, Bürki.
   Source: unihockey.ch, 2026-03-05 (27714)
6. **No link at all.** Something.
   Source: a blog
"""
    items, skipped = floorball.parse_funfacts_md(md)
    urls = {i["text"].split(".")[0]: i["url"] for i in items}
    assert urls["Spectator record"] == "https://unihockey.ch/news/mnner-wm-2028-in-prag-27714/"
    assert urls["Most goals"] == "https://floorball.sport/wfc-records/"
    assert urls["Only three remain"] == "https://unihockey.ch/news/mnner-wm-2028-in-prag-27714/"
    assert len(skipped) == 1 and "No link" in skipped[0]


def test_seeding_keeps_a_fun_fact_translated_in_place(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "floorball_men", "Unihockey")
        a = repo.upsert_athlete(s, sport, "Mika Testhänni", nationality="SUI")
        s.flush()
        assert floorball.add_fact(s, a, "fun_fact", "youngest in squad (19)", "https://example.org/x")
        s.flush()
        f = s.scalar(select(m.Fact).where(m.Fact.athlete_id == a.id))
        f.value = {**f.value, "text_en": f.value_text, "text": "Jüngster im Kader (19)"}
        f.value_text = "Jüngster im Kader (19)"  # what the go-live did: German in place
        s.flush()
        assert not floorball.add_fact(s, a, "fun_fact", "youngest in squad (19)", "https://example.org/x")
        assert (
            s.scalar(select(m.Fact.id).where(m.Fact.athlete_id == a.id, m.Fact.kind == "fun_fact").offset(1))
            is None
        )


# --- deep: translations inside the cap ----------------------------------------------------------------------


def test_translation_stops_when_the_budget_is_used_up(db, monkeypatch):
    from klugscheisser import translate
    from klugscheisser.config import Settings

    calls = []

    def llm(texts):
        calls.append(len(texts))
        return [f"DE {t}" for t in texts], {"input": 10, "output": 10}

    monkeypatch.setattr(translate, "BATCH", 1)
    left = iter([1.0, 0.0, 0.0])
    with Session(db) as s:
        n = translate.ensure(
            s,
            Settings(_env_file=None),
            [("Hej och välkommen", "sv"), ("En annan text", "sv"), ("Tredje", "sv")],
            llm=llm,
            budget_left=lambda: next(left),
        )
        assert n == 1 and calls == [1]
        s.rollback()


def test_current_season_turns_in_july():
    assert club_country.current_season(date(2026, 11, 24)) == 2026
    assert club_country.current_season(date(2026, 3, 1)) == 2025


def test_where_does_a_named_player_play_uses_the_newest_dated_club(clubs):
    with Session(clubs) as s:
        # Jan: an old Swiss station (2015-2019) and a dated SSL current-team fact → Kalmarsund, Sweden
        for q in ("Wo spielt Jan Testbürki?", "Bei welchem Verein spielt Jan Testbürki?"):
            d = _direct(cross.prosa_inputs(s, q, [], None))
            assert d["rows"][0][0] == "Jan Testbürki"
            assert d["rows"][0][1].startswith("Kalmarsund (SSL, Schweden")
        # a game question with a team stays with the games
        assert club_country.WHERE_PLAYS.search("Wo spielt die Schweiz gegen Lettland?") is None


def test_club_rule_knows_the_singular_verein():
    form = cross.understand("Bei welchem Verein spielt Noël Seiler?")
    assert (form.typ, form.kind) == ("fakt", "current_team")


def test_head_to_head_never_asks_which_game(db):
    with Session(db) as s:
        assert cross.ask_back(s, "Bilanz Schweiz gegen Schweden an Weltmeisterschaften", [], None) is None
