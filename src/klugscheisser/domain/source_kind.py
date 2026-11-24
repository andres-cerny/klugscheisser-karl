"""Decide the source kind of an item (drives the emoji label): deterministic where we know, Jev otherwise."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)
ACCOUNTS_SEED = Path("seeds/accounts.yaml")


def load_accounts(session: Session, path: Path = ACCOUNTS_SEED) -> int:
    data = yaml.safe_load(path.read_text()) or {}
    n = 0
    for platform, kinds in data.items():
        for kind, entries in (kinds or {}).items():
            for handle, name in (entries or {}).items():
                key = handle if platform == "rss" else handle.lower()
                row = session.get(m.Account, (platform, key))
                if row is None:
                    row = m.Account(platform=platform, handle=key, kind=kind)
                    session.add(row)
                    n += 1
                row.kind, row.display_name, row.decided_by, row.confidence = kind, name, "list", 1.0
    session.flush()
    return n


def kind_for(
    session: Session,
    *,
    platform: str,
    source_type: str,
    author: str | None,
    own_handles: set[str],
    feed_kind: str | None = None,
    author_full_name: str | None = None,
    sample_text: str = "",
    jev_client=None,
) -> str:
    """self | official | srf | media | boulevard | fan | brand | result | other"""
    if source_type == "official_result":
        return "result"
    if platform != "instagram":
        if feed_kind:
            return feed_kind
        row = session.get(m.Account, ("rss", author or ""))
        return row.kind if row else ("srf" if source_type == "srf" else "media")
    handle = (author or "").lower()
    if handle in own_handles:
        return "self"
    row = session.get(m.Account, ("instagram", handle))
    if row is not None:
        return row.kind
    if jev_client is None:
        return "other"
    try:
        from klugscheisser.classify.jev import classify_account

        kind, confidence = classify_account(jev_client, handle, author_full_name, sample_text)
    except Exception as exc:  # never block ingest on this
        log.warning("account kind for @%s failed: %s", handle, exc)
        return "other"
    session.add(
        m.Account(
            platform="instagram",
            handle=handle,
            kind=kind,
            display_name=author_full_name,
            decided_by="jev",
            confidence=confidence,
        )
    )
    session.flush()
    return kind
