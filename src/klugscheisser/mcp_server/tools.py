"""Tool logic, independent of MCP so it can be tested directly. All outputs are German-labelled."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser import person_status
from klugscheisser.classify.base import vorsicht_reason
from klugscheisser.config import Settings, get_settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.domain.labels import date_de, staff_label
from klugscheisser.domain.names import normalize
from klugscheisser.embed.providers import get_embedder
from klugscheisser.handles.service import usable_handles
from klugscheisser.llm.providers import get_llm
from klugscheisser.mcp_server import caution
from klugscheisser.mcp_server.present import (
    athlete_head,
    envelope,
    handle_out,
    item_compact,
    item_out,
    sort_items,
)
from klugscheisser.projects import wording
from klugscheisser.retrieval.docs import Extra, find_documents, query_embedder
from klugscheisser.retrieval.search import Filters, Hit, athlete_team_filter, get_reranker, list_items, search
from klugscheisser.sports import current_spec, home_team_key

EVENT_ALIASES = {
    "wm": "wch",
    "weltmeisterschaft": "wch",
    "world championship": "wch",
    "worlds": "wch",
    "olympia": "oly",
    "olympics": "oly",
    "olympische": "oly",
}


class NotFound(Exception):
    pass


def parse_since(value: str | None, default_days: int) -> datetime:
    if not value:
        return datetime.now(UTC) - timedelta(days=default_days)
    v = value.strip().lower()
    if m_ := re.fullmatch(r"(\d+)\s*(d|t|tage?|days?)", v):
        return datetime.now(UTC) - timedelta(days=int(m_.group(1)))
    if m_ := re.fullmatch(r"(\d+)\s*(w|wochen?|weeks?)", v):
        return datetime.now(UTC) - timedelta(weeks=int(m_.group(1)))
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m."):
        try:
            d = datetime.strptime(v, fmt)
            if fmt == "%d.%m.":
                d = d.replace(year=datetime.now(UTC).year)
            return d.replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"Unbekanntes Datum '{value}'. Erlaubt: 2026-10-01, 01.10.2026, 7d, 2w")


# --- resolution helpers -------------------------------------------------------------------------


def resolve_athlete_or_candidates(s: Session, query: str) -> m.Athlete | dict[str, Any]:
    a = repo.resolve_athlete(s, query)
    if a is not None:
        return a
    matches = repo.find_athletes(s, query, limit=5)
    if not matches:
        if current_spec().men:  # 2026-11-24: men's projects (floorball)
            raise NotFound(
                f"Kein Athlet gefunden für '{query}'. Fehlt er? hinzufuegen(name='{query}', details=…) schlägt ihn vor."
            )
        raise NotFound(
            f"Keine Athletin gefunden für '{query}'. Fehlt sie? hinzufuegen(name='{query}', details=…) schlägt sie vor."
        )
    return {
        "mehrdeutig": True,
        "frage": f"Mehrere Treffer für '{query}'. Welche ist gemeint?",
        "kandidatinnen": [
            {
                "id": x.athlete.id,
                "name": x.athlete.full_name,
                "geburtsdatum": x.athlete.birth_date.isoformat() if x.athlete.birth_date else None,
            }
            for x in matches
        ],
    }


def resolve_event(s: Session, query: str) -> m.Event:
    q = normalize(query)
    exact = s.scalar(select(m.Event).where(m.Event.key == query.strip().lower()))
    if exact:
        return exact
    for word, key in EVENT_ALIASES.items():  # "WM" / "Weltmeisterschaft" -> "wch" (replaces the word)
        q = re.sub(rf"\b{re.escape(word)}\b", key, q)
    now = datetime.now(UTC)
    tokens = [t for t in q.split() if len(t) > 2 or t.isdigit()]
    best, best_score = None, 0.0
    for ev in s.scalars(select(m.Event)):
        hay = set(
            normalize(
                " ".join(
                    filter(
                        None,
                        [
                            ev.name,
                            ev.key.replace("-", " "),
                            ev.city,
                            ev.competition.name if ev.competition else "",
                        ],
                    )
                )
            ).split()
        )
        matched = sum(1 for t in tokens if t in hay or (len(t) >= 4 and any(h.startswith(t) for h in hay)))
        base = matched / max(len(tokens), 1)
        if base < 0.5 or matched < min(2, len(tokens)):
            continue  # text must match first (two words when there are two: "Tour de France" is no hockey tour)
        score = base + (0.1 if ev.ends_at is None or ev.ends_at >= now else 0)
        score += (
            0.2 if ev.kind != "game" else 0
        )  # "Södertälje", "EHT Lahti": the tournament, not one of its games
        if score > best_score:
            best, best_score = ev, score
    if best is None:
        raise NotFound(f"Kein Anlass gefunden für '{query}'. Verfügbare Anlässe: list_events.")
    return best


def resolve_team(s: Session, query: str) -> m.Team:
    q = normalize(query)
    if q in {"schweiz", "nati", "frauen nati", "sui", "switzerland", "swiss", "schweizer nati"}:
        return repo.get_team(s, home_team_key())
    team = repo.find_team(s, query)
    if team is None:
        raise NotFound(f"Kein Team gefunden für '{query}'.")
    return team


def recent_items(
    s: Session,
    athlete_ids: list[int],
    since: datetime,
    limit: int,
    platform: str | None = None,
    source_types: list[str] | None = None,
) -> list[m.Item]:
    f = Filters(athlete_ids=athlete_ids, since=since, source_types=source_types, platform=platform)
    return sort_items(list_items(s, f, limit=200))[:limit]


def freshen(s: Session, athletes: list[m.Athlete]) -> dict[str, Any]:
    """Update the asked athletes once per day; returns the status for the answer. MCP_REFRESH_NO_WAIT (default):
    start it in the background and answer with the stored data now; off: wait up to AUTO_REFRESH_WAIT_S.

    News and results are refreshed in the background (like the web chat), never inside the tool call: their
    collectors can take ~20 s when the caches have expired."""
    from klugscheisser.jobs.freshness import ensure_fresh

    athletes = [a for a in athletes if a is not None]
    if not athletes:
        return {"status": "aktuell"}
    settings = get_settings()
    wait = 0.0 if settings.mcp_refresh_no_wait else None  # None = auto_refresh_wait_s
    return ensure_fresh(s, settings, athletes, wait_s=wait, fast_sources_now=False).out()


# --- tools --------------------------------------------------------------------------------------


RECENT_INJURY_DAYS = 60  # a resolved injury of the last N days is still mentioned next to the roster line
STATUS_INJURY_DAYS = 365  # get_athlete shows the newest injury report of the last year with its "seither"


def _reason(item: m.Item) -> str:
    """'Verletzung 19.09.' / 'Kontroverse': the ~3-word reason of a Vorsicht item."""
    why = vorsicht_reason(item.classification) or "Vorsicht"
    return f"{why} {date_de(item.published_at)}" if why == "Verletzung" and item.published_at else why


def roster_flags(
    st: person_status.PersonStatus | None, vorsicht: list[tuple[m.Item, str | None]]
) -> dict[str, Any]:
    """Roster line extras: ⚠️ with a short reason (only with a stated reason), and for a recent injury that is
    resolved the status ("Verletzung 22.09., seither: im Aufgebot 13.10., 2 Tore 14.10.")."""
    out: dict[str, Any] = {}
    if vorsicht:
        out["vorsicht_grund"] = ", ".join(dict.fromkeys(_reason(i) for i, _n in vorsicht))
    inj = st.injury if st else None
    if inj and st.resolved() and (datetime.now(UTC).date() - inj.day).days <= RECENT_INJURY_DAYS:
        out["status_hinweis"] = f"{inj.short}, {st.since_md()}"
    return out


def status_out(st: person_status.PersonStatus | None) -> dict[str, Any] | None:
    """get_athlete '🩺 Aktueller Stand': newest injury report (last year) with what happened since, and her
    newest nomination / game."""
    if st is None:
        return None
    out: dict[str, Any] = {}
    inj = st.injury
    if inj and (datetime.now(UTC).date() - inj.day).days <= STATUS_INJURY_DAYS:
        out["verletzung"] = {
            "text": f"Letzte Verletzungsmeldung {inj.day:%d.%m.%Y}: {inj.text}",
            "quelle": inj.quelle,
            "url": inj.url,
            "seither": st.since_md()
            or "seither kein Aufgebot und kein Spiel in unseren Daten (Fitness offen)",
            "erledigt": st.resolved(),
        }
    if st.signals:
        last = st.signals[0]
        out["zuletzt"] = {"text": f"Zuletzt: {last.text}", "quelle": last.quelle, "url": last.url}
    return out or None


def _noted(pairs: list[tuple[m.Item, str | None]], full: bool = False) -> list[dict[str, Any]]:
    """Items of a Vorsicht / Zu beachten section; an older injury report carries its date note."""
    out = []
    for item, note in pairs:
        d = item_out(item) if full else item_compact(item)
        if note:
            d["hinweis_alt"] = note
        out.append(d)
    return out


WATCH_MIN_WORDS = 4  # "Summer Recap☀️🗺️", "In der 56." say nothing in a "Zu beachten" line


def _says_something(d: dict[str, Any]) -> bool:
    """A "Zu beachten" line needs a statement: at least WATCH_MIN_WORDS words (of 2+ letters) in its text."""
    text = d.get("titel") or d.get("zusammenfassung_de") or d.get("zusammenfassung") or ""
    return len(re.findall(r"[^\W\d_]{2,}", text)) >= WATCH_MIN_WORDS


def _notable(i: m.Item) -> bool:
    """⭐ Zu beachten: newsworthy items; routine game results stay in list_events / the results sections."""
    return i.classification.newsworthiness >= 3 and i.source_type != "official_result"


def get_event(s: Session, query: str, posts_per_player: int = 0) -> dict[str, Any]:
    """Event overview. The roster lists names (position, number, club) only; posts_per_player > 0 adds each
    player's Instagram handle and latest posts (long: ~25 lines per team)."""
    ev = resolve_event(s, query)
    from klugscheisser import tournament

    # v18: a tournament with a fact sheet (seeds/events) gets the sheet and, per team, the roster status; players
    # with posts only for Switzerland (not every player of every team)
    sheet = tournament.load_info(ev.key) is not None
    since = datetime.now(UTC) - timedelta(days=get_settings().posts_lookback_days)
    window = get_settings().caution_window_days
    teams = s.execute(
        select(m.Team, m.EventTeam.group_name)
        .join(m.EventTeam)
        .where(m.EventTeam.event_id == ev.id)
        .order_by(m.EventTeam.group_name, m.Team.name)
    ).all()
    entries = list(s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id)))
    if sheet:
        swiss_ids = {t.id for t, _g in teams if t.country == "SUI"}
        entries = [e for e in entries if e.team_id in swiss_ids]
    aktualisierung = freshen(s, [e.athlete for e in entries])
    states = person_status.statuses(s, [e.athlete for e in entries])
    by_team: dict[int, list[m.EventEntry]] = {}
    for e in entries:
        by_team.setdefault(e.team_id, []).append(e)

    team_blocks: list[dict[str, Any]] = []
    watch_items: list[tuple[m.Item, str | None]] = []
    for team, group in teams:
        es = by_team.get(team.id, [])
        block: dict[str, Any] = {"team": team.name, "gruppe": group, "schweiz": team.country == "SUI"}
        from klugscheisser import staff as staff_mod

        if line := staff_mod.compact(s, team, keys=staff_mod.COACH_KEYS | {"gm"}):
            block["staff"] = line  # v19: current coaching staff (frage 'Staff <Team>' for all of it)
        if media := staff_mod.contacts_compact(s, team):
            block["medien"] = media  # 2026-11-01: media manager / press officer with published email + phone
        if sheet and team.country != "SUI":
            st = tournament.roster_status(s, ev, team)
            block.update({k: v for k, v in st.items() if k != "team"})
            if not st["anzahl"]:
                block["kader"] = "Kader noch nicht erfasst."
            team_blocks.append(block)
            continue
        if not es:
            block["kader"] = "Kader noch nicht erfasst."
            team_blocks.append(block)
            continue
        list_types = {e.list_type for e in es}
        block["kader_status"] = "bestätigt" if list_types == {"confirmed"} else "provisorisch"
        from klugscheisser.seed.loader import GAME_SHEET_SOURCE

        # 2026-11-04: the list's source, not a late addition from a game sheet (that one is named per player)
        main = next((e for e in es if not (e.source or "").startswith(GAME_SHEET_SOURCE)), es[0])
        block["kader_quelle"] = {"quelle": main.source, "url": main.source_url}
        if (
            sheet
        ):  # v18: roster status for Switzerland too (date, absences, official list found by the watcher)
            st = tournament.roster_status(s, ev, team)
            block.update({k: v for k, v in st.items() if k != "team"})
        players = []
        order = {"G": 0, "D": 1, "F": 2}
        for e in sorted(es, key=lambda x: (order.get(x.role or "", 3), x.athlete.full_name)):
            if staff_mod.is_staff_entry(e.role):
                continue  # v19: staff is in block["staff"], not among the players
            a = e.athlete
            head = athlete_head(s, a)
            pool = recent_items(s, [a.id], since, 60)
            st = states.get(a.id)
            vorsicht = caution.select(pool, a.id, caution.is_caution, window, status=st)
            watch_items += caution.select(pool, a.id, _notable, window, status=st)
            player = {
                "name": a.full_name,
                "id": a.id,
                "position": head["position"] or staff_label(e.role),
                "nummer": e.jersey_number,
                "verein": (
                    {k: head["vereine_laut_quellen"][0].get(k) for k in ("team", "liga", "saison", "hinweis")}
                    if head["vereine_laut_quellen"]
                    else None
                ),
                "vorsicht": _noted(vorsicht),
                **roster_flags(st, vorsicht),
            }
            if (e.source or "").startswith(GAME_SHEET_SOURCE):  # late addition: say where she was seen
                player["nachgemeldet"] = {"quelle": e.source, "url": e.source_url}
            if posts_per_player > 0:  # detail view: handle + latest posts per player
                posts = recent_items(s, [a.id], since, posts_per_player, platform="instagram")
                player["instagram"] = (
                    {k: head["instagram"][0][k] for k in ("handle", "url", "status")}
                    if head["instagram"]
                    else None
                )
                player["neueste_posts"] = [item_compact(i) for i in posts]
            players.append(player)
        block["spielerinnen"] = players
        team_blocks.append(block)

    notes = {i.id: note for i, note in watch_items}
    seen, watch = set(), []
    fresh_first = sorted(sort_items([i for i, _n in watch_items]), key=lambda i: notes[i.id] is not None)
    for i in fresh_first:  # vorsicht, newsworthiness, recency; older injury reports (with date note) last
        if i.id not in seen:
            seen.add(i.id)
            watch += [d for d in _noted([(i, notes[i.id])]) if _says_something(d)]
    lineup = (
        "Bestätigter Kader."
        if entries and all(e.list_type == "confirmed" for e in entries)
        else "Kader PROVISORISCH – nicht die offizielle Matchaufstellung. Im Gespräch so kennzeichnen."
    )
    return envelope(
        s,
        athlete_ids=[e.athlete_id for e in entries],
        anlass={
            "name": ev.name,
            "key": ev.key,
            "von": date_de(ev.starts_at, True),
            "bis": date_de(ev.ends_at, True),
            "ort": ev.city,
            "status": ev.status,
            "url": ev.source_url,
        },
        aktualisierung=aktualisierung,
        lineup_status=lineup,
        details="Kurzansicht: Kader nur mit Position und Verein. Instagram-Handles: get_team_roster(team, "
        f"'{ev.key}') oder get_ig_handles(team=…); Posts, Bio und News pro {wording().player}: get_athlete bzw. "
        "get_athlete_posts. ⭐ Zu beachten: nur offizielle/Medien-Quellen der letzten "
        f"{window} Tage über {wording().players} im Kader."
        + (
            f" Modus, ganzer Spielplan, Viertelfinal, Abstieg, Geschichte: frage(…); {wording().players} anderer Teams: "
            f"get_team_roster(team, '{ev.key}')."
            if sheet
            else ""
        ),
        zu_beachten=watch[:8],
        **(
            {
                "turnier": tournament.public(
                    tournament.answer(s, ev, "Schweiz", topics=["uebersicht", "spielplan"])
                )["abschnitte"]
            }
            if sheet
            else {}
        ),
        teams=team_blocks,
        # with a fact sheet the schedule is in `turnier`: only played/live games here (results), no repeat
        spiele=event_games(s, ev, results_only=sheet),
    )


def event_games(s: Session, ev: m.Event, results_only: bool = False) -> list[dict[str, Any]]:
    """Stored games of a tournament (schedule and results, Swiss time), for get_event; `results_only`: only games
    that are live or final."""
    from klugscheisser.answer.games import game_when, tournament_games

    if ev.kind == "game":
        return []
    return [
        {
            "datum": game_when(g),
            "spiel": g.name,
            "resultat": g.score if g.status != "scheduled" else None,
            "status": g.status,
            "ort": g.venue,
            "quelle": g.source,
            "url": g.source_url,
        }
        for g in tournament_games(s, ev)
        if not results_only or g.status != "scheduled"
    ]


def get_team_roster(
    s: Session, team: str, competition: str | None = None, key_players: bool = False
) -> dict[str, Any]:
    """`key_players` (MCP `team`, switch TEAM_KEY_PLAYERS): for a national team other than Switzerland also the
    block "Schlüsselspielerinnen aus Schweizer Sicht" (mcp_server/key_players.py)."""
    t = resolve_team(s, team)
    q = select(m.EventEntry).join(m.Event).where(m.EventEntry.team_id == t.id)
    if competition:
        q = q.where(m.Event.id == resolve_event(s, competition).id)
    entries = list(s.scalars(q.order_by(m.Event.starts_at.desc().nulls_last())))
    from klugscheisser import staff

    ev_staff: list[m.EventEntry] = []
    players: dict[int, dict] = {}
    source = None
    if entries:
        newest_event = entries[0].event_id
        entries = [e for e in entries if e.event_id == newest_event]
        # 2026-10-28: the roster's source is a player line's (staff lines may cite another document)
        head_entry = next((e for e in entries if not staff.is_staff_entry(e.role)), entries[0])
        source = {
            "anlass": s.get(m.Event, newest_event).name,
            "quelle": head_entry.source,
            "url": head_entry.source_url,
            "status": head_entry.list_type,
        }
        athletes = [e.athlete for e in entries if not staff.is_staff_entry(e.role)]
        ev_staff = [e for e in entries if staff.is_staff_entry(e.role)]
    else:
        affs = list(s.scalars(select(m.Affiliation).where(m.Affiliation.team_id == t.id)))
        athletes = [a.athlete for a in affs if a.athlete.role == "athlete"]
        source = {
            "quelle": "Vereinszugehörigkeit laut Quellen (Wikidata/Kaderlisten)",
            "status": "unvollständig",
        }
    states = person_status.statuses(s, athletes)
    since = datetime.now(UTC) - timedelta(days=get_settings().posts_lookback_days)
    window = get_settings().caution_window_days
    deutsch = german_cells(s, [*athletes, *(e.athlete for e in ev_staff)], t)
    for a in athletes:
        head = athlete_head(s, a)
        st = states.get(a.id)
        pool = recent_items(s, [a.id], since, 60)
        players[a.id] = {
            "name": a.full_name,
            "id": a.id,
            "nation": a.nationality,
            "position": head["position"],
            "instagram": head["instagram"][:1],
            **roster_flags(st, caution.select(pool, a.id, caution.is_caution, window, status=st)),
        }
        if a.id in deutsch:
            players[a.id]["deutsch"] = deutsch[a.id]
    staff_rows = staff.staff_lines(s, t)
    if get_settings().german_hints:  # staff of other nations: the same Deutsch cell after the staff line
        extra = german_cells(s, [r for r in (s.get(m.Athlete, z.get("id")) for z in staff_rows) if r], t)
        for z in staff_rows:
            if z.get("id") in extra:
                z["deutsch"] = extra[z["id"]]
    deutsch_staff = [
        f"{staff.LABEL.get(staff.role_of(e.role), e.role)}: {e.athlete.full_name} – Deutsch: {deutsch[e.athlete_id]}"
        for e in ev_staff
        if e.athlete_id in deutsch
        and e.athlete.full_name not in " ".join(z.get("text", "") for z in staff_rows)
    ]
    listed = " ".join(z.get("text", "") for z in staff_rows)
    block: list[str] = []
    if key_players:
        from klugscheisser.mcp_server import key_players as kp

        roles = [(e.athlete, staff.LABEL.get(staff.role_of(e.role), e.role or "Staff")) for e in ev_staff]
        block = kp.block(s, t, athletes, players, roles)
    return envelope(
        s,
        athlete_ids=[p["id"] for p in players.values()],
        team=t.name,
        liga=t.league,
        kader_quelle=source,
        spielerinnen=sorted(players.values(), key=lambda p: p["name"]),
        # v19: the team's current staff (with source) and the staff named on this event's roster
        staff=staff_rows,
        # 24.10.: only roster staff not already in the staff list (each person once)
        staff_im_aufgebot=[
            f"{staff.LABEL.get(staff.role_of(e.role), e.role)}: {e.athlete.full_name}"
            for e in ev_staff
            if e.athlete.full_name not in listed
        ],
        deutsch_staff=deutsch_staff,
        **({"deutsch_regel": GERMAN_RULE} if deutsch or any("deutsch" in z for z in staff_rows) else {}),
        **({"schluesselspielerinnen": block} if block else {}),
    )


GERMAN_RULE = (
    "Deutsch: «bestätigt» nur mit deutschem Interview (Quelle + Datum); «wahrscheinlich» = Hinweis wie Saisons bei "
    "einem deutschsprachigen Club (seeds/club_languages.yaml), mit Grund; «kein Hinweis» nennt Schweizer Clubs, die "
    "nicht deutschsprachig sind (Tessin, Romandie). Nur Personen anderer Nationen als SUI/GER/AUT/LIE; nie aus Name "
    "oder Nationalität geschlossen."
)


def german_cells(s: Session, athletes: list[m.Athlete], team: m.Team | None = None) -> dict[int, str]:
    """athlete id -> roster cell 'wahrscheinlich – 2 Saisons EV Zug (2024/25–2025/26)' (GERMAN_HINTS)."""
    if not get_settings().german_hints or not athletes:
        return {}
    from klugscheisser.facts import german

    nations = german.nations_of(s, athletes)
    if team is not None and team.kind == "national" and team.country:
        nations = {aid: nat or team.country for aid, nat in nations.items()}
    return {aid: c for aid, h in german.hints(s, athletes, nations=nations).items() if (c := h.cell())}


def german_line(s: Session, a: m.Athlete) -> dict[str, Any] | None:
    """get_athlete: {'text': 'Deutsch: wahrscheinlich – …', 'quellen': […]} or None (GERMAN_HINTS)."""
    if not get_settings().german_hints:
        return None
    from klugscheisser.facts import german

    h = german.hint(s, a)
    return h.out() if h is not None and h.line() else None


def get_athlete(s: Session, name: str) -> dict[str, Any]:
    a = resolve_athlete_or_candidates(s, name)
    if isinstance(a, dict):
        return envelope(s, **a)
    aktualisierung = freshen(s, [a])
    since = datetime.now(UTC) - timedelta(days=get_settings().posts_lookback_days)
    all_recent = recent_items(s, [a.id], since, 200)
    st = person_status.status_of(s, a)
    vorsicht = caution.select(
        all_recent, a.id, caution.is_caution, get_settings().caution_window_days, status=st
    )
    events = s.execute(
        select(m.Event, m.EventEntry)
        .join(m.EventEntry)
        .where(m.EventEntry.athlete_id == a.id)
        .order_by(m.Event.starts_at.desc().nulls_last())
    ).all()
    from klugscheisser.mcp_server.stats_tools import ep_block

    return envelope(
        s,
        athlete_ids=[a.id],
        aktualisierung=aktualisierung,
        athletin=athlete_head(s, a),
        **ep_block(s, a),  # v11: profile fields, statistik (season, career per league, form), awards
        deutsch=german_line(s, a),
        vorsicht=_noted(vorsicht, full=True),
        aktueller_stand=status_out(st),
        neueste_instagram_posts=[item_out(i) for i in all_recent if i.platform == "instagram"][:6],
        news=[item_out(i) for i in all_recent if i.platform == "rss"][:4],
        resultate=[item_out(i) for i in all_recent if i.source_type == "official_result"][:5],
        anlaesse=[
            {
                "anlass": ev.name,
                "datum": date_de(ev.starts_at, True),
                "kader_status": e.list_type,
                "quelle": e.source,
                "url": e.source_url,
            }
            for ev, e in events
        ],
    )


def get_athlete_posts(
    s: Session, name: str, since: str | None = None, limit: int = 10, include_tagged: bool = True
) -> dict[str, Any]:
    a = resolve_athlete_or_candidates(s, name)
    if isinstance(a, dict):
        return envelope(s, **a)
    aktualisierung = freshen(s, [a])
    types = ["athlete_post", "tagged_post", "srf"] if include_tagged else ["athlete_post"]
    f = Filters(
        athlete_ids=[a.id],
        since=parse_since(since, get_settings().posts_lookback_days),
        source_types=types,
        platform="instagram",
    )
    items = list_items(s, f, limit=limit)
    handles = usable_handles(a)
    last = max((h.last_scraped_at for h in handles if h.last_scraped_at), default=None)
    return envelope(
        s,
        athlete_ids=[a.id],
        aktualisierung=aktualisierung,
        athletin=a.full_name,
        instagram=[handle_out(h) for h in handles],
        zuletzt_abgerufen=date_de(last, True) if last else "noch nie",
        hinweis_posts=(
            "Neueste zuerst. 'eigener Instagram-Post' = von ihm selbst; sonst Posts anderer Accounts, "
            "in denen er markiert oder erwähnt ist."
            if current_spec().men
            else "Neueste zuerst. 'eigener Instagram-Post' = von ihr selbst; sonst Posts anderer Accounts, "
            "in denen sie markiert oder erwähnt ist."
        ),
        vorsicht=[item_out(i) for i in items if caution.is_caution(i)],
        posts=[item_out(i) for i in items],
    )


def get_ig_handles(s: Session, athletes: list[str] | None = None, team: str | None = None) -> dict[str, Any]:
    targets: list[m.Athlete] = []
    missing = []
    if team:
        roster = get_team_roster(s, team)
        targets += [s.get(m.Athlete, p["id"]) for p in roster["spielerinnen"]]
    for name in athletes or []:
        a = repo.resolve_athlete(s, name)
        (targets.append(a) if a else missing.append(name))
    out = []
    for a in targets:
        hs = usable_handles(a)
        out.append(
            {
                "name": a.full_name,
                "handles": [handle_out(h) for h in hs] or "Kein bestätigter Instagram-Account bekannt.",
            }
        )
    return envelope(s, athlete_ids=[a.id for a in targets], athletinnen=out, nicht_gefunden=missing or None)


def whats_new(s: Session, target: str, since: str | None = None, limit: int = 25) -> dict[str, Any]:
    start = parse_since(since, 7)
    f: Filters
    label = target
    try:
        a = repo.resolve_athlete(s, target)
    except Exception:
        a = None
    if a is not None:
        f, label = Filters(athlete_ids=[a.id]), a.full_name
    else:
        try:
            t = resolve_team(s, target)
            f, label = athlete_team_filter(s, t), t.name
        except NotFound:
            ev = resolve_event(s, target)
            ids = list(s.scalars(select(m.EventEntry.athlete_id).where(m.EventEntry.event_id == ev.id)))
            team_ids = list(s.scalars(select(m.EventTeam.team_id).where(m.EventTeam.event_id == ev.id)))
            f, label = Filters(athlete_ids=ids, team_ids=team_ids), ev.name
    f.since = start
    aktualisierung = freshen(s, [s.get(m.Athlete, i) for i in f.athlete_ids or []])
    items = list_items(s, f, limit=limit)
    return envelope(
        s,
        athlete_ids=f.athlete_ids,
        aktualisierung=aktualisierung,
        ziel=label,
        seit=date_de(start, True),
        anzahl=len(items),
        vorsicht=[item_out(i) for i in items if caution.is_caution(i)],
        eintraege=[item_out(i) for i in items],
    )


def search_tool(
    s: Session,
    settings: Settings,
    query: str,
    athlete: str | None = None,
    team: str | None = None,
    since: str | None = None,
    source_type: str | None = None,
    limit: int = 8,
    cards: bool | None = None,
    card_chars: int | None = None,
    newest: bool = False,
) -> dict[str, Any]:
    """MCP search. `cards`: evidence cards (None = MCP_SEARCH_CARDS; the Phase F `suche` always uses them);
    `card_chars`: their size target (default cards.TARGET_CHARS); `newest`: the newest documents of the filters
    instead of the ranked search (no Jev; like whats_new)."""
    use_cards = settings.mcp_search_cards if cards is None else cards
    f = Filters()
    if athlete:
        a = repo.resolve_athlete(s, athlete)
        if a is None:
            raise NotFound(
                f"{'Kein Athlet' if current_spec().men else 'Keine Athletin'} gefunden für '{athlete}'."
            )
        f.athlete_ids = [a.id]
    if team:
        tf = athlete_team_filter(s, resolve_team(s, team))
        f.athlete_ids, f.team_ids = (f.athlete_ids or tf.athlete_ids), tf.team_ids
    if since:
        f.since = parse_since(since, 30)
    if source_type:
        f.source_types = [x.strip() for x in source_type.split(",")]
    aktualisierung = freshen(s, [s.get(m.Athlete, i) for i in f.athlete_ids or []])
    intent = detect_intent(query)
    if newest:

        def run(f: Filters) -> list:
            return [Hit(i, 1.0, {}) for i in list_items(s, f, limit=limit)]
    else:
        run = _search_runner(s, settings, query, limit, intent=intent)
    hits = run(f)
    erweitert = None
    if f.since and not hits:  # nothing that recent: show older context instead of nothing
        hits = run(replace(f, since=f.since - timedelta(days=30)))
        erweitert = (
            f"Keine Treffer seit {date_de(f.since, True)}; gezeigt werden ältere Einträge der letzten "
            f"30 Tage davor. In der Antwort so kennzeichnen."
        )
    if use_cards:
        from klugscheisser.mcp_server import cards as cards_mod
        from klugscheisser.retrieval.docs import search_terms

        seen: set[int] = set()
        unique = [h for h in hits if not (h.item.id in seen or seen.add(h.item.id))][:limit]
        filt = ", ".join(
            f"{k}={v}"
            for k, v in (("athlete", athlete), ("team", team), ("since", since), ("source_type", source_type))
            if v
        )
        return envelope(
            s,
            athlete_ids=f.athlete_ids,
            aktualisierung=aktualisierung,
            frage=query,
            filter=filt or None,
            karten=cards_mod.build(unique, search_terms(query), target=card_chars),
            fuss=cards_mod.footer(len(unique), 0, limit, erweitert),
        )
    return envelope(
        s,
        erweitert=erweitert,
        athlete_ids=f.athlete_ids,
        aktualisierung=aktualisierung,
        frage=query,
        erkannt=intent or None,
        anzahl=len(merged := _merge(hits, limit)),
        treffer=merged,
    )


def dokument_tool(s: Session, id: int, teil: int = 1) -> dict[str, Any]:
    from klugscheisser.mcp_server import cards

    return envelope(s, **cards.dokument(s, id, teil))


def _search_runner(s: Session, settings: Settings, query: str, limit: int, intent=None, reranker=None):
    """Filters -> hits. Default: the shared path (retrieval/docs.find_documents): candidates from full text, the
    nearest vectors, the Jev labels of `intent` (e.g. category injury) and the newest documents; Jev scores all of
    them. MCP_SEARCH_EMBEDDINGS=true: the old hybrid search (labels then shown first, unranked)."""
    if settings.mcp_search_embeddings:
        embedder, rr = get_embedder(settings), reranker or get_reranker(settings, get_llm(settings))

        def old(f: Filters) -> list:
            by_label = list_items(s, replace(f, **intent), limit=limit) if intent else []
            hits = search(s, embedder, rr, query, f, top_n=limit, min_score=settings.rerank_min_score)
            return [Hit(i, 1.0, {"label_match": True}) for i in by_label] + hits

        return old
    embedder = query_embedder(settings)

    def new(f: Filters) -> list:
        extra = [Extra(replace(f, **intent), LABEL_CANDIDATES)] if intent else None
        return find_documents(
            s,
            settings,
            query,
            f,
            top_n=limit,
            min_score=settings.rerank_min_score,
            extra=extra,
            reranker=reranker,
            embedder=embedder,
            group_stories=settings.search_group_stories,
        )

    return new


LABEL_CANDIDATES = 40  # newest documents per Jev label (intent) that go into the ranked pool


INTENTS: list[tuple[re.Pattern, dict[str, Any]]] = [
    (
        re.compile(
            r"verletz|injur|bless|krank|reha|operation|ausfall|kreuzband|meniskus|gehirnersch|concussion|"
            r"\b(acl|lca)\b|fraktur|fractur",
            re.I,
        ),
        {"categories": ["injury"]},
    ),
    (
        re.compile(r"rücktritt|ruecktritt|karriereende|aufhör|retire|abschied|pause", re.I),
        {"storylines": ["karriereende"]},
    ),
    (
        re.compile(r"transfer|wechsel|vertrag|unterschreib|signed|draft|neuer verein|neues team", re.I),
        {"storylines": ["transfer"]},
    ),
    (re.compile(r"sponsor|werbung|partner|kooperation", re.I), {"categories": ["sponsor"]}),
    (re.compile(r"liebe|freund|beziehung|verlob|hochzeit|privat|familie|baby", re.I), {"moods": ["liebe"]}),
    (re.compile(r"heikel|vorsicht|sensibel|tabu", re.I), {"only_vorsicht": True}),
    (re.compile(r"comeback|rückkehr|zurück", re.I), {"storylines": ["comeback"]}),
    (re.compile(r"rekord|meilenstein|jubiläum|milestone", re.I), {"storylines": ["milestone"]}),
]


def detect_intent(query: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rx, add in INTENTS:
        if rx.search(query):
            for k, v in add.items():
                out[k] = (out.get(k) or []) + v if isinstance(v, list) else v
    return out


def _merge(hits: list, limit: int) -> list[dict[str, Any]]:
    seen, out = set(), []
    for h in hits:
        if h.item.id not in seen:
            seen.add(h.item.id)
            out.append(item_out(h.item, h.scores))
            if h.scores.get("auch"):  # story groups: the same story from other sources (SEARCH_GROUP_STORIES)
                out[-1]["auch_berichtet"] = h.scores["auch"]
    return out[:limit]


LIST_PAST_DAYS = 14  # finished national-team games shown by list_events
LIST_GAMES_MAX = 10


def _when_short(dt: datetime | None) -> str:
    from klugscheisser.domain.labels import local

    d = local(dt)
    if d is None:
        return "ohne Datum"
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()]
    return f"{wd} {d:%d.%m.%Y}" + (f" {d:%H:%M}" if (d.hour, d.minute) != (0, 0) else "")


def _game_out(g: m.Event) -> dict[str, Any]:
    out = {
        "key": g.key,
        "datum": _when_short(g.starts_at),
        "name": g.name,
        "resultat": g.score if g.status in ("final", "live") and g.score else None,
        "ort": g.city,
        "wettbewerb": g.competition.name if g.competition else None,
        "quelle": g.source,
        "url": g.source_url,
    }
    return {k: v for k, v in out.items() if v}


def list_events(
    s: Session,
    days: int = 21,
    team: str | None = None,
    last: int | None = None,
    since: str | None = None,
    opponent: str | None = None,
) -> dict[str, Any]:
    """Tournaments as containers (with their game count and next game), single national-team games only in a
    window (last LIST_PAST_DAYS days, next `days` days), club games only counted per league. `team` narrows
    everything to one team and lists its last played games from ALL stored games (every competition, also
    tournament games; `last` N, default 5, or all `since`); `opponent` → the head-to-head of the two teams."""
    from klugscheisser.answer import team_games as tgm
    from klugscheisser.answer.event_games import effective_end, event_games

    now = datetime.now(UTC)
    until = now + timedelta(days=max(1, min(days, 120)))
    t = resolve_team(s, team) if team else None
    if opponent and t is None:
        raise ValueError("opponent nur zusammen mit team (z. B. team='Schweiz', opponent='Österreich').")
    opp = resolve_team(s, opponent) if opponent else None
    tg = (
        tgm.team_games(s, t, opponent=opp, last=last, since=parse_since(since, 0) if since else None, now=now)
        if t is not None
        else None
    )
    containers = list(
        s.scalars(
            select(m.Event)
            .where(m.Event.kind != "game")
            .where(or_(m.Event.ends_at.is_(None), m.Event.ends_at >= now - timedelta(days=7)))
            .order_by(m.Event.starts_at.nulls_last())
        )
    )
    tournament_comps = set(
        s.scalars(
            select(m.Event.competition_id).where(m.Event.kind != "game", m.Event.competition_id.is_not(None))
        )
    )
    nationals = {x.id: x for x in s.scalars(select(m.Team).where(m.Team.kind == "national"))}
    swiss = {i for i, x in nationals.items() if x.country == "SUI"}

    def plays(g: m.Event, ids: set[int]) -> bool:
        return bool({g.home_team_id, g.away_team_id} & ids)

    anlaesse = []
    for ev in containers:
        games = (
            list(
                s.scalars(
                    select(m.Event)
                    .where(m.Event.kind == "game", m.Event.competition_id == ev.competition_id)
                    .order_by(m.Event.starts_at.nulls_last())
                )
            )
            if ev.competition_id is not None
            else []
        )
        if t is not None:
            in_event = s.scalar(
                select(m.EventTeam.team_id).where(m.EventTeam.event_id == ev.id, m.EventTeam.team_id == t.id)
            )
            if in_event is None and not any(plays(g, {t.id}) for g in games):
                continue
        if ev.starts_at and ev.starts_at > now:
            stand = (
                f"in {(ev.starts_at - now).days} Tagen" if (ev.starts_at - now).days > 0 else "beginnt heute"
            )
        elif (end := effective_end(ev, games or event_games(s, ev))) is not None and end < now:
            stand = "beendet"  # 24.10.: no stored end date but all games played → not "läuft"
        else:
            stand = "läuft"
        summary = None
        if games:
            done = sum(1 for g in games if g.status == "final")
            focus = {t.id} if t is not None else swiss
            upcoming = [
                g for g in games if g.status != "final" and (g.starts_at is None or g.starts_at >= now)
            ]
            nxt = next((g for g in upcoming if plays(g, focus)), None) or (upcoming[0] if upcoming else None)
            summary = f"{len(games)} Spiele" + (f", {done} gespielt" if done else "")
            if nxt is not None:
                summary += f"; nächstes: {_when_short(nxt.starts_at)} {nxt.name}"
        anlaesse.append(
            {
                "key": ev.key,
                "name": ev.name,
                "von": date_de(ev.starts_at, True),
                "bis": date_de(ev.ends_at, True),
                "ort": ev.city,
                "stand": stand,
                "spiele": summary,
                "quelle": ev.source,
                "url": ev.source_url,
            }
        )

    singles = [
        g
        for g in s.scalars(
            select(m.Event)
            .where(m.Event.kind == "game")
            .where(m.Event.starts_at >= now - timedelta(days=LIST_PAST_DAYS), m.Event.starts_at <= until)
            .order_by(m.Event.starts_at)
        )
        if g.competition_id not in tournament_comps
    ]
    if t is not None:
        shown = [g for g in singles if plays(g, {t.id})]
        club: list[m.Event] = []
    else:
        shown = [g for g in singles if plays(g, set(nationals))]
        club = [g for g in singles if not plays(g, set(nationals)) and g.starts_at >= now - timedelta(days=3)]
    past = [g for g in shown if g.starts_at < now][::-1][:LIST_GAMES_MAX]
    nxt = [g for g in shown if g.starts_at >= now][:LIST_GAMES_MAX]
    if opp is not None:
        nxt = [g for g in nxt if plays(g, {opp.id})]
    leagues: dict[str, list[m.Event]] = {}
    for g in club:
        home = s.get(m.Team, g.home_team_id) if g.home_team_id else None
        label = (
            (home.league if home and home.league else None)
            or (g.competition.name if g.competition else None)
            or g.source
            or g.key.split("-")[0].upper()
        )
        leagues.setdefault(label, []).append(g)
    klubspiele = [
        {
            "liga": label,
            "anzahl": len(gs),
            "von": date_de(gs[0].starts_at, False),
            "bis": date_de(gs[-1].starts_at, False),
        }
        for label, gs in sorted(leagues.items(), key=lambda x: -len(x[1]))
    ]
    if tg is not None:
        if opp is not None and tg.upcoming is not None and tg.upcoming not in nxt:
            nxt = [tg.upcoming, *nxt]
        letzte = [tgm.game_row(g, {t.id}) for g in tg.games]
        extra = {
            "bilanz": tgm.record(tg.games, {t.id}),
            "gespeichert": tgm.coverage_note(tg),
        }
        window = (
            f"Letzte Spiele: {'Direktvergleich mit ' + opp.name if opp is not None else t.name}, alle gespeicherten "
            f"Wettbewerbe ("
            + (f"seit {since}" if since else f"die letzten {len(tg.games)}")
            + "); "
            + (
                f"nächste Spiele: Duelle mit {opp.name} der nächsten {days} Tage"
                if opp is not None
                else f"nächste Spiele: Einzelspiele von {t.name} der nächsten {days} Tage; Turnierspiele stehen "
                "beim Turnier (nächstes Spiel) und in get_event(key)"
            )
        )
    else:
        letzte = [_game_out(g) for g in past]
        extra = {}
        window = (
            f"Fenster: Spiele der letzten {LIST_PAST_DAYS} und der nächsten {days} Tage"
            ", Nationalteams einzeln, Klubspiele gezählt"
        )
    return envelope(
        s,
        anlaesse=anlaesse,
        spiele_letzte=letzte,
        spiele_naechste=[_game_out(g) for g in nxt],
        klubspiele=klubspiele,
        **{k: v for k, v in extra.items() if v},
        mehr=(
            window + ". Mehr: get_event(key) für einen Anlass; frage('Spielplan …') für Turnierspiele; "
            "list_events(team='…', last=N) für mehr Spiele eines Teams, list_events(team='…', opponent='…') "
            "für den Direktvergleich."
        ),
    )
