`ci.yml` runs exactly what `make check` runs locally — ruff (format + lint) and pytest —
on the supported-Python floor and the newest release. There is deliberately no separate
type-check job: linting is ruff, full stop (see `AGENTS.md`).
