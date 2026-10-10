"""Buying signals: recent X search and Decisions scoring for one industry."""

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from clients.decisions import DecisionsSignalScorer, SignalScore, is_postable_strength
from clients.x import XPost, XRecentSearch, build_recent_search_query
from db.models import Company, Industry, Signal, SignalSource
from db.session import create_session_factory

logger = logging.getLogger(__name__)

DEFAULT_MAX_COMPANIES = 10
DEFAULT_MAX_POSTS = 10


class RecentSearch(Protocol):
    def search_recent(self, query: str, limit: int) -> list[XPost]: ...


class SignalScorer(Protocol):
    def score(self, *, company_name: str, text: str) -> SignalScore: ...


@dataclass
class SignalJobStats:
    companies_queried: int = 0
    posts_read: int = 0
    skipped_existing: int = 0
    scored: int = 0
    postable: int = 0
    errors: list[str] = field(default_factory=list)


def run_signal_job(
    session: Session,
    industry_id: uuid.UUID,
    *,
    searcher: RecentSearch,
    scorer: SignalScorer,
    max_companies: int = DEFAULT_MAX_COMPANIES,
    max_posts: int = DEFAULT_MAX_POSTS,
    now: datetime | None = None,
) -> SignalJobStats:
    """Score recent X posts for the top companies in one industry.

    The caller owns the transaction. Companies are taken in rank order. Each
    company is capped at max_posts reads. A post already stored for `(x, id)`
    is not scored again. Every new post is stored, including refusals. Rows
    with strength of at least 2 are counted as postable for the Slack digest.
    """
    if max_companies < 0:
        raise ValueError("max_companies must be zero or greater")
    if max_posts < 0:
        raise ValueError("max_posts must be zero or greater")
    industry = session.get(Industry, industry_id)
    if industry is None:
        raise LookupError(f"Industry {industry_id} was not found")

    moment = now or datetime.now(UTC)
    stats = SignalJobStats()
    if max_companies == 0 or max_posts == 0:
        return stats

    for company in _top_companies(session, industry_id, max_companies):
        query = build_recent_search_query(company.name, company.x_handle)
        if query is None:
            stats.errors.append(f"{company.name}: no searchable name or X handle")
            continue
        try:
            posts = searcher.search_recent(query, max_posts)
        except Exception as exc:
            stats.errors.append(f"{company.name}: {exc}")
            continue
        stats.companies_queried += 1
        for post in posts:
            _score_post(session, industry, company, post, scorer, moment, stats)

    session.flush()
    logger.info(
        "Signal job for %s queried=%s read=%s scored=%s postable=%s",
        industry.slug,
        stats.companies_queried,
        stats.posts_read,
        stats.scored,
        stats.postable,
    )
    return stats


def _top_companies(
    session: Session, industry_id: uuid.UUID, limit: int
) -> list[Company]:
    return list(
        session.scalars(
            select(Company)
            .where(Company.industry_id == industry_id, Company.is_active.is_(True))
            .order_by(Company.rank_score.desc().nulls_last(), Company.name)
            .limit(limit)
        )
    )


def _score_post(
    session: Session,
    industry: Industry,
    company: Company,
    post: XPost,
    scorer: SignalScorer,
    moment: datetime,
    stats: SignalJobStats,
) -> None:
    stats.posts_read += 1
    existing = session.scalar(
        select(Signal.id).where(
            Signal.source == SignalSource.x,
            Signal.external_id == post.id,
        )
    )
    if existing is not None:
        stats.skipped_existing += 1
        return
    try:
        scored = scorer.score(company_name=company.name, text=post.text)
    except Exception as exc:
        stats.errors.append(f"score {post.id}: {exc}")
        return
    session.add(
        Signal(
            company_id=company.id,
            industry_id=industry.id,
            source=SignalSource.x,
            external_id=post.id,
            text=post.text,
            author=post.author,
            item_url=post.url,
            posted_at=post.posted_at or moment,
            strength=scored.strength,
            confidence=scored.confidence,
            level_label=scored.level_label,
            source_metadata=post.metadata or None,
            fetched_at=moment,
        )
    )
    session.flush()
    stats.scored += 1
    if is_postable_strength(scored.strength):
        stats.postable += 1


def _load_local_env() -> None:
    """Load a local .env without overriding variables already set."""
    candidates = (
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    )
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for line in resolved.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _print_signals(session: Session, industry: Industry) -> None:
    rows = session.scalars(
        select(Signal)
        .where(Signal.industry_id == industry.id, Signal.source == SignalSource.x)
        .order_by(Signal.strength.desc().nulls_last(), Signal.posted_at.desc())
    ).all()
    print(f"{industry.name}: {len(rows)} signals")
    for signal in rows:
        strength = "refused" if signal.strength is None else f"{signal.strength:.2f}"
        mark = "post" if is_postable_strength(signal.strength) else "hold"
        print(
            f"[{mark}] {signal.company.name}  strength={strength}"
            f"  level={signal.level_label or '-'}"
            f"  {signal.item_url}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the X signal job for one industry."
    )
    parser.add_argument(
        "--industry", required=True, help="Industry slug, for example finance"
    )
    parser.add_argument("--max-companies", type=int, default=DEFAULT_MAX_COMPANIES)
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS)
    args = parser.parse_args(argv)
    if args.max_companies < 0:
        raise SystemExit("max-companies must be zero or greater")
    if args.max_posts < 0:
        raise SystemExit("max-posts must be zero or greater")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _load_local_env()
    session = create_session_factory()()
    try:
        industry = session.scalar(
            select(Industry).where(Industry.slug == args.industry)
        )
        if industry is None:
            raise SystemExit(f"Unknown industry slug: {args.industry}")
        token = os.environ.get("X_BEARER_TOKEN")
        if not token:
            raise SystemExit("X_BEARER_TOKEN is not set")
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set")
        with XRecentSearch(token) as searcher:
            stats = run_signal_job(
                session,
                industry.id,
                searcher=searcher,
                scorer=DecisionsSignalScorer(),
                max_companies=args.max_companies,
                max_posts=args.max_posts,
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(
        f"Queried {stats.companies_queried}, read {stats.posts_read}, "
        f"scored {stats.scored}, postable {stats.postable}."
    )
    if stats.errors:
        print(f"{len(stats.errors)} signal errors:", file=sys.stderr)
        for error in stats.errors:
            print(error, file=sys.stderr)
    reporting = create_session_factory()()
    try:
        industry = reporting.scalar(
            select(Industry).where(Industry.slug == args.industry)
        )
        if industry is not None:
            _print_signals(reporting, industry)
    finally:
        reporting.close()


if __name__ == "__main__":
    main()
