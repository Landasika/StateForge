"""Resolve *which object* an expression denotes, from the AST alone.

The state extractor historically matched the literal variable name
``session``. That is one spelling of one convention: Django writes
``request.session[...]``, Flask writes ``session[...]``, and plenty of code
assigns the session to a local first. Matching on the spelling misses all
but one of them.

This module asks the structural question instead. Given a subscript's base
expression it walks back to the object it denotes, following

- attribute access      ``request.session``  -> ``request.session``
- local aliases          ``s = request.session`` then ``s[k]``
- chained aliases        ``b = a`` where ``a = request.session``
- call results           ``get_session()``
- module constants       ``request.session[SESSION_KEY]`` -> ``_auth_user_id``

Nothing here knows about Django, Flask or any application. It knows Python's
expression grammar plus the shared naming convention that a session-like
store is called something ending in ``session``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Words that name a session-like key/value store. Shared vocabulary, not an
# application list: the point is that it is framework-neutral and that the
# decision is made from the resolved object, not from a bare variable name.
SESSION_NAMES = frozenset({
    "session", "sessions", "session_store", "session_data", "session_key",
    "sessiondict", "session_dict", "sessionmap", "session_map",
})


def describe(node: ast.AST) -> str:
    """Render an expression chain as dotted text, e.g. ``request.session``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = describe(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        return describe(node.value)
    if isinstance(node, ast.Call):
        return f"{describe(node.func)}()"
    return ""


@dataclass
class Aliases:
    """Local names bound to expression text within one function scope."""

    table: dict[str, str] = field(default_factory=dict)

    @classmethod
    def collect(cls, fn: ast.AST, rounds: int = 3) -> "Aliases":
        """Collect ``name -> expression`` bindings, resolving chains.

        ``rounds`` bounds the propagation so that ``b = a`` still resolves
        once ``a`` is known, without looping on re-assignment.
        """
        table: dict[str, str] = {}
        for _ in range(max(1, rounds)):
            changed = False
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign):
                    targets, value = [node.target], node.value
                else:
                    continue
                if value is None or not isinstance(
                        value, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
                    continue
                text = describe(value)
                if not text:
                    continue
                head, _, rest = text.partition(".")
                if head in table:
                    base = table[head]
                    text = f"{base}.{rest}" if rest else base
                for target in targets:
                    if isinstance(target, ast.Name) and table.get(target.id) != text:
                        table[target.id] = text
                        changed = True
            if not changed:
                break
        return cls(table)

    def resolve(self, node: ast.AST) -> str:
        """Canonical dotted text for the object an expression denotes."""
        text = describe(node)
        if not text:
            return ""
        head, _, rest = text.partition(".")
        if head in self.table:
            base = self.table[head]
            return f"{base}.{rest}" if rest else base
        return text


def is_session_like(obj_text: str) -> bool:
    """True when a resolved object name is a session-like store.

    Matching is on the leaf word: ``session``, ``sessions`` and compounds
    such as ``app_session`` / ``user_sessions`` all denote the same kind of
    store. Anything else (``cart``, ``ctx.data``) is left alone.
    """
    if not obj_text:
        return False
    leaf = obj_text.rsplit(".", 1)[-1].lower()
    if leaf in SESSION_NAMES:
        return True
    # Compound names: strip a leading qualifier and re-test the tail.
    for sep in ("_", "-"):
        head, _, tail = leaf.rpartition(sep)
        if head and tail in SESSION_NAMES:
            return True
    return False


def module_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, expanded one hop.

    Enough for the common ``SESSION_KEY = "_auth_user_id"`` idiom without
    pretending to be an evaluator.
    """
    out: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) \
                and isinstance(value.value, str):
            out[target.id] = value.value
    return out


def session_key(node: ast.AST, constants: dict[str, str] | None = None) -> str | None:
    """Key of ``<obj>.session["k"]`` / ``session["k"]``, else None.

    The key may be a symbol: ``request.session[SESSION_KEY]`` is the usual
    Django spelling. ``constants`` expands it when known; otherwise the
    symbolic name is returned in angle brackets so the write is still
    recorded rather than silently dropped.
    """
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, (ast.Name, ast.Attribute)):
        return None
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant):
        return str(slice_node.value)
    if isinstance(slice_node, ast.Name):
        if constants and slice_node.id in constants:
            return constants[slice_node.id]
        return f"<{slice_node.id}>"
    return None


def object_of_subscript(node: ast.Subscript,
                        aliases: Aliases | None = None) -> str:
    """Canonical object text for the base of a subscript node."""
    text = describe(node.value)
    if aliases is not None and text:
        head, _, rest = text.partition(".")
        if head in aliases.table:
            base = aliases.table[head]
            text = f"{base}.{rest}" if rest else base
    return text


def is_session_subscript(node: ast.AST, aliases: Aliases | None = None) -> bool:
    """True when ``node`` is a subscript on a session-like object."""
    if not isinstance(node, ast.Subscript):
        return False
    return is_session_like(object_of_subscript(node, aliases))


def object_of_call_receiver(node: ast.Call,
                            aliases: Aliases | None = None) -> str:
    """Canonical object text for the receiver of ``obj.method(...)``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return ""
    text = describe(node.func.value)
    if aliases is not None and text:
        head, _, rest = text.partition(".")
        if head in aliases.table:
            base = aliases.table[head]
            text = f"{base}.{rest}" if rest else base
    return text
