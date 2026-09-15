# relore - repository memory for coding agents

On 8 September somebody opened
[an issue](https://github.com/huggingface/transformers/issues/48630) on
Transformers: `GPTNeoXJapanese` crashes for any `rotary_pct != 1.0`, because
RoPE ignores `partial_rotary_factor`. Within a day a contributor had posted a
correct diagnosis, a maintainer had answered "a PR is very much welcome", and
two different people had each opened a fix. 
Two days later Serge, our agent, picked up the same issue and started 
writing a third patch without knowing there were already two. This happened 
because `gh issue view` did not link all those events together and 
it's easy to miss.

`gh` can fetch any individual issue, PR, or comment. The hard part is knowing
which ones to fetch: the fix may live in another PR, the rationale in a review
comment, and the relevant maintainer decision in a thread the current issue
never links to.

`relore` is what we built to improve this, an index of a repository's own
history optimized for agents. Coding agents can search code, but they usually 
cannot search the decisions that produced the code. `relore` gives them 
queryable repository memory with provenance.

There are [proposals](https://arxiv.org/abs/2603.15566) to record decision
context explicitly in git history. We started from the opposite end: projects
should not have to change how they work. The knowledge already exists, trapped
in years of GitHub discussions.


## What relore is

Search alone isn't enough. Repository history contains maintainer decisions,
contributor hypotheses, automated reviews, and bot-generated text, and those
should not carry the same weight. `relore` (REpository LORE) records who said
what, distinguishes authoritative project decisions from contributor claims,
and keeps machine-generated content out of the default evidence set.

It also keeps a working clone of the repository beside
it, both served over one HTTP API.

![Relore sits between a repository's history and its current code, and answers an agent's questions against both.](relore-diag.png)

*History on one side, the code as it is today on the other, and one tool that
can answer across both.*

`relore` exposes `verbs` for querying discussion history, inspecting code, and recovering why a particular line exists.

Check out the full list at https://github.com/huggingface/relore/blob/main/docs/cli.md

For the specific case we presented earlier, a call to `inflight` directly hints
that there are competing patches for that issue, and that one was closed:

```console
$ relore inflight 48630
2 threads claim to close huggingface/transformers#48630

1. huggingface/transformers#48652 pr  open (approved)  5d  closes  @blipbyte
   > [GPTNeoXJapanese] Fix RoPE ignoring partial_rotary_factor
2. huggingface/transformers#48672 pr  closed by its author  5d  closes  @truongsontung
   > fix: respect partial_rotary_factor in GPTNeoXJapaneseRotaryEmbedding
```

That's usually the first verb the agent will call because we've hinted it in `--help`.

Once the agent gets the overview of the situation, it digs into the discussions
with `threads` where there is a clear distinction between contributor, bots and maintainers, 
then does a couple of `search` calls and looks at the code via `copies` and/or `defs`.

An agent in one of our field tests reported that
`relore defs` gave it a better overview of a file than reading the whole thing.

In that run, `defs` gave the agent the overview it needed for roughly 5% of the
tokens it estimated for reading the whole file.

See the [field report](https://github.com/huggingface/relore/issues/8) it filed at the end of the run. 

`grep` and `rg` return matching lines and leave the model to
reconstruct the program structure around them.

Provenance mattered just as much: distinguishing maintainer guidance from
contributor claims changed the task from fixing one model to auditing the
regression across models.

## One run, end to end

Another real-world example: the [field
report](https://github.com/huggingface/relore/issues/8) for the bug this post
opened with is the whole trace. A cold agent, with no documentation beyond
`relore --help`:

- read the issue and separated the maintainer's comment from the contributor's,
  which is what reframed the task;
- learned the bug was one of several a refactor had regressed;
- searched `partial_rotary_factor` across history, which is how it found the open
  fix, crossing thread boundaries because the fix was not in the thread.
  `relore inflight` exists because of that call: finding it was a lucky side
  effect of a symbol search, and that failure mode deserved its own verb;
- found the refactor that caused the regression;
- then moved to code inspection for the repo-wide audit the maintainer asked for.

Every query was served locally by `relore`. GitHub is contacted only by the
ingestion process, to keep the index fresh.

That last step is what `why` makes direct:

```console
relore why src/.../modeling_gpt_neox_japanese.py:90
```
`git blame` tells you which commit last changed a line. `relore why` follows
that commit to its pull request and surfaces the review comments around that
line, which is the discussion that explains why the change was made.

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

The code is at [huggingface/relore](https://github.com/huggingface/relore).
Issues and pull requests are welcome.
