# ghlore - searching the decisions behind the code

A few weeks ago, Serge, one of the agents we use to maintain Transformers,
spent 2.1 million input tokens debugging a single problem.

It made 153 tool calls. 137 of them were file reads. About 76% of its input
context was just pieces of the repository it had loaded while trying to
understand the problem.

The agent wasn't doing anything obviously wrong. Transformers is just a very
large codebase.


## browsing the code

grep and rg are really good tools, and they are used a lot by coding 
agent to find relevant snippets in code bases. The only caveat is that 
they produce text and the LLM has to parse it to understand it.

You can always reduce the problem by actively pruning / compressing these interactions 
in the context, but it's still a text base system with extra work for you LLM.

Tools like ctags and tree-sitter solve this by providing a searchable symbols 
index, and you can find numerous projects out there that will do exactly 
that and surface it in a CLI for your agent.

So we started building a symbol index of our own and exposing it through a new tool: ghlore.

In one of our field tests, the agent found that ghlore defs gave it a better
overview of a file than reading the whole thing, using roughly 5% of the
tokens.

Instead of loading the implementation, the agent can first ask for its structure:

```
ghlore defs path/to/model.py
```

and only read the parts it actually needs.

But after optimizing code browsing, we ran into a harder problem: sometimes the
information Serge needs isn't in the code at all.


## understanding decisions 

Another problem we ran into is that a checkout contains the current state of a
project. It doesn't contain the project's memory.

So a PR that seems correct may be rejected by a contributor with a reference
to an old attempt on another closed PR or a link to the right solution explained 
in another buried comment.

Or worse: your agent burns token while a fix is already there wating for a review.

Questions we want answers to:

- Is somebody already fixing this?
- Was this approach tried before?
- Why is this line written this way?
- How have maintainers fixed this class of failure in the past?
- What did maintainers explicitly reject?
- Is an old decision still true in the current code?

That is the project "lore" and this concept has even been explored in a paper 
describing a possible protocol for git commits to structure this https://arxiv.org/abs/2603.15566

The interesting part for us is that we don't need projects to start writing
their history differently. Most of that knowledge already exists: it is just
trapped in years of GitHub discussions.

Github search is what you can use to look for this, but has severe limitations.

- it exposes a best-match ranking, but it is opaque and isn't designed around
the kinds of evidence our agents care about.
- it's rate limited and our swarm of agents can quickly hit the cap
- it can find text inside comments, but the result is the containing thread.
The agent then has to fetch and reread that thread to discover which comment
matched, who wrote it, and when.


## meet ghlore, an hybrid index


ghlore is a searchable memory for a GitHub repository.

It indexes issues, PRs, comments, reviews, commit messages and the
relationships between them. Alongside that history it keeps a working clone of
the repository, so an agent can move between “what did people say?” and “what
does the code look like now?” without loading either world wholesale into its
context.

Not every piece of project history should carry the same weight. A maintainer
explaining why an approach was rejected is different from a contributor
speculating about a bug, or a bot posting generated text. ghlore keeps that
provenance and exposes trust as part of search, so agents can restrict a query
to authoritative sources when it matters.

You can see in https://github.com/huggingface/ghlore/issues/9 a field report of 
an agent using ghlore to investigate a Transformer bug.

A fix was already open, eight hours old, and wasn't linked from the issue. The
agent was about to write another patch.

On that bug, a cold agent using only ghlore --help did the following:

- read the issue and immediately extracted the authoritative maintainer comment;
- learned that the bug was part of a broader regression;
- searched partial_rotary_factor across history;
- discovered an already-open fix PR that wasn't linked from the issue;
- found the refactor that introduced the regression;
- then moved to code inspection for the repo-wide audit.

During that investigation, every query was served from the local ghlore index—
no GitHub API calls were made. GitHub is contacted by the ingestion process to
keep the PostgreSQL index fresh, not by every agent doing a search.

One of the queries we're experimenting with is making this connection even more direct:

```

ghlore why src/.../modeling_gpt_neox_japanese.py:90
```

The idea is to go from a line in the current code to the commits, PRs, reviews
and discussions that explain why it looks the way it does. git blame can tell
you who changed a line and when; ghlore why aims to surface the decision behind
it.


## conclusion

We built ghlore because Serge needed a better way to remember Transformers. But
there isn't anything Transformers-specific about the problem.

Mature open-source projects accumulate years of decisions in issues, reviews
and PR comments. Humans learn that lore slowly. Agents start every session
knowing none of it.

ghlore turns that history into something both can query, and connects it back
to the code that exists today.

It's open source, Apache 2.0, and works on any GitHub repository you're willing
to index.

