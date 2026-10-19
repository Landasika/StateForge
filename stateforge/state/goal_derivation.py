"""goal_derivation.py —— Phase 2(MVP):目标基本块 → 状态约束(StateGoal)。

路线(顾问 Phase-2 口径):给定未覆盖目标块,在 ICFG 上找其**必要分支边**
(删边后入口不可达目标的 true/false 边 = 控制依赖守卫),再把守卫块的
行号区间匹配到 StateFact 的 CONTROL 读点,收集其结构化谓词,构成
StateGoal.constraints——"要覆盖此块,持久状态须满足这些条件"。

诚实边界(MVP):
- 必要边 = 函数内支配近似(删边可达性),不做过程间;
- 谓词来自 extract_state_facts 的词面(容器 dict 库/Session/ORM),
  词面外的守卫不产约束(与 5A 词汇边界一致);
- EQ 谓词值侧为动态变量时 value=None(主体侧信息保留,Phase-3 规划期
  经绑定上下文补全)。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from stateforge.core.ir import ProgramIR
from stateforge.state.model import StateFactCandidate, StructuredPredicate


@dataclass(frozen=True)
class StateGoal:
    """覆盖目标块所需的持久状态约束集。

    评审轮二扩展(§6.6,全部缺省空——旧构造/旧语义零变化):
    relations/entity_vars/var_provenance/path_conditions/unresolvable
    由 ``derive_relational_goal`` 填充;``derive_state_goal`` 不填。
    """

    target_block: str
    function: str
    file: str
    constraints: tuple[StructuredPredicate, ...]
    guard_blocks: tuple[str, ...] = field(default=())
    evidence: tuple[str, ...] = field(default=())
    relations: tuple = field(default=())            # tuple[RelPredicate, ...]
    entity_vars: tuple[str, ...] = field(default=())
    var_provenance: tuple = field(default=())       # tuple[VarProvenance, ...]
    path_conditions: tuple = field(default=())      # tuple[PathCondition, ...]
    unresolvable: tuple = field(default=())         # tuple[UnresolvableItem, ...]

    def to_dict(self) -> dict:
        return {
            "target_block": self.target_block,
            "function": self.function,
            "file": self.file,
            "constraints": [p.to_dict() for p in self.constraints],
            "guard_blocks": list(self.guard_blocks),
            "evidence": list(self.evidence),
            "relations": [r.to_dict() for r in self.relations],
            "entity_vars": list(self.entity_vars),
            "var_provenance": [p.to_dict() for p in self.var_provenance],
            "path_conditions": [p.to_dict() for p in self.path_conditions],
            "unresolvable": [u.to_dict() for u in self.unresolvable],
        }


def _function_blocks(ir: ProgramIR, block_id: str) -> set[str]:
    """目标块所在函数的块集(按 file+function 归属)。"""
    blk = ir.blocks.get(block_id)
    if blk is None:
        return set()
    return {b.id for b in ir.blocks.values()
            if b.file == blk.file and b.function == blk.function}


def _reachable(src: str, dst: str, block_ids: set[str], ir: ProgramIR,
               skip_edge: tuple[str, str] | None = None) -> bool:
    """函数子图内 src→dst 可达性(可删一条边)。"""
    adj: dict[str, list[str]] = {}
    for e in ir.edges:
        if e.src not in block_ids or e.dst not in block_ids:
            continue
        if skip_edge and (e.src, e.dst) == skip_edge:
            continue
        adj.setdefault(e.src, []).append(e.dst)
    seen = {src}
    stack = [src]
    while stack:
        n = stack.pop()
        if n == dst:
            return True
        for m in adj.get(n, []):
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return False


def _entry_of(ir: ProgramIR, block_ids: set[str]) -> str | None:
    """函数子图入口(无入边的块;多入口取行号最小者)。"""
    has_in = {e.dst for e in ir.edges
              if e.src in block_ids and e.dst in block_ids}
    entries = [b for b in block_ids if b not in has_in]
    if not entries:
        return None
    return min(entries, key=lambda b: ir.blocks[b].start_line)


def necessary_guard_edges(ir: ProgramIR,
                          target_block: str) -> list[tuple[str, str]]:
    """支配目标块的 (分支块, 必要边方向)——方向感知版。

    false 边 = 目标在守卫的否定侧(如越权分支 owner≠uid),
    是谓词反转(EQ→NE)的依据。
    """
    fblocks = _function_blocks(ir, target_block)
    entry = _entry_of(ir, fblocks)
    if entry is None or entry == target_block:
        return []
    out: list[tuple[str, str]] = []
    branch_blocks = [b for b in fblocks
                     if any(e.src == b and e.kind in ("true", "false")
                            for e in ir.edges)]
    for b in branch_blocks:
        for e in ir.edges:
            if e.src != b or e.kind not in ("true", "false"):
                continue
            if not _reachable(entry, target_block, fblocks, ir,
                              skip_edge=(e.src, e.dst)):
                if (b, e.kind) not in out:
                    out.append((b, e.kind))
                break
    return sorted(out, key=lambda t: ir.blocks[t[0]].start_line)


def necessary_guards(ir: ProgramIR, target_block: str) -> list[str]:
    """支配目标块的分支块:删其任一出边后入口不可达目标。"""
    return [b for b, _kind in necessary_guard_edges(ir, target_block)]


def _same_file(a: str, b: str) -> bool:
    """文件路径形态归一(icfg 产绝对路径,extractor 可能为相对路径)。"""
    from pathlib import Path

    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _facts_in_block(facts: list[StateFactCandidate], file: str,
                    start: int, end: int) -> list[StateFactCandidate]:
    return [f for f in facts
            if _same_file(f.source_file, file) and start <= f.lineno <= end
            and f.relevance.value == "CONTROL"
            and f.structured_predicate is not None]


def derive_state_goal(ir: ProgramIR, facts: list[StateFactCandidate],
                      target_block: str) -> StateGoal | None:
    """目标块 → StateGoal(必要守卫上的 CONTROL 状态读点谓词集)。

    方向感知(否定谓词):目标经守卫 **false 边**到达时,该守卫的
    EQ/IN 谓词反转(NE/NOT_IN)——越权分支(owner≠uid)的约束来源。
    EXISTS 不反转(None-检查/真值的语义已内含方向,边界见文档)。
    """
    blk = ir.blocks.get(target_block)
    if blk is None:
        return None
    from stateforge.state.model import PredicateOp, StructuredPredicate

    _invert = {"EQ": PredicateOp.NE, "IN": PredicateOp.NOT_IN}
    guards = necessary_guard_edges(ir, target_block)
    constraints: list = []
    evidence: list[str] = []
    for g, edge_kind in guards:
        gblk = ir.blocks[g]
        for f in _facts_in_block(facts, gblk.file,
                                 gblk.start_line, gblk.end_line):
            pred = f.structured_predicate
            if edge_kind == "false" and pred.operator.value in _invert:
                pred = StructuredPredicate(
                    _invert[pred.operator.value], pred.subject, pred.value)
            if pred in constraints:
                continue                      # 同谓词去重(EXISTS 双源)
            constraints.append(pred)
            evidence.append(
                f"{f.source_file}:{f.lineno} {f.object_ref}"
                f" {pred.operator.value}"
                f"{'' if edge_kind == 'true' else '(false 边反转)'}")
    # 目标块自身的 CONTROL 读点(块内守卫)也入约束
    for f in _facts_in_block(facts, blk.file, blk.start_line, blk.end_line):
        if f.structured_predicate in constraints:
            continue
        constraints.append(f.structured_predicate)
        evidence.append(
            f"{f.source_file}:{f.lineno} {f.object_ref}"
            f" {f.structured_predicate.operator.value}")
    return StateGoal(
        target_block=target_block,
        function=blk.function,
        file=blk.file,
        constraints=tuple(constraints),
        guard_blocks=tuple(b for b, _kind in guards),
        evidence=tuple(evidence),
    )


# ── 关系目标推导(评审轮二/契约 §6.6;验收关口 A)────────────────────
#
# 方向分析采用**词法早退惯用法**,不复用 necessary_guard_edges 的函数级
# 支配分析——后者在含调用函数上会静默退化(实测:callsite→returnsite 的
# 连接是过程间边,被 _function_blocks 过滤后子图在每个调用点断裂,
# entry 对 ship_order 目标不可达 → 任一边移除都"不可达" → 全部分支
# 被误报必要;旧测试因夹具无调用而未暴露)。词法口径:
#
#   guard-if  G:G.body 全路径终止(Return/Raise)且无 else —— 真侧早退,
#              继续执行 ⇒ ¬test(via_edge="false",谓词反转);目标在
#              G.body 内 ⇒ test 成立(via_edge="true",不反转)。
#   wrapper-if W:非 guard-if、无 else、body 内含 guard-if —— 其内守卫
#              仅在 W 为真时构成目标前置(fall-through 合并口径),W 的
#              test 记为 PathCondition(config/param/local),不是状态谓词。
#
# 诚实边界(阶段 2 §2.3):支持直线早退守卫链;循环/try 内的守卫、
# 带 else 的 wrapper、多比较链不解析 → UnresolvableItem,不猜。

def _all_terminate(stmts) -> bool:
    """语句列表全路径终止:某语句全路径终止 ⇒ 其后不可达 ⇒ True。"""
    for s in stmts:
        if _stmt_all_terminate(s):
            return True
    return False


def _stmt_all_terminate(s) -> bool:
    if isinstance(s, (ast.Return, ast.Raise)):
        return True
    if isinstance(s, ast.If):
        return (_all_terminate(s.body)
                and bool(s.orelse) and _all_terminate(s.orelse))
    return False                                   # 循环/with/… 保守不判


def _parent_map(fn: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _inside_loop_or_try(node, parents: dict[int, ast.AST]) -> bool:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.While, ast.For, ast.AsyncFor, ast.Try)):
            return True
        cur = parents.get(id(cur))
    return False


def _enclosing_ifs(node, fn: ast.AST,
                   parents: dict[int, ast.AST]) -> list[ast.If]:
    out: list[ast.If] = []
    cur = parents.get(id(node))
    while cur is not None and cur is not fn:
        if isinstance(cur, ast.If):
            out.append(cur)
        cur = parents.get(id(cur))
    return out


def _classify_ifs(fn: ast.AST) -> tuple[set[int], set[int]]:
    """(guard-if id 集, wrapper-if id 集);循环/try 内与带 else 的排除。"""
    parents = _parent_map(fn)
    guards: set[int] = set()
    wrappers: set[int] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or node.orelse:
            continue
        if _inside_loop_or_try(node, parents):
            continue
        if _all_terminate(node.body):
            guards.add(id(node))
        else:
            wrappers.add(id(node))
    return guards, wrappers


# 源码算子 →(基态 op,反转 op);Is/IsNot 仅 None 比较形态支持
_CMP_BASE = {"Eq": ("EQ", "NE"), "NotEq": ("NE", "EQ"),
             "In": ("IN", "NOT_IN"), "NotIn": ("NOT_IN", "IN")}
_CMP_NONE = {"Is": ("NOT_EXISTS", "EXISTS"),
             "IsNot": ("EXISTS", "NOT_EXISTS")}


def _compare_relation(term, op_name: str, invert: bool,
                      via_edge: str, src: str, line: int):
    """compare GuardTerm → (RelPredicate | None, UnresolvableItem | None)。"""
    from stateforge.state.relational import (Const, Field, RelPredicate,
                                              RelOp, UnresolvableItem)

    left, right = term.left, term.right
    if left is None or right is None:
        return None, UnresolvableItem(
            what=f"term:{term.raw}", cause="term-unparseable",
            source_file=src, source_line=line)
    if isinstance(left, Const) and isinstance(right, Const):
        return None, UnresolvableItem(
            what=f"term:{term.raw}", cause="const-only-compare",
            source_file=src, source_line=line)

    # Is/IsNot 与 None:存在性形态(左右归一为实体级 Field)
    if op_name in _CMP_NONE:
        entity, other = (left, right) if isinstance(left, Field) \
            else (right, left)
        if not isinstance(entity, Field) \
                or not (isinstance(other, Const) and other.value is None) \
                or entity.name:
            return None, UnresolvableItem(
                what=f"term:{term.raw}", cause="unsupported-none-compare",
                source_file=src, source_line=line)
        base, inv = _CMP_NONE[op_name]
        return RelPredicate(
            op=RelOp(inv if invert else base), left=entity, right=None,
            origin_op=op_name, source_file=src, source_line=line,
            via_edge=via_edge), None

    if op_name not in _CMP_BASE:
        return None, UnresolvableItem(
            what=f"term:{term.raw}", cause=f"unsupported-op:{op_name}",
            source_file=src, source_line=line)
    base, inv = _CMP_BASE[op_name]
    # 布尔对称算子:Const 在左时翻转两边保持 Field 在左(IN 靸对称例外:保持原序)
    if isinstance(left, Const) and isinstance(right, Field) \
            and op_name in ("Eq", "NotEq"):
        left, right = right, left
    return RelPredicate(
        op=RelOp(inv if invert else base), left=left, right=right,
        origin_op=op_name, source_file=src, source_line=line,
        via_edge=via_edge), None


def derive_relational_goal(ir, target_block: str) -> "StateGoal | None":
    """目标块 → 关系目标(StateGoal + §6.6 扩展字段)。

    词法早退惯用法(见模块节注释):guard-if 反转/直行 + wrapper 条件化;
    源码锚点与原始算子全程携带;无法解析项进 unresolvable(不猜)。
    """
    import ast as _ast
    from pathlib import Path as _Path

    from stateforge.state.extractor import extract_guard_terms
    from stateforge.state.provenance import analyze_provenance
    from stateforge.state.relational import (Field, PathCondition, RelOp,
                                              RelPredicate, UnresolvableItem,
                                              VarProvenance)

    blk = ir.blocks.get(target_block)
    if blk is None:
        return None
    src_file = blk.file
    try:
        tree = _ast.parse(_Path(src_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
               and n.name == blk.function), None)
    if fn is None:
        return None

    target_line = blk.start_line
    prov = analyze_provenance(src_file)
    terms = {t.lineno: t for t in extract_guard_terms(fn, src_file)}
    parents = _parent_map(fn)
    guard_ids, wrapper_ids = _classify_ifs(fn)

    relations: list[RelPredicate] = []
    path_conds: dict[tuple, PathCondition] = {}
    unres: list[UnresolvableItem] = []

    for node in _ast.walk(fn):
        if not isinstance(node, _ast.If) or id(node) not in guard_ids:
            continue
        term = terms.get(node.lineno)
        if term is None:
            continue
        inside_body = node.lineno <= target_line <= node.end_lineno
        after = node.end_lineno < target_line
        if not (inside_body or after):
            continue                       # 目标之前的守卫不影响到达
        invert = after                     # 早退反转;body 内直行
        via = "false" if after else "true"

        # 包裹 wrapper → 路径条件。两种口径都记录:
        #   fall-through(目标在 W 外,守卫仅 W-真路径上有效)与
        #   body-内(目标在 W body 里,W-真边是必经——目标只适用该配置)
        for w in _enclosing_ifs(node, fn, parents):
            w_on_path = (w.end_lineno < target_line
                         or w.lineno <= target_line <= w.end_lineno)
            if id(w) in wrapper_ids and w_on_path:
                wterm = terms.get(w.lineno)
                if wterm is not None and wterm.kind == "truthy" \
                        and isinstance(wterm.left, Field) \
                        and not wterm.left.name:
                    name = wterm.left.var.lstrip("?")
                    kind = prov.var_kind(blk.function, name)
                    key = (name, True, kind)
                    path_conds.setdefault(key, PathCondition(
                        var=name, requires=True, kind=kind,
                        source_file=src_file, source_line=w.lineno))
                else:
                    unres.append(UnresolvableItem(
                        what=f"wrapper:{terms[w.lineno].raw if w.lineno in terms else '?'}",
                        cause="wrapper-test-not-simple-name",
                        source_file=src_file, source_line=w.lineno))

        if term.kind == "compare" and term.op:
            rel, bad = _compare_relation(term, term.op, invert, via,
                                         src_file, node.lineno)
            if rel is not None:
                relations.append(rel)
            if bad is not None:
                unres.append(bad)
            continue

        if term.kind == "truthy" and isinstance(term.left, Field):
            fld = term.left
            name = fld.var.lstrip("?")
            if not fld.name:               # 实体级 truthy
                if prov.lookup(blk.function, name) is not None:
                    op = RelOp("NOT_EXISTS" if invert else "EXISTS")
                    relations.append(RelPredicate(
                        op=op, left=fld, right=None, origin_op="Truthy",
                        source_file=src_file, source_line=node.lineno,
                        via_edge=via))
                else:                      # 非实体局部(如 err)→ 路径条件
                    kind = prov.var_kind(blk.function, name)
                    key = (name, not invert, kind)
                    path_conds.setdefault(key, PathCondition(
                        var=name, requires=not invert, kind=kind,
                        source_file=src_file, source_line=node.lineno))
            else:                          # 字段级 truthy
                op = RelOp("FALSY" if invert else "TRUTHY")
                relations.append(RelPredicate(
                    op=op, left=fld, right=None, origin_op="Truthy",
                    source_file=src_file, source_line=node.lineno,
                    via_edge=via))
            continue

        unres.append(UnresolvableItem(
            what=f"guard:{term.raw}", cause=f"guard-kind:{term.kind}",
            source_file=src_file, source_line=node.lineno))

    # 实体变量登记 + 来源;无 provenance 的关系变量 → unresolvable(不猜表)
    entity_vars = sorted({f.var for r in relations
                          for f in (r.left, r.right)
                          if isinstance(f, Field)})
    var_prov: list[VarProvenance] = []
    for var in entity_vars:
        local = var.lstrip("?")
        p = prov.lookup(blk.function, local)
        if p is not None:
            var_prov.append(p)
        else:
            unres.append(UnresolvableItem(
                what=f"var:{var}",
                cause="provenance-unresolved(callee 不可分析/动态来源)",
                source_file=src_file, source_line=0))

    return StateGoal(
        target_block=target_block, function=blk.function, file=src_file,
        constraints=(),
        relations=tuple(relations),
        entity_vars=tuple(entity_vars),
        var_provenance=tuple(var_prov),
        path_conditions=tuple(path_conds.values()),
        unresolvable=tuple(unres),
        evidence=tuple(
            f"{r.source_file}:{r.source_line} {r.render()}"
            f"{'(false 边反转)' if r.via_edge == 'false' else ''}"
            for r in relations),
    )
