"""v17: Claude Sonnet writes all texts when ANTHROPIC_API_KEY is set (llm/text.py). Fake Anthropic client only,
no real API call."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.llm import text as llm_text

MEDIA_NULL = {"type": "object", "properties": {"v": {"type": ["string", "null"]}}}
CLAUDE = Settings(_env_file=None, anthropic_api_key="k", gemini_api_key="g")
GEMINI_ONLY = Settings(_env_file=None, gemini_api_key="g")


class FakeClaude:
    """Stands in for anthropic.Anthropic: records each request, answers with the next canned reply."""

    def __init__(self, *replies, stop_reason="end_turn", usage=None, error=None):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.stop_reason, self.error = stop_reason, error
        self.usage = usage or SimpleNamespace(
            input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        text = self.replies.pop(0) if self.replies else "{}"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber"),
            usage=self.usage,
        )


@pytest.fixture
def fake(monkeypatch):
    def install(*replies, **kw):
        client = FakeClaude(*replies, **kw)
        monkeypatch.setattr(llm_text, "_claude_client", lambda settings: client)
        return client

    return install


# --- choice of provider / model / price ----------------------------------------------------------------------


def test_claude_when_its_key_is_set_and_flip_back_without_code():
    assert llm_text.provider(CLAUDE) == "claude" and llm_text.available(CLAUDE)
    assert (
        llm_text.model(CLAUDE) == "claude-sonnet-5-5"
        and llm_text.model(CLAUDE, "text") == "claude-sonnet-5-5"
    )
    assert llm_text.cost_source(CLAUDE, "answer") == "llm:claude-answer"
    back = CLAUDE.model_copy(update={"llm_provider": "gemini"})
    assert llm_text.provider(back) == "gemini" and llm_text.model(back) == back.gemini_answer_model
    assert llm_text.provider(GEMINI_ONLY) == "gemini" and llm_text.cost_source(GEMINI_ONLY, "facts") == (
        "llm:gemini-facts"
    )
    none = Settings(_env_file=None)
    assert not llm_text.available(none)
    assert not llm_text.available(Settings(_env_file=None, gemini_api_key="g", llm_provider="claude"))


def test_sonnet_cost_counts_cache_reads_and_writes():
    usage = {"input": 10_000, "cached": 3_000, "cache_write": 1_000, "output": 1_000}
    expected = (6_000 * 2.0 + 3_000 * 0.20 + 1_000 * 2.50 + 1_000 * 10.0) / 1e6
    assert llm_text.cost_usd(CLAUDE, usage) == pytest.approx(expected)
    # Gemini prices unchanged
    assert llm_text.cost_usd(GEMINI_ONLY, {"input": 4000, "output": 600}) == pytest.approx(
        4000 / 1e6 * 0.25 + 600 / 1e6 * 1.5
    )


def test_all_json_schemas_map_to_claude_structured_outputs():
    from klugscheisser import translate
    from klugscheisser.adds.team import INTERPRET_SCHEMA
    from klugscheisser.answer import agent, engine, interpret
    from klugscheisser.classify.router import LLM_SCHEMA
    from klugscheisser.facts import extract
    from klugscheisser.media.analyzer import MEDIA_SCHEMA

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    for schema in (
        translate.SCHEMA,
        INTERPRET_SCHEMA,
        agent.PLAN_SCHEMA,
        engine.SCHEMA,
        engine.SCHEMA_PROSA,
        engine.SCHEMA_REPAIR,
        interpret.SCHEMA,
        LLM_SCHEMA,
        extract.SCHEMA,
        MEDIA_SCHEMA,
    ):
        strict = llm_text.strict_schema(schema)
        assert all(o.get("additionalProperties") is False for o in objects(strict))


# --- the call ------------------------------------------------------------------------------------------------


def test_json_call_uses_structured_output_low_effort_and_caches_long_system(fake):
    client = fake('{"uebersetzungen": ["Hallo"]}')
    long_system = "Regel. " * 400
    out, usage = llm_text.generate(CLAUDE, long_system, "Texte: [1] Hej", schema={"type": "object",
        "properties": {"uebersetzungen": {"type": "array", "items": {"type": "string"}}},
        "required": ["uebersetzungen"]}, temperature=0.1)  # fmt: skip
    assert out == {"uebersetzungen": ["Hallo"]}
    req = client.calls[0]
    assert req["model"] == "claude-sonnet-5-5" and "temperature" not in req
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert llm_text.strict_schema(MEDIA_NULL)["properties"]["v"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default" and req["messages"] == [
        {"role": "user", "content": "Texte: [1] Hej"}
    ]
    assert usage == {"input": 1000, "cached": 0, "cache_write": 0, "output": 200}

    fake("kurz")
    text, _ = llm_text.generate(CLAUDE, "Kurz.", "Hallo")
    assert text == "kurz"


def test_usage_includes_cache_and_short_system_is_marked_too(fake):
    usage = SimpleNamespace(
        input_tokens=500, output_tokens=50, cache_read_input_tokens=2500, cache_creation_input_tokens=0
    )
    client = fake('{"a": 1}', usage=usage)
    _, u = llm_text.generate(
        CLAUDE, "Kurz.", "x", schema={"type": "object", "properties": {"a": {"type": "integer"}}}
    )
    # every system prompt is marked: the API itself skips prefixes under 512 tokens at no cost
    assert client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert u == {"input": 3000, "cached": 2500, "cache_write": 0, "output": 50}


def test_prompt_cache_switch_and_min_chars(fake):
    off = Settings(_env_file=None, anthropic_api_key="k", llm_prompt_cache=False)
    client = fake("a", "b", "c")
    llm_text.generate(off, "Regel. " * 400, "x")
    assert client.calls[0]["system"] == [{"type": "text", "text": "Regel. " * 400}]
    assert "cache_control" not in client.calls[0]["messages"][0]  # never on the varying part
    assert "cache_control" not in client.calls[0]
    picky = Settings(_env_file=None, anthropic_api_key="k", anthropic_cache_min_chars=100)
    llm_text.generate(picky, "Kurz.", "x")
    llm_text.generate(picky, "Lang. " * 30, "x")
    assert "cache_control" not in client.calls[1]["system"][0]
    assert client.calls[2]["system"][0]["cache_control"] == {"type": "ephemeral"}
    # the user message (question, documents) is a plain string: no breakpoint, no write premium on it
    assert client.calls[2]["messages"] == [{"role": "user", "content": "x"}]


def test_cost_counts_cache_reads_and_writes_at_their_prices():
    # Sonnet 5.5: input $2, cache write (5 min) $2.50, cache read $0.20, output $10 per 1M tokens
    first = {"input": 3000, "cached": 0, "cache_write": 2000, "output": 100}
    repeat = {"input": 3000, "cached": 2000, "cache_write": 0, "output": 100}
    uncached = {"input": 3000, "cached": 0, "cache_write": 0, "output": 100}
    assert llm_text.cost_usd(CLAUDE, first) == pytest.approx((1000 * 2.0 + 2000 * 2.5 + 100 * 10.0) / 1e6)
    assert llm_text.cost_usd(CLAUDE, repeat) == pytest.approx((1000 * 2.0 + 2000 * 0.2 + 100 * 10.0) / 1e6)
    assert llm_text.cost_usd(CLAUDE, uncached) == pytest.approx((3000 * 2.0 + 100 * 10.0) / 1e6)
    # usage straight from the API: input_tokens excludes the cached and written parts
    u = llm_text.claude_usage(
        SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=2000
        )
    )
    assert u == first


def test_cost_ledger_books_cached_price(fake, db):
    from klugscheisser.answer.engine import LlmAnswerEngine

    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000, cache_creation_input_tokens=0
    )
    fake('{"statements": [], "luecken": []}', usage=usage)
    e = LlmAnswerEngine(CLAUDE)
    _, u = e._ask_model("Frage: x")
    assert e.last_usage["cached"] == 3000 and e.last_usage["cache_write"] == 0
    with Session(db) as s:
        e._record_cost(s, u)
        row = s.scalar(select(m.CostLedger))
        assert row.cost_usd == pytest.approx((1000 * 2.0 + 3000 * 0.2 + 200 * 10.0) / 1e6)


def test_refusal_cut_off_and_empty_credit(fake):
    fake("{}", stop_reason="refusal")
    with pytest.raises(llm_text.LlmRefusal):
        llm_text.generate(CLAUDE, "s", "p", schema={"type": "object", "properties": {}})
    fake('{"a": ', stop_reason="max_tokens")
    with pytest.raises(ValueError):
        llm_text.generate(CLAUDE, "s", "p", schema={"type": "object", "properties": {}})
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.BadRequestError("Your credit balance is too low", response=response, body=None)
    fake(error=err)
    with pytest.raises(llm_text.DailyQuotaExceeded):
        llm_text.generate(CLAUDE, "s", "p")


# --- the users ----------------------------------------------------------------------------------------------


def test_answer_engine_asks_claude_and_books_claude_cost(fake, db):
    from klugscheisser.answer.engine import LlmAnswerEngine

    client = fake('{"statements": [], "luecken": ["nichts"]}')
    e = LlmAnswerEngine(CLAUDE)
    assert e._use_interpreter()  # interpreter is on with Claude alone (no Gemini key needed)
    out, usage = e._ask_model("Frage: x")
    assert out["luecken"] == ["nichts"] and client.calls[0]["model"] == "claude-sonnet-5-5"
    assert e.last_usage["calls"] == 1 and e.last_usage["input"] == 1000
    with Session(db) as s:
        cost = e._record_cost(s, usage)
        row = s.scalar(select(m.CostLedger))
        assert row.source == "llm:claude-answer"
        assert row.cost_usd == pytest.approx(cost) == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1e6)


def test_agent_budget_uses_sonnet_prices():
    from klugscheisser.answer.agent import AgentRun

    run = AgentRun(llm_usage={"input": 2000, "output": 300})
    assert run.cost(CLAUDE) == pytest.approx((2000 * 2.0 + 300 * 10.0) / 1e6)
    assert run.cost(GEMINI_ONLY) == pytest.approx((2000 * 0.25 + 300 * 1.5) / 1e6)


def test_translation_with_claude(fake, db):
    from klugscheisser import translate

    fake(json.dumps({"uebersetzungen": ["Sie hat gewonnen."]}))
    with Session(db) as s:
        assert translate.ensure(s, CLAUDE, [("Hon vann.", "sv")]) == 1
        row = s.scalar(select(m.Translation))
        assert row.text_de == "Sie hat gewonnen." and row.model == "claude-sonnet-5-5"
        assert s.scalar(select(m.CostLedger.source)) == "llm:claude-translate"


def test_fact_extraction_call_goes_to_claude(fake):
    from klugscheisser.facts.extract import cost_source, model_llm

    client = fake('{"fakten": []}')
    out, _ = model_llm(CLAUDE)(
        "System", "Dokumente", {"type": "object", "properties": {"fakten": {"type": "array"}}}
    )
    assert out == {"fakten": []} and cost_source(CLAUDE) == "llm:claude-facts"
    assert client.calls[0]["model"] == CLAUDE.anthropic_model


def test_pronunciation_hint_and_briefing_with_claude(fake):
    from klugscheisser import briefing, pronounce

    fake("KLAA-ra PESCH-la-ro-wa\nzweite Zeile")
    hint, usage = pronounce.write_hint(CLAUDE, "Klára Peslarová", "cs", None)
    assert hint == "KLAA-ra PESCH-la-ro-wa" and usage["output"] == 200
    fake("- Sie fragt oft nach Statistiken.")
    assert briefing._model_call(CLAUDE)("sys", "prompt") == "- Sie fragt oft nach Statistiken."


def test_classification_fallback_provider_is_sonnet():
    from klugscheisser.llm.providers import ClaudeProvider, get_llm

    client = FakeClaude('{"relevant": true}')
    p = ClaudeProvider(CLAUDE, client=client)
    assert p.complete_json("s", "u", {"type": "object", "properties": {"relevant": {"type": "boolean"}}}) == {
        "relevant": True
    }
    assert client.calls[0]["model"] == "claude-sonnet-5-5"
    assert isinstance(get_llm(CLAUDE), ClaudeProvider)


def test_gemini_stays_for_embeddings_tts_and_reels():
    from klugscheisser.embed.providers import get_embedder
    from klugscheisser.media.analyzer import GeminiMediaAnalyzer, get_media_analyzer

    st = CLAUDE.model_copy(update={"embedding_provider": "gemini"})
    assert type(get_embedder(st)).__name__ == "GeminiEmbedder"
    assert isinstance(get_media_analyzer(CLAUDE), GeminiMediaAnalyzer)  # reels need native video
