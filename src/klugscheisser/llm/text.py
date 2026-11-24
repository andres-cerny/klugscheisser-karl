"""v17: the one place where text (and JSON) is written by a language model — Claude Sonnet or Gemini.

Used by the answer engine (writer, question interpreter, agent planner), translations, fact extraction, fun facts,
the briefing suggestion and the pronunciation hint. Which model: `LLM_PROVIDER` (auto | claude | gemini; auto =
Claude when ANTHROPIC_API_KEY is set, else Gemini). Flip back to Gemini with `LLM_PROVIDER=gemini`, no code change.

Not here (stay on Gemini, Anthropic has no equivalent): embeddings (embed/providers.py), the computer voice
(pronounce.tts) and reel/video analysis (media/analyzer.py). Jev (TypeSafe) is separate and unchanged.

Usage dicts (same keys for both providers): input = all prompt tokens (incl. cached), cached = read from the
prompt cache, cache_write = written to the prompt cache (Claude only), output = answer + thinking tokens.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from klugscheisser.config import Settings

log = logging.getLogger(__name__)

MAX_TOKENS = {"answer": 8000, "text": 8000}


class DailyQuotaExceeded(Exception):
    """The model's daily quota (Gemini) or the account's credit (Claude) is used up: stop, try tomorrow."""


class LlmRefusal(Exception):
    """Claude declined (stop_reason "refusal")."""


# --- which provider / model -------------------------------------------------------------------------------


def provider(settings: Settings) -> str:
    """ "claude" or "gemini" for text calls (LLM_PROVIDER; auto/fake = Claude when its key is set)."""
    choice = (settings.llm_provider or "auto").strip().lower()
    if choice in ("claude", "gemini"):
        return choice
    return "claude" if settings.features["claude"] else "gemini"


def available(settings: Settings) -> bool:
    """Is the text model usable (its key set)?"""
    return bool(settings.features[provider(settings)])


def model(settings: Settings, role: str = "answer") -> str:
    """role "answer": answers, interpreter, agent, translations, hints; "text": fact extraction."""
    if provider(settings) == "claude":
        return settings.anthropic_answer_model if role == "answer" else settings.anthropic_model
    return settings.gemini_answer_model if role == "answer" else settings.gemini_text_model


def cost_source(settings: Settings, kind: str) -> str:
    """Cost ledger source, e.g. "llm:claude-answer" / "llm:gemini-answer" (kind: answer, facts, translate)."""
    return f"llm:{provider(settings)}-{kind}"


def cost_usd(settings: Settings, usage: dict | None, which: str | None = None) -> float:
    """USD for one usage dict (prices per 1M tokens in the settings)."""
    u = usage or {}
    inp, out = int(u.get("input", 0) or 0), int(u.get("output", 0) or 0)
    if (which or provider(settings)) == "claude":
        cached, write = int(u.get("cached", 0) or 0), int(u.get("cache_write", 0) or 0)
        fresh = max(0, inp - cached - write)
        return (
            fresh * settings.anthropic_input_usd_per_m
            + cached * settings.anthropic_cache_read_usd_per_m
            + write * settings.anthropic_cache_write_usd_per_m
            + out * settings.anthropic_output_usd_per_m
        ) / 1e6
    return (
        inp * settings.gemini_answer_input_usd_per_m + out * settings.gemini_answer_output_usd_per_m
    ) / 1e6


def add_usage(total: dict, usage: dict | None) -> dict:
    for k, v in (usage or {}).items():
        if isinstance(v, int | float):
            total[k] = total.get(k, 0) + int(v)
    return total


# --- calls ------------------------------------------------------------------------------------------------


def generate(
    settings: Settings,
    system: str,
    prompt: str,
    *,
    schema: dict | None = None,
    role: str = "answer",
    temperature: float | None = None,
    max_tokens: int | None = None,
    wait_on_rate_limit: bool = False,
) -> tuple[Any, dict]:
    """One model call. With `schema`: the parsed JSON dict, else the text. Returns (result, usage).

    temperature is used by Gemini only (Claude Sonnet 5.5 accepts no sampling parameters).
    wait_on_rate_limit: Gemini per-minute 429s are waited out (bulk jobs); a daily quota raises DailyQuotaExceeded.
    """
    if provider(settings) == "claude":
        content, usage = claude_message(
            settings,
            [{"role": "user", "content": prompt}],
            system=system,
            schema=schema,
            model_id=model(settings, role),
            max_tokens=max_tokens or MAX_TOKENS.get(role, 8000),
        )
    else:
        content, usage = _gemini(settings, system, prompt, schema, role, temperature, wait_on_rate_limit)
    return (json.loads(content) if schema is not None else content), usage


_clients: dict[str, Any] = {}


def _claude_client(settings: Settings):
    import anthropic

    key = settings.anthropic_api_key.get_secret_value()
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = anthropic.Anthropic(api_key=key, max_retries=4)
    return client


def claude_message(
    settings: Settings,
    messages: list[dict],
    *,
    system: str | None = None,
    schema: dict | None = None,
    model_id: str | None = None,
    max_tokens: int = 4000,
    client=None,
) -> tuple[str, dict]:
    """One Claude Messages call → (text, usage). JSON via structured outputs (`output_config.format`), system
    prompts are cached (`system_block`), adaptive thinking at `anthropic_effort`, server-side fallback on a
    policy refusal. `client` is injectable for tests."""
    import anthropic

    from klugscheisser.llm import budget

    budget.check("claude")  # inside an answer: per-question budget and daily cap (llm/budget.py)
    client = client or _claude_client(settings)
    output_config: dict[str, Any] = {"effort": settings.anthropic_effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
    kwargs: dict[str, Any] = {}
    if system:
        kwargs["system"] = [system_block(settings, system)]
    started = time.monotonic()
    try:
        response = client.beta.messages.create(
            model=model_id or settings.anthropic_model,
            max_tokens=max_tokens,
            messages=messages,
            output_config=output_config,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    except anthropic.BadRequestError as exc:
        if "credit balance" in str(exc).lower():
            raise DailyQuotaExceeded(str(exc)[:300]) from exc
        raise
    if response.stop_reason == "refusal":
        raise LlmRefusal(getattr(response.stop_details, "category", None) or "refusal")
    usage = claude_usage(response.usage)
    log.info(
        "claude call %s: %.1fs, input %d (cache read %d, cache write %d), output %d, $%.5f",
        model_id or settings.anthropic_model,
        time.monotonic() - started,
        usage["input"],
        usage["cached"],
        usage["cache_write"],
        usage["output"],
        cost_usd(settings, usage, "claude"),
    )
    budget.charge_llm(settings, usage, "claude")
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    if response.stop_reason == "max_tokens" and schema is not None:
        raise ValueError(f"Claude answer cut off at max_tokens={max_tokens}")
    return text, usage


def system_block(settings: Settings, system: str) -> dict:
    """The system prompt as one text block, marked for the prompt cache (`LLM_PROMPT_CACHE`, default on).

    The breakpoint sits on the system prompt, the fixed part of every route (the output schema is rendered with
    it and cached too: measured 2026-10-27); the user message (question, documents) varies per call and stays
    uncached, so no call pays the cache-write premium on text that is never read again. A repeat of the same route within 5 minutes
    reads the prefix at 10 % of the input price; the first call pays 125 % for it (break-even: 2 calls). Prefixes
    under the model's minimum (512 tokens on Sonnet 5.5) are silently not cached and cost nothing extra."""
    block: dict[str, Any] = {"type": "text", "text": system}
    if settings.llm_prompt_cache and len(system) >= settings.anthropic_cache_min_chars:
        block["cache_control"] = {"type": "ephemeral"}  # 5-minute TTL
    return block


def strict_schema(schema: dict) -> dict:
    """A Gemini-style JSON schema as Claude's structured outputs want it: every object closed
    (additionalProperties false), type lists like ["string", "null"] as anyOf, unsupported limits moved into the
    description (anthropic.transform_schema)."""
    import anthropic

    def lists_to_any_of(node):
        if isinstance(node, list):
            return [lists_to_any_of(x) for x in node]
        if not isinstance(node, dict):
            return node
        node = {k: lists_to_any_of(v) for k, v in node.items()}
        if isinstance(node.get("type"), list):
            types = node.pop("type")
            node["anyOf"] = [{**node, "type": t} for t in types]
            node = {
                "anyOf": node["anyOf"],
                **({"description": node["description"]} if "description" in node else {}),
            }
        return node

    return anthropic.transform_schema(lists_to_any_of(schema))


def claude_usage(u) -> dict:
    cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return {
        "input": int(getattr(u, "input_tokens", 0) or 0) + cached + write,
        "cached": cached,
        "cache_write": write,
        "output": int(getattr(u, "output_tokens", 0) or 0),
    }


def _gemini(
    settings: Settings,
    system: str,
    prompt: str,
    schema: dict | None,
    role: str,
    temperature: float | None,
    wait_on_rate_limit: bool,
) -> tuple[str, dict]:
    from google import genai
    from google.genai import types

    from klugscheisser.llm import budget

    budget.check("gemini")  # inside an answer: per-question budget and daily cap (llm/budget.py)
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    config: dict[str, Any] = {"system_instruction": system}
    if schema is not None:
        config.update(response_mime_type="application/json", response_json_schema=schema)
    if temperature is not None:
        config["temperature"] = temperature
    tries = 6 if wait_on_rate_limit else 1
    for attempt in range(tries):
        try:
            response = client.models.generate_content(
                model=model(settings, role), contents=prompt, config=types.GenerateContentConfig(**config)
            )
            break
        except Exception as exc:
            text = str(exc)
            if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
                raise
            if "PerDay" in text or "per day" in text.lower():
                raise DailyQuotaExceeded(text[:300]) from exc
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))
    u = response.usage_metadata
    usage = {
        "input": getattr(u, "prompt_token_count", 0) or 0,
        "output": (getattr(u, "candidates_token_count", 0) or 0)
        + (getattr(u, "thoughts_token_count", 0) or 0),
        "cached": getattr(u, "cached_content_token_count", 0) or 0,  # served from Gemini's prompt cache
    }
    budget.charge_llm(settings, usage, "gemini")
    return response.text or "", usage
