"""Prospect rank from recency, distinct outlets, and extraction confidence."""

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Article, Company, CompanyOrigin, UseCase

LOW_CONFIDENCE_CUTOFF = 0.5
RECENCY_HALF_LIFE_DAYS = 30.0
SOURCE_CREDIT_CAP = 5


def prospect_rank_score(
    *,
    last_seen_at: datetime,
    distinct_source_count: int,
    confidence: float,
    now: datetime,
) -> float:
    """Higher is a stronger prospect.

    Recency halves every 30 days and is weighted 0.45. Distinct outlets, capped at 5,
    are weighted 0.30. Extraction confidence is weighted 0.25.
    """
    age_days = max((now - last_seen_at).total_seconds() / 86400, 0.0)
    recency = math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)
    source_factor = min(distinct_source_count, SOURCE_CREDIT_CAP) / SOURCE_CREDIT_CAP
    return (0.45 * recency) + (0.30 * source_factor) + (0.25 * confidence)


@dataclass
class _MentionStats:
    article_ids: set[uuid.UUID] = field(default_factory=set)
    outlet_keys: set[tuple[str, uuid.UUID]] = field(default_factory=set)
    confidence: float = 0.0
    last_seen_at: datetime | None = None


def refresh_company_ranks(
    session: Session, industry_id: uuid.UUID, now: datetime
) -> int:
    """Recompute rank fields from use cases at or above the confidence cutoff.

    News-origin companies with no qualifying mention are deactivated. Companies
    added in Slack keep their active flag.
    """
    rows = session.execute(
        select(UseCase, Article)
        .join(Article, Article.id == UseCase.article_id)
        .where(
            UseCase.industry_id == industry_id,
            UseCase.company_id.is_not(None),
            UseCase.confidence >= LOW_CONFIDENCE_CUTOFF,
        )
    ).all()
    grouped: dict[uuid.UUID, _MentionStats] = {}
    for use_case, article in rows:
        company_id = use_case.company_id
        if company_id is None:
            continue
        stats = grouped.setdefault(company_id, _MentionStats())
        stats.article_ids.add(article.id)
        if article.source_id is not None:
            stats.outlet_keys.add(("source", article.source_id))
        else:
            stats.outlet_keys.add(("article", article.id))
        seen_at = article.published_at or article.fetched_at
        if stats.last_seen_at is None or seen_at > stats.last_seen_at:
            stats.last_seen_at = seen_at
        if use_case.confidence > stats.confidence:
            stats.confidence = use_case.confidence

    companies = session.scalars(
        select(Company).where(Company.industry_id == industry_id)
    ).all()
    ranked = 0
    for company in companies:
        mention_stats = grouped.get(company.id)
        if mention_stats is None or mention_stats.last_seen_at is None:
            if company.origin == CompanyOrigin.news:
                company.is_active = False
                company.rank_score = None
                company.mention_count = 0
                company.distinct_source_count = 0
            continue
        company.mention_count = len(mention_stats.article_ids)
        company.distinct_source_count = len(mention_stats.outlet_keys)
        company.last_seen_at = mention_stats.last_seen_at
        company.confidence = mention_stats.confidence
        company.rank_score = prospect_rank_score(
            last_seen_at=mention_stats.last_seen_at,
            distinct_source_count=company.distinct_source_count,
            confidence=mention_stats.confidence,
            now=now,
        )
        if company.origin == CompanyOrigin.news:
            company.is_active = True
        if company.is_active and company.rank_score is not None:
            ranked += 1
    return ranked
