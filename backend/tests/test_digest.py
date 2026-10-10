import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from db.models import (
    Article,
    Company,
    CompanyOrigin,
    Industry,
    JobRun,
    JobRunStatus,
    JobType,
    Schedule,
    Signal,
    SignalSource,
    UseCase,
)
from jobs.news import NewsJobStats
from jobs.signals import SignalJobStats
from scheduler import ProspectScheduler, execute_scheduled_job
from slack.digest import post_run_digest

NOW = datetime(2026, 10, 9, 18, tzinfo=UTC)


class RecordingPoster:
    def __init__(self):
        self.messages = []

    def post(self, channel, text, blocks):
        self.messages.append({"channel": channel, "text": text, "blocks": blocks})


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _clients():
    return SimpleNamespace(
        fetcher=object(),
        classifier=object(),
        extractor=object(),
        searcher=object(),
        scorer=object(),
        close=lambda: None,
    )


def _industry(factory, industry_ids):
    session = factory()
    try:
        suffix = uuid.uuid4().hex[:8]
        industry = Industry(
            name=f"Digest {suffix}",
            slug=f"digest-{suffix}",
            search_query="AI use cases in digest",
            is_active=True,
        )
        session.add(industry)
        session.commit()
        industry_ids.append(industry.id)
        return industry.id, industry.name
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _schedule(factory, industry_id, job_type, channel):
    session = factory()
    try:
        row = Schedule(
            industry_id=industry_id,
            job_type=job_type,
            cron_expression="0 0 1 1 *",
            slack_channel_id=channel,
            is_enabled=True,
            max_articles=3,
            max_companies=2,
            max_posts=4,
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _cleanup(factory, industry_ids, urls):
    session = factory()
    try:
        for industry_id in industry_ids:
            industry = session.get(Industry, industry_id)
            if industry is not None:
                session.delete(industry)
        if urls:
            session.execute(delete(Article).where(Article.canonical_url.in_(urls)))
        session.commit()
    finally:
        session.close()


def test_news_digest_includes_new_use_cases_and_prospects(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    urls = []
    poster = RecordingPoster()

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        url = f"https://digest.example/{uuid.uuid4().hex}"
        urls.append(url)
        article = Article(
            canonical_url=url,
            title="Acme adopts AI",
            body="Acme rolled out a model.",
            fetched_at=now,
            applies_to_all=False,
            is_ai_related=True,
        )
        company = Company(
            industry_id=industry_id,
            name="Digest Acme",
            normalized_name=f"digest-acme-{uuid.uuid4().hex[:8]}",
            origin=CompanyOrigin.news,
            mention_count=1,
            distinct_source_count=2,
            confidence=0.9,
            rank_score=0.82,
            is_active=True,
        )
        session.add(article)
        session.add(company)
        session.flush()
        session.add(
            UseCase(
                article_id=article.id,
                company_id=company.id,
                industry_id=industry_id,
                summary="Digest Acme uses AI to review claims.",
                evidence_quote="Digest Acme uses AI.",
                confidence=0.91,
            )
        )
        return NewsJobStats(
            fetched=1,
            classified=1,
            extracted=1,
            use_cases=1,
            companies_ranked=1,
            errors=["timeout"],
        )

    try:
        industry_id, _name = _industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.news, "Cnews")
        execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            news_job=news_job,
            now=NOW,
            on_finished=lambda run_id: post_run_digest(factory, run_id, poster),
        )
        assert len(poster.messages) == 1
        message = poster.messages[0]
        rendered = message["text"] + "\n" + str(message["blocks"])
        assert message["channel"] == "Cnews"
        assert "Digest Acme" in rendered
        assert "review claims" in rendered
        assert urls[0] in rendered
        assert "0.820" in rendered
        assert "1 page could not be fetched." in rendered
    finally:
        _cleanup(factory, industry_ids, urls)


def test_signal_digest_posts_only_strength_at_least_two(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    poster = RecordingPoster()
    strong = f"https://x.com/i/status/{uuid.uuid4().hex}"
    weak = f"https://x.com/i/status/{uuid.uuid4().hex}"

    def signal_job(
        session, industry_id, *, searcher, scorer, max_companies, max_posts, now=None
    ):
        company = Company(
            industry_id=industry_id,
            name="Signal Co",
            normalized_name=f"signal-co-{uuid.uuid4().hex[:8]}",
            origin=CompanyOrigin.manual,
            mention_count=0,
            distinct_source_count=0,
            is_active=True,
        )
        session.add(company)
        session.flush()
        session.add_all(
            [
                Signal(
                    company_id=company.id,
                    industry_id=industry_id,
                    source=SignalSource.x,
                    external_id=uuid.uuid4().hex,
                    text="Signal Co issued an RFP.",
                    author="reporter",
                    item_url=strong,
                    posted_at=now,
                    strength=2.5,
                    confidence=0.8,
                    level_label="moderate",
                    fetched_at=now,
                ),
                Signal(
                    company_id=company.id,
                    industry_id=industry_id,
                    source=SignalSource.x,
                    external_id=uuid.uuid4().hex,
                    text="Signal Co mentioned AI.",
                    author="reporter",
                    item_url=weak,
                    posted_at=now,
                    strength=0.4,
                    confidence=0.5,
                    level_label="weak",
                    fetched_at=now,
                ),
            ]
        )
        return SignalJobStats(companies_queried=1, posts_read=2, scored=2, postable=1)

    try:
        industry_id, _name = _industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.signals, "Csignals")
        execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            signal_job=signal_job,
            now=NOW,
            on_finished=lambda run_id: post_run_digest(factory, run_id, poster),
        )
        rendered = poster.messages[0]["text"] + "\n" + str(poster.messages[0]["blocks"])
        assert poster.messages[0]["channel"] == "Csignals"
        assert "Signal Co" in rendered
        assert "Moderate" in rendered
        assert "2.50" in rendered
        assert strong in rendered
        assert weak not in rendered
    finally:
        _cleanup(factory, industry_ids, [])


def test_failed_job_posts_the_error_to_the_schedule_channel(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    poster = RecordingPoster()

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        raise RuntimeError("boom")

    try:
        industry_id, name = _industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.news, "Cfailed")
        run_id = execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            news_job=news_job,
            now=NOW,
            on_finished=lambda run_id: post_run_digest(factory, run_id, poster),
        )
        session = factory()
        try:
            run = session.get(JobRun, run_id)
            assert run.status == JobRunStatus.failed
        finally:
            session.close()
        assert poster.messages[0]["channel"] == "Cfailed"
        assert name in poster.messages[0]["text"]
        assert "boom" in poster.messages[0]["text"]
    finally:
        _cleanup(factory, industry_ids, [])


def test_manual_run_posts_to_the_command_channel(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    poster = RecordingPoster()

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        return NewsJobStats(fetched=0)

    runner = ProspectScheduler(factory, _clients, _clients, news_job=news_job)
    try:
        industry_id, name = _industry(factory, industry_ids)
        run_id = runner.run_now(
            industry_id,
            JobType.news,
            max_articles=1,
            max_companies=1,
            max_posts=1,
            now=NOW,
            on_finished=lambda finished: post_run_digest(
                factory, finished, poster, channel="C999"
            ),
        )
        session = factory()
        try:
            run = session.get(JobRun, run_id)
            assert run.schedule_id is None
            assert run.job_type == JobType.news
        finally:
            session.close()
        assert poster.messages[0]["channel"] == "C999"
        assert name in poster.messages[0]["text"]
        assert "news" in poster.messages[0]["text"]
    finally:
        _cleanup(factory, industry_ids, [])
