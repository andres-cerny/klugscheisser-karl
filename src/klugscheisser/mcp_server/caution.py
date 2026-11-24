"""Which items count as "⚠️ Vorsicht" (get_athlete) and "⭐ Zu beachten" (get_event).

The classifier flags many things (a fan's joke, a photographer's own surgery post, a July injury report). For the
MCP overview sections only items pass that are
- about the person: she is the main person of the item, not just tagged/mentioned next to others (`main` False),
- recent: published within CAUTION_WINDOW_DAYS (default 21),
- from an official / media source (her own posts, team, federation, league, SRF, media outlets, news sites);
  fan, brand, gossip and other social-media posts only when a trusted source reports the same topic
  (same category) about her within a few days,
- older injury reports: only the newest one, only when no newer injury report about her exists in the window
  (a newer one replaces it), and labelled with its date,
- injury reports followed by a roster nomination or a game appearance (klugscheisser.person_status) are resolved
  and drop out: "Waser needs surgery (19.09.)" no longer shows after she scored twice on 14.10.,
- a Vorsicht flag needs a stated reason (classify.base.vorsicht_reason), else it is no Vorsicht item.
Wikipedia/profile pages never count (reference, and their date is the last edit, not news).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from klugscheisser.classify.base import is_vorsicht, vorsicht_reason
from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de, local
from klugscheisser.person_status import PersonStatus

SOCIAL_PLATFORMS = {"x", "instagram", "tiktok", "threads", "facebook", "bluesky"}
# source_kind values we trust on their own; "web" (unknown site) only for articles, not social posts
TRUSTED_KINDS = {"self", "official", "srf", "public", "media", "result", "profile"}
CORROBORATION_DAYS = 3
NOT_NEWS = {"profile"}  # Wikipedia / profile pages: reference, not news (their date is the last edit)


def is_trusted(item: m.Item) -> bool:
    kind = item.source_kind or "other"
    if kind in TRUSTED_KINDS:
        return True
    return kind == "web" and item.platform not in SOCIAL_PLATFORMS


def is_about(item: m.Item, athlete_id: int) -> bool:
    """She is the subject: linked to the item and not marked as a side person by Jev."""
    link = next((ia for ia in item.athletes if ia.athlete_id == athlete_id), None)
    return link is not None and link.main is not False


def _when(item: m.Item) -> datetime | None:
    ts = item.published_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _category(item: m.Item) -> str | None:
    return item.classification.category if item.classification else None


def is_caution(item: m.Item) -> bool:
    """⚠️ Vorsicht: flagged by the classifier with a stated reason."""
    return is_vorsicht(item.classification)


def is_injury(item: m.Item) -> bool:
    return _category(item) == "injury" or vorsicht_reason(item.classification) == "Verletzung"


def resolved(item: m.Item, status: PersonStatus | None) -> bool:
    """An injury report about her followed by a nomination or a game (she is back)."""
    ts = _when(item)
    return bool(status and ts is not None and is_injury(item) and status.resolved(local(ts).date()))


def _corroborated(item: m.Item, trusted: list[m.Item]) -> bool:
    ts, cat = _when(item), _category(item)
    if ts is None:
        return False
    span = timedelta(days=CORROBORATION_DAYS)
    return any(_category(t) == cat and (tw := _when(t)) is not None and abs(tw - ts) <= span for t in trusted)


def select(
    items: Iterable[m.Item],
    athlete_id: int,
    keep: Callable[[m.Item], bool],
    window_days: int,
    now: datetime | None = None,
    status: PersonStatus | None = None,
) -> list[tuple[m.Item, str | None]]:
    """(item, note) pairs for one person; note is set for an older injury report shown for lack of news.

    `items` are all recent items linked to her (they also serve as corroboration / newer status), `keep` picks
    the kind of item the section is about (vorsicht flag, newsworthiness …), `status` her current status
    (injury reports followed by a nomination / game are left out)."""
    now = now or datetime.now(UTC)
    start = now - timedelta(days=window_days)
    mine = [
        i
        for i in items
        if i.classification and i.source_type not in NOT_NEWS and is_about(i, athlete_id) and _when(i)
    ]
    trusted = [i for i in mine if is_trusted(i)]
    recent = [i for i in mine if _when(i) >= start and keep(i)]
    backing = [t for t in trusted if keep(t)]  # corroboration: a trusted item of the same kind
    out: list[tuple[m.Item, str | None]] = [
        (i, None) for i in recent if (is_trusted(i) or _corroborated(i, backing)) and not resolved(i, status)
    ]
    shown_injury = any(_category(i) == "injury" for i, _ in out)
    if not shown_injury:
        old = sorted(
            (i for i in trusted if _when(i) < start and _category(i) == "injury" and keep(i)),
            key=_when,
            reverse=True,
        )
        if old:
            newest = old[0]
            newer_status = any(
                _when(i) > _when(newest) and _category(i) == "injury" and i.id != newest.id for i in trusted
            )
            if not newer_status and not resolved(newest, status):
                out.append(
                    (
                        newest,
                        f"älterer Bericht vom {date_de(newest.published_at, with_year=True)}; "
                        "seither kein neuer Bericht zur Verletzung"
                        + (", kein Aufgebot und kein Spiel" if status is not None else "")
                        + " in unseren Quellen",
                    )
                )
    return out
