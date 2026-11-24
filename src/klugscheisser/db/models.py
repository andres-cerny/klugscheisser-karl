"""Database schema. Text only; no media files are ever stored."""

from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384
CHUNK_DIM = 768  # item_chunks: gemini-embedding-2 at 768 dims, stored as halfvec

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Sport(Base):
    __tablename__ = "sports"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True)  # "icehockey_women"
    name: Mapped[str] = mapped_column(String(100))


class Competition(Base, Timestamped):
    __tablename__ = "competitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    key: Mapped[str] = mapped_column(String(80), unique=True)  # "wch-2027", "pfwl-2026-27", "pwhl-2026-27"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # league | tournament
    season: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(3))
    source_url: Mapped[str | None] = mapped_column(Text)


class Team(Base, Timestamped):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    key: Mapped[str] = mapped_column(String(80), unique=True)  # "sui-women", "zsc-lions-frauen"
    name: Mapped[str] = mapped_column(String(200))
    short_name: Mapped[str | None] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20), default="club")  # club | national
    country: Mapped[str | None] = mapped_column(String(3))
    league: Mapped[str | None] = mapped_column(String(50))  # PFWL, PWHL, NCAA, SDHL...
    instagram_handle: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(Text)


class Event(Base, Timestamped):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    competition_id: Mapped[int | None] = mapped_column(ForeignKey("competitions.id"))
    key: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20), default="game")  # game | tournament | race
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venue: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | live | final
    home_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    score: Mapped[str | None] = mapped_column(String(30))
    source_url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(30))  # "SIHF", "IIHF", ... for games from results feeds
    competition: Mapped[Competition | None] = relationship()


class EventTeam(Base):
    """Teams participating in a tournament-type event."""

    __tablename__ = "event_teams"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    group_name: Mapped[str | None] = mapped_column(String(20))


class Athlete(Base, Timestamped):
    __tablename__ = "athletes"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    full_name: Mapped[str] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    name_normalized: Mapped[str] = mapped_column(String(200), index=True)
    birth_date: Mapped[date | None] = mapped_column(Date)
    nationality: Mapped[str | None] = mapped_column(String(3))  # IOC/ISO alpha-3, "SUI"
    position: Mapped[str | None] = mapped_column(String(20))  # G | D | F
    shoots: Mapped[str | None] = mapped_column(String(1))
    wikidata_qid: Mapped[str | None] = mapped_column(String(20), unique=True)
    eliteprospects_id: Mapped[str | None] = mapped_column(String(20))
    federation_id: Mapped[str | None] = mapped_column(String(50))
    to_watch: Mapped[bool] = mapped_column(Boolean, default=False)
    # athlete | coach | staff | official — the table holds persons; name kept for compatibility
    role: Mapped[str] = mapped_column(String(20), default="athlete", server_default="athlete")
    notes: Mapped[str | None] = mapped_column(Text)
    # v8: 1 = Swiss national team pool + staff (deep sources), 2 = other WM teams (light), 3 = everyone else.
    # `tier` is recomputed from rosters (tiers.py); `tier_manual` is Fabi's choice and always wins.
    tier: Mapped[int] = mapped_column(Integer, default=3, server_default="3", index=True)
    tier_manual: Mapped[int | None] = mapped_column(Integer)

    sport: Mapped[Sport] = relationship()
    aliases: Mapped[list[AthleteAlias]] = relationship(back_populates="athlete", cascade="all, delete-orphan")
    handles: Mapped[list[Handle]] = relationship(back_populates="athlete", cascade="all, delete-orphan")
    affiliations: Mapped[list[Affiliation]] = relationship(
        back_populates="athlete", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "ix_athletes_name_trgm",
            "name_normalized",
            postgresql_using="gin",
            postgresql_ops={"name_normalized": "gin_trgm_ops"},
        ),
    )


class AthleteAlias(Base):
    """Nicknames, maiden names, spelling variants (ü/ue), all stored normalised."""

    __tablename__ = "athlete_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(200))
    alias_normalized: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="variant")  # variant | nickname | maiden
    athlete: Mapped[Athlete] = relationship(back_populates="aliases")
    __table_args__ = (
        UniqueConstraint("athlete_id", "alias_normalized"),
        Index(
            "ix_aliases_trgm",
            "alias_normalized",
            postgresql_using="gin",
            postgresql_ops={"alias_normalized": "gin_trgm_ops"},
        ),
    )


class Affiliation(Base):
    """Which club / national team an athlete belongs to, over time (Swiss players abroad)."""

    __tablename__ = "athlete_affiliations"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    season: Mapped[str | None] = mapped_column(String(20))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    jersey_number: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    athlete: Mapped[Athlete] = relationship(back_populates="affiliations")
    team: Mapped[Team] = relationship()
    __table_args__ = (UniqueConstraint("athlete_id", "team_id", "season"),)


class Handle(Base, Timestamped):
    __tablename__ = "handles"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(20))  # instagram | x
    handle: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="candidate")
    # candidate | wahrscheinlich | verified | rejected
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list] = mapped_column(JsonType, default=list)  # [{"kind","detail","url"}]
    verified_by: Mapped[str | None] = mapped_column(String(100))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_post_id: Mapped[str | None] = mapped_column(String(100))
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile: Mapped[dict | None] = mapped_column(JsonType)  # bio, followers, is_business, full_name
    athlete: Mapped[Athlete] = relationship(back_populates="handles")
    __table_args__ = (UniqueConstraint("platform", "handle", "athlete_id"),)

    @property
    def usable(self) -> bool:
        return self.status in ("verified", "wahrscheinlich")


class EventEntry(Base):
    """Athlete on a roster for an event (or tournament team)."""

    __tablename__ = "event_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    role: Mapped[str | None] = mapped_column(String(30))  # G/D/F, captain, ...
    jersey_number: Mapped[int | None] = mapped_column(Integer)
    list_type: Mapped[str] = mapped_column(String(20), default="provisional")  # provisional | confirmed
    source: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    athlete: Mapped[Athlete] = relationship()
    team: Mapped[Team | None] = relationship()
    __table_args__ = (UniqueConstraint("event_id", "athlete_id"),)


class Item(Base):
    """One collected thing: post, article, result. `url` is a permalink, never a CDN media URL."""

    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(30))  # official_result | athlete_post | srf | other_media
    platform: Mapped[str] = mapped_column(String(30))  # instagram | rss | iihf | sihf | ...
    source_name: Mapped[str | None] = mapped_column(String(100))  # "SRF", "IIHF", "@alina_mueller"
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    text: Mapped[str | None] = mapped_column(Text)
    image_description: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(20))  # image | video | carousel
    tagged: Mapped[list] = mapped_column(JsonType, default=list)  # tagged / mentioned usernames
    content_hash: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict | None] = mapped_column(JsonType)
    is_deleted_at_source: Mapped[bool] = mapped_column(Boolean, default=False)
    # self | official | srf | media | boulevard | fan | brand | result | other  (drives the emoji label)
    source_kind: Mapped[str | None] = mapped_column(String(20))
    media_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    text_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_tsv: Mapped[str | None] = mapped_column(TSVECTOR)
    # v5: "light" = title/teaser/link only (passed no pre-filter, not classified, not embedded); "full" = all
    level: Mapped[str] = mapped_column(String(10), default="full", server_default="full")
    language: Mapped[str | None] = mapped_column(String(10))

    athletes: Mapped[list[ItemAthlete]] = relationship(back_populates="item", cascade="all, delete-orphan")
    teams: Mapped[list[ItemTeam]] = relationship(cascade="all, delete-orphan")
    classification: Mapped[Classification | None] = relationship(
        back_populates="item", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("url", "content_hash", name="uq_items_url_hash"),
        Index("ix_items_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_items_level", "level"),
    )


class ItemAthlete(Base):
    __tablename__ = "item_athletes"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), primary_key=True)
    match_kind: Mapped[str] = mapped_column(String(20), default="author")  # author | mention | tag | name
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    main: Mapped[bool | None] = mapped_column(Boolean)  # v5: Jev "Hauptperson" (None = only person / unknown)
    item: Mapped[Item] = relationship(back_populates="athletes")
    athlete: Mapped[Athlete] = relationship()


class ItemTeam(Base):
    """Team-level links, e.g. a match report about the Swiss national team that names no player."""

    __tablename__ = "item_teams"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    team: Mapped[Team] = relationship()


class Classification(Base):
    __tablename__ = "classifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), unique=True)
    relevant_p: Mapped[float] = mapped_column(Float)
    relevant: Mapped[bool] = mapped_column(Boolean)
    category: Mapped[str] = mapped_column(String(30))
    category_p: Mapped[float | None] = mapped_column(Float)
    sensitive_p: Mapped[float] = mapped_column(Float)
    vorsicht: Mapped[bool] = mapped_column(Boolean, default=False)
    newsworthiness: Mapped[int] = mapped_column(Integer)  # 1-5
    storyline: Mapped[str | None] = mapped_column(String(30))
    mood: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[dict | None] = mapped_column(JsonType)  # v5: {"studium": 0.92, ...}
    classifier: Mapped[str] = mapped_column(String(50))
    classifier_version: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    item: Mapped[Item] = relationship(back_populates="classification")
    __table_args__ = (
        Index(
            "ix_classifications_topics",
            "topics",
            postgresql_using="gin",
            postgresql_ops={"topics": "jsonb_path_ops"},
        ),
    )


class Account(Base):
    """What kind of account / outlet a source is (official, fan, brand, media, boulevard ...)."""

    __tablename__ = "accounts"
    platform: Mapped[str] = mapped_column(String(20), primary_key=True)  # instagram | rss | web
    handle: Mapped[str] = mapped_column(String(200), primary_key=True)  # lower-case handle or outlet name
    kind: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str | None] = mapped_column(String(200))
    decided_by: Mapped[str] = mapped_column(String(20), default="list")  # list | jev | llm | manual
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaQueue(Base):
    """Transient media URLs waiting for image/video analysis. Rows are deleted after processing
    and purged after 48 h (CDN links expire anyway); no media files are stored."""

    __tablename__ = "media_queue"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    media: Mapped[list] = mapped_column(JsonType)  # [{"kind","url","duration_s"}]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Embedding(Base):
    __tablename__ = "embeddings"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(100))
    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    __table_args__ = (
        UniqueConstraint("item_id", "model"),
        Index(
            "ix_embeddings_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )


class ItemChunk(Base):
    """Search lab 2026-10-25 (switch RETRIEVAL_CHUNKS): one row per passage of a full document with its vector.
    Short documents are one chunk; long ones (> ~2,000 chars) ~1,800-char passages with a small overlap. The
    embedded text has a header (source, date, persons) that is not stored in `text`. Rebuilt by
    `klugscheisser reembed-chunks`; dropping the table loses nothing else."""

    __tablename__ = "item_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    ord: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))  # hash of the whole document text (stale check)
    vector: Mapped[list[float]] = mapped_column(HALFVEC(CHUNK_DIM))
    __table_args__ = (
        UniqueConstraint("item_id", "model", "ord"),
        Index(
            "ix_item_chunks_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "halfvec_cosine_ops"},
        ),
    )


class Job(Base):
    """Background jobs (e.g. Instagram refresh). Worked by an in-process asyncio worker."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    result: Mapped[dict | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CacheEntry(Base):
    """Marks when a (source, target) pair was last fetched, for TTL decisions."""

    __tablename__ = "cache_entries"
    source: Mapped[str] = mapped_column(String(50), primary_key=True)
    target: Mapped[str] = mapped_column(String(200), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CostLedger(Base):
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    units: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --- v4: chats, searches, add requests (server-side, shared across devices) ---------------------------


class Chat(Base, Timestamped):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # context the chat started from: person page, team page, event page (all optional)
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id", ondelete="SET NULL"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    text: Mapped[str] = mapped_column(Text)  # question, or a short plain-text summary of the answer
    answer: Mapped[dict | None] = mapped_column(JsonType)  # AnswerResult as JSON (assistant)
    add_request_id: Mapped[int | None] = mapped_column(ForeignKey("add_requests.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # v6: Fabi's rating of an answer: gut | okay | schlecht, optional reason
    rating: Mapped[str | None] = mapped_column(String(10))
    rating_reason: Mapped[str | None] = mapped_column(Text)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # v7: the project briefing version the answer was written with (traceability)
    briefing_id: Mapped[int | None] = mapped_column(ForeignKey("project_briefings.id", ondelete="SET NULL"))
    chat: Mapped[Chat] = relationship(back_populates="messages")


class SearchLog(Base):
    __tablename__ = "searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300))
    # what was opened from the search: "person:12", "team:3", "event:wch-2026" (None = result list only)
    result_ref: Mapped[str | None] = mapped_column(String(200))
    result_label: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AddRequest(Base, Timestamped):
    """Fabi asked to add a person or team: proposal → confirm (Ja/Nein/Präzisieren) → background load."""

    __tablename__ = "add_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # person | team
    input: Mapped[dict] = mapped_column(JsonType)  # {"name","instagram","details"} or {"request"}
    proposal: Mapped[dict | None] = mapped_column(JsonType)  # adds.base.Proposal.to_json()
    # proposed | rejected | refined | loading | done | failed
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    refine_text: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("add_requests.id", ondelete="SET NULL"))
    cost_estimate_chf: Mapped[float] = mapped_column(Float, default=0.0)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    athlete_ids: Mapped[list] = mapped_column(JsonType, default=list)  # created / updated persons
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)


class AnswerCache(Base):
    """v6: same question + same scope → instant answer; stale when newer documents exist for the scope."""

    __tablename__ = "answer_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(normalized question + scope)
    question: Mapped[str] = mapped_column(Text)
    person_ids: Mapped[list] = mapped_column(JsonType, default=list)
    result: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- v6: fact layer (extracted once; every value with source document, link and verbatim quote) -----------


class CareerStint(Base):
    """Club / college / national-team station of a person, with seasons. Basis for "who played together"."""

    __tablename__ = "career_stints"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    team_name: Mapped[str] = mapped_column(String(200))  # as written in the source
    # v12: Elite Prospects team id: the same club under a new name ("PWHL Boston" → "Boston Fleet") is one team
    ep_team_id: Mapped[str | None] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="club")  # club | college | national | youth
    league: Mapped[str | None] = mapped_column(String(100))
    season_from: Mapped[int | None] = mapped_column(
        Integer
    )  # start year of the first season (2019 = 2019/20)
    season_to: Mapped[int | None] = mapped_column(
        Integer
    )  # start year of the last season; None = open/current
    role: Mapped[str] = mapped_column(
        String(20), default="player"
    )  # player | captain | assistant | coach | staff
    source: Mapped[str] = mapped_column(
        String(30)
    )  # wikidata | wikipedia | college | federation | news | roster
    source_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    source_url: Mapped[str] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)  # verbatim (None only for structured Wikidata statements)
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0
    )  # Jev support of quote → fact (1.0 Wikidata)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed | conflict | unconfirmed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    team: Mapped[Team | None] = relationship()


class Fact(Base):
    """One fact about a person (birth date, position, jersey number, studies, job, injury …)."""

    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)  # see facts.catalog.FACT_KINDS
    value: Mapped[dict] = mapped_column(JsonType)  # {"text": …, "date": "YYYY-MM-DD", "number": …, …}
    value_text: Mapped[str] = mapped_column(Text)  # human-readable German value, also for search
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    season: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(30))
    source_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    source_url: Mapped[str] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")  # confirmed | conflict | unconfirmed
    extractor: Mapped[str] = mapped_column(String(50), default="")  # "wikidata", "gemini-3.1-flash-lite" …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_facts_athlete_kind", "athlete_id", "kind"),)


class SeasonStat(Base):
    """Per-season statistics of a person for one team and league (v11: Elite Prospects; also SIHF, PWHL …).

    `season` is the start year (2026 = 2026/27), like career_stints. Empty values mean "no data", never 0.
    """

    __tablename__ = "season_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    league: Mapped[str] = mapped_column(String(50))
    team_name: Mapped[str | None] = mapped_column(String(200))
    team_kind: Mapped[str] = mapped_column(String(20), default="club")  # club | college | national | youth
    ep_team_id: Mapped[str | None] = mapped_column(
        String(20)
    )  # v12: Elite Prospects team id (stable on renames)
    phase: Mapped[str] = mapped_column(String(10), default="regular")  # regular | playoffs
    games: Mapped[int | None] = mapped_column(Integer)
    goals: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[int | None] = mapped_column(Integer)
    penalty_minutes: Mapped[int | None] = mapped_column(Integer)
    plus_minus: Mapped[int | None] = mapped_column(Integer)
    # goalies
    wins: Mapped[int | None] = mapped_column(Integer)
    losses: Mapped[int | None] = mapped_column(Integer)
    ties: Mapped[int | None] = mapped_column(Integer)
    shutouts: Mapped[int | None] = mapped_column(Integer)
    goals_against: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    toi: Mapped[str | None] = mapped_column(String(20))  # time on ice as written by the source ("1234:56")
    save_pct: Mapped[float | None] = mapped_column(Float)  # 0.918
    gaa: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), default="league")  # eliteprospects | sihf | pwhl …
    source_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "athlete_id", "season", "league", "team_name", "phase", "source", name="uq_season_stats_row"
        ),
    )


class EpProfile(Base):
    """v11: last Elite Prospects page data of a person (cache, so re-imports never refetch) + parsed extras.

    `raw` holds the page's data blocks (playerData, initialLeagueStats, …). `data` holds what has no own table:
    {"form": {"last5": {...}, "last10": {...}}, "career_totals": [...], "highlights": [...],
     "links": {"instagram": url, ...}, "latest": {"team", "league", "season", "jersey_number", "status"}}.
    """

    __tablename__ = "ep_profiles"
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), primary_key=True)
    ep_id: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JsonType)
    data: Mapped[dict] = mapped_column(JsonType, default=dict)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceState(Base):
    """v11: fetch state of a polite scraper (blocks, backoff, daily page count). One row per source."""

    __tablename__ = "source_state"
    source: Mapped[str] = mapped_column(String(30), primary_key=True)  # "eliteprospects"
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict] = mapped_column(JsonType, default=dict)  # {"day": "2026-10-05", "pages": 12, ...}
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Translation(Base):
    """v8: German translation of a quote/summary from a source that is neither German nor English. Made once
    (Gemini Flash-Lite), shown under the original and marked "automatisch übersetzt"; never used as a source."""

    __tablename__ = "translations"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the original text
    source_lang: Mapped[str | None] = mapped_column(String(10))
    text_de: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectSetting(Base):
    """v7: settings Fabi changes in the app for this project (cost caps, nightly hour, news polling)."""

    __tablename__ = "project_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProjectBriefing(Base):
    """v7: the project briefing ("system prompt"), versioned. Each save is a new row; the newest one counts.
    `text` is written by Fabi; `learned` holds the accepted "Was wir gelernt haben" part."""

    __tablename__ = "project_briefings"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    learned: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="Fabi")
    note: Mapped[str | None] = mapped_column(String(300))  # "Gelerntes übernommen", "Text geändert"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BriefingSuggestion(Base):
    """v7: a suggested "learned" part from questions and ratings; becomes part of the briefing only when Fabi
    accepts or edits it (never silently)."""

    __tablename__ = "briefing_suggestions"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    basis: Mapped[dict | None] = mapped_column(JsonType)  # counts and examples the suggestion is based on
    status: Mapped[str] = mapped_column(String(20), default="offen")  # offen | angenommen | abgelehnt
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NamePronunciation(Base):
    """v6 step 7: how a name is pronounced — a Wikimedia recording or a computer voice (normal + slow), the IPA
    spelling where a source has it, and a German-readable hint. Generated once per person and stored."""

    __tablename__ = "name_pronunciations"
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), primary_key=True)
    language: Mapped[str | None] = mapped_column(String(10))  # "cs", "sv", "de-CH"
    source: Mapped[str] = mapped_column(String(20))  # wikimedia | tts
    label: Mapped[str] = mapped_column(
        String(200)
    )  # "Aufnahme (Wikimedia Commons)", "Computerstimme (Tschechisch)"
    source_url: Mapped[str | None] = mapped_column(Text)  # Commons file page
    mime: Mapped[str] = mapped_column(String(40), default="audio/wav")
    audio: Mapped[bytes | None] = mapped_column(LargeBinary)
    audio_slow: Mapped[bytes | None] = mapped_column(
        LargeBinary
    )  # unused: 🐢 = slower playback in the browser
    ipa: Mapped[str | None] = mapped_column(String(200))
    ipa_source_url: Mapped[str | None] = mapped_column(Text)
    hint: Mapped[str | None] = mapped_column(String(200))  # "PESCH-la-ro-wa" (automatisch, ohne Gewähr)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeamStaff(Base):
    """v19: one staff appointment of a team (head coach, assistant, goalie coach, GM, physio …) with since/until.

    Filled from staff seeds (`seeds/staff/<team>.yaml`: appointments with dates + verbatim quote, e.g. the coaching
    change Muller → Miller) and from every roster list that names staff (seed rosters, the roster watcher). Event
    rosters keep their own staff lines in `event_entries`; this table is the team's history across events.
    `since`/`until` are ISO dates of the precision the source gives ("2019", "2026-08", "2026-04-25")."""

    __tablename__ = "team_staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id", ondelete="CASCADE"), index=True)
    role_key: Mapped[str] = mapped_column(
        String(30)
    )  # head_coach | assistant_coach | goalie_coach | … (staff.py)
    role_label: Mapped[str | None] = mapped_column(
        Text
    )  # as written in the source ("Head Coach", "Doktorin")
    since: Mapped[str | None] = mapped_column(String(10))
    until: Mapped[str | None] = mapped_column(String(10))
    last_seen: Mapped[date | None] = mapped_column(Date)  # newest source date naming the person in this role
    source: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_date: Mapped[date | None] = mapped_column(Date)
    quote: Mapped[str | None] = mapped_column(Text)  # verbatim, when the source has a sentence about it
    origin: Mapped[str] = mapped_column(String(20), default="roster")  # seed | roster
    note: Mapped[str | None] = mapped_column(Text)  # e.g. "Vertrag bis Sommer 2029"
    # 2026-11-01: work contact (media managers / press officers), only as the federation/IIHF publishes it
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    athlete: Mapped[Athlete] = relationship()
    team: Mapped[Team] = relationship()
