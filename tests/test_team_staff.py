"""v19: team staff (coach, assistants, GM, physio …) with history: seeds/staff + roster staff blocks → team_staff,
answers for coach/staff questions (MCP frage, get_team_roster, get_event, web chat lookup). No network."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import staff
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds, load_roster_data

TODAY = date(2026, 10, 19)


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _team(s, key="sui-women"):
    return s.scalar(select(m.Team).where(m.Team.key == key))


@pytest.mark.parametrize(
    "label,key",
    [
        ("Head Coach", "head_coach"),
        ("Head coach", "head_coach"),
        ("Assistant Coach", "assistant_coach"),
        ("Goalkeeping coach", "goalie_coach"),
        ("Goaltending Coach", "goalie_coach"),
        ("Video Coach", "video_coach"),
        ("Strength and conditioning coac", "performance_coach"),
        ("Athletic coach", "performance_coach"),
        ("General Manager", "gm"),
        ("GM Womens Hockey", "gm"),
        ("Head of Women's Hockey", "gm"),
        ("Sr. Director, Women's National", "gm"),
        ("Team leader", "team_manager"),
        ("Doktorin", "doctor"),
        ("Team doctor", "doctor"),
        ("Physiotherapeutin", "physio"),
        ("Equipement Manager", "equipment"),
        ("Media Manager / Digital Content Producer", "media"),
        ("Nutritionist", "nutrition"),
        ("Trainer", "athletic_trainer"),
        ("Senior Advisor", "other"),
    ],
)
def test_role_labels_are_normalised(label, key):
    assert staff.role_of(label) == key


def test_swiss_staff_current_and_history(seeded):
    with Session(seeded) as s:
        apps = staff.appointments(s, _team(s), TODAY)
        cur = {(a.key, a.name) for a in apps if a.current}
        assert ("head_coach", "Shannon Miller") in cur
        assert {("assistant_coach", "Saara Kivenmäki"), ("assistant_coach", "Justin Krueger")} <= cur
        assert {
            ("goalie_coach", "Simon Theiler"),
            ("gm", "Melanie Häfliger"),
            ("team_manager", "Sidney Piaget"),
        } <= cur
        assert ("doctor", "Henrik Ulke") in cur  # only on the October roster PDF: evidence from the roster
        miller = next(a for a in apps if a.name == "Shannon Miller" and a.key == "head_coach")
        assert (
            miller.row.since == "2026-08"
            and "Anfang August" in miller.row.quote
            and miller.row.origin == "seed"
        )
        assert miller.row.last_seen == date(2026, 11, 1)  # official WM list (SIHF 01.11.)
        # the Olympics roster (Wikipedia) named Colin Muller: history now, closed by the seed, with successor
        muller = [a for a in apps if a.name == "Colin Muller"]
        assert len(muller) == 1 and not muller[0].current
        assert muller[0].row.until == "2026-04-25" and muller[0].successor == "Shannon Miller"
        assert muller[0].row.last_seen >= date(2026, 2, 1)  # Olympics evidence went into the closed row
        assert s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Saara Kivenmäki")).role == "coach"


def test_wch_roster_keeps_the_staff_of_the_last_roster_pdf(seeded):
    with Session(seeded) as s:
        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        crew = staff.event_staff(s, ev, _team(s))
        names = {e.athlete.full_name for e in crew}
        assert {"Shannon Miller", "Justin Krueger", "Henrik Ulke", "Melanie Häfliger"} <= names
        # 01.11.2026: the WM article names only the head coach; the rest comes from the October roster PDF
        miller = next(e for e in crew if e.athlete.full_name == "Shannon Miller")
        assert miller.list_type == "confirmed" and "frauen-wm-2026" in miller.source_url
        rest = [e for e in crew if e is not miller]
        assert rest and all(
            "wnt-roster-oktober-2026.pdf" in e.source_url and "Södertälje" in e.source for e in rest
        )
        players = [
            e
            for e in s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id))
            if e.team_id == _team(s).id
        ]
        assert len([e for e in players if not staff.is_staff_entry(e.role)]) == 25


def test_staff_question_answers_team_level(seeded):
    with Session(seeded) as s:
        res = staff.answer(s, "Who is the Swiss coach and staff at the WCh?", today=TODAY)
        assert res["team"] == "Schweiz" and res["anlass"]
        titles = [sec["titel"] for sec in res["abschnitte"]]
        assert titles[0].startswith("Staff Schweiz") and any("Aufgebot" in t for t in titles)
        lines = [z for sec in res["abschnitte"] for z in sec["zeilen"]]
        head = next(z for z in lines if z["text"].startswith("Headcoach: Shannon Miller"))
        assert "seit August 2026" in head["text"] and "sihf.ch" not in head["url"] and head["zitat"]
        assert any("Justin Krueger" in z["text"] for z in lines)
        aufgebot = next(sec for sec in res["abschnitte"] if "Aufgebot" in sec["titel"])
        assert "offiziell" in aufgebot["zeilen"][0]["text"]  # official WM list (SIHF 01.11.2026)
        assert all(z["url"].startswith("https://") for z in lines if z["quelle"] != "Karl")
        text = staff.render(res, "x")
        assert "✅ (offiziell)" in text and "Colin Muller" in text  # head-coach change shown as history


def test_coach_questions_by_role_team_and_person(seeded):
    with Session(seeded) as s:
        res = staff.answer(s, "Wer ist der Trainer von Schweden?", today=TODAY)
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert res["team"] == "Schweden" and any("Erika Holst" in t for t in lines)
        cur = [z["text"] for z in res["abschnitte"][0]["zeilen"]]
        assert not any(
            "Physio" in t for t in cur
        )  # "Trainer": coaches only (others named as "nicht gefragt")
        res = staff.answer(s, "Wer ist Goalie-Trainer der Schweiz?", today=TODAY)
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert lines[0].startswith("Headcoach: Shannon Miller")  # head coach always as context
        assert lines[1].startswith("Goalie-Trainer:in: Simon Theiler (belegt ")
        assert "nicht gefragt" in lines[2] and "Physiotherapeut" in lines[2]
        assert not any("25.04.2026" in x for x in lines)  # no head-coach history for a goalie question
        res = staff.answer(s, "Wer ist der Headcoach der Schweiz?", today=TODAY)
        cur = res["abschnitte"][0]["zeilen"]
        assert [z["text"].split(":")[0] for z in cur] == ["Headcoach"]
        res = staff.answer(s, "Seit wann ist Shannon Miller Trainerin?", today=TODAY)
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert any("Shannon Miller (seit August 2026" in t for t in lines)
        assert any("Colin Muller (2019–25.04.2026" in t and "Nachfolge: Shannon Miller" in t for t in lines)
        assert not any("Krueger" in t for t in lines)
        res = staff.answer(s, "Wer ist im Staff von Deutschland?", today=TODAY)
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert any("Jeff MacLeod" in t for t in lines) and any("Physiotherapeut" in t for t in lines)


@pytest.mark.parametrize(
    "question",
    [
        "Wo trainiert Alina Müller?",
        "Was hat Shannon Miller im Interview gesagt?",
        "Wer ist die Trainerin von Lara Stalder im Verein?",
        "Wann ist das Trainingslager?",
        "Wer schoss die meisten Tore?",
    ],
)
def test_no_staff_answer_for_other_questions(seeded, question):
    with Session(seeded) as s:
        assert staff.answer(s, question, today=TODAY) is None


def test_record_keeps_history_and_supersedes_single_roles(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        team = m.Team(sport_id=sport.id, key="fin-women", name="Finnland", kind="national", country="FIN")
        s.add(team)
        s.flush()
        a = repo.upsert_athlete(s, sport, "Coach Alt", role="coach")
        b = repo.upsert_athlete(s, sport, "Coach Neu", role="coach")
        physio = repo.upsert_athlete(s, sport, "Physio Früher", role="staff")
        staff.record(
            s, team, a, "Head coach", source="Olympia", source_url="https://x/1", seen=date(2026, 2, 1)
        )
        staff.record(
            s,
            team,
            physio,
            "Physiotherapist",
            source="Olympia",
            source_url="https://x/1",
            seen=date(2026, 2, 1),
        )
        staff.record(s, team, b, "Head Coach", source="EHT", source_url="https://x/2", seen=date(2026, 10, 1))
        staff.record(
            s, team, a, "Head coach", source="Olympia", source_url="https://x/1", seen=date(2026, 2, 1)
        )
        assert len(list(s.scalars(select(m.TeamStaff).where(m.TeamStaff.athlete_id == a.id)))) == 1
        apps = {x.name: x for x in staff.appointments(s, team, TODAY)}
        assert apps["Coach Neu"].current and not apps["Coach Alt"].current
        assert apps["Coach Alt"].successor == "Coach Neu" and "abgelöst" in apps["Coach Alt"].reason
        assert not apps["Physio Früher"].current and apps["Physio Früher"].reason.startswith("zuletzt belegt")
        # a seed closes the old appointment; old roster evidence stays inside it, a later one opens a new row
        staff.record(
            s, team, a, "Head coach", since="2020", until="2026-03", seen=date(2026, 3, 1), origin="seed"
        )
        staff.record(s, team, a, "Head coach", source="Olympia", seen=date(2026, 2, 1))
        assert len(list(s.scalars(select(m.TeamStaff).where(m.TeamStaff.athlete_id == a.id)))) == 1
        staff.record(s, team, a, "Assistant coach", source="WM", seen=date(2026, 10, 10))
        assert staff.compact(s, team, TODAY) == "Headcoach: Coach Neu; Assistenztrainer:in: Coach Alt"


def test_mcp_frage_routes_staff_questions_without_engine(seeded, monkeypatch):
    from klugscheisser.answer import engine
    from klugscheisser.mcp_server import server

    monkeypatch.setattr(engine, "build_answer_engine", lambda *a: pytest.fail("engine must not be used"))
    out = server.frage("Wer ist der Assistenztrainer der Nati an der WM?")
    text = out.content[0].text
    assert "Datenstand" in text and "Justin Krueger" in text and "Saara Kivenmäki" in text
    assert "Headcoach: Shannon Miller" in text  # context for the asked role
    assert "Physiotherapeut" not in text.split("Nicht gefragt")[0]  # only the asked roles listed
    assert "nicht gefragt" in text and "nicht daraus schliessen" in text


def test_staff_under_the_head_coach_keeps_the_head_coach(seeded):
    """Loop round 2: 'staff under Miller' listed assistants, goalie coach and GM but dropped the head coach."""
    with Session(seeded) as s:
        for q in (
            "Wer ist im Staff unter Head Coach Shannon Miller: Assistenztrainer, Goalie-Trainer, GM?",
            "Who are the assistant coaches, goalie coach and GM under Miller?",
            "Wer gehört zum Trainerstab unter Shannon Miller?",
        ):
            res = staff.answer(s, q, today=TODAY)
            lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
            assert any(x.startswith("Headcoach: Shannon Miller") for x in lines), q
            assert any("Justin Krueger" in x for x in lines), q
        res = staff.answer(s, "Who are the assistant coaches, goalie coach and GM under Miller?", today=TODAY)
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert any("Simon Theiler" in x for x in lines) and any("Melanie Häfliger" in x for x in lines)
        assert "nicht gefragt" in res["hinweis"].lower() or any("nicht gefragt" in x for x in lines)


def test_get_team_roster_and_get_event_show_staff_apart_from_players(seeded):
    from klugscheisser.mcp_server import tools
    from klugscheisser.mcp_server.present import render_markdown

    with Session(seeded) as s:
        out = tools.get_team_roster(s, "Schweiz", "wch-2026")
        names = {p["name"] for p in out["spielerinnen"]}
        assert "Shannon Miller" not in names and len(names) == 25
        assert any(z["text"].startswith("Headcoach: Shannon Miller") for z in out["staff"])
        text = render_markdown(out)
        assert "**Trainer und Staff**" in text and "Justin Krueger" in text
        # 24.10.: roster staff already in the staff list is not repeated in "Staff im Aufgebot"
        assert text.count("Henrik Ulke") == 1 and text.count("Shannon Miller") == 1
        ev = tools.get_event(s, "Frauen-WM", posts_per_player=0)
        sui = next(t for t in ev["teams"] if t["schweiz"])
        assert sui["staff"].startswith("Headcoach: Shannon Miller")
        assert len(sui["spielerinnen"]) == 25
        ger = next(t for t in ev["teams"] if t["team"] == "Deutschland")
        assert "Jeff MacLeod" in ger["staff"]


def test_web_coach_lookup_uses_stored_staff(seeded):
    from klugscheisser.answer.lookups import coach_inputs

    with Session(seeded) as s:
        inputs = coach_inputs(s, "Wer ist der Trainer der Schweiz?")
        direct = inputs.docs[0].extra["direct"]
        texts = [r[0] for r in direct["rows"]]
        assert texts[0].startswith("Headcoach: Shannon Miller")


def test_roster_watch_keeps_staff_when_loading_official_list(seeded, as_provisional):
    from test_wch_2026 import SIHF_ARTICLE, SIHF_LIST, FakeClient, FakeResponse

    from klugscheisser.jobs import roster_watch

    pages = {
        "https://www.sihf.ch/de/news/": FakeResponse(SIHF_LIST),
        "https://www.sihf.ch/de/news/a-frauen/202627/das-wm-kader-der-frauen-fuer-daenemark-steht/": FakeResponse(
            SIHF_ARTICLE
        ),
    }
    with Session(seeded) as s:
        as_provisional(s, "sui-women")
        roster_watch.run(s, client=FakeClient(pages), today=date(2026, 11, 1))
        s.commit()
        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        crew = staff.event_staff(s, ev, _team(s))
        assert "Shannon Miller" in {e.athlete.full_name for e in crew}
        assert crew and all("Kaderwatch" not in (e.source or "") for e in crew)


def test_staff_seed_lines_all_have_sources():
    import yaml

    for path in staff.STAFF_DIR.glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        for p in data["staff"]:
            src = data["sources"][p["src"]]
            assert src["url"].startswith("https://") and src.get("date"), p
            assert p.get("quote"), p


def test_load_roster_data_records_staff_evidence(db):
    with Session(db) as s:
        load_all_seeds(s)
        n = s.scalar(select(m.TeamStaff.id).order_by(m.TeamStaff.id.desc()))
        load_roster_data(
            s,
            {
                "event": "eht-2026-sodertalje",
                "team": "sui-women",
                "list_type": "confirmed",
                "source": "SIHF",
                "source_date": "2026-11-02",
                "players": [{"name": "Lara Stalder", "pos": "F"}],
                "staff": [{"name": "Shannon Miller", "role": "Head Coach"}],
            },
        )
        miller = next(a for a in staff.current(s, _team(s), TODAY) if a.name == "Shannon Miller")
        assert miller.row.last_seen == date(2026, 11, 2)
        assert s.scalar(select(m.TeamStaff.id).order_by(m.TeamStaff.id.desc())) == n  # no duplicate row


# --- 2026-11-01: media managers / press officers with published work contact ----------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Media Managerin",
        "média manažerka ČR A-týmu žen",
        "Medien & Kommunikation",
        "Presseansvarlig",
        "Director of Communications",
        "Manager, Communications",
    ],
)
def test_media_labels_are_media_not_gm_or_coach(label):
    assert staff.role_of(label) == "media" and not staff.is_coach(label)


def test_media_contacts_are_seeded_for_the_teams_with_published_contacts(seeded):
    with Session(seeded) as s:
        for key, name, email in [
            ("sui-women", "Flavia Tschumi", "media@example.org"),
            ("ger-women", "Myriam Wetter", "media@example.org"),
            ("cze-women", "Natálie Skálová", "media@example.org"),
            ("aut-women", "Johannes Pressler", "media@example.org"),
            ("den-women", "Jonas Nygaard", "media@example.org"),
            ("usa-women", "Melissa Katz", "media@example.org"),
            ("can-women", "Esther Madziya", "media@example.org"),
            ("swe-women", "Linus Hugosson", "media@example.org"),
            ("fin-women", "Henna Malmberg", "media@example.org"),
        ]:
            cs = staff.contacts(s, _team(s, key), TODAY)
            assert cs and cs[0]["name"] == name and cs[0]["email"] == email, key
            assert cs[0]["telefon"] and cs[0]["url"].startswith("https://")
            assert cs[0]["rolle"].startswith("Medienverantwortliche:r")
        # a media manager without a published contact is staff, but no contact line
        swe = staff.current(s, _team(s, "swe-women"), TODAY)
        assert any(a.name == "Emma Spennare" and a.key == "media" for a in swe)
        assert [c["name"] for c in staff.contacts(s, _team(s, "swe-women"), TODAY)] == ["Linus Hugosson"]
        # Myriam Wetter was on the roster seed already: one row, now the seed's (with contact)
        rows = list(
            s.scalars(select(m.TeamStaff).join(m.Athlete).where(m.Athlete.full_name == "Myriam Wetter"))
        )
        assert len(rows) == 1 and rows[0].origin == "seed" and rows[0].email
        tschumi = staff.contacts(s, _team(s), TODAY)[0]
        assert "Kontakt laut SIHF, Geschäftsstelle" in (
            s.scalar(select(m.TeamStaff.note).where(m.TeamStaff.athlete_id == tschumi["id"])) or ""
        )


@pytest.mark.parametrize(
    "question",
    [
        "Wer ist die Medienverantwortliche der Schweiz?",
        "Pressekontakt Dänemark für ein Interview?",
        "Media Manager Tschechien",
    ],
)
def test_media_questions_get_the_contact(seeded, question):
    with Session(seeded) as s:
        res = staff.answer(s, question, today=TODAY)
        assert res is not None
        lines = [z["text"] for sec in res["abschnitte"] for z in sec["zeilen"]]
        assert any("E-Mail " in x and "Tel. " in x for x in lines), lines
        assert not any(x.startswith("Assistenztrainer") for x in lines)
        assert all("E-Mail" not in n for n in res["aktuell"])  # names only


def test_media_words_alone_do_not_turn_coach_questions_into_media_questions():
    assert staff.asked_roles("Was schreiben die Medien über die Trainerin der Schweiz?") == staff.COACH_KEYS
    assert staff.asked_roles("Wer ist der Pressesprecher?") == staff.MEDIA_KEYS


def test_roster_event_and_web_show_media_contacts(seeded):
    from klugscheisser.mcp_server import tools
    from klugscheisser.mcp_server.present import render_markdown
    from klugscheisser.web import roster as rs

    with Session(seeded) as s:
        out = tools.get_team_roster(s, "Deutschland", "wch-2026")
        z = next(z for z in out["staff"] if "Myriam Wetter" in z["text"])
        assert (
            z["email"] == "media@example.org" and "E-Mail media@example.org" in z["text"]
        )
        assert "media@example.org" in render_markdown(out)
        ev = tools.get_event(s, "Frauen-WM", posts_per_player=0)
        den = next(t for t in ev["teams"] if t["team"] == "Dänemark")
        assert "Jonas Nygaard" in den["medien"] and "+00 00 000 00 00" in den["medien"]
        text = render_markdown(ev)
        assert "**Medienkontakte der Teams**" in text and "media@example.org" in text
        html = rs.contacts_html([(None, c) for c in staff.contacts(s, _team(s, "ger-women"), TODAY)])
        assert "href='mailto:media@example.org'" in html and "href='tel:+00 00 000 00 00'" in html
        assert "Medienkontakt" in html and rs.contacts_html([]) == ""
