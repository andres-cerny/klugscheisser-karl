"""German interviews from stored texts (step 2 of the German-speakers proposal, 2026-10-25).

Fabi asks which players and coaches of OTHER nations she could interview in German. `facts/german.py` shows
"Deutsch: bestätigt | wahrscheinlich – reason" per person; this module writes the text evidence it reads:
`facts` rows of kind `language_evidence`, value `{"language": "de", "label": "bestaetigt" | "wahrscheinlich",
"reason": …, "date": "YYYY-MM-DD", "outlet": …}` with `source_url`, `source_item_id` and the verbatim `quote`.

Ondrej's rule:
- **bestätigt** only with a German interview: her own words in German, said to a German-language outlet (interview
  or statement "gegenüber SRF"), or audio/video in German — with source and date.
- A translated club press release is NOT bestätigt (at most wahrscheinlich, with the reason): quotes on a club or
  federation site, "Medienmitteilung" wording, or the same quote also in another stored document (a copy or a
  translation). Her own German post caption is wahrscheinlich too (not an interview).
- A quote that is clearly translated (e.g. "sagte sie auf Englisch") or not hers is rejected. Never inferred from
  a name or a nationality.

Steps (`run`): roster persons of current/upcoming tournaments (national teams outside SUI/GER/AUT/LIE) → their
German-language items (linked ones, plus unlinked German items naming her in full) → German direct quotes near her
name (free regex prefilter) → one model call per (item, person) decides whose words they are and in what setting
(llm/text.py, ~$0.01 a call) → the code applies the rule above (the model never sets the label itself) → one fact
row per (person, item). A CacheEntry `german_ev` / `v1:item:{id}:a{athlete}` marks every pair as done, so reruns
(CLI `german-evidence`, the nightly step) only pay for new items. Idempotent: the row of a pair is replaced, never
duplicated.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.answer.engine import quote_in
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.facts.german import EVIDENCE_KIND, GERMAN_NATIONS, norm
from klugscheisser.projects import wording

log = logging.getLogger(__name__)

VERSION = "v1"
CACHE_SOURCE = "german_ev"
RULE_EXTRACTOR = "rule:german-evidence"
CONTEXT_CHARS = 300  # around a quote: the name must be in here, and the model reads it
MAX_PASSAGES = 6  # per (item, person) call
COST_KIND = "german"  # cost ledger source llm:<provider>-german
EST_USD_PER_CALL = 0.012  # Sonnet, ~3k input + ~400 output tokens
SKIP_PLATFORMS = ("wikipedia", "wiki", "wikidata")
SKIP_SOURCE_TYPES = ("official_result",)

# distinctive German function words (not shared with English / Swedish / Danish / Dutch spelling)
_GERMAN_WORDS_TEXT = (
    "und der die das ist nicht ich wir uns unser unsere unserem unseren mich mir mein meine meinem meinen "
    "sehr auch ein eine einen einem einer sind war waren haben habe hatte hatten wird werden wurde bei noch "
    "schon aber dass wenn jetzt hier immer mehr zu zum zur mit auf für fuer gegen nach über ueber auch sich "
    "sie es wie was wo können koennen kann muss müssen wollen will freue freuen gut gross grosse grossen "
    "spiel spiele mannschaft"
)
GERMAN_WORDS = frozenset(_GERMAN_WORDS_TEXT.split())
QUOTE_RX = re.compile(r"[«„“\"‹]([^«»„“”\"‹›]{15,1200})[»“”\"›]")
RELEASE_RX = re.compile(
    r"medienmitteilung|pressemitteilung|pressemeldung|teilt(e)? (der|die|das) (club|verein|klub|verband) mit"
    r"|mitteilung (des|der) (clubs?|vereins?|verbands?)|communiqu",
    re.I,
)
TRANSLATED_RX = re.compile(
    r"auf englisch|in english|übersetzt|uebersetzt|aus dem englischen|aus dem schwedischen|aus dem finnischen"
    r"|aus dem tschechischen|via dolmetscher|mit dolmetscher",
    re.I,
)

INTERVIEW_SETTINGS = ("interview", "stellungnahme_medium", "video_audio", "pressekonferenz")


# --- language and quotes ------------------------------------------------------------------------------------


def german_score(text: str | None) -> tuple[int, float]:
    """(words, share of distinctive German function words)."""
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    if not words:
        return 0, 0.0
    return len(words), sum(w in GERMAN_WORDS for w in words) / len(words)


def looks_german(text: str | None, min_words: int = 12, min_share: float = 0.12) -> bool:
    n, share = german_score(text)
    return n >= min_words and share >= min_share


def quote_is_german(q: str) -> bool:
    n, share = german_score(q)
    hits = sum(w in GERMAN_WORDS for w in re.findall(r"[a-zäöüß]+", q.lower()))
    return n >= 5 and (share >= 0.15 or hits >= 3)


@dataclass
class Passage:
    quote: str
    context: str
    spoken: bool = False  # from a video's spoken sentences (media analysis)


def _name_rx(a: m.Athlete) -> re.Pattern:
    """Her last name (or full name) in normalized text; first name alone counts too ("sagt Noemi")."""
    parts = {norm(a.full_name)}
    if a.last_name and len(norm(a.last_name)) >= 3:
        parts.add(norm(a.last_name))
        # double names: "Nyhlén-Persson" → also "persson"
        parts.update(p for p in norm(a.last_name).split() if len(p) >= 4)
    if a.first_name and len(norm(a.first_name)) >= 4:
        parts.add(norm(a.first_name))
    alts = "|".join(re.escape(p) for p in sorted(parts, key=len, reverse=True) if p)
    return re.compile(r"(?<![a-z0-9])(" + alts + r")(?![a-z0-9])")


def item_text(item: m.Item) -> str:
    return " ".join(filter(None, [item.title, item.text]))


def spoken_quotes(item: m.Item) -> list[str]:
    media = (item.raw or {}).get("media") if isinstance(item.raw, dict) else None
    return [q for q in (media or {}).get("spoken_quotes") or [] if isinstance(q, str) and q.strip()]


def passages(item: m.Item, a: m.Athlete) -> list[Passage]:
    """German direct quotes with her name within CONTEXT_CHARS, plus German sentences spoken in a video of a
    post that is linked to her."""
    text = item_text(item)
    rx = _name_rx(a)
    out: list[Passage] = []
    seen: set[str] = set()
    for mt in QUOTE_RX.finditer(text):
        q = mt.group(1).strip()
        if not quote_is_german(q) or norm(q) in seen:
            continue
        ctx = text[max(0, mt.start() - CONTEXT_CHARS) : mt.end() + CONTEXT_CHARS]
        if not rx.search(norm(ctx)):
            continue
        seen.add(norm(q))
        out.append(Passage(q, ctx))
    for q in spoken_quotes(item):
        if quote_is_german(q) and norm(q) not in seen:
            seen.add(norm(q))
            out.append(Passage(q, f"[Im Video gesprochen] {q}", spoken=True))
    return out[:MAX_PASSAGES]


def own_caption(item: m.Item, a: m.Athlete, link: m.ItemAthlete | None) -> bool:
    """Her own post (her account wrote it) with a German caption of some length."""
    if item.source_kind != "self" or item.platform not in ("instagram", "x"):
        return False
    if link is not None and link.match_kind != "author":
        return False
    return looks_german(item.text, min_words=8, min_share=0.15)


# --- persons and items --------------------------------------------------------------------------------------


@dataclass
class Person:
    athlete: m.Athlete
    country: str
    event: str


def roster_persons(
    s: Session, today: date | None = None, event_keys: list[str] | None = None
) -> dict[int, Person]:
    """Players and staff on rosters of current or upcoming tournaments (or the named events) of national
    teams outside SUI/GER/AUT/LIE."""
    today = today or datetime.now(UTC).date()
    q = (
        select(m.EventEntry, m.Event, m.Team)
        .join(m.Event, m.Event.id == m.EventEntry.event_id)
        .join(m.Team, m.Team.id == m.EventEntry.team_id)
        .where(m.Team.kind == "national", m.Event.kind == "tournament")
    )
    if event_keys:
        q = q.where(m.Event.key.in_(event_keys))
    out: dict[int, Person] = {}
    since = datetime.combine(today - timedelta(days=30), datetime.min.time(), tzinfo=UTC)
    for e, ev, team in s.execute(q.order_by(m.Event.starts_at.desc().nulls_last())):
        if (team.country or "") in GERMAN_NATIONS:
            continue
        if not event_keys:
            end = ev.ends_at or ev.starts_at
            if end is not None and end < since:  # undated tournaments count as current
                continue
        out.setdefault(e.athlete_id, Person(e.athlete, team.country or "", ev.name))
    return out


def _cached(s: Session, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    if not pairs:
        return set()
    targets = {f"{VERSION}:item:{i}:a{a}": (i, a) for i, a in pairs}
    done: set[tuple[int, int]] = set()
    keys = list(targets)
    for k in range(0, len(keys), 1000):
        for t in s.scalars(
            select(m.CacheEntry.target).where(
                m.CacheEntry.source == CACHE_SOURCE, m.CacheEntry.target.in_(keys[k : k + 1000])
            )
        ):
            done.add(targets[t])
    return done


def _mark(s: Session, item_id: int, athlete_id: int) -> None:
    target = f"{VERSION}:item:{item_id}:a{athlete_id}"
    row = s.get(m.CacheEntry, (CACHE_SOURCE, target))
    if row is None:
        s.add(m.CacheEntry(source=CACHE_SOURCE, target=target, fetched_at=datetime.now(UTC)))
    else:
        row.fetched_at = datetime.now(UTC)


def _usable(item: m.Item) -> bool:
    return (
        bool(item.text or spoken_quotes(item))
        and item.platform not in SKIP_PLATFORMS
        and item.source_type not in SKIP_SOURCE_TYPES
        and not item.is_deleted_at_source
    )


def candidate_pairs(
    s: Session, persons: dict[int, Person], since: datetime | None = None, scan_unlinked: bool = True
) -> list[tuple[m.Item, m.Athlete, m.ItemAthlete | None]]:
    """(item, person, link) pairs worth a look: German text, or a video with German speech."""
    ids = list(persons)
    out: list[tuple[m.Item, m.Athlete, m.ItemAthlete | None]] = []
    seen: set[tuple[int, int]] = set()
    for k in range(0, len(ids), 500):
        q = (
            select(m.Item, m.ItemAthlete)
            .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
            .where(m.ItemAthlete.athlete_id.in_(ids[k : k + 500]))
        )
        if since is not None:
            q = q.where(m.Item.scraped_at >= since)
        for item, link in s.execute(q):
            if not _usable(item):
                continue
            if not (looks_german(item_text(item)) or any(quote_is_german(x) for x in spoken_quotes(item))):
                continue
            seen.add((item.id, link.athlete_id))
            out.append((item, persons[link.athlete_id].athlete, link))
    if scan_unlinked:
        # German items that name her in full but are not linked to her (e.g. a report about the opponent)
        names = {
            aid: norm(p.athlete.full_name)
            for aid, p in persons.items()
            if len(norm(p.athlete.full_name)) >= 8
        }
        q = select(m.Item).where(m.Item.text.is_not(None), m.Item.platform.not_in(SKIP_PLATFORMS))
        if since is not None:
            q = q.where(m.Item.scraped_at >= since)
        for item in s.scalars(q.execution_options(yield_per=500)):
            text = item_text(item)
            if not QUOTE_RX.search(text) or not _usable(item) or not looks_german(text):
                continue
            nt = norm(text)
            for aid, name in names.items():
                if (item.id, aid) not in seen and re.search(
                    r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", nt
                ):
                    seen.add((item.id, aid))
                    out.append((item, persons[aid].athlete, None))
    return out


# --- translation / copy checks (free) -----------------------------------------------------------------------


def _host(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").removeprefix("www.")


def copies(s: Session, item: m.Item, quote: str) -> list[m.Item]:
    """Other stored items with the same quote (first 8 words): a copied club release or a translation."""
    words = norm(quote).split()[:8]
    if len(words) < 5:
        return []
    head = " ".join(words)
    plain = set(re.findall(r"(?<![^\W\d_])[A-Za-z]{4,}(?![^\W\d_])", " ".join(quote.split()[:10])))
    longest = sorted(plain, key=len, reverse=True)[:3]  # ASCII words only: ILIKE does not fold accents
    q = select(m.Item).where(m.Item.id != item.id, m.Item.text.is_not(None))
    for w in longest:  # SQL prefilter on the rarest-looking words; the exact check is below
        q = q.where(m.Item.text.ilike(f"%{w}%"))
    rows = s.scalars(q.limit(500))
    return [r for r in rows if head in norm(item_text(r))]


@dataclass
class Context:
    official: bool
    release_words: bool
    translated_words: bool
    copies: list[m.Item] = field(default_factory=list)


def context_flags(s: Session, item: m.Item, p: Passage) -> Context:
    cps = copies(s, item, p.quote) if not p.spoken else []
    return Context(
        official=item.source_kind == "official",
        release_words=bool(RELEASE_RX.search(item_text(item))),
        translated_words=bool(TRANSLATED_RX.search(p.context)),
        copies=cps,
    )


# --- the model call ------------------------------------------------------------------------------------------

SYSTEM = (
    "Du prüfst deutschsprachige Texte für eine Schweizer TV-Sportmoderatorin (Sportjournalistin, {sport_topic}). "
    "Sie will wissen, ob eine bestimmte Person ({player} oder Trainer/in einer anderen Nation) DEUTSCH spricht, "
    "also ob es ein Interview auf Deutsch mit ihr gibt.\n"
    "Du bekommst eine Person, ein Dokument (Medium, Datum, Link) und nummerierte Zitate mit ihrem Umfeld.\n"
    "Für jedes Zitat:\n"
    "- von_ihr: true nur, wenn es eindeutig die Worte DIESER Person sind (nicht Trainerin, Mitspielerin, Funktionär, "
    "Journalist). Bei Zweifel false.\n"
    "- zitat: der Teil, der ihre Worte sind, WÖRTLICH aus dem Text kopiert (3–40 Wörter, keine Auslassungen).\n"
    "- rahmen: interview (Interview/Gespräch mit diesem Medium), stellungnahme_medium (sie sagt es diesem Medium, "
    "'sagt X gegenüber Blick', nach dem Spiel zu SRF), pressekonferenz, video_audio (sie spricht im Video/Audio), "
    "medienmitteilung (Club-/Verbandsmitteilung, Transfer-News, Zitat mit Signatur 'Name, Funktion', Text eines "
    "Clubs), social_media (eigener Post), anderes.\n"
    "- deutsch_original: ja = sie hat es auf Deutsch gesagt (der Text sagt es, sie spricht im Video Deutsch, oder "
    "es ist ein Interview/eine Aussage gegenüber diesem deutschsprachigen Medium und nichts deutet auf eine "
    "Übersetzung hin); nein = klar übersetzt (auf Englisch gesagt, aus einer englischen Mitteilung, Agentur aus dem "
    "Ausland, Pressekonferenz auf Englisch); unklar = sonst.\n"
    "- grund: ein kurzer deutscher Satz, warum.\n"
    "Leite nichts aus Name, Nationalität oder Club ab. Lieber 'unklar' als 'ja'."
)
# 2026-11-24 (floorball project, men): the person is a man; the field name "von_ihr" stays (schema)
MEN_WORDS = {
    "ein Interview auf Deutsch mit ihr gibt": "ein Interview auf Deutsch mit ihm gibt",
    "(nicht Trainerin, Mitspielerin, Funktionär": "(nicht Trainer, Mitspieler, Funktionär",
    "der Teil, der ihre Worte sind": "der Teil, der seine Worte sind",
    "(sie sagt es diesem Medium": "(er sagt es diesem Medium",
    "(sie spricht im Video/Audio)": "(er spricht im Video/Audio)",
    "ja = sie hat es auf Deutsch gesagt (der Text sagt es, sie spricht im Video": "ja = er hat es auf Deutsch "
    "gesagt (der Text sagt es, er spricht im Video",
}


def system() -> str:
    """SYSTEM in the current project's words (ice hockey: unchanged)."""
    from klugscheisser.llm.words import fill
    from klugscheisser.sports import current_spec

    text = SYSTEM
    if current_spec().men:
        for women, men in MEN_WORDS.items():
            text = text.replace(women, men)
    return fill(text)


SCHEMA = {
    "type": "object",
    "properties": {
        "zitate": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nr": {"type": "integer"},
                    "von_ihr": {"type": "boolean"},
                    "zitat": {"type": "string"},
                    "rahmen": {
                        "type": "string",
                        "enum": [
                            "interview",
                            "stellungnahme_medium",
                            "pressekonferenz",
                            "video_audio",
                            "medienmitteilung",
                            "social_media",
                            "anderes",
                        ],
                    },
                    "deutsch_original": {"type": "string", "enum": ["ja", "nein", "unklar"]},
                    "grund": {"type": "string"},
                },
                "required": ["nr", "von_ihr", "zitat", "rahmen", "deutsch_original", "grund"],
            },
        }
    },
    "required": ["zitate"],
}


def _prompt(item: m.Item, a: m.Athlete, person: Person | None, ps: list[Passage]) -> str:
    who = f"{a.full_name}" + (f" ({person.country}, {person.event})" if person else "")
    role = {"coach": "Trainer/in", "staff": "Staff"}.get(a.role, wording().player)
    when = item.published_at.date().isoformat() if item.published_at else "unbekannt"
    lines = [
        f"Person: {who}, {role}",
        f"Dokument: {item.source_name or _host(item.url)} ({item.source_kind or item.platform}), {when}, {item.url}",
    ]
    if item.title:
        lines.append(f"Titel: {item.title}")
    for i, p in enumerate(ps, 1):
        lines.append(f"\nZitat {i}: «{p.quote}»\nUmfeld {i}: …{p.context}…")
    return "\n".join(lines)


def model_llm(settings: Settings):
    from klugscheisser.llm import text as llm_text

    def call(system: str, prompt: str, schema: dict) -> tuple[dict, dict]:
        return llm_text.generate(settings, system, prompt, schema=schema, role="text", temperature=0.0)

    return call


# --- decisions -----------------------------------------------------------------------------------------------


@dataclass
class Decision:
    item_id: int
    athlete_id: int
    name: str
    url: str
    date: str | None
    outlet: str
    label: str | None  # "bestaetigt" | "wahrscheinlich" | None (rejected)
    reason: str
    quote: str | None = None
    short: str = ""

    def out(self) -> dict[str, Any]:
        return {
            "person": self.name,
            "label": self.label or "abgelehnt",
            "grund": self.reason,
            "zitat": self.quote,
            "quelle": self.outlet,
            "datum": self.date,
            "url": self.url,
            "item": self.item_id,
        }


def outlet_of(item: m.Item) -> str:
    name = (item.source_name or "").split(" - ")[0].strip() or _host(item.url)
    return name[:60]


def decide(verdict: dict, p: Passage, ctx: Context, item: m.Item) -> tuple[str | None, str, str]:
    """The rule (the model only describes; the code labels): (label, reason, short reason for the team table)."""
    outlet = outlet_of(item)
    rahmen = verdict.get("rahmen") or "anderes"
    original = verdict.get("deutsch_original") or "unklar"
    if not verdict.get("von_ihr"):
        return None, f"nicht ihre Worte: {verdict.get('grund') or ''}".strip(), ""
    if original == "nein" or ctx.translated_words:
        return None, f"übersetztes Zitat: {verdict.get('grund') or ''}".strip(), ""
    if ctx.official or ctx.release_words:
        return (
            "wahrscheinlich",
            f"deutsches Zitat in einer Club-/Verbandsmitteilung ({outlet}), oft übersetzt, kein Interview",
            f"Zitat in Club-Mitteilung ({outlet})",
        )
    if ctx.copies:
        return (
            "wahrscheinlich",
            f"deutsches Zitat in {outlet}, gleiches Zitat auch bei {outlet_of(ctx.copies[0])}: übernommene "
            "Mitteilung, kein Interview",
            f"übernommenes Zitat ({outlet})",
        )
    if rahmen == "medienmitteilung":
        return (
            "wahrscheinlich",
            f"deutsches Zitat aus einer Mitteilung ({outlet}), oft übersetzt, kein Interview",
            f"Zitat in Mitteilung ({outlet})",
        )
    if rahmen == "social_media":
        return (
            "wahrscheinlich",
            f"deutsches Zitat aus einem Social-Media-Beitrag ({outlet}), kein Interview",
            f"Zitat in Post ({outlet})",
        )
    if rahmen in INTERVIEW_SETTINGS and (original == "ja" or (p.spoken and rahmen == "video_audio")):
        reason = {
            "interview": f"Interview auf Deutsch mit {outlet}",
            "stellungnahme_medium": f"Aussage auf Deutsch gegenüber {outlet}",
            "pressekonferenz": f"Pressekonferenz auf Deutsch ({outlet})",
            "video_audio": f"spricht Deutsch im Video ({outlet})",
        }[rahmen]
        return "bestaetigt", reason, reason
    return (
        "wahrscheinlich",
        f"deutsches Zitat in {outlet}, Originalsprache unklar",
        f"Zitat in {outlet}, Original unklar",
    )


RANK = {"bestaetigt": 2, "wahrscheinlich": 1, None: 0}


def _strength(d: Decision) -> float:
    """facts.confidence, also the order of the clues in facts/german.py: interview > German outlet quote of
    unclear origin > own post > club release > copied release."""
    if d.label == "bestaetigt":
        return 0.9
    short = d.short or d.reason
    for prefix, value in (("übernommen", 0.5), ("Zitat in Club", 0.55), ("Zitat in Mitteilung", 0.55)):
        if short.startswith(prefix):
            return value
    return 0.65 if "Post" in short else 0.7


def write_fact(s: Session, d: Decision, item: m.Item, extractor: str) -> bool:
    """Replace the row of this (person, item); True when something changed."""
    old = list(
        s.scalars(
            select(m.Fact).where(
                m.Fact.athlete_id == d.athlete_id,
                m.Fact.kind == EVIDENCE_KIND,
                m.Fact.source_item_id == item.id,
            )
        )
    )
    value = {
        "language": "de",
        "label": d.label,
        "reason": d.reason,
        "short": d.short or d.reason,
        "date": d.date,
        "outlet": d.outlet,
    }
    if d.label is None:
        changed = False
        for f in old:
            if f.extractor.startswith(("rule:german", "german:")) or f.extractor == extractor:
                s.delete(f)
                changed = True
        return changed
    if (
        len(old) == 1
        and old[0].value == value
        and old[0].quote == d.quote
        and abs((old[0].confidence or 0) - _strength(d)) < 1e-9
    ):
        return False
    for f in old:
        if f.status == "conflict":  # hidden by hand: keep it hidden, do not write again
            return False
        s.delete(f)
    s.add(
        m.Fact(
            athlete_id=d.athlete_id,
            kind=EVIDENCE_KIND,
            value=value,
            value_text=d.reason,
            valid_from=date.fromisoformat(d.date) if d.date else None,
            source=d.outlet[:30],
            source_item_id=item.id,
            source_url=item.url,
            quote=d.quote,
            confidence=_strength(d),
            status="confirmed",
            extractor=extractor[:50],
        )
    )
    return True


@dataclass
class Report:
    persons: int = 0
    pairs: int = 0
    cached: int = 0
    candidates: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    written: int = 0
    stopped: str | None = None
    decisions: list[Decision] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "personen": self.persons,
            "paare": self.pairs,
            "schon_geprueft": self.cached,
            "kandidaten": self.candidates,
            "modell_aufrufe": self.calls,
            "kosten_usd": round(self.cost_usd, 4),
            "geschrieben": self.written,
            "gestoppt": self.stopped,
            "bestaetigt": [d.out() for d in self.decisions if d.label == "bestaetigt"],
            "wahrscheinlich": [d.out() for d in self.decisions if d.label == "wahrscheinlich"],
            "abgelehnt": [d.out() for d in self.decisions if d.label is None],
        }


def run(
    s: Session,
    settings: Settings,
    *,
    paid: bool = True,
    max_usd: float = 0.5,
    since: datetime | None = None,
    event_keys: list[str] | None = None,
    athlete_ids: list[int] | None = None,
    force: bool = False,
    scan_unlinked: bool = True,
    llm=None,
    today: date | None = None,
) -> Report:
    """Find German-interview evidence and write `language_evidence` facts. paid=False: prefilter only (counts
    the candidates and the estimate, writes the free rows of her own German captions, no model call)."""
    from klugscheisser.ingest import costs
    from klugscheisser.llm import text as llm_text

    rep = Report()
    persons = roster_persons(s, today, event_keys)
    if athlete_ids:
        persons = {k: v for k, v in persons.items() if k in set(athlete_ids)}
    rep.persons = len(persons)
    pairs = candidate_pairs(s, persons, since=since, scan_unlinked=scan_unlinked)
    rep.pairs = len(pairs)
    done = set() if force else _cached(s, [(i.id, a.id) for i, a, _ in pairs])
    call = llm or (model_llm(settings) if llm_text.available(settings) else None)
    extractor = "injected-llm" if llm else f"german:{llm_text.model(settings, 'text')}"
    by_pair: dict[tuple[int, int], Decision] = {}
    for item, a, link in pairs:
        if (item.id, a.id) in done:
            rep.cached += 1
            continue
        when = item.published_at.date().isoformat() if item.published_at else None
        base = dict(
            item_id=item.id,
            athlete_id=a.id,
            name=a.full_name,
            url=item.url,
            date=when,
            outlet=outlet_of(item),
        )
        if own_caption(item, a, link):
            first = re.split(r"(?<=[.!?])\s", (item.text or "").strip())[0][:300]
            d = Decision(
                **base,
                label="wahrscheinlich",
                reason=f"eigener Post auf Deutsch ({outlet_of(item)}), kein Interview",
                short="eigener Post auf Deutsch",
                quote=first,
            )
            rep.decisions.append(d)
            if write_fact(s, d, item, RULE_EXTRACTOR):
                rep.written += 1
            _mark(s, item.id, a.id)
            continue
        ps = passages(item, a)
        if not ps:
            _mark(s, item.id, a.id)  # German text, but no German quote near her name: nothing to judge
            continue
        rep.candidates += 1
        if not paid or call is None:
            continue
        if rep.cost_usd + EST_USD_PER_CALL > max_usd:
            rep.stopped = f"Budget {max_usd:.2f} USD erreicht"
            continue
        try:
            out, usage = call(system(), _prompt(item, a, persons.get(a.id), ps), SCHEMA)
        except Exception as exc:  # one failing call must not stop the run
            log.warning("german evidence item %s / %s failed: %s", item.id, a.full_name, str(exc)[:160])
            continue
        rep.calls += 1
        cost = llm_text.cost_usd(settings, usage) if usage else 0.0
        rep.cost_usd += cost
        if cost:
            costs.record(s, llm_text.cost_source(settings, COST_KIND), 1, cost, f"item:{item.id}:a{a.id}")
        source_text = item_text(item) + " " + " ".join(spoken_quotes(item))
        best: Decision | None = None
        for v in out.get("zitate") or []:
            nr = int(v.get("nr") or 0)
            if not 1 <= nr <= len(ps):
                continue
            p = ps[nr - 1]
            quote = (v.get("zitat") or "").strip().strip('«»„“”"')
            if v.get("von_ihr") and not (quote and quote_in(quote, source_text)):
                quote = p.quote if quote_in(p.quote, source_text) else ""
            ctx = context_flags(s, item, p)
            label, reason, short = decide(v, p, ctx, item)
            if label is not None and not quote:
                label, reason, short = None, "Zitat nicht wörtlich im Text", ""
            d = Decision(**base, label=label, reason=reason, quote=quote or p.quote, short=short)
            if best is None or RANK[d.label] > RANK[best.label]:
                best = d
        if best is None:
            best = Decision(**base, label=None, reason="Modell: keine Zitate bewertet", quote=None)
        by_pair[(item.id, a.id)] = best
        rep.decisions.append(best)
        if write_fact(s, best, item, extractor):
            rep.written += 1
        _mark(s, item.id, a.id)
    if not paid:
        rep.stopped = f"nur Vorfilter: {rep.candidates} Kandidaten, geschätzt {rep.candidates * EST_USD_PER_CALL:.2f} USD"
    s.flush()
    return rep


def nightly(s: Session, settings: Settings) -> dict:
    """Nightly step: new items of roster persons (unlinked German items only from the last 3 days)."""
    if not settings.german_evidence_nightly:
        return {"status": "aus (GERMAN_EVIDENCE_NIGHTLY=false)"}
    from klugscheisser.llm import text as llm_text

    rep = run(
        s,
        settings,
        paid=llm_text.available(settings),
        max_usd=settings.german_evidence_max_usd,
        since=None,
        scan_unlinked=True,
    )
    out = rep.out()
    return {k: (len(v) if isinstance(v, list) else v) for k, v in out.items()}


def summarize(decisions: list[Decision]) -> dict[str, list[Decision]]:
    per: dict[str, list[Decision]] = defaultdict(list)
    for d in decisions:
        per[d.name].append(d)
    return per


__all__ = [
    "CACHE_SOURCE",
    "Decision",
    "Report",
    "candidate_pairs",
    "decide",
    "nightly",
    "passages",
    "roster_persons",
    "run",
]
