"""SS-02: Session access index + target frontier query (STATIC_MAY only).

The index is keyed by the Session access events from
``state/session_access.py``.  It answers one question at a time: *given this
reader/condition, which write sites may have produced the value?*  It never
materialises a writer×reader Cartesian product and never mutates ProgramIR.

The target query walks the target function's projected CFG (the existing
``goal_derivation`` intra-function projection, including callsite→returnsite
edges) and keeps one constraint group per simple path.  Conditions that
cannot be resolved to a Session read are reported as unresolved instead of
being guessed.  Every edge/effect is ``STATIC_MAY``; runtime verification is
a later layer.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from stateforge.core.ir import ProgramIR
from stateforge.state.session_access import (
    BACKEND_FLASK_SESSION,
    _alias_session_names,
    _import_bindings,
    _session_names,
    EFFECT_STATIC_MAY,
    KEY_ANY,
    KEY_CONST,
    KEY_SYMBOLIC,
    KEY_UNKNOWN,
    OP_CLEAR,
    OP_DELETE,
    OP_READ,
    OP_READ_MODIFY_WRITE,
    OP_SET,
    READ_MEMBERSHIP,
    READ_GET,
    READ_SUBSCRIPT,
    SessionAccess,
)

MUST_OVERLAP = "MUST_OVERLAP"
MAY_OVERLAP = "MAY_OVERLAP"
DISJOINT = "DISJOINT"

COND_EQ = "EQ"
COND_NE = "NE"
COND_EXISTS = "EXISTS"
COND_NOT_EXISTS = "NOT_EXISTS"
COND_TRUTHY = "TRUTHY"
COND_FALSY = "FALSY"
COND_IN = "IN"
COND_NOT_IN = "NOT_IN"

_INVERT = {
    COND_EQ: COND_NE, COND_NE: COND_EQ,
    COND_EXISTS: COND_NOT_EXISTS, COND_NOT_EXISTS: COND_EXISTS,
    COND_TRUTHY: COND_FALSY, COND_FALSY: COND_TRUTHY,
    COND_IN: COND_NOT_IN, COND_NOT_IN: COND_IN,
}

_MAX_PATHS = 64
_MAX_DEPTH = 64


@dataclass(frozen=True)
class SessionCondition:
    """One path condition that constrains a target through a Session value."""

    key_kind: str
    key_expr: str | None
    op: str
    value: object = None
    value_kind: str = "NONE"          # NONE / CONST / SYMBOLIC
    direction: str = "true"           # true/false edge that carries this condition
    guard_block: str | None = None
    source_file: str = ""
    source_line: int = 0
    source_col: int = 0
    reader_site_id: str | None = None
    resolvable: bool = True
    note: str = ""

    @property
    def canonical(self) -> str:
        key = (f"session.{self.key_expr}" if self.key_kind == KEY_CONST
               else "session.*" if self.key_kind == KEY_ANY
               else f"session.?{self.key_expr or 'unknown'}")
        return f"{key} {self.op} {self.value!r}"

    def to_dict(self) -> dict:
        return {
            "key_kind": self.key_kind,
            "key_expr": self.key_expr,
            "op": self.op,
            "value": self.value,
            "value_kind": self.value_kind,
            "direction": self.direction,
            "guard_block": self.guard_block,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "source_col": self.source_col,
            "reader_site_id": self.reader_site_id,
            "resolvable": self.resolvable,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionCondition":
        return cls(
            key_kind=d["key_kind"], key_expr=d.get("key_expr"),
            op=d["op"], value=d.get("value"),
            value_kind=d.get("value_kind", "NONE"),
            direction=d.get("direction", "true"),
            guard_block=d.get("guard_block"),
            source_file=d.get("source_file", ""),
            source_line=int(d.get("source_line", 0)),
            source_col=int(d.get("source_col", 0)),
            reader_site_id=d.get("reader_site_id"),
            resolvable=bool(d.get("resolvable", True)),
            note=d.get("note", ""))


@dataclass(frozen=True)
class UnresolvedItem:
    what: str
    cause: str
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict:
        return {"what": self.what, "cause": self.cause,
                "source_file": self.source_file,
                "source_line": self.source_line}


@dataclass(frozen=True)
class SessionDependency:
    """A candidate writer for one reader/condition (static may, not proof)."""

    writer_site_id: str
    reader_site_id: str
    writer_block: str | None
    reader_block: str | None
    key_overlap: str
    client_binding: str = "writer.?client == reader.?client"
    candidate_effect: dict = field(default_factory=dict)
    evidence: str = EFFECT_STATIC_MAY
    priority: int = 4

    def to_dict(self) -> dict:
        return {
            "writer_site_id": self.writer_site_id,
            "reader_site_id": self.reader_site_id,
            "writer_block": self.writer_block,
            "reader_block": self.reader_block,
            "key_overlap": self.key_overlap,
            "client_binding": self.client_binding,
            "candidate_effect": self.candidate_effect,
            "evidence": self.evidence,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class SessionFrontier:
    target_block: str
    function: str
    file: str
    path_constraint_groups: tuple[tuple[SessionCondition, ...], ...] = ()
    relevant_reads: tuple[SessionAccess, ...] = ()
    candidate_writers: tuple[SessionDependency, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()
    guard_blocks: tuple[str, ...] = ()
    partial: bool = False
    evidence: str = EFFECT_STATIC_MAY

    @property
    def candidate_blocks(self) -> tuple[str, ...]:
        return tuple(sorted({d.writer_block for d in self.candidate_writers
                             if d.writer_block}))

    def to_dict(self) -> dict:
        return {
            "target_block": self.target_block,
            "function": self.function,
            "file": self.file,
            "path_constraint_groups": [
                [c.to_dict() for c in group]
                for group in self.path_constraint_groups],
            "relevant_reads": [a.to_dict() for a in self.relevant_reads],
            "candidate_writers": [d.to_dict() for d in self.candidate_writers],
            "candidate_blocks": list(self.candidate_blocks),
            "unresolved": [u.to_dict() for u in self.unresolved],
            "guard_blocks": list(self.guard_blocks),
            "partial": self.partial,
            "evidence": self.evidence,
        }


# ------------------------------------------------------------------- index


class SessionAccessIndex:
    """Key → writers/readers/deleters; per-reader queries avoid N×M edges."""

    def __init__(self, accesses: Iterable[SessionAccess] = ()):
        self.accesses: list[SessionAccess] = []
        self.by_site: dict[str, SessionAccess] = {}
        self.by_key: dict[str, list[SessionAccess]] = {}
        self.by_block: dict[str, list[SessionAccess]] = {}
        for access in accesses:
            self.add(access)

    @staticmethod
    def bucket(key_kind: str, key_expr: str | None) -> str:
        if key_kind == KEY_CONST:
            return f"CONST:{key_expr}"
        if key_kind == KEY_ANY:
            return "ANY"
        if key_kind == KEY_SYMBOLIC:
            return f"SYMBOLIC:{key_expr or '?'}"
        return "UNKNOWN"

    def add(self, access: SessionAccess) -> None:
        self.accesses.append(access)
        self.by_site[access.site_id] = access
        self.by_key.setdefault(
            self.bucket(access.key_kind, access.key_expr), []).append(access)
        if access.block_id:
            self.by_block.setdefault(access.block_id, []).append(access)

    # ---- basic views
    def readers(self, key: str | None = None) -> list[SessionAccess]:
        return [a for a in self.accesses
                if a.operation in (OP_READ, OP_READ_MODIFY_WRITE)
                and (key is None or a.key_expr == key
                     or a.key_kind in (KEY_ANY, KEY_SYMBOLIC, KEY_UNKNOWN))]

    def writers_for_key(self, key_kind: str, key_expr: str | None) -> list[SessionAccess]:
        """Writers/deleters that may affect this key (clear included)."""
        out: list[SessionAccess] = []
        for a in self.accesses:
            if a.operation not in (OP_SET, OP_DELETE, OP_CLEAR,
                                   OP_READ_MODIFY_WRITE):
                continue
            overlap = _overlap((key_kind, key_expr),
                               (a.key_kind, a.key_expr))
            if overlap != DISJOINT:
                out.append(a)
        return out

    def writers_of(self, reader: str | SessionAccess,
                   key_kind: str | None = None,
                   key_expr: str | None = None) -> list[SessionDependency]:
        """Query candidate writers for one reader site (not a global join)."""
        if isinstance(reader, str):
            access = self.by_site.get(reader)
        else:
            access = reader
        if access is None:
            return []
        if key_kind is None:
            key_kind = access.key_kind
        if key_expr is None:
            key_expr = access.key_expr
        out: list[SessionDependency] = []
        for writer in self.writers_for_key(key_kind, key_expr):
            overlap = _overlap((key_kind, key_expr),
                               (writer.key_kind, writer.key_expr))
            if overlap == DISJOINT:
                continue
            out.append(SessionDependency(
                writer_site_id=writer.site_id,
                reader_site_id=access.site_id,
                writer_block=writer.block_id,
                reader_block=access.block_id,
                key_overlap=overlap,
                candidate_effect={
                    "operation": writer.operation,
                    "key_kind": writer.key_kind,
                    "key_expr": writer.key_expr,
                    "value": writer.value_expr,
                },
                priority=_writer_priority(key_kind, key_expr, writer),
            ))
        return sorted(out, key=lambda d: (d.priority, d.writer_site_id))

    def to_dict(self) -> dict:
        return {
            "access_count": len(self.accesses),
            "buckets": {k: len(v) for k, v in sorted(self.by_key.items())},
            "accesses": [a.to_dict() for a in self.accesses],
        }


def _overlap(left: tuple[str, str | None],
             right: tuple[str, str | None]) -> str:
    lk, lv = left
    rk, rv = right
    if lk == KEY_ANY or rk == KEY_ANY or lk == KEY_UNKNOWN or rk == KEY_UNKNOWN:
        return MAY_OVERLAP
    if lk == KEY_SYMBOLIC or rk == KEY_SYMBOLIC:
        return MAY_OVERLAP
    if lk == KEY_CONST and rk == KEY_CONST:
        return MUST_OVERLAP if lv == rv else DISJOINT
    return MAY_OVERLAP


def _writer_priority(key_kind: str, key_expr: str | None,
                     writer: SessionAccess, condition_op: str | None = None) -> int:
    """Ordering only (never drops a candidate).

    Direction matters: DELETE/CLEAR cannot satisfy ``uid == 7`` but can
    satisfy ``uid does not exist``.  Values are ordered, not filtered.
    """
    is_delete = writer.operation in (OP_DELETE, OP_CLEAR)
    if condition_op in (COND_NOT_EXISTS, COND_FALSY):
        if is_delete:
            return 0
        return 3
    if is_delete:
        return 3
    if writer.key_kind == KEY_CONST and writer.key_expr == key_expr:
        if isinstance(writer.value_expr, str) and writer.value_expr.startswith("?"):
            return 1
        return 0
    if writer.key_kind == KEY_ANY:
        return 2
    return 2 if writer.key_kind != KEY_CONST else 3


def build_session_index(accesses: Iterable[SessionAccess]) -> SessionAccessIndex:
    return SessionAccessIndex(accesses)


# --------------------------------------------------- condition / path query


def _function_ast(ir: ProgramIR, block_id: str):
    blk = ir.blocks.get(block_id)
    if blk is None:
        return None, None, None
    try:
        tree = ast.parse(Path(blk.file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return blk, None, None
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == blk.function), None)
    return blk, tree, fn


def _session_source(node: ast.AST, aliases: dict[str, tuple[str, str | None, str | None]],
                    names: set[str], flask_names: set[str]):
    """AST expression → (key_kind, key_expr, reader_site_hint) or None.

    Supports ``session.get('k')``, ``session['k']`` and a local alias that
    was assigned from one of those forms.  Dynamic keys stay SYMBOLIC.
    """
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    if isinstance(node, ast.Subscript):
        if not _looks_like_session(node.value, names, flask_names):
            return None
        return _key_from_ast(node.slice) + (None,)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ("get", "pop") \
            and _looks_like_session(node.func.value, names, flask_names):
        arg = node.args[0] if node.args else None
        return _key_from_ast(arg) + (None,)
    return None


def _looks_like_session(node: ast.AST, names: set[str],
                        flask_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id in flask_names and node.attr == "session"
    return False


def _key_from_ast(node: ast.AST | None) -> tuple[str, str | None]:
    if isinstance(node, ast.Constant):
        return KEY_CONST, str(node.value)
    if node is None:
        return KEY_UNKNOWN, None
    try:
        return KEY_SYMBOLIC, f"?{ast.unparse(node)}"
    except Exception:                              # noqa: BLE001
        return KEY_UNKNOWN, None


_SESSION_ALIAS_NAMES: set[str] = {"session"}
_FLASK_MODULE_NAMES: set[str] = {"flask"}


def _collect_aliases(fn: ast.AST, names: set[str],
                     flask_names: set[str]) -> dict[str, tuple[str, str | None, str | None]]:
    """Local alias map; ambiguous branch assignments become KEY_UNKNOWN.

    A variable assigned from two different Session keys on different paths
    must not silently keep the first key.  ``writers_for_key`` treats
    KEY_UNKNOWN as MAY overlap, and the guard is reported unresolved rather
    than guessed.
    """
    sources: dict[str, list[tuple[str, str | None, str | None]]] = {}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            target = node.targets[0].id
            src = _session_source(node.value, _flatten_unambiguous(sources),
                                  names, flask_names)
            if src is None:
                continue
            bucket = sources.setdefault(target, [])
            if (src[0], src[1]) not in {(x[0], x[1]) for x in bucket}:
                bucket.append(src)
                changed = True
    out: dict[str, tuple[str, str | None, str | None]] = {}
    for target, srcs in sources.items():
        unique = {(kk, ke) for kk, ke, _ in srcs}
        if len(unique) == 1:
            out[target] = srcs[0]
        else:
            out[target] = (KEY_UNKNOWN, f"?{target}", None)
    return out


def _flatten_unambiguous(sources):
    out = {}
    for target, srcs in sources.items():
        unique = {(kk, ke) for kk, ke, _ in srcs}
        if len(unique) == 1:
            out[target] = srcs[0]
    return out


def _guard_conditions(fn: ast.AST, guard_line: int, edge_kind: str,
                      aliases, names: set[str], flask_names: set[str],
                      src_file: str, guard_block: str,
                      accesses: list[SessionAccess]) -> tuple[list[SessionCondition],
                                                              list[UnresolvedItem]]:
    node = next((n for n in ast.walk(fn)
                 if isinstance(n, (ast.If, ast.While)) and n.lineno == guard_line),
                None)
    if node is None:
        return [], [UnresolvedItem(
            what=f"guard-line:{guard_line}", cause="guard-ast-not-found",
            source_file=src_file, source_line=guard_line)]

    test = node.test
    effective_edge = edge_kind
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
        effective_edge = "false" if edge_kind == "true" else "true"

    if isinstance(test, ast.BoolOp):
        return [], [UnresolvedItem(
            what=f"guard:{ast.unparse(test)}", cause="boolop-guard",
            source_file=src_file, source_line=guard_line)]

    cond = _condition_from_test(test, effective_edge, aliases, names,
                                flask_names, src_file,
                                guard_line, guard_block, node, accesses)
    if cond is None:
        return [], [UnresolvedItem(
            what=f"guard:{ast.unparse(test)}", cause="not-session-resolvable",
            source_file=src_file, source_line=guard_line)]
    if isinstance(cond, UnresolvedItem):
        return [], [cond]
    return [cond], []


def _condition_from_test(test, edge_kind, aliases, names, flask_names,
                         src_file, guard_line, guard_block, guard_node, accesses):
    # membership: 'k' in session / 'k' not in session
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if isinstance(op, (ast.In, ast.NotIn)) and _looks_like_session(right, names, flask_names):
            kk, ke = _key_from_ast(left)
            op_name = COND_EXISTS if isinstance(op, ast.In) else COND_NOT_EXISTS
            if edge_kind == "false":
                op_name = _INVERT[op_name]
            return _mk_condition(kk, ke, op_name, None, "NONE", "CONST", edge_kind,
                                 guard_block, src_file, guard_line,
                                 getattr(left, "col_offset", 0),
                                 _reader_site(guard_line, accesses), test)

        src = _session_source(left, aliases, names, flask_names) or _session_source(right, aliases, names, flask_names)
        other = right if _session_source(left, aliases, names, flask_names) else left
        if src is not None:
            kk, ke, site_hint = src
            # None checks
            if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(other, ast.Constant) \
                    and other.value is None:
                is_none = isinstance(op, ast.Is)
                op_name = COND_NOT_EXISTS if is_none else COND_EXISTS
                if edge_kind == "false":
                    op_name = _INVERT[op_name]
                return _mk_condition(kk, ke, op_name, None, "NONE", "CONST",
                                     edge_kind, guard_block, src_file,
                                     guard_line, getattr(test, "col_offset", 0),
                                     _reader_site(guard_line, accesses), test)
            # comparison to a constant / symbolic value
            base = None
            if isinstance(op, ast.Eq):
                base = COND_EQ
            elif isinstance(op, ast.NotEq):
                base = COND_NE
            elif isinstance(op, ast.In):
                base = COND_IN
            elif isinstance(op, ast.NotIn):
                base = COND_NOT_IN
            if base is not None:
                value, vkind = _condition_value(other)
                if edge_kind == "false":
                    base = _INVERT[base]
                return _mk_condition(kk, ke, base, value, vkind, "CONST",
                                     edge_kind, guard_block, src_file,
                                     guard_line, getattr(test, "col_offset", 0),
                                     _reader_site(guard_line, accesses), test)
            return UnresolvedItem(
                what=f"guard:{ast.unparse(test)}", cause="unsupported-compare",
                source_file=src_file, source_line=guard_line)

    # truthy / direct session expression
    src = _session_source(test, aliases, names, flask_names)
    if src is not None:
        kk, ke, _hint = src
        op_name = COND_TRUTHY if edge_kind == "true" else COND_FALSY
        return _mk_condition(kk, ke, op_name, None, "NONE", "NONE",
                             edge_kind, guard_block, src_file, guard_line,
                             getattr(test, "col_offset", 0),
                             _reader_site(guard_line, accesses), test)

    return None


def _condition_value(node) -> tuple[object, str]:
    if isinstance(node, ast.Constant):
        return node.value, "CONST"
    try:
        return f"?{ast.unparse(node)}", "SYMBOLIC"
    except Exception:                              # noqa: BLE001
        return None, "NONE"


def _mk_condition(key_kind, key_expr, op, value, value_kind, _kind,
                  direction, guard_block, src_file, line, col,
                  reader_site_id, raw) -> SessionCondition:
    return SessionCondition(
        key_kind=key_kind, key_expr=key_expr, op=op, value=value,
        value_kind=value_kind, direction=direction, guard_block=guard_block,
        source_file=src_file, source_line=line, source_col=col,
        reader_site_id=reader_site_id, resolvable=True,
        note=ast.unparse(raw) if hasattr(ast, "unparse") else "")


def _reader_site(line: int, accesses: list[SessionAccess]) -> str | None:
    for a in accesses:
        if a.source_line == line and a.operation == OP_READ:
            return a.site_id
    return None


def _function_blocks(ir: ProgramIR, target_block: str) -> set[str]:
    blk = ir.blocks.get(target_block)
    if blk is None:
        return set()
    return {b.id for b in ir.blocks.values()
            if b.file == blk.file and b.function == blk.function}


def _enumerate_paths(ir: ProgramIR, target_block: str) -> tuple[list[list[str]], bool]:
    """Simple paths entry→target in the existing intra-function projection."""
    from stateforge.state.goal_derivation import _entry_of, _intra_adjacency

    fblocks = _function_blocks(ir, target_block)
    entry = _entry_of(ir, fblocks)
    if entry is None:
        return [], False
    adj = _intra_adjacency(ir, fblocks, None)
    paths: list[list[str]] = []
    truncated = False

    def dfs(node: str, path: list[str], seen: set[str]):
        nonlocal truncated
        if len(paths) >= _MAX_PATHS or len(path) >= _MAX_DEPTH:
            truncated = True
            return
        if node == target_block:
            paths.append(list(path))
            return
        for nxt in adj.get(node, ()):
            if nxt in seen:
                continue
            seen.add(nxt)
            path.append(nxt)
            dfs(nxt, path, seen)
            path.pop()
            seen.remove(nxt)

    dfs(entry, [entry], {entry})
    return paths, truncated


def _path_groups(ir: ProgramIR, blk, tree, fn, accesses, target_block):
    from stateforge.state.goal_derivation import _entry_of, _intra_adjacency

    module_imports = _import_bindings(tree)
    names, flask_names = _session_names(fn, module_imports)
    names |= set(_alias_session_names(fn, names, flask_names))
    aliases = _collect_aliases(fn, names, flask_names)
    fblocks = _function_blocks(ir, target_block)
    entry = _entry_of(ir, fblocks)
    paths, truncated = _enumerate_paths(ir, target_block)
    if not paths and entry is not None:
        # Fallback: necessary guards (function-level projection).
        from stateforge.state.goal_derivation import necessary_guard_edges
        out = []
        unresolved = []
        for g, kind in necessary_guard_edges(ir, target_block):
            conds, bad = _guard_conditions(fn, ir.blocks[g].start_line, kind,
                                           aliases, names, flask_names,
                                           blk.file, g, accesses)
            out.extend(conds)
            unresolved.extend(bad)
        return ([tuple(out)] if out else []), unresolved, truncated

    kind_by_edge = {(e.src, e.dst): e.kind for e in ir.edges}
    groups: list[tuple[SessionCondition, ...]] = []
    unresolved: list[UnresolvedItem] = []
    seen_groups: set[tuple] = set()
    for path in paths:
        conds: list[SessionCondition] = []
        for src, dst in zip(path, path[1:]):
            kind = kind_by_edge.get((src, dst), "flow")
            if kind not in ("true", "false"):
                continue
            guard_block = ir.blocks[src]
            gc, bad = _guard_conditions(fn, guard_block.start_line, kind,
                                        aliases, names, flask_names,
                                        blk.file, src, accesses)
            conds.extend(gc)
            unresolved.extend(bad)
        key = tuple(c.canonical for c in conds)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        groups.append(tuple(conds))
    return groups, unresolved, truncated


def query_session_frontier(ir: ProgramIR, session_index: SessionAccessIndex,
                           target_block: str,
                           accesses: Iterable[SessionAccess] | None = None
                           ) -> SessionFrontier:
    """Target block → session conditions + candidate writer blocks (MAY)."""
    blk, _tree, fn = _function_ast(ir, target_block)
    if blk is None:
        return SessionFrontier(target_block, "", "", [],
                               unresolved=(UnresolvedItem(
                                   target_block, "target-block-not-found"),),
                               partial=True)
    if fn is None:
        return SessionFrontier(target_block, blk.function, blk.file, [],
                               unresolved=(UnresolvedItem(
                                   target_block, "target-function-unparsable"),),
                               partial=True)
    if accesses is None:
        accesses = session_index.accesses
    accesses = list(accesses)
    fblocks = _function_blocks(ir, target_block)
    fn_accesses = [a for a in accesses if a.block_id in fblocks]

    groups, unresolved, truncated = _path_groups(
        ir, blk, _tree, fn, fn_accesses, target_block)

    # Relevant reads: all reads in the target function whose key appears in
    # any condition; fall back to all reads in the function.
    keys = {(c.key_kind, c.key_expr) for g in groups for c in g}
    relevant = [a for a in fn_accesses
                if a.operation in (OP_READ, OP_READ_MODIFY_WRITE)
                and ((a.key_kind, a.key_expr) in keys
                     or any(c.key_kind == a.key_kind
                            and c.key_expr == a.key_expr for c in
                            (cc for g in groups for cc in g)))]
    if not relevant:
        relevant = [a for a in fn_accesses
                    if a.operation in (OP_READ, OP_READ_MODIFY_WRITE)]

    deps: dict[tuple, SessionDependency] = {}
    for group in groups:
        for cond in group:
            reader_sites = [a.site_id for a in relevant
                            if a.key_kind == cond.key_kind
                            and a.key_expr == cond.key_expr]
            if not reader_sites:
                reader_sites = [cond.reader_site_id or
                                f"condition:{cond.source_file}:{cond.source_line}"]
            for reader_site in reader_sites:
                for writer in session_index.writers_for_key(
                        cond.key_kind, cond.key_expr):
                    overlap = _overlap((cond.key_kind, cond.key_expr),
                                       (writer.key_kind, writer.key_expr))
                    if overlap == DISJOINT:
                        continue
                    dep = SessionDependency(
                        writer_site_id=writer.site_id,
                        reader_site_id=reader_site,
                        writer_block=writer.block_id,
                        reader_block=session_index.by_site.get(
                            reader_site).block_id if reader_site in session_index.by_site
                        else None,
                        key_overlap=overlap,
                        candidate_effect={
                            "operation": writer.operation,
                            "key_kind": writer.key_kind,
                            "key_expr": writer.key_expr,
                            "value": writer.value_expr,
                        },
                        priority=_writer_priority(cond.key_kind, cond.key_expr,
                                                  writer, cond.op),
                    )
                    deps[(dep.writer_site_id, dep.reader_site_id,
                          dep.key_overlap)] = dep

    if not groups:
        unresolved.append(UnresolvedItem(
            what=f"target:{target_block}", cause="no-session-path-condition",
            source_file=blk.file, source_line=blk.start_line))
    if not deps:
        unresolved.append(UnresolvedItem(
            what=f"target:{target_block}", cause="no-static-session-writer",
            source_file=blk.file, source_line=blk.start_line))

    dedup_unresolved = []
    for u in unresolved:
        key = (u.what, u.cause, u.source_line)
        if key not in {(x.what, x.cause, x.source_line) for x in dedup_unresolved}:
            dedup_unresolved.append(u)
    unresolved = dedup_unresolved
    ordered = tuple(sorted(deps.values(),
                           key=lambda d: (d.priority, d.writer_block or "",
                                          d.writer_site_id)))
    return SessionFrontier(
        target_block=target_block,
        function=blk.function,
        file=blk.file,
        path_constraint_groups=tuple(groups),
        relevant_reads=tuple(relevant),
        candidate_writers=ordered,
        unresolved=tuple(unresolved),
        guard_blocks=tuple(sorted({c.guard_block for g in groups for c in g
                                   if c.guard_block})),
        partial=bool(truncated or unresolved),
        evidence=EFFECT_STATIC_MAY,
    )
