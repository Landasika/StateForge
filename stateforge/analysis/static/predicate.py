"""Static predicate extraction: 把 CFG 分支块的测试条件还原成结构化谓词。

只承认**可确定性判定**的一类比较(单运算符、操作数受限),其余显式归类为
``UnsupportedPredicate`` 并给出原因——不做"猜一个谓词"这种伪造。所有失败
路径都返回结构化原因而非抛异常,便于上游统计"静态可解率"。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from pathlib import Path

from stateforge.core.ir import ProgramIR


_OPERATORS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
}

_ALLOWED_OPERANDS = (
    ast.Name,
    ast.Constant,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


@dataclass(frozen=True)
class PredicateInfo:
    predicate_id: str
    block_id: str
    source_file: str
    line: int
    expression: str
    operator: str | None
    true_successor: str | None
    false_successor: str | None
    literal: str | None = None
    literal_side: str | None = None

    def outcome_for(self, successor: str | None) -> bool | None:
        """某个后继对应谓词的 True/False;无关后继返回 None。"""
        if successor is None:
            return None
        if successor == self.true_successor:
            return True
        if successor == self.false_successor:
            return False
        return None


@dataclass(frozen=True)
class UnsupportedPredicate:
    block_id: str
    source_file: str
    line: int
    expression: str
    reason: str


@dataclass
class PredicateExtraction:
    predicates: list[PredicateInfo]
    unsupported: list[UnsupportedPredicate]

    def by_block(self) -> dict[str, PredicateInfo]:
        return {predicate.block_id: predicate for predicate in self.predicates}

    def by_id(self) -> dict[str, PredicateInfo]:
        return {predicate.predicate_id: predicate for predicate in self.predicates}

    def get(self, block_id: str) -> PredicateInfo | None:
        return self.by_block().get(block_id)

    def unsupported_for(self, block_id: str) -> UnsupportedPredicate | None:
        for item in self.unsupported:
            if item.block_id == block_id:
                return item
        return None


class PredicateExtractor:
    def __init__(self, ir: ProgramIR):
        self.ir = ir
        self._source_cache: dict[str, tuple[ast.Module | None, str | None]] = {}
        # callsite → returnsite:测试表达式含调用时,CFG 把 true/false 边挂在
        # callsite 之后新增的 returnsite 上(调用返回后才分叉)。这条配对与
        # runtime/mapper.py 的口径一致(按 callee + callsite_line 匹配),
        # 在这里预算一次,避免逐块重扫全图。
        self._returnsite_for = self._build_returnsite_map()

    def _build_returnsite_map(self) -> dict[str, str]:
        returnsites: dict[tuple[str, int], str] = {}
        for edge in self.ir.edges:
            if (edge.kind == "return" and edge.callsite_line is not None
                    and edge.callee):
                returnsites[(edge.callee, edge.callsite_line)] = edge.dst
        mapping: dict[str, str] = {}
        for edge in self.ir.edges:
            if edge.kind != "call" or not edge.callee:
                continue
            src = self.ir.blocks.get(edge.src)
            if src is None:
                continue
            line = edge.callsite_line or src.start_line
            returnsite = returnsites.get((edge.callee, line))
            if returnsite is not None:
                mapping[edge.src] = returnsite
        return mapping

    def extract(self, block_ids: list[str] | None = None) -> PredicateExtraction:
        if block_ids is None:
            block_ids = [block_id for block_id in self.ir.blocks
                         if self._is_branch_block(block_id)]
        predicates: list[PredicateInfo] = []
        unsupported: list[UnsupportedPredicate] = []
        for block_id in block_ids:
            item = self.extract_block(block_id)
            if isinstance(item, PredicateInfo):
                predicates.append(item)
            else:
                unsupported.append(item)
        return PredicateExtraction(predicates, unsupported)

    def extract_block(self, block_id: str) -> PredicateInfo | UnsupportedPredicate:
        block = self.ir.blocks.get(block_id)
        if block is None:
            return UnsupportedPredicate(block_id, "", 0, "", "missing_block")
        expression = self._test_expression(block)
        if expression is None:
            return UnsupportedPredicate(
                block_id, block.file, block.start_line, "", "missing_test")
        true_successor, false_successor, reason = self._successors(block_id)
        if reason is not None:
            return UnsupportedPredicate(
                block_id, block.file, block.start_line, expression, reason)
        node = self._test_node(block, expression)
        if isinstance(node, UnsupportedPredicate):
            return node
        test = self._test_of(node)
        if test is None:
            return UnsupportedPredicate(
                block_id, block.file, block.start_line, expression,
                "missing_test")
        supported, detail = self._classify(test)
        if not supported:
            return UnsupportedPredicate(
                block_id, block.file, block.start_line, expression, detail)
        operator = _OPERATORS[type(test.ops[0])]
        literal, literal_side = self._literal(test)
        predicate_id = self._predicate_id(block_id, expression)
        return PredicateInfo(
            predicate_id=predicate_id,
            block_id=block_id,
            source_file=block.file,
            line=block.start_line,
            expression=expression,
            operator=operator,
            true_successor=true_successor,
            false_successor=false_successor,
            literal=literal,
            literal_side=literal_side,
        )

    def _is_branch_block(self, block_id: str) -> bool:
        """只认**带 <test> 语句**的块。

        测试表达式含调用时,CFG 把 true/false 边挂在 callsite 之后新增的
        returnsite 上(调用返回后才分叉),returnsite 自己也有 true/false 边
        却没有 <test>——那是分支的"后半段",不是独立谓词,必须排除,否则
        每个含调用的条件都会多报一条 missing_test 噪声。
        """
        return self._test_expression(self.ir.blocks[block_id]) is not None

    @staticmethod
    def _test_expression(block) -> str | None:
        for statement in block.statements:
            if statement.startswith("<test> "):
                return statement[len("<test> "):]
        return None

    def _successors(self, block_id: str
                    ) -> tuple[str | None, str | None, str | None]:
        """取分支的真/假后继。

        若分支块自身没有 true/false 边(测试里调用了函数),沿 ``flow`` 边
        穿过 callsite 走到 returnsite 再取——那才是分支实际发生的地方。
        """
        true_targets, false_targets, reason = self._collect_edges(block_id)
        if reason is None and (true_targets or false_targets):
            return self._finalize(true_targets, false_targets)
        if reason is not None:
            return None, None, reason
        for successor in self._flow_successors(block_id, depth=0):
            true_targets, false_targets, reason = self._collect_edges(successor)
            if reason is not None:
                return None, None, reason
            if true_targets or false_targets:
                return self._finalize(true_targets, false_targets)
        return None, None, "missing_branch_edges"

    def _collect_edges(self, block_id: str
                       ) -> tuple[list[str], list[str], str | None]:
        true_targets: list[str] = []
        false_targets: list[str] = []
        for edge in self.ir.outgoing(block_id):
            if edge.kind == "true":
                true_targets.append(edge.dst)
            elif edge.kind == "false":
                false_targets.append(edge.dst)
        if len(set(true_targets)) > 1 or len(set(false_targets)) > 1:
            return [], [], "ambiguous_successors"
        return true_targets, false_targets, None

    @staticmethod
    def _finalize(true_targets: list[str], false_targets: list[str]
                  ) -> tuple[str | None, str | None, str | None]:
        return (true_targets[0] if true_targets else None,
                false_targets[0] if false_targets else None,
                None)

    def _flow_successors(self, block_id: str, depth: int) -> list[str]:
        """沿"分支后半段"往后找:直接 flow 后继,或 callsite→对端 returnsite。

        callsite 经 ``call`` 边进 callee、再由 callee exit 的 ``return`` 边
        回到 returnsite,所以不能只跟 flow 边。限深避免顺着直线控制流走远,
        把后面别的分支的后继误当成本谓词的后继。
        """
        if depth >= 2:
            return []
        found: list[str] = []
        for edge in self.ir.outgoing(block_id):
            if edge.kind != "flow":
                continue
            found.append(edge.dst)
            found.extend(self._flow_successors(edge.dst, depth + 1))
        paired = self._returnsite_for.get(block_id)
        if paired is not None:
            found.append(paired)
        return found

    def _test_node(self, block, expression: str):
        """回到源码定位这个分支块对应的 AST 节点(行号 + 表达式双重校验)。

        只按行号匹配会在"同一行多个 if"时认错,故用行号与 unparse 后的表达式
        同时相等来定位;定位不到就是 source_mismatch,宁可不给谓词。
        """
        path = Path(block.file)
        key = str(path)
        if key not in self._source_cache:
            try:
                text = path.read_text(encoding="utf-8")
                self._source_cache[key] = (
                    ast.parse(text, filename=key), None)
            except OSError:
                self._source_cache[key] = (None, "source_unavailable")
            except SyntaxError:
                self._source_cache[key] = (None, "source_syntax_error")
        tree, error = self._source_cache[key]
        if tree is None:
            return UnsupportedPredicate(
                block.id, block.file, block.start_line, expression,
                error or "source_unavailable")
        for node in ast.walk(tree):
            test = self._test_of(node)
            if test is None:
                continue
            if getattr(test, "lineno", None) != block.start_line:
                continue
            if ast.unparse(test) == expression:
                return node
        return UnsupportedPredicate(
            block.id, block.file, block.start_line, expression,
            "source_mismatch")

    @staticmethod
    def _test_of(node) -> ast.expr | None:
        if isinstance(node, (ast.If, ast.While)):
            return node.test
        if isinstance(node, (ast.For, ast.AsyncFor)):
            return node.iter
        return None

    @staticmethod
    def _classify(test: ast.expr) -> tuple[bool, str | None]:
        if isinstance(test, ast.Compare):
            if len(test.ops) > 1 or len(test.comparators) > 1:
                return False, "chained_comparison"
            if type(test.ops[0]) not in _OPERATORS:
                return False, "unsupported_operator"
            for operand in (test.left, test.comparators[0]):
                if not PredicateExtractor._supported_operand(operand):
                    return False, "unsupported_operand"
            return True, None
        if isinstance(test, ast.BoolOp):
            return False, "boolean_expression"
        if isinstance(test, ast.Call):
            return False, "function_call"
        if isinstance(test, ast.UnaryOp):
            return False, "unary_expression"
        return False, "unsupported_expression"

    @staticmethod
    def _supported_operand(node: ast.expr) -> bool:
        # 含调用/推导式/布尔/比较的表达式求值可能抛异常或有副作用,
        # 静态不可判定,一律不接受为谓词操作数。
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                            ast.GeneratorExp, ast.Lambda, ast.Call,
                            ast.BoolOp, ast.IfExp, ast.Compare)):
            return False
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return True
        if isinstance(node, ast.Attribute):
            return PredicateExtractor._supported_operand(node.value)
        if isinstance(node, ast.Subscript):
            return (PredicateExtractor._supported_operand(node.value)
                    and PredicateExtractor._supported_operand(node.slice))
        if isinstance(node, ast.Slice):
            return all(part is None or PredicateExtractor._supported_operand(part)
                       for part in (node.lower, node.upper, node.step))
        if isinstance(node, ast.BinOp):
            return (PredicateExtractor._supported_operand(node.left)
                    and PredicateExtractor._supported_operand(node.right))
        if isinstance(node, ast.UnaryOp):
            return PredicateExtractor._supported_operand(node.operand)
        return isinstance(node, _ALLOWED_OPERANDS)

    @staticmethod
    def _literal(test: ast.Compare) -> tuple[str | None, str | None]:
        if isinstance(test.left, ast.Constant):
            return ast.unparse(test.left), "left"
        right = test.comparators[0]
        if isinstance(right, ast.Constant):
            return ast.unparse(right), "right"
        return None, None

    def _predicate_id(self, block_id: str, expression: str) -> str:
        digest = hashlib.sha256(
            f"{block_id}|{expression}".encode("utf-8")).hexdigest()[:16]
        predicate_id = f"pred_{digest}"
        # 防御:谓词 id 不得与块 id 撞名,撞了就加后缀直到不撞。
        while predicate_id in self.ir.blocks:
            predicate_id = f"{predicate_id}_p"
        return predicate_id


def extract_predicates(ir: ProgramIR,
                       block_ids: list[str] | None = None
                       ) -> PredicateExtraction:
    return PredicateExtractor(ir).extract(block_ids)


def extract_predicate(ir: ProgramIR,
                      block_id: str) -> PredicateInfo | UnsupportedPredicate:
    return PredicateExtractor(ir).extract_block(block_id)
