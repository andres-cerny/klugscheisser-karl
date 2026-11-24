"""German as interview language (facts/german.py, seeds/club_languages.yaml): club language map, German-speaking
club years per person, labels bestätigt / wahrscheinlich / kein Hinweis, MCP person / team / suche. No network."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import german
from klugscheisser.mcp_server import present, suche, tools

TODAY = date(2026, 10, 25)


# --- the map -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "team,league,langs",
    [
        ("EVZ Women's Team", "SWHL A (W)", ("de",)),
        ("EV Zug Women", "Swiss Women's League", ("de",)),
        ("HC Davos Ladies", "SWHL A (W)", ("de",)),
        ("ZSC Lions Frauen", "SWHL A", ("de",)),
        ("SC Bern Frauen", None, ("de",)),
        ("SC Langenthal Damen", None, ("de",)),
        ("EHC Basel", None, ("de",)),
        ("HC Ambrì-Piotta Girls", "PFWL", ("it",)),
        ("HCAP Girls", "SWHL A", ("it",)),
        ("Ladies Team Lugano", "SWHL A (W)", ("it",)),
        ("Lausanne HC Féminin", "SWHL A (W)", ("fr",)),
        ("Neuchâtel Hockey Academy Dames", "EWHL SC (W)", ("fr",)),
        ("HC Fribourg-Gottéron Ladies", "PostFinance Women’s League", ("fr", "de")),
        ("EHC Biel-Bienne Spirit U15", "U15-Elit", ("de", "fr")),
        ("EV Bozen Eagles", "EWHL", ("de", "it")),
        ("HK Budapest", "DFEL (W)", ("hu",)),  # German league, Hungarian club
        ("ECDC Memmingen", "DFEL (W)", ("de",)),
        ("Iserlohn Roosters", "DEL", ("de",)),
        ("Some Club", "Germany2 (W)", ("de",)),
        ("DEC Salzburg Eagles", "EWHL", ("de",)),
    ],
)
def test_map_language(team, league, langs):
    place = german.load_map().place(team, league)
    assert place is not None and place.languages == langs


@pytest.mark.parametrize(
    "team,league",
    [
        ("Stoney Creek Sabres", "Provincial WHL (W)"),
        ("Boston College Eagles", "NCAA"),
        ("Växjö Lakers HC", "SDHL"),
        ("Frölunda HC", "SDHL (W)"),
        ("Göteborg HC", "SDHL (W)"),
        (
            "Unknown Swiss Club",
            "SWHL A (W)",
        ),  # Swiss leagues have three language regions: club must be listed
    ],
)
def test_map_unknown_is_none(team, league):
    assert german.load_map().place(team, league) is None


def test_bilingual_is_never_plain_german():
    fr = german.load_map().place("HC Fribourg-Gottéron Ladies")
    assert not fr.german and fr.bilingual_german and "zweisprachig" in fr.language_de


def test_seasons_text():
    assert german.seasons_text([2024, 2025]) == "2024/25–2025/26"
    assert german.seasons_text([2019, 2021, 2022]) == "2019/20, 2021/22–2022/23"


@pytest.mark.parametrize(
    "q,yes",
    [
        ("Wer spricht Deutsch bei Tschechien?", True),
        ("Welche Spielerinnen anderer Nationen sprechen Deutsch?", True),
        ("Kann sie ein Interview auf Deutsch geben?", True),
        ("Who on Finland speaks German?", True),
        ("Wie hat Deutschland gegen die Schweiz gespielt?", False),
        ("Deutsche Spielerinnen in der PWHL", False),
    ],
)
def test_wants_german(q, yes):
    assert german.wants_german(q) is yes


# --- persons -----------------------------------------------------------------------------------------------------


def _world(s):
    sport = repo.get_sport(s, "icehockey_women")
    cze = m.Team(sport_id=sport.id, key="cze-women", name="Tschechien", kind="national", country="CZE")
    s.add(cze)
    s.flush()
    ev = m.Event(key="wch-2026", name="IIHF Frauen-WM 2026", kind="tournament", starts_at=None)
    s.add(ev)
    s.flush()

    def person(name, nat, role="F", **kw):
        a = repo.upsert_athlete(s, sport, name, nationality=nat, **kw)
        s.add(m.EventEntry(event_id=ev.id, athlete_id=a.id, team_id=cze.id, role=role, source="test"))
        return a

    def stat(a, season, team, league, games, source="eliteprospects"):
        s.add(
            m.SeasonStat(
                athlete_id=a.id,
                season=season,
                league=league,
                team_name=team,
                games=games,
                source=source,
                source_url=f"https://www.eliteprospects.com/player/{a.id}",
            )
        )

    def stint(a, team, league, frm, to, source="wikipedia", role="player"):
        s.add(
            m.CareerStint(
                athlete_id=a.id,
                team_name=team,
                league=league,
                season_from=frm,
                season_to=to,
                source=source,
                source_url=f"https://example.org/{source}/{a.id}",
                role=role,
            )
        )

    zug = person("Noemi Testová", "CZE")
    stat(zug, 2024, "EVZ Women's Team", "SWHL A (W)", 9)
    stat(zug, 2025, "EVZ Women's Team", "SWHL A (W)", 26)
    stint(zug, "EV Zug Women", "Swiss Women's League", 2025, None)  # open end in Wikipedia: no extra season
    davos = person("Klára Davosová", "CZE", "D")
    stat(davos, 2025, "HC Davos Ladies", "SWHL A (W)", 25)
    stat(davos, 2026, "HC Davos Ladies", "SWHL A (W)", 6)
    stat(davos, 2026, "HC Davos Ladies", "SWHL A (W)", 9, source="sihf")
    ambri = person("Michaela Ambrìová", "CZE")
    stat(ambri, 2024, "HC Ambrì-Piotta Girls", "SWHL A (W)", 26)
    lugano = person("Michelle Luganová", "CZE")
    stint(lugano, "Ladies Team Lugano", "Women’s League", 2020, 2021)  # Wikipedia "2020–2021" = one season
    budapest = person("Tereza Budapestová", "CZE", "D")
    stat(budapest, 2026, "HK Budapest", "DFEL (W)", 4)
    fribourg = person("Anna Fribourgová", "CZE")
    stat(fribourg, 2023, "HC Fribourg-Gottéron Ladies", "SWHL A (W)", 20)
    coach = repo.upsert_athlete(s, sport, "Tero Trenér", role="coach")
    s.add(m.EventEntry(event_id=ev.id, athlete_id=coach.id, team_id=cze.id, role="Head coach", source="test"))
    stint(coach, "EHC Basel", None, None, None, source="wikidata")
    swiss = repo.upsert_athlete(s, sport, "Lara Schweizer", nationality="SUI")
    stat(swiss, 2025, "EVZ Women's Team", "SWHL A (W)", 30)
    plain = person("Nora Nichts", "CZE")
    stat(plain, 2025, "Frölunda HC", "SDHL (W)", 30)
    s.commit()
    return {
        "zug": zug,
        "davos": davos,
        "ambri": ambri,
        "lugano": lugano,
        "budapest": budapest,
        "fribourg": fribourg,
        "coach": coach,
        "swiss": swiss,
        "plain": plain,
        "team": cze,
        "event": ev,
    }


def test_hints_labels_and_reasons(db):
    with Session(db) as s:
        w = _world(s)
        hs = german.hints(s, [w[k] for k in w if k not in ("team", "event")], TODAY)
        zug = hs[w["zug"].id]
        assert zug.label == german.LIKELY
        assert zug.line().startswith(
            "Deutsch: wahrscheinlich – 2 Saisons EV Zug (Zug, deutschsprachig), 2024/25–2025/26, 35 Spiele"
        )
        assert "Kein deutsches Interview" in zug.line()
        assert zug.cell() == "wahrscheinlich – 2 Saisons EV Zug (2024/25–2025/26)"
        davos = hs[w["davos"].id]
        assert "2025/26–2026/27" in davos.line() and "aktuell dort" in davos.line()
        assert "34 Spiele" in davos.line()  # 25 + best source of 2026 (SIHF 9), not 25 + 6 + 9
        for k, word in (
            ("ambri", "italienischsprachig"),
            ("lugano", "italienischsprachig"),
            ("budapest", "DFEL"),
        ):
            h = hs[w[k].id]
            assert h.label is None and word in h.line() and h.line().startswith("Deutsch: kein Hinweis")
        assert "2020/21" in hs[w["lugano"].id].line() and "2021/22" not in hs[w["lugano"].id].line()
        fr = hs[w["fribourg"].id]
        assert fr.label == german.LIKELY and "zweisprachig" in fr.line() and fr.german_seasons == 0
        coach = hs[w["coach"].id]
        assert (
            coach.label == german.LIKELY and "EHC Basel" in coach.line() and "Jahre unbekannt" in coach.line()
        )
        assert w["swiss"].id not in hs  # German-speaking nations are not labelled
        assert w["plain"].id not in hs  # no clue: nothing


def test_confirmed_needs_language_evidence(db):
    with Session(db) as s:
        w = _world(s)
        a = w["plain"]
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind="languages",
                value={"text": "Deutsch"},
                value_text="Deutsch",
                source="wikidata",
                source_url="https://www.wikidata.org/wiki/Q1",
            )
        )
        s.commit()
        h = german.hint(s, a, TODAY)
        assert h.label == german.LIKELY and "Wikidata nennt Deutsch" in h.line()
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind=german.EVIDENCE_KIND,
                value={
                    "language": "de",
                    "label": "bestaetigt",
                    "reason": "SRF-Interview auf Deutsch",
                    "date": "2026-03-12",
                },
                value_text="SRF-Interview auf Deutsch",
                source="srf",
                source_url="https://www.srf.ch/x",
                quote="«Ich freue mich»",
            )
        )
        s.commit()
        h = german.hint(s, a, TODAY)
        assert h.label == german.CONFIRMED
        assert h.line().startswith("Deutsch: bestätigt – SRF-Interview auf Deutsch (12.03.2026)")
        assert h.sources[0] == ("srf.ch", "https://www.srf.ch/x")


# --- MCP output ---------------------------------------------------------------------------------------------------


def test_team_roster_has_deutsch_column(db):
    with Session(db) as s:
        _world(s)
        text = present.render_markdown(tools.get_team_roster(s, "Tschechien"))
    zug = next(x for x in text.splitlines() if x.startswith("- Noemi Testová"))
    assert zug.endswith("· Deutsch: wahrscheinlich – 2 Saisons EV Zug (2024/25–2025/26)")
    ambri = next(x for x in text.splitlines() if x.startswith("- Michaela Ambrìová"))
    assert "Deutsch: kein Hinweis – HC Ambrì-Piotta" in ambri
    assert not any("Deutsch" in x for x in text.splitlines() if x.startswith("- Nora Nichts"))
    assert "- Headcoach: Tero Trenér – Deutsch: wahrscheinlich – EHC Basel (Jahre unbekannt)" in text
    assert "«bestätigt» nur mit deutschem Interview" in text


def test_person_has_deutsch_line(db):
    with Session(db) as s:
        w = _world(s)
        text = present.render_markdown(tools.get_athlete(s, w["davos"].full_name))
    line = next(x for x in text.splitlines() if x.startswith("- Deutsch:"))
    assert "wahrscheinlich – 2 Saisons HC Davos" in line and "[Elite Prospects]" in line


def test_switch_off(db, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: Settings(german_hints=False))
    with Session(db) as s:
        w = _world(s)
        assert "Deutsch" not in present.render_markdown(tools.get_team_roster(s, "Tschechien"))
        assert "Deutsch:" not in present.render_markdown(tools.get_athlete(s, w["zug"].full_name))


def test_suche_material_answers_who_speaks_german(db):
    with Session(db) as s:
        _world(s)
        text = suche.german_text(s, "Wer spricht Deutsch bei Tschechien?", None, None)
        assert text is not None
        assert "Noemi Testová: wahrscheinlich – 2 Saisons EV Zug" in text
        assert "Tero Trenér (Headcoach): wahrscheinlich" in text
        assert "nicht deutschsprachig: " in text and "Michaela Ambrìová: HC Ambrì-Piotta" in text
        assert "Lara Schweizer" not in text
        assert suche.german_text(s, "Wie spielte Tschechien gegen Finnland?", None, None) is None
        lines, _ = suche.material(
            s, "Welche Spielerinnen anderer Nationen sprechen Deutsch?", None, None, None
        )
        assert any("**Tschechien**" in x for x in lines)
