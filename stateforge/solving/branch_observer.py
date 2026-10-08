"""Phase-4.1 Level 2:运行时分支观测(AST 重写,非 eval、非 fork)。

把目标源码中被支持的 if/while test 重写为观测调用,操作数按原始
求值序各求值一次;不支持的表达式(chained/BoolOp/…)原样保留——
由构造保证 OPERAND_UNAVAILABLE,绝不伪造。
gid 与静态 GuardRecovery 同键(file:line:col:op)。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from stateforge.solving.guard_ir import SUPPORTED_OPS


@dataclass
class BranchObservation:
    guard_id: str
    op: str                    # Eq/NotEq/.../truthy/not_truthy
    lhs: object
    rhs: object                # truthy 类为 None
    actual: bool               # 程序真实执行到的分支结果
    execution_index: int


class Observer:
    def __init__(self) -> None:
        self.records: list[BranchObservation] = []
        self.execution_index = 0

    # ---- 重写注入的钩子(操作数只求值一次,由调用参数语义保证) ----
    def cmp(self, gid: str, op_name: str, lhs, rhs) -> bool:
        result = {"Eq": lambda: lhs == rhs, "NotEq": lambda: lhs != rhs,
                  "Lt": lambda: lhs < rhs, "LtE": lambda: lhs <= rhs,
                  "Gt": lambda: lhs > rhs, "GtE": lambda: lhs >= rhs}[op_name]()
        self._rec(gid, op_name, lhs, rhs, bool(result))
        return result

    def truth(self, gid: str, value) -> object:
        self._rec(gid, "truthy", value, None, bool(value))
        return value                          # 原语义:if 对 value 求真值

    def not_truth(self, gid: str, value) -> bool:
        result = not bool(value)
        self._rec(gid, "not_truthy", value, None, result)
        return result

    def _rec(self, gid, op, lhs, rhs, actual) -> None:
        self.execution_index += 1
        self.records.append(BranchObservation(
            gid, op, lhs, rhs, actual, self.execution_index))

    def by_guard(self, gid: str) -> list[BranchObservation]:
        return [r for r in self.records if r.guard_id == gid]


class _Rewrite(ast.NodeTransformer):
    def __init__(self, file_tag: str):
        self.file_tag = file_tag

    def _gid(self, node, op: str) -> str:
        return f"{self.file_tag}:{node.lineno}:{node.col_offset}:{op}"

    def _supported_compare(self, test: ast.expr):
        return (isinstance(test, ast.Compare) and len(test.ops) == 1
                and type(test.ops[0]).__name__ in SUPPORTED_OPS
                and len(test.comparators) == 1)

    def visit_If(self, node: ast.If) -> ast.If:
        self.generic_visit(node)
        node.test = self._rewrite_test(node.test)
        return node

    def visit_While(self, node: ast.While) -> ast.While:
        self.generic_visit(node)
        node.test = self._rewrite_test(node.test)
        return node

    def _rewrite_test(self, test: ast.expr) -> ast.expr:
        if self._supported_compare(test):
            op_name = type(test.ops[0]).__name__
            call = ast.Call(
                func=ast.Name("_sf_cmp", ast.Load()),
                args=[ast.Constant(self._gid(test, op_name)),
                      ast.Constant(op_name), test.left, test.comparators[0]],
                keywords=[])
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
                and isinstance(test.operand, (ast.Name, ast.Constant)):
            call = ast.Call(func=ast.Name("_sf_not", ast.Load()),
                            args=[ast.Constant(self._gid(test, "not_truthy")),
                                  test.operand], keywords=[])
        elif isinstance(test, (ast.Name, ast.Constant, ast.Call, ast.Attribute,
                                ast.Subscript)):
            call = ast.Call(func=ast.Name("_sf_truth", ast.Load()),
                            args=[ast.Constant(self._gid(test, "truthy")),
                                  test], keywords=[])
        else:
            return test                           # 不支持:原样保留,不观测
        return ast.copy_location(call, test)


def load_observed_target(path: str | Path) -> tuple[ModuleType, Observer]:
    """重写并加载目标模块;返回 (module, observer)。"""
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    tree = _Rewrite(str(path)).visit(tree)
    ast.fix_missing_locations(tree)
    observer = Observer()
    mod = ModuleType(f"observed_{path.stem}")
    mod.__file__ = str(path)
    mod._sf_cmp = observer.cmp
    mod._sf_truth = observer.truth
    mod._sf_not = observer.not_truth
    exec(compile(tree, str(path), "exec"), vars(mod))    # noqa: S102
    return mod, observer
