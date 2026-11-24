"""Profile ingest (v3 P2): raw documents about a person (Wikipedia, college bio pages) -> items linked to her.

Per athlete at most every ``max_age_days`` (TTL in cache_entries: source "profiles", target "athlete:<id>").
Each ProfileDoc becomes an item (source_type "profile", verbatim text, link, date, source) linked to the athlete
with match_kind "profile". Re-running is idempotent: same URL + same text -> no change; changed text -> new
version (store_item keeps the previous texts).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.matcher import Link
from klugscheisser.handles.service import usable_handles
from klugscheisser.ingest.store import IngestContext, ItemDraft, content_hash, store_item
from klugscheisser.interfaces import ProfileCollector, ProfileDoc

log = logging.getLogger(__name__)

CACHE_SOURCE = "profiles"


@dataclass
class ProfilesReport:
    athletes: int = 0
    fresh: int = 0  # skipped: profiles fetched within max_age_days
    docs: int = 0
    items_new: int = 0
    items_changed: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # e.g. player not on the college roster
    errors: list[str] = field(default_factory=list)


def athlete_hints(a: m.Athlete) -> dict:
    teams: list[str] = []
    for aff in a.affiliations:
        for name in (aff.team.name, aff.team.short_name):
            if name and name not in teams:
                teams.append(name)
    return {
        "wikidata_qid": a.wikidata_qid,
        "teams": teams,
        "handles": [h.handle for h in usable_handles(a)],
        "nationality": a.nationality,
    }


def _draft(doc: ProfileDoc, collector: str) -> ItemDraft:
    return ItemDraft(
        source_type=doc.source_type,
        platform=doc.platform,
        url=doc.url,
        title=doc.title,
        text=doc.text,
        source_name=doc.source_name,
        author=doc.source_name,
        source_kind=doc.source_kind,
        published_at=doc.published_at,
        raw={"collector": collector, "language": doc.language},
    )


def ingest_profiles(
    ctx: IngestContext,
    athletes: list[m.Athlete],
    collectors: list[ProfileCollector],
    max_age_days: int = 14,
) -> ProfilesReport:
    from klugscheisser.jobs.refresh import is_fresh, touch

    s = ctx.session
    report = ProfilesReport()
    for a in athletes:
        target = f"athlete:{a.id}"
        if is_fresh(s, CACHE_SOURCE, target, max_age_days * 86400):
            report.fresh += 1
            continue
        report.athletes += 1
        sport = s.get(m.Sport, a.sport_id).key
        hints = athlete_hints(a)
        failed = False
        for collector in collectors:
            notes_before = len(getattr(collector, "notes", []))
            try:
                docs = collector.collect(a.full_name, sport, hints)
            except Exception as exc:  # one source failing must not stop the others
                log.warning("profiles %s for %s failed: %s", collector.name, a.full_name, exc)
                report.errors.append(f"{collector.name} {a.full_name}: {exc}")
                failed = True
                continue
            report.notes.extend(getattr(collector, "notes", [])[notes_before:])
            for doc in docs:
                r = store_item(ctx, _draft(doc, collector.name), [Link(a.id, "profile", 1.0)])
                report.docs += 1
                report.items_new += r.created
                report.items_changed += r.changed and not r.created
                report.by_source[doc.source_name] = report.by_source.get(doc.source_name, 0) + 1
        if not failed:  # retry next time when a source was down
            touch(s, CACHE_SOURCE, target)
        extract_facts_quietly(ctx, a)
    return report


def extract_facts_quietly(ctx: IngestContext, athlete: m.Athlete) -> None:
    """v6: facts (Wikidata + verified extraction from her documents) right after her profiles are stored.
    Documents already extracted are skipped (cache marks), so this never pays twice."""
    st = ctx.settings
    from klugscheisser.llm import text as llm_text

    if not (llm_text.available(st) and st.features["jev"]) or ctx.skip_facts:
        return  # backfills without vectors run the fact backfill separately
    try:
        from klugscheisser.facts.extract import extract_facts
        from klugscheisser.facts.wikidata_import import import_wikidata_facts

        if athlete.wikidata_qid:
            import_wikidata_facts(ctx.session, [athlete])
        extract_facts(ctx.session, st, athlete)
    except Exception as exc:  # facts are an extra; never break the profile ingest
        log.warning("fact extraction for %s failed: %s", athlete.full_name, str(exc)[:160])


def profiles_for_update(session: Session, settings: Settings, athletes: list[m.Athlete]) -> dict:
    """Hook for the update job (jobs/freshness.EXTRA_SOURCES): profiles of exactly these athletes, with TTL."""
    if not settings.profiles_enabled:
        return {"status": "aus"}
    from klugscheisser.collectors.profiles import enabled_profile_collectors
    from klugscheisser.ingest.runner import build_context

    report = ingest_profiles(
        build_context(session, settings),
        athletes,
        enabled_profile_collectors(settings),
        max_age_days=settings.profiles_max_age_days,
    )
    return asdict(report)


def wikipedia_only(settings: Settings) -> list[ProfileCollector]:
    return [c for c in enabled_profile_collectors_for(settings) if c.name == "wikipedia"]


def enabled_profile_collectors_for(settings: Settings) -> list[ProfileCollector]:
    from klugscheisser.collectors.profiles import enabled_profile_collectors

    return enabled_profile_collectors(settings)


def missing_profiles(session: Session, athletes: list[m.Athlete]) -> list[m.Athlete]:
    """Persons for whom no Wikipedia lookup was ever done (no cache mark)."""
    from klugscheisser.jobs.refresh import is_fresh

    return [a for a in athletes if not is_fresh(session, CACHE_SOURCE, f"athlete:{a.id}", 10 * 365 * 86400)]


def ensure_profiles(session: Session, settings: Settings, athletes: list[m.Athlete], limit: int = 5) -> int:
    """Before answering: fetch Wikipedia (home language first) for persons never looked up — independent of
    Instagram. Free, ~1-3 s per person; only for small scopes.

    `PROFILES_IN_BACKGROUND` (default on, 2026-10-25): the lookup runs in the refresh thread pool and the request
    goes on at once (the answer uses what is stored; the next question has the profile). A person already being
    looked up is not started twice (Perplexity fans out the same person in parallel). Returns the number of
    persons looked up (or started)."""
    if not settings.profiles_enabled:
        return 0
    todo = missing_profiles(session, athletes)[:limit] if len(athletes) <= limit else []
    if not todo:
        return 0
    if settings.profiles_in_background:
        return _start_background_lookup(settings, [a.id for a in todo])
    _lookup(session, settings, todo)
    return len(todo)


def _lookup(session: Session, settings: Settings, todo: list[m.Athlete]) -> None:
    from klugscheisser.ingest.runner import build_context

    try:
        ingest_profiles(build_context(session, settings), todo, wikipedia_only(settings), max_age_days=3650)
        session.commit()
    except Exception as exc:
        session.rollback()
        log.warning("profile lookup before answer failed: %s", exc)


_in_flight: set[tuple[str, int]] = set()  # (project key, athlete id) being looked up in the background
_in_flight_lock = threading.Lock()


def _project_key() -> str:
    from klugscheisser import projects

    try:
        return projects.current().key
    except Exception:  # no project registry (tests, one-project setups)
        return "-"


def _start_background_lookup(settings: Settings, ids: list[int], submit=None) -> int:
    key = _project_key()
    with _in_flight_lock:
        new = [i for i in ids if (key, i) not in _in_flight]
        _in_flight.update((key, i) for i in new)
    if not new:
        return 0
    from klugscheisser.jobs.refresh import executor

    try:
        (submit or executor().submit)(_background_lookup, key, new, settings)
    except Exception as exc:  # pool shut down (server stopping): forget the marks, never break the answer
        with _in_flight_lock:
            _in_flight.difference_update((key, i) for i in new)
        log.warning("profile lookup not started: %s", exc)
        return 0
    return len(new)


def _background_lookup(key: str, ids: list[int], settings: Settings) -> None:
    from klugscheisser.db.session import session_scope

    try:
        with session_scope() as s:
            todo = [a for a in (s.get(m.Athlete, i) for i in ids) if a is not None]
            _lookup(s, settings, missing_profiles(s, todo))
    except Exception:
        log.exception("background profile lookup failed")
    finally:
        with _in_flight_lock:
            _in_flight.difference_update((key, i) for i in ids)


# --- full Wikipedia texts (2026-10-25) ------------------------------------------------------------------------

CUT_MARK = " […]"  # collectors/profiles/text.cap_text marks a cut text with it


@dataclass
class FullTextReport:
    candidates: int = 0  # stored Wikipedia profiles whose text was cut (ends with " […]")
    longer: int = 0  # the whole article is longer than the stored text: updated (or would be, dry run)
    unchanged: int = 0
    missing: int = 0  # article gone / now a disambiguation page / no text
    errors: list[str] = field(default_factory=list)
    chars_before: int = 0
    chars_after: int = 0  # over the `longer` documents
    examples: list[tuple[int, str, int, int]] = field(default_factory=list)  # (item id, title, before, after)


def _wiki_lang(item: m.Item) -> str | None:
    lang = (item.raw or {}).get("language")
    if not lang and (mt := re.search(r"\(([a-z]{2,3})\)", item.source_name or "")):
        lang = mt.group(1)
    return lang


def cut_wikipedia_profiles(session: Session, limit: int = 0) -> list[m.Item]:
    """Stored Wikipedia profiles whose text was cut at the old 6,000-character limit (end with " […]")."""
    q = (
        select(m.Item)
        .where(
            m.Item.source_type == "profile",
            m.Item.platform == "wikipedia",
            m.Item.text.like(f"%{CUT_MARK}"),
        )
        .order_by(m.Item.id)
    )
    if limit:
        q = q.limit(limit)
    return list(session.scalars(q))


def backfill_full_wikipedia(
    session: Session, collector, dry_run: bool = False, limit: int = 0, progress=None
) -> FullTextReport:
    """Re-fetch the whole article for every stored Wikipedia profile that was cut, and store the full text in place
    (same item id, so links, facts and the classification stay). Free Wikipedia API, polite client.

    content_hash changes with the text, so `klugscheisser reembed-chunks` (text hash per document) re-splits and
    re-embeds exactly these documents. The document vector (embeddings table) is kept: the document embedder reads
    only the first 8,000 characters (GeminiEmbedder), and the first ~6,000 are the same text as before. Facts are
    not re-extracted (their marks are per item; extraction reads the first 3,000 characters anyway)."""
    from datetime import UTC, datetime

    report = FullTextReport()
    for n, item in enumerate(cut_wikipedia_profiles(session, limit), 1):
        report.candidates += 1
        lang, old = _wiki_lang(item), item.text or ""
        try:
            doc = collector.document(lang, item.title) if lang and item.title else None
        except Exception as exc:  # one page failing must not stop the others
            report.errors.append(f"{item.id} {item.title}: {str(exc)[:120]}")
            continue
        if doc is None or not doc.text:
            report.missing += 1
            continue
        if len(doc.text) <= len(old.removesuffix(CUT_MARK)):
            report.unchanged += 1
            continue
        report.longer += 1
        report.chars_before += len(old)
        report.chars_after += len(doc.text)
        if len(report.examples) < 10:
            report.examples.append((item.id, item.title or "", len(old), len(doc.text)))
        if not dry_run:
            digest = content_hash(doc.text, doc.title)
            # (url, content_hash) is unique: an older copy with this very text would clash
            clash = session.scalar(
                select(m.Item.id).where(
                    m.Item.url == item.url, m.Item.content_hash == digest, m.Item.id != item.id
                )
            )
            if clash is not None:
                report.errors.append(f"{item.id} {item.title}: same text already stored as item {clash}")
                continue
            item.text = doc.text
            item.title = doc.title or item.title
            item.content_hash = digest
            if doc.published_at:
                item.published_at = doc.published_at
            item.raw = {**(item.raw or {}), "full_text_at": datetime.now(UTC).isoformat(timespec="seconds")}
            session.flush()
        if progress and n % 20 == 0:
            progress(n, report)
    return report
