import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin


class CompanyOrigin(enum.StrEnum):
    news = "news"
    manual = "manual"


class JobType(enum.StrEnum):
    news = "news"
    signals = "signals"


class JobRunStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class SignalSource(enum.StrEnum):
    x = "x"
    linkedin = "linkedin"


def _enum_values(choices):
    return [item.value for item in choices]


class Industry(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "industries"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    search_query: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="News search string for this industry, such as 'AI use cases in finance'.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    source_links: Mapped[list["IndustrySource"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    companies: Mapped[list["Company"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    article_links: Mapped[list["ArticleIndustry"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    use_cases: Mapped[list["UseCase"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    job_runs: Mapped[list["JobRun"]] = relationship(
        back_populates="industry",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Source(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "sources"

    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    applies_to_all: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Scrape this URL for every industry, including industries added later.",
    )

    industry_links: Mapped[list["IndustrySource"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    articles: Mapped[list["Article"]] = relationship(
        back_populates="source",
        passive_deletes=True,
    )


class IndustrySource(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "industry_sources"
    __table_args__ = (
        UniqueConstraint(
            "industry_id", "source_id", name="uq_industry_sources_industry_id_source_id"
        ),
    )

    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    industry: Mapped[Industry] = relationship(back_populates="source_links")
    source: Mapped[Source] = relationship(back_populates="industry_links")


class Company(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint(
            "industry_id",
            "normalized_name",
            name="uq_companies_industry_id_normalized_name",
        ),
    )

    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Comparison form of the name. Unique with industry_id, so one company is one row per industry.",
    )
    x_handle: Mapped[str | None] = mapped_column(
        String(64),
        comment="X username used to search recent posts, stored without the @.",
    )
    origin: Mapped[CompanyOrigin] = mapped_column(
        Enum(CompanyOrigin, name="company_origin", values_callable=_enum_values),
        nullable=False,
        comment="news when found in crawled articles; manual when added in Slack.",
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        comment="Extraction confidence from 0 to 1 that this company was identified correctly.",
    )
    mention_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="How many crawled articles in this industry named this company.",
    )
    distinct_source_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="How many distinct news outlets mentioned this company. Used when ranking prospects.",
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="When a crawled article last mentioned this company.",
    )
    rank_score: Mapped[float | None] = mapped_column(
        Float,
        comment="Prospect rank from recency, distinct source count, and confidence. Higher is a stronger prospect.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    industry: Mapped[Industry] = relationship(back_populates="companies")
    use_cases: Mapped[list["UseCase"]] = relationship(
        back_populates="company", passive_deletes=True
    )
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Article(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "articles"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"),
        index=True,
    )
    canonical_url: Mapped[str] = mapped_column(
        Text,
        unique=True,
        nullable=False,
        comment="Stable page URL used to recognize a page that is already stored.",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Fetched page content as markdown.",
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    applies_to_all: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="AI-related coverage that matched no single industry, so every industry treats it as relevant.",
    )
    ai_news_probability: Mapped[float | None] = mapped_column(
        Float,
        comment="Probability from 0 to 1 that the article is about AI news. Null until classified or when the question was refused.",
    )
    ai_workflow_probability: Mapped[float | None] = mapped_column(
        Float,
        comment="Probability from 0 to 1 that the article is about AI used in a business workflow. Null until classified or when the question was refused.",
    )
    is_ai_related: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="True when ai_news_probability or ai_workflow_probability is at least 0.5.",
    )

    source: Mapped[Source | None] = relationship(back_populates="articles")
    industry_links: Mapped[list["ArticleIndustry"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    use_cases: Mapped[list["UseCase"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ArticleIndustry(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "article_industries"
    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "industry_id",
            name="uq_article_industries_article_id_industry_id",
        ),
    )

    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relevance_probability: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Probability from 0 to 1 that the article is about this industry. Stored when it is at least 0.5.",
    )

    article: Mapped[Article] = relationship(back_populates="industry_links")
    industry: Mapped[Industry] = relationship(back_populates="article_links")


class UseCase(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "use_cases"

    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"),
        index=True,
    )
    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Extraction confidence from 0 to 1 for this use case.",
    )

    article: Mapped[Article] = relationship(back_populates="use_cases")
    company: Mapped[Company | None] = relationship(back_populates="use_cases")
    industry: Mapped[Industry] = relationship(back_populates="use_cases")


class Signal(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_signals_source_external_id"),
        CheckConstraint("strength >= 0 AND strength <= 3", name="strength_range"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[SignalSource] = mapped_column(
        Enum(SignalSource, name="signal_source", values_callable=_enum_values),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Id from the source network, such as an X post id. Unique together with source.",
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Handle, person, or page name on the source network.",
    )
    item_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Public link included in the Slack digest.",
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strength: Mapped[float | None] = mapped_column(
        Float,
        comment="Buying-signal score from 0 (none) to 3 (strong). A weighted average, so it can fall between levels. Null when scoring was refused.",
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        comment="Confidence from 0 to 1 in the strength score. Null when scoring was refused.",
    )
    level_label: Mapped[str | None] = mapped_column(
        String(32),
        comment="Label of the most likely strength level: none, weak, moderate, or strong.",
    )
    source_metadata: Mapped[dict | None] = mapped_column(
        "metadata",
        JSONB,
        comment="Source-network fields the jobs do not filter on.",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="signals")
    industry: Mapped[Industry] = relationship(back_populates="signals")


class Schedule(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint(
            "industry_id", "job_type", name="uq_schedules_industry_id_job_type"
        ),
    )

    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, name="job_type", values_callable=_enum_values),
        nullable=False,
    )
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    slack_channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    max_articles: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Most articles one news run may fetch.",
    )
    max_companies: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Most companies one signals run may query.",
    )
    max_posts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Most posts one signals run may read for a single company.",
    )

    industry: Mapped[Industry] = relationship(back_populates="schedules")
    job_runs: Mapped[list["JobRun"]] = relationship(
        back_populates="schedule", passive_deletes=True
    )


class JobRun(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "job_runs"

    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"),
        index=True,
        comment="Schedule that started this run. Null for a manual run, and after that schedule is deleted.",
    )
    industry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("industries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, name="job_type", values_callable=_enum_values),
        nullable=False,
    )
    status: Mapped[JobRunStatus] = mapped_column(
        Enum(JobRunStatus, name="job_run_status", values_callable=_enum_values),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict | None] = mapped_column(
        JSONB,
        comment="Counts and outcomes written when the run finishes.",
    )
    error: Mapped[str | None] = mapped_column(Text)

    schedule: Mapped[Schedule | None] = relationship(back_populates="job_runs")
    industry: Mapped[Industry] = relationship(back_populates="job_runs")
