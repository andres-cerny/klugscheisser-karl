"""Admin and cron CLI: `klugscheisser --help`."""

from __future__ import annotations

import typer

from klugscheisser.config import get_settings

app = typer.Typer(no_args_is_help=True, help="Athlete info pipeline for Swiss women's ice hockey.")
db_app = typer.Typer(no_args_is_help=True, help="Database admin.")
seed_app = typer.Typer(no_args_is_help=True, help="Seed teams, events, rosters and athletes.")
handles_app = typer.Typer(no_args_is_help=True, help="Review social media handles.")
app.add_typer(db_app, name="db")
app.add_typer(seed_app, name="seed")
app.add_typer(handles_app, name="handles")
project_app = typer.Typer(no_args_is_help=True, help="Projects (v7): one database per project.")
app.add_typer(project_app, name="project")
ep_app = typer.Typer(no_args_is_help=True, help="v11: Elite Prospects (polite fetch, import).")
app.add_typer(ep_app, name="ep")

from klugscheisser.free_profiles_cli import app as free_profiles_app  # noqa: E402

app.add_typer(free_profiles_app, name="free-profiles")


@app.callback()
def choose_project(
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        envvar="KLUGSCHEISSER_PROJECT",
        help="project key (default: see projects.yaml)",
    ),
) -> None:
    """Every command works on one project's database; `--project allgemein` picks another one."""
    from klugscheisser import projects

    if project:
        if projects.get(project) is None:
            raise typer.BadParameter(
                f"unknown project {project!r}; known: {[p.key for p in projects.all_projects()]}"
            )
        projects.set_current(project)


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Run Alembic migrations."""

    from klugscheisser import projects
    from klugscheisser.projects_admin import migrate

    migrate(projects.current(), revision)


@project_app.command("list")
def project_list() -> None:
    """All projects with status, database and whether the database exists."""
    from klugscheisser import projects
    from klugscheisser.projects_admin import database_exists

    for p in projects.all_projects():
        exists = database_exists(p.database_url)
        db = p.database or "(DATABASE_URL)"
        jobs = "Jobs an" if p.scheduled_jobs else "nur auf Anfrage"
        typer.echo(f"{p.label:24} {p.key:12} {p.status:9} {db:28} {'da' if exists else 'FEHLT':6} {jobs}")


@project_app.command("create")
def project_create(key: str) -> None:
    """Create the project's database (if missing), run all migrations, load seeds when the project wants them."""
    from klugscheisser import projects
    from klugscheisser.projects_admin import create

    p = projects.get(key)
    if p is None:
        raise typer.BadParameter(f"{key!r} is not in projects.yaml")
    typer.echo(create(p))


@project_app.command("migrate")
def project_migrate() -> None:
    """Run migrations on every project database (schema changes always go to all projects)."""
    from klugscheisser import projects
    from klugscheisser.projects_admin import database_exists, migrate

    for p in projects.all_projects():
        if not database_exists(p.database_url):
            typer.echo(f"{p.key}: Datenbank fehlt (klugscheisser project create {p.key})")
            continue
        migrate(p)
        typer.echo(f"{p.key}: ok")


@project_app.command("backup")
def project_backup(keep: int = typer.Option(7, help="dumps kept per project")) -> None:
    """pg_dump of every project database into backups/ (keeps the newest N per project)."""
    from klugscheisser import projects
    from klugscheisser.projects_admin import backup, disk_free_gb

    for p in projects.all_projects():
        path = backup(p, keep=keep)
        typer.echo(f"{p.key}: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    typer.echo(f"frei: {disk_free_gb():.1f} GB")


@project_app.command("copy-person")
def project_copy_person(source: str, target: str, name: str) -> None:
    """Copy a person with handles, documents, facts and career from one project to another (no new fetches)."""
    from klugscheisser.projects_admin import copy_person_between

    typer.echo(copy_person_between(source, target, name))


@app.command("deep")
def deep_cmd(
    tiers_: str = typer.Option("1,2", "--tiers", help="tiers in order, e.g. 1,2"),
    cap: float = typer.Option(40.0, help="hard cap in USD for the whole run (ledger since start)"),
    steps: str = typer.Option("handles,social,facebook,x,web,media,after", help="steps to run"),
    limit: int | None = typer.Option(None, help="only the first N persons per tier (trial run)"),
    person: list[int] | None = typer.Option(None, help="only these person ids"),  # noqa: B008
    dry_run: bool = typer.Option(False, help="show persons and queries, spend nothing"),
    since: str | None = typer.Option(
        None, help="ISO time the shared budget counts from (parallel workers share one cap)"
    ),
) -> None:
    """v8: deep sources for tier 1/2 (Brave, Instagram history + tagged, Facebook, X, podcasts/videos), then
    media analysis, facts, fun facts and translations. One budget for everything."""
    from datetime import UTC, datetime

    from klugscheisser import deep, tiers
    from klugscheisser.config import get_settings
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope

    settings = get_settings()
    wanted = tuple(x.strip() for x in steps.split(",") if x.strip())
    start = datetime.fromisoformat(since) if since else datetime.now(UTC)
    budget = deep.DeepBudget(cap, start)
    for t in [int(x) for x in tiers_.split(",") if x.strip()]:
        with session_scope() as s:
            ids = [i for i in (person or tiers.ids_in(s, t)) if tiers.effective(s.get(m.Athlete, i)) == t]
            ids = ids[:limit] if limit else ids
            if dry_run:
                for aid in ids[:3]:
                    a = s.get(m.Athlete, aid)
                    typer.echo(f"-- {a.full_name} (Tier {t})")
                    for q in deep.queries_for(a, t) + (deep.media_queries(a) if t == 1 else []):
                        typer.echo(f"   {q.endpoint:6} {q.country or '-':3} {q.lang or '-':3} {q.q}")
                typer.echo(f"Tier {t}: {len(ids)} Personen")
                continue
        if dry_run or not ids:
            continue
        run_steps = tuple(x for x in wanted if x not in ("media", "after"))
        deep.run(settings, t, cap, ids, steps=run_steps, since=start, echo=typer.echo)
        if "media" in wanted:
            deep.media_queue(deep.run_settings(settings, cap), budget, echo=typer.echo)
        if "after" in wanted:
            report = deep.DeepReport()
            deep.after(deep.run_settings(settings, cap), ids, budget, report, echo=typer.echo)
            for note in report.notes[:10]:
                typer.echo(f"  {note}")
    if not dry_run:
        with session_scope() as s:
            typer.echo(f"Gesamt seit Start: ${budget.spent(s):.2f} (Deckel ${cap:.2f})")


@app.command("media-queue")
def media_queue_cmd(
    cap: float = typer.Option(40.0),
    since: str | None = typer.Option(None, help="ISO time the shared budget counts from"),
    shard: str | None = typer.Option(
        None, help="i/n: only queue rows with item_id % n == i (parallel workers)"
    ),
) -> None:
    """v8: work through the media analysis queue now (run cap instead of the daily limit)."""
    from datetime import UTC, datetime

    from klugscheisser import deep
    from klugscheisser.config import get_settings

    start = datetime.fromisoformat(since) if since else datetime.now(UTC)
    part = tuple(int(x) for x in shard.split("/")) if shard else None
    n = deep.media_queue(
        deep.run_settings(get_settings(), cap), deep.DeepBudget(cap, start), typer.echo, shard=part
    )
    typer.echo(f"Gesamt Medien: {n}")


@app.command("tiers")
def tiers_cmd(
    show: int | None = typer.Option(None, help="list the persons of this tier"),
) -> None:
    """v8: recompute tiers from rosters and national-team stints (manual choices stay)."""
    from klugscheisser import tiers
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope

    with session_scope() as s:
        typer.echo(f"tiers: {dict(sorted(tiers.recompute(s).items()))}")
        if show:
            for aid in tiers.ids_in(s, show):
                a = s.get(m.Athlete, aid)
                typer.echo(
                    f"{a.id:5} {a.full_name} ({a.nationality or '?'}, {a.role}) · {tiers.sources_count(s, a.id)} Quellen"
                )


@app.command()
def serve() -> None:
    """Run the MCP server (Streamable HTTP on /mcp)."""
    from klugscheisser.mcp_server.server import serve as run

    run()


@app.command()
def features() -> None:
    """Show which external services are switched on (keys present). Never prints keys."""
    settings = get_settings()
    for name, enabled in settings.features.items():
        typer.echo(f"{name:26} {'ON' if enabled else 'off'}")
    try:  # v11: block / day stop of the polite EP fetcher ("Elite Prospects: pausiert bis …")
        from klugscheisser.collectors.eliteprospects.fetch import status_line
        from klugscheisser.db.session import session_scope

        with session_scope() as s:
            typer.echo(status_line(s, settings))
    except Exception as e:  # no database (yet): the feature list above is still useful
        typer.echo(f"Elite Prospects: Status nicht lesbar ({type(e).__name__})")


@seed_app.command("base")
def seed_base() -> None:
    """Load seeds/teams.yaml and seeds/rosters/*.yaml (idempotent)."""
    from klugscheisser import tiers
    from klugscheisser.db.session import session_scope
    from klugscheisser.seed.loader import load_all_seeds

    with session_scope() as s:
        stats = load_all_seeds(s)
        counts = tiers.recompute(s)
    typer.echo(f"seeded: {stats}; tiers: {dict(sorted(counts.items()))}")


@seed_app.command("floorball")
def seed_floorball(
    seeds_dir: str | None = typer.Option(
        None, "--dir", help="seeds directory (default: the project's seeds_dir)"
    ),
    yes: bool = typer.Option(False, "--yes", help="write (default: dry run, nothing is stored)"),
    enrich: str = typer.Option(
        "torneopal,swissunihockey,wikidata",
        help="free sources after the seeds (comma list, '' = none): torneopal, swissunihockey, iff, wikidata",
    ),
    funfacts: bool = typer.Option(
        False, "--funfacts", help="then LLM fun facts for tier 1+2 (paid, --max-usd)"
    ),
    max_usd: float = typer.Option(3.0, help="cap for the paid fun-fact step (USD)"),
    max_requests: int = typer.Option(800, help="cap of HTTP requests per free source"),
) -> None:
    """2026-11-24: one-off load of a floorball project (event, teams, games, rosters, careers, season stats, staff,
    history, fun facts with source + date), then free sources. Dry run by default; `--yes` writes."""
    from pathlib import Path

    from klugscheisser import projects
    from klugscheisser.db.session import session_scope
    from klugscheisser.seed import floorball

    p = projects.current()
    path = Path(seeds_dir or p.seeds_dir)
    typer.echo(f"Projekt {p.key} ({p.sport}), Seeds {path}, {'SCHREIBT' if yes else 'dry run'}")
    with session_scope() as s:
        stats = floorball.load(s, path)
        typer.echo(f"Seeds: {stats.summary()}")
        for team, line in sorted(stats.per_team.items()):
            typer.echo(f"  {team}: {line}")
        for line in stats.skipped[:30]:
            typer.echo(f"  übersprungen: {line}")
        sources = [x.strip() for x in enrich.split(",") if x.strip()]
        if sources:
            typer.echo(f"Quellen: {floorball.enrich(s, sources, max_requests=max_requests, echo=typer.echo)}")
        if not yes:
            s.rollback()
            typer.echo("(dry run, nichts gespeichert; mit --yes schreiben)")
            return
    if funfacts:
        from klugscheisser import tiers
        from klugscheisser.config import get_settings
        from klugscheisser.facts.funfacts import backfill

        with session_scope() as s:
            ids = sorted(set(tiers.ids_in(s, 1)) | set(tiers.ids_in(s, 2)))
        typer.echo(f"Fun Facts: {backfill(get_settings(), ids, budget_usd=max_usd, echo=typer.echo)}")


@seed_app.command("floorball-repair")
def seed_floorball_repair(
    german: str | None = typer.Option(None, "--german", help="JSON file {English seed text: German}"),
    yes: bool = typer.Option(False, "--yes", help="write (default: dry run)"),
    show: bool = typer.Option(False, "--show", help="list the English texts that still need German"),
) -> None:
    """2026-11-24: one-off for a floorball project loaded before the seed fixes: club countries, research notes
    hidden (never deleted), skipped fun facts added in German. No model call; dry run by default."""
    from pathlib import Path

    from klugscheisser import projects
    from klugscheisser.db.session import session_scope
    from klugscheisser.seed import floorball_repair as fr

    p = projects.current()
    with session_scope() as s:
        rep = fr.repair(s, Path(p.seeds_dir), p.main_event, fr.load_german(german), apply=yes)
        typer.echo(f"Projekt {p.key}, {'SCHREIBT' if yes else 'dry run'}: {rep.summary()}")
        for label, rows in (
            ("Klub-Land", [f"{a}: {b} -> {c}" for a, b, c in rep.club_countries]),
            ("versteckt", rep.hidden),
            ("bereinigt", rep.cleaned),
            ("neu", rep.added),
        ):
            for r in rows:
                typer.echo(f"  {label}: {r}")
        if show:
            import json

            typer.echo(json.dumps({t: "" for t in rep.need_german}, ensure_ascii=False, indent=1))
        if not yes:
            s.rollback()


@app.command("floorball-leagues")
def floorball_leagues_cmd(
    sources: str = typer.Option("ssl,fliiga", help="comma list: ssl, fliiga, swissunihockey, stints"),
    yes: bool = typer.Option(False, "--yes", help="write (default: dry run, nothing is stored)"),
    max_requests: int = typer.Option(400, help="cap of HTTP requests per source site"),
) -> None:
    """2026-11-24: one-off backfill (free) of SSL (Sweden) and F-Liiga (Finland) club season stats for every player
    of the project (all seasons the leagues publish), career stints from them, and outdated open club stints
    ended. The nightly floorball step does the same for the tournament's players. Dry run unless --yes."""
    import json

    from sqlalchemy import select

    from klugscheisser.collectors.floorball import run as floorball_run
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope

    with session_scope() as s:
        ids = list(s.scalars(select(m.Athlete.id).where(m.Athlete.role == "athlete").order_by(m.Athlete.id)))
        out = floorball_run.enrich(
            s,
            ids,
            sources=tuple(x.strip() for x in sources.split(",") if x.strip()),
            dry_run=not yes,
            max_requests=max_requests,
        )
        typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        if not yes:
            s.rollback()
            typer.echo("(dry run, nichts gespeichert; mit --yes schreiben)")


@app.command("staff")
def staff_cmd(
    team: str = typer.Argument("SUI", help="team key or name (SUI = the project's Swiss team)"),
    sync: bool = typer.Option(False, help="first load seeds/staff and the staff lines of all stored rosters"),
) -> None:
    """v19: staff of a team (current + history, with source); --sync fills it without a full `seed base`."""
    from klugscheisser import staff
    from klugscheisser.db.session import session_scope
    from klugscheisser.mcp_server.tools import resolve_team

    with session_scope() as s:
        if sync:
            typer.echo(f"seeds: {staff.load_seeds(s)} lines, rosters: {staff.sync_from_entries(s)} lines")
        from klugscheisser.seed.loader import team_key

        t = resolve_team(s, team_key(team) if team == "SUI" else team)
        for a in staff.appointments(s, t):
            r = a.row
            state = "aktuell" if a.current else a.reason
            typer.echo(
                f"{staff.LABEL.get(r.role_key, r.role_key):28} {a.name:24} {state:34} {r.source or ''}"
            )


@app.command("staff-fix")
def staff_fix_cmd(
    from_: str = typer.Option(
        ..., "--from", help="research JSON, e.g. ~/klugscheisser-wch-staff-history.json"
    ),
    yes: bool = typer.Option(False, "--yes", help="write; without it a dry run (rolled back)"),
) -> None:
    """One-off 2026-11-01: WCh staff research → end outdated staff rows, add missing staff persons + their
    history, set wrong old career stations to 'unconfirmed'. Nothing is deleted. Dry run unless --yes."""
    import json
    from pathlib import Path

    from sqlalchemy.orm import sessionmaker

    from klugscheisser import staff_fix
    from klugscheisser.db.session import get_engine

    data = json.loads(Path(from_).expanduser().read_text())
    s = sessionmaker(bind=get_engine())()
    try:
        rep = staff_fix.run(s, data)
        for line in rep.lines():
            typer.echo(line)
        if yes:
            s.commit()
            typer.echo("GESCHRIEBEN")
        else:
            s.rollback()
            typer.echo("Probelauf, nichts geschrieben (mit --yes schreiben)")
    finally:
        s.close()


@app.command("wch-watch")
def wch_watch() -> None:
    """Check the federations' pages once for the official WCh roster announcement (free; also a nightly step)."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.jobs.roster_watch import run

    with session_scope() as s:
        report = run(s)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=1))


@app.command("wch-rosters")
def wch_rosters(
    yes: bool = typer.Option(False, "--yes", help="write the changes (default: dry run, nothing is stored)"),
    event: str = typer.Option("wch-2026", help="tournament key"),
    web: bool = typer.Option(
        True, "--web/--no-web", help="also run the roster watcher (official pages, free)"
    ),
    accept_drops: bool = typer.Option(
        False, help="apply an official list even when it drops more than 6 players of an official list"
    ),
) -> None:
    """One-off (2026-10-28): bring every team's WCh roster up to date: hand-checked official lists from
    seeds/rosters/<event>-*.yaml, then one kaderwatch run. Prints per team old → new count, added, removed, source and
    date. Dry run by default; --yes writes."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.jobs.roster_watch import update_all

    with session_scope() as s:
        out = update_all(s, event, web=web, accept_drops=accept_drops)
        for t in out["teams"]:
            typer.echo(f"{t['team']} ({t['code']}): {t['vorher']} -> {t['nachher']}")
            when = t["datum"] or "?"
            if len(when) == 10:
                when = f"{when[8:10]}.{when[5:7]}.{when[:4]}"
            typer.echo(f"  Quelle: {t['quelle']} | Stand {when} | {t['url']}")
            if t["dazu"]:
                typer.echo(f"  dazu ({len(t['dazu'])}): {', '.join(t['dazu'])}")
            if t["weg"]:
                typer.echo(f"  weg ({len(t['weg'])}): {', '.join(t['weg'])}")
            for k in ("kaderwatch", "fehler"):
                if t.get(k):
                    typer.echo(f"  {k}: {t[k]}")
        for n in out["kaderwatch_neu"]:
            typer.echo(f"Kaderwatch neu: {n['team']} {n['titel']} {n['url']}")
        if not yes:
            s.rollback()
            typer.echo("Probelauf: nichts gespeichert. Mit --yes schreiben.")
        else:
            typer.echo("Gespeichert.")


@seed_app.command("rosters-wikipedia")
def seed_rosters_wikipedia(
    page: str = typer.Option(
        "Ice hockey at the 2026 Winter Olympics – Women's team rosters", help="English Wikipedia roster page"
    ),
    event: str = typer.Option("oly-2026", help="event key in seeds/teams.yaml"),
    season: str = typer.Option("2025-26"),
    load: bool = typer.Option(True, help="load the written seeds right away"),
) -> None:
    """v8: write seeds/rosters/<event>-<nat>.yaml from a Wikipedia roster page (numbers, coaches, QIDs)."""
    from pathlib import Path

    import yaml

    from klugscheisser.collectors.rosters_wikipedia import fetch_wikitext, parse, resolve_qids, to_seed

    teams = parse(fetch_wikitext(page))
    titles = sorted(
        {p.wiki for t in teams for p in t.players if p.wiki}
        | {t.head_coach_wiki for t in teams if t.head_coach_wiki}
    )
    qids = resolve_qids(titles)
    for team in teams:
        path = Path("seeds/rosters") / f"{event}-{team.country.lower()}.yaml"
        seed = to_seed(team, event, page, season, qids=qids)
        head = f"# Generated by `klugscheisser seed rosters-wikipedia` from {seed['source_url']}\n"
        path.write_text(head + yaml.safe_dump(seed, allow_unicode=True, sort_keys=False, width=110))
        typer.echo(f"{path}: {len(team.players)} players, coach {team.head_coach}")
    typer.echo(f"QIDs resolved: {len(qids)} of {len(titles)}")
    if load:
        seed_base()


@seed_app.command("wikidata")
def seed_wikidata(
    country: list[str] = typer.Option(["SUI"], help="IOC codes, repeatable"),  # noqa: B008
    fixture: str | None = typer.Option(None, help="Use saved SPARQL JSON dir instead of the network"),
) -> None:
    """Import female ice hockey players and their handles from Wikidata."""
    import json
    from pathlib import Path

    from klugscheisser.collectors.wikidata import WikidataCollector, parse_memberships, parse_players
    from klugscheisser.db.session import session_scope
    from klugscheisser.seed.loader import load_wikidata_players
    from klugscheisser.sports import COUNTRY_QIDS, current_spec

    sport = current_spec()
    for code in country:
        if fixture:
            d = Path(fixture)
            players = parse_players(json.loads((d / f"players_{code.lower()}.json").read_text()), code, sport)
            parse_memberships(json.loads((d / f"memberships_{code.lower()}.json").read_text()), players)
            found = list(players.values())
        else:
            found = WikidataCollector().collect(sport, code, COUNTRY_QIDS[code])
        with session_scope() as s:
            stats = load_wikidata_players(s, found)
        typer.echo(f"{code}: {stats.athletes} athletes, {stats.handles} handles")


@seed_app.command("wikidata-bulk")
def seed_wikidata_bulk_cmd(
    sport: str = typer.Option(
        "icehockey_women", help="icehockey_women | ski_alpine_women | ski_alpine_men (registered sport key)"
    ),
    nations: str = typer.Option("SUI", help="comma-separated IOC codes, e.g. SUI,SWE,FIN"),
    dry_run: bool = typer.Option(False, "--dry-run", help="only count, write nothing"),
) -> None:
    """Load active athletes of a sport for several nations from Wikidata (free): name, birth date,
    nationality, QID, position, handles (evidence "wikidata"), current teams. Born before 1980 or career end
    (P2032) more than 3 years ago = not active."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.seed.loader import seed_wikidata_bulk
    from klugscheisser.sports import get_sport

    try:
        spec = get_sport(sport)
    except KeyError as exc:
        raise typer.BadParameter(f"unknown sport: {sport}") from exc
    codes = [c.strip().upper() for c in nations.split(",") if c.strip()]
    settings = get_settings()
    kwargs = {"min_birth_year": settings.seed_min_birth_year, "retired_years": settings.seed_retired_years}
    try:
        if dry_run:
            counts, _ = seed_wikidata_bulk(None, spec.key, codes, dry_run=True, **kwargs)
        else:
            with session_scope() as s:
                counts, stats = seed_wikidata_bulk(s, spec.key, codes, **kwargs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    for c in counts:
        # persons born before min_birth_year are already left out by the query (only odd double dates remain)
        typer.echo(
            f"{c.nation}: {c.found} found (alive, born {kwargs['min_birth_year']}+ or unknown), {c.active} active "
            f"(of them {c.no_birth_date} without birth date), {c.retired + c.born_before} dropped "
            f"(career end > {kwargs['retired_years']} years ago or older); "
            f"{c.with_instagram} with Instagram, {c.with_current_team} with current team"
        )
    active = sum(c.active for c in counts)
    if dry_run:
        typer.echo(f"{spec.key}: {active} active persons (dry run, nothing written)")
    else:
        typer.echo(f"{spec.key}: {stats.athletes} persons loaded, {stats.handles} handles")


@handles_app.command("list")
def handles_list(
    status: str | None = typer.Option(None, help="candidate | wahrscheinlich | verified | rejected"),
    athlete: str | None = None,
) -> None:
    """List handles with status, confidence and evidence."""
    from sqlalchemy import select

    from klugscheisser.db import models as m
    from klugscheisser.db import repo
    from klugscheisser.db.session import session_scope
    from klugscheisser.handles.service import reason_de

    with session_scope() as s:
        q = select(m.Handle).join(m.Athlete).order_by(m.Athlete.full_name)
        if status:
            q = q.where(m.Handle.status == status)
        if athlete:
            a = repo.resolve_athlete(s, athlete)
            if a is None:
                raise typer.BadParameter(f"athlete not found: {athlete}")
            q = q.where(m.Handle.athlete_id == a.id)
        for h in s.scalars(q):
            typer.echo(
                f"{h.id:5} {h.athlete.full_name:28} {h.platform:9} @{h.handle:24} "
                f"{h.status:14} {h.confidence:.2f}  {reason_de(h)}"
            )


@handles_app.command("verify")
def handles_verify(handle_id: int, by: str = typer.Option("admin")) -> None:
    """Mark a handle as manually verified."""
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.handles.service import verify

    with session_scope() as s:
        h = s.get(m.Handle, handle_id)
        verify(h, by)
        typer.echo(f"@{h.handle} -> {h.status}")


@handles_app.command("reject")
def handles_reject(handle_id: int, reason: str = "", by: str = typer.Option("admin")) -> None:
    """Reject a handle (never scraped or shown again)."""
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.handles.service import reject

    with session_scope() as s:
        h = s.get(m.Handle, handle_id)
        reject(h, by, reason)
        typer.echo(f"@{h.handle} -> rejected")


@handles_app.command("add")
def handles_add(
    athlete: str,
    handle: str,
    platform: str = "instagram",
    evidence: str = typer.Option("manual", help="evidence kind, e.g. manual, official_link"),
    detail: str = "",
    url: str | None = None,
) -> None:
    """Add a handle with evidence (e.g. an official team page link)."""
    from klugscheisser.db import repo
    from klugscheisser.db.session import session_scope
    from klugscheisser.handles.service import add_handle_candidate

    with session_scope() as s:
        a = repo.resolve_athlete(s, athlete)
        if a is None:
            raise typer.BadParameter(f"athlete not found: {athlete}")
        h = add_handle_candidate(s, a, platform, handle, {"kind": evidence, "detail": detail, "url": url})
        typer.echo(f"{a.full_name}: @{h.handle} -> {h.status} ({h.confidence:.2f})")


@app.command()
def ingest(
    tier: str | None = typer.Option(None, help="daily | weekly"),
    athlete: str | None = typer.Option(None, help="name or id"),
    event: str | None = typer.Option(None, help="event key, e.g. wch-2026"),
    max_posts: int = typer.Option(12, help="posts per handle per run"),
    news: bool = typer.Option(True, help="also collect SRF/RSS news"),
    instagram: bool = typer.Option(True, help="also collect Instagram"),
) -> None:
    """Collect Instagram posts and news, classify, embed and store."""
    from sqlalchemy import select

    from klugscheisser.db import models as m
    from klugscheisser.db import repo
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.runner import athletes_for_event, athletes_for_tier, run_instagram

    settings = get_settings()
    with session_scope() as s:
        if athlete:
            a = repo.resolve_athlete(s, athlete)
            if a is None:
                raise typer.BadParameter(f"athlete not found: {athlete}")
            targets = [a]
        elif event:
            ev = s.scalar(select(m.Event).where(m.Event.key == event))
            if ev is None:
                raise typer.BadParameter(f"event not found: {event}")
            targets = athletes_for_event(s, ev)
        else:
            targets = athletes_for_tier(s, tier or "daily")
        if news:
            from klugscheisser.collectors.rss import RssCollector
            from klugscheisser.ingest.news import ingest_news
            from klugscheisser.ingest.runner import build_context

            typer.echo(f"news: {ingest_news(build_context(s, settings), RssCollector())}")
        if instagram:
            typer.echo(f"{len(targets)} athletes, instagram source: {settings.instagram_source}")
            typer.echo(f"instagram: {run_instagram(s, settings, targets, max_posts=max_posts)}")


@app.command()
def retention(days: int | None = None) -> None:
    """Purge text of irrelevant items older than N days (default from settings)."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.runner import purge_irrelevant

    with session_scope() as s:
        n = purge_irrelevant(s, days or get_settings().retention_irrelevant_days)
    typer.echo(f"purged {n} items")


@app.command()
def reclassify(only: str | None = typer.Option(None, help="only items classified by e.g. 'rules'")) -> None:
    """Re-classify stored items with the current classifier (Jev / LLM / rules)."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.runner import reclassify_all

    with session_scope() as s:
        typer.echo(reclassify_all(s, get_settings(), only_classifier=only))


@app.command("relink-teams")
def relink_teams_cmd(
    since: str | None = typer.Option(None, help="only items published since this date (YYYY-MM-DD)"),
) -> None:
    """Re-run team matching over stored news/web/result items and add missing team links."""
    from datetime import UTC, datetime

    from klugscheisser.db.session import session_scope
    from klugscheisser.domain.team_matcher import relink_teams

    start = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else None
    with session_scope() as s:
        typer.echo(f"added {relink_teams(s, since=start)} team links")


@app.command("sport-audit")
def sport_audit_cmd(
    apply: bool = typer.Option(False, "--apply", help="really unlink / mark (default: only list)"),
    resolve_urls: bool = typer.Option(
        False, help="for items judged only by their URL section: read the outlet's canonical URL (SRF only)"
    ),
    since: str | None = typer.Option(None, help="only items published since this date (YYYY-MM-DD)"),
) -> None:
    """List stored news items the current sport check would not link to women's ice hockey teams
    (e.g. women's football filed under /eishockey/). Dry run unless --apply."""
    from datetime import UTC, datetime

    from klugscheisser.db.session import session_scope
    from klugscheisser.domain.sport_audit import apply_flags, audit_sport

    start = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else None
    with session_scope() as s:
        flags = audit_sport(s, since=start, resolve_urls=resolve_urls)
        for f in flags:
            typer.echo(f.line())
        links = sum(len(f.remove_teams) for f in flags)
        marks = sum(f.mark_irrelevant for f in flags)
        typer.echo(
            f"{len(flags)} items flagged: {links} team links to remove, {marks} items to mark not relevant"
        )
        if not apply:
            s.rollback()
            typer.echo("dry run: nothing changed (use --apply to change)")
            return
        unlinked, marked = apply_flags(s, flags)
        typer.echo(f"applied: {unlinked} team links removed, {marked} items marked not relevant")


@app.command("german-evidence")
def german_evidence_cmd(
    yes: bool = typer.Option(False, "--yes", help="really call the model (paid, ~$0.01 per candidate)"),
    max_usd: float = typer.Option(0.5, help="stop when this much is spent"),
    event: str | None = typer.Option(
        None, help="event keys, comma-separated (default: current/upcoming tournaments)"
    ),
    athlete: str | None = typer.Option(None, help="only these athlete ids, comma-separated"),
    force: bool = typer.Option(False, help="check pairs again that were already checked"),
    as_json: bool = typer.Option(False, "--json", help="full report as JSON"),
) -> None:
    """German interviews from stored texts: `language_evidence` facts (bestätigt only with a German interview)
    for roster persons of other nations. Without --yes: free prefilter only (lists candidates, estimate)."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.facts.german_evidence import run

    settings = get_settings()
    with session_scope() as s:
        rep = run(
            s,
            settings,
            paid=yes,
            max_usd=max_usd,
            event_keys=[x.strip() for x in event.split(",")] if event else None,
            athlete_ids=[int(x) for x in athlete.split(",")] if athlete else None,
            force=force,
        )
        out = rep.out()
        if not yes:
            s.rollback()  # prefilter only: nothing stored
        if as_json:
            typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))
            return
        for k in (
            "personen",
            "paare",
            "schon_geprueft",
            "kandidaten",
            "modell_aufrufe",
            "kosten_usd",
            "geschrieben",
            "gestoppt",
        ):
            typer.echo(f"{k}: {out[k]}")
        for label in ("bestaetigt", "wahrscheinlich", "abgelehnt"):
            for d in out[label]:
                typer.echo(f"[{label}] {d['person']} – {d['grund']} ({d['datum']}) {d['url']}")
                if d["zitat"]:
                    typer.echo(f"    «{d['zitat'][:200]}»")


@app.command("season-totals")
def season_totals_cmd(
    fetch: bool = typer.Option(True, help="fetch the SIHF league tables (2 free requests); off: sheets only"),
    check: bool = typer.Option(
        False, help="also compare the stored game sheets (SIHF, SDHL) with the league-table rows"
    ),
    dry_run: bool = typer.Option(False, help="compute and report, then roll back (nothing stored)"),
    refetch_sheets: bool = typer.Option(
        False, help="once: load the per-player lines (time on ice, PIM) of older Leijonat sheets (1 req/game)"
    ),
) -> None:
    """Current season totals (free): SIHF, NCAA college pages, SDHL/NDHL (swehockey), Auroraliiga (Leijonat)
    league tables + national team counted from the stored sheets."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.current import SHEET_CHECKS, check_sheets, refresh

    settings = get_settings()
    with session_scope() as s:
        out = refresh(s, settings, fetch=fetch, refetch_lines=refetch_sheets)
        typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        if check:
            for platform, source in SHEET_CHECKS.items():
                bad = check_sheets(s, platform, source)
                typer.echo(f"Prüfung {platform}-Spielberichte: " + ("ok" if not bad else "; ".join(bad)))
        if dry_run:
            s.rollback()
            typer.echo("dry run: nichts gespeichert")


@app.command("youth-backfill")
def youth_backfill_cmd(
    yes: bool = typer.Option(False, "--yes", help="store the rows (without: dry run, nothing stored)"),
    seasons: str | None = typer.Option(
        None, help="only these tournaments, by the January year, e.g. 2018,2022 (default: all 2017-2026)"
    ),
) -> None:
    """2026-10-28: U18 Women's WCh (top division, 2017-2026) per player from the Leijonat results service, for the
    persons Elite Prospects has no row of that tournament for. Free, ~90 requests, idempotent."""
    import json

    from klugscheisser.collectors.results.base import PoliteHttp
    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.youth import backfill

    settings = get_settings()
    with session_scope() as s:
        http = PoliteHttp(max(settings.results_request_delay_s, 2.0))
        only = [int(x) for x in seasons.split(",") if x.strip()] if seasons else None
        out = backfill(s, http, store=yes, **({"seasons": only} if only else {}))
        typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))


def _history_scope(s, scope: str, tier: str | None, days: int):
    from klugscheisser.stats.history import scope_ids

    tiers = [int(x) for x in (tier or "").split(",") if x.strip()]
    return scope_ids(s, [x for x in scope.split(",") if x.strip()], tiers, days)


SCOPE_HELP = "first scope(s): recent-games (played in the stored games of the last --days days), wch-2026 or event:<key>"


@app.command("history-gaps")
def history_gaps_cmd(
    scope: str = typer.Option("recent-games,wch-2026", help=SCOPE_HELP),
    tier: str | None = typer.Option(None, help="then these tiers, e.g. 1,2"),
    days: int = typer.Option(7, help="recent-games: look back this many days"),
    all_rows: bool = typer.Option(False, "--all", help="list every person, not only the thin ones"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """2026-11-01: whose career history is thin (no Elite Prospects data, stations without seasons, no stats of
    earlier seasons). Read-only, free."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.history import audit, summary

    with session_scope() as s:
        gaps = audit(s, _history_scope(s, scope, tier, days))
        shown = gaps if all_rows else [g for g in gaps if g.thin]
        if as_json:
            out = {"summe": summary(gaps), "personen": [{**g.__dict__, "flags": g.flags} for g in shown]}
            typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))
            return
        typer.echo(json.dumps(summary(gaps), ensure_ascii=False, indent=1))
        for g in shown:
            typer.echo(g.line())
        s.rollback()


@app.command("sihf-history")
def sihf_history_cmd(
    yes: bool = typer.Option(False, "--yes", help="store (without: dry run, nothing stored)"),
    seasons: str | None = typer.Option(
        None, help="start years, e.g. 2019,2020 (default: 2014 to last season)"
    ),
    leagues: str | None = typer.Option(
        None, help="SIHF league ids (default 42,43,44,45,46 = SWHL A, B, C groups)"
    ),
    scope: str = typer.Option(
        "recent-games,wch-2026", help=SCOPE_HELP + " (only for the before/after report)"
    ),
    tier: str | None = typer.Option("1,2", help="tiers for the before/after report"),
    days: int = typer.Option(7, help="recent-games: look back this many days"),
) -> None:
    """2026-11-01: past seasons (2014/15+) of the Swiss women's leagues from the SIHF's own tables -> season stats
    for players without Elite Prospects data (namesake check; doubtful ones are listed, not stored) + career
    stations with seasons. Free, ~350 requests (~10 min), idempotent."""
    import json

    from klugscheisser.collectors.results.base import PoliteHttp
    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.history import sihf_backfill

    settings = get_settings()
    with session_scope() as s:
        out = sihf_backfill(
            s,
            PoliteHttp(max(settings.results_request_delay_s, 1.5)),
            store=yes,
            seasons=[int(x) for x in seasons.split(",")] if seasons else None,
            leagues=[int(x) for x in leagues.split(",")] if leagues else None,
            scope=_history_scope(s, scope, tier, days),
        )
        typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))


@app.command("ep-ids")
def ep_ids_cmd(
    yes: bool = typer.Option(False, "--yes", help="store the solid matches (without: dry run)"),
    scope: str = typer.Option("recent-games,wch-2026", help=SCOPE_HELP),
    tier: str | None = typer.Option("1,2", help="then these tiers"),
    days: int = typer.Option(7, help="recent-games: look back this many days"),
) -> None:
    """2026-11-01: missing Elite Prospects ids from stored links and Wikidata (P2481), never from Elite Prospects.
    Only solid matches (own Wikidata item, same birth date) are stored; the rest is listed. Free."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.ep_ids import find_ids

    with session_scope() as s:
        out = find_ids(s, list(_history_scope(s, scope, tier, days)), store=yes)
        if not yes:
            s.rollback()
        typer.echo(json.dumps(out, ensure_ascii=False, indent=1, default=str))


@app.command("profiles-backfill")
def profiles_backfill(
    nations: str | None = typer.Option(None, help="e.g. SUI,SWE (default: all)"),
    batch: int = typer.Option(20, help="persons per commit"),
) -> None:
    """Fetch Wikipedia (home language first) for every person never looked up. Free, polite (~1 req/s)."""
    from sqlalchemy import select

    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.profiles import ingest_profiles, missing_profiles, wikipedia_only
    from klugscheisser.ingest.runner import build_context

    settings = get_settings()
    with session_scope() as s:
        q = select(m.Athlete.id).order_by(m.Athlete.nationality != "SUI", m.Athlete.id)
        if nations:
            q = q.where(m.Athlete.nationality.in_([x.strip().upper() for x in nations.split(",")]))
        ids = [a.id for a in missing_profiles(s, [s.get(m.Athlete, i) for i in s.scalars(q)])]
    typer.echo(f"{len(ids)} persons without Wikipedia lookup")
    collectors = wikipedia_only(settings)
    done = docs = 0
    for start in range(0, len(ids), batch):
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in ids[start : start + batch]]
            ctx = build_context(s, settings)
            ctx.embedder = None  # vectors afterwards in batches: `klugscheisser reembed`
            ctx.skip_facts = True  # facts afterwards: `klugscheisser facts-backfill`
            r = ingest_profiles(ctx, athletes, collectors, max_age_days=3650)
            done += r.athletes
            docs += r.docs
        typer.echo(f"{done}/{len(ids)} persons, {docs} Wikipedia documents")


@app.command("profiles-fulltext")
def profiles_fulltext(
    dry_run: bool = typer.Option(False, "--dry-run", help="fetch and count only, change nothing"),
    limit: int = typer.Option(0, help="at most this many documents (0 = all)"),
) -> None:
    """Whole Wikipedia articles for profiles stored cut at the old 6,000-character limit (" […]" at the end).
    Free (Wikipedia API, ~1 request/s, 2 per article). Afterwards `reembed-chunks` re-embeds the changed ones."""
    from klugscheisser.collectors.profiles import PoliteClient, WikipediaCollector
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.profiles import backfill_full_wikipedia

    settings = get_settings()
    collector = WikipediaCollector(
        PoliteClient(min_delay_s=settings.profiles_request_delay_s),
        max_chars=settings.profiles_wikipedia_max_chars,
    )

    def progress(n, r):
        typer.echo(f"{n} checked: {r.longer} longer, {r.unchanged} unchanged, {r.missing} missing")

    with session_scope() as s:
        r = backfill_full_wikipedia(s, collector, dry_run=dry_run, limit=limit, progress=progress)
        if dry_run:
            s.rollback()
    typer.echo(
        f"{r.candidates} cut Wikipedia profiles: {r.longer} {'would get' if dry_run else 'got'} the whole text "
        f"({r.chars_before:,} -> {r.chars_after:,} chars), {r.unchanged} unchanged, {r.missing} missing, "
        f"{len(r.errors)} errors"
    )
    for item_id, title, before, after in r.examples:
        typer.echo(f"  {item_id} {title}: {before:,} -> {after:,}")
    for err in r.errors[:10]:
        typer.echo(f"  error: {err}")
    if r.longer and not dry_run:
        typer.echo("next: klugscheisser reembed-chunks --dry-run, then reembed-chunks (passage vectors)")


@app.command("facts-backfill")
def facts_backfill(
    nations: str | None = typer.Option(None, help="e.g. SUI,SWE (default: all, Swiss first)"),
    limit: int | None = typer.Option(None, help="max persons for the document extraction"),
    wikidata_only: bool = typer.Option(False, "--wikidata-only", help="only the free Wikidata import"),
    budget: float | None = typer.Option(
        None, help="USD per 24 h for llm:<claude|gemini>-facts (default LLM cap)"
    ),
) -> None:
    """Fact layer: Wikidata facts for every person with a QID, then verified facts from her documents."""
    from klugscheisser.facts.backfill import backfill_documents, backfill_wikidata

    settings = get_settings()
    codes = [x.strip().upper() for x in nations.split(",")] if nations else None
    r = backfill_wikidata(codes, echo=typer.echo)
    typer.echo(f"Wikidata: {r['facts']} facts, {r['stints']} stints, {len(r['errors'])} failed chunks")
    if wikidata_only:
        return
    from klugscheisser.llm import text as llm_text

    if not llm_text.available(settings) or not settings.features["jev"]:
        typer.echo(
            "document extraction needs ANTHROPIC_API_KEY (or GEMINI_API_KEY with LLM_PROVIDER=gemini) "
            "and TYPESAFE_API_KEY (Jev support check)"
        )
        raise typer.Exit(1)
    backfill_documents(settings, codes, limit=limit, budget_usd=budget, echo=typer.echo)


@app.command("funfacts-backfill")
def funfacts_backfill(
    nations: str | None = typer.Option(None, help="e.g. SUI,SWE (default: all)"),
    force: bool = typer.Option(False, help="also persons that already have fun facts"),
    budget: float = typer.Option(5.0, help="max USD for this run"),
) -> None:
    """Create 1–3 sourced fun facts per person (once; afterwards only by hand via the 🔄 button)."""
    from sqlalchemy import select

    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.facts.funfacts import backfill

    with session_scope() as s:
        q = select(m.Athlete.id).order_by(m.Athlete.nationality != "SUI", m.Athlete.id)
        if nations:
            q = q.where(m.Athlete.nationality.in_([x.strip().upper() for x in nations.split(",")]))
        ids = list(s.scalars(q))
    typer.echo(backfill(get_settings(), ids, force=force, budget_usd=budget, echo=typer.echo))


@ep_app.command("urls")
def ep_urls_cmd(
    scope: str = typer.Option("recent-games,wch-2026", help=SCOPE_HELP),
    tier: str | None = typer.Option("1,2", help="then these tiers"),
    days: int = typer.Option(7, help="recent-games: look back this many days"),
    out: str | None = typer.Option(
        None, help="write '<athlete_id> <url>' lines to this file (for the Mac fetch)"
    ),
) -> None:
    """2026-11-01: Elite Prospects pages still missing (no EP data), most important first: the player page when
    the id is known, else a search link. Nothing is requested."""
    from pathlib import Path

    from klugscheisser.db.session import session_scope
    from klugscheisser.stats.history import audit, ep_url_list

    with session_scope() as s:
        rows = ep_url_list(s, audit(s, _history_scope(s, scope, tier, days)))
        s.rollback()
    with_id = [r for r in rows if r["ep_id"]]
    if out:
        Path(out).write_text("".join(f"{r['athlete_id']} {r['url']}\n" for r in with_id))
        typer.echo(f"{len(with_id)} Seiten mit EP-ID nach {out}")
    for r in rows:
        typer.echo(f"{r['athlete_id']:5} {r['name']} [{', '.join(r['scopes'])}] {r['url']}")


@ep_app.command("from-html")
def ep_from_html_cmd(
    html_dir: str = typer.Argument(..., help="folder with saved player pages <athlete_id>.html"),
    out_dir: str = typer.Argument(..., help="folder for the raw files that `ep import --from-dir` reads"),
) -> None:
    """2026-11-01: saved Elite Prospects player pages (e.g. fetched once from Ondrej's Mac) -> raw files for
    `ep import --from-dir`. Checks each page's EP id / name against Karl. Nothing is requested."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.ep_import import html_to_raw

    with session_scope() as s:
        counts = html_to_raw(s, html_dir, out_dir)
        s.rollback()
    for key, value in sorted(counts.items()):
        typer.echo(f"{key:28} {value}")


@ep_app.command("import")
def ep_import_cmd(
    from_dir: str | None = typer.Option(
        None, "--from-dir", help="load cached pages (<athlete_id>.json, e.g. data/eliteprospects/raw) first"
    ),
    person: list[int] = typer.Option(None, "--person", help="athlete id, repeatable"),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="count only, write nothing"),
    no_audio: bool = typer.Option(False, "--no-audio", help="do not download pronunciation recordings"),
) -> None:
    """v11: cached Elite Prospects pages -> facts, awards, season stats, career stints, pronunciation,
    handle candidates (no page is fetched; `ep fetch` does that)."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.ep_import import import_profiles, load_dir

    with session_scope() as s:
        if from_dir:
            loaded = load_dir(s, from_dir, athlete_ids=person or None)
            typer.echo("Seiten: " + ", ".join(f"{k} {v}" for k, v in sorted(loaded.items())))
        counts = import_profiles(s, athlete_ids=person or None, audio=not no_audio and not dry_run)
        if dry_run:
            s.rollback()
    for key, value in sorted(counts.items()):
        typer.echo(f"{key:28} {value}")
    if dry_run:
        typer.echo("(dry run, nichts gespeichert)")


@app.command()
def nightly(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="only show who would be refreshed and the estimated cost (read-only, no request)",
    ),
    day: str | None = typer.Option(None, "--date", help="night to plan, YYYY-MM-DD (default: today)"),
    full: bool | None = typer.Option(
        None, "--full/--regular", help="plan the one-off full run / a regular night (default: by settings)"
    ),
    names: bool = typer.Option(True, "--names/--no-names", help="list the persons (dry run)"),
    last: bool = typer.Option(False, "--last", help="show the newest nightly runs and stop"),
) -> None:
    """Nightly refresh now (normally automatic after NIGHTLY_HOUR): tiers per NIGHTLY_TIERS, prepared events,
    news/results/team accounts once. With --dry-run nothing is written and nothing is paid."""
    import json
    from datetime import date, datetime

    from klugscheisser.db.session import session_scope
    from klugscheisser.domain.labels import TZ
    from klugscheisser.jobs import nightly as nj

    settings = get_settings()
    if last:
        with session_scope() as s:
            for run in nj.last_runs(s):
                typer.echo(json.dumps(run, ensure_ascii=False, indent=1, default=str))
        return
    if not dry_run:
        if day:
            raise typer.BadParameter("--date only with --dry-run")
        typer.echo(json.dumps(nj.run_nightly(settings, full=full), ensure_ascii=False, indent=1, default=str))
        return
    d = date.fromisoformat(day) if day else datetime.now(TZ).date()
    with session_scope() as s:
        s.connection().exec_driver_sql("SET TRANSACTION READ ONLY")  # the dry run cannot write
        is_full = (
            full
            if full is not None
            else (
                d == settings.nightly_full_once_date
                and nj.full_due(s, settings, datetime(d.year, d.month, d.day, 23, 59, tzinfo=TZ))
            )
        )
        hour = settings.nightly_full_once_hour if is_full else settings.nightly_hour
        p = nj.plan(s, settings, datetime(d.year, d.month, d.day, hour, 0, tzinfo=TZ), full=is_full)
        typer.echo(json.dumps(p.out(), ensure_ascii=False, indent=1))
        if names:
            from klugscheisser.db import models as m

            for label, ids in p.groups.items():
                people = sorted(s.get(m.Athlete, i).full_name for i in ids)
                typer.echo(f"\n{label} ({len(ids)}): " + ", ".join(people))
        s.rollback()
    typer.echo(
        "\n(dry run: nichts gespeichert, nichts bezahlt; Schätzung ohne LLM-Klassifizierung neuer Posts)"
    )


@app.command("results-backfill")
def results_backfill_cmd(
    only: str | None = typer.Option(None, "--only", help="competition keys, e.g. wch-2025,oly-2026"),
    dry_run: bool = typer.Option(False, "--dry-run", help="list what would be fetched, no request"),
) -> None:
    """One-time backfill of past tournaments (IIHF_BACKFILL_EVENTS by realtime game ids, WIKI_BACKFILL_PAGES).
    Free sources, polite (one request per game / page). Results are classified by rules (no model call);
    re-runs skip games already stored as final. Never part of the nightly run."""
    import json
    from dataclasses import asdict

    from klugscheisser.classify.router import ClassifierRouter
    from klugscheisser.classify.rules import RuleBasedClassifier
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest.results import RESULTS_LOCK, backfill_collectors, ingest_results
    from klugscheisser.ingest.runner import build_context

    settings = get_settings()
    keys = [k.strip() for k in only.split(",")] if only else None
    collectors = backfill_collectors(settings, keys)
    if dry_run or not collectors:
        for cfg in [*settings.iihf_backfill_events, *settings.wiki_backfill_pages]:
            if not keys or cfg.get("competition_key") in keys:
                where = cfg.get("game_ids") or cfg.get("page")
                typer.echo(f"{cfg.get('competition_key')}: {cfg['competition']} ({where})")
        if not collectors:
            typer.echo("nothing configured for --only")
        return
    from sqlalchemy import text as sql_text

    with session_scope() as s:
        if not s.scalar(sql_text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": RESULTS_LOCK}):
            raise typer.Exit("a results refresh is running; try again later")
        ctx = build_context(s, settings)
        ctx.classifier = ClassifierRouter(RuleBasedClassifier(), None, settings)  # no model call
        report = ingest_results(ctx, collectors, since=None)
    typer.echo(json.dumps(asdict(report), ensure_ascii=False, indent=1))


@ep_app.command("fetch")
def ep_fetch(
    tiers: str | None = typer.Option(None, "--tiers", help="e.g. 1,2 (default: 1,2, plus 3 with EP_TIER3)"),
    person: list[int] | None = typer.Option(None, help="athlete id, repeatable; ignores the tier filter"),  # noqa: B008
    limit: int | None = typer.Option(None, help="fetch at most N pages of persons"),
    dry_run: bool = typer.Option(False, "--dry-run", help="list who is due and why; no request"),
    force: bool = typer.Option(
        False, "--force", help="ignore 'due' (never the block, the day stop or the cap)"
    ),
    scope: str | None = typer.Option(
        None, "--scope", help="override EP_SCOPE: tiers | event:<key> (e.g. event:wch-2026)"
    ),
) -> None:
    """v11: polite Elite Prospects fetch of the due player pages into ep_profiles (6–9 s per page)."""
    from klugscheisser.collectors.eliteprospects.fetch import fetch_profiles
    from klugscheisser.db.session import session_scope

    settings = get_settings()
    tier_list = [int(t) for t in tiers.split(",") if t.strip()] if tiers else None
    with session_scope() as s:
        report = fetch_profiles(
            s,
            settings,
            athlete_ids=person or None,
            tiers=tier_list,
            limit=limit,
            dry_run=dry_run,
            force=force,
            scope=scope,
        )
        for line in report.lines(settings.ep_daily_page_cap):
            typer.echo(line)


@app.command()
def reembed(batch: int = typer.Option(100, help="documents per embedding request")) -> None:
    """Compute vectors for all full documents with the configured embedder (EMBEDDING_PROVIDER)."""
    from sqlalchemy import select

    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.embed.providers import get_embedder

    embedder = get_embedder(get_settings())
    with session_scope() as s:
        done = set(s.scalars(select(m.Embedding.item_id).where(m.Embedding.model == embedder.model)))
        items = [i for i in s.scalars(select(m.Item).where(m.Item.level == "full")) if i.id not in done]
        for start in range(0, len(items), batch):
            chunk = items[start : start + batch]
            texts = [
                " ".join(filter(None, [i.source_name, i.title, i.text, i.image_description])) for i in chunk
            ]
            for item, vector in zip(chunk, embedder.embed_documents(texts), strict=True):
                s.add(m.Embedding(item_id=item.id, model=embedder.model, vector=vector))
            s.flush()
        typer.echo(f"{embedder.model}: {len(items)} documents embedded")


@app.command("reembed-chunks")
def reembed_chunks(
    batch: int = typer.Option(100, help="passages per embedding request"),
    limit: int = typer.Option(0, help="at most this many documents (0 = all)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="only count passages and characters (free)"),
) -> None:
    """Passage vectors for RETRIEVAL_CHUNKS (gemini-embedding-2 @768, item_chunks): new or changed full documents
    only (text hash). Paid: ~$0.20 per M tokens; --dry-run shows the size first."""
    from sqlalchemy import delete, select

    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.embed import chunks as C
    from klugscheisser.embed.providers import GeminiChunkEmbedder

    settings = get_settings()
    model = f"{settings.chunk_embedding_model}-{settings.chunk_embedding_dim}"
    with session_scope() as s:
        have = dict(
            s.execute(
                select(m.ItemChunk.item_id, m.ItemChunk.text_hash).where(
                    m.ItemChunk.model == model, m.ItemChunk.ord == 0
                )
            ).all()
        )
        todo = []
        q = select(m.Item).where(m.Item.level == "full", m.Item.text.is_not(None)).order_by(m.Item.id)
        for item in s.scalars(q):
            body = C.doc_body(item)
            h = C.body_hash(body)
            if have.get(item.id) != h:
                todo.append((item, body, h))
            if limit and len(todo) >= limit:
                break
        work = []  # (item, hash, passage, embed text)
        for item, body, h in todo:
            names = C.item_names(item)
            for p in C.split(body):
                work.append((item, h, p, C.embed_text(item, p, names)))
        chars = sum(len(w[3]) for w in work)
        # measured 2026-10-25 on the lab copy: 3.56 characters per token, $0.20 per M tokens (batch API: half)
        usd = chars / 3.56 / 1e6 * 0.20
        typer.echo(f"{model}: {len(todo)} documents, {len(work)} passages, {chars:,} characters, ~${usd:.2f}")
        if dry_run or not work:
            return
        embedder = GeminiChunkEmbedder(settings)
        stale = {item.id for item, _b, _h in todo if item.id in have}
        if stale:
            s.execute(delete(m.ItemChunk).where(m.ItemChunk.model == model, m.ItemChunk.item_id.in_(stale)))
        for start in range(0, len(work), batch):
            part = work[start : start + batch]
            vecs = embedder.embed_documents([w[3] for w in part])
            for (item, h, p, _t), v in zip(part, vecs, strict=True):
                s.add(
                    m.ItemChunk(
                        item_id=item.id,
                        model=model,
                        ord=p.ord,
                        start=p.start,
                        text=p.text,
                        text_hash=h,
                        vector=v,
                    )
                )
            s.commit()  # paid work: keep what is done if a later request fails
            if start // batch % 10 == 0:
                typer.echo(f"  {start + len(part)}/{len(work)} passages", err=True)
        typer.echo(f"{model}: {len(work)} passages embedded")


@app.command("instagram-refresh")
def instagram_refresh_cmd(
    tiers_: str = typer.Option("1,2", "--tiers", help="tiers, e.g. 1,2"),
    max_posts: int = typer.Option(12, help="latest posts per account (regardless of date)"),
    cap: float = typer.Option(7.0, help="hard Apify cap in USD for this run"),
    dry_run: bool = typer.Option(False, help="only count accounts and the estimated cost"),
) -> None:
    """v15: fetch the latest posts of the tier's public, confirmed Instagram accounts again (also older ones),
    so their images/reels get fresh links; posts without an image description are queued for analysis."""
    from dataclasses import asdict

    from sqlalchemy import select

    from klugscheisser.collectors.instagram.sources import get_instagram_source
    from klugscheisser.db import models as m
    from klugscheisser.db.session import session_scope
    from klugscheisser.handles.service import usable_handles
    from klugscheisser.ingest import costs
    from klugscheisser.ingest.instagram import ingest_instagram
    from klugscheisser.ingest.runner import build_context

    wanted = [int(t) for t in tiers_.split(",") if t.strip()]
    settings = get_settings()
    with session_scope() as s:
        people = list(
            s.scalars(select(m.Athlete).where(m.Athlete.tier.in_(wanted), m.Athlete.role == "athlete"))
        )
        people = [a for a in people if any(not (h.profile or {}).get("private") for h in usable_handles(a))]
        estimate = len(people) * max_posts * settings.apify_cost_per_post_usd
        typer.echo(
            f"{len(people)} Konten · bis {max_posts} Posts · geschätzt ${estimate:.2f} (Deckel ${cap:.2f})"
        )
        if settings.features["brightdata"]:
            from klugscheisser.collectors.instagram.brightdata import BrightDataQuota

            per = min(max_posts, settings.brightdata_history_max_posts)
            typer.echo(
                f"Bright Data: {BrightDataQuota(settings).room()} Records frei diesen Monat · "
                f"bis {per} Posts pro Konto (BRIGHTDATA_HISTORY_MAX_POSTS) → höchstens {len(people) * per}"
            )
        if dry_run:
            return
        local = settings.model_copy(update={"apify_daily_cost_cap_usd": costs.spent_today(s, "apify") + cap})
        ctx = build_context(s, local)
        report = ingest_instagram(ctx, get_instagram_source(local), people, max_posts=max_posts, since=None)
        typer.echo(f"fertig: {asdict(report)}")
    from sqlalchemy import func

    with session_scope() as s:
        typer.echo(
            f"Bilder/Reels in der Warteschlange: {s.scalar(select(func.count()).select_from(m.MediaQueue))}"
        )


@app.command("chat-eval")
def chat_eval_cmd(
    only: str | None = typer.Option(None, help="only these ids, e.g. A6,B4"),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="really make the paid model calls (about $0.05 per run with Gemini, $0.65 with Claude)",
    ),
) -> None:
    """Fixed chat evaluation: the questions of tests/fixtures/chat_eval.yaml with the prosa style, each checked;
    the run is stored under data/chat-eval/ and compared with the previous one. Read-only for the chats."""
    from klugscheisser import chat_eval
    from klugscheisser.answer.engine import build_answer_engine
    from klugscheisser.config import get_settings

    items = chat_eval.load()
    if only:
        wanted = {x.strip() for x in only.split(",")}
        wanted |= {
            it["nach"] for it in items if it["id"] in wanted and it.get("nach")
        }  # follow-ups need their turn
        items = [it for it in items if it["id"] in wanted]
    if not yes:
        from klugscheisser.llm import text as llm_text

        per_question = 0.01 if llm_text.provider(get_settings()) == "claude" else 0.0015  # v17: Sonnet ~7x
        estimate = per_question * len(items)
        typer.echo(f"{len(items)} Fragen, geschätzt ${estimate:.2f}. Nichts ausgeführt: mit --yes fragen.")
        return
    settings = get_settings().model_copy(update={"auto_refresh": False})
    before = chat_eval.previous_run()
    outcomes = chat_eval.run(build_answer_engine(settings), items)
    path = chat_eval.save(outcomes)
    typer.echo(chat_eval.report(outcomes, before))
    typer.echo(f"gespeichert: {path}")


@app.command("chat-replay")
def chat_replay_cmd(
    project: str | None = typer.Option(
        None, "--project", help="only this project (default: the current one)"
    ),
    all_projects: bool = typer.Option(False, "--all", help="every project with a database"),
    limit: int | None = typer.Option(None, help="only the newest N questions per project"),
    styles: str = typer.Option("liste,prosa", help="answer styles, old first"),
    out: str | None = typer.Option(None, help="report file (default: data/chat-replay-<time>.html)"),
    fake: bool = typer.Option(False, help="fake engine: no model, no cost (tests)"),
    yes: bool = typer.Option(False, "--yes", help="really make the paid model calls"),
) -> None:
    """v12: answer every chat question again with the old and the new style; side-by-side HTML report.

    Read-only for the chats (nothing stored, no answer cache). Without --yes only counts and estimates."""
    from datetime import UTC, datetime
    from pathlib import Path

    from sqlalchemy.orm import Session

    from klugscheisser import projects, replay
    from klugscheisser.answer.engine import build_answer_engine
    from klugscheisser.db.session import get_engine
    from klugscheisser.projects_admin import database_exists

    wanted = tuple(x.strip() for x in styles.split(",") if x.strip())
    if bad := [x for x in wanted if x not in replay.STYLES]:
        raise typer.BadParameter(f"unknown style {bad}; known: {list(replay.STYLES)}")
    if project and projects.get(project) is None:
        raise typer.BadParameter(f"unknown project {project!r}")
    keys = (
        [p.key for p in projects.all_projects() if database_exists(p.database_url)]
        if all_projects
        else [project or projects.current().key]
    )
    total = 0
    usd = 0.0
    for key in keys:
        with projects.use(key), Session(get_engine()) as s:
            qs = replay.collect_questions(s, key, get_settings().chat_history_turns, limit)
            est = replay.estimate(len(qs), wanted, replay.average_answer_cost(s))
            s.rollback()
        typer.echo(f"{key}: {est.text()}")
        total += len(qs)
        usd += est.usd
    if not fake and not yes:
        typer.echo(
            f"Gesamt {total} Fragen, geschätzt ${usd:.2f}. Nichts ausgeführt: mit --yes wirklich fragen."
        )
        return
    started = datetime.now(UTC)
    rows = []
    for key in keys:
        with projects.use(key):
            settings = get_settings().model_copy(update={"auto_refresh": False})
            engine = replay.ReplayFakeEngine(settings) if fake else build_answer_engine(settings)
        rows += replay.replay_project(key, engine, wanted, limit, echo=typer.echo)
    ledger = None if fake else sum(replay.ledger_since(k, started) for k in keys)
    note = "Testmodus (fake, ohne KI)" if fake else ""
    path = Path(out or f"data/chat-replay-{started.astimezone().strftime('%Y%m%d-%H%M')}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(replay.report_html(rows, wanted, ledger, note), encoding="utf-8")
    typer.echo(f"{len(rows)} Fragen → {path}" + (f" (Kosten ${ledger:.4f})" if ledger is not None else ""))


@app.command()
def gamewatch(
    show: bool = typer.Option(True, "--status", help="list the games of the watched IIHF events (default)"),
    once: bool = typer.Option(False, "--once", help="one pass now: poll / re-check / follow-up when due"),
    all_games: bool = typer.Option(False, "--all", help="with --status: also games without anything done"),
) -> None:
    """Game watch (jobs/gamewatch.py; runs by itself inside `serve` with GAME_WATCH=true): IIHF games right after
    the final buzzer, then news + Instagram of both teams about an hour later."""
    import json

    from klugscheisser.db.session import session_scope
    from klugscheisser.jobs import gamewatch as gw

    del show  # the status list is the default view; `--status` only names it
    settings = get_settings()
    if once:
        typer.echo(json.dumps(gw.watch_pass(settings), ensure_ascii=False, indent=1, default=str))
        return
    with session_scope() as s:
        rows = gw.status(s, settings)
    typer.echo(
        f"Game watch: {'an' if settings.game_watch else 'aus'} · Abfrage alle {settings.game_watch_poll_minutes} "
        f"Min. · Follow-up {settings.game_watch_followup_delay_min} Min. nach dem Schluss"
        + ("" if settings.game_watch_followup else " (aus)")
        + f" · Kader aus Spielbericht {'an' if settings.game_watch_sheet_rosters else 'aus'}"
        + f" · Kaderwatch nach dem Spiel {'an' if settings.game_watch_roster_watch else 'aus'}"
    )
    for r in rows:  # --status is the default; scheduled games only with --all
        if not all_games and r["status"] == "scheduled" and not r["final_gespeichert"]:
            continue
        typer.echo(
            f"{r['anpfiff']}  {r['spiel']:<26} {(r['resultat'] or r['status']):<10} "
            f"final: {r['final_gespeichert'] or '-':<12} blatt: {r['spielblatt'] or '-':<13} "
            f"kontrolle: {r['nachkontrolle'] or '-':<12} follow-up: {r['follow_up'] or '-'}"
            + (f" ({r['follow_up_status']}: {r['follow_up_ergebnis']})" if r["follow_up_status"] else "")
        )
        for code, k in (r.get("kader_spielbericht") or {}).items():
            for what, label in (
                ("dazu", "nachgemeldet"),
                ("unklar", "unklar"),
                ("nicht_eingesetzt", "nicht eingesetzt"),
            ):
                if k.get(what):
                    typer.echo(f"      {code} {label}: {', '.join(k[what])}")


def main() -> None:
    app()
