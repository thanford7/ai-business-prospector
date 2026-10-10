"""News ingestion: search, scrape, classify, extract, and rank prospects for one industry."""

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from clients.decisions import ArticleClassification, DecisionsArticleGate
from clients.firecrawl import FetchedPage, FirecrawlClient, canonical_url
from db.models import (
    Article,
    ArticleIndustry,
    Company,
    CompanyOrigin,
    Industry,
    IndustrySource,
    Source,
    UseCase,
)
from db.session import create_session_factory
from extraction.graph import (
    ExtractedUseCase,
    GraphUseCaseExtractor,
    build_extraction_graph,
)
from jobs.names import (
    display_company_name,
    is_usable_company_name,
    normalize_company_name,
)
from jobs.ranking import LOW_CONFIDENCE_CUTOFF, refresh_company_ranks
from jobs.seed import seed_industries

logger = logging.getLogger(__name__)

DEFAULT_MAX_ARTICLES = 10


class PageFetcher(Protocol):
    def search_news(self, query: str, limit: int) -> list[FetchedPage]: ...

    def scrape(self, url: str) -> FetchedPage: ...


class ArticleClassifier(Protocol):
    def classify(
        self, title: str, body: str, industries: list[Industry]
    ) -> ArticleClassification: ...


class UseCaseExtractor(Protocol):
    def extract(
        self,
        *,
        title: str,
        body: str,
        industry_name: str,
        source_url: str,
    ) -> list[ExtractedUseCase]: ...


@dataclass
class NewsJobStats:
    fetched: int = 0
    skipped_existing: int = 0
    classified: int = 0
    extracted: int = 0
    use_cases: int = 0
    companies_ranked: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _Candidate:
    url: str
    source_id: uuid.UUID | None
    page: FetchedPage | None


def run_news_job(
    session: Session,
    industry_id: uuid.UUID,
    *,
    fetcher: PageFetcher,
    classifier: ArticleClassifier,
    extractor: UseCaseExtractor,
    max_articles: int = DEFAULT_MAX_ARTICLES,
    now: datetime | None = None,
) -> NewsJobStats:
    """Run the news pipeline for one industry.

    The caller owns the transaction. Pages already stored are not fetched again.
    Use cases are written only for articles the Decisions gate keeps for this industry.
    """
    if max_articles < 0:
        raise ValueError("max_articles must be zero or greater")
    industry = session.get(Industry, industry_id)
    if industry is None:
        raise LookupError(f"Industry {industry_id} was not found")

    moment = now or datetime.now(UTC)
    stats = NewsJobStats()
    active_industries = list(
        session.scalars(
            select(Industry).where(Industry.is_active.is_(True)).order_by(Industry.slug)
        )
    )
    sources = _sources_for_industry(session, industry_id)
    source_ids_by_url = _source_ids_by_url(sources, stats)

    pending_new, backlog = _collect_candidates(
        session,
        industry,
        sources,
        source_ids_by_url,
        fetcher,
        max_articles,
        stats,
    )

    extractions = 0
    for candidate in pending_new[:max_articles]:
        page = _page_for_candidate(candidate, fetcher, stats)
        if page is None:
            continue
        article = _store_classified_article(
            session,
            candidate,
            page,
            classifier,
            active_industries,
            moment,
            stats,
        )
        if article is None:
            continue
        if _extract_if_relevant(session, article, industry, extractor, stats):
            extractions += 1

    for article in backlog:
        if extractions >= max_articles:
            break
        if _extract_if_relevant(session, article, industry, extractor, stats):
            extractions += 1

    stats.companies_ranked = refresh_company_ranks(session, industry.id, moment)
    session.flush()
    logger.info(
        "News job for %s fetched=%s classified=%s extracted=%s use_cases=%s ranked=%s",
        industry.slug,
        stats.fetched,
        stats.classified,
        stats.extracted,
        stats.use_cases,
        stats.companies_ranked,
    )
    return stats


def _sources_for_industry(session: Session, industry_id: uuid.UUID) -> list[Source]:
    linked_ids = select(IndustrySource.source_id).where(
        IndustrySource.industry_id == industry_id
    )
    return list(
        session.scalars(
            select(Source)
            .where(Source.is_active.is_(True))
            .where(or_(Source.applies_to_all.is_(True), Source.id.in_(linked_ids)))
            .order_by(Source.url)
        )
    )


def _source_ids_by_url(
    sources: list[Source], stats: NewsJobStats
) -> dict[str, uuid.UUID]:
    mapped: dict[str, uuid.UUID] = {}
    for source in sources:
        try:
            mapped[canonical_url(source.url)] = source.id
        except ValueError:
            stats.errors.append(f"Invalid source URL: {source.url}")
    return mapped


def _collect_candidates(
    session: Session,
    industry: Industry,
    sources: list[Source],
    source_ids_by_url: dict[str, uuid.UUID],
    fetcher: PageFetcher,
    max_articles: int,
    stats: NewsJobStats,
) -> tuple[list[_Candidate], list[Article]]:
    seen_urls: set[str] = set()
    pending_new: list[_Candidate] = []
    backlog: dict[uuid.UUID, Article] = {}

    def consider(
        url: str, source_id: uuid.UUID | None, page: FetchedPage | None
    ) -> None:
        try:
            key = canonical_url(url)
        except ValueError:
            stats.errors.append(f"Invalid URL: {url}")
            return
        if key in seen_urls:
            return
        seen_urls.add(key)
        if source_id is None:
            source_id = source_ids_by_url.get(key)
        existing = session.scalar(select(Article).where(Article.canonical_url == key))
        if existing is not None:
            stats.skipped_existing += 1
            backlog[existing.id] = existing
            return
        pending_new.append(_Candidate(url=key, source_id=source_id, page=page))

    for source in sources:
        consider(source.url, source.id, None)

    search_limit = max(0, max_articles - len(pending_new))
    if search_limit:
        for page in fetcher.search_news(industry.search_query, search_limit):
            consider(page.url, None, page if page.markdown.strip() else None)

    return pending_new, list(backlog.values())


def _page_for_candidate(
    candidate: _Candidate,
    fetcher: PageFetcher,
    stats: NewsJobStats,
) -> FetchedPage | None:
    if candidate.page is not None and candidate.page.markdown.strip():
        stats.fetched += 1
        return candidate.page
    try:
        page = fetcher.scrape(candidate.url)
    except Exception as exc:
        stats.errors.append(f"{candidate.url}: {exc}")
        return None
    stats.fetched += 1
    return page


def _store_classified_article(
    session: Session,
    candidate: _Candidate,
    page: FetchedPage,
    classifier: ArticleClassifier,
    active_industries: list[Industry],
    moment: datetime,
    stats: NewsJobStats,
) -> Article | None:
    title = page.title.strip() or candidate.url
    body = page.markdown
    try:
        classification = classifier.classify(title, body, active_industries)
    except Exception as exc:
        stats.errors.append(f"classify {candidate.url}: {exc}")
        return None
    article = Article(
        source_id=candidate.source_id,
        canonical_url=candidate.url,
        title=title,
        body=body,
        published_at=page.published_at,
        fetched_at=moment,
        applies_to_all=classification.applies_to_all,
        ai_news_probability=classification.ai_news_probability,
        ai_workflow_probability=classification.ai_workflow_probability,
        is_ai_related=classification.is_ai_related,
    )
    session.add(article)
    for (
        linked_industry_id,
        probability,
    ) in classification.industry_probabilities.items():
        article.industry_links.append(
            ArticleIndustry(
                article_id=article.id,
                industry_id=linked_industry_id,
                relevance_probability=probability,
            )
        )
    session.flush()
    stats.classified += 1
    return article


def _extract_if_relevant(
    session: Session,
    article: Article,
    industry: Industry,
    extractor: UseCaseExtractor,
    stats: NewsJobStats,
) -> bool:
    if not _relevant_to_industry(article, industry.id):
        return False
    already = session.scalar(
        select(UseCase.id).where(
            UseCase.article_id == article.id,
            UseCase.industry_id == industry.id,
        )
    )
    if already is not None:
        return False
    try:
        extracted = extractor.extract(
            title=article.title,
            body=article.body,
            industry_name=industry.name,
            source_url=article.canonical_url,
        )
    except Exception as exc:
        stats.errors.append(f"extract {article.canonical_url}: {exc}")
        return False
    stats.extracted += 1
    for item in extracted:
        if _store_use_case(session, article, industry, item):
            stats.use_cases += 1
    session.flush()
    return True


def _relevant_to_industry(article: Article, industry_id: uuid.UUID) -> bool:
    if not article.is_ai_related:
        return False
    if article.applies_to_all:
        return True
    return any(link.industry_id == industry_id for link in article.industry_links)


def _store_use_case(
    session: Session,
    article: Article,
    industry: Industry,
    item: ExtractedUseCase,
) -> bool:
    if not is_usable_company_name(item.company):
        return False
    summary = item.summary.strip()
    evidence = item.evidence_quote.strip()
    if not summary or not evidence:
        return False
    confidence = max(0.0, min(1.0, float(item.confidence)))
    company_id = None
    if confidence >= LOW_CONFIDENCE_CUTOFF:
        company = _upsert_company(session, industry.id, item.company)
        company_id = company.id if company is not None else None
    session.add(
        UseCase(
            article_id=article.id,
            company_id=company_id,
            industry_id=industry.id,
            summary=summary,
            evidence_quote=evidence,
            confidence=confidence,
        )
    )
    return True


def _upsert_company(
    session: Session, industry_id: uuid.UUID, name: str
) -> Company | None:
    normalized = normalize_company_name(name)
    if not normalized:
        return None
    company = session.scalar(
        select(Company).where(
            Company.industry_id == industry_id,
            Company.normalized_name == normalized,
        )
    )
    display = display_company_name(name)
    if company is None:
        company = Company(
            industry_id=industry_id,
            name=display,
            normalized_name=normalized,
            origin=CompanyOrigin.news,
            mention_count=0,
            distinct_source_count=0,
            is_active=True,
        )
        session.add(company)
        session.flush()
        return company
    if company.origin == CompanyOrigin.news and display:
        company.name = display
    return company


def _load_local_env() -> None:
    """Load a local .env without overriding variables already set.

    The file stays next to docker-compose.yml. This CLI is started from
    backend/, so also look at the repository root two levels above this module.
    """
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


def _print_ranked_companies(session: Session, industry: Industry) -> None:
    companies = session.scalars(
        select(Company)
        .where(
            Company.industry_id == industry.id,
            Company.is_active.is_(True),
            Company.rank_score.is_not(None),
        )
        .order_by(Company.rank_score.desc(), Company.name)
    ).all()
    print(f"{industry.name}: {len(companies)} ranked companies")
    for index, company in enumerate(companies, start=1):
        print(
            f"{index}. {company.name}  rank={company.rank_score:.3f}"
            f"  confidence={company.confidence:.2f}"
            f"  mentions={company.mention_count}"
            f"  sources={company.distinct_source_count}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the news job for one industry.")
    parser.add_argument(
        "--industry", required=True, help="Industry slug, for example finance"
    )
    parser.add_argument("--max-articles", type=int, default=DEFAULT_MAX_ARTICLES)
    args = parser.parse_args(argv)
    if args.max_articles < 0:
        raise SystemExit("max-articles must be zero or greater")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _load_local_env()
    session = create_session_factory()()
    try:
        seed_industries(session)
        industry = session.scalar(
            select(Industry).where(Industry.slug == args.industry)
        )
        if industry is None:
            raise SystemExit(f"Unknown industry slug: {args.industry}")
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            raise SystemExit("FIRECRAWL_API_KEY is not set")
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set")
        with FirecrawlClient(api_key) as fetcher:
            stats = run_news_job(
                session,
                industry.id,
                fetcher=fetcher,
                classifier=DecisionsArticleGate(),
                extractor=GraphUseCaseExtractor(build_extraction_graph()),
                max_articles=args.max_articles,
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(
        f"Fetched {stats.fetched}, classified {stats.classified}, "
        f"extracted {stats.extracted}, use cases {stats.use_cases}."
    )
    if stats.errors:
        print(f"{len(stats.errors)} page errors:", file=sys.stderr)
        for error in stats.errors:
            print(error, file=sys.stderr)
    reporting = create_session_factory()()
    try:
        industry = reporting.scalar(
            select(Industry).where(Industry.slug == args.industry)
        )
        if industry is not None:
            _print_ranked_companies(reporting, industry)
    finally:
        reporting.close()


if __name__ == "__main__":
    main()
