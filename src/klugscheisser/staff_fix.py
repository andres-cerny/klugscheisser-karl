"""One-off (2026-11-01): apply the hand-checked WCh 2026 coach/staff research to the database.

Input: the research JSON (`staff_corrections`, `missing_persons`, `persons`, `existing_stint_issues`), e.g.
`~/klugscheisser-wch-staff-history.json`. CLI: `klugscheisser staff-fix --from <json>` (dry run; `--yes` writes).

- staff_corrections: a concrete, sourced `until=`/`since=` in the suggestion is applied to the team_staff row
  (outdated appointments get an end date; nothing is deleted). Suggestions without a concrete date ("keep",
  "optional", "re-check", "add … if wanted") are listed, not applied.
- missing_persons: person row (identity check against existing persons first: an exact name match is reused and
  reported) + team_staff row with role, source, URL, date and quote. Labels marked "unconfirmed" are listed, not
  added. Their career stints / season stats from `persons` (entries without athlete_id) are inserted like the
  earlier import: search-snippet stints and stints of an unconfirmed identity as 'unconfirmed', Wikipedia
  stats as source 'wiki'.
- existing_stint_issues: the free-text findings are resolved to stint rows by STINT_ISSUES below (checked by
  hand against the data); those rows are set to 'unconfirmed' (never deleted). career_stints has no note
  column: the reason is printed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from klugscheisser import staff
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts.source_site import domain

# publisher names for team_staff.source (the answer adds the date: "Hockey Canada, 24.09.2026")
PUBLISHER = {
    "hockeycanada.ca": "Hockey Canada",
    "ceskyhokej.cz": "ČSLH / ceskyhokej.cz",
    "jihf.or.jp": "JIHF",
    "swehockey.se": "Svenska Ishockeyförbundet",
    "eishockey.at": "ÖEHV",
    "srf.ch": "SRF",
    "ishockey.dk": "DIU / ishockey.dk",
    "deb-online.de": "DEB",
    "teamusa.usahockey.com": "USA Hockey",
    "sihf.ch": "SIHF",
    "yle.fi": "Yle",
    "elmotv.com": "Elmo TV",
    "swisshockeynews.ch": "Swiss Hockey News",
}

# existing_stint_issues → the stints they mean: (athlete_id, team name regex, season_from, season_to, reason).
# Seasons must match exactly (None = empty). Resolved by hand from the findings and the stored rows (2026-11-01).
STINT_ISSUES: list[tuple[int, str, int | None, int | None, str]] = [
    (1405, r"^Calgary$", 2007, 2019, "Calgary 2007–2019 (nzz): sie trainierte Calgary Inferno nur 2018/19"),
    (1405, r"^Schweizer Frauen-Eishockey-Nationalteam$", 2024, 2029, "Start Aug. 2026; 2029 = Vertragsende"),
    (1405, r"^Schweizer Frauennationalteam$", 2024, None, "Nati-Trainerin erst seit Aug. 2026"),
    (1405, r"^Schweiz$", 2024, None, "Nati-Trainerin erst seit Aug. 2026"),
    (1405, r"Duluth", 1998, 2015, "Duluth 1998–2015: Endjahr statt Saisonstart; letzte Saison 2014/15"),
    (1405, r"^Team Canada$", 1998, 1998, "Olympia 1998 = Saison 1997/98"),
    (153, r"^Schweizer Frauen-Nationalmannschaft$", None, 2024, "überlappt; Headcoach-Station 2019–2025"),
    (153, r"^Schweizer Eishockey-Nati der Frauen$", 2019, 2025, "Doppel der Station 2019–2025"),
    (153, r"^Schweizer Frauen-Nationalteam$", 2019, None, "Doppel der Station 2019–2025"),
    (153, r"^ZSC Lions$", 2008, 2010, "mischt Assistent (2008/09) und Headcoach (2010/11)"),
    (307, r"Duluth", None, 2010, "Endjahr statt Saisonstart; richtig 2006–2009"),
    (307, r"^Ilves Tampere$", 2003, None, "offenes Ende von keiner Quelle belegt"),
    (1576, r"^SC Bern$", 2010, None, "zwei Bern-Stationen 2010/11 und 2013/14–2019/20"),
    (1576, r"^Cornell University$", 2006, 2010, "Ende 2009 (Saisonstart)"),
    (1576, r"^Lausanne HC$", 2020, 2022, "Ende 2021 (Saisonstart)"),
    (65, r"^SC Reinach$", 2001, 2011, "Endjahr-Schreibweise; richtig 2000–2010"),
    (65, r"^SC Küsnacht$", None, None, "Doppel von Küssnachter SC 2007/08 (Wikidata, ohne Jahre)"),
]

_DATE_RX = r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)"
_CHANGE = re.compile(rf"\b(until|since)[= ]{_DATE_RX}\b")
_ROW_ID = re.compile(r"\brow (\d+)\b|team_staff id (\d+)")
_VAGUE_LABEL = re.compile(r"unconfirmed", re.I)
STAT_COLS = ("games", "goals", "assists", "points", "penalty_minutes")


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    identity: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def lines(self) -> list[str]:
        out = ["Angewendet:", *(f"  + {x}" for x in self.applied)]
        out += ["Nicht angewendet (vage, nur Info oder nicht gefunden):", *(f"  - {x}" for x in self.skipped)]
        out += ["Identität:", *(f"  ? {x}" for x in self.identity)]
        out.append("Zahlen: " + ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items())))
        return out


def _publisher(url: str | None) -> str:
    d = domain(url)
    for host, name in PUBLISHER.items():
        if d == host or d.endswith("." + host):
            return name
    return d


def _changes(suggested: str) -> dict[str, str]:
    """'set until=2026-04 (end of 2025/26) or 2026-02' → {'until': '2026-04'} (the first value per field)."""
    out: dict[str, str] = {}
    for key, value in _CHANGE.findall(suggested or ""):
        out.setdefault(key, value)
    return out


def _team(s: Session, key: str) -> m.Team | None:
    return repo.get_team(s, key)


def _row_for(s: Session, team: m.Team, aid: int, c: dict) -> m.TeamStaff | None:
    text = f"{c.get('suggested', '')} {c.get('current_row', '')}"
    hit = _ROW_ID.search(text)
    if hit:
        row = s.get(m.TeamStaff, int(hit.group(1) or hit.group(2)))
        if row is not None and row.team_id == team.id and row.athlete_id == aid:
            return row
    rows = list(
        s.scalars(select(m.TeamStaff).where(m.TeamStaff.team_id == team.id, m.TeamStaff.athlete_id == aid))
    )
    first = (c.get("current_row") or "").split(",")[0].split()[0] if c.get("current_row") else ""
    keyed = [r for r in rows if r.role_key == first] or rows
    open_ = [r for r in keyed if r.until is None]
    return open_[0] if len(open_) == 1 else (keyed[0] if len(keyed) == 1 else None)


def apply_corrections(s: Session, data: dict, rep: Report) -> None:
    for c in data.get("staff_corrections") or []:
        who = f"{c.get('team')} {c.get('person')}"
        change = _changes(c.get("suggested", ""))
        if c.get("athlete_id") is None or not change:
            note = " (Personen: siehe missing_persons)" if c.get("athlete_id") is None else ""
            rep.skipped.append(f"Korrektur {who}: «{c.get('suggested')}»{note}")
            continue
        if not c.get("source_url") or not c.get("date"):
            rep.skipped.append(f"Korrektur {who}: ohne Quelle/Datum")
            continue
        team = _team(s, c["team"])
        row = _row_for(s, team, c["athlete_id"], c) if team else None
        if row is None:
            rep.skipped.append(f"Korrektur {who}: keine eindeutige team_staff-Zeile gefunden")
            continue
        done = []
        for key, value in change.items():
            old = getattr(row, key)
            if old == value:
                continue
            if old is not None:
                rep.skipped.append(f"Korrektur {who}: {key} ist schon {old} (vorgeschlagen {value})")
                continue
            setattr(row, key, value)
            done.append(f"{key}={value}")
        if not done:
            continue
        # the dated source of the change becomes the row's source (the roster link only showed the role)
        if row.origin != "seed":
            row.source, row.source_url = _publisher(c["source_url"]), c["source_url"]
            row.source_date = staff.as_date(c["date"])
        rep.applied.append(
            f"team_staff {row.id} {who} ({row.role_key}): {', '.join(done)} [{c['source_url']}]"
        )
        rep.add("team_staff geändert")
    s.flush()


def _clean_label(label: str) -> str:
    """'Assistant Coach (Aug–Sep 2026, history only)' → 'Assistant Coach'; translations in brackets stay."""
    out = re.sub(r"\s*\([^)]*(\d|history)[^)]*\)", "", label or "").strip()
    return out or label


def _words(name: str) -> set[str]:
    return set(name.lower().split())


def _person_entry(data: dict, team: str, name: str) -> dict | None:
    for p in data.get("persons") or []:
        if (
            p.get("athlete_id") is None
            and p.get("team") == team
            and (_words(name) <= _words(p["name"]) or _words(p["name"]) <= _words(name))
        ):
            return p
    return None


def _history_dates(data: dict, name: str) -> dict[str, str]:
    """since/until for a missing person from a correction without athlete_id that names them (Kenyon)."""
    last = name.split()[-1].lower()
    for c in data.get("staff_corrections") or []:
        if c.get("athlete_id") is None and last in (c.get("person") or "").lower():
            return _changes(c.get("suggested", ""))
    return {}


def _nationality(p: dict | None) -> str | None:
    nat = (p or {}).get("nationality") or ""
    return nat if re.fullmatch(r"[A-Z]{3}", nat) else None


def _team_tier(s: Session, team: m.Team) -> int:
    tiers = s.scalars(
        select(m.Athlete.tier)
        .join(m.TeamStaff, m.TeamStaff.athlete_id == m.Athlete.id)
        # press contacts (seeds/staff) say nothing about how important the team's coaches are
        .where(m.TeamStaff.team_id == team.id, m.TeamStaff.role_key != "media")
    ).all()
    return min(tiers) if tiers else (1 if team.country == "SUI" else 2)


def _identity_unconfirmed(p: dict | None) -> bool:
    return bool(p) and bool(re.search(r"NOT confirmed", p.get("notes") or ""))


def add_missing(s: Session, data: dict, rep: Report) -> None:
    from klugscheisser.sports import current_sport_key

    sport = repo.get_sport(s, current_sport_key())
    for mp in data.get("missing_persons") or []:
        who = f"{mp['team']} {mp['name']}"
        if _VAGUE_LABEL.search(mp.get("role_label") or ""):
            rep.skipped.append(f"Person {who}: «{mp['role_label']}» (nicht belegt, nicht angelegt)")
            continue
        team = _team(s, mp["team"])
        if team is None or not mp.get("source_url"):
            rep.skipped.append(f"Person {who}: Team oder Quelle fehlt")
            continue
        p = _person_entry(data, mp["team"], mp["name"])
        name = p["name"] if p and len(p["name"]) > len(mp["name"]) else mp["name"]
        birth = None if _identity_unconfirmed(p) else staff.as_date((p or {}).get("birth_date"))
        found = [x for x in repo.find_athletes(s, name) if x.how == "exact"]
        if not found and name != mp["name"]:
            found = [x for x in repo.find_athletes(s, mp["name"]) if x.how == "exact"]
        if found:
            a0 = found[0].athlete
            rep.identity.append(
                f"{name}: vorhandene Person #{a0.id} übernommen ({a0.role}, {a0.nationality or '-'}, "
                f"geb. {a0.birth_date or '-'}) – gleicher Name, bitte prüfen"
            )
        label = _clean_label(mp.get("role_label") or "")
        role = "coach" if staff.is_coach(label) else "staff"
        if found:
            athlete = found[0].athlete
            if birth and athlete.birth_date is None:
                athlete.birth_date = birth
            if athlete.nationality is None and _nationality(p):
                athlete.nationality = _nationality(p)
        else:
            athlete = repo.upsert_athlete(
                s, sport, name, birth_date=birth, nationality=_nationality(p), role=role
            )
            rep.add("Personen neu")
        if athlete.role == "athlete":  # a former player now on the staff: one person row
            athlete.role = role
        if name != mp["name"]:
            repo.add_aliases(s, athlete, [mp["name"]])
        tier = _team_tier(s, team)
        if athlete.tier_manual is None and (athlete.tier or 3) > tier:
            athlete.tier = tier
        before = s.scalar(
            select(m.TeamStaff.id).where(
                m.TeamStaff.team_id == team.id,
                m.TeamStaff.athlete_id == athlete.id,
                m.TeamStaff.role_key == staff.role_of(label),
            )
        )
        row = staff.record(
            s,
            team,
            athlete,
            label,
            source=_publisher(mp["source_url"]),
            source_url=mp["source_url"],
            seen=staff.as_date(mp.get("date")),
        )
        dates = _history_dates(data, mp["name"])
        row.since = row.since or dates.get("since")
        row.until = row.until or dates.get("until")
        row.quote = row.quote or mp.get("quote")
        rep.applied.append(
            f"Person #{athlete.id} {name} → {team.key} {row.role_key} «{label}»"
            + (f" {row.since or ''}–{row.until}" if row.until else "")
        )
        rep.add("team_staff schon da" if before else "team_staff neu")
        if p:
            _insert_history(s, athlete.id, p, rep)
    s.flush()


def _insert_history(s: Session, aid: int, p: dict, rep: Report) -> None:
    """Career stints + season stats of one research entry (insert only, like the first import)."""
    shaky = _identity_unconfirmed(p)
    for c in p.get("career_stints") or []:
        src = c["source"][:30]
        exists = s.scalar(
            select(m.CareerStint.id).where(
                m.CareerStint.athlete_id == aid,
                m.CareerStint.team_name == c["team"][:200],
                m.CareerStint.source == src,
                m.CareerStint.role == c["role"][:20],
                m.CareerStint.season_from.is_(None)
                if c.get("season_from") is None
                else m.CareerStint.season_from == c["season_from"],
            )
        )
        if exists:
            continue
        unconfirmed = "snippet" in c["source"] or (shaky and c.get("kind") != "national")
        s.add(
            m.CareerStint(
                athlete_id=aid,
                team_name=c["team"][:200],
                kind=c["kind"],
                league=(c.get("league") or None) and c["league"][:100],
                season_from=c.get("season_from"),
                season_to=c.get("season_to"),
                role=c["role"][:20],
                source=src,
                source_url=c["source_url"],
                quote=c.get("quote"),
                confidence=0.6 if unconfirmed else 0.9,
                status="unconfirmed" if unconfirmed else "confirmed",
            )
        )
        rep.add("Stationen neu")
    for r in p.get("season_stats") or []:
        src = "wiki" if r["source"].startswith("wikipedia") else r["source"][:30]
        values: dict[str, Any] = {
            "athlete_id": aid,
            "season": r["season"],
            "league": r["league"][:50],
            "team_name": r["team"][:200],
            "team_kind": r.get("team_kind") or "club",
            "phase": "playoffs" if r.get("phase") == "playoffs" else "regular",
            "source": src,
            "source_url": r["source_url"],
            **{k: r.get(k) for k in STAT_COLS},
        }
        new = s.scalar(
            insert(m.SeasonStat)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_season_stats_row")
            .returning(m.SeasonStat.id)
        )
        rep.add("Statistikzeilen neu", int(new is not None))
    s.flush()


def mark_stint_issues(s: Session, data: dict, rep: Report) -> None:
    known = {i["athlete_id"] for i in data.get("existing_stint_issues") or []}
    for aid, team_rx, frm, to, reason in STINT_ISSUES:
        if aid not in known:
            continue
        rx = re.compile(team_rx)
        q = select(m.CareerStint).where(
            m.CareerStint.athlete_id == aid,
            m.CareerStint.season_from.is_(None) if frm is None else m.CareerStint.season_from == frm,
            m.CareerStint.season_to.is_(None) if to is None else m.CareerStint.season_to == to,
        )
        rows = [x for x in s.scalars(q.order_by(m.CareerStint.id)) if rx.search(x.team_name)]
        if not rows:
            rep.skipped.append(f"Station #{aid} {team_rx} {frm}–{to}: nicht gefunden")
            continue
        for st in rows:
            if st.status == "unconfirmed":
                rep.add("Stationen schon unbestätigt")
                continue
            st.status = "unconfirmed"
            rep.applied.append(
                f"Station {st.id} (#{aid} {st.team_name} {frm}–{to}, {st.source}) → unconfirmed: {reason}"
            )
            rep.add("Stationen auf unconfirmed")
    s.flush()


def run(s: Session, data: dict) -> Report:
    rep = Report()
    apply_corrections(s, data, rep)
    add_missing(s, data, rep)
    mark_stint_issues(s, data, rep)
    return rep
