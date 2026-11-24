"""Results ingest: GameResult -> Event (every game) + Item (every final game) with athlete/team links.

Idempotent: events are keyed "<collector>-<external id>", items by their public URL; re-runs update
score/status and only rewrite an item when its text changes. Games already stored as final are not
fetched again in detail (the collector gets their ids via `known_final`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.matcher import Link
from klugscheisser.ingest.store import IngestContext, ItemDraft, store_item
from klugscheisser.interfaces import GameResult, Participant, ResultsCollector

log = logging.getLogger(__name__)

SOURCE_TYPE = "official_result"
RESULTS_LOCK = 0x6B6C7265  # advisory lock id ("klre"): one results refresh at a time


@dataclass
class ResultsReport:
    games: int = 0
    events_new: int = 0
    events_updated: int = 0
    items_new: int = 0
    items_changed: int = 0
    athlete_links: int = 0
    duplicates_skipped: int = 0  # same game already stored from another source
    adopted: int = 0  # rows of a fallback source taken over by a better source
    tournaments: int = 0  # tournament containers created or updated from the games
    per_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def current_season(tz: str = "Europe/Zurich", today=None) -> int:
    """Season alias of today (August to July): 2026-10-18 -> 2027 (= 2026/27)."""
    from klugscheisser.collectors.results.base import season_of

    return season_of(today or datetime.now(ZoneInfo(tz)).date())


def iihf_events(settings: Settings) -> list:
    """The current IIHF events of the regular run (IIHF_EVENT_ID + IIHF_EVENTS without a game-id range); the game
    watch (jobs/gamewatch.py) watches the same ones."""
    from klugscheisser.collectors.results.iihf import IihfEvent

    event = IihfEvent(settings.iihf_event_id, settings.iihf_event_path, settings.iihf_site_game_id_offset)
    return [event, *(IihfEvent.from_config(c) for c in settings.iihf_events if not c.get("game_ids"))]


def enabled_collectors(settings: Settings) -> list[ResultsCollector]:
    from klugscheisser.collectors.results.iihf import IihfCollector
    from klugscheisser.collectors.results.leijonat import LeijonatCollector
    from klugscheisser.collectors.results.pwhl import PwhlCollector
    from klugscheisser.collectors.results.sdhl import SdhlCollector
    from klugscheisser.collectors.results.sihf import SihfCollector
    from klugscheisser.collectors.results.sihf_nt import NtTeam, SihfNtCollector

    delay, mx = settings.results_request_delay_s, settings.results_max_details
    out: list[ResultsCollector] = []
    if settings.sihf_data_enabled:
        out.append(
            SihfCollector(settings.sihf_league_id, settings.sihf_season, delay_s=delay, max_details=mx)
        )
    if settings.iihf_realtime_enabled:
        events = iihf_events(settings)
        out.append(IihfCollector(events if len(events) > 1 else events[0], delay_s=delay, max_details=mx))
    if settings.pwhl_enabled:
        out.append(PwhlCollector(settings.pwhl_feed_key, settings.pwhl_teams, delay_s=delay, max_details=mx))
    if settings.sdhl_enabled:
        out.append(SdhlCollector(settings.sdhl_schedule_id, delay_s=delay, max_details=mx))
    # national teams: after IIHF (its WCh rows come first), the sihf.ch pages last (fallback source)
    if settings.leijonat_enabled:
        skip = settings.leijonat_skip_when_iihf if settings.iihf_realtime_enabled else None
        out.append(
            LeijonatCollector(
                settings.leijonat_season or current_season(settings.timezone),
                settings.leijonat_level_ids,
                skip=skip or None,
                delay_s=delay,
                max_details=mx,
            )
        )
    if settings.sihf_nt_enabled:
        teams = [NtTeam(**t) for t in settings.sihf_nt_teams]
        out.append(SihfNtCollector(teams, delay_s=delay))
    return out


def backfill_collectors(settings: Settings, only: list[str] | None = None) -> list[ResultsCollector]:
    """Past tournaments (one-time / by hand): IIHF events by realtime game-id range + Wikipedia tournament
    pages. `only`: competition keys to run (default: all configured). Never part of the nightly run."""
    from klugscheisser.collectors.results.iihf import IihfCollector, IihfEvent
    from klugscheisser.collectors.results.wiki_games import WikiGamesCollector, WikiTournament

    def keep(cfg: dict) -> bool:
        return not only or cfg.get("competition_key") in only

    delay = settings.results_request_delay_s
    out: list[ResultsCollector] = []
    events = [IihfEvent.from_config(c) for c in settings.iihf_backfill_events if keep(c)]
    if events:
        out.append(IihfCollector(events, delay_s=delay))
    pages = [WikiTournament.from_config(c) for c in settings.wiki_backfill_pages if keep(c)]
    if pages:
        out.append(WikiGamesCollector(pages, delay_s=delay))
    return out


# --- helpers ----------------------------------------------------------------------------------------


def name_candidates(name: str) -> list[str]:
    """The name as given plus both rotations ('Rüedi Lisa' <-> 'Lisa Rüedi'), since sources differ in
    the order of given and family names."""
    parts = name.split()
    out = [name]
    if len(parts) >= 2:
        out += [" ".join(parts[1:] + parts[:1]), " ".join(parts[-1:] + parts[:-1])]
    return list(dict.fromkeys(out))


def find_team(session: Session, name: str | None, code: str | None, cache: dict) -> m.Team | None:
    key = (name, code)
    if key in cache:
        return cache[key]
    team = None
    if name:
        team = repo.find_team(session, name, fuzzy=False)
    if team is None and code:
        team = repo.find_team(session, code, fuzzy=False)
    if team is None and name and len(name) >= 6:
        team = repo.find_team(session, name, fuzzy=True)
    cache[key] = team
    return team


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def swiss_summary(session: Session, per_athlete: dict[int, list[Participant]]) -> list[str]:
    """'Lisa Rüedi (2 Tore)' for every linked athlete with Swiss nationality."""
    lines = []
    for aid, parts in per_athlete.items():
        athlete = session.get(m.Athlete, aid)
        if athlete is None or athlete.nationality not in ("SUI", None):
            continue
        goals = sum(p.role == "scorer" for p in parts)
        assists = sum(p.role == "assist" for p in parts)
        bits = []
        if goals:
            bits.append(_plural(goals, "Tor", "Tore"))
        if assists:
            bits.append(_plural(assists, "Assist", "Assists"))
        for p in parts:
            if p.role == "goalie":
                bits.append(f"Torhüterin{': ' + p.detail if p.detail else ''}")
                break
        lines.append(f"{athlete.full_name} ({', '.join(bits) or 'im Einsatz'})")
    return sorted(lines)


def game_text(game: GameResult, tz: str, swiss: list[str]) -> str:
    when = game.starts_at.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y") if game.starts_at else None
    head = f"{game.competition}, {when}: " if when else f"{game.competition}: "
    line = f"{head}{game.home} – {game.away} {game.score or ''}".rstrip()
    if game.period_scores:
        line += f" ({', '.join(game.period_scores)})"
    out = [line + "."]
    goals = game.raw.get("goals") or []
    if goals:
        rendered = []
        for g in goals:
            assists = f" ({', '.join(g['assists'])})" if g.get("assists") else ""
            time = f" {g['time']}" if g.get("time") else ""
            rendered.append(f"{g['score']}{time} {g.get('scorer') or '?'}{assists}")
        out.append("Tore: " + "; ".join(rendered) + ".")
    goalies = [p for p in game.participants if p.role == "goalie"]
    if goalies:
        out.append(
            "Torhüterinnen: "
            + "; ".join(
                f"{p.name} ({', '.join(filter(None, [p.team, p.detail]))})"
                if (p.team or p.detail)
                else p.name
                for p in goalies
            )
            + "."
        )
    if game.venue:
        out.append(f"Ort: {game.venue}.")
    if swiss:
        out.append("Schweizerinnen: " + "; ".join(swiss) + ".")
    return "\n".join(out)


def trimmed_raw(game: GameResult) -> dict:
    lineup: dict[str, list[str]] = defaultdict(list)
    for p in game.participants:
        if p.role == "lineup":
            lineup[p.team or "?"].append(p.name)
    return {
        "source": game.source,
        "competition": game.competition,
        "external_id": game.external_id,
        "status": game.status,
        "home": game.home,
        "away": game.away,
        "score": game.score,
        "period_scores": game.period_scores,
        "venue": game.venue,
        "goals": game.raw.get("goals") or [],
        "participants": [asdict(p) for p in game.participants if p.role != "lineup"],
        "lineup": dict(lineup),
        "spectators": game.raw.get("spectators"),
        **({"player_stats": game.raw["player_stats"]} if game.raw.get("player_stats") else {}),
        **({"sheet_players": game.raw["sheet_players"]} if game.raw.get("sheet_players") else {}),
    }


# --- ingest -----------------------------------------------------------------------------------------


def ensure_competition(session: Session, game: GameResult) -> m.Competition | None:
    """The game's competition by name; collectors that pass `raw["competition_key"]` (national-team games: one
    competition per tournament) get the row created when it is missing."""
    comp = session.scalar(select(m.Competition).where(m.Competition.name == game.competition))
    key = game.raw.get("competition_key")
    if comp is not None or not key:
        return comp
    comp = session.scalar(select(m.Competition).where(m.Competition.key == key))
    if comp is None:
        comp = m.Competition(
            sport_id=repo.get_sport(session, game.sport).id,
            key=key[:80],
            name=game.competition[:200],
            kind=game.raw.get("competition_kind") or "tournament",
            season=game.raw.get("season"),
        )
        session.add(comp)
        session.flush()
    return comp


def _local_day(dt: datetime | None, tz: str):
    return dt.astimezone(ZoneInfo(tz)).date() if dt else None


def same_game(session: Session, game: GameResult, cache: dict, tz: str) -> m.Event | None:
    """A stored game of another source on the same local day between the same two teams (either way round)."""
    if game.starts_at is None:
        return None
    home = find_team(session, game.home, game.raw.get("home_code"), cache)
    away = find_team(session, game.away, game.raw.get("away_code"), cache)
    if home is None or away is None:
        return None
    day = _local_day(game.starts_at, tz)
    pair = {home.id, away.id}
    rows = session.scalars(
        select(m.Event).where(
            m.Event.kind == "game",
            m.Event.home_team_id.in_(pair),
            m.Event.away_team_id.in_(pair),
            m.Event.starts_at >= game.starts_at - timedelta(days=1),
            m.Event.starts_at <= game.starts_at + timedelta(days=1),
        )
    )
    for ev in rows:
        if {ev.home_team_id, ev.away_team_id} == pair and _local_day(ev.starts_at, tz) == day:
            return ev
    return None


FALLBACK_COLLECTORS = {"sihfnt"}  # their rows give way to any other source (collector attribute `fallback`)


def _is_fallback_key(key: str) -> bool:
    return key.split("-", 1)[0] in FALLBACK_COLLECTORS


def adopt(session: Session, ev: m.Event, new_key: str) -> None:
    """A better source found a game first stored from a fallback source: the row gets the new key (so links to
    the event stay) and the fallback's score-only result item is removed (the new source writes its own)."""
    from sqlalchemy import delete

    if ev.source_url:
        session.execute(
            delete(m.Item).where(
                m.Item.url == ev.source_url,
                m.Item.source_type == SOURCE_TYPE,
                m.Item.platform == ev.key.split("-", 1)[0],
            )
        )
    ev.key = new_key
    session.flush()


def _key_exists(session: Session, key: str) -> bool:
    return session.scalar(select(m.Event.id).where(m.Event.key == key)) is not None


def sync_tournaments(session: Session, pairs: list[tuple[GameResult, m.Event]], tz: str) -> int:
    """Tournament containers from the games (`raw["tournament"]` = {prefix, key, name, city}): the matching
    stored container (same key, or same prefix with an overlapping date span) gets its end date, start date
    (only moved earlier), city, competition, status and teams from the games; none found -> created.
    Answers find a tournament's games by competition name or by the container's date span (answer/games.py)."""
    zone = ZoneInfo(tz)
    groups: dict[str, list[tuple[GameResult, m.Event]]] = defaultdict(list)
    for game, ev in pairs:
        info = game.raw.get("tournament")
        if info and info.get("key") and ev.starts_at is not None:
            groups[info["key"]].append((game, ev))
    touched = 0
    for key, members in groups.items():
        info = members[0][0].raw["tournament"]
        evs = [ev for _, ev in members]
        first = min(ev.starts_at for ev in evs).astimezone(zone)
        last = max(ev.starts_at for ev in evs).astimezone(zone)
        start = datetime.combine(first.date(), datetime.min.time(), tzinfo=zone)
        end = datetime.combine(last.date(), datetime.max.time().replace(microsecond=0), tzinfo=zone)
        team_ids = {t for ev in evs for t in (ev.home_team_id, ev.away_team_id) if t}
        container = session.scalar(select(m.Event).where(m.Event.key == key, m.Event.kind != "game"))
        if container is None:
            best, best_score = None, -1
            for c in session.scalars(
                select(m.Event).where(m.Event.kind != "game", m.Event.key.startswith(f"{info['prefix']}-"))
            ):
                if c.starts_at is None:
                    continue
                c_end = c.ends_at or c.starts_at + timedelta(days=7)
                if c.starts_at > end + timedelta(days=3) or c_end < start - timedelta(days=3):
                    continue
                c_teams = set(
                    session.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == c.id))
                )
                shared = len(c_teams & team_ids)
                if c_teams and shared < min(2, len(team_ids)):
                    continue  # a parallel tournament of other teams
                if shared > best_score:
                    best, best_score = c, shared
            container = best
        changed = False
        if container is None:
            container = m.Event(
                key=key[:120],
                kind="tournament",
                name=info.get("name") or key,
                status="scheduled",
            )
            session.add(container)
            changed = True
        values = {
            "starts_at": start if container.starts_at is None or container.starts_at > start else None,
            "ends_at": end if container.ends_at is None or container.ends_at < end else None,
            "city": info.get("city") if not container.city else None,
            "competition_id": evs[0].competition_id if not container.competition_id else None,
            "status": "final"
            if all(ev.status == "final" for ev in evs) and container.status != "final"
            else None,
        }
        for attr, value in values.items():
            if value is not None and getattr(container, attr) != value:
                setattr(container, attr, value)
                changed = True
        session.flush()
        linked = set(session.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == container.id)))
        for tid in sorted(team_ids - linked):
            session.add(m.EventTeam(event_id=container.id, team_id=tid))
            changed = True
        session.flush()
        touched += changed
    return touched


def upsert_event(
    session: Session, collector_name: str, game: GameResult, cache: dict
) -> tuple[m.Event, bool, bool]:
    """Returns (event, created, changed)."""
    key = f"{collector_name}-{game.external_id}"
    ev = session.scalar(select(m.Event).where(m.Event.key == key))
    created = False
    if ev is None:
        # two writers (web chat job + MCP job) may meet here: INSERT … ON CONFLICT DO NOTHING waits for the other
        # transaction and then keeps its row instead of failing with UniqueViolation events_key_key
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        new_id = session.scalar(
            pg_insert(m.Event)
            .values(key=key, kind="game", name=f"{game.home} – {game.away}")
            .on_conflict_do_nothing(index_elements=[m.Event.key])
            .returning(m.Event.id)
        )
        created = new_id is not None
        ev = session.scalar(select(m.Event).where(m.Event.key == key))
    comp = ensure_competition(session, game)
    home = find_team(session, game.home, game.raw.get("home_code"), cache)
    away = find_team(session, game.away, game.raw.get("away_code"), cache)
    values = {
        "name": f"{game.home} – {game.away}",
        "competition_id": comp.id if comp else ev.competition_id,
        "starts_at": game.starts_at or ev.starts_at,
        "ends_at": _parse_dt(game.raw.get("end")) or ev.ends_at,
        "venue": (game.venue or ev.venue or "")[:200] or None,
        "city": (game.raw.get("city") or ev.city or "")[:100] or None,
        "home_team_id": home.id if home else ev.home_team_id,
        "away_team_id": away.id if away else ev.away_team_id,
        "source": game.source,
        "source_url": game.url,
    }
    # never move a final game back to live/scheduled (e.g. a stale list response)
    if not (ev.status == "final" and game.status != "final"):
        values["status"] = game.status
        values["score"] = game.score or ev.score
    changed = created
    for attr, value in values.items():
        if getattr(ev, attr) != value:
            setattr(ev, attr, value)
            changed = True
    session.flush()
    return ev, created, changed


def link_participants(
    ctx: IngestContext, game: GameResult
) -> tuple[list[Link], dict[int, list[Participant]]]:
    links: dict[int, Link] = {}
    per_athlete: dict[int, list[Participant]] = defaultdict(list)
    for p in game.participants:
        for candidate in name_candidates(p.name):
            found = ctx.matcher.from_text(candidate)
            if found:
                for link in found:
                    links.setdefault(link.athlete_id, link)
                    per_athlete[link.athlete_id].append(p)
                break
    return list(links.values()), dict(per_athlete)


def store_game_item(ctx: IngestContext, collector_name: str, game: GameResult, ev: m.Event):
    links, per_athlete = link_participants(ctx, game)
    text = game_text(game, ctx.settings.timezone, swiss_summary(ctx.session, per_athlete))
    draft = ItemDraft(
        source_type=SOURCE_TYPE,
        platform=collector_name,
        url=game.url,
        text=text,
        source_name=game.source,
        external_id=game.external_id,
        author=game.source,
        title=f"{game.home} – {game.away} {game.score or ''}".strip(),
        published_at=ev.ends_at or game.starts_at,
        raw=trimmed_raw(game),
        source_kind="result",
    )
    r = store_item(ctx, draft, links)
    existing = {t.team_id for t in r.item.teams}
    for team_id in (ev.home_team_id, ev.away_team_id):
        if team_id and team_id not in existing:
            r.item.teams.append(m.ItemTeam(team_id=team_id))
            existing.add(team_id)
    ctx.session.flush()
    return r, len(links)


def ingest_results(
    ctx: IngestContext,
    collectors: list[ResultsCollector],
    since: datetime | None = None,
    teams: list[str] | None = None,
) -> ResultsReport:
    s = ctx.session
    report = ResultsReport()
    if since is None:
        since = datetime.now(UTC) - timedelta(days=ctx.settings.results_lookback_days)
    cache: dict = {}
    for collector in collectors:
        known = set(
            s.scalars(
                select(m.Item.external_id).where(
                    m.Item.platform == collector.name, m.Item.source_type == SOURCE_TYPE
                )
            )
        )
        if hasattr(collector, "known_final"):
            collector.known_final = known
        try:
            games = collector.collect(since=since, teams=teams)
        except Exception as exc:  # collectors should not raise; guard anyway
            log.exception("results collector %s failed", collector.name)
            report.errors.append(f"{collector.name}: {exc}")
            continue
        report.per_source[collector.source_label] = len(games)
        fallback = getattr(collector, "fallback", False)
        pairs: list[tuple[GameResult, m.Event]] = []
        for game in games:
            report.games += 1
            try:
                with s.begin_nested():
                    key = f"{collector.name}-{game.external_id}"
                    if (
                        not _key_exists(s, key)
                        and (dup := same_game(s, game, cache, ctx.settings.timezone)) is not None
                    ):
                        if fallback or not _is_fallback_key(dup.key):
                            report.duplicates_skipped += 1  # the other source keeps the game
                            continue
                        adopt(s, dup, key)
                        report.adopted += 1
                    ev, created, changed = upsert_event(s, collector.name, game, cache)
                    pairs.append((game, ev))
                    report.events_new += created
                    report.events_updated += changed and not created
                    if game.status == "final" and game.raw.get("detail"):
                        r, n_links = store_game_item(ctx, collector.name, game, ev)
                        report.items_new += r.created
                        report.items_changed += r.changed and not r.created
                        report.athlete_links += n_links
            except Exception as exc:
                log.exception("results: %s %s failed", collector.name, game.external_id)
                report.errors.append(f"{collector.name} {game.external_id}: {exc}")
        try:
            with s.begin_nested():
                report.tournaments += sync_tournaments(s, pairs, ctx.settings.timezone)
        except Exception as exc:
            log.exception("results: tournaments of %s failed", collector.name)
            report.errors.append(f"{collector.name} tournaments: {exc}")
    return report


def results_for_update(session: Session, settings: Settings, athletes: list[m.Athlete]) -> dict:
    """Hook for the daily/on-demand update job: refresh results with a TTL (short while games are live)."""
    from datetime import UTC, timedelta

    from sqlalchemy import select

    from klugscheisser.ingest.runner import build_context
    from klugscheisser.jobs.refresh import is_fresh, touch
    from klugscheisser.sports import current_spec

    if "results" in current_spec().nightly_skip:
        # 2026-11-24: the results collectors (SIHF, IIHF, PWHL, SDHL, Leijonat) are women's ice hockey; in a
        # floorball project a question's on-demand update wrote their games onto the men's floorball national
        # teams. Floorball results come from the project's own nightly step.
        return {"status": f"übersprungen ({current_spec().name_de})"}
    live = session.scalar(select(m.Event.id).where(m.Event.kind == "game", m.Event.status == "live").limit(1))
    ttl = settings.ttl_results_live if live else settings.ttl_results
    if is_fresh(session, "results", "all", ttl):
        return {"status": "aktuell"}
    # one results refresh at a time per project database (held until the caller's transaction ends)
    if not session.scalar(sql_text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": RESULTS_LOCK}):
        return {"status": "läuft bereits"}
    if is_fresh(session, "results", "all", ttl):  # the other writer may have just finished
        return {"status": "aktuell"}
    since = datetime.now(UTC) - timedelta(days=settings.results_lookback_days)
    report = ingest_results(build_context(session, settings), enabled_collectors(settings), since=since)
    touch(session, "results", "all")
    return asdict(report)
