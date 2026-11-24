"""Instagram ingest: incremental per handle, cost-capped, profiles refreshed weekly. Plus the official team
accounts (settings.instagram_team_accounts): only new posts, stored as official items linked to the team."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.instagram.base import InstagramPost
from klugscheisser.collectors.instagram.sources import is_paid, likely_paid, paid_profile_count
from klugscheisser.db import models as m
from klugscheisser.domain.source_kind import kind_for
from klugscheisser.handles.profile import apply_profile
from klugscheisser.handles.service import add_evidence, flag_unavailable, usable_handles
from klugscheisser.ingest import costs
from klugscheisser.ingest.store import IngestContext, ItemDraft, store_item

log = logging.getLogger(__name__)

PROFILE_MAX_AGE = timedelta(days=7)
BATCH = 15
SRF_ACCOUNTS = {"srfsport", "srf", "srfnews", "srf_sport", "rtssport", "rsisport"}


@dataclass
class InstagramReport:
    handles: int = 0
    posts_seen: int = 0
    items_new: int = 0
    items_changed: int = 0
    cost_usd: float = 0.0
    team_posts_new: int = 0  # posts of official team accounts
    team_tags: int = 0  # handle candidates newly tagged by a team account (evidence "team_tags")
    skipped_budget: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)  # Bright Data: "This profile is private" (now skipped)
    review: list[str] = field(default_factory=list)  # Bright Data: "Profile isn't available" -> candidate


def post_source_type(post: InstagramPost, own_handles: set[str]) -> str:
    owner = post.owner_username.lower()
    if owner in own_handles:
        return "athlete_post"
    if owner in SRF_ACCOUNTS:
        return "srf"
    return "tagged_post"


def draft_from_post(post: InstagramPost, own_handles: set[str]) -> ItemDraft:
    return ItemDraft(
        source_type=post_source_type(post, own_handles),
        platform="instagram",
        url=post.url,
        text=post.caption,
        source_name=f"@{post.owner_username}",
        external_id=post.post_id,
        author=post.owner_username,
        published_at=post.published_at,
        image_description=post.informative_alt,
        media=post.media,
        media_type=post.media_type,
        tagged=sorted({*post.tagged, *post.mentions, *post.coauthors}),
        paid_partnership=post.paid_partnership,
        raw=post.raw,
    )


_AUTO = object()


def ingest_instagram(
    ctx: IngestContext,
    source,
    athletes: list[m.Athlete],
    max_posts: int = 12,
    refresh_profiles: bool = True,
    since=_AUTO,
    mentions: bool = False,
    batch_size: int | None = None,
) -> InstagramReport:
    """Own posts (default, incremental) — or with `mentions=True` posts of others that tag the handle.
    v8: `since=None` fetches the whole history up to `max_posts` (tier 1 backfill), a datetime fixes the start."""
    s: Session = ctx.session
    report = InstagramReport()
    handle_rows: dict[str, m.Handle] = {}
    for a in athletes:
        for h in usable_handles(a):
            if not (h.profile or {}).get("private"):
                handle_rows[h.handle] = h
    report.handles = len(handle_rows)
    all_known = set(ctx.matcher.by_handle)
    paid = is_paid(source)
    names = sorted(handle_rows)
    _load_graph_hints(source, handle_rows, ctx.settings)

    step = batch_size or BATCH
    for start in range(0, len(names), step):
        batch = names[start : start + step]
        rows = [handle_rows[n] for n in batch]
        seen = [r.last_scraped_at for r in rows if r.last_scraped_at]
        start_at = (
            since
            if since is not _AUTO
            else (
                min(seen) - timedelta(days=1)
                if len(seen) == len(rows)
                else datetime.now(UTC) - timedelta(days=ctx.settings.instagram_first_run_days)
            )
        )
        left = None
        if paid:
            # only handles that will go to Apify count (hybrid: Graph serves the others for free)
            charged = set(batch if mentions else likely_paid(source, batch))
            # v8: the profile's post count bounds what own posts can cost (history runs ask for up to 400)
            expected = sum(
                max_posts
                if mentions
                else min(max_posts, int((r.profile or {}).get("posts_count") or max_posts) + 3)
                for r in rows
                if r.handle in charged
            )
            estimate = expected * ctx.settings.apify_cost_per_post_usd * (1 if mentions else 2)
            try:
                left = costs.ensure_budget(s, "apify", estimate, ctx.settings.apify_daily_cost_cap_usd)
            except costs.BudgetExceeded as exc:
                if not _partly_free(source) or mentions:
                    log.warning(str(exc))
                    report.skipped_budget.extend(batch)
                    continue
                left = 0.0  # Apify cap reached: the hybrid still reads what Graph can, for free
        try:
            fetch = source.fetch_mentions if mentions else source.fetch_posts
            result = fetch(batch, start_at, max_posts, **({"max_usd": left} if paid else {}))
        except Exception as exc:
            report.errors.append(f"{batch}: {exc}")
            log.exception("instagram fetch failed")
            continue
        if result.missing and (not paid or (left is not None and left <= 0)):
            # Apify budget used up, or no Apify at all and the free sources could not read them
            log.warning("instagram: not read this time (no free source, no Apify): %s", result.missing)
            report.skipped_budget.extend(result.missing)
        if result.cost_usd:
            units = len(batch) if result.paid_handles is None else result.paid_handles
            costs.record(s, "apify", units, result.cost_usd, f"posts for {units} handles")
            report.cost_usd += result.cost_usd
        stored: set[str] = set()
        for handle, posts in result.posts.items():
            for post in posts:
                report.posts_seen += 1
                if post.url in stored:
                    continue
                stored.add(post.url)
                draft = draft_from_post(post, all_known)
                draft.source_kind = kind_for(
                    s,
                    platform="instagram",
                    source_type=draft.source_type,
                    author=post.owner_username,
                    own_handles=all_known,
                    author_full_name=post.owner_full_name,
                    sample_text=post.caption,
                    jev_client=ctx.jev_client,
                )
                links = ctx.matcher.from_post(
                    post.owner_username, post.tagged, post.mentions, post.coauthors, post.caption
                )
                r = store_item(ctx, draft, links)
                report.items_new += r.created
                report.items_changed += r.changed and not r.created
            row = handle_rows.get(handle)
            if row is not None and not mentions:
                row.last_scraped_at = datetime.now(UTC)
                own = [p for p in posts if p.owner_username == handle and p.published_at]
                if own:
                    row.last_seen_post_id = max(own, key=lambda p: p.published_at).post_id
        s.flush()

    if refresh_profiles:
        stale = [
            h
            for h in handle_rows.values()
            if not h.profile
            or datetime.fromisoformat(h.profile.get("fetched_at", "2000-01-01T00:00:00+00:00"))
            < datetime.now(UTC) - PROFILE_MAX_AGE
        ]
        if stale:
            try:
                expected = len(likely_paid(source, [h.handle for h in stale]))
                if expected:
                    costs.ensure_budget(
                        s,
                        "apify",
                        expected * ctx.settings.apify_cost_per_post_usd,
                        ctx.settings.apify_daily_cost_cap_usd,
                    )
                profiles = {p.username: p for p in source.fetch_profiles([h.handle for h in stale])}
                if n := paid_profile_count(source, len(stale)):
                    costs.record(s, "apify", n, n * ctx.settings.apify_cost_per_post_usd, "profiles")
                for h in stale:
                    if (p := profiles.get(h.handle)) is not None:
                        apply_profile(h, p)
            except Exception as exc:
                report.errors.append(f"profiles: {exc}")
        s.flush()
    _save_graph_hints(source, handle_rows)
    _save_page_states(source, handle_rows, report)
    s.flush()
    return report


def _save_page_states(source, rows: dict[str, m.Handle], report: InstagramReport) -> None:
    """What Bright Data's dead_page records said: "private" -> profile.private (skipped from now on, like a
    private profile from Apify), "unavailable" (renamed or deleted) -> handle back to candidate with evidence
    "ig_unavailable" for a manual check. "No public posts in the period" is just empty (not passed on)."""
    states = getattr(source, "page_states", None) or {}
    now = datetime.now(UTC).isoformat()
    for name, row in rows.items():
        state = states.get(name.lower())
        if state == "private":
            row.profile = {**(row.profile or {}), "private": True, "private_checked_at": now}
            report.private.append(name)
        elif state == "unavailable":
            if flag_unavailable(row, "Bright Data: Profil nicht verfügbar (umbenannt oder gelöscht?)"):
                report.review.append(name)
                log.warning("instagram: @%s not available (renamed/deleted?): back to candidate", name)


def _partly_free(source) -> bool:
    """Can this source still read something without paying (the hybrid's Graph part)?"""
    return hasattr(source, "set_graph_hints")


def _load_graph_hints(source, rows: dict[str, m.Handle], settings) -> None:
    """Tell the hybrid source what earlier runs learnt: graph_ok per handle, younger than the recheck time
    (an account may turn professional later, so a 'no' is asked again after META_GRAPH_RECHECK_DAYS)."""
    if not hasattr(source, "set_graph_hints"):
        return
    cutoff = datetime.now(UTC) - timedelta(days=settings.meta_graph_recheck_days)
    hints: dict[str, bool] = {}
    for name, row in rows.items():
        prof = row.profile or {}
        if not isinstance(prof.get("graph_ok"), bool) or not prof.get("graph_checked_at"):
            continue
        try:
            checked = datetime.fromisoformat(prof["graph_checked_at"])
        except (TypeError, ValueError):
            continue
        if prof["graph_ok"] or checked >= cutoff:  # a 'yes' stays a good guess; a 'no' expires
            hints[name] = prof["graph_ok"]
    source.set_graph_hints(hints)


def _save_graph_hints(source, rows: dict[str, m.Handle]) -> None:
    """Store what Graph said in this run in handle.profile (only for handles that already have a profile:
    an empty profile means 'not checked yet' elsewhere)."""
    status = getattr(source, "graph_status", None)
    if not status:
        return
    now = datetime.now(UTC).isoformat()
    for name, ok in status.items():
        row = rows.get(name)
        if row is not None and row.profile:
            row.profile = {**row.profile, "graph_ok": ok, "graph_checked_at": now}


# --- official team accounts --------------------------------------------------------------------------

TEAM_SOURCE = "instagram-team"  # cache_entries source: last fetch per team account
TEAM_MIN_GAP_S = 12 * 3600  # at most twice a day (the nightly run reads them once)


def team_since(s: Session, settings, account: str) -> datetime:
    """Start of the next fetch: one day before the last one (like persons), else INSTAGRAM_FIRST_RUN_DAYS."""
    entry = s.get(m.CacheEntry, (TEAM_SOURCE, account.lower()))
    if entry is not None:
        return entry.fetched_at - timedelta(days=1)
    return datetime.now(UTC) - timedelta(days=settings.instagram_first_run_days)


def team_for_account(s: Session, account: str, seeds: Path = Path("seeds/teams.yaml")) -> m.Team | None:
    """The team whose instagram_handle is this account. A database seeded before the handle was added gets it
    from seeds/teams.yaml (only that field, only for the team listed there)."""
    h = account.lower()
    team = s.scalar(select(m.Team).where(func.lower(m.Team.instagram_handle) == h))
    if team is not None or not seeds.exists():
        return team
    for t in (yaml.safe_load(seeds.read_text()) or {}).get("teams") or []:
        if str(t.get("instagram_handle") or "").lower() == h:
            team = s.scalar(select(m.Team).where(m.Team.key == t["key"]))
            if team is not None and not team.instagram_handle:
                team.instagram_handle = h
                s.flush()
            return team
    return None


def _official(s: Session, account: str, name: str | None) -> None:
    row = s.get(m.Account, ("instagram", account))
    if row is None:
        s.add(m.Account(platform="instagram", handle=account, kind="official", display_name=name))
    elif row.kind != "official":
        row.kind, row.decided_by, row.confidence = "official", "list", 1.0
    if row is not None and name and not row.display_name:
        row.display_name = name
    s.flush()


def _tag_evidence(s: Session, account: str, post: InstagramPost) -> int:
    """A team account tagging an account: evidence "team_tags" for that handle wherever it is a candidate
    (one per team account, not per post)."""
    tagged = {x.lower() for x in (*post.tagged, *post.coauthors)} - {account}
    if not tagged:
        return 0
    n = 0
    rows = s.scalars(
        select(m.Handle).where(
            m.Handle.platform == "instagram", m.Handle.handle.in_(tagged), m.Handle.status != "rejected"
        )
    )
    for h in rows:
        detail = f"markiert von @{account}"
        if any(e["kind"] == "team_tags" and e.get("detail") == detail for e in h.evidence):
            continue
        add_evidence(h, {"kind": "team_tags", "detail": detail, "url": post.url})
        n += 1
    return n


def ingest_instagram_teams(
    ctx: IngestContext, source, accounts: list[str] | None = None, min_gap_s: int | None = None
) -> InstagramReport:
    """New posts of the official team accounts: source kind "official", linked to the team (and to every
    known player tagged or named). Incremental, at most every TEAM_MIN_GAP_S (`min_gap_s`: the game watch's
    follow-up reads an account again when its last read was before the game), inside the Apify cap."""
    from klugscheisser.jobs.refresh import is_fresh, touch

    s, st = ctx.session, ctx.settings
    report = InstagramReport()
    paid = is_paid(source)
    all_known = set(ctx.matcher.by_handle)
    if ctx.team_matcher is None:
        from klugscheisser.domain.team_matcher import TeamMatcher

        ctx.team_matcher = TeamMatcher(s, athlete_matcher=ctx.matcher)
    matcher = ctx.team_matcher
    for account in [
        a.strip().lstrip("@").lower() for a in (st.instagram_team_accounts if accounts is None else accounts)
    ]:
        if not account or is_fresh(
            s, TEAM_SOURCE, account, TEAM_MIN_GAP_S if min_gap_s is None else min_gap_s
        ):
            continue
        report.handles += 1
        team = team_for_account(s, account)
        since = team_since(s, st, account)
        limit = st.instagram_team_max_posts
        kwargs = {}
        if paid:
            estimate = limit * st.apify_cost_per_post_usd if likely_paid(source, [account]) else 0.0
            try:
                kwargs["max_usd"] = costs.ensure_budget(s, "apify", estimate, st.apify_daily_cost_cap_usd)
            except costs.BudgetExceeded as exc:
                if not _partly_free(source):
                    log.warning(str(exc))
                    report.skipped_budget.append(account)
                    continue
                kwargs["max_usd"] = 0.0  # hybrid: Graph may still read the team account for free
        try:
            result = source.fetch_posts([account], since, limit, **kwargs)
        except Exception as exc:
            report.errors.append(f"@{account}: {exc}")
            log.exception("instagram team fetch failed")
            continue
        if account in result.missing:  # neither Graph nor (budget) Apify could read it
            report.skipped_budget.append(account)
            continue
        if result.cost_usd:
            costs.record(s, "apify", 1, result.cost_usd, f"team @{account}")
            report.cost_usd += result.cost_usd
        posts = result.posts.get(account, [])
        _official(s, account, next((p.owner_full_name for p in posts if p.owner_username == account), None))
        for post in posts:
            report.posts_seen += 1
            draft = draft_from_post(post, all_known)
            own = post.owner_username == account
            draft.source_kind = (
                "official"
                if own
                else kind_for(
                    s,
                    platform="instagram",
                    source_type=draft.source_type,
                    author=post.owner_username,
                    own_handles=all_known,
                    author_full_name=post.owner_full_name,
                    sample_text=post.caption,
                    jev_client=ctx.jev_client,
                )
            )
            links = ctx.matcher.from_post(
                post.owner_username, post.tagged, post.mentions, post.coauthors, post.caption
            )
            r = store_item(ctx, draft, links)
            if own:
                report.team_tags += _tag_evidence(s, account, post)
            teams = {t.team_id for t in r.item.teams}
            if own and team is not None and team.id not in teams:  # also a post stored earlier as a mention
                r.item.teams.append(m.ItemTeam(team_id=team.id))
                teams.add(team.id)
            if r.created:
                report.items_new += 1
                report.team_posts_new += own
                for tl in matcher.from_text(post.caption, post.url):
                    if tl.team_id not in teams:
                        r.item.teams.append(m.ItemTeam(team_id=tl.team_id))
                        teams.add(tl.team_id)
            else:
                report.items_changed += r.changed
        touch(s, TEAM_SOURCE, account)
        s.flush()
    return report
