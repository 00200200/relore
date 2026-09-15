"""What a page costs, and how much of it is itself repeated.

The static half of what `fieldrun` measures in a live run. `fieldrun` needs a model, a
network and a few minutes; this needs a database, runs in a second, and answers the one
question that turned out to matter most: **how much of this page is text the page has
already said?**

That is the measure that found relore#71. A thread page reprinted `owner/repo#N pr`, the
thread's title and a 76-character URL under every comment -- three constants the head line
already carries -- and on five real production threads that was 24-33% of the whole page.
No token total showed it, because the total was not obviously wrong; the *repetition* was.

Two views, and the second is the point:

* ``cost`` -- bytes and approximate tokens per page, per verb, so a change to a renderer
  can be measured before and after against the same threads rather than against a memory.
* ``echo`` -- the share of a page's own lines and head-line fields that appear more than
  once in it. A page is allowed to repeat something; it is not allowed to repeat it ten
  times without anyone noticing.

Run it against any indexed database -- production's, or a local sample:

    python benchmarks/probes/page_cost.py --url postgresql+psycopg://…/relore \\
        --repo huggingface/transformers --biggest 5
    python benchmarks/probes/page_cost.py --url sqlite:///local.db --repo owner/name \\
        --thread 37866 --thread 46419

`--json` writes the rows out, so two runs can be diffed. Approximate tokens are bytes/4 and
are labelled as such: the exact number depends on a tokenizer this package does not carry,
and every conclusion here is a ratio.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402

from relore.api.schemas import thread_json  # noqa: E402
from relore.render import render_thread  # noqa: E402
from relore.search import open_backend  # noqa: E402

#: Bytes per token, near enough for a ratio and wrong enough to be labelled. Every finding
#: this script has produced was a proportion, which this constant cancels out of.
BYTES_PER_TOKEN = 4

#: What makes a repeated field a *constant* rather than a word that happens to recur. Both
#: were tuned against the page that motivated this script -- see :func:`echo`.
MIN_ROWS = 3
MIN_SHARE = 0.02


def biggest_threads(engine: Any, repo: str, limit: int) -> list[int]:
    """The longest-discussion threads, which is where a page grammar's cost actually lands.

    A page of ten comments costs the same on a thread with eleven and a thread with 644;
    what changes is how much of the argument it is hiding, so these are the threads worth
    rendering when the question is what a page grammar costs.
    """
    rows = engine.connect().execute(
        text(
            """
            SELECT t.github_number AS number, count(*) AS documents
            FROM threads t JOIN documents d ON d.thread_id = t.id
            WHERE t.repo = :repo AND d.source_type NOT IN ('title', 'body')
            GROUP BY t.github_number ORDER BY documents DESC LIMIT :limit
            """
        ),
        {"repo": repo, "limit": limit},
    )
    return [int(row.number) for row in rows]


def echo(page: str) -> dict[str, Any]:
    """How much of this page the page has already said.

    Whole lines first, because a repeated line is the unambiguous case -- the same URL, the
    same quoted title, the same head. Then *fields*: the space-separated tokens of every
    line, which catches a constant that travels inside an otherwise-varying line, which is
    exactly how ``owner/repo#N pr`` hid for three releases.

    **Both thresholds were wrong on the first draft and the page that motivated the script
    is what corrected them**, which is the only reason to write them down. A bare ``>`` --
    the untrusted-content marker on a blank line of a quoted body -- made a long opening
    post read as 24% repetition, which is a false positive big enough to bury a real one.
    And a constant is *not* usually on half a page's lines: ``huggingface/transformers#46766``
    was on ten lines of fifty, so the first floor missed the very field this exists to find.
    A field earns the name by appearing on at least three lines **and** costing at least 2%
    of the page.
    """
    lines = [line.strip() for line in page.splitlines() if line.strip()]
    # Structure, not content: the quote marker, a fence, a bare bullet. These repeat by
    # design and are a handful of bytes each.
    content = [line for line in lines if len(line) > 4]
    repeated = sum(count for _, count in Counter(content).items() if count > 1)
    fields = [field for line in content for field in line.split() if len(field) > 3]
    counted = Counter(fields)
    constants = [
        (field, count)
        for field, count in counted.most_common(20)
        if count >= MIN_ROWS and (len(field) + 1) * count >= len(page) * MIN_SHARE
    ]
    return {
        "lines": len(lines),
        "repeated_lines": repeated,
        "repeated_line_share": round(repeated / len(content), 3) if content else 0,
        "constant_fields": constants,
        # What removing the repetitions would save: every occurrence after the first.
        "constant_field_bytes": sum((len(f) + 1) * (c - 1) for f, c in constants),
    }


def measure(backend: Any, repo: str, number: int) -> list[dict[str, Any]]:
    """One thread, rendered every way the verb can render it."""
    views = {
        "thread": {},
        "thread --full": {"full": True},
        "thread --outline": {"outline": True},
    }
    rows = []
    for label, kwargs in views.items():
        view = backend.thread(repo, number, **kwargs)
        if view is None:
            continue
        payload = {"thread": thread_json(view)}
        page = render_thread(payload)
        comments = payload["thread"].get("comments") or []
        rows.append(
            {
                "thread": number,
                "view": label,
                "bytes": len(page),
                "tokens_approx": len(page) // BYTES_PER_TOKEN,
                "comments_returned": len(comments) or len(payload["thread"].get("outline") or []),
                "comments_total": payload["thread"].get("comments_total"),
                **echo(page),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="RELORE_DATABASE_URL of an indexed database")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--thread", action="append", type=int, default=[], help="repeatable")
    parser.add_argument("--biggest", type=int, default=0, help="also take the N longest threads")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    engine = create_engine(args.url)
    numbers = list(args.thread)
    if args.biggest:
        numbers += [n for n in biggest_threads(engine, args.repo, args.biggest) if n not in numbers]
    if not numbers:
        raise SystemExit("pass --thread N (repeatable) or --biggest N")

    backend = open_backend(engine)
    rows = [row for number in numbers for row in measure(backend, args.repo, number)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    head = (
        f"{'thread':>8} {'view':17} {'bytes':>7} {'~tok':>6} "
        f"{'lines':>6} {'repeat':>7} {'const B':>8}"
    )
    print(head)
    print("-" * len(head))
    for row in rows:
        print(
            f"{row['thread']:>8} {row['view']:17} {row['bytes']:>7} {row['tokens_approx']:>6} "
            f"{row['lines']:>6} {row['repeated_line_share']:>7} {row['constant_field_bytes']:>8}"
        )
    totals = Counter()
    for row in rows:
        totals["bytes"] += row["bytes"]
        totals["constant"] += row["constant_field_bytes"]
    if totals["bytes"]:
        share = totals["constant"] / totals["bytes"]
        print(
            f"\n{totals['bytes']} bytes over {len(rows)} pages; {totals['constant']} of them "
            f"({share:.0%}) are fields this script saw on at least half the lines of their page."
        )
        print("The list of those fields, per page, is in `--json` under `constant_fields`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
