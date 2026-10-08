"""vulnshop_state_bridge.py —— P4-T3 AppModel 真路径桥(R7 裁决)。

`state_seed_injector` 的第二条路径:靶目录能被分析器提出 **AppModel**
(Flask 端点族)时,直接用真 SSTG(`analysis/builder.build_sstg`,零改动)
+ `state/planning/backward.plan_backward`(零改动)推出请求链,渲染为
C10 序列种子;M1/M2 微靶合成路径保持原样,分派只看 AppModel 可用性
(无靶名特判)。模块名沿用任务简报的 vulnshop 例名——桥本身对任何
"extract_flask 能提出端点"的 Flask 靶通用。

设计裁决(与任务报告互为正文):

1. **模型构建零 LLM**:`analyze()` 的语义标注/relate 通道(`solving.llm`)
   不进本桥——E 臂契约是零 LLM,且 llm:cache 命中与否会让 SSTG 跨机器
   漂移。桥自建:extract_flask(L1,纯静态)→ `_complete_auth_chain`
   (确定性结构推导,见 §2)→ `derive_invariants`(L1,确定性)→
   build_sstg。state_vars/标注不参与注入决策(security_relevant 只影响
   SSTG 节点属性),故省略标注不改变任何注入行为。

2. **认证链完备化(桥级,LLM-free)**:extract_flask 看不见
   `require_user()` 形态的认证(LSF 的正式通道是 LLM relate 的
   auth_chains)。桥按 lifting.apply_lift_result ② 的同形结构规则补:
   触追踪实体(出现于某 precondition/effect 的实体)且非 `/auth/*`、
   非索引的端点 → 需 `Session.authed_as`;`/auth/login` 端点 → 产
   `Session.authed_as`。诚实边界:仅 observes 的端点(如 `/auth/me`)
   不触追踪实体,不加认证前置——它们也不产生/需求任何目标原子,不进链。

3. **判达语义 = 写点覆盖代理(生产侧)**:微路径以"守卫真分支已覆盖"
   判消费侧;AppModel 原子的守卫锚点在 vulnshop 落在 `if guards:` 块内
   (vuln 变体死代码),消费侧代理会永远报未达。镜像改为生产侧:**原子
   的写点代码已覆盖 ⇒ 该状态至少短暂成立过**。锚点两级:
   (a) 值匹配(优先):模型 Effect(var,value) 的 (file,line);及
       mutating 端点函数内"值为该字面量"的赋值(`status = "PAID"`)。
   (b) 值盲(仅当 (a) 为空):mutating 端点函数内提及该属性的写语句
       (赋值目标含属性名 / execute 字符申含 `attr=`)。参数化写
       (`UPDATE users SET role=?`)静态无值,只能值盲——对多值变量可能
       超前判达,属诚实记录的过近似。
   已知过近似:authed 原子锚点 = login 端点行(401 失败路径也覆盖)。

4. **?槽保持符号**:渲染步不代入 `?var`(BindingContext 会把未绑定槽
   变 None),保持 `"?token"` 字面——靶侧(targets/vulnshop/fuzz_target.py
   的请求 dict 面)以模块级绑定表运行期代入/extract 回填。@fresh/@items
   仍经 BindingContext(salt=靶路径哈希,同输入同种子字节)。

5. **桥级链完备化**(镜像 backward 的 _ensure_auth/_resource_completion
   哲学,不动冻结模块):链含 login 无 register → 前插 register(静态
   前置看不见"用户须先存在");路径槽 ?s 无上游 extract → 给链上第一个
   observes 该键(槽名或 id)的步补 extract(如 ?uid ← register 的 id)。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import networkx as nx

from stateforge.analysis.analyzer import derive_invariants
from stateforge.analysis.builder import build_sstg
from stateforge.analysis.flask_adapter import extract_flask
from stateforge.core.ir import ProgramIR
from stateforge.core.model import (AppModel, Effect, PlanStep,
                                   Precondition)
from stateforge.state.planning.backward import plan_backward
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.goals import GoalSpec, register_step

_AUTH_VAR = "Session.authed_as"
_PUBLIC_ROUTES = ("/auth/register", "/auth/login", "/")
_MUTATING = ("POST", "PUT")
_SQL_EXECUTE = ("execute", "executemany", "executescript")


@dataclass(frozen=True)
class AppModelPath:
    """AppModel 路径的合成产物(全部只读共享,与 MicroStateModel 同纪律)。"""

    target: Path                                  # 靶文件(fuzz_target.py)
    app_root: Path                                # 分析根(= target.parent)
    model: AppModel
    sstg: nx.MultiDiGraph
    atoms: dict[str, frozenset[tuple[str, int]]]  # requires 原子 → 判达锚点


# ----------------------------------------------------------- 模型构建(零 LLM)


def _complete_auth_chain(model: AppModel) -> None:
    """§2:触追踪实体且非公共路由 → 需认证;/auth/login → 产认证。"""
    entities = {p.var.split(".", 1)[0] for ep in model.endpoints
                for p in ep.preconditions if "." in p.var}
    entities |= {e.var.split(".", 1)[0] for ep in model.endpoints
                 for e in ep.effects if "." in e.var}
    entities.discard("Session")

    def _public(ep) -> bool:
        return ep.route in _PUBLIC_ROUTES or ep.route.startswith("/auth/")

    for ep in model.endpoints:
        if _public(ep):
            continue
        touches = any(p.var.split(".", 1)[0] in entities
                      for p in ep.preconditions) or \
            any(e.var.split(".", 1)[0] in entities for e in ep.effects)
        if touches and not any(p.var == _AUTH_VAR for p in ep.preconditions):
            ep.preconditions.append(Precondition(
                var=_AUTH_VAR, op="truthy", file=ep.file, line=ep.line))
    for ep in model.endpoints:
        if ep.route.endswith("/auth/login") and \
                not any(e.var == _AUTH_VAR for e in ep.effects):
            ep.effects.append(Effect(
                var=_AUTH_VAR, op="set", key="username",
                file=ep.file, line=ep.line))


def _abs_source(file: str, app_root: Path) -> Path | None:
    p = Path(file)
    if not p.is_absolute():
        p = app_root / p
    try:
        return p.resolve()
    except OSError:
        return None


def _functions_by_file(model: AppModel, app_root: Path
                       ) -> dict[str, dict[str, ast.FunctionDef]]:
    """{绝对文件: {函数名: FunctionDef}}——嵌套(工厂内)函数一并收录。"""
    out: dict[str, dict[str, ast.FunctionDef]] = {}
    for ep in model.endpoints:
        src = _abs_source(ep.file, app_root)
        if src is None or str(src) in out:
            continue
        try:
            tree = ast.parse(src.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            out[str(src)] = {}
            continue
        funcs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        out[str(src)] = funcs
    return out


def _target_text(node) -> str:
    """赋值目标源词集(Name id / 属性名 / 下标字面键)。"""
    words: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            words.append(n.id)
        elif isinstance(n, ast.Attribute):
            words.append(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            words.append(n.value)
    return " ".join(words)


def _write_sites(fn: ast.FunctionDef, attr: str, value: str):
    """函数内 attr 的写语句 → yield (行号, "value"|"attr")。

    value:值为原子字面量的赋值,或 execute 字符串里 `attr='value'`。
    attr :赋值目标提及 attr,或 execute 字符串里 `attr=`(值盲)。
    """
    word = re.compile(rf"\b{re.escape(attr)}\b")
    lhs = re.compile(rf"\b{re.escape(attr)}\s*=")
    rhs = re.compile(
        rf"\b{re.escape(attr)}\s*=\s*['\"]?{re.escape(value)}\b")
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and node.targets:
            if not word.search(_target_text(node.targets[0])):
                continue
            consts = {c.value for c in ast.walk(node.value)
                      if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)}
            yield node.lineno, ("value" if value in consts else "attr")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _SQL_EXECUTE:
            hit = None
            for c in ast.walk(node):
                if not (isinstance(c, ast.Constant) and isinstance(c.value, str)):
                    continue
                if rhs.search(c.value):
                    hit = "value"
                    break
                if lhs.search(c.value):
                    hit = "attr"
            if hit is not None:
                yield node.lineno, hit


def _anchors(model: AppModel, app_root: Path, sstg: nx.MultiDiGraph
             ) -> dict[str, frozenset[tuple[str, int]]]:
    """每个 requires 原子 → 判达锚点集(§3 两级:值匹配优先,空则值盲)。"""
    funcs = _functions_by_file(model, app_root)
    out: dict[str, frozenset[tuple[str, int]]] = {}
    requires = {v for _, v, d in sstg.edges(data=True) if d.get("kind") == "requires"}
    for atom in requires:
        var, _, value = atom.partition("=")
        _entity, _, attr = var.partition(".")
        matched: set[tuple[str, int]] = set()
        blind: set[tuple[str, int]] = set()
        for ep in model.endpoints:
            src = _abs_source(ep.file, app_root)
            fn = funcs.get(str(src), {}).get(ep.func) if src else None
            if ep.methods and ep.methods[0] not in _MUTATING:
                fn = None                      # 值盲/字面赋值扫描限 mutating 端点
            if fn is not None:
                for line, kind in _write_sites(fn, attr, value):
                    (matched if kind == "value" else blind).add((str(src), line))
            if not value:
                continue
            for eff in ep.effects:             # 模型 Effect 锚点(不限 mutating)
                # builder 归一:Session.authed_as 的 set 效应 → <authed> 哨兵原子
                auth_sentinel = var == _AUTH_VAR and eff.value is None
                if eff.var == var and eff.line and \
                        (eff.value == value or auth_sentinel):
                    src2 = _abs_source(eff.file, app_root)
                    if src2 is not None:
                        matched.add((str(src2), eff.line))
        out[atom] = frozenset(matched or blind)
    return out


@lru_cache(maxsize=None)
def _appmodel_path(resolved_target: str) -> AppModelPath | None:
    """memo 体(resolved 绝对路径 str 为键;同 P4-T2 synthesize memo 纪律)。"""
    target = Path(resolved_target)
    if not target.is_file():
        return None
    app_root = target.parent
    try:
        model = extract_flask(app_root)
        if not model.endpoints:
            return None              # 非 Flask 靶(微靶)→ 微路径接管
        _complete_auth_chain(model)
        model.invariants = derive_invariants(model)
        sstg = build_sstg(model)
        return AppModelPath(target=target, app_root=app_root, model=model,
                            sstg=sstg, atoms=_anchors(model, app_root, sstg))
    except Exception:                                      # noqa: BLE001
        return None                  # 分析失败 → 诚实回落微路径(零端点同款)


def appmodel_path(target) -> AppModelPath | None:
    """靶文件 → AppModel 路径产物;不可用(无端点/解析失败)→ None。

    分派语义(R7):None ⇒ 调用方走微靶 M1/M2 合成路径。测试改写靶目录
    后需重算时调 `appmodel_path.cache_clear()`。
    """
    return _appmodel_path(str(Path(target).resolve()))


# memo 控制面挂到公开名上(与 synthesize_micro_model 同款)
appmodel_path.cache_clear = _appmodel_path.cache_clear
appmodel_path.cache_info = _appmodel_path.cache_info


# --------------------------------------------------------------- 判达(§3)


def _coerce_ir(ir) -> ProgramIR | None:
    if ir is None or isinstance(ir, ProgramIR):
        return ir
    try:
        return ProgramIR.from_json(ir)
    except (OSError, ValueError):
        return None


def _covered_lines_by_file(ir: ProgramIR, state: dict, app_root: Path
                           ) -> dict[str, set[int]]:
    """覆盖代理:app_root 内已覆盖块的行集,按绝对文件分组。"""
    covered = set(state.get("covered_blocks") or state.get("blocks") or [])
    root = app_root.resolve()
    out: dict[str, set[int]] = {}
    for bid in covered:
        block = ir.blocks.get(bid)
        if block is None:
            continue
        try:
            f = Path(block.file)
            f = f if f.is_absolute() else root / f
            f = f.resolve()
        except OSError:
            continue
        if not (f == root or root in f.parents):
            continue
        out.setdefault(str(f), set()).update(
            block.line_numbers or range(block.start_line, block.end_line + 1))
    return out


def reached_atoms(ir, state: dict, amp: AppModelPath) -> set[str]:
    """已达 requires 原子(写点覆盖代理,§3;无覆盖态 → 空集)。"""
    ir_obj = _coerce_ir(ir)
    if ir_obj is None:
        return set()
    lines = _covered_lines_by_file(ir_obj, state or {}, amp.app_root)
    return {atom for atom, anchors in amp.atoms.items()
            if any(line in lines.get(file, ()) for file, line in anchors)}


# ----------------------------------------------------- 规划与渲染(§4/§5)


def _complete_plan(model: AppModel, steps: list[PlanStep]) -> list[PlanStep]:
    """§5:login 前置 register;未绑定路径槽补 extract。"""
    steps = list(steps)
    if steps and any(s.action.endswith("/auth/login") for s in steps) and \
            not any(s.action.endswith("/auth/register") for s in steps):
        steps = [register_step()] + steps
    ep_by_action = {f"{ep.methods[0]} {ep.route}": ep for ep in model.endpoints}

    def _observes(step: PlanStep) -> list[str]:
        ep = ep_by_action.get(step.action)
        return ep.observes if ep is not None else []

    bound = {slot.lstrip("?") for s in steps for slot in s.extract.values()}
    used = sorted({v[1:] for s in steps for v in s.path.values()
                   if isinstance(v, str) and v.startswith("?")} - bound)
    for slot in used:
        key = slot if any(slot in _observes(s) for s in steps) else "id"
        for s in steps:
            if key in _observes(s) and f"?{slot}" not in s.extract.values():
                s.extract[key] = f"?{slot}"
                break
    return steps


def _symbolic(ctx: BindingContext, v):
    """@fresh/@items 代入;?槽与其余值原样(靶侧运行期绑定,§4)。"""
    if isinstance(v, str) and v.startswith("@"):
        return ctx.render(v)
    return v


def _render_step(ctx: BindingContext, st: PlanStep) -> dict:
    method, _, route = st.action.partition(" ")
    path = route
    for name, v in st.path.items():
        val = str(_symbolic(ctx, v))
        path = path.replace(f"<int:{name}>", val).replace(f"<{name}>", val)
    step = {"method": method or "GET", "path": path}
    if st.json:
        step["json"] = {k: _symbolic(ctx, v) for k, v in st.json.items()}
    if st.headers:
        step["header"] = {k: _symbolic(ctx, v) for k, v in st.headers.items()}
    if st.extract:
        step["extract"] = dict(st.extract)
    return step


def render_seed(amp: AppModelPath, atom: str, salt: str) -> list[dict]:
    """未达原子 → reach_state goal → plan_backward → C10 步列表。

    无可规划链(coverage-gap)→ 空列表(调用方按 NO_PLAN 审计,不占预算)。
    """
    goal = GoalSpec(name=f"reach_{atom}", vuln_class="state_reach",
                    kind="reach_state", target=atom)
    segment = plan_backward(amp.model, amp.sstg, goal)
    steps = _complete_plan(amp.model, segment.steps)
    ctx = BindingContext(salt=salt)
    return [_render_step(ctx, st) for st in steps]
