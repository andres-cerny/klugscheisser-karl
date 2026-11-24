"""News ingest: RSS -> women's hockey filter -> article excerpt -> athlete/team links -> store."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from klugscheisser.collectors.rss import Article, RssCollector, is_womens_hockey
from klugscheisser.db import models as m
from klugscheisser.domain.team_matcher import TeamMatcher
from klugscheisser.ingest.store import IngestContext, ItemDraft, content_hash, store_item

log = logging.getLogger(__name__)


@dataclass
class NewsReport:
    seen: int = 0
    womens: int = 0
    items_new: int = 0
    items_changed: int = 0
    errors: list[str] = field(default_factory=list)


def store_light(ctx: IngestContext, art) -> None:
    """Light level (v5): no full article, no classification, no vector — cheap, and searchable later."""
    s = ctx.session
    if s.scalar(select(m.Item.id).where(m.Item.url == art.url)) is not None:
        return
    s.add(
        m.Item(
            source_type=art.source_type,
            platform="rss",
            url=art.url,
            title=art.title,
            text=art.summary or art.title,
            source_name=art.feed,
            author=art.feed,
            published_at=art.published_at,
            content_hash=content_hash(art.summary, art.title),
            raw={"feed": art.feed},
            source_kind=art.source_kind,
            level="light",
            language=getattr(art, "language", None),
        )
    )
    s.flush()


# calibrated 2026-10-04 (live Jev, mode "meant"): players named in match reports 0.71–0.90, namesakes from
# football/skiing 0.02–0.05, a linked player not named in the text 0.14
MEANT_MIN = 0.5


def confirmed_links(ctx: IngestContext, article: Article, links: list) -> list:
    """v8: keep a person link only when the article is about THIS person (Jev on title + available text).
    Without Jev: full-name links stay (as before); surname-only links need the women's hockey context.
    A dropped link leaves the article stored, just not linked to that person."""
    from klugscheisser.identity import about_probability

    if not links:
        return links
    s = ctx.session
    text = article.text or article.summary or ""
    kept = []
    for link in links:
        a = s.get(m.Athlete, link.athlete_id)
        p = about_probability(ctx.settings, a, article.title or "", text, ctx.jev_client, mode="meant")
        if p is None:
            if link.kind != "surname" or is_womens_hockey(article):
                kept.append(link)
        elif p >= MEANT_MIN:
            kept.append(link)
        else:
            log.info("news link dropped (%.2f): %s – %s", p, a.full_name, article.url)
    return kept


def team_links(ctx: IngestContext, article: Article) -> list[int]:
    """Teams the article is about (whole-word aliases, women's context for national cues; see TeamMatcher)."""
    if ctx.team_matcher is None:
        ctx.team_matcher = TeamMatcher(ctx.session, athlete_matcher=ctx.matcher)
    checked = getattr(article, "canonical_url", None)
    text = f"{article.title}\n{article.text}"
    return [
        t.team_id
        for t in ctx.team_matcher.from_text(text, url=checked or article.url, url_checked=bool(checked))
    ]


def ingest_news(ctx: IngestContext, collector: RssCollector, fetch_articles: bool = True) -> NewsReport:
    s = ctx.session
    report = NewsReport()
    try:
        articles = collector.articles()
    except Exception as exc:
        report.errors.append(f"feed: {exc}")
        return report
    report.errors.extend(getattr(collector, "errors", []))  # feeds that failed while others worked
    for art in articles:
        report.seen += 1
        known = s.scalar(select(m.Item).where(m.Item.url == art.url))
        if known is not None and known.text and len(known.text) > len(art.summary):
            art.excerpt = known.text  # already fetched once; don't hit the site again
        prelim = ctx.matcher.from_text(f"{art.title} {art.summary}")
        prelim += ctx.matcher.from_surnames(
            f"{art.title} {art.summary}", {x.athlete_id for x in prelim}
        )  # v8
        if not (is_womens_hockey(art) or prelim):
            store_light(ctx, art)  # v5: title/teaser/link only; promoted when a person named in it is added
            continue
        report.womens += 1
        if (
            fetch_articles
            and (art.fetch_article or ctx.settings.news_fetch_all_articles)
            and art.excerpt is None
        ):
            try:
                art.excerpt = collector.fetch_excerpt(art.url)
            except Exception as exc:
                report.errors.append(f"{art.url}: {exc}")
            art.canonical_url = getattr(collector, "canonical", {}).get(art.url)
            if not prelim and not is_womens_hockey(art):
                # the full text / the outlet's real section shows another sport (e.g. women's football filed
                # under /eishockey/ in the feed): keep only the light record
                report.womens -= 1
                store_light(ctx, art)
                continue
        links = ctx.matcher.from_text(f"{art.title}\n{art.text}")
        links += ctx.matcher.from_surnames(f"{art.title}\n{art.text}", {x.athlete_id for x in links})
        links = confirmed_links(ctx, art, links)  # v8: identity check on the article for every person
        draft = ItemDraft(
            source_type=art.source_type,
            platform="rss",
            url=art.url,
            title=art.title,
            text=art.text,
            source_name=art.feed,
            author=art.feed,
            published_at=art.published_at,
            raw={"summary": art.summary, "feed": art.feed}
            | ({"canonical_url": art.canonical_url} if art.canonical_url else {}),
            source_kind=art.source_kind,
        )
        r = store_item(ctx, draft, links)
        r.item.language = art.language
        existing = {t.team_id for t in r.item.teams}
        for team_id in team_links(ctx, art):
            if team_id not in existing:
                r.item.teams.append(m.ItemTeam(team_id=team_id))
                existing.add(team_id)
        s.flush()
        report.items_new += r.created
        report.items_changed += r.changed and not r.created
    return report
