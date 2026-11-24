"""Domain helpers: registrable domain, blocklist matching, outlets whose terms forbid automated reading."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from klugscheisser.collectors.rss import FEEDS_SEED, load_feeds

# second-level public suffixes we meet in practice (no public-suffix dependency)
_TWO_LEVEL = {"co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.jp", "com.br", "co.at", "or.at"}


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url_or_host: str) -> str:
    host = host_of(url_or_host) if "/" in url_or_host else url_or_host.lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_blocked(url: str, patterns: list[str]) -> bool:
    """``x.com`` matches x.com and its subdomains; ``flashscore.*`` matches flashscore under any TLD."""
    host = host_of(url)
    for pattern in patterns:
        pattern = pattern.lower().strip()
        if not pattern:
            continue
        if pattern.endswith(".*"):
            if re.search(rf"(^|\.){re.escape(pattern[:-2])}\.[a-z0-9.-]+$", host):
                return True
        elif host == pattern or host.endswith("." + pattern):
            return True
    return False


@lru_cache
def terms_restricted_domains() -> frozenset[str]:
    """Outlets in seeds/feeds.yaml with ``fetch_article: false``: their terms forbid automated reading of
    article pages (see docs/sources.md), so web fetch keeps only the search snippet for them too."""
    try:
        feeds = load_feeds(FEEDS_SEED)
    except OSError:
        return frozenset()
    return frozenset(registrable_domain(f.url) for f in feeds if not f.fetch_article)
