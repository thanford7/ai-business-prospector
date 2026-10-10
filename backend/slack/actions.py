"""Save Slack modal submissions on the prospect tables."""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clients.x import normalize_handle
from db.models import (
    Company,
    CompanyOrigin,
    Industry,
    IndustrySource,
    JobType,
    Schedule,
    Source,
)
from jobs.names import (
    display_company_name,
    is_usable_company_name,
    normalize_company_name,
)
from jobs.news import DEFAULT_MAX_ARTICLES
from jobs.signals import DEFAULT_MAX_COMPANIES, DEFAULT_MAX_POSTS
from scheduler import normalize_cron_expression, validate_cron_expression
from slack.text import escape_mrkdwn
from slack.views import (
    ACTION_ADD,
    ACTION_PAUSE,
    ACTION_RESUME,
    BLOCK_COMPANY_HANDLE,
    BLOCK_COMPANY_INDUSTRY,
    BLOCK_COMPANY_NAME,
    BLOCK_INDUSTRY_ACTION,
    BLOCK_INDUSTRY_NAME,
    BLOCK_INDUSTRY_PICK,
    BLOCK_INDUSTRY_QUERY,
    BLOCK_PROSPECTS_INDUSTRY,
    BLOCK_RUN_INDUSTRY,
    BLOCK_RUN_JOB,
    BLOCK_SCHEDULE_ARTICLES,
    BLOCK_SCHEDULE_CHANNEL,
    BLOCK_SCHEDULE_COMPANIES,
    BLOCK_SCHEDULE_CRON,
    BLOCK_SCHEDULE_ENABLED,
    BLOCK_SCHEDULE_INDUSTRY,
    BLOCK_SCHEDULE_JOB,
    BLOCK_SCHEDULE_POSTS,
    BLOCK_SOURCE_ACTIVE,
    BLOCK_SOURCE_INDUSTRY,
    BLOCK_SOURCE_SCOPE,
    BLOCK_SOURCE_URL,
    FLAG_ACTIVE,
    FLAG_ENABLED,
    SCOPE_ALL,
    SCOPE_ONE,
    IndustryChoice,
    checked,
    conversation_value,
    read_metadata,
    selected_value,
    text_value,
    view_values,
)

MAX_CAP = 100
_NAME_LIMIT = 255
_QUERY_LIMIT = 1000
_URL_LIMIT = 2000


@dataclass(frozen=True)
class ViewSave:
    """Validation errors, or a confirmation of the saved change."""

    errors: dict[str, str]
    message: str = ""


@dataclass(frozen=True)
class ManualRun:
    industry_id: uuid.UUID
    industry_name: str
    job_type: JobType
    channel_id: str
    user_id: str
    max_articles: int
    max_companies: int
    max_posts: int


def industry_choices(session: Session) -> list[IndustryChoice]:
    rows = session.scalars(select(Industry).order_by(Industry.name)).all()
    return [
        IndustryChoice(id=str(row.id), name=row.name, is_active=row.is_active)
        for row in rows
    ]


def apply_industry_view(session: Session, view: dict) -> ViewSave:
    values = view_values(view)
    action = selected_value(values, BLOCK_INDUSTRY_ACTION, BLOCK_INDUSTRY_ACTION)
    if action == ACTION_ADD:
        return _add_industry(
            session,
            text_value(values, BLOCK_INDUSTRY_NAME, BLOCK_INDUSTRY_NAME),
            text_value(values, BLOCK_INDUSTRY_QUERY, BLOCK_INDUSTRY_QUERY),
        )
    if action in {ACTION_PAUSE, ACTION_RESUME}:
        return _set_industry_active(
            session,
            selected_value(values, BLOCK_INDUSTRY_PICK, BLOCK_INDUSTRY_PICK),
            active=action == ACTION_RESUME,
        )
    return _reject({BLOCK_INDUSTRY_ACTION: "Choose add, pause, or resume."})


def apply_source_view(session: Session, view: dict) -> ViewSave:
    values = view_values(view)
    url = _clean_url(text_value(values, BLOCK_SOURCE_URL, BLOCK_SOURCE_URL))
    if url is None:
        return _reject({BLOCK_SOURCE_URL: "Enter an http or https URL."})
    scope = selected_value(values, BLOCK_SOURCE_SCOPE, BLOCK_SOURCE_SCOPE)
    if scope not in {SCOPE_ALL, SCOPE_ONE}:
        return _reject({BLOCK_SOURCE_SCOPE: "Choose who this source applies to."})
    industry: Industry | None = None
    if scope == SCOPE_ONE:
        industry_id = _parse_uuid(
            selected_value(values, BLOCK_SOURCE_INDUSTRY, BLOCK_SOURCE_INDUSTRY)
        )
        industry = (
            session.get(Industry, industry_id) if industry_id is not None else None
        )
        if industry is None:
            return _reject({BLOCK_SOURCE_INDUSTRY: "Choose an industry."})
    source = session.scalar(select(Source).where(Source.url == url))
    created = source is None
    if source is None:
        source = Source(url=url, is_active=True, applies_to_all=False)
        session.add(source)
        try:
            _flush(session)
        except IntegrityError:
            return _reject({BLOCK_SOURCE_URL: "That URL is already saved."})
    source.is_active = checked(
        values, BLOCK_SOURCE_ACTIVE, BLOCK_SOURCE_ACTIVE, FLAG_ACTIVE
    )
    source.applies_to_all = scope == SCOPE_ALL
    session.execute(delete(IndustrySource).where(IndustrySource.source_id == source.id))
    if industry is not None:
        session.add(IndustrySource(industry_id=industry.id, source_id=source.id))
    _flush(session)
    return _saved(
        _source_message(created=created, url=url, industry=industry, source=source)
    )


def apply_company_view(session: Session, view: dict) -> ViewSave:
    values = view_values(view)
    industry_id = _parse_uuid(
        selected_value(values, BLOCK_COMPANY_INDUSTRY, BLOCK_COMPANY_INDUSTRY)
    )
    industry = session.get(Industry, industry_id) if industry_id is not None else None
    if industry is None:
        return _reject({BLOCK_COMPANY_INDUSTRY: "Choose an industry."})
    name = text_value(values, BLOCK_COMPANY_NAME, BLOCK_COMPANY_NAME)
    if not is_usable_company_name(name):
        return _reject({BLOCK_COMPANY_NAME: "Enter a company name."})
    handle, handle_error = _parse_handle(
        text_value(values, BLOCK_COMPANY_HANDLE, BLOCK_COMPANY_HANDLE)
    )
    if handle_error is not None:
        return _reject({BLOCK_COMPANY_HANDLE: handle_error})
    normalized = normalize_company_name(name)
    display = display_company_name(name)
    company = session.scalar(
        select(Company).where(
            Company.industry_id == industry.id,
            Company.normalized_name == normalized,
        )
    )
    if company is None:
        session.add(
            Company(
                industry_id=industry.id,
                name=display,
                normalized_name=normalized,
                x_handle=handle,
                origin=CompanyOrigin.manual,
                mention_count=0,
                distinct_source_count=0,
                is_active=True,
            )
        )
        try:
            _flush(session)
        except IntegrityError:
            return _reject({BLOCK_COMPANY_NAME: "That company is already saved."})
        return _saved(
            _company_message(
                created=True, industry=industry, name=display, handle=handle
            )
        )
    previous_name = company.name
    previous_handle = company.x_handle
    if handle is not None:
        company.x_handle = handle
    if company.origin == CompanyOrigin.manual:
        company.name = display
        company.is_active = True
    _flush(session)
    return _saved(
        _company_message(
            created=False,
            industry=industry,
            name=company.name,
            handle=company.x_handle,
            previous_name=previous_name,
            previous_handle=previous_handle,
        )
    )


def apply_schedule_view(session: Session, view: dict) -> ViewSave:
    values = view_values(view)
    industry_id = _parse_uuid(
        selected_value(values, BLOCK_SCHEDULE_INDUSTRY, BLOCK_SCHEDULE_INDUSTRY)
    )
    industry = session.get(Industry, industry_id) if industry_id is not None else None
    if industry is None:
        return _reject({BLOCK_SCHEDULE_INDUSTRY: "Choose an industry."})
    job_name = selected_value(values, BLOCK_SCHEDULE_JOB, BLOCK_SCHEDULE_JOB)
    if job_name not in {JobType.news.value, JobType.signals.value}:
        return _reject({BLOCK_SCHEDULE_JOB: "Choose news or signals."})
    cron = text_value(values, BLOCK_SCHEDULE_CRON, BLOCK_SCHEDULE_CRON)
    cron_error = validate_cron_expression(cron)
    if cron_error is not None:
        return _reject({BLOCK_SCHEDULE_CRON: cron_error})
    channel_id = conversation_value(
        values, BLOCK_SCHEDULE_CHANNEL, BLOCK_SCHEDULE_CHANNEL
    )
    if not channel_id or len(channel_id) > 32:
        return _reject({BLOCK_SCHEDULE_CHANNEL: "Choose a Slack channel."})
    articles, article_errors = _parse_cap(
        text_value(values, BLOCK_SCHEDULE_ARTICLES, BLOCK_SCHEDULE_ARTICLES),
        BLOCK_SCHEDULE_ARTICLES,
        "Max articles",
    )
    if article_errors:
        return _reject(article_errors)
    companies, company_errors = _parse_cap(
        text_value(values, BLOCK_SCHEDULE_COMPANIES, BLOCK_SCHEDULE_COMPANIES),
        BLOCK_SCHEDULE_COMPANIES,
        "Max companies",
    )
    if company_errors:
        return _reject(company_errors)
    posts, post_errors = _parse_cap(
        text_value(values, BLOCK_SCHEDULE_POSTS, BLOCK_SCHEDULE_POSTS),
        BLOCK_SCHEDULE_POSTS,
        "Max posts",
    )
    if post_errors:
        return _reject(post_errors)
    job_type = JobType(job_name)
    enabled = checked(
        values, BLOCK_SCHEDULE_ENABLED, BLOCK_SCHEDULE_ENABLED, FLAG_ENABLED
    )
    cron_expression = normalize_cron_expression(cron)
    row = session.scalar(
        select(Schedule).where(
            Schedule.industry_id == industry.id, Schedule.job_type == job_type
        )
    )
    created = row is None
    if row is None:
        row = Schedule(
            industry_id=industry.id,
            job_type=job_type,
            cron_expression=cron_expression,
            slack_channel_id=channel_id,
            is_enabled=enabled,
            max_articles=articles,
            max_companies=companies,
            max_posts=posts,
        )
        session.add(row)
    else:
        row.cron_expression = cron_expression
        row.slack_channel_id = channel_id
        row.is_enabled = enabled
        row.max_articles = articles
        row.max_companies = companies
        row.max_posts = posts
    try:
        _flush(session)
    except IntegrityError:
        return _reject({BLOCK_SCHEDULE_JOB: "That industry already has this job."})
    return _saved(
        _schedule_message(
            created=created,
            industry=industry,
            job_type=job_type,
            cron=cron_expression,
            channel_id=channel_id,
            articles=articles,
            companies=companies,
            posts=posts,
            enabled=enabled,
        )
    )


def prepare_manual_run(
    session: Session, view: dict
) -> tuple[dict[str, str], ManualRun | None]:
    values = view_values(view)
    channel_id, user_id = read_metadata(view)
    industry_id = _parse_uuid(
        selected_value(values, BLOCK_RUN_INDUSTRY, BLOCK_RUN_INDUSTRY)
    )
    industry = session.get(Industry, industry_id) if industry_id is not None else None
    if industry is None:
        return {BLOCK_RUN_INDUSTRY: "Choose an industry."}, None
    job_name = selected_value(values, BLOCK_RUN_JOB, BLOCK_RUN_JOB)
    if job_name not in {JobType.news.value, JobType.signals.value}:
        return {BLOCK_RUN_JOB: "Choose news or signals."}, None
    if not channel_id:
        return {
            BLOCK_RUN_JOB: "Open /prospect run again from the channel that should get the digest."
        }, None
    job_type = JobType(job_name)
    articles, companies, posts = _caps_for(session, industry.id, job_type)
    return {}, ManualRun(
        industry_id=industry.id,
        industry_name=industry.name,
        job_type=job_type,
        channel_id=channel_id,
        user_id=user_id,
        max_articles=articles,
        max_companies=companies,
        max_posts=posts,
    )


def selected_prospect_industry(
    session: Session, view: dict
) -> tuple[dict[str, str], Industry | None]:
    industry_id = _parse_uuid(
        selected_value(
            view_values(view), BLOCK_PROSPECTS_INDUSTRY, BLOCK_PROSPECTS_INDUSTRY
        )
    )
    industry = session.get(Industry, industry_id) if industry_id is not None else None
    if industry is None:
        return {BLOCK_PROSPECTS_INDUSTRY: "Choose an industry."}, None
    return {}, industry


def _add_industry(session: Session, name: str, search_query: str) -> ViewSave:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > _NAME_LIMIT:
        return _reject({BLOCK_INDUSTRY_NAME: "Enter an industry name."})
    slug = _slugify(cleaned)
    if not slug:
        return _reject({BLOCK_INDUSTRY_NAME: "Enter an industry name."})
    taken = session.scalar(
        select(Industry.id).where(
            or_(Industry.slug == slug, func.lower(Industry.name) == cleaned.casefold())
        )
    )
    if taken is not None:
        return _reject(
            {BLOCK_INDUSTRY_NAME: "An industry with that name already exists."}
        )
    query = " ".join(search_query.split())
    if not query:
        query = f"AI use cases in {cleaned.casefold()}"
    if len(query) > _QUERY_LIMIT:
        return _reject({BLOCK_INDUSTRY_QUERY: "That search query is too long."})
    session.add(Industry(name=cleaned, slug=slug, search_query=query, is_active=True))
    try:
        _flush(session)
    except IntegrityError:
        return _reject(
            {BLOCK_INDUSTRY_NAME: "An industry with that name already exists."}
        )
    return _saved(
        f"Added industry *{escape_mrkdwn(cleaned)}*. Search query: {escape_mrkdwn(query)}."
    )


def _set_industry_active(session: Session, raw_id: str, *, active: bool) -> ViewSave:
    industry_id = _parse_uuid(raw_id)
    industry = session.get(Industry, industry_id) if industry_id is not None else None
    if industry is None:
        return _reject({BLOCK_INDUSTRY_ACTION: "Choose an industry."})
    industry.is_active = active
    _flush(session)
    verb = "Resumed" if active else "Paused"
    return _saved(f"{verb} industry *{escape_mrkdwn(industry.name)}*.")


def _source_message(
    *, created: bool, url: str, industry: Industry | None, source: Source
) -> str:
    verb = "Saved" if created else "Updated"
    target = (
        "every industry"
        if source.applies_to_all
        else f"*{escape_mrkdwn(industry.name if industry else '')}*"
    )
    status = "active" if source.is_active else "paused"
    return f"{verb} {escape_mrkdwn(url)} for {target}. The source is {status}."


def _company_message(
    *,
    created: bool,
    industry: Industry,
    name: str,
    handle: str | None,
    previous_name: str = "",
    previous_handle: str | None = None,
) -> str:
    industry_name = escape_mrkdwn(industry.name)
    shown = escape_mrkdwn(name)
    if created:
        if handle:
            return f"Added *{shown}* in *{industry_name}* with X handle @{escape_mrkdwn(handle)}."
        return f"Added *{shown}* in *{industry_name}*."
    changes: list[str] = []
    if name != previous_name:
        changes.append(f"name is *{shown}*")
    if handle and handle != previous_handle:
        changes.append(f"X handle is @{escape_mrkdwn(handle)}")
    if not changes:
        return f"Saved *{shown}* in *{industry_name}*."
    return f"Updated *{shown}* in *{industry_name}*: {'; '.join(changes)}."


def _schedule_message(
    *,
    created: bool,
    industry: Industry,
    job_type: JobType,
    cron: str,
    channel_id: str,
    articles: int,
    companies: int,
    posts: int,
    enabled: bool,
) -> str:
    verb = "Saved" if created else "Updated"
    state = "enabled" if enabled else "paused"
    return (
        f"{verb} the {job_type.value} schedule for *{escape_mrkdwn(industry.name)}*. "
        f"It runs `{cron}` UTC and posts to {_channel_ref(channel_id)}. "
        f"Caps are {articles} articles, {companies} companies, and {posts} posts. "
        f"The schedule is {state}."
    )


def _channel_ref(channel_id: str) -> str:
    if re.fullmatch(r"[A-Z0-9]+", channel_id):
        return f"<#{channel_id}>"
    return escape_mrkdwn(channel_id)


def _reject(errors: dict[str, str]) -> ViewSave:
    return ViewSave(errors=errors)


def _saved(message: str) -> ViewSave:
    return ViewSave(errors={}, message=message)


def _caps_for(
    session: Session, industry_id: uuid.UUID, job_type: JobType
) -> tuple[int, int, int]:
    row = session.scalar(
        select(Schedule).where(
            Schedule.industry_id == industry_id, Schedule.job_type == job_type
        )
    )
    if row is None:
        return DEFAULT_MAX_ARTICLES, DEFAULT_MAX_COMPANIES, DEFAULT_MAX_POSTS
    return row.max_articles, row.max_companies, row.max_posts


def _parse_cap(raw: str, block_id: str, label: str) -> tuple[int, dict[str, str]]:
    if not raw.isascii() or not raw.isdigit():
        return 0, {block_id: f"{label} must be a whole number from 0 to {MAX_CAP}."}
    value = int(raw)
    if value > MAX_CAP:
        return 0, {block_id: f"{label} must be {MAX_CAP} or less."}
    return value, {}


def _parse_handle(value: str) -> tuple[str | None, str | None]:
    if not value.strip():
        return None, None
    username = normalize_handle(value)
    if username is None:
        return None, "Use an X handle of 1-15 letters, numbers, or underscores."
    return username, None


def _clean_url(value: str) -> str | None:
    url = value.strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or len(url) > _URL_LIMIT
    ):
        return None
    return url


def _slugify(name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", name).casefold()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return cleaned[:255]


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


def _flush(session: Session) -> None:
    with session.begin_nested():
        session.flush()
