import copy
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

# Test database. Under pytest-xdist (`pytest -n 4`) every worker gets its own copy: <name>_gw0, <name>_gw1 …
# A missing database is created (from TEST_DATABASE_TEMPLATE, see ensure_database), so a worktree can use its
# own name: TEST_DATABASE_URL=postgresql+psycopg:///klugscheisser_test_<topic> uv run pytest -n 4
BASE_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://localhost/klugscheisser_test")
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")  # "gw0", "gw1" … (set by xdist before conftest loads)


def worker_db_url(url: str) -> str:
    """The URL with the xdist worker id appended to the database name (unchanged without xdist)."""
    if not WORKER:
        return url
    u = make_url(url)
    return u.set(database=f"{u.database}_{WORKER}").render_as_string(hide_password=False)


TEST_DB_URL = worker_db_url(BASE_TEST_DB_URL)
os.environ["DATABASE_URL"] = TEST_DB_URL
# fixtures have fixed dates: use a long window so tests don't expire
os.environ["INSTAGRAM_FIRST_RUN_DAYS"] = "3650"
os.environ["POSTS_LOOKBACK_DAYS"] = "3650"
os.environ["AUTO_REFRESH"] = "false"  # tests that need it enable it explicitly
os.environ["PROJECTS_FILE"] = ""  # one project on the test database; project tests build their own registry
for flag in (
    "SIHF_DATA_ENABLED",
    "IIHF_REALTIME_ENABLED",
    "PWHL_ENABLED",
    "SDHL_ENABLED",
    "LEIJONAT_ENABLED",
    "SIHF_NT_ENABLED",
):
    os.environ[flag] = "false"  # no live result sources in tests; result tests pass explicit settings
os.environ["PROFILES_ENABLED"] = "false"  # profile tests pass explicit settings
# status tools wait up to 60 s for a background job; in tests no job runs unless a test starts one, so do not
# wait (tests that need the wait pass explicit settings)
os.environ["STATUS_WAIT_S"] = "0"
# tests never use real keys from .env
for key in (
    "APIFY_TOKEN",
    "TYPESAFE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "META_GRAPH_TOKEN",
    "FIRECRAWL_API_KEY",
    "MCP_AUTH_TOKEN",
    "BRAVE_API_KEY",
    "X_BEARER_TOKEN",
):
    os.environ[key] = ""

# The `vector` extension needs a superuser to create. On the server the role `ondrej` is no superuser, so a new
# test database is copied from a template that already has it (made once with ~/setup/preview-db-vector.sh).
# Elsewhere (CI, a superuser role) the database is created empty and the first migration creates the extension.
TEMPLATE_DB = os.environ.get("TEST_DATABASE_TEMPLATE", "klugscheisser_preview_template")


def ensure_database(url: str) -> None:
    """CREATE DATABASE when missing (copy of TEMPLATE_DB when that exists, so it has `vector`)."""
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}):
                return
            has_template = c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEMPLATE_DB})
            template = f' TEMPLATE "{TEMPLATE_DB}"' if has_template else ""
            for attempt in range(5):  # two workers copying the same template at once: Postgres may refuse one
                try:
                    c.execute(text(f'CREATE DATABASE "{u.database}"{template}'))
                    return
                except Exception:
                    if c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}):
                        return
                    if attempt == 4:
                        raise
                    import time

                    time.sleep(0.5 * (attempt + 1))
    finally:
        admin.dispose()


DROP_OUR_OBJECTS = """
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
  END LOOP;
  FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
           WHERE n.nspname = 'public' AND t.typtype = 'e'
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = t.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('DROP TYPE IF EXISTS public.%I CASCADE', r.typname);
  END LOOP;
END $$;
"""


def fresh_schema(url: str):
    """Create the database if needed, drop our tables and enum types (keep the extensions: on the server they
    belong to postgres and a non-superuser could not create them again), run all migrations. Returns an engine."""
    ensure_database(url)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(DROP_OUR_OBJECTS))
    command.upgrade(cfg, "head")
    return eng


# --- no real network -----------------------------------------------------------------------------------------
# Tests run offline: a forgotten mock must fail at once instead of waiting for a timeout (or passing only when
# the internet is reachable). Local connections (Postgres, in-process test clients) stay allowed.
# RUN_NETWORK_TESTS=1 turns the guard off for the `network` tests.
if os.environ.get("RUN_NETWORK_TESTS") != "1":
    import socket

    _LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}
    _real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    _real_getaddrinfo = socket.getaddrinfo

    def _is_local(address) -> bool:
        if isinstance(address, (str, bytes)):  # unix socket path
            return True
        host = address[0] if isinstance(address, tuple) and address else None
        return host in _LOCAL_HOSTS or (isinstance(host, str) and host.startswith("127."))

    def _blocked(address):
        return OSError(f"real network access in a test ({address!r}); mock it or mark the test `network`")

    def _guarded_connect(self, address):
        if not _is_local(address):
            raise _blocked(address)
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if not _is_local(address):
            raise _blocked(address)
        return _real_connect_ex(self, address)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local((host.decode() if isinstance(host, bytes) else host,)):
            raise socket.gaierror(socket.EAI_NONAME, f"real network access in a test ({host!r})")
        return _real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="network test; set RUN_NETWORK_TESTS=1")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def engine():
    eng = fresh_schema(TEST_DB_URL)
    yield eng
    eng.dispose()


def truncate_all(conn) -> None:
    """Empty every table and reset every id sequence, as TRUNCATE … RESTART IDENTITY on all tables would, but
    only touching the tables that have rows (most tests fill a few; truncating all 37 costs ~30 ms a test)."""
    from klugscheisser.db.models import Base

    tables = [t.name for t in reversed(Base.metadata.sorted_tables)]
    used = conn.execute(
        text("SELECT ARRAY[" + ", ".join(f'EXISTS (SELECT 1 FROM "{t}")' for t in tables) + "]")
    )
    full = [t for t, has_rows in zip(tables, used.scalar(), strict=True) if has_rows]
    if full:
        conn.execute(text(f"TRUNCATE {', '.join(full)} CASCADE"))
    conn.execute(
        text(
            "SELECT setval(format('%I.%I', schemaname, sequencename), 1, false) FROM pg_sequences "
            "WHERE schemaname = 'public' AND last_value IS NOT NULL"
        )
    )


@pytest.fixture
def db(engine):
    """Clean tables for every test."""
    with engine.begin() as conn:
        truncate_all(conn)
    from klugscheisser import projects

    projects.clear_cache()  # project settings from the app are cached a few seconds
    return engine


# --- seed cache ----------------------------------------------------------------------------------------------
# About 70 fixtures call `load_all_seeds` (all teams, rosters, events, staff from seeds/) on an empty database.
# That takes ~7 s of Python each time and was most of the suite's runtime. The first call in a test process
# runs the real loader and keeps a binary copy of every table (COPY … TO STDOUT) plus the sequence values;
# later calls on an *empty* database (fresh sequences too) with the *same* seed files load that copy back (COPY … FROM STDIN, a few
# milliseconds). The result is the same rows with the same ids. Anything else (tables not empty, other seed
# folder, changed files, a failed restore) runs the real loader. Off switch: KS_TEST_SEED_CACHE=0.
from klugscheisser.seed import loader as _seed_loader  # noqa: E402  (after DATABASE_URL is set)

_real_load_all_seeds = _seed_loader.load_all_seeds
_seed_cache: dict[tuple, tuple] = {}


def _seed_key(seeds_dir: Path) -> tuple:
    root = Path(seeds_dir).resolve()
    files = tuple(
        sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file())
    )
    return (str(root), files)


def _tables() -> list[str]:
    from klugscheisser.db.models import Base

    return [t.name for t in Base.metadata.sorted_tables]  # parents before children


def _pristine(cur, tables: list[str]) -> bool:
    """Every table empty and every sequence unused (as right after the `db` fixture's TRUNCATE … RESTART
    IDENTITY): only then does the real loader produce exactly the cached rows and ids."""
    rows = " OR ".join(f'EXISTS (SELECT 1 FROM "{t}")' for t in tables)
    cur.execute(
        f"SELECT ({rows}) OR EXISTS (SELECT 1 FROM pg_sequences "
        "WHERE schemaname = 'public' AND last_value IS NOT NULL)"
    )
    return not cur.fetchone()[0]


def _snapshot(cur, tables: list[str]) -> dict:
    data: dict[str, bytes] = {}
    for t in tables:
        cur.execute(f'SELECT EXISTS (SELECT 1 FROM "{t}")')
        if not cur.fetchone()[0]:
            continue
        buf = bytearray()
        with cur.copy(f'COPY "{t}" TO STDOUT (FORMAT BINARY)') as cp:
            for chunk in cp:
                buf += chunk
        data[t] = bytes(buf)
    cur.execute(
        "SELECT format('%I.%I', schemaname, sequencename), last_value FROM pg_sequences "
        "WHERE schemaname = 'public' AND last_value IS NOT NULL"
    )
    seqs = dict(cur.fetchall())
    # timestamps the loader wrote (created_at = now() …) lie between the transaction start and now
    cur.execute("SELECT now(), clock_timestamp()")
    start, end = cur.fetchone()
    cur.execute(
        "SELECT table_name, array_agg(column_name::text) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%' GROUP BY table_name"
    )
    stamps = {t: cols for t, cols in cur.fetchall() if t in data}
    return {"data": data, "seqs": seqs, "start": start, "end": end, "stamps": stamps}


def _restore(cur, snap: dict, tables: list[str]) -> None:
    for t in tables:
        if t in snap["data"]:
            with cur.copy(f'COPY "{t}" FROM STDIN (FORMAT BINARY)') as cp:
                cp.write(snap["data"][t])
    for name, value in snap["seqs"].items():
        cur.execute("SELECT setval(%s, %s)", (name, value))
    # move the loader's own timestamps to this transaction's now(), as a real run would have written them;
    # fixed times from the seed files (game starts …) lie outside that window and stay as they are
    for t, cols in snap["stamps"].items():
        sets = ", ".join(
            f'"{c}" = CASE WHEN "{c}" BETWEEN %(a)s AND %(b)s THEN "{c}" + (now() - %(a)s) ELSE "{c}" END'
            for c in cols
        )
        cur.execute(f'UPDATE "{t}" SET {sets}', {"a": snap["start"], "b": snap["end"]})


def _cached_load_all_seeds(session, seeds_dir: Path = _seed_loader.SEEDS_DIR):
    if os.environ.get("KS_TEST_SEED_CACHE", "1") == "0":
        return _real_load_all_seeds(session, seeds_dir)
    session.flush()
    raw = session.connection().connection.driver_connection  # psycopg connection inside the session's tx
    tables = _tables()
    with raw.cursor() as cur:
        if not _pristine(cur, tables):
            return _real_load_all_seeds(session, seeds_dir)
    key = _seed_key(seeds_dir)
    hit = _seed_cache.get(key)
    if hit is not None:
        snap, stats = hit
        nested = session.begin_nested()
        try:
            with raw.cursor() as cur:
                _restore(cur, snap, tables)
            nested.commit()
            session.expire_all()
            return copy.deepcopy(stats)
        except Exception:
            nested.rollback()  # fall back to the real loader below
    stats = _real_load_all_seeds(session, seeds_dir)
    session.flush()
    with raw.cursor() as cur:
        _seed_cache[key] = (_snapshot(cur, tables), copy.deepcopy(stats))
    return stats


_seed_loader.load_all_seeds = _cached_load_all_seeds


@pytest.fixture(autouse=True)
def _restore_mcp_freshen():
    """Some tests set `tools.freshen` by plain assignment; put it back so later tests on the same worker
    (e.g. test_v2_freshness) see the real person update."""
    from klugscheisser.mcp_server import tools

    real = tools.freshen
    yield
    tools.freshen = real


@pytest.fixture
def as_provisional():
    """Put teams' WCh 2026 player lists back to "provisional" (the state before their official list). Since 03.11.2026
    every seeded WCh list is official; kaderwatch tests that need a team still waiting for its list use this."""
    from sqlalchemy import select as _select

    from klugscheisser.db import models as _m

    def mark(session, *team_keys: str, event: str = "wch-2026") -> None:
        ev = session.scalar(_select(_m.Event).where(_m.Event.key == event))
        for key in team_keys:
            team = session.scalar(_select(_m.Team).where(_m.Team.key == key))
            for e in session.scalars(
                _select(_m.EventEntry).where(
                    _m.EventEntry.event_id == ev.id, _m.EventEntry.team_id == team.id
                )
            ):
                if e.role in (None, "G", "D", "F"):
                    e.list_type = "provisional"
        session.flush()

    return mark


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze `datetime.now()` in the given modules, for tests whose fixtures sit at fixed dates around the
    WCh 2026 (they passed in Nov 2026 and failed later once the real date left the time windows).
    Use: `frozen_now(datetime(2026, 11, 4, 12, tzinfo=UTC), tools, team_games)`."""
    import datetime as _dt

    def freeze(at, *modules):
        class _Frozen(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return at if tz is None else at.astimezone(tz)

        for mod in modules:
            monkeypatch.setattr(mod, "datetime", _Frozen)
        return at

    return freeze
