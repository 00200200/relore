"""Request and response shapes, versioned from the first commit (section 7).

The moment a second consumer exists, renaming a field breaks a stranger -- so the fields
of section 7 are all present now, including the ones whose *effect* is a later milestone.
``kind`` is the clearest case: it selects section 6's decay regime and section 6.2's trust
floor, neither of which exists yet, and it is accepted and echoed anyway because adding it
later would be a breaking change to a shipped API for no reason.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from ghlore.search.queries import (
    HUMAN_TRUST,
    MACHINE_TRUST,
    MAX_HITS,
    QUERY_KINDS,
    SORTS,
    Hit,
    ThreadView,
)


class SearchRequest(BaseModel):
    """``POST /api/v1/search``.

    ``limit`` is clamped, not validated: a client asking for 500 gets 10 (section 6). The
    signal filters read the tables section 5.3's extraction pass fills.
    """

    query: str = ""
    kind: str | None = Field(default=None, description=f"one of {list(QUERY_KINDS)}")
    trust: str | None = Field(
        default=None,
        description=(
            f"raise the floor to one of {list(HUMAN_TRUST)}, or {MACHINE_TRUST!r} to ask the "
            "separate question of what the deployment's own bots claimed. The server decides "
            "the default and a request can never lower it"
        ),
    )
    repos: list[str] = Field(
        default=[],
        description=(
            "narrow to these repositories. It can only ever *subtract* from the token's "
            "scope (section 11): a name the token cannot see is dropped, not granted, so "
            "asking for one returns nothing rather than an error. Empty means every "
            "repository already in scope"
        ),
    )
    files: list[str] = []
    symbols: list[str] = []
    errors: list[str] = []
    tests: list[str] = []
    labels: list[str] = []
    since: dt.datetime | None = None
    limit: int = MAX_HITS
    sort: str = Field(
        default="relevance",
        description=(
            f"one of {list(SORTS)}. Ordering only -- it reorders the same hits relevance "
            "would have chosen, and never changes which ones they are"
        ),
    )
    compact: bool = Field(
        default=False, description="shorter snippets, no score internals, for a tight budget"
    )
    render: bool = Field(
        default=False,
        description=(
            "also return the exact text the CLI would print, envelope included. What "
            "section 8's 'view as the model sees it' displays; off by default because an "
            "agent reading the JSON would be paying for the same content twice"
        ),
    )
    expand: bool = Field(
        default=True,
        description=(
            "section 6's query expansion: fan the call out into error, test, symbol, file "
            "and free-text legs and merge them. On by default because the plain search "
            "ANDs every content term, which is what makes a pasted traceback match "
            "nothing. Turn it off to ask exactly one question"
        ),
    )


class LabelRequest(BaseModel):
    """``POST /api/v1/label`` -- section 8's relevance judgement, section 10's ground truth."""

    query: str
    repo: str
    number: int
    source_type: str
    verdict: str = Field(description="relevant | not_relevant | decisive")
    kind: str | None = None
    #: The signal filters the judged search actually carried. A label is only
    #: interpretable next to the whole query, and two examples can share the text and
    #: differ only in their `--file` -- folding the labels back into section 10's
    #: evaluation set has no way to tell them apart without this.
    filters: dict[str, list[str]] = {}
    note: str = ""


def hit_json(hit: Hit, *, compact: bool = False) -> dict[str, Any]:
    """One hit, as served.

    ``trust`` and ``age`` are always present and always rendered: age changes what a model
    concludes, authority changes it more (section 6, section 6.2). ``compact`` drops the
    score and its breakdown, which only a person tuning weights has any use for.
    """
    out: dict[str, Any] = {
        "repo": hit.repo,
        "number": hit.number,
        "type": hit.thread_type,
        "title": hit.title,
        "source_type": hit.source_type,
        "url": hit.url,
        "author": hit.author,
        "trust": hit.trust,
        "age": hit.age,
        "snippet": hit.snippet,
    }
    if not compact:
        out["score"] = round(hit.score, 6)
        out["breakdown"] = {k: round(v, 6) for k, v in hit.breakdown.items()}
    return out


def thread_json(view: ThreadView, *, compact: bool = False) -> dict[str, Any]:
    return {
        "repo": view.repo,
        "number": view.number,
        "type": view.thread_type,
        "title": view.title,
        "url": view.url,
        "author": view.author,
        "state": view.state,
        "age": view.age,
        "labels": list(view.labels),
        "body": view.body,
        "files": list(view.files),
        "links": [dict(link) for link in view.links],
        # Named so the cap is visible in the response rather than inferred from a short
        # list: a caller that cannot tell truncation from a quiet thread will read ten
        # comments as the whole argument.
        "comments": [hit_json(hit, compact=compact) for hit in view.comments],
        "comments_returned": len(view.comments),
        "comments_total": view.total_documents,
        # A focus orders the comments and never selects them, so the count that matters is
        # how many carried every term: zero next to ten returned comments says "your
        # question matched nothing, this is the thread in order" rather than "nothing here".
        "focus": view.focus,
        "focus_matched": view.focus_matched,
    }
