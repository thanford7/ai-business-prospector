import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from db.models import (
    Company,
    CompanyOrigin,
    Industry,
    JobRun,
    JobRunStatus,
    JobType,
    Schedule,
)
from jobs.news import NewsJobStats
from jobs.signals import SignalJobStats
from main import create_app
from scheduler import ProspectScheduler, execute_scheduled_job, schedule_job_id

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
YEARLY = "0 0 1 1 *"


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _new_industry(factory, industry_ids):
    session = factory()
    try:
        suffix = uuid.uuid4().hex[:8]
        industry = Industry(
            name=f"Phase4 {suffix}",
            slug=f"phase4-{suffix}",
            search_query="AI use cases in phase4",
            is_active=True,
        )
        session.add(industry)
        session.commit()
        industry_ids.append(industry.id)
        return industry.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _schedule(factory, industry_id, job_type, **overrides):
    session = factory()
    try:
        row = Schedule(
            industry_id=industry_id,
            job_type=job_type,
            cron_expression=overrides.get("cron_expression", YEARLY),
            slack_channel_id="C0123",
            is_enabled=overrides.get("is_enabled", True),
            max_articles=overrides.get("max_articles", 3),
            max_companies=overrides.get("max_companies", 2),
            max_posts=overrides.get("max_posts", 4),
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _cleanup(factory, industry_ids):
    session = factory()
    try:
        for industry_id in industry_ids:
            industry = session.get(Industry, industry_id)
            if industry is not None:
                session.delete(industry)
        session.commit()
    finally:
        session.close()


def _clients():
    return SimpleNamespace(
        fetcher=object(),
        classifier=object(),
        extractor=object(),
        searcher=object(),
        scorer=object(),
        close=lambda: None,
    )


def test_reload_registers_enabled_schedules_and_drops_disabled(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    try:
        industry_id = _new_industry(factory, industry_ids)
        news_id = _schedule(
            factory, industry_id, JobType.news, cron_expression="15 8 * * 1"
        )
        signals_id = _schedule(factory, industry_id, JobType.signals, is_enabled=False)
        runner = ProspectScheduler(factory, _clients, _clients)
        assert runner.reload() == 1
        assert runner.scheduler.get_job(schedule_job_id(news_id)) is not None
        assert runner.scheduler.get_job(schedule_job_id(signals_id)) is None

        session = factory()
        try:
            row = session.get(Schedule, news_id)
            row.is_enabled = False
            session.commit()
        finally:
            session.close()
        assert runner.reload() == 0
        assert runner.job_count() == 0
    finally:
        _cleanup(factory, industry_ids)


def test_invalid_cron_is_skipped(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    try:
        industry_id = _new_industry(factory, industry_ids)
        bad_id = _schedule(
            factory, industry_id, JobType.news, cron_expression="not a cron"
        )
        good_id = _schedule(
            factory, industry_id, JobType.signals, cron_expression="0 6 * * *"
        )
        runner = ProspectScheduler(factory, _clients, _clients)
        assert runner.reload() == 1
        assert runner.scheduler.get_job(schedule_job_id(bad_id)) is None
        assert runner.scheduler.get_job(schedule_job_id(good_id)) is not None
    finally:
        _cleanup(factory, industry_ids)


def test_news_schedule_records_success_and_passes_caps(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    seen = {}

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        seen["max_articles"] = max_articles
        seen["now"] = now
        return NewsJobStats(fetched=2, classified=1, use_cases=1, companies_ranked=1)

    def signal_job(*args, **kwargs):
        raise AssertionError("signal job should not run")

    try:
        industry_id = _new_industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.news, max_articles=3)
        run_id = execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            news_job=news_job,
            signal_job=signal_job,
            now=NOW,
        )
        session = factory()
        try:
            run = session.get(JobRun, run_id)
            assert run.status == JobRunStatus.succeeded
            assert run.schedule_id == schedule_id
            assert run.industry_id == industry_id
            assert run.job_type == JobType.news
            assert run.started_at == NOW
            assert run.finished_at is not None
            assert run.error is None
            assert run.stats["fetched"] == 2
            assert run.stats["classified"] == 1
            assert run.stats["companies_ranked"] == 1
        finally:
            session.close()
        assert seen == {"max_articles": 3, "now": NOW}
    finally:
        _cleanup(factory, industry_ids)


def test_signal_schedule_records_success_and_passes_caps(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    seen = {}

    def signal_job(
        session, industry_id, *, searcher, scorer, max_companies, max_posts, now=None
    ):
        seen["max_companies"] = max_companies
        seen["max_posts"] = max_posts
        return SignalJobStats(companies_queried=2, posts_read=4, scored=4, postable=1)

    try:
        industry_id = _new_industry(factory, industry_ids)
        schedule_id = _schedule(
            factory,
            industry_id,
            JobType.signals,
            max_companies=2,
            max_posts=4,
        )
        run_id = execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            signal_job=signal_job,
            now=NOW,
        )
        session = factory()
        try:
            run = session.get(JobRun, run_id)
            assert run.status == JobRunStatus.succeeded
            assert run.job_type == JobType.signals
            assert run.stats["postable"] == 1
            assert run.stats["scored"] == 4
        finally:
            session.close()
        assert seen == {"max_companies": 2, "max_posts": 4}
    finally:
        _cleanup(factory, industry_ids)


def test_failed_job_rolls_back_and_records_the_error(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        session.add(
            Company(
                industry_id=industry_id,
                name="Rolled Back",
                normalized_name=f"rolled-back-{uuid.uuid4().hex[:8]}",
                origin=CompanyOrigin.news,
                mention_count=0,
                distinct_source_count=0,
                is_active=True,
            )
        )
        session.flush()
        raise RuntimeError("FIRECRAWL_API_KEY is not set")

    try:
        industry_id = _new_industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.news)
        run_id = execute_scheduled_job(
            factory,
            schedule_id,
            news_clients=_clients,
            signal_clients=_clients,
            news_job=news_job,
            now=NOW,
        )
        session = factory()
        try:
            run = session.get(JobRun, run_id)
            companies = session.scalar(
                select(func.count())
                .select_from(Company)
                .where(Company.industry_id == industry_id)
            )
            assert run.status == JobRunStatus.failed
            assert run.error == "RuntimeError: FIRECRAWL_API_KEY is not set"
            assert run.stats is None
            assert run.finished_at is not None
            assert companies == 0
        finally:
            session.close()
    finally:
        _cleanup(factory, industry_ids)


def test_disabled_or_missing_schedule_does_not_record_a_run(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    try:
        industry_id = _new_industry(factory, industry_ids)
        schedule_id = _schedule(factory, industry_id, JobType.news, is_enabled=False)
        assert (
            execute_scheduled_job(
                factory,
                schedule_id,
                news_clients=_clients,
                signal_clients=_clients,
                now=NOW,
            )
            is None
        )
        assert (
            execute_scheduled_job(
                factory,
                uuid.uuid4(),
                news_clients=_clients,
                signal_clients=_clients,
                now=NOW,
            )
            is None
        )
        session = factory()
        try:
            count = session.scalar(
                select(func.count())
                .select_from(JobRun)
                .where(JobRun.industry_id == industry_id)
            )
            assert count == 0
        finally:
            session.close()
    finally:
        _cleanup(factory, industry_ids)


def test_saved_cron_fires_the_matching_job_once(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    calls = []

    def news_job(
        session, industry_id, *, fetcher, classifier, extractor, max_articles, now=None
    ):
        calls.append(max_articles)
        return NewsJobStats(fetched=1)

    runner = ProspectScheduler(factory, _clients, _clients, news_job=news_job)
    try:
        industry_id = _new_industry(factory, industry_ids)
        schedule_id = _schedule(
            factory, industry_id, JobType.news, max_articles=5, cron_expression=YEARLY
        )
        runner.reload()
        runner.start()
        job = runner.scheduler.get_job(schedule_job_id(schedule_id))
        assert job is not None
        job.modify(next_run_time=datetime.now(UTC))
        deadline = time.monotonic() + 10
        recorded_stats = None
        while time.monotonic() < deadline:
            session = factory()
            try:
                run = session.scalar(
                    select(JobRun).where(JobRun.schedule_id == schedule_id)
                )
                if run is not None and run.status == JobRunStatus.succeeded:
                    recorded_stats = dict(run.stats)
                    break
            finally:
                session.close()
            time.sleep(0.05)
        else:
            raise AssertionError("cron did not record a succeeded run")
        assert calls == [5]
        assert recorded_stats["fetched"] == 1
        assert recorded_stats["errors"] == []
        session = factory()
        try:
            count = session.scalar(
                select(func.count())
                .select_from(JobRun)
                .where(JobRun.schedule_id == schedule_id)
            )
            assert count == 1
        finally:
            session.close()
    finally:
        runner.shutdown(wait=True)
        _cleanup(factory, industry_ids)


def test_health_reports_registered_schedules(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    runner = ProspectScheduler(factory, _clients, _clients)
    try:
        industry_id = _new_industry(factory, industry_ids)
        _schedule(factory, industry_id, JobType.news)
        app = create_app(runner)
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "schedules": 1}
        assert runner.scheduler.running is False
    finally:
        runner.shutdown(wait=False)
        _cleanup(factory, industry_ids)
