"""v19: team staff (head coach, assistants, goalie coach, GM, team manager, physio, doctor …) with history.

Where the data comes from
- `seeds/staff/<team>.yaml`: appointments checked by hand from official pages / agency reports, with since/until
  and a verbatim quote (e.g. the coaching change Colin Muller → Shannon Miller at the Swiss women's team).
- Every roster list that names staff: seed rosters (`seeds/rosters/*.yaml` `staff:` blocks), official lists loaded
  by the roster watcher (both through `seed.loader.load_roster_data`), and `sync_from_entries` for staff lines
  already stored in `event_entries` (Wikipedia rosters, older loads).

Who counts as current: an appointment with an `until` in the past is history. For one-holder roles (head coach,
GM) the person with the newest evidence is current and older holders are history ("abgelöst"). For the others, a
person whose newest evidence is more than STALE_DAYS older than the team's newest staff evidence is shown as
"zuletzt belegt" history, not as current staff.

Answers (`answer` / `render`): MCP `frage` routes questions about Trainer/Coach/Staff/Betreuer/Assistenz here
(team-level answer, not the players' documents); `get_team_roster` / `get_event` show the staff block; the web
chat's coach lookup (answer/lookups.py) uses `current` first."""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)

STAFF_DIR = Path(__file__).resolve().parents[2] / "seeds" / "staff"
STALE_DAYS = 180
PLAYER_ROLES = (None, "", "G", "D", "F")

# (key, pattern on the source's role label, German label, plural label). First match wins: specific before generic.
ROLES: list[tuple[str, str, str, str]] = [
    (
        "head_coach",
        r"head ?coach|cheftrainer\w*|headcoach|nationaltrainer\w*|hlavn[ií] tren[eé]r|päävalmentaja"
        r"|förbundskapten|bundestrainer\w*",
        "Headcoach",
        "Headcoach",
    ),
    (  # 2026-11-01 (Hockey Canada): before assistant_coach, never a second GM (one-holder role)
        "assistant_gm",
        r"assist\w* (general manager|gm)\b",
        "Assistant General Manager",
        "Assistant General Manager",
    ),
    (
        "assistant_coach",
        r"assist\w*|co-?trainer\w*|associate coach|asistent",
        "Assistenztrainer:in",
        "Assistenztrainer:innen",
    ),
    (
        "goalie_coach",
        r"goal\w* ?coach|goalie-?trainer\w*|torh[üu]ter\w*|torwart\w*|brankář",
        "Goalie-Trainer:in",
        "Goalie-Trainer:innen",
    ),
    ("video_coach", r"video", "Videocoach", "Videocoaches"),
    (
        "performance_coach",
        r"performance|strength|conditioning|athlet\w* coach|athletik\w*|fitness",
        "Athletik-/Performance-Coach",
        "Athletik-/Performance-Coaches",
    ),
    ("skills_coach", r"skills?", "Skills-Coach", "Skills-Coaches"),
    (
        "media",
        r"m[eé]dia|medien\w*|press\w*|communicat\w*|kommunikation\w*|content|tiskov\w*|viestint\w*|tiedott\w*"
        r"|kommunikat\w*|広報",
        "Medienverantwortliche:r",
        "Medienverantwortliche",
    ),
    (
        "gm",
        r"general manager|\bgm\b|head of women|head women|director|direktor\w*|sportchef\w*|leiter\w* frauen",
        "General Manager:in / Leitung",
        "General Manager / Leitung",
    ),
    (
        "team_manager",
        r"team ?manager\w*|team ?lead\w*|teamleit\w*|teambetreu\w*",
        "Teammanager:in",
        "Teammanager:innen",
    ),
    ("doctor", r"doctor|doktor\w*|ärzt\w*|arzt|physician|lékař|\blæge", "Teamärzt:in", "Teamärzt:innen"),
    ("physio", r"physio\w*|therap\w*|fyzio\w*|fysio\w*", "Physiotherapeut:in", "Physiotherapeut:innen"),
    ("equipment", r"equip\w*|material\w*|kit", "Materialwart:in", "Materialwart:innen"),
    ("nutrition", r"nutrition\w*|ernährung\w*", "Ernährungsberater:in", "Ernährungsberater:innen"),
    ("athletic_trainer", r"^trainer$|athletic trainer", "Betreuer:in (Trainer)", "Betreuer:innen (Trainer)"),
    ("coach", r"coach|trainer\w*", "Trainer:in", "Trainer:innen"),
]
_ROLE_RX = [(k, re.compile(p, re.I), de, pl) for k, p, de, pl in ROLES]
ORDER = {k: i for i, (k, *_r) in enumerate(ROLES)} | {"assistant_gm": 7.5}  # shown after the GM
LABEL = {k: de for k, _p, de, _pl in ROLES} | {"other": "Staff"}
PLURAL = {k: pl for k, _p, _de, pl in ROLES} | {"other": "Staff"}
COACH_KEYS = {
    "head_coach",
    "assistant_coach",
    "goalie_coach",
    "video_coach",
    "performance_coach",
    "skills_coach",
    "coach",
}
SINGLE = {"head_coach", "gm"}
MEDIA_KEYS = {"media"}

# --- questions -----------------------------------------------------------------------------------------------

# 2026-11-01: the team's media manager / press officer ("Wer ist die Medienverantwortliche?", "Pressekontakt
# Kanada"); only role words, so "Was schreiben die Medien über die Trainerin?" stays a coach question
MEDIA_Q = (
    r"medienverantwortlich\w*|medienchef\w*|mediensprecher\w*|medienstelle|medienkontakt\w*|medienbeauftragt\w*|"
    r"media ?manager\w*|media officer|media contact\w*|"
    r"presse(sprecher\w*|verantwortlich\w*|chef\w*|stelle|kontakt\w*|attach\w*|offizier\w*|abteilung)|"
    r"press (officer|attach\w*|contact\w*)|kommunikationsverantwortlich\w*|kommunikationschef\w*"
)

STAFF_Q = re.compile(
    r"\b(trainer\w*|coach\w*|headcoach\w*|cheftrainer\w*|nationaltrainer\w*|staff|trainerstab|betreuer\w*|"
    r"assistenz\w*|assistant\w*|co-?trainer\w*|goalie-?trainer\w*|torh[üu]tertrainer\w*|videocoach\w*|"
    r"physio\w*|teamarzt|teamärzt\w*|ärzt\w*|arzt|doktor\w*|materialwart\w*|equipment|team ?manager\w*|"
    r"general manager|\bgm\b|sportchef\w*|wer trainiert|" + MEDIA_Q + r")\b",
    re.I,
)
WIDE_Q = re.compile(r"\b(staff|trainerstab|betreuer\w*|coaching staff|team hinter|alle|ganze[nr]?)\b", re.I)
# specific role words in a question → only these roles
ASK_ROLE: list[tuple[re.Pattern, set[str]]] = [
    (
        re.compile(
            r"head ?coach\w*|headcoach\w*|cheftrainer\w*|nationaltrainer\w*",
            re.I,
        ),
        {"head_coach"},
    ),
    (re.compile(r"assist\w*|co-?trainer\w*", re.I), {"assistant_coach"}),
    (re.compile(r"goalie\w*|torh[üu]ter\w*|goalkeep\w*|goaltend\w*", re.I), {"goalie_coach"}),
    (re.compile(r"video\w*", re.I), {"video_coach"}),
    (re.compile(r"physio\w*", re.I), {"physio"}),
    (re.compile(r"\b(ärzt\w*|arzt|doktor\w*|doctor)\b", re.I), {"doctor"}),
    (re.compile(r"material\w*|equipment", re.I), {"equipment"}),
    (re.compile(r"team ?manager\w*", re.I), {"team_manager"}),
    (re.compile(r"general manager|\bgm\b|sportchef\w*|head of", re.I), {"gm"}),
    (re.compile(r"athletik\w*|fitness|performance", re.I), {"performance_coach"}),
    (re.compile(MEDIA_Q, re.I), MEDIA_KEYS),
]
HISTORY_Q = re.compile(
    r"\b(seit wann|wechsel\w*|vorgänger\w*|nachfolge\w*|früher\w*|bisher\w*|vorher|abgelöst|ersetzt|neue[rn]?|"
    r"geschichte|history|replaced|predecessor|since when|bis wann)\b",
    re.I,
)
# not about who the staff is: what a coach said, a player's own training, the training camp
NOT_STAFF = re.compile(
    r"\b(trainingslager|training camp|trainingsspiel\w*|sagt\w*|gesagt|meint\w*|äusser\w*|aeusser\w*|"
    r"interview\w*|zitat\w*|kritik\w*|kritisier\w*|lob\w*|instagram|post(s|et)?|news|neuigkeit\w*)\b",
    re.I,
)


def is_staff_question(question: str) -> bool:
    if re.search(MEDIA_Q, question, re.I):  # "Pressekontakt für ein Interview" is about the staff
        return True
    return bool(STAFF_Q.search(question)) and not NOT_STAFF.search(question)


def asked_roles(question: str) -> set[str] | None:
    """The roles a question asks for (None = all staff). 'Wer ist der Trainer?' → coaches only."""
    keys: set[str] = set()
    for rx, ks in ASK_ROLE:
        if rx.search(question):
            keys |= ks
    if keys:
        return keys
    if WIDE_Q.search(question):
        return None
    return COACH_KEYS


# --- roles and dates -------------------------------------------------------------------------------------------


def role_of(label: str | None) -> str:
    """Role key of a source label ('Head Coach' → head_coach, 'Doktorin' → doctor, 'Equipement Manager' → equipment)."""
    text = (label or "").strip()
    for key, rx, _de, _pl in _ROLE_RX:
        if rx.search(text):
            return key
    return "other"


def is_coach(label: str | None) -> bool:
    return role_of(label) in COACH_KEYS


def is_staff_entry(entry_role: str | None) -> bool:
    """A roster line of a staff member (players carry G/D/F or nothing)."""
    return entry_role not in PLAYER_ROLES


def _start(value: str | None) -> date | None:
    """'2019' → 2019-01-01, '2026-08' → 2026-08-01, '2026-04-25' → itself."""
    if not value:
        return None
    parts = [int(x) for x in str(value).split("-")]
    return date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)


def _end(value: str | None) -> date | None:
    """'2019' → 2019-12-31, '2026-08' → 2026-08-31, '2026-04-25' → itself."""
    if not value:
        return None
    parts = [int(x) for x in str(value).split("-")]
    if len(parts) == 1:
        return date(parts[0], 12, 31)
    if len(parts) == 2:
        nxt = date(parts[0] + (parts[1] == 12), parts[1] % 12 + 1, 1)
        return nxt - timedelta(days=1)
    return date(*parts)


MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def date_text(value: str | None) -> str:
    """'2019' → '2019', '2026-08' → 'August 2026', '2026-04-25' → '25.04.2026'."""
    if not value:
        return ""
    parts = str(value).split("-")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{MONTHS[int(parts[1]) - 1]} {parts[0]}"
    return f"{int(parts[2]):02d}.{int(parts[1]):02d}.{parts[0]}"


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)


def as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _start(str(value))


# --- storing ---------------------------------------------------------------------------------------------------


def record(
    s: Session,
    team: m.Team,
    athlete: m.Athlete,
    label: str | None,
    *,
    source: str | None = None,
    source_url: str | None = None,
    seen: date | None = None,
    since: str | None = None,
    until: str | None = None,
    quote: str | None = None,
    note: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    origin: str = "roster",
    key: str | None = None,
    fill_only: bool = False,
) -> m.TeamStaff | None:
    """Store one piece of evidence that `athlete` holds role `label` at `team` (idempotent).

    Seeds (origin 'seed') carry since/until/quote and own their row; rosters only add evidence (last_seen,
    newest source). A roster line inside a closed appointment (e.g. Olympics 2026 roster, Muller until 04.2026)
    only updates that row's last_seen; after it, a new open row starts."""
    key = key or role_of(label)
    rows = list(
        s.scalars(
            select(m.TeamStaff).where(
                m.TeamStaff.team_id == team.id,
                m.TeamStaff.athlete_id == athlete.id,
                m.TeamStaff.role_key == key,
            )
        )
    )
    row = None
    if origin == "seed":
        row = next((r for r in rows if r.origin == "seed" and r.since == since), None)
        row = row or next((r for r in rows if r.origin != "seed" and r.until is None), None)
    else:
        row = next((r for r in rows if r.until is None), None)
        if row is None and seen is not None:
            inside = next((r for r in rows if r.until and _end(r.until) and seen <= _end(r.until)), None)
            if inside is not None:  # evidence from within a closed appointment
                if inside.last_seen is None or seen > inside.last_seen:
                    inside.last_seen = seen
                return inside
    if row is not None and fill_only and row.last_seen is not None:
        return row  # sync of stored rosters: the date a list was loaded is no new evidence
    if row is None:
        row = m.TeamStaff(team_id=team.id, athlete_id=athlete.id, role_key=key, origin=origin)
        s.add(row)
    newer = seen is not None and (row.source_date is None or seen >= row.source_date)
    if origin == "seed":
        row.origin = "seed"
        row.since, row.until = since, until
        row.quote, row.note = quote, note
        row.email, row.phone = email, phone
        row.role_label = label or row.role_label
        row.source, row.source_url, row.source_date = source, source_url, seen
    elif row.origin != "seed" and (newer or row.source is None):
        row.role_label = label or row.role_label
        row.source, row.source_url, row.source_date = source, source_url, seen
    if seen is not None and (row.last_seen is None or seen > row.last_seen):
        row.last_seen = seen
    s.flush()
    return row


@functools.lru_cache(maxsize=32)
def _read(path: str, mtime: float) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _contact_note(p: dict, sources: dict) -> str | None:
    """The line's note, plus where email/phone come from when that is another page (`contact_src`)."""
    csrc = sources.get(p.get("contact_src")) if p.get("contact_src") else None
    bits = [p.get("note"), f"Kontakt laut {csrc['name']}" if csrc else None]
    return "; ".join(b for b in bits if b) or None


def load_seed_data(s: Session, data: dict, name: str = "staff") -> int:
    """One staff seed: `team`, `sources` {key: {name, url, date}}, `staff` [{name, role, since, until, src, quote,
    email, phone}] (email/phone: work contact exactly as the federation/IIHF publishes it, never guessed)."""
    from klugscheisser.db import repo

    team = repo.get_team(s, data["team"])
    if team is None:
        log.warning("staff seed %s: unknown team %s", name, data.get("team"))
        return 0
    from klugscheisser.sports import current_sport_key

    sport = repo.get_sport(s, current_sport_key())  # 2026-11-24: the project's sport ("floorball_men")
    sources = data.get("sources") or {}
    n = 0
    for p in data.get("staff") or []:
        src = sources.get(p.get("src"), {})
        role = "coach" if is_coach(p.get("role")) else "staff"
        athlete = repo.upsert_athlete(s, sport, p["name"], nationality=p.get("nationality"), role=role)
        if athlete.role == "athlete":  # a former player now on the staff: one person row
            athlete.role = role
        record(
            s,
            team,
            athlete,
            p.get("role"),
            source=src.get("name"),
            source_url=src.get("url"),
            seen=as_date(src.get("date")),
            since=_iso(p.get("since")),
            until=_iso(p.get("until")),
            quote=p.get("quote"),
            note=_contact_note(p, sources),
            email=(p.get("email") or "").strip() or None,
            phone=str(p.get("phone") or "").strip() or None,
            origin="seed",
        )
        n += 1
    return n


def load_seeds(s: Session, staff_dir: Path | None = None) -> int:
    n = 0
    for path in sorted((staff_dir or STAFF_DIR).glob("*.yaml")):
        n += load_seed_data(s, _read(str(path.resolve()), path.stat().st_mtime), path.name)
    return n


def sync_from_entries(s: Session) -> int:
    """Staff lines already stored on event rosters → team appointments (evidence date: the event's start, or the
    day the list was loaded when that is earlier — a provisional WM list is not evidence from November)."""
    n = 0
    rows = s.execute(
        select(m.EventEntry, m.Event, m.Team)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .join(m.Team, m.Team.id == m.EventEntry.team_id)
    ).all()
    for e, ev, team in rows:
        if not is_staff_entry(e.role):
            continue
        dates = [d for d in (as_date(ev.starts_at), as_date(e.fetched_at)) if d]
        record(
            s,
            team,
            e.athlete,
            e.role,
            source=e.source,
            source_url=e.source_url,
            seen=min(dates) if dates else None,
            fill_only=True,
        )
        n += 1
    return n


# --- reading -----------------------------------------------------------------------------------------------------


@dataclass
class Appointment:
    row: m.TeamStaff
    current: bool
    reason: str = ""  # why it is history: "bis …", "abgelöst durch …", "zuletzt belegt …"
    successor: str | None = None

    @property
    def key(self) -> str:
        return self.row.role_key

    @property
    def name(self) -> str:
        return self.row.athlete.full_name

    def evidence(self) -> date:
        return max(d for d in (_start(self.row.since), self.row.last_seen, date.min) if d)


def appointments(s: Session, team: m.Team, today: date | None = None) -> list[Appointment]:
    today = today or datetime.now(UTC).date()
    rows = list(s.scalars(select(m.TeamStaff).where(m.TeamStaff.team_id == team.id)))
    out = [Appointment(r, True) for r in rows]
    for a in out:
        end = _end(a.row.until)
        if end is not None and end < today:
            a.current, a.reason = False, f"bis {date_text(a.row.until)}"
    open_ = [a for a in out if a.current]
    for key in SINGLE:
        holders = sorted((a for a in open_ if a.key == key), key=lambda a: a.evidence(), reverse=True)
        for old in holders[1:]:
            old.current = False
            old.reason = f"abgelöst (zuletzt belegt {old.evidence():%d.%m.%Y})"
            old.successor = holders[0].name
    # a successor of a closed one-holder appointment (Muller until 04.2026 → Miller)
    for a in out:
        if not a.current and a.key in SINGLE and a.successor is None:
            nxt = [b for b in out if b.key == a.key and b.current and b.row.athlete_id != a.row.athlete_id]
            if nxt:
                a.successor = nxt[0].name
    still = [a for a in out if a.current]
    if still:
        newest = max(a.evidence() for a in still)
        for a in still:
            if a.key not in SINGLE and not a.row.since and a.evidence() < newest - timedelta(days=STALE_DAYS):
                a.current, a.reason = False, f"zuletzt belegt {a.evidence():%d.%m.%Y}"
    out.sort(key=lambda a: (not a.current, ORDER.get(a.key, 99), -a.evidence().toordinal(), a.name))
    return out


def current(s: Session, team: m.Team, today: date | None = None) -> list[Appointment]:
    return [a for a in appointments(s, team, today) if a.current]


def event_staff(s: Session, ev: m.Event, team: m.Team) -> list[m.EventEntry]:
    entries = s.scalars(
        select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id)
    )
    staff = [e for e in entries if is_staff_entry(e.role)]
    staff.sort(key=lambda e: (ORDER.get(role_of(e.role), 99), e.athlete.full_name))
    return staff


def compact(s: Session, team: m.Team, today: date | None = None, keys: set[str] | None = None) -> str | None:
    """'Headcoach: Shannon Miller; Assistenztrainer:innen: Saara Kivenmäki, Justin Krueger; …' (current staff)."""
    groups: dict[str, list[str]] = {}
    for a in current(s, team, today):
        if keys is None or a.key in keys:
            groups.setdefault(a.key, []).append(a.name)
    if not groups:
        return None
    return "; ".join(
        f"{(PLURAL if len(names) > 1 else LABEL)[k]}: {', '.join(names)}" for k, names in groups.items()
    )


# --- answer ------------------------------------------------------------------------------------------------------


@dataclass
class Line:
    text: str
    quelle: str
    url: str
    zitat: str | None = None

    def out(self) -> dict[str, Any]:
        d = {"text": self.text, "quelle": self.quelle, "url": self.url, "art": _kind(self.url)}
        if self.zitat:
            d["zitat"] = self.zitat
        return d


@dataclass
class Section:
    titel: str
    zeilen: list[Line] = field(default_factory=list)


def _site(url: str | None) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.removeprefix("www.") if url else "Kaderliste"


def _label(row: m.TeamStaff) -> str:
    """German role label; the source's own title in brackets where ours is a broad group ('GM (Head of …)')."""
    src = (row.role_label or "").strip()
    if row.role_key == "other":
        return src or "Staff"
    label = LABEL.get(row.role_key, "Staff")
    if (
        row.role_key in ("gm", "coach", "athletic_trainer", "performance_coach", "media")
        and src
        and src.lower() not in label.lower()
    ):
        return f"{label} ({src})"
    return label


OFFICIAL_SITES = re.compile(
    r"sihf\.ch|deb-online\.de|eishockey\.at|hockeycanada\.ca|ceskyhokej\.cz|leijonat\.fi|jihf\.or\.jp|"
    r"usahockey\.com|swehockey\.se|via\.tt\.se|iihf\.com|ishockey\.dk",
    re.I,
)


def _kind(url: str | None) -> str:
    if not url:
        return "other"
    if "wikipedia.org" in url:
        return "web"
    return "official" if OFFICIAL_SITES.search(url) else "media"


def _source_name(row: m.TeamStaff) -> str:
    when = f", {row.source_date:%d.%m.%Y}" if row.source_date and row.origin != "seed" else ""
    return (row.source or _site(row.source_url)) + when


def contact_text(row: m.TeamStaff | None = None, email: str | None = None, phone: str | None = None) -> str:
    """' – E-Mail x@y.ch, Tel. +41 …' (work contact as published), '' without one."""
    if row is not None:
        email, phone = row.email, row.phone
    bits = [b for b in (f"E-Mail {email}" if email else "", f"Tel. {phone}" if phone else "") if b]
    return f" – {', '.join(bits)}" if bits else ""


def _period(a: Appointment) -> str:
    r = a.row
    bits = []
    if r.since:
        bits.append(
            f"seit {date_text(r.since)}" if a.current else f"{date_text(r.since)}–{date_text(r.until) or '?'}"
        )
    elif not a.current and r.until:
        bits.append(f"bis {date_text(r.until)}")
    if r.note:
        bits.append(r.note)
    if a.current and not r.since and r.last_seen:
        bits.append(f"belegt {r.last_seen:%d.%m.%Y}")
    return f" ({'; '.join(bits)})" if bits else ""


def _team_for(s: Session, question: str, team_id: int | None, person_ids: list[int] | None) -> m.Team | None:
    from klugscheisser.tournament import nations_in

    if team_id:
        return s.get(m.Team, team_id)
    if person_ids:
        row = s.scalar(
            select(m.TeamStaff)
            .where(m.TeamStaff.athlete_id.in_(person_ids))
            .order_by(m.TeamStaff.last_seen.desc().nulls_last())
        )
        if row is not None:
            return row.team
    nations = nations_in(question)
    if len(nations) > 1:
        return None
    code = nations[0] if nations else None
    if code is None:
        try:
            from klugscheisser.answer.lookups import sides

            found = sides(s, question)
        except Exception:  # team matching problems never block the staff answer
            found = []
        if len(found) == 1:
            return found[0].team
        from klugscheisser import projects

        code = projects.current().home_nation  # the project's team when no other team is named
    from klugscheisser.sports import team_suffix

    q = select(m.Team).where(m.Team.country == code, m.Team.kind == "national")
    teams = list(s.scalars(q))
    women = [t for t in teams if t.key.endswith(f"-{team_suffix()}")]  # "-women" / "-men" (floorball)
    return (women or teams or [None])[0]


def _named(s: Session, question: str) -> list[int]:
    try:
        from klugscheisser.domain.matcher import AthleteMatcher

        return [link.athlete_id for link in AthleteMatcher(s).from_text(question)]
    except Exception:  # matcher problems never block the staff answer
        return []


def _event_for(s: Session, question: str, event_id: int | None, team: m.Team) -> m.Event | None:
    from klugscheisser.tournament import TOURNAMENT

    if event_id:
        return s.get(m.Event, event_id)
    if not TOURNAMENT.search(question) and not re.search(
        r"\b(euro hockey tour|eht|weht|olympi\w*)\b", question, re.I
    ):
        return None
    now = datetime.now(UTC)
    evs = list(
        s.scalars(
            select(m.Event)
            .join(m.EventTeam)
            .where(m.EventTeam.team_id == team.id, m.Event.kind == "tournament")
        )
    )
    if re.search(r"olympi\w*", question, re.I):
        evs = [e for e in evs if e.key.startswith("oly")]
    elif re.search(r"euro hockey tour|eht|weht", question, re.I):
        evs = [e for e in evs if e.key.startswith("eht")]
    else:
        evs = [e for e in evs if e.key.startswith("wch")] or evs
    if not evs:
        return None
    upcoming = sorted(
        (e for e in evs if e.ends_at is None or e.ends_at >= now), key=lambda e: e.starts_at or now
    )
    past = sorted(
        (e for e in evs if e.ends_at is not None and e.ends_at < now), key=lambda e: e.starts_at or now
    )
    return (upcoming or past[::-1])[0]


def answer(
    s: Session,
    question: str,
    team_id: int | None = None,
    event_id: int | None = None,
    person_ids: list[int] | None = None,
    today: date | None = None,
) -> dict[str, Any] | None:
    """Team-level staff answer: current staff, the staff on an event roster, changes/history. None when the
    question is no staff question or nothing is stored for the team (the normal answer engine then answers)."""
    if not is_staff_question(question):
        return None
    if person_ids is None:
        person_ids = _named(s, question)
    if person_ids:  # a named player ("Wer ist Trainerin von Alina Müller?") is no staff question for us
        staff_ids = set(
            s.scalars(select(m.TeamStaff.athlete_id).where(m.TeamStaff.athlete_id.in_(person_ids)))
        )
        if not staff_ids:
            return None
        person_ids = sorted(staff_ids)
    team = _team_for(s, question, team_id, person_ids)
    if team is None:
        return None
    apps = appointments(s, team, today)
    ev = _event_for(s, question, event_id, team)
    ev_staff = event_staff(s, ev, team) if ev is not None else []
    if not apps and not ev_staff:
        return None
    keys = asked_roles(question)
    focus = set(person_ids or [])
    if focus:  # "Seit wann ist Shannon Miller Trainerin?": her roles, plus predecessors in the same roles
        own = {a.key for a in apps if a.row.athlete_id in focus}
        if keys == COACH_KEYS or keys == {"head_coach"}:
            keys = own or keys
        elif keys is not None:  # "Wer sind die Assistenten unter Miller?": the asked roles plus hers
            keys |= own
    asked = keys
    if keys is not None:
        keys = keys | {"head_coach"}  # the head coach always as context when only some roles are asked

    def wanted(key: str) -> bool:
        return keys is None or key in keys

    sections: list[Section] = []
    cur = Section(
        f"Staff {team.name} (aktuell)"
        if keys is None or keys - COACH_KEYS
        else f"Trainer {team.name} (aktuell)"
    )
    names: list[str] = []
    for a in apps:
        if a.current and wanted(a.key):
            r = a.row
            cur.zeilen.append(
                Line(
                    f"{_label(r)}: {a.name}{_period(a)}{contact_text(r)}",
                    _source_name(r),
                    r.source_url or "",
                    r.quote,
                )
            )
            names.append(a.name)
    if cur.zeilen:
        sections.append(cur)
    if ev is not None:
        sec = Section(f"Staff im Aufgebot: {ev.name}")
        listed = [e for e in ev_staff if wanted(role_of(e.role))]
        if listed:
            status = "offiziell" if all(e.list_type == "confirmed" for e in listed) else "provisorisch"
            names = "; ".join(f"{LABEL.get(role_of(e.role), e.role)}: {e.athlete.full_name}" for e in listed)
            sec.zeilen.append(
                Line(
                    f"{team.name}, {status}: {names}",
                    listed[0].source or _site(listed[0].source_url),
                    listed[0].source_url or "",
                )
            )
        else:
            sec.zeilen.append(
                Line(
                    f"Für {ev.name} ist noch kein Staff-Aufgebot von {team.name} gespeichert"
                    + (
                        " (offizielles Aufgebot noch nicht publiziert); es gilt der aktuelle Staff oben."
                        if cur.zeilen
                        else "."
                    ),
                    "Karl",
                    "",
                )
            )
        sections.append(sec)
    if keys is not None:
        other = sorted(
            {a.key for a in apps if a.current and a.key not in keys}, key=lambda k: ORDER.get(k, 99)
        )
        if other:
            sections.append(
                Section(
                    "Nicht gefragt",
                    [
                        Line(
                            f"Weitere gespeicherte Rollen (nicht gefragt, nicht fehlend): "
                            f"{', '.join(PLURAL[k] for k in other)} – alle mit frage('Staff {team.name}').",
                            "Karl",
                            "",
                        )
                    ],
                )
            )
    hist = Section("Wechsel und frühere Besetzungen")
    show_history = bool(HISTORY_Q.search(question)) or focus or (asked is not None and asked & SINGLE)
    for a in apps:
        context_only = asked is not None and a.key not in asked  # head coach added as context only
        if a.current or not wanted(a.key) or (not show_history and (a.key not in SINGLE or context_only)):
            continue
        if not show_history and a.reason.startswith("zuletzt"):
            continue
        r = a.row
        succ = f", Nachfolge: {a.successor}" if a.successor else ""
        hist.zeilen.append(
            Line(
                f"{_label(r)}: {a.name}{_period(a)} – {a.reason}{succ}",
                _source_name(r),
                r.source_url or "",
                r.quote,
            )
        )
    if hist.zeilen:
        sections.append(hist)
    if not any(sec.zeilen for sec in sections):
        return None
    ids = sorted({a.row.athlete_id for a in apps} | {e.athlete_id for e in ev_staff})
    return {
        "team": team.name,
        "frage": question,
        "anlass": ev.name if ev is not None else None,
        "aktuell": names,
        "nur_trainer": keys is not None and not (keys - COACH_KEYS),
        "abschnitte": [{"titel": sec.titel, "zeilen": [ln.out() for ln in sec.zeilen]} for sec in sections],
        "personen_ids": ids,
        "hinweis": "Gespeicherter, belegter Staff (Karl). Jede Zeile mit ihrem Link übernehmen; nichts ergänzen. "
        "Nicht aufgeführte Rollen wurden nicht gefragt – das heisst nicht, dass es sie nicht gibt.",
    }


def render(result: dict[str, Any], datenstand: str | None = None) -> str:
    from klugscheisser.mcp_server.present import zeile_prefix

    out = [f"**Datenstand:** {datenstand}"] if datenstand else []
    out.append(f"**Trainer und Staff: {result['team']}**")
    for sec in result["abschnitte"]:
        out.append(f"\n**{sec['titel']}**")
        for z in sec["zeilen"]:
            link = f" [{z['quelle']}]({z['url']})" if z.get("url") else ""
            quote = f" «{z['zitat']}»" if z.get("zitat") else ""
            out.append(f"- {zeile_prefix(z.get('art'), None)} {z['text']}{quote}{link}")
    out.append(
        "\n_Zeilen unverändert übernehmen (Aussage + Link). Rollen, die hier fehlen, wurden nicht gefragt – nicht "
        "daraus schliessen, dass es sie nicht gibt._"
    )
    return "\n".join(out)


def staff_lines(s: Session, team: m.Team, today: date | None = None) -> list[dict[str, Any]]:
    """Current staff as {text, quelle, url} lines (get_team_roster / get_event)."""
    out = []
    for a in current(s, team, today):
        r = a.row
        out.append(
            {
                "text": f"{_label(r)}: {a.name}{_period(a)}{contact_text(r)}",
                "quelle": _source_name(r),
                "url": r.source_url or "",
                "id": r.athlete_id,
                **({"email": r.email} if r.email else {}),
                **({"telefon": r.phone} if r.phone else {}),
            }
        )
    return out


def contacts(s: Session, team: m.Team, today: date | None = None) -> list[dict[str, Any]]:
    """Current staff with a published work contact (media manager / press officer first): web page + get_event."""
    out = []
    for a in current(s, team, today):
        r = a.row
        if r.email or r.phone:
            out.append(
                {
                    "name": a.name,
                    "id": r.athlete_id,
                    "rolle": _label(r),
                    "email": r.email,
                    "telefon": r.phone,
                    "quelle": _source_name(r),
                    "url": r.source_url or "",
                }
            )
    out.sort(key=lambda c: not c["rolle"].startswith(LABEL["media"]))
    return out


def contacts_compact(s: Session, team: m.Team, today: date | None = None) -> str | None:
    """'Flavia Tschumi (Medienverantwortliche:r (Media Manager)) – E-Mail …, Tel. …' (get_event per team)."""
    cs = contacts(s, team, today)
    return (
        "; ".join(f"{c['name']} ({c['rolle']}){contact_text(None, c['email'], c['telefon'])}" for c in cs)
        or None
    )
