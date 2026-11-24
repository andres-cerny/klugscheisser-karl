"""v4: add-request lifecycle: propose → ✅ Ja (load) / ❌ Nein / ✏️ Präzisieren (new proposal)."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from klugscheisser.adds.base import Proposal, estimate_load_cost_chf
from klugscheisser.config import Settings
from klugscheisser.db import models as m

# chat messages that ask to add someone: "Füge das schwedische Nationalteam hinzu", "füge Lara Stalder hinzu"
ADD_RE = re.compile(r"^\s*(füge|fuege|bitte füge)\s+(?P<what>.+?)\s+hinzu\s*[.!]?\s*$", re.IGNORECASE)
TEAM_WORDS = re.compile(
    r"\b(team|nationalteam|nationalmannschaft|mannschaft|kader|verein|club|klub|frauen|damen|alle)\b",
    re.IGNORECASE,
)


def parse_add_message(text: str) -> tuple[str, str] | None:
    """('team', text) or ('person', name) when a chat message asks to add someone, else None."""
    match = ADD_RE.match(text or "")
    if not match:
        return None
    what = match.group("what").strip()
    if TEAM_WORDS.search(what):
        return "team", text.strip()
    return "person", re.sub(r"^(die|den|der)\s+", "", what, flags=re.IGNORECASE)


def _resolve(settings: Settings, s: Session, req: m.AddRequest, previous: Proposal | None) -> Proposal:
    from klugscheisser.adds.resolvers import get_person_resolver, get_team_resolver

    inp = req.input or {}
    if req.kind == "team":
        return get_team_resolver(settings).propose(
            s, inp.get("request", ""), refine=req.refine_text, previous=previous
        )
    return get_person_resolver(settings).propose(
        s,
        inp.get("name", ""),
        instagram=inp.get("instagram") or None,
        details=inp.get("details") or "",
        refine=req.refine_text,
        previous=previous,
    )


def _finish(settings: Settings, req: m.AddRequest, proposal: Proposal) -> None:
    if not proposal.cost_estimate_chf and proposal.persons:
        with_ig = sum(1 for p in proposal.persons if p.instagram)
        proposal.cost_estimate_chf = estimate_load_cost_chf(settings, len(proposal.persons), with_ig)
    req.proposal = proposal.to_json()
    req.cost_estimate_chf = proposal.cost_estimate_chf


def propose(s: Session, settings: Settings, kind: str, inp: dict) -> m.AddRequest:
    req = m.AddRequest(kind=kind, input=inp, status="proposed")
    s.add(req)
    s.flush()
    _finish(settings, req, _resolve(settings, s, req, None))
    s.flush()
    return req


def refine(s: Session, settings: Settings, req: m.AddRequest, text: str) -> m.AddRequest:
    """✏️ Präzisieren: the old request is closed, a new one carries the correction."""
    req.status = "refined"
    new = m.AddRequest(
        kind=req.kind, input=req.input, status="proposed", refine_text=text.strip(), parent_id=req.id
    )
    s.add(new)
    s.flush()
    previous = Proposal.from_json(req.proposal) if req.proposal else None
    _finish(settings, new, _resolve(settings, s, new, previous))
    s.flush()
    return new


def reject(s: Session, req: m.AddRequest) -> None:
    if req.status == "proposed":
        req.status = "rejected"
    s.flush()


def over_cap(settings: Settings, req: m.AddRequest) -> bool:
    return (req.cost_estimate_chf or 0.0) > settings.add_cost_cap_chf
