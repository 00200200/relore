"""References to a name, across a local checkout.

``refs`` is optional throughout (section 9's rule 2): a language whose provider does not
offer it says so and exits 0, rather than returning an empty list a caller would read as
"nothing calls this".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ghlore.code import registry
from ghlore.code.api import REFS, MissingParser
from ghlore.code.registry import provider_for
from ghlore.code.walk import read, source_files


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    name: str


@dataclass(frozen=True)
class RefResult:
    """References found, plus the files nobody could answer for.

    ``unsupported`` is the honest part: a checkout of mixed languages where only some have
    a ``refs`` tier produces a partial answer, and a caller that cannot see which files
    were skipped will read the gap as an absence.
    """

    hits: tuple[Hit, ...]
    searched: int
    unsupported: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unsupported


def references(root: str, symbol: str) -> RefResult:
    registry.require_any()
    hits: list[Hit] = []
    unsupported: set[str] = set()
    searched = 0
    for path in source_files(root):
        try:
            provider = provider_for(path)
        except MissingParser:
            continue
        if REFS not in provider.capabilities:
            unsupported.add(provider.name)
            continue
        source = read(path)
        if source is None:
            continue
        searched += 1
        hits.extend(_matches(provider, path, source, symbol))
    return RefResult(tuple(hits), searched, tuple(sorted(unsupported)))


def _matches(provider, path: str, source: bytes, symbol: str) -> Iterator[Hit]:
    """Compare, never parse.

    Rule 1 makes ``qualname`` opaque above the provider interface, so ``symbol`` is matched
    whole against either the name written at the call site or a qualname the provider
    resolved -- and **not** by splitting it on a dot. Splitting would be core deciding what
    the separator is, which is the one thing rule 1 forbids: it is ``::`` in one language
    and ``#`` in another, and the split would quietly do the wrong thing in both.

    The consequence is a real and declared limit: ``ghlore refs Gemma3Model.forward``
    matches only where a provider resolves callees, and Python's does not (see
    :class:`~ghlore.code.providers.python.PythonProvider`). ``ghlore refs forward`` is the
    query that works today.
    """
    for reference in provider.refs(path, source):
        if symbol in (reference.name, reference.qualname):
            yield Hit(path=path, line=reference.line, name=reference.name)
