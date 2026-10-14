"""goal_directed_eval.py —— Phase 6:目标定向构造 三臂对照评测(微靶族)。

三臂(统一请求预算,固定随机种子):
  A single_random   单请求随机:每请求随机 op + 随机参数值(中性集,
                    不含谓词常量——预设即作弊);
  B seq_random      多请求无引导:随机 2-3 步序列(等价请求数预算),
                    即"保留多请求执行、去掉静态状态引导"消融;
  C goal_directed   目标定向:derive→plan→execute(谓词驱动参数)。

目标块经验推导(零手挑):对每个 op 以中性参数单请求执行一次,
覆盖并集之外的、含函数调用的语句块 = 状态门深块。

指标(顾问三问):State Reach(命中目标率)、Cost/Target(每命中
目标耗请求数)、Δblocks(相对单请求并集的增量覆盖)。
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.runtime.plan_executor import (_load_target_module,
                                              execute_plan)
from stateforge.runtime.tracer import TraceSession
from stateforge.runtime.mapper import map_trace
from stateforge.state.extractor import extract_state_facts
from stateforge.state.goal_to_plan import arm_params, dispatch_arms, \
    plan_for_block

REQUEST_BUDGET = 60
_SEQ_LEN = (2, 3)
_RANDOM_VALUES = ("", "v", "x", "1")        # 中性值集(零谓词泄露)
_RNG_SEED = 7


@dataclass
class ArmResult:
    arm: str
    reached: list[str] = field(default_factory=list)
    cost_per_target: dict[str, int] = field(default_factory=dict)
    requests_used: int = 0
    extra_blocks: int = 0


@dataclass
class TargetReport:
    source: str
    targets: list[str]
    arms: list[ArmResult]


def derive_state_gated_blocks(source_file: str, ir: ProgramIR,
                              arms) -> list[str]:
    """单请求全 op 扫描(中性参数)→ 覆盖并集;并集外含调用的语句块。"""
    mod = _load_target_module(source_file)
    reset = getattr(mod, "reset_state", None)
    import json

    params_by_arm = {name: arm_params(source_file, s, e)
                     for name, s, e in arms}
    union: set[str] = set()
    root = str(Path(source_file).resolve().parent)
    with TraceSession(root=root) as session:
        for name, _s, _e in arms:
            callable(reset) and reset()         # 每 op 冷启动:防 op 间
            payload = json.dumps(               # 状态泄漏误判目标集
                {"json": {"op": name,
                          **params_by_arm.get(name, {})}}).encode()
            with session.request() as rid:
                try:
                    mod.target(payload)
                except Exception:                  # noqa: BLE001
                    pass
            evs = [e for e in session.events if e.request_id == rid]
            union |= set(map_trace(evs, ir).blocks)
    callable(reset) and reset()
    src_name = Path(source_file).name
    out = []
    for b in ir.blocks.values():
        if (src_name in b.file and b.function == "_handle"
                and b.kind == "block" and b.id not in union
                and any("(" in s and not s.startswith("HITS")
                        for s in b.statements)):
            out.append(b.id)
    return out


def _run_requests(mod, requests, ir, targets) -> tuple[set[str], set[str]]:
    """执行请求序列(单步形),返回 (累计覆盖块, 首次命中的目标集)。"""
    import json

    root = str(Path(mod.__file__).resolve().parent)
    covered: set[str] = set()
    with TraceSession(root=root) as session:
        for payload in requests:
            with session.request() as rid:
                try:
                    mod.target(payload)
                except Exception:                  # noqa: BLE001
                    pass
            evs = [e for e in session.events if e.request_id == rid]
            covered |= set(map_trace(evs, ir).blocks)
    return covered, covered & set(targets)


def arm_single_random(source_file, ir, arms, targets,
                      budget=REQUEST_BUDGET) -> ArmResult:
    mod = _load_target_module(source_file)
    reset = getattr(mod, "reset_state", None)
    callable(reset) and reset()
    rng = random.Random(_RNG_SEED)
    names = [a[0] for a in arms]
    params_by_arm = {name: arm_params(source_file, s, e)
                     for name, s, e in arms}
    import json

    result = ArmResult(arm="single_random")
    covered_union: set[str] = set()
    for i in range(budget):
        name = rng.choice(names)
        body = {"op": name, **{k: rng.choice(_RANDOM_VALUES)
                               for k in params_by_arm.get(name, {})}}
        callable(reset) and reset()             # 单请求 = 冷启动语义
        covered, hit = _run_requests(
            mod, [json.dumps({"json": body}).encode()], ir, targets)
        covered_union |= covered
        result.requests_used += 1
        for t in hit - set(result.reached):
            result.reached.append(t)
            result.cost_per_target[t] = result.requests_used
    result.extra_blocks = len(covered_union)
    return result


def arm_seq_random(source_file, ir, arms, targets,
                   budget=REQUEST_BUDGET) -> ArmResult:
    mod = _load_target_module(source_file)
    reset = getattr(mod, "reset_state", None)
    callable(reset) and reset()
    rng = random.Random(_RNG_SEED)
    names = [a[0] for a in arms]
    params_by_arm = {name: arm_params(source_file, s, e)
                     for name, s, e in arms}
    import json

    result = ArmResult(arm="seq_random")
    covered_union: set[str] = set()
    while result.requests_used < budget:
        n = rng.choice(_SEQ_LEN)
        body_seq = []
        for _ in range(n):
            name = rng.choice(names)
            body_seq.append({"json": {
                "op": name,
                **{k: rng.choice(_RANDOM_VALUES)
                   for k in params_by_arm.get(name, {})}}})
        data = json.dumps({"steps": body_seq}).encode()
        callable(reset) and reset()             # 每序列冷启动(序列内保持)
        covered, hit = _run_requests(mod, [data], ir, targets)
        covered_union |= covered
        result.requests_used += n
        for t in hit - set(result.reached):
            result.reached.append(t)
            result.cost_per_target[t] = result.requests_used
    result.extra_blocks = len(covered_union)
    return result


def arm_goal_directed(source_file, ir, facts, arms,
                      targets) -> ArmResult:
    result = ArmResult(arm="goal_directed")
    for t in targets:
        planned = plan_for_block(ir, facts, t)
        if planned is None:
            continue
        _goal, segment = planned
        mod = _load_target_module(source_file)
        reset = getattr(mod, "reset_state", None)
        callable(reset) and reset()
        res = execute_plan(segment, mod, ir, facts, t, source_file, arms)
        result.requests_used += len(res.steps)
        if res.target_covered:
            result.reached.append(t)
            result.cost_per_target[t] = len(res.steps)
    return result


def evaluate_target(source_file: str, ir: ProgramIR,
                    facts) -> TargetReport:
    arms = dispatch_arms(source_file)
    targets = derive_state_gated_blocks(source_file, ir, arms)
    return TargetReport(
        source=Path(source_file).name,
        targets=targets,
        arms=[
            arm_single_random(source_file, ir, arms, targets),
            arm_seq_random(source_file, ir, arms, targets),
            arm_goal_directed(source_file, ir, facts, arms, targets),
        ])


def run_eval(suite_dir: str | Path,
             out_dir: str | Path = "results/goal_eval") -> dict:
    """stateful 套件三靶评测;产出 report.json + report.md。"""
    from stateforge.analysis.static.icfg import build_icfg

    suite = Path(suite_dir)
    ir = build_icfg(suite)
    facts = extract_state_facts(suite, ir=ir)
    reports = []
    for name in sorted(p.stem for p in suite.glob("t_state_*.py")):
        reports.append(evaluate_target(str(suite / f"{name}.py"), ir, facts))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "suite": str(suite), "budget": REQUEST_BUDGET,
               "reports": [asdict(r) for r in reports]}
    (out / "report.json").write_text(
        __import__("json").dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = ["# goal_directed_eval 三臂对照(Phase 6)", "",
             f"预算={REQUEST_BUDGET} 请求/靶,随机种子={_RNG_SEED}", ""]
    for r in reports:
        lines.append(f"## {r.source}(目标 {len(r.targets)} 个)")
        lines.append("| 臂 | reach | cost/target | requests |")
        lines.append("|---|---|---|---|")
        for a in r.arms:
            costs = (";".join(f"{k.split('::')[2]}:{v}"
                              for k, v in a.cost_per_target.items())
                     or "—")
            lines.append(f"| {a.arm} | {len(a.reached)}/{len(r.targets)}"
                         f" | {costs} | {a.requests_used} |")
        goal_arm = next((a for a in r.arms if a.arm == "goal_directed"),
                        None)
        if goal_arm is not None:
            missing = [t for t in r.targets if t not in goal_arm.reached]
            if missing:
                lines.append("")
                lines.append(
                    f"goal 未达 {len(missing)} 个:"
                    + ";".join(m.split('::')[2] for m in missing)
                    + " —— 否定分支/双演员门(owner≠uid 类),"
                      "超出 MVP 谓词主张面(条件反转见顾问 Phase-2 边界)")
        lines.append("")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("suite", nargs="?",
                        default="stateforge/targets/stateful")
    parser.add_argument("--out", default="results/goal_eval")
    args = parser.parse_args()
    report = run_eval(args.suite, args.out)
    for r in report["reports"]:
        print(f"{r['source']}: targets={len(r['targets'])}")
        for a in r["arms"]:
            print(f"  {a['arm']:14} reach={len(a['reached'])}"
                  f"/{len(r['targets'])} requests={a['requests_used']}")
