"""MCP tool output for LLM clients (gap loop 2026-10-17): one compact text, no JSON copy, capped lists with a hint,
list_events with tournaments as containers, and tournament questions routed to the tournament sheet."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import tournament
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import present
from klugscheisser.seed.loader import load_all_seeds


def _item(i: int, **kw) -> dict:
    return {
        "zeile_prefix": "📰 (Medienbericht)",
        "quelle_link": f"[SRF, 0{i % 9 + 1}.10.](https://srf.ch/{i})",
        "url": f"https://srf.ch/{i}",
        "titel": f"Titel Nummer {i} über die Nati",
        "text": f"Titel Nummer {i} über die Nati. Danach folgt ein längerer Text mit Details zum Spiel {i} "
        "und zu den Torschützinnen, der als Auszug gezeigt wird.",
        "athletinnen": ["Lara Stalder", "Alina Müller"],
        "kategorie": "Resultat",
        **kw,
    }


# --- one representation -------------------------------------------------------------------------------------


def test_render_has_no_json_copy_and_caps_lists_with_a_hint():
    result = {
        "datenstand": "Instagram: heute 09:00",
        "quelle": present.QUELLE,
        "hinweis": present.HINWEIS,
        "ziel": "Schweiz",
        "seit": "04.10.2026",
        "anzahl": 20,
        "eintraege": [_item(i) for i in range(20)],
    }
    text = present.render_markdown(result)
    assert "```json" not in text and present.HINWEIS not in text
    assert text.startswith("**Datenstand:** Instagram: heute 09:00")
    assert text.count("](https://srf.ch/") == present.SECTION_MAX
    assert "… und 8 weitere (mehr: whats_new" in text
    assert "Danach folgt ein längerer Text" in text  # excerpt for the list the tool is about
    assert "über Lara Stalder, Alina Müller" in text


def test_render_keeps_every_field_somewhere():
    """A key without its own rendering still reaches the client (no silent loss after dropping the JSON)."""
    text = present.render_markdown({"datenstand": "x", "neues_feld": {"a": 1, "b": "zwei"}})
    assert "neues_feld: a: 1; b: zwei" in text


def test_render_roster_handles_and_ambiguity():
    roster = {
        "datenstand": "x",
        "team": "Schweiz",
        "liga": None,
        "kader_quelle": {
            "anlass": "WM 2026",
            "quelle": "SIHF",
            "url": "https://sihf.ch/k",
            "status": "provisional",
        },
        "spielerinnen": [
            {
                "name": "Lara Stalder",
                "id": 1,
                "nation": "SUI",
                "position": "Stürmerin",
                "instagram": [
                    {"handle": "@lara", "url": "https://www.instagram.com/lara/", "status": "verifiziert"}
                ],
            }
        ],
    }
    text = present.render_markdown(roster)
    assert "Kader WM 2026, provisorisch [SIHF](https://sihf.ch/k)" in text
    assert "- Lara Stalder (Stürmerin, SUI) [@lara](https://www.instagram.com/lara/)" in text
    handles = present.render_markdown(
        {
            "datenstand": "x",
            "athletinnen": [{"name": "A B", "handles": "Kein bestätigter Instagram-Account bekannt."}],
        }
    )
    assert "- A B: Kein bestätigter Instagram-Account bekannt." in handles
    amb = present.render_markdown(
        {
            "datenstand": "x",
            "mehrdeutig": True,
            "frage": "Mehrere Treffer für 'Müller'.",
            "kandidatinnen": [{"id": 1, "name": "Alina Müller", "geburtsdatum": "1998-03-12"}],
        }
    )
    assert "- Alina Müller (geb. 1998-03-12)" in amb


def test_render_answer_shows_follow_up_question_refresh_and_computed_basis():
    st = {
        "zeile_prefix": "🧮 (berechnet)",
        "aussage": "Die Älteste ist A.",
        "zitat": "A, geboren 1990",
        "quelle_link": "[Berechnet](https://x.ch/1)",
        "url": "https://x.ch/1",
        "berechnet": True,
        "basis": [{"quelle": "EP", "url": "https://x.ch/1"}, {"quelle": "Wikidata", "url": "https://x.ch/2"}],
    }
    text = present.render_answer(
        {
            "datenstand": "x",
            "statements": [st],
            "aktualisierung": "Aktualisierung läuft im Hintergrund.",
            "rueckfrage": "Welches Spiel?",
            "rueckfrage_vorschlaege": ["Schweiz – Kanada"],
        }
    )
    assert "_Aktualisierung läuft im Hintergrund._" in text
    assert "**Rückfrage:** Welches Spiel?" in text and "- Schweiz – Kanada" in text
    assert "Berechnet aus: [Wikidata](https://x.ch/2)" in text
    assert "```json" not in text


def test_tools_send_text_only_without_structured_copy(db):
    import anyio

    from klugscheisser.mcp_server import server

    res = server.get_event("Tour de France")
    assert res.structured_content is None
    assert "Fehler: Kein Anlass gefunden" in res.content[0].text
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    for name in ("get_event", "get_athlete", "search", "whats_new", "list_events", "frage", "stats"):
        assert tools[name].output_schema is None, name  # no schema → clients do not expect structured content
    assert {"days", "team"} <= set(tools["list_events"].input_schema["properties"])


# --- list_events --------------------------------------------------------------------------------------------


@pytest.fixture
def events(db):
    now = datetime.now(UTC)
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        teams = {}
        for key, name, kind, country, league in [
            ("sui-women", "Schweiz", "national", "SUI", None),
            ("can-women", "Kanada", "national", "CAN", None),
            ("aut-women", "Österreich", "national", "AUT", None),
            ("evz", "EV Zug", "club", "SUI", "PFWL"),
            ("zsc", "ZSC Lions", "club", "SUI", "PFWL"),
        ]:
            teams[key] = m.Team(
                sport_id=sport.id, key=key, name=name, kind=kind, country=country, league=league
            )
            s.add(teams[key])
        comp = m.Competition(sport_id=sport.id, key="cup-x", name="Test Cup", kind="tournament")
        s.add(comp)
        s.flush()
        s.add(
            m.Event(
                key="cup-x",
                name="Test Cup",
                kind="tournament",
                competition_id=comp.id,
                starts_at=now + timedelta(days=10),
                ends_at=now + timedelta(days=14),
                city="Herning",
            )
        )

        def game(key, home, away, days, **kw):
            s.add(
                m.Event(
                    key=key,
                    name=f"{teams[home].name} – {teams[away].name}",
                    kind="game",
                    home_team_id=teams[home].id,
                    away_team_id=teams[away].id,
                    starts_at=now + timedelta(days=days),
                    **kw,
                )
            )

        for i in range(6):  # tournament games: only counted in the container
            game(f"cup-g{i}", "sui-women", "can-women", 10 + i % 4, competition_id=comp.id)
        game("old-friendly", "sui-women", "aut-women", -200)  # no end date, long ago: not listed any more
        game("last-friendly", "sui-women", "aut-women", -3, status="final", score="3:1")
        game("next-friendly", "can-women", "sui-women", 5)
        game("far-friendly", "can-women", "aut-women", 60)
        for i in range(5):
            game(f"club-{i}", "evz", "zsc", 1 + i)
        s.commit()
    return db


def test_list_events_tournaments_as_containers_games_in_a_window(events):
    from klugscheisser.mcp_server import tools

    with Session(events) as s:
        out = tools.list_events(s)
        assert [e["key"] for e in out["anlaesse"]] == ["cup-x"]
        cup = out["anlaesse"][0]
        assert cup["spiele"].startswith("6 Spiele; nächstes: ") and "Schweiz – Kanada" in cup["spiele"]
        assert [g["key"] for g in out["spiele_letzte"]] == ["last-friendly"]
        assert out["spiele_letzte"][0]["resultat"] == "3:1"
        assert [g["key"] for g in out["spiele_naechste"]] == ["next-friendly"]
        assert out["klubspiele"] == [
            {
                "liga": "PFWL",
                "anzahl": 5,
                "von": out["klubspiele"][0]["von"],
                "bis": out["klubspiele"][0]["bis"],
            }
        ]
        text = present.render_markdown(out)
        assert "old-friendly" not in text and "cup-g1" not in text and "club-1" not in text
        assert "(`cup-x`)" in text and "PFWL: 5 Spiele" in text and "list_events(team=" in text
        assert len(text) < 3000
        zug = tools.list_events(s, team="EV Zug", days=30)
        assert [g["key"] for g in zug["spiele_naechste"]] == [f"club-{i}" for i in range(5)]
        assert zug["anlaesse"] == [] and zug["klubspiele"] == []
        longer = tools.list_events(s, days=90)
        assert "far-friendly" in [g["key"] for g in longer["spiele_naechste"]]


# --- tournament questions -------------------------------------------------------------------------------------


@pytest.fixture
def wch(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        return s.scalar(select(m.Event.id).where(m.Event.key == "wch-2026"))


def test_nations_are_teams_not_places_or_articles():
    assert tournament.nations_in("Wie viele Punkte braucht man für den Viertelfinal?") == []
    assert tournament.nations_in("Can Switzerland win?") == ["SUI"]
    assert tournament.nations_in("Frauen-WM in Dänemark: Gegnerinnen der Schweiz? WM 2025 in Tschechien") == [
        "SUI"
    ]
    assert tournament.nations_in("Wann spielt die Schweiz gegen Dänemark?") == ["SUI", "DEN"]
    assert tournament.nations_in("SUI vs CAN") == ["SUI", "CAN"]


def test_days_in_question():
    d1, d2 = date(2026, 11, 6), date(2026, 11, 16)
    assert tournament.days_in("Wer spielt am ersten Tag?", d1, d2) == [d1]
    assert tournament.days_in("Spiele am 13.11.?", d1, d2) == [date(2026, 11, 13)]
    assert tournament.days_in("Was läuft am 8. November?", d1, d2) == [date(2026, 11, 8)]
    assert tournament.days_in("Who plays on November 9?", d1, d2) == [date(2026, 11, 9)]
    assert tournament.days_in("Was läuft am Samstag?", d1, d2) == [date(2026, 11, 7), date(2026, 11, 14)]
    assert tournament.days_in("Wer spielt morgen?", d1, d2, today=date(2026, 11, 7)) == [date(2026, 11, 8)]
    assert tournament.days_in("Wie funktioniert der Modus?", d1, d2) == []
    assert tournament.days_in("Termine 2024 im April", d1, d2) == []


@pytest.mark.parametrize(
    "question",
    [
        "Wie viele Punkte braucht die Schweiz an der WM für den Viertelfinal?",
        "Zählt bei Punktgleichheit an der WM die Tordifferenz?",
        "Gegen wen spielt die Schweiz im Viertelfinal?",  # no "WM" in it: format words name it
        "Why is the women's WCh in November this year?",
        "When does Switzerland play Canada at the Worlds?",
        "Welche Teams haben ihren WM-Kader schon bekanntgegeben?",
    ],
)
def test_tournament_type_questions_go_to_the_sheet(db, wch, question):
    with Session(db) as s:
        ev = tournament.event_for_question(s, question)
        assert ev is not None and ev.key == "wch-2026"
        res = tournament.answer(s, ev, question)
        assert res["abschnitte"] and all(sec["zeilen"] for sec in res["abschnitte"])


@pytest.mark.parametrize(
    "question",
    [
        "Wer hat an der WM 2025 die meisten Tore für die Schweiz geschossen?",
        "Wer spielt in den PWHL-Playoffs im Viertelfinal?",
        "Olympia-Turnier 2026: Resultate der Schweiz",
        "Wie hat die Schweiz an der Euro Hockey Tour in Lahti gespielt?",
    ],
)
def test_player_and_other_competition_questions_stay_with_the_engine(db, wch, question):
    with Session(db) as s:
        assert tournament.event_for_question(s, question) is None


def test_who_plays_on_the_first_day_gets_that_day_only(db, wch):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        res = tournament.answer(s, ev, "Wer spielt wann am ersten Tag der WM?")
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert len(lines) == 4 and all("06.11." in x for x in lines)
        rest = tournament.answer(s, ev, "Welche WM-Spiele sind am 14.11.?")
        assert "spielfreier Tag" in rest["abschnitte"][0]["zeilen"][-1]["text"]


def test_current_and_past_wm_question_keeps_all_topics(db, wch):
    """p01 (Perplexity, 2026-10-16): the current WM plus a comparison with 2025 was answered with history only."""
    q = (
        "Frauen-WM in Dänemark: Welches Jahr, Austragungsdaten, Gruppen und Gegnerinnen der Schweiz, genauer "
        "Spielmodus inklusive Viertelfinalpaarungen, Halbfinal-Setzung und Abstiegsregelung? Vergleich zur WM 2025 "
        "in Tschechien: Was ändert?"
    )
    with Session(db) as s:
        res = tournament.answer(s, s.get(m.Event, wch), q)
        themes = [sec["thema"] for sec in res["abschnitte"]]
        assert {"modus", "gruppen", "abstieg"} <= set(themes) and themes[-1] == "geschichte"
        past = tournament.answer(s, s.get(m.Event, wch), "Wo fand die Frauen-WM 2024 statt?")
        assert [sec["thema"] for sec in past["abschnitte"]] == ["geschichte"]


def test_teams_in_different_groups_get_a_clear_line(db, wch):
    with Session(db) as s:
        res = tournament.answer(s, s.get(m.Event, wch), "Wann spielt die Schweiz an der WM gegen Kanada?")
        text = " ".join(z["text"] for sec in res["abschnitte"] for z in sec["zeilen"])
        assert "Kein Vorrundenspiel Schweiz – Kanada" in text and "Gruppe B" in text
