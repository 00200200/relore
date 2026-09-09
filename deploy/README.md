# Deploying ghlore

The chart runs four things: a Postgres 16 StatefulSet with pgvector, `ghlored serve`
(the API and the section 8 web UI), one `ghlored poll` Deployment per indexed
repository, and a nightly `pg_dump` CronJob. `ghlored migrate` runs as a Helm hook
before any of them, on every install and upgrade.

**The production values are not in this repository.** `deploy/helm/values.yaml` is
the chart's *shape* and safe defaults; the environment lives in the operator's own
repository and is passed with `-f`. `deploy.sh` refuses to run without it, because
a values file that silently falls back to chart defaults produces a healthy pod
with settings quietly unset.

```bash
deploy/scripts/deploy.sh --plan -f ../env/prod.yaml          # what it manages
deploy/scripts/deploy.sh --dry-run -f ../env/prod.yaml       # full manifest
deploy/scripts/deploy.sh --context <ctx> -n ghlore -f ../env/prod.yaml
deploy/scripts/logs.sh -c poll --repo huggingface/serge --since 2h
```

## What has to exist first

| Thing | Why |
| --- | --- |
| A Secret named by `existingSecret` | `GITHUB_TOKEN` (`issues:read` + `pull_requests:read`, **never** write — section 11), `GHLORE_API_TOKENS`, `POSTGRES_PASSWORD`. Produce it with the environment's secret operator via the `externalSecret` block, or by hand. |
| `poll.repos` | The repositories to index — and section 11's allowlist, which is re-checked per thread. An allowlist, never a denylist. |
| `botAccounts` | Section 6.2's bot-account list. Leaving it empty in a deployment that indexes its own agent's comments reintroduces what section 11 refused. |

## After the first deploy

1. **Backfill, once per repository.** The poll loop keeps an index current; it does
   not create one. `ghlored backfill --repo <repo>` is a restartable day for a
   large repository, `ghlored sample --repo <repo> --since <date>` a bounded window.
2. **Resolve authority.** `ghlored authority --repo <repo>`. Without it every
   org-team maintainer reads as an unresolved `MEMBER` and the `authoritative`
   tier is empty — measured on `huggingface/serge`: 0 of 415 documents before, 288
   after. Rationale queries answer from that tier, so until this runs they return
   nothing.
3. **Check `ghlored status`.** It prints the backend and its capabilities, the
   per-pass high-water marks, the sample window if the corpus is one, and the
   count of superseded documents kept (section 14.1).

## Changing the bot list or the allowlist

Both live in the ConfigMap. `trust` and `author_is_bot` are `DERIVED_COLUMNS`
(section 5.1), so an edit does not move existing documents by itself — re-derive
after deploying:

```bash
kubectl -n ghlore exec deploy/ghlore -- ghlored derive --repo <repo>
```

That writes only the documents whose tier actually changed.
