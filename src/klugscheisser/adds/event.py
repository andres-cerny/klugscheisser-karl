"""v5 phase 4: event preparation ("Ich gehe an die WM 2026").

1. Proposal (background, ~1 min for 10 teams): per team the stored roster plus coaches/staff (and the roster
   from Wikidata/Wikipedia when we have none), cost estimate → ✅ Ja / ❌ Nein.
2. Load (background): persons + event entries, Instagram (as far as the cap allows), linking stored news,
   results, profiles, and a web search per person (Brave, pages only where robots/terms allow, namesake check
   with Jev) — slow is fine here, answers later come from the database.
3. Nightly refresh for the event's persons in a date range (jobs/nightly.py).
Stored as an AddRequest with kind "event"; input = {"event_key", "team_ids", "nightly_from", "nightly_to"}.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds.base import (
    FIRST_LOAD_RESULTS_PER_HANDLE,
    Proposal,
    ProposedPerson,
    ProposedTeam,
    SourceRef,
)
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec, current_sport_key

log = logging.getLogger(__name__)

WEB_RESULTS_PER_PERSON = 4


def estimate_event_cost_chf(settings: Settings, persons: int, with_instagram: int) -> float:
    usd = with_instagram * (FIRST_LOAD_RESULTS_PER_HANDLE + 1) * settings.apify_cost_per_post_usd
    if settings.brave_api_key is not None:
        usd += persons * settings.brave_cost_per_request_usd  # one web search per person
    return round(usd * settings.usd_to_chf, 2)


def event_teams(s: Session, event: m.Event, team_ids: list[int] | None = None) -> list[m.Team]:
    teams = list(
        s.scalars(
            select(m.Team).join(m.EventTeam).where(m.EventTeam.event_id == event.id).order_by(m.Team.name)
        )
    )
    if team_ids:
        teams = [t for t in teams if t.id in set(team_ids)]
    return sorted(teams, key=lambda t: t.country != "SUI")  # Swiss team first


def default_nightly_range(event: m.Event) -> tuple[date | None, date | None]:
    if event.starts_at is None:
        return None, None
    start = event.starts_at.date() - timedelta(days=3)
    end = (event.ends_at or event.starts_at).date()
    return start, end


# --- proposal ----------------------------------------------------------------------------------------


def _staff_for(
    settings: Settings, s: Session, team: m.Team, resolver
) -> tuple[list[ProposedPerson], list[str]]:
    """Coaches/staff (and players when we have no roster) from Wikidata/Wikipedia via the team resolver."""
    from klugscheisser.adds.team import interpret

    notes: list[str] = []
    try:
        spec = current_spec()  # 2026-11-24: the project's sport ("Männer-Nationalteam Unihockey")
        req = interpret(
            f"{team.name} {'Männer' if spec.men else 'Frauen'}-Nationalteam {wording().sport_de}",
            resolver.llm,
        )
        req.kind, req.gender = "national", "male" if spec.men else "female"
        if team.country:
            req.country = team.country
        entity, _ = resolver.wikidata_team(req, set(), notes)
        if entity is None:
            return [], [f"{team.name}: kein Wikidata-Eintrag für Trainer/Staff gefunden."]
        roster, source, _section, _lang = resolver.wikipedia_roster(entity)
        out: list[ProposedPerson] = []
        if roster is not None:
            for row in roster.rows:
                out.append(
                    ProposedPerson(
                        name=row.name,
                        role=row.role,
                        function_de=row.function_de if row.role != "athlete" else None,
                        sport_key=spec.key,
                        nationality=row.nationality or team.country,
                        birth_date=row.birth_date,
                        position=row.position if row.role == "athlete" else None,
                        jersey_number=row.number,
                        sources=[source],
                    )
                )
        known = {p.name.lower() for p in out}
        for qid, label in entity.coaches:
            if label.lower() not in known:
                out.append(
                    ProposedPerson(
                        name=label,
                        role="coach",
                        function_de="Cheftrainer/in",
                        sport_key=spec.key,
                        wikidata_qid=qid,
                        sources=[SourceRef(f"Wikidata {entity.qid}", entity.url)],
                    )
                )
        return out, notes
    except Exception as exc:  # one team failing never blocks the others
        log.warning("event prep: staff for %s failed: %s", team.name, exc)
        return [], [f"{team.name}: Trainer/Staff nicht gefunden ({exc})."]


def build_event_proposal(
    s: Session, settings: Settings, event: m.Event, team_ids: list[int] | None = None, resolver=None
) -> Proposal:
    from klugscheisser.adds.person import find_existing
    from klugscheisser.adds.resolvers import get_team_resolver
    from klugscheisser.handles.service import usable_handles

    resolver = resolver or get_team_resolver(settings)
    persons: list[ProposedPerson] = []
    notes: list[str] = []
    lines: list[str] = []
    for team in event_teams(s, event, team_ids):
        entries = list(
            s.scalars(
                select(m.EventEntry).where(m.EventEntry.event_id == event.id, m.EventEntry.team_id == team.id)
            )
        )
        team_persons: list[ProposedPerson] = []
        for en in entries:
            a = en.athlete
            team_persons.append(
                ProposedPerson(
                    name=a.full_name,
                    role=a.role or "athlete",
                    sport_key=a.sport.key if a.sport else current_sport_key(),
                    nationality=a.nationality,
                    position=a.position,
                    jersey_number=en.jersey_number,
                    instagram=usable_handles(a)[0].handle if usable_handles(a) else None,
                    instagram_source="wikidata" if usable_handles(a) else None,
                    existing_id=a.id,
                    event_team_id=team.id,
                    sources=[SourceRef(en.source or "Kader", en.source_url)],
                )
            )
        extra, team_notes = _staff_for(settings, s, team, resolver)
        notes += team_notes
        known = {p.name.lower() for p in team_persons}
        for p in extra:
            if p.name.lower() in known or (entries and p.role == "athlete"):
                continue  # our stored roster wins for players; Wikipedia adds coaches/staff
            if existing := find_existing(s, p.wikidata_qid, p.name, p.birth_date):
                p.existing_id = existing.id
            p.event_team_id = team.id
            team_persons.append(p)
        players = sum(1 for p in team_persons if p.role == "athlete")
        staff = len(team_persons) - players
        lines.append(f"{team.name}: {players} {wording().players}, {staff} Trainer/Staff")
        persons += team_persons
    with_ig = sum(1 for p in persons if p.instagram)
    cost = estimate_event_cost_chf(settings, len(persons), with_ig)
    start, end = default_nightly_range(event)
    summary = (
        f"Vorbereitung {event.name}: {len(persons)} Personen in {len(lines)} Teams "
        f"({with_ig} mit Instagram). " + " · ".join(lines)
    )
    if start and end:
        summary += f". Nächtliche Aktualisierung {start:%d.%m.}–{end:%d.%m.%Y}."
    if cost > settings.event_prep_cap_chf:
        notes.append(
            f"Schätzung über dem Limit von {settings.event_prep_cap_chf:.0f} CHF: Instagram wird nur so weit "
            "geladen, wie das Limit reicht."
        )
    return Proposal(
        kind="event",
        found=bool(persons),
        summary=summary if persons else f"Keine Teams/Kader für {event.name} gefunden.",
        persons=persons,
        team=ProposedTeam(name=event.name, kind="event"),
        notes=notes,
        cost_estimate_chf=cost,
    )


def propose_event(
    s: Session,
    settings: Settings,
    event: m.Event,
    team_ids: list[int] | None = None,
    nightly: tuple[date | None, date | None] | None = None,
    submit=None,
) -> m.AddRequest:
    """Creates the request and builds the proposal in the background (status resolving → proposed)."""
    start, end = nightly or default_nightly_range(event)
    req = m.AddRequest(
        kind="event",
        status="resolving",
        input={
            "event_key": event.key,
            "team_ids": team_ids or [],
            "nightly_from": start.isoformat() if start else None,
            "nightly_to": end.isoformat() if end else None,
        },
    )
    s.add(req)
    s.flush()
    s.commit()
    from klugscheisser.jobs.refresh import executor

    (submit or executor().submit)(_resolve_job, req.id, settings)
    return req


def _resolve_job(request_id: int, settings: Settings, resolver=None) -> None:
    with session_scope() as s:
        req = s.get(m.AddRequest, request_id)
        event = s.scalar(select(m.Event).where(m.Event.key == req.input["event_key"]))
        try:
            proposal = build_event_proposal(s, settings, event, req.input.get("team_ids") or None, resolver)
            req.proposal, req.cost_estimate_chf, req.status = (
                proposal.to_json(),
                proposal.cost_estimate_chf,
                "proposed",
            )
        except Exception as exc:
            log.exception("event proposal %s failed", request_id)
            req.status, req.error = "failed", str(exc)


# --- apply + load ----------------------------------------------------------------------------------


def apply_event(s: Session, req: m.AddRequest) -> list[int]:
    """Persons (new or existing), national-team affiliation and event entries."""
    from klugscheisser.adds.load import _affiliate, create_person

    event = s.scalar(select(m.Event).where(m.Event.key == req.input["event_key"]))
    proposal = Proposal.from_json(req.proposal or {})
    ids: list[int] = []
    for p in proposal.persons:
        athlete = create_person(s, p, req)
        if p.event_team_id:
            team = s.get(m.Team, p.event_team_id)
            src = p.sources[0] if p.sources else None
            _affiliate(s, athlete, team, src.label if src else "Vorbereitung", src.url if src else None)
            exists = s.scalar(
                select(m.EventEntry).where(
                    m.EventEntry.event_id == event.id, m.EventEntry.athlete_id == athlete.id
                )
            )
            if exists is None:
                s.add(
                    m.EventEntry(
                        event_id=event.id,
                        athlete_id=athlete.id,
                        team_id=team.id,
                        role=p.position if p.role == "athlete" else p.role,
                        jersey_number=p.jersey_number,
                        list_type="provisional",
                        source=src.label if src else "Vorbereitung",
                        source_url=src.url if src else None,
                    )
                )
        ids.append(athlete.id)
    req.athlete_ids = list(dict.fromkeys(ids))
    s.flush()
    return req.athlete_ids


def prep_web_search(
    settings: Settings, athlete_ids: list[int], budget_usd: float, progress=None
) -> dict[str, Any]:
    """One Brave query per person; read only allowed pages; store a page only if Jev says it is about
    THIS person (never a namesake)."""
    from klugscheisser.answer.engine import GeminiAnswerEngine
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.web_search import get_web_search
    from klugscheisser.web_search.store import store_web_page

    stats = {"suchen": 0, "seiten": 0, "gespeichert": 0, "andere_person": 0}
    if settings.brave_api_key is None:
        return {**stats, "hinweis": "keine Websuche (BRAVE_API_KEY fehlt)"}
    checker = GeminiAnswerEngine(settings)
    for n, aid in enumerate(athlete_ids, 1):
        if stats["suchen"] * settings.brave_cost_per_request_usd >= budget_usd:
            stats["hinweis"] = "Budget erreicht"
            break
        if progress and n % 5 == 1:
            progress(f"Websuche ({n}/{len(athlete_ids)})")
        with session_scope() as s:
            a = s.get(m.Athlete, aid)
            web = get_web_search(settings, session=s)
            if web is None:
                break
            try:
                from klugscheisser.sports import get_sport

                noun = get_sport(a.sport.key).person_de
            except Exception:
                noun = "Sportlerin"
            query = f'"{a.full_name}" {noun}'
            try:
                results = web.search(query, limit=WEB_RESULTS_PER_PERSON)
            except Exception as exc:
                log.warning("prep web search %s failed: %s", a.full_name, exc)
                continue
            stats["suchen"] += 1
            ctx = build_context(s, settings)
            for r in results:
                page = web.fetch(r.url)
                if not page.allowed or not page.text:
                    continue
                stats["seiten"] += 1
                if not checker._is_about(a, page.title, page.text):
                    stats["andere_person"] += 1
                    continue
                if store_web_page(ctx, page, [a.id], query) is not None:
                    stats["gespeichert"] += 1
    return stats


def confirm_event(s: Session, settings: Settings, req: m.AddRequest, submit=None) -> m.Job:
    if req.status != "proposed":
        raise ValueError(f"Anfrage #{req.id} ist nicht offen (Status {req.status}).")
    apply_event(s, req)
    from klugscheisser.adds.load import start_add_load

    return start_add_load(s, settings, req, submit)


def nightly_persons(s: Session, today: date | None = None) -> list[int]:
    """Persons of all prepared events whose nightly range includes today."""
    today = today or datetime.now(UTC).date()
    ids: list[int] = []
    for req in s.scalars(
        select(m.AddRequest).where(m.AddRequest.kind == "event", m.AddRequest.status == "done")
    ):
        start, end = req.input.get("nightly_from"), req.input.get("nightly_to")
        if start and end and date.fromisoformat(start) <= today <= date.fromisoformat(end):
            ids += req.athlete_ids or []
    return list(dict.fromkeys(ids))
