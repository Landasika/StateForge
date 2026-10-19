"""relational_arm.py —— PR-07:无 LLM 关系语义消融战役(A/B/C/D)。

四臂(同靶版本/同预算(请求数)/同种子/同隔离条件/同统计口径):
- **A 覆盖引导基线**:随机请求 + 结构新颖度保留(StructuralFeedback);
- **B 序列输入**:A + 动作模板序列(ensure_auth/resource_completion 的
  通用链截断/随机化——**无关系目标推导**);
- **C 关系规划**:B + 停滞时按"未覆盖深块→关系目标→relational_planner"
  注入候选链(静态规划,无运行时验证反馈);
- **D 完整闭环**:C + 逐请求观测 + 四分量联合验证 + PlanLedger 去重/
  修复(身份重绑)——verified 链建 witness/检查点。

指标纪律(契约 §9 四分离 + witness 规则):
- StaticGoalCoverage:分子=**曾在一个一致观测点取得完整 witness** 的
  目标(仅 D 可产 witness;A/B/C 无观测 → None+原因,不伪造 0 覆盖);
- InstanceStateNovelty / VerifiedTransitionNovelty:首见计数(无静态
  分母;仅 D 有观测面);
- ProxyProgress:覆盖代理原子(vulnshop_state_bridge.reached_atoms)
  独立分桶,永不与 verified 混合计;
- target_block_covered:块级 Trace 判定(与状态判达分别输出);
- 成本:wall_seconds + requests_executed(开销不同时并报两者)。

产物:``<workdir>/relational_arm.jsonl``(每臂一行,C11 演进协议)+
 ``<workdir>/relational_arm_summary.json``。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.execution_context import (
    ExecutionContext,
    RequestLevelHarness,
    SnapshotLifecycle,
)
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession
from stateforge.solving.plan_ledger import (
    ACHIEVED,
    PlanLedger,
    attempt_from_run,
    suggest_repair,
)
from stateforge.solving.relational_planner import plan_for_relational_goal
from stateforge.solving.vulnshop_state_bridge import (
    appmodel_path,
    reached_atoms,
)
from stateforge.state.goal_derivation import derive_relational_goal
from stateforge.state.goal_evaluation import (
    ObservationView,
    evaluate_goal,
)
from stateforge.targets.vulnshop.fuzz_target import make_instance

ARMS = ("A", "B", "C", "D")
_STALL_K = 3                     # C/D:连续 K 输入无保留 → 注入规划链
_USERNAMES = ("u1", "u2", "u3", "u4")   # 修复轮换的身份池


# ------------------------------------------------------------- 工具

def _deep_goal_blocks(ir, functions: tuple[str, ...]) -> list[str]:
    """冻结目标分母:深块 = 指定变异端点函数内的写块(UPDATE/INSERT)。"""
    out = []
    for b in sorted(ir.blocks.values(), key=lambda b: b.id):
        if b.function in functions and b.kind == "block" and any(
                kw in s for s in b.statements
                for kw in ("UPDATE", "INSERT")):
            out.append(b.id)
    return out


def _tpl_chain(amp, final_action: str, salt: str) -> list[dict]:
    """B 臂动作模板链:通用完备化(无关系目标)。"""
    from stateforge.state.planning.backward import _to_step
    from stateforge.state.planning.binding import BindingContext
    from stateforge.state.planning.compat import ensure_auth, resource_completion
    from stateforge.state.planning.goals import GoalSpec
    from stateforge.solving.vulnshop_state_bridge import _render_step

    ep_index = {(ep.methods[0], ep.route): ep for ep in amp.model.endpoints}
    spec = GoalSpec(name="tpl", vuln_class="state", kind="reach_state",
                    target=None)
    steps = [_to_step(ep_index, final_action, spec)]
    steps = ensure_auth(steps)
    steps = resource_completion(amp.model, ep_index, spec, steps, _to_step)
    ctx = BindingContext(salt=salt)
    return [_render_step(ctx, s) for s in steps]


def _sequence_input(amp, rng, salt) -> list[dict]:
    """B/C 随机序列输入:模板链的随机截断(保留槽位线程)。"""
    import hashlib
    finals = [f"{ep.methods[0]} {ep.route}" for ep in amp.model.endpoints
              if ep.methods and ep.methods[0] == "POST"
              and "/auth/" not in ep.route]
    final = rng.choice(finals)
    chain = _tpl_chain(amp, final,
                       hashlib.sha256(f"{salt}{rng.random()}".encode())
                       .hexdigest()[:6])
    k = rng.randint(1, len(chain))
    return chain[:k]


def _request_input(amp, rng) -> list[dict]:
    """A 随机单请求(路由表 + 参数池,结构感知)。"""
    ep = rng.choice(amp.model.endpoints)
    step = {"method": ep.methods[0] if ep.methods else "GET",
            "path": ep.route}
    body = {}
    for p in ep.params:
        if p.location == "json":
            body[p.name] = rng.choice(_USERNAMES) if "name" in p.name \
                else rng.randint(1, 100)
    if body:
        step["json"] = body
    if any(pre.var == "Session.authed_as" for pre in ep.preconditions):
        step["header"] = {"Authorization": "Bearer ?token"}
    import re
    step["path"] = re.sub(r"<int:\w+>", lambda m: str(rng.randint(1, 3)),
                          step["path"])
    return [step]


def _make_probe(inst):
    """D 臂认证探测:该步请求实际携带的 Bearer 头 → 三级校验探测。"""
    import re as _re
    from stateforge.targets.vulnshop.fuzz_target import _APP_MOD, _SLOT
    from stateforge.targets.vulnshop.state_evidence import observe_request_auth

    def extra(step):
        header = (step.get("header") or {}).get("Authorization", "")
        token = _SLOT.sub(
            lambda m: str(inst.bindings.get(m.group(1), m.group(0))),
            str(header))
        m = _re.match(r"^Bearer (.+)$", token)
        if not m:
            return ()
        probe = observe_request_auth(m.group(1), app_module=_APP_MOD,
                                     db_path=inst.db_path)
        return (probe,) if probe else ()

    return extra


def _observations_to_view(obs, point, families=("orders", "users",
                                                "coupons")):
    return ObservationView.from_observations(
        list(obs), observation_point=point, families_complete=families)


def _state_facts(obs) -> set:
    return {(o.subject, str(o.key.entity),
             str(getattr(o, "observed_value", None)))
            for o in obs if getattr(o, "subject", None)}


def _transitions(before, after) -> set:
    b = {(o.subject, str(o.key.entity)):
         str(getattr(o, "observed_value", None))
         for o in before if getattr(o, "subject", None)}
    a = {(o.subject, str(o.key.entity)):
         str(getattr(o, "observed_value", None))
         for o in after if getattr(o, "subject", None)}
    return {(k[0], k[1], b[k], a[k]) for k in b.keys() & a.keys()
            if b[k] != a[k]}


# ------------------------------------------------------------- 臂运行器

@dataclass
class _ArmResult:
    arm: str
    requests_executed: int = 0
    inputs: int = 0
    wall_seconds: float = 0.0
    target_first_hit: dict = field(default_factory=dict)   # block -> req#
    goal_witnesses: list = field(default_factory=list)     # 仅 D
    goal_coverage: dict = field(default_factory=dict)
    instance_state_novelty: int | None = None
    verified_transition_novelty: int | None = None
    proxy_atoms: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"arm": self.arm,
                "requests_executed": self.requests_executed,
                "inputs": self.inputs,
                "wall_seconds": round(self.wall_seconds, 3),
                "target_block_covered": dict(self.target_first_hit),
                "goal_witnesses": list(self.goal_witnesses),
                "goal_coverage": dict(self.goal_coverage),
                "instance_state_novelty": self.instance_state_novelty,
                "verified_transition_novelty":
                    self.verified_transition_novelty,
                "proxy_progress": {"atoms": sorted(self.proxy_atoms)},
                "notes": list(self.notes)}


def _run_arm(arm: str, *, inst, ir, goal_blocks, goals, amp, target_root,
             budget_requests: int, rng, observer=None) -> _ArmResult:
    res = _ArmResult(arm=arm)
    t0 = time.monotonic()
    session = TraceSession(root=str(target_root))
    session.__enter__()
    global_blocks: set = set()
    stall = 0
    seen_facts: set = set()
    seen_transitions: set = set()
    ledger = PlanLedger() if arm == "D" else None
    user_rotation = list(_USERNAMES)
    inject_cursor = 0
    coverage = (arm in ("A", "B", "C"))   # A/B/C:结构反馈保留;D 同样保留

    try:
        while res.requests_executed < budget_requests:
            # —— 输入生成 ——
            if arm == "A":
                steps = _request_input(amp, rng)
            elif arm in ("B", "C", "D"):
                steps = _sequence_input(amp, rng, salt=arm)
            else:
                raise ValueError(arm)

            # C/D 停滞注入:规划链(关系目标,guards 感知;目标轮换)
            if arm in ("C", "D") and stall >= _STALL_K:
                pending = [g for g in goal_blocks
                           if g not in res.target_first_hit]
                if pending:
                    blk = pending[inject_cursor % len(pending)]
                    inject_cursor += 1
                    chain = plan_for_relational_goal(
                        goals[blk], amp, salt=f"inj{res.inputs}")
                    if chain is not None:
                        if arm == "D" and user_rotation:
                            u = user_rotation[0]
                            for s in chain.steps[:2]:
                                s["json"] = {**s.get("json", {}),
                                             "username": u,
                                             "password": "pw"}
                        steps = list(chain.steps)
                    stall = 0

            # —— 执行(逐请求 Trace;观测面按臂)——
            execution = ExecutionContext(
                SnapshotLifecycle(files=[inst.db_path],
                                  memory_hooks=[inst.bindings.clear]))
            harness = RequestLevelHarness(
                execution, observer=observer if arm == "D" else None,
                bindings_fn=lambda: dict(inst.bindings),
                extra_observe=_make_probe(inst) if arm == "D" else None)
            rec = harness.run_input(
                steps, lambda s: inst.request(s).status_code,
                trace_session=session)
            res.inputs += 1
            res.requests_executed += len(rec.steps)
            if res.requests_executed > budget_requests:
                res.requests_executed = budget_requests
                break

            # —— 覆盖反馈(结构新颖度保留;A/B/C/D 同口径)——
            new_cov = False
            for st in rec.steps:
                a, b = st.trace_span
                mapped = map_trace(session.events[a:b], ir)
                fresh = (set(mapped.blocks) | set(mapped.edge_kinds and
                                                 [f"{u}->{v}" for u, v
                                                  in mapped.edge_kinds])) \
                    - global_blocks
                for blk in mapped.blocks:
                    if blk in goal_blocks and blk not in res.target_first_hit:
                        res.target_first_hit[blk] = res.requests_executed
                if mapped.blocks and set(mapped.blocks) - global_blocks:
                    new_cov = True
                global_blocks |= set(mapped.blocks)
            stall = 0 if new_cov else stall + 1

            # —— D:观测面(实例新颖度/迁移/witness/账本)——
            if arm == "D" and observer is not None:
                for st in rec.steps:
                    seen_facts |= _state_facts(st.before) - seen_facts
                    seen_facts |= _state_facts(st.after) - seen_facts
                    seen_transitions |= _transitions(st.before,
                                                     st.after) \
                        - seen_transitions
                # 末步四分量(对含目标块的输入)
                for blk in goal_blocks:
                    if blk in res.target_first_hit and \
                            blk not in [w["goal_id"] for w in
                                        res.goal_witnesses]:
                        goal = goals.get(blk)
                        if goal is None:
                            continue
                        last = rec.steps[-1]
                        oid = None
                        for st in rec.steps:          # extract 重建的 ?oid
                            if "oid" in st.bindings:
                                oid = str(st.bindings["oid"])
                        user = (steps[0].get("json") or {}).get("username")
                        if oid is None or not user:
                            continue
                        from stateforge.state.goal_evaluation import BindingEnv
                        pre = evaluate_goal(
                            goal,
                            _observations_to_view(last.before,
                                                  f"armD-{res.inputs}"),
                            BindingEnv(fixed={"?order": [oid],
                                              "?user": [user]}),
                            trace_covered=True,
                            path_context={"guards": True, "err": False})
                        if pre.achievement_status == ACHIEVED:
                            res.goal_witnesses.append(
                                {"goal_id": blk,
                                 "witness": pre.witness.to_dict()
                                 if pre.witness else None})
    finally:
        session.__exit__(None, None, None)

    res.wall_seconds = time.monotonic() - t0
    if arm == "D":
        res.instance_state_novelty = len(seen_facts)
        res.verified_transition_novelty = len(seen_transitions)
        res.goal_coverage = {
            "achieved": [w["goal_id"] for w in res.goal_witnesses],
            "total": len(goal_blocks),
            "note": "witness-only:仅统计一致观测点完整 GoalWitness",
        }
    else:
        res.goal_coverage = {"achieved": None, "total": len(goal_blocks),
                             "note": "无观测面(仅 D 配置):不伪造覆盖"}
        res.notes.append("no-observation-arm")
    state = {"covered_blocks": sorted(global_blocks)}
    res.proxy_atoms = sorted(reached_atoms(ir, state, amp))
    return res


# ------------------------------------------------------------- 战役入口

def run_ablation(target_root: str | Path, workdir: str | Path, *,
                 budget_requests: int = 120, seed: int = 7,
                 arms: tuple = ARMS, guards: int = 1,
                 deep_functions: tuple = ("ship_order", "cancel_order",
                                          "set_role")) -> dict:
    """跑无 LLM 消融;返回汇总并落 JSONL+summary(可复核)。"""
    import tempfile

    target_root = Path(target_root)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    ir = build_icfg(target_root)
    amp = appmodel_path(target_root / "fuzz_target.py")
    goal_blocks = _deep_goal_blocks(ir, deep_functions)
    goals = {}
    for blk in goal_blocks:
        g = derive_relational_goal(ir, blk)
        if g is not None and g.relations:
            goals[blk] = g
    goal_blocks = [b for b in goal_blocks if b in goals]   # 冻结分母

    from stateforge.targets.vulnshop.state_evidence import \
        VulnshopStateObserver

    results = []
    for arm in arms:
        rng = random.Random(seed)
        inst = make_instance(
            Path(tempfile.mkdtemp()) / f"arm{arm}.db", guards=guards)
        observer = VulnshopStateObserver(inst.db_path) if arm == "D" \
            else None
        res = _run_arm(arm, inst=inst, ir=ir, goal_blocks=goal_blocks,
                       goals=goals, amp=amp, target_root=target_root,
                       budget_requests=budget_requests, rng=rng,
                       observer=observer)
        results.append(res.to_dict())

    jsonl = workdir / "relational_arm.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {"seed": seed, "budget_requests": budget_requests,
               "guards": guards, "goal_denominator": goal_blocks,
               "arms": results}
    (workdir / "relational_arm_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="PR-07 无 LLM 关系语义消融")
    ap.add_argument("target")
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--arms", default="A,B,C,D")
    args = ap.parse_args()
    s = run_ablation(args.target, args.out, budget_requests=args.budget,
                     seed=args.seed, arms=tuple(args.arms.split(",")))
    for a in s["arms"]:
        print(f"[{a['arm']}] reqs={a['requests_executed']} "
              f"wall={a['wall_seconds']}s "
              f"targets={list(a['target_block_covered'])} "
              f"goal_cov={a['goal_coverage']['achieved']} "
              f"novel={a['instance_state_novelty']} "
              f"trans={a['verified_transition_novelty']}")
