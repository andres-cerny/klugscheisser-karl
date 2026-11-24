# Klugscheisser Karl: where time and money go (MCP / Perplexity, briefly the web chat)

Read-only analysis, 2026-10-25. Data: journal of `klugscheisser.service` 2026-10-10 → 2026-10-25 (readable,
no permission issue), live DB `klugscheisser` (SELECT only), live code at 9aa5717. No paid calls, no changes.

## In one paragraph

Karl is used lightly: about **100 Perplexity tool calls in 14 days, in 5 sessions** (11.10., 16.10., 17.10.,
19.10., 24.10.). All of Karl's own time for those calls adds up to **about 5 minutes**: 141 s of tool time
since timing logs started on 11.10. at 23:00, plus about 160 s spent waiting for refreshes on 11.10. What
Perplexity/Fabi paid for in money is tiny: **$0.33 for 69 timed calls** (about $0.005 per call; a `frage`
that has to be written by Claude costs $0.02–0.06). Most money goes elsewhere: **intake** (X, Apify, about
$0.20 on a normal day, $4.97 on the one-off full run on 15.10.) and **our own test runs** (`chat-eval`, about
$0.90 per run, three runs = $2.68, 8× what Fabi's questions cost). Most of the time goes to `frage` (82% of
Perplexity tool time), and inside `frage` to **Claude writing/checking** and **one runaway answer** (562
statements, 33.7 s, 140,228 chars), which the 12k cap and `MAX_CROSS_STATEMENTS=80` from PR #33 now cover.
A short-term MCP result cache would bring **almost nothing**: Perplexity never sent the exact same call
twice within 10 minutes.

## Top time sinks (ranked, Perplexity calls)

| # | What | Numbers | Status |
|---|------|---------|--------|
| 1 | **Waiting for the auto-refresh (old 60 s setting)** | 11.10.: 3 calls waited 60.1 s, 60.5 s, 39.2 s (whats_new / get_athlete_posts on Alina Müller); about 160 s in all | Fixed by `AUTO_REFRESH_WAIT_S=3` (11.10.). |
| 2 | **One runaway cross-person answer** | `frage` "Bei welchen Klubs spielen die Spielerinnen des aktuellen Kaders der finnischen …" (24.10. 15:45): 33.7 s, of which 33.1 s was "web+rest" = translating the quotes of 562 statements (104 new translations, $0.062); 140,228 chars sent to Perplexity | Fixed in PR #33 (live 21:44 on 24.10., after this call): 80 statements max, 12k chars max. No Perplexity call since to confirm. |
| 3 | **Claude writing + checking (`antwort+pruefung`)** | 25 engine `frage` calls: 35.0 s in all, p50 2.6 s, max 8.8 s; makes up most of the 9–12 s answers | The main cost of a normal paid answer. |
| 4 | **Fixed overhead of every engine `frage`: candidates + Jev rerank** | `kandidaten` (Postgres reads up to 600 documents and builds them) 12.2 s in all, p50 1.3 s; `rerank` (Jev, about 15 parallel TypeSafe requests per call) 10.0 s in all, p50 1.2 s, so about 2.5 s per answer before Claude even starts | Not split further (see "what to measure"). |
| 5 | **`search` tool** | 16 calls, each 0.73–1.25 s (1 Gemini query vector + 1 Jev rerank per call); 14.8 s in all | Fine. |
| 6 | **Parallel fan-out by Perplexity** | 24.10. 15:47: 12 `frage` calls at once ("Bei welchem Klub spielt X …?"), all came back empty (0 statements, about 300 chars). Calls that take 0.1 s alone took 0.5–1.2 s in parallel; one took 6.2 s because `scope` (5.65 s) fetched a Wikipedia profile for a player we had never looked up (`ensure_profiles`, done inside the request) | Fan-out is Perplexity's choice; the empty answers and the in-request Wikipedia fetch are ours. |
| 7 | **3 s refresh wait in `get_athlete`** | 17.10.: 2 calls hit the full 3 s (Lucie Tenenbaum 3.14 s, Remo Egger 3.13 s); the refresh jobs then took 11 s and 25 s and found **0 new items** | Small, but so far it never paid off. |

Everything else is fast: rule shortcuts in `frage` (staff, games, tournament, connections) take 0.04–0.32 s,
`list_events`/`stats`/`status` 0.03–0.07 s, `get_team_roster` 0.5–0.9 s, `get_event` 1.1 s.

**Not visible to us, but probably bigger for Fabi:** how long Perplexity itself takes to read our answers.
The median answer was 4,217 chars, p90 29,021, and 23 of 69 were over 12k (7 over 30k: search up to 41,664,
get_athlete Lara Stalder 55,291, the frage above 140,228). All of these came before the 12k cap went live, so
the cap should already help Perplexity's side noticeably.

## Per MCP tool (timed calls, 11.10. 23:00 → 25.10.; Perplexity + test harness)

| tool | calls | p50 s | p90 s | max s | chars p50 | chars max | paid |
|------|------:|------:|------:|------:|----------:|----------:|------|
| frage | 39 | 0.57 | 9.16 | 33.68 | 1,985 | 140,228 | 14 of 39, avg $0.027, max $0.093 |
| search | 17 | 0.94 | 1.09 | 1.25 | 16,942 | 41,664 | $0 in ledger (Gemini vector + Jev not recorded, see below) |
| get_athlete | 14 | 0.20 | 2.33 | 3.14 | 8,699 | 55,291 | 3 calls started Apify refreshes, $0.014 |
| list_events | 6 | 0.04 | 0.07 | 0.07 | 2,528 | 15,573 | $0 |
| get_team_roster | 3 | 0.85 | 0.91 | 0.92 | 6,341 | 6,341 | $0 |
| get_event | 2 | 1.14 | 1.17 | 1.18 | 15,293 | 19,795 | $0 |
| stats | 2 | 0.05 | 0.05 | 0.05 | 5,278 | 6,073 | $0 |
| status | 1 | 0.06 | – | 0.06 | 423 | 423 | $0 |

- **Who called:** 69 calls `Perplexity-MCP/1.0` (4 AWS IP ranges 44.210/44.214/44.221/54.234), 15 calls
  `python-httpx2` (our gap harness / stand-in), plus curl from this server on 11.10. Before the timing log
  (11.10. 21:08–21:18) there were 38 more calls: 25 `stats` (23 of them within 5 s, one per player), 8
  `get_athlete_posts`, 2 `get_athlete`, 2 `frage`, 1 `whats_new`.
- **Perplexity only:** 69 calls, p50 0.73 s, p90 5.97 s, max 33.68 s, 141 s in all; `frage` 34 calls = 82% of
  the time (25 went through the answer engine, 9 were rule shortcuts or cache hits).
- **Concurrency:** 48 of 84 calls ran alone. Perplexity fans out: bursts of 3 `get_athlete` (17.10.), 6 mixed
  (24.10.), 12 `frage` (24.10.), 23 `stats` (11.10.). Max 5 in flight at once by the timing log.
- **Errors/timeouts:** no tool errors (`(error)` flag never set), no 5xx on `/mcp`, no warnings from the
  answer path. 4× 401 (curl/Mac browser without token). The 60 s waits on 11.10. still returned 200.

## Cost: per call and per day

**Query time (14 days), who caused it:**

| caller | calls | $ | note |
|--------|------:|--:|------|
| Perplexity (Fabi) via MCP | 69 timed (+38 before) | **0.333** | frage $0.31 of it; paid Claude frage calls $0.020–0.062 each (avg ~$0.036 since the Sonnet switch on 17.10.); Gemini era (16.10.) $0.0015–0.0025 |
| Web chat (16.10., 5 answers, 4.2–12.4 s) | 5 | ~0.03 | Gemini at the time |
| Gap harness (python-httpx) | 15 | 0.061 | |
| **chat-eval runs (CLI, not in the service log)** | 3 runs | **~2.68** | 17.10. 15:40–15:46 $0.87, 19.10. 10:40–10:51 $0.90, 24.10. 15:10–15:15 $0.91 |
| MCP-triggered refreshes (Apify) | 6 jobs | ~0.035 | 11.10. + 17.10. |
| Bot page views 19.10. 10:40 (headless Chrome from this server's own IP, 80 `?tab=chat` person pages in 90 s) | 84 refresh jobs | **0.338** Apify | Job queue up to 15 min. Page views start paid refreshes. |

**Per day by provider (cost_ledger, all uses):**

| day | Claude | Gemini | Jev | Apify | X | total |
|-----|-------:|-------:|----:|------:|--:|------:|
| 10-11 | 0 | 0.003 | – | 0.032 | 0.025 | 0.06 |
| 10-12…10-14 | 0 | ≤0.003 | – | 0.13–0.21 | ~0.02 | 0.14–0.23 |
| 10-15 (full run once) | 0 | 0.006 | – | 0.854 | 4.110 | 4.97 |
| 10-16 | 0 | 0.025 | 0 | 0.150 | 0.030 | 0.21 |
| 10-17 | 0.945 | – | 0 | 0.173 | 0.020 | 1.14 |
| 10-18 | 0.081 | – | – | 0.168 | 0.015 | 0.27 |
| 10-19 | 0.908 | – | 0 | 0.581 | 0.020 | 1.51 |
| 10-20…10-23 | 0.02–0.07 | – | – | 0.09–0.16 | 0.01–0.03 | 0.16–0.20 |
| 10-24 | 1.069 | – | 0.119 | 0.159 | 0.010 | 1.36 |
| 10-25 (to 16:45) | 0.089 | – | – | 0.168 | 0.010 | 0.27 |

Brave: **0 calls** (Karl's own web search is off by default, `answer_web_search`). 15-day total $11.07:
X $4.36, Apify $3.31, Claude answers $2.75 (of which chat-eval about $2.7), Claude facts $0.38 (nightly),
Claude translate $0.12, Jev $0.12, Gemini $0.04. The media worker runs on the Claude subscription
(`llm:claude-cli`, $0 billed).

**Intake vs query time:** on a normal day intake is about $0.15–0.25 (Apify Instagram ~$0.10–0.17, X
~$0.015, Claude facts a few cents) and Fabi's questions are $0–0.31. The Claude spikes on 17./19./24.10.
are our chat-eval runs, not Fabi.

**Gaps in the ledger (costs we cannot see):**
- Jev during intake: ~1,300–3,000 TypeSafe requests per day (30,135 in 15 days) are **not** in
  `cost_ledger`; only Jev inside answers is, and only since 24.10. (before: $0 rows).
- The `search` tool's Jev rerank and Gemini query vector are not recorded (they bypass the answer budget).
- Gemini embeddings for intake (793 Gemini requests on 15.10.) are not recorded.

## Caching

- **Answer cache (`answer_cache`, `frage` only):** active for MCP (list style, 24 h, dropped as soon as a
  newer document exists for the asked persons; with no person, any newer document anywhere). Seen working
  once: "Wann spielt die Schweiz das nächste Mal?" 8.92 s → 0.10 s on repeat 19 s later (that repeat was the
  gap harness, not Perplexity). Hits are not logged; 39 rows in the table (18 from 24.10., incl. chat-eval).
- **Translations (`translations`, persistent):** works. In the 562-statement call only 104 quotes were new
  (4×25+4 in one batch, $0.062); 148 new translations in all for 14 days.
- **Query vectors:** no cache (the `search` tool asks Gemini each time, ~1 request per call). `frage` uses
  no embeddings (`RETRIEVAL_EMBEDDINGS` off).
- **Collector caches (`cache_entries`)** are for intake (facts, profiles), not the MCP path.
- **Identical or near-identical Perplexity calls:** 0 exact repeats within 10 minutes. Exact repeats within
  24 h: `list_events(team=Tschechien)` after 13 min (harness, then Perplexity), `get_team_roster(Schweiz)`
  after 37 min. Near-identical rewordings across days ("erste WM im November?" 16.10. and 17.10.; WM
  schedule in German, then English) would not hit an exact-key cache anyway.
- **Gain of a short-TTL MCP result cache:** on this data about 0 s and $0 (at most 1–2 hits in 100 calls,
  each saving ≤1 s on non-frage tools; `frage` repeats are already covered by the answer cache). **Not
  worth building now.** Revisit if usage grows or Perplexity starts repeating calls.

## Auto-refresh

- **`frage` never waits** (`ensure_fresh(wait_s=0)`): it starts a refresh in the background and answers
  from what is stored. `get_athlete`, `get_athlete_posts`, `whats_new`, `get_event`, `search` (with a
  person/team) wait up to `AUTO_REFRESH_WAIT_S` (3 s live).
- **MCP-triggered refreshes, 14 days:** 6 jobs. 11.10. (60 s limit then): 4 jobs, 3 calls waited
  39–60 s, the jobs took 19–72 s, 1 of 4 found something new (1 Instagram post; job 19 also 11 results).
  17.10. (3 s): 2 jobs, both calls waited the full 3 s, jobs took 11 s and 25 s, **0 new items**. 24.10.: no
  refresh at all (the 05:00 nightly had freshened everyone).
- **All 94 auto_update jobs** (mostly from the 19.10. page crawl): average 27 s, max 72 s; only 14 (15%)
  found new Instagram posts (26 posts in all); Apify $0.38; 4 failed.
- So far the waiting has **never** changed a Perplexity answer inside the 3 s window (a refresh takes
  11–72 s, p50 23 s; the 3 s wait can basically never finish in time).

## Quick wins

1. **Check that PR #33 holds** in the next Fabi session: no answer over 12k chars, no `web+rest` over 1–2 s.
   It removes the worst single call (33.7 s, $0.062, 140k chars).
2. **Set `AUTO_REFRESH_WAIT_S=0`** (do what `frage` already does): a refresh takes p50 23 s, so a 3 s wait
   never finishes in time; it only adds 3 s when someone was not fresh. Settings change only, easy to undo.
3. **Move `ensure_profiles` (Wikipedia for never-seen persons) out of the request**, into the background
   like the refresh: it caused the 5.65 s `scope` on 24.10.
4. **No paid refresh from page views by bots / our own probe:** 19.10. 10:40 a headless browser from this
   server opened 80 person pages, which started 84 Apify jobs ($0.34, 15-min queue). For example skip
   `ensure_fresh` for requests from 127.0.0.1/this server's IP or headless UAs, or only refresh on a real
   chat question.
5. **The empty fan-out answers:** "Bei welchem Klub spielt X in der Saison 2026/27?" returned 0 statements
   12 times (not checked whether we store those clubs). Worth a gap-loop case: a single-person club
   question should hit a stored-club shortcut, or say clearly that we don't know (this is about quality
   more than time).
6. **chat-eval costs:** about $0.90 per run is the biggest Claude spend. Fine when it is a deliberate
   choice; run it only when the chat/answer code really changed.
7. Side finding (intake): the media worker logged "media analysis rate limited, stopping" **6,032 times on
   22.–23.10.** (about every 30 s). It should wait much longer after a rate limit.

## What to measure better (minimal logging)

Missing today:
- The `tool … done` line carries no **user agent / request id / cost / cache hit**: today calls are matched
  to Perplexity by timestamps (good enough at this volume, but can go wrong in bursts).
- `cost_ledger` has **no duration** and no call id; `created_at` is the transaction start, so it cannot
  show how long a Claude or Jev call took.
- Engine steps are coarse: `antwort+pruefung` does not split Claude writing, Jev check and repair;
  `kandidaten` does not split Postgres from building documents; `web+rest` hides the quote translation;
  `scope` hides `ensure_profiles`.
- No log line for answer-cache hit/miss, nor for how long the refresh wait took.
- The `search` tool's Gemini + Jev calls and all intake Jev calls are not in the ledger.

Proposed (a few lines each):
1. In `AccessLogMiddleware`, set a contextvar `req_id` (+ UA); in `timed()` log one line:
   `mcp_call req=… tool=… dur=… chars=… capped=… ua=… cost=$… cache=hit|miss|none refresh=waited:x.xs|bg|none`.
   The cost comes from the request's `llm_budget` (already per answer).
2. In the engine, more `lap()` points: `schreiben`, `pruefen`, `reparatur`, `uebersetzen`, `render`,
   `profile` (inside scope), `db` (inside kandidaten).
3. A `duration_ms` column (or a log line with ms) in `costs.record` for every provider call (Claude, Gemini,
   Jev, Apify, X), plus `req_id` when inside a request.
4. Record intake Jev and the `search` tool's Jev/Gemini usage in `cost_ledger` (even with an estimated
   price), so the provider totals are complete.

---

## Technical appendix

Files (scratchpad, `/tmp/claude-1000/-home-ondrej-projects/cb48e24d-11a7-4859-b3cf-28d2945e9b11/scratchpad/searchjev/`):
- `parse.py`: puts rich's wrapped log lines back together into one record per entry (`ev.tsv`).
- `analyze.py`: matches `tool X done in` with its start line (same pid, start = end − duration ± 1.6 s), with
  the access-log POST (same end ± 1.5 s, same duration ± 1.5 s → UA), with the engine `frage answered in`
  line, and with `cost_ledger` rows inside [start − 1 s, end + 2 s]. Output: `out1.txt`.
- `q2.py`: lists the non-frage calls. `ledger.csv`: ledger export.
- `journal.txt` (deleted after parsing, 327 MB): journal dump with secrets redacted while streaming (bearer/key=/token=/x-api-key/sk-ant/AIza); `ev.tsv` (98 MB) is the parsed version.

Log sources used:
- `server.py` `timed()`: `tool <name> done in <s> s, <chars> chars[ (error)]` (since 11.10. 23:00) and
  `tool <name> answer: <preview>`.
- `server.py` `_run()` / `frage`: `tool <fn> args=… kwargs=…` / `tool frage '<q>' person=… team=… event=…`.
- `mcp_server/auth.py` `AccessLogMiddleware`: `POST /mcp/<project> -> <status> <ms>ms … ua=… auth=<header
  names only> ip=…`.
- `answer/engine.py`: `frage answered in <s>s (<step>=<s> …): n ok, n unsure, n dropped, $cost` and
  `answer budget: $… (model $…, Jev $…, n calls, n refused)`.
- httpx `HTTP Request: <METHOD> <url>` (host counts in call windows).

Commands / queries:

```bash
journalctl -u klugscheisser.service --since 2026-10-10 --no-pager -o short-iso \
  | sed -E 's/(bearer |key=|token=|x-api-key: )[A-Za-z0-9._~+\/=-]{6,}/\1***/Ig; s/sk-ant[A-Za-z0-9_-]*/***/g; s/AIza[A-Za-z0-9_-]*/***/g' > journal.txt
python3 -I parse.py journal.txt ev.tsv && python3 -I analyze.py .
```

```sql
-- cost by source
select source, count(*), round(sum(cost_usd)::numeric,3) usd, sum(units) from cost_ledger
 where created_at >= '2026-10-10' group by 1 order by usd desc nulls last;
-- per day by provider
select (created_at at time zone 'Europe/Zurich')::date d,
  round(sum(cost_usd) filter (where source like 'llm:claude%')::numeric,3) claude,
  round(sum(cost_usd) filter (where source like 'llm:gemini%')::numeric,3) gemini,
  round(sum(cost_usd) filter (where source like 'jev%')::numeric,3) jev,
  round(sum(cost_usd) filter (where source like 'apify%')::numeric,3) apify,
  round(sum(cost_usd) filter (where source like 'x%')::numeric,3) x,
  round(sum(cost_usd)::numeric,3) total
 from cost_ledger where created_at >= '2026-10-10' group by 1 order by 1;
-- query-time ledger by hour (finds chat-eval runs: clusters outside MCP sessions)
select to_char(date_trunc('hour', created_at at time zone 'Europe/Zurich'),'MM-DD HH24h'), count(*),
  round(sum(cost_usd)::numeric,3) from cost_ledger where created_at >= '2026-10-10'
  and (source = 'llm:claude-answer' or source in ('jev','llm:claude-translate','llm:gemini-answer')) group by 1 order by 1;
-- ledger export for correlation
\copy (select id, source, units, cost_usd, left(note,80), to_char(created_at at time zone 'Europe/Zurich',
  'YYYY-MM-DD"T"HH24:MI:SS.MS') from cost_ledger where created_at >= '2026-10-10' order by created_at) to 'ledger.csv' csv
-- refresh jobs: duration, queue time, did they find anything
select kind, status, count(*), avg(extract(epoch from finished_at-started_at)),
  max(extract(epoch from finished_at-started_at)) from jobs where created_at >= '2026-10-10' group by 1,2;
select id, created_at, extract(epoch from started_at-created_at) queue_s, extract(epoch from finished_at-started_at) run_s,
  params->'athlete_ids', result->'instagram'->>'items_new', result->'instagram'->>'cost_usd'
  from jobs where kind='auto_update' and created_at >= '2026-10-10' order by id;
select count(*), sum((result->'instagram'->>'items_new')::int),
  count(*) filter (where (result->'instagram'->>'items_new')::int > 0),
  sum((result->'instagram'->>'cost_usd')::numeric) from jobs where kind='auto_update' and created_at >= '2026-10-10';
-- caches
select (created_at at time zone 'Europe/Zurich')::date, count(*) from answer_cache group by 1 order by 1;
select (created_at at time zone 'Europe/Zurich')::date, model, count(*) from translations
  where created_at >= '2026-10-10' group by 1,2 order by 1;
select source, count(*), max(fetched_at) from cache_entries group by 1 order by 2 desc;
-- web chat volume
select (created_at at time zone 'Europe/Zurich')::date, count(*) from chat_messages
  where created_at >= '2026-10-10' group by 1;
```

Caveats: the timing log exists only since 11.10. 23:00 (earlier calls: access-log ms only). Ledger rows
are matched to calls by time; in overlapping calls (24.10. 15:45:40 and 15:45:48) costs were split by the
engine's own `answer budget` lines ($0.0314 and $0.0619). chat-eval runs are spotted from ledger clusters
outside any MCP or web request (CLI runs do not write to the service journal).
