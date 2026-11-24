"""v8: deep sources for tier 1 and tier 2 ("Backfill" once, then the same steps on "Alles neu laden").

Per person, by tier (docs/build-plan-v8.md):

| | tier 1 | tier 2 |
|---|---|---|
| Brave (web + news, own language, `site:` queries for boulevard/local outlets) | 30 queries | 4 queries |
| Instagram own posts | full history (≤ 400) | latest 10 |
| Instagram tagged posts (Apify "mentions") | latest 50, last 2 years | – |
| Facebook (own page, text only) | latest 30 | – |
| X | own posts + name search (light) | – |
| podcasts/videos (links only) | 2 extra queries | – |

Every search hit: allowed page -> full text; Jev: is it her? -> stored with its date and linked. Pages we may
not read (outlet terms/robots, e.g. Blick) -> title + teaser + link, after the same Jev check. Hits that fail
the check are not linked to her. One budget for the whole run (`DeepBudget`, ledger since the start).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.projects import wording

log = logging.getLogger(__name__)

# language, Brave country, "interview", sport word — by nationality
LANG = {
    "SUI": ("de", "CH", "Interview", "Eishockey"),
    "GER": ("de", "DE", "Interview", "Eishockey"),
    "AUT": ("de", "AT", "Interview", "Eishockey"),
    "CAN": ("en", "CA", "interview", "hockey"),
    "USA": ("en", "US", "interview", "hockey"),
    "FIN": ("fi", "FI", "haastattelu", "jääkiekko"),
    "SWE": ("sv", "SE", "intervju", "ishockey"),
    "CZE": ("cs", "", "rozhovor", "hokej"),
    "DEN": ("da", "DK", "interview", "ishockey"),
    "JPN": ("ja", "JP", "インタビュー", "アイスホッケー"),
    "FRA": ("fr", "FR", "interview", "hockey"),
    "ITA": ("it", "IT", "intervista", "hockey"),
}
# floorball projects (2026-11-24): the sport word in the home language ("hockey" → "floorball" in English)
FLOORBALL_WORDS = {
    "SUI": "Unihockey",
    "GER": "Floorball",
    "AUT": "Floorball",
    "FIN": "salibandy",
    "SWE": "innebandy",
    "NOR": "innebandy",
    "CZE": "florbal",
    "SVK": "florbal",
    "DEN": "floorball",
    "LAT": "florbols",
    "POL": "unihokej",
}


def _sport_words(nationality: str | None) -> tuple[str, str]:
    """(sport word in the person's language, English sport word) of the current project's sport."""
    from klugscheisser.sports import current_spec

    if current_spec().family == "unihockey":
        return FLOORBALL_WORDS.get(nationality or "", "floorball"), "floorball"
    return LANG.get(nationality or "", ("en", "ALL", "interview", "hockey"))[3], "hockey"


# Swiss boulevard, people, regional and public media (the site: queries of tier 1)
SWISS_SITES = [
    "blick.ch",
    "20min.ch",
    "watson.ch",
    "srf.ch",
    "nzz.ch",
    "tagesanzeiger.ch",
    "luzernerzeitung.ch",
    "aargauerzeitung.ch",
    "schweizer-illustrierte.ch",
    "annabelle.ch",
    "nau.ch",
    "swissinfo.ch",
    "suedostschweiz.ch",
    "bernerzeitung.ch",
    "rts.ch",
    "rsi.ch",
    "swisshockeynews.ch",
]
MEDIA_LINK_HOSTS = {
    "youtube.com": "video",
    "youtu.be": "video",
    "vimeo.com": "video",
    "open.spotify.com": "podcast",
    "podcasts.apple.com": "podcast",
    "soundcloud.com": "podcast",
    "srf.ch/audio": "podcast",
    "srf.ch/play": "video",
    "podcasts.google.com": "podcast",
}
SOCIAL_HOSTS = ("instagram.com", "facebook.com", "x.com", "twitter.com", "tiktok.com", "linkedin.com")
RESULTS_PER_QUERY = 8
MARK = "deep"  # CacheEntry per person: last deep run


class BraveCreditExhausted(Exception):
    """The Brave prepaid credit is used up: stop the web step (nothing more can be searched)."""


@dataclass
class Query:
    q: str
    endpoint: str = "web"
    country: str | None = None
    lang: str | None = None


@dataclass
class DeepReport:
    persons: int = 0
    queries: int = 0
    pages_read: int = 0
    stored: int = 0
    teaser_only: int = 0
    not_her: int = 0
    media_links: int = 0
    instagram_posts: int = 0
    tagged_posts: int = 0
    facebook_posts: int = 0
    x_posts: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None
    notes: list[str] = field(default_factory=list)


class DeepBudget:
    """Everything recorded in the cost ledger since the run started counts (Apify, Brave, X, Claude/Gemini)."""

    def __init__(self, cap_usd: float, since: datetime | None = None) -> None:
        self.cap = cap_usd
        self.since = since or datetime.now(UTC)

    def spent(self, s: Session) -> float:
        return float(
            s.scalar(
                select(func.coalesce(func.sum(m.CostLedger.cost_usd), 0.0)).where(
                    m.CostLedger.created_at >= self.since
                )
            )
            or 0.0
        )

    def left(self, s: Session) -> float:
        return self.cap - self.spent(s)


def run_settings(settings: Settings, cap_usd: float) -> Settings:
    """The daily safety brakes are lifted for this run; the run's own cap holds instead."""
    return settings.model_copy(
        update={
            "apify_daily_cost_cap_usd": cap_usd,
            "llm_daily_cost_cap_usd": cap_usd,
            "brave_daily_cap": 5000,
            "media_daily_limit": 5000,
            "x_daily_cost_cap_usd": min(cap_usd, 3.0),
        }
    )


# --- queries ------------------------------------------------------------------------------------------------


def queries_for(a: m.Athlete, tier: int) -> list[Query]:
    name = f'"{a.full_name}"'
    lang, cc, interview, _ = LANG.get(a.nationality or "", ("en", "ALL", "interview", "hockey"))
    sport, hockey = _sport_words(a.nationality)
    clubs = [
        x.team.name for x in sorted(a.affiliations, key=lambda x: x.season or "", reverse=True) if x.team
    ]
    club = next((c for c in clubs if c not in ("Schweiz",)), None)
    coach = a.role == "coach"
    if tier == 2:
        out = [
            Query(f"{name} {sport}", "web", cc, lang),
            Query(f"{name} {interview}", "web", cc, lang),
            Query(f"{name} {hockey}", "news", "ALL", "en"),
            Query(f"{name} {club}" if club else f"{name} {hockey} interview", "web", "ALL", "en"),
        ]
        return out
    out = [
        Query(name, "web", cc, lang),
        Query(f"{name} {interview}", "web", cc, lang),
        Query(f"{name} {sport}", "web", cc, lang),
        Query(f"{name} Porträt", "web", cc, lang),
        Query(name, "news", cc, lang),
        Query(f"{name} {sport}", "news", cc, lang),
        Query(f"{name} {hockey} interview", "web", "ALL", "en"),
        Query(f"{name} {hockey}", "news", "ALL", "en"),
        Query(f"{name} {hockey}", "web", "CH", "fr"),
        Query(f"{name} {hockey}", "web", "CH", "it"),
        Query(f"{name} Familie" if not coach else f"{name} {_coach_word()}", "web", cc, lang),
        Query(f"{name} Studium Beruf", "web", cc, lang),
        Query(f"{name} Sponsor", "web", cc, lang),
    ]
    if club:
        out.append(Query(f"{name} {club}", "web", cc, lang))
    out += [Query(f"site:{site} {name}", "web", cc, lang) for site in SWISS_SITES]
    return out[:30]


def _coach_word() -> str:
    from klugscheisser.sports import current_spec

    return "Trainer" if current_spec().men else "Trainerin"


def media_queries(a: m.Athlete) -> list[Query]:
    return [
        Query(f'"{a.full_name}"', "videos", "ALL", None),
        Query(f'"{a.full_name}" Podcast', "web", "ALL", None),
    ]


def media_kind(url: str) -> str | None:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = f"{host}{parts.path}"
    for key, kind in MEDIA_LINK_HOSTS.items():
        if path.startswith(key) or host == key:
            if key == "open.spotify.com" and "/episode/" not in parts.path and "/show/" not in parts.path:
                return None
            if host.endswith("youtube.com") and not ("watch" in parts.path or "/shorts/" in parts.path):
                return None
            return kind
    return None


def _parse_age(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _known_urls(s: Session, athlete_id: int) -> set[str]:
    return set(
        s.scalars(
            select(m.Item.url)
            .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
            .where(m.ItemAthlete.athlete_id == athlete_id)
        )
    )


# --- web, news, media links ---------------------------------------------------------------------------------


def web_for_person(
    settings: Settings, athlete_id: int, tier: int, budget: DeepBudget, report: DeepReport, web=None
) -> None:
    from klugscheisser.domain.matcher import Link
    from klugscheisser.identity import about_probability, is_about, mentions_name
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.ingest.store import ItemDraft, store_item
    from klugscheisser.web_search.brave import BraveSearch
    from klugscheisser.web_search.store import canonical_url, outlet_for, store_web_page

    with session_scope() as s:
        a = s.get(m.Athlete, athlete_id)
        web = web or BraveSearch(settings, session=s)
        if getattr(web, "session", None) is None:
            web.session = s
        ctx = build_context(s, settings)
        known = _known_urls(s, a.id)
        queries = queries_for(a, tier) + (media_queries(a) if tier == 1 else [])
        seen: set[str] = set()
        for q in queries:
            if budget.left(s) <= 0:
                report.stopped = "budget"
                return
            try:
                results = web.search(
                    q.q,
                    limit=RESULTS_PER_QUERY,
                    endpoint=q.endpoint,
                    country=q.country,
                    search_lang=q.lang,
                    keep_social=True,
                )
            except Exception as exc:
                if " 402" in str(exc) or "402 Payment" in str(exc):
                    raise BraveCreditExhausted("Brave: Guthaben aufgebraucht (402) – bitte aufladen") from exc
                report.notes.append(f"{a.full_name}: Brave {q.q[:40]}: {exc}")
                continue
            report.queries += 1
            for r in results:
                url = canonical_url(r.url)
                if url in seen or url in known:
                    continue
                seen.add(url)
                host = urlsplit(url).netloc.lower()
                kind = media_kind(url)
                published = _parse_age(r.page_age)
                if kind is not None:
                    if tier != 1:
                        continue
                    if _store_media_link(s, ctx, settings, a, r, url, kind, published, report):
                        known.add(url)
                    continue
                if any(host.endswith(x) for x in SOCIAL_HOSTS):
                    continue  # social media come from their own collectors
                page = web.fetch(url)
                if page.allowed and page.text:
                    report.pages_read += 1
                    if not is_about(settings, a, page.title, page.text, ctx.jev_client):
                        report.not_her += 1
                        continue
                    if published and not getattr(page, "published_at", None):
                        page.published_at = published
                    if store_web_page(ctx, page, [a.id], q.q) is not None:
                        report.stored += 1
                        known.add(url)
                    continue
                # not readable (outlet terms / robots): title + teaser + link. Only stories about her: her name
                # in the title, or Jev says the teaser is about her (team reports that only list her are left out)
                teaser = f"{r.title}\n{r.snippet}".strip()
                if not mentions_name(a, r.title, r.snippet):
                    continue
                in_title = mentions_name(a, r.title, "") or _surname_in(a, r.title)
                mode = "meant" if in_title else "about"
                p = about_probability(settings, a, r.title, r.snippet, ctx.jev_client, mode=mode)
                if p is not None and p < (0.5 if in_title else 0.8):
                    report.not_her += 1
                    continue
                source_kind, outlet = outlet_for(s, url)
                draft = ItemDraft(
                    source_type="web",
                    platform="web",
                    url=url,
                    title=r.title,
                    text=teaser,
                    source_name=(outlet or host.removeprefix("www."))[:100],
                    published_at=published,
                    raw={"query": q.q, "teaser_only": True, "note": page.note},
                    source_kind=source_kind,
                )
                store_item(ctx, draft, [Link(a.id, "name", 0.8)])
                report.teaser_only += 1
                known.add(url)
            s.commit()


def _surname_in(a: m.Athlete, text: str) -> bool:
    from klugscheisser.domain.names import normalize

    last = normalize(a.full_name).split()[-1]
    return len(last) >= 4 and last in normalize(text).split()


def _store_media_link(s, ctx, settings, a, r, url, kind, published, report) -> bool:
    """Podcast/video link: Jev must say she appears or talks in it (title + description). No download."""
    from klugscheisser.domain.matcher import Link
    from klugscheisser.identity import mentions_name
    from klugscheisser.ingest.store import ItemDraft, store_item

    if not mentions_name(a, r.title, r.snippet):
        return False
    if ctx.jev_client is not None or settings.features["jev"]:
        from klugscheisser.classify.jev import JevClient, noul_probability
        from klugscheisser.identity import about_question

        jev = ctx.jev_client or JevClient(settings)
        question = about_question(a, "meant")
        he = wording().pronoun_de
        question["appears"] = {
            "type": "noul",
            "instructions": f"Kommt {a.full_name} in diesem {'Video' if kind == 'video' else 'Podcast'} selbst vor "
            f"oder spricht {he} darin (nicht nur erwähnt)?",
            "criteria": {
                "true": f"{he} ist Gast, wird interviewt oder ist zu sehen/hören",
                "false": "nur erwähnt",
            },
        }
        try:
            ans = jev.ask(f"{r.title}\n{r.snippet}\n{r.extra or ''}", question)
        except Exception as exc:
            report.notes.append(f"{a.full_name}: Jev {exc}")
            return False
        if noul_probability(ans["about"]) < 0.5 or noul_probability(ans["appears"]) < 0.5:
            report.not_her += 1
            return False
    extra = r.extra or {}
    bits = [
        r.title,
        r.snippet,
        f"Kanal: {extra['kanal']}" if extra.get("kanal") else "",
        f"Dauer: {extra['dauer']}" if extra.get("dauer") else "",
    ]
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    draft = ItemDraft(
        source_type="media_link",
        platform=kind,  # video | podcast
        url=url,
        title=r.title,
        text="\n".join(b for b in bits if b),
        source_name=(extra.get("kanal") or host)[:100],
        published_at=published,
        raw={"link_only": True, **extra},
        source_kind="media",
    )
    store_item(ctx, draft, [Link(a.id, "name", 0.8)])
    report.media_links += 1
    return True


# --- social -------------------------------------------------------------------------------------------------


def social_for_persons(
    settings: Settings,
    athlete_ids: list[int],
    tier: int,
    budget: DeepBudget,
    report: DeepReport,
    fresh: bool = True,
) -> None:
    """fresh: first run (history); otherwise only what is new since the last scrape ("Alles neu laden")."""
    from klugscheisser.collectors.instagram.sources import get_instagram_source
    from klugscheisser.ingest.instagram import ingest_instagram
    from klugscheisser.ingest.runner import build_context

    with session_scope() as s:
        ctx = build_context(s, settings)
        source = get_instagram_source(settings)
        if source is None or budget.left(s) <= 0:
            return
        athletes = [s.get(m.Athlete, i) for i in athlete_ids]
        from klugscheisser.ingest.instagram import _AUTO

        if tier == 1:
            r = ingest_instagram(
                ctx, source, athletes, max_posts=400, since=None if fresh else _AUTO, batch_size=1
            )
            report.instagram_posts += r.items_new
            s.commit()
            if budget.left(s) > 0 and hasattr(source, "fetch_mentions"):
                since = datetime.now(UTC) - timedelta(days=730 if fresh else 30)
                r = ingest_instagram(
                    ctx,
                    source,
                    athletes,
                    max_posts=50,
                    since=since,
                    mentions=True,
                    refresh_profiles=False,
                    batch_size=1,
                )
                report.tagged_posts += r.items_new
        else:
            r = ingest_instagram(
                ctx, source, athletes, max_posts=10, since=None if fresh else _AUTO, batch_size=5
            )
            report.instagram_posts += r.items_new
        report.notes += [f"Instagram: {e}" for e in r.errors[:5]]
        s.commit()


def facebook_for_persons(
    settings: Settings, athlete_ids: list[int], budget: DeepBudget, report: DeepReport
) -> None:
    from klugscheisser.collectors.facebook import ApifyFacebookSource
    from klugscheisser.domain.matcher import Link
    from klugscheisser.ingest import costs
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.ingest.store import ItemDraft, store_item

    if settings.apify_token is None:
        return
    source = ApifyFacebookSource(settings)
    for aid in athlete_ids:
        with session_scope() as s:
            a = s.get(m.Athlete, aid)
            pages = [h for h in a.handles if h.platform == "facebook" and h.usable]
            if not pages or budget.left(s) < 0.2:
                continue
            ctx = build_context(s, settings)
            for h in pages:
                try:
                    posts, usd = source.fetch_posts(h.handle, limit=30, max_usd=min(0.5, budget.left(s)))
                except Exception as exc:
                    report.notes.append(f"Facebook {h.handle}: {exc}")
                    continue
                costs.record(s, "apify", len(posts), usd, f"facebook {h.handle}")
                for p in posts:
                    draft = ItemDraft(
                        source_type="athlete_post",
                        platform="facebook",
                        url=p.url,
                        text=p.text,
                        source_name=f"Facebook {p.page_name or h.handle}",
                        author=p.page_name or h.handle,
                        published_at=p.published_at,
                        raw={"page": h.handle},
                        source_kind="self",
                    )
                    report.facebook_posts += store_item(ctx, draft, [Link(a.id, "author", 1.0)]).created
                h.last_scraped_at = datetime.now(UTC)


def x_light(
    settings: Settings, athlete_ids: list[int], budget: DeepBudget, report: DeepReport, names: int = 10
) -> None:
    """Light X test for tier 1: own posts of the known handles + a name search (last 7 days) for some players."""
    from klugscheisser.collectors.x_api import XApiClient
    from klugscheisser.domain.matcher import Link
    from klugscheisser.identity import about_probability
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.ingest.x import _record, _store, ingest_x

    if settings.x_bearer_token is None:
        report.notes.append("X: kein Token")
        return
    client = XApiClient(settings)
    with session_scope() as s:
        ctx = build_context(s, settings)
        athletes = [s.get(m.Athlete, i) for i in athlete_ids]
        with_x = [a for a in athletes if any(h.platform == "x" for h in a.handles)]
        r = ingest_x(ctx, client, with_x, team_accounts=False)
        report.x_posts += r.posts_new
        players = [a for a in athletes if a.role == "athlete"][:names]
        for a in players:
            if budget.left(s) <= 0:
                break
            try:
                posts = client.search_recent(f'"{a.full_name}" -is:retweet', max_results=10)
            except Exception as exc:
                report.notes.append(f"X Suche {a.full_name}: {exc}")
                continue
            finally:
                _record(s, settings, client, r, f"search {a.full_name}")
            for post in posts:
                p = about_probability(settings, a, "", post.text, ctx.jev_client, mode="meant")
                if p is not None and p < 0.5:
                    continue
                report.x_posts += _store(ctx, post, [Link(a.id, "name", 0.8)], "other_media", "web")
        s.commit()


# --- the run ------------------------------------------------------------------------------------------------


def run(
    settings: Settings,
    tier: int,
    cap_usd: float,
    athlete_ids: list[int] | None = None,
    steps: tuple[str, ...] = ("handles", "web", "social", "facebook", "x", "after"),
    since: datetime | None = None,
    echo: Callable[[str], None] = print,
    fresh: bool = True,
) -> DeepReport:
    """One tier (or the given persons). `since`: count the budget from this moment (shared across tiers)."""
    from klugscheisser import tiers
    from klugscheisser.handles import check

    st = run_settings(settings, cap_usd)
    budget = DeepBudget(cap_usd, since)
    report = DeepReport()
    with session_scope() as s:
        ids = athlete_ids or tiers.ids_in(s, tier)
    report.persons = len(ids)
    echo(f"Tier {tier}: {len(ids)} Personen, Budget ${cap_usd:.2f}")

    if "handles" in steps:
        from klugscheisser.collectors.instagram.sources import get_instagram_source

        with session_scope() as s:
            # profiles of "wahrscheinlich" handles first (cheap), then Jev decides
            athletes = [s.get(m.Athlete, i) for i in ids]
            source = get_instagram_source(st)
            missing = [
                h
                for a in athletes
                for h in a.handles
                if h.platform == "instagram" and h.status == "wahrscheinlich" and not h.profile
            ]
            if source is not None and missing:
                from klugscheisser.collectors.instagram.sources import paid_profile_count
                from klugscheisser.handles.profile import apply_profile
                from klugscheisser.ingest import costs

                try:
                    profiles = {p.username: p for p in source.fetch_profiles([h.handle for h in missing])}
                    if n := paid_profile_count(source, len(missing)):
                        costs.record(s, "apify", n, n * st.apify_cost_per_post_usd, "profiles v8")
                    for h in missing:
                        if (p := profiles.get(h.handle)) is not None:
                            apply_profile(h, p)
                except Exception as exc:
                    report.notes.append(f"Profile: {exc}")
            rep = check.CheckReport()
            from klugscheisser.classify.jev import JevClient

            jev = JevClient(st) if st.features["jev"] else None
            if jev is not None:
                check.check_instagram(s, jev, ids, rep)
                if tier == 1 and "facebook" in steps and st.brave_api_key is not None:
                    from klugscheisser.web_search.brave import BraveSearch

                    check.find_facebook(s, jev, BraveSearch(st, session=s), ids, rep)
            echo(
                f"Handles: {rep.checked} geprüft, {rep.verified} verifiziert, {rep.demoted} zurückgestuft, "
                f"Facebook {rep.facebook_found}/{rep.facebook_queries}"
            )
            report.notes += rep.notes
    if "social" in steps:
        social_for_persons(st, ids, tier, budget, report, fresh=fresh)
        echo(f"Instagram: {report.instagram_posts} eigene, {report.tagged_posts} markierte Posts neu")
    if "facebook" in steps and tier == 1:
        facebook_for_persons(st, ids, budget, report)
        echo(f"Facebook: {report.facebook_posts} Posts neu")
    if "x" in steps and tier == 1:
        x_light(st, ids, budget, report)
        echo(f"X: {report.x_posts} Posts neu")
    if "web" in steps:
        for n, aid in enumerate(ids, 1):
            with session_scope() as s:
                if budget.left(s) <= 0:
                    report.stopped = "budget"
                    break
            try:
                web_for_person(st, aid, tier, budget, report)
            except BraveCreditExhausted as exc:
                report.stopped = "brave_credit"
                echo(f"{exc} — Websuche gestoppt bei Person {n}/{len(ids)}")
                break
            except Exception as exc:  # one person (e.g. a URL a parallel worker stored) must not stop the run
                log.warning("web for person %s failed: %s", aid, exc)
                report.notes.append(f"Web {aid}: {str(exc)[:120]}")
            if n % 5 == 0 or n == len(ids):
                echo(
                    f"Web {n}/{len(ids)}: {report.queries} Suchen, {report.stored} Seiten, "
                    f"{report.teaser_only} Teaser, {report.media_links} Podcasts/Videos, {report.not_her} nicht sie"
                )
    with session_scope() as s:
        report.spent_usd = round(budget.spent(s), 2)
    echo(
        f"Tier {tier} fertig: ${report.spent_usd:.2f} seit Start"
        + (f" (gestoppt: {report.stopped})" if report.stopped else "")
    )
    return report


# --- after the sources: facts, fun facts, translations --------------------------------------------------------


def after(settings: Settings, ids: list[int], budget: DeepBudget, report: DeepReport, echo=print) -> None:
    """Facts from the new documents, fun facts for persons with new documents (tier rules), German for quotes
    and summaries that are neither German nor English."""
    from klugscheisser import translate
    from klugscheisser.facts import funfacts
    from klugscheisser.facts.extract import DailyQuotaExceeded, extract_facts, select_documents

    changed: list[int] = []
    for aid in ids:
        with session_scope() as s:
            a = s.get(m.Athlete, aid)
            last = s.get(m.CacheEntry, (MARK, f"athlete:{aid}"))
            if select_documents(s, a):
                changed.append(aid)
            for _ in range(6):  # MAX_DOCS per call; a deep run brings more
                if budget.left(s) <= 0 or not select_documents(s, a):
                    break
                try:
                    extract_facts(s, settings, a)
                except DailyQuotaExceeded as exc:
                    report.notes.append(f"KI-Tageslimit: {exc}")
                    break
                except Exception as exc:
                    report.notes.append(f"Fakten {a.full_name}: {str(exc)[:120]}")
                    break
                s.commit()
            if last is None and aid not in changed:
                changed.append(aid)  # first deep run: fun facts with the tier's rules anyway
    echo(f"Fakten: {len(changed)} Personen mit neuen Dokumenten")
    for aid in changed:
        with session_scope() as s:
            if budget.left(s) <= 0:
                report.stopped = "budget"
                break
            try:
                funfacts.generate(s, settings, s.get(m.Athlete, aid))
            except Exception as exc:
                report.notes.append(f"Fun Facts {aid}: {str(exc)[:120]}")
    echo(f"Fun Facts: {len(changed)} Personen neu erstellt")
    with session_scope() as s:
        if budget.left(s) <= 0:  # the run's cap covers the translations too
            report.stopped = "budget"
            echo("Übersetzungen: übersprungen (Budget aufgebraucht)")
            return
        translate.backfill(s, settings, ids, echo=echo, budget_left=lambda: budget.left(s))


# --- "Alles neu laden" (web app) ------------------------------------------------------------------------------

PER_PERSON_CAP = {1: 2.0, 2: 0.4}


def _run_job(job_id: int, ids: list[int], settings: Settings) -> None:
    from klugscheisser import tiers

    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
        groups: dict[int, list[int]] = {}
        fresh_ids: set[int] = set()
        for aid in ids:
            a = s.get(m.Athlete, aid)
            t = tiers.effective(a)
            if t in (1, 2):
                groups.setdefault(t, []).append(aid)
                if s.get(m.CacheEntry, (MARK, f"athlete:{aid}")) is None:
                    fresh_ids.add(aid)
        started = datetime.now(UTC)
    total = DeepReport()
    try:
        for t, group in sorted(groups.items()):
            cap = PER_PERSON_CAP[t] * len(group)
            for fresh in (True, False):
                part = [i for i in group if (i in fresh_ids) == fresh]
                if part:
                    r = run(settings, t, cap, part, since=started, echo=lambda *_: None, fresh=fresh)
                    for k in (
                        "instagram_posts",
                        "tagged_posts",
                        "facebook_posts",
                        "x_posts",
                        "stored",
                        "teaser_only",
                        "media_links",
                    ):
                        setattr(total, k, getattr(total, k) + getattr(r, k))
        with session_scope() as s:
            total.spent_usd = DeepBudget(0, started).spent(s)
        new = total.instagram_posts + total.tagged_posts + total.facebook_posts + total.x_posts
        new += total.stored + total.teaser_only + total.media_links
        result, status, error = {"items_new": new, "cost_usd": total.spent_usd}, "done", None
    except Exception as exc:
        log.exception("deep job %s failed", job_id)
        result, status, error = None, "failed", str(exc)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.result, job.error, job.finished_at = status, result, error, datetime.now(UTC)


def start_job(s: Session, settings: Settings, ids: list[int], submit=None) -> m.Job:
    """Background job for the "Alles neu laden" button (one person or an event's roster)."""
    from klugscheisser.jobs.refresh import executor

    job = m.Job(kind="deep", params={"athlete_ids": ids})
    s.add(job)
    s.flush()
    s.commit()
    (submit or executor().submit)(_run_job, job.id, ids, settings)
    return job


def media_queue(
    settings: Settings, budget: DeepBudget, echo=print, batch: int = 50, shard: tuple[int, int] | None = None
) -> int:
    """Work through the media queue now (the run's cap instead of the daily limit). Returns analyses done."""
    from klugscheisser.media.analyzer import get_media_analyzer
    from klugscheisser.media.worker import process_media_queue

    analyzer = get_media_analyzer(settings)
    if analyzer is None:
        return 0
    done = 0
    while True:
        with session_scope() as s:
            if budget.left(s) <= 0:
                echo("Medien: Budget erreicht")
                break
            r = process_media_queue(s, settings, analyzer, limit=batch, pause_s=0.5, shard=shard)
        done += r.analyzed
        echo(f"Medien: {done} analysiert" + (f", {len(r.errors)} Fehler" if r.errors else ""))
        if r.analyzed == 0 or r.rate_limited or r.limit_reached:
            break
    return done
