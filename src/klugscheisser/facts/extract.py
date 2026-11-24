"""Facts from documents: one model call per person (v17: Claude Sonnet, or Gemini Flash-Lite with
LLM_PROVIDER=gemini) over her profile-like documents, every fact with document id and verbatim quote, then
checked like answers (quote verbatim in the document, Jev: does the quote, read in its context, support the
fact?).

- facts whose quote is not in the document are dropped;
- facts Jev does not support (below `answer_support_threshold`) are stored with status "unconfirmed";
- different values for single-valued kinds (birth date, height, position) are kept side by side with status
  "conflict", unless one of them is the Wikidata value (that one stays "confirmed").
A CacheEntry ("facts", "item:{id}:a{athlete}") marks every document as done for that person, so reruns do not
pay again.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.answer.engine import Doc, GeminiAnswerEngine, quote_in
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import source_label
from klugscheisser.domain.names import normalize
from klugscheisser.facts.catalog import FACT_KINDS
from klugscheisser.facts.teams import TeamResolver
from klugscheisser.ingest import costs
from klugscheisser.llm import text as llm_text
from klugscheisser.llm.text import DailyQuotaExceeded  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

MAX_DOCS = 8
DOC_CHARS = 3000
MAX_QUOTE_WORDS = 60  # the prompt asks for 3–40; longer is not a quote any more
CACHE_SOURCE = "facts"
COST_SOURCE = "llm:gemini-facts"  # with Gemini; cost_source(settings) gives the current one
SINGLE_VALUED = ("birth_date", "height_cm", "position")
PROFILE_PLATFORMS = ("wikipedia",)
SKIP_SOURCE_TYPES = ("athlete_post", "official_result")
SKIP_PLATFORMS = ("instagram", "x")
STINT_KINDS = ("club", "college", "national", "youth")
ROLES = ("player", "captain", "assistant", "coach", "staff")


SYSTEM = (
    "Du liest Dokumente über EINE Person ({person} oder Trainerin) und schreibst Fakten über sie heraus.\n"
    "Regeln:\n"
    "1. Jeder Fakt stützt sich auf GENAU EIN Dokument (ID wie D2) und enthält ein wörtliches Zitat daraus: "
    "exakt kopiert, 3 bis 40 Wörter, keine Auslassungen, keine Übersetzung, keine Umformulierung.\n"
    "2. Nur Fakten über die genannte Person. Andere Personen im Text (Mitspielerinnen, Geschwister, "
    "Namensvetter, Männerteams) weglassen.\n"
    "3. Nur was im Zitat steht. Nichts ableiten oder schätzen (kein Alter → Geburtsjahr, kein 'ehemalig' → "
    "'aktuell').\n"
    "4. 'fakten': art aus der Liste; wert = kurzer deutscher Wert (z. B. '{forward}', '168 cm', "
    "'Wirtschaft an der Universität Zürich'); datum als JJJJ-MM-TT, JJJJ-MM oder JJJJ, sonst leer; "
    "zahl nur bei Zahlen (Grösse in cm, Rückennummer), sonst leer. Leere Felder als ''.\n"
    "5. 'stationen': Vereine, College-Teams, Nationalteams, Juniorenteams mit Saisons. von/bis als Saison "
    "('2019/20') oder Jahr; bis leer, wenn offen oder unbekannt. rolle: player, captain, assistant, coach "
    "oder staff. art: club, college, national oder youth. Das Zitat muss Team UND Jahre/Saisons enthalten; "
    "von/bis nur angeben, wenn sie im Zitat stehen, sonst leer lassen.\n"
    "6. Verletzungen und Familie nur, wenn ausdrücklich im Dokument genannt.\n"
    "7. Lieber weniger Fakten als unbelegte."
)
# 2026-11-24 (floorball project, men): the person is a man
MEN_WORDS = {
    "oder Trainerin) und schreibst Fakten über sie heraus": "oder Trainer) und schreibst Fakten über ihn heraus",
    "(Mitspielerinnen, Geschwister, Namensvetter, Männerteams)": "(Mitspieler, Geschwister, Namensvetter, "
    "Frauenteams)",
}


def system() -> str:
    """SYSTEM in the current project's words (ice hockey: "Eishockeyspielerin oder Trainerin", "Stürmerin")."""
    from klugscheisser.llm.words import position_labels_de
    from klugscheisser.projects import wording

    text = SYSTEM
    if _men():
        for women, men in MEN_WORDS.items():
            text = text.replace(women, men)
    return text.replace("{person}", wording().person).replace(
        "{forward}", position_labels_de().get("F", "Stürmerin")
    )


def _men() -> bool:
    from klugscheisser.sports import current_spec

    return current_spec().men


_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "fakten": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "art": {"type": "string", "enum": list(FACT_KINDS)},
                    "wert": _STR,
                    "datum": _STR,
                    "zahl": _STR,
                    "team": _STR,
                    "ereignis": _STR,
                    "zusatz": _STR,
                    "dok": _STR,
                    "zitat": _STR,
                },
                "required": ["art", "wert", "datum", "zahl", "dok", "zitat"],
            },
        },
        "stationen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "team": _STR,
                    "liga": _STR,
                    "von": _STR,
                    "bis": _STR,
                    "rolle": {"type": "string", "enum": list(ROLES)},
                    "art": {"type": "string", "enum": list(STINT_KINDS)},
                    "dok": _STR,
                    "zitat": _STR,
                },
                "required": ["team", "liga", "von", "bis", "rolle", "art", "dok", "zitat"],
            },
        },
    },
    "required": ["fakten", "stationen"],
}

_SEASON = re.compile(r"((?:19|20)\d{2})(?:\s*[/–\-]\s*(\d{2,4}))?")
_DATE = re.compile(r"^((?:19|20)\d{2})(?:-(\d{2}))?(?:-(\d{2}))?$")


@dataclass
class ExtractStats:
    athlete_id: int
    docs: int = 0
    facts: int = 0
    stints: int = 0
    dropped: int = 0  # quote not verbatim, unknown document, unknown kind
    unconfirmed: int = 0
    conflicts: int = 0
    cost_usd: float = 0.0
    dropped_examples: list[str] = field(default_factory=list)
    error: str | None = None


def parse_season(text: str | None) -> int | None:
    """'2019/20' / '2019-20' / '2019–2020' / '2019' → 2019 (start year of the season)."""
    if not text:
        return None
    match = _SEASON.search(text)
    return int(match.group(1)) if match else None


def _marker(item_id: int, athlete_id: int) -> str:
    return f"item:{item_id}:a{athlete_id}"


def _mentions(a: m.Athlete, text: str) -> bool:
    """The document names her (last name or an alias). Guards against pages about someone else."""
    norm = f" {normalize(text)} "
    names = {normalize(a.last_name or a.full_name.split()[-1]), normalize(a.full_name)}
    names |= {al.alias_normalized for al in a.aliases}
    return any(n and f" {n} " in norm for n in names)


def select_documents(
    session: Session, a: m.Athlete, items: list[m.Item] | None = None, limit: int = MAX_DOCS
) -> list[m.Item]:
    """Her profile-like documents not yet extracted: Wikipedia first, then college/federation/club profiles,
    web pages, then news where she is the main person; newest first within a group."""
    if items is None:
        q = (
            select(m.Item)
            .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
            .where(
                m.ItemAthlete.athlete_id == a.id,
                m.Item.level == "full",
                m.Item.text.is_not(None),
                m.Item.text_purged_at.is_(None),
                m.Item.is_deleted_at_source.is_(False),
                m.Item.platform.not_in(SKIP_PLATFORMS),
                m.Item.source_type.not_in(SKIP_SOURCE_TYPES),
            )
        )
        items = list(session.scalars(q))
    done = set(
        session.scalars(
            select(m.CacheEntry.target).where(
                m.CacheEntry.source == CACHE_SOURCE,
                m.CacheEntry.target.in_([_marker(i.id, a.id) for i in items]),
            )
        )
    )
    picked = []
    for item in items:
        if _marker(item.id, a.id) in done or not (item.text or "").strip():
            continue
        if item.platform in SKIP_PLATFORMS or item.source_type in SKIP_SOURCE_TYPES:
            continue
        if item.source_type not in ("profile", "web"):  # news: only where she is the main person
            links = [ia for ia in item.athletes if ia.athlete_id == a.id]
            main = links[0].main if links else None
            if main is False or (main is None and len(item.athletes) > 1):
                continue
        picked.append(item)

    def rank(item: m.Item) -> tuple:
        group = (
            0
            if item.platform in PROFILE_PLATFORMS
            else 1
            if item.source_type == "profile"
            else 2
            if item.source_type == "web"
            else 3
        )
        when = item.published_at or item.scraped_at or datetime(1970, 1, 1, tzinfo=UTC)
        return (group, -when.timestamp())

    return sorted(picked, key=rank)[:limit]


def _doc(item: m.Item, a: m.Athlete) -> Doc:
    label = source_label(item.platform, item.source_name, item.published_at)
    text = " ".join(filter(None, [item.title, item.text]))[:DOC_CHARS]
    return Doc(
        key="",
        text=text,
        meta=f"{label} · über: {a.full_name}",
        url=item.url,
        quelle=label,
        zeile_prefix="",
        item_id=item.id,
    )


def _person_line(a: m.Athlete) -> str:
    bits = [a.full_name]
    if a.birth_date:
        bits.append(f"geb. {a.birth_date:%d.%m.%Y}")
    if a.nationality:
        bits.append(a.nationality)
    bits.append({"coach": "Trainer" if _men() else "Trainerin", "staff": "Staff"}.get(a.role, _person()))
    return ", ".join(bits)


# --- model call -----------------------------------------------------------------------------------------


def model_llm(settings: Settings):
    """callable(system, prompt, schema) -> (dict, usage) with the text model (llm/text.py, role "text": Claude
    Sonnet or Gemini Flash-Lite); Gemini per-minute 429s are waited out, a daily quota raises
    DailyQuotaExceeded."""

    def call(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
        return llm_text.generate(
            settings, system, prompt, schema=schema, role="text", temperature=0.1, wait_on_rate_limit=True
        )

    return call


gemini_llm = model_llm  # old name


def cost_source(settings: Settings) -> str:
    """ "llm:claude-facts" or "llm:gemini-facts" (COST_SOURCE)."""
    return llm_text.cost_source(settings, "facts")


# --- values ---------------------------------------------------------------------------------------------


def _number(text: str | None) -> float | int | None:
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    x = float(match.group(0).replace(",", "."))
    return int(x) if x.is_integer() else x


def _date_value(text: str | None) -> dict:
    match = _DATE.match((text or "").strip())
    if not match:
        return {}
    y, mo, d = match.groups()
    if d:
        return {"date": f"{y}-{mo}-{d}"}
    if mo:
        return {"date": f"{y}-{mo}", "precision": "month"}
    return {"year": int(y)}


def _value(kind: str, f: dict, sport_key: str | None) -> dict:
    keys = FACT_KINDS[kind]["value"]
    wert = str(f.get("wert", "")).strip()
    value: dict = {"text": wert}
    if "date" in keys:
        value.update(_date_value(f.get("datum")))
    if "number" in keys:
        n = _number(f.get("zahl")) if f.get("zahl") else None
        if n is None and kind in ("height_cm", "jersey_number"):
            n = _number(wert)
        if kind == "height_cm" and isinstance(n, float) and n < 3:
            n = round(n * 100)  # "1,68 m"
        value["number"] = n
    if "year" in keys:
        value["year"] = _date_value(f.get("datum")).get("year") or parse_season(f.get("datum"))
    if "years" in keys or "season" in keys:
        value["years" if "years" in keys else "season"] = str(f.get("datum") or "").strip()
    for src, dst in (("team", "team"), ("ereignis", "event"), ("zusatz", "extra")):
        if f.get(src):
            value[dst] = str(f[src]).strip()
    if kind == "position" and sport_key:
        from klugscheisser.sports import get_sport

        with contextlib.suppress(KeyError):
            value["code"] = get_sport(sport_key).position_code(wert)
    return {k: v for k, v in value.items() if v not in (None, "")}


def compare_key(kind: str, value: dict) -> str | None:
    """Comparable value of single-valued kinds (None: not comparable)."""
    if kind == "birth_date":
        return value.get("date") or (str(value["year"]) if value.get("year") else None)
    if kind == "height_cm":
        n = value.get("number")
        return str(round(float(n))) if n not in (None, "") else None
    if kind == "position":
        return value.get("code") or normalize(value.get("text") or "") or None
    return None


def _same_birth(a: str, b: str) -> bool:
    """'1994' and '1994-05-15' do not conflict (one is less precise)."""
    return a.startswith(b) or b.startswith(a)


def resolve_conflicts(session: Session, athlete_id: int, kind: str) -> int:
    """Mark differing values of a single-valued kind. Returns the number of facts in conflict."""
    facts = list(
        session.scalars(
            select(m.Fact).where(
                m.Fact.athlete_id == athlete_id,
                m.Fact.kind == kind,
                m.Fact.status.in_(("confirmed", "conflict")),
            )
        )
    )
    keyed = [(f, compare_key(kind, f.value or {})) for f in facts]
    keyed = [(f, k) for f, k in keyed if k]
    keys: list[str] = []
    for _, k in keyed:
        if not any(k == x or (kind == "birth_date" and _same_birth(k, x)) for x in keys):
            keys.append(k)
    if len(keys) <= 1:
        for f, _ in keyed:
            f.status = "confirmed"
        return 0
    # reference values: Elite Prospects for hockey kinds (v11) when it has one, otherwise Wikidata
    from klugscheisser.facts.queries import EP_FIRST_KINDS

    ref_source = "wikidata"
    if kind in EP_FIRST_KINDS and any(f.source == "eliteprospects" for f, _ in keyed):
        ref_source = "eliteprospects"
    elif kind == "height_cm" and any(f.source == "hockeycanada" for f, _ in keyed):
        ref_source = "hockeycanada"  # 2026-10-25: official tournament roster before Wikidata
    wikidata = {k for f, k in keyed if f.source == ref_source}

    def close(k: str, w: str) -> bool:  # feet/inches rounding: heights within 2 cm agree
        return kind == "height_cm" and abs(float(k) - float(w)) <= 2

    n = 0
    for f, k in keyed:
        agrees = any(k == w or close(k, w) or (kind == "birth_date" and _same_birth(k, w)) for w in wikidata)
        f.status = "confirmed" if agrees else "conflict"
        n += f.status == "conflict"
    session.flush()
    return n


# --- extraction -----------------------------------------------------------------------------------------


def _fact_statement(a: m.Athlete, kind: str, wert: str, datum: str) -> str:
    label = FACT_KINDS[kind]["de"]
    return f"{a.full_name} – {label}: {wert}" + (f" ({datum})" if datum and datum not in wert else "")


def _stint_statement(a: m.Athlete, st: dict) -> str:
    when = "–".join(x for x in (st.get("von"), st.get("bis")) if x) or "Zeitraum unbekannt"
    role = {
        "captain": " als Captain",
        "assistant": " als Assistant",
        "coach": " als Trainer" if _men() else " als Trainerin",
    }.get(st.get("rolle"), "")
    liga = f", {st['liga']}" if st.get("liga") else ""
    return f"{a.full_name} spielte {when} für {st['team']}{liga}{role}."


def extract_facts(
    session: Session,
    settings: Settings,
    athlete: m.Athlete,
    items: list[m.Item] | None = None,
    llm=None,
    support=None,
) -> dict:
    """Extract, check and store facts for ONE person from up to MAX_DOCS of her documents."""
    a = athlete
    stats = ExtractStats(athlete_id=a.id)
    chosen = select_documents(session, a, items)
    now = datetime.now(UTC)
    # documents that do not name her are marked done without a model call (and never feed facts)
    docs: list[Doc] = []
    for item in chosen:
        d = _doc(item, a)
        if _mentions(a, d.text):
            docs.append(d)
        else:
            session.merge(m.CacheEntry(source=CACHE_SOURCE, target=_marker(item.id, a.id), fetched_at=now))
    stats.docs = len(docs)
    if not docs:
        session.flush()
        return stats.__dict__
    for i, d in enumerate(docs, 1):
        d.key = f"D{i}"
    listing = "\n\n".join(f"[{d.key}] {d.meta}\n{d.text}" for d in docs)
    kinds = "\n".join(f"- {k}: {v['de']}" for k, v in FACT_KINDS.items())
    prompt = f"Person: {_person_line(a)}\n\nFakt-Arten:\n{kinds}\n\nDokumente:\n{listing}"
    call = llm or model_llm(settings)
    out, usage = call(system(), prompt, SCHEMA)
    stats.cost_usd = _record_cost(session, settings, usage, a)

    by_key = {d.key: d for d in docs}
    candidates: list[tuple[str, dict, Doc, str, str]] = []  # (type, raw, doc, quote, statement)

    def check(raw: dict) -> tuple[Doc, str] | None:
        d = by_key.get(str(raw.get("dok", "")).strip())
        zitat = str(raw.get("zitat", "")).strip()
        if d is None or not quote_in(zitat, d.text) or len(zitat.split()) > MAX_QUOTE_WORDS:
            stats.dropped += 1
            if len(stats.dropped_examples) < 5:
                stats.dropped_examples.append(zitat[:120])
            return None
        return d, zitat

    for f in out.get("fakten", []) or []:
        kind = f.get("art")
        if kind not in FACT_KINDS or not str(f.get("wert", "")).strip():
            stats.dropped += 1
            continue
        if (ok := check(f)) is None:
            continue
        d, zitat = ok
        stmt = _fact_statement(a, kind, str(f["wert"]).strip(), str(f.get("datum") or ""))
        candidates.append(("fact", f, d, zitat, stmt))
    for st in out.get("stationen", []) or []:
        if not str(st.get("team", "")).strip():
            stats.dropped += 1
            continue
        if (ok := check(st)) is None:
            continue
        d, zitat = ok
        candidates.append(("stint", st, d, zitat, _stint_statement(a, st)))

    engine = GeminiAnswerEngine(settings, support_checker=support)
    scores = engine._support_scores([(z, s_, d) for _, _, d, z, s_ in candidates])
    stats.cost_usd += round(getattr(engine, "_jev_tokens", 0) / 1e6 * 0.042, 6)
    if any(sc is None for sc in scores):
        # no support check (Jev off or failed): store nothing and leave the documents for a later run
        stats.error = "support check unavailable"
        session.flush()
        return stats.__dict__
    threshold = settings.answer_support_threshold
    resolver = TeamResolver(session)
    sport_key = a.sport.key if a.sport else None
    touched: set[str] = set()
    seen: set[tuple] = set()
    for (typ, raw, d, zitat, _stmt), score in zip(candidates, scores, strict=True):
        status = "unconfirmed" if score < threshold else "confirmed"
        confidence = round(float(score), 3)
        source = _source_name(session, d)
        if typ == "fact":
            kind = raw["art"]
            value = _value(kind, raw, sport_key)
            key = ("fact", kind, json.dumps(value, sort_keys=True), d.item_id)
            if key in seen:
                continue
            seen.add(key)
            session.add(
                m.Fact(
                    athlete_id=a.id,
                    kind=kind,
                    value=value,
                    value_text=str(raw["wert"]).strip()[:500],
                    season=parse_season(raw.get("datum")) if kind in ("captaincy",) else None,
                    source=source,
                    source_item_id=d.item_id,
                    source_url=d.url,
                    quote=zitat,
                    confidence=confidence,
                    status=status,
                    extractor=llm_text.model(settings, "text") if llm is None else "injected-llm",
                )
            )
            stats.facts += 1
            if kind in SINGLE_VALUED and status != "unconfirmed":
                touched.add(kind)
        else:
            team = str(raw["team"]).strip()[:200]
            kind = raw.get("art") if raw.get("art") in STINT_KINDS else "club"
            key = ("stint", normalize(team), raw.get("von"), raw.get("bis"), d.item_id)
            if key in seen:
                continue
            seen.add(key)
            season_from, season_to = parse_season(raw.get("von")), parse_season(raw.get("bis"))
            if season_from and season_to and season_to < season_from:
                season_to = season_from
            session.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_id=resolver.resolve(team, national=kind == "national", country=a.nationality),
                    team_name=team,
                    kind=kind,
                    league=(raw.get("liga") or None) and str(raw["liga"])[:100],
                    season_from=season_from,
                    season_to=season_to,
                    role=raw.get("rolle") if raw.get("rolle") in ROLES else "player",
                    source=source,
                    source_item_id=d.item_id,
                    source_url=d.url,
                    quote=zitat,
                    confidence=confidence,
                    status=status,
                )
            )
            stats.stints += 1
        stats.unconfirmed += status == "unconfirmed"
    session.flush()
    for kind in touched:
        stats.conflicts += resolve_conflicts(session, a.id, kind)
    for d in docs:
        session.merge(m.CacheEntry(source=CACHE_SOURCE, target=_marker(d.item_id, a.id), fetched_at=now))
    session.flush()
    return stats.__dict__


def _source_name(session: Session, d: Doc) -> str:
    """wikipedia | college | federation | web | news (CareerStint.source vocabulary)."""
    item = session.get(m.Item, d.item_id) if d.item_id else None
    if item is None:
        return "news"
    if item.platform == "wikipedia":
        return "wikipedia"
    if item.source_type == "profile":
        college = ".edu" in item.url or "athletics" in item.url or "sports.com" in item.url
        return "college" if college else "federation"
    if item.source_type == "web":
        return "web"
    return "news"


def _record_cost(session: Session, settings: Settings, usage: dict, a: m.Athlete) -> float:
    cost = llm_text.cost_usd(settings, usage)
    if cost:
        costs.record(session, cost_source(settings), 1, cost, f"athlete:{a.id}")
    return round(cost, 6)


def _person() -> str:
    from klugscheisser.projects import wording

    return wording().person
