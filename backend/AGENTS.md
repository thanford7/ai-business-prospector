# Agent guide

Python 3.11+ service: FastAPI, SQLAlchemy 2, Alembic, Postgres, and pytest. Follow the practices below when changing this repo.

## Imports

Put every import at module level. Group them in this order, with a blank line between groups:

1. Standard library
2. Third-party packages
3. Local packages (`clients`, `db`, `extraction`, `jobs`)

```python
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from db.models import Company
from jobs.names import normalize_company_name
```

Import a name once, at the top, and use it throughout the module. Do this for functions, methods, and fixtures as well.

```python
# Preferred
from langgraph.graph import END, START, StateGraph


def compile_extraction_graph(model: object):
    builder = StateGraph(ExtractionState)
```

```python
# Avoid
def compile_extraction_graph(model: object):
    from langgraph.graph import END, START, StateGraph
```

Keep a heavy or optional import inside a function only when importing it at module level would cause a circular import, and leave a one-line comment naming that cycle.

## Style

- Type public functions and methods. Use `list[str]`, `dict[str, int]`, and `X | None` from the standard library. `Optional` is already used in this repo; match the file you are editing.
- Keep modules focused. Jobs orchestrate, clients talk to one external API, and `db` owns persistence.
- Prefer small functions with explicit arguments over long procedures that close over mutable state.
- Name internal helpers with a leading underscore. Keep public entry points unprefixed (`run_news_job`, `refresh_company_ranks`).
- Use `logging.getLogger(__name__)` for runtime messages. Leave `print` for CLI output in `main`.
- Raise specific exceptions. Let callers see HTTP and database failures; do not swallow them with a bare `except`.
- Read configuration from the environment at the boundary that needs it (`DATABASE_URL`, model names). Do not hard-code secrets.

## Data

- Model tables with SQLAlchemy 2 `Mapped` columns and mixins in `db/base.py`.
- Change the schema only through a new Alembic revision. Keep `upgrade` and `downgrade` paired.
- Open sessions with `create_session_factory`. Commit in the caller that owns the unit of work.

## Tests

- Put tests in `tests/`, named `test_<module>.py`.
- Exercise behavior with plain functions and pytest fixtures. Skip integration tests that need Postgres when neither `DATABASE_URL` nor `TEST_DATABASE_URL` is set. Those tests use a separate database (`TEST_DATABASE_URL`, or the application database name with a `_test` suffix) and do not read or write the application database.
- Pass stand-ins through the protocols already used by the news job (`PageFetcher`, `ArticleClassifier`, `UseCaseExtractor`) when a test should not call a live API.
