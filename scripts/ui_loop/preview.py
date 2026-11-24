#!/usr/bin/env python3
"""Private preview instance of the web app for the /ui-loop (docs/ui-loop.md).

    python3 scripts/ui_loop/preview.py db-refresh [--max-age-hours 24]
    python3 scripts/ui_loop/preview.py start <worktree> [--tunnel]
    python3 scripts/ui_loop/preview.py tunnel        # public https://….trycloudflare.com link
    python3 scripts/ui_loop/preview.py restart [<worktree>]
    python3 scripts/ui_loop/preview.py stop
    python3 scripts/ui_loop/preview.py status

Safety (never the live install): the database is always `klugscheisser_preview` (copy of the newest
nightly dump in ~/klugscheisser/backups, read only), the app runs from a worktree without `.env` with a short
allow-list of environment variables (no API keys at all), every paid or scraping collector switched off, and
its own projects file with `scheduled_jobs: false` (no nightly run, no news poller, no nightly pg_dump).
Standard library only, so it runs with plain `python3`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PREVIEW_DB = "klugscheisser_preview"
TEMPLATE_DB = "klugscheisser_preview_template"  # made once by ~/setup/preview-db-vector.sh (has `vector`)
PROTECTED_DBS = {
    "klugscheisser",
    "klugscheisser_allgemein",
    "klugscheisser_test",
    "postgres",
    "template0",
    "template1",
}
PORT = 8010
HOST = "127.0.0.1"
APP_UNIT = "klug-preview"
TUNNEL_UNIT = "klug-preview-tunnel"
LIVE = Path.home() / "klugscheisser"
BACKUPS = LIVE / "backups"
LIVE_ENV = LIVE / ".env"
STATE = Path(os.environ.get("KLUG_PREVIEW_STATE", Path.home() / ".local/state/klug-preview"))
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
CLOUDFLARED = shutil.which("cloudflared") or "/usr/local/bin/cloudflared"
SOCKET = "/var/run/postgresql"
DATABASE_URL = f"postgresql+psycopg:///{PREVIEW_DB}?host={SOCKET}"
ROOT_HINT = "sudo bash ~/setup/preview-db-vector.sh"

# Everything that could cost money, scrape or touch anything outside the preview DB is off. API keys are simply
# never passed (the app's features are "on iff key present"); these switches cover the free collectors too.
SAFE_ENV = {
    "DATABASE_URL": DATABASE_URL,
    "MCP_HOST": HOST,
    "MCP_PORT": str(PORT),
    "TIMEZONE": "Europe/Zurich",
    "LLM_PROVIDER": "fake",
    "EMBEDDING_PROVIDER": "fake",
    "MEDIA_PROVIDER": "off",
    "CLAUDE_CLI_PATH": "/nonexistent/claude",  # media via Ondrej's Claude subscription: never
    "WEB_ANSWER_ENGINE": "fake",
    "ANSWER_INTERPRETER": "false",
    "ANSWER_AGENT": "false",
    "ANSWER_WEB_SEARCH": "false",
    "AUTO_REFRESH": "false",
    "WEB_AUTO_REFRESH": "false",
    "INSTAGRAM_SOURCE": "fixture",
    "NEWS_POLL_MINUTES": "0",
    "NEWS_FETCH_ALL_ARTICLES": "false",
    "PROFILES_ENABLED": "false",
    "PROFILES_COLLEGE_ENABLED": "false",
    "SIHF_DATA_ENABLED": "false",
    "IIHF_REALTIME_ENABLED": "false",
    "PWHL_ENABLED": "false",
    "SDHL_ENABLED": "false",
    "EP_ENABLED": "false",
    "APIFY_DAILY_COST_CAP_USD": "0",
    "LLM_DAILY_COST_CAP_USD": "0",
    "X_DAILY_COST_CAP_USD": "0",
    "NIGHTLY_CAP_USD": "0",
    "NIGHTLY_FULL_CAP_USD": "0",
    "ADD_COST_CAP_CHF": "0",
    "EVENT_PREP_CAP_CHF": "0",
    "BRAVE_DAILY_CAP": "0",
    "INSTAGRAM_TEAM_ACCOUNTS": "[]",
    "X_TEAM_ACCOUNTS": "[]",
}

PROJECTS_YAML = f"""# written by scripts/ui_loop/preview.py — preview only, never the live databases
default: eishockey
projects:
  eishockey:
    name: Frauen-Eishockey (Vorschau)
    emoji: "🏒"
    status: active
    database: {PREVIEW_DB}
    scheduled_jobs: false
    seeds: false
    sport: icehockey_women
"""


def die(msg: str, code: int = 1) -> None:
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def psql(sql: str, db: str = "postgres") -> str:
    r = run(["psql", "-h", SOCKET, "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc", sql], capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def guard_db(name: str) -> None:
    """Only the preview database may ever be dropped or recreated."""
    if name != PREVIEW_DB or name in PROTECTED_DBS:
        die(f"Sicherung: nur {PREVIEW_DB} darf neu gemacht werden, nicht {name!r}")


def db_exists(name: str) -> bool:
    return psql(f"select 1 from pg_database where datname = '{name}'") == "1"


def newest_dump() -> Path:
    dumps = sorted(BACKUPS.glob("eishockey-*.dump"))
    if not dumps:
        die(f"keine Sicherung eishockey-*.dump in {BACKUPS}")
    return dumps[-1]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def check_worktree(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not (path / "pyproject.toml").is_file() or not (path / "src/klugscheisser").is_dir():
        die(f"{path} ist kein klugscheisser_athlete-Checkout")
    if path == LIVE.resolve() or LIVE.resolve() in path.parents:
        die("die Live-Installation ~/klugscheisser wird nie für die Vorschau benutzt")
    if (path / ".env").exists():
        die(f"{path}/.env existiert: die Vorschau läuft nur ohne .env (sonst würden API-Keys geladen)")
    return path


# --- secrets (never printed) ----------------------------------------------------------------------


def read_live_env_value(key: str) -> str | None:
    """One value from the live .env, read without printing anything."""
    if not LIVE_ENV.is_file():
        return None
    for line in LIVE_ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$", line)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            return v or None
    return None


def web_password() -> str:
    """WEB_PASSWORD from the environment (if set) or the live .env. Never printed."""
    pw = os.environ.get("WEB_PASSWORD") or read_live_env_value("WEB_PASSWORD")
    if not pw:
        die("WEB_PASSWORD weder in der Umgebung noch in ~/klugscheisser/.env")
    return pw


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def app_env_file() -> Path:
    """systemd EnvironmentFile (mode 600): the safe switches + password + fresh session/MCP secrets."""
    env = dict(SAFE_ENV)
    env["PROJECTS_FILE"] = str(STATE / "projects.yaml")
    env["WEB_PASSWORD"] = web_password()
    env["WEB_SECRET"] = secrets.token_urlsafe(32)
    env["MCP_AUTH_TOKEN"] = secrets.token_urlsafe(32)  # /mcp is not needed in the preview: locked
    # systemd EnvironmentFile: KEY="value" with \ and " escaped
    lines = [f'{k}="' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for k, v in env.items()]
    path = STATE / "app.env"
    write_private(path, "\n".join(lines) + "\n")
    write_private(STATE / "projects.yaml", PROJECTS_YAML)
    return path


# --- db-refresh -----------------------------------------------------------------------------------


def cmd_db_refresh(args: argparse.Namespace) -> None:
    stamp = STATE / "db-refreshed"
    if args.max_age_hours and stamp.exists() and db_exists(PREVIEW_DB):
        age_h = (time.time() - stamp.stat().st_mtime) / 3600
        if age_h < args.max_age_hours:
            print(f"{PREVIEW_DB} ist {age_h:.1f} h alt (< {args.max_age_hours} h): bleibt.")
            return
    if not db_exists(TEMPLATE_DB):
        die(
            f"Vorlage {TEMPLATE_DB} fehlt (Postgres-Erweiterung vector braucht einmal sudo). "
            f"Ondrej führt einmal aus:  {ROOT_HINT}"
        )
    if unit_active(APP_UNIT):
        die("die Vorschau läuft noch: zuerst  preview.py stop")
    dump = newest_dump()
    guard_db(PREVIEW_DB)
    print(f"{PREVIEW_DB} neu aus {dump.name} …")
    psql(f'DROP DATABASE IF EXISTS "{PREVIEW_DB}" WITH (FORCE)')
    psql(f'CREATE DATABASE "{PREVIEW_DB}" TEMPLATE "{TEMPLATE_DB}"')
    # `vector` already exists in the template (owned by postgres): skip its entries so the restore does not
    # try to COMMENT on an extension ondrej does not own (pg_trgm / unaccent are trusted: restored normally)
    toc = run(["pg_restore", "-l", str(dump)], capture_output=True, check=True).stdout
    keep = [
        ln for ln in toc.splitlines() if not re.search(r"\b(EXTENSION - |COMMENT - EXTENSION )vector\b", ln)
    ]
    listfile = STATE / "restore.list"
    write_private(listfile, "\n".join(keep) + "\n")
    r = run(
        [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "-h",
            SOCKET,
            "-d",
            PREVIEW_DB,
            "-L",
            str(listfile),
            str(dump),
        ],
        capture_output=True,
    )
    errors = [ln for ln in r.stderr.splitlines() if "error" in ln.lower()]
    if r.returncode and errors:
        print("\n".join(errors[:20]), file=sys.stderr)
        die(f"pg_restore: {len(errors)} Fehler")
    for ext in ("unaccent", "pg_trgm", "vector"):
        psql(f"CREATE EXTENSION IF NOT EXISTS {ext}", PREVIEW_DB)
    worktree = check_worktree(Path(args.worktree) if args.worktree else repo_root())
    migrate(worktree)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(dump.name + "\n")
    n = psql("select count(*) from athletes", PREVIEW_DB)
    print(f"ok: {PREVIEW_DB} aus {dump.name}, {n} Personen, Migrationen von {worktree}")


def sync_venv(worktree: Path) -> None:
    r = run([UV, "sync", "--all-extras", "--quiet"], cwd=worktree, capture_output=True)
    if r.returncode:
        die(f"uv sync in {worktree}: {r.stderr.strip()[-500:]}")


def clean_env() -> dict[str, str]:
    """Environment for subprocesses that load app settings: allow-list only, no keys."""
    keep = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "USER", "LOGNAME") if k in os.environ}
    return {**keep, **SAFE_ENV, "PROJECTS_FILE": str(STATE / "projects.yaml")}


def migrate(worktree: Path) -> None:
    write_private(STATE / "projects.yaml", PROJECTS_YAML)
    sync_venv(worktree)
    r = run(
        [UV, "run", "klugscheisser", "project", "migrate"], cwd=worktree, env=clean_env(), capture_output=True
    )
    if r.returncode:
        die(f"Migration: {(r.stderr or r.stdout).strip()[-800:]}")
    print(r.stdout.strip())


# --- app + tunnel (transient systemd user units) --------------------------------------------------


def unit_active(unit: str) -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def stop_unit(unit: str) -> None:
    run(["systemctl", "--user", "stop", unit], capture_output=True)
    run(["systemctl", "--user", "reset-failed", unit], capture_output=True)


def healthy(timeout_s: float = 60) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def port_busy() -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


def cmd_start(args: argparse.Namespace) -> None:
    worktree = check_worktree(Path(args.worktree))
    if not db_exists(PREVIEW_DB):
        die(f"{PREVIEW_DB} fehlt: zuerst  preview.py db-refresh")
    if unit_active(APP_UNIT):
        stop_unit(APP_UNIT)
    if port_busy():
        die(f"Port {PORT} ist schon belegt (anderes Programm)")
    sync_venv(worktree)
    migrate(worktree)  # the branch may bring a new migration; preview DB only
    envfile = app_env_file()
    log = STATE / "app.log"
    stop_unit(APP_UNIT)
    r = run(
        [
            "systemd-run",
            "--user",
            f"--unit={APP_UNIT}",
            "--collect",
            "--quiet",
            f"--working-directory={worktree}",
            "-p",
            f"EnvironmentFile={envfile}",
            "-p",
            f"StandardOutput=truncate:{log}",
            "-p",
            "StandardError=inherit",
            UV,
            "run",
            "--no-sync",
            "klugscheisser",
            "serve",
        ],
        capture_output=True,
    )
    if r.returncode:
        die(f"systemd-run: {r.stderr.strip()}")
    (STATE / "worktree").write_text(str(worktree) + "\n")
    if not healthy():
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        stop_unit(APP_UNIT)
        die(f"die Vorschau startet nicht (Log {log}):\n{tail}")
    print(f"Vorschau läuft: http://{HOST}:{PORT}/p/eishockey/  (Code: {worktree}, Log: {log})")
    if args.tunnel:
        cmd_tunnel(args)


def cmd_restart(args: argparse.Namespace) -> None:
    saved = STATE / "worktree"
    wt = args.worktree or (saved.read_text().strip() if saved.exists() else None)
    if not wt:
        die("welcher Worktree? preview.py restart <worktree>")
    tunnel = unit_active(TUNNEL_UNIT)
    cmd_start(argparse.Namespace(worktree=wt, tunnel=False))
    if tunnel:
        print(f"Tunnel läuft weiter: {tunnel_url() or '?'}")


def tunnel_url() -> str | None:
    log = STATE / "tunnel.log"
    if not log.exists():
        return None
    found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(errors="replace"))
    return found[-1] if found else None


def cmd_tunnel(args: argparse.Namespace) -> None:
    if not unit_active(APP_UNIT):
        die("die Vorschau läuft nicht: zuerst  preview.py start <worktree>")
    stop_unit(TUNNEL_UNIT)
    log = STATE / "tunnel.log"
    log.unlink(missing_ok=True)
    r = run(
        [
            "systemd-run",
            "--user",
            f"--unit={TUNNEL_UNIT}",
            "--collect",
            "--quiet",
            "-p",
            f"StandardOutput=truncate:{log}",
            "-p",
            "StandardError=inherit",
            CLOUDFLARED,
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://{HOST}:{PORT}",
        ],
        capture_output=True,
    )
    if r.returncode:
        die(f"systemd-run cloudflared: {r.stderr.strip()}")
    end, url = time.time() + 45, None
    while time.time() < end and not url:
        time.sleep(1)
        url = tunnel_url()
    if not url:
        stop_unit(TUNNEL_UNIT)
        die(f"kein trycloudflare-Link erhalten (Log {log})")
    # the new hostname needs a few seconds in DNS before it answers
    end, code = time.time() + 60, None
    while time.time() < end:
        try:
            req = urllib.request.Request(f"{url}/healthz", headers={"User-Agent": "klug-preview/1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
                break
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    print(
        f"Öffentlicher Vorschau-Link: {url}/p/eishockey/"
        + ("" if code == 200 else "  (antwortet noch nicht)")
    )


def cmd_stop(args: argparse.Namespace) -> None:
    stop_unit(TUNNEL_UNIT)
    stop_unit(APP_UNIT)
    print("Vorschau und Tunnel gestoppt.")


def cmd_status(args: argparse.Namespace) -> None:
    app, tun = unit_active(APP_UNIT), unit_active(TUNNEL_UNIT)
    info = {
        "app": "läuft" if app else "aus",
        "app_ok": healthy(2) if app else False,
        "local": f"http://{HOST}:{PORT}/p/eishockey/",
        "worktree": (STATE / "worktree").read_text().strip() if (STATE / "worktree").exists() else None,
        "tunnel": "läuft" if tun else "aus",
        "tunnel_url": tunnel_url() if tun else None,
        "db": PREVIEW_DB if db_exists(PREVIEW_DB) else "fehlt",
        "db_from": (STATE / "db-refreshed").read_text().strip()
        if (STATE / "db-refreshed").exists()
        else None,
        "template": "ok" if db_exists(TEMPLATE_DB) else f"fehlt ({ROOT_HINT})",
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("db-refresh", help=f"{PREVIEW_DB} neu aus der neuesten Sicherung")
    p.add_argument("--max-age-hours", type=float, default=0, help="nur wenn älter (0 = immer)")
    p.add_argument("--worktree", help="Code für die Migrationen (Standard: dieser Checkout)")
    p.set_defaults(fn=cmd_db_refresh)
    p = sub.add_parser("start", help="App eines Worktrees auf 127.0.0.1:8010 starten")
    p.add_argument("worktree")
    p.add_argument("--tunnel", action="store_true", help="auch den öffentlichen Link starten")
    p.set_defaults(fn=cmd_start)
    p = sub.add_parser("restart", help="neu starten (gleicher oder anderer Worktree)")
    p.add_argument("worktree", nargs="?")
    p.set_defaults(fn=cmd_restart)
    sub.add_parser("tunnel", help="Cloudflare Quick Tunnel starten").set_defaults(fn=cmd_tunnel)
    sub.add_parser("stop", help="App und Tunnel stoppen").set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="was läuft").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.fn(args)


if __name__ == "__main__":
    main()
