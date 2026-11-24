"""`klugscheisser free-profiles …`: free profile sources (2026-10-25). All free, no model call, run by hand
(the current WCh roster from Hockey Canada is also read in the nightly step "hockeycanada")."""

from __future__ import annotations

import typer

app = typer.Typer(
    no_args_is_help=True, help="Free profile sources: Hockey Canada, Wikipedia infobox, Wikidata."
)

FOCUS_HELP = "who: focus (SUI roster + other WCh players in the Swiss league), event (whole WCh field) or all"


def _ids(s, who: str) -> list[int] | None:
    from klugscheisser.facts.coverage import focus_groups

    if who == "all":
        return None
    g = focus_groups(s)
    return sorted(set(g["a"]) | set(g["b"])) if who == "focus" else g["all"]


def _print_counts(counts: dict) -> None:
    for key, value in sorted(counts.items()):
        typer.echo(f"  {key:28} {value}")


@app.command("coverage")
def coverage_cmd(
    event: str | None = typer.Option(
        None, help="event key of the rosters (default: the project's main event)"
    ),
) -> None:
    """Coverage table: height, weight, shoots, birth date, photo, past-season stats per focus group."""
    from klugscheisser import projects
    from klugscheisser.db.session import session_scope
    from klugscheisser.facts.coverage import FIELDS, table

    event = event or projects.current().main_event  # "wch-2026" in the ice hockey project

    with session_scope() as s:
        rows = table(s, event)
    typer.echo(f"{'Gruppe':48} {'n':>4} " + " ".join(f"{f:>10}" for f in FIELDS))
    for label, c in rows:
        typer.echo(f"{label:48} {c['n']:>4} " + " ".join(f"{c[f]:>10}" for f in FIELDS))


@app.command("hockeycanada")
def hockeycanada_cmd(
    only: str | None = typer.Option(None, help="event keys, comma separated (default: all, newest first)"),
    who: str = typer.Option("all", help=FOCUS_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="fetch and match, store nothing"),
    show_unmatched: bool = typer.Option(False, "--unmatched", help="list roster lines without a person"),
) -> None:
    """Tournament rosters (WCh 2026/2025/2024, Olympics 2026, U18 WCh 2026/2025) from hockeycanada.ca →
    height, weight, shoots, position, birth date, hometown (+ number and club for WCh 2026). ~65 requests, 3 s apart."""
    from klugscheisser.collectors.hockeycanada import EVENTS, HockeyCanadaClient, event_by_key
    from klugscheisser.config import get_settings
    from klugscheisser.db.session import session_scope
    from klugscheisser.facts import hockeycanada_import as hc

    settings = get_settings()
    events = [event_by_key(k.strip()) for k in only.split(",")] if only else list(EVENTS)
    client = HockeyCanadaClient(delay_s=settings.hockeycanada_delay_s)
    with session_scope() as s:
        ids = _ids(s, who)
        r = hc.run(s, client, events, only_athlete_ids=set(ids) if ids is not None else None)
        if dry_run:
            s.rollback()
    typer.echo(
        f"{len(r.events)} Turniere, {r.rosters} Kader ({len(r.empty_rosters)} leer), {r.lines} Zeilen, "
        f"{r.matched} zugeordnet → {r.persons} Personen, {len(r.unmatched)} ohne Person, "
        f"{len(r.ambiguous)} mehrdeutig, {r.requests} Abrufe"
    )
    _print_counts(r.counts)
    if r.empty_rosters:
        typer.echo("leer (noch nicht gemeldet): " + ", ".join(r.empty_rosters))
    for e in r.errors:
        typer.echo(f"  Fehler: {e}")
    if show_unmatched:
        for line in r.unmatched + r.ambiguous:
            typer.echo(f"  - {line}")
    if dry_run:
        typer.echo("(dry run, nichts gespeichert)")


def _athletes(s, who: str):
    from sqlalchemy import select

    from klugscheisser.db import models as m

    ids = _ids(s, who)
    q = select(m.Athlete).where(m.Athlete.role == "athlete")
    if ids is not None:
        q = q.where(m.Athlete.id.in_(ids))
    return list(s.scalars(q.order_by(m.Athlete.id)))


@app.command("wikidata")
def wikidata_cmd(
    who: str = typer.Option("event", help=FOCUS_HELP),
    no_photos: bool = typer.Option(False, "--no-photos", help="facts only"),
    dry_run: bool = typer.Option(False, "--dry-run", help="fetch, store nothing"),
) -> None:
    """Wikidata facts (now with weight P2067) and the Commons photo (P18, link + author + licence) for persons
    with a QID. Free: one SPARQL request per 50 persons, 2 API calls per missing photo."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.facts.wikidata_extra import photos
    from klugscheisser.facts.wikidata_import import import_wikidata_facts

    with session_scope() as s:
        athletes = _athletes(s, who)
        stats = import_wikidata_facts(s, athletes)
        typer.echo(f"Wikidata: {stats['persons']} Personen mit QID, {stats['facts']} neue Fakten")
        if not no_photos:
            _print_counts(photos(s, athletes))
        if dry_run:
            s.rollback()
            typer.echo("(dry run, nichts gespeichert)")


@app.command("wikipedia-infobox")
def wikipedia_infobox_cmd(
    who: str = typer.Option("event", help=FOCUS_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="fetch, store nothing"),
) -> None:
    """Infobox of the stored en/de/fr Wikipedia articles → height, weight, shoots, position, birth place
    (source Wikipedia, below EP / Hockey Canada / Wikidata). Free: 1 API request per 50 articles."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.facts import wikipedia_infobox as wi

    with session_scope() as s:
        r = wi.run(s, _ids(s, who))
        if dry_run:
            s.rollback()
    typer.echo(
        f"{r.articles} Artikel, {r.fetched} gelesen, {r.with_infobox} mit Infobox, {r.persons} Personen mit Werten, "
        f"{r.requests} Abrufe"
    )
    _print_counts(r.counts)
    if dry_run:
        typer.echo("(dry run, nichts gespeichert)")
