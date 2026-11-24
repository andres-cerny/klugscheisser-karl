"""v6: answer cache. Same question + same scope → instant answer, until newer documents exist for the scope
(or after MAX_AGE). Follow-ups in a chat are never cached (they depend on the conversation)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.domain.names import normalize
from klugscheisser.interfaces import AnswerResult

MAX_AGE = timedelta(hours=24)


def key(
    question: str,
    person_ids,
    team_id,
    event_id,
    since,
    web,
    briefing_id=None,
    style: str = "liste",
    version: str | None = None,
) -> str:
    """A new briefing version (v7) means new answers. v12: the answer style and the prosa prompt version are
    part of the key, so a cached list answer is never shown as prosa (and a new prompt means new answers).
    "liste" keys stay as before (cached list answers remain valid)."""
    raw = json.dumps(
        [normalize(question), sorted(person_ids or []), team_id, event_id, str(since), bool(web)]
        + ([briefing_id] if briefing_id else [])
        + ([style, version] if style != "liste" else []),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _newest_document(s, person_ids: list[int]) -> datetime | None:
    q = select(func.max(m.Item.scraped_at))
    if person_ids:
        q = q.join(m.ItemAthlete).where(m.ItemAthlete.athlete_id.in_(person_ids))
    return s.scalar(q)


def get(cache_key: str) -> AnswerResult | None:
    from klugscheisser import calllog
    from klugscheisser.chat.service import result_from_json

    with session_scope() as s:
        row = s.get(m.AnswerCache, cache_key)
        if row is None:
            calllog.note("cache", "miss")
            return None
        newest = _newest_document(s, row.person_ids or [])
        if datetime.now(UTC) - row.created_at > MAX_AGE or (newest and newest > row.created_at):
            s.delete(row)
            calllog.note("cache", "stale")
            return None
        calllog.note("cache", "hit")
        res = result_from_json(row.result)
        res.aus_cache = row.created_at.isoformat()
        return res


def put(cache_key: str, question: str, result: AnswerResult) -> None:
    with session_scope() as s:
        data = asdict(result)
        data["aus_cache"] = None
        row = s.get(m.AnswerCache, cache_key) or m.AnswerCache(key=cache_key)
        row.question, row.person_ids, row.result, row.created_at = (
            question,
            result.person_ids,
            data,
            datetime.now(UTC),
        )
        s.merge(row)
