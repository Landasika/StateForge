"""goal_checkpoint.py —— Phase 5:验证链检查点存档 + C10 种子注入 + 复用。

闭环(顾问外循环):内循环(Phase-4 plan_executor)判达后——
  ① 检查点落档:链 + 目标 + 证据(可审计、可跨 epoch 复用);
  ② C10 序列种子注入 Atheris 语料(复用 encode_sequence_seed 与
     state_spool 落盘惯例,消费端逐字节互认);
  ③ 复用重放:从检查点直接执行(不重推导),验证检查点仍判达——
     "不能仅因命中写入语句就认定状态已成功构造"的落地。

域归属:solving(L3)——语料注入是求解域职责(state_seed_injector
同族);引用 state/runtime/fuzzing 均为向下。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.fuzzing.corpus_manager import CorpusManager
from stateforge.fuzzing.entry_driver import encode_sequence_seed


@dataclass
class GoalCheckpoint:
    """一条已验证的状态构造链(测试起点)。"""

    checkpoint_id: str
    target_block: str
    source_file: str
    goal_constraints: list[dict]          # StateGoal.constraints 序列化
    steps: list[dict]                     # [{"json": {...}}, ...](C10 步)
    hits: list[str] = field(default_factory=list)
    step_evidence: list[dict] = field(default_factory=list)
    created_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.checkpoint_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "GoalCheckpoint":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**d)

    def sequence_seed(self) -> bytes:
        """C10 整体种子(与消费端解码逐字节互认)。"""
        return encode_sequence_seed(self.steps)

    def to_segment(self) -> PlanSegment:
        steps = []
        for i, s in enumerate(self.steps):
            j = dict(s.get("json", {}))
            arm = j.get("op", f"step:{i}")       # 保留臂名供写块验证
            steps.append(PlanStep(action=f"POST op={arm}", json=j))
        return PlanSegment(goal=f"checkpoint:{self.checkpoint_id}",
                           steps=steps)


def checkpoint_from_execution(target_block: str, source_file: str,
                              goal, execution) -> GoalCheckpoint | None:
    """判达的 PlanExecution → 检查点(未判达不存档)。"""
    if not execution.target_covered or not execution.steps:
        return None
    step_jsons = [dict(s.request) for s in execution.steps]
    payload = json.dumps(
        {"target_block": target_block,
         "steps": step_jsons}, sort_keys=True).encode()
    return GoalCheckpoint(
        checkpoint_id=f"gc-{hashlib.sha256(payload).hexdigest()[:12]}",
        target_block=target_block,
        source_file=str(source_file),
        goal_constraints=[p.to_dict() for p in goal.constraints],
        steps=[{"json": j} for j in step_jsons],
        hits=list(execution.hits),
        step_evidence=[
            {"action": s.action, "ok": s.ok, "write_block": s.write_block,
             "covered": s.n_covered} for s in execution.steps])


def inject_checkpoint_seed(ckpt: GoalCheckpoint,
                           corpus_dir: str | Path) -> str | None:
    """检查点 → C10 种子写语料(state_spool 惯例:spool+corpus 双写,
    内容哈希去重);返回 cid,None=重复。"""
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    spool = CorpusManager(corpus_dir.parent / "goal_spool")
    data = ckpt.sequence_seed()
    cid = spool.save(data, epoch=0, new_blocks=0, new_edges=0,
                     discovered_blocks=0, discovered_edges=0,
                     interesting_reason="goal_checkpoint",
                     origin="GOAL")
    if cid:
        (corpus_dir / cid).write_bytes(data)
    return cid


def replay_checkpoint(ckpt: GoalCheckpoint, ir, facts,
                      arms) -> tuple:
    """复用重放:检查点 steps 直接执行(零推导),返回 PlanExecution。

    检查点有效性判据 = 重放后目标块仍覆盖(状态构造的可复现证据)。
    """
    from stateforge.runtime.plan_executor import (_load_target_module,
                                                  execute_plan)

    mod = _load_target_module(ckpt.source_file)
    return execute_plan(ckpt.to_segment(), mod, ir, facts,
                        ckpt.target_block, ckpt.source_file, arms)
