"""AST to function-level basic-block CFG.

The builder intentionally models only executable control flow.  Branch blocks
contain the test metadata but not the branch body, and break/continue edges
target the surrounding loop's exit/condition respectively.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from stateforge.core.ir import BasicBlock, FlowEdge, FunctionIR


@dataclass
class FunctionCFG:
    function: FunctionIR
    blocks: dict[str, BasicBlock] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    # Build-time ownership only; not serialized into ProgramIR.  Keeping AST
    # roots avoids assigning multiline calls to an unrelated line-range block.
    ast_nodes: dict[str, list[ast.AST]] = field(default_factory=dict)


class _Builder:
    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef, file: str,
                 module: str, id_file: str | None = None):
        self.node, self.file, self.module = node, file, module
        # H6(评审§2):ID 用相对路径(跨机器稳定);file 保持绝对路径供动态匹配
        self.id_file = id_file or file
        self.raw_blocks: list[tuple[str, str, list[ast.stmt], ast.expr | None]] = []
        self.edges: list[tuple[str, str, str, str | None]] = []
        self.ordinal = 0
        # Structural nodes without statements (e.g. the `try` anchor) still
        # own a source line; otherwise their line events fall outside every
        # block and cannot be attributed.
        self.anchor: dict[str, int] = {}
        self.returns: list[str] = []     # H1:return terminal 块,build 时统一连 exit

    def new(self, kind: str, stmts: list[ast.stmt] | None = None,
            test: ast.expr | None = None) -> str:
        self.ordinal += 1
        raw = f"b{self.ordinal}"
        self.raw_blocks.append((raw, kind, stmts or [], test))
        return raw

    def edge(self, src: str, dst: str, kind: str = "flow", condition: str | None = None):
        self.edges.append((src, dst, kind, condition))

    def append(self, block: str, stmt: ast.stmt) -> None:
        for i, (raw, kind, stmts, test) in enumerate(self.raw_blocks):
            if raw == block:
                stmts.append(stmt)
                self.raw_blocks[i] = (raw, kind, stmts, test)
                return

    def build(self) -> FunctionCFG:
        entry = self.new("entry")
        exits = self._sequence(self.node.body, [(entry, "flow")], [])
        exit_node = self.new("exit")
        for src, kind in exits:
            self.edge(src, exit_node, kind)
        # H1(评审§11):显式 return 的 terminal 块由 _sequence 返回空、
        # 不在 exits 里——必须单独连到 exit,否则 exit 不可达、
        # 动态 return 事件与静态图语义脱节。
        for block in self.returns:
            self.edge(block, exit_node, "flow")
        return self._finalize(entry, exit_node)

    def _sequence(self, stmts: list[ast.stmt], incoming: list[tuple[str, str]],
                  loops: list[tuple[str, str]]) -> list[tuple[str, str]]:
        current = incoming
        for stmt in stmts:
            current = self._stmt(stmt, current, loops)
        return current

    def _connect(self, incoming: list[tuple[str, str]], target: str) -> None:
        for src, kind in incoming:
            self.edge(src, target, kind)

    def _stmt(self, stmt: ast.stmt, incoming: list[tuple[str, str]],
              loops: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if isinstance(stmt, ast.If):
            branch = self.new("branch", test=stmt.test)
            self._connect(incoming, branch)
            true_end = self._sequence(stmt.body, [(branch, "true")], loops)
            false_end = self._sequence(stmt.orelse, [(branch, "false")], loops)
            if not stmt.orelse:
                false_end = [(branch, "false")]
            return true_end + false_end

        if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
            # ``for`` has no ``test`` field; its iterator is the condition
            # that controls whether another iteration is entered.
            test = getattr(stmt, "test", None)
            if test is None and isinstance(stmt, (ast.For, ast.AsyncFor)):
                test = stmt.iter
            branch = self.new("branch", test=test)
            self._connect(incoming, branch)
            after = self.new("loop_exit")
            body_end = self._sequence(stmt.body, [(branch, "true")],
                                      loops + [(branch, after)])
            for src, _ in body_end:
                self.edge(src, branch, "flow", "back")
            if stmt.orelse:
                false_end = self._sequence(stmt.orelse, [(branch, "false")], loops)
                for src, kind in false_end:
                    self.edge(src, after, kind)
            else:
                self.edge(branch, after, "false")
            return [(after, "flow")]

        if isinstance(stmt, ast.Break):
            block = self.new("terminal", [stmt])
            self._connect(incoming, block)
            if loops:
                self.edge(block, loops[-1][1], "flow", "break")
            return []

        if isinstance(stmt, ast.Continue):
            block = self.new("terminal", [stmt])
            self._connect(incoming, block)
            if loops:
                self.edge(block, loops[-1][0], "flow", "continue")
            return []

        if isinstance(stmt, ast.Return):
            block = self.new("terminal", [stmt])
            self._connect(incoming, block)
            self.returns.append(block)
            return []

        if isinstance(stmt, ast.Raise):
            # F1:raise 终止正常控制流,直连函数出口;不解码 enclosing
            # except handler(与 Try 的保守近似一致,spec §10)
            block = self.new("terminal", [stmt])
            self._connect(incoming, block)
            self.returns.append(block)      # 复用 return→exit 的统一出口连接
            return []

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            # Keep the header distinct so later straight-line statements cannot
            # absorb it, and recurse into the body like any other suite.  Exit
            # protocol / exception suppression remain conservative.
            header = self.new("with", [stmt])
            self._connect(incoming, header)
            before_returns = len(self.returns)
            before_edges = len(self.edges)
            normal = self._sequence(stmt.body, [(header, "flow")], loops)
            # Python emits the header line again while leaving a context.
            # Reuse that observable block rather than inventing an ambiguous
            # same-line cleanup node. It conservatively has both entry and
            # cleanup continuations, including abrupt completion paths.
            self._connect(normal, header)
            for terminal in self.returns[before_returns:]:
                self.edge(terminal, header)
            if self.returns[before_returns:]:
                self.returns.append(header)
            for src, dst, kind, condition in list(self.edges[before_edges:]):
                if condition in ("break", "continue"):
                    self.edge(src, header)
                    self.edge(header, dst, kind, condition)
            return [(header, "flow")] if normal else []

        if isinstance(stmt, ast.Try):
            # CONSERVATIVE APPROXIMATION(评审§12):handler 从 try 入口可达
            # (flow+condition),不是从真实抛出点出发的 exception 边;
            # 正常流与异常派发在静态上均自 try_entry 出发——过近似,已记
            # spec §10;动态 exception 事件仅留诊断,不参与结构反馈。
            try_entry = self.new("try")
            self.anchor[try_entry] = stmt.lineno
            self._connect(incoming, try_entry)
            normal = self._sequence(stmt.body, [(try_entry, "flow")], loops)
            for handler in stmt.handlers:
                # Keep the first-stage edge vocabulary small.  Exception
                # dispatch is represented as a flow edge with condition
                # metadata rather than introducing a sixth edge kind.
                handler_end = self._sequence(handler.body, [(try_entry, "flow")], loops)
                normal.extend(handler_end)
            if stmt.orelse:
                normal = self._sequence(stmt.orelse, normal, loops)
            if stmt.finalbody:
                normal = self._sequence(stmt.finalbody, normal, loops)
            return normal

        # Consecutive straight-line statements share one block.  This keeps
        # IDs stable while preserving the exact statement range.
        if incoming and len(incoming) == 1 and incoming[0][1] == "flow":
            src = incoming[0][0]
            if self.raw_blocks and any(r == src and k == "block" for r, k, _, _ in self.raw_blocks):
                self.append(src, stmt)
                return [(src, "flow")]
        block = self.new("block", [stmt])
        self._connect(incoming, block)
        return [(block, "flow")]

    def _finalize(self, entry: str, exit_node: str) -> FunctionCFG:
        ids: dict[str, str] = {}
        used: dict[str, int] = {}
        blocks: dict[str, BasicBlock] = {}
        ast_nodes: dict[str, list[ast.AST]] = {}
        for raw, kind, stmts, test in self.raw_blocks:
            # Identity uses the statement *start* lines only, so ids stay
            # stable; the line set additionally covers each statement's whole
            # span, so continuation lines of a multi-line statement map here
            # instead of falling outside every block.
            starts = [getattr(s, "lineno", self.node.lineno) for s in stmts]
            lines = []
            if kind == "with":
                # The header owns its own line and the context expressions;
                # the body belongs to the recursively built blocks and must
                # not claim its first line twice.
                for stmt in stmts:
                    lines.append(getattr(stmt, "lineno", self.node.lineno))
                    for item in stmt.items:
                        start = getattr(item.context_expr, "lineno", stmt.lineno)
                        end = getattr(item.context_expr, "end_lineno", start) or start
                        lines.extend(range(start, end + 1))
            else:
                for stmt in stmts:
                    start = getattr(stmt, "lineno", self.node.lineno)
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                        # A nested definition is one line for its parent: its
                        # body belongs to the nested function's own blocks.
                        lines.append(start)
                        continue
                    end = getattr(stmt, "end_lineno", start) or start
                    lines.extend(range(start, end + 1))
            if test is not None:
                test_lines = range(getattr(test, "lineno", self.node.lineno),
                                   getattr(test, "end_lineno", test.lineno) + 1)
                starts.extend(test_lines)
                lines.extend(test_lines)
            if not starts:
                starts = [self.node.lineno]
            if not lines:
                # Structural anchors (the `try` header) keep their own line in
                # the match set while the id stays on the function's line.
                lines = [self.anchor.get(raw, self.node.lineno)]
            lo, hi = min(starts), max(starts)
            base = f"{self.id_file}::{self.node.name}::{lo}-{hi}::{kind}"
            used[base] = used.get(base, 0) + 1
            bid = f"{base}::{used[base]}"
            ids[raw] = bid
            if kind == "with":
                # Body ASTs belong to their recursively built blocks.
                ast_nodes[bid] = [item.context_expr for stmt in stmts
                                  for item in stmt.items]
            else:
                ast_nodes[bid] = ([test] if test is not None else []) + list(stmts)
            text = [ast.unparse(s) for s in stmts]
            if test is not None:
                text = [f"<test> {ast.unparse(test)}"] + text
            blocks[bid] = BasicBlock(bid, self.file, self.module, self.node.name,
                                     lo, hi, kind, text, sorted(set(lines)))
        edges: list[FlowEdge] = []
        for src, dst, kind, condition in self.edges:
            if src in ids and dst in ids:
                edges.append(FlowEdge(ids[src], ids[dst], kind, condition))
        qname = f"{self.module}.{self.node.name}"
        fn = FunctionIR(qname, self.file, self.module, ids[entry], ids[exit_node], list(blocks))
        return FunctionCFG(fn, blocks, edges, ast_nodes)


def build_function_cfg(node: ast.FunctionDef | ast.AsyncFunctionDef,
                       file: str, module: str,
                       id_file: str | None = None) -> FunctionCFG:
    return _Builder(node, file, module, id_file).build()
