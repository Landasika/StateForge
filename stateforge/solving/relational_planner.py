"""relational_planner.py —— PR-05 / M0-C:完整关系合取 → 计划候选。

与既有规划入口的关系(不重写 planner,SSTG 仍是生产者索引):
- 消费 ``derive_relational_goal`` 的**完整关系合取**(EXISTS/归属/状态),
  不用 ``_goal_atom`` 式"挑一个代表性原子"承担成功判定;
- 常量关系(EQ Field-Const)→ SSTG requires 原子(后缀匹配,零实体名
  硬编码)→ ``render_seed`` 反向链;跨实体关系(EQ Field-Field)→
  **绑定义务**(?user ≡ ?token 认证主体,运行时探测裁决),不造链;
- 基础链 = 终结动作(目标块所在端点)经 ``compat.ensure_auth`` +
  ``resource_completion`` 补认证/资源创建(既有通用机制);
- 符号同一性:?token 全链单一绑定、?oid 由 create 的 extract 产生、
  pay/ship 消费——资源 ID 不写死;
- 动作失效语义(最小版):链内出现认证**重绑定**(第二次 login)→
  binding_conflicts 标注(旧身份的创建/消费链可能失效);候选不因此
  拒绝——执行后联合验证做最终裁决(M0-B);
- guards 条件化:config 型路径条件之后的守卫关系只在对应旗标可用时
  必要(行序启发,early-return 惯用法下成立);``runtime_path_context``
  声明 guards=False 时**不把 guards=True 的前置套上**该执行。

三个概念分离:``RelationalPlanCandidate``(静态候选,未经执行)≠
``VerifiedPlan``(执行+四分量判达后)≠ 重放检查点(复用
``goal_checkpoint.GoalCheckpoint``;可变 ID 由 extract 重建)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from stateforge.solving.vulnshop_state_bridge import (
    AppModelPath,
    _render_step,
    render_seed,
)
from stateforge.state.planning.backward import _to_step, plan_backward
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.compat import ensure_auth, resource_completion
from stateforge.state.planning.goals import GoalSpec
from stateforge.state.relational import Const, Field, RelPredicate


# ------------------------------------------------------------- 候选 IR

@dataclass(frozen=True)
class BindingObligation:
    """计划内符号同一性义务(执行期由绑定机制+运行时验证裁决)。"""

    slot: str                     # "?token" / "?oid"
    producers: tuple[str, ...]    # 生产该槽的动作(login/create)
    consumers: tuple[str, ...]    # 消费该槽的动作
    note: str = ""


@dataclass(frozen=True)
class RelationalPlanCandidate:
    """静态计划候选(未执行;不得冒充已验证)。"""

    goal_id: str                          # StateGoal.target_block
    steps: tuple[dict, ...]               # C10 请求 dict(符号槽保持字面)
    obligations: tuple[BindingObligation, ...] = ()
    conditional_on: tuple[str, ...] = ()  # 生效依赖的 config 旗标("guards")
    dropped_relations: tuple[str, ...] = ()   # 条件不适用而豁免的关系(render)
    uncovered_relations: tuple[str, ...] = () # SSTG 无生产者的关系(诚实记录)
    binding_conflicts: tuple[str, ...] = ()   # 失效语义标注(重绑定等)
    notes: tuple[str, ...] = ()
    signature: str = ""                   # 内容哈希(PR-06 计划去重消费)

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "steps": [dict(s) for s in self.steps],
            "obligations": [{"slot": o.slot, "producers": list(o.producers),
                             "consumers": list(o.consumers), "note": o.note}
                            for o in self.obligations],
            "conditional_on": list(self.conditional_on),
            "dropped_relations": list(self.dropped_relations),
            "uncovered_relations": list(self.uncovered_relations),
            "binding_conflicts": list(self.binding_conflicts),
            "notes": list(self.notes),
            "signature": self.signature,
        }


@dataclass(frozen=True)
class VerifiedPlan:
    """执行并经四分量联合验证的计划(≠候选)。"""

    candidate: RelationalPlanCandidate
    achievement_status: str               # VERIFIED_ACHIEVED / …
    relations_truth: str
    auth_binding_truth: str
    trace_covered: bool
    witness: dict | None                  # GoalAchievementWitness.to_dict()
    failure_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"candidate": self.candidate.to_dict(),
                "achievement_status": self.achievement_status,
                "relations_truth": self.relations_truth,
                "auth_binding_truth": self.auth_binding_truth,
                "trace_covered": self.trace_covered,
                "witness": self.witness,
                "failure_reasons": list(self.failure_reasons)}


# ------------------------------------------------------------- 内部工具

def _step_key(step: dict) -> tuple:
    return (step.get("method", "GET"), step.get("path", ""),
            json.dumps(step.get("json") or {}, sort_keys=True,
                       ensure_ascii=False))


def _action_label(step: dict) -> str:
    return f"{step.get('method', 'GET')} {step.get('path', '')}"


def _consumes_token(step: dict) -> bool:
    auth = (step.get("header") or {}).get("Authorization", "")
    return "?token" in str(auth)


def _produces_token(step: dict) -> bool:
    return "?token" in (step.get("extract") or {}).values()


def _final_action(amp: AppModelPath, goal) -> str | None:
    """目标块所在端点 → 动作名(函数名匹配;零路由硬编码)。"""
    for ep in amp.model.endpoints:
        if ep.func == getattr(goal, "function", ""):
            return f"{ep.methods[0] if ep.methods else 'GET'} {ep.route}"
    return None


def _active_relations(goal, runtime_path_context
                      ) -> tuple[tuple, tuple[str], tuple[str]]:
    """(生效常量关系全集, 豁免关系 render, 生效依赖旗标)。

    guards 条件化启发(early-return 惯用法,文档化):config 型路径条件
    行**之后**的守卫关系视为该旗标条件化;``runtime_path_context``
    声明旗标为 False 时豁免这些关系(guards=0 的执行不套 guards=1 前置)。
    """
    rels = tuple(getattr(goal, "relations", ()))
    pcs = tuple(getattr(goal, "path_conditions", ()))
    config_vars = {pc.var: pc for pc in pcs if pc.kind == "config"}
    if not runtime_path_context:
        return rels, (), tuple(sorted(config_vars))

    dropped: list[str] = []
    keep: list = []
    for rel in rels:
        conditional_vars = [
            v for v, pc in config_vars.items()
            if pc.source_line and rel.source_line
            and pc.source_line < rel.source_line
            and runtime_path_context.get(v) is False]
        if conditional_vars:
            dropped.append(f"{rel.render()}(条件 {conditional_vars} 为 False,"
                           f"不适用本执行)")
        else:
            keep.append(rel)
    active_flags = tuple(sorted(
        v for v in config_vars
        if runtime_path_context.get(v) is not False))
    return tuple(keep), tuple(dropped), active_flags


def detect_auth_rebinding(steps: Sequence[dict]) -> list[str]:
    """失效语义(最小版):链内第二次 login = 认证重绑定。

    身份切换后,先前身份创建的资源(?oid 归属)与后续消费步骤的
    ?token 义务可能失效。返回标注(不拒绝候选)——执行后联合验证
    做最终裁决(M0-B 四分量;任务书 M0-C 验收案例 2)。
    """
    logins = [i for i, s in enumerate(steps)
              if str(s.get("path", "")).endswith("/auth/login")]
    if len(logins) > 1:
        return [f"auth-rebinding:链内 {len(logins)} 次 login"
                f"(位置 {logins})——身份切换后,先前身份创建的资源与"
                f"后续消费步骤的 ?token 义务可能失效(执行验证裁决)"]
    return []


# ------------------------------------------------------------- 主入口

def plan_for_relational_goal(goal, amp: AppModelPath,
                             *, runtime_path_context: Mapping[str, bool] | None = None,
                             salt: str = "sf") -> RelationalPlanCandidate | None:
    """完整关系目标 → 计划候选(未执行;返回 None = 无法定位终结动作)。"""
    final_action = _final_action(amp, goal)
    if final_action is None:
        return None

    rels, dropped, flags = _active_relations(goal, runtime_path_context)

    # 1) 常量关系 → SSTG 原子(后缀匹配)→ 反向链;跨实体关系 → 义务
    requires_atoms = sorted({v for _, v, d in amp.sstg.edges(data=True)
                             if d.get("kind") == "requires"})
    chains: list[list[dict]] = []
    obligations_notes: list[str] = []
    uncovered: list[str] = []
    for rel in rels:
        if not (isinstance(rel.right, Const)):
            if isinstance(rel.right, Field):        # 跨实体:绑定义务,不造链
                obligations_notes.append(
                    f"{rel.render()}:{rel.left.var} ≡ {rel.right.var} 的"
                    f"同一性经绑定义务保证(?token 单一主体),"
                    f"运行时探测+联合验证裁决")
            continue
        suffix = f".{rel.left.name}={rel.right.value}"
        atom = next((a for a in requires_atoms if a.endswith(suffix)), None)
        if atom is None:
            uncovered.append(f"{rel.render()}(SSTG 无生产者)")
            continue
        chains.append(render_seed(amp, atom, salt))

    # 2) 基础链:终结动作(经 _to_step 物化 path ?槽/auth 头)→ 通用完备化
    #    (ensure_auth 补 register/login;resource_completion 补资源创建)
    ep_index = {(ep.methods[0], ep.route): ep for ep in amp.model.endpoints}
    spec = GoalSpec(name="relational_final", vuln_class="state",
                    kind="reach_state", target=None)
    final_ps = _to_step(ep_index, final_action, spec)
    plan_steps = ensure_auth([final_ps])
    plan_steps = resource_completion(amp.model, ep_index, spec,
                                     plan_steps, _to_step)
    ctx = BindingContext(salt=salt)
    base = [_render_step(ctx, ps) for ps in plan_steps]

    # 3) 合并(原子链先行——内部序已由 plan_backward 拓扑保证;终结动作钉尾)
    merged: list[dict] = []
    seen: set[tuple] = set()
    for chain in chains:
        for st in chain:
            k = _step_key(st)
            if k not in seen:
                seen.add(k)
                merged.append(st)
    for st in base:
        k = _step_key(st)
        if k not in seen:
            seen.add(k)
            merged.append(st)              # 完备化新增步(register/create)补位
    final_key = _step_key(base[-1])
    final_steps = [s for s in merged if _step_key(s) == final_key]
    merged = [s for s in merged if _step_key(s) != final_key]
    merged.extend(final_steps)             # 终结动作最后

    # 4) 义务抽取(符号同一性)
    labels = [_action_label(s) for s in merged]
    obligations: list[BindingObligation] = []
    token_consumers = tuple(l for l, s in zip(labels, merged)
                            if _consumes_token(s))
    if token_consumers:
        obligations.append(BindingObligation(
            slot="?token",
            producers=tuple(l for l, s in zip(labels, merged)
                            if _produces_token(s)),
            consumers=token_consumers,
            note="认证主体单一性:create/pay/ship 共用同一 token"
                 "(身份切换=义务破坏,验证期裁决)"))
    oid_consumers = tuple(l for l, s in zip(labels, merged)
                          if "?oid" in str(s.get("path", "")))
    if oid_consumers:
        obligations.append(BindingObligation(
            slot="?oid",
            producers=tuple(l for l, s in zip(labels, merged)
                            if "id" in (s.get("extract") or {})),
            consumers=oid_consumers,
            note="资源同一性:?oid 由 create 的 extract 重建,不写死 ID"))
    obligations.extend(
        BindingObligation(slot="relation", producers=(), consumers=(),
                          note=n) for n in obligations_notes)

    # 5) 失效语义标注(公共检测函数,注入场景复用)
    conflicts = detect_auth_rebinding(merged)

    signature = hashlib.sha256(json.dumps(
        {"goal": getattr(goal, "target_block", ""),
         "steps": [_step_key(s) for s in merged]},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]

    return RelationalPlanCandidate(
        goal_id=getattr(goal, "target_block", ""),
        steps=tuple(merged),
        obligations=tuple(obligations),
        conditional_on=flags,
        dropped_relations=dropped,
        uncovered_relations=tuple(uncovered),
        binding_conflicts=tuple(conflicts),
        notes=(f"final-action: {final_action}",),
        signature=signature)
