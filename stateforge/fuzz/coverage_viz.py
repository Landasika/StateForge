"""coverage_viz.py —— 在静态图上可视化代码覆盖(端点访问 + 块覆盖着色)。

用法:
  # 1) 跑一组请求,采集覆盖
  python3 -m stateforge.fuzz.coverage_viz run stateforge/targets/vulnshop --framework flask

  # 2) 出覆盖着色 SVG
  python3 -m stateforge.fuzz.coverage_viz show stateforge/targets/vulnshop

流程:发请求(vulnshop test_client) → sys.settrace 采集行覆盖
     → 行→块映射 → G_xfg 着色 → SVG
"""

from __future__ import annotations

import json
import sys
import trace as trace_mod
from pathlib import Path

import networkx as nx

# ---------------------------------------------------------------- 覆盖采集


def collect_coverage(app_root: str, framework: str = "flask",
                     requests: list[dict] | None = None) -> set[int]:
    """对目标发一组请求,返回被覆盖的行号集合(按 (file, line) 采集后转为行号)。

    默认请求:对每个端点发一次(路由级探测)。
    """
    from stateforge.analyzer.analyzer import analyze
    model = analyze(Path(app_root), framework)

    if requests is None:
        requests = _default_requests(model)

    # 加载 target app(进程内 WSGI)
    app = _load_app(app_root, framework)

    # 用 sys.settrace 采集
    covered_lines: set[tuple[str, int]] = set()

    def tracer(frame, event, arg):
        if event == "line":
            covered_lines.add((frame.f_code.co_filename, frame.f_lineno))
        return tracer

    client = app.test_client()
    sys.settrace(tracer)
    try:
        for req in requests:
            try:
                if req.get("json"):
                    resp = client.open(req["route"], method=req.get("method", "GET"),
                                       json=req["json"])
                else:
                    resp = client.open(req["route"], method=req.get("method", "GET"))
                print(f"  {req.get('method','GET'):6s} {req['route']:40s} → {resp.status_code}")
            except Exception as e:
                print(f"  {req.get('method','GET'):6s} {req['route']:40s} → ERR {e}")
    finally:
        sys.settrace(None)

    # 过滤:只保留目标项目内的行
    root = str(Path(app_root).resolve())
    return {(f, l) for f, l in covered_lines if f.startswith(root)}


def _default_requests(model) -> list[dict]:
    """对每个端点发一次基础请求(路由级探测)。"""
    reqs = []
    for ep in model.endpoints:
        route = ep.route
        for p in ep.params:
            if "int:" in route:
                route = route.replace(f"<int:{p.name}>", "1")
            elif f"<{p.name}>" in route:
                route = route.replace(f"<{p.name}>", "test")
        req = {"method": ep.methods[0], "route": route}
        if ep.methods[0] == "POST":
            req["json"] = {}
        reqs.append(req)
    return reqs


def _load_app(app_root: str, framework: str):
    if framework == "flask":
        import importlib
        import inspect
        sys.path.insert(0, str(Path(app_root).parent))
        mod_name = Path(app_root).name
        # 尝试 app.py 的 create_app 或模块级 app
        for entry in ["app", "application"]:
            try:
                mod = importlib.import_module(f"{mod_name}.{entry}")
                if hasattr(mod, "create_app"):
                    return mod.create_app(db_path="/tmp/coverage_viz.db",
                                          guards=0, init_db=True)
                if hasattr(mod, "app"):
                    return mod.app
            except (ImportError, AttributeError):
                continue
        raise RuntimeError(f"无法加载 {app_root} 的 Flask app")
    raise RuntimeError(f"framework {framework} 暂不支持")


# ---------------------------------------------------------------- 行→块映射


def map_coverage_to_blocks(app_root: str, framework: str,
                           covered: set[tuple[str, int]]) -> dict:
    """把覆盖行映射到 G_xfg 的块节点(精确匹配优先,范围匹配兜底)。

    精度改进(任务 B):
    ① 精确匹配:块内每条语句的起始行号逐一核对(不是范围包含)
    ② 范围兜底:精确匹配失败的块,退回行范围判断(处理多行语句)
    ③ 文件消歧:同目录下的同名文件用完整相对路径消歧
    """
    from stateforge.analyzer.analyzer import analyze
    from stateforge.analyzer.cfg import build_xfg

    model = analyze(Path(app_root), framework)
    cg_path = Path(app_root) / ".pycg-callgraph.json"
    xfg = build_xfg(model, callgraph_path=cg_path if cg_path.exists() else None)

    # 构建 文件 → 行号集合(用相对路径消歧同名文件)
    root = Path(app_root).resolve()
    by_file: dict[str, set[int]] = {}
    for f, l in covered:
        try:
            rel = str(Path(f).resolve().relative_to(root))
        except ValueError:
            rel = Path(f).name
        by_file.setdefault(rel, set()).add(l)

    # 对每个块节点:精确匹配(语句行号)→范围兜底
    covered_blocks = {}
    for n, d in xfg.nodes(data=True):
        if d.get("kind") not in ("block", "branch", "terminal"):
            continue
        stmts = d.get("stmts", [])
        lines = d.get("lines")
        if not lines:
            continue
        # 文件消歧:用相对路径
        d_file = d.get("file", "")
        try:
            rel = str(Path(d_file).resolve().relative_to(root))
        except (ValueError, TypeError):
            rel = Path(d_file).name if d_file else ""
        file_lines = by_file.get(rel, set())
        if not file_lines:
            continue

        lo, hi = lines
        # ① 精确匹配:块内语句的起始行号
        stmt_lines = {s.lineno for s in stmts if hasattr(s, "lineno")}
        exact_hit = bool(stmt_lines & file_lines)
        # ② 范围兜底:处理多行语句(如 if cond: 跨行)
        range_hit = any(lo <= l <= hi for l in file_lines)

        if exact_hit or range_hit:
            covered_blocks[n] = True
            xfg.nodes[n]["covered"] = True

    return {"xfg": xfg, "model": model, "covered_blocks": covered_blocks,
            "total_blocks": sum(1 for n, d in xfg.nodes(data=True)
                               if d.get("kind") in ("block", "branch", "terminal"))}


# ---------------------------------------------------------------- 可视化


def render_coverage_svg(xfg: nx.MultiDiGraph, model, covered_blocks: dict,
                         out_path: str) -> None:
    """覆盖着色 SVG:绿=已覆盖,灰=未覆盖,红框=状态锚点。"""
    from stateforge.analyzer.svg import KIND_STYLE

    _W, _H, _GAP_X, _GAP_Y = 190, 46, 60, 70

    # 只保留块级节点 + action
    nodes = [n for n, d in xfg.nodes(data=True)
             if d.get("kind") in ("block", "branch", "terminal", "entry", "exit", "action")]
    gg = xfg.subgraph(nodes)

    # 层式布局
    from stateforge.analyzer.svg import _layers, _esc
    entry = next((n for n in nodes if xfg.nodes[n].get("kind") == "entry"), None)
    if entry is None and nodes:
        entry = nodes[0]
    if entry is None:
        return
    depth = _layers(gg, entry)
    max_d = max(depth.values()) if depth else 0
    # 手动布局(复用 _layout 逻辑)
    by_layer: dict[int, list] = {}
    for n, d_val in depth.items():
        by_layer.setdefault(d_val, []).append(n)
    pos = {}
    for d_val, nodes_in_layer in sorted(by_layer.items()):
        for i, n in enumerate(nodes_in_layer):
            pos[n] = (i * (_W + _GAP_X), d_val * (_H + _GAP_Y))
    xs = [p[0] for p in pos.values()] or [0]
    width, height = max(xs) + _W + 40, (max_d + 1) * (_H + _GAP_Y) + 70

    lines = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"font-family='Helvetica,Arial,sans-serif'>",
        f"<text x='20' y='28' font-size='17' font-weight='bold'>"
        f"Coverage Map — {len(covered_blocks)}/{sum(1 for n in nodes if xfg.nodes[n].get('kind') != 'action')} blocks covered"
        f"</text>",
        "<defs><marker id='a' markerWidth='9' markerHeight='9' refX='8' refY='3' "
        "orient='auto'><path d='M0,0 L9,3 L0,6 z' fill='#555'/></marker></defs>",
    ]

    for n, (x, y) in pos.items():
        d = xfg.nodes[n]
        kind = d.get("kind", "block")
        is_covered = d.get("covered", False)
        has_anchor = bool(d.get("anchors"))

        if kind == "action":
            fill, stroke = "#d6f5d6", "#2e8b57"
        elif is_covered:
            fill = "#c8facc"          # 绿色=已覆盖
            stroke = "#28a745"
        else:
            fill = "#f0f0f0"          # 灰色=未覆盖
            stroke = "#ccc"
        sw = "2.5" if has_anchor else "1"

        shape = "box" if kind == "action" else "rect"
        rx = "7"
        if kind == "branch":
            shape = "rect"
            rx = "12"

        lines.append(
            f"<{shape} x='{x}' y='{y+40}' width='{_W}' height='{_H}' rx='{rx}' "
            f"fill='{fill}' stroke='{stroke}' stroke-width='{sw}'/>")

        label = n.split("::")[-1] if "::" in n else n
        if kind == "action":
            label = n
        lines.append(f"<text x='{x+8}' y='{y+58}' font-size='11' fill='#111'>"
                     f"{_esc(label)[:32]}</text>")
        if has_anchor:
            anchors = d["anchors"]
            atext = " ".join(f"{a['kind'][0]}:{a.get('var','')}" for a in anchors[:3])
            lines.append(f"<text x='{x+8}' y='{y+72}' font-size='9' fill='#c00'>"
                         f"{_esc(atext)[:40]}</text>")

    for u, v, d in gg.edges(data=True):
        if u not in pos or v not in pos:
            continue
        (x1, y1), (x2, y2) = pos[u], pos[v]
        sx, sy = x1 + _W / 2, y1 + 40 + _H
        tx, ty = x2 + _W / 2, y2 + 40
        color = {"cfg": "#888", "call": "#7b2fbf", "state-link": "#2b6cb0",
                "entry-edge": "#2e8b57"}.get(d.get("kind"), "#888")
        lines.append(f"<line x1='{sx}' y1='{sy}' x2='{tx}' y2='{ty}' "
                     f"stroke='{color}' marker-end='url(#a)' stroke-width='1'/>")

    lines.append("</svg>")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"[coverage-viz] {out_path}")


# ---------------------------------------------------------------- CLI


def main():
    if len(sys.argv) < 3:
        print("用法:")
        print("  python3 -m stateforge.fuzz.coverage_viz run <target_dir> [--framework flask]")
        print("  python3 -m stateforge.fuzz.coverage_viz show <target_dir> [--framework flask]")
        sys.exit(1)

    cmd = sys.argv[1]
    target = sys.argv[2]
    framework = "flask"
    if "--framework" in sys.argv:
        framework = sys.argv[sys.argv.index("--framework") + 1]

    if cmd == "run":
        print(f"[coverage] 采集 {target} ...")
        covered = collect_coverage(target, framework)
        cov_file = Path(target) / ".coverage_lines.json"
        cov_file.write_text(json.dumps([list(c) for c in covered]))
        print(f"[coverage] {len(covered)} 行覆盖 → {cov_file}")

    elif cmd == "show":
        cov_file = Path(target) / ".coverage_lines.json"
        if not cov_file.exists():
            print("先跑 'run' 采集覆盖")
            sys.exit(1)
        covered = {tuple(c) for c in json.loads(cov_file.read_text())}
        result = map_coverage_to_blocks(target, framework, covered)
        n_cov = len(result["covered_blocks"])
        n_total = result["total_blocks"]
        print(f"[coverage] {n_cov}/{n_total} 块覆盖 ({100*n_cov/max(n_total,1):.0f}%)")
        out = Path("results") / "coverage_map.svg"
        render_coverage_svg(result["xfg"], result["model"],
                           result["covered_blocks"], str(out))
        # 端点覆盖
        model = result["model"]
        from stateforge.analyzer.analyzer import analyze
        m = analyze(Path(target), framework)
        visited = []
        for ep in m.endpoints:
            route = ep.route
            for p in ep.params:
                route = route.replace(f"<int:{p.name}>", "1").replace(f"<{p.name}>", "x")
            key = f"{ep.methods[0]} {route}"
            visited.append(key)
        print(f"[endpoints] {len(visited)} 端点可访问")


if __name__ == "__main__":
    main()
