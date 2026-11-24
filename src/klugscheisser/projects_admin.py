"""v7: project administration — create a project database, migrate all projects, backups, copy a person.

Used by `klugscheisser project …` (cli.py) and the nightly job (backups).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from klugscheisser import projects
from klugscheisser.db import models as m
from klugscheisser.projects import Project

log = logging.getLogger(__name__)
BACKUP_DIR = Path("backups")
KEEP_BACKUPS = 7  # per project; older dumps are deleted after a successful new one


def database_exists(url: str) -> bool:
    u = make_url(url)
    eng = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            return bool(c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}))
    finally:
        eng.dispose()


EMPTY_TEMPLATE = "klugscheisser_preview_template"


def create_database(url: str) -> bool:
    """CREATE DATABASE if missing. Returns True when it was created."""
    if database_exists(url):
        return False
    u = make_url(url)
    eng = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            # 2026-11-24: copy the empty template that already has `vector` (only a superuser can create that
            # extension; docs/testing.md), else a plain database (the first migration creates the extensions)
            has_template = c.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": EMPTY_TEMPLATE}
            )
            template = f' TEMPLATE "{EMPTY_TEMPLATE}"' if has_template else ""
            c.execute(text(f'CREATE DATABASE "{u.database}"{template}'))
    finally:
        eng.dispose()
    return True


def migrate(p: Project, revision: str = "head") -> None:
    """Alembic upgrade for one project database (the migration creates pgvector, unaccent, pg_trgm)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", p.database_url.replace("%", "%%"))
    command.upgrade(cfg, revision)


def create(p: Project) -> dict[str, Any]:
    created = create_database(p.database_url)
    migrate(p)
    stats: dict[str, Any] = {}
    if p.seeds:
        from klugscheisser import projects
        from klugscheisser.db.session import get_engine, session_scope
        from klugscheisser.seed.loader import load_all_seeds

        with projects.use(p), session_scope(get_engine(p.database_url)) as s:
            stats = load_all_seeds(s, Path(p.seeds_dir))
    return {"projekt": p.key, "datenbank_neu": created, "seeds": stats}


# --- backups ---------------------------------------------------------------------------------------


def backup(p: Project, directory: Path = BACKUP_DIR, keep: int = KEEP_BACKUPS) -> Path:
    """pg_dump (custom format) of one project database; keeps the newest `keep` dumps of that project."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump nicht gefunden")
    directory.mkdir(parents=True, exist_ok=True)
    u = make_url(p.database_url)
    target = directory / f"{p.key}-{datetime.now(UTC):%Y%m%d-%H%M%S}.dump"
    cmd = ["pg_dump", "-Fc", "-f", str(target), "-d", u.database or ""]
    if u.host:
        cmd += ["-h", u.host]
    if u.port:
        cmd += ["-p", str(u.port)]
    if u.username:
        cmd += ["-U", u.username]
    env = None
    if u.password:
        import os

        env = {**os.environ, "PGPASSWORD": u.password}
    subprocess.run(cmd, check=True, env=env, capture_output=True)
    old = sorted(directory.glob(f"{p.key}-*.dump"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink(missing_ok=True)
    return target


def disk_free_gb(directory: Path = BACKUP_DIR) -> float:
    directory.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(directory).free / 1e9


# --- copy a person between projects ----------------------------------------------------------------

SKIP = {"id"}


def _clone(row: Any, **override: Any) -> Any:
    cls = type(row)
    cols = {c.key: getattr(row, c.key) for c in inspect(cls).column_attrs if c.key not in SKIP}
    cols.update(override)
    return cls(**cols)


def _sport(dst: Session, sport: m.Sport) -> m.Sport:
    found = dst.scalar(select(m.Sport).where(m.Sport.key == sport.key))
    if found is None:
        found = _clone(sport)
        dst.add(found)
        dst.flush()
    return found


def _team_id(src: Session, dst: Session, team_id: int | None) -> int | None:
    if team_id is None:
        return None
    team = src.get(m.Team, team_id)
    if team is None:
        return None
    return dst.scalar(select(m.Team.id).where(m.Team.key == team.key))


def _item(src: Session, dst: Session, item: m.Item, items: dict[int, int]) -> int:
    """Copy one document (with classification + vectors) unless the target has it already."""
    if item.id in items:
        return items[item.id]
    found = dst.scalar(select(m.Item).where(m.Item.url == item.url, m.Item.content_hash == item.content_hash))
    if found is None:
        found = _clone(item)
        dst.add(found)
        dst.flush()
        if item.classification is not None:
            dst.add(_clone(item.classification, item_id=found.id))
        for emb in src.scalars(select(m.Embedding).where(m.Embedding.item_id == item.id)):
            dst.add(_clone(emb, item_id=found.id))
        dst.flush()
    items[item.id] = found.id
    return found.id


def copy_person(src: Session, dst: Session, athlete: m.Athlete) -> dict[str, Any]:
    """Person, aliases, handles, documents (+ checks, vectors), facts, career, stats. Nothing is fetched or paid
    again. Teams are linked only when the target project knows the same team (by key); the team name stays."""
    existing = None
    if athlete.wikidata_qid:
        existing = dst.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == athlete.wikidata_qid))
    if existing is None:
        existing = dst.scalar(
            select(m.Athlete).where(
                m.Athlete.full_name == athlete.full_name, m.Athlete.birth_date == athlete.birth_date
            )
        )
    if existing is not None:
        return {"person": existing.full_name, "id": existing.id, "neu": False}
    sport = _sport(dst, src.get(m.Sport, athlete.sport_id))
    a = _clone(athlete, sport_id=sport.id)
    dst.add(a)
    dst.flush()
    counts = {"aliase": 0, "handles": 0, "dokumente": 0, "fakten": 0, "stationen": 0, "statistik": 0}
    for al in src.scalars(select(m.AthleteAlias).where(m.AthleteAlias.athlete_id == athlete.id)):
        dst.add(_clone(al, athlete_id=a.id))
        counts["aliase"] += 1
    for h in src.scalars(select(m.Handle).where(m.Handle.athlete_id == athlete.id)):
        dst.add(_clone(h, athlete_id=a.id))
        counts["handles"] += 1
    items: dict[int, int] = {}
    for link in src.scalars(select(m.ItemAthlete).where(m.ItemAthlete.athlete_id == athlete.id)):
        new_id = _item(src, dst, src.get(m.Item, link.item_id), items)
        dst.add(_clone(link, item_id=new_id, athlete_id=a.id))
        counts["dokumente"] += 1
    for f in src.scalars(select(m.Fact).where(m.Fact.athlete_id == athlete.id)):
        item_id = _item(src, dst, src.get(m.Item, f.source_item_id), items) if f.source_item_id else None
        dst.add(_clone(f, athlete_id=a.id, source_item_id=item_id))
        counts["fakten"] += 1
    for st in src.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == athlete.id)):
        item_id = _item(src, dst, src.get(m.Item, st.source_item_id), items) if st.source_item_id else None
        dst.add(_clone(st, athlete_id=a.id, team_id=_team_id(src, dst, st.team_id), source_item_id=item_id))
        counts["stationen"] += 1
    for ss in src.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == athlete.id)):
        dst.add(_clone(ss, athlete_id=a.id))
        counts["statistik"] += 1
    for cache in src.scalars(
        select(m.CacheEntry).where(
            m.CacheEntry.target == f"athlete:{athlete.id}", m.CacheEntry.source == "funfacts"
        )
    ):
        dst.merge(m.CacheEntry(source=cache.source, target=f"athlete:{a.id}", fetched_at=cache.fetched_at))
    dst.flush()
    return {"person": a.full_name, "id": a.id, "neu": True, **counts}


def copy_person_between(from_key: str, to_key: str, name: str) -> dict[str, Any]:
    from klugscheisser.db import repo
    from klugscheisser.db.session import get_engine, session_scope

    src_p, dst_p = projects.get(from_key), projects.get(to_key)
    if src_p is None or dst_p is None:
        raise KeyError("unbekanntes Projekt")
    with (
        session_scope(get_engine(src_p.database_url)) as src,
        session_scope(get_engine(dst_p.database_url)) as dst,
    ):
        athlete = repo.resolve_athlete(src, name)
        if athlete is None:
            raise LookupError(f"{name} nicht im Projekt {src_p.name}")
        return copy_person(src, dst, athlete)
