"""Current status of a person: the newest of injury report, roster nomination and game appearance.

Injury reports have no end date. Gap loop round 2 (2026-10-19): Karl still said "Waser needs surgery (19.09.),
fitness unknown" although she was named in the 13.10. squad and scored 2 goals on 14.10. So every injury line is
reconciled with what we know happened *after* it:

- injury/absence report: newest item about her classified as injury from an official / media source (not fan
  or gossip posts), or a dated `injury` fact (e.g. from a roster announcement "fehlt verletzt"),
- roster nomination: a confirmed event roster with her (date = announcement date of the roster seed, else the
  event start); provisional lists ("Annahme: letztes Aufgebot") are no nomination,
- game appearance: a stored game report (SIHF, IIHF, Leijonat, SDHL, PWHL …) where she is listed – scorer,
  assist, goalie (with minutes) or in the lineup.

`PersonStatus.since()` lists the nominations/games after the newest injury report ("seither: im Aufgebot 13.10.,
2 Tore 14.10."); an injury with such a signal counts as resolved (mcp_server/caution.py drops it from ⚠️).
Deterministic, from stored data only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.labels import local
from klugscheisser.domain.names import name_variants, normalize

GAME_LOOKBACK_DAYS = 200  # game reports older than this do not tell anything about her current status
SINCE_MAX = 3  # signals listed after "seither:"
_SWISS_LINE = re.compile(r"^Schweizer(?:innen)?: (.+)$", re.M)  # 2026-11-24: also men's game texts
_ENTRY = re.compile(r"([^;()]+?) \(([^)]*)\)")


@dataclass
class Signal:
    kind: str  # "verletzung" | "aufgebot" | "spiel"
    day: date
    short: str  # "im Aufgebot 13.10.", "2 Tore 14.10.", "Verletzung 19.09."
    text: str  # one sentence with context (event / game / report title)
    url: str
    quelle: str
    item_id: int | None = None


@dataclass
class PersonStatus:
    athlete_id: int
    name: str
    injury: Signal | None = None
    signals: list[Signal] = field(default_factory=list)  # nominations + games, newest first

    def since(self, day: date | None = None) -> list[Signal]:
        """Nominations/games after `day` (default: the newest injury report), oldest first, at most SINCE_MAX
        (the newest ones)."""
        ref = day or (self.injury.day if self.injury else None)
        if ref is None:
            return []
        after = [s for s in self.signals if s.day > ref]
        return sorted(after[:SINCE_MAX], key=lambda s: s.day)

    def resolved(self, day: date | None = None) -> bool:
        """An injury reported on `day` (default: the newest) is followed by a nomination or a game."""
        return bool(self.since(day))

    @property
    def latest(self) -> Signal | None:
        newest = self.signals[0] if self.signals else None
        if self.injury and (newest is None or self.injury.day >= newest.day):
            return self.injury
        return newest

    def since_text(self, day: date | None = None) -> str | None:
        """'seither: im Aufgebot 13.10., 2 Tore 14.10.' or None."""
        after = self.since(day)
        return "seither: " + ", ".join(s.short for s in after) if after else None

    def since_md(self, day: date | None = None) -> str | None:
        """Same with a link per signal (MCP text)."""
        after = self.since(day)
        if not after:
            return None
        return "seither: " + ", ".join(f"[{s.short}]({s.url})" if s.url else s.short for s in after)


def _d(dt: datetime | date | None) -> date | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return local(dt if dt.tzinfo else dt.replace(tzinfo=UTC)).date()
    return dt


def _dm(d: date) -> str:
    return f"{d:%d.%m.}" if d.year == datetime.now(UTC).year else f"{d:%d.%m.%Y}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# --- game appearances -------------------------------------------------------------------------------------


def _keys(a: m.Athlete) -> set[str]:
    keys = set(name_variants(a.full_name)) | {al.alias_normalized for al in a.aliases}
    parts = normalize(a.full_name).split()
    if len(parts) >= 2:  # "Waser Luisa" (family name first, as some score sheets write it)
        keys.add(" ".join(parts[1:] + parts[:1]))
    return {k for k in keys if k}


def _is_her(name: str | None, keys: set[str]) -> bool:
    return bool(name) and bool(name_variants(name) & keys)


def appearance(item: m.Item, a: m.Athlete, keys: set[str] | None = None) -> str | None:
    """What she did in this game report: '2 Tore', '1 Tor, 1 Assist', 'Torhüterin (60:00 Min., 1 Gegentor)',
    'im Einsatz'; None when the report does not list her (a linked report is no proof she played)."""
    keys = keys or _keys(a)
    # the Swiss summary line of our own game text: "Schweizerinnen: Luisa Waser (2 Tore); …"
    if (mt := _SWISS_LINE.search(item.text or "")) is not None:
        for name, detail in _ENTRY.findall(mt.group(1)):
            if _is_her(name.strip(), keys):
                return _detail_de(detail)
    raw = item.raw or {}
    parts = [p for p in raw.get("participants") or [] if _is_her(p.get("name"), keys)]
    if parts:
        goals = sum(p.get("role") == "scorer" for p in parts)
        assists = sum(p.get("role") == "assist" for p in parts)
        bits = [_plural(goals, "Tor", "Tore")] if goals else []
        bits += [_plural(assists, "Assist", "Assists")] if assists else []
        goalie = next((p for p in parts if p.get("role") == "goalie"), None)
        if goalie is not None:
            label = _goalie_label()
            bits.append(f"{label} ({goalie['detail']})" if goalie.get("detail") else label)
        return ", ".join(bits) or "im Einsatz"
    for names in (raw.get("lineup") or {}).values():
        if any(_is_her(n, keys) for n in names or []):
            return "im Einsatz"
    return None


def _detail_de(detail: str) -> str:
    """'Torhüterin: 1 Gegentore, 60:00 Min.' -> 'Torhüterin (1 Gegentore, 60:00 Min.)'; others unchanged."""
    detail = detail.strip()
    for label in ("Torhüterin", "Torhüter"):  # 2026-11-24: men's game texts (floorball)
        if detail.startswith(f"{label}:"):
            return f"{label} ({detail.removeprefix(f'{label}:').strip()})"
    return detail


def _goalie_label() -> str:
    """'Torhüterin', or the project's word ('Torhüter' for men's floorball)."""
    from klugscheisser.sports import current_spec

    return current_spec().position_labels_de.get("G", "Torhüterin")


def _game_signals(s: Session, athletes: list[m.Athlete], now: datetime) -> dict[int, list[Signal]]:
    since = now - timedelta(days=GAME_LOOKBACK_DAYS)
    rows = s.execute(
        select(m.ItemAthlete.athlete_id, m.Item)
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .where(
            m.ItemAthlete.athlete_id.in_([a.id for a in athletes]),
            m.Item.source_type == "official_result",
            m.Item.published_at >= since,
            m.Item.published_at <= now,
        )
    ).all()
    by_id = {a.id: a for a in athletes}
    keys = {a.id: _keys(a) for a in athletes}
    out: dict[int, list[Signal]] = {}
    for aid, item in rows:
        what = appearance(item, by_id[aid], keys[aid])
        day = _d(item.published_at)
        if what is None or day is None:
            continue
        game = (item.title or "").strip()
        out.setdefault(aid, []).append(
            Signal(
                "spiel",
                day,
                f"{what} {_dm(day)}",
                f"{_dm(day)} {game}: {what}" if game else f"{_dm(day)}: {what}",
                item.url,
                item.source_name or item.platform,
                item.id,
            )
        )
    return out


# --- roster nominations ------------------------------------------------------------------------------------


def _nomination_day(e: m.EventEntry, ev: m.Event) -> tuple[date | None, str]:
    """(day, how): announcement date of the roster seed, else the event start ("Start"), else when we stored it."""
    from klugscheisser import tournament

    if e.team is not None:
        seed = tournament._roster_seed(ev.key, e.team.key)
        if raw := seed.get("source_date"):
            try:
                return date.fromisoformat(str(raw)), "bekanntgegeben"
            except ValueError:
                pass
    if (e.source or "").startswith("Kaderwatch"):  # official list found by the roster watcher on that day
        return _d(e.fetched_at), "publiziert"
    if ev.starts_at is not None:
        return _d(min(ev.starts_at, e.fetched_at) if e.fetched_at else ev.starts_at), "Start"
    return _d(e.fetched_at), "erfasst"


def _roster_signals(s: Session, ids: list[int], today: date) -> dict[int, list[Signal]]:
    rows = s.execute(
        select(m.EventEntry, m.Event)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.athlete_id.in_(ids), m.EventEntry.list_type == "confirmed")
    ).all()
    out: dict[int, list[Signal]] = {}
    for e, ev in rows:
        if e.role not in (None, "G", "D", "F"):  # staff
            continue
        day, how = _nomination_day(e, ev)
        if day is None or day > today:
            continue
        out.setdefault(e.athlete_id, []).append(
            Signal(
                "aufgebot",
                day,
                f"im Aufgebot {_dm(day)}",
                f"im Aufgebot für {ev.name} ({how} {_dm(day)})",
                e.source_url or ev.source_url or "",
                e.source or "Kader",
            )
        )
    return out


# --- injury reports -------------------------------------------------------------------------------------------


def _injury_signals(s: Session, ids: list[int], now: datetime) -> dict[int, Signal]:
    from klugscheisser.mcp_server.caution import NOT_NEWS, is_trusted
    from klugscheisser.mcp_server.present import first_sentence

    best: dict[int, Signal] = {}

    def keep(aid: int, sig: Signal) -> None:
        if aid not in best or sig.day > best[aid].day:
            best[aid] = sig

    rows = s.execute(
        select(m.ItemAthlete.athlete_id, m.Item)
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .join(m.Classification, m.Classification.item_id == m.Item.id)
        .where(
            m.ItemAthlete.athlete_id.in_(ids),
            m.ItemAthlete.main.is_not(False),
            m.Classification.category == "injury",
            m.Item.published_at.is_not(None),
            m.Item.published_at <= now,
            m.Item.source_type.not_in(list(NOT_NEWS)),
        )
    ).all()
    for aid, item in rows:
        if not is_trusted(item):  # a fan's "she won't be on the ice" is no injury report
            continue
        day = _d(item.published_at)
        title = first_sentence(item.title or item.text, limit=140)
        keep(
            aid,
            Signal(
                "verletzung",
                day,
                f"Verletzung {_dm(day)}",
                title,
                item.url,
                item.source_name or item.platform,
                item.id,
            ),
        )
    for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id.in_(ids), m.Fact.kind == "injury")):
        src = s.get(m.Item, f.source_item_id) if f.source_item_id else None
        day = _fact_day(f, src)
        if day is None or day > _d(now):
            continue
        quelle = (src.source_name or src.platform) if src is not None else f.source
        keep(
            f.athlete_id,
            Signal("verletzung", day, f"Verletzung {_dm(day)}", f.value_text, f.source_url, quelle),
        )
    return best


def _fact_day(f: m.Fact, src: m.Item | None) -> date | None:
    """When it was reported: the source document's date (an extracted "date" can be a surgery date or an
    injury years ago), else the fact's own date."""
    if src is not None and src.published_at is not None and src.source_type != "profile":
        return _d(src.published_at)  # (a Wikipedia/profile page's date is its last edit, not a report date)
    raw = (f.value or {}).get("date")
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    if f.valid_from:
        return f.valid_from
    return None  # undated (e.g. an old story about a past illness): says nothing about today


# --- entry point --------------------------------------------------------------------------------------------


def statuses(
    s: Session, athletes: Iterable[m.Athlete], now: datetime | None = None
) -> dict[int, PersonStatus]:
    """Status of each person (one batch of queries for a whole roster)."""
    now = now or datetime.now(UTC)
    people = [a for a in athletes if a is not None and a.role in (None, "athlete")]  # not coaches / staff
    if not people:
        return {}
    ids = [a.id for a in people]
    today = _d(now)
    games = _game_signals(s, people, now)
    rosters = _roster_signals(s, ids, today)
    injuries = _injury_signals(s, ids, now)
    out = {}
    for a in people:
        sig = sorted(games.get(a.id, []) + rosters.get(a.id, []), key=lambda x: x.day, reverse=True)
        out[a.id] = PersonStatus(a.id, a.full_name, injuries.get(a.id), sig)
    return out


def status_of(s: Session, a: m.Athlete, now: datetime | None = None) -> PersonStatus | None:
    """Status of one person (None for coaches / staff)."""
    return statuses(s, [a], now).get(a.id)
