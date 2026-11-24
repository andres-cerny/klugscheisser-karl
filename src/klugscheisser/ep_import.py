"""v11: import cached Elite Prospects pages (`ep_profiles.raw`) into facts, season_stats, career_stints,
`ep_profiles.data`, the name pronunciation and handle candidates. No page is fetched here (that is
`collectors.eliteprospects.fetch`); the only download is the player's pronunciation mp3.

Per person, idempotent: the rows with source "eliteprospects" are brought to exactly what the page says
(unchanged rows keep their id, stale ones are deleted, new ones added), so a second run changes nothing.
Other sources' rows are never deleted or edited, with two exceptions:
- conflicts: for `facts.queries.EP_FIRST_KINDS` a confirmed fact of another source with a different value is
  set to status "conflict" (EP wins, the other value stays visible as conflict). The ids we set are kept in
  `ep_profiles.data["conflicts"]`; on the next import those that now agree (or EP has no value any more) are
  set back to "confirmed". Conflicts set by others are left alone.
- handles: an Instagram / X link on the page adds the evidence "eliteprospects" to the handle (new handles
  start as "candidate"; the status is never raised here).

Awards that another source already has (same award and season, or the same medal and year) are not added a
second time; they are listed in `ep_profiles.data["duplicates"]` with the id of the existing fact.

Stints: one per team (national rows of one team under one name, see parse.stint_team_name) and kind over
consecutive seasons; season_to None when the stint reaches the season of the page's data date. The transfer
into the team (and extensions) are attached as `quote` ("Transfer 21.08.2023: A → B (…). Quelle: <link>"),
source_url stays the player page. Stints without a transfer have quote None (structured data, like Wikidata).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.eliteprospects.parse import (
    COUNTRIES,
    EpRecord,
    EpSeasonRow,
    parse,
    short_league,
    stint_team_name,
)
from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize
from klugscheisser.facts.extract import compare_key
from klugscheisser.facts.queries import EP_FIRST_KINDS
from klugscheisser.facts.teams import TeamResolver, team_key
from klugscheisser.facts.wikidata_import import POSITION_DE, medal_metal
from klugscheisser.handles.service import clean_handle
from klugscheisser.stats.queries import season_label

log = logging.getLogger(__name__)

SOURCE = "eliteprospects"
BLOCKS = (
    "playerData",
    "initialLeagueStats",
    "initialCupStats",
    "initialCareerTotals",
    "initialAwards",
    "initialTransactions",
    "initialGameLog",
)
AUDIO_LABEL = "Aufnahme (Elite Prospects)"
AUDIO_PREFIX = "https://files.eliteprospects.com/"
AUDIO_MAX_BYTES = 2_000_000
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)
STAT_COLUMNS = (
    "games",
    "goals",
    "assists",
    "points",
    "penalty_minutes",
    "plus_minus",
    "wins",
    "losses",
    "ties",
    "shutouts",
    "goals_against",
    "saves",
    "save_pct",
    "gaa",
    "toi",
    "team_kind",
)
DETAIL_DE = {
    "C": "Center",
    "LW": "linker Flügel",
    "RW": "rechter Flügel",
    "W": "Flügel",
    "F": "Stürmerin",
    "D": "Verteidigerin",
    "G": "Torhüterin",
}
METAL_DE = {"gold": "Gold", "silver": "Silber", "bronze": "Bronze"}
EVIDENCE_DETAIL = "auf dem Elite-Prospects-Profil verlinkt"


@dataclass
class FactSpec:
    kind: str
    value: dict
    value_text: str
    season: int | None = None

    def __post_init__(self) -> None:
        self.value = {k: v for k, v in self.value.items() if v not in (None, "", [])}

    @property
    def key(self) -> tuple:
        return (
            self.kind,
            json.dumps(self.value, sort_keys=True, ensure_ascii=False),
            self.value_text,
            self.season,
        )


def _fact_key(f: m.Fact) -> tuple:
    return (f.kind, json.dumps(f.value or {}, sort_keys=True, ensure_ascii=False), f.value_text, f.season)


def season_of(d: datetime | date | None) -> int | None:
    """Season (start year) a date belongs to: from August on the new season (2026-10-04 -> 2026)."""
    if d is None:
        return None
    return d.year if d.month >= 8 else d.year - 1


# --- raw files -> ep_profiles ------------------------------------------------------------------------------


def _same_person(a: m.Athlete, raw: dict) -> bool:
    """The cached page belongs to this person: same EP id, or (no EP id stored) the same name. A file from
    another project's database could carry the id of a different person."""
    ep_id = str(raw.get("eliteprospects_id") or "")
    if a.eliteprospects_id:
        return a.eliteprospects_id == ep_id
    return normalize(a.full_name) == normalize(str(raw.get("name") or ""))


def load_dir(s: Session, path: str | Path, athlete_ids: list[int] | None = None) -> dict[str, int]:
    """Upsert cached pages (`<athlete_id>.json`, format of data/eliteprospects/raw) into `ep_profiles`.
    A page older than the stored one is skipped; `data` and `imported_at` are kept."""
    counts: Counter = Counter()
    wanted = set(athlete_ids or [])
    for file in sorted(Path(path).glob("*.json")):
        try:
            raw = json.loads(file.read_text())
        except (OSError, ValueError) as exc:
            log.warning("ep raw file %s unreadable: %s", file.name, exc)
            counts["unreadable"] += 1
            continue
        aid = raw.get("athlete_id")
        if not isinstance(aid, int) or (wanted and aid not in wanted):
            continue
        counts["files"] += 1
        a = s.get(m.Athlete, aid)
        if a is None or not _same_person(a, raw):
            counts["other_person"] += 1
            continue
        fetched = _dt(raw.get("fetched_at")) or datetime.now(UTC)
        blocks = {k: raw.get(k) for k in BLOCKS}
        ep_id = str(raw.get("eliteprospects_id") or a.eliteprospects_id or "")
        if not a.eliteprospects_id and ep_id and not _id_taken(s, ep_id, a.id):
            # 2026-11-01: a page saved by hand (Mac) for a person without id, checked by `html_to_raw`
            a.eliteprospects_id = ep_id
            counts["ep_id_set"] += 1
        url = raw.get("url") or ""
        row = s.get(m.EpProfile, aid)
        if row is None:
            s.add(m.EpProfile(athlete_id=aid, ep_id=ep_id, url=url, fetched_at=fetched, raw=blocks, data={}))
            counts["loaded"] += 1
        elif row.raw == blocks and row.url == url:
            counts["unchanged"] += 1
        elif row.fetched_at and fetched < row.fetched_at:
            counts["older"] += 1  # the fetcher already stored a newer page
        else:
            row.raw, row.url, row.ep_id, row.fetched_at = blocks, url, ep_id, fetched
            counts["updated"] += 1
    s.flush()
    return dict(counts)


def _id_taken(s: Session, ep_id: str, athlete_id: int) -> bool:
    return (
        s.scalar(
            select(m.Athlete.id)
            .where(m.Athlete.eliteprospects_id == ep_id, m.Athlete.id != athlete_id)
            .limit(1)
        )
        is not None
    )


def html_to_raw(s: Session, html_dir: str | Path, out_dir: str | Path) -> dict[str, int]:
    """2026-11-01: saved player pages `<athlete_id>.html` -> raw files `<athlete_id>.json` for `load_dir`.
    A page is kept only when it is hers: the stored EP id equals the page's, or (no id stored) the same name and,
    when both are known, the same birth date. Pages without player data (e.g. a bot challenge) are counted."""
    from klugscheisser.collectors.eliteprospects.fetch import extract_blocks, looks_like_captcha

    counts: Counter = Counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for file in sorted(Path(html_dir).glob("*.htm*")):
        counts["files"] += 1
        stem = file.stem.split("_", 1)[0]
        if not stem.isdigit() or (a := s.get(m.Athlete, int(stem))) is None:
            counts["unknown_athlete_id"] += 1
            continue
        page = file.read_text(errors="replace")
        blocks = extract_blocks(page)
        if blocks is None:
            counts["captcha" if looks_like_captcha(page) else "no_player_data"] += 1
            continue
        p = (blocks.get("playerData") or {}).get("player") or {}
        ep_id, name = str(p.get("id") or ""), str(p.get("name") or "")
        born = p.get("dateOfBirth")
        if a.eliteprospects_id:
            ok = a.eliteprospects_id == ep_id
        else:
            ok = normalize(a.full_name) == normalize(name) and (
                not (born and a.birth_date) or str(a.birth_date) == str(born)[:10]
            )
        if not ok:
            log.warning("ep html %s: page of %s (%s) is not %s", file.name, name, ep_id, a.full_name)
            counts["other_person"] += 1
            continue
        path = p.get("eliteprospectsUrlPath") or f"/player/{ep_id}"
        raw = {
            "athlete_id": a.id,
            "eliteprospects_id": ep_id,
            "name": a.full_name,
            "tier": a.tier,
            "url": f"https://www.eliteprospects.com{path}",
            "fetched_at": datetime.fromtimestamp(file.stat().st_mtime, UTC).isoformat(),
            **blocks,
        }
        (out / f"{a.id}.json").write_text(json.dumps(raw, ensure_ascii=False))
        counts["written"] += 1
    return dict(counts)


def _dt(value: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


# --- facts from a record ------------------------------------------------------------------------------------


def _position(rec: EpRecord) -> FactSpec | None:
    p = rec.profile
    if not p.position:
        return None
    text = POSITION_DE.get(p.position, p.position)
    detail = [x for x in p.detailed_position if x != p.position]
    detail_de = "/".join(DETAIL_DE.get(x, x) for x in p.detailed_position) if detail else None
    value = {"text": text, "code": p.position, "detail": "/".join(p.detailed_position) if detail else None}
    return FactSpec("position", value, f"{text} ({detail_de})" if detail_de else text)


def _contract_text(text: str, season_to: int | None) -> str:
    if season_to is None:
        return text
    rest = text[5:].strip()  # "26/27+1" -> "+1"
    return season_label(season_to) + (f" ({rest})" if rest else "")


def _award_name(name: str) -> str:
    """'SDHL (W) Most Points' -> 'SDHL Most Points'; 'SDHL (W) SM-silver Medal' -> 'SDHL SM-Silber' (a lower
    "medal" must not read as an Olympic/WM medal on the Erfolge list)."""
    text = short_league(name)
    for en, de in (("gold", "Gold"), ("silver", "Silber"), ("bronze", "Bronze")):
        text = re.sub(rf"\b{en}\s+medal\b", de, text, flags=re.IGNORECASE)
        text = re.sub(rf"(?<=-){en}\b", de, text, flags=re.IGNORECASE)
    return text


def award_specs(rec: EpRecord) -> list[FactSpec]:
    out = []
    for aw in rec.awards:
        year = aw.season + 1  # the award is given at the end of the season (Olympics Feb, WM spring)
        if aw.medal:
            event = f"{aw.event} {year}"
            text = f"{event}: {METAL_DE[aw.medal]}"
            value = {
                "text": text,
                "metal": aw.medal,
                "year": year,
                "event": event,
                "season": aw.season,
                "league": aw.league,
            }
            out.append(FactSpec("medal", value, text, season=aw.season))
            continue
        name = _award_name(aw.name)
        extra = aw.number or aw.detail
        text = f"{name}" + (f" ({extra})" if extra else "") + f", {season_label(aw.season)}"
        value = {
            "text": name,
            "year": year,
            "season": aw.season,
            "number": aw.number,
            "detail": aw.detail,
            "league": aw.league,
        }
        out.append(FactSpec("award", value, text, season=aw.season))
    return out


def fact_specs(rec: EpRecord) -> list[FactSpec]:
    """All facts of one page (without the award dedupe against other sources)."""
    p, out = rec.profile, []
    if p.place_of_birth:
        text = p.place_of_birth + (f", {p.birth_region}" if p.birth_region else "")
        text += f" ({p.birth_country})" if p.birth_country else ""
        value = {
            "text": p.place_of_birth,
            "region": p.birth_region,
            "country": p.birth_country,
            "raw": p.place_of_birth_raw,
        }
        out.append(FactSpec("birth_place", value, text))
    if p.height_cm:
        out.append(FactSpec("height_cm", {"number": p.height_cm}, f"{p.height_cm} cm"))
    if p.weight_kg:
        out.append(FactSpec("weight_kg", {"number": p.weight_kg}, f"{p.weight_kg} kg"))
    if spec := _position(rec):
        out.append(spec)
    if p.shoots in ("L", "R"):
        side = "links" if p.shoots == "L" else "rechts"
        value = {"text": p.shoots, "catches": True if p.catches else None}
        out.append(FactSpec("shoots", value, f"{side} (Fanghand)" if p.catches else side))
    if p.youth_team:
        out.append(FactSpec("youth_team", {"text": p.youth_team}, p.youth_team))
    latest = rec.latest
    if p.status != "retired" and latest.get("team"):
        season = latest.get("season")
        bits = [x for x in (short_league(latest.get("league")), season and season_label(season)) if x]
        value = {
            "team": latest["team"],
            "league": latest.get("league"),
            "season": season,
            "jersey": latest.get("jersey_number"),
            "status": latest.get("status"),
        }
        text = latest["team"] + (f" ({', '.join(bits)})" if bits else "")
        out.append(FactSpec("current_team", value, text, season=season))
        if latest.get("jersey_number") is not None:
            n = latest["jersey_number"]
            out.append(
                FactSpec(
                    "jersey_number",
                    {"number": n, "team": latest["team"]},
                    f"Nummer {n} ({latest['team']})",
                    season=season,
                )
            )
    if p.contract:
        value = {"text": p.contract, "season_to": p.contract_season_to}
        out.append(FactSpec("contract", value, _contract_text(p.contract, p.contract_season_to)))
    for agency in p.agencies:
        out.append(FactSpec("agency", {"text": agency}, agency))
    for d in rec.drafts:
        bits = [f"{d.league}-Draft {d.year}"]
        if d.round:
            bits.append(f"Runde {d.round}")
        if d.overall:
            bits.append(f"Nr. {d.overall}")
        text = f"Gedraftet von {d.team} ({', '.join(bits)})" if d.team else ", ".join(bits)
        value = {"league": d.league, "year": d.year, "round": d.round, "overall": d.overall, "team": d.team}
        out.append(FactSpec("draft", value, text))
    for r in rec.relatives:  # SENSITIVE kind: shown with "Vorsicht" (facts.queries), like other sources
        text = f"{r.relation}: {r.name}"
        out.append(
            FactSpec("family_in_sport", {"text": text, "relation": r.relation, "person": r.name}, text)
        )
    out.extend(award_specs(rec))
    # one fact per value
    unique: dict[tuple, FactSpec] = {}
    for spec in out:
        unique.setdefault(spec.key, spec)
    return list(unique.values())


# --- award dedupe -------------------------------------------------------------------------------------------

_FILLER = {"w", "the", "of", "der", "die", "des", "das", "award", "trophy"}


def _tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in _FILLER}


def _year(f: m.Fact) -> int | None:
    v = f.value or {}
    for raw in (v.get("year"), str(v.get("date") or "")[:4]):
        if raw and str(raw).isdigit():
            return int(raw)
    return f.season + 1 if f.season else None


def _duplicate_of(spec: FactSpec, others: list[m.Fact]) -> m.Fact | None:
    """An award/medal of another source that is the same as this EP award (see module docstring)."""
    v = spec.value
    for f in others:
        year = _year(f)
        if spec.kind == "medal":
            metal = (f.value or {}).get("metal") if f.kind == "medal" else medal_metal(f.value_text)
            if metal == v["metal"] and year == v["year"]:
                return f
            continue
        if f.kind != "award" or year is None or year not in (v["season"], v["year"]):
            continue
        mine, theirs = _tokens(v["text"]), _tokens((f.value or {}).get("text") or f.value_text)
        if theirs and (mine == theirs or (len(theirs) >= 2 and theirs <= mine)):
            return f
    return None


# --- conflicts ----------------------------------------------------------------------------------------------

EXONYMS = {
    "prag": "praha",
    "prague": "praha",
    "genf": "geneve",
    "geneva": "geneve",
    "olmutz": "olomouc",
    "reichenberg": "liberec",
    "jakobstad": "pietarsaari",
    "vanda": "vantaa",
    "helsingfors": "helsinki",
    "esbo": "espoo",
    "abo": "turku",
    "munich": "munchen",
    "vienna": "wien",
    "lucerne": "luzern",
    "gothenburg": "goteborg",
    "copenhagen": "kobenhavn",
    "kopenhagen": "kobenhavn",
    "moskau": "moskva",
    "moscow": "moskva",
    "new york city": "new york",
    "sankt gallen": "st gallen",
    "bienne": "biel",
    "biel bienne": "biel",
    "freiburg": "fribourg",
    "neuenburg": "neuchatel",
    "pilsen": "plzen",
    "brunn": "brno",
    "budweis": "ceske budejovice",
    "cologne": "koln",
    "kischinau": "chisinau",
    "chisinau": "chisinau",
}
_PLACE_NOISE = re.compile(r"\b(parish|forsamling|gemeinde|kommune|municipality|stadt)\b")
_COUNTRY_WORDS = {normalize(c) for c in COUNTRIES} | {
    "schweiz",
    "suisse",
    "svizzera",
    "schweden",
    "finnland",
    "kanada",
    "deutschland",
    "tschechien",
    "osterreich",
    "danemark",
    "norwegen",
    "vereinigte staaten",
    "united states",
}


def _place_key(text: str | None) -> str | None:
    """'Prag 2' -> 'praha', 'Wald ZH' -> 'wald', 'Själevads församling' -> 'sjalevads'; a country -> None."""
    first = normalize((text or "").split(",")[0])
    if re.search(r"\w+s forsamling\b", first):  # Swedish parish: "själevads församling" -> "själevad"
        first = re.sub(r"(\w+)s forsamling\b", r"\1", first)
    first = _PLACE_NOISE.sub(" ", first)
    first = re.sub(r"\s+(\d+|[a-z]{2})$", "", first.strip())  # "prag 2", "wald zh", "buchs ag"
    first = re.sub(r"\s+", " ", first).strip()
    if not first or first in _COUNTRY_WORDS:
        return None
    return EXONYMS.get(first, first)


def _position_code(value: dict) -> str | None:
    if value.get("code"):
        return str(value["code"]).upper()[:1]
    text = normalize(value.get("text") or "")
    if re.search(r"torh|goal|keeper", text):
        return "G"
    if re.search(r"verteid|defen", text):
        return "D"
    if re.search(r"sturm|sturmer|forward|center|centre|flugel|wing", text):
        return "F"
    return compare_key("position", value)


def _hand(value: dict) -> str | None:
    text = normalize(str(value.get("text") or ""))
    if text[:1] in ("l", "r"):
        return text[:1].upper()
    return None


def value_key(kind: str, value: dict, value_text: str = "") -> str | None:
    """Comparable value for the EP-first kinds (None: cannot be compared)."""
    v = value or {}
    if kind in ("height_cm", "weight_kg"):
        try:
            return str(round(float(v.get("number"))))
        except (TypeError, ValueError):
            return None
    if kind == "position":
        return _position_code(v)
    if kind == "shoots":
        return _hand(v)
    if kind == "birth_place":
        return _place_key(v.get("text") or value_text)
    if kind in ("youth_team", "current_team"):
        name = v.get("team") or v.get("text") or value_text
        return team_key(name) if name else None
    if kind == "contract":
        return str(v["season_to"]) if v.get("season_to") else (normalize(v.get("text") or "") or None)
    if kind == "agency":
        return normalize(v.get("text") or value_text) or None
    return None


def _disagrees(kind: str, other: m.Fact, ep: list[m.Fact]) -> bool | None:
    """True: the other source says something else than EP; False: agrees; None: not comparable."""
    ov = other.value or {}
    if kind == "jersey_number":
        team = ov.get("team")
        mine = [f for f in ep if team and team_key((f.value or {}).get("team") or "") == team_key(team)]
        if not mine or ov.get("number") in (None, ""):
            return None
        return all(str(ov["number"]).strip() != str((f.value or {}).get("number")) for f in mine)
    if kind == "draft":
        if not ov.get("year"):
            return None
        return int(ov["year"]) not in {
            int((f.value or {})["year"]) for f in ep if (f.value or {}).get("year")
        }
    theirs = value_key(kind, ov, other.value_text)
    mine = {value_key(kind, f.value or {}, f.value_text) for f in ep} - {None}
    if theirs is None or not mine:
        return None
    return theirs not in mine


def _sync_conflicts(
    s: Session, a: m.Athlete, ep_facts: list[m.Fact], owned_before: set[int], counts
) -> list[int]:
    by_kind: dict[str, list[m.Fact]] = {}
    for f in ep_facts:
        by_kind.setdefault(f.kind, []).append(f)
    others = s.scalars(
        select(m.Fact).where(
            m.Fact.athlete_id == a.id,
            m.Fact.kind.in_(EP_FIRST_KINDS),
            m.Fact.source != SOURCE,
            m.Fact.status.in_(("confirmed", "conflict")),
        )
    ).all()
    owned: set[int] = set()
    for f in others:
        mine = by_kind.get(f.kind) or []
        if not mine or not _disagrees(f.kind, f, mine):
            continue
        if f.status == "confirmed":
            f.status = "conflict"
            counts["conflicts"] += 1
            owned.add(f.id)
        elif f.id in owned_before:
            owned.add(f.id)  # still ours
    for fid in owned_before - owned:
        f = s.get(m.Fact, fid)
        if f is not None and f.status == "conflict":
            f.status = "confirmed"
            counts["restored"] += 1
    s.flush()
    return sorted(owned)


# --- writers ------------------------------------------------------------------------------------------------


def _sync_facts(s: Session, a: m.Athlete, specs: list[FactSpec], url: str, counts) -> list[m.Fact]:
    existing = s.scalars(
        select(m.Fact).where(m.Fact.athlete_id == a.id, m.Fact.source == SOURCE).order_by(m.Fact.id)
    ).all()
    wanted = {spec.key: spec for spec in specs}
    kept: dict[tuple, m.Fact] = {}
    for f in existing:
        key = _fact_key(f)
        if key in wanted and key not in kept and f.source_url == url:
            kept[key] = f
            if f.status != "confirmed":  # our own value: EP ranks first for its kinds
                f.status = "confirmed"
        else:
            s.delete(f)
            counts["facts_removed"] += 1
    for key, spec in wanted.items():
        if key in kept:
            continue
        f = m.Fact(
            athlete_id=a.id,
            kind=spec.kind,
            value=spec.value,
            value_text=spec.value_text,
            season=spec.season,
            source=SOURCE,
            source_url=url,
            quote=None,
            confidence=1.0,
            status="confirmed",
            extractor=SOURCE,
        )
        s.add(f)
        kept[key] = f
        counts["facts_added"] += 1
    s.flush()
    for spec in specs:
        counts[f"fact:{spec.kind}"] += 1
    return list(kept.values())


def _sync_season_stats(
    s: Session, a: m.Athlete, rows: list[EpSeasonRow], url: str, fetched: datetime, counts
) -> None:
    existing = {
        (r.season, r.league, r.team_name, r.phase): r
        for r in s.scalars(
            select(m.SeasonStat).where(m.SeasonStat.athlete_id == a.id, m.SeasonStat.source == SOURCE)
        )
    }
    seen = set()
    for row in rows:
        key = (row.season, row.league, row.team, row.phase)
        seen.add(key)
        values = {c: getattr(row, c) for c in STAT_COLUMNS}
        values["ep_team_id"] = _team_id_of(row.team_path)
        old = existing.get(key)
        if old is None:
            s.add(
                m.SeasonStat(
                    athlete_id=a.id,
                    season=row.season,
                    league=row.league,
                    team_name=row.team,
                    phase=row.phase,
                    source=SOURCE,
                    source_url=url,
                    fetched_at=fetched,
                    **values,
                )
            )
            counts["season_stats_added"] += 1
            continue
        changed = {c: v for c, v in values.items() if getattr(old, c) != v}
        if old.source_url != url:
            changed["source_url"] = url
        if changed:
            for c, v in changed.items():
                setattr(old, c, v)
            old.fetched_at = fetched
            counts["season_stats_updated"] += 1
    for key, old in existing.items():
        if key not in seen:
            s.delete(old)
            counts["season_stats_removed"] += 1
    counts["season_stats"] += len(rows)
    s.flush()


def _team_id_of(path: str | None) -> str | None:
    hit = re.match(r"/team/(\d+)", path or "")
    return hit.group(1) if hit else None


def _transfer_season(d: date) -> int:
    """Season a transfer is for: moves from May on count for the coming season."""
    return d.year if d.month >= 5 else d.year - 1


def _transfer_line(t) -> str:
    when = f"{t.date:%d.%m.%Y}" if t.date else "?"
    label = "Vertragsverlängerung" if t.type == "extension" else "Leihe" if t.type == "loan" else "Transfer"
    teams = f"{t.from_team} → {t.to_team}" if t.from_team and t.from_team != t.to_team else (t.to_team or "")
    line = f"{label} {when}: {teams}" + (f" ({t.extra})" if t.extra else "")
    return line + (f". Quelle: {t.source}" if t.source else "")


@dataclass
class StintSpec:
    team_name: str
    kind: str
    league: str | None
    season_from: int
    season_to: int | None
    team_ids: set[str]
    quote: str | None = None
    ep_team_id: str | None = None  # v12: the Elite Prospects team id of this stint
    former: list[str] = field(default_factory=list)  # earlier names of the same EP team


def stint_specs(rec: EpRecord, current: int | None) -> list[StintSpec]:
    """Stints from the regular-season rows; transfers into the team attached as quote."""
    seasons: dict[tuple[str, str], dict[int, list[EpSeasonRow]]] = {}
    for r in rec.seasons:
        if r.phase != "regular":
            continue
        # v12: clubs/colleges/youth teams by Elite Prospects team id (a renamed club is one stint, from–until);
        # national teams by name (EP has separate ids for "Switzerland" and "Switzerland Olympics")
        tid = _team_id_of(r.team_path) if r.team_kind != "national" else None
        group = f"ep:{tid}" if tid else stint_team_name(r.team)
        seasons.setdefault((group, r.team_kind), {}).setdefault(r.season, []).append(r)
    out: list[StintSpec] = []
    for (group, kind), by_season in seasons.items():
        years = sorted(by_season)
        runs: list[list[int]] = [[years[0]]]
        for y in years[1:]:
            if y == runs[-1][-1] + 1:
                runs[-1].append(y)
            else:
                runs.append([y])
        for run in runs:
            rows = [r for y in run for r in by_season[y]]
            league_rows = [r for r in rows if not r.cup and not r.aggregate] or rows
            league = max(league_rows, key=lambda r: (r.season, r.games or 0)).league or None
            to = None if current is not None and run[-1] >= current else run[-1]
            ids = {i for i in (_team_id_of(r.team_path) for r in rows) if i}
            newest = max(rows, key=lambda r: (r.season, r.games or 0))
            name = stint_team_name(newest.team)
            spec = StintSpec(name, kind, league, run[0], to, ids)
            if group.startswith("ep:"):
                spec.ep_team_id = group[3:]
                spec.former = sorted({stint_team_name(r.team) for r in rows} - {name})
            out.append(spec)
    for spec in out:
        lines = []
        for t in rec.transfers:
            if t.date is None or not t.to_team or t.type not in ("confirmed", "loan", "extension"):
                continue
            same = (_team_id_of(t.to_team_path) in spec.team_ids) or (
                team_key(stint_team_name(t.to_team)) == team_key(spec.team_name)
            )
            ts = _transfer_season(t.date)
            end = spec.season_to if spec.season_to is not None else 9999
            first = spec.season_from - 1 if t.type != "extension" else spec.season_from
            if same and first <= ts <= end:
                lines.append(_transfer_line(t))
        if spec.former:
            lines.insert(0, "Früher: " + ", ".join(spec.former))
        spec.quote = "\n".join(lines) or None
    return sorted(out, key=lambda x: (x.season_from, x.kind, x.team_name))


def _sync_stints(s: Session, a: m.Athlete, specs: list[StintSpec], url: str, counts) -> None:
    resolver: TeamResolver | None = None
    existing = {
        (st.team_name, st.kind, st.season_from): st
        for st in s.scalars(
            select(m.CareerStint).where(m.CareerStint.athlete_id == a.id, m.CareerStint.source == SOURCE)
        )
    }
    seen = set()
    for spec in specs:
        key = (spec.team_name[:200], spec.kind, spec.season_from)
        seen.add(key)
        old = existing.get(key)
        if resolver is None:
            resolver = TeamResolver(s)
        team_id = _resolve_team(resolver, spec)
        fields = {
            "league": (spec.league or None) and spec.league[:100],
            "season_to": spec.season_to,
            "team_id": team_id,
            "ep_team_id": spec.ep_team_id,
            "quote": spec.quote,
            "source_url": url,
            "status": "confirmed",
        }
        if old is None:
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_name=key[0],
                    kind=spec.kind,
                    season_from=spec.season_from,
                    role="player",
                    source=SOURCE,
                    confidence=1.0,
                    **fields,
                )
            )
            counts["stints_added"] += 1
            continue
        changed = {k: v for k, v in fields.items() if getattr(old, k) != v}
        for k, v in changed.items():
            setattr(old, k, v)
        counts["stints_updated"] += bool(changed)
    for key, old in existing.items():
        if key not in seen:
            s.delete(old)
            counts["stints_removed"] += 1
    counts["stints"] += len(specs)
    s.flush()


def _resolve_team(resolver: TeamResolver, spec: StintSpec) -> int | None:
    if spec.kind == "national":
        country = COUNTRIES.get(spec.team_name)
        if country is None:
            return None  # youth national teams ("Switzerland U18") are not our team rows
        # our national team rows are the women's teams; the page is a women's player page
        return resolver.resolve(spec.team_name, f"{spec.team_name} women", national=True, country=country)
    return resolver.resolve(spec.team_name, youth=spec.kind == "youth")


# --- pronunciation and handles ------------------------------------------------------------------------------


def _pronunciation(s: Session, a: m.Athlete, rec: EpRecord, http, counts) -> None:
    url = rec.profile.pronunciation_url
    if not url or not url.startswith(AUDIO_PREFIX):
        return
    row = s.get(m.NamePronunciation, a.id)
    if row is not None and row.source == "wikimedia":
        counts["audio_skipped_wikimedia"] += 1  # a Wikimedia recording always wins
        return
    if row is not None and row.source not in ("tts", SOURCE):
        return
    if row is not None and row.source == SOURCE and row.source_url == url and row.audio:
        return  # already stored
    try:
        r = http.get(url)
        r.raise_for_status()
        data = r.content
    except Exception as exc:  # the import never fails because of the audio
        log.warning("ep pronunciation download failed for %s: %s", a.id, str(exc)[:160])
        counts["audio_failed"] += 1
        return
    if not data or len(data) > AUDIO_MAX_BYTES:
        counts["audio_failed"] += 1
        return
    if row is None:
        try:
            from klugscheisser.pronounce import language

            lang = language(a)
        except Exception:
            lang = None
        row = m.NamePronunciation(athlete_id=a.id, language=lang, source=SOURCE, label=AUDIO_LABEL)
        s.add(row)
    row.source, row.label, row.source_url, row.mime = SOURCE, AUDIO_LABEL, url, "audio/mpeg"
    row.audio, row.audio_slow = data, None
    counts["audio"] += 1
    s.flush()


_IG_RESERVED = {"p", "reel", "reels", "explore", "stories", "accounts", "tv"}
_X_RESERVED = {"intent", "share", "home", "i", "search", "hashtag"}


def handle_from_url(platform: str, url: str) -> str | None:
    """'https://www.instagram.com/alinamueller_/' -> 'alinamueller_'; 'https://twitter.com/lara72122' ->
    'lara72122' (lower case, like handles.service.clean_handle)."""
    hit = re.search(r"(?:instagram\.com|twitter\.com|x\.com)/@?([A-Za-z0-9_.]+)", url or "")
    if not hit:
        return None
    name = clean_handle(hit.group(1)).strip(".")
    reserved = _IG_RESERVED if platform == "instagram" else _X_RESERVED
    return name if name and name not in reserved else None


def _handles(s: Session, a: m.Athlete, rec: EpRecord, url: str, counts) -> None:
    evidence = {"kind": "eliteprospects", "detail": EVIDENCE_DETAIL, "url": url}
    for platform in ("instagram", "x"):
        name = handle_from_url(platform, rec.links.get(platform, ""))
        if not name:
            continue
        h = s.scalar(
            select(m.Handle).where(
                m.Handle.athlete_id == a.id, m.Handle.platform == platform, m.Handle.handle == name
            )
        )
        if h is None:
            s.add(
                m.Handle(
                    athlete_id=a.id,
                    platform=platform,
                    handle=name,
                    status="candidate",
                    confidence=0.0,
                    evidence=[evidence],
                )
            )
            counts["handles_added"] += 1
        elif not any(e.get("kind") == "eliteprospects" for e in h.evidence or []):
            h.evidence = [*(h.evidence or []), evidence]  # status unchanged: the account check decides
            counts["handles_evidence"] += 1
    s.flush()


# --- one person ---------------------------------------------------------------------------------------------


def _transfer_dict(t) -> dict:
    return {
        "date": t.date.isoformat() if t.date else None,
        "season": _transfer_season(t.date) if t.date else None,
        "from_team": t.from_team,
        "to_team": t.to_team,
        "from_info": t.from_info,
        "to_info": t.to_info,
        "extra": t.extra,
        "type": t.type,
        "source": t.source,
        "url": t.url,
    }


def record_of(row: m.EpProfile) -> EpRecord:
    raw = {
        **(row.raw or {}),
        "athlete_id": row.athlete_id,
        "eliteprospects_id": row.ep_id,
        "url": row.url,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }
    return parse(raw)


def import_one(
    s: Session, a: m.Athlete, row: m.EpProfile, http=None, audio: bool = True, counts=None
) -> Counter:
    counts = counts if counts is not None else Counter()
    rec = record_of(row)
    url = row.url or rec.url or ""
    fetched = row.fetched_at or datetime.now(UTC)
    current = season_of(fetched)
    data = dict(row.data or {})
    # awards that another source already has: not a second visible row
    specs, duplicates = [], []
    others = s.scalars(
        select(m.Fact)
        .where(
            m.Fact.athlete_id == a.id,
            m.Fact.kind.in_(("award", "medal")),
            m.Fact.source != SOURCE,
            m.Fact.status != "unconfirmed",
        )
        .order_by(m.Fact.id)
    ).all()
    for spec in fact_specs(rec):
        dup = _duplicate_of(spec, others) if spec.kind in ("award", "medal") else None
        if dup is None:
            specs.append(spec)
        else:
            duplicates.append(
                {
                    "kind": spec.kind,
                    "text": spec.value_text,
                    "season": spec.season,
                    "same_as_fact_id": dup.id,
                    "same_as_source": dup.source,
                }
            )
    counts["award_duplicates"] += len(duplicates)
    ep_facts = _sync_facts(s, a, specs, url, counts)
    owned_before = {int(x) for x in data.get("conflicts") or []}
    conflicts = _sync_conflicts(s, a, ep_facts, owned_before, counts)
    _sync_season_stats(s, a, rec.seasons, url, fetched, counts)
    _sync_stints(s, a, stint_specs(rec, current), url, counts)
    if audio and http is not None:
        _pronunciation(s, a, rec, http, counts)
    _handles(s, a, rec, url, counts)
    row.data = {
        "form": rec.form,
        "career_totals": rec.career_totals,
        "highlights": rec.highlights,
        "links": rec.links,
        "latest": rec.latest,
        "transfers": [_transfer_dict(t) for t in rec.transfers],
        "conflicts": conflicts,
        "duplicates": duplicates,
    }
    row.imported_at = datetime.now(UTC)
    counts["persons"] += 1
    counts["projected_dropped"] += rec.projected_dropped
    s.flush()
    return counts


def _http_client():
    import httpx

    return httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": BROWSER_UA})


def import_profiles(
    s: Session, athlete_ids: list[int] | None = None, http=None, audio: bool = True
) -> dict[str, int]:
    """Import every cached EP page (or those of `athlete_ids`). `http`: an httpx.Client-like object for the
    pronunciation mp3 (default: a real client, only created when a download is needed)."""
    counts: Counter = Counter()
    q = select(m.EpProfile).order_by(m.EpProfile.athlete_id)
    if athlete_ids:
        q = q.where(m.EpProfile.athlete_id.in_(athlete_ids))
    own_client = None
    try:
        for row in s.scalars(q).all():
            a = s.get(m.Athlete, row.athlete_id)
            if a is None or (a.eliteprospects_id and row.ep_id and a.eliteprospects_id != row.ep_id):
                counts["other_person"] += 1
                continue
            client = http
            if client is None and audio and _needs_audio(s, a, row):
                own_client = own_client or _http_client()
                client = own_client
            try:
                with s.begin_nested():
                    import_one(s, a, row, http=client, audio=audio, counts=counts)
            except Exception as exc:  # one broken page must not stop the rest
                log.exception("ep import failed for %s", a.id)
                counts["errors"] += 1
                counts[f"error:{a.id}:{str(exc)[:80]}"] += 1
    finally:
        if own_client is not None:
            own_client.close()
    return dict(counts)


def _needs_audio(s: Session, a: m.Athlete, row: m.EpProfile) -> bool:
    url = ((row.raw or {}).get("playerData") or {}).get("player", {}) or {}
    url = url.get("pronunciationUrl") if isinstance(url, dict) else None
    if not url:
        return False
    p = s.get(m.NamePronunciation, a.id)
    return p is None or p.source == "tts" or (p.source == SOURCE and p.source_url != url)
