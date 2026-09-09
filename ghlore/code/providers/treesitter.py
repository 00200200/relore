"""The tree-sitter engine, shared by every first-class provider.

tree-sitter is the good path, and it is chosen over Python's stdlib ``ast`` -- which is
free and exact -- for two reasons that both matter more than exactness:

* it parses **syntactically broken files**, the normal state of a tree an agent is halfway
  through editing;
* one interface then serves every language with a grammar, so adding a language is a
  package rather than a second parser.

A subclass supplies the grammar module and the node types; nothing here knows any syntax.
The grammar is loaded lazily and a missing one raises at construction, which
``registry.py`` turns into a warning and a fallback (rule 3).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator
from typing import Any

from ghlore.code.api import DEFS, EXTENTS, REFS, Definition, Reference


class TreeSitterProvider:
    """Base class. Subclasses set the four class attributes below."""

    name: str = "tree-sitter"
    patterns: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset({DEFS, EXTENTS, REFS})

    #: The pip distribution that carries the grammar, e.g. ``tree_sitter_python``.
    grammar_module: str = ""
    #: Node types that introduce a definition, mapped to the ``kind`` reported for them.
    definition_nodes: dict[str, str] = {}
    #: Node types that count as a reference to a name.
    reference_nodes: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._parser = _parser_for(self.grammar_module)

    # -- the interface ---------------------------------------------------

    def defs(self, path: str, source: bytes) -> Iterable[Definition]:
        """Definitions in one file, in source order.

        A file with a syntax error still yields the definitions before *and* after the
        break: tree-sitter produces a tree with ERROR nodes rather than refusing, and
        walking past them is the whole reason this is not the stdlib parser.
        """
        root = self._parser.parse(source).root_node
        return list(self._walk(root, source, parents=()))

    def refs(self, path: str, source: bytes) -> Iterable[Reference]:
        if REFS not in self.capabilities:
            return []
        root = self._parser.parse(source).root_node
        return list(self._references(root, source))

    # -- walking ---------------------------------------------------------

    def _walk(self, node: Any, source: bytes, *, parents: tuple[str, ...]) -> Iterator[Definition]:
        for child in node.children:
            kind = self.definition_nodes.get(child.type)
            if kind is None:
                yield from self._walk(child, source, parents=parents)
                continue
            name = self._name_of(child, source)
            if not name:
                yield from self._walk(child, source, parents=parents)
                continue
            qualname = self.qualname(parents, name)
            yield Definition(
                qualname=qualname,
                name=name,
                kind=self.kind_for(kind, parents),
                start_line=child.start_point[0] + 1,
                # Only when declared: an end line a caller cannot trust is worse than
                # none, because `enclosing_symbol` would present a guess as exact.
                end_line=child.end_point[0] + 1 if EXTENTS in self.capabilities else None,
                parent=self.qualname(parents[:-1], parents[-1]) if parents else None,
            )
            yield from self._walk(child, source, parents=(*parents, name))

    def _references(self, node: Any, source: bytes) -> Iterator[Reference]:
        for child in node.children:
            if child.type in self.reference_nodes:
                name = self._reference_name(child, source)
                if name:
                    yield Reference(name=name, line=child.start_point[0] + 1)
            yield from self._references(child, source)

    def _name_of(self, node: Any, source: bytes) -> str:
        field = node.child_by_field_name("name")
        return _text(field, source) if field is not None else ""

    def _reference_name(self, node: Any, source: bytes) -> str:
        return _text(node, source)

    # -- the parts a language owns ---------------------------------------

    def qualname(self, parents: tuple[str, ...], name: str) -> str:
        """Join a name to its enclosing scopes. **Rule 1: the provider owns this.**

        The default is a dot, which is right for a great many languages and wrong for
        several; a provider whose language spells it ``::`` or ``#`` overrides this and core
        is none the wiser.
        """
        return ".".join((*parents, name))

    def kind_for(self, kind: str, parents: tuple[str, ...]) -> str:
        return kind


def _parser_for(module_name: str) -> Any:
    """Load a grammar and build a parser, or raise so the registry can degrade.

    Both imports are deferred: ``tree-sitter`` is the ``[code]`` extra and each grammar is
    its own per-language extra, so neither may be imported at module scope (section 2).
    """
    if not module_name:
        raise RuntimeError("a TreeSitterProvider subclass must set grammar_module")
    import tree_sitter

    grammar = importlib.import_module(module_name)
    return tree_sitter.Parser(tree_sitter.Language(grammar.language()))


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")
