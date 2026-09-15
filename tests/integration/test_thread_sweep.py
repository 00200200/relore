"""Getting past the page cap without raising it (huggingface/relore#70).

`thread` serves ten comments. The cap is the contract (section 6) and raising it is not the
fix -- what the cap does to a caller who needs the *whole* thread is. Measured on one agent
run against `transformers#37866`, 70 comments: the agent came back **six times** with six
different `--focus` strings and spent 3,558 tokens seeing overlapping samples. That is the
shape this project keeps re-finding, one level up from re-reading a file at a different
line range each time.

So two views, both asserted here. `--outline` is one line per comment -- the `defs` move
applied to a discussion, ask for the shape then read the parts -- and `--after` turns the
repeated sample into a sweep. What matters about both is that they are honest about what
they did not reach: an outline that capped silently would be a worse version of the page.
"""

from __future__ import annotations

import pytest
from fake_github import FakeGitHub
from sqlalchemy import Engine

from relore.api.schemas import thread_json
from relore.ingest.index_thread import index_thread
from relore.render import render_thread
from relore.search import open_backend
from relore.search.queries import (
    AFTER_CURSOR,
    ENDS_AND_MIDDLE,
    MAX_OUTLINE_COMMENTS,
    OUTLINE_VIEW,
)

REPO = "owner/name"


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub(REPO)


def _thread_of(engine: Engine, fake: FakeGitHub, comments: int):
    """One issue with ``comments`` comments, each a minute after the last."""
    issue = fake.add_issue(1, body="the opening post")
    for n in range(comments):
        fake.add_comment(
            issue,
            1000 + n,
            f"comment number {n}",
            created_at=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z",
        )
    with fake.client() as client:
        index_thread(engine, client, fake.repo, 1)
    return open_backend(engine)


def _ids(view) -> list[str]:
    return [hit.source_id for hit in view.comments]


# -- the outline ------------------------------------------------------------


def test_the_outline_carries_every_comment_where_the_page_carries_ten(
    engine: Engine, fake: FakeGitHub
) -> None:
    """The measured case, in miniature. Seventy comments, a page of ten, and the outline is
    the only view that can tell the caller what the other sixty are."""
    backend = _thread_of(engine, fake, 70)

    page = backend.thread(REPO, 1)
    outline = backend.thread(REPO, 1, outline=True)

    assert len(page.comments) == 10
    assert len(outline.outline) == 70
    assert outline.outline_total == 70


def test_the_outline_is_chronological_and_not_ranked(engine: Engine, fake: FakeGitHub) -> None:
    """Ranking it is what `--focus` already does, and the reason to read a whole thread is
    usually that ranking has not worked -- which is the run this came from."""
    backend = _thread_of(engine, fake, 12)

    rows = backend.thread(REPO, 1, outline=True).outline

    assert [r.source_id for r in rows] == [str(1000 + n) for n in range(12)]


def test_an_outline_line_says_which_comment_and_never_answers_with_it(
    engine: Engine, fake: FakeGitHub
) -> None:
    """It carries the id to ask for it by, the standing to weigh it by, and a snippet short
    of quotable. A line long enough to answer with would make this a second, worse page."""
    backend = _thread_of(engine, fake, 3)

    (row, *_) = backend.thread(REPO, 1, outline=True).outline

    assert row.source_id == "1000"
    assert row.trust and row.age and row.author
    assert len(row.snippet) <= 80


def test_the_outline_cap_is_announced_not_silent(engine: Engine, fake: FakeGitHub) -> None:
    """Nothing is cheap enough to serve a 644-comment thread whole -- measured: 200 rows of
    one came to 6,626 tokens, five times the page it replaces. So the outline is capped
    too, and the one thing it must never do is stop quietly."""
    backend = _thread_of(engine, fake, MAX_OUTLINE_COMMENTS + 15)

    view = backend.thread(REPO, 1, outline=True)

    assert len(view.outline) == MAX_OUTLINE_COMMENTS
    assert view.total_documents == MAX_OUTLINE_COMMENTS + 15, "the denominator is the thread"


# -- the sweep --------------------------------------------------------------


def test_after_returns_only_what_follows_the_cursor(engine: Engine, fake: FakeGitHub) -> None:
    backend = _thread_of(engine, fake, 30)

    first = backend.thread(REPO, 1, after="1004")

    assert _ids(first) == [str(1005 + n) for n in range(10)]


def test_a_cursor_makes_the_page_sequential_rather_than_a_sample(
    engine: Engine, fake: FakeGitHub
) -> None:
    """`ends+middle` is a sampling strategy for "show me this thread", and composed with a
    cursor it is a trap: the sample's last row is the thread's *last* comment, so the
    obvious next call returns nothing and reads as "that was the end of it"."""
    backend = _thread_of(engine, fake, 30)

    assert backend.thread(REPO, 1).selection == ENDS_AND_MIDDLE
    assert backend.thread(REPO, 1, after="1004").selection == AFTER_CURSOR


def test_a_sweep_reaches_every_comment_and_repeats_none(engine: Engine, fake: FakeGitHub) -> None:
    """The property the six-call `--focus` loop could not have.

    Started from the outline, which is the index this pages the bodies of. Starting from
    the *default* page instead is the trap the render guards: its last row is the thread's
    last comment, so the first `--after` lands past the end."""
    backend = _thread_of(engine, fake, 35)

    seen: list[str] = []
    cursor = backend.thread(REPO, 1, outline=True).outline[0].source_id
    seen.append(cursor)
    while True:
        view = backend.thread(REPO, 1, after=cursor)
        if not view.comments:
            break
        seen += _ids(view)
        cursor = _ids(view)[-1]

    assert seen == sorted(seen, key=int)
    assert len(seen) == len(set(seen)) == 35


def test_a_sweep_that_ran_past_the_end_says_so(engine: Engine, fake: FakeGitHub) -> None:
    """The trap, and the guard. `--after` taken from a sampled page starts at the thread's
    last comment, so it correctly returns nothing -- and "nothing" here reads as "that was
    the end of it" to a caller who has seen ten of thirty-five."""
    backend = _thread_of(engine, fake, 35)
    sampled = backend.thread(REPO, 1)

    view = backend.thread(REPO, 1, after=_ids(sampled)[-1])

    assert view.comments == ()
    out = render_thread({"thread": thread_json(view)})
    assert "nothing follows that comment" in out
    assert "SAMPLED page" in out


def test_the_denominator_stays_the_whole_thread_under_a_cursor(
    engine: Engine, fake: FakeGitHub
) -> None:
    """ "10 of 35" is the number a reader needs. "10 of what is left after where I resumed"
    is a different fact wearing the same sentence."""
    backend = _thread_of(engine, fake, 35)

    assert backend.thread(REPO, 1, after="1020").total_documents == 35


def test_an_unknown_cursor_narrows_nothing_rather_than_failing(
    engine: Engine, fake: FakeGitHub
) -> None:
    """A sweep can legitimately reach an id this thread does not hold -- a comment the trust
    floor excludes, for one -- and a caller cannot tell that from a typo from outside. An
    error here would end a sweep on the one case it exists to survive."""
    backend = _thread_of(engine, fake, 12)

    view = backend.thread(REPO, 1, after="does-not-exist")

    assert len(view.comments) == 10


def test_a_cursor_and_a_focus_compose(engine: Engine, fake: FakeGitHub) -> None:
    """The cursor is admission, the focus is order (`_focused`). Applying one must not
    silently turn off the other."""
    backend = _thread_of(engine, fake, 30)

    view = backend.thread(REPO, 1, focus="comment number 25", after="1020")

    assert view.comments
    assert all(int(hit.source_id) > 1020 for hit in view.comments)


def test_the_outline_sweeps_too(engine: Engine, fake: FakeGitHub) -> None:
    """A thread too long to outline whole is swept rather than truncated. The first design
    ignored the cursor here, which put every row past the cap permanently out of reach --
    the silent incompleteness this verb exists to end, reintroduced by the fix for it."""
    backend = _thread_of(engine, fake, MAX_OUTLINE_COMMENTS + 15)

    first = backend.thread(REPO, 1, outline=True).outline
    rest = backend.thread(REPO, 1, outline=True, after=first[-1].source_id).outline

    assert len(rest) == 15
    assert not {r.source_id for r in first} & {r.source_id for r in rest}


# -- the narrowed outline (huggingface/relore#71) ----------------------------


def test_a_focus_narrows_the_outline_instead_of_ranking_it(
    engine: Engine, fake: FakeGitHub
) -> None:
    """Both times the measured run reached for an outline it ran `--outline --json` through
    a client-side filter -- 411 and 236 tokens against ~2,593 for the rendered outline of
    the same thread. It wanted the ids carrying a word, so that is a flag now."""
    backend = _thread_of(engine, fake, 30)

    view = backend.thread(REPO, 1, outline=True, focus="number 7")

    # `7` is in 7 and 17 and 27; `number` is in all thirty. Any-term, so all thirty.
    assert view.outline_matched == 30
    assert len(view.outline) == 30
    assert not view.outline_widened


def test_the_narrowing_is_a_disjunction_not_a_conjunction(engine: Engine, fake: FakeGitHub) -> None:
    """A conjunction is what empties a thread page as soon as a caller passes a sentence
    (`_focused`), and an empty outline would read as "no comment here mentions that"."""
    backend = _thread_of(engine, fake, 30)

    view = backend.thread(REPO, 1, outline=True, focus="quantization 17")

    assert [hit.source_id for hit in view.outline] == ["1017"]
    assert view.outline_matched == 1


def test_a_narrowing_that_carried_nothing_widens_rather_than_emptying(
    engine: Engine, fake: FakeGitHub
) -> None:
    backend = _thread_of(engine, fake, 12)

    view = backend.thread(REPO, 1, outline=True, focus="quantization")

    assert view.outline_matched == 0
    assert view.outline_widened is True
    assert len(view.outline) == 12
    out = render_thread({"thread": thread_json(view)})
    assert "no comment carries any of 'quantization'" in out


def test_the_narrowing_reads_every_chunk_of_a_comment(engine: Engine, fake: FakeGitHub) -> None:
    """An outline row is a comment's *first* document, and its terms may be in its fourth.
    Matching only the row would drop the long comments, which are the ones worth finding."""
    issue = fake.add_issue(1, body="the opening post")
    fake.add_comment(issue, 1000, "a short one", created_at="2026-01-01T00:00:00Z")
    fake.add_comment(
        issue,
        1001,
        "padding. " * 900 + " zzzunique tail",
        created_at="2026-01-01T00:01:00Z",
    )
    with fake.client() as client:
        index_thread(engine, client, fake.repo, 1)
    backend = open_backend(engine)

    view = backend.thread(REPO, 1, outline=True, focus="zzzunique")

    assert [hit.source_id for hit in view.outline] == ["1001"]
    assert view.outline_matched == 1


def test_a_narrowed_outline_is_cased_the_way_the_caller_typed_it(
    engine: Engine, fake: FakeGitHub
) -> None:
    backend = _thread_of(engine, fake, 12)

    assert backend.thread(REPO, 1, outline=True, focus="COMMENT NUMBER 3").outline_matched == 12


def test_the_narrowing_composes_with_the_cursor(engine: Engine, fake: FakeGitHub) -> None:
    backend = _thread_of(engine, fake, 30)

    view = backend.thread(REPO, 1, outline=True, focus="number", after="1020")

    assert [hit.source_id for hit in view.outline] == [str(1021 + n) for n in range(9)]


def test_an_outline_does_not_also_compute_a_page_nobody_reads(
    engine: Engine, fake: FakeGitHub
) -> None:
    """`render_thread` returns after the outline, so the page's ten rows are work thrown
    away -- and an answer computed in order to be discarded is how two selections drift."""
    backend = _thread_of(engine, fake, 30)

    view = backend.thread(REPO, 1, outline=True, focus="number")

    assert view.comments == ()
    assert view.selection == OUTLINE_VIEW
    assert view.focus_matched is None, "the page's conjunction count is not the outline's"
