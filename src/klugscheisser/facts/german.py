"""German as an interview language: "bestätigt" / "wahrscheinlich" per person, with the reason (no model calls).

Fabi's question: which players and coaches of OTHER nations could she interview in German? Ondrej's rule
(2026-10-25):

- **bestätigt** only with a German interview (her own words in German, or audio/video in German), with source and
  date. Those rows come from texts (step 2, later): table `facts`, kind `language_evidence`, value
  `{"language": "de", "label": "bestaetigt" | "wahrscheinlich", "reason": "...", "date": "YYYY-MM-DD"}` plus
  `source_url` and the verbatim `quote`. This module already reads them.
- **wahrscheinlich**: every other clue, always with its reason: years at a German-speaking club (derived here from
  career stints, season stats and affiliations with the curated map `seeds/club_languages.yaml`), a stored
  "Sprachen: Deutsch" fact (Wikidata/Wikipedia), a `language_evidence` row labelled likely. A bilingual club
  (Fribourg, Biel, Bozen) is named as bilingual and never counted as plain German.
- No clue: no label. A Swiss (or German-league) club that is NOT German-speaking (Ambrì, Lugano, Lausanne,
  HK Budapest) is named as a "kein Hinweis" note, so an assistant does not read "Swiss league" as German.

Persons of German-speaking nations (SUI, GER, AUT, LIE) get nothing: Fabi's question is about other nations.
Never inferred from a name or a nationality.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.facts.source_site import cite_label, item_info, needs_site, row_site

MAP_PATH = Path(__file__).resolve().parents[3] / "seeds" / "club_languages.yaml"
GERMAN_NATIONS = frozenset({"SUI", "GER", "AUT", "LIE"})
EVIDENCE_KIND = "language_evidence"  # step 2 writes these rows (see module doc)
CONFIRMED, LIKELY = "bestätigt", "wahrscheinlich"
LANG_DE = {
    "de": "Deutsch",
    "fr": "Französisch",
    "it": "Italienisch",
    "rm": "Romanisch",
    "hu": "Ungarisch",
    "cs": "Tschechisch",
    "sk": "Slowakisch",
    "en": "Englisch",
}
SOURCE_DE = {
    "eliteprospects": "Elite Prospects",
    "wikipedia": "Wikipedia",
    "wikidata": "Wikidata",
    "sihf": "sihf.ch",
}  # web-type sources are cited by site + date ("EV Zug, 16.12.2025", facts/source_site.py)
SOURCE_RANK = {"eliteprospects": 0, "sihf": 1, "wikipedia": 2, "wikidata": 3}
TZ = ZoneInfo("Europe/Zurich")


def norm(text: str | None) -> str:
    """'EVZ Women’s Team' -> 'evz women s team' (lower case, no accents, ß -> ss, punctuation -> space)."""
    t = unicodedata.normalize("NFKD", (text or "").replace("ß", "ss").replace("ẞ", "ss"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _phrase_rx(phrase: str) -> re.Pattern:
    star = phrase.endswith("*")
    words = norm(phrase.rstrip("*")).split()
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(r"(?<![a-z0-9])" + body + (r"[a-z0-9]*" if star else "") + r"(?![a-z0-9])")


@dataclass(frozen=True)
class Place:
    """One map entry: a club (or a group of clubs, or a league) and its working language(s)."""

    club: str | None  # display name; None = show the name as written
    where: str
    languages: tuple[str, ...]
    country: str | None
    league_rule: bool = False
    warn: bool = False  # not German-speaking although it plays in a German league: say so

    @property
    def note_worthy(self) -> bool:
        """A non-German club worth a "kein Hinweis" note: Swiss (3 language regions) or `warn`."""
        return not self.german and not self.bilingual_german and (self.country == "SUI" or self.warn)

    @property
    def german(self) -> bool:  # plain German, not bilingual
        return self.languages == ("de",)

    @property
    def bilingual_german(self) -> bool:
        return len(self.languages) > 1 and "de" in self.languages

    @property
    def language_de(self) -> str:
        names = [LANG_DE.get(x, x) for x in self.languages]
        return names[0] if len(names) == 1 else "zweisprachig " + "/".join(names)


@dataclass(frozen=True)
class LanguageMap:
    version: int
    clubs: tuple[tuple[tuple[re.Pattern, ...], Place], ...]
    leagues: tuple[tuple[tuple[re.Pattern, ...], Place], ...]

    def place(
        self, team_name: str | None, league: str | None = None, country: str | None = None
    ) -> Place | None:
        """The club's place: club entries first (exceptions like HK Budapest before any league), then the
        league, then the linked team's country (GER/AUT/LIE = German). Swiss clubs count only when listed."""
        name = norm(team_name)
        if name:
            for rxs, p in self.clubs:
                if any(rx.search(name) for rx in rxs):
                    return p
        lg = norm(league)
        if lg:
            for rxs, p in self.leagues:
                if any(rx.search(lg) for rx in rxs):
                    return p
        if country in ("GER", "AUT", "LIE"):
            land = {"GER": "Deutschland", "AUT": "Österreich"}.get(country, "Liechtenstein")
            return Place(None, f"{land}, deutschsprachig", ("de",), country, league_rule=True)
        return None


def _entries(rows: list[dict], league_rule: bool) -> tuple:
    out = []
    for r in rows or []:
        langs = r["language"] if isinstance(r["language"], list) else [r["language"]]
        place = Place(
            None if league_rule else r.get("club"),
            r.get("where") or r.get("league") or "",
            tuple(langs),
            r.get("country"),
            league_rule,
            bool(r.get("warn")),
        )
        out.append((tuple(_phrase_rx(str(x)) for x in r["match"]), place))
    return tuple(out)


@lru_cache(maxsize=4)
def load_map(path: Path = MAP_PATH) -> LanguageMap:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return LanguageMap(
        int(data.get("version") or 1),
        _entries(data.get("clubs") or [], False),
        _entries(data.get("leagues") or [], True),
    )


def season_start(today: date | None = None) -> int:
    today = today or datetime.now(TZ).date()
    return today.year if today.month >= 8 else today.year - 1


def season_label(y: int) -> str:
    return f"{y}/{(y + 1) % 100:02d}"


def seasons_text(seasons: Iterable[int]) -> str:
    """{2019, 2021, 2022} -> '2019/20, 2021/22–2022/23'."""
    ys = sorted(set(seasons))
    runs: list[list[int]] = []
    for y in ys:
        if runs and y == runs[-1][-1] + 1:
            runs[-1].append(y)
        else:
            runs.append([y])
    return ", ".join(
        season_label(r[0]) if len(r) == 1 else f"{season_label(r[0])}–{season_label(r[-1])}" for r in runs
    )


@dataclass
class ClubYears:
    """One club (map entry) in a person's career: seasons, games, whether she is there now."""

    name: str
    place: Place
    seasons: set[int] = field(default_factory=set)
    # seasons from Wikipedia/Wikidata/web stints: used only when no season table, EP stint or roster has the club
    text_seasons: set[int] = field(default_factory=set)
    games: dict[int, int] = field(default_factory=dict)  # season -> games (best source)
    years_unknown: bool = False
    current: bool = False
    sources: dict[str, str] = field(default_factory=dict)  # source label -> url
    roles: set[str] = field(default_factory=set)

    @property
    def n_seasons(self) -> int:
        return len(self.seasons)

    @property
    def n_games(self) -> int | None:
        return sum(self.games.values()) if self.games else None

    def where(self) -> str:
        w = self.place.where
        if "sprach" in w or not self.place.german:
            return w
        return f"{w}, deutschsprachig"

    def short(self) -> str:
        """'2 Saisons EV Zug (2024/25–2025/26)'; unknown years: 'EHC Basel (Jahre unbekannt)'."""
        if not self.seasons:
            return f"{self.name} (Jahre unbekannt)"
        n = self.n_seasons
        return f"{n} Saison{'s' if n > 1 else ''} {self.name} ({seasons_text(self.seasons)})"

    def long(self) -> str:
        """'2 Saisons EV Zug (Zug, deutschsprachig), 2024/25–2025/26, 48 Spiele, aktuell dort'."""
        coach = " als Trainer" if self.roles and self.roles <= {"coach", "staff"} else ""
        if not self.seasons:
            return f"{self.name} ({self.where()}){coach}, Jahre unbekannt"
        n = self.n_seasons
        bits = [
            f"{n} Saison{'s' if n > 1 else ''} {self.name} ({self.where()}){coach}",
            seasons_text(self.seasons),
        ]
        if self.n_games:
            bits.append(f"{self.n_games} Spiele")
        if self.current:
            bits.append("aktuell dort")
        return ", ".join(bits)


@dataclass
class GermanHint:
    """The 'Deutsch' line of one person. `label` None = no clue (then only `notes` may say why not)."""

    athlete_id: int
    label: str | None = None
    reasons: list[str] = field(default_factory=list)  # long reasons, strongest first
    short_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # "kein Hinweis" notes (Swiss club, but Italian-speaking)
    sources: list[tuple[str, str]] = field(default_factory=list)  # (label, url)
    german_clubs: list[ClubYears] = field(default_factory=list)
    other_clubs: list[ClubYears] = field(default_factory=list)

    @property
    def german_seasons(self) -> int:
        return len({y for c in self.german_clubs if c.place.german for y in c.seasons})

    def line(self) -> str | None:
        """person: 'Deutsch: wahrscheinlich – 2 Saisons EV Zug (Zug, deutschsprachig), 2024/25–2025/26, …'."""
        if self.label is None:
            if not self.notes:
                return None
            return "Deutsch: kein Hinweis – " + "; ".join(self.notes)
        text = f"Deutsch: {self.label} – " + "; ".join(self.reasons)
        if self.label == LIKELY:
            text += ". Kein deutsches Interview in Karls Daten; Klubjahre sind nur ein Hinweis"
        if self.notes:
            text += ". Nicht deutschsprachig: " + "; ".join(self.notes)
        return text

    def cell(self) -> str | None:
        """team roster column: 'wahrscheinlich – 2 Saisons EV Zug (2024/25–2025/26)'."""
        if self.label is None:
            return "kein Hinweis – " + "; ".join(self.notes) if self.notes else None
        return f"{self.label} – " + "; ".join(self.short_reasons[:2])

    def out(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.line(),
            "kurz": self.cell(),
            "quellen": [{"quelle": q, "url": u} for q, u in self.sources[:3]],
        }


def _season_of(text: str | None) -> int | None:
    mt = re.match(r"\s*(\d{4})", text or "")
    return int(mt.group(1)) if mt else None


def club_years(
    s: Session, athlete_ids: list[int], today: date | None = None, lang_map: LanguageMap | None = None
) -> dict[int, list[ClubYears]]:
    """Per person: every career club the map knows (German or not), merged across sources by map entry."""
    lm = lang_map or load_map()
    now = season_start(today)
    out: dict[int, dict[str, ClubYears]] = defaultdict(dict)
    if not athlete_ids:
        return {}

    def get(aid: int, team_name: str, place: Place) -> ClubYears:
        name = place.club or team_name
        key = place.club or norm(team_name)
        cy = out[aid].get(key)
        if cy is None:
            cy = out[aid][key] = ClubYears(name, place)
        return cy

    def add_source(cy: ClubYears, source: str, url: str | None, site: str | None = None) -> None:
        label = cite_label(source, url, site, SOURCE_DE)
        if url and label not in cy.sources:
            cy.sources[label] = url

    # season stats: one row per season/league/phase/source; games = best source per season
    games: dict[tuple[int, str, int, str], int] = defaultdict(int)
    for r in s.scalars(
        select(m.SeasonStat).where(
            m.SeasonStat.athlete_id.in_(athlete_ids), m.SeasonStat.team_kind != "national"
        )
    ):
        place = lm.place(r.team_name, r.league)
        if place is None or r.season > now:
            continue
        cy = get(r.athlete_id, r.team_name or "", place)
        cy.seasons.add(r.season)
        add_source(cy, r.source, r.source_url)
        if r.games:
            games[(r.athlete_id, place.club or norm(r.team_name), r.season, r.source)] += r.games
    for (aid, key, season, _src), g in games.items():
        cy = out[aid][key]
        cy.games[season] = max(cy.games.get(season, 0), g)

    # career stints (Elite Prospects, Wikipedia, Wikidata, web). Open end: EP means "still there"; other
    # sources often mean "at the time of writing", so only the first season counts.
    countries = dict(s.execute(select(m.Team.id, m.Team.country)).all())
    stints = s.scalars(
        select(m.CareerStint).where(
            m.CareerStint.athlete_id.in_(athlete_ids), m.CareerStint.kind != "national"
        )
    ).all()
    info = item_info(s, {st.source_item_id for st in stints if needs_site(st.source)})
    for st in stints:
        place = lm.place(st.team_name, st.league, countries.get(st.team_id))
        if place is None:
            continue
        cy = get(st.athlete_id, st.team_name, place)
        cy.roles.add(st.role or "player")
        add_source(cy, st.source, st.source_url, row_site(st, info))
        if st.season_from is None:
            if not cy.seasons:
                cy.years_unknown = True
            continue
        end = st.season_to
        if (
            st.source == "eliteprospects"
        ):  # EP: season_to is the start year of the last season, open = current
            cy.seasons.update(range(st.season_from, min(now if end is None else end, now) + 1))
            continue
        # Wikipedia/web write "2020–2021" for one season (end year) and leave the end open "at the time of
        # writing": count up to the year before the end year, open = only the first season
        last = st.season_from if end is None or end <= st.season_from else end - 1
        cy.text_seasons.update(range(st.season_from, min(last, now) + 1))

    # affiliations with a season ("2025-26", rosters naming the club)
    for af, team in s.execute(
        select(m.Affiliation, m.Team)
        .join(m.Team, m.Team.id == m.Affiliation.team_id)
        .where(m.Affiliation.athlete_id.in_(athlete_ids), m.Team.kind != "national")
    ):
        place = lm.place(team.name, team.league, team.country)
        y = _season_of(af.season)
        if place is None or y is None or y > now:
            continue
        cy = get(af.athlete_id, team.name, place)
        cy.seasons.add(y)

    result: dict[int, list[ClubYears]] = {}
    for aid, clubs in out.items():
        for cy in clubs.values():
            if not cy.seasons:
                cy.seasons = set(cy.text_seasons)
            if cy.seasons:
                cy.years_unknown = False
            cy.current = now in cy.seasons
            cy.roles.discard("")
        result[aid] = sorted(clubs.values(), key=lambda c: (-(max(c.seasons) if c.seasons else 0), c.name))
    return result


def _evidence_rows(s: Session, athlete_ids: list[int]) -> dict[int, list[m.Fact]]:
    out: dict[int, list[m.Fact]] = defaultdict(list)
    if not athlete_ids:
        return out
    for f in s.scalars(
        select(m.Fact)
        .where(m.Fact.athlete_id.in_(athlete_ids), m.Fact.kind.in_([EVIDENCE_KIND, "languages"]))
        .order_by(m.Fact.id)
    ):
        if f.status == "conflict":
            continue
        out[f.athlete_id].append(f)
    return out


def _date_de(v: Any) -> str:
    try:
        return date.fromisoformat(str(v)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(v or "")


def nations_of(s: Session, athletes: list[m.Athlete]) -> dict[int, str | None]:
    """Nationality, else the country of her newest national-team roster entry (staff often lack one)."""
    out = {a.id: a.nationality for a in athletes}
    missing = [a.id for a in athletes if not a.nationality]
    if missing:
        rows = s.execute(
            select(m.EventEntry.athlete_id, m.Team.country)
            .join(m.Team, m.Team.id == m.EventEntry.team_id)
            .join(m.Event, m.Event.id == m.EventEntry.event_id)
            .where(m.EventEntry.athlete_id.in_(missing), m.Team.kind == "national")
            .order_by(m.Event.starts_at.asc().nulls_first())
        ).all()
        for aid, country in rows:  # newest last wins
            if country:
                out[aid] = country
    return out


def hints(
    s: Session,
    athletes: list[m.Athlete],
    today: date | None = None,
    nations: dict[int, str | None] | None = None,
) -> dict[int, GermanHint]:
    """GermanHint per person of a non-German-speaking nation (persons of SUI/GER/AUT/LIE are left out)."""
    nat = nations or nations_of(s, athletes)
    ids = [a.id for a in athletes if (nat.get(a.id) or "") not in GERMAN_NATIONS]
    clubs = club_years(s, ids, today)
    evidence = _evidence_rows(s, ids)
    info = item_info(s, {f.source_item_id for fs in evidence.values() for f in fs if needs_site(f.source)})

    def cite(f: m.Fact) -> str:
        return cite_label(f.source, f.source_url, row_site(f, info), SOURCE_DE)

    out: dict[int, GermanHint] = {}
    for aid in ids:
        h = GermanHint(aid)
        # 1. text evidence (facts/german_evidence.py): German interviews first, with the quote; other German
        # quotes (club releases, copies, own posts) are one combined clue after the club years
        likely_rows: list[tuple[str, m.Fact, dict]] = []
        for f in evidence.get(aid, []):
            v = f.value if isinstance(f.value, dict) else {}
            if f.kind != EVIDENCE_KIND or v.get("language", "de") != "de":
                continue
            day = str(v.get("date") or f.valid_from or "")
            if v.get("label") in ("bestaetigt", CONFIRMED, "confirmed"):
                when = f" ({_date_de(day)})" if day else ""
                reason = f"{v.get('reason') or f.value_text}{when}"
                quote = (f.quote or "").strip()
                if quote:
                    quote = quote if len(quote) <= 140 else quote[:137].rsplit(" ", 1)[0] + " …"
                h.label = CONFIRMED
                h.reasons.insert(len(h.sources), reason + (f": «{quote}»" if quote else ""))
                h.short_reasons.insert(len(h.sources), reason)
                h.sources.append((cite(f), f.source_url))
            else:
                likely_rows.append((day, f, v))
        likely_rows.sort(key=lambda r: (r[1].confidence or 0, r[0]), reverse=True)  # strongest, then newest
        # 2. club years
        for cy in clubs.get(aid, []):
            if cy.place.german or cy.place.bilingual_german:
                h.german_clubs.append(cy)
            else:
                h.other_clubs.append(cy)
        plain = [c for c in h.german_clubs if c.place.german]
        both = [c for c in h.german_clubs if c.place.bilingual_german]
        for c in plain:
            h.reasons.append(c.long())
            h.short_reasons.append(c.short() + (", aktuell" if c.current else ""))
        for c in both:
            h.reasons.append(f"nur zweisprachiger Club: {c.long()}")
            h.short_reasons.append(f"zweisprachig: {c.short()}")
        for c in h.german_clubs:
            best = sorted(c.sources.items(), key=lambda kv: SOURCE_RANK.get(_src_key(kv[0]), 9))
            if best:
                h.sources.append(best[0])
        if likely_rows:
            day, f, v = likely_rows[0]
            when = f" ({_date_de(day)})" if day else ""
            more = len(likely_rows) - 1
            extra = (
                f"; {more} weitere{'r' if more == 1 else ''} solche{'r' if more == 1 else ''} Text{'e' if more > 1 else ''}"
                if more
                else ""
            )
            h.reasons.append(f"{v.get('reason') or f.value_text}{when}{extra}")
            h.short_reasons.append(f"{v.get('short') or v.get('reason') or f.value_text}{when}")
            h.sources.append((cite(f), f.source_url))
        # 3. stored "Sprachen: Deutsch" (Wikidata / Wikipedia), a clue only
        for f in evidence.get(aid, []):
            if f.kind == "languages" and "deutsch" in norm(f.value_text):
                src = cite(f)
                h.reasons.append(f"{src} nennt Deutsch als Sprache")
                h.short_reasons.append(f"{src}: Sprache Deutsch")
                h.sources.append((src, f.source_url))
                break
        if h.label is None and h.reasons:
            h.label = LIKELY
        h.notes = [
            f"{c.name} ({c.where()}{', ' + seasons_text(c.seasons) if c.seasons else ''})"
            for c in h.other_clubs
            if c.place.note_worthy
        ]
        if h.label is not None or h.notes:
            out[aid] = h
    return out


def _src_key(label: str) -> str:
    return next((k for k, v in SOURCE_DE.items() if v == label), label)


def hint(s: Session, athlete: m.Athlete, today: date | None = None) -> GermanHint | None:
    return hints(s, [athlete], today).get(athlete.id)


# --- suche: "Wer spricht Deutsch bei Tschechien?" ------------------------------------------------------------

GERMAN_Q = re.compile(
    r"(sprech\w*|sprich\w*|spricht|kann|können|koennen)\s+(\w+\s+){0,3}deutsch|deutsch\w*\s+(sprech|sprich|interview)"
    r"|auf\s+deutsch|deutschsprachig|german[- ]speak|speaks?\s+german|in\s+german\b|interview\w*\s+(\w+\s+){0,3}german",
    re.I,
)


def wants_german(question: str) -> bool:
    return bool(GERMAN_Q.search(question or ""))


def roster_lines(s: Session, team_ids: list[int] | None = None, today: date | None = None) -> list[str]:
    """Material lines for the newest tournament roster of each (non-German-speaking) national team:
    who has a German clue, who is explicitly not German (Ticino/Romandie clubs), and the data coverage."""
    from klugscheisser import staff as staff_mod

    q = (
        select(m.EventEntry, m.Event)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .join(m.Team, m.Team.id == m.EventEntry.team_id)
        .where(m.Team.kind == "national", m.Event.kind == "tournament")
    )
    if team_ids:
        q = q.where(m.EventEntry.team_id.in_(team_ids))
    else:  # no team named: the newest tournament with rosters (WCh 2026), all its teams
        ev_id = s.scalar(
            select(m.Event.id)
            .join(m.EventEntry, m.EventEntry.event_id == m.Event.id)
            .where(m.Event.kind == "tournament")
            .order_by(m.Event.starts_at.desc().nulls_last())
            .limit(1)
        )
        q = q.where(m.Event.id == ev_id)
    rows = s.execute(q.order_by(m.Event.starts_at.desc().nulls_last())).all()
    newest: dict[int, int] = {}
    entries: dict[int, list[m.EventEntry]] = defaultdict(list)
    for e, ev in rows:
        newest.setdefault(e.team_id, ev.id)
        if newest[e.team_id] == ev.id:
            entries[e.team_id].append(e)
    lines: list[str] = []
    for team_id, es in entries.items():
        team = s.get(m.Team, team_id)
        if team is None or (team.country or "") in GERMAN_NATIONS:
            continue
        ev = s.get(m.Event, newest[team_id])
        athletes = [e.athlete for e in es]
        nat = {a.id: team.country for a in athletes}
        hs = hints(s, athletes, today, nations=nat)
        with_stints = set(
            s.scalars(
                select(m.CareerStint.athlete_id)
                .where(m.CareerStint.athlete_id.in_([a.id for a in athletes]))
                .distinct()
            )
        )
        labelled = []
        for e in sorted(es, key=lambda x: (staff_mod.is_staff_entry(x.role), x.athlete.full_name)):
            h = hs.get(e.athlete_id)
            if h is None or h.label is None:
                continue
            role = (
                staff_mod.LABEL.get(staff_mod.role_of(e.role), e.role)
                if staff_mod.is_staff_entry(e.role)
                else None
            )
            src = h.sources[0] if h.sources else None
            link = f" [{src[0]}]({src[1]})" if src and src[1] else ""
            labelled.append(f"{e.athlete.full_name}{f' ({role})' if role else ''}: {h.cell()}{link}")
        not_de = [
            f"{e.athlete.full_name}: {'; '.join(hs[e.athlete_id].notes)}"
            for e in es
            if e.athlete_id in hs and hs[e.athlete_id].label is None
        ]
        status = "provisorisch" if any(e.list_type != "confirmed" for e in es) else "bestätigt"
        head = f"- **{team.name}** (Kader {ev.name}, {status}; Karriere-Stationen bekannt für {len(with_stints)}/{len(es)}):"
        body = "; ".join(labelled) if labelled else "niemand mit Deutsch-Hinweis in Karls Daten"
        line = f"{head} Deutsch {body}."
        if not_de:
            line += " Schweizer/DACH-Liga, aber nicht deutschsprachig: " + "; ".join(not_de) + "."
        lines.append(line)
    if lines:
        lines.append(
            "_Regel: «bestätigt» nur mit deutschem Interview (Quelle + Datum); «wahrscheinlich» = Hinweis wie Jahre "
            "bei einem deutschsprachigen Club, mit Grund; Ambrì/Lugano (Tessin) sind italienisch-, Lausanne/"
            "Neuchâtel/Genève französischsprachig, Fribourg und Biel zweisprachig. Ohne Hinweis: nicht genannt. "
            "Schweiz/Deutschland/Österreich: nicht bewertet. Im Zweifel den Medienchef des Teams fragen._"
        )
    return lines


__all__ = [
    "CONFIRMED",
    "EVIDENCE_KIND",
    "LIKELY",
    "ClubYears",
    "GermanHint",
    "LanguageMap",
    "Place",
    "club_years",
    "hint",
    "hints",
    "load_map",
    "nations_of",
    "norm",
    "roster_lines",
    "seasons_text",
    "wants_german",
]
