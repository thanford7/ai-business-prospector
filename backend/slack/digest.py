"""Slack digests posted when a news or signal job finishes."""

import logging
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clients.decisions import POST_STRENGTH_CUTOFF
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
    UseCase,
)
from slack.text import (
    divider_block,
    escape_mrkdwn,
    flat,
    header_block,
    section_block,
    slack_link,
)

logger = logging.getLogger(__name__)

DIGEST_LIMIT = 10
PROSPECT_LIMIT = 15


class DigestPoster(Protocol):
    def post(self, channel: str, text: str, blocks: list[dict]) -> None: ...


def post_run_digest(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    poster: DigestPoster,
    channel: str | None = None,
) -> None:
    """Post the digest for a finished run.

    Scheduled runs use the schedule channel. A manual run passes `channel`.
    """
    session = session_factory()
    try:
        run = session.get(JobRun, run_id)
        if run is None:
            logger.warning("Job run %s was not found for the digest", run_id)
            return
        industry = session.get(Industry, run.industry_id)
        if industry is None:
            logger.warning("Industry %s was not found for the digest", run.industry_id)
            return
        destination = channel
        if not destination and run.schedule_id is not None:
            schedule = session.get(Schedule, run.schedule_id)
            if schedule is not None:
                destination = schedule.slack_channel_id
        if not destination:
            logger.warning("Job run %s has no Slack channel", run_id)
            return
        text, blocks = render_run_message(session, run, industry)
        job_type = _job_type(run)
    finally:
        session.close()
    poster.post(destination, text, blocks)
    logger.info(
        "Posted %s digest for run %s to %s", job_type.value, run_id, destination
    )


def render_run_message(
    session: Session, run: JobRun, industry: Industry
) -> tuple[str, list[dict]]:
    job_type = _job_type(run)
    label = _job_label(job_type)
    title = f"{flat(industry.name)} {label}"
    if _status(run) == JobRunStatus.failed:
        detail = escape_mrkdwn(run.error or "The job failed.")[:500]
        text = f"{title} failed: {flat(run.error or 'The job failed.')[:300]}"
        return text, [
            header_block(title),
            section_block([detail], empty="The job failed."),
        ]
    if job_type == JobType.signals:
        return _signal_message(session, run, industry, title)
    return _news_message(session, run, industry, title)


def render_prospect_blocks(session: Session, industry: Industry) -> list[dict]:
    ranked, watching = _prospects(session, industry.id, PROSPECT_LIMIT)
    blocks = [
        section_block(
            [
                _ranked_line(company, index)
                for index, company in enumerate(ranked, start=1)
            ],
            empty="No ranked prospects yet.",
        )
    ]
    if watching:
        blocks.append(divider_block())
        blocks.append(
            section_block(
                [
                    "*Added in Slack*",
                    *[_watching_line(company) for company in watching],
                ],
                empty="No companies added in Slack.",
            )
        )
    return blocks


def _news_message(
    session: Session, run: JobRun, industry: Industry, title: str
) -> tuple[str, list[dict]]:
    stats = run.stats or {}
    use_cases = _new_use_cases(session, industry.id, run)
    prospects = _ranked_companies(session, industry.id, DIGEST_LIMIT)
    prospect_lines = [
        _ranked_line(company, index) for index, company in enumerate(prospects, start=1)
    ]
    if not prospect_lines:
        prospect_lines = ["No ranked prospects yet."]
    use_case_count = _stat(stats, "use_cases", len(use_cases))
    summary = (
        f"Fetched {_stat(stats, 'fetched', 0)}, classified {_stat(stats, 'classified', 0)}, "
        f"{use_case_count} use cases."
    )
    blocks = [
        header_block(title),
        section_block(
            [
                summary,
                *_error_line(
                    stats, "page could not be fetched.", "pages could not be fetched."
                ),
            ],
            empty=summary,
        ),
        divider_block(),
        section_block(
            ["*New use cases*", *_use_case_lines(use_cases)],
            empty="*New use cases*\nNone in this run.",
        ),
        divider_block(),
        section_block(
            ["*Top prospects*", *prospect_lines],
            empty="*Top prospects*\nNo ranked prospects yet.",
        ),
    ]
    text = (
        f"{title}: {use_case_count} new use cases, {len(prospects)} ranked prospects."
    )
    return text, blocks


def _signal_message(
    session: Session, run: JobRun, industry: Industry, title: str
) -> tuple[str, list[dict]]:
    stats = run.stats or {}
    signals = _new_signals(session, industry.id, run)
    postable = _stat(stats, "postable", len(signals))
    summary = f"Scored {_stat(stats, 'scored', 0)} posts. {postable} are strong enough to review."
    blocks = [
        header_block(title),
        section_block(
            [
                summary,
                *_error_line(
                    stats,
                    "lookup could not be completed.",
                    "lookups could not be completed.",
                ),
            ],
            empty=summary,
        ),
        divider_block(),
        section_block(
            _signal_lines(signals), empty="No posts scored 2 or higher in this run."
        ),
    ]
    text = f"{title}: {postable} posts to review."
    return text, blocks


def _new_use_cases(
    session: Session, industry_id: uuid.UUID, run: JobRun
) -> list[tuple[UseCase, Article, Company | None]]:
    if run.started_at is None:
        return []
    rows = session.execute(
        select(UseCase, Article, Company)
        .join(Article, Article.id == UseCase.article_id)
        .outerjoin(Company, Company.id == UseCase.company_id)
        .where(UseCase.industry_id == industry_id, Article.fetched_at == run.started_at)
        .order_by(UseCase.confidence.desc())
        .limit(DIGEST_LIMIT)
    ).all()
    return [(use_case, article, company) for use_case, article, company in rows]


def _new_signals(
    session: Session, industry_id: uuid.UUID, run: JobRun
) -> list[tuple[Signal, Company]]:
    if run.started_at is None:
        return []
    rows = session.execute(
        select(Signal, Company)
        .join(Company, Company.id == Signal.company_id)
        .where(
            Signal.industry_id == industry_id,
            Signal.fetched_at == run.started_at,
            Signal.strength >= POST_STRENGTH_CUTOFF,
        )
        .order_by(Signal.strength.desc(), Signal.posted_at.desc())
        .limit(DIGEST_LIMIT)
    ).all()
    return [(signal, company) for signal, company in rows]


def _ranked_companies(
    session: Session, industry_id: uuid.UUID, limit: int
) -> list[Company]:
    return list(
        session.scalars(
            select(Company)
            .where(
                Company.industry_id == industry_id,
                Company.is_active.is_(True),
                Company.rank_score.is_not(None),
            )
            .order_by(Company.rank_score.desc(), Company.name)
            .limit(limit)
        )
    )


def _prospects(
    session: Session, industry_id: uuid.UUID, limit: int
) -> tuple[list[Company], list[Company]]:
    ranked = _ranked_companies(session, industry_id, limit)
    watching = list(
        session.scalars(
            select(Company)
            .where(
                Company.industry_id == industry_id,
                Company.is_active.is_(True),
                Company.origin == CompanyOrigin.manual,
                Company.rank_score.is_(None),
            )
            .order_by(Company.name)
            .limit(limit)
        )
    )
    return ranked, watching


def _use_case_lines(rows: list[tuple[UseCase, Article, Company | None]]) -> list[str]:
    if not rows:
        return ["None in this run."]
    lines = []
    for use_case, article, company in rows:
        name = company.name if company is not None else "Unknown company"
        summary = escape_mrkdwn(use_case.summary)
        if len(summary) > 180:
            summary = summary[:179] + "…"
        lines.append(
            f"• *{escape_mrkdwn(name)}* — {summary} ({slack_link(article.canonical_url, 'source')})"
        )
    return lines


def _signal_lines(rows: list[tuple[Signal, Company]]) -> list[str]:
    lines = []
    for signal, company in rows:
        label = signal.level_label or "signal"
        shown = label[:1].upper() + label[1:]
        strength = signal.strength if signal.strength is not None else 0
        lines.append(
            f"• *{escape_mrkdwn(company.name)}* — {escape_mrkdwn(shown)} ({strength:.2f}) — "
            f"{slack_link(signal.item_url, 'post')}"
        )
    return lines


def _ranked_line(company: Company, index: int) -> str:
    score = company.rank_score if company.rank_score is not None else 0
    return (
        f"{index}. *{escape_mrkdwn(company.name)}*{_handle(company)} — "
        f"rank {score:.3f} · {company.distinct_source_count} sources"
    )


def _watching_line(company: Company) -> str:
    return f"• *{escape_mrkdwn(company.name)}*{_handle(company)}"


def _handle(company: Company) -> str:
    if not company.x_handle:
        return ""
    return f" @{escape_mrkdwn(company.x_handle)}"


def _error_line(stats: dict, singular: str, plural: str) -> list[str]:
    errors = stats.get("errors") or []
    if not isinstance(errors, list) or not errors:
        return []
    phrase = singular if len(errors) == 1 else plural
    return [f"{len(errors)} {phrase}"]


def _stat(stats: dict, key: str, default: int) -> int:
    value = stats.get(key, default)
    return value if isinstance(value, int) else default


def _job_type(run: JobRun) -> JobType:
    if isinstance(run.job_type, JobType):
        return run.job_type
    return JobType(run.job_type)


def _status(run: JobRun) -> JobRunStatus:
    if isinstance(run.status, JobRunStatus):
        return run.status
    return JobRunStatus(run.status)


def _job_label(job_type: JobType) -> str:
    if job_type == JobType.signals:
        return "buying signals"
    return "news"
