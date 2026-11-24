"""Contracts between the core and pluggable modules (results, news feeds, media analysis).

Sport-agnostic on purpose: a later multi-sport results API is just another ResultsCollector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

# --- results --------------------------------------------------------------------------------------

ParticipantRole = Literal["scorer", "assist", "goalie", "lineup", "penalty", "finisher", "starter"]


@dataclass
class Participant:
    name: str
    role: ParticipantRole
    team: str | None = None
    detail: str | None = None  # "1:0, 12:34 (PP)", "Rang 3, +0.45", "Saves 31/33"


@dataclass
class GameResult:
    source: str  # label shown to the user: "SIHF", "IIHF", "PWHL", "SDHL"
    sport: str  # "icehockey_women", later "ski_alpine_women"
    competition: str  # "PostFinance Women's League 2026/27", "IIHF Frauen-WM 2026"
    external_id: str  # stable id at the source, e.g. SIHF gameId
    url: str  # public page a human can open (used as the source link)
    status: Literal["scheduled", "live", "final"]
    starts_at: datetime | None = None
    home: str | None = None  # for races: None
    away: str | None = None
    score: str | None = None  # "4:0", "3:2 n.V." ; for races None
    period_scores: list[str] = field(default_factory=list)
    venue: str | None = None
    participants: list[Participant] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # trimmed source payload, no personal data beyond names


class ResultsCollector(Protocol):
    name: str  # "sihf", "iihf", "pwhl", "sdhl"
    source_label: str  # "SIHF"

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        """Recent and upcoming games. `teams` (team names) narrows the query when the source allows it."""
        ...


# --- news feeds -----------------------------------------------------------------------------------

SourceKind = Literal[
    "self",
    "official",
    "srf",
    "public",
    "media",
    "boulevard",
    "fan",
    "brand",
    "result",
    "web",
    "profile",
    "other",
]


@dataclass
class FeedConfig:
    name: str  # label, e.g. "SRF", "Blick"
    url: str
    source_kind: SourceKind  # srf | media | boulevard
    source_type: str = "other_media"  # items.source_type: "srf" for SRF, else "other_media"
    section_filter: str | None = None  # substring the article URL must contain, e.g. "/eishockey/"
    fetch_article: bool = False  # fetch the public article page for an excerpt (only if robots.txt allows)
    language: str = "de"  # ISO 639-1 of the feed (v5)
    country: str | None = None  # IOC code of the outlet (v5)


# --- media analysis -------------------------------------------------------------------------------


@dataclass
class MediaRef:
    kind: Literal["image", "video"]
    url: str  # transient CDN URL, never persisted beyond the media queue
    duration_s: float | None = None


@dataclass
class MediaAnalysis:
    description: str | None  # German, factual, 1-3 sentences
    spoken_quotes: list[str] = field(default_factory=list)  # automatically transcribed
    visible_text: str | None = None
    model: str = ""
    analyzed: list[str] = field(default_factory=list)  # e.g. ["image", "image", "video"]
    input_tokens: int = 0  # v8: for the cost ledger
    output_tokens: int = 0


class MediaAnalyzer(Protocol):
    name: str

    def analyze(self, media: list[MediaRef], caption: str) -> MediaAnalysis: ...


# --- v3: raw material (profiles, Wikipedia, interviews) ---------------------------------------------


@dataclass
class ProfileDoc:
    """A raw document about a person: player profile page, Wikipedia summary, interview. Stored as an Item
    (source_type "profile" | "interview", platform e.g. "wikipedia" | "web"), linked to the person."""

    url: str  # public page a human can open
    title: str
    text: str  # verbatim text (excerpt); never paraphrased
    source_name: str  # "Wikipedia (de)", "St. Cloud State Athletics", "SRF"
    source_kind: SourceKind  # official | media | other ...
    source_type: Literal["profile", "interview"] = "profile"
    platform: str = "web"
    language: str | None = None
    published_at: datetime | None = None


class ProfileCollector(Protocol):
    name: str

    def collect(self, full_name: str, sport: str, hints: dict) -> list[ProfileDoc]:
        """hints: {"wikidata_qid", "teams": [...], "handles": [...], "nationality"} to find the right pages."""
        ...


# --- v3: web search ---------------------------------------------------------------------------------


@dataclass
class WebResult:
    url: str
    title: str
    snippet: str
    page_age: str | None = None  # v8: Brave's page date (ISO), when known
    extra: dict | None = None  # v8: e.g. video duration / channel from the video endpoint


@dataclass
class WebPage:
    url: str
    title: str
    text: str | None  # None when fetching is not allowed (robots/terms) or failed
    allowed: bool
    fetched_at: datetime | None = None
    note: str | None = None  # e.g. "robots.txt disallows", "paywall"


class WebSearch(Protocol):
    name: str

    def search(self, query: str, limit: int = 5) -> list[WebResult]: ...

    def fetch(self, url: str) -> WebPage: ...


# --- v3: answer engine contract (used by the MCP tool `frage` and the web app) ----------------------


@dataclass
class Statement:
    aussage: str  # German sentence written by the model, checked against the quote
    zitat: str  # verbatim quote from the document
    item_id: int | None  # stored document (None for web pages not stored yet)
    url: str  # from our DB / fetched page, never from the model
    quelle: str  # "[Instagram-Bio @x, 03.10.]"
    quelle_link: str  # "[Instagram-Bio @x, 03.10.](https://…)"
    zeile_prefix: str  # emoji (meaning), 🌐 (Web) for web documents
    vorsicht: bool = False
    web: bool = False
    support: float | None = None  # Jev probability that the quote supports the statement
    zitat_de: str | None = None  # v8: German translation of a quote that is neither German nor English
    # v12: source computed from stored data ("Berechnet aus gespeicherten Daten"); `basis` = its underlying
    # sources [{"quelle", "url"}]; `url` is then the first basis link
    berechnet: bool = False
    basis: list[dict] = field(default_factory=list)


@dataclass
class AnswerSentence:
    """v12 prosa: one sentence (or one table row as text) of a written answer. `quellen` are 1-based numbers
    into AnswerResult.quellen. `ton` is the confidence wording (Part E): sicher | eine_quelle | vorsicht |
    unsicher."""

    text: str
    quellen: list[int] = field(default_factory=list)
    support: float | None = None
    ton: str = "sicher"


@dataclass
class AnswerBlock:
    """v12 prosa: a paragraph (typ "text", `saetze`) or a table (typ "tabelle", `spalten` + `zeilen`, each row
    an AnswerSentence whose `text` is the cells joined by " | " and `zellen` the cells)."""

    typ: str  # text | tabelle
    saetze: list[AnswerSentence] = field(default_factory=list)
    spalten: list[str] = field(default_factory=list)
    zeilen: list[dict] = field(default_factory=list)  # {"zellen": [...], "satz": AnswerSentence}


@dataclass
class AnswerResult:
    frage: str
    datenstand: str
    statements: list[Statement]
    unbestaetigt: list[Statement] = field(default_factory=list)  # quote ok but support below threshold
    luecken: list[str] = field(default_factory=list)  # what the sources did not answer
    verworfen: int = 0  # statements dropped by the checks (invented quote / document)
    kosten_usd: float = 0.0
    personen: list[str] = field(default_factory=list)
    person_ids: list[int] = field(default_factory=list)  # resolved scope; carried over for chat follow-ups
    aktualisierung: str | None = None  # e.g. "Tägliche Aktualisierung läuft im Hintergrund …"
    scope_hinweis: str | None = (
        None  # how the question was understood, e.g. "Olympia-Kader 2026 (23 Personen)"
    )
    # no confirmed statement: the closest documents we have (title + link), so the user sees what exists
    naechste_quellen: list[dict] = field(default_factory=list)
    durchsucht: str | None = None  # "23 Personen · 140 Dokumente geprüft"
    aus_cache: str | None = None  # ISO time of the cached answer
    zeiten: dict = field(default_factory=dict)  # seconds per step (logging, rating review)
    # web hits we may not read (terms/robots): title + link only, clearly "nicht geprüft"
    nicht_geprueft: list[dict] = field(default_factory=list)
    briefing_id: int | None = None  # v7: project briefing version the answer was written with
    # v12 prosa answers (web chat): written answer + numbered source list; `statements` stays filled with one
    # statement per cited source, so MCP, cache and the rating review keep working
    stil: str = "liste"  # liste | prosa
    antwort: list[AnswerBlock] = field(default_factory=list)
    # numbered 1..n, referenced by AnswerSentence.quellen
    quellen: list[Statement] = field(default_factory=list)
    nicht_sicher: list[AnswerSentence] = field(default_factory=list)  # below the Jev threshold (collapsed)
    rueckfrage: str | None = None  # Part D: ask back instead of answering
    rueckfrage_vorschlaege: list[str] = field(default_factory=list)  # tap-to-send suggestions
    naechste_fragen: list[str] = field(default_factory=list)  # Part G: suggested follow-ups


class AnswerEngine(Protocol):
    def answer(
        self,
        question: str,
        person_ids: list[int] | None = None,
        team_id: int | None = None,
        event_id: int | None = None,
        since: datetime | None = None,
        web: bool = True,
        history: list[dict] | None = None,
        style: str | None = None,
    ) -> AnswerResult:
        """`history`: previous chat turns, oldest first: {"frage", "antwort", "personen"} (for follow-ups).
        `style` (v12): "liste" (default, MCP) or "prosa" (web chat written answer)."""
        ...
