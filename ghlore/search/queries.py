"""The query vocabulary, the result caps, and the two things every hit must carry.

Nothing in here touches a database: it is what the backends (section 4.1) agree on and
what the API and the CLI both render. The caps are the interesting part.

It imports one thing from the ingest side -- section 5.3's error normalizer -- because the
error filter is a two-sided normal form and both sides have to use the same function. That
module is stdlib-only for the same reason.

**Result caps are the contract, not a default.** The number behind them is measured: in
the motivating deployment every task session resends its whole conversation each turn at
~40k input tokens, and all seven sessions that ran LLM turns were stopped by a budget
guard rather than by finishing (use-cases section 7d). A retrieval tool that returns
generously does not add context, it subtracts turns from the patch. A client may ask for
less. Never more.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ghlore.ingest.extract import error_query_form

#: Section 6. Requests are clamped to these, never rejected for exceeding them -- a
#: client asking for 50 gets 10, because failing the call helps nobody.
MAX_HITS = 10
MAX_SNIPPET_CHARS = 400
#: ``--compact`` for a client on a tight context budget (section 6). A client may ask
#: for less; never more.
COMPACT_SNIPPET_CHARS = 160
#: Section 6's dedup keeps the best chunk per ``(thread_id, source_type)``; this is the
#: other half of "one chatty thread cannot occupy the whole result page", since a single
#: thread has five source types to spend slots on.
MAX_HITS_PER_THREAD = 3
#: ``thread`` is never returnable in full: a 200-comment thread is the case this exists
#: for. The body gets a longer window than a comment because it is the one document whose
#: opening is reliably worth reading.
MAX_THREAD_COMMENTS = 10
MAX_BODY_CHARS = 800

#: The query kinds of section 6's decay table and section 6.2's trust table. The per-kind
#: **trust floor** is applied (see :func:`trust_policy`); kind-aware **decay** is not, and
#: waits for the weighted ranking in milestone 3.
QUERY_KINDS = ("failure", "precedent", "rationale")

#: Human tiers, weakest first. ``machine`` is deliberately not on this ladder: it is not a
#: lower rung, it is excluded (section 6.2, section 11).
HUMAN_TRUST = ("reported", "authoritative")
MACHINE_TRUST = "machine"

#: How a page is *ordered*. It does not change which documents are on it: section 10.6 is
#: the record of what happens when recency decides selection -- each thread contributes its
#: newest document, which on a merged pull request is the approving review, and a real query
#: came back nine-tenths ``LGTM``. So ``newest`` reorders the same best-per-thread hits that
#: ``relevance`` would have returned; it never picks different ones.
#:
#: Sorting is not decay. Decay (section 6) folds age into the score and can demote a correct
#: old answer; this leaves the score alone and answers a different question -- "what has
#: moved lately" rather than "what answers this".
SORTS = ("relevance", "newest")


class QueryError(ValueError):
    """A request that cannot be served as asked. Distinct from an empty result, which is
    not an error: no results is a successful call with nothing in it, always."""


@dataclass(frozen=True)
class SearchQuery:
    """One ``/search`` request, after validation and clamping.

    ``repos`` is **required and concrete**. Section 11 puts per-token repo scoping in the
    query layer rather than in a handler so that a new endpoint cannot forget it, and an
    empty tuple therefore means *no repository is in scope* -- an empty result, never
    everything. Resolving "this token may see all of them" into a list of names happens
    where the token is read, so nothing below here has a wildcard to mishandle.
    """

    repos: tuple[str, ...]
    text: str = ""
    kind: str | None = None
    trust: str | None = None
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    since: dt.datetime | None = None
    limit: int = MAX_HITS
    compact: bool = False
    sort: str = "relevance"

    def __post_init__(self) -> None:
        # Section 6's error term matches the *normalized* form (section 5.3), so a caller
        # who pastes what their terminal printed has to be put in that form here -- or
        # every `--error` query silently matches nothing, which looks exactly like an
        # index that has nothing to say. `error_query_form` pulls the raised error out of
        # a whole traceback, so pasting the traceback works too.
        if self.errors:
            object.__setattr__(
                self,
                "errors",
                tuple(dict.fromkeys(filter(None, map(error_query_form, self.errors)))),
            )
        if self.kind is not None and self.kind not in QUERY_KINDS:
            raise QueryError(f"unknown query kind {self.kind!r}; one of {list(QUERY_KINDS)}")
        if self.trust is not None and self.trust not in (*HUMAN_TRUST, MACHINE_TRUST):
            raise QueryError(f"unknown trust tier {self.trust!r}")
        if self.since is not None and self.since.tzinfo is None:
            raise QueryError("since must be timezone-aware")
        if self.sort not in SORTS:
            raise QueryError(f"unknown sort {self.sort!r}; one of {list(SORTS)}")
        object.__setattr__(self, "limit", max(1, min(int(self.limit), MAX_HITS)))

    @property
    def terms(self) -> tuple[str, ...]:
        return tokenize(self.text)

    @property
    def snippet_chars(self) -> int:
        return COMPACT_SNIPPET_CHARS if self.compact else MAX_SNIPPET_CHARS

    @property
    def has_signal_filter(self) -> bool:
        return bool(self.files or self.symbols or self.errors or self.tests)


@dataclass(frozen=True)
class Hit:
    """One result. ``trust`` and ``age`` are rendered, not implied (section 6, section 6.2).

    Age changes what a model concludes; authority changes it more. A four-year-old comment
    about a file that still exists may be exactly right about intent and badly wrong about
    the current code, and the cheapest way to get an agent to treat it that way is to tell
    it how old it is. ``[MEMBER]`` versus ``[contributor claim]`` in front of a snippet
    costs four tokens and is the difference between a fact and someone's opinion.
    """

    repo: str
    number: int
    thread_type: str
    title: str
    source_type: str
    url: str | None
    author: str | None
    trust: str
    age: str
    snippet: str
    score: float
    created_at: dt.datetime | None = None
    #: Every term of the score, for the human tuning the weights (section 8). An agent has
    #: no use for *why* something ranked; the person has nothing else. Dropped by
    #: ``--compact``.
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ThreadView:
    """One thread, capped. Section 6: a 200-comment thread must never come back in full."""

    repo: str
    number: int
    thread_type: str
    title: str
    url: str | None
    author: str | None
    state: str | None
    age: str
    labels: tuple[str, ...]
    body: str
    comments: tuple[Hit, ...]
    files: tuple[str, ...] = ()
    links: tuple[dict[str, Any], ...] = ()
    total_documents: int = 0


@dataclass(frozen=True)
class BackendInfo:
    """What engine answered, and what it can do (section 4.1).

    ``status`` prints this so a surprising result set is diagnosable rather than
    mysterious, and section 10's benchmark records it so a recall number measured on
    ``bm25`` is never compared with one measured on ``ts_rank_cd``.

    ``capabilities`` lists what this backend can answer **today**, not which indexes
    happen to exist: a declared capability nothing implements is a lie a reader would
    reasonably act on.
    """

    name: str
    ranking: str
    capabilities: frozenset[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ranking": self.ranking,
            "capabilities": sorted(self.capabilities),
        }


#: Section 6.2's per-query-kind floors. The same query kinds that need different recency
#: need different authority, and the reason is that the corpus holds two different things
#: that look identical in the schema: *reports* ("I hit this error"), which a stranger can
#: make as well as anyone, and *judgements* ("that's intentional"), which are only worth
#: retrieving from someone entitled to make them.
_KIND_FLOOR: dict[str, str] = {
    # The whole value is that someone entitled to decide has decided. An unauthorised
    # answer here is a guess the agent will act on, and it is worse than an empty result.
    "rationale": "authoritative",
    # The opinion needs authority; the artifact does not -- see `merged_pr_is_precedent`.
    "precedent": "authoritative",
    # A stranger's traceback carrying the same exception is real evidence. They are
    # reporting, not adjudicating.
    "failure": "reported",
}


@dataclass(frozen=True)
class TrustPolicy:
    """Which documents a query may see, as data rather than as SQL.

    This module stays free of any database import (see the header), so the policy is
    described here and rendered into a predicate by the backend.
    """

    tiers: tuple[str, ...]
    #: Section 6.2's one disjunction: on a *precedent* query a merged pull request is
    #: precedent whoever wrote it, because merging is the maintainer's act and authority
    #: attaches to the merge rather than to the author. A first-time contributor's merged
    #: change is precedent; their unmerged proposal is not.
    merged_pr_is_precedent: bool = False

    def __contains__(self, trust: str) -> bool:
        return trust in self.tiers


def trust_policy(requested: str | None = None, kind: str | None = None) -> TrustPolicy:
    """Section 6.2's filter: authority is a floor, and the server decides the default.

    Machine-authored documents are **excluded, not down-weighted** (section 6.2,
    section 11). The corpus contains the deployment's own agent's comments; returning one
    as "prior discussion" makes the agent's unreviewed output its own evidence -- a
    poisoning loop with a database in the middle, and one that looks like retrieval
    working. So ``trust=machine`` is not a lowered floor, it is a separate, explicit
    question ("what did the bot claim here?") and the only way to see them; a query kind
    does not change that either way.

    Otherwise the floor is the **higher** of the query kind's default and whatever the
    caller asked for. A caller may raise it and can never lower it, which is why this
    takes the maximum rather than preferring the request.
    """
    if requested == MACHINE_TRUST:
        return TrustPolicy((MACHINE_TRUST,))

    floors = [_KIND_FLOOR.get(kind or "", HUMAN_TRUST[0])]
    if requested is not None:
        floors.append(requested)
    effective = max(HUMAN_TRUST.index(floor) for floor in floors)

    return TrustPolicy(
        tiers=HUMAN_TRUST[effective:],
        # Dropped when the caller raises the floor explicitly: asking for `authoritative`
        # is asking for authoritative documents, not for an exemption from the request.
        merged_pr_is_precedent=(kind == "precedent" and requested is None),
    )


def admissible_trust(requested: str | None = None, kind: str | None = None) -> tuple[str, ...]:
    """The tiers alone, for callers that render the floor rather than filter on it."""
    return trust_policy(requested, kind).tiers


_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> tuple[str, ...]:
    """Split free text into the terms both backends will match on.

    Identifiers keep their underscores and lose everything else, because
    ``to_tsvector('english', ...)`` and FTS5's ``unicode61`` both shred ``snake_case`` and
    ``camelCase`` the same way (section 1). Nothing here is a substitute for the engine's
    own parser -- it exists so a snippet can be centred on a match, and so the SQLite
    backend can build a ``MATCH`` expression out of user text without inheriting FTS5's
    query syntax.
    """
    return tuple(m.group(0) for m in _WORD.finditer(text))


def render_age(then: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """``3d``, ``7mo``, ``4y`` -- rendered, because a bare timestamp makes a model do
    arithmetic it will skip (section 6)."""
    if then is None:
        return "?"
    now = now or dt.datetime.now(dt.timezone.utc)
    seconds = (now - then).total_seconds()
    if seconds < 0:
        return "0h"
    hours = seconds / 3600
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{int(hours)}h"
    days = hours / 24
    if days < 60:
        return f"{int(days)}d"
    months = days / 30.44
    if months < 24:
        return f"{int(months)}mo"
    return f"{days / 365.25:.0f}y"


def snippet(text: str, terms: tuple[str, ...] = (), *, limit: int = MAX_SNIPPET_CHARS) -> str:
    """A window of ``text``, centred on the first matching term.

    Centring rather than taking the head is what makes section 8's question -- "are these
    results any good?" -- answerable by a person reading ten results: a 6000-character
    chunk's opening paragraph usually does not contain the term that matched it.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    start = _first_match(text, terms)
    start = max(0, min(start - limit // 3, len(text) - limit))
    window = text[start : start + limit]
    if start:  # do not begin mid-word
        window = window[window.find(" ") + 1 :] if " " in window else window
    return ("…" if start else "") + window.rstrip() + "…"


def _first_match(text: str, terms: tuple[str, ...]) -> int:
    lowered = text.lower()
    positions = [p for term in terms if (p := lowered.find(term.lower())) >= 0]
    return min(positions) if positions else 0
