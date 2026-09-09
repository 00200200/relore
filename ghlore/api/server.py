"""``ghlored serve`` -- the HTTP JSON API and the page that calls it (section 7, section 8).

Three properties are enforced here rather than documented and hoped for.

**Read-only.** The engine is opened read-only on both dialects (section 11): a
``SELECT``-only role on Postgres, a ``mode=ro`` URI on SQLite. There is no write path for
anyone (section 11's *why*), and the one endpoint that writes -- section 8's relevance
labelling -- writes a JSONL file that is never indexed and never retrieved, so it cannot
become project memory the next agent reads as evidence.

**The untrusted-content envelope is not optional.** Every response goes through
:func:`ghlore.security.untrusted.scrub_tree` on the way out, applied to the whole payload
rather than to a list of content fields, so adding an endpoint cannot forget it. An
unknown client cannot be assumed to add it.

**Scope is resolved before the query layer sees it.** A token's repositories arrive as
concrete names (:mod:`ghlore.api.tokens`), and every query carries them.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import Engine, select

from ghlore import __version__
from ghlore.api import ui
from ghlore.api.schemas import LabelRequest, SearchRequest, hit_json, thread_json
from ghlore.api.tokens import LABEL_SCOPE, Authenticator, AuthError, RateLimited, Token
from ghlore.render import render_search, render_thread
from ghlore.search import QueryError, SearchQuery, expand, open_backend, search_expanded
from ghlore.search.queries import HUMAN_TRUST, admissible_trust
from ghlore.security.untrusted import NOTICE, scrub_tree
from ghlore.store import schema as s
from ghlore.store.dialect import is_deployment_grade
from ghlore.store.migrations import describe
from ghlore.store.repository import index_summary

log = logging.getLogger(__name__)

LABELS_ENV = "GHLORE_LABELS_PATH"
VERDICTS = ("relevant", "not_relevant", "decisive")


class Deps:
    """What the handlers need, built once. Not a framework choice -- the point is that the
    read-only engine and the backend are created at startup, so a request cannot open a
    writable connection by accident."""

    def __init__(
        self,
        engine: Engine,
        *,
        auth: Authenticator | None = None,
        labels_path: Path | None = None,
    ) -> None:
        self.engine = engine
        self.backend = open_backend(engine)
        self.auth = auth or Authenticator()
        self.labels_path = labels_path
        self.counters: Counter[str] = Counter()

    def indexed_repos(self) -> tuple[str, ...]:
        with self.engine.connect() as conn:
            return tuple(
                str(repo)
                for (repo,) in conn.execute(
                    select(s.threads.c.repo.distinct()).order_by(s.threads.c.repo)
                )
            )

    def status(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            summary = index_summary(conn)
        return {
            "version": __version__,
            "backend": self.backend.info().as_dict(),
            "schema": describe(self.engine),
            **summary,
        }


def caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> Token:
    """Authenticate and charge one request. Both limits are per token (section 7).

    Module level, reading its :class:`Deps` off the app rather than off a closure, so the
    annotation below resolves under postponed evaluation -- and so the dependency is one
    function rather than one per app.
    """
    deps: Deps = request.app.state.deps
    try:
        token = deps.auth.authenticate(authorization)
    except AuthError as exc:
        deps.counters["denied"] += 1
        raise HTTPException(status_code=401, detail=str(exc)) from None
    try:
        deps.auth.charge(token)
    except RateLimited as exc:
        deps.counters["rate_limited"] += 1
        raise HTTPException(
            status_code=429,
            detail={"limit": exc.which, "of": exc.limit, "retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    deps.counters[f"request:{request.url.path}"] += 1
    return token


#: Every handler takes this, so a route cannot be added without being authenticated and
#: charged: leaving it out is a missing argument, not a silently open endpoint.
Caller = Annotated[Token, Depends(caller)]


def build_app(
    engine: Engine,
    *,
    auth: Authenticator | None = None,
    labels_path: Path | None = None,
) -> FastAPI:
    deps = Deps(engine, auth=auth, labels_path=labels_path)
    app = FastAPI(title="ghlore", version=__version__, docs_url="/api/docs")
    app.state.deps = deps

    @app.post("/api/v1/search")
    def search(body: SearchRequest, token: Caller) -> Response:
        repos = token.scope(deps.indexed_repos())
        try:
            query = SearchQuery(
                repos=repos,
                text=body.query,
                kind=body.kind,
                trust=body.trust,
                files=tuple(body.files),
                symbols=tuple(body.symbols),
                errors=tuple(body.errors),
                tests=tuple(body.tests),
                labels=tuple(body.labels),
                since=body.since,
                limit=body.limit,
                compact=body.compact,
            )
        except QueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        legs = expand(query) if body.expand else ()
        hits = search_expanded(deps.backend, query) if body.expand else deps.backend.search(query)
        payload = {
            "notice": NOTICE,
            "backend": deps.backend.info().as_dict(),
            "query": {
                "text": query.text,
                "kind": query.kind,
                # Named so an empty result is diagnosable rather than mysterious: a
                # raised floor and a genuinely quiet corpus look identical otherwise.
                "trust_floor": list(admissible_trust(query.trust, query.kind)),
                "limit": query.limit,
                "repos": list(repos),
                # The same reason as the floor: with expansion on, the question actually
                # asked is not the string the caller sent, and an empty result is only
                # diagnosable if the legs are visible (section 6).
                "legs": [{"leg": leg.name, "term": leg.term} for leg in legs],
            },
            "count": len(hits),
            "hits": [hit_json(hit, compact=body.compact) for hit in hits],
        }
        return _json(
            payload,
            render=(lambda scrubbed: render_search(scrubbed, compact=body.compact))
            if body.render
            else None,
        )

    @app.get("/api/v1/thread/{number}")
    def thread(
        number: int,
        token: Caller,
        repo: str | None = None,
        focus: str = "",
        compact: bool = False,
        render: bool = False,
    ) -> Response:
        """One thread, capped (section 6). ``repo`` is optional only while a token's scope
        holds exactly one repository -- with several, a bare number is ambiguous and
        guessing would silently answer about the wrong project."""
        repos = token.scope(deps.indexed_repos())
        if repo is None:
            if len(repos) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"pass repo=: this token can see {list(repos)}",
                )
            repo = repos[0]
        if repo not in repos:
            # 404, not 403: whether a repository exists is not this token's business.
            raise HTTPException(status_code=404, detail=f"{repo}#{number} not found")
        view = deps.backend.thread(repo, number, focus=focus)
        if view is None:
            raise HTTPException(status_code=404, detail=f"{repo}#{number} not found")
        payload = {"notice": NOTICE, "thread": thread_json(view, compact=compact)}
        return _json(
            payload,
            render=(lambda scrubbed: render_thread(scrubbed, compact=compact)) if render else None,
        )

    @app.post("/api/v1/precedent")
    def precedent(token: Caller) -> Response:
        return _json(
            {
                "detail": "precedent is milestone 4 (the build plan section 13)",
                "precedents": [],
            },
            status=501,
        )

    @app.get("/api/v1/status")
    def status(token: Caller) -> Response:
        return _json({**deps.status(), "usage": deps.auth.usage(token)})

    @app.post("/api/v1/label")
    def label(body: LabelRequest, token: Caller) -> Response:
        """Section 8's relevance judgement, appended to JSONL.

        This is the only request in the API that writes, and it is not a write to the
        index: section 10's evaluation set is a byproduct of somebody using the UI rather
        than a chore nobody schedules, and it has to land somewhere. It is gated on a
        token scope and on a configured path, it never reaches ``documents``, and nothing
        retrieves it -- so invariant 3 holds: no agent conclusion becomes project memory.
        """
        if deps.labels_path is None:
            raise HTTPException(
                status_code=503, detail=f"labelling is off; start ghlored serve with {LABELS_ENV}"
            )
        if not token.may(LABEL_SCOPE):
            raise HTTPException(status_code=403, detail="this token may not label")
        if body.verdict not in VERDICTS:
            raise HTTPException(status_code=400, detail=f"verdict must be one of {list(VERDICTS)}")
        row = {
            "labelled_at": _now(),
            "query": body.query,
            "kind": body.kind,
            "repo": body.repo,
            "number": body.number,
            "source_type": body.source_type,
            "filters": {k: v for k, v in body.filters.items() if v},
            "verdict": body.verdict,
            "note": body.note,
            # Section 10 refuses to compare across backends, and section 6.2 warns that a
            # re-resolved repo_authority can legitimately move documents between tiers.
            # Both mean a label is only interpretable next to what produced it.
            "backend": deps.backend.info().as_dict(),
        }
        with deps.labels_path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        return _json({"labelled": row})

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> Response:
        """Unauthenticated on purpose: it carries counts, never content."""
        return PlainTextResponse(_prometheus(deps), media_type="text/plain; version=0.0.4")

    @app.get("/", response_class=HTMLResponse)
    def home() -> Response:
        return HTMLResponse(ui.page())

    return app


def _json(
    payload: dict[str, Any],
    *,
    render: Callable[[dict[str, Any]], str] | None = None,
    status: int = 200,
) -> Response:
    """Every response body, scrubbed (section 11), and the envelope applied last.

    The scrub is here so it is structurally impossible for a handler to skip: it walks the
    whole tree, so a field added tomorrow is covered by the code written today.

    That is also why ``render`` is a callback rather than a field the handler fills in.
    Rendering first and scrubbing afterwards strips the envelope's *own* delimiters out of
    the rendered text -- the scrub cannot tell ours from an attacker's, which is the whole
    point of scrubbing them -- and section 8's "view as the model sees it" would then show
    a page with the one layer a person is inspecting quietly removed. So: scrub the
    content, then wrap it. Nothing runs after the envelope.
    """
    body = scrub_tree(payload)
    if render is not None:
        body["rendered"] = render(body)
    return JSONResponse(body, status_code=status)


def _now() -> str:
    from ghlore.store.dialect import utcnow

    return utcnow().isoformat()


def _prometheus(deps: Deps) -> str:
    state = deps.status()
    lines = [
        "# HELP ghlore_threads Indexed threads.",
        "# TYPE ghlore_threads gauge",
        f"ghlore_threads {state['threads']}",
        "# HELP ghlore_documents Indexed documents.",
        "# TYPE ghlore_documents gauge",
        f"ghlore_documents {state['documents']}",
        "# HELP ghlore_raw_objects Staged raw API objects.",
        "# TYPE ghlore_raw_objects gauge",
        f"ghlore_raw_objects {state['raw_objects']}",
        "# HELP ghlore_requests_total Requests served, by path.",
        "# TYPE ghlore_requests_total counter",
    ]
    for key, value in sorted(deps.counters.items()):
        if key.startswith("request:"):
            lines.append(f'ghlore_requests_total{{path="{key[8:]}"}} {value}')
    lines += [
        "# HELP ghlore_denied_total Requests rejected by authentication.",
        "# TYPE ghlore_denied_total counter",
        f"ghlore_denied_total {deps.counters['denied']}",
        "# HELP ghlore_rate_limited_total Requests rejected by a rate limit.",
        "# TYPE ghlore_rate_limited_total counter",
        f"ghlore_rate_limited_total {deps.counters['rate_limited']}",
        "# HELP ghlore_pass_high_water_seconds Per-pass high-water mark (section 5.2).",
        "# TYPE ghlore_pass_high_water_seconds gauge",
    ]
    for row in state["passes"]:
        if row["high_water"]:
            import datetime as dt

            moment = dt.datetime.fromisoformat(row["high_water"])
            lines.append(
                f'ghlore_pass_high_water_seconds{{repo="{row["repo"]}",pass="{row["pass"]}"}} '
                f"{moment.timestamp():.0f}"
            )
    return "\n".join(lines) + "\n"


def serve(url: str, *, host: str, port: int, allow_sqlite: bool) -> int:
    """Section 4.1's second guardrail, and the loopback rule.

    ``ghlored serve`` refuses a SQLite URL without ``--allow-sqlite``: a laptop index
    served to a team is how "the ranking is bad" becomes unfalsifiable. And a daemon with
    no tokens configured refuses to bind anywhere but loopback -- a laptop should not have
    to mint a token to read its own index, but an open index on a network is not a
    default anyone chose.
    """
    import uvicorn

    from ghlore.store.dialect import make_engine

    engine = make_engine(url, read_only=True)
    if not is_deployment_grade(engine) and not allow_sqlite:
        engine.dispose()
        raise SystemExit(
            "ghlored serve: refusing to serve a SQLite index. Postgres is the only "
            "supported deployment (the build plan section 4.1). Pass --allow-sqlite "
            "for local development."
        )

    auth = Authenticator.from_env()
    if auth.open and host not in ("127.0.0.1", "::1", "localhost"):
        engine.dispose()
        raise SystemExit(
            f"ghlored serve: no tokens configured, so refusing to bind {host}. "
            "Set GHLORE_API_TOKENS, or bind 127.0.0.1."
        )
    if auth.open:
        log.warning("no GHLORE_API_TOKENS configured: anyone who can reach %s may read", host)

    labels = os.environ.get(LABELS_ENV)
    app = build_app(engine, auth=auth, labels_path=Path(labels) if labels else None)
    trust_note = ", ".join(HUMAN_TRUST)
    log.info("serving %s on %s:%s (default trust tiers: %s)", url, host, port, trust_note)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
