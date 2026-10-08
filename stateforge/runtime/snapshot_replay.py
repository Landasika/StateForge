"""snapshot_replay.py —— SF3-T5 后半:PlanSegment ↔ 快照 epoch 对齐。

一条 PlanSegment = 一个 epoch = 一个叶(SF3 术语表;二阶乘积律的工程落地)。
verify 读回步**并入同一 epoch**(链构造的状态必须还活着才能核验)——这正是
exp/second_order.py 实验结论的执行面:检出 = 使用点 oracle × 状态链,缺一不可。
"""

from __future__ import annotations

from stateforge.core.model import PlanSegment
from stateforge.runtime.replay import GoalResult, StepTrace, _coerce, _safe_json
from stateforge.state.planning.binding import BindingContext
from stateforge.state.planning.checkers import CHECKERS, verify_request
from stateforge.state.planning.goals import GoalSpec
from stateforge.runtime.snapshot.client import CheckpointHandle


class SnapshotReplay:
    def __init__(self, ckpt: CheckpointHandle, salt: str | None = None):
        self.ckpt = ckpt
        self.salt = salt

    def run_segment(self, segment: PlanSegment, goal: GoalSpec,
                    checker=None) -> GoalResult:
        ctx = BindingContext(salt=self.salt)
        steps = segment.steps
        vreq_probe = verify_request(goal.name, ctx)
        has_verify = vreq_probe is not None

        def request_fn(i, rs):
            for j in range(len(rs)):        # 响应写回绑定(extract 消费)
                if j < len(steps):
                    ctx.absorb(steps[j].extract, rs[j].get("body"))
            if i < len(steps):
                route, json_body, headers = ctx.render_step(steps[i])
                return {"method": steps[i].action.partition(" ")[0],
                        "route": route,
                        "json": {k: _coerce(v) for k, v in (json_body or {}).items()} or None,
                        "headers": headers or None}
            vreq = verify_request(goal.name, ctx)   # verify 步:用已填绑定渲染
            return vreq

        out = self.ckpt.serve(request_fn, quota=len(steps) + (1 if has_verify else 0))

        result = GoalResult(goal=segment.goal, hit=False)
        for step, resp in zip(steps, out.responses):
            result.steps.append(StepTrace(step, step.action.partition(" ")[2],
                                          resp["status"], resp.get("body")))
            ctx.absorb(step.extract, resp.get("body"))
        for resp in out.responses:          # blocked 语义:首个 >=400 处
            if resp["status"] is not None and resp["status"] >= 400:
                idx = out.responses.index(resp)
                where = steps[idx].action if idx < len(steps) else "verify"
                result.reason = f"blocked at {where} → {resp['status']}"
                return result
        if not has_verify:
            check = checker or CHECKERS.get(goal.name)
            return check(result, ctx, (None, {}))
        verify = (out.responses[-1]["status"], out.responses[-1].get("body") or {})
        check = checker or CHECKERS.get(goal.name)
        return check(result, ctx, verify)
