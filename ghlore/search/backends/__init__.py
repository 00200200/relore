"""One backend per dialect, chosen by a lookup rather than by a branch.

Section 4.1 puts retrieval behind a ``SearchBackend`` per dialect *because* the two
engines are different. The choice of which one is a registry keyed on the answer
``store/dialect.py`` gives -- that module is the only one allowed to know which database
this is (``tests/unit/test_no_dialect_leak.py``), and a dict lookup is not a second seam.
"""

from __future__ import annotations

from sqlalchemy import Engine

from ghlore.search.backends.base import SearchBackend
from ghlore.search.backends.postgres import PostgresBackend
from ghlore.search.backends.sqlite import SqliteBackend
from ghlore.store.dialect import dialect_name

BACKENDS: dict[str, type[SearchBackend]] = {
    "postgresql": PostgresBackend,
    "sqlite": SqliteBackend,
}


def open_backend(engine: Engine) -> SearchBackend:
    name = dialect_name(engine)
    try:
        return BACKENDS[name](engine)
    except KeyError:
        raise NotImplementedError(
            f"no search backend for {name!r}; ghlore supports Postgres in production and "
            "SQLite for tests and laptops (the build plan section 4.1)"
        ) from None


__all__ = ["BACKENDS", "PostgresBackend", "SearchBackend", "SqliteBackend", "open_backend"]
