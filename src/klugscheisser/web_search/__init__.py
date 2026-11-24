"""v3 P4 web enrichment: Brave search, allowed-page fetching, storing verified pages as documents."""

from __future__ import annotations

from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.interfaces import WebSearch


def web_search_enabled(settings: Settings) -> bool:
    return settings.brave_api_key is not None and settings.brave_daily_cap > 0


def get_web_search(settings: Settings, session: Session | None = None) -> WebSearch | None:
    """BraveSearch when BRAVE_API_KEY is set, else None (answer engine then works without web)."""
    if not web_search_enabled(settings):
        return None
    from klugscheisser.web_search.brave import BraveSearch

    return BraveSearch(settings, session=session)
