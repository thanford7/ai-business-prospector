import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import httpx
from openai import OpenAI
from sqlalchemy import select

from clients.decisions import DecisionsArticleGate
from clients.firecrawl import FirecrawlClient
from db.models import Article, Company, CompanyOrigin, IndustrySource, Source, UseCase
from extraction.graph import (
    ExtractedUseCase,
    ExtractionBatch,
    GraphUseCaseExtractor,
    compile_extraction_graph,
)
from jobs.news import run_news_job
from jobs.ranking import prospect_rank_score
from jobs.seed import seed_industries

NOW = datetime(2026, 10, 9, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 8, tzinfo=UTC)


def _predicate(name, probability):
    return SimpleNamespace(type="predicate", name=name, probability=probability)


def _refusal(name):
    return SimpleNamespace(type="refusal", name=name)


class RecordingDecisions:
    def __init__(self, answers_by_title):
        self.answers_by_title = answers_by_title
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        title = kwargs["input"].split("\n", 1)[0]
        return SimpleNamespace(
            answers=_answers_for(kwargs["questions"], self.answers_by_title[title])
        )


class RecordingModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def with_structured_output(self, schema):
        assert schema is ExtractionBatch
        return self

    def invoke(self, prompt: str) -> ExtractionBatch:
        self.prompts.append(prompt)
        if "Acme deploys AI" in prompt:
            return ExtractionBatch(
                use_cases=[
                    ExtractedUseCase(
                        company="Acme Inc.",
                        summary="Acme is piloting invoice matching.",
                        evidence_quote="Acme is piloting AI to match invoices.",
                        confidence=0.9,
                    )
                ]
            )
        if "Generic AI roundup" in prompt:
            return ExtractionBatch(
                use_cases=[
                    ExtractedUseCase(
                        company="Globex",
                        summary="Globex rolled out a forecasting assistant.",
                        evidence_quote="Globex rolled out an AI forecasting assistant.",
                        confidence=0.8,
                    )
                ]
            )
        if "Weak mention" in prompt:
            return ExtractionBatch(
                use_cases=[
                    ExtractedUseCase(
                        company="Maybe Co",
                        summary="A vague aside.",
                        evidence_quote="Maybe Co was mentioned in passing.",
                        confidence=0.2,
                    )
                ]
            )
        return ExtractionBatch(use_cases=[])


def _answers_for(questions, script):
    answers = []
    for question in questions:
        name = question["name"]
        if name in script:
            probability = script[name]
        elif name.startswith("industry_"):
            probability = script.get("*industry*")
        else:
            probability = None
        if probability is None:
            answers.append(_refusal(name))
        else:
            answers.append(_predicate(name, probability))
    return answers


def _run(session, search_items, scrape_pages, answers_by_title, max_articles=10):
    scraped: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if request.url.path == "/v2/search":
            return httpx.Response(
                200, json={"success": True, "data": {"news": search_items}}
            )
        if request.url.path == "/v2/scrape":
            scraped.append(body["url"])
            page = scrape_pages[body["url"]]
            return httpx.Response(200, json={"success": True, "data": page})
        return httpx.Response(404, json={"success": False})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.dev"
    )
    decisions = RecordingDecisions(answers_by_title)
    model = RecordingModel()
    finance = next(
        industry for industry in seed_industries(session) if industry.slug == "finance"
    )
    stats = run_news_job(
        session,
        finance.id,
        fetcher=FirecrawlClient("test-key", http=http),
        classifier=DecisionsArticleGate(
            client=cast(OpenAI, SimpleNamespace(decisions=decisions))
        ),
        extractor=GraphUseCaseExtractor(compile_extraction_graph(model)),
        max_articles=max_articles,
        now=NOW,
    )
    return stats, finance, scraped, decisions, model


def test_news_job_classifies_pages_and_ranks_only_gated_companies(session):
    token = uuid.uuid4().hex[:8]
    source_url = f"https://www.trade-{token}.example/finance"
    canonical_source = f"https://trade-{token}.example/finance"
    acme_url = f"https://news-{token}.example/acme?utm_source=newsletter"
    generic_url = f"https://news-{token}.example/generic"
    retailer_url = f"https://news-{token}.example/retailer"
    paused_url = f"https://paused-{token}.example/feed"

    source = Source(url=source_url, is_active=True, applies_to_all=False)
    paused = Source(url=paused_url, is_active=False, applies_to_all=True)
    session.add_all([source, paused])
    session.flush()
    finance = next(
        industry for industry in seed_industries(session) if industry.slug == "finance"
    )
    session.add(IndustrySource(industry_id=finance.id, source_id=source.id))
    session.add(
        Company(
            industry_id=finance.id,
            name="Kept Manual",
            normalized_name="kept manual",
            origin=CompanyOrigin.manual,
            mention_count=0,
            distinct_source_count=0,
            is_active=True,
        )
    )
    session.flush()

    search_items = [
        {
            "url": acme_url,
            "title": "Acme deploys AI",
            "markdown": "Acme is piloting AI to match invoices.",
            "date": "2026-10-08T00:00:00Z",
        },
        {
            "url": generic_url,
            "title": "Generic AI roundup",
            "markdown": "Globex rolled out an AI forecasting assistant.",
            "date": "2026-10-08T00:00:00Z",
        },
        {
            "url": retailer_url,
            "title": "Retailer AI",
            "markdown": "A grocer is piloting AI.",
            "date": "2026-10-08T00:00:00Z",
        },
    ]
    for existing in session.scalars(select(Source)).all():
        if existing.id != source.id:
            existing.is_active = False
    source.is_active = True
    answers = {
        "Markets recap": {},
        "Acme deploys AI": {
            "ai_news": 0.91,
            "ai_workflow": 0.2,
            "industry_finance": 0.88,
            "*industry*": 0.1,
        },
        "Generic AI roundup": {"ai_news": 0.8, "ai_workflow": 0.1, "*industry*": 0.1},
        "Retailer AI": {
            "ai_news": 0.7,
            "ai_workflow": 0.2,
            "industry_consumer-packaged-goods": 0.93,
            "*industry*": 0.1,
        },
    }
    stats, finance, scraped, decisions, _model = _run(
        session,
        search_items,
        {
            canonical_source: {
                "markdown": "Stocks were mixed.",
                "metadata": {"title": "Markets recap", "sourceURL": source_url},
            }
        },
        answers,
    )

    assert scraped == [canonical_source]
    assert paused_url not in scraped
    assert stats.classified == 4
    assert stats.extracted == 2
    assert stats.use_cases == 2
    assert {call["input"].split("\n", 1)[0] for call in decisions.calls} == set(answers)
    assert all(
        any(question["name"] == "industry_finance" for question in call["questions"])
        for call in decisions.calls
    )

    articles = {
        article.title: article
        for article in session.scalars(select(Article)).all()
        if token in article.canonical_url
    }
    markets = articles["Markets recap"]
    assert markets.ai_news_probability is None
    assert markets.ai_workflow_probability is None
    assert markets.is_ai_related is False
    assert markets.source_id == source.id
    assert markets.use_cases == []

    acme = articles["Acme deploys AI"]
    assert acme.canonical_url == f"https://news-{token}.example/acme"
    assert acme.ai_news_probability == 0.91
    assert acme.is_ai_related is True
    assert acme.published_at == PUBLISHED
    assert [(link.relevance_probability) for link in acme.industry_links] == [0.88]
    assert len(acme.use_cases) == 1

    generic = articles["Generic AI roundup"]
    assert generic.applies_to_all is True
    assert generic.industry_links == []
    assert len(generic.use_cases) == 1

    retailer = articles["Retailer AI"]
    assert retailer.is_ai_related is True
    assert retailer.applies_to_all is False
    assert retailer.use_cases == []
    packaged = next(
        industry
        for industry in seed_industries(session)
        if industry.slug == "consumer-packaged-goods"
    )
    assert {link.industry_id for link in retailer.industry_links} == {packaged.id}

    companies = {
        company.normalized_name: company
        for company in session.scalars(
            select(Company).where(Company.industry_id == finance.id)
        ).all()
    }
    assert "maybe" not in companies
    assert companies["kept manual"].is_active is True
    assert companies["kept manual"].rank_score is None
    acme_company = companies["acme"]
    assert acme_company.origin == CompanyOrigin.news
    assert acme_company.name == "Acme Inc."
    assert acme_company.mention_count == 1
    assert acme_company.distinct_source_count == 1
    assert acme_company.is_active is True
    expected = prospect_rank_score(
        last_seen_at=PUBLISHED,
        distinct_source_count=1,
        confidence=0.9,
        now=NOW,
    )
    assert acme_company.rank_score == expected
    assert companies["globex"].rank_score is not None
    assert stats.companies_ranked == 2

    second, _, scraped_again, decisions_again, model_again = _run(
        session,
        search_items,
        {
            canonical_source: {
                "markdown": "Stocks were mixed.",
                "metadata": {"title": "Markets recap", "sourceURL": source_url},
            }
        },
        answers,
    )
    assert scraped_again == []
    assert decisions_again.calls == []
    assert model_again.prompts == []
    assert second.skipped_existing == 4
    assert second.classified == 0
    assert (
        len(session.scalars(select(UseCase).where(UseCase.article_id == acme.id)).all())
        == 1
    )


def test_low_confidence_name_is_stored_without_a_company(session):
    for existing in session.scalars(select(Source)).all():
        existing.is_active = False
    token = uuid.uuid4().hex[:8]
    url = f"https://news-{token}.example/weak"
    stats, finance, _, _, _ = _run(
        session,
        [
            {
                "url": url,
                "title": "Weak mention",
                "markdown": "Maybe Co was mentioned in passing.",
                "date": "2026-10-08T00:00:00Z",
            }
        ],
        {},
        {
            "Weak mention": {
                "ai_news": 0.9,
                "ai_workflow": 0.1,
                "industry_finance": 0.8,
                "*industry*": 0.0,
            }
        },
    )
    assert stats.use_cases == 1
    assert stats.companies_ranked == 0
    use_case = session.scalar(
        select(UseCase)
        .join(Article)
        .where(Article.canonical_url == f"https://news-{token}.example/weak")
    )
    assert use_case is not None
    assert use_case.company_id is None
    assert use_case.confidence == 0.2
    assert (
        session.scalar(
            select(Company).where(
                Company.industry_id == finance.id, Company.normalized_name == "maybe"
            )
        )
        is None
    )
