"""Backfill of the fact layer: Wikidata for every person with a QID (batched, free), then document extraction
person by person (Swiss first), with progress, cost and a clean stop on the model's daily quota or budget."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.facts.extract import DailyQuotaExceeded, cost_source, extract_facts, select_documents
from klugscheisser.facts.wikidata_import import CHUNK, import_wikidata_facts
from klugscheisser.ingest import costs


def _person_ids(nations: list[str] | None) -> list[int]:
    with session_scope() as s:
        q = select(m.Athlete.id).order_by(m.Athlete.nationality != "SUI", m.Athlete.id)
        if nations:
            q = q.where(m.Athlete.nationality.in_(nations))
        return list(s.scalars(q))


def backfill_wikidata(nations: list[str] | None, echo: Callable[[str], None] = print, wiki=None) -> dict:
    with session_scope() as s:
        q = select(m.Athlete.id).where(m.Athlete.wikidata_qid.is_not(None))
        q = q.order_by(m.Athlete.nationality != "SUI", m.Athlete.id)
        if nations:
            q = q.where(m.Athlete.nationality.in_(nations))
        ids = list(s.scalars(q))
    total = {"persons": 0, "with_data": 0, "facts": 0, "stints": 0, "skipped_other_sport": 0, "errors": []}
    for start in range(0, len(ids), CHUNK):
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in ids[start : start + CHUNK]]
            r = import_wikidata_facts(s, athletes, wiki=wiki)
        for k in ("persons", "with_data", "facts", "stints", "skipped_other_sport"):
            total[k] += r[k]
        total["errors"] += r["errors"]
        echo(
            f"Wikidata {min(start + CHUNK, len(ids))}/{len(ids)} persons: "
            f"+{r['facts']} facts, +{r['stints']} stints"
        )
    return total


def backfill_documents(
    settings: Settings,
    nations: list[str] | None,
    limit: int | None = None,
    budget_usd: float | None = None,
    echo: Callable[[str], None] = print,
    llm=None,
    support=None,
) -> dict:
    budget = settings.llm_daily_cost_cap_usd if budget_usd is None else budget_usd
    total = {
        "persons": 0,
        "docs": 0,
        "facts": 0,
        "stints": 0,
        "dropped": 0,
        "unconfirmed": 0,
        "cost_usd": 0.0,
    }
    ids = _person_ids(nations)
    done = 0
    for aid in ids:
        if limit and done >= limit:
            break
        with session_scope() as s:
            a = s.get(m.Athlete, aid)
            if not select_documents(s, a):
                continue
            if costs.spent_today(s, cost_source(settings)) >= budget:
                echo(f"stopped: daily budget ${budget:.2f} for {cost_source(settings)} used up")
                total["stopped"] = "budget"
                break
            try:
                r = extract_facts(s, settings, a, llm=llm, support=support)
            except DailyQuotaExceeded as exc:
                echo(f"stopped: model daily quota / credit ({exc})")
                total["stopped"] = "quota"
                break
            name = a.full_name
        done += 1
        total["persons"] += 1
        for k in ("docs", "facts", "stints", "dropped", "unconfirmed"):
            total[k] += r[k]
        total["cost_usd"] = round(total["cost_usd"] + r["cost_usd"], 6)
        echo(
            f"{done}{'/' + str(limit) if limit else ''} {name}: {r['docs']} docs, {r['facts']} facts, "
            f"{r['stints']} stints, {r['dropped']} dropped, {r['unconfirmed']} unconfirmed, "
            f"{r['conflicts']} conflicts, ${r['cost_usd']:.4f}"
            + (f" ({r['error']})" if r.get("error") else "")
        )
    echo(f"total ${total['cost_usd']:.4f} for {total['persons']} persons")
    return total
