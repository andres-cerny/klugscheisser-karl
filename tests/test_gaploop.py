"""MCP gap loop: answer logging, stand-in client (fakes, no API calls), run report/compare, read-only lookup."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.gaploop import have, standin
from klugscheisser.gaploop.report import compare_report, run_report

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_gap_questions.yaml"


# --- 1. answer logging ---------------------------------------------------------------------------------------


def test_tool_answer_is_logged_on_one_line(db, caplog):
    from klugscheisser.mcp_server import server

    with caplog.at_level(logging.INFO, logger="klugscheisser.tools"):
        server.list_events()
    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("tool list_events answer: ")
    )
    assert "\n" not in line
    assert len(line) <= len("tool list_events answer: ") + server.ANSWER_PREVIEW_CHARS + 1


def test_answer_preview_trims_and_flattens():
    from mcp.types import CallToolResult, TextContent

    from klugscheisser.mcp_server.server import answer_preview

    r = CallToolResult(content=[TextContent(type="text", text="Datenstand: heute\n\n- a\n" + "x" * 2000)])
    p = answer_preview(r)
    assert p.startswith("Datenstand: heute ⏎ - a ⏎ xxx") and p.endswith("…") and len(p) == 601
    assert answer_preview("kurz") == "kurz" and answer_preview({"a": 1}) == '{"a": 1}'


# --- 2. stand-in client with fakes --------------------------------------------------------------------------


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict = field(default_factory=dict)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in asdict(self).items() if v is not None}


def resp(blocks, stop, inp=1000, out=200):
    usage = SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


class FakeBackend:
    server_instructions = "Verified data layer."

    def __init__(self):
        self.called = []

    async def list_tools(self):
        return [
            {"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}
            for n in ("get_athlete", "frage", "hinzufuegen", "refresh", "refresh_status")
        ]

    async def call(self, name, args):
        self.called.append((name, args))
        return f"Datenstand: 17.10.2026\n{name} für {args.get('name')}", False


def _run(cap=0.5):
    return standin.Run(
        started_at="2026-10-17T12:00:00+00:00",
        model=standin.MODEL,
        effort="medium",
        server="fake",
        project="eishockey",
        questions_file="x.yaml",
        cap_usd=cap,
        instructions="Du hilfst einer Sportmoderatorin.",
    )


def test_standin_records_tools_answer_cost_and_blocks_writes():
    client = FakeClient(
        [
            resp(
                [
                    Block("text", text="Ich schaue nach."),
                    Block("tool_use", id="t1", name="get_athlete", input={"name": "Alina Müller"}),
                    Block("tool_use", id="t2", name="hinzufuegen", input={"name": "X"}),
                ],
                "tool_use",
            ),
            resp([Block("text", text="Datenstand: 17.10.2026\nAlina spielt in Hamilton.")], "end_turn"),
        ]
    )
    backend = FakeBackend()
    run = anyio.run(
        standin.run_questions, client, backend, [{"id": "q1", "question": "Wo spielt Alina Müller?"}], _run()
    )
    q = run.questions[0]
    offered = {t["name"] for t in client.calls[0]["tools"]}
    assert offered == {"get_athlete", "frage"}  # writing / refresh tools are never offered
    assert "Klugscheisser Karl" in client.calls[0]["system"] and "KEINE Websuche" in client.calls[0]["system"]
    assert "Verified data layer." in client.calls[0]["system"]
    assert backend.called == [("get_athlete", {"name": "Alina Müller"})]  # blocked tool not executed
    assert [(c.name, c.blocked) for c in q.tool_calls] == [("get_athlete", False), ("hinzufuegen", True)]
    assert "get_athlete für Alina Müller" in q.tool_calls[0].output
    # tool results go back in ONE user message
    last = client.calls[1]["messages"][2]  # (the list is shared, the final answer is appended later)
    assert last["role"] == "user" and len(last["content"]) == 2 and last["content"][1]["is_error"] is True
    assert q.answer.endswith("Alina spielt in Hamilton.") and q.stop == "end_turn" and q.turns == 2
    expected = 2 * (1000 * 2.0 + 200 * 10.0) / 1e6
    assert q.cost_usd == pytest.approx(expected) and run.cost_usd == pytest.approx(expected)
    assert q.tokens == {"input": 2000, "output": 400, "cache_read": 0, "cache_write": 0}


def test_cost_cap_stops_before_the_call():
    client = FakeClient([])
    run = anyio.run(
        standin.run_questions, client, FakeBackend(), [{"id": "q1", "question": "Hallo?"}], _run(cap=0.001)
    )
    assert client.calls == [] and run.questions[0].stop.startswith("cost_cap")


def test_report_compare_and_roundtrip(tmp_path):
    run = _run()
    run.questions = [
        standin.QuestionRun(
            id="q1",
            question="Wo spielt Alina?",
            tool_calls=[standin.ToolCall("get_athlete", {"name": "Alina"}, "x" * 5000, 1.2)],
            answer="In Hamilton.",
            duration_s=3.0,
            tokens={"input": 10, "output": 5},
            cost_usd=0.01,
            turns=2,
            stop="end_turn",
        )
    ]
    jp, mp = standin.save(run, tmp_path / "a")
    md = mp.read_text()
    assert "## q1: Wo spielt Alina?" in md and '`get_athlete` `{"name": "Alina"}`' in md
    assert "more chars in run.json" in md and "In Hamilton." in md and "median answer time 3.0 s" in md
    back = standin.load_run(jp)
    assert back.questions[0].tool_calls[0].output == "x" * 5000
    other = standin.load_run(jp)
    other.questions[0].answer = "Bei den Boston Fleet."
    cmp = compare_report(back, other)
    assert "In Hamilton." in cmp and "Bei den Boston Fleet." in cmp and "### A" in cmp and "### B" in cmp
    assert run_report(back) == md


def test_cli_compare_is_free(tmp_path, capsys):
    run = _run()
    run.questions = [standin.QuestionRun(id="q1", question="?", answer="A")]
    standin.save(run, tmp_path / "a")
    standin.save(run, tmp_path / "b")
    assert standin.main(["compare", str(tmp_path / "a"), str(tmp_path / "b")]) == 0
    assert "# Compare stand-in runs" in capsys.readouterr().out


def test_target_default_live_loop_and_custom(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg:///live")
    assert standin.resolve_target(None, None, False, "eishockey") == (
        standin.DEFAULT_URL,
        "postgresql+psycopg:///live",
    )
    # loop: its own port and the copy database, never the live ledger
    assert standin.resolve_target(None, None, True, "eishockey") == (
        standin.LOOP_URL,
        standin.LOOP_DATABASE_URL,
    )
    assert ":8020/" in standin.LOOP_URL and "klugscheisser_loop" in standin.LOOP_DATABASE_URL
    # another endpoint without --database-url: no ledger delta (it would read the wrong database)
    assert standin.resolve_target("http://127.0.0.1:9999/mcp/eishockey", None, False, "eishockey")[1] is None
    other = standin.resolve_target("http://x/mcp/eishockey", "postgresql+psycopg:///y", False, "eishockey")
    assert other[1] == "postgresql+psycopg:///y"
    with pytest.raises(SystemExit):
        standin.resolve_target(None, None, True, "allgemein")


def test_seed_questions_file():
    data = yaml.safe_load(FIXTURE.read_text())
    ids = [q["id"] for q in data["questions"]]
    assert len(ids) == len(set(ids)) and all(q["question"].strip() for q in data["questions"])
    assert sum(q["origin"].startswith("perplexity-log") for q in data["questions"]) == 6
    assert [q["id"] for q in standin.load_questions(FIXTURE, only=["p01", "s01"])] == ["p01", "s01"]


def test_live_instructions_are_packaged():
    text_ = standin.INSTRUCTIONS["eishockey"].read_text()
    assert "Klugscheisser Karl" in text_ and len(text_) < 1000


# --- 3. read-only lookup ------------------------------------------------------------------------------------


@pytest.fixture
def stored(db):
    now = datetime.now(UTC)
    with session_scope() as s:
        sport = repo.get_sport(s, "icehockey_women")
        sui = m.Team(sport_id=sport.id, key="sui-women", name="Schweiz", kind="national", country="SUI")
        aut = m.Team(sport_id=sport.id, key="aut-women", name="Österreich", kind="national", country="AUT")
        s.add_all([sui, aut])
        s.flush()
        s.add(
            m.Event(
                key="g-old",
                name="SUI-AUT",
                kind="game",
                status="final",
                score="3:1",
                starts_at=now - timedelta(hours=30),
                home_team_id=sui.id,
                away_team_id=aut.id,
                source="IIHF",
            )
        )
        s.add(
            m.Event(
                key="g-next",
                name="AUT-SUI",
                kind="game",
                status="scheduled",
                starts_at=now + timedelta(days=3),
                home_team_id=aut.id,
                away_team_id=sui.id,
                source="IIHF",
            )
        )
        a = repo.upsert_athlete(s, sport, "Testa Muster")
        s.flush()
        s.add(m.Affiliation(athlete_id=a.id, team_id=sui.id, season="2026-27"))
        item = m.Item(
            source_type="srf",
            platform="rss",
            source_name="SRF",
            url="https://srf.ch/1",
            title="Testa Muster verletzt",
            published_at=now - timedelta(days=1),
            content_hash="h1",
        )
        s.add(item)
        s.flush()
        s.add(m.ItemAthlete(item_id=item.id, athlete_id=a.id))
        s.add(m.ItemTeam(item_id=item.id, team_id=sui.id))
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind="injury",
                value={"text": "Knie"},
                value_text="Knieverletzung",
                source="srf",
                source_url="https://srf.ch/1",
            )
        )
    return now


def test_lookup_shows_stored_rows(stored, engine):
    eng = have.open_readonly(str(engine.url.render_as_string(hide_password=False)))
    try:
        with Session(eng) as s:
            d = have.lookup(s, persons=["Testa Muster", "Niemand Unbekannt"], teams=["Schweiz"])
    finally:
        eng.dispose()
    assert d["overview"]["hours_since_latest_swiss_game"] == pytest.approx(30, abs=0.2)
    t = d["teams"][0]
    assert t["latest_games"][0]["score"] == "3:1" and t["next_games"][0]["home"] == "Österreich"
    assert t["current_members"]["count"] == 1 and t["news_in_window"][0]["title"] == "Testa Muster verletzt"
    p = d["persons"][0]
    assert p["injury_facts"][0]["value"] == "Knieverletzung" and p["items_total"] == 1
    assert d["persons"][1]["found"] is False
    md = have.render(d)
    assert "## Team: Schweiz" in md and "Knieverletzung" in md and "**not found**" in md


def test_readonly_connection_refuses_writes(db, engine):
    eng = have.open_readonly(str(engine.url.render_as_string(hide_password=False)))
    try:
        with eng.connect() as c, pytest.raises(Exception, match="read-only"):
            c.execute(text("INSERT INTO sports (key, name) VALUES ('x', 'x')"))
        with Session(eng) as s:
            have.lookup(s)  # rolls back; the session stays usable
            assert s.scalar(text("SELECT 1")) == 1
    finally:
        eng.dispose()
