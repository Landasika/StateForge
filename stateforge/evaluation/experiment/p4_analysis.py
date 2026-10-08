"""p4_analysis.py —— P4-T6a:聚合 p4_formal 战役产物 → 论文口径表(只读)。

输入布局(发现式,不钉靶名;对 results/ 严格只读——本脚本从不写
p4_formal 目录内的任何路径):

    <p4_formal>/camp_<target>/      campaign.py(--groups D1,E)产物
        summary.json                per_run 行:E/D1 含 final_blocks /
                                    auc_blocks_norm;E 另可含
                                    state_atoms_reached /
                                    state_injections_total(按设计可缺,
                                    键偏斜 → 统计只覆盖携带的 run)
        runXX/E/group_summary.json  state_atoms 列表(联合口径用;
                                    缺则 state_ledger.json 末边界快照兜底)
        runXX/E/epochs/state_injections.jsonl   审计账本(null 溯源用)
    <p4_formal>/abl_<target>/       state_ablation.py 产物
        state_ablation.json         arms[*].per_run / state_runs / pairwise
        runXX/<arm>/epochs/state_injections.jsonl   审计账本(null 溯源用)

输出:单一 JSON(--out 缺省仅 stdout 不落盘;给定则写 <out> 并同步写
<out>.txt 文本表)+ 可读文本表(恒进 stdout)。

口径(与 campaign.py/_stats 及 state_arm_report 并集口径对齐):

- 配对差 = 逐 run E−D1 / E−对照臂(任一侧缺 → 跳过该 run,诚实 n);
  wins/ties/losses 按 delta 符号(全零差 = wins=0 且 losses=0 且
  ties==n);AUC delta 用 auc_blocks_norm(campaign 的 state_gain 同款);
- E 状态原子:summary 标量逐 run mean + run 目录 group_summary
  state_atoms(缺则 state_ledger.json)跨 run **并集**(count==len(ids),
  不用 max——与 state_arm_report._merge_state 同口径);
- D1 coverage-proxy:campaign summary 不携带 D1 状态字段(非 state 臂)
  → 诚实缺省附注,不造数(coverage-proxy 需 IR+靶重算,本脚本只读);
- 消融两问(state_ablation 的 pairwise 不消费,按 per_run 自行配对):
  planner_contribution = E−E-rand(blocks+atoms),trigger_value =
  E−E-always;原子配对用 per_run.atoms_reached,联合口径用 state_runs;
- 零差 null:全零配对差 → null-with-reason,溯源审计账本
  (state_injections.jsonl)状态计数:
      R2A_VACUOUS          旁路已开却 0 INJECTED(R2-(a) vacuous)
      TRIGGER_NET_ZERO     有写入仍逐 run 打平(触发纪律净零;INJECT_ALWAYS=0
                           时措辞 = "bypass never fired,无条件注入",与证据自洽)
      PLANNER_NET_ZERO     随机序列臂与 E 打平(规划器管线净零)
      STATE_NEVER_ENGAGED  E 臂 0 INJECTED(状态机制从未介入)
      STATE_GAIN_NET_ZERO  有注入仍与 D1 打平
      NO_LEDGER_EVIDENCE   零差但无账本可溯源(诚实挂起)

状态原子可测性披露(终审 I3):消融半边的 atoms 三指标仅微靶路径可测
      (state_metrics 在 synthesize_micro_model=None 的 AppModel/Flask 靶
      上早退 _EMPTY_STATE → 结构性零,非测得零)。analyze_ablation 按
      state_ablation.json 的 target 字段重判可测性(同款分派,只读);
      无微模型的靶:arms[*].atoms_* 与配对 atoms 置 null、文本表渲染
      "-"/n/a 并附注;靶路径不可寻 → 无法判定,数字保持原样(不猜)。
      campaign 半边 E 原子经 reached_state_atoms 的 AppModel 分派另径
      可测(vulnshop union=4 即该路径产物),两侧口径不同源。

用法:
    python -m stateforge.evaluation.experiment.p4_analysis <p4_formal_dir>
        [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

SCHEMA = "p4_analysis/1"
CAMP_PREFIX = "camp_"
ABL_PREFIX = "abl_"

# 三问命名(消融两问与 state_ablation.py pairwise 的 label 对齐)
PLANNER = "planner_contribution"        # E−E-rand:规划器贡献
TRIGGER = "trigger_value"               # E−E-always:R2 触发价值
STATE_GAIN = "state_gain"               # E−D1:state 注入净贡献

_STATE_KEYS = ("state_atoms_reached", "state_injections_total")


# --------------------------------------------------------------- 只读读取


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_ledger(workdir: Path) -> list[dict]:
    """臂工作目录的 state_injections.jsonl(逐行 JSON;坏行跳过)。"""
    p = Path(workdir) / "epochs" / "state_injections.jsonl"
    if not p.exists():
        return []
    recs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs


def _tally(workdirs: list[Path]) -> dict:
    """多 run 审计账本状态计数(null 溯源证据;只读)。"""
    counts: dict[str, int] = {}
    found = 0
    for wd in workdirs:
        p = Path(wd) / "epochs" / "state_injections.jsonl"
        if not p.exists():
            continue
        found += 1
        for r in _read_ledger(wd):
            s = r.get("status") or r.get("event") or "<none>"
            counts[s] = counts.get(s, 0) + 1
    return {"ledgers_found": found,
            "statuses": dict(sorted(counts.items()))}


# --------------------------------------------------------------- 统计原语


def _stats(vals) -> dict:
    """mean/stdev/n(样本 stdev;n<2 → None)——与 campaign._stats 同口径。"""
    vs = [v for v in vals if v is not None]
    if not vs:
        return {"mean": None, "stdev": None, "n": 0}
    return {"mean": round(statistics.mean(vs), 4),
            "stdev": round(statistics.stdev(vs), 4) if len(vs) > 1 else None,
            "n": len(vs)}


def _paired(xs, ys) -> dict:
    """逐 run 配对差分布(xs[i]−ys[i];任一侧 None → 跳过该 run,诚实 n)。"""
    deltas = [x - y for x, y in zip(xs, ys)
              if x is not None and y is not None]
    out = {"mean": None, "median": None, "stdev": None, "n": 0,
           "min": None, "max": None, "wins": 0, "ties": 0, "losses": 0,
           "deltas": []}
    if not deltas:
        return out
    out.update(
        mean=round(statistics.mean(deltas), 4),
        median=round(statistics.median(deltas), 4),
        stdev=round(statistics.stdev(deltas), 4) if len(deltas) > 1 else None,
        n=len(deltas),
        min=round(min(deltas), 4), max=round(max(deltas), 4),
        wins=sum(1 for d in deltas if d > 0),
        ties=sum(1 for d in deltas if d == 0),
        losses=sum(1 for d in deltas if d < 0),
        deltas=[round(d, 4) for d in deltas])
    return out


def _all_zero(p: dict) -> bool:
    """全零配对差(null 判定):n>0 且逐 run delta 全为 0。"""
    return bool(p["n"]) and p["wins"] == 0 and p["losses"] == 0 \
        and p["ties"] == p["n"]


# --------------------------------------------------------------- 发现


def discover(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """<root>/camp_* 与 <root>/abl_* → ({target: dir}, {target: dir})。

    abl_* 目录须含 state_ablation.json 才算真消融产物——IR 脚手架
    (如 abl_ir_*)落同前缀的临时目录会被幻影靶污染报告,跳过。
    """
    camps: dict[str, Path] = {}
    abls: dict[str, Path] = {}
    if not Path(root).is_dir():
        return camps, abls
    for d in sorted(Path(root).iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith(CAMP_PREFIX):
            camps[d.name[len(CAMP_PREFIX):]] = d
        elif d.name.startswith(ABL_PREFIX):
            if (d / "state_ablation.json").is_file():
                abls[d.name[len(ABL_PREFIX):]] = d
    return camps, abls


# --------------------------------------------------------------- 战役半边


def _camp_atoms_union(camp_dir: Path) -> list[str] | None:
    """各 run E 工作目录的判达原子并集:group_summary.state_atoms 优先,
    state_ledger.json(末边界快照)兜底——并集口径同
    state_arm_report._merge_state(count==len(ids))。"""
    union: set[str] = set()
    saw = False
    for e_dir in sorted(Path(camp_dir).glob("run*/E")):
        atoms = None
        gs = _read_json(e_dir / "group_summary.json")
        if isinstance(gs, dict) and isinstance(gs.get("state_atoms"), list):
            atoms = [a for a in gs["state_atoms"] if isinstance(a, str)]
        else:
            led = _read_json(e_dir / "epochs" / "state_ledger.json")
            if isinstance(led, dict) and isinstance(led.get("atoms"), list):
                atoms = [a for a in led["atoms"] if isinstance(a, str)]
        if atoms is not None:
            saw = True
            union |= set(atoms)
    return sorted(union) if saw else None


def _camp_null(camp_dir: Path, state_gain: dict | None) -> dict | None:
    """state_gain 全零差 → null-with-reason(溯源 E 臂审计账本)。"""
    if state_gain is None:
        return None
    fb, auc = state_gain["final_blocks"], state_gain["auc_blocks_norm"]
    if not _all_zero(fb):
        return None
    ev = _tally(sorted(Path(camp_dir).glob("run*/E")))
    st = ev["statuses"]
    if ev["ledgers_found"] == 0:
        code = "NO_LEDGER_EVIDENCE"
        reason = ("zero-diff but no state_injections.jsonl found under "
                  "camp run dirs — cannot attribute")
    elif st.get("INJECTED", 0) == 0:
        code = "STATE_NEVER_ENGAGED"
        skips = st.get("SKIP_R2_PROGRESS", 0)
        reason = (f"R2 skipped at {skips} audited boundary/boundaries, "
                  f"0 INJECTED across {ev['ledgers_found']} E-arm "
                  f"ledger(s) — state machinery never engaged on this target"
                  if skips else
                  f"0 INJECTED across {ev['ledgers_found']} E-arm ledger(s) "
                  f"— no state seeds were ever written on this target")
    else:
        code = "STATE_GAIN_NET_ZERO"
        reason = (f"{st['INJECTED']} state seed(s) written yet E matched D1 "
                  f"on all {fb['n']} paired runs — state injection net-zero "
                  f"on this target")
    return {"comparison": STATE_GAIN, "arms": ["E", "D1"], "n": fb["n"],
            "blocks_mean_delta": fb["mean"], "auc_mean_delta": auc["mean"],
            "reason_code": code, "reason": reason, "evidence": ev}


def analyze_campaign(camp_dir: Path) -> dict | None:
    """camp_<target>/ → 战役半边 section(nulls 键随 section 返回)。"""
    summary = _read_json(Path(camp_dir) / "summary.json")
    if not isinstance(summary, dict) \
            or not isinstance(summary.get("per_run"), list):
        return None
    rows = [r for r in summary["per_run"] if isinstance(r, dict)]
    if not rows:
        return None

    def cells(arm: str) -> list:
        return [r.get(arm) if isinstance(r.get(arm), dict) else None
                for r in rows]

    def col(arm: str, key: str) -> list:
        return [(c.get(key) if isinstance(c, dict) else None)
                for c in cells(arm)]

    arm_names = sorted({k for r in rows for k in r
                        if k != "seed" and isinstance(r.get(k), dict)})
    arms: dict[str, dict] = {}
    for arm in arm_names:
        entry = {key: _stats(col(arm, key))
                 for key in ("final_blocks", "auc_blocks_norm", *_STATE_KEYS)}
        arms[arm] = entry

    state_gain = None
    if "E" in arms and "D1" in arms:
        state_gain = {
            "final_blocks": _paired(col("E", "final_blocks"),
                                    col("D1", "final_blocks")),
            "auc_blocks_norm": _paired(col("E", "auc_blocks_norm"),
                                       col("D1", "auc_blocks_norm"))}

    e_arms = arms.get("E", {})
    atoms_union = _camp_atoms_union(camp_dir)
    has_state = any(e_arms[k]["n"] for k in _STATE_KEYS if k in e_arms)
    state_atoms = None
    if has_state or atoms_union is not None:
        state_atoms = {
            "E_mean": e_arms.get("state_atoms_reached", {}).get("mean"),
            "E_n": e_arms.get("state_atoms_reached", {}).get("n"),
            "E_injections_mean": e_arms.get("state_injections_total",
                                            {}).get("mean"),
            "E_union_count": len(atoms_union) if atoms_union is not None
            else None,
            "E_union_atoms": atoms_union,
            "D1_coverage_proxy": None,
            "D1_coverage_proxy_note":
                "campaign summary 不携带 D1 状态字段(非 state 臂);"
                "coverage-proxy 需 IR+靶重算,本脚本对 results/ 只读不重算"}

    null = _camp_null(camp_dir, state_gain)
    return {"dir": str(camp_dir), "runs": len(rows), "arms": arms,
            "state_gain": state_gain, "state_atoms": state_atoms,
            "nulls": [null] if null else []}


# --------------------------------------------------------------- 消融半边


def _atoms_measurable(target) -> bool | None:
    """消融 atoms 三指标可测性(同 state_metrics 的微靶分派;只读重判)。

    AppModel/Flask 靶(如 vulnshop)无微模型 → state_metrics 早退
    _EMPTY_STATE,消融 atoms 是结构性零而非测得零。True=微靶可测;
    False=无微模型(未测);None=靶路径不可寻,无法判定(不猜)。
    相对路径按 cwd 解析(正式产物 target 为仓库根相对路径)。
    """
    from stateforge.solving.state_seed_injector import synthesize_micro_model
    if not isinstance(target, str) or not target:
        return None
    p = Path(target)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        return None
    try:
        return synthesize_micro_model(p) is not None
    except Exception:                          # noqa: BLE001
        return None


def _abl_null(abl_dir: Path, key: str, other: str,
              paired: dict | None) -> dict | None:
    """消融对比全零差 → null-with-reason(溯源对照臂审计账本)。"""
    if paired is None:
        return None
    b = paired["blocks"]
    a = paired["atoms"] if paired["atoms"] is not None else {"mean": None}
    if not _all_zero(b):
        return None
    ev = _tally(sorted(Path(abl_dir).glob(f"run*/{other}")))
    st = ev["statuses"]
    if ev["ledgers_found"] == 0:
        code = "NO_LEDGER_EVIDENCE"
        reason = ("zero-diff but no state_injections.jsonl found under "
                  f"{other} run dirs — cannot attribute")
    elif key == TRIGGER:
        if st.get("INJECTED", 0) == 0:
            code = "R2A_VACUOUS"
            reason = (f"R2-(a) vacuous on this target: trigger bypass fired "
                      f"at {st.get('INJECT_ALWAYS', 0)} boundary/boundaries "
                      f"but the injector had no writable goals (0 INJECTED "
                      f"in {other} ledgers)")
        else:
            code = "TRIGGER_NET_ZERO"
            always = st.get("INJECT_ALWAYS", 0)
            if always:
                reason = (f"R2-(b) bypass engaged ({always} boundaries, "
                          f"{st['INJECTED']} seeds written) yet E matched it "
                          f"on all {b['n']} paired runs — trigger "
                          f"discipline net-zero on this target")
            else:
                # 微靶实况:旁路只在 new_atoms 非空时审计 INJECT_ALWAYS,
                # 覆盖代理不见新原子 → 0 条,但 E-always 无条件注入
                # (INJECTED>0)。措辞必须与证据自洽,不写"bypass engaged"。
                reason = (f"bypass never fired (0 new-atom boundaries); "
                          f"E-always injected unconditionally "
                          f"({st['INJECTED']} seeds) yet E matched it on "
                          f"all {b['n']} paired runs — trigger discipline "
                          f"net-zero on this target")
    else:                                          # PLANNER
        if st.get("INJECTED", 0) == 0:
            code = "PLANNER_NET_ZERO"
            reason = (f"random arm wrote 0 seeds "
                      f"({st.get('NO_LEGAL', 0)} NO_LEGAL) across "
                      f"{ev['ledgers_found']} ledger(s) — planner "
                      f"contribution untestable on this run set")
        else:
            code = "PLANNER_NET_ZERO"
            reason = (f"random legal sequences ({st['INJECTED']} seeds, "
                      f"path=random) matched E on all {b['n']} paired runs "
                      f"— planner pipeline net-zero on this target")
    return {"comparison": key, "arms": ["E", other], "n": b["n"],
            "blocks_mean_delta": b["mean"], "atoms_mean_delta": a["mean"],
            "reason_code": code, "reason": reason, "evidence": ev}


def analyze_ablation(abl_dir: Path) -> dict | None:
    """abl_<target>/ → 消融半边 section(nulls 键随 section 返回)。"""
    rep = _read_json(Path(abl_dir) / "state_ablation.json")
    if not isinstance(rep, dict) or not isinstance(rep.get("arms"), dict):
        return None
    arms_json = rep["arms"]
    all_runs = sorted({r for a in arms_json.values() if isinstance(a, dict)
                       for r in (a.get("per_run") or {})})
    # I3:atoms 三指标可测性重判(无微模型靶 → 未测,null/-,非测得零)
    atoms_ok = _atoms_measurable(rep.get("target"))

    def col(arm: str, key: str) -> list:
        per = arms_json[arm].get("per_run") or {}
        return [(per.get(r) or {}).get(key) for r in all_runs]

    def union_atoms(arm: str) -> list[str]:
        return sorted({atom
                       for r in (arms_json[arm].get("state_runs") or [])
                       if isinstance(r, dict)
                       for atom in (r.get("atoms") or [])
                       if isinstance(atom, str)})

    arms_out: dict[str, dict] = {}
    for arm in sorted(arms_json):
        if not isinstance(arms_json[arm], dict):
            continue
        inj = col(arm, "injections_total")
        arms_out[arm] = {
            "mean_blocks": _stats(col(arm, "blocks")),
            "mean_edges": _stats(col(arm, "edges")),
            "atoms_union_count": (len(union_atoms(arm))
                                  if atoms_ok is not False else None),
            "atoms_union": (union_atoms(arm)
                            if atoms_ok is not False else None),
            "injections_total": sum(v for v in inj
                                    if isinstance(v, (int, float)))}

    def pair(other: str) -> dict | None:
        if "E" not in arms_json or other not in arms_json:
            return None
        return {"blocks": _paired(col("E", "blocks"), col(other, "blocks")),
                "atoms": (_paired(col("E", "atoms_reached"),
                                  col(other, "atoms_reached"))
                          if atoms_ok is not False else None)}

    pc, tv = pair("E-rand"), pair("E-always")
    nulls = [n for n in (_abl_null(abl_dir, PLANNER, "E-rand", pc),
                         _abl_null(abl_dir, TRIGGER, "E-always", tv))
             if n is not None]
    runs = rep.get("runs")
    section: dict = {"dir": str(abl_dir),
                     "runs": runs if isinstance(runs, int) else len(all_runs),
                     "mode": rep.get("mode"), "atoms_measurable": atoms_ok,
                     "arms": arms_out,
                     PLANNER: pc, TRIGGER: tv, "nulls": nulls}
    if atoms_ok is False:
        section["atoms_note"] = (
            "状态原子三指标仅微靶路径可测;本靶无微模型"
            "(synthesize_micro_model=None,AppModel 路径),atoms 为未测"
            "(null),非测得零")
    return section


# --------------------------------------------------------------- 聚合


def analyze(root: Path) -> dict:
    """<p4_formal_dir> → 单一报告 JSON(对 root 只读)。"""
    root = Path(root)
    camps, abls = discover(root)
    report: dict = {
        "schema": SCHEMA, "root": str(root), "targets": {},
        "coverage": {
            "campaign_dirs": sorted(str(p) for p in camps.values()),
            "ablation_dirs": sorted(str(p) for p in abls.values()),
            "targets_without_ablation": sorted(t for t in camps
                                               if t not in abls),
            "ablations_without_campaign": sorted(t for t in abls
                                                 if t not in camps)},
        "nulls": []}
    for t in sorted(set(camps) | set(abls)):
        entry: dict = {"campaign": None, "ablation": None, "nulls": []}
        if t in camps:
            entry["campaign"] = analyze_campaign(camps[t])
            for n in (entry["campaign"] or {}).pop("nulls", []):
                n["target"] = t
                entry["nulls"].append(n)
        if t in abls:
            entry["ablation"] = analyze_ablation(abls[t])
            for n in (entry["ablation"] or {}).pop("nulls", []):
                n["target"] = t
                entry["nulls"].append(n)
        report["targets"][t] = entry
        report["nulls"].extend(entry["nulls"])
    return report


# --------------------------------------------------------------- 文本表


def _fmt(v, nd: int = 4) -> str:
    if v is None:
        return "-"
    s = f"{float(v):.{nd}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


def _signed(v) -> str:
    if v is None:
        return "-"
    s = _fmt(abs(v))
    return f"+{s}" if v >= 0 else f"-{s}"


def _pm(st: dict) -> str:
    if not st or st.get("mean") is None:
        return "-"
    sd = st.get("stdev")
    return f"{_fmt(st['mean'])}±{_fmt(sd) if sd is not None else '-'}"


def _wtl(p: dict) -> str:
    return f"{p.get('wins', 0)}/{p.get('ties', 0)}/{p.get('losses', 0)}"


def render_text(report: dict) -> str:
    cov = report["coverage"]
    lines = [f"P4 formal analysis — {report.get('root')}",
             f"schema {report.get('schema')} | "
             f"campaign dirs {len(cov['campaign_dirs'])} | "
             f"ablation dirs {len(cov['ablation_dirs'])}"]
    for t, e in report["targets"].items():
        lines.append(f"\n== {t} ==")
        camp, ab = e.get("campaign"), e.get("ablation")
        if camp:
            lines.append(f"[campaign] runs={camp['runs']}")
            lines.append("  arm  final_blocks   auc_blocks_norm"
                         "  atoms(mean)  inj(mean)")
            for arm in sorted(camp["arms"]):
                a = camp["arms"][arm]
                atoms, inj = a.get("state_atoms_reached") or {}, \
                    a.get("state_injections_total") or {}
                atoms_s = (f"{_fmt(atoms.get('mean'))} (n={atoms['n']})"
                           if atoms.get("n") else "-")
                inj_s = (f"{_fmt(inj.get('mean'))} (n={inj['n']})"
                         if inj.get("n") else "-")
                lines.append(f"  {arm:<4} {_pm(a['final_blocks']):<14} "
                             f"{_pm(a['auc_blocks_norm']):<16} "
                             f"{atoms_s:<12} {inj_s}")
            sg = camp.get("state_gain")
            if sg:
                fb, auc = sg["final_blocks"], sg["auc_blocks_norm"]
                lines.append(f"  E−D1 state_gain: blocks mean={_signed(fb['mean'])}"
                             f" median={_signed(fb['median'])}"
                             f" stdev={_fmt(fb['stdev'])} n={fb['n']}"
                             f"  wins/ties/losses={_wtl(fb)}")
                lines.append(f"           auc_norm: mean={_signed(auc['mean'])}"
                             f" median={_signed(auc['median'])} n={auc['n']}"
                             f"  wins/ties/losses={_wtl(auc)}")
            sa = camp.get("state_atoms")
            if sa:
                ua = ", ".join(sa.get("E_union_atoms") or []) or "-"
                lines.append(f"  E state atoms: mean={_fmt(sa['E_mean'])}"
                             f" (n={sa['E_n']}) | union={sa['E_union_count']}: {ua}")
                lines.append(f"  D1 coverage-proxy: n/a — "
                             f"{sa['D1_coverage_proxy_note']}")
        if ab:
            lines.append(f"[ablation] runs={ab['runs']} mode={ab.get('mode')}")
            lines.append("  arm       blocks±stdev    atoms_union  injections")
            for arm in sorted(ab["arms"]):
                a = ab["arms"][arm]
                atoms_cell = (a["atoms_union_count"]
                              if a["atoms_union_count"] is not None else "-")
                lines.append(f"  {arm:<9} {_pm(a['mean_blocks']):<15} "
                             f"{atoms_cell!s:<12} "
                             f"{a['injections_total']}")
            if ab.get("atoms_measurable") is False:
                lines.append(f"  atoms n/a — {ab.get('atoms_note')}")
            for key, label, other in ((PLANNER, "planner contribution", "E-rand"),
                                      (TRIGGER, "trigger value", "E-always")):
                p = ab.get(key)
                if not p:
                    continue
                b = p["blocks"]
                if p["atoms"] is not None:
                    a = p["atoms"]
                    atoms_txt = (f"atoms mean={_signed(a['mean'])}"
                                 f" n={a['n']} wins={a['wins']}")
                else:                       # I3:无微模型靶,atoms 未测
                    atoms_txt = "atoms n/a(未测:无微模型)"
                lines.append(f"  E−{other} ({label}): blocks"
                             f" mean={_signed(b['mean'])}"
                             f" median={_signed(b['median'])}"
                             f" stdev={_fmt(b['stdev'])} n={b['n']}"
                             f" wins/ties/losses={_wtl(b)} | {atoms_txt}")
        if not camp and not ab:
            lines.append("  (no campaign / ablation artifacts found)")
        for n in e.get("nulls", ()):
            lines.append(f"  NULL [{n['reason_code']}] {n['comparison']}:"
                         f" blocks Δ={_fmt(n.get('blocks_mean_delta'))}"
                         f" (n={n.get('n')}) — {n['reason']}")
    lines.append(f"\nnulls: {len(report['nulls'])}")
    for n in report["nulls"]:
        lines.append(f"  [{n['target']}] {n['comparison']} → "
                     f"{n['reason_code']}: {n['reason']}")
    return "\n".join(lines)


# --------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P4-T6a:聚合 p4_formal 战役产物 → 论文口径表(只读)")
    parser.add_argument("p4_formal_dir",
                        help="p4_formal 目录(含 camp_*/abl_*;只读)")
    parser.add_argument("--out", default=None,
                        help="JSON 输出路径(缺省仅 stdout 不落盘;"
                             "给定则同时写 <out>.txt 文本表)")
    args = parser.parse_args(argv)
    root = Path(args.p4_formal_dir)
    camps, abls = discover(root)
    if not camps and not abls:
        parser.error(f"{root} 下无 {CAMP_PREFIX}*/{ABL_PREFIX}* 目录")
    report = analyze(root)
    text = render_text(report)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        out.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
        print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
