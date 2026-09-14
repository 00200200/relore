# relore - searching the decisions behind the code

A few weeks ago, Serge, one of the agents we use to maintain Transformers,
spent 2.1 million input tokens on a single problem and produced no fix.

It made 153 tool calls. 137 of them were re-reads of a file it had already
opened, one of them 53 times. Those numbers are from our own job store, for one
run of the nightly integration-failure triage.

The agent wasn't doing anything obviously wrong. Transformers is just a very
large codebase, and reading it one file at a time is expensive.

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

Not every piece of project history is equally authoritative.
A maintainer explaining why an approach was rejected is different from a contributor
speculating about a bug, or a bot posting generated text. relore keeps that
provenance and exposes trust as part of search, so agents can restrict a query
to authoritative sources when it matters.

You can see in https://github.com/huggingface/relore/issues/8 a field report of 
an agent using relore to investigate a Transformers bug.

A fix was already open, eight hours old, and wasn't linked from the issue. The
agent was about to write another patch.

On that bug, a cold agent using only `relore --help` did the following:

- read the issue and immediately extracted the authoritative maintainer comment;
- learned that the bug was part of a broader regression;
- searched partial_rotary_factor across history;
- discovered an already-open fix PR that wasn't linked from the issue;
- found the refactor that introduced the regression;
- then moved to code inspection for the repo-wide audit.

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

