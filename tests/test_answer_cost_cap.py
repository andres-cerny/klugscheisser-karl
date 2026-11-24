"""2026-10-19: what one answer may cost (llm/budget.py): per-question budget, daily cap before every paid call,
graceful degradation (fewer persons, no model → stored documents), every paid call in cost_ledger, and game
questions about a tournament answered from the stored games (answer/event_games.py), not the cross route."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.answer import cross, event_games
from klugscheisser.answer import engine as eng
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest import costs
from klugscheisser.llm import budget
from klugscheisser.llm import text as llm_text

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def _ledger(s: Session, source: str) -> float:
    return float(
        s.scalar(
            select(func.coalesce(func.sum(m.CostLedger.cost_usd), 0.0)).where(m.CostLedger.source == source)
        )
    )


# --- the budget itself ---------------------------------------------------------------------------------------


def test_budget_refuses_once_spent_and_outside_an_answer_nothing_is_checked():
    budget.check()  # no answer running: never refuses
    b = budget.AnswerBudget(max_usd=0.05)
    b.before_call()
    b.charge_llm(0.03)
    assert b.affordable(0.02) and not b.affordable(0.03)
    b.charge_jev(500_000)  # $0.021
    with pytest.raises(budget.AnswerBudgetExceeded):
        b.before_call("jev")
    assert b.refused == 1 and b.left() == 0


def test_daily_cap_counts_answer_model_and_jev(db):
    st = Settings(_env_file=None, llm_daily_cost_cap_usd=1.0)
    with Session(db) as s:
        costs.record(s, llm_text.cost_source(st, "answer"), 1, 0.7, "frage")
        costs.record(s, "jev", 1, 0.2, "frage")
        costs.record(s, "apify", 1, 5.0, "other source")
        s.commit()
    with budget.answering(st) as b:
        assert b.daily_spent_before == pytest.approx(0.9)
        assert b.left() == pytest.approx(0.08)  # min(question budget 0.08, daily 0.1)
        b.charge_llm(0.1)
        assert b.daily_exhausted
        with pytest.raises(budget.AnswerBudgetExceeded):
            budget.check("claude")


def test_finish_books_what_the_call_sites_did_not(db):
    with Session(db) as s, budget.answering(SETTINGS) as b:
        b.charge_llm(0.01)
        costs.record(
            s, llm_text.cost_source(SETTINGS, "answer"), 1, 0.01, "frage"
        )  # booked by the writer step
        b.charge_jev(1_000_000)  # rerank / support checks: nobody books them
        b.finish(s, SETTINGS)
        s.commit()
        assert _ledger(s, llm_text.cost_source(SETTINGS, "answer")) == pytest.approx(0.01)  # not twice
        assert _ledger(s, "jev") == pytest.approx(budget.JEV_USD_PER_M)


def test_jev_client_is_checked_and_charged():
    import httpx

    from klugscheisser.classify.jev import JevClient

    def handler(request):
        return httpx.Response(200, json={"answers": {"q": {"noul": True}}, "usage": {"input_tokens": 2000}})

    client = JevClient(
        SETTINGS, client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://j")
    )
    with budget.answering(SETTINGS) as b:
        client.ask("state", {"q": {}})
        assert b.jev_tokens == 2000 and b.jev_usd > 0
        b.charge_llm(1.0)
        with pytest.raises(budget.AnswerBudgetExceeded):
            client.ask("state", {"q": {}})


# --- the engine ----------------------------------------------------------------------------------------------


@pytest.fixture
def lara(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Lara Testalder", nationality="SUI")
        item = m.Item(
            source_type="srf",
            platform="rss",
            url="https://srf/a",
            title="Lara Testalder trifft",
            text="Lara Testalder trifft doppelt beim 4:1 gegen Lugano. Sie war die beste Spielerin.",
            content_hash="a1",
            published_at=datetime.now(UTC),
        )
        s.add(item)
        s.flush()
        item.athletes.append(m.ItemAthlete(athlete_id=a.id))
        s.commit()
        return a.id


def test_daily_cap_used_up_answers_from_stored_documents_without_model(db, lara):
    st = Settings(_env_file=None, auto_refresh=False, llm_daily_cost_cap_usd=1.0)
    with Session(db) as s:
        costs.record(s, llm_text.cost_source(st, "answer"), 1, 1.5, "frage")
        s.commit()

    def no_model(system, prompt, schema):
        raise AssertionError("no model call when the daily cap is used up")

    e = eng.GeminiAnswerEngine(st, llm=no_model, support_checker=lambda pairs: [1.0] * len(pairs))
    res = e.answer("Was macht Lara Testalder?", person_ids=[lara], use_cache=False)
    assert res.statements and "Lara Testalder trifft doppelt" in res.statements[0].zitat
    assert res.statements[0].url == "https://srf/a"
    assert any("Tageslimit" in g for g in res.luecken)


def test_question_budget_stops_further_calls_and_the_answer_says_so(db, lara):
    st = Settings(_env_file=None, auto_refresh=False, answer_max_usd=0.01)
    calls = []

    def costly(system, prompt, schema):
        calls.append(prompt)
        # $0.25 with Gemini prices: more than the whole budget in one call
        return {"statements": [], "luecken": ["x"]}, {"input": 1_000_000, "output": 0}

    e = eng.GeminiAnswerEngine(st, llm=costly, support_checker=lambda pairs: [1.0] * len(pairs))
    with budget.answering(st) as b:
        e._ask_model("erste Frage")
        with pytest.raises(budget.AnswerBudgetExceeded):
            e._ask_model("zweite Frage")
    assert len(calls) == 1 and b.refused == 1
    res = e.answer("Was macht Lara Testalder?", person_ids=[lara], use_cache=False)
    assert res.kosten_usd == pytest.approx(0.25, abs=0.01)
    with Session(db) as s:  # booked once
        assert _ledger(s, llm_text.cost_source(st, "answer")) == pytest.approx(0.25, abs=0.01)


# --- group questions: persons capped and ranked -----------------------------------------------------------


@pytest.fixture
def team_of_20(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        ids = []
        now = datetime.now(UTC)
        for i in range(20):
            a = repo.upsert_athlete(s, sport, f"Spielerin Nummer{i:02d}", nationality="SUI")
            # the last persons' documents fit the question best (word match: "verletzt")
            words = "ist verletzt und fällt aus" if i >= 15 else "ist fit und spielt gut"
            item = m.Item(
                source_type="srf",
                platform="rss",
                url=f"https://srf/{i}",
                title=f"Nummer{i:02d}",
                text=f"Spielerin Nummer{i:02d} {words}.",
                content_hash=f"h{i}",
                published_at=now - timedelta(hours=i),
            )
            s.add(item)
            s.flush()
            item.athletes.append(m.ItemAthlete(athlete_id=a.id))
            ids.append(a.id)
        s.commit()
        return ids


class GroupEngine:
    def __init__(self, settings):
        self.settings = settings
        self.groups = []

    def _write(self, question, docs, who):
        self.groups.append(who)
        return [], [], [], 0, {}


def test_group_question_reads_the_most_relevant_persons_first(db, team_of_20):
    e = GroupEngine(Settings(_env_file=None, answer_max_persons=8))
    with Session(db) as s:
        _ok, _u, missing, _d, _us, _n, skipped = cross.block_evidence(
            e, s, "Wer ist verletzt?", team_of_20, None
        )
    assert len(e.groups) == 1 and len(e.groups[0]) == 8
    assert {f"Spielerin Nummer{i:02d}" for i in range(15, 20)} <= set(e.groups[0])  # best match first
    assert skipped and not set(skipped) & set(e.groups[0]) and not set(missing) & set(skipped)


def test_group_question_writer_calls_limited_by_the_budget(db, team_of_20):
    st = Settings(_env_file=None, answer_max_persons=20, answer_max_usd=0.0001)
    e = GroupEngine(st)
    with Session(db) as s, budget.answering(st):
        *_, skipped = cross.block_evidence(e, s, "Wer ist verletzt?", team_of_20, None)
    assert len(e.groups) == 1  # the first writer call always runs, the next ones only when affordable
    assert len(skipped) == 20 - len(e.groups[0])


def test_cross_route_names_the_persons_left_out(db, team_of_20, monkeypatch):
    monkeypatch.setattr(cross, "persons_for", lambda s, q, form: (team_of_20, "Nati (20)", [team_of_20]))
    e = GroupEngine(Settings(_env_file=None, answer_max_persons=8))
    with Session(db) as s:
        out = cross.try_answer(e, s, "Wer ist verletzt?", [], None, None, False)
    gaps = out[2]
    assert any(g.startswith("Nicht ausgewertet") for g in gaps)


# --- game questions about a tournament ------------------------------------------------------------------------


@pytest.fixture
def tournament(db):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        teams = {}
        for ioc, name in (("SUI", "Schweiz"), ("FIN", "Finnland"), ("CZE", "Tschechien")):
            t = m.Team(sport_id=sport.id, key=f"{ioc.lower()}-women", name=name, kind="national", country=ioc)
            s.add(t)
            teams[ioc] = t
        s.flush()
        start = datetime.now(UTC) - timedelta(days=20)
        ev = m.Event(
            key="eht-2026-testhausen",
            name="Euro Hockey Tour, Testhausen",
            kind="tournament",
            starts_at=start,
            ends_at=start + timedelta(days=4),
            city="Testhausen (FIN)",
        )
        s.add(ev)
        s.flush()
        for t in teams.values():
            s.add(m.EventTeam(event_id=ev.id, team_id=t.id))
        games = [
            (
                "FIN",
                "SUI",
                "4:1",
                "Tore: 1:0 Anna Finn. Torhüterinnen: Saskia Testmaurer (Schweiz, 30 Paraden).",
            ),
            ("SUI", "CZE", "3:2 n.P.", "Tore: 3:2 Penalty-Schiessen Leoni Testbalzer."),
            ("FIN", "CZE", "2:0", "Tore: 1:0 Anna Finn."),
        ]
        for i, (h, a, score, report) in enumerate(games):
            url = f"https://results/game/{i}"
            s.add(
                m.Event(
                    key=f"g-{i}",
                    name=f"{teams[h].name} – {teams[a].name}",
                    kind="game",
                    starts_at=start + timedelta(days=i + 1),
                    status="final",
                    score=score,
                    home_team_id=teams[h].id,
                    away_team_id=teams[a].id,
                    venue="Testarena",
                    source_url=url,
                    source="Leijonat",
                )
            )
            s.add(
                m.Item(
                    source_type="official_result",
                    platform="results",
                    source_name="Leijonat",
                    url=url,
                    title=f"{teams[h].name} – {teams[a].name} {score}",
                    text=f"{teams[h].name} – {teams[a].name} {score}. {report}",
                    content_hash=f"r{i}",
                    published_at=start + timedelta(days=i + 1),
                )
            )
        s.commit()
        return ev.id


def test_game_question_finds_the_named_teams_games(db, tournament):
    with Session(db) as s:
        found = event_games.find(s, "Wie hat die Schweiz in Testhausen gespielt?")
        assert found is not None and found.team.name == "Schweiz" and len(found.games) == 2
        assert found.docs[0].computed and "4:1" in found.docs[0].text and "3:2 n.P." in found.docs[0].text
        assert any("Saskia Testmaurer" in d.text for d in found.docs)
        # with the tool's event argument, no city in the question
        found = event_games.find(s, "Wer stand im Tor?", event_id=tournament)
        assert found is not None and len(found.games) == 3
        assert event_games.find(s, "Wie war die Stimmung in Testhausen?") is None  # not a game question
        assert event_games.find(s, "Kader für Testhausen?") is None


def test_mcp_game_question_skips_the_cross_route(db, tournament, monkeypatch):
    def no_cross(*a, **k):
        raise AssertionError("game questions about a tournament do not read every player")

    monkeypatch.setattr(cross, "try_answer", no_cross)
    prompts = []

    def llm(system, prompt, schema):
        prompts.append(prompt)
        return {
            "statements": [
                {
                    "aussage": "Saskia Testmaurer stand gegen Finnland im Tor.",
                    "zitat": "Torhüterinnen: Saskia Testmaurer (Schweiz, 30 Paraden)",
                    "dok": "D2",
                    "kategorie": "resultat",
                }
            ],
            "luecken": [],
        }, {}

    e = eng.GeminiAnswerEngine(SETTINGS, llm=llm, support_checker=lambda pairs: [1.0] * len(pairs))
    res = e.answer("Wer stand bei der Schweiz in Testhausen im Tor?", use_cache=False)
    assert len(prompts) == 1 and "Testarena" in prompts[0]
    assert res.statements and "Testmaurer" in res.statements[0].aussage
    assert "Spiele" in (res.scope_hinweis or "") and "2 Spiele" in res.durchsucht
