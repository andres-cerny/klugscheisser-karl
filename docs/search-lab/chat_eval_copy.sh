#!/usr/bin/env bash
# chat-eval of one code checkout against a COPY database (never the live DBs, never klugscheisser_loop,
# never ~/klugscheisser). Make the copy first, e.g. `createdb -T klugscheisser_searchlab klugscheisser_chatcheck`
# (one copy per run, so the runs cannot see each other's writes). Paid: Claude Sonnet + Jev + Gemini query vectors, about $0.9-1.0 per run (65 questions).
#
#   with-secrets jobscan bloodwork -- bash chat_eval_copy.sh <checkout dir> <label> <copy db> [out dir]
#
# Keys: ANTHROPIC_API_KEY + GEMINI_API_KEY (bloodwork.env, borrowed from Klugscheisser) and TYPESAFE_API_KEY
# (jobscan.env) from the environment. Collector keys are blanked: no Apify / Brave / X calls.
set -euo pipefail
DIR="$(cd "$1" && pwd -P)"; LABEL="$2"; DB="$3"; OUT="${4:-$DIR/data/chat-eval}"
case "$DB" in klugscheisser|klugscheisser_allgemein|klugscheisser_loop|"") echo "not a copy DB: '$DB'" >&2; exit 2;; esac
[[ "$DIR" != "$HOME/klugscheisser"* ]] || { echo "never the live install" >&2; exit 2; }
[[ ! -e "$DIR/.env" ]] || { echo "$DIR/.env exists (would load other settings)" >&2; exit 2; }
for k in ANTHROPIC_API_KEY GEMINI_API_KEY TYPESAFE_API_KEY; do [[ -n "${!k:-}" ]] || { echo "missing $k" >&2; exit 2; }; done
mkdir -p "$OUT"
cd "$DIR"
env DATABASE_URL="postgresql+psycopg:///$DB?host=/var/run/postgresql" PROJECTS_FILE="" \
  EMBEDDING_PROVIDER=gemini AUTO_REFRESH=false SCHEDULED_JOBS=false \
  LLM_DAILY_COST_CAP_USD=5 AGENT_MAX_USD=0.04 ANSWER_MAX_USD=0.08 \
  APIFY_TOKEN= BRAVE_API_KEY= X_BEARER_TOKEN= XAI_API_KEY= FIRECRAWL_API_KEY= META_GRAPH_TOKEN= \
  uv run klugscheisser chat-eval --yes 2>&1 | tee "$OUT/$LABEL.txt"
cp "$(ls -t data/chat-eval/*.json | head -1)" "$OUT/$LABEL.json"
echo "saved: $OUT/$LABEL.txt and .json"
