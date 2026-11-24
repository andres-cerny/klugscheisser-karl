"""v4: chats with follow-ups (server-side), search log, add persons/teams (proposal → Ja/Nein/Präzisieren → load),
sources page and per-person data sources."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.adds import flow
from klugscheisser.adds import load as add_load
from klugscheisser.adds.base import Proposal, ProposedPerson, ProposedTeam, SourceRef
from klugscheisser.answer.engine import Doc, GeminiAnswerEngine, history_block
from klugscheisser.chat import service as chat_svc
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import usable_handles
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app

SETTINGS = Settings(_env_file=None, web_answer_engine="fake")
HX = {"HX-Request": "true"}


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


@pytest.fixture
def client(seeded):
    with TestClient(build_web_app(SETTINGS)) as c:
        yield c


def athlete_id(engine, name: str) -> int:
    with Session(engine) as s:
        return repo.resolve_athlete(s, name).id


# --- chat service -----------------------------------------------------------------------------------------


def test_scope_named_person_wins_else_follow_up_else_context(seeded):
    with Session(seeded) as s:
        alina, lara = repo.resolve_athlete(s, "Alina Müller"), repo.resolve_athlete(s, "Lara Stalder")
        chat = chat_svc.create_chat(s, "Was gibt es Neues?", athlete_id=alina.id)
        assert chat_svc.scope_for(s, chat, "Was gibt es Neues?", []) == {"person_ids": [alina.id]}
        assert chat_svc.scope_for(s, chat, "und Lara Stalder?", []) == {"person_ids": [lara.id]}
        past = [{"frage": "und Lara Stalder?", "antwort": "…", "personen": [], "person_ids": [lara.id]}]
        assert chat_svc.scope_for(s, chat, "Was hat sie dazu gesagt?", past) == {"person_ids": [lara.id]}
        free = chat_svc.create_chat(s, "Wer studiert?")
        assert chat_svc.scope_for(s, free, "Wer studiert?", []) == {"team_id": None, "event_id": None}


def test_history_pairs_questions_and_answers(seeded):
    from klugscheisser.interfaces import AnswerResult, Statement

    with Session(seeded) as s:
        chat = chat_svc.create_chat(s, "x" * 200)
        assert len(chat.title) <= 82 and chat.title.endswith("…")
        st = Statement("Sie studiert Sport.", "studiere Sport", 1, "https://x", "[X]", "[X](https://x)", "📸")
        for q in ("Frage 1", "Frage 2", "Frage 3"):
            chat_svc.add_question(s, chat, q)
            chat_svc.add_answer(
                s, chat, AnswerResult(q, "heute", [st], personen=["Lara Stalder"], person_ids=[7])
            )
        chat_svc.add_question(s, chat, "offen")  # not answered yet: not part of the history
        s.refresh(chat)
        past = chat_svc.history(chat, 2)
        assert [p["frage"] for p in past] == ["Frage 2", "Frage 3"]
        assert past[-1]["person_ids"] == [7] and past[-1]["antwort"] == "Sie studiert Sport."
        back = chat_svc.result_from_json(chat.messages[1].answer)
        assert back.statements[0].zitat == "studiere Sport"


def test_recent_searches_dedup_newest_first(seeded):
    with Session(seeded) as s:
        chat_svc.log_search(s, "alina")
        chat_svc.log_search(s, "ali", "person:1", "Alina Müller")
        chat_svc.log_search(s, "alina m", "person:1", "Alina Müller")
        chat_svc.log_search(s, "  ")
        rows = chat_svc.recent_searches(s)
        assert [(r.query, r.result_ref) for r in rows] == [("alina m", "person:1"), ("alina", None)]


def test_follow_up_history_goes_into_prompt_not_as_source():
    seen = {}

    def fake_llm(system, prompt, schema):
        seen["prompt"], seen["system"] = prompt, system
        return {"statements": [], "luecken": []}, {}

    eng = GeminiAnswerEngine(SETTINGS, llm=fake_llm, support_checker=lambda pairs: [1.0] * len(pairs))
    doc = Doc(
        key="",
        text="Ich studiere Sport.",
        meta="[Instagram]",
        url="https://x",
        quelle="[X]",
        zeile_prefix="📸",
    )
    past = [{"frage": "Wer studiert?", "antwort": "Lara Stalder studiert.", "personen": ["Lara Stalder"]}]
    eng._write("Was genau?", [doc], ["Lara Stalder"], past)
    assert seen["prompt"].startswith("Bisheriges Gespräch (nur Kontext, keine Quelle):")
    assert "- Frage: Wer studiert?" in seen["prompt"] and "Frage: Was genau?" in seen["prompt"]
    assert "keine Quelle" in seen["system"]
    assert history_block(None) == ""


# --- chat in the web app ----------------------------------------------------------------------------------


def test_chat_flow_home_recent_and_follow_up(client, seeded):
    r = client.post("/chat", data={"frage": "Wer spielt in der PWHL?"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/chat/")
    chat_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/chat/{chat_id}").text
    assert "Wer spielt in der PWHL?" in page and f"/chat/{chat_id}/antwort" in page  # pending answer
    client.post(f"/chat/{chat_id}/antwort", headers=HX)
    follow = client.post(f"/chat/{chat_id}/nachricht", data={"frage": "und Lara Stalder?"}, headers=HX)
    assert "und Lara Stalder?" in follow.text and "/antwort" in follow.text
    client.post(f"/chat/{chat_id}/antwort", headers=HX)
    with Session(seeded) as s:
        chat = s.get(m.Chat, chat_id)
        assert [x.role for x in chat.messages] == ["user", "assistant", "user", "assistant"]
        assert chat.messages[-1].answer["person_ids"] == [athlete_id(seeded, "Lara Stalder")]
    home = client.get("/").text
    assert "Letzte Chats" in home and "Wer spielt in der PWHL?" in home and "Letzte Suchen" in home
    assert 'href="/chats"' in home and 'role="search"' in home  # Mehr menu + one field
    assert "Wer spielt in der PWHL?" in client.get("/chats").text


def test_search_logged_and_shown_on_home(client, seeded):
    pid = athlete_id(seeded, "Alina Müller")
    client.get(f"/person/{pid}", params={"q": "alina"})
    client.get("/suche", params={"q": "zsc"})  # full search page (Enter)
    client.get("/suche", params={"q": "zs"}, headers=HX)  # typing: not logged
    home = client.get("/").text
    assert f"/person/{pid}" in home and "Alina Müller" in home and "/suche?q=zsc" in home
    assert "/suche?q=zs'" not in home


def test_person_page_two_columns_chat_and_sources(client, seeded):
    pid = athlete_id(seeded, "Alina Müller")
    html = client.get(f"/person/{pid}", params={"tab": "chat"}).text
    assert "class='pg' data-view='chat'" in html and 'class="pg-info"' in html and 'class="pg-chat"' in html
    assert (
        "setView(this,'info')\"><span class='l'>Profil</span></button>" in html
        and "Datenquellen</h2>" in html
    )
    r = client.post("/chat", data={"frage": "Hat sie Hobbys?", "person": pid, "eingebettet": "1"}, headers=HX)
    assert 'id="chatpanel"' in r.text and "HX-Push-Url" not in r.headers  # stays on the person page
    again = client.get(f"/person/{pid}").text
    assert "Hat sie Hobbys?" in again  # latest chat about her is shown
    assert "Hat sie Hobbys?" not in client.get(f"/person/{pid}", params={"neu": "1"}).text


def test_sources_page(client):
    html = client.get("/quellen").text
    for name in ("SRF", "Wikidata", "Brave Search API", "SIHF", "Instagram via Apify"):
        assert name in html
    assert "Nutzungsbedingungen verbieten Scraping" in html  # v11: Elite Prospects is read (politely)


# --- add persons / teams ----------------------------------------------------------------------------------


def person_proposal(**kw) -> Proposal:
    p = ProposedPerson(
        name="Hanna Testsson",
        sport_key="icehockey_women",
        nationality="SWE",
        birth_date=date(1999, 3, 12),
        team="Frölunda HC",
        position="F",
        wikidata_qid="Q999001",
        instagram="hanna.testsson",
        instagram_source="fabi",
        instagram_check="nicht geprüft",
        sources=[SourceRef("Wikidata Q999001", "https://www.wikidata.org/wiki/Q999001")],
    )
    return Proposal(
        kind="person", found=True, summary="Hanna Testsson (SWE), Eishockeyspielerin", persons=[p], **kw
    )


@pytest.fixture
def fake_resolve(monkeypatch):
    calls = []

    def resolve(settings, s, req, previous):
        calls.append((req.kind, dict(req.input), req.refine_text, previous))
        if req.kind == "team":
            players = [
                ProposedPerson(name=f"Spielerin {i}", sport_key="icehockey_women", nationality="SWE")
                for i in range(3)
            ]
            coach = ProposedPerson(name="Trainer Test", role="coach", function_de="Cheftrainer")
            return Proposal(
                kind="team",
                found=True,
                summary="Schweden Frauen, 3 Spielerinnen, 1 Trainer",
                persons=[*players, coach],
                team=ProposedTeam(
                    name="Schweden Frauen", country="SWE", roster_source=SourceRef("Wikipedia", "https://w")
                ),
            )
        if req.refine_text:
            return person_proposal(notes=["präzisiert"])
        return person_proposal()

    monkeypatch.setattr(flow, "_resolve", resolve)
    return calls


def test_add_person_yes_creates_person_with_fabi_handle_and_loads(client, seeded, fake_resolve, monkeypatch):
    jobs = []
    monkeypatch.setattr(
        "klugscheisser.jobs.refresh.executor",
        lambda: type("X", (), {"submit": lambda self, *a, **k: jobs.append(a)})(),
    )
    r = client.post(
        "/hinzufuegen", data={"kind": "person", "name": "Hanna Testsson", "instagram": "@hanna"}, headers=HX
    )
    assert "Hanna Testsson (SWE)" in r.text and "Ja, hinzufügen" in r.text and "Präzisieren" in r.text
    assert "Geschätzte Kosten" in r.text and "Limit 5 CHF" in r.text
    assert fake_resolve[0][1]["instagram"] == "@hanna"
    with Session(seeded) as s:
        req_id = s.scalar(select(m.AddRequest.id))
    r = client.post(f"/hinzufuegen/{req_id}/ja", headers=HX)
    assert "Lade Daten im Hintergrund" in r.text and len(jobs) == 1
    with Session(seeded) as s:
        req = s.get(m.AddRequest, req_id)
        assert req.status == "loading" and req.job_id
        a = s.get(m.Athlete, req.athlete_ids[0])
        assert a.full_name == "Hanna Testsson" and a.wikidata_qid == "Q999001" and a.nationality == "SWE"
        assert "Hinzugefügt von Fabi" in a.notes
        h = a.handles[0]
        assert h.handle == "hanna.testsson" and h.status == "wahrscheinlich"  # never verified by Fabi alone
        assert h.evidence[0]["kind"] == "fabi"
        assert any(af.team.name == "Frölunda HC" for af in a.affiliations)
    # second Ja does nothing
    client.post(f"/hinzufuegen/{req_id}/ja", headers=HX)
    assert len(jobs) == 1


def test_add_no_and_refine(client, seeded, fake_resolve):
    client.post("/hinzufuegen", data={"kind": "person", "name": "Hanna Testsson"}, headers=HX)
    with Session(seeded) as s:
        first = s.scalar(select(m.AddRequest.id))
    r = client.post(
        f"/hinzufuegen/{first}/praezisieren", data={"text": "die Schwedin, Jahrgang 1999"}, headers=HX
    )
    assert "präzisiert: «die Schwedin, Jahrgang 1999»" in r.text and "Hinweis: präzisiert" in r.text
    assert fake_resolve[-1][2] == "die Schwedin, Jahrgang 1999" and fake_resolve[-1][3] is not None
    with Session(seeded) as s:
        old, new = s.scalars(select(m.AddRequest).order_by(m.AddRequest.id)).all()
        assert old.status == "refined" and new.parent_id == old.id
        new_id = new.id
    r = client.post(f"/hinzufuegen/{new_id}/nein", headers=HX)
    assert "Verworfen" in r.text
    with Session(seeded) as s:
        assert s.get(m.AddRequest, new_id).status == "rejected"
        assert not [x for x in repo.find_athletes(s, "Hanna Testsson") if x.how == "exact"]


def test_add_team_from_chat_with_coach_and_load_job(client, seeded, fake_resolve, monkeypatch):
    r = client.post(
        "/chat", data={"frage": "Füge das schwedische Frauen-Nationalteam hinzu"}, follow_redirects=False
    )
    chat_id = int(r.headers["location"].rsplit("/", 1)[1])
    html = client.post(f"/chat/{chat_id}/antwort", headers=HX).text
    assert "Schweden Frauen, 3 Spielerinnen, 1 Trainer" in html and ">Ja, " in html
    assert fake_resolve[0][0] == "team"
    with Session(seeded) as s:
        req = s.scalar(select(m.AddRequest))
        s.commit()
        job = add_load.confirm(s, SETTINGS, req, submit=lambda *a: None)
        team = s.get(m.Team, req.team_id)
        assert team.name == "Schweden Frauen" and team.country == "SWE"
        people = [s.get(m.Athlete, i) for i in req.athlete_ids]
        assert {p.role for p in people} == {"athlete", "coach"} and len(people) == 4
        assert "Cheftrainer" in next(p for p in people if p.role == "coach").notes
        assert all(any(af.team_id == team.id for af in p.affiliations) for p in people)
        job_id, req_id = job.id, req.id
    # run the load synchronously without paid sources
    monkeypatch.setattr("klugscheisser.jobs.freshness.EXTRA_SOURCES", [])
    monkeypatch.setattr("klugscheisser.jobs.freshness.SLOW_SOURCES", [])
    add_load.run_add_load(job_id, req_id, SETTINGS)
    with Session(seeded) as s:
        assert s.get(m.AddRequest, req_id).status == "done"
        assert s.get(m.Job, job_id).result["schritt"] == "fertig"
    page = client.get(f"/chat/{chat_id}").text
    assert "Fertig" in page and "Spielerin 0" in page


def test_parse_add_message():
    assert flow.parse_add_message("Füge das schwedische Frauen-Nationalteam hinzu") == (
        "team",
        "Füge das schwedische Frauen-Nationalteam hinzu",
    )
    assert flow.parse_add_message("füge Hanna Olsson hinzu") == ("person", "Hanna Olsson")
    assert flow.parse_add_message("Wer spielt in Schweden?") is None


def test_new_sport_registered_on_the_fly(seeded):
    with Session(seeded) as s:
        req = m.AddRequest(kind="person", input={}, status="proposed")
        s.add(req)
        s.flush()
        p = ProposedPerson(
            name="Lara Testgut",
            sport_key="ski_alpine_women",
            sport_name_de="Ski alpin Frauen",
            person_de="Skirennfahrerin",
        )
        req.proposal = Proposal(kind="person", found=True, summary="x", persons=[p]).to_json()
        add_load.apply_proposal(s, req)
        a = repo.resolve_athlete(s, "Lara Testgut")
        assert a.sport.key == "ski_alpine_women" and a.sport.name == "Ski alpin Frauen"
        from klugscheisser.sports import get_sport

        assert get_sport("ski_alpine_women").person_de == "Skirennfahrerin"


def test_link_existing_news_to_new_person(seeded):
    with Session(seeded) as s:
        item = m.Item(
            source_type="srf",
            platform="rss",
            url="https://srf.ch/x",
            title="Hanna Testsson trifft doppelt",
            text="Hanna Testsson trifft doppelt.",
            content_hash="h1",
        )
        s.add(item)
        s.flush()
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Hanna Testsson")
        assert add_load.link_existing_items(s, [a.id]) == 1
        assert add_load.link_existing_items(s, [a.id]) == 0


def test_person_sources_lists_handles_and_profiles(seeded):
    from klugscheisser.handles.service import add_handle_candidate
    from klugscheisser.sources_registry import person_sources

    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        add_handle_candidate(
            s, a, "instagram", "lara.stalder7", {"kind": "fabi", "detail": "von Fabi angegeben"}
        )
        rows = person_sources(s, a)
        ig = next(r for r in rows if r["name"] == "Instagram @lara.stalder7")
        assert "von Fabi angegeben" in ig["info"] and usable_handles(a)


def test_estimate_and_budget(seeded):
    from klugscheisser.adds.base import estimate_load_cost_chf

    assert estimate_load_cost_chf(SETTINGS, 25, 25) < SETTINGS.add_cost_cap_chf
    req = m.AddRequest(kind="person", input={}, proposal={"resolver_cost_usd": 1.0})
    assert add_load.action_budget_usd(SETTINGS, req) == pytest.approx(5.0 / 0.8 - 1.0)
