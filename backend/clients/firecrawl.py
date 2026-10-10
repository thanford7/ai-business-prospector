"""Firecrawl REST client for news search and page scrape."""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class FirecrawlError(RuntimeError):
    """The Firecrawl API rejected a search or scrape call."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    markdown: str
    published_at: datetime | None


def canonical_url(url: str) -> str:
    """Return a stable URL for dedup: no fragment, no tracking query, no trailing slash."""
    raw = url.strip()
    if not raw:
        raise ValueError("URL is empty")
    parsed = urlsplit(raw)
    if not parsed.netloc and not parsed.path:
        raise ValueError(f"URL is not usable: {url}")
    scheme = (parsed.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme is not http or https: {url}")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    if not host:
        raise ValueError(f"URL has no host: {url}")
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_KEYS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = "&".join(f"{key}={value}" for key, value in query_pairs)
    return urlunsplit((scheme, host, path, query, ""))


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def pages_from_search(payload: dict) -> list[FetchedPage]:
    data = payload.get("data") or {}
    items = data if isinstance(data, list) else data.get("news") or []
    pages: list[FetchedPage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        page = _page_from_search_item(item)
        if page is not None:
            pages.append(page)
    return pages


def page_from_scrape(payload: dict, fallback_url: str) -> FetchedPage:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    url = metadata.get("sourceURL") or metadata.get("url") or fallback_url
    title = metadata.get("title") or metadata.get("ogTitle") or url
    published = parse_datetime(
        metadata.get("publishedTime") or metadata.get("published_at")
    )
    return FetchedPage(
        url=str(url),
        title=str(title).strip() or str(url),
        markdown=str(data.get("markdown") or ""),
        published_at=published,
    )


class FirecrawlClient:
    """Calls POST /v2/search and POST /v2/scrape. One client is safe for a single job run."""

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        base_url: str = FIRECRAWL_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._owns_http = http is None
        self._http = http or httpx.Client(base_url=base_url, timeout=120)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "FirecrawlClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search_news(self, query: str, limit: int) -> list[FetchedPage]:
        if limit < 1:
            return []
        payload = self._post(
            "/v2/search",
            {
                "query": query,
                "limit": limit,
                "sources": ["news"],
                "scrapeOptions": {"formats": [{"type": "markdown"}]},
            },
        )
        return pages_from_search(payload)

    def scrape(self, url: str) -> FetchedPage:
        payload = self._post(
            "/v2/scrape",
            {"url": url, "formats": [{"type": "markdown"}]},
        )
        return page_from_scrape(payload, fallback_url=url)

    def _post(self, path: str, body: dict) -> dict:
        response = self._http.post(
            path,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FirecrawlError(
                f"Firecrawl {path} failed with HTTP {response.status_code}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise FirecrawlError(f"Firecrawl {path} returned a non-object response")
        if payload.get("success") is False:
            raise FirecrawlError(
                str(payload.get("error") or f"Firecrawl {path} failed")
            )
        return payload


def _page_from_search_item(item: dict) -> FetchedPage | None:
    raw_metadata = item.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    url = item.get("url") or metadata.get("sourceURL") or metadata.get("url")
    if not url:
        return None
    title = item.get("title") or metadata.get("title") or url
    published = parse_datetime(
        item.get("date") or item.get("publishedDate") or metadata.get("publishedTime")
    )
    markdown = item.get("markdown") or ""
    return FetchedPage(
        url=str(url),
        title=str(title).strip() or str(url),
        markdown=str(markdown),
        published_at=published,
    )
