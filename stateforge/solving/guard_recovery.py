"""Phase-4.1 Level 1:Frontier edge → BranchGuard 恢复(静态)。

路线:frontier (u,v) → u 必为 branch 块 → 反解原文件该行所属函数的
复合语句 → 取其 test 表达式 → 按 4.1 支持面分类(==/!=/</<=/>/>=/truthy);
解释不了的返回 UNSUPPORTED_GUARD,不猜。
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.solving.guard_ir import OP_SYMBOL, SUPPORTED_OPS, BranchGuard


def _function_map(ir: ProgramIR) -> dict[str, ast.FunctionDef]:
    """qname → AST(按 ir.metadata.root 重解析,源即真相)。"""
    root = Path(ir.metadata["root"])
    out: dict[str, ast.FunctionDef] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(root))
        # icfg 模块名 = 相对路径去后缀;块 file 为绝对路径,键用 rel 前缀匹配
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[str(path)] = out.get(str(path)) or {}
        # 简化:存 (abs_path, func_name, lineno) 的 test 表索引
    return out


class _TestIndex:
    """(函数名, test_lineno) → test 表达式 AST;覆盖 if/while/for 与嵌套。"""

    def __init__(self, ir: ProgramIR):
        self.by_line: dict[tuple[str, int], ast.expr] = {}
        root = Path(ir.metadata["root"])
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._index_function(node)

    def _index_function(self, fn: ast.FunctionDef) -> None:
        stack = list(fn.body)
        while stack:
            stmt = stack.pop()
            if isinstance(stmt, (ast.If, ast.While)):
                test = getattr(stmt, "test", None)
                if test is not None:
                    self.by_line[(fn.name, test.lineno)] = test
            if isinstance(stmt, ast.For) and hasattr(stmt, "iter"):
                self.by_line.setdefault((fn.name, stmt.iter.lineno), stmt.iter)
            stack.extend(ast.iter_child_nodes(stmt))

    def get(self, func: str, lineno: int) -> ast.expr | None:
        return self.by_line.get((func, lineno))


def _classify(test: ast.expr) -> tuple[str, str]:
    """→ (op, expression_text);不支持 → ("UNSUPPORTED", text)。"""
    text = ast.unparse(test)
    if isinstance(test, ast.Compare) and len(test.ops) == 1 \
            and isinstance(test.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
                                         ast.Gt, ast.GtE)) \
            and len(test.comparators) == 1:
        return type(test.ops[0]).__name__, text
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = test.operand
        if isinstance(inner, ast.Name) or isinstance(inner, ast.Constant):
            return "truthy", text
        return "UNSUPPORTED", text
    if isinstance(test, (ast.Name, ast.Constant, ast.Call, ast.Attribute,
                          ast.Subscript)):
        return "truthy", text
    # In / NotIn 也是真假测试(成员测试),归入 truthy;LLM 可从 source 推理
    if isinstance(test, ast.Compare) and len(test.ops) == 1 \
            and isinstance(test.ops[0], (ast.In, ast.NotIn)):
        return "truthy", text
    return "UNSUPPORTED", text


def recover_guard(ir: ProgramIR, source_block: str, target_block: str,
                  polarity: str, test_index: _TestIndex | None = None
                  ) -> BranchGuard:
    block = ir.blocks[source_block]
    idx = test_index or _TestIndex(ir)
    test = idx.get(block.function, block.start_line)
    if block.kind != "branch" or test is None:
        return BranchGuard(
            guard_id=f"{block.file}:{block.start_line}:0:none",
            source_block=source_block, target_block=target_block,
            polarity=polarity, expression="", op="none",
            lineno=block.start_line, col_offset=0,
            status="BRANCH_NOT_FOUND")
    op, text = _classify(test)
    status = "ok" if op != "UNSUPPORTED" else "UNSUPPORTED_GUARD"
    return BranchGuard(
        guard_id=f"{block.file}:{test.lineno}:{test.col_offset}:{op}",
        source_block=source_block, target_block=target_block,
        polarity=polarity, expression=text,
        op="UNSUPPORTED" if op == "UNSUPPORTED" else op,
        lineno=test.lineno, col_offset=test.col_offset, status=status)


def recover_frontier_guards(ir: ProgramIR,
                            frontier_edges: list[tuple[str, str]]) -> list[BranchGuard]:
    """对一批 frontier edge 恢复 guard(一次建索引)。"""
    idx = _TestIndex(ir)
    out = []
    for (u, v) in frontier_edges:
        edge = next(e for e in ir.edges if e.src == u and e.dst == v)
        out.append(recover_guard(ir, u, v, edge.kind, idx))
    return out
