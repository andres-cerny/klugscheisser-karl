"""v6 step 3: questions across people and teams.

Step 0  understand the question → a small form (rules first, Gemini JSON only when the rules find nothing)
Block 1 who?            → persons from game / event / nation / team (written once, filled from the form)
Block 2 fact X          → facts table (one template for every fact kind), sorted; Instagram followers
Block 3 shared          → played together (overlapping seasons at the same team)
Block 4 free topic      → per person her own top documents (one LATERAL query), then the normal checked answer
                          step per group of persons, in parallel ("keine Belege für: …" for the rest)
Comparisons ("die Älteste") are computed here, never written by the model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.facts.source_site import cite_name, needs_site, site_label
from klugscheisser.interfaces import Statement
from klugscheisser.llm.text import add_usage
from klugscheisser.llm.words import fill, players_dat, position_labels_de
from klugscheisser.projects import ContextPool, wording
from klugscheisser.sports import current_spec, home_team_key, national_team_key

log = logging.getLogger(__name__)

GROUP = 8  # persons per answer call in block 4 (parallel)
PER_PERSON_DOCS = 3
MIN_DOCS_PER_PERSON = 10  # newest documents per person Jev reads at least, however many persons


@dataclass
class Form:
    typ: str = "offen"  # fakt | beziehung | offen_pro_person | offen
    kind: str | None = None  # fact kind, "instagram_followers", "stationen"
    sort: str | None = None  # asc | desc
    limit: int | None = None  # "die Älteste" → 1, "die drei Ältesten" → 3
    nations: list[str] = field(default_factory=list)
    days: int | None = None
    extra_kinds: list[str] = field(default_factory=list)  # v12: more kinds asked in the same question
    age_min: int | None = None  # v12: "über 30" → older than 30 (exclusive), "ab 30" → 30 and older
    age_max: int | None = None  # "unter 25" → younger than 25
    age_text: str | None = None  # "über 30" as asked, for the answer


# v12: age thresholds ("wie viele waren über 30", "players under 25", "30 or older")
AGE_OVER = re.compile(r"\b(über|ueber|over|older than|älter als|mehr als|more than)\s+(\d{2})\b", re.I)
AGE_FROM = re.compile(
    r"\b(?:(ab|mindestens|at least)\s+(\d{2})|(\d{2})\s*(oder älter|or older|\+|und älter))", re.I
)
AGE_UNDER = re.compile(r"\b(unter|under|younger than|jünger als|weniger als|less than)\s+(\d{2})\b", re.I)
NOT_AGE = re.compile(
    r"\b(punkte|tore|points|goals|spiele|games|assists|follower|cm|kg|minuten|minutes)\b", re.I
)


# rules: question words → fact kind (+ sort). Order matters (first match wins).
FACT_RULES: list[tuple[re.Pattern, str, str | None, int | None]] = [
    # v13: the youth club (Elite Prospects "Juniorenklub")
    (
        re.compile(
            r"\b(juniorenklub|juniorenclub|juniorin|junioren|nachwuchs\w*|jugendklub|als kind|"
            r"youth (club|team)|grew up playing|junior club)\b",
            re.I,
        ),
        "youth_team",
        None,
        None,
    ),
    (re.compile(r"\b(älteste|ältesten|oldest|eldest)\b", re.I), "birth_date", "asc", 1),
    (re.compile(r"\b(jüngste|jüngsten|youngest)\b", re.I), "birth_date", "desc", 1),
    (re.compile(r"follower", re.I), "instagram_followers", "desc", None),
    (re.compile(r"\b(grösste|größte|grössten|größten|tallest)\b", re.I), "height_cm", "desc", 1),
    (re.compile(r"\b(kleinste|kleinsten|shortest)\b", re.I), "height_cm", "asc", 1),
    (
        re.compile(r"\b(wie gross|wie groß|how tall|körpergrösse|grösse|height)\b", re.I),
        "height_cm",
        "desc",
        None,
    ),
    # v12: before the age rule, so "born in" / "wo geboren" is the place, not the date
    (
        re.compile(
            r"\b(geburtsort|geboren in|wo .{0,30}geboren|woher|hometown|home town|born in|where .{0,30}born|"
            r"birthplace|birth place|place of birth)",
            re.I,
        ),
        "birth_place",
        None,
        None,
    ),
    (
        re.compile(
            r"\b(alter|alt sind|wie alt|how old|age|ages|geburtstag|birthday|geboren am|geburtsdatum|jahrgang|"
            r"born|date of birth|year of birth|birth date|wann .{0,20}geboren)\b",
            re.I,
        ),
        "birth_date",
        "asc",
        None,
    ),
    # gaps: family in the sport ("Hat sie Geschwister im Eishockey?"); a sensitive kind (Vorsicht)
    (
        re.compile(
            r"\b(geschwister|bruder|brüder|schwester|schwestern|familie|familien|eltern|vater|mutter|cousine|"
            r"cousin|siblings?|brothers?|sisters?|family|parents|father|mother)\b",
            re.I,
        ),
        "family_in_sport",
        None,
        None,
    ),
    (
        re.compile(r"\b(rückennummer|trikotnummer|nummer|jersey|number|shirt number)\b", re.I),
        "jersey_number",
        None,
        None,
    ),
    (re.compile(r"\b(sprache|sprachen|spricht|languages?|speaks?)\b", re.I), "languages", None, None),
    (
        re.compile(r"\b(college|universit|hochschule|studier|studium|stud(y|ies|ied))", re.I),
        "college",
        None,
        None,
    ),
    (
        re.compile(
            r"\b(schusshand|welche schusshand|mit welcher hand|schiesst (sie |er )?(links|rechts)|links oder rechts|"
            r"rechts oder links|fanghand|fängt (sie |er )?(links|rechts)|shoots|shoot (left|right)|left or right|"
            r"right or left|which hand|handedness|catch(es)? (left|right)|catches|left.handed|right.handed)\b",
            re.I,
        ),
        "shoots",
        None,
        None,
    ),
    (re.compile(r"\b(draft|gedraftet|drafted)\b", re.I), "draft", None, None),
    (
        re.compile(
            r"\b(wo (hat|haben) .{0,20}gespielt|stationen|vereine|clubs|played for|where did .{0,20}play|"
            r"career stops|teams has she played)",
            re.I,
        ),
        "stationen",
        None,
        None,
    ),
    # v12: the current club ("and her club?", "bei welchem Verein spielt sie?")
    (
        re.compile(
            r"\b((aktuelle[nrs]?|derzeitige[nrs]?|jetzige[nrs]?|current) (club|klub|verein|team)|"
            r"(ihr|ihren|ihrem|her|sein|seinen|seinem|his) (club|klub|verein)|(bei |in |für )?welche[mnr]? (clubs?|klubs?|vereins?|vereine?n?|teams?|mannschaften?)|"
            r"spiel\w* (aktuell |derzeit |jetzt |momentan )?(bei|für) (einem|einer|welche\w*) .{0,30}?(klub|club|verein)|"
            r"wo spielt (sie|er)|spielt (sie|er) (aktuell|derzeit|jetzt|momentan|heute)|"
            r"where does (she|he) play|(which|what) (club|team)|for which (club|team))\b",
            re.I,
        ),
        "current_team",
        None,
        None,
    ),
]
# v12: further kinds asked in the same question ("Kader mit Alter und Club") → one computed document each
EXTRA_KIND_CUES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(alter|age|ages|geboren|born|jahrgang|geburtsdatum)\b", re.I), "birth_date"),
    (re.compile(r"\b(club|clubs|klub|verein|team now)\b", re.I), "current_team"),
    (re.compile(r"\b(grösse|größe|height|tall)\b", re.I), "height_cm"),
    (re.compile(r"\b(nummer|number|jersey)\b", re.I), "jersey_number"),
    (re.compile(r"\b(position|positions)\b", re.I), "position"),
    (re.compile(r"\b(geburtsort|birthplace|hometown)\b", re.I), "birth_place"),
]
RELATION = re.compile(
    r"(zusammen gespielt|zusammengespielt|spielten? zusammen|mitspieler|teamkolleg|kennen sich|played together|"
    r"teammates?\b|same team|(gleichen?|selben?|demselben|denselben|derselben|dem gleichen|im gleichen) (verein|team|club|klub|mannschaft)|played (with|alongside)|mit wem .{0,30}gespielt|"
    r"spielte mit wem|shared a team|(zusammen|gemeinsam)\b.{0,40}\bgespielt|played .{0,30}together)",
    re.I,
)
PER_PERSON = re.compile(r"\b(wer|welche|who|which)\b", re.I)
SAID = re.compile(r"\b(sagt|sagte|sagen|said|says|say|erzählt|erzählte|interview|meint|meinte)\b", re.I)


CLUB_NOW = re.compile(
    r"\b(spielen|spielt|spielend|play|plays|playing|aktuell\w*|derzeit\w*|momentan|jetzt|heute|current\w*|now|"
    r"saison 20\d\d/\d\d|20\d\d/\d\d|20\d\d-\d\d)\b",
    re.I,
)
CLUB_PAST = re.compile(
    r"\b(gespielt|spielte|spielten|played|früher\w*|ehemalig\w*|former\w*|karriere|career|stationen|bisher\w*|"
    r"jemals|ever)\b",
    re.I,
)


def _asks_current_club(question: str) -> bool:
    """'Bei welchen Klubs (Vereine) spielen die Spielerinnen?' asks for the club now, not every career station."""
    return bool(CLUB_NOW.search(question)) and not CLUB_PAST.search(question)


def understand(question: str) -> Form:
    from klugscheisser.adds.wiki import countries_in

    form = Form(nations=countries_in(question))
    # v12: "… – nur die Schweizerinnen?" (a tap-to-send narrowing): only that nation
    if (only := re.search(r"\b(nur|only)\s+(die |der |den |the )?(\w+)", question, re.I)) and (
        narrowed := countries_in(only.group(3))
    ):
        form.nations = narrowed
    if RELATION.search(question):
        form.typ = "beziehung"
        return form
    if not NOT_AGE.search(question):
        over, frm, under = AGE_OVER.search(question), AGE_FROM.search(question), AGE_UNDER.search(question)
        if over or frm or under:
            form.typ, form.kind, form.sort = "fakt", "birth_date", "asc"
            if over:
                form.age_min, form.age_text = int(over.group(2)) + 1, f"über {over.group(2)}"
            elif frm:
                n = int(frm.group(2) or frm.group(3))
                form.age_min, form.age_text = n, f"{n} oder älter"
            if under:
                form.age_max = int(under.group(2)) - 1
                form.age_text = " und ".join(x for x in (form.age_text, f"unter {under.group(2)}") if x)
            return form
    for rx, kind, sort, limit in FACT_RULES:
        if kind == "family_in_sport" and SAID.search(question):
            continue  # "Was sagt sie über ihre Familie?": her own words, not the stored fact
        if hit := rx.search(question):
            if kind == "stationen" and _asks_current_club(question):
                kind = "current_team"  # "Bei welchen Klubs (Vereine) spielen sie?": now, not the career
            elif kind == "current_team" and CLUB_PAST.search(question):
                kind = "stationen"  # "Bei welchen Klubs hat sie gespielt?": the career
            form.typ, form.kind, form.sort = "fakt", kind, sort
            plural = re.search(r"\b(drei|three|3|fünf|five|5|alle|all)\b", question, re.I)
            form.limit = None if plural else limit
            rest = question[: hit.start()] + " " + question[hit.end() :]  # "wo … geboren" is not also a date
            form.extra_kinds = [k for rx2, k in EXTRA_KIND_CUES if k != kind and rx2.search(rest)]
            return form
    if PER_PERSON.search(question):
        form.typ = "offen_pro_person"
    if re.search(
        r"\b(vor dem spiel|before the game|aktuell|derzeit|gerade|form|letzte woche|letzten \d+ tagen|"
        r"last week|last \d+ days|recently|zuletzt)\b",
        question,
        re.I,
    ):
        form.days = 14
    return form


# --- block 1: who? -------------------------------------------------------------------------------------


def persons_for(s: Session, question: str, form: Form) -> tuple[list[int], str | None, list[list[int]]]:
    """Persons in scope + a note + groups per nation (for relations between two teams)."""
    from klugscheisser.answer.engine import event_cues, scope_from_text
    from klugscheisser.retrieval.search import athlete_team_filter

    event = None
    for rx, prefix in event_cues():
        if rx.search(question):
            event = s.scalar(
                select(m.Event)
                .where(m.Event.key.startswith(prefix), m.Event.kind != "game")
                .order_by(m.Event.starts_at.desc().nulls_last())
            )
            break
    groups: list[list[int]] = []
    notes: list[str] = []
    for ioc in form.nations:
        team = s.scalar(select(m.Team).where(m.Team.key == national_team_key(ioc)))
        if team is None:
            continue
        if event is not None:
            ids = list(
                s.scalars(
                    select(m.EventEntry.athlete_id).where(
                        m.EventEntry.event_id == event.id, m.EventEntry.team_id == team.id
                    )
                )
            )
            label = f"{team.name}, Kader {event.name}"
        else:
            ids = athlete_team_filter(s, team).athlete_ids or []
            label = f"Nationalteam {team.name}"
            if len(ids) < 10:  # no roster stored: all active players of that nation in our database
                ids = list(
                    s.scalars(
                        select(m.Athlete.id).where(m.Athlete.nationality == ioc, m.Athlete.role == "athlete")
                    )
                )
                label = f"{wording().players} aus {team.name} in unserer Datenbank"
        ids = _players(s, ids)  # v12: player questions never include coaches/staff (Olympia roster)
        if ids:
            groups.append(ids)
            notes.append(f"{label} ({len(ids)})")
    if groups:
        ids = list(dict.fromkeys(i for g in groups for i in g))
        return ids, " + ".join(notes), groups
    ids, note = scope_from_text(s, question)
    ids = _players(s, ids)
    if note and ids:
        note = re.sub(r"\(\d+ Personen\)$", f"({len(ids)} Personen)", note)
    return ids, note, [ids] if ids else []


def _players(s: Session, ids) -> list[int]:
    from klugscheisser.answer.games import _players as players

    return players(s, ids)


# --- statements from stored facts ------------------------------------------------------------------------


def _stmt(
    aussage: str,
    zitat: str,
    url: str,
    quelle: str,
    prefix: str = "🪪 (Profil)",
    vorsicht: bool = False,
    item_id: int | None = None,
) -> Statement:
    return Statement(
        aussage=aussage,
        zitat=zitat,
        item_id=item_id,
        url=url,
        quelle=quelle,
        quelle_link=f"[{quelle.strip('[]')}]({url})",
        zeile_prefix=prefix,
        vorsicht=vorsicht,
        support=1.0,
    )


SOURCE_NAMES = {"sihf": "sihf.ch"}


def _source_label(source: str, url: str, site: str | None = None) -> str:
    """`site`: citable site + date of a web-type source ("[EV Zug, 16.12.2025]"); without it the domain."""
    if source == "wikidata":
        return f"[Wikidata {url.rsplit('/', 1)[-1]}]"
    if "wikipedia.org" in url:
        return f"[Wikipedia ({url.split('//', 1)[1][:2]})]"
    if site or needs_site(source):
        site = site or site_label(source, url)
        if site:
            return f"[{site}]"
    return f"[{SOURCE_NAMES.get(source, source)}]"


def _value_sort_key(row, kind: str):
    v = row.value or {}
    if kind == "birth_date":
        return v.get("date") or "9999"
    if kind in ("height_cm", "jersey_number", "appearances"):
        try:
            return float(v.get("number"))
        except (TypeError, ValueError):
            return float("-inf")
    return row.value_text


def block_facts(s: Session, form: Form, ids: list[int]) -> tuple[list[Statement], list[str]]:
    from klugscheisser.facts.catalog import FACT_KINDS
    from klugscheisser.facts.queries import facts_for, stints_for

    names = {a.id: a.full_name for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    out: list[Statement] = []
    if form.kind == "instagram_followers":
        rows = []
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids))):
            for h in a.handles:
                p = h.profile or {}
                if h.platform == "instagram" and h.usable and p.get("followers") is not None:
                    rows.append((p["followers"], a, h, p))
        rows.sort(key=lambda r: r[0], reverse=form.sort != "asc")
        have = {r[1].id for r in rows}
        for followers, a, h, p in rows[: form.limit or len(rows)]:
            when = (p.get("fetched_at") or "")[:10]
            label = f"[Instagram-Profil @{h.handle}, Stand {when}]"
            out.append(
                _stmt(
                    f"{a.full_name}: {followers:,} Follower auf Instagram (@{h.handle}).".replace(",", "'"),
                    f"{followers} Follower",
                    f"https://www.instagram.com/{h.handle}/",
                    label,
                    "📸 (Profil)",
                )
            )
        return out, [names[i] for i in ids if i not in have]
    if form.kind == "stationen":
        stints = stints_for(s, ids)
        have = {st.athlete_id for st in stints}
        if len(ids) > 5:  # many persons: their newest stations only (562 lines for 33 players on 24.10.)
            stints = _newest_stints(stints, STATIONS_PER_PERSON)
        for st in stints:
            league = f", {st.league}" if st.league else ""
            out.append(
                _stmt(
                    f"{st.athlete_name}: {st.team_name}{league} ({st.seasons}).",
                    st.quote or f"Mitglied von {st.team_name}",
                    st.source_url,
                    _source_label(st.source, st.source_url, getattr(st, "site", None)),
                    item_id=st.source_item_id,
                )
            )
        return out, [names[i] for i in ids if i not in have]
    rows = facts_for(s, ids, form.kind)
    covered: set[int] = set()
    if form.kind == "current_team":
        extra, covered = _current_club_statements(
            s, [i for i in ids if i not in {r.athlete_id for r in rows}], names
        )
        out += extra
    if form.sort:
        rows.sort(key=lambda r: _value_sort_key(r, form.kind), reverse=form.sort == "desc")
    have = {r.athlete_id for r in rows} | covered
    label_de = FACT_KINDS.get(form.kind, {}).get("de", form.kind)
    for r in rows[: form.limit or len(rows)]:
        conflict = f" (abweichende Angabe: {r.conflicts[0].value_text})" if r.conflicts else ""
        out.append(
            _stmt(
                f"{r.athlete_name}: {label_de} {r.value_text}{conflict}.",
                r.quote or f"{label_de}: {r.value_text}",
                r.source_url,
                _source_label(r.source, r.source_url, getattr(r, "site", None)),
                vorsicht=r.vorsicht,
                item_id=r.source_item_id,
            )
        )
    return out, [names[i] for i in ids if i not in have]


STATIONS_PER_PERSON = 4


def _newest_stints(stints: list, n: int) -> list:
    """Each person's `n` newest stations (open/latest first), in person order; the rest is left out."""
    by: dict[int, list] = {}
    for st in stints:
        by.setdefault(st.athlete_id, []).append(st)
    out = []
    for sts in by.values():
        sts = sorted(
            sts, key=lambda x: (x.season_from is None, -(x.season_from or 0), x.source != "eliteprospects")
        )
        seen: set[str] = set()
        keep = []
        for st in sts:
            from klugscheisser.facts.teams import team_key

            k = team_key(st.team_name)
            if k in seen:
                continue
            seen.add(k)
            keep.append(st)
            if len(keep) >= n:
                break
        out += keep
    return out


def _current_club_statements(
    s: Session, ids: list[int], names: dict[int, str]
) -> tuple[list[Statement], set[int]]:
    """Persons without a stored "current club" fact: the club of this season from the roster affiliation, the
    Elite Prospects season row or the newest open career station (answer.connections.current_clubs)."""
    from klugscheisser.answer.connections import _season_label, current_clubs, season_start

    out = []
    y = season_start()
    found = current_clubs(s, ids, y)
    for aid, clubs in found.items():
        for c in clubs[:2]:
            since = (
                f" (seit {_season_label(c.since)})" if c.since and c.since < y else f" ({_season_label(y)})"
            )
            out.append(
                _stmt(
                    f"{names.get(aid, '?')}: aktueller Klub {c.name}{since}.",
                    f"{c.name}{since}",
                    c.url or "",
                    f"[{c.quelle}]",
                )
            )
    return out, set(found)


def _pairs(stints_a: list, stints_b: list) -> list:
    """Like facts.queries.played_together, on given stints (stored stations + stations from season stats)."""
    from functools import lru_cache

    from klugscheisser.facts.queries import OPEN_END, STATUS_RANK, Pair
    from klugscheisser.facts.teams import team_key

    team_key = lru_cache(maxsize=None)(team_key)  # v12: 46 × 46 players' stations compare names many times

    def _same_team(a, b) -> bool:  # = facts.queries._same_team with the cached key
        if a.ep_team_id and b.ep_team_id:
            return a.ep_team_id == b.ep_team_id
        if a.team_id is not None and b.team_id is not None:
            return a.team_id == b.team_id
        return team_key(a.team_name) == team_key(b.team_name)

    best: dict[tuple, Pair] = {}

    def quality(x) -> tuple:
        return (STATUS_RANK.get(x.status, 3), x.source != "wikidata", -(x.confidence or 0.0))

    for a in stints_a:
        for b in stints_b:
            if a.athlete_id == b.athlete_id or not _same_team(a, b):
                continue
            start = max(a.season_from, b.season_from)
            end = min(
                OPEN_END if a.season_to is None else a.season_to,
                OPEN_END if b.season_to is None else b.season_to,
            )
            if start > end:
                continue
            key = (tuple(sorted((a.athlete_id, b.athlete_id))), a.team_id or team_key(a.team_name))
            old = best.get(key)
            if old is None or (quality(a), quality(b)) < (quality(old.a), quality(old.b)):
                best[key] = Pair(a, b, a.team_name, start, None if end == OPEN_END else end)
    return sorted(best.values(), key=lambda p: (p.a.athlete_name, p.b.athlete_name, p.season_from))


GROUP_WORDS = {
    "nationalteam",
    "nati",
    "nationalmannschaft",
    "team",
    "teams",
    "kader",
    "mannschaft",
    "national",
    "olympia",
    "olympic",
    "olympics",
    "frauen",
    "women",
    "männer",
    "herren",
    "schweizer",
    "swiss",
}


def relation_words(s: Session, question: str) -> list[str]:
    """Team named in a relation question ("Wer spielte zusammen in Northeastern?") → the words that pick its
    stations (stored stations and Elite Prospects season rows), e.g. ["northeastern"]."""
    from klugscheisser.adds.wiki import countries_in
    from klugscheisser.domain.names import normalize
    from klugscheisser.facts.teams import team_key

    words = {
        normalize(w)
        for w in re.findall(r"\b[A-ZÄÖÜÉ][\w'.-]{3,}", question)
        if not countries_in(w) and not PER_PERSON.fullmatch(w)
    } - GROUP_WORDS  # "Nationalteam", "Nati", "Team": the group of the question, not a club
    if not words:
        return []
    names = set(s.scalars(select(m.CareerStint.team_name).distinct())) | set(
        s.scalars(select(m.SeasonStat.team_name).where(m.SeasonStat.team_name.is_not(None)).distinct())
    )
    keys = {w for n in names for w in team_key(n).split()}
    return sorted(w for w in words if w in keys)


def _stints_with(s: Session, words: list[str]) -> list[int]:
    """Persons with a station (stored or from season rows) at a team whose name has one of the words."""
    from klugscheisser.facts.teams import team_key

    rows = list(s.execute(select(m.CareerStint.athlete_id, m.CareerStint.team_name))) + list(
        s.execute(
            select(m.SeasonStat.athlete_id, m.SeasonStat.team_name).where(m.SeasonStat.team_name.is_not(None))
        )
    )
    return sorted({aid for aid, name in rows if set(words) & set(team_key(name).split())})


def relation_pairs(
    s: Session, groups: list[list[int]], ids: list[int], team_words: list[str] | None = None
) -> list:
    """Pairs with overlapping seasons at the same team, from stored stations and (v11) the Elite Prospects
    season rows. `team_words`: only teams with one of these words in the name. One pair per persons + team."""
    from klugscheisser.facts.queries import prefer_ep, stints_for
    from klugscheisser.facts.teams import team_key
    from klugscheisser.stats.search import season_stints

    a_ids, b_ids = (groups[0], groups[1]) if len(groups) >= 2 else (ids, ids)

    def stations(pids: list[int]) -> list:
        # EP national-team rows only when a team is asked for: otherwise every national player pairs with
        # every other one ("Switzerland 2021/22") and hides the club and college stations
        ep = [x for x in season_stints(s, pids) if team_words or x.kind != "national"]
        return [x for x in prefer_ep(stints_for(s, pids) + ep) if x.season_from is not None]

    out = []
    seen = set()
    two_nations = len(groups) >= 2
    for p in _pairs(stations(a_ids), stations(b_ids)):
        if two_nations and "national" in (p.a.kind, p.b.kind):
            continue  # two nations never share a national team; only clubs/colleges count
        if team_words and not set(team_words) & set(team_key(p.team_name).split()):
            continue
        key = (*sorted((p.a.athlete_id, p.b.athlete_id)), p.team_name)
        if p.a.athlete_id == p.b.athlete_id or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def pair_seasons(p) -> str:
    """'2019/20–2020/21', '2019/20 (laut Quelle seit)'."""
    end = f"/{(p.season_to + 1) % 100:02d}" if p.season_to else " (laut Quelle seit)"
    return f"{p.season_from}/{(p.season_from + 1) % 100:02d}" + (
        f"–{p.season_to}{end}" if p.season_to and p.season_to != p.season_from else ""
    )


def block_relations(
    s: Session, groups: list[list[int]], ids: list[int], team_words: list[str] | None = None
) -> list[Statement]:
    """Pairs with overlapping seasons at the same team (see relation_pairs), one statement each."""
    out = []
    for p in relation_pairs(s, groups, ids, team_words):
        b_label = _source_label(p.b.source, p.b.source_url, getattr(p.b, "site", None))
        out.append(
            _stmt(
                f"{p.a.athlete_name} und {p.b.athlete_name} spielten zusammen bei {p.team_name} "
                f"({pair_seasons(p)}); Beleg für {p.b.athlete_name}: {b_label.strip('[]')} {p.b.source_url}",
                p.a.quote or f"{p.a.athlete_name}: {p.a.team_name} {p.a.seasons}",
                p.a.source_url,
                _source_label(p.a.source, p.a.source_url, getattr(p.a, "site", None)),
                "🤝 (gemeinsame Station)",
            )
        )
    return out


# --- block 4: evidence per person ---------------------------------------------------------------------------

EVIDENCE_SQL = text(
    """
    SELECT p.aid, d.item_id FROM unnest(CAST(:ids AS int[])) AS p(aid)
    CROSS JOIN LATERAL (
        SELECT i.id AS item_id
        FROM item_athletes ia
        JOIN items i ON i.id = ia.item_id
        JOIN embeddings e ON e.item_id = i.id AND e.model = :model
        LEFT JOIN classifications c ON c.item_id = i.id
        WHERE ia.athlete_id = p.aid AND i.level = 'full' AND (c.id IS NULL OR c.relevant)
          AND (CAST(:since AS timestamptz) IS NULL OR i.published_at >= CAST(:since AS timestamptz))
        ORDER BY e.vector <=> CAST(:q AS vector)
        LIMIT :k
    ) d
    """
)
NEWEST_SQL = text(
    """
    SELECT p.aid, d.item_id FROM unnest(CAST(:ids AS int[])) AS p(aid)
    CROSS JOIN LATERAL (
        SELECT i.id AS item_id FROM item_athletes ia JOIN items i ON i.id = ia.item_id
        LEFT JOIN classifications c ON c.item_id = i.id
        WHERE ia.athlete_id = p.aid AND i.level = 'full' AND (c.id IS NULL OR c.relevant)
          AND (CAST(:since AS timestamptz) IS NULL OR i.published_at >= CAST(:since AS timestamptz))
        ORDER BY i.published_at DESC NULLS LAST LIMIT :k
    ) d
    """
)


def evidence_items(
    s: Session, settings, question: str, ids: list[int], days: int | None
) -> tuple[dict[int, list[int]], dict[int, float]]:
    """Each person's own top documents for the question (one query for all persons), and how relevant each
    person's best document is (Jev score; with vectors: the rank order)."""
    from klugscheisser.embed.providers import get_embedder

    since = datetime.now(UTC) - timedelta(days=days) if days else None
    if not settings.retrieval_embeddings:
        return _evidence_by_jev(s, settings, question, ids, since)
    params = {"ids": ids, "since": since, "k": PER_PERSON_DOCS}
    try:
        emb = get_embedder(settings)
        qvec = emb.embed_query(question)
        rows = s.execute(EVIDENCE_SQL, {**params, "model": emb.model, "q": str(qvec)}).all()
    except Exception as exc:  # no vector (quota): newest documents per person
        log.warning("evidence by vector failed, newest instead: %s", str(exc)[:120])
        s.rollback()
        rows = s.execute(NEWEST_SQL, params).all()
    out: dict[int, list[int]] = {}
    for aid, item_id in rows:
        out.setdefault(aid, []).append(item_id)
    return out, {aid: float(len(v)) for aid, v in out.items()}


def _evidence_by_jev(
    s: Session, settings, question: str, ids: list[int], since
) -> tuple[dict[int, list[int]], dict[int, float]]:
    """Without embeddings: Jev scores each person's newest documents, the best PER_PERSON_DOCS per person
    (score above 0) are kept. 2026-10-19: Jev reads at most `answer_jev_max_docs` documents in all (many persons →
    fewer newest documents each, the ones with the question's words first), not 80 per person (135 persons were
    ~190 Jev calls)."""
    from klugscheisser.answer.engine import item_doc, rerank, search_terms

    cap = max(1, settings.answer_jev_max_docs)
    k = settings.evidence_jev_docs_per_person
    if ids:
        k = max(MIN_DOCS_PER_PERSON, min(k, cap // len(ids)))
    params = {"ids": ids, "since": since, "k": k}
    rows = s.execute(NEWEST_SQL, params).all()
    item_ids = list(dict.fromkeys(item_id for _, item_id in rows))
    terms = search_terms(question)
    docs = [item_doc(s.get(m.Item, i), terms) for i in item_ids]
    if len(docs) > cap:  # the documents with most of the question's words first (stable: newest first)
        docs = sorted(docs, key=lambda d: -_term_hits(d, terms))[:cap]
    score = {d.item_id: d.score for d in rerank(settings, question, docs)}
    out: dict[int, list[int]] = {}
    for aid, item_id in rows:
        out.setdefault(aid, []).append(item_id)
    best = {
        aid: [i for i in sorted(its, key=lambda i: score.get(i, 0), reverse=True) if score.get(i, 0) > 0][
            :PER_PERSON_DOCS
        ]
        for aid, its in out.items()
    }
    relevance = {aid: max((score.get(i, 0) for i in its), default=0.0) for aid, its in best.items()}
    return best, relevance


def _term_hits(d, terms: list[str]) -> int:
    low = f"{d.meta} {d.text}".lower()
    return sum(1 for w in terms if w in low)


def rank_persons(
    s: Session, question: str, ids: list[int], per_person: dict[int, list[int]], relevance: dict[int, float]
) -> list[int]:
    """Persons with evidence, most relevant first: named in the question, then the best document's score, then
    the scope's own order (roster)."""
    named = set(_named(s, question))
    order = {a: i for i, a in enumerate(ids)}
    with_docs = [a for a in ids if per_person.get(a)]
    return sorted(with_docs, key=lambda a: (a not in named, -relevance.get(a, 0.0), order[a]))


def block_evidence(engine, s: Session, question: str, ids: list[int], days: int | None):
    """Group question answered person by person: each person's best documents, the writer in groups of GROUP.
    2026-10-19: at most `answer_max_persons` persons (most relevant first) and only as many writer calls as the
    answer budget allows (llm/budget.py); the others come back in `skipped` (named in the answer, not hidden).
    Returns (ok, unsure, missing, dropped, usage, n_docs, skipped)."""
    from klugscheisser.answer.engine import item_doc
    from klugscheisser.llm import budget as llm_budget

    st = engine.settings
    per_person, relevance = evidence_items(s, st, question, ids, days)
    names = {a.id: a.full_name for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    ranked = rank_persons(s, question, ids, per_person, relevance)
    limit = max(1, int(getattr(st, "answer_max_persons", 16) or 16))
    chosen, skipped = ranked[:limit], ranked[limit:]
    groups = [chosen[i : i + GROUP] for i in range(0, len(chosen), GROUP)]
    jobs = []
    for g in groups:
        docs, seen = [], set()
        for aid in g:
            for item_id in per_person.get(aid, []):
                if item_id not in seen:
                    seen.add(item_id)
                    docs.append(item_doc(s.get(m.Item, item_id)))
        if docs:
            jobs.append((docs, [names[a] for a in g], g))
    b = llm_budget.current()
    if b is not None and len(jobs) > 1:  # only the writer calls the budget can pay (always the first)
        left, keep = b.left(), []
        for j in jobs:
            est = llm_budget.estimate_llm_usd(st, sum(len(d.text) + len(d.meta) for d in j[0]) + 600)
            if keep and est > left:
                skipped += j[2]
                continue
            keep.append(j)
            left -= est
        jobs = keep
    ok, unsure, gaps, dropped, usage = [], [], [], 0, {"input": 0, "output": 0}
    if jobs:
        with ContextPool(max_workers=min(5, len(jobs))) as pool:  # model calls in parallel
            for o, u, g, d, us in pool.map(lambda j: engine._write(question, j[0], j[1]), jobs):
                ok += o
                unsure += u
                gaps += g
                dropped += d
                add_usage(usage, us)
    skipped_set = set(skipped)
    covered = {n for st_ in ok for n in names.values() if n in st_.aussage}
    missing = [n for a, n in names.items() if n not in covered and a not in skipped_set]
    n_docs = sum(len(v) for v in per_person.values())
    return ok, unsure, missing, dropped, usage, n_docs, [names[a] for a in skipped if a in names]


def try_answer(
    engine,
    s: Session,
    question: str,
    athletes: list[m.Athlete],
    scope_note: str | None,
    since,
    explicit_person: bool,
):
    """Returns (statements, unsure, gaps, dropped, usage, note, durchsucht, ids) or None (→ normal path)."""
    form = understand(question)
    team_words = relation_words(s, question) if form.typ == "beziehung" and not form.nations else []
    if explicit_person and len(athletes) <= 1 and not team_words:
        return None  # one person: the normal path reads her documents (incl. facts in Wikipedia text)
    named = _named(s, question) if form.typ == "fakt" else []
    if team_words:  # "Wer spielte zusammen in Northeastern?": everyone with a station there
        ids = _stints_with(s, team_words)
        note, groups = f"Stationen bei '{', '.join(team_words)}' ({len(ids)} Personen)", [ids]
    elif (
        len(named) >= 2
    ):  # 24.10.: "Bei welchen Klubs spielen Nieminen, Ekoluoma, …?": these, not the whole team
        ids, groups = named, [named]
        note = f"Genannte Personen ({len(named)})"
    else:
        ids, note, groups = persons_for(s, question, form)
    if not ids:
        ids = [a.id for a in athletes]
        note, groups = scope_note, [ids]
    if not ids:
        return None
    if form.typ == "fakt":
        stmts, missing = block_facts(s, form, ids)
        gaps = (
            [f"Kein Wert für: {', '.join(missing[:20])}" + (" …" if len(missing) > 20 else "")]
            if missing
            else []
        )
        return stmts, [], gaps, 0, {}, note, f"{len(ids)} Personen · gespeicherte Fakten", ids
    if form.typ == "beziehung":
        stmts = block_relations(s, groups, ids, team_words or None)
        gaps = [] if stmts else ["Keine gemeinsamen Stationen in den gespeicherten Daten gefunden."]
        return stmts, [], gaps, 0, {}, note, f"{len(ids)} Personen · Stationen verglichen", ids
    if form.typ == "offen_pro_person" and len(ids) > 5:
        ok, unsure, missing, dropped, usage, n_docs, skipped = block_evidence(
            engine, s, question, ids, form.days
        )
        gaps = (
            [f"Keine Belege für: {', '.join(missing[:20])}" + (" …" if len(missing) > 20 else "")]
            if missing
            else []
        )
        if skipped:  # cost limit: the most relevant persons were read, these not
            gaps.append(
                f"Nicht ausgewertet ({len(skipped)} weitere Personen mit Dokumenten, die passendsten zuerst "
                f"gelesen): {', '.join(skipped[:20])}" + (" …" if len(skipped) > 20 else "")
            )
        return (
            ok,
            unsure,
            gaps,
            dropped,
            usage,
            note,
            f"{len(ids)} Personen · {n_docs} Dokumente geprüft",
            ids,
        )
    return None


# --- v11: stats questions ("Wie viele Punkte hat Stalder diese Saison?") ----------------------------------------


def stats_statements(rows: list[dict]) -> list[Statement]:
    """One line per stats row: the computed sentence, the source's own values as the quote, and its link."""
    out = []
    for r in rows:
        rank = f"{r['rang']}. " if r.get("rang") else ""
        note = f" ({r['hinweis']})" if r.get("hinweis") else ""
        label = r["quelle_link"][1 : r["quelle_link"].index("](")]
        out.append(
            Statement(
                aussage=f"{rank}{r['satz']}{note}",
                zitat=r["zitat"],
                item_id=None,
                url=r["url"],
                quelle=f"[{label}]",
                quelle_link=r["quelle_link"],
                zeile_prefix=r["zeile_prefix"],
                support=1.0,
            )
        )
    return out


def try_stats(s: Session, question: str, athletes: list[m.Athlete]):
    """Stats questions are answered from the season rows (computed, never written by a model). Same return
    shape as try_answer, or None when the question is not about stats."""
    from klugscheisser.stats import search

    sq = search.understand(s, question, scope_ids=[a.id for a in athletes])
    if sq is None:
        return None
    try:
        res = search.query(s, **sq.params)
    except ValueError as exc:
        return [], [], [str(exc)], 0, {}, None, "Statistik", [a.id for a in athletes]
    f = res.filters
    understood = ", ".join(
        str(v if not isinstance(v, list) else " / ".join(map(str, v)))
        for k, v in f.items()
        if k in ("personen", "team", "liga", "saison", "nation", "gruppierung", "sortierung")
    )
    note = " · ".join([f"Statistik: {understood}", *sq.annahmen, *res.hinweise])
    ids = list(dict.fromkeys(r["person_id"] for r in res.rows)) or res.person_ids
    gaps = [] if res.rows else ["Keine Statistik in unseren Daten zu dieser Frage."]
    return (
        stats_statements(res.rows),
        [],
        gaps,
        0,
        {},
        note,
        f"{res.total} Statistikzeilen ({' + '.join(dict.fromkeys(r['quelle'] for r in res.rows)) or 'Statistik'})",
        ids,
    )


# --- v12 interface for the prosa writer (Part B, D, F; filled in by the cross part) ---------------------------


@dataclass
class ProsaInputs:
    """What the cross step hands to the prosa writer instead of finished statements.
    `docs`: computed documents first (Doc.computed=True), then the per-person source documents.
    `athlete_ids`: the scope actually used (a recognised game narrows it to that game's players).
    `scope_note`: "Verstanden als …". `since`/`until`: date window (game day ±2 for a recognised game)."""

    docs: list = field(default_factory=list)
    athlete_ids: list[int] = field(default_factory=list)
    scope_note: str | None = None
    since: object | None = None
    until: object | None = None


@dataclass
class AskBack:
    """Part D: the app asks back instead of answering (too unclear, several matching games, too wide)."""

    text: str
    vorschlaege: list[str] = field(default_factory=list)


# --- v12: computed documents (Part B) -----------------------------------------------------------------------

COMPUTED_LABEL = "[Berechnet aus gespeicherten Daten]"
COMPUTED_PREFIX = "🧮 (berechnet)"
MAX_LISTED = 40  # persons listed in one computed document
MAX_SOURCE_DOCS = 40  # per-person source documents after the computed ones
WIDE = 40  # a "wer/who" question over more persons than this without narrowing → ask back
SOURCE_DE = {
    "wikidata": "Wikidata",
    "wikipedia": "Wikipedia",
    "eliteprospects": "Elite Prospects",
    "hockeycanada": "Hockey Canada",
    "sihf": "sihf.ch",
    "roster": "Kaderliste",
    "instagram": "Instagram",
}
TOP_WORD = {
    ("birth_date", "asc"): ("Älteste", "älteste zuerst"),
    ("birth_date", "desc"): ("Jüngste", "jüngste zuerst"),
    ("height_cm", "desc"): ("Grösste", "grösste zuerst"),
    ("height_cm", "asc"): ("Kleinste", "kleinste zuerst"),
    ("instagram_followers", "desc"): ("Meiste Follower", "meiste zuerst"),
    ("instagram_followers", "asc"): ("Wenigste Follower", "wenigste zuerst"),
}
NONE_STORED = {  # "für 2 Spielerinnen ist kein Geburtsdatum gespeichert"
    "birth_date": "kein Geburtsdatum",
    "birth_place": "kein Geburtsort",
    "height_cm": "keine Grösse",
    "jersey_number": "keine Rückennummer",
    "position": "keine Position",
    "current_team": "kein aktuelles Team",
    "instagram_followers": "keine Follower-Zahl",
    "stationen": "keine Station",
    "languages": "keine Sprache",
    "college": "kein College",
    "shoots": "keine Schusshand",
    "draft": "kein Draft",
    "family_in_sport": "keine Angabe zur Familie im Sport",
}
MULTI_KINDS = {"family_in_sport"}  # every stored value counts (brother and mother), not only the best one
SORTABLE = {"birth_date", "height_cm", "instagram_followers", "jersey_number"}
PRONOUN = re.compile(r"\b(sie|ihr|ihre|ihren|ihrem|ihrer|her|she)\b", re.I)
# men's projects (floorball): "er/sein/he/his" too; not in the women's projects, where "er" is the coach
PRONOUN_MEN = re.compile(
    r"\b(sie|ihr|ihre|ihren|ihrem|ihrer|er|ihn|ihm|sein|seine|seinen|seinem|seiner|he|him|his)\b", re.I
)


def _pronoun() -> re.Pattern:
    return PRONOUN_MEN if current_spec().men else PRONOUN


CUT_OFF = re.compile(r"\b(und|mit|with|and|the|der|die|das|von|of|gegen|vs|zu|to|from)\s*\??\s*$", re.I)


@dataclass
class _Val:
    """One stored value of one person, with its source (the citable unit behind a computed document)."""

    aid: int
    name: str
    value: str  # as shown: "24.05.1989 (37 Jahre)"
    key: object  # sort key (None: no comparable value)
    source: str
    url: str
    label: str  # "[Wikidata Q123]"
    quote: str | None = None
    item_id: int | None = None
    vorsicht: bool = False


def _today():
    from klugscheisser.domain.labels import TZ

    return datetime.now(TZ).date()


def _age(born, today) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _roster_source(s: Session, aid: int) -> tuple[str, str] | None:
    """(label, url) of the newest roster entry with a link: where roster birth dates / positions come from."""
    row = s.execute(
        select(m.EventEntry, m.Event)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .where(m.EventEntry.athlete_id == aid)
        .order_by(m.Event.starts_at.desc().nulls_last())
    ).first()
    if row is None:
        return None
    en, ev = row
    url = en.source_url or ev.source_url
    if not url:
        return None
    return f"[Kader {ev.name}{', ' + en.source if en.source else ''}]", url


def fact_values(s: Session, kind: str, ids: list[int]) -> list[_Val]:
    """Stored values of one kind for these persons (one per person; stations: one per station), each with its
    source. Birth date and position fall back to the person record when its roster or Wikidata is the
    source; a value without a source is never used."""
    from klugscheisser.facts.catalog import FACT_KINDS
    from klugscheisser.facts.queries import facts_for, stints_for

    today = _today()
    out: list[_Val] = []
    if kind == "instagram_followers":
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids))):
            for h in a.handles:
                p = h.profile or {}
                if h.platform == "instagram" and h.usable and p.get("followers") is not None:
                    when = (p.get("fetched_at") or "")[:10]
                    out.append(
                        _Val(
                            a.id,
                            a.full_name,
                            f"{p['followers']:,} Follower (@{h.handle})".replace(",", "'"),
                            p["followers"],
                            "instagram",
                            f"https://www.instagram.com/{h.handle}/",
                            f"[Instagram-Profil @{h.handle}, Stand {when}]",
                            f"{p['followers']} Follower",
                        )
                    )
                    break
        return out
    if kind == "stationen":
        for st in stints_for(s, ids):
            league = f", {st.league}" if st.league else ""
            out.append(
                _Val(
                    st.athlete_id,
                    st.athlete_name,
                    f"{st.team_name}{league} ({st.seasons})",
                    None,
                    st.source,
                    st.source_url,
                    _source_label(st.source, st.source_url, getattr(st, "site", None)),
                    st.quote,
                    st.source_item_id,
                )
            )
        return out
    label_de = FACT_KINDS.get(kind, {}).get("de", kind)
    if kind in MULTI_KINDS:
        return _all_values(s, kind, ids)
    for r in facts_for(s, ids, kind):
        v, shown = r.value or {}, r.value_text
        if kind == "draft":
            shown = draft_text(v) or shown
        if kind == "birth_date" and v.get("date"):
            try:
                born = datetime.fromisoformat(v["date"]).date()
                shown = f"{born:%d.%m.%Y} ({_age(born, today)} Jahre)"
            except ValueError:
                pass
        if r.conflicts:
            shown += f" (abweichende Angabe: {r.conflicts[0].value_text})"
        out.append(
            _Val(
                r.athlete_id,
                r.athlete_name,
                shown,
                _value_sort_key(r, kind),
                r.source,
                r.source_url,
                _source_label(r.source, r.source_url, getattr(r, "site", None)),
                r.quote or f"{label_de}: {r.value_text}",
                r.source_item_id,
                r.vorsicht,
            )
        )
    if kind in ("birth_date", "position"):
        have = {v.aid for v in out}
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_([i for i in ids if i not in have]))):
            raw = a.birth_date if kind == "birth_date" else a.position
            if not raw:
                continue
            src = _roster_source(s, a.id)
            if src is None and a.wikidata_qid:
                src = (f"[Wikidata {a.wikidata_qid}]", f"https://www.wikidata.org/wiki/{a.wikidata_qid}")
            if src is None:
                continue  # never a value without a source
            if kind == "birth_date":
                shown, key, quote = (
                    f"{raw:%d.%m.%Y} ({_age(raw, today)} Jahre)",
                    raw.isoformat(),
                    f"{raw:%d.%m.%Y}",
                )
            else:
                shown = quote = position_labels_de().get(raw, raw)
                key = None
            out.append(_Val(a.id, a.full_name, shown, key, "roster", src[1], src[0], quote))
        order = {aid: i for i, aid in enumerate(ids)}
        out.sort(key=lambda v: order.get(v.aid, 0))
    return out


def draft_text(v: dict) -> str | None:
    """ "Boston Fleet (PWHL-Draft 2023, 1. Runde, Nr. 3)" from the stored draft value; None without a team."""
    if not v.get("team"):
        return None
    league = f"{v['league']}-Draft" if v.get("league") else "Draft"
    bits = [f"{league} {v['year']}" if v.get("year") else league]
    if v.get("round"):
        bits.append(f"{v['round']}. Runde")
    if v.get("overall"):
        bits.append(f"Nr. {v['overall']}")
    return f"{v['team']} ({', '.join(bits)})"


def _all_values(s: Session, kind: str, ids: list[int]) -> list[_Val]:
    """All stored values of a kind per person (in `ids` order), one per distinct value, structured ones
    (Elite Prospects "Bruder: Dario Stalder") first; each with its own source."""
    from klugscheisser.facts.catalog import SENSITIVE_KINDS
    from klugscheisser.facts.queries import STATUS_RANK

    rows = list(
        s.execute(
            select(m.Fact, m.Athlete.full_name)
            .join(m.Athlete, m.Athlete.id == m.Fact.athlete_id)
            .where(m.Fact.athlete_id.in_(ids), m.Fact.kind == kind, m.Fact.status != "unconfirmed")
        )
    )
    order = {aid: i for i, aid in enumerate(dict.fromkeys(ids))}
    rows.sort(
        key=lambda r: (
            order.get(r[0].athlete_id, 0),
            not (r[0].value or {}).get("person"),
            STATUS_RANK.get(r[0].status, 3),
            r[0].source != "eliteprospects",
            -(r[0].id or 0),
        )
    )
    out, seen = [], set()
    for f, name in rows:
        key = (f.athlete_id, re.sub(r"\W+", " ", (f.value_text or "").lower()).strip())
        if key in seen or not f.source_url:
            continue
        seen.add(key)
        out.append(
            _Val(
                f.athlete_id,
                name,
                f.value_text,
                None,
                f.source,
                f.source_url,
                _source_label(f.source, f.source_url),
                f.quote or f.value_text,
                f.source_item_id,
                kind in SENSITIVE_KINDS,
            )
        )
    return out


def _sorted_values(s: Session, form: Form, kind: str, ids: list[int]) -> list[_Val]:
    vals = fact_values(s, kind, ids)
    if form.sort and kind in SORTABLE and kind == form.kind:
        known = [v for v in vals if v.key not in (None, "9999", float("-inf"))]
        rest = [v for v in vals if v.key in (None, "9999", float("-inf"))]
        known.sort(key=lambda v: v.key, reverse=form.sort == "desc")
        vals = known + rest
    return vals


def _scope_label(note: str | None, n: int) -> str:
    if not note:
        return f"Ausgewählte {wording().players} ({n})"
    note = note.removesuffix(" (aus der vorherigen Frage)")
    return re.sub(r"\s*\(\d+( Personen| Spielerinnen| Spieler)?\)\s*$", "", note)


def _basis(pairs) -> list[dict]:
    out, seen = [], set()
    for quelle, url in pairs:
        if url and url not in seen:
            seen.add(url)
            out.append({"quelle": quelle.strip("[]"), "url": url})
    return out


def _computed_doc(body: str, label: str, names: list[str], basis: list[dict]):
    from klugscheisser.answer.engine import Doc

    return Doc(
        key="",
        text=body,
        meta=f"{COMPUTED_LABEL} · {label} · über: {', '.join(dict.fromkeys(names)) or '-'}",
        url=basis[0]["url"] if basis else "",
        quelle=COMPUTED_LABEL,
        zeile_prefix=COMPUTED_PREFIX,
        computed=True,
        basis=basis,
    )


def _source_doc(body: str, label: str, url: str, who: str, item_id=None, vorsicht=False):
    from klugscheisser.answer.engine import Doc

    return Doc(
        key="",
        text=body,
        meta=f"{label} · Stammdaten · über: {who}",
        url=url,
        quelle=label,
        zeile_prefix="🪪 (Profil)",
        item_id=item_id,
        vorsicht=vorsicht,
    )


def fact_inputs(s: Session, form: Form, kind: str, ids: list[int], label: str) -> tuple[list, list]:
    """One computed document for a fact kind over the scope + one source document per value.
    Returns ([computed doc] or [] when nothing is stored, source docs)."""
    from klugscheisser.facts.catalog import FACT_KINDS

    vals = _sorted_values(s, form, kind, ids)
    if not vals:
        return [], []
    if kind == "injury":
        _with_status(s, vals)
    if kind == "birth_date" and (form.age_min is not None or form.age_max is not None):
        return _age_inputs(s, form, vals, ids, label)
    names = dict(s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all())
    label_de = {"instagram_followers": "Instagram-Follower", "stationen": "Stationen"}.get(
        kind, FACT_KINDS.get(kind, {}).get("de", kind)
    )
    have = {v.aid for v in vals}
    missing = [names[i] for i in ids if i not in have and i in names]
    sources = "/".join(sorted({_src_name(v.source, v.url, v.label) for v in vals}))
    sorted_kind = bool(form.sort and kind == form.kind and kind in SORTABLE)
    top = TOP_WORD.get((kind, form.sort)) if sorted_kind else None
    head = f"{label} nach {label_de}" + (f", {top[1]}" if top else "")
    parts = [f"{head} (gespeicherte Daten: {sources}; Stand {_today():%d.%m.%Y}):"]
    if top:
        best = [v for v in vals if v.key is not None and v.key == vals[0].key]
        if best:
            parts.append(f"{top[0]}: {'; '.join(f'{v.name}, {v.value}' for v in best)}.")
    if kind == "stationen" or kind in MULTI_KINDS:
        per: dict[str, list[str]] = {}
        for v in vals:
            per.setdefault(v.name, []).append(v.value)
        listing = "; ".join(f"{n}: {', '.join(x)}" for n, x in list(per.items())[:MAX_LISTED])
        more = len(per) - MAX_LISTED
    else:
        listing = "; ".join(
            f"{i}. {v.name} {v.value}" if sorted_kind else f"{v.name}: {v.value}"
            for i, v in enumerate(vals[:MAX_LISTED], 1)
        )
        more = len(vals) - MAX_LISTED
    if more > 0:
        listing += f"; … ({more} weitere)"
    parts.append(("Reihenfolge: " if sorted_kind else "Werte: ") + listing + ".")
    parts.append(f"({wording().plural(len(ids))}, {len(missing)} ohne {label_de})")
    if missing:
        n = len(missing)
        who = ", ".join(missing[:20]) + (" …" if n > 20 else "")
        parts.append(
            f"Für {wording().plural(n)} ist {NONE_STORED.get(kind, 'kein Wert')} gespeichert: {who}."
        )
    computed = _computed_doc(
        " ".join(parts), label, [v.name for v in vals] + missing, _basis((v.label, v.url) for v in vals)
    )
    computed.vorsicht = any(v.vorsicht for v in vals)  # family / injury: marked Vorsicht like its sources
    if kind in MULTI_KINDS:
        computed.extra["direct"] = _multi_direct(vals, missing, label, label_de, kind)
    docs = []
    for v in vals[:MAX_SOURCE_DOCS]:
        if kind == "draft":
            body = f"{v.name} wurde von {v.value} gedraftet."
        else:
            body = f"{v.name}: {label_de} {v.value}."
        if v.quote and v.quote not in body:
            body += f" Laut Quelle: {v.quote}"
        docs.append(_source_doc(body, v.label, v.url, v.name, v.item_id, v.vorsicht))
    return [computed], docs


def _with_status(s: Session, vals: list[_Val]) -> None:
    """Injury values get the person's current status (gap loop round 2: an injury report has no end):
    "Tumor am Hals (seither: 1 Assist 11.10., im Aufgebot 13.10., 2 Tore 14.10.)"."""
    from klugscheisser.person_status import statuses

    people = list(s.scalars(select(m.Athlete).where(m.Athlete.id.in_({v.aid for v in vals}))))
    states = statuses(s, people)
    for v in vals:
        st = states.get(v.aid)
        if st is None or st.injury is None:
            continue
        if since := st.since_text():
            v.value += f" ({since}; Verletzungsmeldung damit überholt)"
        else:
            v.value += f" (letzte Meldung {st.injury.day:%d.%m.%Y}; seither weder Aufgebot noch Spiel in unseren Daten)"


def _src_name(source: str, url: str | None, site: str | None = None) -> str:
    """Short source name for a 'Quellen: …' line: the site of a web-type source ('EV Zug', 'srf.ch'), never "Web"."""
    return cite_name(source, url, site, SOURCE_DE)


def _short_source(v: _Val) -> str:
    """ "Elite Prospects", "srf.ch": the source as a short table cell."""
    if needs_site(v.source):
        return _src_name(v.source, v.url, v.label)
    return SOURCE_DE.get(v.source, v.label.strip("[]"))


def _multi_direct(vals: list[_Val], missing: list[str], label: str, label_de: str, kind: str) -> dict:
    """Direct answer (no model) for a kind with several values per person ("Geschwister im Eishockey?"): the
    values in one sentence, a table with each value's source, and for whom nothing is stored."""
    per: dict[str, list[str]] = {}
    for v in vals:
        per.setdefault(v.name, []).append(v.value)
    if len(per) <= 3:
        summary = "; ".join(
            f"{n} – {label_de} laut gespeicherten Daten: {', '.join(x)}" for n, x in per.items()
        )
    else:
        summary = f"{len(per)} {wording().players} mit gespeicherten Angaben zu {label_de} ({label})"
    unknown = None
    if missing:
        n = len(missing)
        unknown = (
            f"Für {wording().plural(n)} ist {NONE_STORED.get(kind, 'kein Wert')} gespeichert: "
            + ", ".join(missing[:20])
            + (" …" if n > 20 else "")
        )
    return {
        "summary": summary,
        "rows": [[v.name, v.value, _short_source(v), f"{v.name}: {v.value}"] for v in vals[:MAX_LISTED]],
        "unknown": unknown,
        "spalten": [wording().player, label_de, "Quelle"],
    }


def _age_on(born: str, today) -> int | None:
    try:
        y, mth, d = (int(x) for x in born[:10].split("-"))
    except (TypeError, ValueError):
        return None
    return today.year - y - ((today.month, today.day) < (mth, d))


def _age_inputs(s: Session, form: Form, vals: list, ids: list[int], label: str) -> tuple[list, list]:
    """ "Wie viele waren über 30?": count, the list of who is (with age and birth date), and for whom no birth
    date is stored. The engine answers this straight from the computed document (no model)."""
    today = _today()
    names = dict(s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all())
    have = {v.aid for v in vals if v.key not in (None, "9999")}
    hits = []
    for v in vals:
        age = _age_on(str(v.key or ""), today)
        if age is None:
            continue
        if (form.age_min is None or age >= form.age_min) and (form.age_max is None or age <= form.age_max):
            hits.append((v, age))
    missing = sorted(names[i] for i in ids if i not in have and i in names)
    known = len(have & set(ids))
    summary = f"Ergebnis: {len(hits)} von {known} {players_dat()} mit gespeichertem Geburtsdatum sind {form.age_text}"
    summary += f" (Alter am {today:%d.%m.%Y}; {label})"
    rows = [[v.name, v.value, f"{v.name}: {v.value}"] for v, _age in hits]
    unknown = None
    if missing:
        unknown = (
            f"Für {wording().plural(len(missing))} ist kein Geburtsdatum gespeichert "
            f"(unbekannt): " + ", ".join(missing[:20]) + (" …" if len(missing) > 20 else "")
        )
    sources = "/".join(sorted({_src_name(v.source, v.url, v.label) for v, _ in hits})) or "gespeicherte Daten"
    body = f"{label}, Alter {form.age_text} (Quellen: {sources}; Stand {today:%d.%m.%Y}): {summary}."
    if rows:
        body += " " + "; ".join(r[2] for r in rows[:MAX_LISTED]) + "."
    if unknown:
        body += f" {unknown}."
    computed = _computed_doc(
        body, label, [v.name for v, _ in hits] + missing, _basis((v.label, v.url) for v, _ in hits)
    )
    computed.extra["direct"] = {
        "summary": summary,
        "rows": rows,
        "unknown": unknown,
        "spalten": [wording().player, "Geboren (Alter)"],
    }
    docs = [
        _source_doc(f"{v.name}: Geburtsdatum {v.value}.", v.label, v.url, v.name, v.item_id, v.vorsicht)
        for v, _ in hits[:MAX_SOURCE_DOCS]
    ]
    return [computed], docs


NATION_DE = {
    "SUI": "Schweiz",
    "FIN": "Finnland",
    "SWE": "Schweden",
    "CZE": "Tschechien",
    "GER": "Deutschland",
    "CAN": "Kanada",
    "USA": "USA",
    "JPN": "Japan",
    "AUT": "Österreich",
    "FRA": "Frankreich",
    "SVK": "Slowakei",
    "NOR": "Norwegen",
    "DEN": "Dänemark",
    "ITA": "Italien",
    "HUN": "Ungarn",
    "CHN": "China",
    # 2026-11-24: floorball nations (Unihockey-WM)
    "LAT": "Lettland",
    "EST": "Estland",
    "SLO": "Slowenien",
    "POL": "Polen",
    "SGP": "Singapur",
    "THA": "Thailand",
    "PHI": "Philippinen",
    "KOR": "Südkorea",
    "NED": "Niederlande",
    "GBR": "Grossbritannien",
}


def _without_stations(s: Session, ids: list[int]) -> list[str]:
    """Names of the persons with no stored club/college station: for them "played together" is unknown."""
    known = set(
        s.scalars(
            select(m.CareerStint.athlete_id).where(
                m.CareerStint.athlete_id.in_(ids), m.CareerStint.kind.in_(("club", "college"))
            )
        )
    )
    rows = s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all()
    return sorted(name for aid, name in rows if aid not in known)


def relation_inputs(
    s: Session, groups: list[list[int]], ids: list[int], label: str, team_words: list[str] | None = None
) -> tuple[list, list]:
    """One computed document "who played together with whom" + one source document per station used."""
    pairs = relation_pairs(s, groups, ids, team_words)
    stints = [x for p in pairs for x in (p.a, p.b)]
    sources = (
        "/".join(sorted({_src_name(x.source, x.source_url, getattr(x, "site", None)) for x in stints}))
        or "keine"
    )
    head = (
        f"Gemeinsame Stationen: {label} (gleichzeitig im selben Team laut gespeicherten Stationen, "
        f"Quellen: {sources}; Stand {_today():%d.%m.%Y}):"
    )
    if len(groups) >= 2:
        head += " Paare je aus beiden Teams, nur Clubs/Colleges."
    if pairs:
        # one line per pair of persons: the same club under several names ("EV Zug", "EVZ Women's Team") or
        # from several sources is one acquaintance, not several
        from klugscheisser.facts.teams import team_key

        by_pair: dict[tuple[int, int], list] = {}
        rows: list[list[str]] = []  # [pair, teams, line as in the document] for the direct answer
        for p in pairs:
            by_pair.setdefault((p.a.athlete_id, p.b.athlete_id), []).append(p)
        lines = []
        for ps in by_pair.values():
            teams, seen_keys = [], set()
            for p in ps:
                key = team_key(p.team_name)
                # "Frölunda" / "Frölunda HC": one team, not two
                if not any(key == k or key in k or k in key for k in seen_keys):
                    seen_keys.add(key)
                    teams.append(f"{p.team_name} ({pair_seasons(p)})")
            pair, where = f"{ps[0].a.athlete_name} und {ps[0].b.athlete_name}", ", ".join(teams[:3])
            lines.append(f"{pair}: zusammen bei {where}")
            rows.append([pair, where, lines[-1]])
        listing = "; ".join(lines[:MAX_LISTED])
        if len(lines) > MAX_LISTED:
            listing += f"; … ({len(lines) - MAX_LISTED} weitere)"
        involved = {x.athlete_id for ps in by_pair.values() for x in (ps[0].a, ps[0].b)}
        summary = (
            f"Ergebnis: {len(by_pair)} Paare von {players_dat()} haben gleichzeitig im selben Team gespielt"
        )
        if len(groups) >= 2:
            nat = dict(
                s.execute(select(m.Athlete.id, m.Athlete.nationality).where(m.Athlete.id.in_(involved))).all()
            )
            counts: dict[str, int] = {}
            for aid in involved:
                counts[nat.get(aid) or "?"] = counts.get(nat.get(aid) or "?", 0) + 1
            per = ", ".join(
                f"{NATION_DE.get(k, k)}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
            )
            summary += f"; beteiligt sind {len(involved)} von {len(ids)} {players_dat()} ({per})"
        else:
            summary += f"; beteiligt sind {len(involved)} von {len(ids)} {players_dat()}"
        body = f"{head} {summary}. Paare: {listing}."
        unknown = _without_stations(s, ids)
        unknown_text = None
        if unknown:
            unknown_text = (
                f"Ohne gespeicherte Club-/College-Stationen (für sie unbekannt): {len(unknown)} {wording().players}: "
                + ", ".join(unknown[:30])
                + (" …" if len(unknown) > 30 else "")
            )
            body += f" {unknown_text}."
        rest = len(ids) - len(involved) - len(unknown)
        if rest > 0 and len(groups) >= 2:
            rest_text = (
                f"Die übrigen {rest} haben laut den gespeicherten Stationen nie gleichzeitig mit {fill('{a_player_dat}')} "
                "des anderen Teams im selben Club gespielt"
            )
            body += f" {rest_text}."
            unknown_text = f"{rest_text}. {unknown_text}" if unknown_text else rest_text
        direct = {"summary": summary, "rows": rows, "unknown": unknown_text}
    else:
        none = f"Keine zwei dieser {len(ids)} {wording().players} haben laut den gespeicherten Stationen gleichzeitig im selben Team gespielt"
        body = f"{head} {none}."
        direct = {"summary": none, "rows": [], "unknown": None}
    names = [n for p in pairs for n in (p.a.athlete_name, p.b.athlete_name)]
    if not names:
        names = list(s.scalars(select(m.Athlete.full_name).where(m.Athlete.id.in_(ids))))
    basis = _basis(
        (_source_label(x.source, x.source_url, getattr(x, "site", None)), x.source_url) for x in stints
    )
    computed = _computed_doc(body, label, names, basis)
    computed.extra["direct"] = direct  # v12: the engine answers relation questions from this, no model needed
    docs, seen = [], set()
    for x in stints:
        key = (x.athlete_id, x.team_name, x.source_url)
        if key in seen or not x.source_url:
            continue
        seen.add(key)
        league = f", {x.league}" if x.league else ""
        body = f"{x.athlete_name}: Station {x.team_name}{league} ({x.seasons})."
        if x.quote:
            body += f" Laut Quelle: {x.quote}"
        label_x = _source_label(x.source, x.source_url, getattr(x, "site", None))
        docs.append(_source_doc(body, label_x, x.source_url, x.athlete_name, x.source_item_id))
        if len(docs) >= MAX_SOURCE_DOCS:
            break
    return [computed], docs


# --- v12: scope with chat history (Part B/G) ------------------------------------------------------------------


def _named(s: Session, question: str) -> list[int]:
    from klugscheisser.domain.matcher import AthleteMatcher

    return list(dict.fromkeys(link.athlete_id for link in AthleteMatcher(s).from_text(question)))


def _has_own_scope(s: Session, question: str, form: Form) -> bool:
    """Does the question itself name a group (nation, event, team, "die Nati", "das Team")?"""
    from klugscheisser.answer.engine import EVENT_CUES, GENERIC_TEAM, SWISS_TEAM
    from klugscheisser.answer.games import teams_in

    return bool(
        form.nations
        or any(rx.search(question) for rx, _ in EVENT_CUES)
        or SWISS_TEAM.search(question)
        or GENERIC_TEAM.search(question)
        or teams_in(s, question)
    )


def _previous_winners(s: Session, history: list[dict]) -> list[int]:
    """ "Und ihr Club?" after "Wer ist die Älteste?": the person(s) the previous fact question picked, computed
    again from the same data; else the previous answer's only person."""
    prev = history[-1]
    q = prev.get("frage") or ""
    pform = understand(q)
    if pform.typ == "fakt" and pform.limit and pform.kind in SORTABLE:
        ids, _, _ = _scope(s, q, pform, [], history[:-1], depth=1)
        ids = ids or list(prev.get("person_ids") or [])
        vals = [v for v in _sorted_values(s, pform, pform.kind, ids) if v.key not in (None, "9999")]
        if vals:
            return [v.aid for v in vals[: pform.limit]]
    pids = prev.get("person_ids") or []
    return list(pids) if len(pids) == 1 else []


def _scope(
    s: Session, question: str, form: Form, athletes: list, history: list[dict] | None, depth: int = 0
) -> tuple[list[int], str | None, list[list[int]]]:
    """Persons for a fact/relation question: named persons → a pronoun pointing at the previous answer →
    stations at a named team → the group named in the question → the previous question's scope (follow-up
    "und die jüngste?") → the chat's persons."""
    named = _named(s, question)
    if named:
        return named, None, [named]
    pronoun = depth == 0 and history and _pronoun().search(question) and not form.nations
    if pronoun and (winners := _previous_winners(s, history)):
        who = ", ".join(s.scalars(select(m.Athlete.full_name).where(m.Athlete.id.in_(winners))))
        return winners, f"{who} (aus der vorherigen Frage)", [winners]
    # a pronoun on a person page / in a person chat ("Für welches Team spielt sie?"): that person, even when
    # the question contains a group word like "Team"
    if depth == 0 and athletes and len(athletes) <= 5 and _pronoun().search(question) and not form.nations:
        ids = [a.id for a in athletes]
        return ids, None, [ids]
    words = relation_words(s, question) if form.typ == "beziehung" and not form.nations else []
    if words:
        ids = _players(s, _stints_with(s, words))
        note = f"Stationen bei '{', '.join(words)}'"
        if _has_own_scope(s, question, form):  # "Wer aus dem Nationalteam …": only the group's persons
            group, gnote, _ = persons_for(s, question, form)
            if group:
                ids = [i for i in ids if i in set(group)]
                note = f"{gnote or 'Gruppe'} mit {note}"
        return ids, f"{note} ({len(ids)} Personen)", [ids]
    if _has_own_scope(s, question, form):
        ids, note, groups = persons_for(s, question, form)
        if ids:
            return ids, note, groups
    if history and depth < 3:
        prev = history[-1].get("frage") or ""
        ids, note, groups = _scope(s, prev, understand(prev), [], history[:-1], depth + 1)
        if ids:
            return ids, (f"{note} (aus der vorherigen Frage)" if note and depth == 0 else note), groups
        if pids := list(history[-1].get("person_ids") or []):
            return pids, "Personen der vorherigen Antwort", [pids]
    ids = [a.id for a in athletes]
    return ids, None, [ids] if ids else []


# --- v12: games (Part F) --------------------------------------------------------------------------------------


PAST_Q = re.compile(
    r"\b(getroffen|gewonnen|verloren|gespielt|geschossen|erzielt|gehalten|abgeschnitten|endete|ausgegangen|"
    r"resultat\w*|ergebnis\w*|endstand|torschütz\w*|skorer\w*|scorer\w*|war|waren|wurde|hatte|"
    r"scored|won|lost|played|result\w*|final score|who scored)\b",
    re.I,
)
FUTURE_Q = re.compile(
    r"\b(nächste[nms]?|naechste[nms]?|kommende[nms]?|next|upcoming|wann spielt|will)\b", re.I
)


def asks_past(question: str) -> bool:
    """A question about a game that has happened (scorers, result, how it went), not about a coming one."""
    return bool(PAST_Q.search(question)) and not FUTURE_Q.search(question)


def _game_scope(s: Session, question: str, form: Form, history: list[dict] | None):
    """GameScope for a question about one game (or a follow-up to one), else None."""
    from klugscheisser.answer import games

    ref = games.game_ref(s, question)
    if (
        ref is None
        and history
        and form.typ in ("fakt", "beziehung", "offen_pro_person")
        and not _named(s, question)
        and not _has_own_scope(s, question, form)
    ):
        ref = games.game_ref(s, history[-1].get("frage") or "")
    if ref is None:
        return None
    found = games.find_games(s, ref)
    if asks_past(question) and (played := [g for g in found if g.status in ("final", "live")]):
        found = sorted(played, key=lambda g: g.starts_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    if not found:
        return games.scope_without_game(s, ref)
    sc = games.scope_for_game(s, ref, found[0])
    if len(found) > 1:
        others = "; ".join(games.suggestion_label(g) for g in found[1:4])
        sc.note += f" · {len(found)} passende Spiele, das neueste genommen (weitere: {others})"
    return sc


def _game_inputs(s: Session, form: Form, sc) -> ProsaInputs:
    """Computed facts/relations over the game's players, then the game's own documents (official result,
    match reports), then — for open questions — only players with documents in the window."""
    from klugscheisser.answer import games
    from klugscheisser.answer.engine import item_doc

    label = sc.label or _scope_label(sc.note, len(sc.ids))
    computed, sources = [], []
    if form.typ == "fakt" and sc.ids:
        for kind in [form.kind, *form.extra_kinds]:
            c, d = fact_inputs(s, form, kind, sc.ids, label)
            computed, sources = computed + c, sources + d
    elif form.typ == "beziehung" and sc.ids:
        computed, sources = relation_inputs(s, sc.groups, sc.ids, label)
    docs = computed + [item_doc(i) for i in sc.items]
    if form.typ not in ("fakt", "beziehung") and sc.since is not None and sc.ids:
        _, per = games.window_items(s, sc.sides, sc.ids, sc.since, sc.until)
        seen = {i.id for i in sc.items}
        docs += [item_doc(i) for i in per if i.id not in seen]
    return ProsaInputs(
        docs=_numbered(docs + sources), athlete_ids=sc.ids, scope_note=sc.note, since=sc.since, until=sc.until
    )


def _numbered(docs: list) -> list:
    for i, d in enumerate(docs, 1):
        d.key = f"D{i}"  # unique; the engine renumbers when it adds its own documents
    return docs


# --- gaps 2026-10-05: current league, transfers, next tournament (computed, answered without a model) --------

PLAYS_NOW = re.compile(
    r"\b(spielt|spielen|play|plays|playing|ist in|sind in|is in|are in|aktiv in|active in|unter vertrag)\b",
    re.I,
)
NOT_NOW = re.compile(
    r"\b(gespielt|spielte|spielten|played|früher|ehemalige?n?|former|formerly|zusammen|together|"
    r"punkte|tore|points|goals|assists|scorer)\b",
    re.I,
)
# league words → (label, phrase for the answer, test on the stored league "PWHL (W)" without " (W)")
LEAGUES: list[tuple[re.Pattern, str, str, object]] = [
    (re.compile(r"\bpwhl\b", re.I), "PWHL", "in der PWHL", lambda lg: lg.startswith("PWHL")),
    (re.compile(r"\bsdhl\b", re.I), "SDHL", "in der SDHL", lambda lg: lg.startswith("SDHL")),
    (
        re.compile(
            r"\bncaa\b|college.{0,40}\b(us|usa|u\.s\.?|amerika|america|united states|vereinigten staaten)\b|"
            r"\b(us|usa|amerikanische[nmrs]?|american)[ -]college",
            re.I,
        ),
        "NCAA (USA)",
        "College-Hockey in den USA (NCAA)",
        lambda lg: lg.startswith("NCAA"),
    ),
    (
        re.compile(
            r"\bu ?sports\b|college.{0,40}\b(kanada|canada)\b|\b(kanadische[nmrs]?|canadian)[ -](college|uni)",
            re.I,
        ),
        "U Sports (Kanada)",
        "College-Hockey in Kanada (U Sports)",
        lambda lg: lg.replace(" ", "").startswith("USPORTS"),
    ),
    (
        re.compile(r"\b(college|colleges|universit\w*|hochschul\w*)", re.I),
        "College (NCAA/U Sports)",
        "College-Hockey (NCAA/U Sports)",
        lambda lg: lg.startswith("NCAA") or lg.replace(" ", "").startswith("USPORTS"),
    ),
    (
        re.compile(r"\bswhl\b|\bpfwl\b|women'?s league", re.I),
        "SWHL",
        "in der SWHL",
        lambda lg: lg.startswith("SWHL"),
    ),
    (
        re.compile(r"\bdfel\b|frauen.?bundesliga", re.I),
        "DFEL",
        "in der DFEL",
        lambda lg: lg.startswith("DFEL"),
    ),
    (
        re.compile(r"auroraliiga", re.I),
        "Auroraliiga",
        "in der Auroraliiga",
        lambda lg: lg.startswith("AURORALIIGA"),
    ),
    (re.compile(r"\bewhl\b", re.I), "EWHL", "in der EWHL", lambda lg: lg.startswith("EWHL")),
]
NATI_WORDS = re.compile(
    r"\b(nati|nationalteam|nationalmannschaft|national team|national-team|kader|roster|squad|olympi\w*|wm)\b",
    re.I,
)


def _league(raw: str | None) -> str:
    return re.sub(r"\s*\(w\)\s*$", "", raw or "", flags=re.I).strip()


def _season(y) -> str:
    try:
        y = int(y)
    except (TypeError, ValueError):
        return ""
    return f"{y}/{(y + 1) % 100:02d}"


def league_ref(question: str):
    """ "Welche Schweizerinnen spielen in der PWHL?", "Who plays college hockey in the US?" → (label, phrase,
    test) of the league; None for career ("gespielt", "former") or stats questions."""
    if not PLAYS_NOW.search(question) or NOT_NOW.search(question):
        return None
    for rx, label, phrase, test in LEAGUES:
        if rx.search(question):
            return label, phrase, test
    return None


def _group_scope(
    s: Session, question: str, form: Form, athletes: list, history: list[dict] | None
) -> tuple[list[int], str | None]:
    """Named persons → a nation as such ("Schweizerinnen": all players of that nation in our database; with a
    team word the national team) → a named group → nothing (the caller decides the default)."""
    named = _named(s, question)
    if named:
        return named, None
    if form.nations and not NATI_WORDS.search(question):
        ids = list(
            s.scalars(
                select(m.Athlete.id).where(
                    m.Athlete.nationality.in_(form.nations), m.Athlete.role == "athlete"
                )
            )
        )
        nat = " + ".join(NATION_DE.get(n, n) for n in form.nations)
        return ids, f"{wording().players} aus {nat} in unserer Datenbank ({len(ids)})"
    if _has_own_scope(s, question, form):
        ids, note, _ = persons_for(s, question, form)
        if ids:
            return ids, note
    if history and _pronoun().search(question):
        ids, note, _ = _scope(s, question, form, athletes, history)
        return ids, note
    return [], None


def league_inputs(s: Session, question: str, ref, athletes: list, history: list[dict] | None) -> ProsaInputs:
    """Who plays in a league now: the current team stored per person (Elite Prospects "latest", not the
    career), never a former team. Direct answer: count, [Spielerin, Team (Liga, Saison)], who has no current
    team stored."""
    from klugscheisser.facts.queries import facts_for

    label_lg, phrase, test = ref
    form = understand(question)
    # "College-Hockey in Kanada / in the US": there the country is the place, not the players' nation
    where = {"NCAA (USA)": "USA", "U Sports (Kanada)": "CAN"}.get(label_lg)
    if where in form.nations and len(form.nations) > 1:
        form.nations = [n for n in form.nations if n != where]
    ids, note = _group_scope(s, question, form, athletes, history)
    scoped = bool(ids)
    if not scoped:  # no group named: everyone in our database with a current team
        ids = _players(
            s, s.scalars(select(m.Fact.athlete_id).where(m.Fact.kind == "current_team").distinct())
        )
        note = f"alle {wording().players} mit aktuellem Team in unserer Datenbank ({len(ids)})"
    label = _scope_label(note, len(ids))
    hits, without = [], []
    have = set()
    for r in facts_for(s, ids, "current_team"):
        v = r.value or {}
        if str(v.get("status") or "").lower() in ("retired", "zurückgetreten"):
            continue
        lg = _league(v.get("league"))
        if not lg:
            continue
        have.add(r.athlete_id)
        if test(lg.upper()):
            hits.append((r, v, lg))
    names = dict(s.execute(select(m.Athlete.id, m.Athlete.full_name).where(m.Athlete.id.in_(ids))).all())
    without = sorted(names[i] for i in ids if i not in have and i in names)
    hits.sort(key=lambda h: ((h[1].get("team") or "").lower(), h[0].athlete_name))
    rows, docs = [], []
    for r, v, lg in hits:
        team = v.get("team") or r.value_text
        where = f"{team} ({', '.join(x for x in (lg, _season(v.get('season'))) if x)})"
        rows.append([r.athlete_name, where, f"{r.athlete_name}: {where}"])
        src = _source_label(r.source, r.source_url, getattr(r, "site", None))
        body = f"{r.athlete_name}: Aktuelles Team {where}."
        if r.quote and r.quote not in body:
            body += f" Laut Quelle: {r.quote}"
        docs.append(_source_doc(body, src, r.source_url, r.athlete_name, r.source_item_id))
    n = len(hits)
    sources = (
        "/".join(sorted({_src_name(r.source, r.source_url, getattr(r, "site", None)) for r, _, _ in hits}))
        or "gespeicherte Daten"
    )
    verb = "spielt" if n == 1 else "spielen"
    summary = f"{wording().plural(n)} {verb} aktuell {phrase}"
    summary += f" (Auswahl: {label}; aktuelles Team laut {sources}, Stand {_today():%d.%m.%Y}; frühere Clubs zählen nicht)"
    unknown = None
    if scoped and without:
        unknown = (
            f"Ohne gespeichertes aktuelles Team (für sie unbekannt): {wording().plural(len(without))}: "
            + ", ".join(without[:20])
            + (" …" if len(without) > 20 else "")
        )
    body = f"{label}, aktuell {phrase}: {summary}."
    if rows:
        body += " " + "; ".join(r[2] for r in rows[:MAX_LISTED]) + "."
    if unknown:
        body += f" {unknown}."
    computed = _computed_doc(
        body,
        label,
        [r.athlete_name for r, _, _ in hits] + without,
        _basis(
            (_source_label(r.source, r.source_url, getattr(r, "site", None)), r.source_url)
            for r, _, _ in hits
        ),
    )
    computed.extra["direct"] = {
        "summary": summary,
        "rows": rows,
        "unknown": unknown,
        "spalten": [wording().player, f"Team ({label_lg}, Saison)"],
    }
    return ProsaInputs(
        docs=_numbered([computed, *docs[:MAX_SOURCE_DOCS]]),
        athlete_ids=[r.athlete_id for r, _, _ in hits] or ids,
        scope_note=note,
    )


TRANSFER_Q = re.compile(
    r"\b(club|klub|verein|team|teams|clubs)\s*(ge)?wechsel\w*|\bgewechselt\b|\bwechsel(te|ten|n|t)?\b|"
    r"\btransfers?\b|\bneuzug[aä]ng\w*|\bneue[nmrs]? (club|klub|verein)\b|"
    r"\b(changed|switched|moved|new) (clubs?|teams?)\b|\bsigned (with|for)\b|\bmoved to\b",
    re.I,
)


def transfer_window(question: str, today=None):
    """Time words → (since, until, label): "Sommer 2026" = 01.05.–30.09.2026, "diesen/letzten Sommer",
    "letzte Wochen", "letzte Woche", "letzten 10 Tage", a month ("im August", "Juli 2026"), a year ("2025").
    Nothing said: the last 3 months."""
    from datetime import date

    from klugscheisser.answer.games import MONTHS

    today = today or _today()
    q = question.lower()

    def summer(y: int):
        return date(y, 5, 1), date(y, 9, 30), f"Sommer {y}"

    if mt := re.search(r"\b(sommer|summer)\s+(20\d{2})\b", q):
        return summer(int(mt.group(2)))
    if re.search(r"\b(letzte[nrs]?|vergangene[nrs]?|last|past) (sommer|summer)\b", q):
        return summer(today.year if today.month >= 10 else today.year - 1)
    if re.search(r"\b(sommer|summer)\b", q):
        return summer(today.year if today.month >= 5 else today.year - 1)
    if mt := re.search(r"\b(letzte[nr]?|vergangene[nr]?|last|past)\s+(\d+)\s+(tage|tagen|days)\b", q):
        n = int(mt.group(2))
        return today - timedelta(days=n), today, f"letzte {n} Tage"
    if mt := re.search(r"\b(letzte[nr]?|vergangene[nr]?|last|past)\s+(\d+\s+)?(wochen|weeks)\b", q):
        n = int(mt.group(2) or 4)
        return today - timedelta(weeks=n), today, f"letzte {n} Wochen"
    if re.search(r"\b(letzte[nr]?|vergangene[nr]?|last|past)\s+(woche|week)\b", q):
        return today - timedelta(days=7), today, "letzte Woche"
    if mt := re.search(r"\b(letzte[nr]?|vergangene[nr]?|last|past)\s+(\d+\s+)?(monate|monaten|months)\b", q):
        n = int(mt.group(2) or 3)
        return today - timedelta(days=30 * n), today, f"letzte {n} Monate"
    if re.search(r"\b(kürzlich|zuletzt|neulich|recently|lately)\b", q):
        return today - timedelta(days=30), today, "letzte 30 Tage"
    months = "|".join(sorted(MONTHS, key=len, reverse=True))
    mt = re.search(rf"\b({months})\.?\s+(20\d{{2}})\b", q) or re.search(
        rf"\b(?:im|in|seit|during|since)\s+({months})\b", q
    )
    if mt:
        month = MONTHS[mt.group(1)]
        year = int(mt.group(2)) if mt.lastindex and mt.lastindex >= 2 and mt.group(2) else None
        if year is None:
            year = today.year if month <= today.month else today.year - 1
        start = date(year, month, 1)
        end = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
        return start, min(end, today) if start <= today else end, f"{start:%m.%Y}"
    if mt := re.search(r"\b(20\d{2})\b", q):
        y = int(mt.group(1))
        return date(y, 1, 1), min(date(y, 12, 31), today) if y == today.year else date(y, 12, 31), str(y)
    return today - timedelta(days=90), today, "letzte 3 Monate (kein Zeitraum genannt)"


def _extra_de(extra: str | None) -> str:
    """Elite Prospects' transfer note in German: "3 years" → "3 Jahre", "Traded" → "Trade"."""
    x = (extra or "").strip()
    if mt := re.fullmatch(r"(\d+)\s*years?", x, re.I):
        n = int(mt.group(1))
        return f"{n} Jahr{'e' if n != 1 else ''}"
    return {"traded": "Trade", "college commitment": "College-Zusage", "loan": "Leihe"}.get(x.lower(), x)


def _domain(url: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/", 1)[0]
    return host or "Quelle"


def transfer_inputs(s: Session, question: str, athletes: list, history: list[dict] | None) -> ProsaInputs:
    """Club changes from the stored Elite Prospects transfers (date, from, to, length, the club's own news link)
    of the persons in scope; contract extensions and retirements are no change. Default scope: all players
    with tier 1/2 (the ones followed closely), said in the scope note."""
    from sqlalchemy import func

    form = understand(question)
    ids, note = _group_scope(s, question, form, athletes, history)
    if not ids:
        ids = _players(
            s,
            s.scalars(select(m.Athlete.id).where(func.coalesce(m.Athlete.tier_manual, m.Athlete.tier) <= 2)),
        )
        note = f"{wording().players} mit Tier 1/2 ({len(ids)})"
    since, until, when = transfer_window(question)
    label = _scope_label(note, len(ids))
    people = {a.id: a for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(ids)))}
    profiles = {
        p.athlete_id: p for p in s.scalars(select(m.EpProfile).where(m.EpProfile.athlete_id.in_(ids)))
    }
    found = []
    for aid, p in profiles.items():
        for t in (p.data or {}).get("transfers") or []:
            day = str(t.get("date") or "")[:10]
            if not day or not (since.isoformat() <= day <= until.isoformat()):
                continue
            src, to = t.get("from_team"), t.get("to_team")
            if t.get("type") == "extension" or (src and to and src == to):
                continue  # contract extension: no change
            if not to and str(t.get("to_info") or "").lower() == "retired":
                continue  # retirement: no new club
            if not (t.get("url") or t.get("source")):
                continue  # never a value without a source
            found.append((people[aid], day, t))
    # tier 1 first (Fabi's players), each by date
    found.sort(key=lambda x: ((x[0].tier_manual or x[0].tier or 3) > 1, x[1], x[0].full_name))
    rows, basis, docs = [], [], []
    for a, day, t in found:
        src = t.get("from_team") or t.get("from_info") or "?"
        to = t.get("to_team") or ("Ziel unbekannt" if t.get("to_info") in (None, "unknown") else t["to_info"])
        move, dd = f"{src} → {to}", f"{day[8:10]}.{day[5:7]}.{day[:4]}"
        extra = _extra_de(t.get("extra"))
        line = f"{a.full_name}: {move}, {dd}" + (f" ({extra})" if extra else "")
        rows.append([a.full_name, move, dd, extra or "–", line])
        if t.get("url"):
            basis.append(("[Elite Prospects Transfer]", t["url"]))
        if t.get("source"):
            basis.append((f"[{_domain(t['source'])}]", t["source"]))
        url = t.get("source") or t.get("url")
        body = f"Wechsel {line}."
        docs.append(_source_doc(body, f"[{_domain(url)}]", url, a.full_name))
    n = len(found)
    nat = sum(1 for a, _, _ in found if a.nationality == "SUI")
    summary = f"{n} Wechsel zwischen {since:%d.%m.%Y} und {until:%d.%m.%Y} ({when}; {label})"
    if n and nat:
        summary += f", davon {nat} von {fill('{swiss_players_dat}')}"
    summary += " – laut den gespeicherten Transfers bei Elite Prospects, ohne Vertragsverlängerungen"
    without = sorted(a.full_name for aid, a in people.items() if aid not in profiles)
    unknown = None
    if without:
        unknown = (
            f"Für {wording().plural(len(without))} sind keine Transferdaten gespeichert "
            "(kein Elite-Prospects-Profil): " + ", ".join(without[:20]) + (" …" if len(without) > 20 else "")
        )
    body = f"Transfers {label}, {when}: {summary}."
    if rows:
        body += " " + "; ".join(r[-1] for r in rows[:MAX_LISTED]) + "."
    if unknown:
        body += f" {unknown}."
    computed = _computed_doc(body, label, [a.full_name for a, _, _ in found], _basis(basis))
    computed.extra["direct"] = {
        "summary": summary,
        "rows": rows,
        "unknown": unknown,
        "spalten": [wording().player, "Wechsel (von → nach)", "Datum", "Zusatz"],
    }
    from klugscheisser.domain.labels import TZ as tz

    return ProsaInputs(
        docs=_numbered([computed, *docs[:MAX_SOURCE_DOCS]]),
        athlete_ids=list(dict.fromkeys(a.id for a, _, _ in found)) or ids,
        scope_note=f"{note} · Transfers {when}",
        since=datetime.combine(since, datetime.min.time(), tz),
        until=datetime.combine(until, datetime.max.time(), tz),
    )


TOURNAMENT_ASK = re.compile(
    r"\b(wann|when|wo|where|datum|date|dates|termin|kader|aufgebot|roster|squad|lineup|nominiert\w*|"
    r"nominated|selected|dabei|wer (ist|sind|spielt|spielen|fährt|fahren|reist)|who (is|are|plays|goes))\b",
    re.I,
)


def tournament_ref(question: str):
    """ "Wann ist die nächste WM und wer ist im Kader?" → (TOURNAMENTS entry, "next" | "last")."""
    from klugscheisser.answer.games import LATEST, NEXT, tournaments

    if not TOURNAMENT_ASK.search(question):
        return None
    t = next((t for t in tournaments() if t[0].search(question)), None)
    if t is None:
        return None
    if NEXT.search(question):
        return t, "next"
    if LATEST.search(question):
        return t, "last"
    if re.search(r"\b(wann|when|wo|where)\b", question, re.I):
        return t, "next"
    return None


def _tournament_event(s: Session, prefix: str, which: str) -> m.Event | None:
    now = datetime.now(UTC)
    events = [
        e
        for e in s.scalars(select(m.Event).where(m.Event.key.startswith(prefix), m.Event.kind != "game"))
        if e.starts_at
    ]
    if which == "next":  # not yet over (a running tournament counts), nearest first
        coming = sorted((e for e in events if (e.ends_at or e.starts_at) >= now), key=lambda e: e.starts_at)
        return coming[0] if coming else None
    started = sorted((e for e in events if e.starts_at <= now), key=lambda e: e.starts_at, reverse=True)
    return started[0] if started else None


def tournament_inputs(s: Session, question: str, ref) -> ProsaInputs:
    """The next (or last) tournament of a kind: date and place in the summary, the stored roster of the named
    nation (default Switzerland) as rows, players only (no staff)."""
    from klugscheisser.answer.games import _local_day

    (_rx, _comp, prefix, word), which = ref
    word_de = {"next": "Nächste", "last": "Letzte"}[which]
    ev = _tournament_event(s, prefix, which)
    form = understand(question)
    nation = (form.nations or ["SUI"])[0]
    team = s.scalar(select(m.Team).where(m.Team.key == national_team_key(nation)))
    if ev is None:
        summary = f"{word_de} {word}: kein solches Turnier in den gespeicherten Daten"
        computed = _computed_doc(f"{summary}.", f"{word_de} {word}", [], [])
        computed.extra["direct"] = {"summary": summary, "rows": [], "unknown": None}
        return ProsaInputs(docs=_numbered([computed]), athlete_ids=[], scope_note=f"{word_de} {word}")
    start, end = _local_day(ev.starts_at), _local_day(ev.ends_at)
    if start and end and end != start:
        dates = (
            f"{start:%d.%m.}–{end:%d.%m.%Y}" if start.year == end.year else f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"
        )
    else:
        dates = f"{start:%d.%m.%Y}" if start else "Datum unbekannt"
    place = ev.city or ev.venue or ""
    entries = []
    if team is not None:
        entries = [
            en
            for en in s.scalars(
                select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == team.id)
            )
            if en.role in (None, "G", "D", "F")
        ]
    keep = set(_players(s, [en.athlete_id for en in entries]))
    entries = [en for en in entries if en.athlete_id in keep]
    pos_order = {"G": 0, "D": 1, "F": 2}
    entries.sort(key=lambda en: (pos_order.get(en.role or "", 3), en.athlete.full_name))
    team_name = team.name if team is not None else NATION_DE.get(nation, nation)
    summary = f"{word_de} {word}: {ev.name}, {dates}" + (f" in {place}" if place else "")
    if ev.status == "final":
        summary += " (beendet)"
    notes = {en.source for en in entries if en.source}
    provisional = any(n.lower().startswith("annahme") or "nicht publiziert" in n.lower() for n in notes)
    if entries:
        summary += f"; Kader {team_name}: {len(entries)} {wording().players}" + (
            " (provisorisch)" if provisional else ""
        )
    rows = []
    for en in entries:
        pos = position_labels_de().get(en.role or "", "–")
        nr = f"#{en.jersey_number}" if en.jersey_number else ""
        rows.append(
            [en.athlete.full_name, " ".join(x for x in (pos, nr) if x), f"{en.athlete.full_name} ({pos})"]
        )
    if not entries:
        unknown = f"Für {team_name} ist noch kein Kader für dieses Turnier gespeichert"
    elif provisional:
        unknown = (
            "Provisorischer Kader: der offizielle Kader ist noch nicht publiziert; gespeichert ist das letzte "
            "Aufgebot vor dem Turnier (Annahme)"
        )
    else:
        unknown = None
    basis = [(_source_label("web", ev.source_url or ""), ev.source_url)] if ev.source_url else []
    roster_urls = list(dict.fromkeys(en.source_url for en in entries if en.source_url))
    basis += [(f"[Kader {team_name}, {ev.name}]", u) for u in roster_urls]
    body = f"{summary}."
    if rows:
        body += " Kader: " + "; ".join(r[2] for r in rows) + "."
    if notes:
        body += " Herkunft des Kaders: " + "; ".join(sorted(notes)) + "."
    if unknown:
        body += f" {unknown}."
    computed = _computed_doc(
        body, f"{word_de} {word}", [en.athlete.full_name for en in entries], _basis(basis)
    )
    computed.extra["direct"] = {
        "summary": summary,
        "rows": rows,
        "unknown": unknown,
        "spalten": [wording().player, "Position"],
    }
    docs = []
    if ev.source_url:
        docs.append(
            _source_doc(
                f"{ev.name}: {dates}" + (f", {place}" if place else "") + ".",
                _source_label("web", ev.source_url),
                ev.source_url,
                ev.name,
            )
        )
    for u in roster_urls[:3]:
        names = ", ".join(en.athlete.full_name for en in entries if en.source_url == u)
        docs.append(
            _source_doc(f"Kader {team_name}, {ev.name}: {names}.", f"[Kader {team_name}]", u, team_name)
        )
    return ProsaInputs(
        docs=_numbered([computed, *docs]),
        athlete_ids=[en.athlete_id for en in entries],
        scope_note=f"{ev.name}: Kader {team_name} ({len(entries)})",
    )


def gap_kind(question: str, form: Form | None = None) -> tuple[str, object] | None:
    """("transfer", None) | ("liga", league ref) | ("turnier", tournament ref) | None."""
    form = form or understand(question)
    open_q = form.typ in ("offen", "offen_pro_person")
    transfer_ok = form.typ != "beziehung" and (open_q or form.kind in ("current_team", "stationen"))
    if transfer_ok and TRANSFER_Q.search(question):
        return "transfer", None
    league_ok = open_q or form.kind in ("college", "current_team")
    if league_ok and (lr := league_ref(question)) is not None:
        return "liga", lr
    if (cc := _club_country_ref(question)) is not None:
        return "land", cc  # 2026-11-24: "Welche Deutschen spielen in der Schweiz?" (clubs of a country)
    if open_q and (tr := tournament_ref(question)) is not None:
        return "turnier", tr
    return None


def _club_country_ref(question: str):
    from klugscheisser.answer import club_country

    if league_ref(question) is not None:
        return None  # a league named ("in der SDHL"): the league answer
    return club_country.parse(question)


def _gap_inputs(
    s: Session, question: str, form: Form, athletes: list, history: list[dict] | None
) -> ProsaInputs | None:
    """Transfers, current league, next tournament: one computed document each (with a direct answer)."""
    gk = gap_kind(question, form)
    if gk is None:
        return None
    kind, ref = gk
    if kind == "transfer":
        return transfer_inputs(s, question, athletes, history)
    if kind == "liga":
        return league_inputs(s, question, ref, athletes, history)
    if kind == "land":
        from klugscheisser.answer import club_country

        return club_country.inputs(s, question, ref, athletes, history)
    return tournament_inputs(s, question, ref)


def prosa_inputs(
    s: Session, question: str, athletes: list, history: list[dict] | None = None
) -> ProsaInputs | None:
    """Fact/relation/game questions → computed documents for the writer (also with chat history). None: not
    a question the cross step understands; the engine searches documents as usual.

    - game recognised (or a follow-up to one): scope = that game's players, window = game day ±2, the game's
      documents first; a game not stored → the named tournament / latest meeting, said in `scope_note`
    - fact question ("die Älteste", "how old", "and her club?"): one computed document per fact kind + one
      source document per person
    - relation question ("wer hat mit wem zusammengespielt"): one computed document of pairs + the stations
    """
    from klugscheisser.answer.lookups import direct_lookup

    if not _named(s, question) and (cc := _club_country_ref(question)) is not None:
        from klugscheisser.answer import (
            club_country,
        )  # before games/H2H: "Deutsche in der Schweiz" is no game

        return club_country.inputs(s, question, cc, athletes, history)
    if (
        hit := direct_lookup(s, question, athletes)
    ) is not None:  # v13: coach, form, next/last game of a team
        return hit
    form = understand(question)
    sc = _game_scope(s, question, form, history)
    if sc is not None:
        return _game_inputs(s, form, sc)
    if (gap := _gap_inputs(s, question, form, athletes, history)) is not None:
        return gap  # transfers, current league, next tournament
    from klugscheisser.answer import club_country

    where = (
        form.typ not in ("fakt", "beziehung")
        and club_country.WHERE_PLAYS.search(question)
        and _named(s, question)
    )
    if form.typ not in ("fakt", "beziehung") and not where:
        return None
    ids, note, groups = _scope(s, question, form, athletes, history)
    if not ids:
        return None
    label = _scope_label(note, len(ids))
    asks_club = where or (form.typ == "fakt" and form.kind == "current_team" and not form.extra_kinds)
    # 2026-11-24: "Wo spielt Jan Bürki?": the newest dated club (roster, league row), not an old article
    if (
        asks_club
        and len(ids) <= MAX_LISTED
        and (pc := club_country.person_club_inputs(s, ids, _scope_label(note, len(ids)))) is not None
    ):
        pc.scope_note = note
        return pc
    if form.typ == "fakt":
        computed, sources = [], []
        for kind in [form.kind, *form.extra_kinds]:
            c, d = fact_inputs(s, form, kind, ids, label)
            computed, sources = computed + c, sources + d
        if not computed:
            return None  # nothing stored: the engine reads the documents (e.g. the Wikipedia text)
    else:
        words = relation_words(s, question) if not form.nations else []
        computed, sources = relation_inputs(s, groups, ids, label, words or None)
    return ProsaInputs(docs=_numbered(computed + sources), athlete_ids=ids, scope_note=note)


# --- v12: ask back (Part D, no model) -------------------------------------------------------------------------


def _latest_games(
    s: Session, nations: list[str] | None = None, limit: int = 1, clubs: bool = True
) -> list[m.Event]:
    """Newest stored final games for a suggestion: between the named nations, else of the Swiss national
    team, else of Swiss clubs. Nothing when none of these is stored (no foreign club games as a tip)."""
    q = (
        select(m.Event)
        .where(m.Event.kind == "game", m.Event.status == "final")
        .order_by(m.Event.starts_at.desc().nulls_last())
    )
    tries = []
    if nations:  # the named nations only: a club game is no narrowing of a question about two nations
        clubs = False
        keys = [national_team_key(n) for n in nations]
        ids = select(m.Team.id).where(m.Team.key.in_(keys))
        if len(keys) >= 2:
            tries.append(q.where(m.Event.home_team_id.in_(ids), m.Event.away_team_id.in_(ids)))
        else:
            tries.append(q.where(m.Event.home_team_id.in_(ids) | m.Event.away_team_id.in_(ids)))
    sui = select(m.Team.id).where(m.Team.key == home_team_key())
    tries.append(q.where(m.Event.home_team_id.in_(sui) | m.Event.away_team_id.in_(sui)))
    swiss_clubs = select(m.Team.id).where(m.Team.country == "SUI", m.Team.kind != "national")
    if clubs:
        tries.append(q.where(m.Event.home_team_id.in_(swiss_clubs) | m.Event.away_team_id.in_(swiss_clubs)))
    for tq in tries:
        if found := list(s.scalars(tq.limit(limit))):
            return found
    return []


def ask_back(s: Session, question: str, athletes: list, history: list[dict] | None = None) -> AskBack | None:
    """Part D/F: a question the app should ask back about (no model call). None: answer normally.

    - a game that matches several stored games → "Welches Spiel meinst du?" + the games
    - a "wer/who" question over more than WIDE persons with no narrowing (game, date, topic)
    - no clear subject / cut off ("who was a teammate with whom") with nothing in the chat to go on
    Never for named persons, stats questions or a question the current flow answers."""
    from klugscheisser.answer import games
    from klugscheisser.answer.engine import question_topics
    from klugscheisser.stats import search as stats

    if stats.understand(s, question, scope_ids=[a.id for a in athletes]) is not None:
        return None
    if _club_country_ref(question) is not None:
        return None  # nation X at clubs of country Y: answered from the stored clubs
    base = question.strip().rstrip("?").strip()
    if not athletes:
        from klugscheisser.answer import team_games

        if team_games.for_question(s, question):
            return None  # recent games / head-to-head of teams: answered from the stored games
    from klugscheisser.tournament import TOPICS

    if TOPICS["bilanz"].search(question):
        return None  # 2026-11-24: "Bilanz Schweiz gegen Schweden": all meetings, never "Welches Spiel meinst du?"
    ref = games.game_ref(s, question)
    if ref is not None:
        found = games.find_games(s, ref)
        if asks_past(question):
            # 24.10. (chat-eval E2): "Wer hat … getroffen?" offered the unplayed Södertälje game; past questions
            # only mean played games, and with several the latest is answered (the others named in the note)
            played = [g for g in found if g.status in ("final", "live")]
            if played:
                return None
        if len(found) > 1:
            return AskBack(
                "Welches Spiel meinst du?", [f"{base} – Spiel {games.suggestion_label(g)}" for g in found[:3]]
            )
        return None
    if _named(s, question):
        return None
    form = understand(question)
    if form.typ == "beziehung" and len(form.nations) >= 2:
        return None  # "Welche Schwedinnen haben mit Schweizerinnen …": two nations are narrow enough
    if gap_kind(question, form) is not None:
        return None  # transfers / current league / next tournament: answered from data
    own = _has_own_scope(s, question, form) or bool(form.typ == "beziehung" and relation_words(s, question))
    if not own and not athletes and not history:
        latest = _latest_games(s)
        if form.typ == "beziehung":
            tips = ["Wer aus der Nati hat schon mit wem im selben Club gespielt?"]
            tips += [f"Wer im Spiel {games.suggestion_label(g)} hat schon zusammen gespielt?" for g in latest]
            tips.append(f"Welche {wording().players} aus der Schweiz und Schweden haben zusammen gespielt?")
            return AskBack(
                fill("Wessen {teammates} meinst du? Nenne ein Team, ein Spiel oder {a_player_acc}."), tips[:3]
            )
        short = (
            form.typ == "offen_pro_person" and len(question.split()) <= 3 and not question_topics(question)
        )
        if short or (form.typ != "fakt" and CUT_OFF.search(question)):
            tips = [f"{base} – in der Nati?"]
            tips += [f"{base} – im Spiel {games.suggestion_label(g)}?" for g in latest]
            return AskBack("Die Frage scheint unvollständig. Um wen oder welches Spiel geht es?", tips)
    if form.typ == "offen_pro_person" and not form.days and not question_topics(question):
        ids, note, _ = _scope(s, question, form, athletes, history)
        if len(ids) > WIDE:
            tips = [f"{base} – im Spiel {games.suggestion_label(g)}?" for g in _latest_games(s, form.nations)]
            if len(form.nations) >= 2:
                for ioc in form.nations[:2]:
                    if team := s.scalar(select(m.Team).where(m.Team.key == national_team_key(ioc))):
                        tips.append(f"{base} – nur {team.name}?")
            else:
                tips.append(f"{base} – nur die Nati?")
            tips.append(f"{base} – in den letzten 14 Tagen?")
            scope = f" ({_scope_label(note, len(ids))})" if note else ""
            return AskBack(f"Das wären {len(ids)} {wording().players}{scope}. Wen meinst du genau?", tips[:3])
    return None
