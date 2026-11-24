"""X posts as documents: persons' own accounts (tiered handle check, never auto-verified) and official team
accounts (settings.x_team_accounts). Incremental: only posts newer than the last one we stored."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.x_api import XApiClient, XPost, XUser
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.matcher import Link
from klugscheisser.domain.names import name_variants, normalize
from klugscheisser.domain.sport import sport_verdict
from klugscheisser.handles.service import add_evidence, recompute
from klugscheisser.ingest import costs
from klugscheisser.ingest.store import IngestContext, ItemDraft, store_item
from klugscheisser.projects import wording

log = logging.getLogger(__name__)
SOURCE = "x"


@dataclass
class XReport:
    profiles_checked: int = 0
    handles_read: int = 0
    posts_new: int = 0
    team_posts_new: int = 0
    cost_usd: float = 0.0
    skipped_budget: list[str] = field(default_factory=list)
    skipped_first_read: list[str] = field(default_factory=list)  # never read before, first reads not allowed
    errors: list[str] = field(default_factory=list)


def apply_x_profile(handle: m.Handle, user: XUser | None) -> None:
    """Profile name / bio as evidence for an X handle (same tiers as Instagram)."""
    url = f"https://x.com/{handle.handle}"
    if user is None:
        handle.profile = {"missing": True, "fetched_at": datetime.now(UTC).isoformat()}
        return
    handle.profile = {
        "x_id": user.id,
        "full_name": user.name,
        "biography": user.description,
        "verified_badge": user.verified,
        "followers": user.followers,
        "external_url": user.url,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    athlete = handle.athlete
    if user.name and normalize(user.name) in name_variants(athlete.full_name) | {
        a.alias_normalized for a in athlete.aliases
    }:
        add_evidence(handle, {"kind": "profile_name", "detail": f"X-Profilname '{user.name}'", "url": url})
    bio = user.description or ""
    clubs = [normalize(a.team.name) for a in athlete.affiliations if a.team]
    if sport_verdict(bio).looks_hockey() or any(c and c in normalize(bio) for c in clubs) or "🇨🇭" in bio:
        add_evidence(
            handle, {"kind": "profile_bio", "detail": f"X-Bio nennt {wording().sport_de}/Team", "url": url}
        )
    recompute(handle)


def _budget(s: Session, settings: Settings, estimate: float) -> bool:
    try:
        costs.ensure_budget(s, SOURCE, estimate, settings.x_daily_cost_cap_usd)
        return True
    except costs.BudgetExceeded as exc:
        log.warning(str(exc))
        return False


def _record(s: Session, settings: Settings, client: XApiClient, report: XReport, note: str) -> None:
    cost = client.users_read * settings.x_cost_per_user_usd + client.posts_read * settings.x_cost_per_post_usd
    if cost:
        costs.record(s, SOURCE, client.users_read + client.posts_read, cost, note)
        report.cost_usd += cost
    client.users_read = client.posts_read = 0


def _store(ctx: IngestContext, post: XPost, links: list[Link], source_type: str, kind: str) -> bool:
    text = post.text + (f"\n(zitiert: {post.quoted_url})" if post.quoted_url else "")
    draft = ItemDraft(
        source_type=source_type,
        platform="x",
        url=post.url,
        text=text,
        source_name=f"@{post.author}",
        external_id=post.id,
        author=post.author,
        published_at=post.created_at,
        tagged=post.mentions,
        raw={"x_id": post.id, "lang": post.lang},
        source_kind=kind,
    )
    r = store_item(ctx, draft, links)
    if r.created and ctx.team_matcher is not None:
        for tl in ctx.team_matcher.from_text(post.text, post.url):
            if tl.team_id not in {t.team_id for t in r.item.teams}:
                r.item.teams.append(m.ItemTeam(team_id=tl.team_id))
    return r.created


def _team_matcher(ctx: IngestContext):
    if ctx.team_matcher is None:
        from klugscheisser.domain.team_matcher import TeamMatcher

        ctx.team_matcher = TeamMatcher(ctx.session, athlete_matcher=ctx.matcher)
    return ctx.team_matcher


def is_first_read(h: m.Handle) -> bool:
    """Never read before: the first read costs a profile lookup plus up to X_FIRST_RUN_POSTS posts."""
    return h.last_scraped_at is None


def ingest_x(
    ctx: IngestContext,
    client: XApiClient,
    athletes: list[m.Athlete],
    team_accounts: bool = True,
    first_read_ids: set[int] | None = None,
) -> XReport:
    """`first_read_ids` (nightly run): only these persons' never-read handles are read for the first time;
    None = everyone (on-demand updates)."""
    s, st = ctx.session, ctx.settings
    report = XReport()
    _team_matcher(ctx)
    for a in athletes:
        for h in [h for h in a.handles if h.platform == "x" and h.status != "rejected"]:
            if first_read_ids is not None and a.id not in first_read_ids and is_first_read(h):
                report.skipped_first_read.append(h.handle)
                continue
            if not (h.profile or {}).get("x_id") and not (h.profile or {}).get("missing"):
                if not _budget(s, st, st.x_cost_per_user_usd):
                    report.skipped_budget.append(h.handle)
                    continue
                try:
                    apply_x_profile(h, client.user(h.handle))
                    report.profiles_checked += 1
                except Exception as exc:
                    report.errors.append(f"@{h.handle}: {exc}")
                    continue
                finally:
                    _record(s, st, client, report, f"profile @{h.handle}")
            x_id = (h.profile or {}).get("x_id")
            if not h.usable or not x_id:
                continue
            n = st.x_first_run_posts if not h.last_seen_post_id else 20
            if not _budget(s, st, n * st.x_cost_per_post_usd):
                report.skipped_budget.append(h.handle)
                continue
            user = XUser(id=x_id, username=h.handle)
            try:
                posts = client.posts(user, since_id=h.last_seen_post_id, max_results=n)
            except Exception as exc:
                report.errors.append(f"@{h.handle}: {exc}")
                continue
            finally:
                _record(s, st, client, report, f"posts @{h.handle}")
            report.handles_read += 1
            for post in posts:
                links = [Link(a.id, "author", 1.0)] + [
                    x for x in ctx.matcher.from_text(post.text) if x.athlete_id != a.id
                ]
                report.posts_new += _store(ctx, post, links, "athlete_post", "self")
            if posts:
                h.last_seen_post_id = max(posts, key=lambda p: int(p.id)).id
            h.last_scraped_at = datetime.now(UTC)
            s.flush()
    if team_accounts:
        _team_accounts(ctx, client, report)
    return report


def _team_accounts(ctx: IngestContext, client: XApiClient, report: XReport) -> None:
    from klugscheisser.jobs.refresh import is_fresh, touch

    s, st = ctx.session, ctx.settings
    for account in st.x_team_accounts:
        if is_fresh(s, "x", account, 3 * 3600):  # official accounts: at most every 3 hours
            continue
        if not _budget(s, st, st.x_cost_per_user_usd + 20 * st.x_cost_per_post_usd):
            report.skipped_budget.append(account)
            continue
        last = s.scalar(
            select(func.max(m.Item.external_id)).where(m.Item.platform == "x", m.Item.author == account)
        )
        try:
            user = client.user(account)
            posts = client.posts(user, since_id=last, max_results=20) if user else []
        except Exception as exc:
            report.errors.append(f"@{account}: {exc}")
            continue
        finally:
            _record(s, st, client, report, f"team @{account}")
        for post in posts:
            report.team_posts_new += _store(
                ctx, post, ctx.matcher.from_text(post.text), "other_media", "official"
            )
        touch(s, "x", account)
        s.flush()


def x_for_update(session: Session, settings: Settings, athletes: list[m.Athlete]) -> dict:
    """Update-job source (freshness.SLOW_SOURCES): X posts of the given persons + team accounts."""
    if not settings.features.get("x"):
        return {"x": "aus (kein X_BEARER_TOKEN)"}
    from klugscheisser.ingest.runner import build_context

    report = ingest_x(build_context(session, settings), XApiClient(settings), athletes)
    return asdict(report)
