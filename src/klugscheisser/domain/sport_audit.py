"""Find stored news items that today's sport check would not have stored as women's ice hockey.

Dry run by default: lists the items and what would change. With ``apply=True``:
- team links the current TeamMatcher would not make are removed (e.g. women's football "Nati" -> sui-women,
  links that rest only on an unchecked /eishockey/ feed URL, links of multi-sport tickers);
- items that are clearly about another sport, would not pass the news filter and are linked to no person are
  marked not relevant (``classifications.relevant = false``), so answers and search skip them; the item stays.
Items the check judges to be ice hockey are never touched (the women's filter is not this check's business).
Every changed item gets ``raw["sport_check"]`` (date, verdict, removed team keys) so the change can be traced.

Only news (RSS) and web-search items are checked: other sources (Wikipedia profiles, results, team accounts)
link teams on other grounds than the text's sport.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.rss import Article, canonical_url, is_womens_hockey
from klugscheisser.db import models as m
from klugscheisser.domain.sport import SportVerdict, is_ticker, sport_verdict
from klugscheisser.domain.team_matcher import TeamMatcher

log = logging.getLogger(__name__)

PLATFORMS = ("rss", "web")


@dataclass
class SportFlag:
    item_id: int
    url: str
    title: str
    verdict: SportVerdict
    remove_teams: list[str] = field(default_factory=list)  # team keys
    mark_irrelevant: bool = False
    canonical_url: str | None = None

    def line(self) -> str:
        what = []
        if self.remove_teams:
            what.append("unlink " + ",".join(self.remove_teams))
        if self.mark_irrelevant:
            what.append("mark not relevant")
        v = self.verdict
        cues = ", ".join(f"{k} {n}" for k, n in sorted(v.other.items()))
        why = f"{v.sport} ({v.source}; hockey {v.hockey}{', ' + cues if cues else ''})"
        moved = f" -> {self.canonical_url}" if self.canonical_url and self.canonical_url != self.url else ""
        return f"#{self.item_id} {why}: {'; '.join(what)} | {self.title[:90]} | {self.url}{moved}"


def _fetch_hosts() -> set[str]:
    """Hosts whose article pages we may fetch (fetch_article: true in seeds/feeds.yaml)."""
    from klugscheisser.collectors.rss import DEFAULT_FEEDS

    return {urlparse(f.url).hostname for f in DEFAULT_FEEDS if f.fetch_article}


def _resolve(http, url: str) -> str | None:
    r = http.get(url)
    if r.status_code != 200:
        return None
    return canonical_url(r.text, str(r.url)) or url


def audit_sport(
    s: Session,
    since: datetime | None = None,
    resolve_urls: bool = False,
    http=None,
    delay_s: float = 1.0,
) -> list[SportFlag]:
    """Read-only (unless ``resolve_urls``, which only GETs article pages of fetch-allowed outlets).

    Flags team links today's rules would not make: texts about another sport, multi-sport tickers, and items
    whose only hockey evidence is an unchecked feed URL (/eishockey/ in a feed link proves nothing: SRF and Blick
    file women's football there). Items with a hockey cue in their own text are never touched."""
    matcher = TeamMatcher(s)
    q = select(m.Item).where(m.Item.platform.in_(PLATFORMS), m.Item.level == "full").order_by(m.Item.id)
    if since is not None:
        q = q.where(func.coalesce(m.Item.published_at, m.Item.scraped_at) >= since)
    hosts = _fetch_hosts() if resolve_urls else set()
    if resolve_urls and http is None:
        import httpx

        from klugscheisser.collectors.rss import USER_AGENT

        http = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    flags = []
    for item in s.scalars(q):
        raw = item.raw or {}
        art = Article(
            feed=item.source_name or "",
            source_type=item.source_type,
            url=item.url,
            source_kind=item.source_kind or "",
            title=item.title or "",
            summary=raw.get("summary") or "",
            published_at=item.published_at,
            excerpt=item.text,
            canonical_url=raw.get("canonical_url"),
        )
        body = f"{art.title}\n{art.text}"
        ticker = is_ticker(body, item.url)
        verdict = sport_verdict(body, art.canonical_url or art.url)
        resolved = None
        if (
            resolve_urls
            and verdict.source == "url"
            and not art.canonical_url
            and urlparse(item.url).hostname in hosts
        ):
            try:
                time.sleep(delay_s)
                resolved = art.canonical_url = _resolve(http, item.url)
            except Exception as exc:  # network trouble: judge without it
                log.warning("resolve %s: %s", item.url, exc)
            verdict = sport_verdict(body, art.canonical_url or art.url)
        if verdict.is_hockey and verdict.source == "body" and not ticker:
            continue  # the text itself is about ice hockey: not this check's business
        if item.platform != "rss" and not (verdict.is_other or verdict.sport == "mixed"):
            continue  # web pages: only sport mistakes (feed URL sections and tickers are a news matter)
        existing = {t.team_id: t.team.key for t in item.teams}
        has_person = bool(item.athletes)
        # RSS: today's news filter; web search results passed no filter, so their own text must name hockey
        passes = has_person or (is_womens_hockey(art) if item.platform == "rss" else verdict.names_hockey)
        keep: set[int] = set()
        if passes:
            checked = art.canonical_url
            keep = {
                t.team_id for t in matcher.from_text(body, url=checked or art.url, url_checked=bool(checked))
            }
        remove = sorted(k for tid, k in existing.items() if tid not in keep)
        relevant = item.classification is not None and bool(item.classification.relevant)
        mark = not passes and verdict.is_other and relevant
        if remove or mark:
            flags.append(SportFlag(item.id, item.url, item.title or "", verdict, remove, mark, resolved))
    return flags


def apply_flags(s: Session, flags: list[SportFlag]) -> tuple[int, int]:
    """Unlink / mark as listed. Returns (team links removed, items marked not relevant)."""
    unlinked = marked = 0
    today = datetime.now(UTC).date().isoformat()
    for f in flags:
        item = s.get(m.Item, f.item_id)
        if item is None:
            continue
        for link in [t for t in item.teams if t.team.key in f.remove_teams]:
            item.teams.remove(link)
            unlinked += 1
        if f.mark_irrelevant and item.classification is not None:
            item.classification.relevant = False
            marked += 1
        raw = dict(item.raw or {})
        raw["sport_check"] = {
            "date": today,
            "sport": f.verdict.sport,
            "removed_teams": f.remove_teams,
            "marked_irrelevant": f.mark_irrelevant,
        }
        if f.canonical_url:
            raw["canonical_url"] = f.canonical_url
        item.raw = raw
    s.flush()
    return unlinked, marked
