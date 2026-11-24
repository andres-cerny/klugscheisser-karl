"""Handle candidates, evidence and status.

Status ladder: candidate -> wahrscheinlich -> verified (or rejected).
- verified: strong evidence (official team/federation link, manual admin check) or several independent
  signals that together reach VERIFIED_AT.
- wahrscheinlich: usable but shown with its reason; reaches LIKELY_AT.
Only `verified` and `wahrscheinlich` handles are ever scraped or returned to users.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import name_in_handle

# independent evidence -> weight; combined as 1 - prod(1 - w)
EVIDENCE_WEIGHTS = {
    "manual": 1.0,
    "official_link": 0.9,  # linked from team / federation / league site
    "wikidata": 0.5,
    "fabi": 0.5,  # entered by Fabi when adding a person: usable as "wahrscheinlich", never verified alone
    "team_follows": 0.45,  # team account follows this account
    "team_tags": 0.35,  # team account tags this account in posts
    "profile_bio": 0.5,  # bio mentions team, hockey, national team
    "profile_name": 0.3,  # profile full name matches athlete
    "ig_verified": 0.3,  # Instagram blue badge (public figure; not proof of identity by itself)
    "name_in_handle": 0.2,
    "search_hit": 0.1,
    "jev_profile": 0.5,  # v8: Jev read profile + captions: the person's own account
    "eliteprospects": 0.4,  # v11: linked on her Elite Prospects profile (curated, but not alone "wahrscheinlich")
}
STRONG = {"manual", "official_link"}
VERIFIED_AT = 0.8
LIKELY_AT = 0.5

STATUS_LABEL_DE = {"verified": "verifiziert", "wahrscheinlich": "wahrscheinlich"}


def clean_handle(handle: str) -> str:
    h = handle.strip().lstrip("@").rstrip("/")
    if "instagram.com/" in h:
        h = h.split("instagram.com/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return h.lower()


def score(evidence: list[dict]) -> float:
    remaining = 1.0
    for kind in {e["kind"] for e in evidence}:
        remaining *= 1 - EVIDENCE_WEIGHTS.get(kind, 0.0)
    return round(1 - remaining, 3)


def recompute(handle: m.Handle) -> None:
    if handle.status == "rejected":
        return
    handle.confidence = score(handle.evidence)
    kinds = {e["kind"] for e in handle.evidence}
    if kinds & STRONG or handle.confidence >= VERIFIED_AT:
        new = "verified"
    elif handle.confidence >= LIKELY_AT:
        new = "wahrscheinlich"
    else:
        new = "candidate"
    if new == "verified" and handle.status != "verified":
        handle.verified_at = datetime.now(UTC)
        handle.verified_by = "manual" if "manual" in kinds else "auto:" + ",".join(sorted(kinds))
    handle.status = new


def add_handle_candidate(
    session: Session, athlete: m.Athlete, platform: str, handle: str, evidence: dict
) -> m.Handle:
    name = clean_handle(handle)
    row = session.scalar(
        select(m.Handle).where(
            m.Handle.athlete_id == athlete.id, m.Handle.platform == platform, m.Handle.handle == name
        )
    )
    if row is None:
        row = m.Handle(athlete_id=athlete.id, platform=platform, handle=name, evidence=[])
        session.add(row)
    add_evidence(row, evidence)
    if name_in_handle(athlete.full_name, name) >= 0.5:
        add_evidence(row, {"kind": "name_in_handle", "detail": "Name im Handle"})
    session.flush()
    return row


def add_evidence(handle: m.Handle, evidence: dict) -> None:
    key = (evidence["kind"], evidence.get("detail"))
    if key not in {(e["kind"], e.get("detail")) for e in handle.evidence}:
        handle.evidence = [*handle.evidence, evidence]  # reassign so JSON change is tracked
    recompute(handle)


def verify(handle: m.Handle, by: str) -> None:
    handle.status = "candidate"
    add_evidence(handle, {"kind": "manual", "detail": f"geprüft von {by}"})
    handle.verified_by = by


def reject(handle: m.Handle, by: str, reason: str | None = None) -> None:
    handle.status = "rejected"
    handle.evidence = [*handle.evidence, {"kind": "rejected", "detail": reason or "", "by": by}]


def flag_unavailable(handle: m.Handle, detail: str) -> bool:
    """The platform says this profile is not available (renamed or deleted): evidence "ig_unavailable" (weight
    0) and back to "candidate", so it is neither scraped nor shown until someone checks it (`klugscheisser
    handles list --status candidate`, then `handles verify <id>` or `handles reject <id>`), like a Jev doubt.
    True when the handle was usable before (newly flagged)."""
    was_usable = handle.usable
    handle.evidence = [
        *handle.evidence,
        {"kind": "ig_unavailable", "detail": detail, "at": datetime.now(UTC).date().isoformat()},
    ]
    if handle.status != "rejected":
        handle.status = "candidate"
    return was_usable


def reason_de(handle: m.Handle) -> str:
    labels = {
        "manual": "manuell geprüft",
        "official_link": "verlinkt auf offizieller Seite",
        "wikidata": "in Wikidata eingetragen",
        "fabi": "von Fabi angegeben",
        "team_follows": "Team-Account folgt",
        "team_tags": "vom Team-Account markiert",
        "profile_bio": "Bio nennt Team/Eishockey",
        "profile_name": "Profilname stimmt",
        "ig_verified": "Instagram-verifiziert",
        "name_in_handle": "Name im Handle",
        "search_hit": "Suchtreffer",
        "jev_profile": "Jev: eigener Account",
        "eliteprospects": "auf Elite Prospects verlinkt",
        "ig_unavailable": "Instagram: Profil nicht verfügbar, bitte prüfen",
    }
    kinds = []
    for e in handle.evidence:
        label = labels.get(e["kind"])
        if label and label not in kinds:
            kinds.append(label)
    return ", ".join(kinds)


def usable_handles(athlete: m.Athlete, platform: str = "instagram") -> list[m.Handle]:
    hs = [h for h in athlete.handles if h.platform == platform and h.usable]
    return sorted(hs, key=lambda h: (h.status != "verified", -h.confidence))
