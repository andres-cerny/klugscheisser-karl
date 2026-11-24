"""v6 step 5: game brief — rosters, one sourced line per player, connections, planned games; chat command."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


def test_brief_page_and_chat_command(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        cz = [
            repo.upsert_athlete(s, sport, f"Testová Hráčka {i}", nationality="CZE", position="F")
            for i in range(12)
        ]
        cz[0].birth_date = date(2000, 1, 1)
        cze = repo.get_team(s, "cze-women")  # v8: CZE has a seeded roster; the test players join the team
        s.add_all(m.Affiliation(athlete_id=a.id, team_id=cze.id, season="2025-26") for a in cz)
        wch = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))  # v18: CZE has a WM roster
        s.add_all(m.EventEntry(event_id=wch.id, athlete_id=a.id, team_id=cze.id, role="F") for a in cz)
        lara = repo.resolve_athlete(s, "Alina Müller")  # 2026-10-28: Stalder is off the WM list (injured)
        club = m.Team(sport_id=sport.id, key="brynas-t", name="Brynäs IF Dam", kind="club", country="SWE")
        s.add(club)
        s.flush()
        for a in (lara, cz[0]):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_id=club.id,
                    team_name="Brynäs IF Dam",
                    kind="club",
                    season_from=2020,
                    season_to=2021,
                    source="wikidata",
                    source_url="https://www.wikidata.org/wiki/Q9",
                )
            )
        s.commit()
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get("/brief", params={"a": "CZE", "b": "SUI", "event": "wch-2026"}).text
        assert "<h1>Tschechien – Schweiz</h1>" in html and "Verbindungen zwischen den Teams" in html
        assert "zusammen bei Brynäs IF Dam" in html and "Testová Hráčka 0" in html and "Stürmerin" in html
        assert "/person/" in html and "?tab=chat" in html
        assert "Bitte zwei verschiedene Teams" in c.get("/brief", params={"a": "SUI", "b": "SUI"}).text
        assert "Spiel-Brief</summary>" in c.get("/event/wch-2026").text
        r = c.post(
            "/chat",
            data={"frage": "Erstelle einen Brief für Tschechien gegen Schweiz"},
            follow_redirects=False,
        )
        chat_id = int(r.headers["location"].rsplit("/", 1)[1])
        answer = c.post(f"/chat/{chat_id}/antwort", headers={"HX-Request": "true"}).text
        assert "/brief?a=CZE&amp;b=SUI" in answer or "/brief?a=CZE&b=SUI" in answer
