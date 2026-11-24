"""Watch official sources for the announcement of the official WCh roster (nightly step "kaderwatch").

Per team one listing page of the federation (seeds/events/<event>.yaml, `roster_watch`), fetched politely (identified
User-Agent, robots.txt, one request per page and night, free). A new link whose text/URL matches the team's pattern is
the announcement: it is remembered in `source_state` ("roster_watch"), stored as an official news item for the team
(so whats_new shows it) and named in every tournament answer ("Offizieller WM-Kader publiziert am …").
For Switzerland (sihf.ch) the article is also read: the list "Torhüterinnen (3): Name (Klub), …" becomes the confirmed
WCh roster (source "Kaderwatch: …"; a provisional seed never replaces it), and sentences naming injured/absent players
are kept with the finding. Other federations: flagged only (their pages differ too much to parse safely).
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.projects import wording
from klugscheisser.sports import national_team_key
from klugscheisser.staff import is_staff_entry

log = logging.getLogger(__name__)

STATE = "roster_watch"
LINK = re.compile(r"<a\b[^>]*?href=\"([^\"#]+)\"[^>]*>(.*?)</a>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")
POS_BLOCK = re.compile(  # 2026-11-24: also the men's forms ("Torhüter (3): …")
    r"(Torhüter(?:innen)?|Verteidiger(?:innen)?|Stürmer(?:innen)?)\s*\(\d+\)\s*:\s*(.+?)(?=(?:Torhüter|"
    r"Verteidiger|Stürmer)(?:innen)?\s*\(\d+\)\s*:|\n|$)",
    re.S,
)
POS = {"Torhüterinnen": "G", "Verteidigerinnen": "D", "Stürmerinnen": "F"}
POS |= {"Torhüter": "G", "Verteidiger": "D", "Stürmer": "F"}
ABSENT = re.compile(
    r"[^.!?\n]*(verletz|fehl(t|en)\b|verzicht|nicht dabei|ausfall|fällt .* aus|absage)[^.!?\n]*[.!?]", re.I
)
MIN_PLAYERS = 18  # a parsed list shorter than this is not trusted as a roster


def watch_state(s: Session) -> dict:
    import copy

    row = s.get(m.SourceState, STATE)
    return copy.deepcopy(row.state or {}) if row is not None else {}


def _save(s: Session, state: dict) -> None:
    row = s.get(m.SourceState, STATE)
    if row is None:
        row = m.SourceState(source=STATE, state={})
        s.add(row)
    from sqlalchemy.orm.attributes import flag_modified

    row.state = state
    flag_modified(row, "state")
    s.flush()


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(TAG.sub(" ", fragment))).strip()


def page_links(body: str, base: str, content_type: str = "") -> list[tuple[str, str]]:
    """(absolute url, link text) of a listing page; WordPress JSON (`wp-json/wp/v2/posts`) gives link + title."""
    if "json" in content_type or body.lstrip().startswith("["):
        import json

        try:
            posts = json.loads(body)
        except ValueError:
            return []
        return [
            (p.get("link", ""), _text((p.get("title") or {}).get("rendered", "")))
            for p in posts
            if isinstance(p, dict)
        ]
    return [(urljoin(base, href), _text(text)) for href, text in LINK.findall(body)]


YOUTH = re.compile(r"\bu ?(16|18|20)\b|junior|nachwuchs", re.I)


def matching(links: list[tuple[str, str]], pattern: str, exclude: str | None = None) -> list[tuple[str, str]]:
    """Links whose text + URL match `pattern` (and not `exclude`, e.g. "camp"); youth teams never count. A URL that
    appears several times (image link without text, then the headline) keeps its longest text."""
    rx = re.compile(pattern, re.I)
    ex = re.compile(exclude, re.I) if exclude else None
    out: dict[str, str] = {}
    for url, text in links:
        hay = f"{text} {url}".lower()
        wanted = rx.search(hay) and not YOUTH.search(hay) and not (ex and ex.search(hay))
        if wanted and (url not in out or len(text) > len(out[url])):
            out[url] = text
    return list(out.items())


def article_text(body: str) -> str:
    """Readable text of an article page (scripts/styles dropped, block tags as line breaks)."""
    t = re.sub(r"(?s)<(script|style|nav|header|footer)[^>]*>.*?</\1>", "", body)
    t = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", t, flags=re.I)
    t = html_lib.unescape(TAG.sub("", t))
    return "\n".join(line.strip() for line in t.splitlines() if line.strip())


def parse_sihf_roster(text: str) -> list[dict]:
    """'Torhüterinnen (3): Andrea Brändli (PWHL Detroit / USA), …' → [{name, pos, club}] (SIHF article format)."""
    players: list[dict] = []
    for label, block in POS_BLOCK.findall(text):
        for name, club in re.findall(r"([^,()]+?)\s*\(([^)]*)\)", block):
            parts = [p.strip() for p in club.split("/") if p.strip()]
            country = parts[-1] if len(parts) > 1 and re.fullmatch(r"[A-Z]{3}", parts[-1]) else None
            clubs = [p for p in parts if p != country]
            p = {"name": name.strip(), "pos": POS[label]}
            if clubs:
                p["club"] = clubs[0]
            if country:
                p["country"] = country
            players.append(p)
    return players


def absent_sentences(text: str) -> list[str]:
    return [m_.group(0).strip() for m_ in ABSENT.finditer(text)][:6]


def _team(s: Session, code: str) -> m.Team | None:
    return s.scalar(select(m.Team).where(m.Team.key == national_team_key(code)))


def _confirmed(s: Session, ev: m.Event, team: m.Team) -> bool:
    rows = list(
        s.scalars(
            select(m.EventEntry.list_type).where(
                m.EventEntry.event_id == ev.id,
                m.EventEntry.team_id == team.id,
                m.EventEntry.role.in_(("G", "D", "F")),
            )
        )
    )
    return bool(rows) and all(r == "confirmed" for r in rows)


def _news_item(s: Session, team: m.Team, url: str, title: str, text: str) -> None:
    from klugscheisser.ingest.store import content_hash

    if s.scalar(select(m.Item.id).where(m.Item.url == url).limit(1)) is not None:
        return
    item = m.Item(
        source_type="other_media",
        platform="web",
        source_name=urlsplit(url).netloc.removeprefix("www."),
        url=url,
        title=title or "Offizieller WM-Kader",
        text=text,
        published_at=datetime.now(UTC),
        content_hash=content_hash(text, title),
        source_kind="official",
        level="full",
        language="de",
    )
    item.teams.append(m.ItemTeam(team_id=team.id))
    s.add(item)
    s.flush()


FED = {  # federation label in the stored source ("Kaderwatch: <label>, offizieller WM-Kader (…)")
    "SUI": "SIHF",
    "USA": "USA Hockey",
    "CAN": "Hockey Canada",
    "CZE": "ČSLH",
    "SWE": "Svenska Ishockeyförbundet",
    "FIN": "Suomen Jääkiekkoliitto",
    "JPN": "JIHF",
    "DEN": "Danmarks Ishockey Union",
    "GER": "DEB",
    "AUT": "ÖEHV",
}
MAX_DROP_CONFIRMED = 6  # an official list losing more players than this at once is reported, not applied
BOT_WALL = re.compile(r"just a moment|cf-chl|challenge-platform|captcha", re.I)


def _players(s: Session, ev: m.Event, team: m.Team) -> list[m.EventEntry]:
    return [
        e
        for e in s.scalars(
            select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id)
        )
        if not is_staff_entry(e.role)
    ]


def resolve(s: Session, players: list[dict], old: list[m.EventEntry]) -> list[tuple[dict, int | None]]:
    """(player, athlete id or None = new person). Exact name first; a name the source misspells ("Josefine
    Holngren") is matched only to a clear fuzzy hit among the players already on this team's list."""
    from klugscheisser.db.repo import find_athletes

    old_ids = {e.athlete_id for e in old}
    out = []
    for p in players:
        if p.get("athlete_id"):
            out.append((p, p["athlete_id"]))
            continue
        hits = find_athletes(s, p["name"])
        exact = [h for h in hits if h.how == "exact"]
        if exact:
            out.append((p, exact[0].athlete.id))
            continue
        near = [h for h in hits if h.athlete.id in old_ids and h.score >= 0.6]
        if len(near) == 1:
            p["athlete_id"], p["name_in_source"] = near[0].athlete.id, p["name"]
            out.append((p, near[0].athlete.id))
        else:
            out.append((p, None))
    return out


def apply_official(
    s: Session,
    ev: m.Event,
    team: m.Team,
    code: str,
    players: list[dict],
    source: str,
    source_url: str,
    source_date: date,
    state: dict,
    accept_drops: bool = False,
) -> dict[str, Any]:
    """Make `players` the official (confirmed) WCh list of `team`. Players no longer on it lose their event entry
    (kept in the roster history with the old list, seed/loader.record_removed); staff lines stay. Unchanged lists are
    not touched (source + date of the stored list stay)."""
    from klugscheisser.seed.loader import load_roster_data

    old = _players(s, ev, team)
    was_confirmed = bool(old) and all(e.list_type == "confirmed" for e in old)
    pairs = resolve(s, players, old)
    new_ids = {i for _p, i in pairs if i is not None}
    old_ids = {e.athlete_id for e in old}
    added = [p["name"] for p, i in pairs if i is None or i not in old_ids]
    from klugscheisser.seed.loader import GAME_SHEET_SOURCE

    # players added from a game sheet (late additions) stay when the official list does not name them yet
    removed = [
        e.athlete.full_name
        for e in old
        if e.athlete_id not in new_ids and not (e.source or "").startswith(GAME_SHEET_SOURCE)
    ]
    numbers_new = {i: p.get("number") for p, i in pairs if i is not None and p.get("number")}
    numbers_changed = any(
        e.jersey_number != numbers_new[e.athlete_id] for e in old if e.athlete_id in numbers_new
    )
    out: dict[str, Any] = {
        "alt": len(old),
        "alt_status": "offiziell" if was_confirmed else ("provisorisch" if old else "keiner"),
        "neu": len(players),
        "dazu": added,
        "weg": removed,
        "quelle": source,
        "url": source_url,
        "datum": f"{source_date:%d.%m.%Y}",
    }
    if was_confirmed and not added and not removed and not numbers_changed:
        return {**out, "status": "unverändert"}
    if was_confirmed and len(removed) > MAX_DROP_CONFIRMED and not accept_drops:
        return {
            **out,
            "status": f"NICHT übernommen: {len(removed)} {wording().players} fielen weg (bitte prüfen)",
        }
    kept = [
        {
            "athlete_id": e.athlete_id,
            "role": e.role,
            "source": e.source,
            "source_url": e.source_url,
            "list_type": e.list_type,
        }
        for e in s.scalars(
            select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id)
        )
        if is_staff_entry(e.role)
    ]
    load_roster_data(
        s,
        {
            "event": ev.key,
            "team": team.key,
            "list_type": "confirmed",
            "season": f"{ev.starts_at.year}-{str(ev.starts_at.year + 1)[2:]}" if ev.starts_at else None,
            "source": source,
            "source_url": source_url,
            "source_date": source_date.isoformat(),
            "players": players,
            "staff": kept,
        },
        f"kaderwatch {code}",
    )
    state.setdefault("applied", {})[code] = {
        "date": source_date.isoformat(),
        "source": source,
        "url": source_url,
        "n": len(players),
        "dazu": added,
        "weg": removed,
    }
    return {**out, "status": "übernommen"}


def _get(client, url: str):
    from klugscheisser.collectors.profiles.http import Disallowed

    try:
        r = client.get(url)
    except Disallowed as exc:
        raise RuntimeError(f"nicht erlaubt (robots.txt): {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"nicht erreichbar: {exc}") from exc
    if r.status_code >= 400:
        if BOT_WALL.search(r.text[:20000] or ""):
            raise RuntimeError(f"HTTP {r.status_code}, Bot-Schutz der Seite (nicht umgangen)")
        raise RuntimeError(f"HTTP {r.status_code}")
    return r


def official_list(client, kind: str, url: str) -> tuple[list[dict], str]:
    """(players, text) of an official list page of format `kind` (roster_parsers / parse_sihf_roster)."""
    from klugscheisser.jobs import roster_parsers as rp

    r = _get(client, url)
    if kind == "jihf_pdf":
        pdf = url if url.lower().endswith(".pdf") else None
        if pdf is None:
            pdfs = [
                u for u, txt in page_links(r.text, url) if u.lower().endswith(".pdf") and "メンバー" in txt
            ]
            if not pdfs:
                return [], ""
            pdf = pdfs[0]
        data = r.content if pdf == url else _get(client, pdf).content
        text = rp.pdf_text(data)
        return rp.parse_jihf_text(text), text
    text = article_text(r.text)
    if kind == "sihf":
        return parse_sihf_roster(text), text
    parser = rp.PARSERS.get(kind)
    return (parser(text) if parser else []), text


class Mirror:
    """IIHF tournament rosters as mirrored on the Hockey Canada stats site (collectors/hockeycanada.py): all teams,
    filled once the federations have entered them. Read at most once per run (~11 polite requests)."""

    def __init__(self, event_key: str, client=None) -> None:
        self.event_key, self.client, self._rosters, self.error = event_key, client, None, None

    def roster(self, code: str):
        if self._rosters is None:
            self._rosters = {}
            try:
                from klugscheisser.collectors.hockeycanada import event_by_key

                hc = self.client
                got, errors = hc.rosters(event_by_key(self.event_key))
                self._rosters = {r.team.code: r for r in got}
                self.error = "; ".join(errors) or None
            except KeyError:
                self.error = "Turnier nicht auf hockeycanada.ca"
            except Exception as exc:  # never stops the watcher
                self.error = str(exc)[:200]
        return self._rosters.get(code)


def mirror_players(r) -> list[dict]:
    return [
        {
            "name": p.name,
            "pos": p.position,
            **({"number": p.jersey} if p.jersey else {}),
            **({"club_text": p.club} if p.club else {}),
        }
        for p in r.players
        if p.position in ("G", "D", "F")
    ]


def run(
    s: Session,
    client=None,
    today: date | None = None,
    info_dir: Path | None = None,
    mirror_client=None,
    accept_drops: bool = False,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """One check of all watched teams of all tournaments whose watch is still open. Never raises per team.
    `only` = IOC codes to check (the game watch after a final: the two teams of that game).

    1. Announcement: a new link on the federation's listing page matching the team's `pattern` (not `exclude`, e.g.
       camp rosters) → official news item + finding (named in every tournament answer).
    2. List (also for teams already confirmed, so late replacements are seen): parsed from the official list page
       (`list_url`, else the announcement; format `parse`), else from the IIHF roster mirror on hockeycanada.ca. A
       list of >= MIN_PLAYERS that differs from the stored one replaces it (apply_official)."""
    from klugscheisser import tournament
    from klugscheisser.collectors.profiles.http import PoliteClient
    from klugscheisser.seed.loader import WATCH_SOURCE

    client = client or PoliteClient(min_delay_s=3.0)
    today = today or datetime.now(UTC).date()
    state = watch_state(s)
    found: dict = state.setdefault("found", {})
    report: dict[str, Any] = {"geprueft": [], "neu": [], "kader": {}, "fehler": {}, "uebersprungen": []}
    for key in tournament.info_events(info_dir):
        info = tournament.load_info(key, info_dir) or {}
        watch = info.get("roster_watch") or {}
        if not watch or (watch.get("until") and date.fromisoformat(watch["until"]) < today):
            continue
        ev = s.scalar(select(m.Event).where(m.Event.key == key))
        if ev is None:
            continue
        if mirror_client is None:  # same polite client (robots.txt, identified UA); 3 s between pages
            from klugscheisser.collectors.hockeycanada import HockeyCanadaClient

            mirror_client = HockeyCanadaClient(client=client)
        mirror = Mirror(key, mirror_client)
        for code, spec in (watch.get("teams") or {}).items():
            if only is not None and code not in only:
                continue
            team = _team(s, code)
            if team is None:
                continue
            report["geprueft"].append(code)
            errors: list[str] = []
            text = ""
            if spec.get("url") and code not in found and not _confirmed(s, ev, team):
                try:
                    r = _get(client, spec["url"])
                    links = page_links(r.text, spec["url"], r.headers.get("content-type", ""))
                    hits = matching(links, spec["pattern"], spec.get("exclude"))
                except Exception as exc:  # one federation down never stops the others
                    errors.append(f"Liste: {exc}")
                    hits = []
                if hits:
                    url, title = hits[0]
                    found[code] = {
                        "url": url,
                        "titel": title,
                        "seen": today.isoformat(),
                        "date_de": f"{today:%d.%m.%Y}",
                        "loaded": False,
                    }
                    report["neu"].append({"team": code, "url": url, "titel": title})
            entry = found.get(code) or {}
            players: list[dict] = []
            src_url, src_date, label = None, today, FED.get(code, code)
            list_url = spec.get("list_url") or (entry.get("url") if spec.get("parse") else None)
            if list_url and spec.get("parse") and spec["parse"] != "hockeycanada":
                try:
                    players, text = official_list(client, spec["parse"], list_url)
                    src_url = list_url
                    if spec.get("list_date") and list_url == spec.get("list_url"):
                        src_date = date.fromisoformat(spec["list_date"])
                    elif entry.get("seen"):
                        src_date = date.fromisoformat(entry["seen"])
                except Exception as exc:
                    errors.append(f"Kaderseite: {exc}")
                if players and len(players) < MIN_PLAYERS:
                    errors.append(
                        f"Kaderseite: nur {len(players)} {wording().players} gelesen, nicht übernommen"
                    )
                    players = []
            if not players and (spec.get("parse") == "hockeycanada" or spec.get("mirror", True)):
                hc = mirror.roster(code)
                got = mirror_players(hc) if hc is not None else []
                if len(got) >= MIN_PLAYERS:
                    players, src_url = got, hc.url
                    label = (
                        "Hockey Canada"
                        if code == "CAN"
                        else f"IIHF-Turnierkader (Spiegel auf hockeycanada.ca), Team {FED.get(code, code)}"
                    )
                    if entry.get("seen") and code == "CAN":
                        src_date = date.fromisoformat(entry["seen"])
                elif mirror.error and spec.get("parse") == "hockeycanada":
                    errors.append(f"Hockey Canada: {mirror.error}")
            if players:
                pub = f"{src_date:%d.%m.%Y}"
                source = f"{WATCH_SOURCE}: {label}, offizieller WM-Kader ({pub})"
                change = apply_official(
                    s, ev, team, code, players, source, src_url, src_date, state, accept_drops=accept_drops
                )
                report["kader"][code] = change
                if entry:
                    entry["loaded"] = change["status"] in ("übernommen", "unverändert")
                    entry["spielerinnen"] = len(players)
            if entry and text and code in [n["team"] for n in report["neu"]]:
                entry["fehlen_saetze"] = absent_sentences(text)
            if code in [n["team"] for n in report["neu"]]:
                _news_item(
                    s,
                    team,
                    entry["url"],
                    entry["titel"] or f"Offizieller WM-Kader {team.name}",
                    text[:4000] or f"Offizieller WM-Kader {team.name} publiziert: {entry['titel']}",
                )
            if errors:
                report["fehler"][code] = "; ".join(errors)
    state["last_run"] = datetime.now(UTC).isoformat()
    if only is None:  # a two-team check after a game keeps the last full report
        state["last_report"] = {k: v for k, v in report.items() if k != "uebersprungen"}
    _save(s, state)
    return report


def snapshot(s: Session, ev: m.Event) -> dict[str, dict[str, Any]]:
    """Per team (IOC code): players on the stored list, status, source and date (tournament.roster_status)."""
    from klugscheisser import tournament

    out = {}
    for team in s.scalars(select(m.Team).join(m.EventTeam).where(m.EventTeam.event_id == ev.id)):
        st = tournament.roster_status(s, ev, team)
        out[team.country or team.key] = {
            "team": team.name,
            "names": sorted(e.athlete.full_name for e in _players(s, ev, team)),
            "status": st["status"],
            "quelle": st.get("quelle"),
            "url": st.get("url"),
            "datum": st.get("datum"),
        }
    return out


def update_all(
    s: Session,
    event_key: str | None = None,
    seeds_dir: Path | None = None,
    web: bool = True,
    accept_drops: bool = False,
) -> dict[str, Any]:
    """One-off catch-up for all teams of one tournament: reload its roster seeds (hand-checked official lists, with
    the earlier lists they are based on), then one kaderwatch run (official pages + IIHF mirror). Returns the per-team
    change (before → after); the caller commits or rolls back (dry run)."""
    import yaml

    from klugscheisser import projects, staff
    from klugscheisser.seed.loader import load_roster

    event_key = event_key or projects.current().main_event  # "wch-2026" in the ice hockey project
    rosters = (seeds_dir or Path(projects.current().seeds_dir)) / "rosters"  # SEEDS_DIR for eishockey
    ev = s.scalar(select(m.Event).where(m.Event.key == event_key))
    if ev is None:
        raise ValueError(f"unknown event {event_key}")
    before = snapshot(s, ev)
    paths = sorted(rosters.glob(f"{event_key}-*.yaml"))
    bases = []
    for p in paths:
        base = (yaml.safe_load(p.read_text()) or {}).get("based_on")
        if base and (rosters / f"{base}-{p.stem.rsplit('-', 1)[-1]}.yaml").exists():
            bases.append(rosters / f"{base}-{p.stem.rsplit('-', 1)[-1]}.yaml")
    for p in bases + [p for p in paths if p not in bases]:
        load_roster(s, p)
    staff.sync_from_entries(s)
    watch = run(s, accept_drops=accept_drops) if web else None
    s.flush()
    after = snapshot(s, ev)
    teams = []
    for code in sorted(after, key=lambda c: (c != "SUI", after[c]["team"])):
        a, b = before.get(code) or {"names": [], "status": "-"}, after[code]
        teams.append(
            {
                "code": code,
                "team": b["team"],
                "vorher": f"{len(a['names'])} ({a['status']})",
                "nachher": f"{len(b['names'])} ({b['status']})",
                "dazu": sorted(set(b["names"]) - set(a["names"])),
                "weg": sorted(set(a["names"]) - set(b["names"])),
                "quelle": b["quelle"],
                "url": b["url"],
                "datum": b["datum"],
                **(
                    {"kaderwatch": watch["kader"][code]["status"]} if watch and code in watch["kader"] else {}
                ),
                **({"fehler": watch["fehler"][code]} if watch and code in watch["fehler"] else {}),
            }
        )
    return {"teams": teams, "kaderwatch_neu": (watch or {}).get("neu", [])}
