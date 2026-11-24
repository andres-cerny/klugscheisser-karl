"""Collectors for raw documents about a person (v3 P2): Wikipedia articles, college player bio pages.

Swiss club sites (EVZ, ZSC Lions, SC Bern) were checked 2026-10-03: robots.txt allows crawling, but none has
individual women's player pages with bio text (EVZ: one team page; ZSC/SCB: no player pages in the sitemap),
so there is no club collector. EVZ's women's news (``/news/teams/women/``) carries interviews -> news feeds.
"""

from __future__ import annotations

from klugscheisser.collectors.profiles.college import SidearmRosterCollector
from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector
from klugscheisser.config import Settings
from klugscheisser.interfaces import ProfileCollector

__all__ = ["PoliteClient", "SidearmRosterCollector", "WikipediaCollector", "enabled_profile_collectors"]


def enabled_profile_collectors(settings: Settings) -> list[ProfileCollector]:
    client = PoliteClient(
        min_delay_s=settings.profiles_request_delay_s, max_crawl_delay_s=settings.profiles_max_crawl_delay_s
    )
    out: list[ProfileCollector] = [
        WikipediaCollector(
            client,
            settings.profiles_wikipedia_languages,
            settings.profiles_wikipedia_max_chars,
            max_languages=settings.profiles_max_languages,
        )
    ]
    if settings.profiles_college_enabled:
        out.append(SidearmRosterCollector(client=client, max_chars=settings.profiles_max_chars))
    return out
