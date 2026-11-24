"""v11: polite fetch of Elite Prospects player pages into `ep_profiles.raw` (the importer turns them into data).

EP's terms forbid scraping; robots.txt allows `/player/`. Private project, Ondřej accepts the risk of a block
(2026-10-04, docs/build-plan-v11-eliteprospects.md "Polite fetching"). Every rule is a setting (EP_*):
- One request at a time (a second run waits for the first: `running` + heartbeat in `source_state`), a random
  gap of 6–9 s between two requests (also across runs), a normal browser user agent, Accept-Language en,
  no login, no cookies, only `https://www.eliteprospects.com/player/…` (redirects elsewhere are not followed).
- 403 / 429 / captcha page → stop at once and pause every EP read for 7 days (`source_state.blocked_until`).
- 5xx / timeouts / connection errors → wait 30 s, 60 s, 120 s (Retry-After is honoured, a longer one than
  EP_RETRY_AFTER_MAX_S stops instead), then stop for the rest of the local day.
- 404 / page without player data → noted per person (tried again after EP_FAILED_RETRY_DAYS), the run goes on;
  5 in a row → stop for the day (the page layout may have changed).
- At most EP_DAILY_PAGE_CAP requests per local day (Europe/Zurich), retries and redirects included.
- Only who is due: tier 1 daily, tier 2 weekly, tier 3 monthly (EP_TIER3), never fetched first within a tier;
  plus persons whose team played since their last fetch (games from the results collectors).
- EP_SCOPE=event:<key> (2026-10-25, Ondřej's go for event:wch-2026) replaces the tier rule: only the players on
  that event's rosters (provisional + confirmed, role athlete), each once per local day, never fetched first,
  then oldest; persons without an EP id are counted, not looked up. From the second day after the event's end
  nothing is read any more (the scope switches itself off).

What is stored: the page's data blocks (`KEEP`) from `__NEXT_DATA__` → props.pageProps, same format as
`data/eliteprospects/raw/<id>.json` without the id/name/tier wrapper; `ep_profiles.data` / `imported_at` belong
to the importer. The state is committed after every request, so a crash never loses a block or the page count.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ

log = logging.getLogger(__name__)

SOURCE = "eliteprospects"
HOST = "www.eliteprospects.com"
BASE_URL = f"https://{HOST}"
KEEP = [
    "playerData",
    "initialLeagueStats",
    "initialCupStats",
    "initialCareerTotals",
    "initialAwards",
    "initialTransactions",
    "initialGameLog",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}
BLOCK_CODES = {401, 403, 429}
TRANSIENT_CODES = {408}  # plus every 5xx
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
RUNNING_STALE = timedelta(minutes=15)  # longer than the longest wait between two heartbeats
GAME_LOOKBACK = timedelta(days=14)  # "after a game" looks at final games of the last two weeks
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# only looked for when a 200 page has no __NEXT_DATA__ (a real player page always has it)
_CAPTCHA_MARKERS = (
    "captcha",
    "challenge-platform",
    "cf-chl",
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
)
EVERY_LABEL = {1: "täglich", 2: "wöchentlich", 3: "monatlich"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def player_url(ep_id: str) -> str:
    return f"{BASE_URL}/player/{ep_id}"


def allowed_url(url: str) -> bool:
    """Only player pages on the main host (robots.txt allows /player/; everything else stays untouched)."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == HOST and parts.path.startswith("/player/")


def looks_like_captcha(page: str) -> bool:
    low = page.lower()
    return any(marker in low for marker in _CAPTCHA_MARKERS)


def extract_blocks(page: str) -> dict[str, Any] | None:
    """The kept page data blocks, or None when the page has no (readable) player data."""
    mt = _NEXT_DATA.search(page)
    if not mt:
        return None
    try:
        props = json.loads(mt.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(props, dict) or not (props.get("playerData") or {}).get("player"):
        return None
    return {k: props.get(k) for k in KEEP}


def retry_after_s(value: str | None, now: datetime) -> float | None:
    """Retry-After as seconds (number or HTTP date); None when missing or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def local_day(dt: datetime) -> date:
    return dt.astimezone(TZ).date()


def _label(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def current_season(now: datetime) -> tuple[int, str]:
    """(start year, "2026-27") of the season running at `now` (a season starts in August)."""
    d = local_day(now)
    start = d.year if d.month >= 8 else d.year - 1
    return start, f"{start}-{(start + 1) % 100:02d}"


# --- state ------------------------------------------------------------------------------------------


def state_row(s: Session, create: bool = True) -> m.SourceState | None:
    row = s.get(m.SourceState, SOURCE)
    if row is None and create:
        row = m.SourceState(source=SOURCE, state={})
        s.add(row)
        s.flush()
    return row


def day_state(raw: dict | None, today: date) -> dict:
    """The stored state with the per-day counters reset when the local day changed."""
    state = copy.deepcopy(raw or {})  # nested dicts too: the loaded value must stay untouched
    if state.get("day") != today.isoformat():
        state.update(day=today.isoformat(), pages=0, stopped=None)
    state.setdefault("failed", {})
    return state


def blocked(row: m.SourceState | None, now: datetime) -> bool:
    return bool(row and row.blocked_until and row.blocked_until > now)


def status_line(s: Session, settings: Settings, now: datetime | None = None) -> str:
    """One German line for `klugscheisser features` and the sources page."""
    now = now or _utcnow()
    row = state_row(s, create=False)
    if blocked(row, now):
        reason = (row.state or {}).get("block_reason") or "gesperrt"
        return f"Elite Prospects: pausiert bis {_label(row.blocked_until)} ({reason})"
    if not settings.ep_enabled:
        return "Elite Prospects: aus (EP_ENABLED)"
    state = day_state(row.state if row else None, local_day(now))
    kind, key = parse_scope(settings.ep_scope)
    scope = f" (nur Kader {key})" if kind == "event" else ""
    line = f"Elite Prospects: an{scope}, heute {state['pages']}/{settings.ep_daily_page_cap} Seiten"
    if state.get("stopped"):
        line += f", für heute gestoppt ({state['stopped']})"
    return line


def pause_notice(s: Session, now: datetime | None = None) -> str | None:
    """The "pausiert bis …" text while EP is blocked, else None."""
    now = now or _utcnow()
    row = state_row(s, create=False)
    if not blocked(row, now):
        return None
    reason = (row.state or {}).get("block_reason") or "gesperrt"
    return f"pausiert bis {_label(row.blocked_until)} ({reason})"


# --- who is due -------------------------------------------------------------------------------------


@dataclass
class DuePerson:
    athlete_id: int
    ep_id: str
    name: str
    tier: int
    fetched_at: datetime | None
    reason: str
    url: str | None = None  # the page's last final URL (/player/<id>/<slug>): one request instead of two


def page_url(ep_id: str, stored: str | None) -> str:
    """The stored final URL when it is this id's player page, else /player/<id> (EP redirects that to
    /player/<id>/<slug>, and every hop counts against the daily cap)."""
    if stored and allowed_url(stored) and urlsplit(stored).path.startswith(f"/player/{ep_id}/"):
        return stored
    return player_url(ep_id)


@dataclass
class Scope:
    """Who EP_SCOPE says is read: every tier rule ("tiers") or only the players of one event's rosters."""

    kind: str = "tiers"  # tiers | event
    key: str | None = None
    name: str | None = None
    roster: list[int] = field(default_factory=list)  # athlete ids (role athlete) with an EP id
    without_ep_id: list[tuple[int, str]] = field(default_factory=list)  # (athlete id, name)
    problem: str | None = None  # set: nothing is read (unknown value, event missing or over)

    @property
    def label(self) -> str:
        return f"Kader {self.key}" if self.kind == "event" else "Tiers"


def parse_scope(value: str | None) -> tuple[str, str | None]:
    """("tiers", None) or ("event", key); ("invalid", value) for anything else."""
    v = (value or "tiers").strip()
    if v.lower() == "tiers":
        return "tiers", None
    if v.lower().startswith("event:") and v[6:].strip():
        return "event", v[6:].strip()
    return "invalid", v


def resolve_scope(s: Session, value: str | None, now: datetime) -> Scope:
    kind, key = parse_scope(value)
    if kind == "tiers":
        return Scope()
    if kind == "invalid":
        return Scope(kind="event", key=key, problem=f"EP_SCOPE={key!r} unbekannt (tiers oder event:<key>)")
    scope = Scope(kind="event", key=key)
    ev = s.scalars(select(m.Event).where(m.Event.key == key)).first()
    if ev is None:
        scope.problem = f"EP_SCOPE: Ereignis {key} nicht gefunden"
        return scope
    scope.name = ev.name
    end = ev.ends_at or ev.starts_at
    if end is not None and (local_day(now) - local_day(end)).days > 1:
        scope.problem = (
            f"{ev.name} ist vorbei ({local_day(end):%d.%m.%Y}), EP_SCOPE={value} liest nichts mehr"
        )
        return scope
    rows = s.execute(
        select(m.Athlete.id, m.Athlete.full_name, m.Athlete.eliteprospects_id)
        .join(m.EventEntry, m.EventEntry.athlete_id == m.Athlete.id)
        .where(m.EventEntry.event_id == ev.id, m.Athlete.role == "athlete")
        .distinct()
        .order_by(m.Athlete.id)
    ).all()
    for aid, name, ep_id in rows:
        if ep_id and str(ep_id).strip():
            scope.roster.append(aid)
        else:
            scope.without_ep_id.append((aid, name))
    return scope


def _every_days(settings: Settings, tier: int) -> int:
    return {1: settings.ep_tier1_every_days, 2: settings.ep_tier2_every_days}.get(
        tier, settings.ep_tier3_every_days
    )


def last_games(s: Session, athlete_ids: list[int], now: datetime) -> dict[int, datetime]:
    """Latest final game (start time, last two weeks) per person: games of her current teams (affiliations of
    this season or open-ended, open career stints) and result items she is linked to (line-ups)."""
    if not athlete_ids:
        return {}
    since = now - GAME_LOOKBACK
    start_year, season = current_season(now)
    today = local_day(now)
    teams = select(m.Affiliation.athlete_id, m.Affiliation.team_id).where(
        m.Affiliation.athlete_id.in_(athlete_ids),
        or_(
            m.Affiliation.season == season,
            and_(
                m.Affiliation.season.is_(None),
                or_(m.Affiliation.valid_to.is_(None), m.Affiliation.valid_to >= today),
            ),
        ),
    )
    stints = select(m.CareerStint.athlete_id, m.CareerStint.team_id).where(
        m.CareerStint.athlete_id.in_(athlete_ids),
        m.CareerStint.team_id.is_not(None),
        m.CareerStint.status != "conflict",
        or_(m.CareerStint.season_to.is_(None), m.CareerStint.season_to >= start_year),
        or_(m.CareerStint.season_from.is_(None), m.CareerStint.season_from <= start_year),
    )
    team_of: dict[int, set[int]] = {}
    for aid, team_id in [*s.execute(teams).all(), *s.execute(stints).all()]:
        team_of.setdefault(team_id, set()).add(aid)
    out: dict[int, datetime] = {}

    def note(aid: int, when: datetime | None) -> None:
        if when is not None and (aid not in out or when > out[aid]):
            out[aid] = when

    if team_of:
        games = s.execute(
            select(m.Event.home_team_id, m.Event.away_team_id, m.Event.starts_at).where(
                m.Event.kind == "game",
                m.Event.status == "final",
                m.Event.starts_at >= since,
                m.Event.starts_at <= now,
                or_(m.Event.home_team_id.in_(team_of), m.Event.away_team_id.in_(team_of)),
            )
        ).all()
        for home, away, starts_at in games:
            for team_id in (home, away):
                for aid in team_of.get(team_id, ()):
                    note(aid, starts_at)
    lineups = s.execute(
        select(m.ItemAthlete.athlete_id, func.max(m.Item.published_at))
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .where(
            m.ItemAthlete.athlete_id.in_(athlete_ids),
            m.Item.source_type == "official_result",
            m.Item.published_at >= since,
            m.Item.published_at <= now,
        )
        .group_by(m.ItemAthlete.athlete_id)
    ).all()
    for aid, when in lineups:
        note(aid, when)
    return out


def due_persons(
    s: Session,
    settings: Settings,
    athlete_ids: list[int] | None = None,
    tiers: list[int] | None = None,
    force: bool = False,
    now: datetime | None = None,
    failed: dict | None = None,
    scope: Scope | None = None,
) -> list[DuePerson]:
    """Persons with an EP id whose page is due, ordered by tier, then never fetched, then oldest fetch.

    Explicit `athlete_ids` ignore the tier filter and the scope; `force` ignores "due" (and the retry pause after
    a page without data) but nothing else. An event `scope`: only its roster, every page once per local day."""
    now = now or _utcnow()
    today = local_day(now)
    tier = func.coalesce(m.Athlete.tier_manual, m.Athlete.tier)
    q = (
        select(
            m.Athlete.id,
            m.Athlete.eliteprospects_id,
            m.Athlete.full_name,
            tier,
            m.EpProfile.fetched_at,
            m.EpProfile.url,
        )
        .outerjoin(m.EpProfile, m.EpProfile.athlete_id == m.Athlete.id)
        .where(m.Athlete.eliteprospects_id.is_not(None), m.Athlete.role == "athlete")
    )
    by_event = scope is not None and scope.kind == "event" and not athlete_ids
    if athlete_ids:
        q = q.where(m.Athlete.id.in_(athlete_ids))
    elif by_event:
        q = q.where(m.Athlete.id.in_(scope.roster or [-1]))
    else:
        wanted = set(tiers or [1, 2, 3])
        if not settings.ep_tier3:
            wanted.discard(3)
        q = q.where(tier.in_(wanted or {0}))
    rows = s.execute(q).all()
    games = last_games(s, [r[0] for r in rows], now) if settings.ep_after_game and not by_event else {}
    failed = failed or {}
    out: list[DuePerson] = []
    for aid, ep_id, name, t, fetched_at, stored_url in rows:
        t = t or 3
        reason = None
        if fetched_at is None:
            reason = "neu"
        elif by_event:
            if local_day(fetched_at) < today:
                reason = f"{scope.label}: täglich"
        elif (today - local_day(fetched_at)).days >= _every_days(settings, t):
            reason = f"Tier {t}: {EVERY_LABEL.get(t, 'monatlich')}"
        elif (game := games.get(aid)) is not None and game > fetched_at:
            reason = f"Spiel am {game.astimezone(TZ).strftime('%d.%m.')}"
        fail = failed.get(str(aid))
        if not force:
            if reason is None:
                continue
            if fail and (today - date.fromisoformat(fail["day"])).days < settings.ep_failed_retry_days:
                continue
        elif reason is None:
            reason = "erzwungen"
        ep_id = str(ep_id).strip()
        out.append(DuePerson(aid, ep_id, name, t, fetched_at, reason, page_url(ep_id, stored_url)))
    oldest = datetime.min.replace(tzinfo=UTC)
    # event scope: no tier order, so a roster larger than the daily cap rotates (oldest page first)
    out.sort(
        key=lambda d: (
            0 if by_event else d.tier,
            d.fetched_at is not None,
            d.fetched_at or oldest,
            d.athlete_id,
        )
    )
    return out


# --- fetching ---------------------------------------------------------------------------------------


@dataclass
class FetchReport:
    dry_run: bool = False
    scope: str = "Tiers"
    due: list[DuePerson] = field(default_factory=list)
    without_ep_id: list[tuple[int, str]] = field(default_factory=list)  # event scope: roster without EP id
    fetched: list[int] = field(default_factory=list)
    failed: list[tuple[int, str, str]] = field(default_factory=list)  # (athlete_id, name, reason)
    stopped: str | None = None  # why the run ended early / did nothing
    blocked_until: datetime | None = None
    pages_today: int = 0
    requests: int = 0
    waits: list[float] = field(default_factory=list)  # backoff waits (s)

    def out(self) -> dict[str, Any]:
        return {
            "umfang": self.scope,
            "fällig": len(self.due),
            "ohne_ep_id": len(self.without_ep_id),
            "geholt": len(self.fetched),
            "ohne_daten": len(self.failed),
            "anfragen": self.requests,
            "seiten_heute": self.pages_today,
            "gestoppt": self.stopped,
            "pausiert_bis": self.blocked_until.isoformat() if self.blocked_until else None,
        }

    def lines(self, cap: int | None = None) -> list[str]:
        out = []
        if self.dry_run:
            out.append(f"Elite Prospects (Probelauf, keine Anfrage, {self.scope}): {len(self.due)} fällig")
            if cap is not None and len(self.due) > cap - self.pages_today:
                out.append(f"  Tageslimit: heute nur noch {max(cap - self.pages_today, 0)} Seiten")
            out += [f"  {d.athlete_id:5} {d.name} (Tier {d.tier}): {d.reason}" for d in self.due]
            if self.without_ep_id:
                out.append(f"Ohne EP-ID (nicht geholt): {len(self.without_ep_id)}")
                out += [f"  {aid:5} {name}" for aid, name in self.without_ep_id]
        else:
            limit = f"/{cap}" if cap else ""
            out.append(
                f"Elite Prospects: {len(self.fetched)} geholt, {len(self.failed)} ohne Daten, "
                f"{self.requests} Anfragen, heute {self.pages_today}{limit} Seiten"
            )
            out += [f"  {aid:5} {name}: {why}" for aid, name, why in self.failed]
            if self.without_ep_id:
                out.append(f"  {len(self.without_ep_id)} Personen im Kader ohne EP-ID (nicht geholt)")
        if self.stopped:
            out.append(f"Gestoppt: {self.stopped}")
        return out


class _Block(Exception):
    def __init__(self, reason: str, retry_after: float | None = None, detail: dict | None = None) -> None:
        super().__init__(reason)
        self.reason, self.retry_after, self.detail = reason, retry_after, detail


def block_detail(r: httpx.Response) -> dict:
    """What the blocking answer looked like (never the page itself): for telling a bot wall from a ban."""
    text = r.text or ""
    title = _TITLE.search(text)
    return {
        "status": r.status_code,
        "bytes": len(text),
        "title": re.sub(r"\s+", " ", title.group(1)).strip()[:120] if title else None,
        "captcha": looks_like_captcha(text),
        "server": r.headers.get("server"),
    }


class _Stop(Exception):
    def __init__(self, reason: str, for_day: bool) -> None:
        super().__init__(reason)
        self.reason, self.for_day = reason, for_day


def default_client(settings: Settings) -> httpx.Client:
    # redirects are followed by hand (only to /player/ pages); cookies are cleared before every request
    return httpx.Client(timeout=settings.ep_timeout_s, follow_redirects=False, headers=HEADERS)


class _Run:
    def __init__(self, s, settings, row, state, http, clock, sleep, rng, report) -> None:
        self.s, self.settings, self.row, self.state = s, settings, row, state
        self.http, self.clock, self.sleep, self.rng, self.report = http, clock, sleep, rng, report
        last = state.get("last_request_at")
        self.last: datetime | None = datetime.fromisoformat(last) if last else None

    def save(self) -> None:
        self.state["heartbeat"] = self.clock().isoformat()
        self.row.state = copy.deepcopy(self.state)  # JSON columns only notice a new value
        self.report.pages_today = self.state["pages"]
        self.s.commit()

    def _gap(self) -> None:
        if self.last is None:
            return
        remaining = (
            self.rng.uniform(self.settings.ep_min_gap_s, self.settings.ep_max_gap_s)
            - (self.clock() - self.last).total_seconds()
        )
        if remaining > 0:
            self.sleep(remaining)

    def _get(self, url: str, gap: bool = True) -> httpx.Response:
        if not allowed_url(url):
            raise ValueError(f"not an allowed EP url: {url}")
        if self.state["pages"] >= self.settings.ep_daily_page_cap:
            raise _Stop(f"Tageslimit {self.settings.ep_daily_page_cap} Seiten erreicht", for_day=False)
        if gap:
            self._gap()
        cookies = getattr(self.http, "cookies", None)
        if cookies is not None:
            cookies.clear()
        self.state["pages"] += 1
        self.last = self.clock()
        self.state["last_request_at"] = self.last.isoformat()
        self.report.requests += 1
        self.save()
        return self.http.get(url, headers=HEADERS)

    def _follow(self, url: str) -> httpx.Response:
        """GET with redirects followed by hand, only to player pages (a hop is no new page: no gap)."""
        r = self._get(url)
        for _ in range(MAX_REDIRECTS):
            if r.status_code not in REDIRECT_CODES or not r.headers.get("location"):
                return r
            nxt = urljoin(str(r.url), r.headers["location"])
            if not allowed_url(nxt):
                return r
            r = self._get(nxt, gap=False)
        return r

    def load(self, url: str) -> tuple[dict | None, str, str | None]:
        """(blocks, final url, failure reason); raises _Block / _Stop."""
        attempt = 0
        backoff = list(self.settings.ep_backoff_s)
        while True:
            retry_after = None
            try:
                r = self._follow(url)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                transient = (
                    "Zeitüberschreitung" if isinstance(e, httpx.TimeoutException) else "Verbindungsfehler"
                )
            else:
                code = r.status_code
                if code in BLOCK_CODES:
                    raise _Block(
                        f"HTTP {code}",
                        retry_after_s(r.headers.get("retry-after"), self.clock()),
                        block_detail(r),
                    )
                if code == 200:
                    blocks = extract_blocks(r.text)
                    if blocks is not None:
                        return blocks, str(r.url), None
                    if not _NEXT_DATA.search(r.text) and looks_like_captcha(r.text):
                        raise _Block("Captcha/Prüfseite", detail=block_detail(r))
                    return None, str(r.url), "keine Spielerdaten auf der Seite"
                if code < 500 and code not in TRANSIENT_CODES:
                    if code in REDIRECT_CODES:
                        return None, url, "Weiterleitung ausserhalb von /player/"
                    return None, url, "nicht gefunden (HTTP 404)" if code in (404, 410) else f"HTTP {code}"
                transient = f"HTTP {code}"
                retry_after = retry_after_s(r.headers.get("retry-after"), self.clock())
            if attempt >= len(backoff):
                raise _Stop(f"{transient}, auch nach {attempt} Wiederholungen", for_day=True)
            wait = backoff[attempt]
            if retry_after is not None:
                if retry_after > self.settings.ep_retry_after_max_s:
                    raise _Stop(f"{transient}, Server bittet um {retry_after:.0f} s Pause", for_day=True)
                wait = max(wait, retry_after)
            log.info("elite prospects: %s at %s, retry in %.0f s", transient, url, wait)
            self.report.waits.append(wait)
            self.sleep(wait)
            attempt += 1

    def store(self, person: DuePerson, blocks: dict, final_url: str) -> None:
        prof = self.s.get(m.EpProfile, person.athlete_id)
        if prof is None:
            prof = m.EpProfile(athlete_id=person.athlete_id, data={})
            self.s.add(prof)
        prof.ep_id = person.ep_id
        prof.url = final_url
        prof.fetched_at = self.clock()
        prof.raw = blocks


def fetch_profiles(
    s: Session,
    settings: Settings,
    athlete_ids: list[int] | None = None,
    tiers: list[int] | None = None,
    http: Any = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    until: datetime | None = None,
    rng: random.Random | None = None,
    scope: str | None = None,
) -> FetchReport:
    """Fetch the due EP player pages (see the module docstring for the rules). `dry_run` lists who is due and
    why, without any request or state change. `until` ends the run at that time (nightly window). `scope`
    overrides EP_SCOPE ("tiers" or "event:<key>"). Commits the session after every request."""
    clock = clock or _utcnow
    sleep = sleep or time.sleep
    rng = rng or random.Random()
    now = clock()
    report = FetchReport(dry_run=dry_run)
    who = resolve_scope(s, scope if scope is not None else settings.ep_scope, now)
    report.scope, report.without_ep_id = who.label, who.without_ep_id
    row = state_row(s, create=not dry_run)
    state = day_state(row.state if row else None, local_day(now))
    report.pages_today = state["pages"]

    heartbeat = state.get("heartbeat")
    if blocked(row, now):
        report.blocked_until = row.blocked_until
        report.stopped = (
            f"pausiert bis {_label(row.blocked_until)} ({state.get('block_reason') or 'gesperrt'})"
        )
    elif state.get("stopped"):
        report.stopped = f"für heute gestoppt ({state['stopped']})"
    elif not settings.ep_enabled:
        report.stopped = "EP_ENABLED ist aus"
    elif who.problem and not athlete_ids:
        report.stopped = who.problem
    elif state["pages"] >= settings.ep_daily_page_cap:
        report.stopped = f"Tageslimit {settings.ep_daily_page_cap} Seiten erreicht"
    elif state.get("running") and heartbeat and now - datetime.fromisoformat(heartbeat) < RUNNING_STALE:
        report.stopped = "ein anderer Lauf holt gerade Seiten"
    if who.problem and not athlete_ids:
        report.due = []
    else:
        report.due = due_persons(
            s, settings, athlete_ids, tiers, force=force, now=now, failed=state["failed"], scope=who
        )
    if limit is not None:
        report.due = report.due[: max(limit, 0)]
    if dry_run or report.stopped:
        if report.stopped and not dry_run:
            log.info("elite prospects: nothing fetched: %s", report.stopped)
        return report

    run = _Run(s, settings, row, state, http or default_client(settings), clock, sleep, rng, report)
    state["running"] = True
    run.save()
    streak = 0
    try:
        for person in report.due:
            if until is not None and clock() >= until:
                report.stopped = "Nachtfenster vorbei"
                break
            url = person.url or player_url(person.ep_id)
            try:
                blocks, final_url, why = run.load(url)
                if blocks is None and url != player_url(person.ep_id) and why.startswith("nicht gefunden"):
                    url = player_url(person.ep_id)  # stored slug gone: once more by id (EP redirects)
                    blocks, final_url, why = run.load(url)
            except _Block as b:
                now_ = clock()
                pause_until = now_ + timedelta(days=settings.ep_block_pause_days)
                if b.retry_after:
                    pause_until = max(pause_until, now_ + timedelta(seconds=b.retry_after))
                row.blocked_until = pause_until
                state["block_reason"] = f"{b.reason} bei {person.name}"
                state["blocked_at"] = now_.isoformat()
                state["block_detail"] = b.detail
                report.blocked_until = pause_until
                report.stopped = f"gesperrt ({b.reason} bei {url}), pausiert bis {_label(pause_until)}"
                log.warning(
                    "ELITE PROSPECTS BLOCKED (%s at %s): all EP reads paused until %s",
                    b.reason,
                    url,
                    pause_until.isoformat(),
                )
                break
            except _Stop as stop:
                report.stopped = stop.reason
                if stop.for_day:
                    state["stopped"] = stop.reason
                    report.stopped = f"für heute gestoppt ({stop.reason})"
                    log.warning("elite prospects: stopped for today: %s", stop.reason)
                break
            if blocks is not None:
                run.store(person, blocks, final_url)
                state["failed"].pop(str(person.athlete_id), None)
                report.fetched.append(person.athlete_id)
                streak = 0
            else:
                state["failed"][str(person.athlete_id)] = {
                    "day": local_day(clock()).isoformat(),
                    "reason": why,
                }
                report.failed.append((person.athlete_id, person.name, why))
                log.info("elite prospects: %s (%s): %s", person.name, url, why)
                streak += 1
                if streak >= settings.ep_fail_streak_stop:
                    reason = f"{streak} Seiten ohne Spielerdaten nacheinander"
                    state["stopped"] = reason
                    report.stopped = f"für heute gestoppt ({reason})"
                    log.warning("elite prospects: stopped for today: %s", reason)
                    break
            run.save()
    finally:
        state["running"] = False
        state["last_run"] = {"at": clock().isoformat(), **report.out()}
        run.save()
        if http is None:
            run.http.close()
    return report
