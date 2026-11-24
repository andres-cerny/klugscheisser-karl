#!/usr/bin/env bash
# Branch instance of Karl for the MCP gap loop (docs/mcp-gap-loop.md, section 5). Never the live install.
#
#   scripts/loop_instance.sh reset [--dump FILE] [--worktree DIR]   # klugscheisser_loop new from the newest backup
#   scripts/loop_instance.sh start [DIR] [--auto-refresh] [--collectors]
#   scripts/loop_instance.sh stop
#   scripts/loop_instance.sh status
#   scripts/loop_instance.sh exec [DIR] -- <command …>              # a CLI command with the instance's env
#
# DIR = the worktree / branch checkout to run (default: the checkout this script is in).
# The instance: http://127.0.0.1:8020 (MCP: /mcp/eishockey), localhost only, database klugscheisser_loop only
# (own projects file, no "allgemein" project, scheduled_jobs false: no nightly run, news poller, EP, nightly
# pg_dump). Same answer keys as live (Anthropic, Gemini, Jev, MCP token, read from ~/klugscheisser/.env or the
# environment, never printed). Off unless asked for: auto-refresh (--auto-refresh) and the paid collectors
# Apify / X / Brave (--collectors: passes their keys and the live caps; without it the keys are not passed at
# all). Media analysis is always off. Log: DIR/data/loop-instance.log.
set -euo pipefail

DB=klugscheisser_loop
TEMPLATE=klugscheisser_preview_template # owned by ondrej, has `vector` (~/setup/preview-db-vector.sh)
PORT=8020
HOST=127.0.0.1
UNIT=klug-loop
LIVE="$HOME/klugscheisser"
BACKUPS="$LIVE/backups"
SOCK=/var/run/postgresql
STATE="${KLUG_LOOP_STATE:-$HOME/.local/state/klug-loop}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
DATABASE_URL="postgresql+psycopg:///$DB?host=$SOCK"

die() { echo "ERROR: $*" >&2; exit 1; }
psqlc() { PGOPTIONS="-c client_min_messages=warning" psql -h "$SOCK" -d "${2:-postgres}" -v ON_ERROR_STOP=1 -Atqc "$1"; }
db_exists() { [[ "$(psqlc "select 1 from pg_database where datname = '$1'")" == 1 ]]; }
running() { systemctl --user is-active --quiet "$UNIT"; }
healthy() { curl -fsS -m 3 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; }

check_worktree() {
  local d
  d="$(cd "${1:-$REPO}" 2>/dev/null && pwd -P)" || die "no such folder: ${1:-}"
  [[ -f "$d/pyproject.toml" && -d "$d/src/klugscheisser" ]] || die "$d is not a klugscheisser_athlete checkout"
  local live
  live="$(cd "$LIVE" 2>/dev/null && pwd -P || echo "$LIVE")"
  [[ "$d" != "$live" && "$d" != "$live"/* ]] || die "the live install $LIVE is never used by the loop instance"
  [[ ! -e "$d/.env" ]] || die "$d/.env exists: the instance only runs without .env (it would load live settings)"
  echo "$d"
}

write_projects() {
  mkdir -p "$STATE" && chmod 700 "$STATE"
  cat >"$STATE/projects.yaml" <<EOF
# written by scripts/loop_instance.sh: MCP gap loop instance, never the live databases
default: eishockey
projects:
  eishockey:
    name: Frauen-Eishockey (Loop)
    emoji: "🏒"
    status: active
    database: $DB
    scheduled_jobs: false
    seeds: false
    sport: icehockey_women
EOF
}

# Settings for subprocesses without any key (uv sync, migrations).
plain_env() {
  env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" USER="${USER:-ondrej}" \
    DATABASE_URL="$DATABASE_URL" PROJECTS_FILE="$STATE/projects.yaml" "$@"
}

migrate() {
  local wt="$1"
  write_projects
  (cd "$wt" && "$UV" sync --all-extras --quiet) || die "uv sync failed in $wt"
  (cd "$wt" && plain_env "$UV" run --no-sync klugscheisser project migrate) || die "migration failed"
}

# Instance environment (systemd EnvironmentFile, mode 600). Keys come from the environment (e.g. with-secrets
# klugscheisser) or the live .env; values are never printed.
write_env() {
  write_projects
  LOOP_AUTO_REFRESH="$1" LOOP_COLLECTORS="$2" LOOP_DB_URL="$DATABASE_URL" LOOP_PORT="$PORT" \
    LOOP_HOST="$HOST" LOOP_STATE="$STATE" LIVE_ENV="$LIVE/.env" python3 - <<'PY'
import os, re, secrets
from pathlib import Path

live = {}
p = Path(os.environ["LIVE_ENV"])
if p.is_file():
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            else:  # like python-dotenv: " # comment" after an unquoted value
                v = re.sub(r"\s+#.*$", "", v)
            live[m.group(1)] = v

def key(name):
    return os.environ.get(name) or live.get(name) or ""

collectors = os.environ["LOOP_COLLECTORS"] == "1"
env = {}
# same answer path as live: models, Jev, embeddings, MCP token, answer settings
for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "TYPESAFE_API_KEY", "MCP_AUTH_TOKEN", "EMBEDDING_PROVIDER",
          "NEWS_FETCH_ALL_ARTICLES", "AGENT_MAX_USD", "AUTO_REFRESH_WAIT_S"):
    if key(k):
        env[k] = key(k)
missing = [k for k in ("ANTHROPIC_API_KEY", "MCP_AUTH_TOKEN") if not env.get(k)]
if missing:
    raise SystemExit("ERROR: missing in env and live .env: " + ", ".join(missing))
if collectors:  # paid collectors only on request
    for k in ("APIFY_TOKEN", "X_BEARER_TOKEN", "BRAVE_API_KEY"):
        if key(k):
            env[k] = key(k)
else:
    env.update({"APIFY_DAILY_COST_CAP_USD": "0", "X_DAILY_COST_CAP_USD": "0", "BRAVE_DAILY_CAP": "0",
                "ADD_COST_CAP_CHF": "0", "EVENT_PREP_CAP_CHF": "0",
                # without a key Instagram falls back to the test fixtures: point it at an empty folder so a
                # refresh cannot put fake test posts into the copy
                "FIXTURES_DIR": os.environ["LOOP_STATE"] + "/no-fixtures"})
# Karl's own model spend in the loop DB: $2/day unless LOOP_LLM_CAP_USD says otherwise (loop budget is small)
env["LLM_DAILY_COST_CAP_USD"] = os.environ.get("LOOP_LLM_CAP_USD") or "2"
auto = "true" if os.environ["LOOP_AUTO_REFRESH"] == "1" else "false"
env.update({
    "DATABASE_URL": os.environ["LOOP_DB_URL"],
    "PROJECTS_FILE": os.environ["LOOP_STATE"] + "/projects.yaml",
    "MCP_HOST": os.environ["LOOP_HOST"],
    "MCP_PORT": os.environ["LOOP_PORT"],
    "MCP_ALLOWED_HOSTS": "[]",
    "AUTO_REFRESH": auto,
    "WEB_AUTO_REFRESH": auto,
    "NEWS_POLL_MINUTES": "0",
    "MEDIA_PROVIDER": "off",
    "CLAUDE_CLI_PATH": "/nonexistent/claude",
    "EP_ENABLED": "false",
    "NIGHTLY_CAP_USD": "0",
    "NIGHTLY_FULL_CAP_USD": "0",
    "INSTAGRAM_TEAM_ACCOUNTS": "[]",
    "X_TEAM_ACCOUNTS": "[]",
    "WEB_PASSWORD": secrets.token_urlsafe(24),  # web app not needed: locked with a throwaway password
    "WEB_SECRET": secrets.token_urlsafe(32),
})
out = Path(os.environ["LOOP_STATE"]) / "app.env"
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    for k, v in env.items():
        f.write(f'{k}="' + v.replace("\\", "\\\\").replace('"', '\\"') + '"\n')
PY
}

cmd_reset() {
  local dump="" wt=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dump) dump="$2"; shift 2 ;;
      --worktree) wt="$2"; shift 2 ;;
      *) die "reset: unknown option $1" ;;
    esac
  done
  wt="$(check_worktree "$wt")"
  running && die "the instance is running: first  scripts/loop_instance.sh stop"
  if [[ -z "$dump" ]]; then
    dump="$(ls -1 "$BACKUPS"/eishockey-*.dump 2>/dev/null | sort | tail -1)"
    [[ -n "$dump" ]] || die "no backup eishockey-*.dump in $BACKUPS"
  fi
  [[ -r "$dump" ]] || die "cannot read $dump"
  db_exists "$TEMPLATE" || die "template $TEMPLATE missing (needs sudo once): sudo bash ~/setup/preview-db-vector.sh"
  [[ "$DB" == klugscheisser_loop ]] || die "safety: only klugscheisser_loop may be re-created"
  mkdir -p "$STATE" && chmod 700 "$STATE"
  echo "$DB new from $(basename "$dump") …"
  psqlc "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE)"
  psqlc "CREATE DATABASE \"$DB\" TEMPLATE \"$TEMPLATE\""
  # `vector` is already in the template (owned by postgres): skip its TOC entries
  pg_restore -l "$dump" | grep -Ev '(EXTENSION - |COMMENT - EXTENSION )vector\b' >"$STATE/restore.list"
  local errs="$STATE/restore.err"
  if ! pg_restore --no-owner --no-privileges -h "$SOCK" -d "$DB" -L "$STATE/restore.list" "$dump" 2>"$errs"; then
    if grep -qi error "$errs"; then
      grep -i error "$errs" | head -20 >&2
      die "pg_restore failed ($(grep -ci error "$errs") errors)"
    fi
  fi
  for ext in unaccent pg_trgm vector; do psqlc "CREATE EXTENSION IF NOT EXISTS $ext" "$DB"; done
  # settings Fabi set in the live app (copied with the dump) must not switch background work back on
  psqlc "DELETE FROM project_settings WHERE key IN ('news_poll_minutes','nightly_cap_usd','apify_daily_cost_cap_usd','x_daily_cost_cap_usd','llm_daily_cost_cap_usd','nightly_hour')" "$DB" || true
  migrate "$wt"
  basename "$dump" >"$STATE/db-from"
  echo "ok: $DB from $(basename "$dump"), $(psqlc 'select count(*) from athletes' "$DB") persons"
}

cmd_start() {
  local wt="" auto=0 coll=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --auto-refresh) auto=1; shift ;;
      --collectors) coll=1; shift ;;
      -*) die "start: unknown option $1" ;;
      *) wt="$1"; shift ;;
    esac
  done
  wt="$(check_worktree "$wt")"
  db_exists "$DB" || cmd_reset --worktree "$wt"
  if running; then systemctl --user stop "$UNIT"; fi
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  if curl -s -m 2 -o /dev/null "http://$HOST:$PORT/" 2>/dev/null; then die "port $PORT is already in use"; fi
  migrate "$wt" # the branch may bring a new migration (loop DB only)
  write_env "$auto" "$coll"
  mkdir -p "$wt/data"
  local log="$wt/data/loop-instance.log"
  systemd-run --user --unit="$UNIT" --collect --quiet --working-directory="$wt" \
    -p "EnvironmentFile=$STATE/app.env" -p "StandardOutput=append:$log" -p "StandardError=append:$log" \
    "$UV" run --no-sync klugscheisser serve
  echo "$wt" >"$STATE/worktree"
  printf 'auto_refresh=%s collectors=%s\n' "$auto" "$coll" >"$STATE/flags"
  for _ in $(seq 60); do healthy && break; sleep 1; done
  if ! healthy; then
    tail -30 "$log" >&2 || true
    systemctl --user stop "$UNIT" 2>/dev/null || true
    die "instance did not start (log $log)"
  fi
  echo "running: http://$HOST:$PORT/mcp/eishockey  (code $wt, DB $DB, log $log)"
}

cmd_stop() {
  systemctl --user stop "$UNIT" 2>/dev/null || true
  systemctl --user reset-failed "$UNIT" 2>/dev/null || true
  echo "loop instance stopped."
}

cmd_status() {
  local st="off"
  running && st="running"
  running && healthy && st="running, healthy"
  echo "instance:  $st (unit $UNIT, http://$HOST:$PORT/mcp/eishockey)"
  echo "worktree:  $(cat "$STATE/worktree" 2>/dev/null || echo -)"
  echo "flags:     $(cat "$STATE/flags" 2>/dev/null || echo -)"
  if db_exists "$DB"; then echo "database:  $DB from $(cat "$STATE/db-from" 2>/dev/null || echo '?')"
  else echo "database:  $DB missing (scripts/loop_instance.sh reset)"; fi
}

cmd_exec() {
  local wt=""
  while [[ $# -gt 0 && "$1" != "--" ]]; do wt="$1"; shift; done
  [[ "${1:-}" == "--" && $# -ge 2 ]] || die "usage: loop_instance.sh exec [DIR] -- <command …>"
  shift
  wt="$(check_worktree "$wt")"
  [[ -f "$STATE/app.env" ]] || write_env 0 0
  cd "$wt"
  # load the env file without a shell (values may contain $ or quotes), then run the command
  exec python3 - "$STATE/app.env" "$@" <<'PY'
import os, re, sys
env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "LANG", "USER", "LOGNAME", "TERM")}
for line in open(sys.argv[1], encoding="utf-8"):
    k, _, v = line.rstrip("\n").partition("=")
    if k:
        env[k] = re.sub(r"\\(.)", r"\1", v[1:-1])
os.execvpe(sys.argv[2], sys.argv[2:], env)
PY
}

cmd="${1:-}"
[[ -n "$cmd" ]] && shift
case "$cmd" in
  reset) cmd_reset "$@" ;;
  start) cmd_start "$@" ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  exec) cmd_exec "$@" ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
