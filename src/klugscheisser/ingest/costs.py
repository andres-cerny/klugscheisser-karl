"""Daily cost caps for paid sources (Apify, LLM). Spend is recorded in cost_ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m


class BudgetExceeded(Exception):
    pass


def spent_today(session: Session, source: str) -> float:
    since = datetime.now(UTC) - timedelta(hours=24)
    return float(
        session.scalar(
            select(func.coalesce(func.sum(m.CostLedger.cost_usd), 0.0)).where(
                m.CostLedger.source == source, m.CostLedger.created_at >= since
            )
        )
    )


def remaining(session: Session, source: str, cap_usd: float) -> float:
    return max(0.0, cap_usd - spent_today(session, source))


def ensure_budget(session: Session, source: str, estimate_usd: float, cap_usd: float) -> float:
    """Raise if the estimate would exceed the rolling 24h cap; return what is left."""
    left = remaining(session, source, cap_usd)
    if estimate_usd > left:
        raise BudgetExceeded(
            f"{source}: estimated ${estimate_usd:.3f} exceeds remaining daily budget ${left:.3f} (cap ${cap_usd})"
        )
    return left


def record(session: Session, source: str, units: int, cost_usd: float, note: str | None = None) -> None:
    session.add(m.CostLedger(source=source, units=units, cost_usd=cost_usd, note=note))
    session.flush()
    from klugscheisser import calllog
    from klugscheisser.llm import budget

    calllog.add_cost(cost_usd)  # inside an MCP tool call: shown in its `mcp_call` line

    if (b := budget.current()) is not None:  # inside an answer: this part is booked (llm/budget.py finish())
        b.booked_in_ledger(source, cost_usd)
