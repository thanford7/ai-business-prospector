"""FastAPI process that hosts the prospector scheduler."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response

from db.session import create_session_factory
from scheduler import ProspectScheduler, default_news_clients, default_signal_clients
from slack.app import SlackRuntime, build_slack_runtime

logger = logging.getLogger(__name__)


def load_local_env() -> None:
    """Load a local .env without overriding variables already set."""
    candidates = (
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
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


def build_scheduler() -> ProspectScheduler:
    load_local_env()
    return ProspectScheduler(
        create_session_factory(),
        default_news_clients,
        default_signal_clients,
    )


def create_app(scheduler: ProspectScheduler | None = None) -> FastAPI:
    own_process = scheduler is None
    holder: dict[str, SlackRuntime | None] = {"runtime": None}

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runner = scheduler if scheduler is not None else build_scheduler()
        runtime = build_slack_runtime(runner) if own_process else None
        holder["runtime"] = runtime
        try:
            runner.reload()
            runner.start()
            if runtime is not None:
                runtime.start()
            application.state.scheduler = runner
            application.state.slack = runtime
            logger.info("Scheduler started with %s jobs", runner.job_count())
            yield
        finally:
            if runtime is not None:
                runtime.close()
            runner.shutdown()

    api = FastAPI(title="AI business prospector", lifespan=lifespan)

    @api.get("/health")
    def health() -> dict[str, object]:
        runner: ProspectScheduler = api.state.scheduler
        return {"status": "ok", "schedules": runner.job_count()}

    @api.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        runtime = holder["runtime"]
        if runtime is None:
            return Response(status_code=503, content="Slack is not configured")
        return await runtime.http.handle(request)

    return api


app = create_app()


def main() -> None:
    import uvicorn

    load_local_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run("main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
