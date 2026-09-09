"""Python, the first language -- and nothing more than a configuration of node types.

The point of this file is how short it is. If adding a language needed more than a grammar
module, a node-type map and possibly a ``qualname`` override, the seam in
:mod:`ghlore.code.api` would not be real.
"""

from __future__ import annotations

from ghlore.code.providers.treesitter import TreeSitterProvider


class PythonProvider(TreeSitterProvider):
    name = "python"
    patterns = ("*.py", "*.pyi")
    grammar_module = "tree_sitter_python"
    definition_nodes = {"class_definition": "class", "function_definition": "function"}
    #: Names in a call position. Deliberately not every identifier: a reference graph that
    #: counts every mention of ``self`` ranks nothing, and section 1's use for refs is
    #: "who calls this".
    reference_nodes = ("call",)

    def kind_for(self, kind: str, parents: tuple[str, ...]) -> str:
        """A function inside anything is a method, which is what a reader expects to see
        next to ``Gemma3Model.forward``."""
        return "method" if kind == "function" and parents else kind

    def _reference_name(self, node, source: bytes) -> str:
        """The callee of a call, as written.

        ``foo()`` gives ``foo``; ``a.b.foo()`` gives ``foo``, because the attribute chain
        in front of it is a value this provider cannot resolve, and a *name* is what
        section 1 needs -- resolving a mention to ``Gemma3Model.forward`` is the
        disambiguation tier, and it belongs with the daemon's lens where a whole tree is
        in scope, not with a pure function of one file.
        """
        function = node.child_by_field_name("function")
        if function is None:
            return ""
        if function.type == "attribute":
            attribute = function.child_by_field_name("attribute")
            function = attribute if attribute is not None else function
        return source[function.start_byte : function.end_byte].decode("utf-8", "replace")
