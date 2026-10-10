"""Block Kit modals for the /prospect command."""

import json
from dataclasses import dataclass

from slack.text import header_block

CALLBACK_INDUSTRIES = "prospect_industries"
CALLBACK_SOURCES = "prospect_sources"
CALLBACK_COMPANIES = "prospect_companies"
CALLBACK_SCHEDULE = "prospect_schedule"
CALLBACK_RUN = "prospect_run"
CALLBACK_PROSPECTS = "prospect_prospects"

BLOCK_INDUSTRY_ACTION = "industry_action"
BLOCK_INDUSTRY_NAME = "industry_name"
BLOCK_INDUSTRY_QUERY = "industry_query"
BLOCK_INDUSTRY_PICK = "industry_pick"
BLOCK_SOURCE_URL = "source_url"
BLOCK_SOURCE_SCOPE = "source_scope"
BLOCK_SOURCE_INDUSTRY = "source_industry"
BLOCK_SOURCE_ACTIVE = "source_active"
BLOCK_COMPANY_INDUSTRY = "company_industry"
BLOCK_COMPANY_NAME = "company_name"
BLOCK_COMPANY_HANDLE = "company_handle"
BLOCK_SCHEDULE_INDUSTRY = "schedule_industry"
BLOCK_SCHEDULE_JOB = "schedule_job"
BLOCK_SCHEDULE_CRON = "schedule_cron"
BLOCK_SCHEDULE_CHANNEL = "schedule_channel"
BLOCK_SCHEDULE_ARTICLES = "schedule_articles"
BLOCK_SCHEDULE_COMPANIES = "schedule_companies"
BLOCK_SCHEDULE_POSTS = "schedule_posts"
BLOCK_SCHEDULE_ENABLED = "schedule_enabled"
BLOCK_RUN_INDUSTRY = "run_industry"
BLOCK_RUN_JOB = "run_job"
BLOCK_PROSPECTS_INDUSTRY = "prospects_industry"

ACTION_ADD = "add"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
SCOPE_ALL = "all"
SCOPE_ONE = "one"
FLAG_ACTIVE = "active"
FLAG_ENABLED = "enabled"

HELP_TEXT = (
    "*Prospect commands*\n"
    "`/prospect industries` — add, pause, or resume an industry\n"
    "`/prospect sources` — add a news URL for one industry or every industry\n"
    "`/prospect companies` — add a company and an optional X handle\n"
    "`/prospect schedule` — set the cron, channel, and caps\n"
    "`/prospect run` — run news or signals now\n"
    "`/prospect prospects` — show the ranked list"
)


@dataclass(frozen=True)
class IndustryChoice:
    id: str
    name: str
    is_active: bool

    @property
    def label(self) -> str:
        suffix = " (active)" if self.is_active else " (paused)"
        room = 75 - len(suffix)
        name = self.name if len(self.name) <= room else self.name[: room - 1] + "…"
        return f"{name}{suffix}"


def command_metadata(channel_id: str, user_id: str) -> str:
    return json.dumps({"channel_id": channel_id, "user_id": user_id})


def read_metadata(view: dict) -> tuple[str, str]:
    raw = view.get("private_metadata") or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("channel_id") or ""), str(data.get("user_id") or "")


def view_values(view: dict) -> dict:
    state = view.get("state") or {}
    values = state.get("values") or {}
    return values if isinstance(values, dict) else {}


def text_value(values: dict, block_id: str, action_id: str) -> str:
    return str(_element(values, block_id, action_id).get("value") or "").strip()


def selected_value(values: dict, block_id: str, action_id: str) -> str:
    option = _element(values, block_id, action_id).get("selected_option") or {}
    if not isinstance(option, dict):
        return ""
    return str(option.get("value") or "").strip()


def checked(values: dict, block_id: str, action_id: str, flag: str) -> bool:
    selected = _element(values, block_id, action_id).get("selected_options") or []
    if not isinstance(selected, list):
        return False
    return any(
        isinstance(item, dict) and item.get("value") == flag for item in selected
    )


def conversation_value(values: dict, block_id: str, action_id: str) -> str:
    return str(
        _element(values, block_id, action_id).get("selected_conversation") or ""
    ).strip()


def industries_modal(options: list[IndustryChoice], metadata: str) -> dict:
    blocks = [
        _input(
            BLOCK_INDUSTRY_ACTION,
            "Action",
            _radio(
                BLOCK_INDUSTRY_ACTION,
                (
                    (ACTION_ADD, "Add"),
                    (ACTION_PAUSE, "Pause"),
                    (ACTION_RESUME, "Resume"),
                ),
                ACTION_ADD,
            ),
        ),
        _input(
            BLOCK_INDUSTRY_NAME,
            "Name",
            _text_input(BLOCK_INDUSTRY_NAME, "Finance"),
            optional=True,
            hint="Required when adding an industry.",
        ),
        _input(
            BLOCK_INDUSTRY_QUERY,
            "Search query",
            _text_input(BLOCK_INDUSTRY_QUERY, "AI use cases in finance"),
            optional=True,
            hint="Leave blank to use AI use cases in the industry name.",
        ),
    ]
    if options:
        blocks.append(
            _input(
                BLOCK_INDUSTRY_PICK,
                "Industry",
                _industry_select(BLOCK_INDUSTRY_PICK, options),
                optional=True,
                hint="Required when pausing or resuming.",
            )
        )
    return _modal(CALLBACK_INDUSTRIES, "Industries", "Save", blocks, metadata=metadata)


def sources_modal(options: list[IndustryChoice], metadata: str) -> dict:
    blocks = [
        _input(
            BLOCK_SOURCE_URL,
            "News URL",
            _text_input(BLOCK_SOURCE_URL, "https://example.com/news"),
        ),
        _input(
            BLOCK_SOURCE_SCOPE,
            "Applies to",
            _radio(
                BLOCK_SOURCE_SCOPE,
                ((SCOPE_ONE, "One industry"), (SCOPE_ALL, "Every industry")),
                SCOPE_ONE,
            ),
        ),
    ]
    if options:
        blocks.append(
            _input(
                BLOCK_SOURCE_INDUSTRY,
                "Industry",
                _industry_select(BLOCK_SOURCE_INDUSTRY, options),
                optional=True,
                hint="Required when the URL applies to one industry.",
            )
        )
    blocks.append(
        _input(
            BLOCK_SOURCE_ACTIVE,
            "Status",
            _checkbox(BLOCK_SOURCE_ACTIVE, FLAG_ACTIVE, "Active", checked=True),
            optional=True,
        )
    )
    return _modal(CALLBACK_SOURCES, "Sources", "Save", blocks, metadata=metadata)


def companies_modal(options: list[IndustryChoice], metadata: str) -> dict:
    return _modal(
        CALLBACK_COMPANIES,
        "Companies",
        "Save",
        [
            _input(
                BLOCK_COMPANY_INDUSTRY,
                "Industry",
                _industry_select(BLOCK_COMPANY_INDUSTRY, options),
            ),
            _input(BLOCK_COMPANY_NAME, "Name", _text_input(BLOCK_COMPANY_NAME, "Acme")),
            _input(
                BLOCK_COMPANY_HANDLE,
                "X handle",
                _text_input(BLOCK_COMPANY_HANDLE, "@acme"),
                optional=True,
                hint="Leave blank to keep the current handle.",
            ),
        ],
        metadata=metadata,
    )


def schedule_modal(
    options: list[IndustryChoice],
    metadata: str,
    *,
    channel_id: str,
    caps: tuple[int, int, int],
) -> dict:
    articles, companies, posts = caps
    return _modal(
        CALLBACK_SCHEDULE,
        "Schedule",
        "Save",
        [
            _input(
                BLOCK_SCHEDULE_INDUSTRY,
                "Industry",
                _industry_select(BLOCK_SCHEDULE_INDUSTRY, options),
            ),
            _input(
                BLOCK_SCHEDULE_JOB,
                "Job",
                _radio(
                    BLOCK_SCHEDULE_JOB,
                    (("news", "News"), ("signals", "Signals")),
                    "news",
                ),
            ),
            _input(
                BLOCK_SCHEDULE_CRON,
                "Cron",
                _text_input(BLOCK_SCHEDULE_CRON, "0 8 * * 1"),
                hint="Five UTC fields: minute hour day month weekday.",
            ),
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Example: `0 8 * * 1` runs Mondays at 08:00 UTC.",
                    }
                ],
            },
            _input(
                BLOCK_SCHEDULE_CHANNEL,
                "Channel",
                _conversation_select(BLOCK_SCHEDULE_CHANNEL, channel_id),
            ),
            _input(
                BLOCK_SCHEDULE_ARTICLES,
                "Max articles",
                _text_input(
                    BLOCK_SCHEDULE_ARTICLES, str(articles), initial=str(articles)
                ),
            ),
            _input(
                BLOCK_SCHEDULE_COMPANIES,
                "Max companies",
                _text_input(
                    BLOCK_SCHEDULE_COMPANIES, str(companies), initial=str(companies)
                ),
            ),
            _input(
                BLOCK_SCHEDULE_POSTS,
                "Max posts",
                _text_input(BLOCK_SCHEDULE_POSTS, str(posts), initial=str(posts)),
            ),
            _input(
                BLOCK_SCHEDULE_ENABLED,
                "Schedule",
                _checkbox(
                    BLOCK_SCHEDULE_ENABLED, FLAG_ENABLED, "Enabled", checked=True
                ),
                optional=True,
            ),
        ],
        metadata=metadata,
    )


def run_modal(options: list[IndustryChoice], metadata: str) -> dict:
    return _modal(
        CALLBACK_RUN,
        "Run now",
        "Run",
        [
            _input(
                BLOCK_RUN_INDUSTRY,
                "Industry",
                _industry_select(BLOCK_RUN_INDUSTRY, options),
            ),
            _input(
                BLOCK_RUN_JOB,
                "Job",
                _radio(
                    BLOCK_RUN_JOB, (("news", "News"), ("signals", "Signals")), "news"
                ),
            ),
        ],
        metadata=metadata,
    )


def prospects_modal(options: list[IndustryChoice], metadata: str) -> dict:
    return _modal(
        CALLBACK_PROSPECTS,
        "Prospects",
        "Show",
        [
            _input(
                BLOCK_PROSPECTS_INDUSTRY,
                "Industry",
                _industry_select(BLOCK_PROSPECTS_INDUSTRY, options),
            )
        ],
        metadata=metadata,
    )


def prospects_result_modal(industry_name: str, blocks: list[dict]) -> dict:
    body = [header_block(industry_name), *blocks]
    return {
        "type": "modal",
        "callback_id": "prospect_prospects_result",
        "title": _plain("Prospects", limit=24),
        "close": _plain("Close", limit=24),
        "blocks": body,
    }


def modal_for(
    command: str,
    options: list[IndustryChoice],
    metadata: str,
    *,
    channel_id: str,
    caps: tuple[int, int, int],
) -> dict:
    if command == "industries":
        return industries_modal(options, metadata)
    if command == "sources":
        return sources_modal(options, metadata)
    if command == "companies":
        return companies_modal(options, metadata)
    if command == "schedule":
        return schedule_modal(options, metadata, channel_id=channel_id, caps=caps)
    if command == "run":
        return run_modal(options, metadata)
    if command == "prospects":
        return prospects_modal(options, metadata)
    raise ValueError(f"Unknown prospect command: {command}")


def _element(values: dict, block_id: str, action_id: str) -> dict:
    block = values.get(block_id) or {}
    if not isinstance(block, dict):
        return {}
    element = block.get(action_id) or {}
    return element if isinstance(element, dict) else {}


def _plain(text: str, *, limit: int = 75) -> dict[str, str | bool]:
    cleaned = " ".join(text.split()) or " "
    return {"type": "plain_text", "text": cleaned[:limit], "emoji": False}


def _option(text: str, value: str) -> dict:
    return {"text": _plain(text), "value": value[:75]}


def _input(
    block_id: str,
    label: str,
    element: dict,
    *,
    optional: bool = False,
    hint: str | None = None,
) -> dict:
    block: dict = {
        "type": "input",
        "block_id": block_id,
        "label": _plain(label, limit=2000),
        "element": element,
        "optional": optional,
    }
    if hint:
        block["hint"] = _plain(hint, limit=2000)
    return block


def _text_input(
    action_id: str, placeholder: str, *, initial: str | None = None
) -> dict:
    element: dict = {
        "type": "plain_text_input",
        "action_id": action_id,
        "placeholder": _plain(placeholder, limit=150),
    }
    if initial is not None:
        element["initial_value"] = initial[:3000]
    return element


def _radio(action_id: str, choices: tuple[tuple[str, str], ...], initial: str) -> dict:
    options = [_option(label, value) for value, label in choices]
    initial_option = next(item for item in options if item["value"] == initial)
    return {
        "type": "radio_buttons",
        "action_id": action_id,
        "options": options,
        "initial_option": initial_option,
    }


def _checkbox(action_id: str, value: str, label: str, *, checked: bool) -> dict:
    option = _option(label, value)
    element: dict = {"type": "checkboxes", "action_id": action_id, "options": [option]}
    if checked:
        element["initial_options"] = [option]
    return element


def _industry_select(action_id: str, options: list[IndustryChoice]) -> dict:
    return {
        "type": "static_select",
        "action_id": action_id,
        "placeholder": _plain("Choose an industry"),
        "options": [_option(item.label, item.id) for item in options[:100]],
    }


def _conversation_select(action_id: str, channel_id: str) -> dict:
    element: dict = {
        "type": "conversations_select",
        "action_id": action_id,
        "placeholder": _plain("Choose a channel"),
        "filter": {"include": ["public", "private"], "exclude_bot_users": True},
    }
    if channel_id:
        element["initial_conversation"] = channel_id
    return element


def _modal(
    callback_id: str, title: str, submit: str, blocks: list[dict], *, metadata: str
) -> dict:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": _plain(title, limit=24),
        "submit": _plain(submit, limit=24),
        "close": _plain("Cancel", limit=24),
        "private_metadata": metadata,
        "blocks": blocks,
    }
