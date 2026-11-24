"""Wikidata extras for the free profile sources (2026-10-25): weight (P2067, via the normal Wikidata fact import),
the Commons photo (P18) in one batch instead of lazily per page view.

Photos: only the link, author and licence are stored (fact "photo", source "wikimedia", `web/photo.py`); the image
itself is shown from Commons with its attribution. A person without a P18 statement is asked again after 30 days.
"""

from __future__ import annotations

import time
from collections import Counter

from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.web import photo


def photos(
    s: Session, athletes: list[m.Athlete], fetch=None, pause_s: float = 1.0, sleep=time.sleep
) -> Counter:
    """Store the Commons photo of every person with a QID that has none yet (2 API calls per person)."""
    counts: Counter = Counter()
    for a in athletes:
        if not a.wikidata_qid:
            counts["no_qid"] += 1
            continue
        if photo.stored_photo(s, a.id) is not None:
            counts["had_photo"] += 1
            continue
        if not photo.photo_due(s, a):
            counts["recent_miss"] += 1
            continue
        meta = photo.ensure_photo(s, a, fetch=fetch)
        counts["photo_added" if meta else "no_photo"] += 1
        sleep(pause_s)
    return counts
