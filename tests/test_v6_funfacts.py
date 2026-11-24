"""v6 step 6: fun facts — sourced, max 3, stored with date, regenerated only by hand."""

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.answer.engine import GeminiAnswerEngine
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import funfacts as ff
from klugscheisser.web import build_web_app

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


def fake_engine(n: int):
    def llm(system, prompt, schema):
        return {
            "statements": [
                {
                    "aussage": f"Fakt {i}",
                    "dok": "D1",
                    "zitat": "ihre Mutter stammt aus der Slowakei",
                    "kategorie": "anderes",
                }
                for i in range(n)
            ],
            "luecken": [],
        }, {}

    return GeminiAnswerEngine(SETTINGS, llm=llm, support_checker=lambda p: [0.95] * len(p))


def test_generate_and_rerun(db, monkeypatch):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Testa Fun")
        item = m.Item(
            source_type="srf",
            platform="rss",
            url="https://srf.ch/a",
            title="Porträt",
            text="Testa Fun: ihre Mutter stammt aus der Slowakei.",
            content_hash="x",
        )
        s.add(item)
        s.flush()
        item.athletes.append(m.ItemAthlete(athlete_id=a.id))
        s.flush()
        rows = ff.generate(s, SETTINGS, a, engine=fake_engine(5))
        assert len(rows) == 3 and rows[0].source_url == "https://srf.ch/a" and rows[0].quote
        first = ff.generated_at(s, a.id)
        assert first is not None
        ff.generate(s, SETTINGS, a, engine=fake_engine(1))
        assert len(ff.fun_facts(s, a.id)) == 1  # rerun replaces, does not add
        s.commit()
        aid = a.id
    monkeypatch.setattr(ff, "generate", lambda s, st, a, engine=None: [])
    with TestClient(build_web_app(SETTINGS)) as c:
        page = c.get(f"/person/{aid}").text
        assert "Fun Facts" in page and "Neu erstellen" in page and "erstellt am" in page
        r = c.post(f"/person/{aid}/funfacts", headers={"HX-Request": "true"})
        assert r.status_code == 200 and "funfacts" in r.text
