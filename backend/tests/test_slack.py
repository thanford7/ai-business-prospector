import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from db.models import (
    Company,
    CompanyOrigin,
    Industry,
    IndustrySource,
    JobType,
    Schedule,
    Source,
)
from jobs.names import normalize_company_name
from main import create_app
from scheduler import (
    NewsClients,
    ProspectScheduler,
    SignalClients,
    validate_cron_expression,
)
from slack.actions import (
    ManualRun,
    apply_company_view,
    apply_industry_view,
    apply_schedule_view,
    apply_source_view,
)
from slack.app import (
    build_bolt_app,
    open_prospect_command,
    parse_prospect_command,
    submit_industries,
    submit_prospects,
    submit_run,
    submit_schedule,
)
from slack.digest import DigestPoster, render_prospect_blocks
from slack.views import IndustryChoice, command_metadata, modal_for

YEARLY = "0 0 1 1 *"


class Ack:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _view(values, *, channel="C1", user="U1"):
    return {
        "state": {"values": values},
        "private_metadata": json.dumps({"channel_id": channel, "user_id": user}),
    }


def _values(**fields):
    return {block_id: {block_id: payload} for block_id, payload in fields.items()}


def _text(value):
    return {"value": value}


def _option(value):
    return {"selected_option": {"value": value}}


def _checks(*values):
    return {"selected_options": [{"value": value} for value in values]}


def _conversation(value):
    return {"selected_conversation": value}


def _industry(session, name="Widgets"):
    industry = Industry(
        name=name,
        slug=name.casefold(),
        search_query=f"AI use cases in {name.casefold()}",
        is_active=True,
    )
    session.add(industry)
    session.flush()
    return industry


def _block_ids(view):
    return [block.get("block_id") for block in view["blocks"]]


def test_cron_expression_rules():
    assert validate_cron_expression("0 8 * * 1") is None
    assert validate_cron_expression("  15 8 * * 1  ") is None
    invalid = validate_cron_expression("not a cron")
    extra_field = validate_cron_expression("0 8 * * 1 *")
    assert invalid is not None and "five" in invalid
    assert extra_field is not None and "five" in extra_field
    assert validate_cron_expression("") == "Enter a cron expression."


def test_parse_prospect_command():
    assert parse_prospect_command("") == "help"
    assert parse_prospect_command("  Industries ") == "industries"
    assert parse_prospect_command("schedule 0 8 * * 1") == "schedule"
    assert parse_prospect_command("nope") == "unknown"


def test_modals_cover_each_command():
    choice = IndustryChoice(id=str(uuid.uuid4()), name="Finance", is_active=True)
    metadata = command_metadata("C1", "U1")
    expected = {
        "industries": "prospect_industries",
        "sources": "prospect_sources",
        "companies": "prospect_companies",
        "schedule": "prospect_schedule",
        "run": "prospect_run",
        "prospects": "prospect_prospects",
    }
    for command, callback_id in expected.items():
        view = modal_for(
            command, [choice], metadata, channel_id="C1", caps=(10, 10, 10)
        )
        assert view["type"] == "modal"
        assert view["callback_id"] == callback_id
        assert view["submit"]["text"]
    schedule = modal_for(
        "schedule", [choice], metadata, channel_id="C42", caps=(10, 10, 10)
    )
    assert "schedule_cron" in _block_ids(schedule)
    channel = next(
        block
        for block in schedule["blocks"]
        if block.get("block_id") == "schedule_channel"
    )
    assert channel["element"]["initial_conversation"] == "C42"
    assert any("UTC" in json.dumps(block) for block in schedule["blocks"])
    empty = modal_for("industries", [], metadata, channel_id="C1", caps=(10, 10, 10))
    assert "industry_name" in _block_ids(empty)
    assert "industry_pick" not in _block_ids(empty)


def test_help_and_unknown_do_not_open_a_modal():
    opened = []

    def fail_factory():
        raise AssertionError("help should not query the database")

    ack = Ack()
    open_prospect_command(
        fail_factory, ack, {"text": "help"}, lambda trigger, view: opened.append(view)
    )
    assert opened == []
    assert "/prospect industries" in ack.calls[0][0][0]

    ack = Ack()
    open_prospect_command(
        fail_factory, ack, {"text": "nope"}, lambda trigger, view: opened.append(view)
    )
    assert opened == []
    assert "Unknown command" in ack.calls[0][0][0]


def test_sources_without_an_industry_asks_for_one():
    class Empty:
        def scalars(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

        def close(self):
            return None

    opened = []
    ack = Ack()
    open_prospect_command(
        lambda: Empty(),
        ack,
        {"text": "sources", "trigger_id": "t"},
        lambda trigger, view: opened.append(view),
    )
    assert opened == []
    assert "/prospect industries" in ack.calls[0][0][0]


def test_industries_command_opens_the_modal():
    class Empty:
        def scalars(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

        def close(self):
            return None

    opened = []
    ack = Ack()
    open_prospect_command(
        lambda: Empty(),
        ack,
        {
            "text": "industries",
            "trigger_id": "trigger",
            "channel_id": "C1",
            "user_id": "U1",
        },
        lambda trigger, view: opened.append((trigger, view)),
    )
    assert ack.calls == [((), {})]
    assert opened[0][0] == "trigger"
    assert opened[0][1]["callback_id"] == "prospect_industries"


def test_failed_dialog_is_reported():
    class Empty:
        def scalars(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

        def close(self):
            return None

    notes: list[tuple[str, str, str]] = []

    def open_view(_trigger, _view):
        raise SlackApiError("failed", {"ok": False})

    open_prospect_command(
        lambda: Empty(),
        Ack(),
        {"text": "industries", "trigger_id": "t", "channel_id": "C1", "user_id": "U1"},
        open_view,
        notes.append,
    )
    assert notes == ["Could not open the dialog. Try again."]


def test_add_pause_and_resume_industry(session):
    added = apply_industry_view(
        session,
        _view(
            _values(
                industry_action=_option("add"),
                industry_name=_text("Widgets"),
                industry_query=_text(""),
            )
        ),
    )
    assert added.errors == {}
    assert (
        added.message
        == "Added industry *Widgets*. Search query: AI use cases in widgets."
    )
    industry = session.scalar(select(Industry).where(Industry.slug == "widgets"))
    assert industry.search_query == "AI use cases in widgets"
    assert industry.is_active is True

    duplicate = apply_industry_view(
        session,
        _view(_values(industry_action=_option("add"), industry_name=_text("widgets"))),
    )
    assert "already exists" in duplicate.errors["industry_name"]
    assert duplicate.message == ""

    paused = apply_industry_view(
        session,
        _view(
            _values(
                industry_action=_option("pause"),
                industry_pick=_option(str(industry.id)),
            )
        ),
    )
    assert paused.errors == {}
    assert paused.message == "Paused industry *Widgets*."
    assert industry.is_active is False

    resumed = apply_industry_view(
        session,
        _view(
            _values(
                industry_action=_option("resume"),
                industry_pick=_option(str(industry.id)),
            )
        ),
    )
    assert resumed.errors == {}
    assert resumed.message == "Resumed industry *Widgets*."
    assert industry.is_active is True

    missing = apply_industry_view(
        session, _view(_values(industry_action=_option("pause")))
    )
    assert missing.errors["industry_action"] == "Choose an industry."
    assert missing.message == ""


def test_source_can_apply_to_one_industry_or_every_industry(session):
    industry = _industry(session)
    other = _industry(session, name="Gadgets")
    url = "https://example.com/widgets"
    saved = apply_source_view(
        session,
        _view(
            _values(
                source_url=_text(url),
                source_scope=_option("one"),
                source_industry=_option(str(industry.id)),
                source_active=_checks("active"),
            )
        ),
    )
    assert saved.errors == {}
    assert saved.message == f"Saved {url} for *Widgets*. The source is active."
    source = session.scalar(select(Source).where(Source.url == url))
    assert source.applies_to_all is False
    assert source.is_active is True
    link = session.scalar(
        select(IndustrySource).where(IndustrySource.source_id == source.id)
    )
    assert link.industry_id == industry.id

    updated = apply_source_view(
        session,
        _view(
            _values(
                source_url=_text(url),
                source_scope=_option("all"),
                source_industry=_option(str(other.id)),
                source_active=_checks(),
            )
        ),
    )
    assert updated.errors == {}
    assert updated.message == f"Updated {url} for every industry. The source is paused."
    assert source.applies_to_all is True
    assert source.is_active is False
    assert (
        session.scalar(
            select(func.count())
            .select_from(IndustrySource)
            .where(IndustrySource.source_id == source.id)
        )
        == 0
    )

    invalid = apply_source_view(
        session,
        _view(_values(source_url=_text("notaurl"), source_scope=_option("all"))),
    )
    assert "http" in invalid.errors["source_url"]
    assert invalid.message == ""


def test_company_handle_and_manual_origin(session):
    industry = _industry(session)
    added = apply_company_view(
        session,
        _view(
            _values(
                company_industry=_option(str(industry.id)),
                company_name=_text("Acme Corp"),
                company_handle=_text("@acme"),
            )
        ),
    )
    assert added.errors == {}
    assert added.message == "Added *Acme Corp* in *Widgets* with X handle @acme."
    company = session.scalar(select(Company).where(Company.normalized_name == "acme"))
    assert company.origin == CompanyOrigin.manual
    assert company.x_handle == "acme"
    assert company.is_active is True

    renamed = apply_company_view(
        session,
        _view(
            _values(
                company_industry=_option(str(industry.id)),
                company_name=_text("Acme"),
                company_handle=_text(""),
            )
        ),
    )
    assert renamed.errors == {}
    assert renamed.message == "Updated *Acme* in *Widgets*: name is *Acme*."
    assert company.name == "Acme"
    assert company.x_handle == "acme"

    invalid_name = apply_company_view(
        session,
        _view(
            _values(
                company_industry=_option(str(industry.id)),
                company_name=_text("unknown"),
                company_handle=_text("acme"),
            )
        ),
    )
    assert invalid_name.errors["company_name"] == "Enter a company name."
    assert invalid_name.message == ""

    invalid_handle = apply_company_view(
        session,
        _view(
            _values(
                company_industry=_option(str(industry.id)),
                company_name=_text("Other"),
                company_handle=_text("bad handle"),
            )
        ),
    )
    assert "X handle" in invalid_handle.errors["company_handle"]
    assert invalid_handle.message == ""

    news = Company(
        industry_id=industry.id,
        name="Beta Inc",
        normalized_name=normalize_company_name("Beta Inc"),
        origin=CompanyOrigin.news,
        mention_count=0,
        distinct_source_count=0,
        is_active=True,
    )
    session.add(news)
    session.flush()
    linked = apply_company_view(
        session,
        _view(
            _values(
                company_industry=_option(str(industry.id)),
                company_name=_text("Beta"),
                company_handle=_text("@beta"),
            )
        ),
    )
    assert linked.errors == {}
    assert linked.message == "Updated *Beta Inc* in *Widgets*: X handle is @beta."
    assert news.name == "Beta Inc"
    assert news.x_handle == "beta"
    assert news.origin == CompanyOrigin.news


def test_schedule_rejects_invalid_cron_before_save(session):
    industry = _industry(session)
    invalid_cron = apply_schedule_view(
        session,
        _view(
            _values(
                schedule_industry=_option(str(industry.id)),
                schedule_job=_option("news"),
                schedule_cron=_text("not a cron"),
                schedule_channel=_conversation("C123"),
                schedule_articles=_text("10"),
                schedule_companies=_text("10"),
                schedule_posts=_text("10"),
                schedule_enabled=_checks("enabled"),
            )
        ),
    )
    assert "five" in invalid_cron.errors["schedule_cron"]
    assert invalid_cron.message == ""
    assert session.scalars(select(Schedule)).all() == []

    invalid_cap = apply_schedule_view(
        session,
        _view(
            _values(
                schedule_industry=_option(str(industry.id)),
                schedule_job=_option("news"),
                schedule_cron=_text("0 8 * * 1"),
                schedule_channel=_conversation("C123"),
                schedule_articles=_text("101"),
                schedule_companies=_text("10"),
                schedule_posts=_text("10"),
                schedule_enabled=_checks("enabled"),
            )
        ),
    )
    assert "100" in invalid_cap.errors["schedule_articles"]
    assert invalid_cap.message == ""


def test_schedule_updates_the_existing_row(session):
    industry = _industry(session)
    first = _schedule_values(industry.id, cron="0 8 * * 1", articles="4")
    created = apply_schedule_view(session, _view(first))
    assert created.errors == {}
    assert created.message == (
        "Saved the news schedule for *Widgets*. It runs `0 8 * * 1` UTC and posts to <#C123>. "
        "Caps are 4 articles, 3 companies, and 2 posts. The schedule is enabled."
    )
    second = _schedule_values(
        industry.id, cron="30 9 * * 1", articles="6", enabled=False
    )
    updated = apply_schedule_view(session, _view(second))
    assert updated.errors == {}
    assert "Updated the news schedule for *Widgets*" in updated.message
    assert "`30 9 * * 1`" in updated.message
    assert "The schedule is paused." in updated.message
    rows = session.scalars(select(Schedule)).all()
    assert len(rows) == 1
    assert rows[0].cron_expression == "30 9 * * 1"
    assert rows[0].max_articles == 6
    assert rows[0].is_enabled is False
    assert rows[0].slack_channel_id == "C123"


def test_prospect_list_includes_ranked_and_slack_companies(session):
    industry = _industry(session)
    session.add(
        Company(
            industry_id=industry.id,
            name="A<B>",
            normalized_name="ab",
            origin=CompanyOrigin.news,
            mention_count=2,
            distinct_source_count=2,
            rank_score=0.82,
            is_active=True,
        )
    )
    session.add(
        Company(
            industry_id=industry.id,
            name="Manual Co",
            normalized_name="manual",
            x_handle="manualco",
            origin=CompanyOrigin.manual,
            mention_count=0,
            distinct_source_count=0,
            is_active=True,
        )
    )
    session.flush()
    rendered = json.dumps(render_prospect_blocks(session, industry))
    assert "A&lt;B&gt;" in rendered
    assert "0.820" in rendered
    assert "Manual Co" in rendered
    assert "@manualco" in rendered
    assert "Added in Slack" in rendered


def test_prospects_submission_updates_the_modal(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    try:
        industry_id = _commit_industry(factory, industry_ids)
        ack = Ack()
        submit_prospects(
            factory, ack, _view(_values(prospects_industry=_option(str(industry_id))))
        )
        view = ack.calls[0][1]["view"]
        assert ack.calls[0][1]["response_action"] == "update"
        assert "No ranked prospects yet." in json.dumps(view)
    finally:
        _cleanup(factory, industry_ids)


def test_run_uses_schedule_caps_and_rejects_a_missing_industry():
    started: list[ManualRun] = []
    ack = Ack()
    submit_run(
        lambda: SimpleNamespace(
            get=lambda *_args, **_kwargs: None,
            close=lambda: None,
            rollback=lambda: None,
        ),
        ack,
        _view({}),
        start_job=started.append,
        notify=lambda *_args: None,
    )
    assert started == []
    assert ack.calls[0][1]["response_action"] == "errors"


def test_run_submission_uses_saved_caps(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    started: list[ManualRun] = []
    notes: list[tuple[str, str, str]] = []
    try:
        industry_id = _commit_industry(factory, industry_ids)
        session = factory()
        try:
            session.add(
                Schedule(
                    industry_id=industry_id,
                    job_type=JobType.news,
                    cron_expression=YEARLY,
                    slack_channel_id="C777",
                    is_enabled=True,
                    max_articles=7,
                    max_companies=8,
                    max_posts=9,
                )
            )
            session.commit()
        finally:
            session.close()
        ack = Ack()
        submit_run(
            factory,
            ack,
            _view(
                _values(
                    run_industry=_option(str(industry_id)), run_job=_option("news")
                ),
                channel="C55",
                user="U9",
            ),
            start_job=started.append,
            notify=lambda channel, user, text: notes.append((channel, user, text)),
        )
        assert ack.calls == [((), {})]
        assert notes[0][0] == "C55"
        assert started[0].max_articles == 7
        assert started[0].max_companies == 8
        assert started[0].max_posts == 9
        assert started[0].channel_id == "C55"
        assert started[0].job_type == JobType.news
    finally:
        _cleanup(factory, industry_ids)


def test_saving_a_schedule_reloads_the_scheduler(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    runner = ProspectScheduler(
        factory, _unused_news_clients(), _unused_signal_clients()
    )
    notes: list[tuple[str, str, str]] = []
    try:
        industry_id = _commit_industry(factory, industry_ids)
        ack = Ack()
        submit_schedule(
            factory,
            runner.reload,
            ack,
            _view(_schedule_values(industry_id, cron="15 8 * * 1", articles="4")),
            notify=lambda channel, user, text: notes.append((channel, user, text)),
        )
        assert ack.calls == [((), {})]
        assert runner.job_count() == 1
        assert notes[0][0] == "C1"
        assert notes[0][1] == "U1"
        assert "Saved the news schedule" in notes[0][2]
        assert "`15 8 * * 1`" in notes[0][2]

        ack = Ack()
        before = len(notes)
        submit_schedule(
            factory,
            runner.reload,
            ack,
            _view(_schedule_values(industry_id, cron="not a cron", articles="4")),
            notify=lambda channel, user, text: notes.append((channel, user, text)),
        )
        assert ack.calls[0][1]["response_action"] == "errors"
        assert runner.job_count() == 1
        assert len(notes) == before

        ack = Ack()
        submit_schedule(
            factory,
            runner.reload,
            ack,
            _view(
                _schedule_values(
                    industry_id, cron="15 8 * * 1", articles="4", enabled=False
                )
            ),
        )
        assert ack.calls == [((), {})]
        assert runner.job_count() == 0
    finally:
        runner.shutdown(wait=False)
        _cleanup(factory, industry_ids)


def test_industry_save_posts_what_changed(migrated_engine):
    factory = _factory(migrated_engine)
    industry_ids: list[uuid.UUID] = []
    notes: list[tuple[str, str, str]] = []
    try:
        ack = Ack()
        submit_industries(
            factory,
            ack,
            _view(
                _values(
                    industry_action=_option("add"),
                    industry_name=_text("Widgets <Co>"),
                    industry_query=_text("AI & widgets"),
                )
            ),
            notify=lambda channel, user, text: notes.append((channel, user, text)),
        )
        assert ack.calls == [((), {})]
        assert notes == [
            (
                "C1",
                "U1",
                "Added industry *Widgets &lt;Co&gt;*. Search query: AI &amp; widgets.",
            )
        ]
        session = factory()
        try:
            industry = session.scalar(
                select(Industry).where(Industry.slug == "widgets-co")
            )
            assert industry is not None
            industry_ids.append(industry.id)
        finally:
            session.close()

        ack = Ack()
        submit_industries(
            factory,
            ack,
            _view(
                _values(
                    industry_action=_option("add"), industry_name=_text("Widgets <Co>")
                )
            ),
            notify=lambda channel, user, text: notes.append((channel, user, text)),
        )
        assert ack.calls[0][1]["response_action"] == "errors"
        assert len(notes) == 1
    finally:
        _cleanup(factory, industry_ids)


def test_bolt_app_registers_the_prospect_command():
    bolt = build_bolt_app(
        bot_token="xoxb-test",
        signing_secret="secret",
        session_factory=lambda: None,
        scheduler=cast(ProspectScheduler, SimpleNamespace()),
        poster=cast(DigestPoster, SimpleNamespace()),
        verify_token=False,
    )
    assert len(bolt._listeners) == 7


def test_slack_events_without_configuration_is_unavailable(migrated_engine):
    factory = _factory(migrated_engine)
    runner = ProspectScheduler(
        factory, _unused_news_clients(), _unused_signal_clients()
    )
    try:
        app = create_app(runner)
        with TestClient(app) as client:
            response = client.post("/slack/events", content=b"{}")
            health = client.get("/health")
        assert response.status_code == 503
        assert health.status_code == 200
    finally:
        runner.shutdown(wait=False)


def _schedule_values(industry_id, *, cron, articles, enabled=True):
    fields = _values(
        schedule_industry=_option(str(industry_id)),
        schedule_job=_option("news"),
        schedule_cron=_text(cron),
        schedule_channel=_conversation("C123"),
        schedule_articles=_text(articles),
        schedule_companies=_text("3"),
        schedule_posts=_text("2"),
    )
    if enabled:
        fields["schedule_enabled"] = {"schedule_enabled": _checks("enabled")}
    return fields


def _unused_news_clients() -> Callable[[], NewsClients]:
    return cast(Callable[[], NewsClients], lambda: SimpleNamespace(close=lambda: None))


def _unused_signal_clients() -> Callable[[], SignalClients]:
    return cast(
        Callable[[], SignalClients], lambda: SimpleNamespace(close=lambda: None)
    )


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _commit_industry(factory, industry_ids):
    session = factory()
    try:
        suffix = uuid.uuid4().hex[:8]
        industry = Industry(
            name=f"Phase5 {suffix}",
            slug=f"phase5-{suffix}",
            search_query="AI use cases in phase5",
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
