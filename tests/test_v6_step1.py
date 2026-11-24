"""v6 step 1: scope from the question, stored person data as quotable facts, honest "no answer", answer cache,
ratings."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def test_scope_from_question(seeded):
    with Session(seeded) as s:
        ids, note = eng.scope_from_text(s, "Wer ist die Älteste im Olympia-Team?")
        assert len(ids) == 23 and "Olympische Spiele 2026" in note
        ids, note = eng.scope_from_text(s, "Who has the most followers in the team?")
        assert ids and "angenommen" in note
        ids, note = eng.scope_from_text(s, "Wie geht es der Nati?")
        assert ids and "angenommen" not in note
        assert eng.scope_from_text(s, "Was gibt es Neues?") == ([], None)


def test_fact_docs_for_oldest_and_followers(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        a.birth_date, a.wikidata_qid = date(1994, 5, 15), a.wikidata_qid or "Q123"
        h = add_handle_candidate(s, a, "instagram", "lara.stalder7", {"kind": "manual"})
        h.profile = {"followers": 12345, "fetched_at": "2026-10-03T10:00:00+00:00"}
        s.flush()
        docs = eng.fact_docs(s, [a])
        texts = " ".join(d.text for d in docs)
        assert "geboren am 15.05.1994" in texts and "12345 Follower" in texts
        assert all(d.url.startswith("https://") for d in docs)
        assert eng.STRUCTURED.search("Wer ist die Älteste?") and eng.STRUCTURED.search("most followers")


def test_no_answer_shows_closest_sources_and_cache(seeded, monkeypatch):
    calls = []

    def llm(system, prompt, schema):
        calls.append(1)
        return {"statements": [], "luecken": ["Keine Aussage der Trainerin"]}, {}

    with Session(seeded) as s:
        lara = repo.resolve_athlete(s, "Lara Stalder")
        item = m.Item(
            source_type="srf",
            platform="rss",
            url="https://srf.ch/x",
            title="Das Team von Trainerin Miller",
            text="Das Team von Trainerin Shannon Miller verliert.",
            content_hash="h",
        )
        s.add(item)
        s.flush()
        item.athletes.append(m.ItemAthlete(athlete_id=lara.id))
        s.commit()
        lara_id = lara.id
    e = eng.GeminiAnswerEngine(SETTINGS, llm=llm, support_checker=lambda p: [1.0] * len(p))
    r1 = e.answer("Was sagte die Trainerin?", person_ids=[lara_id])
    assert not r1.statements and r1.naechste_quellen and r1.naechste_quellen[0]["url"] == "https://srf.ch/x"
    assert r1.durchsucht.startswith("1 Personen") and r1.aus_cache is None
    r2 = e.answer("Was sagte die Trainerin?", person_ids=[lara_id])
    assert r2.aus_cache and len(calls) == 1  # from the cache, no model call
    r3 = e.answer("Was sagte die Trainerin?", person_ids=[lara_id], use_cache=False)
    assert r3.aus_cache is None and len(calls) == 2


def test_rating_flow(seeded):
    with TestClient(build_web_app(SETTINGS)) as c:
        r = c.post("/chat", data={"frage": "Was gibt es Neues bei Lara Stalder?"}, follow_redirects=False)
        chat_id = int(r.headers["location"].rsplit("/", 1)[1])
        html = c.post(f"/chat/{chat_id}/antwort", headers={"HX-Request": "true"}).text
        assert "Wie gut war die Antwort?" in html and "value='gut'" in html
        with Session(seeded) as s:
            msg_id = s.scalar(select(m.ChatMessage.id).where(m.ChatMessage.role == "assistant"))
        w = c.post(
            f"/bewertung/{msg_id}",
            data={"rating": "schlecht", "grund": "Quelle veraltet"},
            headers={"HX-Request": "true"},
        ).text
        assert "gespeichert" in w and "Quelle veraltet" in w
        page = c.get("/bewertungen").text
        assert "schlecht: 1" in page and "Quelle veraltet" in page
        data = c.get("/bewertungen.json").json()
        assert data[0]["bewertung"] == "schlecht" and data[0]["frage"].startswith("Was gibt es Neues")
