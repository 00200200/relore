# ghlore — GitHub project memory

**Index a repository's complete issue and pull-request history, and serve it back as
searchable project memory for coding agents and humans.**

The knowledge that explains *why* a codebase is the way it is mostly does not live in the
codebase. It lives in a review comment from three years ago: why a fallback cannot be
removed, which approach was tried and rejected, what a maintainer made the last five
contributors change. An agent fixing a bug cannot read any of it. `ghlore` makes it
queryable.

## Try it

```bash
# No PyPI release yet, so install from main -- `pip install ghlore` would fetch
# whatever else owns that name.
pip install git+https://github.com/huggingface/ghlore
export GHLORE_API=https://your-ghlore  # a running `ghlored serve`
export GHLORE_TOKEN=…                  # if that daemon requires one

ghlore search "AttributeError: 'NoneType' object has no attribute 'shape'" --kind failure
ghlore search "why is this cast here" --kind rationale --file src/model.py
ghlore thread 47720 --focus "cropping"
```

The daemon's own web page carries worked examples, the CLI setup for *that* index, and a
snippet for a `CLAUDE.md` / `AGENTS.md`. There is no MCP server on purpose: any agent with
a shell can already call an HTTP API.

Running it needs a Postgres and a GitHub token with `issues:read` + `pull_requests:read` —
never write. `deploy/` has a Helm chart and the scripts that are its interface.

## Why not GitHub search

For one-off human lookups `/search/issues` is often the right tool: authoritative,
instantly fresh, free, and it searches every repo at once. As an *agent's* retrieval layer
it falls down. Measured against `huggingface/transformers` (47,513 threads, 134,841 inline
review comments):

| | GitHub search | ghlore |
| --- | --- | --- |
| Comment-level results | searches comments but **returns the thread** — no snippet, no location | the matching document, with author, URL, and file/line |
| Ranking | `best-match`, opaque | explicit weighted terms, tunable |
| "which threads touched this file" | no qualifier exists | indexed join |
| "this bug and its merged fix, as one record" | not expressible | first-class `precedents` |
| Throughput | **30 req/min, shared across the token** | a database query |
| Determinism | ranking may drift | same query, same answer |

Two rows carry it. **Comments:** searching that repo for
`_prepare_4d_causal_attention_mask` returns 26 threads — 19 matched in *comments*. Three
quarters of the mentions of a technical symbol live in comments, and for each one GitHub
hands back a thread number, leaving the caller to re-read the thread to find what matched.

**Throughput:** 30 req/min is shared by everything using the token — the same token the
triage dispatcher and review bot already spend. One serge task session makes 30–150 tool
calls. Once indexed, **`ghlore` answers every query from Postgres and makes no GitHub
request at all**; ten agents in parallel cost the same as one. The rate-limited surface is
confined to ingest. The trade is freshness: an answer is at most one poll interval behind.

The deeper reason is not text search: **GitHub search has no schema to join on.** Files,
symbols, test ids, bug↔fix pairs — no qualifier syntax reaches them.

## What it does

```text
  GitHub REST + GraphQL          a blobless git clone
          |                              |
          |  poll (delta)                |  the lens: line -> name, at each
          |  + backfill (full history)   |  document's own commit; renames
          v                              v
     ghlored index  ------>  Postgres  (threads, documents, signals, precedents)
                                |
                                |  full-text + trigram + exact-token, weighted
                                v
                          ghlored serve  ->  HTTP JSON API + web UI
                                |
                  ghlore (CLI)  |  a browser
                        |
                        |  map / defs / refs: the same parser, run locally
                        v  against the working tree. No server, no network.
                  your checkout
```

**Indexed:** titles, bodies, every conversation comment, every review submission, every
inline review comment (with file and line), commit messages, changed-file lists for merged
PRs, labels, state, and the issue↔PR links connecting a report to the change that closed it.

**Extracted as exactly-matchable signals**, because for technical retrieval the
discriminating token is usually exact rather than semantic: exception types and normalized
error messages, file paths, symbols, test ids, commit shas.

**Not indexed: your source code.** No source text is stored server-side and no API
response returns any. Code is nevertheless *parsed*, because a line number is not a durable
address and a name is:

- **Server-side, at index time — the lens.** Reading the tree at a document's own commit
  turns `foo.py:412` into `Gemma3Model.forward`, so "every review comment ever left inside
  this function" survives the file being edited. Also gives rename chains, and
  `ghlore why <path:line>`. Footprint: two nullable columns and a rename table.
- **Client-side — `map` / `defs` / `refs`.** The same parser against your dirty checkout.
  Local and offline, and therefore correct about the branch you are on, which a remote
  index never is.

**Languages are plugins.** A provider claims file patterns and declares what it can do
(`defs`, `extents`, `refs`). tree-sitter providers are the good path — they handle the
broken files an agent is halfway through editing — and a `ctags --output-format=json`
provider is the breadth fallback, so an unsupported language degrades to definitions
rather than nothing. Python ships first. The fallback needs **universal**-ctags: the BSD
ctags macOS ships as `/usr/bin/ctags` has no `--output-format`, so the provider probes for
the real thing and reports itself unavailable rather than returning an empty list.

## Two binaries, on purpose

| | reads | writes | needs |
| --- | --- | --- | --- |
| `ghlore` | the HTTP API; your local checkout | nothing | an API URL + a scoped token — the code verbs need neither |
| `ghlored` | GitHub, Postgres | Postgres | a GitHub token + a database |

A security boundary, not packaging taste. `ghlore` — the binary agents get — **cannot open
a database connection, because the code to do so is not in it**, asserted by a test over
the import graph. Parsers and the server are optional extras, so the sandbox install stays
thin.

## Quickstart

```bash
GH=git+https://github.com/huggingface/ghlore   # no PyPI release yet; install from main
pip install $GH                       # client only
pip install "ghlore[postgres] @ $GH"   # the daemon

# server side
export GHLORE_DATABASE_URL=postgresql://localhost/ghlore   # or sqlite:///ghlore.db, dev only
export GITHUB_TOKEN=...                                    # issues:read + pull_requests:read
ghlored migrate
ghlored backfill --repo owner/name           # full history; resumable, ~a day
ghlored poll     --repo owner/name --interval 5m
ghlored sweep    --repo owner/name           # weekly: reconcile deletions

# section 10's benchmark: mine candidates, fold the UI's labels in, score the baselines
ghlored mine  --repo owner/name --out benchmarks/owner-name.jsonl
ghlored judge --set benchmarks/owner-name.jsonl --labels labels.jsonl
ghlored bench --repo owner/name --set benchmarks/owner-name.jsonl \
  --system index --system index+expand --system github
# --system grep --clone <checkout> adds the second baseline; it reads a working tree, so
# point it at a detached worktree of origin/main, not a branch someone is working on

# serving. One token per consumer, each scoped to repositories:
#   secret:repos[:scopes]   repos is a comma list or *, scopes adds `label` for the UI
export GHLORE_API_TOKENS="tok_agent:owner/name tok_ui:*:label"
export GHLORE_LABELS_PATH=./labels.jsonl     # enables the UI's relevance labelling
ghlored serve --port 8080 --host 0.0.0.0
# With no tokens set, serve refuses anything but --host 127.0.0.1; with a SQLite URL it
# refuses outright unless you pass --allow-sqlite.

# client side
export GHLORE_API=http://localhost:8080
export GHLORE_TOKEN=tok_agent                # only if the daemon requires one
# A query is an AND of every content term, so two or three distinctive ones beat a
# sentence. A pasted traceback works too: section 6's expansion fans it out into error,
# file and symbol legs and merges them. See docs/cli.md.
ghlore search "optional mask dtype" --limit 5
ghlore search "repeat guard" --trust authoritative   # only what a maintainer settled
ghlore search "poll wedged" --repo owner/name        # narrow the token's scope
ghlore search "flaky teardown" --sort newest         # same hits, most recent first
ghlore search "$(cat failure.txt)"                   # a whole traceback is one call
ghlore search "optional mask dtype" --no-expand      # ask exactly one question
ghlore thread 12345 --focus "optional mask"
ghlore status                         # counts, freshness, and which search backend answered
ghlore map --limit 40                 # ranked repo map, no server needed
ghlore defs src/models/foo.py
ghlore refs forward
ghlore why src/models/foo.py:412      # milestone 4
```

The filter flags are repeatable — a traceback names more than one file. They read the
signal tables that §5.3's extraction pass fills, and **they AND with the text query**, so
passing one narrows an otherwise-good search rather than widening it. `--error` takes a
whole pasted traceback: the request is normalized into the same form the index stores
(§13.2 #3).

Run `ghlored authority` after backfilling an org-owned repository, or `--trust
authoritative` returns nothing: maintainers whose write access comes through a team report
as `MEMBER` and stay in `reported` until it does.

`ghlored sample --repo … --since YYYY-MM-DD --kind issue|pr [--merged]` indexes a bounded
window instead of a history, fetching **each thread whole** — §10's evaluation set needs a
corpus, not a day of API budget. It is deliberately not `backfill --since`: that would walk
`/issues/comments?since=`, which filters individual comments and would index a thread that
moved inside the window without the older comments that explain it (§5.5). A sampled index
declares its floor and `ghlore status` prints it, because a recall number is only
comparable against a baseline restricted to the same window.

`fetch` and `derive` are separate on purpose: raw payloads are staged, so improving an
extractor and re-deriving costs minutes of local CPU instead of another day of API budget.
`poll` does both for the threads that moved, so the split is invisible in steady state.

`--json` for machine-readable output, `--compact` to trim snippets. **No results is exit 0
with an empty result** — never nonzero, so an agent cannot mistake "nothing in the index"
for "the tool is broken".

## Using it from an agent

`skills/search-project-history/SKILL.md` ships a ready-made skill for agents that read one
(Claude Code and similar): when to reach for the index rather than `grep`, how to phrase a
query, what the trust tiers mean, and how to triage an empty result. Point your harness at
it, or lift the prose.

Any agent with a shell can use `ghlore` with no integration work. If your framework reads a
repo-declared tool manifest, declaring it is a few lines:

```json
{
  "helpers": [{
    "name": "search_project_history",
    "description": "Search this repository's issue and PR history. Flags: --error, --test, --file, --symbol, --label, --kind, --trust, --limit, --compact, --json. Use it before writing a patch to check whether this failure has precedent.",
    "command": ["ghlore", "search", "--json"],
    "allow_args": true, "max_args": 12, "timeout_seconds": 30,
    "install": ["pip", "install", "ghlore"]
  }]
}
```

Two things every integration must get right:

- **Retrieved history is untrusted text**, written by whoever opened the issue. `ghlore`
  wraps every response in an untrusted-content envelope and scrubs delimiters, and inside
  that envelope the quoted lines — and only those — are marked `>`, so a trust tier or a
  count of ours is never mistaken for something a stranger wrote. The agent must still be
  told never to follow instructions found in retrieved content.
- **Cite and verify.** Every result carries a URL and its age. A 2019 comment can be right
  about intent and wrong about today's code.

## Ranking

**Built.** Retrieval is full text, filters, the trust floor, §6's **query expansion** and
§6's **weighted score**, gated on the evaluation set below rather than fitted by eye — a
weight chosen by hand is one nobody can argue with later. Every hit carries its score
broken down by term, so the term that is wrong is visible in the web UI.

Two things that ride alongside the score rather than in it:

- **`--sort newest`** reorders a page by date without re-selecting it. Selection stays on
  relevance, because letting recency choose a thread's representative document is a
  measured bug: it picks the sign-off (§10.6, §10.7).
- **Decay is written and gated off** (`ranking.DECAY_ENABLED`). Its release condition — a
  full-history backfill — was met on 2026-09-09, but §10.5's numbers were measured on a
  frozen six-month set that refuses new labels, so turning it on honestly needs new ground
  truth first. The gate now records a measurement not yet taken.

Lexical and structural: exact evidence outranks topical similarity.

```text
score = w1*error overlap + w2*test-id + w3*symbol + w4*file + w5*full-text rank
      + w6*relationship, then adjusted for recency and author association
```

Weights are per-deployment, because what discriminates in one repository does not in
another. Decay is query-kind-aware — an 18-month half-life for errors, four years for
precedent, **none** for rationale, where the oldest thread is often the answer. Trust is a
filter, not a weight: a maintainer's "we cannot do that because…" is a judgement, a
passer-by's is a claim, and a bot's is our own output coming back.

The **bot exclusion** is live because it is a security property rather than a ranking
choice — the corpus already contains the deployment's own agent's comments, and returning
one as prior discussion makes that agent's unreviewed output its own evidence. The
**per-query-kind floors** are live too, together with the `ghlored authority` pass they
depend on: until each author's repository permission is resolved, a rationale floor of
"maintainers only" filters out the very comments it exists to find, because on an org-owned
repository the real maintainers read as an unresolved `MEMBER`. Measured before that pass
existed: zero of 415 documents were `authoritative`.

### What the benchmark says

Two sets, both model-judged, both scored against **both** baselines — GitHub search and the
agent's own `grep`. Postgres `ts_rank_cd`, window 2026-03-01, and every baseline restricted
to that same window (`grep` reads a working tree, which has no window at all). Recall@10:

| corpus / slice | n | unranked | **+ expansion** | GitHub | `grep` |
| --- | --- | --- | --- | --- | --- |
| transformers `failure` | 48 | 0.938 | **1.000** | 0.875 | 0.000 |
| transformers `precedent` | 40 | 0.375 | **0.475** | 0.325 | 0.200 |
| transformers `rationale`, pooled | 51 | 0.765 | **0.980** | 0.294 | 0.314 |
| transformers `rationale`, leak-free | 7 | 0.286 | **0.857** | 0.857 | 0.143 |
| bot-reviews `rationale`, leak-free | 9 | 0.222 | **1.000** | 0.667 | 0.667 |

**Four things to know before quoting any of it**, because the interesting parts are the
caveats:

1. **The pooled `rationale` figure is mostly a leak.** A *human* reviewer's finding is
   itself a document in the answer's own thread, so any lexical system reaches the answer
   without retrieving anything. Only a *bot's* finding is clean, because §6.2 excludes
   machine documents from reads — hence the separate leak-free rows, which are the ones
   that mean something. The bot-reviews corpus (diffusers and mlinter, where bot review
   comments are 4.7% and 54% of the traffic against transformers' 0.9%) exists purely to
   make that slice wider than seven examples.
2. **Unranked, `ghlore` lost that slice on both corpora** — 0.286 and 0.222 — and the cause
   was *under-retrieval*, not ranking: 6 of 9 queries returned nothing, because the query is
   drawn from the bot's finding and that finding is the only document carrying all its
   terms, which §6.2 refuses to read. Search ANDs content terms, so it asked for the one
   document the index will not return.
3. **Query expansion fixed it** (§10.4): fanning one call into error, test, symbol, file and
   free-text legs — plus the caller's own filters asked without their text — and merging
   them. Every slice improved and none regressed. The two leg families do disjoint work,
   which is measured: the filters leg alone fixes `failure`, the derived legs alone fix
   `precedent`, `rationale` needs both.
4. **`grep`'s zero on `failure` is the sharpest result in the set, and it is structural.**
   Those answers are mostly *issues*, and grep's only route from a file to a thread is "the
   threads that touched this path" — a PR-shaped relation. Traced over every candidate
   rather than the top ten, the answer is in grep's list for 13 of 48 examples and never
   above rank 26.

5. **The weighted ranking closed the one gap expansion left** (§10.5). Expansion raised
   `precedent`'s recall while its MRR stayed flat at 0.191 — it *found* the precedent thread
   and could not order it, because a textless leg scored every row 0. The weights moved that
   MRR to **0.286** at Recall@10 0.525. The mechanism is not the weight values: scoring each
   leg against the *whole* question rather than against its own single filter is the entire
   delta, and keeping the weights while dropping that reproduces the pre-weights numbers
   exactly. A weight can only discriminate over evidence the filter did not already require.
6. **Then a single real query found what 139 examples could not** (§10.6). Asked with a
   file filter, nine of ten slots came back `LGTM`, `Thx`, `Yep` — because the term that
   scores text a query is *not* filtering on was a conjunction, so almost nothing scored
   above zero and the page fell through to a recency tie-break that picks each thread's
   last comment. Filtering ANDs; scoring ORs. **Recall@k is blind to this**: a page of ten
   slots where nine are noise scores identically to ten useful ones, as long as the answer
   is on both. A recall number is necessary and not sufficient.

Two limits the table does not show. `failure` is close to circular — its ground truth is
"the threads carrying this error" and `--error` matches exactly that — so no weight set can
be fitted on it, and it is the one slice that lost a little MRR (0.938 → 0.927, at unchanged
ceiling recall) when the weights landed. That was left uncompensated on purpose. And the
weight *values* are not yet falsifiable on this corpus at all, for the reason in point 5 —
`thread_links` and a live `w_rel` are milestone 4's first chance to test one.

Vector search is deliberately **not** in v1: the column and extension are provisioned,
nothing writes them, and turning them on is gated on a measured benchmark.

## Security

- The index is **read-only to every client** — the API connects with a `SELECT`-only role.
- **Repository allowlist, not denylist**, re-checked per thread, so a private repository
  cannot leak into a public answer.
- **Per-token repo scoping.** A client token cannot reach outside its set.
- **Untrusted-content envelope** on every response, server-side and never optional.
- **Secrets redacted at ingest.** Public data is not the same as safe to re-serve.
- **No write path, for anyone.** Every document has a human author and a URL, so an agent's
  wrong conclusion can never become project memory the next agent retrieves as evidence.
  The single writing endpoint — the web UI's relevance labelling, which feeds the benchmark
  — appends to a JSONL file, is gated on a token scope, and is retrieved by no query. The
  property is not "nothing writes"; it is "nothing an agent produces becomes evidence".
- **A daemon with no tokens configured will not bind anything but loopback**, and refuses a
  SQLite index outright without an explicit flag: a laptop index served to a team is how
  "the ranking is bad" becomes unfalsifiable.

## Prior art

- **GitHub search** — complementary, and the benchmark baseline. If `ghlore` does not
  clearly beat it, the index is not earning its operating cost.
- **`opencode-semantic-memory` / GHMEM** — an MCP server with GitLab bulk ingest, the
  closest thing that exists. It embeds issue **title, description and labels**; comments,
  reviews and inline review comments are not ingested, which is the whole ballgame here.
  Forge data is a deliberate second-class citizen there and the only corpus here, its core
  loop is agent-*authored* memory where `ghlore` has no write path, and it is a
  per-developer local daemon on ~2 GB of `torch` where this is a shared service with a thin
  client. If what you want is "my agent should remember yesterday", use theirs.
- **`yksanjo/gmem`** — agent-authored memory for a Solana workspace. Different corpus;
  worth reading for its append-only `(kind, natural_id, version)` entity model.

Borrowed, with credit: splitting fetch from derive (GHMEM), rendering a result's age,
secret redaction on ingest, LLM-free keyword extraction, progress visible during a
multi-hour backfill, and append-only entity versioning (`gmem`).

## Docs

- [`AGENTS.md`](AGENTS.md) — the operating contract: invariants that have tests behind
  them, the API limits that make a backfill look successful while missing most of the
  corpus, and what was deferred with a reason.
- **The build plan** — scope, data model, delta detection, API and CLI contracts,
  milestones. It is the specification this codebase is written against, and every `section
  N` reference in the source points into it. It lives with the deployment that commissioned
  it rather than in this repository, because it carries operational detail about that
  deployment; ask its operator for a copy.
- [`docs/cli.md`](docs/cli.md) — worked query examples against a real index, and the four
  ways a healthy index returns nothing.
- **The use-cases companion** — the evidence base the build plan argues from, including
  where this tool would have changed nothing. Held alongside the build plan, and for the
  same reason.

A Jekyll site over `docs/` is planned, not built. The markdown here is the source of truth.

## License

[Apache-2.0](LICENSE).
