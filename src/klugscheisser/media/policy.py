"""v8: which media of a post Gemini looks at (images/reels cost money; the caption is always kept).

- Only after Jev classified the post as relevant (or Vorsicht): "🌊☀️" or "yolo" posts are never analysed.
- Fan accounts: caption only.
- Persons of tier 2: image posts only; reels caption only.
- Persons of tier 1: image posts; a reel only when it looks like an interview where she talks (Jev reads the
  caption and the posting account: "Interview", "Q&A", "erzählt", podcast clip, media/club/sponsor account).
  Training clips and the like: caption only.
- Persons of tier 3 (and posts without a linked person): as before (images and reels).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.interfaces import MediaRef

log = logging.getLogger(__name__)
INTERVIEW_YES = 0.6
# cheap pre-filter: a reel whose caption has none of these words is not an interview (no Jev call needed)
INTERVIEW_HINT = re.compile(
    r"interview|q ?& ?a|fragen|frage|erzähl|spricht|talks?|podcast|episode|folge|gespräch|rozhovor|intervju|"
    r"haastattelu|entrevue|intervista|behind the scenes|get to know|kennenlernen|story|portr[aä]it|"
    r"🎙|🎤|🎧|\?",
    re.I,
)


def person_tier(s: Session, item: m.Item) -> int:
    """Best (lowest) tier among the persons linked to the item; 3 when none."""
    tiers = [
        (ia.athlete.tier_manual or ia.athlete.tier or 3) for ia in item.athletes if ia.athlete is not None
    ]
    return min(tiers) if tiers else 3


def looks_like_interview(jev, item: m.Item) -> bool:
    text = f"{item.title or ''}\n{item.text or ''}"
    if not INTERVIEW_HINT.search(text):
        return False
    if jev is None:
        return True  # hint words present, no Jev: analyse (rare; Jev is normally on)
    from klugscheisser.classify.jev import noul_probability

    question = {
        "interview": {
            "type": "noul",
            "instructions": "Ist dieses Video ein Interview oder Gespräch, in dem die Person selbst über sich, "
            "ihre Karriere oder ihr Leben spricht?",
            "criteria": {
                "true": "Interview, Q&A, Podcast-Ausschnitt, Porträt, sie erzählt etwas über sich",
                "false": "Training, Spielszenen, Musikvideo, Werbung, Feier, Alltag ohne Gespräch",
            },
        }
    }
    state = f"Account: {item.author or item.source_name or '–'} ({item.source_kind or '–'})\n{text[:3000]}"
    try:
        return noul_probability(jev.ask(state, question)["interview"]) >= INTERVIEW_YES
    except Exception as exc:
        log.warning("interview check failed: %s", exc)
        return False


def allowed_media(s: Session, item: m.Item, media: list[MediaRef], jev=None) -> list[MediaRef]:
    """The media of `item` that go to the analysis queue (possibly none)."""
    if not media:
        return []
    c = item.classification
    if c is not None and not (c.relevant or c.vorsicht):
        return []
    if item.source_kind == "fan":
        return []
    tier = person_tier(s, item)
    if tier == 3:
        return media
    videos = [x for x in media if x.kind == "video"]
    if not videos:
        return media  # image posts: tier 1 and 2
    if tier == 2:
        return []  # reels: caption only for tier 2
    return media if looks_like_interview(jev, item) else []  # tier 1: only interview reels
