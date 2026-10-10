"""X API v2 recent-search client for buying-signal posts."""

import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from clients.firecrawl import parse_datetime

X_BASE_URL = "https://api.x.com"
RECENT_SEARCH_PATH = "/2/tweets/search/recent"
MAX_QUERY_CHARS = 512
MAX_RESULTS_PER_REQUEST = 100
MIN_RESULTS_PER_REQUEST = 10
_SIGNAL_CLAUSE = (
    "(AI OR pilot OR RFP OR evaluating OR implementing OR vendor) -is:retweet lang:en"
)
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class XSearchError(RuntimeError):
    """The X recent-search API rejected a query."""


@dataclass(frozen=True)
class XPost:
    id: str
    text: str
    author: str
    url: str
    posted_at: datetime | None
    metadata: dict


def build_recent_search_query(name: str | None, handle: str | None) -> str | None:
    """Build the self-serve recent-search query for one company.

    The shape is `("Name" OR @handle) (AI OR ...) -is:retweet lang:en`.
    A missing name or an unusable handle is left out. Returns None when neither
    term can be searched. The quoted name is shortened so the query stays within
    the 512-character self-serve limit.
    """
    quoted = _quoted_name(name)
    username = normalize_handle(handle)
    terms: list[str] = []
    if quoted:
        terms.append(f'"{quoted}"')
    if username:
        terms.append(f"@{username}")
    if not terms:
        return None
    subject = terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
    query = f"{subject} {_SIGNAL_CLAUSE}"
    if len(query) <= MAX_QUERY_CHARS or not quoted:
        return query if len(query) <= MAX_QUERY_CHARS else None
    overhead = len(query) - len(quoted)
    budget = MAX_QUERY_CHARS - overhead
    if budget < 1:
        if username:
            return f"@{username} {_SIGNAL_CLAUSE}"
        return None
    shortened = f'"{quoted[:budget]}"'
    if username:
        subject = f"({shortened} OR @{username})"
    else:
        subject = shortened
    return f"{subject} {_SIGNAL_CLAUSE}"


def normalize_handle(handle: str | None) -> str | None:
    if handle is None:
        return None
    username = handle.strip().lstrip("@")
    if not _USERNAME.fullmatch(username):
        return None
    return username


def posts_from_search(payload: dict) -> tuple[list[XPost], str | None]:
    users = _users_by_id(payload.get("includes"))
    posts: list[XPost] = []
    for item in payload.get("data") or []:
        post = _post_from_item(item, users)
        if post is not None:
            posts.append(post)
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    token = meta.get("next_token")
    return posts, token if isinstance(token, str) and token else None


class XRecentSearch:
    """Calls GET /2/tweets/search/recent. One client is safe for a single job run."""

    def __init__(
        self,
        bearer_token: str,
        *,
        http: httpx.Client | None = None,
        base_url: str = X_BASE_URL,
    ) -> None:
        self._token = bearer_token
        self._owns_http = http is None
        self._http = http or httpx.Client(base_url=base_url, timeout=30)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "XRecentSearch":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_recent(self, query: str, limit: int) -> list[XPost]:
        if limit < 1:
            return []
        if len(query) > MAX_QUERY_CHARS:
            raise XSearchError(
                f"Query is {len(query)} characters; the limit is {MAX_QUERY_CHARS}"
            )
        collected: list[XPost] = []
        seen: set[str] = set()
        next_token: str | None = None
        while len(collected) < limit:
            remaining = limit - len(collected)
            page_size = min(
                MAX_RESULTS_PER_REQUEST, max(MIN_RESULTS_PER_REQUEST, remaining)
            )
            params: dict[str, str | int] = {
                "query": query,
                "max_results": page_size,
                "tweet.fields": "created_at,author_id,lang",
                "expansions": "author_id",
                "user.fields": "username,name",
            }
            if next_token:
                params["next_token"] = next_token
            payload = self._get(params)
            posts, next_token = posts_from_search(payload)
            for post in posts:
                if post.id in seen:
                    continue
                seen.add(post.id)
                collected.append(post)
                if len(collected) >= limit:
                    break
            if not next_token or not posts:
                break
        return collected

    def _get(self, params: dict[str, str | int]) -> dict:
        response = self._http.get(
            RECENT_SEARCH_PATH,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise XSearchError(
                f"X recent search failed with HTTP {response.status_code}: {_error_detail(response)}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise XSearchError("X recent search returned a non-object response")
        if payload.get("errors") and not payload.get("data"):
            raise XSearchError(_error_detail(response) or "X recent search failed")
        return payload


def _quoted_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = " ".join(name.replace('"', " ").replace("\\", " ").split())
    return cleaned or None


def _users_by_id(includes: object) -> dict[str, dict]:
    if not isinstance(includes, dict):
        return {}
    users: dict[str, dict] = {}
    for user in includes.get("users") or []:
        if isinstance(user, dict) and user.get("id"):
            users[str(user["id"])] = user
    return users


def _post_from_item(item: object, users: dict[str, dict]) -> XPost | None:
    if not isinstance(item, dict):
        return None
    post_id = str(item.get("id") or "").strip()
    text = str(item.get("text") or "").strip()
    if not post_id or not text:
        return None
    author_id = str(item.get("author_id") or "")
    user = users.get(author_id, {})
    username = normalize_handle(
        user.get("username") if isinstance(user.get("username"), str) else None
    )
    display = str(user.get("name") or "").strip()
    author = username or display or author_id or "unknown"
    url = (
        f"https://x.com/{username}/status/{post_id}"
        if username
        else f"https://x.com/i/web/status/{post_id}"
    )
    metadata: dict = {}
    if author_id:
        metadata["author_id"] = author_id
    lang = item.get("lang")
    if isinstance(lang, str) and lang:
        metadata["lang"] = lang
    return XPost(
        id=post_id,
        text=text,
        author=author,
        url=url,
        posted_at=parse_datetime(item.get("created_at")),
        metadata=metadata,
    )


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if not isinstance(payload, dict):
        return response.text[:300]
    detail = payload.get("detail") or payload.get("title")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("message"):
            detail = first["message"]
    return str(detail or "")[:300]
