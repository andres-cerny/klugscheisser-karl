"""Floorball (Unihockey) project seeds: one tournament, its teams, rosters with careers, stats and fun facts.

`klugscheisser -p unihockey seed floorball` (dry run by default, `--yes` writes). Reads a seeds directory (default:
the project's `seeds_dir`, `seeds/floorball`) and reuses the ice hockey tables and loaders:

    <dir>/event.yaml            tournament (or <dir>/events/<event>.yaml): same format as seeds/events/wch-2026.yaml
                                (event, name, aliases, starts, ends, host, sources, venues, groups, seeding, facts,
                                history, games). Extra keys used here:
                                  teams: [{code: SUI, name: Schweiz, group: A}] (or `groups:` A: {teams: [SUI, …]})
                                  competition: {key, name, season} (default: the event's key/name)
                                  schedule_source / games_source: source key / name of the games (e.g. the IFF)
                                  games: [{key, utc, home: SUI, away: SWE, venue, city, url, source}]
    <dir>/teams.yaml            optional: like seeds/teams.yaml (clubs with leagues); national teams are created from
                                the event when missing (key "<ioc>-men", name in German)
    <dir>/rosters/*.yaml        one per team, the ice hockey roster format (event, team: SUI or sui-men, list_type,
                                season, source, source_url, source_date, players, staff). Per player, optional:
                                  birth, qid, nationality, captain (C/A), height_cm, birth_place, hometown, shoots,
                                  career: [{team, league, country, from: 2019, to: 2022, kind: club|national|youth}]
                                  stats: [{season: 2025, team, league, games, goals, assists, points, pim, phase}]
                                  facts: [{kind: award|medal|studies|job|family_in_sport|…, text, date, source_url,
                                           source, quote}]
                                  funfacts: [{text, source_url, source, date, quote}]
                                Missing source_url on a line = the roster's source_url.
    <dir>/staff/*.yaml          optional: staff appointments (format of seeds/staff/*.yaml, team: sui-men)
    <dir>/funfacts.yaml         optional: [{person, text, source, url, date, quote}]
    <dir>/funfacts.md           optional, markdown: "## <Person>" headings, then bullets "- text … (source, date, url)"
                                (or a markdown link [source, date](url)); a bullet without a URL is skipped
                                (no fact without a source).

Every fact keeps its source (name, URL) and date; seeded fun facts are kind `fun_fact` with source "seed" (the
LLM fun-fact generator keeps them). Idempotent: running it twice adds nothing new.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.names import normalize
from klugscheisser.seed import loader
from klugscheisser.seed.floorball_info import is_research_roster, normalize_roster
from klugscheisser.sports import current_spec, national_team_key

log = logging.getLogger(__name__)

SEED_SOURCE = "seed"  # facts.source / career_stints.source of curated seed lines


@dataclass
class FloorballStats:
    teams: int = 0
    games: int = 0
    rosters: int = 0
    athletes: int = 0
    entries: int = 0
    careers: int = 0
    season_stats: int = 0
    facts: int = 0
    fun_facts: int = 0
    club_countries: int = 0  # clubs whose country was corrected from the rosters
    tournament_fun_facts: int = (
        0  # funfacts.md items about no stored person (answered by the tournament sheet)
    )
    staff: int = 0
    skipped: list[str] = field(default_factory=list)
    per_team: dict[str, str] = field(default_factory=dict)  # roster file -> "20 Spieler, 9 Staff, confirmed"

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d["skipped"] = len(self.skipped)
        d.pop("per_team", None)
        return d


def nation_de(code: str) -> str:
    from klugscheisser.tournament import NATION_DE

    return NATION_DE.get(code.upper(), code.upper())


def event_file(seeds_dir: Path) -> Path | None:
    if (seeds_dir / "event.yaml").exists():
        return seeds_dir / "event.yaml"
    files = sorted((seeds_dir / "events").glob("*.yaml")) if (seeds_dir / "events").exists() else []
    return files[0] if files else None


def _date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _teams_of(info: dict) -> list[dict]:
    """The event's teams: `teams:` list, or the nation codes of `groups:`."""
    out: dict[str, dict] = {}
    for t in info.get("teams") or []:
        if isinstance(t, str):
            t = {"code": t}
        out[t["code"].upper()] = dict(t)
    for group, g in (info.get("groups") or {}).items():
        codes = g.get("teams") if isinstance(g, dict) else g
        for c in codes or []:
            out.setdefault(c.upper(), {"code": c.upper()})
            out[c.upper()].setdefault("group", group)
    return list(out.values())


def load_event(session: Session, info: dict, path: Path, stats: FloorballStats) -> m.Event:
    """Competition, national teams, the tournament event (with groups) and its games."""
    spec = current_spec()
    sport = repo.get_sport(session, spec.key, spec.name_de)
    comp_d = info.get("competition") or {}
    comp_key = comp_d.get("key") or info["event"]
    comp = session.scalar(select(m.Competition).where(m.Competition.key == comp_key))
    if comp is None:
        comp = m.Competition(sport_id=sport.id, key=comp_key, name=comp_d.get("name") or info["name"])
        session.add(comp)
    comp.name = comp_d.get("name") or info["name"]
    comp.kind = comp_d.get("kind") or "tournament"
    comp.season = str(comp_d.get("season") or str(info.get("starts", ""))[:4] or "") or None
    session.flush()
    ev = session.scalar(select(m.Event).where(m.Event.key == info["event"]))
    if ev is None:
        ev = m.Event(key=info["event"], name=info["name"], kind="tournament", status="scheduled")
        session.add(ev)
    ev.name, ev.kind, ev.competition_id = info["name"], "tournament", comp.id
    if info.get("starts"):
        ev.starts_at = loader._dt(f"{info['starts']}T00:00:00")
    if info.get("ends"):
        ev.ends_at = loader._dt(f"{info['ends']}T23:59:00")
    for f in ("city", "venue", "source_url"):
        if info.get(f):
            setattr(ev, f, info[f])
    if not ev.source_url:
        first = next(iter((info.get("sources") or {}).values()), {}) or {}
        ev.source_url = first.get("url")
    session.flush()
    for t in _teams_of(info):
        code = t["code"].upper()
        key = national_team_key(code)
        team = repo.get_team(session, key)
        if team is None:
            team = m.Team(sport_id=sport.id, key=key, name=t.get("name") or nation_de(code))
            session.add(team)
            stats.teams += 1
        team.kind, team.country, team.short_name = "national", code, code
        team.name = t.get("name") or team.name
        for f in ("instagram_handle", "website"):
            if t.get(f):
                setattr(team, f, t[f])
        session.flush()
        link = session.get(m.EventTeam, (ev.id, team.id))
        if link is None:
            session.add(m.EventTeam(event_id=ev.id, team_id=team.id, group_name=t.get("group")))
        elif t.get("group"):
            link.group_name = t["group"]
    session.flush()
    stats.games += loader.load_event_info(session, path)
    return ev


# --- per-player extras --------------------------------------------------------------------------------------


def _season(value) -> int | None:
    """2025, "2025", "2025-26", "2025/26" -> 2025 (start year)."""
    if value in (None, ""):
        return None
    mt = re.match(r"(\d{4})", str(value))
    return int(mt.group(1)) if mt else None


def _career(session: Session, athlete: m.Athlete, c: dict, url: str, stats: FloorballStats) -> None:
    name = c.get("team")
    if not name or not url:
        return
    kind = c.get("kind") or "club"
    season_from, season_to = _season(c.get("from")), _season(c.get("to"))
    exists = session.scalar(
        select(m.CareerStint.id).where(
            m.CareerStint.athlete_id == athlete.id,
            m.CareerStint.team_name == name,
            m.CareerStint.season_from.is_(None)
            if season_from is None
            else m.CareerStint.season_from == season_from,
        )
    )
    if exists:
        return
    team = None
    if kind == "national" and c.get("country"):
        team = repo.get_team(session, national_team_key(c["country"]))
    team = team or repo.find_team(session, name, fuzzy=False)
    session.add(
        m.CareerStint(
            athlete_id=athlete.id,
            team_id=team.id if team else None,
            team_name=name[:200],
            kind=kind,
            league=(c.get("league") or None),
            season_from=season_from,
            season_to=season_to,
            role=c.get("role") or "player",
            source=(c.get("source") or SEED_SOURCE)[:30],
            source_url=c.get("source_url") or url,
            quote=c.get("quote"),
            status="confirmed",
        )
    )
    stats.careers += 1


STAT_FIELDS = ("games", "goals", "assists", "points", "wins", "losses", "saves", "goals_against", "shutouts")


def _stat(session: Session, athlete: m.Athlete, st: dict, url: str, stats: FloorballStats) -> None:
    season = _season(st.get("season"))
    league = (st.get("league") or "").strip()
    if season is None or not league or not url:
        return
    source = (st.get("source") or SEED_SOURCE)[:30]
    phase = st.get("phase") or "regular"
    team_name = st.get("team")
    row = session.scalar(
        select(m.SeasonStat).where(
            m.SeasonStat.athlete_id == athlete.id,
            m.SeasonStat.season == season,
            m.SeasonStat.league == league[:50],
            m.SeasonStat.team_name == team_name if team_name else m.SeasonStat.team_name.is_(None),
            m.SeasonStat.phase == phase,
            m.SeasonStat.source == source,
        )
    )
    if row is None:
        row = m.SeasonStat(
            athlete_id=athlete.id,
            season=season,
            league=league[:50],
            team_name=team_name,
            phase=phase,
            source=source,
            source_url=st.get("source_url") or url,
            team_kind=st.get("kind") or "club",
        )
        session.add(row)
        stats.season_stats += 1
    for f in STAT_FIELDS:
        if st.get(f) is not None:
            setattr(row, f, int(st[f]))
    if st.get("pim") is not None:
        row.penalty_minutes = int(st["pim"])
    if st.get("plus_minus") is not None:
        row.plus_minus = int(st["plus_minus"])
    for f in ("save_pct", "gaa"):
        if st.get(f) is not None:
            setattr(row, f, float(st[f]))


def add_fact(
    session: Session,
    athlete: m.Athlete,
    kind: str,
    text: str,
    url: str | None,
    *,
    source_name: str | None = None,
    when: Any = None,
    quote: str | None = None,
    extra: dict | None = None,
    unverified: bool = False,
) -> bool:
    """One sourced fact (idempotent by person + kind + text). No URL = not stored. The stable key is the seed's
    own (English) text: a row translated in place keeps it as `value.text_en` (or `text_en_raw` before a
    cleanup), so a second run adds no English copy; a hidden research note (`research_note`) stays hidden."""
    text = (text or "").strip()
    if not text or not url:
        return False
    exists = session.scalar(
        select(m.Fact.id).where(
            m.Fact.athlete_id == athlete.id,
            m.Fact.kind.in_((kind, "research_note")),
            or_(
                m.Fact.value_text == text,
                m.Fact.value["text_en"].as_string() == text,
                m.Fact.value["text_en_raw"].as_string() == text,
            ),
        )
    )
    if exists:
        return False
    d = _date(when)
    value: dict[str, Any] = {"text": text, **(extra or {})}
    if d:
        value["date"] = d.isoformat()
    if source_name:
        value["quelle"] = source_name
    session.add(
        m.Fact(
            athlete_id=athlete.id,
            kind=kind,
            value=value,
            value_text=text,
            valid_from=d if kind != "fun_fact" else None,
            source=SEED_SOURCE,
            source_url=url,
            quote=quote,
            confidence=0.5 if unverified else 1.0,
            status="unconfirmed"
            if unverified
            else "confirmed",  # a single secondary source: check before air
            extractor=SEED_SOURCE,
        )
    )
    return True


def _value_fact(session: Session, athlete: m.Athlete, kind: str, value, url: str, source: str | None) -> bool:
    """Simple profile values (height, birth place …) as catalog facts."""
    if value in (None, ""):
        return False
    if kind == "height_cm":
        text = f"{int(value)} cm"
        return add_fact(session, athlete, kind, text, url, source_name=source, extra={"number": int(value)})
    return add_fact(session, athlete, kind, str(value), url, source_name=source)


def load_player_extras(
    session: Session, roster: dict, stats: FloorballStats, by_name: dict[str, m.Athlete] | None = None
) -> None:
    """Career, season stats, facts and fun facts of the players (and staff) of one roster seed."""
    url = roster.get("source_url") or ""
    src_name = roster.get("source")
    for p in [*(roster.get("players") or []), *(roster.get("staff") or [])]:
        if not p.get("name"):
            continue
        athlete = _find(session, p["name"], p.get("qid"))
        if athlete is None:
            stats.skipped.append(f"{p['name']}: not found after the roster load")
            continue
        if by_name is not None:
            by_name[normalize(p["name"])] = athlete
        purl = p.get("source_url") or url
        for kind in ("height_cm", "birth_place", "hometown", "nickname", "languages"):
            if _value_fact(session, athlete, kind, p.get(kind), purl, src_name):
                stats.facts += 1
        if p.get("shoots") and not athlete.shoots:
            athlete.shoots = str(p["shoots"])[:1].upper()
        for c in p.get("career") or []:
            _career(session, athlete, c, purl, stats)
        for st in p.get("stats") or []:
            _stat(session, athlete, st, purl, stats)
        for f in p.get("facts") or []:
            if add_fact(
                session,
                athlete,
                f.get("kind") or "milestone",
                f.get("text", ""),
                f.get("source_url") or purl,
                source_name=f.get("source") or src_name,
                when=f.get("date"),
                quote=f.get("quote"),
            ):
                stats.facts += 1
        for f in p.get("funfacts") or p.get("fun_facts") or []:
            if isinstance(f, str):
                f = {"text": f}
            if add_fact(
                session,
                athlete,
                "fun_fact",
                f.get("text", ""),
                f.get("source_url") or f.get("url") or purl,
                source_name=f.get("source") or src_name,
                when=f.get("date"),
                quote=f.get("quote"),
                unverified=bool(f.get("unverified")),
            ):
                stats.fun_facts += 1
    session.flush()


def _find(session: Session, name: str, qid: str | None = None) -> m.Athlete | None:
    if qid:
        a = session.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == qid))
        if a:
            return a
    hits = [h.athlete for h in repo.find_athletes(session, name) if h.how == "exact"]
    return hits[0] if hits else None


# --- fun facts files ---------------------------------------------------------------------------------------

_URL = re.compile(r"https?://[^\s)\]>]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b|\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")


def _date_in(text: str) -> str | None:
    mt = _DATE.search(text)
    if not mt:
        return None
    if mt.group(1):
        return mt.group(1)
    d, mo, y = mt.group(2), mt.group(3), mt.group(4)
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _item_fact(heading: str | None, lines: list[str]) -> dict | None:
    body = " ".join(x.strip() for x in lines if x.strip())
    main = re.split(r"\bSources?:", body, maxsplit=1)
    text, source_part = main[0], (main[1] if len(main) > 1 else "")
    link = _MD_LINK.search(body)
    url_m = _URL.search(source_part) or _URL.search(body)
    url = link.group(2) if link else (url_m.group(0).rstrip(".,;") if url_m else None)
    when = _date_in(source_part) or _date_in(body)
    source = (
        link.group(1)
        if link
        else (re.split(r",\s*(?=\d{4}-|https?://)", source_part.strip())[0][:120] or None)
    )
    fact = _MD_LINK.sub("", text)
    fact = _URL.sub("", fact)
    fact = (
        re.sub(r"\s*[\(\[]\s*(Quelle:?)?[^()\[\]]*[\)\]]\s*$", "", fact) if link or not source_part else fact
    )
    fact = re.sub(r"\s*(—|–|-)?\s*Quelle:.*$", "", fact, flags=re.I)
    unverified = bool(re.search(r"\[unverified", fact, re.I))
    fact = re.sub(r"\*?\*?\[unverified[^\]]*\]\*?\*?", "", fact, flags=re.I)
    fact = re.sub(r"\*\*|__", "", fact).strip(" -–—;,")
    if not fact:
        return None
    return {
        "person": heading,
        "text": fact,
        "source": source,
        "url": url,
        "date": when,
        "unverified": unverified,
    }


def _source_key(text: str) -> str | None:
    """'IFF Men's WFC Records (as above).' -> 'iff men s wfc records' (a source's name to match earlier links)."""
    t = re.sub(r"\(.*?\)|https?://\S+|\d{4}-\d{2}(-\d{2})?|retrieved.*$", " ", text or "", flags=re.I)
    t = re.split(r"[;,.]", t.strip(), maxsplit=1)[0]
    key = normalize(t)
    return key if len(key) >= 6 and len(key.split()) >= 2 else None


def parse_funfacts_md(text: str) -> tuple[list[dict], list[str]]:
    """Markdown fun facts -> [{person, text, source, url, date, unverified}]. Two layouts:
    '## <Person>' + '- fact ([source, date](url))' bullets, or topic headings + numbered items with a 'Source: …,
    <date>, <url>' line (the persons are then found by name in the text, see load_funfacts). `person` is the
    heading. Items without a URL are skipped (no fact without a source); returns (facts, skipped)."""
    out: list[dict] = []
    skipped: list[str] = []
    heading: str | None = None
    current: list[str] | None = None
    # 2026-11-24: a source line may point back instead of repeating the link ("(same article)", "(as above)",
    # "IFF Men's WFC Records", "unihockey.ch 28199"): resolved to a link given earlier in the file
    by_id = {
        mt.group(1): u for u in _URL.findall(text) if (mt := re.search(r"-(\d{4,6})/?$", u.rstrip(".,;")))
    }
    by_name: dict[str, str] = {}
    last_url: list[str] = []

    def resolve(lines: list[str], item: dict) -> str | None:
        body = " ".join(lines)
        part = re.split(r"\bSources?:", body, maxsplit=1)
        src = part[1] if len(part) > 1 else ""
        if not src.strip():
            return None
        if re.search(
            r"same (article|source|page)|as above|ebenda|ibid|gleiche[rn]? (artikel|quelle)", src, re.I
        ):
            named = _source_key(src)
            if named and named in by_name:
                return by_name[named]
            return last_url[-1] if last_url else None
        for num in re.findall(r"\b(\d{5})\b", src):  # "unihockey.ch, 2026-03-05 (28199)"
            if num in by_id:
                return by_id[num]
        named = _source_key(src)
        return by_name.get(named) if named else None

    def flush() -> None:
        nonlocal current
        if current and re.search(r"do not use|nicht verwenden|april fools", " ".join(current), re.I):
            skipped.append(f"{heading}: ausdrücklich nicht verwenden: {current[0][:60]}")
            current = None
        if current:
            item = _item_fact(heading, current)
            if item and not item["url"] and (url := resolve(current, item)):
                item["url"] = url
                src = re.sub(r"\s*\([^)]*\)", "", item.get("source") or "")
                item["source"] = re.split(r"\.\s|\.$", src.strip(), maxsplit=1)[0].strip() or None
            if item and item["url"]:
                out.append(item)
                last_url.append(item["url"])
                body = " ".join(current)
                for name, url in re.findall(
                    r"([A-Z][^;:,()]{3,60}?),?\s+(?:\(retrieved[^)]*\)\s*)?(https?://[^\s)]+)", body
                ):
                    if key := _source_key(name):
                        by_name.setdefault(key, url.rstrip(".,;"))
                if item.get("source") and (key := _source_key(item["source"])):
                    by_name.setdefault(key, item["url"])
            elif item:
                skipped.append(f"{heading}: {item['text'][:80]}")
        current = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if mt := re.match(r"^#{2,4}\s+(.+?)\s*$", line):
            flush()
            heading = re.sub(r"\s*[\(\[].*$", "", mt.group(1)).strip(" *_")
            heading = re.sub(r"^\d+\.?\s*", "", heading)
            continue
        if line.startswith("#"):
            continue
        if re.match(r"^(\d+\.|[-*])\s+", line) and heading is not None:  # a new item at the left margin
            flush()
            current = [re.sub(r"^(\d+\.|[-*])\s+", "", line)]
        elif current is not None and line.strip():
            current.append(re.sub(r"^\s*[-*]\s+", "", line))
        elif current is not None and not line.strip():
            continue
    flush()
    return out, skipped


def _mentioned(session: Session, text: str) -> list[m.Athlete]:
    """Persons named with their full name in a text (fun facts under a topic heading)."""
    norm = f" {normalize(text)} "
    return [
        a
        for a in session.scalars(select(m.Athlete))
        if len(a.full_name.split()) >= 2 and f" {normalize(a.full_name)} " in norm
    ]


def load_funfacts(session: Session, items: list[dict], stats: FloorballStats) -> None:
    for f in items:
        name = f.get("person") or f.get("name")
        a = _find(session, name, f.get("qid")) if name else None
        persons = [a] if a is not None else _mentioned(session, f.get("text", ""))
        if not persons:
            stats.tournament_fun_facts += 1  # about the tournament: tournament.py topic "funfacts"
            continue
        for person in persons:
            if add_fact(
                session,
                person,
                f.get("kind") or "fun_fact",
                f.get("text", ""),
                f.get("url") or f.get("source_url"),
                source_name=f.get("source"),
                when=f.get("date"),
                quote=f.get("quote"),
                unverified=bool(f.get("unverified")),
            ):
                stats.fun_facts += 1
    session.flush()


# --- all -----------------------------------------------------------------------------------------------------


def load(session: Session, seeds_dir: Path) -> FloorballStats:
    """Everything in the seeds directory, in order: clubs, event + teams + games, rosters (players, staff,
    careers, stats, facts), staff appointments, fun facts. Tiers are recomputed at the end."""
    from klugscheisser import staff, tiers

    stats = FloorballStats()
    path = event_file(seeds_dir)
    if path is None:
        raise FileNotFoundError(f"no event.yaml or events/*.yaml in {seeds_dir}")
    from klugscheisser.tournament import _read

    info = _read(str(path.resolve()), path.stat().st_mtime)  # the research format is normalized there
    if (seeds_dir / "teams.yaml").exists():
        stats.teams += loader.load_base(session, seeds_dir / "teams.yaml").teams
    load_event(session, info, path, stats)
    rosters = sorted((seeds_dir / "rosters").glob("*.yaml")) if (seeds_dir / "rosters").exists() else []
    for rp in rosters:
        data = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
        if "not_qualified" in str(data.get("status") or ""):
            stats.per_team[str(data.get("code") or rp.stem)] = "nicht qualifiziert (nicht geladen)"
            continue
        status = str(data.get("status") or "")
        if is_research_roster(data):
            data = normalize_roster(data, info["event"])
        if not data.get("team"):
            stats.skipped.append(f"{rp.name}: no team")
            continue
        data.setdefault("event", info["event"])
        data["team"] = loader.team_key(data["team"])
        if repo.get_team(session, data["team"]) is None:
            stats.skipped.append(f"{rp.name}: unknown team {data['team']}")
            continue
        s = loader.load_roster_data(session, data, rp.name)
        stats.rosters += 1
        stats.athletes += s.athletes
        stats.entries += s.entries
        load_player_extras(session, data, stats)
        players = len(data.get("players") or [])
        stats.per_team[str(data["team"])] = (
            f"{players} Spieler, {len(data.get('staff') or [])} Staff, {data.get('list_type')}"
            + (f" ({status})" if status else "")
        )
    stats.club_countries += len(repair_club_countries(session, seeds_dir, info["event"]))
    if (seeds_dir / "staff").exists():
        stats.staff += staff.load_seeds(session, seeds_dir / "staff")
    stats.staff += staff.sync_from_entries(session)
    if (seeds_dir / "funfacts.yaml").exists():
        items = yaml.safe_load((seeds_dir / "funfacts.yaml").read_text(encoding="utf-8")) or []
        load_funfacts(session, items if isinstance(items, list) else items.get("funfacts", []), stats)
    if (seeds_dir / "funfacts.md").exists():
        items, skipped = parse_funfacts_md((seeds_dir / "funfacts.md").read_text(encoding="utf-8"))
        stats.skipped += [f"funfacts.md without URL: {s}" for s in skipped]
        load_funfacts(session, items, stats)
    tiers.recompute(session)
    session.flush()
    return stats


def club_countries(seeds_dir: Path, event_key: str) -> dict[str, str]:
    """{club name: IOC} from the research rosters: the league's or the stated country of the club first; a
    club only named without either gets the players' nation when all its players come from one nation."""
    explicit: dict[str, set[str]] = {}
    guess: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for rp in sorted((seeds_dir / "rosters").glob("*.yaml")) if (seeds_dir / "rosters").exists() else []:
        data = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
        if not is_research_roster(data) or "not_qualified" in str(data.get("status") or ""):
            continue
        for p in normalize_roster(data, event_key).get("players") or []:
            if not p.get("club") or not p.get("country"):
                continue
            k = normalize(p["club"])
            names.setdefault(k, p["club"])
            (guess if p.get("country_guess") else explicit).setdefault(k, set()).add(p["country"])
    out = {}
    for k, name in names.items():
        votes = explicit.get(k) or guess.get(k) or set()
        if len(votes) == 1:
            out[name] = next(iter(votes))
    return out


def repair_club_countries(
    session: Session, seeds_dir: Path, event_key: str, apply: bool = True
) -> list[tuple[str, str | None, str]]:
    """Clubs of the rosters whose stored country differs from the seeds' (before 2026-11-24 every club without a
    stated country was stored as Swiss). Returns [(club, old, new)]; changes them when `apply`. Idempotent."""
    changes = []
    for name, ioc in club_countries(seeds_dir, event_key).items():
        team = session.scalar(select(m.Team).where(m.Team.kind == "club", m.Team.name == name))
        if team is None or team.country == ioc:
            continue
        changes.append((team.name, team.country, ioc))
        if apply:
            team.country = ioc
    session.flush()
    return changes


# --- free sources after the seeds ----------------------------------------------------------------------------


def event_athletes(session: Session, event_key: str | None = None) -> list[m.Athlete]:
    from klugscheisser import projects

    key = event_key or projects.current().main_event
    return list(
        session.scalars(
            select(m.Athlete)
            .join(m.EventEntry, m.EventEntry.athlete_id == m.Athlete.id)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .where(m.Event.key == key)
            .distinct()
            .order_by(m.Athlete.id)
        )
    )


def wikidata_qids(session: Session, athletes: list[m.Athlete], echo=print) -> int:
    """QIDs for roster players without one: the sport's Wikidata players of each nation (one free SPARQL query
    per nation), matched by exact name (and birth date when both have one). Returns the number of new QIDs."""
    from klugscheisser.collectors.wikidata import WikidataCollector
    from klugscheisser.sports import COUNTRY_QIDS

    spec = current_spec()
    missing = [a for a in athletes if not a.wikidata_qid and a.nationality in COUNTRY_QIDS]
    nations = sorted({a.nationality for a in missing})
    taken = set(session.scalars(select(m.Athlete.wikidata_qid).where(m.Athlete.wikidata_qid.is_not(None))))
    found = 0
    wd = WikidataCollector()
    for nat in nations:
        try:
            players = wd.collect(spec, nat, COUNTRY_QIDS[nat])
        except Exception as exc:  # network / timeout: the seeds stay as they are
            echo(f"  Wikidata {nat}: {str(exc)[:100]}")
            continue
        by_name: dict[str, list] = {}
        for p in players:
            by_name.setdefault(normalize(p.name), []).append(p)
        for a in (x for x in missing if x.nationality == nat):
            cands = [
                p
                for p in by_name.get(normalize(a.full_name), [])
                if a.birth_date is None or p.birth_date in (None, a.birth_date)
            ]
            if len(cands) == 1 and cands[0].qid not in taken:
                a.wikidata_qid = cands[0].qid
                if a.birth_date is None and cands[0].birth_date:
                    a.birth_date = cands[0].birth_date
                taken.add(cands[0].qid)
                found += 1
    session.flush()
    return found


def enrich(session: Session, sources: list[str], max_requests: int = 800, echo=print) -> dict[str, Any]:
    """Free sources for the players of the project's tournament: Wikidata (QIDs, facts, photos) and the floorball
    adapters (collectors/floorball: Swiss Unihockey, IFF)."""
    athletes = event_athletes(session)
    out: dict[str, Any] = {"personen": len(athletes)}
    if "wikidata" in sources:
        from klugscheisser.facts.wikidata_extra import photos
        from klugscheisser.facts.wikidata_import import import_wikidata_facts

        out["wikidata_qids_neu"] = wikidata_qids(session, athletes, echo)
        with_qid = [a for a in athletes if a.wikidata_qid]
        out["wikidata"] = import_wikidata_facts(session, with_qid)
        out["fotos"] = photos(session, with_qid)
    if "torneopal" in sources:
        from klugscheisser import projects
        from klugscheisser.collectors.floorball import torneopal

        try:
            out["resultate"] = torneopal.update(session, projects.current().main_event)
        except Exception as exc:  # network: the seeded schedule stays
            out["resultate"] = f"Fehler: {str(exc)[:120]}"
    adapters = [x for x in sources if x not in ("wikidata", "torneopal")]
    if adapters:
        try:
            from klugscheisser.collectors.floorball import run as floorball_run
        except ImportError:
            out["adapter"] = "collectors/floorball fehlt"
        else:
            out.update(
                floorball_run.enrich(
                    session,
                    [a.id for a in athletes],
                    sources=tuple(adapters),
                    dry_run=False,
                    max_requests=max_requests,
                )
            )
    return out
