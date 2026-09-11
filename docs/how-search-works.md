# How the search works

*What kind of index this is, what goes into it, and what it is better and worse at than the
two tools you already have.*

## TL;DR

**Lexical, ranked, with exact structural filters. No embeddings.** It is not a semantic
index and it is not grep.

- Prose is **Postgres full-text**: `to_tsvector('english', …)` with a GIN index, queried
  with `plainto_tsquery` and ranked by `ts_rank_cd`. It **stems** — searching `load` finds
  "loading" — and it **ranks**. It does not match substrings and it does not match meaning.
- A query is an **AND of every content term**. `optional mask dtype` works; `crash when the
  optional mask is missing` returns nothing. Two or three distinctive terms, not a sentence.
- **Errors, test ids, symbols, file paths and labels are extracted from the prose at ingest
  and matched exactly** as filters. `--error` is the one fuzzy case: a containment test
  against a *normalized* form, so a pasted traceback whose numbers differ still matches.
- One call is **fanned out** into error / test / symbol / file / free-text legs and merged,
  which is what makes pasting a whole traceback work. That is lexical fan-out, not
  semantics.
- Results are **ranked with a weighted score**, and every hit carries its author's standing
  and its age.
- `documents.embedding vector(384)` exists in the schema and **nothing writes it**. Vector
  search is a v2 option, not a shipped feature.

The daemon says which of these it can do rather than asking you to trust a document:

```
$ ghlore status
version      0.3.2
backend      postgresql / ts_rank_cd  capabilities: fulltext, weighted
```

`capabilities` is the authoritative answer. If a vector tier ever lands, that line changes
before this page does.

---

## What is in the index

**The conversation, not the code.** Issues, pull request bodies, reviews, inline review
comments — what people *said*. There is no source code in the database at all; that is a
design invariant with a test behind it, not an omission. `ghlore map|defs|refs` do read
code, but they run against *your* working tree, offline, and never touch the server.

In production that is `huggingface/transformers`' complete history plus `huggingface/serge`
— about 48,000 threads and 466,000 indexed documents, kept current by a five-minute poll.

### One document per GitHub unit, split only when long

A title, a body, a comment, a review, a review comment each become a **document**. A short
comment is never split: one review comment is usually one complete thought, and splitting
it destroys the thing that made it worth retrieving. Past ~6,000 characters (~1,500 tokens)
a body or comment is chunked, on its own headings where it has them.

Chunks are why a hit can say `passage 4 of 7 in this comment` — a long comment is several
documents, and the API tells you so rather than serving one twice.

### Five signal tables, extracted from the prose

Extraction is **precision-first and anchored on structure**, never on "looks like an
identifier": a stack frame, a diff line, a fenced block, a pytest node id. A wrong symbol
costs ranking quality on every future query; a missed one costs a single query.

| table | what lands in it | how a filter matches it |
| --- | --- | --- |
| `thread_errors` | `ValueError: expected 768 features, got 1024`, **normalized** — numbers, addresses and tensor dumps collapsed | `--error`, containment against the normalized form |
| `thread_tests` | `tests/test_x.py::TestMask::test_shapes[cuda]`, and the parametrization-stripped form | `--test`, exact |
| `thread_symbols` | names from stack frames, definitions on diff lines, inline code spans | `--symbol`, exact |
| `thread_files` | paths from the diff, from an inline comment's anchor, and from prose — **kept apart**, because only the first two are evidence the thread changed something | `--file`, exact |
| `thread_links` | `Fixes #N` / `Closes #N` edges | `ghlore inflight`, exact |

The normalization on errors is the one that earns its keep: you paste what your terminal
printed, shapes and addresses and all, and it is put into the same form the index stored.
Without it every `--error` query would silently match nothing, which looks exactly like an
index with nothing to say.

### Every document carries who said it

`authoritative` (someone with write access, resolved from the GitHub API), `reported`
(anyone else), `machine` (bots, including our own). **Machine-authored documents are
excluded from every default result set** — returning an agent's own past output as prior
discussion makes its unreviewed work its own evidence. `--trust machine` is the only way to
see them, and they come back labelled.

`--kind` picks the floor for you: `rationale` and `precedent` require authority,
`failure` accepts any human tier, because a stranger's traceback is real evidence.

---

## How a query is answered

1. **Scope.** The repositories in scope, applied in the query layer so no endpoint can
   forget it.
2. **Expansion** (on by default, `--no-expand` to turn off). Your call is split into legs:
   the errors, test ids, symbols and paths it can recognise, plus your text as written.
   Each leg is asked separately and the results merged. A bare identifier is read as a
   symbol as well as text; a pasted traceback stops being one long AND.
3. **Filters.** `EXISTS` against the signal tables above — exact, except `--error`.
4. **Trust floor**, from `--kind` or `--trust`. Never lowered by a request.
5. **Score.** A weighted sum, ordered by how *specific* the evidence is — meaning how many
   threads could carry it by coincidence:

   | term | weight | why |
   | --- | --- | --- |
   | error | 3.0 | machine-minted and nearly unique |
   | test id | 3.0 | unique by construction |
   | symbol | 1.5 | specific, but `forward` appears in thousands of comments |
   | file | 1.0 | the unit; has a known recall floor until rename chains exist |
   | full-text rank | 1.0 | one lexical match is worth one file overlap |

   Kind-aware time decay is implemented and **switched off**: its half-lives describe an
   eight-year history and the corpora it was measured on are six-month samples, where it
   cost accuracy and bought nothing.
6. **Caps.** Best chunk per source type per thread, at most 3 hits from any one thread, 10
   hits, 400-character snippets. The caps are the contract, not a default — they are what
   stops one chatty thread owning the page.

On a laptop the engine is SQLite FTS5 with `bm25` and a porter stemmer. It answers the same
*questions*, but a score from one engine says nothing about the other, which is why
`status` prints which one answered.

---

## Versus `grep`

`grep` reads the tree; this reads the discussion. They do not overlap, and the honest split
is **grep for what the code is, ghlore for why it is that way.**

**Where this wins**

- The answer is often in an *issue*, which has no file to grep. On the benchmark's
  failure-diagnosis slice `grep` scores Recall@10 **0.000** — not because it searched badly
  but because its only file→thread bridge is pull-request-shaped.
- A review comment's anchor survives the file being edited: it is stored as a symbol and a
  path, not a line number.
- Ranking, standing and age. `grep` gives you occurrences in arbitrary order with no way to
  tell a maintainer's verdict from a passer-by's guess.
- One call covers years of history that is not in your checkout at all.

**Where `grep` wins**

- Anything about the current tree: definitions, callers, "does this string still appear".
  Use `ghlore map|defs|refs` for that — they are local, offline, and not this index.
- Exact substring anywhere, including inside identifiers. Full-text tokenizes, so
  `_maybe_import` does not match `_maybe_import_sdnq` as a substring the way `grep` would.
- No dependency, no network, no freshness question.

## Versus a semantic (embedding) index

**What a vector index would buy:** paraphrase. "why is the mask cast here" would find a
comment that says "we upcast before the softmax for numerical stability" with no shared
terms. That is the real gap, and this index does not close it.

**What it would cost, and why it is not here yet:**

- An embedding model in the ingest path and a vector column that must be recomputed for
  every document when the model changes — the `content_hash` already reserves room for the
  model id so that a swap invalidates everything exactly once, on purpose.
- It would not help the queries this is measured on. The strongest signals in this corpus
  are machine-minted strings — a normalized exception, a pytest node id — where exact
  matching is not a poor approximation of semantics, it is the correct answer.
- Nobody could tell whether it helped. Ranking changes here are gated on a frozen
  evaluation set, and adding a tier whose benefit is assumed is how a ranking becomes
  unfalsifiable.

**What is used instead:** the expansion fan-out. A question with no shared terms still
reaches the right thread when it carries an error, a path, a test id or an identifier,
because those become their own legs. That covers the agent-shaped questions; it does not
cover an English paraphrase with no anchor in it, and that limitation is real.

The measured shape, Recall@10 / MRR, over 139 judged examples against both baselines:

| slice | n | ghlore | GitHub search | grep |
| --- | --- | --- | --- | --- |
| `failure` | 48 | **1.000** / 0.927 | 0.875 / 0.743 | 0.000 / 0.000 |
| `precedent` | 40 | **0.525** / **0.286** | 0.325 / 0.102 | 0.200 / 0.130 |
| `rationale`, leak-free | 7 | 0.857 / 0.602 | 0.857 / 0.618 | 0.143 / 0.036 |
| `rationale`, leak-free, second corpus | 9 | **1.000** / **0.944** | 0.667 / 0.611 | 0.667 / 0.389 |

Two caveats the numbers do not carry on their own: the judgements are model-made, not
human, and the pooled `rationale` slice is excluded here because it leaks — a human
reviewer's finding is itself a document in the answer's own thread, which flatters this
index and nothing else. The leak-free rows are the ones worth quoting, and on the first of
them this is level with GitHub search rather than ahead of it.

## When to reach for which

| question | tool |
| --- | --- |
| "what calls this function today?" | `grep`, or `ghlore refs` on your checkout |
| "has anyone hit this exception?" | `ghlore search "<error>" --kind failure` |
| "why is this code like this?" | `ghlore search "<terms>" --kind rationale --file <path>` |
| "is somebody already fixing this?" | `ghlore inflight <n>` |
| "what did the maintainer say in that thread?" | `ghlore thread <n> --focus "<what you care about>"` |
| "find me something worded completely differently" | neither, today — see above |

## Known limits, stated plainly

- **The AND.** A sentence returns nothing. This is the single biggest cause of a
  disappointing first query.
- **No paraphrase matching.** Synonyms do not match; stemming is the whole of the
  flexibility.
- **Freshness.** Discussion is at most one poll interval behind (~5 minutes in production).
  A pull request that first appears through polling carries no diff stats or GraphQL-only
  reviews until the per-PR pass runs over it again.
- **`--file` has a recall floor** until rename chains exist: a thread that predates a file's
  last move cannot match on today's path.
- **Trust tiers are inert until `ghlored authority` has run.** On an org repository,
  maintainers whose write access comes through a team read as `MEMBER` and stay in
  `reported`, which empties the `authoritative` tier and therefore every `--kind rationale`
  query.
- **A sampled index declares its window**, and a recall number from one is only comparable
  against a baseline restricted to the same window. `status` prints it.
