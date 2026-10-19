"""plan_ledger.py —— PR-06:计划尝试账本(失败记忆/去重/修复建议)。

三条调度约束(任务书):
1. **403 ≠ 归属错误**:失败归因只来自运行时反证(evaluation 的分量),
   HTTP 状态码仅作上下文记录,永不单独作为约束失败分类依据;
2. **同键去重**:(goal_id, baseline_id, plan_signature, binding) 已
   反证/未定的尝试不盲重试——防同一失败条件无限重复同一计划;
   基线、绑定或新证据变化才允许重试;
3. **未知 ≠ 失败**:UNVERIFIABLE/观测未定保持 UNKNOWN 待观察,不触发
   重规划循环;PROXY_PROGRESS 不冒充 verified(witness 只属于
   VERIFIED_ACHIEVED);有效链保留为可重放检查点(符号链=重放规则,
   可变 ID 由 extract 重建),未执行确认的候选保留候选标记。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from stateforge.solving.goal_checkpoint import GoalCheckpoint
from stateforge.solving.relational_planner import RelationalPlanCandidate

ACHIEVED = "VERIFIED_ACHIEVED"
NOT_ACHIEVED = "VERIFIED_NOT_ACHIEVED"
UNKNOWN = "UNKNOWN"

# 归因词表(只由 evaluation 分量决定;HTTP 状态码不入词表)
REASON_AUTH_MISMATCH = "auth-subject-mismatch"
REASON_OWNERSHIP_REFUTED = "identity-ownership-refuted"
REASON_STATE_REFUTED = "state-precondition-refuted"
REASON_RESOURCE_REFUTED = "resource-absent-refuted"
REASON_RELATION_REFUTED = "relation-refuted"
REASON_UNKNOWN_PENDING = "unknown-pending-observation"


@dataclass(frozen=True)
class PlanAttempt:
    """一次计划执行尝试的完整记录(任务书字段清单)。"""

    attempt_id: str
    goal_id: str
    baseline_id: str                 # 基线标识(输入/检查点语境)
    plan_signature: str              # candidate.signature
    binding: dict                    # 执行期 fixed 绑定(?order/?user)
    achievement_status: str          # VERIFIED_ACHIEVED/NOT_ACHIEVED/UNKNOWN
    failed_predicate: str | None = None      # 首个反证关系(render@binding)
    unknown_reasons: tuple = ()
    evidence_refs: tuple = ()
    observation_point: str = ""
    http_status: object = None       # 关键步响应码(仅上下文,不作归因)
    exception: str | None = None
    reached_blocks: int | None = None        # 关键步轨迹覆盖块数
    trace_target_covered: bool | None = None
    failure_reason: str | None = None        # attribute_failure 输出
    binding_conflicts: tuple = ()
    ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unknown_reasons"] = list(self.unknown_reasons)
        d["evidence_refs"] = list(self.evidence_refs)
        d["binding_conflicts"] = list(self.binding_conflicts)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PlanAttempt":
        return cls(**{**d,
                     "unknown_reasons": tuple(d.get("unknown_reasons", ())),
                     "evidence_refs": tuple(d.get("evidence_refs", ())),
                     "binding_conflicts": tuple(
                         d.get("binding_conflicts", ()))})


class PlanLedger:
    """尝试账本:内存查询 + JSONL 持久化(尽力而为,不反噬主流程)。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.attempts: list[PlanAttempt] = []
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        self.attempts.append(
                            PlanAttempt.from_dict(json.loads(line)))
                    except (ValueError, TypeError):
                        continue                  # 坏行跳过:账本宁缺毋坏

    def record(self, attempt: PlanAttempt) -> None:
        self.attempts.append(attempt)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(attempt.to_dict(),
                                        ensure_ascii=False) + "\n")
            except OSError:
                pass

    def find(self, *, goal_id: str, baseline_id: str,
             plan_signature: str, binding: dict) -> list[PlanAttempt]:
        want = json.dumps(binding, sort_keys=True, ensure_ascii=False)
        return [a for a in self.attempts
                if a.goal_id == goal_id and a.baseline_id == baseline_id
                and a.plan_signature == plan_signature
                and json.dumps(a.binding, sort_keys=True,
                               ensure_ascii=False) == want]

    def should_retry(self, *, goal_id: str, baseline_id: str,
                     plan_signature: str, binding: dict,
                     new_evidence: bool = False) -> tuple[bool, str]:
        """同键去重裁决:(bool 决定, str 原因)。"""
        prior = self.find(goal_id=goal_id, baseline_id=baseline_id,
                          plan_signature=plan_signature, binding=binding)
        if not prior:
            return True, "no-prior-attempt"
        if any(a.achievement_status == ACHIEVED for a in prior):
            return False, "achieved:目标已达成,无需重试"
        if new_evidence:
            return True, "new-evidence:出现新证据,允许同键重试"
        if all(a.achievement_status == UNKNOWN for a in prior):
            return False, ("unknown-pending:同键观测未定,待新证据"
                           "(不可观测≠失败,不触发重规划循环)")
        return False, ("failed-dedup:同键已反证——换绑定/基线/计划"
                       "签名后再试(修复建议见 failure_reason)")

    def failed_constraints(self, goal_id: str) -> list[str]:
        return [a.failed_predicate for a in self.attempts
                if a.goal_id == goal_id and a.achievement_status ==
                NOT_ACHIEVED and a.failed_predicate]


# ------------------------------------------------------------- 归因与修复

def attribute_failure(evaluation, http_status=None) -> str:
    """失败归因——**只**由 evaluation 分量决定。

    HTTP 状态码(403/404/409…)仅作上下文入账本,永不单独作为约束失败
    分类依据:403 可能是归属错误,也可能是其他守卫;只有观测到
    order.user_id != 当前认证主体(evaluation 反证)才确认身份关系失败。
    """
    if getattr(evaluation, "auth_binding_truth", None) == "FALSE":
        return REASON_AUTH_MISMATCH
    if getattr(evaluation, "relations_truth", None) == "FALSE":
        fc = getattr(evaluation, "failed_constraint", None) or ""
        if "user_id" in fc:
            return REASON_OWNERSHIP_REFUTED
        if "status" in fc:
            return REASON_STATE_REFUTED
        if "EXISTS" in fc:
            return REASON_RESOURCE_REFUTED
        return REASON_RELATION_REFUTED
    return REASON_UNKNOWN_PENDING


_REPAIRS = {
    REASON_AUTH_MISMATCH: [
        "rebind:以实际执行身份为 ?user 重建目标绑定(该身份自有资源)",
        "re-login:恢复目标身份后重放(注意 auth-rebinding 失效标注)",
    ],
    REASON_OWNERSHIP_REFUTED: [
        "rebind:?order 换为执行身份名下的资源(extract 重建,勿写死 ID)",
        "re-login:以资源属主身份重放整链(链内保持单一 login)",
    ],
    REASON_STATE_REFUTED: [
        "producer:确认状态生产者步在链内且参数满足观测值"
        "(如 pay amount 对照订单 total)",
    ],
    REASON_RESOURCE_REFUTED: [
        "resource:以 extract 重建 ?oid(资源缺席已反证,勿复用旧 ID)",
    ],
    REASON_RELATION_REFUTED: ["relation:检查该关系的生产者与绑定"],
    REASON_UNKNOWN_PENDING: [],           # 未知保持待观察,不给伪建议
}


def suggest_repair(reason: str) -> list[str]:
    """已确认失败 → 替代动作/绑定建议(确定性词表;未知→空)。"""
    return list(_REPAIRS.get(reason, ()))


# ------------------------------------------------------------- 装配辅助

def attempt_from_run(candidate: RelationalPlanCandidate, record,
                     evaluation, *, baseline_id: str,
                     binding: dict | None = None,
                     http_status=None, reached_blocks: int | None = None,
                     trace_target_covered: bool | None = None,
                     attempt_seq: int = 0) -> PlanAttempt:
    """执行记录 + 判定结果 → 账本条目(关键字段自 candidate/evaluation)。"""
    key_step = record.steps[-1] if record.steps else None
    witness = getattr(evaluation, "witness", None)
    binding = dict(binding or (dict(witness.binding)
                                if witness is not None else {}))
    return PlanAttempt(
        attempt_id=f"{candidate.signature[:8]}-{baseline_id}-{attempt_seq}",
        goal_id=candidate.goal_id,
        baseline_id=baseline_id,
        plan_signature=candidate.signature,
        binding={k: str(v) for k, v in binding.items()},
        achievement_status=evaluation.achievement_status,
        failed_predicate=evaluation.failed_constraint,
        unknown_reasons=tuple(evaluation.unknown_reasons),
        evidence_refs=tuple(evaluation.evidence_refs),
        observation_point=getattr(evaluation, "observation_point", ""),
        http_status=http_status if http_status is not None
        else (key_step.status if key_step else None),
        exception=(key_step.error if key_step else None),
        reached_blocks=reached_blocks,
        trace_target_covered=trace_target_covered,
        failure_reason=attribute_failure(
            evaluation, key_step.status if key_step else None),
        binding_conflicts=tuple(candidate.binding_conflicts),
        ts=time.time())


def checkpoint_from_verified(verified, source_file: str) -> GoalCheckpoint:
    """有效链 → 可重放检查点(仅 VERIFIED_ACHIEVED;候选不自动升级)。"""
    if verified.achievement_status != ACHIEVED:
        raise ValueError(
            f"仅 VERIFIED_ACHIEVED 可建检查点,当前 "
            f"{verified.achievement_status}(候选/失败链保留原标记)")
    cand = verified.candidate
    return GoalCheckpoint(
        checkpoint_id=f"vp-{cand.signature}",
        target_block=cand.goal_id,
        source_file=str(source_file),
        goal_constraints=[],
        steps=[dict(s) for s in cand.steps],
        hits=["verified:" + str(verified.witness is not None)],
        step_evidence=[{"action": f"{s.get('method', 'GET')} "
                                  f"{s.get('path', '')}"}
                       for s in cand.steps])
