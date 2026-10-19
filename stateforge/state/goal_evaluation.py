"""goal_evaluation.py —— PR-03:完整关系目标的联合求值(单一判定入口)。

设计(任务书 PR-03 / 契约 §2.2/§6.3/§6.6.4):
- ``ObservationView``:单一一致观测点的实体快照(经 StateObservation v2
  构建);**一个视图 = 一个观测点**,结构上排除 t1/t2 历史拼接。
- ``evaluate_goal``:候选绑定**有限枚举**合取求值——同一赋值贯穿全部
  关系(?order/?user 不许各自挑最有利实例);跨族实体独立(不隐式
  join);FALSE 需全候选穷尽且各有反证;UNKNOWN 保守传播。
- ``GoalAchievementWitness``:成功记账=某观测点完整目标 TRUE 的见证
  (绑定+关系+观测点),**不**从 StateEvidenceLedger 历史原子拼合
  (PR-03 硬规则;ledger 职责不变)。
- 可观测性:属性级声明(UNPROBED ≠ NOT_OBSERVABLE;未探测 → UNKNOWN,
  **不永久 UNVERIFIABLE**);认证主体关联显式化(DB 存在 user 行 ≠
  当前请求认证为它——``auth_subject_verified`` 只有绑定来源含认证
  证据时才 True)。

复用:relational 的 IR 与 evaluate_relation(含 PR-03 MISSING 语义),
不复制事实台账/求值器。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from stateforge.state.relational import (
    Field,
    RelPredicate,
    evaluate_relation,
)
from stateforge.state.runtime_observer import (
    NOT_OBSERVABLE,
    UNPROBED,
    observability_of,
)

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
DEFAULT_MAX_CANDIDATES = 64


# ------------------------------------------------------------- 观测视图

@dataclass(frozen=True)
class ObservationView:
    """单一一致观测点的实体快照。

    - ``entities``:``{"orders": {"3": {"user_id": 7, "status": "PAID"}}}``
      —— 家族键 = 观测 source 的表名(``db:<table>``);行字段 = subject
      后缀("user_id";"exists" 存在性不入行,实体在场即存在)。
    - ``table_roots``:表名 → 主体根("orders"→"Order"),取自观测
      subject 前缀,供属性级可观测性查询拼 ``<Root>.<attr>``。
    - ``enumeration_complete``:家族全表读成功(枚举完整 → 缺席可判
      MISSING);``failed_families``:读失败(**不是**完整枚举)。
    - ``observability``:观测器的 subject_families 声明(原样携带)。
    - 一个视图只对应一个 ``observation_point``;跨点拼接被结构性排除。
    """

    entities: dict[str, dict[str, dict]] = field(default_factory=dict)
    table_roots: dict[str, str] = field(default_factory=dict)
    enumeration_complete: dict[str, bool] = field(default_factory=dict)
    failed_families: frozenset = frozenset()
    observability: dict[str, str] = field(default_factory=dict)
    observation_point: str = ""

    @classmethod
    def from_observations(cls, observations: Iterable,
                          *,
                          observation_point: str = "",
                          families_complete: Sequence[str] = (),
                          families_failed: Sequence[str] = (),
                          observability: Mapping[str, str] | None = None,
                          ) -> "ObservationView":
        """StateObservation 批次(同一观测点)→ 视图。

        ``families_complete/failed`` 由调用方(观测器协议层)声明;
        缺省两者皆空 = 枚举完整性未知(缺席一律 UNKNOWN,保守)。
        """
        entities: dict[str, dict[str, dict]] = {}
        roots: dict[str, str] = {}
        for obs in observations:
            subject = getattr(obs, "subject", None)
            source = getattr(obs, "source", "")
            if not subject or "." not in subject:
                continue                    # v1 形态/无 subject:视图不收
            root, attr = subject.split(".", 1)
            table = source.split(":", 1)[1] if source.startswith("db:") \
                else source or root.lower()
            roots.setdefault(table, root)
            row = entities.setdefault(table, {}).setdefault(
                str(obs.key.entity), {})
            if attr != "exists":            # 存在性由实体在场承载
                row[attr] = getattr(obs, "observed_value", None)
        return cls(entities=entities, table_roots=roots,
                   enumeration_complete={f: True for f in families_complete},
                   failed_families=frozenset(families_failed),
                   observability=dict(observability or {}),
                   observation_point=observation_point)

    # ---- 求值面 ----

    def flat_snapshot(self) -> dict[str, dict]:
        """``{"orders:3": {...}}`` 扁平快照(relational.evaluate_relation
        的 snapshot 约定:实体键 = "<家族>:<实例>")。"""
        return {f"{fam}:{eid}": row
                for fam, members in self.entities.items()
                for eid, row in members.items()}

    def candidates(self, family: str) -> list[str]:
        return [f"{family}:{eid}" for eid in sorted(self.entities.get(
            family, {}), key=str)]

    def root_of(self, family: str) -> str:
        return self.table_roots.get(family, family)

    def attr_observability(self, family: str, attr: str) -> str:
        return observability_of(self.observability,
                                f"{self.root_of(family)}.{attr}")


# ------------------------------------------------------------- 结果 IR

@dataclass(frozen=True)
class GoalAchievementWitness:
    """一次目标达成的历史见证:单一观测点上完整目标 TRUE。

    成功记账只来自本结构(evaluate_goal 产生),**禁止**从
    StateEvidenceLedger 分别查历史原子再求合取(PR-03 硬规则)。
    """

    goal_id: str                       # StateGoal.target_block
    observation_point: str
    binding: tuple[tuple[str, str], ...]      # (("?", "orders:3"), ...) 排序
    relations: tuple[str, ...]                # 渲染(审计可读)

    def to_dict(self) -> dict:
        return {"goal_id": self.goal_id,
                "observation_point": self.observation_point,
                "binding": dict(self.binding),
                "relations": list(self.relations)}


@dataclass(frozen=True)
class GoalEvaluation:
    """evaluate_goal 的完整结果(三值 + 证据 + 原因)。"""

    status: str                                   # TRUE / FALSE / UNKNOWN
    satisfying: tuple[dict, ...] = ()             # 满足赋值(可多个)
    witness: GoalAchievementWitness | None = None
    failed_constraint: str | None = None          # 首个反证(带绑定上下文)
    unknown_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()           # 消费实体的观测来源
    observation_point: str = ""
    binding_provenance: tuple[tuple[str, str], ...] = ()  # 首个满足赋值
    auth_subject_verified: bool = False

    def to_dict(self) -> dict:
        return {"status": self.status,
                "satisfying": [dict(b) for b in self.satisfying],
                "witness": self.witness.to_dict() if self.witness else None,
                "failed_constraint": self.failed_constraint,
                "unknown_reasons": list(self.unknown_reasons),
                "evidence_refs": list(self.evidence_refs),
                "observation_point": self.observation_point,
                "binding_provenance": dict(self.binding_provenance),
                "auth_subject_verified": self.auth_subject_verified}


# ------------------------------------------------------------- 主入口

def _var_families(goal) -> dict[str, str]:
    """实体变量 → 家族(=provenance 表名)。无 provenance 的变量不映射。"""
    return {p.var: p.table for p in getattr(goal, "var_provenance", ())
            if getattr(p, "table", None)}


def evaluate_goal(goal, view: ObservationView,
                  binding_candidates: Mapping[str, Sequence[str]] | None = None,
                  *,
                  max_candidates: int = DEFAULT_MAX_CANDIDATES,
                  binding_provenance: Mapping[str, str] | None = None,
                  ) -> GoalEvaluation:
    """关系目标 × 单点视图 × 候选绑定 → 三值判定(唯一联合判定入口)。

    语义(PR-03/契约 §6.3):
    - 候选来源:显式 ``binding_candidates``(原始实例 id,如 "12")∪
      视图家族枚举;按变量族前缀展开为 "<家族>:<实例>" 键。
    - 有限枚举:赋值空间超 ``max_candidates`` → 只确定性枚举前 N 个,
      找到满足即 TRUE,否则 **UNKNOWN**(不得轻易确定 FALSE)。
    - FALSE 门槛:空间完整穷尽 且 每个赋值都有关系判 FALSE(有反证);
      任一赋值被 UNKNOWN 阻断 → 整体 UNKNOWN。
    - 可观测性:关系字段族未声明(NOT_OBSERVABLE/UNPROBED)→ 该关系
      UNKNOWN + 原因标注(UNPROBED 不永久 UNVERIFIABLE,契约 §6.5)。
    - 独立合取:跨族实体彼此独立;本函数**不合成**目标未声明的关联
      (Order.user_id==User.id 必须显式出现在 goal.relations)。
    """
    relations: tuple = getattr(goal, "relations", ())
    goal_id = getattr(goal, "target_block", "")
    reasons: list[str] = []
    point = view.observation_point

    fam_of = _var_families(goal)

    # 1) 候选绑定构造
    cand_map: dict[str, list[str]] = {}
    for var in sorted({f.var for r in relations
                       for f in (r.left, r.right)
                       if isinstance(f, Field)}):
        fam = fam_of.get(var)
        if fam is None:
            reasons.append(f"var-family-unresolved:{var}"
                           "(provenance 无表;候选无法枚举)")
            cand_map[var] = []
            continue
        if fam in view.failed_families:
            reasons.append(f"family-observation-failed:{fam}")
        raw = set(binding_candidates.get(var, ()) 
                  if binding_candidates else set())
        raw |= set(view.entities.get(fam, {}))
        cand_map[var] = [f"{fam}:{r}" for r in sorted(raw, key=str)]

    # 2) 属性级可观测性预检(不可观测字段 → 关系直接 UNKNOWN)
    for rel in relations:
        for side in (rel.left, rel.right):
            if not (isinstance(side, Field) and side.name):
                continue
            fam = fam_of.get(side.var)
            if fam is None:
                continue
            obs_status = view.attr_observability(fam, side.name)
            if obs_status == NOT_OBSERVABLE:
                reasons.append(
                    f"attr-not-observable:{view.root_of(fam)}.{side.name}")
            elif obs_status == UNPROBED:
                reasons.append(
                    f"attr-unprobed:{view.root_of(fam)}.{side.name}")

    # 3) 有限枚举
    spaces = [cand_map.get(v, []) or [None]
              for v in sorted(cand_map)]
    total = 1
    for s in spaces:
        total *= len(s)
    capped = total > max_candidates

    flat = view.flat_snapshot()
    satisfying: list[dict] = []
    first_false: str | None = None
    unknown_blocked = False

    enumerators = [itertools.islice(itertools.product(*spaces),
                                    max_candidates)]
    for combo in enumerators[0]:
        binding = {v: e for v, e in zip(sorted(cand_map), combo)
                   if e is not None}
        verdict = "TRUE"
        for rel in relations:
            v = evaluate_relation(rel, flat, binding,
                                  view.enumeration_complete)
            if v == FALSE:
                if first_false is None:
                    first_false = (f"{rel.render()} @ "
                                   f"{ {k: val for k, val in binding.items()} }")
                verdict = FALSE
                break
            if v == UNKNOWN:
                verdict = UNKNOWN
        if verdict == TRUE:
            satisfying.append(dict(binding))
        elif verdict == UNKNOWN:
            unknown_blocked = True

    # 4) 汇总
    if satisfying:
        best = satisfying[0]
        prov = tuple((v, (binding_provenance or {}).get(v, ""))
                     for v in sorted(best))
        auth_vars = [p.var for p in getattr(goal, "var_provenance", ())
                     if getattr(p, "auth_subject", False)]
        auth_ok = bool(auth_vars) and all(
            str((binding_provenance or {}).get(v, "")).startswith(
                ("auth", "symbol-slot"))
            for v in auth_vars if v in best)
        witness = GoalAchievementWitness(
            goal_id=goal_id, observation_point=point,
            binding=tuple(sorted(best.items())),
            relations=tuple(r.render() for r in relations))
        refs = _evidence_refs(view, best)
        if auth_vars and not auth_ok:
            reasons.append("auth-association-unverified"
                           "(DB 存在 user 行 ≠ 请求认证为它;需绑定来源"
                           "含认证证据)")
        return GoalEvaluation(
            status=TRUE, satisfying=tuple(satisfying), witness=witness,
            unknown_reasons=tuple(reasons), evidence_refs=refs,
            observation_point=point, binding_provenance=prov,
            auth_subject_verified=auth_ok)

    if capped:
        reasons.append(f"candidate-cap:{total}>{max_candidates}"
                       "(空间未穷尽,不判 FALSE)")
        status = UNKNOWN
    elif unknown_blocked or any(v == [] and relations_needs_var(
            relations, v) for v in cand_map):
        status = UNKNOWN
        for v, cands in cand_map.items():
            if not cands:
                reasons.append(f"no-candidates:{v}")
    else:
        status = FALSE if first_false else (UNKNOWN if relations else TRUE)
    if status == FALSE and first_false:
        reasons.insert(0, f"failed:{first_false}")
    return GoalEvaluation(
        status=status, satisfying=(), witness=None,
        failed_constraint=first_false if status == FALSE else None,
        unknown_reasons=tuple(reasons), observation_point=point)


def relations_needs_var(relations, var: str) -> bool:
    return any(f.var == var for r in relations
               for f in (r.left, r.right) if isinstance(f, Field))


def _evidence_refs(view: ObservationView, binding: dict) -> tuple:
    """满足赋值消费的实体 → 观测来源引用(审计链)。"""
    refs = []
    for var, entity in sorted(binding.items()):
        fam = str(entity).split(":", 1)[0]
        refs.append(f"{var}={entity}@{point_ref(view, fam)}")
    return tuple(refs)


def point_ref(view: ObservationView, family: str) -> str:
    return view.observation_point or f"family:{family}"
