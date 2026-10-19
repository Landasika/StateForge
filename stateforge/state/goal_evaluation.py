"""goal_evaluation.py —— 联合求值与完整判达分类(PR-03 + PR-03.1 热修复)。

PR-03.1 修复的四个判定漏洞(评审 2026-10-19,P0-A/B/C/D):
- **A 绑定污染**:显式请求绑定(fixed:路径参数/响应提取/认证上下文)
  不得与视图全集并集——指定 ?order=12 时不得让 order:13 顶替出
  witness。BindingEnv 显式区分 fixed / enumerable / hints。
- **B 认证未证实即 witness**:完整判达 = 分类器四分量
  (relations/path/auth/trace),DB 关系 TRUE ≠ 目标达成;请求身份
  关联只认**同观测点的 verified 认证探测事实**(RequestAuth.subject),
  不认 provenance 字符串("symbol-slot:?token" 本身不是服务器确认)。
- **C 证据过滤**:视图只收同观测点、RUNTIME_VERIFIED、值有效
  (UNKNOWN 不入、MISSING→确认缺席、exists=False→确认缺席)的事实;
  同点同字段冲突值、跨观测点混合 → 显式拒绝,不静默覆盖。
- **D 路径条件/未解析项入闸**:goal.path_conditions 与 unresolvable
  参与完整判达;guards=False 的执行不能证明 guards=True 守卫链;
  Trace 命中是完整 witness 的必要分量(状态判达与覆盖分别输出)。

结果结构(评审要求的四分量拆分):
``relations_truth``(仅关系)/ ``path_conditions_truth`` /
``auth_binding_truth`` / ``target_trace_covered`` →
``classify_achievement`` → ``achievement_status``
(VERIFIED_ACHIEVED / VERIFIED_NOT_ACHIEVED / UNKNOWN);
witness 只在 VERIFIED_ACHIEVED 产生,且只来自单一观测点完整成立。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from stateforge.state.relational import (
    Field,
    RelOp,
    evaluate_relation,
)
from stateforge.state.runtime_observer import (
    NOT_OBSERVABLE,
    RUNTIME_VERIFIED,
    UNPROBED,
    observability_of,
)

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
VERIFIED_ACHIEVED = "VERIFIED_ACHIEVED"
VERIFIED_NOT_ACHIEVED = "VERIFIED_NOT_ACHIEVED"
DEFAULT_MAX_CANDIDATES = 64
AUTH_SUBJECT = "RequestAuth.subject"     # 认证探测事实的 subject 约定


# ------------------------------------------------------------- 绑定环境

@dataclass(frozen=True)
class BindingEnv:
    """PR-03.1(P0-A):绑定候选的显式分类。

    - ``fixed``:本次请求**确定**绑定(路径参数/响应提取/认证上下文
      产出的实例)。只评估所列实例,**绝不与视图全集并集**——
      "request /orders/12/ship" 的 ?order=12 不得被 order:13 顶替。
    - ``enumerable``:显式受限的可搜索候选(存在量词语义);缺省
      (未列)→ 视图全族枚举。
    - ``hints``:提示,不参与判定,不暗中升级为 fixed。
    - ``provenance``:绑定来源,仅审计展示(**不是**认证证据,
      P0-B:服务器身份确认只认 RequestAuth.subject 探测事实)。
    值均为原始实例 id("12"/"bob"),家族前缀由变量 provenance 补。
    """

    fixed: Mapping[str, Sequence[str]] = field(default_factory=dict)
    enumerable: Mapping[str, Sequence[str]] = field(default_factory=dict)
    hints: Mapping[str, str] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)


# ------------------------------------------------------------- 观测视图

@dataclass(frozen=True)
class ObservationView:
    """单一一致观测点的 verified 实体快照(PR-03.1 P0-C 证据过滤)。

    - 只收 ``RUNTIME_VERIFIED`` 事实;``value_kind=UNKNOWN`` 不入;
      ``MISSING``/``exists=False`` → ``confirmed_absent``(确认缺席,
      EXISTS 可判 FALSE),不创建在场实体。
    - 同点同字段冲突值 / observed_at 混点 → ``ValueError``(拒绝
      构建,不静默 last-wins)。
    - ``request_auth_subject``:同点 verified 认证探测
      (``RequestAuth.subject``,observed_value=认证主体实例 id)——
      P0-B 的唯一请求身份关联来源。
    """

    entities: dict[str, dict[str, dict]] = field(default_factory=dict)
    table_roots: dict[str, str] = field(default_factory=dict)
    confirmed_absent: frozenset = frozenset()
    request_auth_subject: str | None = None
    enumeration_complete: dict[str, bool] = field(default_factory=dict)
    failed_families: frozenset = frozenset()
    observability: dict[str, str] = field(default_factory=dict)
    observation_point: str = ""

    @classmethod
    def from_observations(cls, observations: Iterable_observations,
                          *,
                          observation_point: str = "",
                          families_complete: Sequence[str] = (),
                          families_failed: Sequence[str] = (),
                          observability: Mapping[str, str] | None = None,
                          ) -> "ObservationView":
        entities: dict[str, dict[str, dict]] = {}
        roots: dict[str, str] = {}
        absent: set[str] = set()
        seen: dict[tuple, object] = {}
        auth_subject: str | None = None
        points: set[int] = set()

        for obs in observations:
            # P0-C:证据过滤——仅 RUNTIME_VERIFIED 入视图
            ev = getattr(obs, "evidence", None)
            ev_val = getattr(ev, "value", ev)
            if ev_val is not None and ev_val != RUNTIME_VERIFIED:
                continue
            points.add(int(getattr(obs, "observed_at", -1)))

            subject = getattr(obs, "subject", None)
            if not subject or "." not in subject:
                continue                      # v1 形态/无 subject:视图不收
            source = getattr(obs, "source", "")
            table = source.split(":", 1)[1] if source.startswith("db:") \
                else source or subject.split(".", 1)[0].lower()

            if subject == AUTH_SUBJECT:       # P0-B:认证探测事实
                if obs.value is True \
                        and getattr(obs, "value_kind", None) not in \
                        (None, "UNKNOWN"):
                    auth_subject = str(getattr(obs, "observed_value", None))
                continue

            root, attr = subject.split(".", 1)
            roots.setdefault(table, root)
            eid = str(obs.key.entity)
            kind = getattr(obs, "value_kind", None)

            if attr == "exists":
                if obs.value is False:
                    absent.add(f"{table}:{eid}")   # 确认缺席,不建在场实体
                elif obs.value is True:
                    entities.setdefault(table, {}).setdefault(eid, {})
                continue
            if kind == "UNKNOWN":             # 未观测值不得伪装成确定状态
                continue
            if kind == "MISSING":
                absent.add(f"{table}:{eid}")
                continue
            fkey = (table, eid, attr)
            val = getattr(obs, "observed_value", None)
            if fkey in seen and seen[fkey] != val:
                raise ValueError(
                    f"同观测点冲突值: {root}.{attr} of {table}:{eid} "
                    f"= {seen[fkey]!r} vs {val!r}(拒绝静默覆盖)")
            seen[fkey] = val
            entities.setdefault(table, {}).setdefault(eid, {})[attr] = val

        if len(points) > 1:
            raise ValueError(
                f"跨观测点混合(observed_at={sorted(points)}):"
                f"拒绝构建单一视图——调用方必须逐点拆分(P0-C)")
        return cls(entities=entities, table_roots=roots,
                   confirmed_absent=frozenset(absent),
                   request_auth_subject=auth_subject,
                   enumeration_complete={f: True for f in families_complete},
                   failed_families=frozenset(families_failed),
                   observability=dict(observability or {}),
                   observation_point=observation_point)

    # ---- 求值面 ----

    def flat_snapshot(self) -> dict[str, dict]:
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
    """完整判达见证:单一观测点上四分量全 TRUE。

    只在 ``achievement_status == VERIFIED_ACHIEVED`` 时产生;不从
    StateEvidenceLedger 历史原子拼合(P0-C/契约 witness 规则)。
    """

    goal_id: str
    observation_point: str
    binding: tuple[tuple[str, str], ...]
    relations: tuple[str, ...]
    path_conditions: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"goal_id": self.goal_id,
                "observation_point": self.observation_point,
                "binding": dict(self.binding),
                "relations": list(self.relations),
                "path_conditions": [pc.render() if hasattr(pc, "render")
                                    else str(pc)
                                    for pc in self.path_conditions]}


@dataclass(frozen=True)
class GoalEvaluation:
    """evaluate_goal 完整结果:四分量 + 分类(P0-D 拆分)。"""

    relations_truth: str                        # 仅关系合取(TRUE/FALSE/UNKNOWN)
    achievement_status: str                     # VERIFIED_ACHIEVED/…
    path_conditions_truth: str = UNKNOWN
    auth_binding_truth: str = UNKNOWN
    target_trace_covered: str = UNKNOWN         # TRUE/FALSE/UNKNOWN(未查)
    satisfying: tuple[dict, ...] = ()
    witness: GoalAchievementWitness | None = None
    failed_constraint: str | None = None
    unknown_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    observation_point: str = ""
    binding_provenance: tuple[tuple[str, str], ...] = ()

    @property
    def status(self) -> str:
        """兼容别名 = relations_truth(仅关系,非完整判达——P0-B 命名
        显式化:完整判达看 achievement_status)。"""
        return self.relations_truth

    def to_dict(self) -> dict:
        return {"relations_truth": self.relations_truth,
                "achievement_status": self.achievement_status,
                "path_conditions_truth": self.path_conditions_truth,
                "auth_binding_truth": self.auth_binding_truth,
                "target_trace_covered": self.target_trace_covered,
                "satisfying": [dict(b) for b in self.satisfying],
                "witness": self.witness.to_dict() if self.witness else None,
                "failed_constraint": self.failed_constraint,
                "unknown_reasons": list(self.unknown_reasons),
                "evidence_refs": list(self.evidence_refs),
                "observation_point": self.observation_point,
                "binding_provenance": dict(self.binding_provenance)}


# ------------------------------------------------------------- 分类器

def classify_achievement(*, relations_truth: str,
                         path_conditions_truth: str,
                         auth_binding_truth: str,
                         target_trace_covered: str,
                         goal_complete: bool) -> str:
    """四分量 → achievement_status(契约 §6.5 的判定核心子集)。

    - goal 不完整(unresolvable 非空)→ UNKNOWN(goal-incomplete):
      关系可以 TRUE,但完整判达不得宣告。
    - 任一分量 UNKNOWN → UNKNOWN(该分量记入 reasons)。
    - relations FALSE(可靠反证)→ VERIFIED_NOT_ACHIEVED。
    - auth FALSE(探测显示身份不符)→ VERIFIED_NOT_ACHIEVED。
    - path FALSE(如 guards=False)→ UNKNOWN(path-condition-mismatch:
      该执行未经目标所适用的守卫链,**不能**反证目标,也不能证明)。
    - trace FALSE(状态 TRUE 但代码未进入)→ UNKNOWN
      (trace-contradicts-state:观测不一致,交上层审计)。
    - 四分量 TRUE → VERIFIED_ACHIEVED。
    """
    if not goal_complete:
        return UNKNOWN
    if relations_truth == FALSE or auth_binding_truth == FALSE:
        return VERIFIED_NOT_ACHIEVED
    if path_conditions_truth == FALSE or target_trace_covered == FALSE:
        return UNKNOWN
    if UNKNOWN in (relations_truth, path_conditions_truth,
                   auth_binding_truth, target_trace_covered):
        return UNKNOWN
    return VERIFIED_ACHIEVED


# ------------------------------------------------------------- 主入口

def _var_families(goal) -> dict[str, str]:
    return {p.var: p.table for p in getattr(goal, "var_provenance", ())
            if getattr(p, "table", None)}


def _prefixed(fam: str, raw_ids: Sequence[str]) -> list[str]:
    out = []
    for r in raw_ids:
        out.append(r if str(r).startswith(f"{fam}:") else f"{fam}:{r}")
    return out


def evaluate_goal(goal, view: ObservationView,
                  env: BindingEnv | None = None,
                  *,
                  binding_candidates: Mapping[str, Sequence[str]] | None = None,
                  path_context: Mapping[str, bool] | None = None,
                  trace_covered: bool | None = None,
                  max_candidates: int = DEFAULT_MAX_CANDIDATES,
                  ) -> GoalEvaluation:
    """关系目标 × 单点 verified 视图 × 绑定环境 → 四分量 + 完整判达。

    P0-A:``binding_candidates``(旧参)与 ``env.fixed`` 语义一致
    (确定绑定,不与全集并集);``env.enumerable`` 才是可搜索候选。
    P0-B:认证分量只认 ``view.request_auth_subject``(同点探测事实)。
    P0-D:``path_context`` 提供 guards/err 等运行时取值;
    ``trace_covered`` 由调用方 Trace 判定后传入(None=未查)。
    """
    relations: tuple = getattr(goal, "relations", ())
    goal_id = getattr(goal, "target_block", "")
    reasons: list[str] = []
    point = view.observation_point
    fam_of = _var_families(goal)
    # 环境归一:位置传旧式 dict(=fixed 语义)或 keyword binding_candidates
    if isinstance(env, Mapping):
        merged = {**dict(env), **dict(binding_candidates or {})}
        env = BindingEnv(fixed=merged)
    elif env is None:
        env = BindingEnv(fixed=dict(binding_candidates or {}))

    # 1) 候选构造(P0-A:fixed 优先且不并集;enumerable 受限或缺省全族)
    cand_map: dict[str, list[str]] = {}
    fixed_vars: dict[str, list[str]] = {}
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
        fixed_raw = list(env.fixed.get(var, ()))
        if fixed_raw:
            fixed_vars[var] = _prefixed(fam, fixed_raw)
            cand_map[var] = fixed_vars[var]
            continue                       # P0-A:确定绑定,不并全集
        enum_raw = list(env.enumerable.get(var, ()))
        if enum_raw:
            cand_map[var] = _prefixed(fam, enum_raw)
        else:
            cand_map[var] = view.candidates(fam)

    # 2) 属性级可观测性预检(不可观测/未探测 → 关系 UNKNOWN + 原因)
    for rel in relations:
        for side in (rel.left, rel.right):
            if not (isinstance(side, Field) and side.name):
                continue
            fam = fam_of.get(side.var)
            if fam is None:
                continue
            st = view.attr_observability(fam, side.name)
            if st == NOT_OBSERVABLE:
                reasons.append(
                    f"attr-not-observable:{view.root_of(fam)}.{side.name}")
            elif st == UNPROBED:
                reasons.append(
                    f"attr-unprobed:{view.root_of(fam)}.{side.name}")

    # 3) 有限枚举(P0-A:同一赋值贯穿;上限外不判 FALSE)
    spaces = [cand_map.get(v, []) or [None] for v in sorted(cand_map)]
    total = 1
    for s in spaces:
        total *= len(s)
    capped = total > max_candidates
    flat = view.flat_snapshot()
    absent = view.confirmed_absent

    satisfying: list[dict] = []
    first_false: str | None = None
    unknown_blocked = False
    for combo in itertools.islice(itertools.product(*spaces),
                                  max_candidates):
        binding = {v: e for v, e in zip(sorted(cand_map), combo)
                   if e is not None}
        verdict = "TRUE"
        for rel in relations:
            if rel.op in (RelOp.EXISTS, RelOp.NOT_EXISTS):
                ent = binding.get(rel.left.var)
                if ent is not None and ent in absent:
                    # 确认缺席(verified exists=False / MISSING):
                    # EXISTS 反证、NOT_EXISTS 成立——无需枚举完整性
                    v = "FALSE" if rel.op is RelOp.EXISTS else "TRUE"
                else:
                    v = evaluate_relation(rel, flat, binding,
                                          view.enumeration_complete)
            else:
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

    if satisfying:
        relations_truth = TRUE
    elif first_false and not capped and not unknown_blocked:
        relations_truth = FALSE
    elif not relations:
        relations_truth = TRUE
    else:
        relations_truth = UNKNOWN
        for v, cands in cand_map.items():
            if not cands:
                reasons.append(f"no-candidates:{v}")
        if capped:
            reasons.append(f"candidate-cap:{total}>{max_candidates}"
                           "(空间未穷尽,不判 FALSE)")
    if relations_truth == FALSE and first_false:
        reasons.insert(0, f"failed:{first_false}")

    # 4) 分量:path_conditions(P0-D)
    path_truth = TRUE
    for pc in getattr(goal, "path_conditions", ()):
        cur = (path_context or {}).get(pc.var)
        if cur is None:
            path_truth = UNKNOWN
            reasons.append(f"path-condition-unchecked:{pc.var}"
                           f"(requires={pc.requires})")
        elif bool(cur) != pc.requires:
            path_truth = FALSE
            reasons.append(f"path-condition-mismatch:{pc.var}"
                           f"(requires={pc.requires},got={cur})")

    # 5) 分量:认证绑定(P0-B:只认同点探测事实)
    auth_vars = [p.var for p in getattr(goal, "var_provenance", ())
                 if getattr(p, "auth_subject", False)]
    auth_truth = TRUE
    auth_matched_binding: dict | None = None
    if auth_vars:
        if view.request_auth_subject is None:
            auth_truth = UNKNOWN
            reasons.append("auth-association-unverified"
                           "(无同观测点 RequestAuth.subject 探测事实;"
                           "DB 存在 user 行 ≠ 请求认证为它)")
        else:
            for sat in satisfying:
                if all(str(sat.get(v, "")).split(":", 1)[-1]
                       == view.request_auth_subject for v in auth_vars
                       if v in sat):
                    auth_matched_binding = sat
                    break
            if auth_matched_binding is None:
                auth_truth = FALSE if relations_truth == TRUE else UNKNOWN
                reasons.append(
                    f"auth-subject-mismatch:探测主体="
                    f"{view.request_auth_subject!r} 与满足绑定不符")

    # 6) 分量:Trace(调用方独立判定后传入;None=未查)
    trace_truth = UNKNOWN if trace_covered is None else \
        (TRUE if trace_covered else FALSE)
    if trace_covered is None:
        reasons.append("trace-unchecked(目标块进入由 Trace 独立判定)")

    goal_complete = not getattr(goal, "unresolvable", ())
    if not goal_complete:
        reasons.append("goal-incomplete(unresolvable 非空;"
                       "关系可 TRUE,完整判达不得宣告)")

    achievement = classify_achievement(
        relations_truth=relations_truth,
        path_conditions_truth=path_truth,
        auth_binding_truth=auth_truth,
        target_trace_covered=trace_truth,
        goal_complete=goal_complete)

    witness = None
    best = auth_matched_binding or (satisfying[0] if satisfying else None)
    refs: tuple = ()
    prov: tuple = tuple((v, env.provenance.get(v, ""))
                        for v in sorted(best)) if best is not None else ()
    if achievement == VERIFIED_ACHIEVED and best is not None:
        witness = GoalAchievementWitness(
            goal_id=goal_id, observation_point=point,
            binding=tuple(sorted(best.items())),
            relations=tuple(r.render() for r in relations),
            path_conditions=tuple(
                pc.render() for pc in getattr(goal, "path_conditions", ())))
        refs = tuple(f"{v}={e}@{point or 'view'}"
                     for v, e in sorted(best.items()))

    return GoalEvaluation(
        relations_truth=relations_truth,
        achievement_status=achievement,
        path_conditions_truth=path_truth,
        auth_binding_truth=auth_truth,
        target_trace_covered=trace_truth,
        satisfying=tuple(satisfying),
        witness=witness,
        failed_constraint=first_false if relations_truth == FALSE else None,
        unknown_reasons=tuple(reasons),
        evidence_refs=refs,
        observation_point=point,
        binding_provenance=prov)
