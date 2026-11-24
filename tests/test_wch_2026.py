"""WCh 2026 as a complete event: tournament sheet (format, schedule, history), provisional rosters of all teams,
results flowing into the seeded games, and the roster-announcement watcher (fake HTTP, no network)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import tournament
from klugscheisser.db import models as m
from klugscheisser.interfaces import GameResult
from klugscheisser.seed.loader import load_all_seeds


@pytest.fixture
def wch(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        return s.scalar(select(m.Event.id).where(m.Event.key == "wch-2026"))


def test_info_file_every_fact_has_a_known_source():
    info = tournament.load_info("wch-2026")
    sources = info["sources"]
    assert all(s["url"].startswith("https://") for s in sources.values())
    for f in info["facts"] + info["history_notes"] + info["playoffs"] + info["pre_games"]:
        assert f["src"] in sources, f
    assert len(info["games"]) == 20 and len(info["history"]) == 26


def test_games_seeded_with_collector_keys_and_rosters_for_all_teams(db, wch):
    with Session(db) as s:
        sui_usa = s.scalar(select(m.Event).where(m.Event.key == "iihf-13547"))
        assert sui_usa.name == "Schweiz – USA" and sui_usa.city == "Esbjerg"
        assert sui_usa.starts_at == datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
        assert "gamecenter" in sui_usa.source_url and sui_usa.competition_id is not None
        ev = s.get(m.Event, wch)
        for team in s.scalars(select(m.Team).join(m.EventTeam).where(m.EventTeam.event_id == ev.id)):
            st = tournament.roster_status(s, ev, team)
            assert st["anzahl"] >= 20, team.name
            assert st["datum"], team.name
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        st = tournament.roster_status(s, ev, sui)
        # official WM list (SIHF 01.11.2026); 03.11.2026: every team's seeded list is official
        assert st["status"] == "offiziell" and st["anzahl"] == 25 and "SIHF" in st["quelle"]
        ger = s.scalar(select(m.Team).where(m.Team.key == "ger-women"))
        assert tournament.roster_status(s, ev, ger)["status"] == "offiziell"
        can = s.scalar(select(m.Team).where(m.Team.key == "can-women"))
        assert any(a["name"] == "Marie-Philip Poulin" for a in tournament.roster_status(s, ev, can)["fehlen"])


def test_results_pipeline_updates_the_seeded_game(db, wch):
    from klugscheisser.ingest.results import upsert_event

    game = GameResult(
        source="IIHF",
        sport="icehockey_women",
        competition="IIHF Frauen-WM 2026",
        external_id="13547",
        url="https://www.iihf.com/en/events/2026/ww/gamecenter/playbyplay/75141/3-sui-vs-usa",
        status="final",
        starts_at=datetime(2026, 11, 6, 18, 0, tzinfo=UTC),
        home="Schweiz",
        away="USA",
        score="2:3",
        venue="Hockey Arena Esbjerg",
        raw={"home_code": "SUI", "away_code": "USA"},
    )
    with Session(db) as s:
        n = s.scalar(select(m.Event.id).where(m.Event.kind == "game").order_by(m.Event.id.desc()))
        ev, created, _ = upsert_event(s, "iihf", game, {})
        assert not created and ev.key == "iihf-13547" and ev.score == "2:3"
        assert s.scalar(select(m.Event.id).where(m.Event.kind == "game").order_by(m.Event.id.desc())) == n
        s.commit()
        load_all_seeds(s)  # re-seeding keeps the result
        s.commit()
        assert s.scalar(select(m.Event).where(m.Event.key == "iihf-13547")).score == "2:3"
        res = tournament.answer(s, s.get(m.Event, wch), "Resultate der Schweiz an der WM")
        assert any("2:3" in z["text"] for sec in res["abschnitte"] for z in sec["zeilen"])


@pytest.mark.parametrize(
    "question,section,needle",
    [
        ("Wie funktioniert der Modus an der Frauen-WM?", "modus", "Weltrangliste"),
        ("Gegen wen spielt die Schweiz an der WM?", "gruppen", "Tschechien"),
        ("Wann spielt die Schweiz an der WM?", "spielplan", "19:00"),
        ("Wer könnte im WM-Viertelfinal der Gegner der Schweiz sein?", "viertelfinal", "A1 gegen B4"),
        ("Wer steigt an der WM ab?", "abstieg", "A5 gegen B5"),
        ("Wo fand die WM früher statt und wie war die Schweiz klassiert?", "geschichte", "Burlington"),
        ("Warum ist die WM im November?", "november", "Olympiajahren"),
        ("Welche Testspiele hat die Schweiz vor der WM?", "vorbereitung", "Södertälje"),
        ("Wer ist im WM-Kader der Schweiz?", "kader", "Lara Stalder"),
    ],
)
def test_tournament_questions_are_answered_from_the_sheet(db, wch, question, section, needle):
    with Session(db) as s:
        ev = tournament.event_for_question(s, question)
        assert ev is not None and ev.key == "wch-2026"
        res = tournament.answer(s, ev, question)
        secs = [sec for sec in res["abschnitte"] if sec["thema"] == section]
        assert secs, res["themen"]
        text = " ".join(z["text"] for sec in secs for z in sec["zeilen"])
        assert needle in text
        assert all(z["url"].startswith("https://") for sec in res["abschnitte"] for z in sec["zeilen"])
        assert len(tournament.render(res, "x")) < 9000  # compact, not a wall of text


def test_person_and_player_questions_stay_with_the_engine(db, wch):
    with Session(db) as s:
        assert tournament.event_for_question(s, "Wer hat an der WM die meisten Tore geschossen?") is None
        assert tournament.event_for_question(s, "Gossip vor der WM?") is None
        assert tournament.event_for_question(s, "Ist Lara Stalder im WM-Kader?") is None
        assert tournament.event_for_question(s, "Wer hatte ein Comeback?", event_id=wch) is None
        assert tournament.event_for_question(s, "Wie ist der Modus?", event_id=wch) is not None


def test_frage_tool_answers_tournament_question_fast_without_engine(db, wch, monkeypatch):
    from klugscheisser.answer import engine
    from klugscheisser.mcp_server import server

    monkeypatch.setattr(engine, "build_answer_engine", lambda *a: pytest.fail("engine must not be used"))
    out = server.frage("Wie funktioniert der WM-Modus und wer steigt ab?")
    text = out.content[0].text
    assert "Datenstand" in text and "A1–B4" in text and "Relegationsspiel" in text
    assert out.structured_content is None and text.count("](https://") >= 2


def test_get_event_is_compact_with_sheet_and_roster_status(db, wch):
    from klugscheisser.mcp_server import tools
    from klugscheisser.mcp_server.present import render_markdown

    with Session(db) as s:
        out = tools.get_event(s, "Frauen-WM", posts_per_player=0)
    assert out["anlass"]["key"] == "wch-2026"
    assert {t["team"] for t in out["teams"]} >= {"Schweiz", "Kanada", "Dänemark"}
    sui = next(t for t in out["teams"] if t["schweiz"])
    assert len(sui["spielerinnen"]) == 25 and sui["spielerinnen"][0]["position"] == "Torhüterin"
    assert out["lineup_status"] == "Bestätigter Kader."
    assert sui["status"] == "offiziell" and sui["anzahl"] == 25 and "fehlen" in sui
    can = next(t for t in out["teams"] if t["team"] == "Kanada")
    # 2026-10-28: Hockey Canada's official roster (19.10.2026) replaced the 48-player camp list
    assert "spielerinnen" not in can and can["status"] == "offiziell" and can["anzahl"] == 25
    assert out["turnier"] and out["turnier"][0]["zeilen"]
    text = render_markdown(out)
    assert "Gruppe A" in text and "Kader der Teams" in text and len(text) < 15000


SIHF_LIST = """<html><body><a href="/de/news/a-frauen/202627/frauen-nationalteam-reist-fuer-zweites-turnier/">Lahti</a>
<a href="/de/news/a-frauen/202627/das-wm-kader-der-frauen-fuer-daenemark-steht/">Das WM-Kader der Frauen steht</a>
</body></html>"""
SIHF_ARTICLE = (
    "<html><body><p>Head Coach Shannon Miller hat ihr WM-Aufgebot bestimmt. Alina Marti fehlt verletzt.</p>"
    "<p>Torhüterinnen (3): Andrea Brändli (PWHL Detroit / USA), Saskia Maurer (SC Bern), Chiara Pfosi (EV Zug)</p>"
    "<p>Verteidigerinnen (8): "
    + ", ".join(f"Vera Test{i} (EV Zug)" for i in range(8))
    + "</p><p>Stürmerinnen (12): "
    + ", ".join(f"Stella Sturm{i} (ZSC Lions)" for i in range(11))
    + ", Lara Stalder (EV Zug)</p></body></html>"
)


class FakeResponse:
    def __init__(self, text, status=200, ctype="text/html"):
        self.text, self.status_code, self.headers = text, status, {"content-type": ctype}


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        if url not in self.pages:
            return FakeResponse("<html></html>")
        return self.pages[url]


def test_roster_watch_finds_and_loads_swiss_list_and_flags_others(db, wch, as_provisional):
    from klugscheisser.jobs import roster_watch

    pages = {
        "https://www.sihf.ch/de/news/": FakeResponse(SIHF_LIST),
        "https://www.sihf.ch/de/news/a-frauen/202627/das-wm-kader-der-frauen-fuer-daenemark-steht/": FakeResponse(
            SIHF_ARTICLE
        ),
        "https://ishockey.dk/wp-json/wp/v2/posts?per_page=20&_fields=date,link,title": FakeResponse(
            '[{"link": "https://ishockey.dk/vm-truppen-kvinder/", "title": {"rendered": "Her er VM-truppen for kvinder"}}]',
            ctype="application/json",
        ),
    }
    client = FakeClient(pages)
    with Session(db) as s:
        as_provisional(s, "sui-women", "den-women")  # as before their official lists
        report = roster_watch.run(s, client=client, today=date(2026, 11, 1))
        s.commit()
        assert {n["team"] for n in report["neu"]} == {"SUI", "DEN"}
        # GER/AUT: official list already; only their list page is re-read (fake: empty page, nothing changes)
        assert "SUI" in report["geprueft"] and "GER" not in {n["team"] for n in report["neu"]}
        ev = s.get(m.Event, wch)
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        st = tournament.roster_status(s, ev, sui)
        assert st["status"] == "offiziell" and st["anzahl"] == 23 and st["quelle"].startswith("Kaderwatch")
        den = s.scalar(select(m.Team).where(m.Team.key == "den-women"))
        assert "publiziert am 01.11.2026" in tournament.roster_status(s, ev, den)["offizieller_kader"]
        found = roster_watch.watch_state(s)["found"]
        assert any("verletzt" in x for x in found["SUI"]["fehlen_saetze"])
        assert s.scalar(select(m.Item).where(m.Item.url.like("%ishockey.dk/vm-truppen%"))) is not None
        # a seed not newer than the watcher's list never replaces it; a second run does not announce it again,
        # it only re-reads the official list (late replacements) and leaves an unchanged list alone
        load_all_seeds(s)
        s.commit()
        assert tournament.roster_status(s, ev, sui)["anzahl"] == 23
        n_calls = len(client.calls)
        again = roster_watch.run(s, client=client, today=date(2026, 11, 2))
        assert "https://www.sihf.ch/de/news/" not in client.calls[n_calls:] and again["neu"] == []
        assert again["kader"]["SUI"]["status"] == "unverändert"
        # after the tournament nothing is fetched any more
        n_calls = len(client.calls)
        roster_watch.run(s, client=client, today=date(2026, 11, 20))
        assert len(client.calls) == n_calls


def test_roster_watch_survives_errors_and_robots(db, wch, as_provisional):
    from klugscheisser.collectors.profiles.http import Disallowed
    from klugscheisser.jobs import roster_watch

    class Broken:
        def get(self, url, **kw):
            if "leijonat" in url:
                raise Disallowed(url)
            raise RuntimeError("down")

    with Session(db) as s:
        as_provisional(s, "fin-women", "sui-women")
        report = roster_watch.run(s, client=Broken(), today=date(2026, 10, 20))
        assert report["neu"] == [] and "nicht erlaubt" in report["fehler"]["FIN"]
        assert "nicht erreichbar" in report["fehler"]["SUI"]


def test_parse_sihf_roster_on_real_format():
    text = roster_watch_text()
    players = __import__("klugscheisser.jobs.roster_watch", fromlist=["x"]).parse_sihf_roster(text)
    assert len(players) == 28
    assert players[0] == {"name": "Andrea Brändli", "pos": "G", "club": "PWHL Detroit", "country": "USA"}


def roster_watch_text() -> str:
    seed = yaml.safe_load(Path("seeds/rosters/eht-2026-sodertalje-sui.yaml").read_text())
    blocks = {"G": "Torhüterinnen", "D": "Verteidigerinnen", "F": "Stürmerinnen"}
    out = []
    for pos, label in blocks.items():
        ps = [p for p in seed["players"] if p["pos"] == pos]
        out.append(
            f"{label} ({len(ps)}): "
            + ", ".join(
                f"{p['name']} ({p['club']}{' / ' + p['country'] if p.get('country') else ''})" for p in ps
            )
        )
    return "\n".join(out)
