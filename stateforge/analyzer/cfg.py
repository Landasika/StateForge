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
        linenos = [s.lineno for s in stmts] or [func.lineno]
        lines = (min(linenos), max(linenos))
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
    """PyCG 限定名 → (已知模块, 末名)。嵌套函数(app.create_app.f)→(app 模块, f)。

    两种匹配:①前缀精确;②后缀匹配(≥2 段防歧义)——桥接器与模块图的计算根
    不一致时(PyCG 从 targets/ 跑出 vulnshop.app.*,_module_map 上溯出
    stateforge.targets.vulnshop.app),后缀匹配把二者对上。
    """
    parts = g.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        if mod in mod2file:
            return mod, parts[-1]
    node_mod, name = ".".join(parts[:-1]), parts[-1]
    if "." in node_mod:
        for m in mod2file:
            if m.endswith("." + node_mod):
                return m, name
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


# ---------------------------------------------------------------- 数据流层(T+11)

def _block_defs(stmts: list) -> set[str]:
    """块内定义的名字(Assign/AnnAssign/AugAssign 目标、For 目标)。"""
    out: set[str] = set()
    for s in stmts:
        targets: list = []
        if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = s.targets if isinstance(s, ast.Assign) else [s.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                out.update(e.id for e in t.elts if isinstance(e, ast.Name))
    return out


def _block_uses(stmts: list) -> set[str]:
    """块内读取的名字(Name 载荷;近似不含定义点自读)。"""
    out: set[str] = set()
    for s in stmts:
        for node in ast.walk(s):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                out.add(node.id)
    return out


def reaching_definitions(g: nx.DiGraph) -> dict:
    """经典 may 到达定值:in[b] = ∪ out[pred];out[b] = in[b] 覆盖后并入本块定义。"""
    defs = {n: _block_defs(d.get("stmts", [])) for n, d in g.nodes(data=True)}
    live: dict[str, set] = {n: set() for n in g.nodes}
    out: dict[str, set] = {}
    changed = True
    while changed:
        changed = False
        for n in g.nodes:
            new_in: set = set()
            for p in g.predecessors(n):
                for name, where in out.get(p, set()):
                    if name not in defs[n]:
                        new_in.add((name, where))
            new_out = new_in | {(name, n) for name in defs[n]}
            if new_in != live[n] or new_out != out.get(n, set()):
                live[n], out[n] = changed_flag = new_in, new_out  # noqa: F841
                changed = True
    return {"in": live, "out": out}


def _stmt_text(stmt) -> str:
    try:
        return ast.unparse(stmt)
    except Exception:  # noqa: BLE001
        return ""


def _block_text_at(d: dict, line: int | None = None) -> str:
    stmts = d.get("stmts", [])
    if line is not None:
        stmts = [s for s in stmts if getattr(s, "lineno", None) == line] or stmts
    return "; ".join(_stmt_text(s) for s in stmts)


def _param_source_vars(g: nx.DiGraph, param_names: list[str]) -> set[str]:
    """请求源变量集:直接从参数提取定义的变量,经赋值链传播(不动点)。"""
    import re as _re

    def stmt_defines_from(s, p: str) -> str | None:
        """语句若把 p(参数)定义进某变量,返回该变量名。"""
        if not isinstance(s, (ast.Assign, ast.AnnAssign)):
            return None
        targets = s.targets if isinstance(s, ast.Assign) else [s.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            return None
        text = _stmt_text(s)
        # 直接形态:amount = body.get("amount") / request_data["amount"]
        if _re.search(rf"['\"]{ _re.escape(p)}['\"]|\b{_re.escape(p)}\b", text):
            return names[0]
        return None

    sources: set[str] = set()
    changed = True
    while changed:
        changed = False
        for _n, d in g.nodes(data=True):
            for s in d.get("stmts", []):
                text = _stmt_text(s)
                for p in param_names:
                    var = stmt_defines_from(s, p)
                    if var and var not in sources:
                        sources.add(var)
                        changed = True
                # 传播:RHS 提及源变量的赋值,其目标也是源
                if isinstance(s, (ast.Assign, ast.AnnAssign)):
                    targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                    names = [t.id for t in targets if isinstance(t, ast.Name)]
                    if names and any(_re.search(rf"\b{v}\b", text) for v in sources
                                     if v not in names) and names[0] not in sources:
                        # 目标名出现在 RHS 之外(它本身就是目标),用 RHS 文本判断
                        rhs = text.split("=", 1)[-1] if "=" in text else text
                        if any(_re.search(rf"\b{v}\b", rhs) for v in sources if v != names[0]):
                            sources.add(names[0])
                            changed = True
    return sources


def anchor_param_influence(g: nx.DiGraph, ep) -> dict[str, list[dict]]:
    """参数 → 其影响的锚点(直接出现在锚语句,或经到达定值的定义链)。"""
    import re as _re

    params = [p.name for p in ep.params if p.location in ("json", "query")]
    if not params:
        return {}
    sources = _param_source_vars(g, params)
    rd = reaching_definitions(g)["in"]
    result: dict[str, list[dict]] = {}
    for n, d in g.nodes(data=True):
        for anchor in d.get("anchors", []):
            text = _block_text_at(d, anchor.get("line"))
            used = _block_uses(d.get("stmts", []))
            # 到达该块的定义文本(传递源)
            def_texts = " ; ".join(
                _block_text_at(g.nodes[where]) for (name, where) in rd.get(n, set())
            )
            combined = f"{text} ; {def_texts}"
            for p in params:
                direct = _re.search(rf"['\"]{p}['\"]|\b{p}\b", text)
                transitive = any(_re.search(rf"\b{v}\b", combined) for v in sources)
                if (direct and p in sources | set(params)) or (transitive and p in sources):
                    result.setdefault(p, []).append(anchor)
    return result


def block_gates(g: nx.DiGraph) -> dict[str, list[dict]]:
    """块级门条件(may 语义):**某条**入口→b 路径上可能拦截的 pre/guard 锚。

    为什么不是支配者(must):状态门常是条件性的(如 `if GUARDS:` 包住的检查)——
    守卫分支并非必经,但它仍是"可能挡住这块代码的门"。查询语义
    "哪道门可能挡着这个未覆盖区域"要的正是 may。
    """
    nodes = [n for n in g.nodes if g.nodes[n].get("kind") != "missing"]
    if not nodes:
        return {}
    entry = next((n for n in nodes if g.nodes[n].get("kind") == "entry"), nodes[0])
    forward: set = set()

    def _fr(n, seen):
        if n in seen:
            return
        seen.add(n)
        for s in g.successors(n):
            _fr(s, seen)

    _fr(entry, forward)
    gates: dict[str, list[dict]] = {}
    for b in nodes:
        if b not in forward:
            continue
        # 反向可达(含多前驱汇合)∩ 正向可达 - 自身 = 某条入口→b 路径上的块
        rr: set = set()
        stack = [b]
        while stack:
            n = stack.pop()
            for p in g.predecessors(n):
                if p not in rr:
                    rr.add(p)
                    stack.append(p)
        path_blocks = (rr & forward) - {b}
        anchors = []
        for m in path_blocks:
            for a in g.nodes[m].get("anchors", []):
                if a["kind"] in ("pre", "guard"):
                    anchors.append(a)
        if anchors:
            gates[b] = anchors
    return gates


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
