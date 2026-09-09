# Worked examples

Setup, install and token wiring are in the [README](../README.md#quickstart); the *why* is
in the build plan, which is held with the deployment that commissioned it. This page is neither — it is what the queries actually
look like, and the four ways they come back empty when the index is healthy.

Every command below was run against a real index of `huggingface/serge`: 114 threads, 415
documents, Postgres backend.

---

## Query like this, not like that

**A query is an AND of every content term.** The backend uses `plainto_tsquery`
deliberately — it is the one thing FTS5 can also express, so both dialects answer the same
question instead of diverging quietly (§4.1). The practical consequence is the single
biggest cause of a disappointing result:

| query | hits |
| --- | --- |
| `429` | 3 |
| `timeout budget` | 2 |
| `rate limit` | 4 |
| `crash when the optional mask is missing` | **0** |

Two or three distinctive terms, not a sentence — that table is what one AND-ed sentence
costs you.

**A pasted traceback is the exception, and it used to be the worst case.** §6's query
expansion now fans one call out into capped error, test-id, symbol, file and free-text
legs and merges them, so the traceback's lines stop being required terms and become
separate questions. Either form works:

```bash
ghlore search "$(pbpaste)"             # the traceback, expanded server-side
ghlore search --error "$(pbpaste)"     # the traceback, as a structural filter
ghlore search "optional mask dtype" --no-expand   # exactly one question, no fan-out
```

Expansion also reads a bare identifier as one: `ghlore search "_maybe_import_sdnq"` asks
the symbol filter as well as the text, without you having to say so. It never *loses* a
result the plain query would have found — the caller's own query is always one of the legs
— and a query with no identifier, path or traceback in it expands to that single leg and
costs nothing extra.

## An error you just hit

The highest-value shape. It lands on the PR that fixed it:

```
$ ghlore --compact search "429" --limit 2

1. huggingface/serge#92 pr  [authoritative]  15d  body  @tarekziade
   Survive a rate limit instead of losing the task to it
   ## Why One 429 ended the `deepseek_vl` task on 2026-08-18 *after it had already
   spent 1.13M input tokens*…
   https://github.com/huggingface/serge/pull/92
```

`--compact` and `--json` are **global** flags — they go before the verb:

```bash
ghlore --compact search "429"      # correct
ghlore search "429" --compact      # error: unrecognized arguments
```

Every hit carries its **age** (`15d`) and its **trust tier** (`[authoritative]`). Age
changes what a model concludes; authority changes it more (§6.2).

## "Is this intentional?" — raise the trust floor

```
$ ghlore --compact search "repeat guard" --trust authoritative

2 hits · trust floor: authoritative
1. huggingface/serge#99 pr  [authoritative]  5d  @tarekziade
   Count what a session spent, and what ended it
```

A drive-by comment outranking a maintainer's one-line correction is *worse* than an empty
result: the agent acts on it and nobody checks. `--trust` may only **raise** the floor.

Better, pass `--kind` and let the **server** choose it — a caller then gets the policy right
without knowing it:

```bash
ghlore search "<terms>" --kind rationale   # floor: authoritative
ghlore search "<terms>" --kind failure     # floor: any human tier -- a stranger's
                                           # traceback is real evidence
ghlore search "<terms>" --kind precedent   # authoritative, or any human tier on a merged
                                           # PR: merging is the maintainer's act, so a
                                           # first-time contributor's merged change counts
```

`trust_floor` in the `--json` response reports what was actually applied.

Both are inert until `ghlored authority` has run: maintainers whose write access comes
through a team read as `MEMBER` and stay in `reported`, leaving the `authoritative` tier
empty.

## "What did our own bot claim here?"

Machine-authored documents are excluded from every default result set, because returning
one as prior discussion makes an agent's unreviewed output its own evidence (§11). Asking
for the tier explicitly is the only way to see them, and they arrive labelled:

```
$ ghlore search "review" --trust machine

1. huggingface/serge#26 pr  [MACHINE — our own bot, not evidence]  2mo  @sergereview
```

## One thread, without all of it

```
$ ghlore thread 92 --focus "backoff retry"
...
-- 0 of 1 comments --
(1 not shown: a thread is never returnable in full. Narrow it with a focus query.)
```

`--focus` selects comments by relevance to *that* query. A 200-comment thread has no full
form; `search` is capped at 10 hits and 400 characters of snippet. The caps are the
contract, not a default (§6).

## The offline verbs

`map`, `defs` and `refs` need **no server, no index and no network** — they read the
checkout you are standing in, uncommitted edits included, which is the whole point (§1).

```
$ ghlore defs ghlore/ingest/authority.py
43-56        class     AuthorityResult
59-117       function  resolve_authority
120-156      function  _resolve_one

$ ghlore refs resolve_authority
./ghlore/daemon.py:211
./ghlore/ingest/backfill.py:117

$ ghlore map ghlore/ingest
48 definitions in 10 files, top 40 by callers
  parse_timestamp            function  10 callers   ghlore/ingest/timestamps.py:19
  derive_thread              function  5 callers    ghlore/ingest/index_thread.py:89
```

---

## Empty results that are not faults

**No results is always exit 0 with an empty result.** So when something comes back empty,
it is one of these before it is a bug:

**1. The query was a sentence.** See the table above.

**2. A structural filter was involved.** `--file`, `--symbol`, `--error` and `--test` read
the signal tables that build-plan §5.3's extraction pass fills, and they *AND* with the text
query — so one of them narrows an otherwise-good search:

```
$ ghlore search "429"                              → 3 hits
$ ghlore search "429" --file reviewbot/llm.py      → 0 hits   # nothing said both
```

Three things make one match nothing on an index that does hold the answer. `--file` takes
the path **as the repository spells it**, not an absolute one from a traceback — mapping
those needs the working clone of milestone 4. `--test` takes a runner id
(`tests/test_x.py::test_y`, with or without its `[params]`), not a bare function name; a
bare name is a `--symbol`. And `--error` may be given a whole pasted traceback, which is
normalized into the form the index stores — but a *paraphrase* of an error is text, so pass
it as the query instead.

**3. A trust floor removed everything.** `--trust authoritative` — or `--kind rationale` /
`--kind precedent`, which imply it — on an index where `ghlored authority` never ran returns
nothing at all. Check `trust_floor` in the `--json` response.

**4. Two verbs are parsed but not built.** They say so rather than returning an empty
result that reads like an answer:

```
$ ghlore precedent --kind bug_fix
ghlore precedent: not implemented yet (milestone 4, the build plan section 13)
$ ghlore why ghlore/cli.py:10
ghlore why: not implemented yet (milestone 4, the build plan section 13)
```

A daemon that is *down* is a different message from an index that is empty, and the client
tells you which — that distinction is deliberate (§12).

## Checking the index rather than the query

```
$ ghlore status
backend   postgresql / ts_rank_cd  capabilities: fulltext
schema    applied [1, 2, 3], pending []
index     114 threads, 415 documents, 332 raw objects
  huggingface/serge [threads] high-water 2026-09-03T06:55:06+00:00 last-ok …
quota     1/60 per minute, 1/5000 today
```

`capabilities` is how you tell which engine answered. A SQLite index scores with `bm25` and
has no trigram or vector tier, so a result set from one says nothing about the other —
which is why `ghlored serve` refuses a SQLite URL without `--allow-sqlite`, and why §10's
benchmark refuses to mix backends.
