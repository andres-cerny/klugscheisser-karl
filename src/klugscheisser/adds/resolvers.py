"""Factories for the add resolvers (v4). Keyword arguments are injected in tests (fake HTTP, Jev, LLM, web)."""

from __future__ import annotations

from typing import Any

from klugscheisser.adds.base import PersonResolver, TeamResolver
from klugscheisser.adds.person import WikiPersonResolver
from klugscheisser.adds.team import TeamRosterResolver
from klugscheisser.adds.wiki import WikiClient
from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.config import Settings


def _wiki(http: PoliteClient | None, wiki: WikiClient | None) -> WikiClient:
    return wiki or WikiClient(http)


def get_person_resolver(
    settings: Settings, *, http: PoliteClient | None = None, wiki: WikiClient | None = None, **inject: Any
) -> PersonResolver:
    """WikiPersonResolver. `inject`: web_search=, jev=, instagram_source= (None disables the service)."""
    return WikiPersonResolver(settings, wiki=_wiki(http, wiki), **inject)


def get_team_resolver(
    settings: Settings, *, http: PoliteClient | None = None, wiki: WikiClient | None = None, **inject: Any
) -> TeamResolver:
    """TeamRosterResolver. `inject`: llm= (None = rule-based interpretation)."""
    return TeamRosterResolver(settings, wiki=_wiki(http, wiki), **inject)
