"""gap_analysis.py —— 覆盖缺口分析:哪些块没到、离哪最近、怎么构造输入到达。

三个问题:
  ① 哪些块没被覆盖?
  ② 哪个未覆盖块离已覆盖区域最近?(下一个该打的)
  ③ 要到达它,需要什么输入/前置操作?

用法:
  python3 -m stateforge.fuzz.gap_analysis stateforge/targets/vulnshop
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import networkx as nx


# ---------------------------------------------------------------- 缺口分析


def find_gaps(xfg: nx.MultiDiGraph, covered_blocks: set[str]) -> list[dict]:
    """找出所有未覆盖块,计算它们到已覆盖区域的距离和路径。

    返回按距离排序的缺口列表,每个包含:
      block, distance, path(从最近的覆盖块到它的路径), blocking_gate
    """
    # 所有块级节点
    all_blocks = {n for n, d in xfg.nodes(data=True)
                  if d.get("kind") in ("block", "branch", "terminal")}
    uncovered = all_blocks - covered_blocks
    if not uncovered:
        return []

    # BFS from all covered blocks simultaneously (multi-source BFS)
    dist: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    queue = deque()
    for b in covered_blocks & all_blocks:
        dist[b] = 0
        parent[b] = None
        queue.append(b)

    while queue:
        u = queue.popleft()
        for v in xfg.successors(u):
            if v not in dist:
                dist[v] = dist[u] + 1
                parent[v] = u
                queue.append(v)

    # 构建缺口报告
    gaps = []
    for b in uncovered:
        if b not in dist:
            # 不可达(没有从覆盖块到它的路径)
            gaps.append({
                "block": b,
                "distance": float("inf"),
                "path": [],
                "blocking_gate": None,
                "func": xfg.nodes[b].get("func", ""),
                "lines": xfg.nodes[b].get("lines", (0, 0)),
                "anchors": xfg.nodes[b].get("anchors", []),
            })
            continue

        # 回溯路径
        path = []
        cur = b
        while cur is not None:
            path.append(cur)
            cur = parent.get(cur)
        path.reverse()

        # 找 blocking gate:路径上最后一个(离目标最近的)带 pre/guard 锚的块
        blocking_gate = None
        for pb in reversed(path[:-1]):  # 排除目标自身
            anchors = xfg.nodes[pb].get("anchors", [])
            gates = [a for a in anchors if a.get("kind") in ("pre", "guard")]
            if gates:
                blocking_gate = {
                    "block": pb,
                    "gates": gates,
                    "cond": xfg.nodes[pb].get("kind") == "branch",
                }
                break

        gaps.append({
            "block": b,
            "distance": dist[b],
            "path": path,
            "blocking_gate": blocking_gate,
            "func": xfg.nodes[b].get("func", ""),
            "lines": xfg.nodes[b].get("lines", (0, 0)),
            "anchors": xfg.nodes[b].get("anchors", []),
        })

    gaps.sort(key=lambda g: g["distance"] if g["distance"] != float("inf") else 9999)
    return gaps


# ---------------------------------------------------------------- 输入构造建议


def suggest_input(gap: dict, xfg: nx.MultiDiGraph, model) -> str:
    """为缺口构造到达建议(人话)。

    判断逻辑:
    ① 该块所在的端点是否被调用过?
       → 没调用 → 建议调用该端点
       → 调用过但没走到 → 分支条件不满足
    ② 分支条件不满足 → 从锚点/门信息推导需要什么前置状态
    """
    func = gap.get("func", "")
    lines = gap.get("lines", (0, 0))

    # 找该函数对应的端点
    endpoint = None
    for ep in model.endpoints:
        if ep.func == func:
            endpoint = ep
            break

    if endpoint is None:
        return f"函数 {func} L{lines[0]}(无对应端点,可能是 helper)"

    # 检查该函数是否有任何块被覆盖(比只查入口块更准确:
    # 函数被调了但走了不同分支 ≠ 函数没被调)
    func_covered = any(
        xfg.nodes[n].get("covered", False)
        for n, d in xfg.nodes(data=True)
        if d.get("func") == func and d.get("kind") in ("block", "branch", "terminal", "entry")
    )

    if not func_covered:
        # 端点本身没被调用
        suggestions = [f"调用 {endpoint.methods[0]} {endpoint.route}"]
        # 检查是否需要 auth
        has_auth_pre = any(p.var == "Session.authed_as"
                          for p in endpoint.preconditions)
        if has_auth_pre:
            suggestions.append("(需要先 POST /auth/register + POST /auth/login)")
        return " → ".join(suggestions)

    # 端点被调了但没走到这个块 → 分支条件不满足
    # 从路径上的分支块提取条件
    path = gap.get("path", [])
    conditions = []
    import ast as _ast
    for pb in path:
        d = xfg.nodes[pb]
        if d.get("kind") == "branch":
            for s in d.get("stmts", []):
                if hasattr(s, "test"):
                    try:
                        cond_text = _ast.unparse(s.test)
                        conditions.append(cond_text)
                    except Exception:
                        pass

    parts = [f"{endpoint.methods[0]} {endpoint.route} 已调用但未到此块"]

    if conditions:
        parts.append(f"需满足分支条件: {' AND '.join(conditions[:3])}")

    # 从锚点推导前置状态
    from stateforge.analyzer.cfg import build_cfg, attach_anchors, block_gates
    for ep2 in model.endpoints:
        if ep2.func == func:
            g2 = build_cfg(ep2)
            attach_anchors(g2, ep2)
            gates = block_gates(g2)
            # 找到 gap 对应的块(按行号)
            lo, hi = lines
            for block_id, gate_list in gates.items():
                b_lines = g2.nodes[block_id].get("lines", (0, 0))
                if b_lines[0] <= lo <= b_lines[1]:
                    if gate_list:
                        gate_vars = list({a["var"] for a in gate_list})
                        parts.append(f"需过门: {', '.join(gate_vars)}")
                        for gv in gate_vars:
                            if "auth" in gv.lower():
                                parts.append("  → 先登录")
                            elif "status" in gv.lower():
                                parts.append("  → 先执行前置操作使状态到位")
                            elif "owner" in gv.lower() or "user_id" in gv.lower():
                                parts.append("  → 用资源所有者的身份操作")
                    break
            break

    # 目标块自身的语义
    anchors = gap.get("anchors", [])
    if anchors:
        for a in anchors:
            if a.get("kind") == "eff":
                parts.append(f"到达后将写入: {a.get('var', '')}")

    return "\n    ".join(parts)


def _is_covered(xfg: nx.MultiDiGraph, node: str | None) -> bool:
    if node is None:
        return False
    return xfg.nodes[node].get("covered", False)


# ---------------------------------------------------------------- 主流程


def analyze(target_dir: str, framework: str = "flask") -> None:
    """主入口:加载覆盖数据 → 找缺口 → 排序 → 输出建议。"""
    from stateforge.analyzer.analyzer import analyze
    from stateforge.analyzer.cfg import build_xfg
    from stateforge.fuzz.coverage_viz import map_coverage_to_blocks

    # 加载覆盖
    cov_file = Path(target_dir) / ".coverage_lines.json"
    if not cov_file.exists():
        print("先跑 coverage_viz run 采集覆盖数据")
        sys.exit(1)
    covered = {tuple(c) for c in json.loads(cov_file.read_text())}

    # 建图 + 映射
    result = map_coverage_to_blocks(target_dir, framework, covered)
    xfg, model = result["xfg"], result["model"]
    covered_blocks = set(result["covered_blocks"].keys())
    total = result["total_blocks"]

    # 缺口分析
    gaps = find_gaps(xfg, covered_blocks)

    # 输出
    print(f"\n{'='*70}")
    print(f"覆盖缺口分析: {len(covered_blocks)}/{total} 块已覆盖 "
          f"({100*len(covered_blocks)/max(total,1):.0f}%), "
          f"{len(gaps)} 个未覆盖块")
    print(f"{'='*70}\n")

    # 按函数分组
    by_func: dict[str, list] = {}
    for g in gaps:
        by_func.setdefault(g["func"], []).append(g)

    for func, func_gaps in sorted(by_func.items(), key=lambda x: x[1][0]["distance"]):
        print(f"--- {func} ({len(func_gaps)} 个缺口) ---")
        for g in func_gaps[:3]:  # 每个函数最多显示 3 个
            print(f"\n  块: {g['block']}")
            print(f"    {suggest_input(g, xfg, model)}")
        if len(func_gaps) > 3:
            print(f"    ... 还有 {len(func_gaps)-3} 个缺口")
        print()

    # 总结:最近的 5 个可行动缺口
    reachable_gaps = [g for g in gaps if g["distance"] != float("inf")]
    if reachable_gaps:
        print(f"{'='*70}")
        print("最近的 5 个可行动缺口(优先攻击):")
        print(f"{'='*70}")
        for i, g in enumerate(reachable_gaps[:5], 1):
            print(f"\n  [{i}] {g['func']} L{g['lines'][0]} "
                  f"(距离 {g['distance']} 步)")
            print(f"      {suggest_input(g, xfg, model)}")


def state_distance_analysis(target_dir: str, framework: str = "flask") -> None:
    """状态距离分析:对每个未覆盖块,计算从当前应用状态出发还需几个请求。"""
    from stateforge.analyzer.analyzer import analyze
    from stateforge.analyzer.cfg import (
        build_cfg, attach_anchors, block_gates, build_xfg,
    )
    from stateforge.fuzz.coverage_viz import map_coverage_to_blocks
    from stateforge.graph.builder import build_sstg
    from stateforge.planner.backward import plan_backward
    from stateforge.planner.goals import GoalSpec

    cov_file = Path(target_dir) / ".coverage_lines.json"
    if not cov_file.exists():
        print("先跑 coverage_viz run")
        sys.exit(1)
    covered = {tuple(c) for c in json.loads(cov_file.read_text())}
    result = map_coverage_to_blocks(target_dir, framework, covered)
    xfg, model = result["xfg"], result["model"]
    covered_blocks = set(result["covered_blocks"].keys())

    gaps = find_gaps(xfg, covered_blocks)
    sstg = build_sstg(model)

    print(f"\n{'='*70}")
    print(f"状态距离分析: {len(gaps)} 个未覆盖块")
    print(f"{'='*70}\n")

    for g in gaps[:10]:
        func = g["func"]
        lines = g["lines"]
        # 找到对应的端点
        ep = next((e for e in model.endpoints if e.func == func), None)
        if not ep:
            continue

        # 拿这个函数的块门
        cfg = build_cfg(ep)
        attach_anchors(cfg, ep)
        gates = block_gates(cfg)

        # 找 gap 对应的块的门
        target_gates = []
        for block_id, gate_list in gates.items():
            b_lines = cfg.nodes[block_id].get("lines", (0, 0))
            if b_lines[0] <= lines[0] <= b_lines[1]:
                target_gates = gate_list
                break

        # 分析每个门的状态可达性
        gate_status = []
        for gate in target_gates:
            var = gate.get("var", "")
            if var == "Session.authed_as":
                # 检查 auth 是否已覆盖
                auth_covered = any(
                    "auth" in cf.lower() or "login" in cf.lower()
                    for cf in [g2.get("func", "") for g2 in
                               [{"func": xfg.nodes[b].get("func", "")}
                                for b in covered_blocks]]
                )
                if auth_covered:
                    gate_status.append((var, "✓ 已登录", 0))
                else:
                    gate_status.append((var, "✗ 需登录", 2))  # register+login
            elif var in model.state_vars:
                sv = next(s for s in model.state_vars if s.var == var)
                domain = sv.domain or []
                gate_status.append((var, f"? 需到达特定值(域:{domain[:3]})", 1))
            else:
                gate_status.append((var, f"? 条件待验证", 1))

        # 计算总状态距离(所有门的最大距离 + 1 个到达作)
        state_dist = max((d for _, _, d in gate_status), default=0) + 1

        # 代码距离
        code_dist = g["distance"]

        # 可达性
        reachable = code_dist != float("inf")
        reach_str = "✅ 可达" if reachable else "❌ 不可达(静态图无路径)"

        print(f"  {func} L{lines[0]}")
        print(f"    代码距离: {code_dist} 步 | 状态距离: {state_dist} 个请求 | {reach_str}")
        if target_gates:
            for var, status, d in gate_status:
                print(f"    门: {var:24s} {status}")
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "state":
        target = sys.argv[2] if len(sys.argv) > 2 else "stateforge/targets/vulnshop"
        state_distance_analysis(target)
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "stateforge/targets/vulnshop"
        framework = sys.argv[2] if len(sys.argv) > 2 else "flask"
        analyze(target, framework)
