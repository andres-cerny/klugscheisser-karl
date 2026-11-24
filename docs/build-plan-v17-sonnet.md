# v17: Claude Sonnet instead of Gemini for all texts (2026-10-17)

Ondřej has an Anthropic API key. Every place where Gemini wrote **text or JSON** now uses Claude Sonnet 5.5
(`claude-sonnet-5-5`) when `ANTHROPIC_API_KEY` is set. Jev (TypeSafe) is unchanged.

## One switch

- `LLM_PROVIDER` (existing setting) now decides for **all** text calls: `auto` (default) = Claude when
  `ANTHROPIC_API_KEY` is set, else Gemini; `claude`; `gemini`. Back to Gemini without code change:
  `LLM_PROVIDER=gemini` in `.env`, restart.
- New module `src/klugscheisser/llm/text.py`: `generate()` (one call, text or JSON), `provider()`, `available()`,
  `model()`, `cost_usd()`, `cost_source()`. All former direct Gemini text calls go through it.
- Models: `ANTHROPIC_ANSWER_MODEL` (answers, interpreter, agent, translations, pronunciation hint, briefing) and
  `ANTHROPIC_MODEL` (fact extraction, classification fallback, reranking, image descriptions); both default to
  `claude-sonnet-5-5` (was `claude-opus-5-5` for `ANTHROPIC_MODEL`). `ANTHROPIC_EFFORT=low` (adaptive thinking,
  thinks only when needed).

## What moved to Claude Sonnet

| Place | File |
|---|---|
| Answer writer (liste + prosa, repair, web enrichment, fun facts, cross-person answers) | `answer/engine.py` `_ask_model` |
| Question interpreter (v13) | `answer/interpret.py` via the engine |
| Agent planner + writer (v14) | `answer/agent.py` via the engine |
| Quote / summary translations | `translate.py` |
| Fact extraction (profiles, backfill, deep runs) | `facts/extract.py`, `facts/backfill.py` |
| Fun facts | `facts/funfacts.py` (engine) |
| Briefing suggestion "Was wir gelernt haben" | `briefing.py` |
| Pronunciation hint text ("KLAA-ra …") | `pronounce.write_hint` |
| Classification fallback during Jev outages, LLM reranker, team-roster reading | `llm/providers.py` (`get_llm`, already preferred Claude; now Sonnet) |
| Image-only media analysis with `MEDIA_PROVIDER=claude` | `media/analyzer.py` (now with token counts and Claude prices in the ledger) |

Mapping of Gemini features:
- JSON schema mode → Claude structured outputs (`output_config.format`); schemas are closed automatically
  (`additionalProperties: false`), `["string", "null"]` becomes `anyOf` (`text.strict_schema`).
- `temperature` → dropped for Claude (Sonnet 5.5 accepts no sampling parameters).
- Prompt caching: every system prompt gets `cache_control` (`llm/text.system_block`, switch `LLM_PROMPT_CACHE`,
  default on); the output schema is rendered with it and cached too. Repeated calls of the same route within
  5 minutes pay 10 % for that part, the first pays 125 %. Until 2026-10-27 only system prompts of 2000+ characters
  were marked, which missed the fact extraction (1678 cached tokens), the MCP list writer (1052), the German
  evidence check (~1260), the agent planner (~680) and the repair step (~590): German is ~2 characters per token,
  not 4. Prefixes under 512 tokens (translation, classification) are skipped by the API at no cost. Each call logs
  `claude call …: input N (cache read R, cache write W) …` at INFO; chat-eval prints the cache totals.
- Refusals → `LlmRefusal`; server-side fallback (`fallbacks: "default"`) kept. "Credit balance too low" →
  `DailyQuotaExceeded` (backfills stop cleanly, like Gemini's daily quota).

## What stays on Gemini (no Anthropic equivalent)

- **Embeddings** (`EMBEDDING_PROVIDER=gemini`, `embed/providers.py`): Anthropic has no embedding model.
- **Computer voice** (`pronounce.tts`, Gemini TTS): Anthropic has no text-to-speech.
- **Reel/video analysis** (`GeminiMediaAnalyzer`): Claude takes no video. `MEDIA_PROVIDER=auto` still prefers
  Gemini; the live server uses `claude-cli` (subscription) anyway, unchanged.

## Costs

Ledger sources follow the provider: `llm:claude-answer`, `llm:claude-facts`, `llm:claude-translate`
(Gemini: `llm:gemini-…` as before). Prices in config (per 1M tokens): input $2.00, output $10.00, cache read
$0.20, cache write $2.50. Thinking tokens count as output.

Rough estimate (Sonnet is ~8x Gemini Flash-Lite on input, ~7x on output):

| | Gemini Flash-Lite | Claude Sonnet 5.5 |
|---|---|---|
| Average chat question (chat-eval: $0.05 / 65 questions) | ~$0.001 | ~$0.007–0.01 |
| Written prosa answer with documents (~10k in / 1.2k out) | ~$0.004 | ~$0.03 |
| Question with agent (2 plan rounds + second write) | ~$0.008 + Jev | ~$0.05–0.07 incl. Jev |
| Fact extraction, one person (~8k in / 1k out) | ~$0.004 | ~$0.03 |
| Typical night (only new profile docs → facts, translations) | ~$0.05 | ~$0.3–0.5 |
| Full chat-eval run | ~$0.05 | ~$0.65 |

## Caps — not raised, Ondřej decides

- `AGENT_MAX_USD=0.02` counts the planner calls and Jev reading while the agent collects (not the final write).
  A Sonnet planner round costs ~$0.005, so two rounds still fit unless Jev reads a lot; proposal: **$0.04**.
- `LLM_DAILY_COST_CAP_USD=2.0` limits the fact backfill per day (and the pronunciation hint/TTS). With Sonnet $2
  covers ~70 persons instead of ~500. Proposal: **$5** while backfilling, else $2 is fine. Answers are not
  limited by this cap.
- Event preparation and deep runs count all ledger costs against their own cap (`EVENT_PREP_CAP_CHF`); fact
  extraction there is now ~7x more expensive.

## Tests

`tests/test_v17_sonnet.py` (fake Anthropic client, no API call): provider choice and flip back, Sonnet cost with
cache, every JSON schema maps to structured outputs, request shape (model, effort, no temperature, caching,
fallback), refusal / cut-off / empty credit, engine + interpreter on Claude alone, ledger source names, agent
budget prices, translation, fact extraction, pronunciation hint, briefing, classification fallback, and that
embeddings / reels stay on Gemini.

## After the key is installed

Ondřej puts `ANTHROPIC_API_KEY=…` into `~/klugscheisser/.env` himself (the app reads `.env`), the update is
deployed and restarted as in `docs/server-handoff.md`, then:
`uv run klugscheisser chat-eval --yes` in `~/klugscheisser` (~$0.65 now) and compare with the 54/54 / 65-question
baseline. If worse: `LLM_PROVIDER=gemini` and restart.

## chat-eval with Sonnet (2026-10-17, live data, feat/sonnet code)

| | Gemini (09.10., run 20261009-133418) | Sonnet 5.5 (17.10.) |
|---|---|---|
| Score | 65/65 | 64/65 |
| Cost | $0.061 | $0.686 (~11x) |
| Average time per question | 2.8 s | 5.0 s (median 4.8 s) |
| Model calls / input / output tokens | 93 / 257k / 11.6k | 92 / 531k (235k from cache) / 21.9k |

- Only miss: I13 "Wann spielt Frölunda das nächste Mal?" expects Färjestad (the game of 09.10.); that game is
  over, the answer (18.10. Skellefteå) is right — the fixture is date-bound, not a model problem.
- Better: E2 (Schweiz–Tschechien) now names all scorers (Enzler, Leemann, Balzer in the shootout) instead of
  only Balzer; S4 (Northeastern record) now gives 254 career points plus the older 168-point milestone.
- Worse-looking: J2 ("Enzler Stalder in der gleichen Linie?") — Gemini said "Linien-Aufstellungen sind nicht
  gespeichert" and showed the table of 3 joint goals; Sonnet writes prose about mutual assists and no longer
  says that line-ups are not stored.
- Writer-heavy answers cost $0.02–0.045 each and take 6–19 s (Gemini 3–11 s).
