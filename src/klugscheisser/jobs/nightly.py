"""Nightly refresh (v5, reworked 2026-10-11).

Runs once per night after `nightly_hour` (Europe/Zurich, default 05:00) in a daemon thread started by `serve`:
- every person of the tiers in NIGHTLY_TIERS whose turn it is tonight (tier 1 every night, tier 2 one seventh
  per night as a stable weekly rotation: person id mod 7 = day number mod 7), plus the persons of prepared
  events whose date range includes today;
- Instagram (Apify, grouped by last fetch so nobody's window grows), X (only incremental reads, first reads
  only for NIGHTLY_X_FIRST_READ_TIERS), profiles (Wikipedia/college, own 14-day TTL);
- once for everyone: news, results (SIHF/IIHF/PWHL/SDHL), the official X accounts (@SwissIceHockey) and the
  official Instagram team accounts (INSTAGRAM_TEAM_ACCOUNTS, e.g. @swissicehockeywomen; only new posts).
The cost is estimated first (`plan`); when an estimate is above its cap the paid part is skipped cleanly.
NIGHTLY_FULL_ONCE_DATE: on that local date one full run (all of NIGHTLY_FULL_TIERS, X first reads allowed, one
cap NIGHTLY_FULL_CAP_USD for Apify + X) starts at NIGHTLY_FULL_ONCE_HOUR and replaces that day's regular run.
`klugscheisser nightly --dry-run` prints the plan without writing or paying.
v11: then the polite Elite Prospects fetch (only inside EP_NIGHTLY_START_HOUR..EP_NIGHTLY_END_HOUR, 05:00–09:00,
once per day, with EP_ENABLED; with a start at/after NIGHTLY_HOUR only once that day's refresh is done, so it runs
right after it) and the importer for the fetched persons; the project backup comes last.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import TZ

log = logging.getLogger(__name__)

MARKER = ("nightly", "events")  # the regular run of a local day (name kept from v5)
RATE_DAYS = 90  # posting rate per account from the stored posts of the last 90 days
PROFILE_CHUNK = 10  # profiles are fetched in chunks; the run stops between chunks after NIGHTLY_END_HOUR


def full_marker(d: date) -> tuple[str, str]:
    return ("nightly", f"full:{d.isoformat()}")


def _local(now: datetime | None) -> datetime:
    return (now or datetime.now(TZ)).astimezone(TZ)


def full_due(s: Session, settings: Settings, now: datetime | None = None) -> bool:
    """The one-off full night: its date, from its hour on, not yet started."""
    now = _local(now)
    d = settings.nightly_full_once_date
    if d is None or now.date() != d or now.hour < settings.nightly_full_once_hour:
        return False
    return s.get(m.CacheEntry, full_marker(d)) is None


def due(s: Session, settings: Settings, now: datetime | None = None) -> bool:
    now = _local(now)
    if full_due(s, settings, now):
        return True
    if now.hour < settings.nightly_hour:
        return False
    last = s.get(m.CacheEntry, MARKER)
    return last is None or last.fetched_at.astimezone(TZ).date() < now.date()


# --- who and how much --------------------------------------------------------------------------------


def in_rotation(athlete_id: int, day: date, every: int) -> bool:
    """Stable rotation: with every=7 each person comes once a week, always on the same weekday."""
    return every <= 1 or athlete_id % every == day.toordinal() % every


def _tier_ids(s: Session, tier: int) -> list[int]:
    from klugscheisser import tiers

    return tiers.ids_in(s, tier)


def select_persons(s: Session, settings: Settings, day: date, full: bool = False) -> dict[str, list[int]]:
    """Tonight's persons per group (label -> ids). A person is listed in the first group that has her."""
    from klugscheisser.adds.event import nightly_persons

    groups: dict[str, list[int]] = {}
    seen: set[int] = set()

    def add(label: str, ids: list[int]) -> None:
        fresh = [i for i in ids if i not in seen]
        seen.update(fresh)
        if fresh:
            groups[label] = fresh

    if full:
        for tier in sorted(settings.nightly_full_tiers):
            add(f"tier {tier} (alle, einmalig)", _tier_ids(s, tier))
    for tier, every in sorted(settings.nightly_tiers.items()):
        ids = [i for i in _tier_ids(s, tier) if in_rotation(i, day, every)]
        add(f"tier {tier}" + (f" (1/{every})" if every > 1 else ""), ids)
    add("Anlässe", nightly_persons(s, day))
    return groups


@dataclass
class Plan:
    day: date
    full: bool
    groups: dict[str, list[int]]
    first_read_ids: list[int]  # persons whose never-read X handles may be read tonight
    ig_handles: int = 0
    ig_first: int = 0
    ig_buckets: list[list[int]] = field(default_factory=list)  # persons grouped by their last Instagram fetch
    ig_team: dict[str, float] = field(default_factory=dict)  # official Instagram team account -> estimate USD
    x_handles: int = 0
    x_first: int = 0
    x_first_skipped: int = 0
    profiles_due: int = 0
    apify_usd: float = 0.0
    x_usd: float = 0.0
    apify_cap: float = 0.0
    x_cap: float = 0.0
    total_cap: float | None = None
    apify_cycle_spent: float = 0.0  # ledger, since the start of the Apify billing cycle
    apify_cycle_cap: float = 0.0
    problems: list[str] = field(default_factory=list)

    @property
    def ids(self) -> list[int]:
        return [i for ids in self.groups.values() for i in ids]

    @property
    def ok(self) -> bool:
        return not self.problems

    def out(self) -> dict:
        return {
            "nacht": self.day.isoformat(),
            "art": "voll (einmalig)" if self.full else "regulär",
            "personen": len(self.ids),
            "gruppen": {k: len(v) for k, v in self.groups.items()},
            "instagram_accounts": self.ig_handles,
            "instagram_erstabrufe": self.ig_first,
            "instagram_teamaccounts_usd": {f"@{k}": round(v, 3) for k, v in self.ig_team.items()},
            "x_accounts": self.x_handles,
            "x_erstabrufe": self.x_first,
            "x_erstabrufe_ausgelassen": self.x_first_skipped,
            "profile_faellig": self.profiles_due,
            "schaetzung_usd": {
                "apify": round(self.apify_usd, 3),
                "x": round(self.x_usd, 3),
                "total": round(self.apify_usd + self.x_usd, 3),
            },
            "limiten_usd": {
                "apify": round(self.apify_cap, 3),
                "x": round(self.x_cap, 3),
                **({"total": self.total_cap} if self.total_cap is not None else {}),
            },
            "apify_zyklus_usd": {
                "bisher": round(self.apify_cycle_spent, 2),
                "limite": self.apify_cycle_cap,
            },
            **({"abbruch": self.problems} if self.problems else {}),
        }


def _rates(s: Session, platform: str, authors: set[str]) -> dict[str, float]:
    """Posts per day of each account (lower-case name) over the last RATE_DAYS."""
    if not authors:
        return {}
    since = datetime.now(UTC) - timedelta(days=RATE_DAYS)
    rows = s.execute(
        select(func.lower(m.Item.author), func.count())
        .where(
            m.Item.platform == platform,
            func.lower(m.Item.author).in_(authors),
            m.Item.published_at >= since,
        )
        .group_by(func.lower(m.Item.author))
    ).all()
    return {a: n / RATE_DAYS for a, n in rows}


def _days_since(day: date, when: datetime | None) -> int:
    return max(1, (day - when.astimezone(TZ).date()).days) if when else 1


def estimate(s: Session, settings: Settings, plan: Plan) -> Plan:
    """Fill the counts and the expected cost of `plan` (no request, no write)."""
    from klugscheisser.handles.service import usable_handles
    from klugscheisser.ingest.instagram import PROFILE_MAX_AGE
    from klugscheisser.ingest.profiles import CACHE_SOURCE
    from klugscheisser.ingest.x import is_first_read
    from klugscheisser.jobs.refresh import is_fresh

    athletes = [a for a in (s.get(m.Athlete, i) for i in plan.ids) if a is not None]
    first_ok = set(plan.first_read_ids)
    ig = {a.id: [h for h in usable_handles(a) if not (h.profile or {}).get("private")] for a in athletes}
    xs = {a.id: [h for h in a.handles if h.platform == "x" and h.status != "rejected"] for a in athletes}
    ig_rate = _rates(s, "instagram", {h.handle.lower() for hs in ig.values() for h in hs})
    x_rate = _rates(
        s,
        "x",
        {h.handle.lower() for hs in xs.values() for h in hs} | {a.lower() for a in settings.x_team_accounts},
    )
    from klugscheisser.collectors.instagram.sources import apify_used, graph_first

    # without Apify in the chosen source (e.g. Graph + Bright Data) Instagram costs nothing
    price = settings.apify_cost_per_post_usd if apify_used(settings) else 0.0
    profile_cutoff = datetime.now(UTC) - PROFILE_MAX_AGE
    graph = graph_first(settings)  # handles Graph read last time cost nothing (hybrid source)
    buckets: dict[date | None, list[int]] = {}
    for a in athletes:
        hs = ig[a.id]
        if not hs:
            continue
        seen = [h.last_scraped_at for h in hs]
        key = None if any(x is None for x in seen) else min(seen).astimezone(TZ).date()
        buckets.setdefault(key, []).append(a.id)
        for h in hs:
            plan.ig_handles += 1
            if h.last_scraped_at is None:
                plan.ig_first += 1
                count = int((h.profile or {}).get("posts_count") or 0)
                posts = min(12, count + 3) if count else 12
            else:
                # the fetch starts one day before the last one (ingest_instagram); at least one result
                days = _days_since(plan.day, h.last_scraped_at) + 1
                posts = min(12.0, max(1.0, ig_rate.get(h.handle.lower(), 0.0) * days))
            if graph and (h.profile or {}).get("graph_ok") is True:
                continue  # posts and profile come from Graph for free
            plan.apify_usd += posts * price
            fetched = (h.profile or {}).get("fetched_at")
            if not fetched or datetime.fromisoformat(fetched) < profile_cutoff:
                plan.apify_usd += price  # weekly profile refresh (one result)
    plan.ig_buckets = [buckets[k] for k in sorted(buckets, key=lambda d: d or date.min)]
    _estimate_ig_teams(s, settings, plan)
    if settings.features.get("x"):
        for a in athletes:
            for h in xs[a.id]:
                prof = h.profile or {}
                if prof.get("missing"):
                    continue
                if is_first_read(h):
                    if a.id not in first_ok:
                        plan.x_first_skipped += 1
                        continue
                    plan.x_handles += 1
                    plan.x_first += 1
                    # profile lookup; posts only for usable handles (a candidate is read only if the lookup
                    # makes it usable: the run's cap covers that)
                    plan.x_usd += (0 if prof.get("x_id") else settings.x_cost_per_user_usd) + (
                        settings.x_first_run_posts * settings.x_cost_per_post_usd if h.usable else 0
                    )
                    continue
                if not prof.get("x_id"):
                    plan.x_handles += 1
                    plan.x_usd += settings.x_cost_per_user_usd + 20 * settings.x_cost_per_post_usd
                    continue
                if not h.usable:
                    continue
                plan.x_handles += 1
                days = _days_since(plan.day, h.last_scraped_at)
                plan.x_usd += (
                    min(20.0, x_rate.get(h.handle.lower(), 0.0) * days) * settings.x_cost_per_post_usd
                )
        for account in settings.x_team_accounts:  # official accounts: one profile lookup + new posts
            entry = s.get(m.CacheEntry, ("x", account))
            days = _days_since(plan.day, entry.fetched_at if entry else None)
            posts = min(20.0, max(1.0, x_rate.get(account.lower(), 0.0) * days))
            plan.x_usd += settings.x_cost_per_user_usd + posts * settings.x_cost_per_post_usd
    if settings.profiles_enabled:
        ttl = settings.profiles_max_age_days * 86400
        plan.profiles_due = sum(1 for a in athletes if not is_fresh(s, CACHE_SOURCE, f"athlete:{a.id}", ttl))
    return plan


def _estimate_ig_teams(s: Session, settings: Settings, plan: Plan) -> None:
    """Official Instagram team accounts: first read up to INSTAGRAM_TEAM_MAX_POSTS, afterwards the posting
    rate times the days since the last fetch (+1, the fetch starts a day early); at least one result."""
    from klugscheisser.collectors.instagram.sources import apify_used
    from klugscheisser.ingest.instagram import TEAM_SOURCE

    accounts = [a.strip().lstrip("@").lower() for a in settings.instagram_team_accounts if a.strip()]
    rate = _rates(s, "instagram", set(accounts))
    limit = float(settings.instagram_team_max_posts)
    for account in accounts:
        entry = s.get(m.CacheEntry, (TEAM_SOURCE, account))
        if entry is None:
            posts = limit
        else:
            days = _days_since(plan.day, entry.fetched_at) + 1
            posts = min(limit, max(1.0, rate.get(account, 0.0) * days))
        plan.ig_team[account] = posts * settings.apify_cost_per_post_usd if apify_used(settings) else 0.0
        plan.apify_usd += plan.ig_team[account]


def cycle_start(day: date, start_day: int) -> date:
    """First day of the Apify billing cycle that contains `day`."""
    start_day = max(1, min(start_day, 28))
    if day.day >= start_day:
        return day.replace(day=start_day)
    prev = day.replace(day=1) - timedelta(days=1)
    return prev.replace(day=start_day)


def apify_cycle_spent(s: Session, settings: Settings, day: date) -> float:
    start = cycle_start(day, settings.apify_cycle_start_day)
    since = datetime(start.year, start.month, start.day, tzinfo=TZ)
    return float(
        s.scalar(
            select(func.coalesce(func.sum(m.CostLedger.cost_usd), 0.0)).where(
                m.CostLedger.source == "apify", m.CostLedger.created_at >= since
            )
        )
    )


def plan(s: Session, settings: Settings, now: datetime | None = None, full: bool | None = None) -> Plan:
    """Tonight's persons, counts, cost estimate and caps. Reads only."""
    from klugscheisser.ingest import costs

    now = _local(now)
    if full is None:
        full = full_due(s, settings, now)
    groups = select_persons(s, settings, now.date(), full=full)
    first_tiers = set(settings.nightly_full_tiers if full else settings.nightly_x_first_read_tiers)
    first_ids = [i for tier in sorted(first_tiers) for i in _tier_ids(s, tier)]
    first_ids += groups.get("Anlässe", [])  # prepared events: everything, as before
    p = estimate(s, settings, Plan(now.date(), full, groups, first_ids))
    if full:
        p.total_cap = settings.nightly_full_cap_usd
        p.apify_cap = min(settings.nightly_cap_usd, p.total_cap)
        p.x_cap = max(0.0, p.total_cap - p.apify_cap)
    else:
        p.apify_cap = settings.nightly_cap_usd
        p.x_cap = costs.remaining(s, "x", settings.x_daily_cost_cap_usd)
    p.apify_cycle_cap = settings.apify_cycle_cap_usd
    p.apify_cycle_spent = apify_cycle_spent(s, settings, p.day)
    if p.apify_cycle_cap and p.apify_cycle_spent + p.apify_usd > p.apify_cycle_cap:
        p.problems.append(
            f"Apify-Monat: bisher ${p.apify_cycle_spent:.2f} + Schätzung ${p.apify_usd:.2f} über "
            f"${p.apify_cycle_cap:.2f}"
        )
    if p.apify_usd > p.apify_cap:
        p.problems.append(f"Apify: Schätzung ${p.apify_usd:.2f} über Limite ${p.apify_cap:.2f}")
    if p.x_usd > p.x_cap:
        p.problems.append(f"X: Schätzung ${p.x_usd:.2f} über Limite ${p.x_cap:.2f}")
    if p.total_cap is not None and p.apify_usd + p.x_usd > p.total_cap:
        p.problems.append(f"Total: Schätzung ${p.apify_usd + p.x_usd:.2f} über Limite ${p.total_cap:.2f}")
    return p


def run_settings(s: Session, settings: Settings, p: Plan) -> Settings:
    """Settings for the run: the plan's caps on top of what was already spent in the last 24 h (the ledger
    caps are rolling 24 h). Only the full night raises the X cap."""
    from klugscheisser.ingest import costs

    update = {"apify_daily_cost_cap_usd": costs.spent_today(s, "apify") + p.apify_cap}
    if p.full:
        update["x_daily_cost_cap_usd"] = costs.spent_today(s, "x") + p.x_cap
    return settings.model_copy(update=update)


# --- the run -----------------------------------------------------------------------------------------


def _merge(reports: list[dict]) -> dict:
    out: dict = {}
    for r in reports:
        for k, v in r.items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
            elif isinstance(v, list):
                out[k] = [*out.get(k, []), *v]
    return out


def _step(result: dict, name: str, fn) -> None:
    from klugscheisser.sports import current_spec

    if name in current_spec().nightly_skip:  # 2026-11-24: ice-hockey-only sources in a floorball project
        result[name] = {"status": f"übersprungen ({current_spec().name_de})"}
        return
    try:
        result[name] = fn()
    except Exception as exc:  # one source failing must not stop the others
        log.exception("nightly step %s failed", name)
        result[name] = {"fehler": str(exc)}
        result.setdefault("fehler", []).append(f"{name}: {exc}")


def _instagram(p: Plan, settings: Settings) -> dict:
    from klugscheisser.ingest.runner import run_instagram

    reports = []
    for ids in p.ig_buckets:  # same last fetch -> same window, so tier 1 doesn't fetch a week of tier 2 posts
        with session_scope() as s:
            reports.append(asdict(run_instagram(s, settings, [s.get(m.Athlete, i) for i in ids])))
    if p.ig_team:  # official team accounts (in the plan's estimate and caps)
        from klugscheisser.collectors.instagram.sources import get_instagram_source
        from klugscheisser.ingest.instagram import ingest_instagram_teams
        from klugscheisser.ingest.runner import build_context

        with session_scope() as s:
            report = ingest_instagram_teams(
                build_context(s, settings), get_instagram_source(settings), list(p.ig_team)
            )
            reports.append(asdict(report))
    return _merge(reports)


def _x(p: Plan, settings: Settings) -> dict:
    if not settings.features.get("x"):
        return {"x": "aus (kein X_BEARER_TOKEN)"}
    from klugscheisser.collectors.x_api import XApiClient
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.ingest.x import ingest_x

    with session_scope() as s:
        athletes = [a for a in (s.get(m.Athlete, i) for i in p.ids) if a is not None]
        report = ingest_x(
            build_context(s, settings),
            XApiClient(settings),
            athletes,
            team_accounts=True,
            first_read_ids=set(p.first_read_ids),
        )
        return asdict(report)


def _news(settings: Settings) -> dict:
    from klugscheisser.collectors.rss import RssCollector
    from klugscheisser.ingest.news import ingest_news
    from klugscheisser.ingest.runner import build_context
    from klugscheisser.jobs.refresh import is_fresh, touch

    with session_scope() as s:
        if is_fresh(s, "news", "all", settings.ttl_news):
            return {"status": "aktuell"}
        report = asdict(ingest_news(build_context(s, settings), RssCollector()))
        touch(s, "news", "all")
        return report


def _results(settings: Settings) -> dict:
    from klugscheisser.ingest.results import results_for_update

    with session_scope() as s:
        return results_for_update(s, settings, [])


def _season_totals(settings: Settings) -> dict:
    """Current season totals (SIHF league tables, national team from the Leijonat sheets): stats/current.py."""
    if not settings.season_totals_enabled:
        return {"status": "aus (SEASON_TOTALS_ENABLED=false)"}
    from klugscheisser.stats.current import refresh

    with session_scope() as s:
        return refresh(s, settings)


def _roster_watch() -> dict:
    """Official WCh roster announcements (free: one listing page per federation, see jobs/roster_watch.py)."""
    from klugscheisser.jobs.roster_watch import run

    with session_scope() as s:
        return run(s)


def _hockeycanada(settings: Settings) -> dict:
    """Official tournament rosters on hockeycanada.ca (free, ~11 pages; facts/hockeycanada_import.nightly)."""
    from klugscheisser.facts.hockeycanada_import import nightly

    with session_scope() as s:
        return nightly(s, settings)


def _german_evidence(settings: Settings) -> dict:
    """German interviews of roster persons in new items (facts/german_evidence.py; cached per item and person)."""
    from klugscheisser.facts.german_evidence import nightly

    with session_scope() as s:
        return nightly(s, settings)


def _profiles(p: Plan, settings: Settings, clock=None) -> dict:
    from klugscheisser.ingest.profiles import profiles_for_update

    clock = clock or (lambda: datetime.now(TZ))
    begin = clock().astimezone(TZ)
    # a run started in the night stops at NIGHTLY_END_HOUR; a run started by hand during the day does not
    deadline = (
        begin.replace(hour=settings.nightly_end_hour, minute=0, second=0, microsecond=0)
        if begin.hour < settings.nightly_end_hour
        else None
    )
    reports, stopped = [], None
    ids = p.ids
    for start in range(0, len(ids), PROFILE_CHUNK):
        if deadline is not None and clock().astimezone(TZ) >= deadline:
            stopped = f"nach {settings.nightly_end_hour}:00 gestoppt, {len(ids) - start} Personen in den nächsten Nächten"
            break
        with session_scope() as s:
            athletes = [s.get(m.Athlete, i) for i in ids[start : start + PROFILE_CHUNK]]
            reports.append(profiles_for_update(s, settings, [a for a in athletes if a is not None]))
    out = _merge(reports)
    if stopped:
        out["gestoppt"] = stopped
    return out


def _floorball(settings: Settings, max_requests: int = 300) -> dict:
    """2026-11-24: free floorball sources for the tournament's players (collectors/floorball, request cap):
    Swiss Unihockey, SSL (ssl.se) and F-Liiga (fliiga.com) season stats, then outdated open stints ended."""
    from klugscheisser import projects
    from klugscheisser.collectors.floorball import run as floorball_run
    from klugscheisser.collectors.floorball import torneopal
    from klugscheisser.seed.floorball import event_athletes

    out: dict = {}
    with session_scope() as s:  # schedule + results of the tournament (official results service)
        out["resultate"] = torneopal.update(s, projects.current().main_event)
    with session_scope() as s:
        ids = [a.id for a in event_athletes(s)]
        out.update(
            floorball_run.enrich(
                s, ids, sources=("swissunihockey", "ssl", "fliiga"), dry_run=False, max_requests=max_requests
            )
        )
    return out


def _run_nightly_job(job_id: int, p: Plan, settings: Settings) -> None:
    from klugscheisser.sports import current_spec

    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.status, job.started_at = "running", datetime.now(UTC)
    result: dict = {}
    _step(result, "instagram", lambda: _instagram(p, settings))
    _step(result, "x", lambda: _x(p, settings))
    _step(result, "news", lambda: _news(settings))
    _step(result, "results", lambda: _results(settings))
    _step(result, "saisonwerte", lambda: _season_totals(settings))
    _step(result, "kaderwatch", _roster_watch)
    _step(result, "hockeycanada", lambda: _hockeycanada(settings))
    _step(result, "profiles", lambda: _profiles(p, settings))
    _step(result, "deutsch_interviews", lambda: _german_evidence(settings))
    if current_spec().family == "unihockey":
        _step(result, "floorball", lambda: _floorball(settings))
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        job.result, job.finished_at = result, datetime.now(UTC)
        job.status = "failed" if result.get("fehler") else "done"
        job.error = "; ".join(result.get("fehler", [])) or None


def run_nightly(settings: Settings, now: datetime | None = None, full: bool | None = None) -> dict:
    """One nightly run (also by hand: `klugscheisser nightly`). The full night falls back to a regular run
    when its estimate is above its cap."""
    from klugscheisser import tiers

    now = _local(now)
    with session_scope() as s:
        tiers.recompute(s)  # v8: rosters/stints may have changed during the day
    with session_scope() as s:
        is_full = full_due(s, settings, now) if full is None else full
        p = plan(s, settings, now, full=is_full)
        aborted: list[str] = []
        if is_full and not p.ok:
            log.warning("nightly full run aborted: %s", p.problems)
            aborted = [f"Volllauf abgebrochen: {x}" for x in p.problems]
            p = plan(s, settings, now, full=False)
        for key in [MARKER] + ([full_marker(now.date())] if is_full else []):
            entry = s.get(m.CacheEntry, key) or m.CacheEntry(source=key[0], target=key[1])
            entry.fetched_at = now.astimezone(UTC)
            s.merge(entry)
        params = {"athlete_ids": p.ids, "plan": p.out(), **({"abgebrochen": aborted} if aborted else {})}
        if not p.ok:  # over a cap: nothing paid tonight
            s.add(m.Job(kind="nightly", status="failed", params=params, error="; ".join(p.problems)))
            log.warning("nightly refresh skipped: %s", p.problems)
            return {**p.out(), "status": "abgebrochen", **({"abgebrochen": aborted} if aborted else {})}
        local = run_settings(s, settings, p)
        job = m.Job(kind="nightly", params=params)
        s.add(job)
        s.flush()
        job_id = job.id
    log.info("nightly refresh: %s", p.out())
    _run_nightly_job(job_id, p, local)
    with session_scope() as s:
        job = s.get(m.Job, job_id)
        log.info("nightly refresh: %s persons, status %s", len(p.ids), job.status)
        return {
            **p.out(),
            "job_id": job_id,
            "status": job.status,
            **({"abgebrochen": aborted} if aborted else {}),
        }


def last_runs(s: Session, limit: int = 3) -> list[dict]:
    """The newest nightly jobs (for `klugscheisser nightly --last`)."""
    jobs = s.scalars(select(m.Job).where(m.Job.kind == "nightly").order_by(m.Job.id.desc()).limit(limit))
    return [
        {
            "job_id": j.id,
            "status": j.status,
            "gestartet": j.started_at.astimezone(TZ).isoformat(timespec="minutes") if j.started_at else None,
            "fertig": j.finished_at.astimezone(TZ).isoformat(timespec="minutes") if j.finished_at else None,
            "plan": (j.params or {}).get("plan"),
            "ergebnis": j.result,
            "fehler": j.error,
        }
        for j in jobs
    ]


# --- v11: Elite Prospects ---------------------------------------------------------------------------

EP_MARKER = ("nightly", "eliteprospects")


def ep_window(settings: Settings, now: datetime) -> tuple[datetime, datetime]:
    """Local [EP_NIGHTLY_START_HOUR, EP_NIGHTLY_END_HOUR) of `now`'s day (05:00–09:00 since 2026-10-25, was
    03:00–05:00 before the refresh)."""
    local = now.astimezone(TZ)
    start = local.replace(hour=settings.ep_nightly_start_hour, minute=0, second=0, microsecond=0)
    end_hour = settings.ep_nightly_end_hour
    end = (
        local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if end_hour > settings.ep_nightly_start_hour
        else start + timedelta(hours=2)
    )
    return start, end


def nightly_done_today(s: Session, now: datetime) -> bool:
    """Today's regular nightly refresh has started (marker) and no nightly job is still running (one started
    more than 6 h ago counts as crashed)."""
    last = s.get(m.CacheEntry, MARKER)
    if last is None or last.fetched_at.astimezone(TZ).date() < now.date():
        return False
    running = s.scalar(
        select(func.count())
        .select_from(m.Job)
        .where(
            m.Job.kind == "nightly",
            m.Job.status == "running",
            m.Job.started_at >= now - timedelta(hours=6),  # a crashed run never blocks EP for good
        )
    )
    return not running


def ep_due(s: Session, settings: Settings, now: datetime | None = None) -> bool:
    """Once per local day, inside the nightly window, only with EP_ENABLED; a window that starts at/after the
    nightly hour also waits for that day's refresh to be done (EP right after it, never in parallel)."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if not settings.ep_enabled:
        return False
    start, end = ep_window(settings, now)
    if not start <= now < end:
        return False
    if settings.ep_nightly_start_hour >= settings.nightly_hour and not nightly_done_today(s, now):
        return False
    last = s.get(m.CacheEntry, EP_MARKER)
    return last is None or last.fetched_at.astimezone(TZ).date() < now.date()


def import_fetched(settings: Settings, athlete_ids: list[int]) -> dict | None:
    """Turn the freshly fetched `ep_profiles.raw` of these persons into facts, stats and stints — plus every
    page still waiting from an earlier run (imported_at missing or older than fetched_at), so a failed import
    is retried the next night instead of waiting for the next fetch."""
    from sqlalchemy import or_, select

    from klugscheisser.db import models as m
    from klugscheisser.ep_import import import_profiles

    with session_scope() as s:
        waiting = s.scalars(
            select(m.EpProfile.athlete_id).where(
                or_(m.EpProfile.imported_at.is_(None), m.EpProfile.imported_at < m.EpProfile.fetched_at)
            )
        ).all()
        ids = list(dict.fromkeys([*athlete_ids, *waiting]))
        if not ids:
            return None
        return import_profiles(s, athlete_ids=ids)


def run_ep_nightly(settings: Settings, now: datetime | None = None, **fetch_kwargs) -> dict:
    """One nightly EP run: due pages (EP_SCOPE: tier 1 daily, tier 2 weekly, after games … or every player of
    one event's rosters) until the window ends, then the importer for the fetched persons. By hand: `klugscheisser ep fetch` (no window there)."""
    from klugscheisser.collectors.eliteprospects.fetch import fetch_profiles

    now = (now or datetime.now(TZ)).astimezone(TZ)
    with session_scope() as s:
        entry = s.get(m.CacheEntry, EP_MARKER) or m.CacheEntry(source=EP_MARKER[0], target=EP_MARKER[1])
        entry.fetched_at = now.astimezone(UTC)
        s.merge(entry)
    with session_scope() as s:
        report = fetch_profiles(s, settings, until=ep_window(settings, now)[1], **fetch_kwargs)
    out = report.out()
    log.info("nightly elite prospects: %s", out)
    out["import"] = import_fetched(settings, report.fetched)
    return out


def scheduled_projects():
    """v7: background schedules only for active projects that want them (not archived, not "Allgemein")."""
    from klugscheisser import projects

    return [p for p in projects.active_projects() if p.scheduled_on()]  # 2026-11-24: scheduled_until


def start_nightly_worker(settings: Settings | None = None, check_every_s: float = 600.0) -> threading.Thread:
    """Nightly refresh per scheduled project (each with its own settings: nightly hour, cap)."""
    from klugscheisser import projects
    from klugscheisser.config import get_settings

    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            for p in scheduled_projects():
                with projects.use(p):
                    try:
                        local = get_settings()
                        with session_scope() as s:
                            run = due(s, local)
                        if run:
                            run_nightly(local)
                    except Exception:
                        log.exception("nightly refresh failed (%s)", p.key)
                    try:  # v11: Elite Prospects pages after the refresh, before the backup
                        from klugscheisser.sports import current_spec

                        with session_scope() as s:
                            ep_run = "elite_prospects" not in current_spec().nightly_skip and ep_due(s, local)
                        if ep_run:
                            run_ep_nightly(local)
                    except Exception:
                        log.exception("nightly Elite Prospects fetch failed (%s)", p.key)
                    try:
                        if run:
                            from klugscheisser.projects_admin import backup

                            backup(p)  # v7: nightly dump per project database
                    except Exception:
                        log.exception("nightly backup failed (%s)", p.key)
            for p in projects.active_projects():  # v7: weekly "Was wir gelernt haben" suggestion
                with projects.use(p):
                    try:
                        from klugscheisser import briefing

                        with session_scope() as s:
                            if briefing.due(s):
                                briefing.suggest(s, get_settings())
                    except Exception:
                        log.exception("briefing suggestion failed (%s)", p.key)
            stop.wait(check_every_s)

    thread = threading.Thread(target=loop, name="nightly-refresh", daemon=True)
    thread.start()
    return thread


def start_news_poller(settings: Settings | None = None, tick_s: float = 60.0) -> threading.Thread | None:
    """v5: read all news feeds every `news_poll_minutes`, independent of questions and of Instagram — every
    person in the database gets linked news (name matching), light documents stay searchable.
    v7: per scheduled project, each with its own interval (0 = off for that project)."""
    from klugscheisser import projects
    from klugscheisser.config import get_settings

    def loop() -> None:
        from klugscheisser.collectors.rss import RssCollector
        from klugscheisser.ingest.news import ingest_news
        from klugscheisser.ingest.runner import build_context
        from klugscheisser.jobs.refresh import is_fresh, touch

        stop = threading.Event()
        while not stop.is_set():
            for p in scheduled_projects():
                with projects.use(p):
                    try:
                        local = get_settings()
                        if local.news_poll_minutes <= 0:
                            continue
                        with session_scope() as s:
                            if is_fresh(s, "news", "all", local.news_poll_minutes * 60 - 30):
                                continue
                            r = ingest_news(build_context(s, local), RssCollector())
                            touch(s, "news", "all")
                        log.info(
                            "news poll %s: %s seen, %s relevant, %s new", p.key, r.seen, r.womens, r.items_new
                        )
                    except Exception:
                        log.exception("news poll failed (%s)", p.key)
            stop.wait(tick_s)

    thread = threading.Thread(target=loop, name="news-poller", daemon=True)
    thread.start()
    return thread
