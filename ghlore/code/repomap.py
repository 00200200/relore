"""The ranked repo map: what a stranger should read first.

Ranked by **how much of the rest of the tree calls it**, which is the cheap version of the
reference-graph ranking a repo map wants. A definition nothing calls may still matter, but
a definition forty other files call is where a reader has to start, and counting call sites
needs no configuration and no tuning to be defensible.

Note what this does *not* do: it never splits a ``qualname``. A provider reports both the
whole qualified name and the bare ``name`` a call site would have written, so the call
counter is keyed on the latter and the former is passed through untouched -- section 9's
rule 1, which exists because the separator is the language.

Providers without a ``refs`` tier still contribute their definitions; they just cannot
contribute edges, so their symbols rank by the tie-break rather than by callers. That is
the per-tier degradation of rule 2, and :attr:`RepoMap.ranked_by` says which happened so a
flat ranking is not mistaken for a flat codebase.

**Same-named methods share a count**, and it shows: three classes each defining ``info``
all report the callers of every ``info`` in the tree. That is the honest ceiling of a
graph keyed on the written name, and lifting it is section 1's symbol-disambiguation tier
-- resolving a call site to ``Gemma3Model.forward`` needs a whole tree in scope, which a
pure function of one file does not have. Read a caller count as "this name is busy", not
"this definition is".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ghlore.code import registry
from ghlore.code.api import REFS, MissingParser
from ghlore.code.registry import provider_for
from ghlore.code.walk import read, source_files


@dataclass(frozen=True)
class Entry:
    qualname: str
    kind: str
    path: str
    line: int
    callers: int


@dataclass(frozen=True)
class RepoMap:
    entries: tuple[Entry, ...]
    files: int
    definitions: int
    #: ``"callers"`` when at least one provider supplied a reference graph, else
    #: ``"declaration order"``.
    ranked_by: str


def repo_map(root: str, *, limit: int = 40) -> RepoMap:
    registry.require_any()
    found: list[tuple[str, str, str, int, str]] = []  # qualname, name, kind, line, path
    calls: Counter[str] = Counter()
    files = 0
    saw_refs = False

    for path in source_files(root):
        try:
            provider = provider_for(path)
        except MissingParser:
            continue
        source = read(path)
        if source is None:
            continue
        files += 1
        for d in provider.defs(path, source):
            found.append((d.qualname, d.name, d.kind, d.start_line, path))
        if REFS in provider.capabilities:
            saw_refs = True
            for reference in provider.refs(path, source):
                calls[reference.name] += 1

    entries = [
        Entry(qualname, kind, path, line, calls.get(name, 0))
        for qualname, name, kind, line, path in found
    ]
    # Callers first, then a stable order, so two runs over an unchanged tree agree.
    entries.sort(key=lambda e: (-e.callers, e.path, e.line))
    return RepoMap(
        entries=tuple(entries[:limit]),
        files=files,
        definitions=len(found),
        ranked_by="callers" if saw_refs else "declaration order",
    )
