# relore - searching the decisions behind the code

On 8 September somebody opened
[an issue](https://github.com/huggingface/transformers/issues/48630) on
Transformers: `GPTNeoXJapanese` crashes for any `rotary_pct != 1.0`, because
RoPE ignores `partial_rotary_factor`. Within a day a contributor had posted a
correct diagnosis, a maintainer had answered "a PR is very much welcome", and
two different people had each opened a fix —
[#48652](https://github.com/huggingface/transformers/pull/48652) and
[#48672](https://github.com/huggingface/transformers/pull/48672) — neither
aware of the other. The second one's author closed his own PR the next morning.

Two days later one of our agents picked up the same issue and started writing a
third.

It was not being careless. This is the whole of what the issue hands you:

```console
$ gh issue view 48630 --repo huggingface/transformers --comments
author:	vinitsonawane45
association:	none
--
I investigated this issue and confirmed the root cause. `GPTNeoXJapaneseAttention`
correctly applies `partial_rotary_factor` when calculating `rotary_ndims`, but
`GPTNeoXJapaneseRotaryEmbedding.compute_default_rope_parameters()` still uses the
full `head_dim` […]

I'd be interested in implementing this. Since you mentioned that you already have
a fix and regression test ready, would you prefer to open the PR yourself […]
--
author:	zucchini-nlp
association:	member
--
A PR is very much welcome @blipbyte , it had a few regressions under the linked PR
and some of them were fixed by Cyril recently. I suppose we still missed a few
models, so would be really cool if you can check all models and revert
`partial_rotation` where it got deleted
--
```

Two comments. A diagnosis and a "yes please" — and a third person, in the first
one, offering to write the patch as well. Not one of them mentions that two
already exist. Both pull requests say `Fixes #48630` in their bodies, so GitHub
does know; it records that as a cross-reference in the issue's *timeline*, which
is not something an agent reading the thread ever sees.

One call answers it:

```console
$ relore inflight 48630
2 threads claim to close huggingface/transformers#48630

1. huggingface/transformers#48652 pr  open (approved)  5d  closes  @blipbyte
   > [GPTNeoXJapanese] Fix RoPE ignoring partial_rotary_factor
2. huggingface/transformers#48672 pr  closed by its author  5d  closes  @truongsontung
   > fix: respect partial_rotary_factor in GPTNeoXJapaneseRotaryEmbedding
```

Two people had already written this patch. One of them threw his away. The
question "is somebody already fixing this?" had an answer the whole time, and
it was one join away from the issue the agent was reading.

The maintainer's comment on that thread is worth more than either fix:

> it had a few regressions under the linked PR and some of them were fixed by
> Cyril recently. I suppose we still missed a few models, so would be really
> cool if you can check all models and revert `partial_rotation` where it got
> deleted

The task was never "fix this model". It was "a refactor broke several models,
some are already repaired, find the rest." No amount of reading
`modeling_gpt_neox_japanese.py` tells you that. The agent's own
[field report](https://github.com/huggingface/relore/issues/8) calls it the
highest-value output of the session.

That is the shape of the problem. The other half is what it costs. A few weeks
ago Serge, one of the agents we use to maintain Transformers, spent 2.1 million
input tokens on a single problem and produced no fix. It made 153 tool calls;
137 of them re-opened a file it had already opened — `modular_blt.py` 53 times,
a different slice of it each time. Those numbers are from our own job store,
for one run of the nightly integration-failure triage. The calls themselves are
cheap. What is expensive is the turn around each one, because every turn
re-sends the whole conversation.

Neither agent was doing anything obviously wrong. Transformers is a very large
codebase, reading it one file at a time is expensive — and the things it does
not contain are not in it at any price.

## Browsing the code

`grep` and `rg` are great tools, and coding agents use them constantly to find relevant snippets 
in large codebases. But they return matching lines without much structure or prioritization. 
The LLM still has to reconstruct the surrounding program structure from those results.

You can reduce the cost by pruning or compressing those interactions, 
but it is still a text-based workflow that leaves extra work to the model.

Tools like `ctags` and `tree-sitter` solve this by providing a searchable symbols 
index, and you can find numerous projects out there that will do exactly 
that and surface it in a CLI for your agent.

So we first added structural code navigation of our own and exposed it through
a new tool: `relore`.

In one of our field tests, the agent reported that `relore defs` gave it a
better overview of a file than reading the whole thing, for roughly 5% of the
tokens — its own words, in the
[field report](https://github.com/huggingface/relore/issues/8) it filed at the
end of the run.

Instead of loading the implementation, the agent can first ask for its structure:

```
relore defs path/to/model.py
```

and only read the parts it actually needs.

But after optimizing code browsing, we ran into a harder problem: sometimes the
information Serge needs isn't in the code at all.


## Understanding decisions

Another problem we ran into is that a checkout contains the current state of a
project. It doesn't contain the project's memory.

So a PR that seems correct may be rejected by a contributor with a reference
to an old attempt on another closed PR or a link to the right solution explained 
in another buried comment.

Or worse: your agent burns tokens while a fix is already there waiting for review.

Questions we want answers to:

- Is somebody already fixing this?
- Was this approach tried before?
- Why is this line written this way?
- How have maintainers fixed this class of failure in the past?
- What did maintainers explicitly reject?
- Is an old decision still true in the current code?

This is the project's "lore." There have been
[proposals](https://arxiv.org/abs/2603.15566) to make this kind of context
explicit and structured in git history. Our starting point is different:
projects don't need to change how they work. Much of that knowledge already
exists—it's just trapped in years of GitHub discussions.

GitHub Search is often the right tool for one-off human lookups—fresh, authoritative, 
cross-repo—and falls down specifically as a high-throughput agent retrieval layer:

- it exposes a best-match ranking, but it is opaque and isn't designed around
the kinds of evidence our agents care about.
- it's rate limited and our swarm of agents can quickly hit the cap
- it can find text inside comments, but the result is the containing thread.
The agent then has to fetch and reread that thread to discover which comment
matched, who wrote it, and when.


## Meet Relore: repository memory

`relore` (REpository LORE) is a searchable memory for a GitHub repository.

It indexes issues, PRs, comments, reviews, commit messages and the
relationships between them. Alongside that history it keeps a working clone of
the repository, so an agent can move between “what did people say?” and “what
does the code look like now?” without loading either world wholesale into its
context.

![Relore sits between a repository's history and its current code, and answers an agent's questions against both.](relore-diag.png)

*History on one side, the code as it is today on the other, and one tool that
can answer across both.*

Not every piece of project history is equally authoritative. A maintainer
explaining why an approach was rejected is different from a contributor
speculating about a bug, or a bot posting generated text — and the difference is
four tokens in front of a snippet.

Back on the GPTNeoXJapanese bug. The reporter's own body says *"this is a
regression from #39847"* — but that is a contributor's attribution, and acting
on it means rewriting a model. Asked plainly, confirming it puts the answer on
the page without putting it first:

```console
$ relore search "standardize rope partial_rotary_factor refactor all models"
1. #43020  [contributor claim]   7mo  Add mimo v2 flash
2. #46121  [contributor claim]   3mo  `convert_rope_params_to_dict` raises `TypeError` …
3. #39847  [authoritative]      13mo  🚨 [v5] Refactor RoPE for layer types
```

One flag:

```console
$ relore search "standardize rope partial_rotary_factor refactor all models" --trust authoritative
1. #39847  [authoritative]  13mo  🚨 [v5] Refactor RoPE for layer types   @zucchini-nlp
```

The attribution is confirmed from the side that decides: authored by the same
maintainer who answered the issue, and carrying the 🚨 that marks a deliberate
breaking change. A contributor's claim and an authoritative thread agreeing is
stronger than either alone, and it took one flag rather than a diff.

**The flag is also the wrong one to reach for next, which is the part worth
internalising.** The maintainer's real ask was *"check all models"*, and one of
the models is in that first page — `#48241, MiniMaxM2 silently applies full-head
RoPE`, a contributor's bug report. Raise the floor and it disappears, because
"I hit this error" is a report and reports come from anyone. The tiers are not a
quality ranking. Reports and judgements are different things, and the floor is
for the second.

**The tier is not our judgement, which is the point and also the limit.** It
comes from GitHub's own `author_association`, narrowed against write access — and
that association is not a fact about the comment, it is computed when you ask.
So when a maintainer leaves the organisation, everything they ever wrote quietly
becomes a contributor claim.

On [#28056](https://github.com/huggingface/transformers/issues/28056) — a
34-comment argument about `use_cache` and gradient checkpointing — the comment
that settles the design is @gante's, written while he maintained that part of
the library. He has since moved on, so GitHub now returns `CONTRIBUTOR` for it,
and for his comments going back to 2023. Raising the floor hides the answer.

We are fixing it
([#74](https://github.com/huggingface/relore/issues/74)): merging a pull request
*is* the write act and it is dated, so who held the keys and when is derivable
from the history the index already holds — no API call, and no trusting the
present tense about the past. It is a good example of what this whole project
keeps running into. Provenance is the most useful thing in the index and the
easiest thing to get subtly, silently wrong, and the failure is never an error
message: it is a page that looks complete.

Until then, the tier is a lens rather than a gate. Every page labels both tiers
and hides neither, and raising the floor is something to do when the question is
specifically "what did the people who decide think" — remembering that it is
GitHub's answer to who those people are today, not ours, and not the repository's
answer at the time.

The [field report](https://github.com/huggingface/relore/issues/8) for the bug
this post opened with is the whole trace. A cold agent, with no documentation
beyond `relore --help`:

- read the issue and immediately separated the maintainer's comment from the
  contributor's, which is what reframed the task;
- learned the bug was one of several a refactor had regressed;
- searched `partial_rotary_factor` across history, which is how it found the
  open fix — crossing thread boundaries, because the fix was not in the thread.
  `relore inflight` exists because of that call: it was a lucky side effect of a
  symbol search, and an agent's most expensive failure mode deserves a verb;
- found the refactor that caused the regression, with the flag above;
- then moved to code inspection for the repo-wide audit the maintainer asked for.

During that investigation, every query was served from the local Relore
index—no GitHub API calls were made. GitHub is contacted by the ingestion
process to keep the PostgreSQL index fresh, not by every agent doing a search.

relore can make this connection more direct:

```
relore why src/.../modeling_gpt_neox_japanese.py:90
```

`git blame` can tell you which commit last changed a line. `relore why` follows that commit back to the 
pull request and surfaces the review comments around that line, the discussion 
that can explain why the change was made.

It connects the code that exists today back to the discussion that shaped it.

## What that changes

The triage run above made 153 tool calls, 137 of them back into a file it had
already opened, and produced no fix. It also had no way to ask about any of the
history in that list: the agent's entire toolset was `grep`, `read_file`,
`list_dir` and `fetch_url`, so "is somebody already fixing this?" was not a
question it could ask, at any budget.

The field-report run reached the root cause, the culprit PR and the already-open
fix in about ten calls. A [later run](https://github.com/huggingface/relore/issues/58)
on a bug it had never seen found the in-flight PR on its third call and came back
with two findings that neither the issue nor that PR contained, without cloning
the repository at all. Everything relore handed that session came to 25,767
tokens — a median of 306 tokens per call, and less in total than three reads of
the one file the triage job opened 53 times.

Those are different bugs in different harnesses, so this is a change of shape
rather than a controlled benchmark — and we are deliberately not putting a
number opposite the 2.1M, because the total bill of an agent session is set by
its harness and its turn count far more than by any one tool. The shape is the
point: the questions that used to cost a hundred file visits, or that the agent
simply could not ask, now cost a few hundred tokens each.

## Conclusion

We built `relore` because Serge needed a better way to remember Transformers. But
there isn't anything Transformers-specific about the problem.

Mature open-source projects accumulate years of decisions in issues, reviews
and PR comments. Humans learn that lore slowly. Agents start every session
knowing none of it.

`relore` turns that history into something both can query, and connects it back
to the code that exists today.

It's open source, Apache 2.0, and works on any GitHub repository you're willing
to index.

## Try it

```bash
pip install "relore[postgres] @ git+https://github.com/huggingface/relore"

export RELORE_DATABASE_URL=postgresql://localhost/relore
export GITHUB_TOKEN=...   # issues:read + pull_requests:read, never write

relored migrate && relored backfill --repo owner/name && relored authority --repo owner/name
relore search "why is this cast here" --kind rationale --file src/model.py
```

A full history is resumable and takes about a day; `relored sample --repo
owner/name --since YYYY-MM-DD` indexes a window in minutes if you want to try it
on something smaller first. `relored serve` then puts the index behind an HTTP
API, which is how agents reach it, and `relore --help` is the reference.

The code is at [huggingface/relore](https://github.com/huggingface/relore).
Issues and pull requests are welcome.

