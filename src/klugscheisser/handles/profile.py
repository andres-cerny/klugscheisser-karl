"""Turn a fetched Instagram profile into handle evidence (profile name, bio, badge)."""

from __future__ import annotations

from datetime import UTC, datetime

from klugscheisser.collectors.instagram.base import InstagramProfile
from klugscheisser.db import models as m
from klugscheisser.domain.names import name_variants, normalize
from klugscheisser.domain.sport import sport_verdict
from klugscheisser.handles.service import add_evidence
from klugscheisser.projects import wording

GRAPH_HINT_KEYS = ("graph_ok", "graph_checked_at")  # can Graph Business Discovery read this account?
KEEP_OLD_SOURCES = ("graph", "brightdata")  # profiles without category / badge / business flag


def apply_profile(handle: m.Handle, profile: InstagramProfile) -> None:
    athlete = handle.athlete
    old = handle.profile or {}
    # Graph knows no category / badge; Bright Data never filled category or business flag (test 2026-10-28):
    # keep what Apify found earlier instead of wiping it at the weekly refresh
    keep = profile.source in KEEP_OLD_SOURCES
    business = profile.is_business if profile.is_business is not None else old.get("is_business")
    handle.profile = {
        "full_name": profile.full_name,
        "biography": profile.biography,
        "category": profile.category or (old.get("category") if keep else None),
        "verified_badge": profile.verified_badge or (bool(old.get("verified_badge")) if keep else False),
        "followers": profile.followers,
        "posts_count": profile.posts_count,
        "private": profile.private,
        "external_url": profile.external_url,
        "fetched_at": datetime.now(UTC).isoformat(),
        **({"is_business": business} if business is not None else {}),
        **{k: old[k] for k in GRAPH_HINT_KEYS if k in old},  # set by the Instagram ingest
    }
    url = f"https://www.instagram.com/{handle.handle}/"
    if profile.full_name and normalize(profile.full_name) in name_variants(athlete.full_name) | {
        a.alias_normalized for a in athlete.aliases
    }:
        add_evidence(
            handle, {"kind": "profile_name", "detail": f"Profilname '{profile.full_name}'", "url": url}
        )
    bio = profile.biography or ""
    clubs = [normalize(a.team.name) for a in athlete.affiliations if a.team]
    if sport_verdict(bio).looks_hockey() or any(c and c in normalize(bio) for c in clubs) or "🇨🇭" in bio:
        add_evidence(
            handle, {"kind": "profile_bio", "detail": f"Bio nennt {wording().sport_de}/Team", "url": url}
        )
    if profile.verified_badge:
        add_evidence(handle, {"kind": "ig_verified", "detail": "Instagram-Verifizierungshaken", "url": url})
