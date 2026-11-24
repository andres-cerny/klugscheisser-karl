"""v7: projects — one database per project (eishockey, allgemein, later ski …), chosen per request.

`projects.yaml` lists the projects. The current project is a context variable: the web app sets it from the
`/p/<key>` prefix or the remembered choice, the MCP connector from `/mcp/<key>`, the CLI from `--project`,
background jobs per project. `get_engine()` and `get_settings()` follow it, so the rest of the code does not
need to know about projects. Without a projects file there is one project ("eishockey") on DATABASE_URL.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.engine import make_url

DEFAULT_KEY = "eishockey"


@dataclass(frozen=True)
class Wording:
    topic: str = "Schweizer Frauen-Eishockey"  # "über Spielerinnen aus dem …" (prompts, German)
    person: str = "Eishockeyspielerin"  # one person of the project (identity and relevance questions)
    persons: str = "Eishockeyspielerinnen"
    topic_en: str = "Swiss women's ice hockey"  # MCP tool descriptions (English)
    example_question: str = "Wer studiert neben dem Eishockey?"
    # 2026-11-24 (floorball project, men): the words the prompts, answers, UI and MCP texts use
    sport_de: str = "Eishockey"  # the sport's name ("Unihockey")
    sport_en: str = "ice hockey"  # ("floorball")
    player: str = "Spielerin"  # one player, table headers ("Spieler")
    players: str = "Spielerinnen"  # ("Spieler")
    teammates: str = "Mitspielerinnen"  # ("Mitspieler")
    player_en: str = "player"
    players_en: str = "players"
    gender_en: str = "women's"  # "the Swiss women's national team" ("men's")
    team_de: str = "Frauen-Nationalteam"  # the national team ("Nationalteam")
    league_de: str = "PostFinance Women's League"  # the home league ("Unihockey Prime League")
    worlds_de: str = "Frauen-WM"  # the world championship ("Unihockey-WM")
    pronoun_de: str = "sie"  # "Was macht sie neben dem Sport?" ("er")

    def plural(self, n: int) -> str:
        """'1 Spielerin' / '2 Spielerinnen' (German count + noun)."""
        return f"{n} {self.player if n == 1 else self.players}"


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    emoji: str = "🏒"
    status: str = "active"  # active | archived (archived: no jobs, data stays)
    database: str | None = (
        None  # database name on the DATABASE_URL server, or a full URL; None = DATABASE_URL
    )
    scheduled_jobs: bool = True  # news poller + nightly refresh (False for the on-demand "Allgemein")
    seeds: bool = True  # load seeds/ (teams, feeds, rosters) on create
    sport: str | None = "icehockey_women"  # default sport for adds; None = any (detected per person)
    wording: Wording = field(default_factory=Wording)
    settings: dict[str, Any] = field(default_factory=dict)  # Settings overrides (cost caps, nightly hour …)
    # 2026-11-24: where the project's curated seeds live (teams.yaml, rosters/, events/, staff/) and its main
    # tournament (tiers, coverage, default event of commands); home nation = tier 1
    seeds_dir: str = "seeds"
    # 2026-11-24: last day (Swiss time) of scheduled jobs; from the day after, nightly/news/game watch stop by
    # themselves (unihockey: 2026-12-14, the day after the WCh final). None = no end.
    scheduled_until: date | None = None
    main_event: str = "wch-2026"
    home_nation: str = "SUI"

    @property
    def active(self) -> bool:
        return self.status == "active"

    def scheduled_on(self, day: date | None = None) -> bool:
        """Background schedules run for this project today (Swiss date): wanted, active and not past the end."""
        if not (self.active and self.scheduled_jobs):
            return False
        if self.scheduled_until is None:
            return True
        if day is None:
            from zoneinfo import ZoneInfo

            day = datetime.now(ZoneInfo("Europe/Zurich")).date()
        return day <= self.scheduled_until

    @property
    def database_url(self) -> str:
        from klugscheisser.config import Settings

        base = Settings().database_url
        if not self.database:
            return base
        if "://" in self.database:
            return self.database
        return make_url(base).set(database=self.database).render_as_string(hide_password=False)

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


def _parse(raw: dict[str, Any]) -> tuple[str, dict[str, Project]]:
    out: dict[str, Project] = {}
    for key, p in (raw.get("projects") or {}).items():
        p = dict(p or {})
        wording = Wording(**(p.pop("wording", None) or {}))
        if p.get("scheduled_until") and not isinstance(p["scheduled_until"], date):
            p["scheduled_until"] = date.fromisoformat(str(p["scheduled_until"]))
        out[key] = Project(key=key, wording=wording, **p)
    if not out:
        out[DEFAULT_KEY] = Project(key=DEFAULT_KEY, name="Frauen-Eishockey")
    default = raw.get("default") or next(iter(out))
    return default, out


@lru_cache
def _load(path: str) -> tuple[str, dict[str, Project]]:
    p = Path(path) if path else None
    if p is None or not p.is_file():
        return _parse({})
    return _parse(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def registry() -> tuple[str, dict[str, Project]]:
    from klugscheisser.config import Settings

    return _load(Settings().projects_file or "")


def all_projects() -> list[Project]:
    return list(registry()[1].values())


def active_projects() -> list[Project]:
    return [p for p in all_projects() if p.active]


def get(key: str | None) -> Project | None:
    if not key:
        return None
    return registry()[1].get(key.strip().lower())


def default() -> Project:
    d, projects = registry()
    return projects[d]


def wording() -> Wording:
    """Words for prompts of the current project ("Eishockeyspielerin" / "Sportlerin oder Sportler")."""
    return current().wording


_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("klugscheisser_project", default=None)


def current() -> Project:
    return get(_current.get()) or default()


@contextmanager
def use(project: Project | str) -> Iterator[Project]:
    p = project if isinstance(project, Project) else get(project)
    if p is None:
        raise KeyError(f"unknown project: {project}")
    token = _current.set(p.key)
    try:
        yield p
    finally:
        _current.reset(token)


def set_current(key: str) -> contextvars.Token:
    return _current.set(key)


class ContextPool(ThreadPoolExecutor):
    """Thread pool whose tasks keep the caller's project (threads do not inherit context variables)."""

    def submit(self, fn: Callable, /, *args, **kwargs):
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


# --- per-project settings (yaml + the app's settings page, stored in the project's own database) ------

_db_cache: dict[str, tuple[float, dict[str, Any]]] = {}
DB_TTL_S = 30.0
EDITABLE = {
    # key -> (label, type); shown on the settings page
    "apify_daily_cost_cap_usd": ("Apify (Instagram) pro Tag, USD", float),
    "llm_daily_cost_cap_usd": ("KI-Texte pro Tag, USD", float),
    "x_daily_cost_cap_usd": ("X pro Tag, USD", float),
    "nightly_cap_usd": ("Nächtliche Aktualisierung pro Nacht, USD", float),
    "nightly_hour": ("Nächtliche Aktualisierung ab (Uhr)", int),
    "news_poll_minutes": ("News abholen alle … Minuten (0 = aus)", int),
}


def db_overrides(p: Project) -> dict[str, Any]:
    hit = _db_cache.get(p.key)
    if hit and time.monotonic() - hit[0] < DB_TTL_S:
        return hit[1]
    values: dict[str, Any] = {}
    try:
        from sqlalchemy import select

        from klugscheisser.db import models as m
        from klugscheisser.db.session import get_engine, session_scope

        with session_scope(get_engine(p.database_url)) as s:
            for row in s.scalars(select(m.ProjectSetting)):
                if row.key in EDITABLE:
                    values[row.key] = row.value
    except Exception:  # table missing (old database) or database down: yaml values only
        values = {}
    _db_cache[p.key] = (time.monotonic(), values)
    return values


def clear_cache(key: str | None = None) -> None:
    if key is None:
        _db_cache.clear()
    else:
        _db_cache.pop(key, None)


def overrides(p: Project) -> dict[str, Any]:
    return {**p.settings, **db_overrides(p)}
