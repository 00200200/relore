# relore - searching the decisions behind the code

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

In another case, our agent followed threads in a couple of PR and
ended up building a patch based on a contributor claim that was completely
false. 

The worst case we've seen is when Copilot or some other bots add comments
in PRs that are completely misleading.

The `gh` client can be used to tap into that memory but suffers from 
some limitation where the important data is missed or hard to recollect. 

`relore` is what we built to improve this, an index of a repository's own
history optimized for agents. A better way to remember Transformers.

## What relore is

`relore` (REpository LORE) indexes a repository's complete issue and
pull-request history — issues, PRs, comments, reviews, commit messages and the
relationships between them — and keeps a working clone of the repository beside
it, both served over one HTTP API.

![Relore sits between a repository's history and its current code, and answers an agent's questions against both.](relore-diag.png)

*History on one side, the code as it is today on the other, and one tool that
can answer across both.*

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

`relore` comes with a lot of `verbs` that can be used to search discussions threads, 
code symbols, or why a given line is written like this.

A typical session looks like this:

```
relore inflight 47720 
relore why src/model.py:90 
relore search "AttributeError: 'NoneType' object has no attribute 'shape'" --kind failure
relore search "why is this cast here" --kind rationale --file src/model.py
relore search --symbol GemmaRotaryEmbedding  
relore copies compute_default_rope_parameters
```

`copies` returns all occurences of a symbole and there's also `defs` that 
returns a module symbols:

```
$ relore defs relore/ingest/authority.py
43-56        class     AuthorityResult
59-117       function  resolve_authority
120-156      function  _resolve_one
```

An agent in one of our field tests reported that
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
