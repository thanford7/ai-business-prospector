"""Load enabled schedule rows and run them on cron.

Run one process. A second replica would register the same cron and fire each
job twice. Cron expressions are five fields in UTC.
"""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clients.decisions import DecisionsArticleGate, DecisionsSignalScorer
from clients.firecrawl import FirecrawlClient
from clients.x import XRecentSearch
from db.models import JobRun, JobRunStatus, JobType, Schedule
from extraction.graph import GraphUseCaseExtractor, build_extraction_graph
from jobs.news import (
    ArticleClassifier,
    NewsJobStats,
    PageFetcher,
    UseCaseExtractor,
    run_news_job,
)
from jobs.signals import RecentSearch, SignalJobStats, SignalScorer, run_signal_job

logger = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 3600


@dataclass(frozen=True)
class JobRequest:
    industry_id: uuid.UUID
    job_type: JobType
    max_articles: int
    max_companies: int
    max_posts: int
    schedule_id: uuid.UUID | None = None


def normalize_cron_expression(expression: str) -> str:
    return " ".join(expression.split())


def validate_cron_expression(expression: str) -> str | None:
    """Return an error message, or None when the expression is a five-field UTC cron."""
    normalized = normalize_cron_expression(expression)
    if not normalized:
        return "Enter a cron expression."
    if len(normalized) > 128:
        return "That cron expression is too long."
    if len(normalized.split(" ")) != 5:
        return "Use five cron fields: minute hour day month weekday, in UTC."
    try:
        CronTrigger.from_crontab(normalized, timezone="UTC")
    except ValueError:
        return "That cron expression is not valid."
    return None


@dataclass
class NewsClients:
    fetcher: PageFetcher
    classifier: ArticleClassifier
    extractor: UseCaseExtractor

    def close(self) -> None:
        _close_resource(self.fetcher)


@dataclass
class SignalClients:
    searcher: RecentSearch
    scorer: SignalScorer

    def close(self) -> None:
        _close_resource(self.searcher)


def schedule_job_id(schedule_id: uuid.UUID) -> str:
    return f"schedule:{schedule_id}"


def default_news_clients() -> NewsClients:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is not set")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    return NewsClients(
        fetcher=FirecrawlClient(api_key),
        classifier=DecisionsArticleGate(),
        extractor=GraphUseCaseExtractor(build_extraction_graph()),
    )


def default_signal_clients() -> SignalClients:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise RuntimeError("X_BEARER_TOKEN is not set")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")
    return SignalClients(searcher=XRecentSearch(token), scorer=DecisionsSignalScorer())


def execute_job(
    session_factory: sessionmaker[Session],
    request: JobRequest,
    *,
    news_clients: Callable[[], NewsClients],
    signal_clients: Callable[[], SignalClients],
    news_job: Callable[..., NewsJobStats] = run_news_job,
    signal_job: Callable[..., SignalJobStats] = run_signal_job,
    now: datetime | None = None,
    on_finished: Callable[[uuid.UUID], None] | None = None,
) -> uuid.UUID:
    """Create a job run, execute it with the request caps, and record the outcome.

    The news and signal jobs commit with this function. A failure rolls their
    work back and stores the error on the run. `on_finished` receives the run
    id after that outcome is committed, including failed runs.
    """
    moment = now or datetime.now(UTC)
    job_type = (
        request.job_type
        if isinstance(request.job_type, JobType)
        else JobType(request.job_type)
    )
    session = session_factory()
    try:
        run = JobRun(
            schedule_id=request.schedule_id,
            industry_id=request.industry_id,
            job_type=job_type,
            status=JobRunStatus.running,
            started_at=moment,
        )
        session.add(run)
        session.commit()
        run_id = run.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    job_session = session_factory()
    clients: NewsClients | SignalClients | None = None
    stats: NewsJobStats | SignalJobStats | None = None
    status = JobRunStatus.succeeded
    error: str | None = None
    try:
        if job_type == JobType.news:
            opened_news = news_clients()
            clients = opened_news
            stats = news_job(
                job_session,
                request.industry_id,
                fetcher=opened_news.fetcher,
                classifier=opened_news.classifier,
                extractor=opened_news.extractor,
                max_articles=request.max_articles,
                now=moment,
            )
        elif job_type == JobType.signals:
            opened_signals = signal_clients()
            clients = opened_signals
            stats = signal_job(
                job_session,
                request.industry_id,
                searcher=opened_signals.searcher,
                scorer=opened_signals.scorer,
                max_companies=request.max_companies,
                max_posts=request.max_posts,
                now=moment,
            )
        else:
            raise RuntimeError(f"Unknown job type: {job_type}")
        job_session.commit()
    except Exception as exc:
        job_session.rollback()
        status = JobRunStatus.failed
        error = f"{exc.__class__.__name__}: {exc}"
        stats = None
        logger.exception("Job %s for %s failed", job_type.value, request.industry_id)
    finally:
        if clients is not None:
            try:
                clients.close()
            except Exception:
                logger.exception("Failed to close clients for job %s", run_id)
        job_session.close()

    record = session_factory()
    try:
        job_run = record.get(JobRun, run_id)
        if job_run is None:
            raise LookupError(f"Job run {run_id} was not found")
        job_run.status = status
        job_run.finished_at = datetime.now(UTC)
        job_run.stats = asdict(stats) if stats is not None else None
        job_run.error = error
        record.commit()
    except Exception:
        record.rollback()
        raise
    finally:
        record.close()
    logger.info("Job %s for %s %s", job_type.value, request.industry_id, status.value)
    if on_finished is not None:
        try:
            on_finished(run_id)
        except Exception:
            logger.exception("Digest for job run %s failed", run_id)
    return run_id


def execute_scheduled_job(
    session_factory: sessionmaker[Session],
    schedule_id: uuid.UUID,
    *,
    news_clients: Callable[[], NewsClients],
    signal_clients: Callable[[], SignalClients],
    news_job: Callable[..., NewsJobStats] = run_news_job,
    signal_job: Callable[..., SignalJobStats] = run_signal_job,
    now: datetime | None = None,
    on_finished: Callable[[uuid.UUID], None] | None = None,
) -> uuid.UUID | None:
    """Run one enabled schedule and return the job run id.

    Returns None when the schedule is missing or disabled. Caps come from the
    schedule row. A manual run uses `execute_job` with no schedule id.
    """
    session = session_factory()
    try:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            logger.warning("Schedule %s was not found", schedule_id)
            return None
        if not schedule.is_enabled:
            logger.info("Schedule %s is disabled", schedule_id)
            return None
        request = JobRequest(
            industry_id=schedule.industry_id,
            job_type=schedule.job_type
            if isinstance(schedule.job_type, JobType)
            else JobType(schedule.job_type),
            max_articles=schedule.max_articles,
            max_companies=schedule.max_companies,
            max_posts=schedule.max_posts,
            schedule_id=schedule.id,
        )
    finally:
        session.close()
    return execute_job(
        session_factory,
        request,
        news_clients=news_clients,
        signal_clients=signal_clients,
        news_job=news_job,
        signal_job=signal_job,
        now=now,
        on_finished=on_finished,
    )


class ProspectScheduler:
    """In-process cron runner backed by the schedules table."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        news_clients: Callable[[], NewsClients],
        signal_clients: Callable[[], SignalClients],
        *,
        scheduler: BackgroundScheduler | None = None,
        news_job: Callable[..., NewsJobStats] = run_news_job,
        signal_job: Callable[..., SignalJobStats] = run_signal_job,
    ) -> None:
        self._session_factory = session_factory
        self._news_clients = news_clients
        self._signal_clients = signal_clients
        self._news_job = news_job
        self._signal_job = signal_job
        self._on_finished: Callable[[uuid.UUID], None] | None = None
        self.scheduler = scheduler or BackgroundScheduler(timezone="UTC")

    def reload(self) -> int:
        """Replace registered cron jobs with the enabled schedule rows.

        An invalid cron expression is logged and skipped so one bad row does
        not stop the others. Returns how many jobs were registered.
        """
        session = self._session_factory()
        try:
            rows = list(
                session.scalars(select(Schedule).where(Schedule.is_enabled.is_(True)))
            )
            specs = [(row.id, row.cron_expression) for row in rows]
        finally:
            session.close()

        wanted: set[str] = set()
        for schedule_id, expression in specs:
            job_id = schedule_job_id(schedule_id)
            normalized = normalize_cron_expression(expression)
            error = validate_cron_expression(normalized)
            if error is not None:
                logger.error(
                    "Schedule %s has an invalid cron expression: %s (%s)",
                    schedule_id,
                    expression,
                    error,
                )
                continue
            trigger = CronTrigger.from_crontab(normalized, timezone="UTC")
            self.scheduler.add_job(
                self._trigger,
                trigger=trigger,
                id=job_id,
                args=[schedule_id],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
            )
            wanted.add(job_id)

        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith("schedule:") and job.id not in wanted:
                self.scheduler.remove_job(job.id)
        logger.info("Registered %s schedule cron jobs", len(wanted))
        return len(wanted)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    def job_count(self) -> int:
        return sum(
            1 for job in self.scheduler.get_jobs() if job.id.startswith("schedule:")
        )

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def set_digest_handler(self, handler: Callable[[uuid.UUID], None] | None) -> None:
        """Post a digest after each cron run. Set this before `start`."""
        self._on_finished = handler

    def run_now(
        self,
        industry_id: uuid.UUID,
        job_type: JobType,
        *,
        max_articles: int,
        max_companies: int,
        max_posts: int,
        on_finished: Callable[[uuid.UUID], None] | None = None,
        now: datetime | None = None,
    ) -> uuid.UUID:
        """Run one job immediately and record it with a null schedule id."""
        resolved = job_type if isinstance(job_type, JobType) else JobType(job_type)
        return execute_job(
            self._session_factory,
            JobRequest(
                industry_id=industry_id,
                job_type=resolved,
                max_articles=max_articles,
                max_companies=max_companies,
                max_posts=max_posts,
                schedule_id=None,
            ),
            news_clients=self._news_clients,
            signal_clients=self._signal_clients,
            news_job=self._news_job,
            signal_job=self._signal_job,
            now=now,
            on_finished=on_finished,
        )

    def _trigger(self, schedule_id: uuid.UUID) -> None:
        try:
            execute_scheduled_job(
                self._session_factory,
                schedule_id,
                news_clients=self._news_clients,
                signal_clients=self._signal_clients,
                news_job=self._news_job,
                signal_job=self._signal_job,
                on_finished=self._on_finished,
            )
        except Exception:
            logger.exception(
                "Scheduled job %s failed before a run could be recorded", schedule_id
            )


def _close_resource(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()
