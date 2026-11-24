# Tests

Offline, no keys needed. Postgres 17 + pgvector.

```bash
uv run pytest tests/test_v3_web.py          # while working: only the files you touched
uv run pytest -k "photo or card"            # … or by name
uv run pytest -n 4                          # everything, 4 parallel workers (~1 min on the server)
uv run pytest                               # everything, one process (~2–3 min)
```

On this server the full suite took about an hour until 2026-10-25; see "Why it is fast" before changing the
test setup.

## Databases

- Default: `klugscheisser_test` (`TEST_DATABASE_URL` to change). The session fixture drops our tables and runs
  all migrations once per test process, so any branch works on any test database.
- `pytest -n 4`: every worker uses its own copy, `<name>_gw0` … `<name>_gw3` (the plain name is not used).
  `test_v7_projects` also needs a second project database: `<name>_v7` (per worker `<name>_gw0_v7` …).
- Missing databases are created automatically as a copy of `klugscheisser_preview_template` (has the `vector`
  extension, which only a superuser can create; `TEST_DATABASE_TEMPLATE` to use another one). Without that
  template (CI, a superuser role) they are created empty and the first migration creates the extensions.
- **Rule: every worktree / agent uses its own test database** (no waiting on other sessions):
  `TEST_DATABASE_URL=postgresql+psycopg:///klugscheisser_test_<topic> uv run pytest -n 4`. They stay until
  dropped; drop them when the work is done (the name plus `_gw0` … and `_v7` copies):
  `for d in $(psql -d postgres -Atc "select datname from pg_database where datname ~ '^klugscheisser_test_<topic>(_gw[0-9]+)?(_v7)?$'"); do dropdb "$d"; done`.
  `bash scripts/ui_loop/tests_busy.sh [name]` says whether another pytest uses the same name.

## Why it is fast (tests/conftest.py)

1. **Seed cache.** About 70 fixtures load all seeds (`load_all_seeds`: teams, 22 rosters, events, staff), which
   takes ~7 s each time. The first call per test process runs the real loader and keeps a binary copy of the
   tables and sequence values; later calls on an empty database with unchanged seed files load the copy (~0.1
   s). Rows, ids and statistics are identical; timestamps the loader wrote are moved to the current
   transaction time, as a real run would write them. Anything else (database not empty, another seed folder,
   changed files) runs the real loader. Compare with `KS_TEST_SEED_CACHE=0`.
2. **No status waits.** `STATUS_WAIT_S=0`: status tools do not wait 60 s for background jobs that never run in
   tests (tests that test the waiting pass explicit settings).
3. **No real network.** Connections to anything but localhost / unix sockets fail at once with a clear
   message, so a missing mock cannot hang on a timeout or depend on the internet. `RUN_NETWORK_TESTS=1` lifts
   the guard for the tests marked `network`.
4. **Cheap reset.** The per-test `db` fixture truncates only tables that have rows and resets the sequences.
5. **pytest-xdist** for `-n 4` (one database per worker).

Writing tests: use the `db` fixture (clean tables), call `load_all_seeds` once per test (re-seeding a filled
database runs the real loader, ~4 s), mock HTTP with `httpx.MockTransport` / `respx`, and pass wait times
explicitly instead of relying on defaults.
