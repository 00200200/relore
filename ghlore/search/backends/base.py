"""What both backends share, and the five things they do not.

Section 4.1: the portable core behaves identically on both dialects, but **the search
layer does not port** -- ``tsvector``+GIN is not FTS5, and ``ts_rank_cd`` is not ``bm25``.
So filters, scoping, caps, section 6's weighted score and the thread view live here, once,
and each subclass supplies only what its engine expresses differently:

* :meth:`SearchBackend.info` -- which engine, and what it can answer
* :meth:`SearchBackend.fts_score` -- the relevance expression
* :meth:`SearchBackend.fts_filter` -- the predicate, and any join it needs
* :meth:`SearchBackend.unfiltered_fts_score` -- the same relevance, for text this query is
  *not* filtering on. Postgres can; FTS5 cannot, and says so by returning ``None``.
* :meth:`SearchBackend.age_days` -- how old a row is, for section 6's decay

Everything else is ordinary SQL over the shared connection, which is why a filter bug
cannot be dialect-specific. Section 6's *weights* are shared for the same reason: the
overlap terms are counted with the same predicates the filters use, so a term can never
score what the filter would not have matched.
"""

from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from functools import reduce
from typing import Any

from sqlalchemy import (
    Engine,
    Select,
    and_,
    case,
    exists,
    func,
    literal,
    nulls_last,
    or_,
    select,
)

from ghlore.search.queries import (
    HUMAN_TRUST,
    MAX_BODY_CHARS,
    MAX_HITS_PER_THREAD,
    MAX_THREAD_COMMENTS,
    BackendInfo,
    Hit,
    SearchQuery,
    ThreadView,
    admissible_trust,
    render_age,
    snippet,
    tokenize,
    trust_policy,
)
from ghlore.search.ranking import WEIGHTS, RankSpec, half_life
from ghlore.store import schema as s


class SearchBackend(ABC):
    """Read-only retrieval over one engine.

    The engine handed in is expected to be the read-only one (section 11). Nothing here
    writes, and nothing here is allowed to: the API's whole connection is ``SELECT``-only
    on both dialects, so a write would fail at the driver rather than be caught by review.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- the dialect-specific three --------------------------------------

    @abstractmethod
    def info(self) -> BackendInfo: ...

    @abstractmethod
    def fts_score(self, text: str) -> Any:
        """A column expression: higher is more relevant."""

    @abstractmethod
    def fts_filter(self, stmt: Select, text: str) -> Select:
        """Narrow ``stmt`` to rows matching ``text``, adding whatever join that needs."""

    @abstractmethod
    def unfiltered_fts_score(self, text: str) -> Any | None:
        """Relevance to ``text`` for a query that is **not** filtering on it, or ``None``.

        Section 6's expansion runs signal legs that carry no free text at all, and the
        caller's words are still evidence about which of those rows is the best one. On
        Postgres ``ts_rank_cd`` is an ordinary expression and can say so. On SQLite it
        cannot: ``bm25()`` is an auxiliary function of the FTS5 index and is an error
        anywhere but a query that MATCHed it, so that backend returns ``None`` and the
        lexical term is simply absent from those legs. Section 4.1 already says a number
        from one backend describes a different engine; this is one of the places it does.
        """

    @abstractmethod
    def age_days(self, column: Any) -> Any:
        """Age of ``column`` in days, as a float expression. Section 6's decay input."""

    # -- search ----------------------------------------------------------

    def search(self, query: SearchQuery, *, rank_as: RankSpec | None = None) -> list[Hit]:
        """Section 6: full text, filters, the trust floor, and the weighted score.

        ``rank_as`` is "filter by the leg, score by the whole question" (see
        ``ranking.py``). Expansion passes the caller's own terms plus everything it derived
        from them, so a leg that filters on one path still orders its rows by how much of
        the *call* each thread carries. Left ``None`` -- every caller but expansion -- a
        query is scored against itself, which is what it always meant.

        No results is a successful call with nothing in it. That includes the case where
        the token's scope is empty -- section 11 fails closed, so *no* repository in scope
        means nothing, never everything.
        """
        if not query.repos:
            return []
        spec = rank_as or RankSpec.of(query)
        with self.engine.connect() as conn:
            return [self._hit(row, query) for row in conn.execute(self._select(query, spec))]

    def _select(self, query: SearchQuery, spec: RankSpec | None = None) -> Select:
        """Section 6's dedup and per-thread cap, in SQL rather than after the fetch.

        This has to be the database's job, and the reason is a bug that looked like a
        passing test. Fetching N rows and capping them in Python caps the wrong set: a
        thread with forty matching comments fills the fetch before a second thread is ever
        read, so the cap has nothing left to choose and the page is one thread regardless.
        Two window functions -- ``row_number`` over ``(thread, source_type)`` and then over
        the thread -- mean the ``LIMIT`` applies to rows that already survived both, which
        is correct at any corpus size.

        The score is materialized in its own subquery first, and that layer is not
        cosmetic: FTS5 refuses its auxiliary functions anywhere but a direct query over the
        index ("unable to use function bm25 in the requested context"), so a window that
        orders by ``bm25()`` is an error rather than a slow plan. Ranking by an already
        computed column is portable and gives both dialects the same shape.
        """
        spec = spec or RankSpec.of(query)
        filtering_on_text = bool(query.text.strip())
        terms = self._score_terms(query, spec)
        score = self._weighted(terms, spec)
        scored = score is not None

        stmt = (
            select(
                s.documents.c.id,
                s.documents.c.thread_id,
                s.threads.c.repo,
                s.threads.c.github_number,
                s.threads.c.thread_type,
                s.threads.c.title,
                s.documents.c.source_type,
                s.documents.c.url,
                s.documents.c.author,
                s.documents.c.trust,
                s.documents.c.body_text,
                s.documents.c.github_created_at,
                (score if scored else literal(0.0)).label("score"),
                # Section 8's per-hit score breakdown: every term as its own number, for
                # the person tuning the weights. They are already computed for the sum, so
                # carrying them out costs nothing and is the only way a ranking complaint
                # can be specific rather than "the order looks wrong".
                *(expression.label(f"term_{name}") for name, expression in terms.items()),
            )
            .select_from(s.documents.join(s.threads, s.documents.c.thread_id == s.threads.c.id))
            .where(*self._filters(query))
        )
        if filtering_on_text:
            stmt = self.fts_filter(stmt, query.text)
        rows = stmt.subquery("scored")

        chunks = select(
            rows,
            func.row_number()
            .over(
                partition_by=[rows.c.thread_id, rows.c.source_type],
                order_by=_order(rows, scored),
            )
            .label("chunk_rank"),
        ).subquery("chunks")

        best = (
            select(
                chunks,
                func.row_number()
                .over(partition_by=[chunks.c.thread_id], order_by=_order(chunks, scored))
                .label("thread_rank"),
            )
            .where(chunks.c.chunk_rank == 1)
            .subquery("best")
        )
        return (
            select(best)
            .where(best.c.thread_rank <= MAX_HITS_PER_THREAD)
            .order_by(*_present_order(best, scored, query.sort))
            .limit(query.limit)
        )

    # -- section 6's score -----------------------------------------------

    def _score_terms(self, query: SearchQuery, spec: RankSpec) -> dict[str, Any]:
        """Every term of section 6's sum that this call can express, unweighted.

        A term is *absent* rather than zero when the question does not contain it: a query
        with no error in it should not carry an ``error`` column of zeros through three
        subqueries, and section 8's breakdown reads better when it lists what was actually
        asked. A weight of 0 removes a term for the same reason -- which is how
        ``relationship`` stays out of the SQL until milestone 4 fills ``thread_links``.
        """
        candidates = {
            "error": (
                WEIGHTS.error,
                self._overlap(s.thread_errors, s.thread_errors.c.message_norm, spec.errors, True),
            ),
            "test": (
                WEIGHTS.test,
                self._overlap(s.thread_tests, s.thread_tests.c.test_id, spec.tests),
            ),
            "symbol": (
                WEIGHTS.symbol,
                self._overlap(s.thread_symbols, s.thread_symbols.c.symbol, spec.symbols),
            ),
            "file": (
                WEIGHTS.file,
                self._overlap(s.thread_files, s.thread_files.c.path, spec.files),
            ),
            "lexical": (WEIGHTS.lexical, self._lexical(query, spec)),
        }
        return {
            name: expression
            for name, (weight, expression) in candidates.items()
            if weight and expression is not None
        }

    def _weighted(self, terms: dict[str, Any], spec: RankSpec) -> Any | None:
        """Section 6's sum, decayed. ``None`` when there is nothing to score at all.

        Returning ``None`` rather than a bound zero is what keeps ``_order`` honest: a
        query with no terms is not a query whose every row ties, it is one that should come
        back newest-first and say so.
        """
        if not terms:
            return None
        total = reduce(
            operator.add,
            (literal(getattr(WEIGHTS, name)) * expression for name, expression in terms.items()),
        )
        decay = self._decay(spec.kind)
        return total if decay is None else total * decay

    def _overlap(
        self, table: Any, column: Any, values: tuple[str, ...], fuzzy: bool = False
    ) -> Any | None:
        """How much of what was asked this thread carries, in ``[0, 1]``.

        A *fraction* rather than a count, so the term is comparable across questions of
        different sizes: a thread matching both of two requested files should not outrank
        one matching all four of four. Each value is tested with the **same predicate the
        filter uses** (:meth:`_thread_has`), which is not tidiness -- section 13.2 #3
        records what happens when the two sides of a term disagree about normal form, and
        sharing the predicate makes disagreeing impossible.
        """
        if not values:
            return None
        matched = reduce(
            operator.add,
            (
                case((self._thread_has(table, column, (value,), fuzzy=fuzzy), 1.0), else_=0.0)
                for value in values
            ),
        )
        return matched / literal(float(len(values)))

    def _lexical(self, query: SearchQuery, spec: RankSpec) -> Any | None:
        """The full-text term, from whichever side of the join is available.

        When the query filters on its own text the FTS join exists and both engines can
        score it. When it does not -- every signal leg of an expanded call -- only a
        backend that can rank text it did not MATCH has anything to say, which is what
        :meth:`unfiltered_fts_score` answers.
        """
        if query.text.strip():
            return self.fts_score(query.text)
        if spec.text.strip():
            return self.unfiltered_fts_score(spec.text)
        return None

    def _decay(self, kind: str | None) -> Any | None:
        """Section 6's kind-aware decay as a column expression, or ``None`` for no decay.

        ``rationale`` has no half-life on purpose -- the oldest thread is often the answer
        -- and neither does an unspecified kind, because decay is the only factor that can
        demote a correct old result and the safe default is to leave the order alone.

        Two guards that are not decoration. A row with no timestamp decays by nothing
        rather than to nothing: an unknown date is missing evidence, not old evidence. And
        a *future* timestamp is clamped to age zero rather than allowed to push the
        denominator below 1 and score above the undecayed maximum -- GitHub's clock is not
        ours, and a skewed row must not be able to buy rank with it.
        """
        life = half_life(kind)
        if life is None:
            return None
        age = self.age_days(s.documents.c.github_created_at)
        fresh = case((age > literal(0.0), age), else_=literal(0.0))
        return func.coalesce(literal(1.0) / (literal(1.0) + fresh / literal(life)), literal(1.0))

    @staticmethod
    def _trust_filter(query: SearchQuery) -> Any:
        """Section 6.2's floor, including its one disjunction.

        A precedent query answers from the ``authoritative`` tier *or* from any human tier
        on a merged pull request, because merging is the maintainer's act and authority
        attaches to the merge rather than to the author. Expressed here rather than in
        ``queries.py`` so that module stays free of a database import.
        """
        policy = trust_policy(query.trust, query.kind)
        condition = s.documents.c.trust.in_(policy.tiers)
        if policy.merged_pr_is_precedent:
            condition = or_(
                condition,
                and_(
                    s.threads.c.merged_at.isnot(None),
                    s.documents.c.trust.in_(HUMAN_TRUST),
                ),
            )
        return condition

    def _filters(self, query: SearchQuery) -> list[Any]:
        """Every predicate that is not full text.

        Repo scoping is first and unconditional: section 11 applies it here, in the query
        layer, rather than in a handler, so that adding an endpoint cannot drop it.
        """
        where: list[Any] = [
            s.threads.c.repo.in_(query.repos),
            self._trust_filter(query),
        ]
        if query.since is not None:
            where.append(s.documents.c.github_created_at >= query.since)
        if query.labels:
            where.append(self._thread_has(s.thread_labels, s.thread_labels.c.label, query.labels))
        # The four signal filters below read tables that milestone 3's extraction pass
        # fills. They are wired now because they are versioned API surface (section 7) and
        # because a filter written later against a populated table is a filter nobody
        # tested empty -- but until that pass runs they correctly match nothing.
        if query.files:
            where.append(self._thread_has(s.thread_files, s.thread_files.c.path, query.files))
        if query.symbols:
            where.append(
                self._thread_has(s.thread_symbols, s.thread_symbols.c.symbol, query.symbols)
            )
        if query.errors:
            where.append(
                self._thread_has(
                    s.thread_errors, s.thread_errors.c.message_norm, query.errors, fuzzy=True
                )
            )
        if query.tests:
            where.append(self._thread_has(s.thread_tests, s.thread_tests.c.test_id, query.tests))
        return where

    @staticmethod
    def _thread_has(
        table: Any, column: Any, values: tuple[str, ...], *, fuzzy: bool = False
    ) -> Any:
        """``EXISTS`` against a signal table, as OR across the requested values.

        Section 4's signal tables are separate from ``documents`` so that an exact match
        can be weighted independently of prose; at this milestone that means filtered
        independently of it. ``fuzzy`` is a ``LIKE`` containment test, used for error
        strings where the caller pastes more than the indexed normalized form -- it is not
        the trigram tier, which is Postgres-only and lands with the ranking work.
        """
        if fuzzy:
            match = or_(*[column.like(f"%{value}%") for value in values])
        else:
            match = column.in_(values)
        return exists().where(and_(table.c.thread_id == s.documents.c.thread_id, match))

    def _hit(self, row: Any, query: SearchQuery) -> Hit:
        return Hit(
            repo=row.repo,
            number=int(row.github_number),
            thread_type=row.thread_type,
            title=row.title,
            source_type=row.source_type,
            url=row.url,
            author=row.author,
            trust=row.trust,
            age=render_age(row.github_created_at),
            snippet=snippet(row.body_text, query.terms, limit=query.snippet_chars),
            score=float(row.score or 0.0),
            created_at=row.github_created_at,
            breakdown=_breakdown(row),
        )

    # -- one thread ------------------------------------------------------

    def thread(self, repo: str, number: int, *, focus: str = "") -> ThreadView | None:
        """One thread, capped (section 6).

        ``focus`` **orders** which comments come back and never selects them -- see
        :meth:`_focused`. Without it the order is the opening and the closing of the
        thread rather than the first N: on a long thread the resolution is at the end, and
        returning only the beginning reliably returns the part that was wrong.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(s.threads).where(
                    s.threads.c.repo == repo, s.threads.c.github_number == number
                )
            ).one_or_none()
            if row is None:
                return None
            labels = tuple(
                str(label)
                for (label,) in conn.execute(
                    select(s.thread_labels.c.label)
                    .where(s.thread_labels.c.thread_id == row.id)
                    .order_by(s.thread_labels.c.label)
                )
            )
            files = tuple(
                str(path)
                for (path,) in conn.execute(
                    select(s.thread_files.c.path.distinct())
                    .where(s.thread_files.c.thread_id == row.id)
                    .order_by(s.thread_files.c.path)
                )
            )
            links = tuple(
                {"relationship": rel, "target": int(target)}
                for rel, target in conn.execute(
                    select(s.thread_links.c.relationship, s.threads.c.github_number)
                    .select_from(
                        s.thread_links.join(
                            s.threads, s.thread_links.c.target_thread_id == s.threads.c.id
                        )
                    )
                    .where(s.thread_links.c.source_thread_id == row.id)
                )
            )
            body = self._body(conn, row.id)
            comments, total, matched = self._comments(conn, row, focus)

        return ThreadView(
            repo=row.repo,
            number=int(row.github_number),
            thread_type=row.thread_type,
            title=row.title,
            url=row.url,
            author=row.author,
            state=row.state,
            age=render_age(row.created_at),
            labels=labels,
            body=body,
            comments=comments,
            files=files,
            links=links,
            total_documents=total,
            focus=focus,
            focus_matched=matched,
        )

    def _body(self, conn: Any, thread_id: int) -> str:
        text = conn.execute(
            select(s.documents.c.body_text)
            .where(
                s.documents.c.thread_id == thread_id,
                s.documents.c.source_type == "body",
                s.documents.c.chunk_index == 0,
            )
            .limit(1)
        ).scalar_one_or_none()
        return snippet(text or "", limit=MAX_BODY_CHARS)

    def _comments(
        self, conn: Any, thread: Any, focus: str
    ) -> tuple[tuple[Hit, ...], int, int | None]:
        base = select(
            s.documents.c.id,
            s.documents.c.source_type,
            s.documents.c.url,
            s.documents.c.author,
            s.documents.c.trust,
            s.documents.c.body_text,
            s.documents.c.github_created_at,
        ).where(
            s.documents.c.thread_id == thread.id,
            s.documents.c.source_type.notin_(("title", "body")),
            s.documents.c.trust.in_(admissible_trust(None)),
        )
        total = len(conn.execute(base).all())

        matched: int | None = None
        if focus.strip():
            matched = len(conn.execute(self.fts_filter(base, focus)).all())
            rows = self._focused(conn, base, focus)
        else:
            rows = _chronological(conn, base, MAX_THREAD_COMMENTS)

        terms = tokenize(focus)
        hits = tuple(
            Hit(
                repo=thread.repo,
                number=int(thread.github_number),
                thread_type=thread.thread_type,
                title=thread.title,
                source_type=row.source_type,
                url=row.url,
                author=row.author,
                trust=row.trust,
                age=render_age(row.github_created_at),
                snippet=snippet(row.body_text, terms),
                score=float(row.score or 0.0),
                created_at=row.github_created_at,
            )
            for row in rows
        )
        return hits, total, matched

    def _focused(self, conn: Any, base: Select, focus: str) -> list[Any]:
        """Order a thread's comments by a focus query. **Never filter on it.**

        The thread *is* the admission decision, and there is none left to make inside it.
        Both engines' text filter is a conjunction -- ``plainto_tsquery`` is a plain AND,
        FTS5 gets one quoted term per word -- which is right for corpus-wide ``search``,
        where it is what stops a pasted sentence matching everything. Applied inside one
        thread it empties the page as soon as the caller passes a sentence, and ``--help``
        invites exactly that ("select the comments that answer this").

        Measured on ``huggingface/transformers#28056``, 30 comments: ``cache`` returned 10,
        ``use_cache gradient`` 2, ``use_cache gradient checkpointing warning`` **0**. Every
        term is in the thread; no single comment carries all four. A monotonic decrease
        with term count ending in an empty page is the signature, and an empty page reads
        as "this thread has nothing relevant" -- the one thing it did not mean.

        So the focus scores, and the page is never empty on a thread that has comments.
        Postgres ranks the whole thread with the disjunction
        :meth:`unfiltered_fts_score` already builds, so an all-terms match still sorts
        first and a partial match follows it. FTS5 cannot score what it did not ``MATCH``
        (see the sqlite backend), so there the matched comments lead and the chronological
        remainder fills the rest of the page.
        """
        ranking = self.unfiltered_fts_score(focus)
        if ranking is not None:
            return list(
                conn.execute(
                    base.add_columns(ranking.label("score"))
                    .order_by(ranking.desc(), s.documents.c.github_created_at.asc().nullslast())
                    .limit(MAX_THREAD_COMMENTS)
                )
            )

        score = self.fts_score(focus)
        rows = list(
            conn.execute(
                self.fts_filter(base.add_columns(score.label("score")), focus)
                .order_by(score.desc())
                .limit(MAX_THREAD_COMMENTS)
            )
        )
        if len(rows) >= MAX_THREAD_COMMENTS:
            return rows
        chosen = {row.id for row in rows}
        remainder = [row for row in _chronological(conn, base, None) if row.id not in chosen]
        return rows + _ends(remainder, MAX_THREAD_COMMENTS - len(rows))


def _chronological(conn: Any, base: Select, limit: int | None) -> list[Any]:
    """A thread's comments oldest-first, scored zero: the order with nothing to rank."""
    rows = list(
        conn.execute(
            base.add_columns(literal(0.0).label("score")).order_by(
                s.documents.c.github_created_at.asc().nullslast()
            )
        )
    )
    return rows if limit is None else _ends(rows, limit)


def _breakdown(row: Any) -> dict[str, float]:
    """Section 8's per-hit score breakdown, read off the columns ``_select`` carried out.

    Only the terms this question actually contained appear, which is the point: a
    breakdown padded with zeros for everything the caller did not ask says less than one
    that lists what was weighed.
    """
    out = {"score": float(row.score or 0.0)}
    for key in row._mapping:
        if str(key).startswith("term_"):
            out[str(key)[len("term_") :]] = float(row._mapping[key] or 0.0)
    return out


def _order(source: Any, scored: bool) -> list[Any]:
    """Best first, newest first, and stable. **The selection order.**

    Score leads only when there *is* one: ordering by a bound constant is at best ignored
    and at worst a type error, and recency is the honest order for a query with nothing to
    rank. Since section 6's weighted score, "there is one" means the question carried a
    term of any kind -- not merely that it carried text, which is the change that gives a
    textless expansion leg an order at all (section 10.4 #1).

    The id tail makes the order total, so a page is reproducible -- which matters when the
    next thing built on top of it is a labelling UI (section 8).

    This one is used by the two ``row_number()`` windows that *choose* which document
    represents a chunk and a thread, and it stays on relevance whatever the caller asked
    to sort by -- see :func:`_present_order`.
    """
    lead = [source.c.score.desc()] if scored else []
    return [*lead, source.c.github_created_at.desc(), source.c.id.desc()]


def _present_order(source: Any, scored: bool, sort: str) -> list[Any]:
    """How the chosen hits are *presented*, which is not how they were chosen.

    ``newest`` must not reach the ``row_number()`` windows. Section 10.6 is the record of
    what happens when recency decides which document represents a thread: it picks the
    thread's *last* one, which on a merged pull request is the approving review, and a real
    query came back nine hits of ``LGTM``, ``Thx``, ``Nice!``, ``Yep``. Selecting on
    relevance and then ordering by date gives the best answer *from* each thread, most
    recent first -- which is what someone asking for a date sort wants, and not what
    sorting the whole candidate set by date would return.

    ``nulls_last`` because a document with no timestamp must not lead a date sort, and the
    two dialects disagree on where a NULL goes by default: Postgres puts it first on
    ``DESC``, SQLite last. As a tie-break that was invisible; as the leading key it decides
    the page.
    """
    if sort == "newest":
        return [nulls_last(source.c.github_created_at.desc()), source.c.id.desc()]
    return _order(source, scored)


def _ends(rows: list[Any], limit: int) -> list[Any]:
    """The opening and the closing of a thread, in chronological order.

    A long thread's resolution is at the end. Truncating to the first N returns the part
    that was wrong and drops the part that settled it.
    """
    if len(rows) <= limit:
        return rows
    head = limit // 2
    return rows[:head] + rows[len(rows) - (limit - head) :]
