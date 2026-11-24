"""v6 step 6: 1–3 fun facts per person for a moderator: interesting or relatable for viewers, an interview
hook to connect with the player, and not found everywhere.

Generated once (backfill) and on request ("🔄 neu erstellen"), never on every ingest. Stored as Fact rows
(kind "fun_fact") with source link + verbatim quote; the generation date is a CacheEntry ("funfacts").
The sentences go through the same checks as answers: quote verbatim in the document, Jev support, sensitive
topics (relationships, health) only from the person herself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.llm import text as llm_text
from klugscheisser.projects import wording
from klugscheisser.sports import current_spec

log = logging.getLogger(__name__)

MAX_DOCS = 12
MAX_FACTS = 3
# v8: tier 1 gets 5 fun facts (at least one from social media when the posts hold one), tier 2 and 3 get 3
FACTS_BY_TIER = {1: 5, 2: 3, 3: 3}
DOCS_BY_TIER = {1: 20, 2: 14, 3: MAX_DOCS}
SOCIAL_DOCS = 10  # tier 1: this many own/tagged social posts are always among the documents
EXTRA_CANDIDATES = 2  # ask for more than needed: the checks (verbatim quote, Jev support) drop some
SOCIAL_PLATFORMS = ("instagram", "facebook", "x")
MARK = "funfacts"
# documents that tend to hold personal stories come first
PERSONAL_TOPICS = ("herkunft", "familie_sport", "engagement", "studium", "beruf", "meilenstein", "comeback")


def generated_at(s: Session, athlete_id: int) -> datetime | None:
    row = s.get(m.CacheEntry, (MARK, f"athlete:{athlete_id}"))
    return row.fetched_at if row else None


def fun_facts(s: Session, athlete_id: int) -> list[m.Fact]:
    return list(
        s.scalars(
            select(m.Fact)
            .where(m.Fact.athlete_id == athlete_id, m.Fact.kind == "fun_fact")
            .order_by(m.Fact.id)
        )
    )


def _docs(s: Session, athlete: m.Athlete, limit: int = MAX_DOCS, social: int = 0):
    from klugscheisser.answer.engine import Doc, _prefix, bio_docs, item_doc

    items = list(
        s.scalars(
            select(m.Item)
            .join(m.ItemAthlete)
            .outerjoin(m.Classification)
            .where(
                m.ItemAthlete.athlete_id == athlete.id,
                m.Item.level == "full",
                (m.Classification.id.is_(None)) | (m.Classification.relevant.is_(True)),
            )
        )
    )

    def score(i: m.Item) -> tuple:
        topics = (i.classification.topics or {}) if i.classification else {}
        personal = max((topics.get(t, 0) for t in PERSONAL_TOPICS), default=0)
        own = i.source_type in ("athlete_post", "interview")
        profile = i.platform in ("wikipedia", "web")
        return (personal, own, profile, i.published_at or datetime.min.replace(tzinfo=UTC))

    items.sort(key=score, reverse=True)
    chosen = items[:limit]
    if social:  # v8: make sure social posts are there to pick from (Wikipedia must not crowd them out)
        # posts with something to say first: longer captions or an image/video description
        cand = [i for i in items if i.platform in SOCIAL_PLATFORMS and i not in chosen]
        cand.sort(key=lambda i: len(i.text or "") + len(i.image_description or ""), reverse=True)
        posts = cand[:social]
        chosen = posts + chosen[: max(limit - len(posts), 0)]
    docs = [item_doc(i) for i in chosen] + bio_docs([athlete])
    # stored facts (Wikidata etc.) as one quotable document with its own link
    facts = [
        f
        for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == athlete.id, m.Fact.source == "wikidata"))
        if f.kind in ("birth_place", "languages", "nickname", "shoots", "height_cm", "award", "draft")
    ]
    if facts and athlete.wikidata_qid:
        text = "; ".join(f"{f.kind}: {f.value_text}" for f in facts)
        label = f"[Wikidata {athlete.wikidata_qid}]"
        docs.append(
            Doc(
                key="",
                text=f"{athlete.full_name} – {text}.",
                meta=f"{label} · Stammdaten · über: {athlete.full_name}",
                url=f"https://www.wikidata.org/wiki/{athlete.wikidata_qid}",
                quelle=label,
                zeile_prefix=_prefix("profile", None),
            )
        )
    return docs


def _is_social(s: Session, st) -> bool:
    item = s.get(m.Item, st.item_id) if st.item_id else None
    return bool(item and item.platform in SOCIAL_PLATFORMS) or "instagram.com" in (st.url or "")


def generate(s: Session, settings: Settings, athlete: m.Athlete, engine=None) -> list[m.Fact]:
    """(Re)create the fun facts of one person. Returns the stored rows (possibly empty)."""
    from klugscheisser.answer.engine import GeminiAnswerEngine

    tier = athlete.tier_manual or athlete.tier or 3
    n_facts = FACTS_BY_TIER.get(tier, MAX_FACTS)
    docs = _docs(s, athlete, DOCS_BY_TIER.get(tier, MAX_DOCS), SOCIAL_DOCS if tier == 1 else 0)
    s.execute(  # 2026-11-24: curated seed fun facts (seed/floorball.py, source "seed") stay
        delete(m.Fact).where(
            m.Fact.athlete_id == athlete.id, m.Fact.kind == "fun_fact", m.Fact.source != "seed"
        )
    )
    rows: list[m.Fact] = []
    if docs:
        engine = engine or GeminiAnswerEngine(settings)
        social_rule = (
            " Mindestens ein Fun Fact soll aus ihren Social-Media-Posts (Instagram, Facebook, X: Bildtext, "
            "Bildbeschreibung, Gesagtes im Video) stammen, wenn diese etwas Passendes hergeben – sonst keiner "
            "daraus, nichts erfinden."
            if tier == 1
            else ""
        )
        w, men = wording(), current_spec().men
        to_player = f"zum {w.player}" if men else f"zur {w.player}"
        question = (
            f"Nenne {n_facts} bis {n_facts + EXTRA_CANDIDATES} Fun Facts über {athlete.full_name} für eine "
            "Sportmoderatorin (weniger nur, wenn die Dokumente nicht mehr hergeben). Ein guter "
            "Fun Fact ist etwas, das sie in der Übertragung sagen kann und das die Zuschauer interessant finden "
            "oder mit dem sie sich identifizieren können; das ihr im Interview einen Einstieg gibt, um eine "
            f"Verbindung {to_player} herzustellen; und das man nicht überall findet. Beispiele (nur Beispiele, "
            f"keine feste Liste): andere Sportarten, die {w.pronoun_de} betrieben hat, Studium oder Beruf neben dem Sport, "
            "Herkunft und Geschichte, Familie mit Sportbezug, Hobbys, Haustiere, Rituale, Kurioses, Anekdoten, "
            "aber auch der Sport selbst: eine wirklich bemerkenswerte Statistik, ein Rekord, eine besondere "
            f"Geschichte aus {'seiner' if men else 'ihrer'} Karriere. Nichts Banales wie Position, aktueller Verein oder gewöhnliche "
            "Zahlen. Jede Aussage kurz und konkret." + social_rule
        )
        ok, _unsure, _gaps, _dropped, usage = engine._write(question, docs, [athlete.full_name])
        engine._record_cost(s, usage, note="funfacts")
        if tier == 1 and len(ok) > n_facts:  # keep one from social media when the cut would drop it
            social = [st for st in ok if _is_social(s, st)]
            if social and not any(_is_social(s, st) for st in ok[:n_facts]):
                ok = [*ok[: n_facts - 1], social[0]]
        for rank, st in enumerate(ok[:n_facts], 1):
            row = m.Fact(
                athlete_id=athlete.id,
                kind="fun_fact",
                value={
                    "text": st.aussage,
                    "rank": rank,
                    "zeile_prefix": st.zeile_prefix,
                    "quelle": st.quelle,
                },
                value_text=st.aussage,
                source="funfacts",
                source_item_id=st.item_id,
                source_url=st.url,
                quote=st.zitat,
                confidence=st.support if st.support is not None else 1.0,
                status="confirmed",
                extractor=llm_text.model(settings, "answer"),
            )
            s.add(row)
            rows.append(row)
    entry = s.get(m.CacheEntry, (MARK, f"athlete:{athlete.id}")) or m.CacheEntry(
        source=MARK, target=f"athlete:{athlete.id}"
    )
    entry.fetched_at = datetime.now(UTC)
    s.merge(entry)
    s.flush()
    return rows


def backfill(
    settings: Settings, ids: list[int], force: bool = False, budget_usd: float = 5.0, echo=print
) -> dict:
    """All given persons once (skips those already done unless force); stops at the budget."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.ingest import costs

    done = skipped = facts = 0
    source = llm_text.cost_source(settings, "answer")
    with session_scope() as s:
        start_spend = costs.spent_today(s, source)
    for n, aid in enumerate(ids, 1):
        with session_scope() as s:
            if not force and generated_at(s, aid) is not None:
                skipped += 1
                continue
            if costs.spent_today(s, source) - start_spend >= budget_usd:
                echo(f"Budget von ${budget_usd:.2f} erreicht – Stopp nach {done} Personen.")
                break
            try:
                facts += len(generate(s, settings, s.get(m.Athlete, aid)))
                done += 1
            except Exception as exc:
                log.warning("fun facts for %s failed: %s", aid, str(exc)[:160])
                if "PerDay" in str(exc) or "credit balance" in str(exc).lower():
                    echo("Tageslimit / Guthaben des Modells erreicht – Stopp.")
                    break
        if n % 25 == 0:
            echo(f"{n}/{len(ids)} Personen, {done} neu, {facts} Fun Facts")
    return {"neu": done, "uebersprungen": skipped, "fun_facts": facts}
