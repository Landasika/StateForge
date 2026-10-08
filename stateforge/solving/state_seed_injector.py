"""state_seed_injector.py —— P3-T3 规划器→序列种子注入(policy="state")。

轮间接缝上的"状态盲区补种":当深状态原子未达且上一轮无新状态节点
(R2)时,从**静态状态分析**(M1/M2,`run_state_analysis`)合成规划器
输入,`plan_backward` 反向推出请求链,渲染为 C10 序列种子写入语料。
下一轮 libFuzzer 把整条序列当一条输入执行并围绕它变异——参数槽位的
精确值不是本注入器的目标,fuzzer 的变异才是(见绑定策略)。

链路(全部真实组件,零 LLM、零新依赖)::

    run_state_analysis(靶目录)                 # M1/M2:谓词/守卫/变异候选
      └─(按靶文件过滤 + op 分派归属)→ MicroStateModel
           ├─ AppModel(每 op 一个 Endpoint)── analysis/builder.build_sstg
           │     action "POST <op>" ──requires──▶ state "Session.k=v"
           │            ▲──produces──┘(writer×guard 值join,soft 语义)
           └─ state/planning/backward.plan_backward(reach_state goal)
                 └─ PlanStep ×N ── BindingContext.render_step ──▶ 单请求 dict
                       └─ 末步补受门 op(R1:前缀步素状态,末步=当前请求)
                             └─ encode_sequence_seed ──▶ corpus 落盘

设计裁决(docstring 即契约,详见任务报告):

1. **SSTG 合成(soft-producer 语义)**:微靶无 AppModel。op 分派
   (`op == "login"` 分支)给动作词表;M2 静态候选给"哪行 SET/守卫哪个
   session 变量";二者按行号 join 归属到 op。写者×守卫值 join 出
   produces 边——`login` 写 `session["role"]`、别处守卫 `role=="admin"`
   ⇒ login 视为 `Session.role=admin` 的候选生产者(静态写不出"该请求
   真会置 admin",运行时由 fuzzer+覆盖裁决,与 backward.py 的
   soft-producer 同一哲学)。守卫无对应写者时由 plan_backward 的
   soft-producer 兜底接管。

2. **绑定策略**:goal 原子值为 EQ 字面值时,写路径上喂该变量的请求
   参数绑成字面值(`role="admin"`);其余(truthy/中性槽)一律绑中性值
   `"x"`。`?var/@fresh` 槽位经 `BindingContext` 渲染,salt 由靶路径哈希
   派生——**同输入必同种子字节**(seeded ordering)。

3. **状态节点记账(覆盖代理)**:entry_driver 不产会话观测,补观测即
   膨胀驱动(裁决禁止);故"原子已达"= 守卫真分支的 IR 块已覆盖
   (guard 行号 → AST 真分支行集 ∩ covered_blocks 行集)。
   **本模块不实例化 TransitionRecorder**(若未来接入动态边:一
   worker/会话一个 recorder,T1 评审契约);R2 的"上轮是否有新状态
   节点"由 epoch 侧 `state_ledger.json` 差分判定(本模块提供
   `reached_state_atoms`)。

4. 落盘与 llm 注入同款:CorpusManager spool(`<workdir>/state_spool`)+
   直接写 `corpus_dir/<cid>`(内容哈希去重);审计 JSONL 追加到
   `corpus_dir.parent/epochs/state_injections.jsonl`。
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import networkx as nx

from stateforge.analysis.builder import build_sstg
from stateforge.core.ir import ProgramIR
from stateforge.core.model import (AppModel, Effect, Endpoint, Param,
                                   PlanStep, Precondition)
from stateforge.fuzzing.corpus_manager import CorpusManager
from stateforge.fuzzing.entry_driver import encode_sequence_seed
from stateforge.state.observer import run_state_analysis
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.goals import GoalSpec

STATE_BUDGET = 3        # 每个注入点最多写入的序列种子数(llm 臂 max_seeds 同量级)
NEUTRAL = "x"           # 非字面值槽位的中性绑定:fuzzer 围绕它变异才是重点
_MUTATIONS = frozenset({"set", "update"})
_SCHEMA_KEYS = frozenset({"op", "json", "query", "form", "header", "cookie"})

# M2 守卫候选算子 → build_sstg 谓词词表(只取能落状态原子的算子;
# GT/GE/LT/LE 无原子语义,跳过)
_GUARD_TO_PRE = {
    "EQ": ("eq", True), "NE": ("ne", True), "IN": ("in", True),
    "TRUTHY": ("truthy", False), "IS_NOT_NONE": ("truthy", False),
    "FALSY": ("falsy", False), "IS_NONE": ("falsy", False),
}


# --------------------------------------------------------------- 数据形状


@dataclass(frozen=True)
class GuardInfo:
    """一条守卫的合成记录(M2 候选 + op 归属 + 真分支行集)。"""

    var: str                      # "Session.role"
    pre_op: str                   # eq/ne/in/truthy/falsy
    value: str | None
    line: int                     # 守卫 If/While 行(M2 证据行)
    body_lines: frozenset[int]    # 真分支行集(覆盖代理判达用)
    op: str                       # 所属 op(admin_panel)
    atom: str | None = None       # 关联 SSTG 原子(建图后回填)


@dataclass
class OpInfo:
    """一个请求 op 的动作词表项。"""

    name: str
    params: list[str] = field(default_factory=list)          # body 读的参数名
    writes: dict[str, str | None] = field(default_factory=dict)  # session 键 → 喂值参数
    func: str = "target"


@dataclass
class WriteInfo:
    op: str
    key: str                     # session 键
    feed: str | None             # 喂值参数名(解析不出 → None)
    line: int


@dataclass
class MicroStateModel:
    """微靶的规划器输入合成产物(AppModel + SSTG + 归属信息)。"""

    target: Path
    ops: dict[str, OpInfo]
    guards: list[GuardInfo]
    model: AppModel
    sstg: nx.MultiDiGraph


# ----------------------------------------------------------- AST 归属解析


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _stmt_lines(stmts) -> set[int]:
    out: set[int] = set()
    for s in stmts:
        out.update(range(s.lineno, s.end_lineno + 1))
    return out


def _dispatch_var(tree) -> str | None:
    """`op = str(body.get("op", ...))` / `x = body["op"]` → 分派变量名。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id == "str" and value.args:
            value = value.args[0]
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr == "get" and value.args \
                and isinstance(value.args[0], ast.Constant) \
                and value.args[0].value == "op":
            return names[0]
        if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant) \
                and value.slice.value == "op":
            return names[0]
    return None


def _op_branches(tree, dispatch: str) -> dict[str, set[int]]:
    """`if op == "login":` 分支行集(if 行 + 真分支体,不含 elif 链)。"""
    out: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        test = node.test
        if not (isinstance(test.left, ast.Name) and test.left.id == dispatch
                and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, str)):
            continue
        out.setdefault(test.comparators[0].value, set()).update(
            {node.lineno} | _stmt_lines(node.body))
    return out


def _body_var(tree) -> str | None:
    """`body = seed.get("json", seed)` → 请求体变量名。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) \
                and node.value.func.attr == "get" and node.value.args \
                and isinstance(node.value.args[0], ast.Constant) \
                and node.value.args[0].value == "json":
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names:
                return names[0]
    return None


def _owner(line: int, branches: dict[str, set[int]]) -> str | None:
    """行号 → 所属 op(嵌套分派时取最内层 = 最小分支)。"""
    for op in sorted(branches, key=lambda o: (len(branches[o]), o)):
        if line in branches[op]:
            return op
    return None


def _op_params(tree, branches, body_var) -> dict[str, set[str]]:
    """分支内读的请求参数名:body.get("K") / body["K"](剔除 schema 键)。"""
    out: dict[str, set[str]] = {op: set() for op in branches}
    if body_var is None:
        return out
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        keys: list[str] = []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == body_var and node.args \
                and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            keys.append(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                and isinstance(node.value, ast.Name) and node.value.id == body_var \
                and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            keys.append(node.slice.value)
        if not keys:
            continue
        op = _owner(line, branches)
        if op is None:
            continue
        for k in keys:
            if k not in _SCHEMA_KEYS:
                out[op].add(k)
    return out


def _session_writes(tree, branches, params) -> list[WriteInfo]:
    """`session["k"] = <rhs>` → WriteInfo(喂值参数 = rhs 名/body.get 键)。"""
    out: list[WriteInfo] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "session"
                and isinstance(node.targets[0].slice, ast.Constant)):
            continue
        key = str(node.targets[0].slice.value)
        op = _owner(node.lineno, branches)
        if op is None:
            continue                       # op 分派之外的 session 写:无请求面,跳过
        feed = _feed_param(node.value, params[op])
        out.append(WriteInfo(op=op, key=key, feed=feed, line=node.lineno))
    return out


def _feed_param(rhs, branch_params: set[str]) -> str | None:
    """写右侧表达式 → 喂值参数名(名字直用或 body.get("K") 键)。"""
    if isinstance(rhs, ast.Name):
        return rhs.id if rhs.id in branch_params else None
    if isinstance(rhs, ast.Call) and isinstance(rhs.func, ast.Attribute) \
            and rhs.func.attr == "get" and rhs.args \
            and isinstance(rhs.args[0], ast.Constant) \
            and isinstance(rhs.args[0].value, str):
        k = rhs.args[0].value
        return k if k in branch_params else None
    return None


def _if_bodies(tree) -> dict[int, frozenset[int]]:
    """If/While 行号 → 真分支行集(守卫判达的覆盖代理)。"""
    out: dict[int, frozenset[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            out.setdefault(node.lineno, frozenset(_stmt_lines(node.body)))
    return out


# ------------------------------------------------ M2 静态候选(权威登记)


def _target_candidates(path: Path) -> list[dict]:
    """run_state_analysis 的 M2 静态候选,过滤到本靶文件。"""
    try:
        analysis = run_state_analysis(path.parent)
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for cand in analysis.get("transitions", {}).get("static_candidates", []):
        evidence = (cand.get("evidence") or [""])[0]
        file_part = evidence.rsplit(":", 1)[0]
        try:
            same = Path(file_part).resolve() == path.resolve()
        except OSError:
            same = False
        if same:
            out.append(cand)
    return out


def _parse_guard_op(raw: str) -> tuple[str, str | None] | None:
    """"guard:EQ(admin)" → ("EQ", "admin");"guard:TRUTHY" → ("TRUTHY", None)。"""
    body = raw.removeprefix("guard:")
    if not body:
        return None
    if "(" in body and body.endswith(")"):
        kind, _, rest = body.partition("(")
        return kind, rest[:-1]
    return body, None


# --------------------------------------------------------------- 合成主入口


@lru_cache(maxsize=None)
def _synthesized_model(resolved_target: str) -> MicroStateModel | None:
    """synthesize_micro_model 的 memo 体(resolved 绝对路径 str 为键)。

    静态合成(M1/M2 run_state_analysis + AST 归属 + build_sstg)对同一
    静态靶文件结果不变:10-run 战役里每个轮间边界经 reached_state_atoms
    (1 次)+ inject_state_seeds(2 次)至多触发 3 次合成,重复解析纯属
    浪费(P4-T2 memo)。缓存对象被调用方共享是安全的——本模块与
    state_arm_report/plan_backward 均只读 model/sstg,无原地修改。
    """
    path = Path(resolved_target)
    if not path.is_file():
        return None
    tree = _parse(path)
    if tree is None:
        return None
    dispatch = _dispatch_var(tree)
    if dispatch is None:
        return None
    branches = _op_branches(tree, dispatch)
    if not branches:
        return None
    params = _op_params(tree, branches, _body_var(tree))
    writes = _session_writes(tree, branches, params)
    if_bodies = _if_bodies(tree)

    ops: dict[str, OpInfo] = {}
    guards: list[GuardInfo] = []
    write_map: dict[tuple[str, str], WriteInfo] = {}
    for w in writes:
        write_map[(w.op, w.key)] = w
        ops.setdefault(w.op, OpInfo(name=w.op))
    for cand in _target_candidates(path):
        line = int((cand.get("evidence") or ["0:0"])[0].rsplit(":", 1)[1])
        op = _owner(line, branches)
        if op is None:
            continue
        entry = ops.setdefault(op, OpInfo(name=op, func=cand.get("function") or "target"))
        if cand["op"] in _MUTATIONS:                       # 写候选:权威变量登记
            key = cand["var"].split(".", 1)[-1]
            w = write_map.get((op, key))
            entry.writes[key] = w.feed if w is not None else None
        else:                                              # 守卫候选
            parsed = _parse_guard_op(cand["op"])
            if parsed is None:
                continue
            kind, value = parsed
            pre = _GUARD_TO_PRE.get(kind)
            if pre is None:
                continue
            pre_op, has_val = pre
            guards.append(GuardInfo(
                var=f"Session.{cand['var'].split('.', 1)[-1]}",
                pre_op=pre_op, value=value if has_val else None,
                line=line, body_lines=if_bodies.get(line, frozenset()), op=op))
    for op, keys in params.items():
        ops.setdefault(op, OpInfo(name=op)).params = sorted(keys)

    guard_values: dict[str, list[str]] = {}                # session 键 → 原子值集
    for g in guards:
        part = g.value if g.pre_op in ("eq", "ne", "in") else (
            "True" if g.pre_op == "truthy" else "False")
        if part:
            guard_values.setdefault(g.var.split(".", 1)[-1], []).append(part)

    endpoints = []
    for op in sorted(ops):
        entry = ops[op]
        pres = [Precondition(var=g.var, op=g.pre_op, value=g.value,
                             file=path.name, line=g.line)
                for g in sorted((g for g in guards if g.op == op),
                                key=lambda g: g.line)]
        effs = []
        for key in sorted(entry.writes):
            values = sorted(set(guard_values.get(key, [])))
            line = write_map.get((op, key), WriteInfo(op, key, None, 0)).line
            for v in values or [None]:                     # 无守卫值 → 通配 "*"
                effs.append(Effect(var=f"Session.{key}", op="set", value=v,
                                   file=path.name, line=line))
        endpoints.append(Endpoint(
            func=entry.func, route=op, methods=["POST"],
            params=[Param(name=p, location="json") for p in entry.params],
            preconditions=pres, effects=effs,
            observes=sorted(set(entry.writes) |
                            {g.var.split(".", 1)[-1] for g in guards if g.op == op})))

    model = AppModel(app_root=str(path.parent), framework="flask",
                     endpoints=endpoints)
    sstg = build_sstg(model)
    micro = MicroStateModel(target=path, ops=ops, guards=[], model=model,
                            sstg=sstg)
    for g in guards:                                       # 守卫 → SSTG 原子回填
        part = g.value if g.pre_op in ("eq", "ne", "in") else (
            "True" if g.pre_op == "truthy" else "False")
        micro.guards.append(replace(g, atom=_requires_atom(
            sstg, f"POST {g.op}", g.var, part)))
    return micro


def synthesize_micro_model(target) -> MicroStateModel | None:
    """靶源码 → 规划器输入(AppModel + SSTG + op/守卫归属)。

    非 op 分派靶(无 `op == "..."` 请求词表)→ None(诚实跳过)。
    Memo(P4-T2,体见 _synthesized_model):按 resolved target path 缓存,
    同靶跨 str/Path 调用归一到同一键 → 同一模型对象;测试改写靶文件后
    需重算时调 `synthesize_micro_model.cache_clear()`。
    """
    return _synthesized_model(str(Path(target).resolve()))


# memo 控制面挂到公开名上(测试/工具清缓存或观测命中率,不必摸私有体)
synthesize_micro_model.cache_clear = _synthesized_model.cache_clear
synthesize_micro_model.cache_info = _synthesized_model.cache_info


def _requires_atom(sstg: nx.MultiDiGraph, action: str, var: str,
                   value_part: str | None) -> str | None:
    """动作的 requires 边中 (var, value) 匹配的原子(以建出的 SSTG 为权威)。"""
    if value_part is None:
        return None
    for _, v, d in sstg.out_edges(action, data=True):
        if d.get("kind") != "requires":
            continue
        attrs = sstg.nodes[v]
        if attrs.get("var") == var and attrs.get("value") == value_part:
            return v
    return None


# ------------------------------------------------------------ goal 与判达


def goal_atoms(sstg: nx.MultiDiGraph) -> set[str]:
    """SSTG 的全部 requires 原子 = 可规划目标(深状态节点词表)。"""
    return {v for _, v, d in sstg.edges(data=True) if d.get("kind") == "requires"}


def _coerce_ir(ir) -> ProgramIR | None:
    if ir is None or isinstance(ir, ProgramIR):
        return ir
    try:
        return ProgramIR.from_json(ir)
    except (OSError, ValueError):
        return None


def _covered_lines(ir: ProgramIR, target: Path, state: dict) -> set[int]:
    """覆盖代理:本靶文件内已覆盖块的行集。"""
    covered = set(state.get("covered_blocks") or state.get("blocks") or [])
    lines: set[int] = set()
    for bid in covered:
        block = ir.blocks.get(bid)
        if block is None:
            continue
        try:
            if Path(block.file).resolve() != target.resolve():
                continue
        except OSError:
            continue
        lines.update(block.line_numbers or
                     range(block.start_line, block.end_line + 1))
    return lines


def reached_state_atoms(ir, target, state: dict) -> set[str]:
    """已达状态原子:守卫真分支块已覆盖(覆盖代理记账,见模块 docstring §3)。"""
    micro = synthesize_micro_model(target)
    ir_obj = _coerce_ir(ir)
    if micro is None or ir_obj is None:
        return set()
    lines = _covered_lines(ir_obj, micro.target, state or {})
    return {g.atom for g in micro.guards
            if g.atom and lines & g.body_lines}


# ------------------------------------------------------------- 规划与渲染


def _goal_units(micro: MicroStateModel, reached: set[str]) -> list[tuple[str, str]]:
    """(受门 op, 首个未达原子)按 op 名排序——一个 op 一条种子(覆盖其全部前置)。"""
    out = []
    for op in sorted(micro.ops):
        unreached = sorted({g.atom for g in micro.guards
                            if g.op == op and g.atom and g.atom not in reached})
        if unreached:
            out.append((op, unreached[0]))
    return out


def _overrides(micro: MicroStateModel, atom: str) -> dict[str, str]:
    """绑定策略:EQ 字面值绑字面值,其余中性(见模块 docstring §2)。"""
    var, _, value_part = atom.partition("=")
    key = var.partition(".")[2]
    guard = next((g for g in micro.guards if g.atom == atom), None)
    bind = value_part if guard is not None and guard.pre_op == "eq" else NEUTRAL
    out: dict[str, str] = {}
    for op in sorted(micro.ops):
        feed = micro.ops[op].writes.get(key)
        if feed:
            out.setdefault(feed, bind)
    return out


def _plan_requests(micro: MicroStateModel, op: str) -> list[dict]:
    """plan_backward 反向链 + 末步受门 op → 单请求 dict 序列(C10)。"""
    action = f"POST {op}"
    req_atoms = sorted({v for _, v, d in micro.sstg.out_edges(action, data=True)
                        if d.get("kind") == "requires"})
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for atom in req_atoms:                       # 受门 op 的全部前置:种子自含
        goal = GoalSpec(name=f"reach_{atom}", vuln_class="state_reach",
                        kind="reach_state", target=atom,
                        param_overrides=_overrides(micro, atom))
        for st in plan_backward(micro.model, micro.sstg, goal).steps:
            if st.action not in seen:
                seen.add(st.action)
                steps.append(st)
    if action not in seen:                       # R1:末步 = 当前请求(打门那步)
        steps.append(PlanStep(action=action,
                              json={p: NEUTRAL for p in micro.ops[op].params}))
    ctx = BindingContext(salt=hashlib.sha256(
        str(micro.target.resolve()).encode("utf-8")).hexdigest()[:8])
    requests = []
    for st in steps:
        route, json_body, headers = ctx.render_step(st)
        request = {"json": {"op": route, **json_body}}
        if headers:
            request["header"] = headers
        requests.append(request)
    return requests


# --------------------------------------------------------------- 注入主入口


def _audit(audit_path: Path, record: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def inject_state_seeds(ir, target, state: dict, corpus_dir,
                       budget: int = STATE_BUDGET) -> int:
    """未达深状态原子 → 规划 → C10 序列种子写入语料;返回写入数。

    goal 派生(内部):全部 requires 原子 − 已达原子(覆盖代理);
    R2 的触发另一半(上轮无新状态节点)由 epoch 侧账本判定,不在本函数。
    """
    micro = synthesize_micro_model(target)
    if micro is None or budget <= 0:
        return 0
    reached = reached_state_atoms(ir, target, state)
    goals = _goal_units(micro, reached)

    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    spool = CorpusManager(corpus_dir.parent / "state_spool")
    audit_path = corpus_dir.parent / "epochs" / "state_injections.jsonl"

    written = 0
    for op, atom in goals[:budget]:
        try:
            requests = _plan_requests(micro, op)
        except Exception as exc:                          # noqa: BLE001
            _audit(audit_path, {"goal": atom, "op": op,
                                "status": f"PLAN_ERROR_{type(exc).__name__}"})
            continue
        data = encode_sequence_seed(requests)
        cid = spool.save(data, epoch=0, new_blocks=0, new_edges=0,
                         discovered_blocks=0, discovered_edges=0,
                         interesting_reason="state_plan", origin="STATE")
        record = {"goal": atom, "op": op,
                  "steps": [r.get("json", {}).get("op") for r in requests]}
        if cid:
            (corpus_dir / cid).write_bytes(data)
            written += 1
            record.update(status="INJECTED", seed_id=cid)
        else:
            record.update(status="DUPLICATE")
        _audit(audit_path, record)
    return written
