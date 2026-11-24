"""v4: confirmed proposal → persons in the DB → background load (Instagram, news links, results, profiles).

Cost cap: one add action (proposal + load) may spend at most `add_cost_cap_chf`. Instagram is the only paid
source in the load; persons beyond the budget get no Instagram load now (their posts load on demand later).
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.adds.base import FIRST_LOAD_RESULTS_PER_HANDLE, Proposal, ProposedPerson
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import date_de
from klugscheisser.handles.service import add_handle_candidate, usable_handles

log = logging.getLogger(__name__)

ROLES = {"athlete", "coach", "staff", "official"}


# --- create persons ----------------------------------------------------------------------------------


def ensure_sport(s: Session, p: ProposedPerson, default_key: str | None = None) -> m.Sport:
    """Generic sport layer: unknown sports are registered on the fly from the confirmed proposal."""
    from klugscheisser.sports import SportSpec, current_sport_key, get_sport, register

    key = p.sport_key or default_key or current_sport_key()  # the project's sport ("icehockey_women", …)
    try:
        spec = get_sport(key)
    except KeyError:
        spec = register(
            SportSpec(
                key=key,
                name_de=p.sport_name_de or key,
                person_de=p.person_de or "Sportlerin",
                wikidata_occupation="",
            )
        )
    return repo.get_sport(s, key, spec.name_de)


def _team(
    s: Session, sport: m.Sport, name: str, kind: str, country: str | None, league: str | None
) -> m.Team:
    from klugscheisser.domain.names import normalize

    team = repo.find_team(s, name, fuzzy=False)
    if team is None:
        key = normalize(name).replace(" ", "-")[:80]
        if repo.get_team(s, key) is not None:
            key = f"{key[:70]}-{sport.id}"
        team = m.Team(sport_id=sport.id, key=key, name=name, kind=kind, country=country, league=league)
        s.add(team)
        s.flush()
    return team


def _affiliate(
    s: Session, athlete: m.Athlete, team: m.Team, source: str, url: str | None, number=None
) -> None:
    exists = s.scalar(
        select(m.Affiliation).where(m.Affiliation.athlete_id == athlete.id, m.Affiliation.team_id == team.id)
    )
    if exists is None:
        s.add(
            m.Affiliation(
                athlete_id=athlete.id, team_id=team.id, jersey_number=number, source=source, source_url=url
            )
        )
        s.flush()


def create_person(s: Session, p: ProposedPerson, req: m.AddRequest) -> m.Athlete:
    sport = ensure_sport(s, p)
    athlete = s.get(m.Athlete, p.existing_id) if p.existing_id else None
    position = p.position if p.position and len(p.position) <= 20 else None
    if athlete is None:
        athlete = repo.upsert_athlete(
            s,
            sport,
            p.name,
            birth_date=p.birth_date,
            nationality=p.nationality,
            wikidata_qid=p.wikidata_qid,
            eliteprospects_id=p.eliteprospects_id,
            position=position,
        )
        if p.role in ROLES and athlete.role == "athlete":
            athlete.role = p.role
    else:  # only fill what is missing
        for key, value in {
            "birth_date": p.birth_date,
            "nationality": p.nationality,
            "eliteprospects_id": p.eliteprospects_id,
            "position": position,
        }.items():
            if value is not None and getattr(athlete, key) in (None, ""):
                setattr(athlete, key, value)
        if p.wikidata_qid and not athlete.wikidata_qid:
            taken = s.scalar(select(m.Athlete.id).where(m.Athlete.wikidata_qid == p.wikidata_qid))
            if taken is None:
                athlete.wikidata_qid = p.wikidata_qid
    note = f"Hinzugefügt von Fabi am {date_de(datetime.now(UTC), True)} (Anfrage #{req.id})"
    if p.function_de:
        note += f"; Funktion: {p.function_de}"
    if note not in (athlete.notes or ""):
        athlete.notes = f"{athlete.notes}\n{note}".strip() if athlete.notes else note
    if p.instagram:
        if p.instagram_source == "wikidata" and p.wikidata_qid:
            evidence = {
                "kind": "wikidata",
                "detail": f"Wikidata {p.wikidata_qid}",
                "url": f"https://www.wikidata.org/wiki/{p.wikidata_qid}",
            }
        else:
            evidence = {"kind": "fabi", "detail": f"von Fabi angegeben (Anfrage #{req.id})"}
        add_handle_candidate(s, athlete, "instagram", p.instagram, evidence)
    if p.team:
        src = p.sources[0] if p.sources else None
        team = _team(s, sport, p.team, "club", None, None)
        _affiliate(s, athlete, team, src.label if src else "Angabe beim Hinzufügen", src.url if src else None)
    s.flush()
    return athlete


def apply_proposal(s: Session, req: m.AddRequest) -> list[int]:
    """Create/update persons (and the team) of a confirmed proposal; returns the athlete ids."""
    proposal = Proposal.from_json(req.proposal or {})
    team: m.Team | None = None
    if proposal.team is not None:
        pt = proposal.team
        sport = ensure_sport(s, ProposedPerson(name="", sport_key=pt.sport_key))
        team = s.get(m.Team, pt.existing_id) if pt.existing_id else None
        team = team or _team(s, sport, pt.name, pt.kind, pt.country, pt.league)
        req.team_id = team.id
    ids = []
    for p in proposal.persons:
        athlete = create_person(s, p, req)
        if team is not None:
            src = proposal.team.roster_source if proposal.team else None
            _affiliate(
                s,
                athlete,
                team,
                src.label if src else "Kader beim Hinzufügen",
                src.url if src else None,
                p.jersey_number,
            )
        ids.append(athlete.id)
    req.athlete_ids = list(dict.fromkeys(ids))
    s.flush()
    return req.athlete_ids


# --- link stored documents to the new persons ---------------------------------------------------------


def link_existing_items(s: Session, athlete_ids: list[int], max_items: int = 3000, ctx=None) -> int:
    """News/results/web pages already stored before the person existed: link them by name now.

    Light documents (title/teaser only, v5) that name her are promoted to full: classified and embedded
    when an ingest context is given."""
    from klugscheisser.domain.matcher import AthleteMatcher
    from klugscheisser.ingest.store import apply_classification, classify_input, embed_item

    athletes = [s.get(m.Athlete, i) for i in athlete_ids]
    names = [a.last_name or a.full_name.split()[-1] for a in athletes if a is not None]
    if not names:
        return 0
    wanted = set(athlete_ids)
    cond = or_(*[m.Item.text.ilike(f"%{n}%") for n in names], *[m.Item.title.ilike(f"%{n}%") for n in names])
    items = s.scalars(
        select(m.Item).where(m.Item.platform != "instagram", cond).order_by(m.Item.id.desc()).limit(max_items)
    )
    matcher = AthleteMatcher(s)
    added = 0
    for item in items:
        have = {ia.athlete_id for ia in item.athletes}
        for link in matcher.from_text(f"{item.title or ''}\n{item.text or ''}"):
            if link.athlete_id in wanted and link.athlete_id not in have:
                item.athletes.append(m.ItemAthlete(athlete_id=link.athlete_id, match_kind="name"))
                have.add(link.athlete_id)
                added += 1
                if item.level == "light":
                    item.level = "full"
                    if ctx is not None:
                        s.flush()
                        apply_classification(s, item, ctx.classifier.classify(classify_input(item)))
                        embed_item(s, ctx.embedder, item)
    s.flush()
    return added


# --- background load ----------------------------------------------------------------------------------


def action_budget_usd(settings: Settings, req: m.AddRequest) -> float:
    spent = float((req.proposal or {}).get("resolver_cost_usd") or 0.0)
    cap = settings.event_prep_cap_chf if req.kind == "event" else settings.add_cost_cap_chf
    return max(0.0, cap / settings.usd_to_chf - spent)


def _progress(job_id: int, **fields: Any) -> None:
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.result = {**(job.result or {}), **fields}


def run_add_load(job_id: int, request_id: int, settings: Settings) -> None:
    """Worker: profiles check of new handles → Instagram → link stored news → results → profiles."""
    from klugscheisser.collectors.instagram.sources import (
        get_instagram_source,
        instagram_configured,
        paid_profile_count,
    )
    from klugscheisser.handles.profile import apply_profile
    from klugscheisser.ingest import costs
    from klugscheisser.ingest.runner import run_instagram
    from klugscheisser.jobs.freshness import EXTRA_SOURCES, SLOW_SOURCES

    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
        req = s.get(m.AddRequest, request_id)
        ids = list(req.athlete_ids)
        kind = req.kind
        budget = action_budget_usd(settings, req)
    status, error = "done", None
    steps: dict[str, Any] = {}
    try:
        # 1. check new Instagram handles (profile name, bio) → tiered status; one Apify run for all
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in ids]
            todo = [h for a in athletes for h in a.handles if h.platform == "instagram" and not h.profile]
            if todo and instagram_configured(settings) and budget > 0.05:
                _progress(job_id, schritt="Instagram-Profile prüfen", personen=len(ids))
                source = get_instagram_source(settings)
                profiles = {p.username.lower(): p for p in source.fetch_profiles([h.handle for h in todo])}
                # Apify charges per result: no more than it returned, and nothing for what Graph read
                n = min(len(profiles), paid_profile_count(source, len(todo)))
                cost = n * settings.apify_cost_per_post_usd
                if n:
                    costs.record(s, "apify", n, cost, f"add:{request_id} profiles")
                budget -= cost
                for h in todo:
                    if p := profiles.get(h.handle):
                        apply_profile(h, p)
                steps["profile_geprueft"] = len(profiles)
        # 2. Instagram posts, as many persons as the action budget allows
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in ids]
            with_ig = [a for a in athletes if usable_handles(a)]
            per_person = (FIRST_LOAD_RESULTS_PER_HANDLE + 1) * settings.apify_cost_per_post_usd
            n = min(len(with_ig), int(budget // per_person)) if per_person > 0 else len(with_ig)
            chosen, later = with_ig[:n], with_ig[n:]
            if chosen:
                _progress(job_id, schritt=f"Instagram laden ({len(chosen)} Personen)")
                # raise the daily Apify cap by exactly this action's budget (agreed: 5 CHF per action)
                cap = costs.spent_today(s, "apify") + budget
                local = settings.model_copy(update={"apify_daily_cost_cap_usd": cap})
                steps["instagram"] = asdict(run_instagram(s, local, chosen))
                budget -= len(chosen) * per_person  # estimate; the ledger has the exact amount
            steps["instagram_spaeter"] = [a.full_name for a in later]
        # 3. link stored news/results/web pages that already name the persons
        _progress(job_id, schritt="Gespeicherte News verknüpfen")
        with session_scope() as s:
            from klugscheisser.ingest.runner import build_context

            steps["verknuepft"] = link_existing_items(s, ids, ctx=build_context(s, settings))
        # 3b. event preparation: one web search per person (slow is fine here, answers never search the web)
        if kind == "event":
            from klugscheisser.adds.event import prep_web_search

            steps["websuche"] = prep_web_search(
                settings, ids, budget, progress=lambda text: _progress(job_id, schritt=text)
            )
        # 4. results + profiles (Wikipedia, college pages)
        for i, extra in enumerate([*EXTRA_SOURCES, *SLOW_SOURCES], 1):
            _progress(job_id, schritt=f"Resultate und Profile ({i}/{len(EXTRA_SOURCES) + len(SLOW_SOURCES)})")
            with session_scope() as s:
                athletes = [s.get(m.Athlete, a) for a in ids]
                try:
                    steps[getattr(extra, "__name__", f"extra{i}")] = extra(s, settings, athletes)
                except Exception as exc:  # one failing source never fails the whole load
                    log.warning("add load %s: %s failed: %s", request_id, extra, exc)
                    steps[getattr(extra, "__name__", f"extra{i}")] = {"fehler": str(exc)}
    except Exception as exc:
        log.exception("add load %s failed", request_id)
        status, error = "failed", str(exc)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.finished_at, job.error = status, datetime.now(UTC), error
        job.result = {**(job.result or {}), **steps, "schritt": "fertig" if status == "done" else "Fehler"}
        req = s.get(m.AddRequest, request_id)
        req.status, req.error = ("done" if status == "done" else "failed"), error


def start_add_load(s: Session, settings: Settings, req: m.AddRequest, submit=None) -> m.Job:
    job = m.Job(kind="add_load", params={"add_request_id": req.id, "athlete_ids": req.athlete_ids})
    s.add(job)
    s.flush()
    req.job_id, req.status = job.id, "loading"
    s.commit()  # the worker thread must see the rows
    from klugscheisser.jobs.refresh import executor

    (submit or executor().submit)(run_add_load, job.id, req.id, settings)
    return job


def confirm(s: Session, settings: Settings, req: m.AddRequest, submit=None) -> m.Job:
    """✅ Ja: create the persons, then load in the background."""
    if req.status != "proposed":
        raise ValueError(f"Anfrage #{req.id} ist nicht offen (Status {req.status}).")
    apply_proposal(s, req)
    return start_add_load(s, settings, req, submit)
