import json
from datetime import UTC, datetime

import httpx
import pytest

from clients.firecrawl import (
    FirecrawlClient,
    FirecrawlError,
    canonical_url,
    page_from_scrape,
    pages_from_search,
)


def test_canonical_url_strips_tracking_and_www():
    assert (
        canonical_url(
            "https://www.News.Example/acme/?utm_source=newsletter&id=1#section"
        )
        == "https://news.example/acme?id=1"
    )


def test_canonical_url_rejects_empty_and_non_http():
    with pytest.raises(ValueError):
        canonical_url("   ")
    with pytest.raises(ValueError):
        canonical_url("ftp://files.example/report")


def test_pages_from_search_reads_news_markdown():
    pages = pages_from_search(
        {
            "success": True,
            "data": {
                "news": [
                    {
                        "url": "https://news.example/acme",
                        "title": "Acme deploys AI",
                        "markdown": "Acme is piloting AI.",
                        "date": "2026-10-08T00:00:00Z",
                    },
                    {"title": "Missing url"},
                ]
            },
        }
    )
    assert len(pages) == 1
    assert pages[0].title == "Acme deploys AI"
    assert pages[0].markdown == "Acme is piloting AI."
    assert pages[0].published_at == datetime(2026, 10, 8, tzinfo=UTC)


def test_page_from_scrape_uses_metadata_title():
    page = page_from_scrape(
        {
            "success": True,
            "data": {
                "markdown": "# Markets",
                "metadata": {
                    "title": "Markets recap",
                    "sourceURL": "https://trade.example/finance",
                    "publishedTime": "2026-10-01T12:00:00+00:00",
                },
            },
        },
        fallback_url="https://trade.example/finance",
    )
    assert page.title == "Markets recap"
    assert page.url == "https://trade.example/finance"
    assert page.published_at == datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_search_news_posts_news_source_and_markdown_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "news": [
                        {
                            "url": "https://news.example/a",
                            "title": "A",
                            "markdown": "body",
                        }
                    ]
                },
            },
        )

    client = FirecrawlClient(
        "secret",
        http=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.dev"
        ),
    )
    pages = client.search_news("AI use cases in finance", 3)
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {
        "query": "AI use cases in finance",
        "limit": 3,
        "sources": ["news"],
        "scrapeOptions": {"formats": [{"type": "markdown"}]},
    }
    assert pages[0].markdown == "body"


def test_scrape_posts_markdown_format_and_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"success": False, "error": "slow down"})

    client = FirecrawlClient(
        "secret",
        http=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.dev"
        ),
    )
    with pytest.raises(FirecrawlError):
        client.scrape("https://trade.example/finance")
