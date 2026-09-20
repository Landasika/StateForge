"""cfg.py —— T+11(v2.1 第一步):静态控制流图 + 执行流图 G_xfg。

三层:
  build_cfg(func|endpoint)   过程内 CFG:基本块/分支块/终止块;T/F/回边/异常边(近似)
  attach_anchors(cfg, ep)    SA-PR 事实(Effect/Precondition/Guard/Sink,带 file:line)
                             钉到行范围包含它的块 —— 状态锚点
  build_xfg(model)           端点动作节点 + 各端点 CFG 子图 + 跨端点共享状态虚边

近似(v1,记录于 LIMITATIONS):break/continue 当终止;Try 的 handler 从 try 入口
可达;orelse/finally 按顺序近似——Flask 处理器形态下足够。
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import networkx as nx

from stateforge.core.model import AppModel, Endpoint

_FILE_CACHE: dict[str, ast.Module] = {}


def _module(path: str) -> ast.Module:
    if path not in _FILE_CACHE:
        try:
            _FILE_CACHE[path] = ast.parse(Path(path).read_text(encoding="utf-8"),
                                          filename=path)
        except (OSError, SyntaxError):
            _FILE_CACHE[path] = ast.Module(body=[], type_ignores=[])
    return _FILE_CACHE[path]


def _locate_func(path: str, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(_module(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def build_cfg(func: ast.FunctionDef | Endpoint, prefix: str = "",
              file: str | None = None) -> nx.DiGraph:
    """过程内 CFG。接受 FunctionDef 或 Endpoint(按 ep.file+ep.func 反查函数)。"""
    if isinstance(func, Endpoint):
        file = func.file
        fdef = _locate_func(func.file, func.func)
        if fdef is None:
            g = nx.DiGraph()
            g.add_node(f"{prefix}missing", kind="missing", func=func.func)
            return g
        func = fdef
    file = file or "<ast>"

    g = nx.DiGraph()
    counter = itertools.count()
    fname = func.name

    def new_block(kind: str, stmts: list) -> str:
        bid = f"{prefix}{kind[0]}{next(counter)}"
        lines = (min([func.lineno] + [s.lineno for s in stmts]),
                 max([func.lineno] + [s.lineno for s in stmts]))
        g.add_node(bid, kind=kind, stmts=stmts, lines=lines, file=file, func=fname,
                   anchors=[])
        return bid

    def connect(edges, target):
        for p, cond in edges:
            g.add_edge(p, target, cond=cond)

    def link(stmts: list[ast.stmt], edges: list[tuple[str, str | None]]):
        for s in stmts:
            edges = link_stmt(s, edges)
        return edges

    def link_stmt(s: ast.stmt, edges):
        if isinstance(s, ast.If):
            br = new_block("branch", [s])
            connect(edges, br)
            t_end = link(s.body, [(br, "T")])
            f_end = link(s.orelse, [(br, "F")]) if s.orelse else [(br, "F")]
            return t_end + f_end
        if isinstance(s, (ast.While, ast.For, ast.AsyncFor)):
            br = new_block("branch", [s])
            connect(edges, br)
            body_end = link(s.body, [(br, "T")])
            for p, _c in body_end:          # 回边(忽略 cond 标注)
                g.add_edge(p, br, cond="back")
            if s.orelse:
                return link(s.orelse, [(br, "F")]) + []
            return [(br, "F")]
        if isinstance(s, ast.Return):
            t = new_block("terminal", [s])
            t_attr = g.nodes[t]
            t_attr["terminal"] = True
            connect(edges, t)
            return []
        if isinstance(s, (ast.Break, ast.Continue)):
            # v1 近似:当终止处理(罕见于 Flask 处理器;见 LIMITATIONS)
            t = new_block("terminal", [s])
            g.nodes[t]["terminal"] = True
            connect(edges, t)
            return []
        if isinstance(s, ast.Try):
            body_end = link(s.body, edges)
            for handler in s.handlers:      # 异常边近似:handler 自 try 入口可达
                h_end = link(handler.body, edges)
                body_end = body_end + h_end
            if s.orelse:
                body_end = body_end + link(s.orelse, body_end)
            if s.finalbody:
                body_end = link(s.finalbody, body_end)
            return body_end
        # 顺序语句:可扩展则并入当前开放块
        if (len(edges) == 1 and edges[0][1] is None
                and g.nodes[edges[0][0]].get("kind") == "block"):
            g.nodes[edges[0][0]]["stmts"].append(s)
            lo, hi = g.nodes[edges[0][0]]["lines"]
            g.nodes[edges[0][0]]["lines"] = (min(lo, s.lineno), max(hi, s.lineno))
            return edges
        b = new_block("block", [s])
        connect(edges, b)
        return [(b, None)]

    entry = new_block("entry", [])
    end = link(func.body, [(entry, None)])
    if end:                                 # 自然结尾 → 隐式出口
        x = new_block("exit", [])
        connect(end, x)
    return g


def attach_anchors(cfg: nx.DiGraph, ep: Endpoint) -> nx.DiGraph:
    """SA-PR 事实钉到块:行范围包含(或最近)事实行号。"""
    facts = ([("pre", p.var, p.line, p.op) for p in ep.preconditions]
             + [("eff", e.var, e.line, e.op) for e in ep.effects]
             + [("guard", g.kind, g.line, None) for g in ep.guards]
             + [("sink", k.kind, k.line, None) for k in ep.sinks])
    blocks = [(n, d["lines"]) for n, d in cfg.nodes(data=True)
              if d.get("lines") and d.get("kind") not in ("missing",)]
    for kind, var, line, op in facts:
        if not line:
            continue
        hit = next((n for n, (lo, hi) in blocks if lo <= line <= hi), None)
        if hit is None:                     # 最近块兜底(行号漂移)
            hit = min(blocks, key=lambda b: min(abs(b[1][0] - line),
                                                abs(b[1][1] - line)))[0]
        cfg.nodes[hit]["anchors"].append(
            {"kind": kind, "var": var, "line": line, "op": op})
    return cfg


def load_callgraph(path: str | Path) -> dict[str, list[str]]:
    """PyCG JSON:模块限定函数名 → 被调函数列表。"""
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def _module_map(app_root: str | Path) -> dict[str, Path]:
    """项目内 .py 文件 → 点分模块名(沿 __init__.py 向上走)。"""
    skip = {"tests", "__pycache__", "golden", "probes", "database"}
    out: dict[str, Path] = {}
    for py in sorted(Path(app_root).rglob("*.py")):
        if any(part in skip for part in py.parts) or py.name in ("adapter.py", "serve.py"):
            continue
        parts = [py.with_suffix("").name]
        parent = py.parent
        while (parent / "__init__.py").exists():
            parts.append(parent.name)
            parent = parent.parent
        out[".".join(reversed(parts))] = py
    return out


def _block_calls_name(stmts: list, name: str) -> bool:
    """块内语句(含嵌套)是否调用了名为 name 的函数(裸名或属性名)。"""
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Name) and f.id == name) or (
                    isinstance(f, ast.Attribute) and f.attr == name
                ):
                    return True
    return False


def _canon(g: str, mod2file: dict[str, Path]) -> tuple[str | None, str]:
    """PyCG 限定名 → (最长已知模块前缀, 末名)。嵌套函数(app.create_app.f)→(app 模块, f)。"""
    parts = g.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        if mod in mod2file:
            return mod, parts[-1]
    return None, parts[-1]


def build_xfg(model: AppModel, callgraph_path: str | Path | None = None) -> nx.MultiDiGraph:
    """G_xfg v2(基于②添加①):PyCG 调用图为骨架,项目函数展开为过程内 CFG,
    库函数为 ext 叶子;调用边下沉到含调用语句的块;锚点钉块;跨端点状态虚边。

    callgraph_path=None → 退化为端点级装配(旧行为,无调用链)。
    """
    xfg = nx.MultiDiGraph()
    cg = load_callgraph(callgraph_path) if callgraph_path else {}
    mod2file = _module_map(model.app_root)
    file2mod = {str(p.resolve()): m for m, p in mod2file.items()}

    # ---- 展开项目函数(端点函数必展;调用图覆盖到的项目函数也展)
    endpoint_of_func: dict[str, tuple[str, Endpoint]] = {}
    for ep in model.endpoints:
        mod = file2mod.get(str(Path(ep.file).resolve()), None)
        if mod:
            endpoint_of_func[f"{mod}.{ep.func}"] = (f"{ep.methods[0]} {ep.route}", ep)

    expanded: dict[str, nx.DiGraph] = {}
    canon_keys = {_canon(g, mod2file) for g in cg}      # 嵌套函数名规范化后的键集合
    for mod, fpath in mod2file.items():
        tree = _module(str(fpath))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualified = f"{mod}.{node.name}"
            is_endpoint = qualified in endpoint_of_func
            in_cg = (mod, node.name) in canon_keys
            if not (is_endpoint or in_cg):
                continue
            cfg_n = build_cfg(node, prefix="", file=str(fpath))
            if is_endpoint:
                attach_anchors(cfg_n, endpoint_of_func[qualified][1])
            for n, d in cfg_n.nodes(data=True):
                xfg.add_node(f"{qualified}::{n}", **d, func_id=qualified)
            for u, v, d in cfg_n.edges(data=True):
                xfg.add_edge(f"{qualified}::{u}", f"{qualified}::{v}",
                             kind="cfg", cond=d.get("cond"))
            entry = next((n for n in cfg_n.nodes
                          if cfg_n.nodes[n].get("kind") == "entry"), None)
            expanded[qualified] = cfg_n
            if entry:
                xfg.nodes[f"{qualified}::{entry}"].setdefault("kind", "entry")

    # ---- 端点动作节点 → 处理器入口
    for qualified, (action, ep) in endpoint_of_func.items():
        xfg.add_node(action, kind="action", func=ep.func, file=ep.file)
        cfg_n = expanded.get(qualified)
        if cfg_n is not None:
            for n in cfg_n.nodes:
                if cfg_n.nodes[n].get("kind") == "entry":
                    xfg.add_edge(action, f"{qualified}::{n}", kind="entry-edge")

    # ---- ② 的调用边下沉到块级(嵌套函数名规范化后匹配)
    for caller_cg, callees in cg.items():
        cmod, cname = _canon(caller_cg, mod2file)
        cfg_c = expanded.get(f"{cmod}.{cname}") if cmod else None
        if cfg_c is None:
            continue                                   # 外部 caller 的边不入图
        for g in callees:
            gmod, gname = _canon(g, mod2file)
            target_cfg = expanded.get(f"{gmod}.{gname}") if gmod else None
            if target_cfg is not None:
                target = next((f"{gmod}.{gname}::{n}" for n in target_cfg
                               if target_cfg.nodes[n].get("kind") == "entry"), None)
                kind = "call"
            else:
                target = g                             # 库函数/未知:叶子
                xfg.add_node(g, kind="ext")
                kind = "call-ext"
            if target is None:
                continue
            sites = [f"{cmod}.{cname}::{b}" for b, d in cfg_c.nodes(data=True)
                     if _block_calls_name(d.get("stmts", []), gname)]
            for s in sites or [f"{cmod}.{cname}::{next(iter(cfg_c.nodes))}"]:
                xfg.add_edge(s, target, kind=kind, callee=g)

    # ---- 跨端点状态虚边(同变量:写锚块 → 读锚块)
    writes: dict[str, list[tuple[str, str]]] = {}
    reads: dict[str, list[tuple[str, str]]] = {}
    for n, d in xfg.nodes(data=True):
        for a in d.get("anchors", []):
            ref = (d.get("func_id"), n)
            if a["kind"] == "eff":
                writes.setdefault(a["var"], []).append(ref)
            elif a["kind"] == "pre":
                reads.setdefault(a["var"], []).append(ref)
    for var in set(writes) & set(reads):
        for (wa, wb), (ra, rb) in itertools.product(writes[var], reads[var]):
            if wa != ra:
                xfg.add_edge(wb, rb, kind="state-link", var=var)
    return xfg


def xfg_to_dot(xfg: nx.MultiDiGraph, path: str | Path) -> None:
    lines = ["digraph XFG {", "  rankdir=LR;", '  node [fontname="Helvetica"];']
    for n, d in xfg.nodes(data=True):
        if d.get("kind") == "action":
            lines.append(f'  "{n}" [shape=box, style=bold];')
        elif d.get("kind") in ("branch",):
            lines.append(f'  "{n}" [shape=diamond];')
        elif d.get("kind") == "ext":
            lines.append(f'  "{n}" [shape=rect, style=filled, fillcolor=gray90];')
        elif d.get("kind") == "entry":
            lines.append(f'  "{n}" [shape=circle];')
        elif d.get("kind") == "block":
            marked = ' color=red' if d.get("anchors") else ""
            lines.append(f'  "{n}" [shape=rect{marked}];')
        else:
            continue
    for u, v, k, d in xfg.edges(keys=True, data=True):
        kind = d.get("kind", "")
        if kind == "cfg":
            cond = d.get("cond")
            attr = f' [label="{cond}"]' if cond else ""
        elif kind == "state-link":
            attr = f' [style=dashed, color=blue, label="{d.get("var")}"]'
        elif kind == "entry-edge":
            attr = " [style=bold]"
        elif kind == "call":
            attr = f' [color=purple, label="call"]'
        elif kind == "call-ext":
            attr = ' [color=gray, style=dotted]'
        else:
            attr = ""
        lines.append(f'  "{u}" -> "{v}"{attr};')
    lines.append("}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
