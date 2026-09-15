# relore - searching the decisions behind the code

On 8 September somebody opened
[an issue](https://github.com/huggingface/transformers/issues/48630) on
Transformers: `GPTNeoXJapanese` crashes for any `rotary_pct != 1.0`, because
RoPE ignores `partial_rotary_factor`. Within a day a contributor had posted a
correct diagnosis, a maintainer had answered "a PR is very much welcome", and
two different people had each opened a fix. 

Two days later Serge, our agent, picked up the same issue and started 
writing a third without knowing there were already two. This happened 
because `gh issue view` did not link all those events together and 
it's easy to miss.
 
`relore` is what we built to answer that question directly. It is an index of a
repository's own history.

For that specific case, `relore` has the `inflight` verb, that digs and
provide a full overview of everything related to an issue:

```console
$ relore inflight 48630
2 threads claim to close huggingface/transformers#48630

1. huggingface/transformers#48652 pr  open (approved)  5d  closes  @blipbyte
   > [GPTNeoXJapanese] Fix RoPE ignoring partial_rotary_factor
2. huggingface/transformers#48672 pr  closed by its author  5d  closes  @truongsontung
   > fix: respect partial_rotary_factor in GPTNeoXJapaneseRotaryEmbedding
```

Knowing if someone else is doing something about it before doing any new work
spares a lot of work for an agent. 

The two other questions that are useful to ask are: *Why is it like this?* 
and *Is that still true?*

We need the index to let us know why a fallback cannot be removed, 
which approach was tried and rejected, what a maintainer made the 
last five contributors change. This knowloedge is not in the codebase,
it's reading all of the comments made in the issues and PRs over the years.

But that also needs to be verified against the latest HEAD to make sure a 
3 years old claim still hold, so grepping the code is still an important step.

## What relore is

`relore` (REpository LORE) indexes a repository's complete issue and
pull-request history — issues, PRs, comments, reviews, commit messages and the
relationships between them — and keeps a working clone of the repository beside
it, both served over one HTTP API.

![Relore sits between a repository's history and its current code, and answers an agent's questions against both.](relore-diag.png)

*History on one side, the code as it is today on the other, and one tool that
can answer across both.*

Three properties make it more efficient than a search box.

**Retrieval is at comment level.** GitHub Search finds text inside a comment and
returns the containing thread. The agent then fetches that thread and rereads it
to work out which comment matched, who wrote it and when. `relore` returns the
comment itself, with its author and its standing attached.

**Relationships are stored as edges.** `Fixes #N` is a link in the index, so
`inflight` is a lookup instead of a text search that might miss.

**The code is served too**, at HEAD, from the same API, with no checkout on your
side. "Is that still true?" costs one call.

The code side came first. An agent in one of our field tests reported that
`relore defs` gave it a better overview of a file than reading the whole thing,
**for roughly 5% of the tokens** — its own words, in the
[field report](https://github.com/huggingface/relore/issues/8) it filed at the
end of the run. `grep` and `rg` return matching lines and leave the model to
reconstruct the program structure around them. Asking for the structure first,
then reading only what matters, is an order of magnitude cheaper.

There are [proposals](https://arxiv.org/abs/2603.15566) to record decision
context explicitly in git history. We started from the opposite end: projects
should not have to change how they work. The knowledge already exists, trapped
in years of GitHub discussions.

## Provenance, and why it is hard

Not every piece of project history is equally authoritative. A maintainer
explaining why an approach was rejected is different from a contributor
speculating about a bug, or a bot posting generated text. `relore` puts that
difference in four tokens in front of every snippet.

Back on the GPTNeoXJapanese bug: the reporter's body claims *"this is a
regression from #39847"*. That is a contributor's attribution, and acting on it
means rewriting a model. One flag confirms it from the side that decides:

```console
$ relore search "standardize rope partial_rotary_factor refactor all models"
1. #43020  [contributor claim]   7mo  Add mimo v2 flash
2. #46121  [contributor claim]   3mo  `convert_rope_params_to_dict` raises `TypeError` …
3. #39847  [authoritative]      13mo  🚨 [v5] Refactor RoPE for layer types
4. #48241  [contributor claim]  22d   MiniMaxM2 silently applies full-head RoPE …

$ relore search "…" --trust authoritative
1. #39847  [authoritative]  13mo  🚨 [v5] Refactor RoPE for layer types   @zucchini-nlp
```

Same maintainer who answered the issue, and the 🚨 marks a deliberate breaking
change.

**The same flag is the wrong one to reach for next.** The maintainer's real ask
was "check all models", and one of those models is result 4, a contributor's bug
report. Raise the floor and it vanishes, because "I hit this error" is a report,
and reports come from anyone. **The tiers describe provenance, not quality.**
Raise the floor when you want judgements; leave it down when you want reports.

Provenance also has to be **time-aware**, and ours is not yet. The tier comes
from GitHub's `author_association`, which is computed when you ask rather than
stored against the comment. When a maintainer leaves the organisation,
everything they ever wrote quietly becomes a contributor claim. On
[#28056](https://github.com/huggingface/transformers/issues/28056), a 34-comment
argument about `use_cache`, the comment that settles the design is @gante's,
written while he maintained that part of the library; GitHub now returns
`CONTRIBUTOR` for it, and for his comments going back to 2023. Raising the floor
hides the answer.

We are fixing it ([#74](https://github.com/huggingface/relore/issues/74)):
merging a pull request is the write act, and it is dated, so who held the keys
and when is derivable from the history the index already holds. Provenance is
the most useful thing in the index and the easiest thing to get silently wrong.
The failure is never an error message. It is a page that looks complete.

## One run, end to end

The [field report](https://github.com/huggingface/relore/issues/8) for the bug
this post opened with is the whole trace. A cold agent, with no documentation
beyond `relore --help`:

- read the issue and separated the maintainer's comment from the contributor's,
  which is what reframed the task;
- learned the bug was one of several a refactor had regressed;
- searched `partial_rotary_factor` across history, which is how it found the open
  fix, crossing thread boundaries because the fix was not in the thread.
  `relore inflight` exists because of that call: finding it was a lucky side
  effect of a symbol search, and that failure mode deserved its own verb;
- found the refactor that caused the regression;
- then moved to code inspection for the repo-wide audit the maintainer asked for.

Every query was served from the local index. GitHub is contacted only by the
ingestion process, to keep the index fresh.

That last step is what `why` makes direct:

```console
relore why src/.../modeling_gpt_neox_japanese.py:90
```

`git blame` tells you which commit last changed a line. `relore why` follows
that commit to its pull request and surfaces the review comments around that
line, which is the discussion that explains why the change was made.

## What that changes

The triage run at the top could not ask any of the three questions above. Its
entire toolset was `grep`, `read_file`, `list_dir` and `fetch_url`, so "is
somebody already fixing this?" was unavailable to it at any budget.

The field-report run reached the root cause, the culprit PR and the already-open
fix in about ten calls. A [later run](https://github.com/huggingface/relore/issues/58)
on a bug it had never seen found the in-flight PR on its third call and came back
with two findings that neither the issue nor that PR contained, without cloning
the repository at all. Everything relore handed that session came to 25,767
tokens, a median of 306 per call. That is less in total than three reads of the
one file the triage job opened 53 times.

Those are different bugs in different harnesses, so this is not a controlled
benchmark and we are deliberately not putting a number opposite the 2.1M. A
session's bill is set by its harness and its turn count far more than by any one
tool, and quoting a ratio across two harnesses would measure the harnesses.

What changes is the path. The history of a project is a graph: issues link to
the pull requests that close them, reviews attach to the lines they are about,
commits carry the threads that argued them. An agent without that graph
rediscovers it one file read at a time, and some of it it cannot rediscover at
all. With it, a question that used to be a hundred file visits is a lookup along
an edge that already exists.

## Conclusion

We built `relore` because our agents needed a better way to remember
Transformers. But there is nothing Transformers-specific about the problem.

Mature open-source projects accumulate years of decisions in issues, reviews and
PR comments. Humans learn that lore slowly. Agents start every session knowing
none of it.

`relore` turns that history into something both can query, and connects it back
to the code that exists today.

It is open source, Apache 2.0, and works on any GitHub repository you are willing
to index.

## Try it

```bash
pip install "relore[postgres] @ git+https://github.com/huggingface/relore"

export RELORE_DATABASE_URL=postgresql://localhost/relore
export GITHUB_TOKEN=...   # issues:read + pull_requests:read, never write

relored migrate
relored backfill --repo owner/name    # the history
relored authority --repo owner/name   # who had write access
relored clone     --repo owner/name   # the working clone the code verbs read
```

`relored` owns the database; `relore` is a thin client and never touches it, so
the index goes behind an HTTP API and the client points at it:

```bash
relored serve --port 8080             # in another terminal
export RELORE_API=http://localhost:8080

relore search "why is this cast here" --kind rationale --file src/model.py
relore why src/model.py:42            # blame → the pull request → what reviewers said there
```

Set `RELORE_API`, or the client talks to our own deployment and cannot reach
it; `relore status` will say so.

A full history is resumable and takes about a day. `relored sample --repo
owner/name --since YYYY-MM-DD` indexes a window in minutes if you want something
smaller first. The clone is optional for the history verbs and required for the
code side: without it `grep`, `symbol`, `copies`, `defs`/`refs` and `why` answer
`503` naming that command. `relore --help` is the reference.

The code is at [huggingface/relore](https://github.com/huggingface/relore).
Issues and pull requests are welcome.
