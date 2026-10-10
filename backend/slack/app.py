"""Bolt app for /prospect commands and the digest poster."""

import logging
import os
import re
import threading
from collections.abc import Callable

from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from slack_sdk.web import WebClient

from jobs.news import DEFAULT_MAX_ARTICLES
from jobs.signals import DEFAULT_MAX_COMPANIES, DEFAULT_MAX_POSTS
from scheduler import ProspectScheduler
from slack.actions import (
    ManualRun,
    apply_company_view,
    apply_industry_view,
    apply_schedule_view,
    apply_source_view,
    industry_choices,
    prepare_manual_run,
    selected_prospect_industry,
)
from slack.digest import DigestPoster, post_run_digest, render_prospect_blocks
from slack.views import (
    BLOCK_COMPANY_NAME,
    BLOCK_INDUSTRY_NAME,
    BLOCK_PROSPECTS_INDUSTRY,
    BLOCK_RUN_INDUSTRY,
    BLOCK_SCHEDULE_CRON,
    BLOCK_SOURCE_URL,
    CALLBACK_COMPANIES,
    CALLBACK_INDUSTRIES,
    CALLBACK_PROSPECTS,
    CALLBACK_RUN,
    CALLBACK_SCHEDULE,
    CALLBACK_SOURCES,
    HELP_TEXT,
    command_metadata,
    modal_for,
    prospects_result_modal,
    read_metadata,
)

logger = logging.getLogger(__name__)

COMMANDS = ("industries", "sources", "companies", "schedule", "run", "prospects")


class SlackWebPoster:
    """Post a digest with the Bolt WebClient."""

    def __init__(self, client: WebClient) -> None:
        self._client = client

    def post(self, channel: str, text: str, blocks: list[dict]) -> None:
        self._client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks or None,
            unfurl_links=False,
            unfurl_media=False,
        )


class SlackRuntime:
    """Socket Mode plus the HTTP handler for one Bolt app."""

    def __init__(
        self, bolt: App, poster: SlackWebPoster, socket: SocketModeHandler | None
    ) -> None:
        self.bolt = bolt
        self.poster = poster
        self.socket = socket
        self.http = SlackRequestHandler(bolt)

    def start(self) -> None:
        if self.socket is not None:
            self.socket.connect()
            logger.info("Slack Socket Mode connected")

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()


def parse_prospect_command(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return "help"
    name = cleaned.split(maxsplit=1)[0].lower()
    if name == "help":
        return "help"
    if name in COMMANDS:
        return name
    return "unknown"


def build_bolt_app(
    *,
    bot_token: str,
    signing_secret: str,
    session_factory: Callable,
    scheduler: ProspectScheduler,
    poster: DigestPoster,
    verify_token: bool = True,
) -> App:
    bolt = App(
        token=bot_token,
        signing_secret=signing_secret,
        token_verification_enabled=verify_token,
    )
    _register_listeners(bolt, session_factory, scheduler, poster)
    return bolt


def build_slack_runtime(scheduler: ProspectScheduler) -> SlackRuntime | None:
    """Build the Bolt app when the bot token and signing secret are set.

    Socket Mode starts only when SLACK_APP_TOKEN is set. The digest handler is
    attached before the caller starts the scheduler.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not bot_token or not signing_secret:
        logger.info("Slack is not configured")
        return None
    bolt = App(token=bot_token, signing_secret=signing_secret)
    poster = SlackWebPoster(bolt.client)
    _register_listeners(bolt, scheduler.session_factory, scheduler, poster)
    scheduler.set_digest_handler(
        lambda run_id: post_run_digest(scheduler.session_factory, run_id, poster)
    )
    socket = SocketModeHandler(bolt, app_token) if app_token else None
    if socket is None:
        logger.info("SLACK_APP_TOKEN is not set; Slack is HTTP only")
    return SlackRuntime(bolt, poster, socket)


def open_prospect_command(
    session_factory: Callable,
    ack: Callable,
    command: dict,
    open_view: Callable[[str, dict], None],
    respond: Callable | None = None,
) -> None:
    name = parse_prospect_command(command.get("text") or "")
    if name == "help":
        ack(HELP_TEXT)
        return
    if name == "unknown":
        ack(f"Unknown command.\n{HELP_TEXT}")
        return
    try:
        options = _load_industries(session_factory)
    except Exception:
        logger.exception("Could not load industries for /prospect")
        ack("Could not load industries. Try again.")
        return
    if name != "industries" and not options:
        ack("Add an industry with `/prospect industries` first.")
        return
    channel_id = str(command.get("channel_id") or "")
    user_id = str(command.get("user_id") or "")
    view = modal_for(
        name,
        options,
        command_metadata(channel_id, user_id),
        channel_id=channel_id,
        caps=(DEFAULT_MAX_ARTICLES, DEFAULT_MAX_COMPANIES, DEFAULT_MAX_POSTS),
    )
    ack()
    try:
        open_view(str(command.get("trigger_id") or ""), view)
    except SlackApiError:
        logger.exception("Could not open the %s dialog", name)
        if respond is not None:
            respond("Could not open the dialog. Try again.")


def submit_industries(
    session_factory: Callable,
    ack: Callable,
    view: dict,
    notify: Callable[[str, str, str], None] | None = None,
) -> None:
    _save(
        session_factory,
        ack,
        lambda session: apply_industry_view(session, view),
        BLOCK_INDUSTRY_NAME,
        view,
        notify,
    )


def submit_sources(
    session_factory: Callable,
    ack: Callable,
    view: dict,
    notify: Callable[[str, str, str], None] | None = None,
) -> None:
    _save(
        session_factory,
        ack,
        lambda session: apply_source_view(session, view),
        BLOCK_SOURCE_URL,
        view,
        notify,
    )


def submit_companies(
    session_factory: Callable,
    ack: Callable,
    view: dict,
    notify: Callable[[str, str, str], None] | None = None,
) -> None:
    _save(
        session_factory,
        ack,
        lambda session: apply_company_view(session, view),
        BLOCK_COMPANY_NAME,
        view,
        notify,
    )


def submit_schedule(
    session_factory: Callable,
    reload_schedules: Callable[[], object],
    ack: Callable,
    view: dict,
    notify: Callable[[str, str, str], None] | None = None,
) -> None:
    _save(
        session_factory,
        ack,
        lambda session: apply_schedule_view(session, view),
        BLOCK_SCHEDULE_CRON,
        view,
        notify,
        on_success=reload_schedules,
    )


def submit_run(
    session_factory: Callable,
    ack: Callable,
    view: dict,
    *,
    start_job: Callable[[ManualRun], None],
    notify: Callable[[str, str, str], None],
) -> None:
    session = session_factory()
    try:
        errors, spec = prepare_manual_run(session, view)
    except Exception:
        session.rollback()
        logger.exception("Could not prepare a manual run")
        ack(
            response_action="errors",
            errors={BLOCK_RUN_INDUSTRY: "Could not start that run. Try again."},
        )
        return
    finally:
        session.close()
    if errors or spec is None:
        ack(
            response_action="errors",
            errors=errors or {BLOCK_RUN_INDUSTRY: "Choose an industry."},
        )
        return
    ack()
    try:
        notify(
            spec.channel_id,
            spec.user_id,
            f"Running {spec.job_type.value} for {spec.industry_name}.",
        )
    except Exception:
        logger.exception("Could not post the run acknowledgement")
    start_job(spec)


def submit_prospects(session_factory: Callable, ack: Callable, view: dict) -> None:
    session = session_factory()
    try:
        errors, industry = selected_prospect_industry(session, view)
        blocks = (
            render_prospect_blocks(session, industry) if industry is not None else []
        )
        name = industry.name if industry is not None else ""
    except Exception:
        session.rollback()
        logger.exception("Could not load prospects")
        ack(
            response_action="errors",
            errors={BLOCK_PROSPECTS_INDUSTRY: "Could not load prospects. Try again."},
        )
        return
    finally:
        session.close()
    if errors or industry is None:
        ack(
            response_action="errors",
            errors=errors or {BLOCK_PROSPECTS_INDUSTRY: "Choose an industry."},
        )
        return
    ack(response_action="update", view=prospects_result_modal(name, blocks))


def start_manual_run(
    scheduler: ProspectScheduler,
    poster: DigestPoster,
    spec: ManualRun,
) -> None:
    """Run a manual job on a background thread and post its digest to the command channel."""

    def work() -> None:
        try:
            scheduler.run_now(
                spec.industry_id,
                spec.job_type,
                max_articles=spec.max_articles,
                max_companies=spec.max_companies,
                max_posts=spec.max_posts,
                on_finished=lambda run_id: post_run_digest(
                    scheduler.session_factory,
                    run_id,
                    poster,
                    channel=spec.channel_id,
                ),
            )
        except Exception as exc:
            logger.exception("Manual %s run failed to start", spec.job_type.value)
            try:
                poster.post(
                    spec.channel_id,
                    f"Could not start the {spec.job_type.value} job: {exc}",
                    [],
                )
            except Exception:
                logger.exception("Could not report the failed manual run")

    threading.Thread(target=work, name="prospect-manual-run", daemon=True).start()


def _register_listeners(
    bolt: App,
    session_factory: Callable,
    scheduler: ProspectScheduler,
    poster: DigestPoster,
) -> None:
    @bolt.command("/prospect")
    def prospect_command(ack, command, client, respond):
        open_prospect_command(
            session_factory,
            ack,
            command,
            lambda trigger_id, view: client.views_open(
                trigger_id=trigger_id, view=view
            ),
            respond,
        )

    @bolt.view(CALLBACK_INDUSTRIES)
    def industries_view(ack, view, client):
        submit_industries(session_factory, ack, view, notify=_confirmation(client))

    @bolt.view(CALLBACK_SOURCES)
    def sources_view(ack, view, client):
        submit_sources(session_factory, ack, view, notify=_confirmation(client))

    @bolt.view(CALLBACK_COMPANIES)
    def companies_view(ack, view, client):
        submit_companies(session_factory, ack, view, notify=_confirmation(client))

    @bolt.view(CALLBACK_SCHEDULE)
    def schedule_view(ack, view, client):
        submit_schedule(
            session_factory, scheduler.reload, ack, view, notify=_confirmation(client)
        )

    @bolt.view(CALLBACK_RUN)
    def run_view(ack, view, client):
        submit_run(
            session_factory,
            ack,
            view,
            start_job=lambda spec: start_manual_run(scheduler, poster, spec),
            notify=lambda channel, user, text: client.chat_postEphemeral(
                channel=channel, user=user, text=text
            ),
        )

    @bolt.view(CALLBACK_PROSPECTS)
    def prospects_view(ack, view):
        submit_prospects(session_factory, ack, view)


def _load_industries(session_factory: Callable) -> list:
    session = session_factory()
    try:
        return industry_choices(session)
    finally:
        session.close()


def _confirmation(client: WebClient) -> Callable[[str, str, str], None]:
    def notify(channel: str, user: str, text: str) -> None:
        if not channel or not text:
            return
        mention = f"<@{user}> " if re.fullmatch(r"[A-Z0-9]+", user or "") else ""
        client.chat_postMessage(
            channel=channel,
            text=f"{mention}{text}",
            unfurl_links=False,
            unfurl_media=False,
        )

    return notify


def _save(
    session_factory: Callable,
    ack: Callable,
    work: Callable,
    error_block: str,
    view: dict,
    notify: Callable[[str, str, str], None] | None = None,
    *,
    on_success: Callable[[], object] | None = None,
) -> None:
    session = session_factory()
    try:
        outcome = work(session)
        if outcome.errors:
            ack(response_action="errors", errors=outcome.errors)
            return
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Slack action failed")
        ack(
            response_action="errors",
            errors={error_block: "Could not save that. Try again."},
        )
        return
    finally:
        session.close()
    if on_success is not None:
        try:
            on_success()
        except Exception:
            logger.exception("Could not reload schedules after saving")
    ack()
    if notify is None or not outcome.message:
        return
    channel_id, user_id = read_metadata(view)
    try:
        notify(channel_id, user_id, outcome.message)
    except Exception:
        logger.exception("Could not post the Slack confirmation")
