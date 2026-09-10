"""``ghlore`` -- the read-only client.

Two verb families, deliberately in one binary:

* **history verbs** (``search``, ``thread``, ``precedent``, ``why``, ``status``) talk to a
  ``ghlored`` over HTTP. They need ``GHLORE_API`` and, if that daemon requires one, a
  token in ``GHLORE_TOKEN``.
* **code verbs** (``map``, ``defs``, ``refs``) run locally against the working tree and
  never touch the network, a database, or a token.

This module must not import :mod:`ghlore.store`, :mod:`ghlore.github` or
:mod:`ghlore.api`. That is asserted by ``tests/unit/test_module_boundary.py`` and it is
what makes "an agent cannot write to the index" a property of the build. See AGENTS.md.
It reaches :mod:`ghlore.render` and :mod:`ghlore.security.untrusted`, which are pure
functions over dictionaries and text -- one renderer, so what an agent reads here and what
a person inspects in the web UI cannot drift.

**No results is exit 0 with an empty result**, always: an agent must not be able to
mistake "nothing in the index" for "the tool is broken". A *transport* failure is a
different thing and does exit non-zero, with a sentence saying which.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from ghlore import __version__
from ghlore.code.api import MissingParser
from ghlore.render import render_search, render_status, render_thread

API_ENV = "GHLORE_API"
TOKEN_ENVS = ("GHLORE_TOKEN", "GHLORE_API_TOKEN")
# The deployed daemon, so an agent that was handed nothing still reaches an index. It is
# on an internal ALB, so this default works on the VPN and nowhere else -- which is the
# right way round: a wrong answer from an empty local daemon is worse than a refusal.
DEFAULT_API = "https://ghlore.huggingface.tech"

_MILESTONE = {"precedent": 4, "why": 4, "map": 2, "defs": 2, "refs": 2}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ghlore", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"ghlore {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--compact", action="store_true", help="trim snippets for a tight context budget"
    )
    p.add_argument(
        "--api",
        default=None,
        help=f"the ghlored base URL (default: {API_ENV}, else {DEFAULT_API})",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    s = sub.add_parser("search", help="search the indexed issue/PR history")
    s.add_argument("query", nargs="?", default="")
    # Repeatable, because the API takes a list of each: a traceback names more than one
    # file, and section 6's expansion will fan a single call out over all of them.
    for flag, help_text in (
        ("--error", "an exception line or normalized message"),
        ("--test", "a test id"),
        ("--file", "a repository path"),
        ("--symbol", "a function or class name"),
        ("--label", "a GitHub label"),
    ):
        s.add_argument(flag, action="append", default=[], help=f"{help_text} (repeatable)")
    s.add_argument("--kind", help="failure | precedent | rationale")
    s.add_argument(
        "--trust",
        help="raise the floor to 'authoritative', or 'machine' to ask what our own bots said",
    )
    s.add_argument("--since", help="ISO timestamp; only documents written after it")
    # Narrows the token's scope, never widens it -- see the server. Repeatable, so it
    # reads like the other filters even though one name is the usual case.
    s.add_argument(
        "--repo",
        action="append",
        default=[],
        dest="repos",
        help="OWNER/NAME; narrow to this repository (repeatable)",
    )
    # Spelled out rather than imported from `search.queries`, like `--kind` above: reaching
    # that module executes `ghlore/search/__init__.py`, which imports SQLAlchemy, and
    # `test_module_boundary` exists to catch exactly that. The server validates the value.
    s.add_argument(
        "--sort",
        choices=("relevance", "newest"),
        default="relevance",
        help="'newest' orders the same hits by date instead of score",
    )
    s.add_argument("--limit", type=int, default=10)
    s.add_argument(
        "--no-expand",
        dest="expand",
        action="store_false",
        help="ask exactly one question instead of section 6's fan-out over the legs",
    )

    t = sub.add_parser("thread", help="one thread, comments selected by relevance")
    t.add_argument("number", type=int)
    t.add_argument("--focus", default="", help="select the comments that answer this")
    t.add_argument("--repo", help="OWNER/NAME; needed when the token can see several")

    pr = sub.add_parser("precedent", help="completed units of work and what they consisted of")
    pr.add_argument("--kind")
    pr.add_argument("--file")

    w = sub.add_parser("why", help="review comments left on this line's code when it was written")
    w.add_argument("location", metavar="PATH:LINE")

    sub.add_parser("status", help="index freshness and coverage")

    m = sub.add_parser("map", help="ranked repo map of the local checkout (no server)")
    m.add_argument("--limit", type=int, default=40)
    m.add_argument("path", nargs="?", default=".")

    d = sub.add_parser("defs", help="definitions in a local file (no server)")
    d.add_argument("path")

    r = sub.add_parser("refs", help="references to a symbol in the local checkout (no server)")
    r.add_argument("symbol")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "search": _search,
        "thread": _thread,
        "status": _status,
        "map": _map,
        "defs": _defs,
        "refs": _refs,
    }.get(args.verb)
    if handler is None:
        raise SystemExit(
            f"ghlore {args.verb}: not implemented yet "
            f"(milestone {_MILESTONE[args.verb]}, the build plan section 13)"
        )
    try:
        return handler(args)
    except MissingParser as exc:
        # An install, not a bug -- so a sentence, never an ImportError traceback (AGENTS.md,
        # section 2). Raised by the registry rather than by an eager `import tree_sitter`
        # check here, because a machine with universal-ctags and no grammar can still
        # answer, and refusing it would be wrong.
        raise SystemExit(f"ghlore: {exc}") from None


# -- history verbs ---------------------------------------------------------


def _search(args: argparse.Namespace) -> int:
    payload = _call(
        args,
        "POST",
        "/api/v1/search",
        body={
            "query": args.query,
            "kind": args.kind,
            "trust": args.trust,
            "files": args.file,
            "symbols": args.symbol,
            "errors": args.error,
            "tests": args.test,
            "labels": args.label,
            "repos": args.repos,
            "since": args.since,
            "limit": args.limit,
            "compact": args.compact,
            "sort": args.sort,
            "expand": args.expand,
            # The server renders it, so the envelope a person inspects in the web UI and
            # the one an agent reads here are the same string from the same code.
            "render": not args.json,
        },
    )
    return _emit(
        args,
        payload,
        lambda: payload.get("rendered") or render_search(payload, compact=args.compact),
    )


def _thread(args: argparse.Namespace) -> int:
    query = {
        "focus": args.focus,
        "compact": str(args.compact).lower(),
        "render": str(not args.json).lower(),
    }
    if args.repo:
        query["repo"] = args.repo
    payload = _call(args, "GET", f"/api/v1/thread/{args.number}", params=query)
    return _emit(
        args,
        payload,
        lambda: payload.get("rendered") or render_thread(payload, compact=args.compact),
    )


def _status(args: argparse.Namespace) -> int:
    payload = _call(args, "GET", "/api/v1/status")
    return _emit(args, payload, lambda: render_status(payload))


def _emit(args: argparse.Namespace, payload: dict[str, Any], text) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(text())
    return 0


# -- code verbs: local, offline, no token ----------------------------------
#
# Deliberately local (section 1). The whole justification for a daemon is that
# `/search/issues` is 30 requests/minute shared across the token; that constraint does not
# exist for code -- a parser over the working tree costs nothing, needs no token, and
# describes *the tree the caller is actually on*, dirty files included. A server-side code
# index knows `main`; the agent is on a feature branch mid-edit.


def _map(args: argparse.Namespace) -> int:
    from ghlore.code.repomap import repo_map

    result = repo_map(args.path, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                {
                    "files": result.files,
                    "definitions": result.definitions,
                    "ranked_by": result.ranked_by,
                    "entries": [vars(e) for e in result.entries],
                },
                indent=2,
            )
        )
        return 0
    print(
        f"{result.definitions} definitions in {result.files} files, "
        f"top {len(result.entries)} by {result.ranked_by}"
    )
    for entry in result.entries:
        callers = f"{entry.callers} callers" if entry.callers else "-"
        print(f"  {entry.qualname:44} {entry.kind:9} {callers:12} {entry.path}:{entry.line}")
    return 0


def _defs(args: argparse.Namespace) -> int:
    from ghlore.code.defs import definitions
    from ghlore.code.walk import read

    source = read(args.path)
    if source is None:
        raise SystemExit(f"ghlore: cannot read {args.path}")
    found = definitions(args.path, source)
    if args.json:
        print(json.dumps([vars(d) for d in found], indent=2))
        return 0
    for definition in found:
        extent = (
            f"{definition.start_line}-{definition.end_line}"
            if definition.end_line
            else str(definition.start_line)
        )
        print(f"{extent:12} {definition.kind:9} {definition.qualname}")
    return 0


def _refs(args: argparse.Namespace) -> int:
    from ghlore.code.refs import references

    result = references(".", args.symbol)
    if args.json:
        print(
            json.dumps(
                {
                    "searched": result.searched,
                    "unsupported": list(result.unsupported),
                    "hits": [vars(h) for h in result.hits],
                },
                indent=2,
            )
        )
        return 0
    for hit in result.hits:
        print(f"{hit.path}:{hit.line}")
    print(f"-- {len(result.hits)} references in {result.searched} files")
    if result.unsupported:
        # Section 9's rule 2: say so and exit 0, rather than return an empty list a caller
        # would read as "nothing calls this".
        print(
            f"   ({', '.join(result.unsupported)} offers no reference tier, so files it "
            "claims were not searched)"
        )
    return 0


# -- transport -------------------------------------------------------------


def _call(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One request, with the failure modes told apart.

    A 404 or an empty index is data; a refused connection, a 401 and a 429 are not, and
    each gets its own sentence. Blurring them is how an agent decides the project has no
    history when the daemon is simply down.
    """
    import httpx

    base = args.api or os.environ.get(API_ENV) or DEFAULT_API
    headers = {"accept": "application/json"}
    # The *name* too, not just the value: a 401 has to say which variable was rejected,
    # and there are two it could have come from.
    sent_from = next((e for e in TOKEN_ENVS if os.environ.get(e)), None)
    if sent_from:
        headers["authorization"] = f"Bearer {os.environ[sent_from]}"

    try:
        response = httpx.request(
            method,
            f"{base.rstrip('/')}{path}",
            json=body,
            params=params,
            headers=headers,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"ghlore: cannot reach {base} ({exc.__class__.__name__}). "
            f"The default is the deployed daemon, which is reachable on the VPN only — "
            f"connect, or set {API_ENV} to your own, or start `ghlored serve`."
        ) from None

    if response.status_code == 401:
        # Two failures, not one. Telling an operator who has already exported a token to
        # export a token sends them looking for a typo in the value, when the usual cause
        # is that the value is fine and belongs to a different daemon.
        if sent_from:
            raise SystemExit(
                f"ghlore: {base} rejected the token in {sent_from}. A token is only valid "
                f"on the daemon whose GHLORE_API_TOKENS lists it, so one minted for "
                f"another deployment will not work here."
            )
        raise SystemExit(
            f"ghlore: {base} requires a token. Set {TOKEN_ENVS[0]} to one scoped to your "
            "repositories."
        )
    if response.status_code == 429:
        detail = _detail(response)
        raise SystemExit(f"ghlore: rate limited ({detail}). Retry after the header says to.")
    if response.status_code >= 400:
        raise SystemExit(
            f"ghlore: {base}{path} returned {response.status_code}: {_detail(response)}"
        )
    return dict(response.json())


def _detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return detail if isinstance(detail, str) else json.dumps(detail)


if __name__ == "__main__":
    sys.exit(main())
