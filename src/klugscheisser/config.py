"""Settings from environment / `.env`. Every external service is off unless its key is set."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # core
    database_url: str = "postgresql+psycopg://localhost/klugscheisser"
    projects_file: str | None = (
        "projects.yaml"  # v7: one database per project ("" = one project on DATABASE_URL)
    )
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_auth_token: SecretStr | None = None
    # extra Host headers accepted besides localhost, e.g. your tunnel hostname
    mcp_allowed_hosts: list[str] = []
    # browser-based MCP clients that may send an Origin header
    mcp_allowed_origins: list[str] = [
        "https://claude.ai",
        "https://www.perplexity.ai",
        "https://perplexity.ai",
        "https://enterprise.perplexity.ai",
        "https://chatgpt.com",
    ]
    # one `mcp_call tool=… dur=… ua=… cost=… cache=… refresh=… args=…` line per tool call (calllog.py)
    mcp_call_log: bool = True
    timezone: str = "Europe/Zurich"

    # paid / keyed services (feature is on iff key present)
    apify_token: SecretStr | None = None
    # posts/mentions actor: "apify/instagram-scraper" (default) or "apidojo/instagram-scraper" (cheaper, fewer
    # fields: set APIFY_COST_PER_POST_USD=0.0005 with it). Profiles always use the profile actor (has bios).
    apify_instagram_actor: str = "apify/instagram-scraper"
    apify_instagram_profile_actor: str = "apify/instagram-scraper"
    meta_graph_token: SecretStr | None = None
    meta_ig_business_user_id: str | None = None
    meta_graph_api_version: str = "v24.0"
    # Graph Business Discovery: at most this many calls per rolling hour in one process (Meta allows ~200/h);
    # after that, or after a rate-limit/token error, the hybrid source sends the rest to Apify
    meta_graph_max_calls_per_run: int = 150
    meta_graph_page_size: int = 25  # posts per Graph call (pagination for longer histories)
    meta_graph_recheck_days: int = 30  # a handle Graph could not read is asked again after this many days
    # Bright Data Web Scraper API (Instagram posts + profiles datasets): free tier 5000 records per calendar
    # month (no card: over it, calls just fail). The hybrid asks it after Graph, before Apify. Records are
    # counted per month in the cost ledger (source "brightdata"); no call that could pass the cap is made.
    brightdata_api_token: SecretStr | None = None
    brightdata_free_records_per_month: int = 5000
    brightdata_monthly_record_cap: int = 4500  # stop using Bright Data before this many records in a month
    brightdata_cost_per_record_usd: float = 0.0015  # pay as you go after the free records ($1.50 / 1000)
    brightdata_timeout_s: int = 600  # max wait for one snapshot (trigger -> ready -> download)
    brightdata_poll_s: float = 10.0  # wait between progress checks
    brightdata_batch_size: int = 20  # profile URLs per snapshot
    # never ask Bright Data for more posts per handle than this (history loads ask for up to 400; the monthly
    # reservation uses the capped number). What is older stays unread, nothing goes to Apify for it.
    brightdata_history_max_posts: int = 50
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-latest"
    anthropic_api_key: SecretStr | None = None
    # v17: Claude Sonnet writes all texts when ANTHROPIC_API_KEY is set (LLM_PROVIDER=gemini flips back)
    anthropic_model: str = (
        "claude-sonnet-5-5"  # facts, classification fallback, reranking, image descriptions
    )
    anthropic_answer_model: str = "claude-sonnet-5-5"  # answers, interpreter, agent, translations, hints
    anthropic_effort: str = "low"  # adaptive thinking; low = thinks only when needed (cheapest, fastest)
    # Claude Sonnet 5.5 prices per 1M tokens (Oct 2026): cache reads 0.1x, 5-min cache writes 1.25x of input
    anthropic_input_usd_per_m: float = 2.0
    anthropic_output_usd_per_m: float = 10.0
    anthropic_cache_read_usd_per_m: float = 0.20
    anthropic_cache_write_usd_per_m: float = 2.50
    # Prompt caching (llm/text.py): the fixed part of each call (system prompt; the output schema is rendered with
    # it) is cached for 5 min, repeats read it at 10 % of the input price. false = no cache_control at all.
    llm_prompt_cache: bool = True
    # system prompts shorter than this are not marked (0 = all; Sonnet 5.5 itself skips prefixes < 512 tokens
    # at no cost; German prompts are ~2 characters per token, so the old 2000-char limit missed 600-token prompts)
    anthropic_cache_min_chars: int = 0
    gemini_api_key: SecretStr | None = None
    # text classification fallback uses a lite model so the flash quota stays free for images/reels
    gemini_text_model: str = "gemini-3.1-flash-lite"
    gemini_model: str = "gemini-3.8-flash"  # 2.5 no longer offered to new accounts (2026-10)
    xai_api_key: SecretStr | None = None
    x_bearer_token: SecretStr | None = None  # official X API (pay-per-use); X is on iff set
    firecrawl_api_key: SecretStr | None = None

    # provider selection
    # every text/JSON model call (answers, interpreter, agent, translations, facts, classification fallback):
    # auto = Claude when ANTHROPIC_API_KEY is set, else Gemini. Embeddings, TTS and reels stay on Gemini.
    llm_provider: str = Field("auto", description="auto | claude | gemini | fake")
    embedding_provider: str = Field("fake", description="fake | local | gemini")
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384
    instagram_source: str = Field(
        "auto",
        description="auto | hybrid | apify | graph | brightdata | fixture (auto: hybrid when two or more of "
        "Graph, Bright Data, Apify are set)",
    )
    fixtures_dir: str = "tests/fixtures"

    # thresholds
    classify_irrelevant_below: float = 0.2
    classify_uncertain_below: float = 0.6
    sensitive_threshold: float = 0.45  # calibrated on real posts, see tests/fixtures/vorsicht_labels.yaml
    rerank_min_score: float = 0.2

    # caching (seconds) and cost caps
    ttl_instagram: int = 1800
    ttl_news: int = 300
    ttl_results_live: int = 60
    ttl_results: int = 3600  # when no game is live
    apify_daily_cost_cap_usd: float = 2.0
    apify_cost_per_post_usd: float = 0.0027  # instagram-scraper, pay per result (Oct 2026)
    llm_daily_cost_cap_usd: float = 2.0
    retention_irrelevant_days: int = 30
    instagram_first_run_days: int = 90  # how far back the first scrape of a handle goes
    # official team accounts read nightly as team sources (only new posts; linked to the team whose
    # instagram_handle in seeds/teams.yaml is this account). @swissicehockeywomen: linked from sihf.ch.
    # League / club / federation accounts (2026-10-30) bring back most club posts that tag players
    # (Bright Data has no tagged-posts "mentions"); ~360 Bright Data records per month together.
    instagram_team_accounts: list[str] = [
        "swissicehockeywomen",
        "postfinance_womensleague",
        "fr_gotteron_ladies",
        "lionsfrauen",
        "scb_frauen_official",
        "swissicehockeyfederation",
    ]
    instagram_team_max_posts: int = 12  # per account and night (first read: INSTAGRAM_FIRST_RUN_DAYS back)
    posts_lookback_days: int = 120  # how far back tools look for "latest" posts
    # MCP "⚠️ Vorsicht" / "⭐ Zu beachten": only items of the last N days (older injury report only without news)
    caution_window_days: int = 21
    # testing switch: fetch article pages for ALL feeds, ignoring fetch_article in seeds/feeds.yaml
    # (several outlets' terms forbid automated reading; keep false outside private testing)
    news_fetch_all_articles: bool = False
    # --- answer engine (v3 P3); Gemini models/prices apply with LLM_PROVIDER=gemini (or no Anthropic key) ---
    gemini_answer_model: str = "gemini-3.1-flash-lite"
    gemini_answer_input_usd_per_m: float = 0.25
    gemini_answer_output_usd_per_m: float = 1.50
    # v6 step 7: name pronunciation (computer voice). Prices are conservative estimates for the cost cap.
    gemini_tts_model: str = "gemini-3.8-flash-lite-tts"
    gemini_tts_voice: str = "Kore"
    gemini_tts_input_usd_per_m: float = 0.5
    gemini_tts_output_usd_per_m: float = 10.0
    answer_support_threshold: float = 0.6  # Jev: quote supports statement
    answer_web_results: int = 4
    answer_web_search: bool = False  # v5: no web search while answering (Perplexity does the web)
    # v12: web chat answer style. "prosa" = written answer with numbered sources (the model writes, Jev checks every
    # sentence); "liste" = one checked statement per line (v5–v11). MCP always gets "liste". Default stays
    # "liste" until Ondřej has seen the replay report; a project can switch in its settings.
    web_answer_style: str = "liste"
    answer_support_threshold_prosa: float = 0.6
    answer_support_threshold_computed: float = 0.4  # sentences resting only on computed documents
    answer_interpreter: bool = (
        True  # v13: the model reads the question into a template first (web chat, prosa)
    )
    # v14: second stage (agent) when the fast answer is not enough: Jev judges, the model plans and writes
    answer_agent: bool = True
    agent_max_rounds: int = 2
    agent_max_usd: float = 0.02  # per question; the agent stops and answers with what it has
    # 2026-10-19: what ONE answer (MCP frage, web chat) may cost in total: interpreter, Jev, writer calls, agent.
    # Spent → no more paid calls; the answer degrades (fewer persons, word match instead of Jev, no agent) and
    # says what was left out (llm/budget.py). The daily cap llm_daily_cost_cap_usd is checked before every call.
    answer_max_usd: float = 0.08
    # group questions answered person by person (answer/cross.py block_evidence): at most this many persons, the
    # most relevant to the question first; the others are named as "nicht ausgewertet"
    answer_max_persons: int = 16
    # chat answers without embedding search (08.10.2026, chat-eval group "Suche": 6/8 vs 3/8): Jev reads every
    # document of the persons in scope (up to answer_jev_max_docs), questions without a person use full text
    # and Jev's topic tags. true = the old way (newest 150 + vector hits).
    retrieval_embeddings: bool = False
    # exp/search-jev-path v2: chat, agent and MCP `search` share retrieval/docs.py (candidates from full text,
    # vectors, labels, the persons' documents; Jev scores up to search_jev_max_docs for MCP). Vector candidates
    # (~40 nearest, Gemini query vector) off: RETRIEVAL_VECTOR_CANDIDATES=false. MCP_SEARCH_EMBEDDINGS=true = the
    # old MCP search (vectors + full text, RRF, Jev on the top 24).
    retrieval_vector_candidates: bool = True
    mcp_search_embeddings: bool = False
    search_jev_max_docs: int = 120
    # search lab 2026-10-25: vector candidates from passages (item_chunks, gemini-embedding-2 @768, header
    # "Quelle | Datum | Personen" in the embedded text; doc score = best passage, which Jev then reads).
    # Needs `klugscheisser reembed-chunks` first; off = the document vectors (embeddings table) as before.
    # default on since the lab (33 labelled queries: vector recall@40 0.54 -> 0.63, MCP search nDCG@8 0.71 -> 0.77
    # together with the card text + reader context below). Without passage vectors in the database (before the
    # backfill) the document vectors are used as before.
    retrieval_chunks: bool = True
    # search lab 2026-10-25 phase C: Jev reads one (question, document) pair per request, JSON state with title,
    # source, kind, date (+ age), persons and the passage; jev_workers requests in parallel. Reader context (the
    # Sportmoderatorin, tie-breakers only) in the instructions; rerank_noul_min = cut-off on the calibrated
    # "contains evidence" probability instead of rerank_min_score.
    # lab result: pairwise was NOT better than 20 per request (nDCG 0.70-0.71 vs 0.77) and 2-3x slower; kept off.
    jev_pairwise: bool = False
    jev_workers: int = 32
    # MCP search (find_documents): Jev reads a card line (title | source (kind) | date (age) | über) + passage,
    # with the reader context first in the state. Chat answers (answer/engine.rerank) are unchanged.
    jev_reader_context: bool = True
    jev_card_text: bool = True
    # MCP `search` answers with evidence cards (mcp_server/cards.py: plain-word type/source, date + age, the
    # passage Jev judged or the full short text, one link per card) and offers `dokument(id)` for the full text.
    # Off = the old list lines. Changes what Perplexity sees: switch on together with the connector text.
    mcp_search_cards: bool = False
    # Phase F (2026-10-25): Perplexity sees the new read tool set (suche, person, team, events, stats, dokument;
    # cards on, no server-side writing) instead of the old 16 tools; maintenance tools (status, refresh*,
    # hinzufuegen*) are hidden. Switch on together with docs/perplexity-instruction-phase-f.md.
    mcp_new_tools: bool = False
    # MCP read tools (get_event, get_athlete, get_athlete_posts, whats_new, search) answer at once with the stored
    # data; the person update runs in the background (the answer says so, with its Datenstand line).
    # false = wait up to auto_refresh_wait_s as before.
    mcp_refresh_no_wait: bool = True
    # MCP search (find_documents) without a person/team filter: persons named in the question text (full name,
    # alias, unique surname of a tier 1/2 person, a small typo; the ingest matcher domain/matcher.py) bring their
    # own documents as extra candidates (share `search_names_share` of the Jev cap), the other candidates stay,
    # so a topic question keeps results about other people. Off = the question text only (as before).
    # Lab 2026-10-25 (8 person questions, name only in the text): her own relevant posts among the candidates
    # 10/19 -> 14/19, relevant recall@120 0.73 -> 0.88; nDCG@8 unchanged (within Jev noise), +80-150 ms on such questions.
    search_names: bool = True
    search_names_share: float = 0.67
    # Story groups (retrieval/stories.py, 2026-10-25): after Jev scoring, documents of the same story (same person or
    # team, published within 48 h, passage vectors >= 0.91 similar; never profiles, results, digests) are folded into
    # one entry with an 'Auch berichtet von: … – dokument(ids)' line, so the list shows more distinct stories.
    # search_group_stories: MCP `suche` / `search` (default on); chat_group_stories: the answer engine's document
    # list (web chat and MCP `frage`; default off until chat-eval has been run with it).
    search_group_stories: bool = True
    chat_group_stories: bool = False
    # German as interview language (facts/german.py, 2026-10-25): MCP person/team (and the old get_athlete /
    # get_team_roster) show a "Deutsch: bestätigt | wahrscheinlich – reason" line for persons of other nations,
    # derived from career clubs (seeds/club_languages.yaml) and language evidence; suche answers "Wer spricht
    # Deutsch bei …". Off: no Deutsch lines anywhere.
    german_hints: bool = True
    # MCP `team` (Phase F) for a national team other than Switzerland: a block "Schlüsselspielerinnen aus Schweizer
    # Sicht" before the roster (mcp_server/key_players.py, 2026-10-28): points in the stored games against
    # Switzerland, Swiss clubs in the career (+ Deutsch label), season top scorers, recent awards, Vorsicht notes.
    # Model-free, ~2-4k characters. Off: the roster as before.
    team_key_players: bool = True
    # German interviews from stored texts (facts/german_evidence.py): the nightly step checks new German items of
    # roster persons (WCh etc.) for her own German quotes and writes `language_evidence` facts (bestätigt only for
    # a German interview). Free prefilter, ~$0.01 per candidate; cap per night below.
    german_evidence_nightly: bool = True
    german_evidence_max_usd: float = 0.30
    rerank_noul_min: float | None = None
    chunk_embedding_model: str = "gemini-embedding-2"
    chunk_embedding_dim: int = 768
    # query prefix for gemini-embedding-2: "search result" | "question answering"
    chunk_query_task: str = "search result"
    # gemini-embedding-001 query task type (RETRIEVAL_QUERY | QUESTION_ANSWERING)
    embedding_query_task: str = "RETRIEVAL_QUERY"
    # vectors for new documents at ingest (Gemini with EMBEDDING_PROVIDER=gemini); always on while any switch above
    # uses vectors (`embeddings_on`). Only with all three off can ingest skip them (gaps later: `reembed`).
    ingest_embeddings: bool = True
    answer_jev_max_docs: int = 600
    evidence_jev_docs_per_person: int = 80  # group questions (block 4): newest documents per person Jev reads
    auto_refresh: bool = True  # update asked athletes once per day before answering
    auto_refresh_wait_s: float = 60.0  # max seconds a tool call waits for that update
    # max seconds the status tools (refresh_status, hinzufuegen_antwort ja, hinzufuegen_status) wait for a job;
    # separate from AUTO_REFRESH_WAIT_S so answers stay fast while asking for the status still waits
    status_wait_s: float = 60.0

    # --- media analysis ---
    media_provider: str = Field("auto", description="auto | gemini | claude | claude-cli | fake | off")
    # v15: Claude Code CLI with Ondřej's subscription (no API key); several posts per call
    claude_cli_path: str = "claude"
    claude_cli_model: str = "sonnet"
    claude_cli_batch_posts: int = 5
    claude_cli_timeout_s: int = 300
    media_max_images: int = 5  # images per post sent in one request
    media_max_video_seconds: int = 180  # longer reels are not analysed
    media_max_inline_mb: float = 20.0  # larger videos go through the Gemini Files API
    media_max_upload_mb: float = 100.0  # larger videos are skipped
    # free tier (observed 2026-10): 20 requests/day per model, so GEMINI_MODEL + fallbacks give ~60/day
    media_fallback_models: list[str] = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]
    # v8: media cost per 1M tokens (conservative estimate for the Flash media model; images ~1k tokens each,
    # video ~300 tokens/s) — recorded in the ledger per analysis
    gemini_media_input_usd_per_m: float = 0.50
    gemini_media_output_usd_per_m: float = 3.00
    media_daily_limit: int = 150  # analyses per rolling 24 h (v5: Gemini billing on; was 60 on the free tier)
    media_min_interval_s: float = 2.0  # pause between analyses
    media_stale_hours: int = 48  # queue rows older than this are dropped (CDN links expire)
    # after a rate limit the worker waits 60 s (Gemini) or 30 min (Claude subscription / no model left), doubling
    # with every further rate limit in a row, up to this cap; a pass that analyses something resets it
    media_backoff_max_s: float = 4 * 3600.0
    gemini_embedding_model: str = "gemini-embedding-001"  # used with EMBEDDING_PROVIDER=gemini

    # --- results collectors ---
    # All ON for the private PoC (decision 2026-10-03, terms risk accepted by Ondřej; see docs/sources.md).
    # Re-evaluate before any SRF/commercial use.
    sihf_data_enabled: bool = True
    iihf_realtime_enabled: bool = True
    pwhl_enabled: bool = True
    sdhl_enabled: bool = True
    sihf_league_id: int = 42  # PostFinance Women's League
    sihf_season: int = 2027  # SIHF season alias: 2027 = 2026/27
    # 2026-10-25: nightly step "saisonwerte": SIHF league season tables + national-team totals from the stored
    # Leijonat sheets (stats/current.py); newer than the EP import, shown instead of it where they overlap
    season_totals_enabled: bool = True
    # NCAA: the college's own stats page (seeds/college_sites.yaml, robots.txt + Crawl-delay 30 s: ~3 min a night)
    season_totals_college_enabled: bool = True
    # 2026-10-28: Swedish league tables on stats.swehockey.se ("Players By Team" + "Scoring Leaders" for the
    # Stand: 2 requests per league); schedule ids change every season (2026/27 below)
    season_totals_swehockey_enabled: bool = True
    season_totals_swehockey_leagues: list[dict] = [
        {"schedule_id": 20960, "league": "SDHL (W)", "name": "SDHL"},
        {"schedule_id": 20958, "league": "NDHL (W)", "name": "NDHL Norra"},
        {"schedule_id": 20959, "league": "NDHL (W)", "name": "NDHL Södra"},
    ]
    # 2026-10-28: Finnish league tables of tulospalvelu.leijonat.fi (4 requests per league; season alias as
    # LEIJONAT_SEASON or from today's date). 73 = Auroraliiga
    season_totals_leijonat_leagues_enabled: bool = True
    season_totals_leijonat_leagues: list[dict] = [{"level_id": 73, "league": "Auroraliiga (W)"}]
    # 2026-10-28: Japan women's league, counted from the JIHF game sheets (one page per round + new sheets once;
    # rounds = JIHF "meet" ids of the season: 2026/27 903 1st round, 904 2nd round, 905 final round)
    season_totals_jihf_enabled: bool = True
    season_totals_jihf_meets: list[int] = [903, 904, 905]
    iihf_event_id: int = 773  # Women's World Championship 2026
    iihf_event_path: str = "2026/ww"  # iihf.com URL segment of the event
    iihf_site_game_id_offset: int = 75138  # iihf.com game-centre id = offset + game number (WW 2026)
    # more current IIHF events for the regular run (IihfEvent.from_config keys: event_id, path, site_id_offset,
    # competition, competition_key, tournament); the event above always comes first
    iihf_events: list[dict] = []
    # past events, only via `klugscheisser results-backfill` (never nightly): the realtime event endpoint answers
    # [] after an event, so each names its realtime game-id range (found 2026-10-19, see docs/sources.md)
    iihf_backfill_events: list[dict] = [
        {
            "event_id": 712,
            "path": "2025/ww",
            "competition": "IIHF Frauen-WM 2025",
            "competition_key": "wch-2025",
            "game_ids": [12088, 12116],
            "tz": "Europe/Prague",
            "tournament": {
                "prefix": "wch",
                "key": "wch-2025",
                "name": "IIHF Frauen-WM 2025, České Budějovice",
                "city": "České Budějovice (CZE)",
            },
        },
        {
            "event_id": 793,
            "path": "2025/wwia",
            "competition": "IIHF Frauen-WM 2025 Division I Gruppe A",
            "competition_key": "wchd1a-2025",
            "game_ids": [12322, 12336],
            "tz": "Asia/Shanghai",
            "tournament": {
                "prefix": "wchd1a",
                "key": "wchd1a-2025",
                "name": "IIHF Frauen-WM 2025 Division I A, Shenzhen",
                "city": "Shenzhen (CHN)",
            },
        },
    ]
    # past tournaments the IIHF realtime feed does not serve (Olympics 2026 = IIHF event 852): Wikipedia boxes
    wiki_backfill_pages: list[dict] = [
        {
            "page": "Ice hockey at the 2026 Winter Olympics – Women's tournament",
            "competition": "Olympische Winterspiele 2026 Frauen-Eishockey",
            "competition_key": "oly-2026",
            "tz": "Europe/Rome",
            "tournament": {
                "prefix": "oly",
                "key": "oly-2026",
                "name": "Olympische Spiele 2026 Milano Cortina, Frauenturnier",
                "city": "Mailand (ITA)",
            },
        }
    ]
    pwhl_feed_key: str = "694cfeed58c932ee"  # public key used by thepwhl.com (not officially issued)
    pwhl_teams: list[str] = ["Boston Fleet", "PWHL Detroit", "PWHL Hamilton", "New York Sirens"]
    sdhl_schedule_id: int = 20960  # SDHL 2026/27 on stats.swehockey.se
    # national-team games (EHT, WCh groups) from the Finnish federation's results service (keyless JSON)
    leijonat_enabled: bool = True
    # season alias (2027 = 2026/27); unset = derived from today's date (seasons start in August)
    leijonat_season: int | None = None
    leijonat_level_ids: list[int] = [133]  # 133 = National Team Women
    # subseries another collector already covers while it is on (the WCh comes from IIHF)
    leijonat_skip_when_iihf: str = "world championship"
    # Swiss national-team pages on sihf.ch (results + programme HTML): friendlies, camps; fallback source
    sihf_nt_enabled: bool = True
    sihf_nt_teams: list[dict[str, str]] = [{"path": "womens-national-team", "team": "Schweiz", "code": "SUI"}]
    results_lookback_days: int = 14  # final games older than this are not re-fetched
    results_request_delay_s: float = 1.0  # pause between requests to the same source
    results_max_details: int = 20  # max game-detail requests per source and run
    # --- game watch (2026-10-31, jobs/gamewatch.py): IIHF games read right after the final buzzer ---
    # thread `game-watch` in `serve`: polls the IIHF event feed only from the scheduled start until the game is
    # final (at most GAME_WATCH_MAX_HOURS), re-checks the sheet after the final, then one follow-up per game
    # (news + Instagram of the two teams' accounts and roster players) GAME_WATCH_FOLLOWUP_DELAY_MIN later
    game_watch: bool = True
    game_watch_poll_minutes: int = 5  # IIHF event feed (+ game state of live games) while a game runs
    game_watch_max_hours: float = 4.0  # after the scheduled start: stop polling a game that is not final yet
    # after the final: read the sheet again (IIHF corrections); while an event runs: read its schedule (0 = off)
    game_watch_recheck_minutes: list[int] = [10, 30, 120]
    game_watch_schedule_hours: float = 6.0
    game_watch_followup: bool = True
    game_watch_followup_delay_min: int = 60  # after the final: news + Instagram of both teams
    game_watch_followup_max_persons: int = 30  # roster players per team in the follow-up's Instagram read
    game_watch_followup_apify_usd: float = 0.30  # Apify per follow-up (also inside the daily/cycle caps)
    # 2026-11-04: rosters around games. After every new/changed final sheet: players who dressed but are not on the
    # tournament roster are added (jobs/game_rosters.py, database only). In the follow-up: the roster watcher
    # (kaderwatch, free) for the two teams of the game.
    game_watch_sheet_rosters: bool = True
    game_watch_roster_watch: bool = True

    # --- web search (v3 P4) ---
    brave_api_key: SecretStr | None = None  # web search is on iff set (see web_search.web_search_enabled)
    brave_cost_per_request_usd: float = 0.005  # recorded in cost_ledger as source "brave"
    brave_daily_cap: int = 100  # Brave requests per rolling 24 h
    brave_country: str = "CH"
    brave_search_lang: str = "de"
    # never fetched (results from these domains are dropped too); social media is collected elsewhere
    web_blocked_domains: list[str] = [
        "flashscore.*",
        "livesport.*",
        "sofascore.com",
        "eliteprospects.com",
        "instagram.com",
        "facebook.com",
        "x.com",
        "twitter.com",
        "tiktok.com",
    ]
    web_fetch_timeout_s: float = 15.0
    web_fetch_max_bytes: int = 3_000_000  # larger pages are cut off here
    web_text_max_chars: int = 8000  # stored page text, never cut mid-sentence
    # --- profiles (v3 P2) ---
    # Raw documents per person: Wikipedia (de/en/fr) + college bio pages (seeds/college_sites.yaml).
    profiles_enabled: bool = True
    profiles_college_enabled: bool = True  # Sidearm sites: Crawl-delay 30 s -> ~1 min per college player
    profiles_max_age_days: int = 14  # re-fetch a person's profiles at most every N days
    profiles_wikipedia_languages: list[str] = ["de", "en", "fr"]
    profiles_max_chars: int = 6000  # cap per college bio page (cut after a full sentence)
    # Wikipedia articles are stored whole (2026-10-25; was 6,000 chars, cut bios mid-career). Only a safety limit
    # for giant pages; Jev / LLM inputs read passages of it, dokument(id) serves it in parts.
    profiles_wikipedia_max_chars: int = 100_000
    profiles_request_delay_s: float = 1.0  # minimum pause per host when robots.txt sets no Crawl-delay
    profiles_max_crawl_delay_s: float = 60.0  # hosts asking for a longer Crawl-delay are skipped
    # Wikipedia for never-looked-up persons asked about in a question runs in the background (the answer uses
    # what is stored; 24.10.: 5.65 s inside one frage). false = inside the request, as before
    profiles_in_background: bool = True
    # --- web app (v3 P6) ---
    # Cloudflare Access emails allowed in (JSON list). Empty/unset = no email check (local dev).
    web_access_emails: list[str] | None = None
    # optional password login (signed cookie) for access without Cloudflare Access
    web_password: SecretStr | None = None
    web_secret: SecretStr | None = None  # cookie signing key; derived from WEB_PASSWORD when unset
    web_session_days: int = 30
    web_auto_refresh: bool = True  # person page starts the daily update in the background (never blocks)
    # page views by bots / headless browsers (user agent: HeadlessChrome, bot, crawler, curl, python …) start no
    # paid update (19.10.: a headless probe opened 80 person pages = 84 Apify jobs); true = they do, as before
    web_refresh_for_bots: bool = False
    web_page_posts: int = 6  # Instagram cards per page ("Mehr laden" loads the next ones)
    web_answer_engine: str = Field("auto", description="auto | fake")
    # --- add persons / teams on demand (v4) ---
    add_cost_cap_chf: float = 5.0  # hard cap per add action (proposal + background load)
    usd_to_chf: float = 0.80  # for cost estimates shown in CHF
    add_max_team_persons: int = 60  # players + coaches + staff loaded for one team
    chat_history_turns: int = 4  # previous question/answer pairs given to the answer engine
    # --- X (official API, pay-per-use since Feb 2026) ---
    x_daily_cost_cap_usd: float = 1.0
    x_cost_per_post_usd: float = 0.005
    x_cost_per_user_usd: float = 0.010
    x_first_run_posts: int = 20  # first read per handle; afterwards only new posts (since_id)
    x_team_accounts: list[str] = ["SwissIceHockey"]  # official accounts read as team sources
    # --- v5: event preparation + nightly refresh ---
    event_prep_cap_chf: float = 40.0  # hard cap for one event preparation (shown before ✅ Ja)
    nightly_hour: int = 5  # local hour (Europe/Zurich) of the nightly refresh
    nightly_cap_usd: float = 2.0  # max Apify spend per regular night (one Apify batch may reserve ~$1.2)
    # who the nightly run refreshes (besides persons of prepared events): tier -> every N nights. N = 1 every
    # night; N = 7: a stable weekly rotation (person id mod N = day number mod N). Tier 3 e.g. {"3": 30}.
    nightly_tiers: dict[int, int] = {1: 1, 2: 7}
    nightly_x_first_read_tiers: list[int] = [
        1
    ]  # X handles never read before: only for these tiers (~$0.11 each)
    # one-off full night: on this local date the run starts at NIGHTLY_FULL_ONCE_HOUR and takes every person of
    # NIGHTLY_FULL_TIERS (X first reads allowed for them), capped at NIGHTLY_FULL_CAP_USD (Apify + X together)
    nightly_full_once_date: date | None = None
    nightly_full_once_hour: int = 1
    nightly_full_tiers: list[int] = [1, 2]
    nightly_full_cap_usd: float = 8.0
    # Apify plan: $19 per billing cycle. Nightly runs are skipped when the ledger's Apify spend of the current
    # cycle plus tonight's estimate would pass this (0 = no check). Cycle start: day of month (ends 2026-11-03).
    apify_cycle_cap_usd: float = 18.0
    apify_cycle_start_day: int = 4
    nightly_end_hour: int = 8  # the slow profile step stops after this local hour (rest: next nights)
    news_poll_minutes: int = 30  # all feeds in the background, independent of questions (0 = off)
    # --- v5 phase 4 ---
    # Wikipedia articles per person: home language(s) by nationality first, then profiles_wikipedia_languages
    profiles_max_languages: int = 4
    seed_min_birth_year: int = 1980  # bulk seeding: older persons count as no longer active
    seed_retired_years: int = 3  # bulk seeding: work period (P2032) ended longer ago -> not active
    # --- v11: Elite Prospects profile pages (collectors/eliteprospects/fetch.py) ---
    # EP's terms forbid scraping; robots.txt allows /player/. Private project, Ondřej accepts the risk of a
    # block (2026-10-04). Off unless switched on; the nightly run reads only what can have changed.
    ep_enabled: bool = False
    # 2026-10-25: free profile sources (facts/hockeycanada_import.py, facts/wikipedia_infobox.py, …)
    hockeycanada_delay_s: float = 3.0  # pause between hockeycanada.ca requests (robots.txt sets none)
    hockeycanada_nightly: bool = True  # nightly: re-read the current WCh rosters until the event is over
    ep_tier3: bool = False  # tier 3 monthly as well (hundreds of pages; off by default)
    ep_daily_page_cap: int = 300  # requests per local day, retries included
    ep_block_pause_days: int = 7  # 403 / 429 / captcha -> no EP request at all for this long
    ep_min_gap_s: float = 6.0  # random pause between two requests: uniform(min, max)
    ep_max_gap_s: float = 9.0
    ep_backoff_s: list[float] = [30.0, 60.0, 120.0]  # 5xx / timeouts: waits before each retry, then stop
    ep_retry_after_max_s: float = 600.0  # a longer Retry-After stops the day instead of waiting
    ep_fail_streak_stop: int = 5  # pages without player data in a row -> stop for the day
    ep_failed_retry_days: int = 7  # a person whose page had no data is tried again after this long
    ep_timeout_s: float = 30.0
    ep_tier1_every_days: int = 1  # refresh intervals per tier, in local days since the last fetch
    ep_tier2_every_days: int = 7
    ep_tier3_every_days: int = 30
    ep_after_game: bool = True  # also refresh persons whose team played since their last fetch
    # nightly EP reads only between these local hours; a start at/after NIGHTLY_HOUR also waits until that
    # day's nightly refresh is done, so EP runs right after it (2026-10-25: was 03:00–05:00, before it)
    ep_nightly_start_hour: int = 5
    ep_nightly_end_hour: int = 9
    # who is read (2026-10-25): "tiers" = tier 1 daily, tier 2 weekly, after games (v11); "event:<key>" = only
    # the players on that event's rosters (provisional + confirmed), each once per local day, nobody else;
    # stops by itself the day after the event ended. Ondřej's go: EP_SCOPE=event:wch-2026 (WCh 2026).
    ep_scope: str = "tiers"

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # `KEY=` in .env means "not configured", not an empty secret
        return None if value == "" else value

    @property
    def embeddings_on(self) -> bool:
        """Does anything use vectors? Then ingest computes them (else no embedding calls at all)."""
        return (
            self.ingest_embeddings
            or self.retrieval_embeddings
            or self.retrieval_vector_candidates
            or self.mcp_search_embeddings
        )

    @property
    def features(self) -> dict[str, bool]:
        return {
            "apify": self.apify_token is not None,
            "graph_business_discovery": self.meta_graph_token is not None
            and self.meta_ig_business_user_id is not None,
            "brightdata": self.brightdata_api_token is not None,
            "jev": self.typesafe_api_key is not None,
            "claude": self.anthropic_api_key is not None,
            "gemini": self.gemini_api_key is not None,
            "xai": self.xai_api_key is not None,
            "x": self.x_bearer_token is not None,
            "firecrawl": self.firecrawl_api_key is not None,
            "mcp_auth": self.mcp_auth_token is not None,
            "eliteprospects": self.ep_enabled,
        }


@lru_cache
def base_settings() -> Settings:
    return Settings()


_per_project: dict[str, tuple[dict, Settings]] = {}


def get_settings() -> Settings:
    """Settings of the current project (v7): environment / .env plus the project's overrides (projects.yaml and
    the app's settings page). Rebuilt only when the overrides change."""
    from klugscheisser import projects

    p = projects.current()
    data = {"database_url": p.database_url, **projects.overrides(p)}
    hit = _per_project.get(p.key)
    if hit is None or hit[0] != data:
        hit = (data, base_settings().model_copy(update=data))
        _per_project[p.key] = hit
    return hit[1]


class CurrentSettings:
    """v7: stands in for Settings in long-lived objects (web app, workers started by `serve`): every attribute is
    read from the settings of whichever project is current at that moment. Cached a few seconds per project."""

    def __init__(self, ttl_s: float = 5.0) -> None:
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, Settings]] = {}

    def resolve(self) -> Settings:
        import time

        from klugscheisser import projects

        key = projects.current().key
        hit = self._cache.get(key)
        if hit is None or time.monotonic() - hit[0] > self._ttl:
            hit = (time.monotonic(), get_settings())
            self._cache[key] = hit
        return hit[1]

    def __getattr__(self, name: str):
        return getattr(self.resolve(), name)
