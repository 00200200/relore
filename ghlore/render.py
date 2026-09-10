"""Turning an API response into the text a model reads. One implementation, two callers.

The CLI prints this, and section 8's "view as the model sees it" shows the *same* string --
including the untrusted envelope and its delimiter scrubbing -- so that a formatting or
injection bug is caught by a person reading it rather than by an agent meeting it mid-task.
Two renderers would drift, and the one that drifted would be the one nobody was looking at.

**Everything here takes plain dictionaries -- the JSON shapes of section 7 -- and imports
nothing but the standard library.** That is what lets it live on both sides of the
boundary: :mod:`ghlore.cli` may not reach a database driver or a web server (AGENTS.md
invariant 1), and importing :mod:`ghlore.search` would pull SQLAlchemy in through the
package's ``__init__``. Dicts in, text out.
"""

from __future__ import annotations

from typing import Any

from ghlore.security.untrusted import envelope

#: What each tier is called in front of a snippet. Four tokens, and the difference between
#: a fact and someone's opinion (section 6.2).
TRUST_LABEL = {
    "authoritative": "authoritative",
    "reported": "contributor claim",
    "machine": "MACHINE — our own bot, not evidence",
}


def render_search(payload: dict[str, Any], *, compact: bool = False) -> str:
    """The result page, wrapped in the envelope.

    Empty is not an error: no hits renders as a sentence saying so, still enveloped, still
    exit 0.
    """
    query = payload.get("query") or {}
    hits = payload.get("hits") or []
    backend = payload.get("backend") or {}
    header = [
        f"{len(hits)} hit{'' if len(hits) == 1 else 's'}"
        + (f" for {query.get('text')!r}" if query.get("text") else "")
        + f"   [{backend.get('name', '?')}/{backend.get('ranking', '?')}]"
    ]
    floor = query.get("trust_floor") or []
    if floor and floor != ["reported", "authoritative"]:
        # A raised floor and a quiet corpus produce the same empty page, so say which.
        header.append(f"trust floor: {', '.join(floor)}")
    if not hits:
        header.append("nothing matched.")

    body = [*header, ""]
    for index, hit in enumerate(hits, start=1):
        body += _hit_lines(index, hit, compact=compact)
    return envelope("\n".join(body).rstrip())


def _hit_lines(index: int, hit: dict[str, Any], *, compact: bool) -> list[str]:
    tier = TRUST_LABEL.get(str(hit.get("trust")), str(hit.get("trust")))
    head = (
        f"{index}. {hit.get('repo')}#{hit.get('number')} {hit.get('type')}  "
        f"[{tier}]  {hit.get('age')}  {hit.get('source_type')}"
    )
    if hit.get("author"):
        head += f"  @{hit['author']}"
    lines = [head, f"   {hit.get('title', '')}", f"   {hit.get('snippet', '')}"]
    if hit.get("url"):
        lines.append(f"   {hit['url']}")
    if not compact and hit.get("score") is not None:
        lines.append(f"   score {hit['score']}")
    lines.append("")
    return lines


def render_thread(payload: dict[str, Any], *, compact: bool = False) -> str:
    """One thread, with the cap stated rather than implied.

    A caller that cannot tell truncation from a quiet thread will read ten comments as the
    whole argument, so the count is on the page.
    """
    thread = payload.get("thread") or {}
    lines = [
        f"{thread.get('repo')}#{thread.get('number')} {thread.get('type')}  "
        f"{thread.get('state')}  {thread.get('age')}",
        f"{thread.get('title', '')}",
    ]
    if thread.get("author"):
        lines.append(f"opened by @{thread['author']}")
    if thread.get("labels"):
        lines.append(f"labels: {', '.join(thread['labels'])}")
    if thread.get("files"):
        lines.append(f"files: {', '.join(thread['files'])}")
    if thread.get("links"):
        lines.append(
            "links: "
            + ", ".join(f"{link['relationship']} #{link['target']}" for link in thread["links"])
        )
    lines += ["", thread.get("body", ""), ""]

    returned, total = thread.get("comments_returned", 0), thread.get("comments_total", 0)
    focus, matched = thread.get("focus") or "", thread.get("focus_matched")
    head = f"-- {returned} of {total} comments"
    if focus:
        head += f", best first for {focus!r}"
        if matched is not None:
            head += f" ({matched} of {total} carry every term)"
    lines.append(head + " --")
    for index, comment in enumerate(thread.get("comments") or [], start=1):
        lines += _hit_lines(index, comment, compact=compact)
    if total > returned:
        lines.append(
            f"({total - returned} not shown: a thread is never returnable in full."
            + ("" if focus else " Narrow it with a focus query.")
            + ")"
        )
    return envelope("\n".join(lines).rstrip(), source=thread.get("url"))


def render_status(payload: dict[str, Any]) -> str:
    """No envelope: this is our own numbers, not retrieved text."""
    backend = payload.get("backend") or {}
    schema = payload.get("schema") or {}
    lines = [
        f"version   {payload.get('version')}",
        f"backend   {backend.get('name')} / {backend.get('ranking')}  "
        f"capabilities: {', '.join(backend.get('capabilities') or []) or 'none'}",
        f"schema    applied {schema.get('applied')}, pending {schema.get('pending')}",
        f"index     {payload.get('threads')} threads, {payload.get('documents')} documents, "
        f"{payload.get('raw_objects')} raw objects",
    ]
    for row in payload.get("passes") or []:
        lines.append(
            f"  {row['repo']} [{row['pass']}] high-water {row['high_water']} "
            f"last-ok {row['last_ok_at']}"
        )
    usage = payload.get("usage")
    if usage:
        lines.append(
            f"quota     {usage['per_minute']['used']}/{usage['per_minute']['limit']} per minute, "
            f"{usage['daily']['used']}/{usage['daily']['limit']} today"
        )
    return "\n".join(lines)
