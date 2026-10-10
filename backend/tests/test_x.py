from datetime import UTC, datetime
from typing import TypedDict

import httpx
import pytest

from clients.x import (
    MAX_QUERY_CHARS,
    XRecentSearch,
    XSearchError,
    build_recent_search_query,
    normalize_handle,
    posts_from_search,
)


def test_query_uses_name_and_handle():
    query = build_recent_search_query("Acme Inc.", "@acme")
    assert query == (
        '("Acme Inc." OR @acme) (AI OR pilot OR RFP OR evaluating OR implementing OR vendor) '
        "-is:retweet lang:en"
    )
    assert len(query) <= MAX_QUERY_CHARS


def test_query_drops_an_unusable_handle_and_quotes_the_name():
    query = build_recent_search_query('Acme "Labs"', "not a handle")
    assert query is not None
    assert query.startswith('"Acme Labs" ')
    assert "@" not in query


def test_query_returns_none_without_a_name_or_handle():
    assert build_recent_search_query("   ", None) is None
    assert normalize_handle(" @Acme_1 ") == "Acme_1"


def test_long_name_is_shortened_to_the_query_limit():
    query = build_recent_search_query("A" * 600, "acme")
    assert query is not None
    assert len(query) == MAX_QUERY_CHARS
    assert "@acme" in query
    assert query.endswith("-is:retweet lang:en")


def test_posts_from_search_join_the_author():
    posts, token = posts_from_search(
        {
            "data": [
                {
                    "id": "100",
                    "text": "Acme issued an RFP for an AI vendor.",
                    "author_id": "9",
                    "created_at": "2026-10-08T00:00:00.000Z",
                    "lang": "en",
                },
                {"id": "101", "text": "   "},
            ],
            "includes": {"users": [{"id": "9", "username": "acme", "name": "Acme"}]},
            "meta": {"result_count": 1, "next_token": "next"},
        }
    )
    assert token == "next"
    assert len(posts) == 1
    assert posts[0].author == "acme"
    assert posts[0].url == "https://x.com/acme/status/100"
    assert posts[0].posted_at == datetime(2026, 10, 8, tzinfo=UTC)
    assert posts[0].metadata == {"author_id": "9", "lang": "en"}


class _SeenRequest(TypedDict):
    auth: str
    params: dict[str, str]


def test_search_recent_requests_recent_search_and_stops_at_the_cap():
    seen: list[_SeenRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "auth": request.headers["Authorization"],
                "params": dict(request.url.params),
            }
        )
        if "next_token" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": str(index),
                            "text": f"post {index}",
                            "author_id": "9",
                            "lang": "en",
                        }
                        for index in range(10)
                    ],
                    "includes": {
                        "users": [{"id": "9", "username": "acme", "name": "Acme"}]
                    },
                    "meta": {"result_count": 10, "next_token": "page-2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "10", "text": "post 10", "author_id": "9", "lang": "en"}
                ],
                "includes": {"users": [{"id": "9", "username": "acme"}]},
                "meta": {"result_count": 1},
            },
        )

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.x.com"
    )
    with XRecentSearch("test-token", http=http) as client:
        posts = client.search_recent('("Acme" OR @acme) lang:en', limit=11)

    assert [post.id for post in posts] == [str(index) for index in range(11)]
    assert seen[0]["auth"] == "Bearer test-token"
    assert seen[0]["params"]["query"] == '("Acme" OR @acme) lang:en'
    assert seen[0]["params"]["max_results"] == "11"
    assert seen[0]["params"]["tweet.fields"] == "created_at,author_id,lang"
    assert seen[0]["params"]["expansions"] == "author_id"
    assert seen[1]["params"]["next_token"] == "page-2"
    assert seen[1]["params"]["max_results"] == "10"


def test_search_recent_uses_the_api_minimum_then_returns_the_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["max_results"] == "10"
        body = {
            "data": [
                {"id": str(index), "text": f"post {index}"} for index in range(10)
            ],
            "meta": {"result_count": 10},
        }
        return httpx.Response(200, json=body)

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.x.com"
    )
    posts = XRecentSearch("test-token", http=http).search_recent("acme", limit=3)
    assert [post.id for post in posts] == ["0", "1", "2"]
    assert posts[0].url == "https://x.com/i/web/status/0"


def test_search_recent_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"title": "Invalid Request", "detail": "query too long"}
        )

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.x.com"
    )
    with pytest.raises(XSearchError, match="HTTP 400"):
        XRecentSearch("test-token", http=http).search_recent("acme", limit=10)


def test_search_recent_treats_an_empty_page_as_no_posts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.x.com"
    )
    assert XRecentSearch("test-token", http=http).search_recent("acme", limit=10) == []
