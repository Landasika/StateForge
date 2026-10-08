"""state_ablation.py —— P4-T4 消融臂:E(基线)/ E-rand(随机序列)/
E-always(恒注入)(R9 裁决:独立脚本,不塞 four_arm——后者已五臂,
再叠加可读性崩)。

回答两个消融问题(逐 run 配对,同靶/同 seed/同 budget/同轮数/同触发语义):

- **E − E-rand = 规划器贡献**:同 R2 触发、同 budget、同 SSTG 动作集下,
  `plan_backward` 反向链 vs 均匀随机合法前缀序列种子——差值隔离"规划器
  推理"本身,而非"有序列种子"这件事;
- **E − E-always = R2 触发价值**:停滞触发(R2-(b):上轮有新状态节点则
  让路)vs 有未达原子即逐边界注入——差值即触发纪律的净效果(可为负:
  budget 被同链 DUPLICATE 烧掉;可为正:盲字节先达时不省一次注入)。

R8 裁决(采样设计,docstring 即契约):

- **同动作集**:采样空间 = 注入器同款 SSTG 的 action 节点(AppModel
  路径 `extract_flask→build_sstg`,微靶 M1/M2 `synthesize_micro_model`
  ——与 `inject_state_seeds` 同一分派、同一图),非随机字节、非异词表;
- **合法前缀序列**:序列每一步的 requires 原子,其生产者(produces 边)
  必须出现在此步之前的前缀里;全图无生产者的原子(coverage-gap,规划器
  NO_PLAN 同款)豁免——该请求本身仍合法可发,只是静态推不出前置;
- **均匀性**:长度 L ~ U{1..max_len}(缺省 RAND_MAX_LEN=4,固定合理
  区间:观测到的规划器链族为 micro 2–3 / vulnshop 3–4;固定区间刻意
  **不**在注入期运行规划器来标定长度分布——那会掏空消融),给定长度下
  对合法序列拒绝采样(均匀;最多 _SAMPLE_TRIES 次仍无 → NO_LEGAL 审计,
  诚实零写入);
- **消融边界(渲染不带规划器知识)**:步渲染不做 goal 字面值绑定、不用
  `backward._JSON_FILL`、不做链完备化(`_ensure_auth`/
  `_resource_completion`/`_complete_plan` 同款)——非 op 槽位一律中性
  "x"(与注入器 NEUTRAL 同款);AppModel 路径参数保持 `?slot` 符号(靶
  侧绑定表运行期代入,无上游 extract 则字面落盘,401/404 = 诚实失败)。
  故 E−E-rand = 链选择 + 值绑定 + 链补全 三者之和(规划器管线的整体
  贡献;若需进一步分解属 P5 议题);
- **确定性**:`random.Random(str(seed))`;run_ablation 按 (run_seed,
  边界序号) 派生每边界流种子——同配置重放逐字节同种子。

接缝(P4-T4,最小且门控):`epoch._inject_state_seeds_between_epochs`
增 `trigger="r2"|"always"` 与 `injector` 钩子两个可选参,`run_epochs` 以
`state_trigger`/`state_injector` 透传;缺省时 E 基线行为逐字节不变
(签名缺省值由 test_state_ablation.py 钉死)。E 臂直接复用
`inject_state_seeds`(不 fork);三臂落盘/审计纪律同源(spool + 内容
哈希去重 + state_injections.jsonl,E-rand 记录带 "path": "random")。

用法:
    python -m stateforge.evaluation.experiment.state_ablation <target.py>
        <ir.json> --out <dir> [--runs 2 --epochs 3 --seconds 3 --seed0 7]

P4-T6b 空 IR 守卫(main):ir 的 blocks=0 → parser.error(退出码 2)点名
      IR 路径并提示套件目录 analyze 口径(同 campaign;守卫只在 CLI 入口,
      库函数 run_ablation 不受影响)。
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import statistics
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.fuzzing.corpus_manager import CorpusManager
from stateforge.fuzzing.entry_driver import encode_sequence_seed
from stateforge.solving.state_seed_injector import (NEUTRAL, STATE_BUDGET,
                                                    reached_state_atoms,
                                                    synthesize_micro_model)
from stateforge.solving.vulnshop_state_bridge import (appmodel_path,
                                                      reached_atoms)

ARMS = ("E", "E-rand", "E-always")
RAND_MAX_LEN = 4        # 随机序列长度上界(U{1..4};见模块 docstring R8)
_SAMPLE_TRIES = 64      # 合法序列拒绝采样上限(超限 NO_LEGAL,诚实零写入)


# ------------------------------------------------------- R8:动作集与合法性


def _sstg_of(target) -> tuple[object | None, object | None, object | None]:
    """注入器同款双路径分派 → (micro, amp, sstg);不可用 → (None, None, None)。"""
    amp = appmodel_path(target)
    if amp is not None:
        return None, amp, amp.sstg
    micro = synthesize_micro_model(target)
    if micro is None:
        return None, None, None
    return micro, None, micro.sstg


def action_set(target) -> list[str]:
    """E-rand 采样词表 = 注入器同款 SSTG 的 action 节点(排序,确定性)。"""
    _micro, _amp, sstg = _sstg_of(target)
    if sstg is None:
        return []
    return sorted(n for n, d in sstg.nodes(data=True)
                  if d.get("kind") == "action")


def requires_producers(target) -> tuple[dict[str, list[str]],
                                        dict[str, list[str]]]:
    """(动作 → requires 原子, 原子 → 生产者动作)——同 SSTG 权威。"""
    _micro, _amp, sstg = _sstg_of(target)
    requires: dict[str, list[str]] = {}
    producers: dict[str, list[str]] = {}
    if sstg is not None:
        for u, v, d in sstg.edges(data=True):
            kind = d.get("kind")
            if kind == "requires":
                requires.setdefault(u, []).append(v)
            elif kind == "produces":
                producers.setdefault(v, []).append(u)
    return requires, producers


def is_legal_prefix(seq: list[str], requires: dict[str, list[str]],
                    producers: dict[str, list[str]]) -> bool:
    """合法前缀:每步的 requires 原子,生产者已在此前前缀(或全图无生产者)。"""
    prefix: set[str] = set()
    for action in seq:
        for atom in requires.get(action, ()):
            ps = producers.get(atom)
            if ps and not (set(ps) & prefix):
                return False
        prefix.add(action)
    return True


def sample_legal_sequence(sstg, rng: random.Random,
                          max_len: int = RAND_MAX_LEN) -> list[str] | None:
    """给定长度 U{1..max_len} + 合法性拒绝采样(均匀;见 docstring R8)。"""
    actions = sorted(n for n, d in sstg.nodes(data=True)
                     if d.get("kind") == "action")
    if not actions:
        return None
    requires: dict[str, list[str]] = {}
    producers: dict[str, list[str]] = {}
    for u, v, d in sstg.edges(data=True):
        kind = d.get("kind")
        if kind == "requires":
            requires.setdefault(u, []).append(v)
        elif kind == "produces":
            producers.setdefault(v, []).append(u)
    for _ in range(_SAMPLE_TRIES):
        seq = [actions[rng.randrange(len(actions))]
               for _ in range(rng.randint(1, max_len))]
        if is_legal_prefix(seq, requires, producers):
            return seq
    return None


# --------------------------------------------------- 步渲染(消融边界)


def _render_random_step(action: str, micro=None, model=None) -> dict:
    """中性绑定步(无 goal 值绑定/无 _JSON_FILL/无链完备化;docstring R8)。

    微路径 = {"json": {"op": op, **{参数: "x"}}}(与 _plan_requests 的
    请求形状同源,值全中性);AppModel 路径 = {method, path, json} 路径
    槽 `?name` 符号(靶侧运行期代入)。
    """
    if micro is not None:
        op = action.partition(" ")[2]
        entry = micro.ops.get(op)
        params = entry.params if entry is not None else []
        return {"json": {"op": op, **{p: NEUTRAL for p in params}}}
    method, _, route = action.partition(" ")
    path = route
    json_body: dict[str, str] = {}
    for ep in getattr(model, "endpoints", ()) or ():
        if (ep.methods[0] if ep.methods else "GET") != method \
                or ep.route != route:
            continue
        for p in ep.params:
            if p.location == "path":
                path = path.replace(f"<int:{p.name}>", f"?{p.name}") \
                           .replace(f"<{p.name}>", f"?{p.name}")
            elif p.location == "json":
                json_body.setdefault(p.name, NEUTRAL)
        break
    req = {"method": method or "GET", "path": path}
    if json_body:
        req["json"] = json_body
    return req


# ------------------------------------------------------- E-rand 注入器


def _audit(audit_path: Path, record: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def inject_random_seeds(ir, target, state, corpus_dir,
                        budget: int = STATE_BUDGET, seed=0,
                        max_len: int = RAND_MAX_LEN) -> int:
    """E-rand 注入策略(seam `injector` 钩子;签名同 inject_state_seeds)。

    与规划器臂同语义:R2-(a) 保留(全部 requires 原子已达 → 零写入),
    budget 封顶,同落盘纪律(spool + 内容哈希去重 + 审计 JSONL,
    "path": "random" 标记采样臂;R2-(b) 由接缝统一管,不在本函数)。
    """
    if budget <= 0:
        return 0
    target = Path(target)
    micro, amp, sstg = _sstg_of(target)
    if sstg is None:
        return 0
    if amp is not None:                        # 未达原子存在才注入(R2-a)
        reached = reached_atoms(ir, state, amp)
        if not (set(amp.atoms) - reached):
            return 0
        model = amp.model
    else:
        reached = reached_state_atoms(ir, target, state)
        if not {g.atom for g in micro.guards if g.atom} - reached:
            return 0
        model = None

    rng = random.Random(str(seed))
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    spool = CorpusManager(corpus_dir.parent / "state_spool")
    audit_path = corpus_dir.parent / "epochs" / "state_injections.jsonl"

    written = 0
    for _ in range(budget):
        seq = sample_legal_sequence(sstg, rng, max_len)
        if seq is None:
            _audit(audit_path, {"path": "random", "status": "NO_LEGAL",
                                "seed": str(seed)})
            break
        steps = [_render_random_step(a, micro=micro, model=model)
                 for a in seq]
        data = encode_sequence_seed(steps)
        cid = spool.save(data, epoch=0, new_blocks=0, new_edges=0,
                         discovered_blocks=0, discovered_edges=0,
                         interesting_reason="state_rand", origin="STATE")
        record = {"path": "random", "seed": str(seed), "length": len(seq),
                  "steps": [s.get("json", {}).get("op")
                            or f"{s.get('method')} {s.get('path')}"
                            for s in steps]}
        if cid:
            (corpus_dir / cid).write_bytes(data)
            written += 1
            record.update(status="INJECTED", seed_id=cid)
        else:
            record.update(status="DUPLICATE")
        _audit(audit_path, record)
    return written


def _rand_injector_factory(run_seed: int, max_len: int = RAND_MAX_LEN):
    """每边界一次流派生 seed=f"{run_seed}:b{序}";同配置重放逐字节确定。"""
    boundary = itertools.count()

    def _inject(ir, target, state, corpus_dir, budget=STATE_BUDGET):
        return inject_random_seeds(ir, target, state, corpus_dir,
                                   budget=budget,
                                   seed=f"{run_seed}:b{next(boundary)}",
                                   max_len=max_len)

    return _inject


# ------------------------------------------------------------- 三臂报告


def run_ablation(target: str, ir: str, out: Path, runs: int = 2,
                 epochs: int = 3, seconds: float = 3.0, seed0: int = 7,
                 mode: str = "frontier", use_feedback: bool = True,
                 max_len: int = RAND_MAX_LEN) -> dict:
    """三臂消融(R9):同靶/同 seed/同 budget/同轮数,逐 run 配对。

    E        = policy="state" 基线(接缝缺省参数,行为逐字节不变);
    E-rand   = 同接缝,injector 钩子换 inject_random_seeds(R8 采样);
    E-always = 同接缝,trigger="always"(R2-(b) 旁路,R2-(a) 保留)。

    报告形状 = state_arm_report 同款(blocks/edges + 状态三指标
    atoms/transitions/deep_state_blocks,逐 run + 并集聚合)+ pairwise
    配对差(E−E-rand=规划器贡献,E−E-always=触发价值)。
    """
    from stateforge.evaluation.experiment.epoch import run_epochs
    from stateforge.evaluation.experiment.state_arm_report import (
        _merge_state, state_metrics)          # 同包协作复用,形状同源不另起炉灶

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"target": str(target), "ir": str(ir), "runs": runs,
                    "epochs": epochs, "seconds": seconds, "seed0": seed0,
                    "mode": mode, "arms": {a: {"per_run": {}} for a in ARMS},
                    "pairwise": {}}

    for k in range(runs):
        seed = seed0 + k * 100
        run = f"run{k:02d}"
        for arm in ARMS:
            kwargs: dict = {}
            if arm == "E-rand":
                kwargs["state_injector"] = _rand_injector_factory(seed,
                                                                 max_len)
            elif arm == "E-always":
                kwargs["state_trigger"] = "always"
            res = run_epochs(str(target), str(ir), out / run / arm, seconds,
                             epochs, seed, arm, mode=mode,
                             use_feedback=use_feedback, policy="state",
                             **kwargs)
            report["arms"][arm]["per_run"][run] = {
                "seed": seed,
                "blocks": res["final_blocks"],
                "edges": res["final_edges"],
                "executions": res["total_executions"],
                "injections_total": res.get("state_injections_total", 0)}

    for arm in ARMS:                           # 状态三指标(逐 run + 并集聚合)
        arm_rep = report["arms"][arm]
        state_runs = []
        for k in range(runs):
            run = f"run{k:02d}"
            st = state_metrics(ir, target, out / run / arm, ledger=True)
            state_runs.append({"run": run, **st})
            arm_rep["per_run"][run].update(
                atoms_reached=st["atoms_reached"],
                transitions_observed=st["transitions_observed"],
                deep_state_blocks=st["deep_state_blocks"])
        arm_rep["state_runs"] = state_runs
        arm_rep["state"] = _merge_state(state_runs)
        blocks = [arm_rep["per_run"][f"run{k:02d}"]["blocks"]
                  for k in range(runs)]
        vals = [b for b in blocks if b is not None]
        arm_rep["mean_blocks"] = round(statistics.mean(vals), 1) if vals \
            else None
        edges = [arm_rep["per_run"][f"run{k:02d}"]["edges"]
                 for k in range(runs)]
        vals = [e for e in edges if e is not None]
        arm_rep["mean_edges"] = round(statistics.mean(vals), 1) if vals \
            else None

    for key, other, label in (                # 逐 run 配对差(块差 + 原子差)
            ("E_minus_E-rand", "E-rand", "planner_contribution"),
            ("E_minus_E-always", "E-always", "trigger_value")):
        e_per = report["arms"]["E"]["per_run"]
        o_per = report["arms"][other]["per_run"]
        blocks_d = [e_per[f"run{k:02d}"]["blocks"] - o_per[f"run{k:02d}"]["blocks"]
                    for k in range(runs)
                    if e_per[f"run{k:02d}"]["blocks"] is not None
                    and o_per[f"run{k:02d}"]["blocks"] is not None]
        atoms_d = [e_per[f"run{k:02d}"]["atoms_reached"]
                   - o_per[f"run{k:02d}"]["atoms_reached"]
                   for k in range(runs)]
        report["pairwise"][key] = {
            "label": label,
            "blocks": round(statistics.mean(blocks_d), 1) if blocks_d else None,
            "atoms": round(statistics.mean(atoms_d), 1) if atoms_d else None,
            "blocks_per_run": {f"run{k:02d}":
                               e_per[f"run{k:02d}"]["blocks"]
                               - o_per[f"run{k:02d}"]["blocks"]
                               for k in range(runs)
                               if e_per[f"run{k:02d}"]["blocks"] is not None
                               and o_per[f"run{k:02d}"]["blocks"] is not None}}
    return report


def render_text(report: dict) -> str:
    lines: list[str] = [
        f"state ablation — {report['target']} "
        f"(runs={report['runs']} epochs={report['epochs']} "
        f"seconds={report['seconds']})"]
    lines.append("arm       mean_blocks  mean_edges  atoms  transitions  "
                 "deep_blocks  injections")
    for arm in ARMS:
        a = report["arms"][arm]
        st = a.get("state", {})
        inj = sum(a["per_run"][f"run{k:02d}"]["injections_total"]
                  for k in range(report["runs"]))
        lines.append(
            f"{arm:<9} {a['mean_blocks']!s:<11} {a['mean_edges']!s:<10} "
            f"{st.get('atoms_reached', 0)!s:<6} "
            f"{st.get('transitions_observed', 0)!s:<12} "
            f"{st.get('deep_state_blocks', 0)!s:<12} {inj}")
    pw = report["pairwise"]
    for key, desc in (("E_minus_E-rand", "planner contribution"),
                      ("E_minus_E-always", "R2 trigger value")):
        if key in pw:
            lines.append(f"{key}: blocks={pw[key]['blocks']:+.1f} "
                         f"atoms={pw[key]['atoms']:+.1f}  ({desc})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P4-T4 消融臂:E / E-rand(随机序列)/ E-always(恒注入)")
    parser.add_argument("target")
    parser.add_argument("ir")
    parser.add_argument("--out", default="results/experiment/state_ablation")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--seed0", type=int, default=7)
    parser.add_argument("--max-len", dest="max_len", type=int,
                        default=RAND_MAX_LEN,
                        help="E-rand 随机序列长度上界(U{1..max_len})")
    args = parser.parse_args(argv)
    # 空 IR 守卫(P4-T6b,同 campaign):0 块 IR 会让三臂不报错地跑空——
    # 启动前载入 IR 校验,blocks=0 → parser.error 点名 IR 路径并提示套件
    # 目录 analyze 口径。
    if not ProgramIR.from_json(args.ir).blocks:
        parser.error(
            f"IR 无块(blocks=0): {args.ir};请对靶套件目录重新 analyze——"
            "`python -m stateforge.cli analyze <suite-dir> "
            "--out results/structural`,再以其 program_ir.json 作为 ir 参数")
    report = run_ablation(args.target, args.ir, Path(args.out), runs=args.runs,
                          epochs=args.epochs, seconds=args.seconds,
                          seed0=args.seed0, max_len=args.max_len)
    text = render_text(report)
    print(text)
    out_json = Path(args.out) / "state_ablation.json"
    out_json.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    (Path(args.out) / "state_ablation.txt").write_text(text + "\n",
                                                       encoding="utf-8")
    print(f"\n[out] {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
