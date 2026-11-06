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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from stateforge.core.model import PlanSegment, PlanStep
from stateforge.fuzzing.seed_publisher import SeedPublisher
from stateforge.core.seeds import encode_sequence_seed


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
    source_hash: str = ""                # Hash of the analyzed source tree, if available.
    verification: dict = field(default_factory=dict)
    replay_evidence: dict = field(default_factory=dict)
    # Execution transport: micro targets take {"json": ...} through target();
    # real applications take {"method","path","form"|"json"} through a client.
    transport: str = "micro"
    # Real applications are fuzzed through their in-process adapter, which is a
    # different file from the analyzed source the checkpoint fingerprints.
    fuzz_target: str = ""

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
            if self.transport == "http":
                steps.append(PlanStep(
                    action=f"{s.get('method', 'GET')} {s.get('path', '/')}",
                    json=dict(s.get("json", {})),
                    form=dict(s.get("form", {})),
                    headers=dict(s.get("header", {}))))
                continue
            j = dict(s.get("json", {}))
            arm = j.get("op", f"step:{i}")       # 保留臂名供写块验证
            steps.append(PlanStep(action=f"POST op={arm}", json=j))
        return PlanSegment(goal=f"checkpoint:{self.checkpoint_id}",
                           steps=steps)


def checkpoint_from_execution(target_block: str, source_file: str,
                              goal, execution, *, ir=None) -> GoalCheckpoint | None:
    """判达的 PlanExecution → 检查点(未判达不存档)。"""
    from stateforge.runtime.plan_executor import verify_plan_execution

    verification = verify_plan_execution(execution)
    if not verification["verified"] or goal.target_block != target_block:
        return None
    step_jsons = [dict(s.request) for s in execution.steps]
    payload = json.dumps(
        {"target_block": target_block,
         "steps": step_jsons}, sort_keys=True).encode()
    return GoalCheckpoint(
        checkpoint_id=f"gc-{hashlib.sha256(payload).hexdigest()[:12]}",
        target_block=target_block,
        source_file=str(source_file),
        source_hash=ir.metadata.get("source_hash", "") if ir is not None else "",
        created_at=time.time(),
        verification=verification,
        goal_constraints=[p.to_dict() for p in goal.constraints],
        steps=[{"json": j} for j in step_jsons],
        hits=list(execution.hits),
        step_evidence=[
            {"action": s.action, "ok": s.ok, "write_block": s.write_block,
             "covered": s.n_covered, "target_block_covered": s.target_block_covered,
             "blocking": s.blocking} for s in execution.steps])


def http_checkpoint_from_execution(target_block: str, source_file: str,
                                   goal_constraints: list[dict], execution,
                                   *, ir=None, fuzz_target: str = "") -> GoalCheckpoint | None:
    """Validated real-application execution → checkpoint (else ``None``).

    Only the request a client actually received becomes a checkpoint step, so a
    replay cannot silently re-derive credentials or encodings.
    """
    from stateforge.runtime.http_plan_executor import verify_http_execution

    verification = verify_http_execution(execution)
    if not verification["verified"]:
        return None
    steps = [dict(s.request) for s in execution.steps]
    if not steps or any(not s.get("method") or not s.get("path") for s in steps):
        return None
    payload = json.dumps({"target_block": target_block, "steps": steps},
                         sort_keys=True).encode()
    return GoalCheckpoint(
        checkpoint_id=f"gc-{hashlib.sha256(payload).hexdigest()[:12]}",
        target_block=target_block,
        source_file=str(source_file),
        source_hash=ir.metadata.get("source_hash", "") if ir is not None else "",
        created_at=time.time(),
        verification=verification,
        goal_constraints=[dict(c) for c in goal_constraints],
        steps=steps,
        transport="http",
        fuzz_target=str(fuzz_target),
        step_evidence=[
            {"action": s.action, "status": s.status, "ok": s.ok,
             "target_block_covered": s.target_covered,
             "session_authenticated": s.session_authenticated,
             "blocking": s.blocking} for s in execution.steps])


def inject_checkpoint_seed(ckpt: GoalCheckpoint,
                           corpus_dir: str | Path) -> str | None:
    """检查点 → C10 种子写语料(state_spool 惯例:spool+corpus 双写,
    内容哈希去重);返回 cid,None=重复。"""
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    publisher = SeedPublisher(corpus_dir, "goal_spool")
    data = ckpt.sequence_seed()
    cid = publisher.save(data,
                     interesting_reason="goal_checkpoint",
                     origin="GOAL")
    return cid


def replay_checkpoint(ckpt: GoalCheckpoint, ir, facts,
                      arms):
    """复用重放:检查点 steps 直接执行(零推导),返回 PlanExecution。

    检查点有效性判据 = 重放后目标块仍覆盖(状态构造的可复现证据)。
    """
    from stateforge.runtime.plan_executor import (load_target_module,
                                                  execute_plan)

    block = ir.blocks.get(ckpt.target_block)
    root = Path(ir.metadata["root"]).resolve()
    if block is None or (root / block.file).resolve() != Path(ckpt.source_file).resolve():
        raise ValueError("checkpoint target/source does not match the analyzed block")
    if ckpt.source_hash:
        from stateforge.analysis.static.icfg import _source_hash
        if (ckpt.source_hash != ir.metadata.get("source_hash")
                or ckpt.source_hash != _source_hash(root)):
            raise ValueError("checkpoint source hash mismatch; analyze and verify again")
    mod = load_target_module(ckpt.source_file)
    return execute_plan(ckpt.to_segment(), mod, ir, facts,
                        ckpt.target_block, ckpt.source_file, arms)
