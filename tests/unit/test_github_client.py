"""The two pagination limits from section 3, against a fake that actually serves them."""

from __future__ import annotations

import json

import httpx
import pytest
from fake_github import BASE, FakeGitHub

from ghlore.github.client import GitHubClient, GitHubError, PaginationCapReached


def _client(handler, **kwargs) -> GitHubClient:
    return GitHubClient(
        "t",
        base_url=BASE,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE),
        sleep=lambda _s: None,
        **kwargs,
    )


def test_paginate_follows_link_headers_rather_than_counting_pages() -> None:
    """Following the header verbatim is what makes cursor pagination automatic."""
    fake = FakeGitHub(page_size=2)
    for n in range(1, 6):
        fake.add_issue(n, updated_at=f"2026-01-0{n}T00:00:00Z")
    numbers = [i["number"] for i in fake.client().paginate("repos/owner/name/issues")]
    assert numbers == [1, 2, 3, 4, 5]


def test_paginate_respects_a_cap() -> None:
    fake = FakeGitHub(page_size=2)
    for n in range(1, 6):
        fake.add_issue(n, updated_at=f"2026-01-0{n}T00:00:00Z")
    got = list(fake.client().paginate("repos/owner/name/issues", cap=3))
    assert len(got) == 3


def test_the_422_page_limit_is_raised_not_swallowed() -> None:
    """A walk that silently stops here looks like a run that found fewer threads."""
    fake = FakeGitHub(page_size=1)
    for n in range(1, 5):
        fake.add_issue(n, updated_at=f"2026-01-0{n}T00:00:00Z")
    fake.page_cap = 2
    with pytest.raises(PaginationCapReached):
        list(fake.client().paginate("repos/owner/name/issues"))


def test_since_chaining_walks_past_the_slice_cap() -> None:
    """`since` + `direction=asc` does not lift the 30,000-item cap; chaining does."""
    items = [{"id": i, "created_at": f"2026-01-01T00:00:{i:02d}Z"} for i in range(6)]

    def handler(request: httpx.Request) -> httpx.Response:
        since = request.url.params.get("since")
        window = [i for i in items if since is None or i["created_at"] >= since]
        return httpx.Response(
            200,
            content=json.dumps(window[:2]).encode(),
            headers={"Content-Type": "application/json"},
        )

    got = list(
        _client(handler).paginate_since_chained("repos/owner/name/issues/comments", slice_cap=2)
    )
    assert [i["id"] for i in got] == [0, 1, 2, 3, 4, 5], "ids must not repeat across slices"


def test_since_chaining_refuses_to_loop_when_it_cannot_advance() -> None:
    """More than a slice-cap of items sharing one timestamp. Stopping beats looping."""
    same = [{"id": i, "created_at": "2026-01-01T00:00:00Z"} for i in range(4)]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(same[:2]).encode(),
            headers={"Content-Type": "application/json"},
        )

    with pytest.raises(GitHubError, match="cannot advance"):
        list(_client(handler).paginate_since_chained("x", slice_cap=2))


def test_a_spent_rate_limit_waits_for_the_window_rather_than_backing_off() -> None:
    slept: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403,
                content=b'{"message":"rate limit"}',
                headers={
                    "Content-Type": "application/json",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "9999999999",
                },
            )
        return httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})

    client = GitHubClient(
        "t",
        base_url=BASE,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE),
        sleep=slept.append,
    )
    assert client.get_json("x") == {}
    assert len(slept) == 1 and slept[0] > 1.0


def test_a_permission_403_is_not_retried() -> None:
    """A 403 that is not a rate limit will never succeed; retrying just burns time."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            403,
            content=b'{"message":"Resource not accessible"}',
            headers={"Content-Type": "application/json"},
        )

    with pytest.raises(GitHubError):
        _client(handler).get_json("x")
    assert calls["n"] == 1


def test_following_a_link_url_keeps_its_query_string() -> None:
    """The regression that hangs rather than fails.

    httpx replaces a URL's query string when handed a ``params`` argument, so passing an
    empty dict while following ``rel="next"`` strips the page (or cursor) out of it and
    the walk re-reads page 1 forever.
    """
    seen_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        seen_pages.append(page)
        current = int(page or 1)
        headers = {"Content-Type": "application/json"}
        if current < 3:
            headers["Link"] = f'<{BASE}/x?page={current + 1}&per_page=1>; rel="next"'
        return httpx.Response(200, content=json.dumps([{"id": current}]).encode(), headers=headers)

    got = list(_client(handler).paginate("x"))
    assert [i["id"] for i in got] == [1, 2, 3]
    assert seen_pages == [None, "2", "3"]
