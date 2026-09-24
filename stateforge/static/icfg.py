"""Build a project-level interprocedural CFG (ICFG)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from stateforge.core.ir import ProgramIR, FlowEdge, BasicBlock
from stateforge.static.cfg import build_function_cfg

_SKIP = {"tests", "__pycache__", "migrations", "node_modules", ".git", "golden"}

_OWNED_STOP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _iter_owned_calls(fn_node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield ast.Call nodes belonging to *fn_node* itself.

    Ownership (spec P1-1): never descend into nested function/lambda/class
    bodies — those calls belong to their own FunctionIR.
    """
    stack = list(fn_node.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _OWNED_STOP):
            continue                      # 嵌套定义体:整棵不入内
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if not any(part in _SKIP for part in path.parts):
            yield path


def _source_hash(root: Path) -> str:
    """P1-7:目标源指纹(path:size:mtime),缓存失效判据。"""
    parts = []
    for path in _files(root):
        st = path.stat()
        parts.append(f"{path}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def build_icfg(app_root: str | Path, callgraph: dict[str, list[str]] | None = None,
               include_all_functions: bool = True) -> ProgramIR:
    root = Path(app_root).resolve()
    ir = ProgramIR(metadata={"root": str(root), "kind": "icfg", "version": 1})
    ast_functions: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    aliases: dict[str, list[str]] = {}
    for path in _files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        module = _module_name(root, path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{module}.{node.name}"
                ast_functions[(module, node.name)] = node
                aliases.setdefault(node.name, []).append(qname)
                rel_id = str(path.relative_to(root))
                cfg = build_function_cfg(node, str(path), module, id_file=rel_id)
                if not include_all_functions and callgraph and qname not in callgraph:
                    continue
                ir.functions[qname] = cfg.function
                ir.blocks.update(cfg.blocks)
                for edge in cfg.edges:
                    ir.add_edge(edge)

    # Resolve calls from real AST call sites (spec P1-2).  Internal calls in
    # a block are serialized as a synthetic chain so execution order and the
    # caller continuation are explicit:
    #   B -flow-> CS#1 -call-> callee1.entry ... callee1.exit -return-> RS#1
    #   RS#1 -flow-> CS#2 ... RS#N -(原 kind)-> succ(B)
    unresolved: list[dict[str, object]] = []
    for qname, fn in list(ir.functions.items()):
        module, _, fname = qname.rpartition(".")
        node = ast_functions.get((module, fname))
        if node is None:
            continue
        block_calls: dict[str, list[tuple[int, int, str]]] = {}
        for call in _iter_owned_calls(node):
            callee_short = _callee_name(call)
            callee = _resolve_callee(callee_short, module, ast_functions, aliases)
            if callee is None and callee_short and callgraph is not None:
                # H3(评审§13):名字解析失败/歧义时,用预生成调用图
                # (PyCG 在 ANOTA 侧预出 JSON,驱动侧 py3.13 不可运行)消歧
                callee = _resolve_via_callgraph(qname, callee_short,
                                                ir, callgraph)
            line = getattr(call, "lineno", 0)
            if callee is None or callee not in ir.functions:
                unresolved.append({"caller": qname, "callee": callee_short,
                                   "line": line})
                continue
            host = next((bid for bid in fn.blocks
                         if line in ir.blocks[bid].line_numbers), None)
            if host is None:
                unresolved.append({"caller": qname, "callee": callee_short,
                                   "reason": "no host block", "line": line})
                continue
            col = getattr(call, "col_offset", 0)
            block_calls.setdefault(host, []).append((line, col, callee))

        for host, calls in block_calls.items():
            calls.sort(key=lambda t: (t[0], t[1]))   # H4:同行按 col_offset
            succs = [out for out in ir.outgoing(host)
                     if out.kind in ("flow", "true", "false")]
            continuations = succs or []          # return helper() 的空后继→exit
            for out in succs:
                ir.remove_edge(host, out.dst, out.kind)
            file = ir.blocks[host].file
            # H6:合成节点 ID 用相对路径(与块 ID 一致,跨机器稳定)
            root_str = str(root)
            id_file = file[len(root_str) + 1:] if file.startswith(root_str + "/") else file
            prev = host
            chain_blocks: list[str] = []
            for n, (line, _col, callee) in enumerate(calls, start=1):
                cs = f"{id_file}::{fname}::{line}-{line}::callsite::{n}"
                rs = f"{id_file}::{fname}::{line}-{line}::returnsite::{n}"
                ir.blocks[cs] = BasicBlock(cs, file, module, fname, line, line,
                                           "callsite", [f"<call> {callee}"], [line])
                ir.blocks[rs] = BasicBlock(rs, file, module, fname, line, line,
                                           "returnsite", [f"<ret> {callee}"], [line])
                chain_blocks.extend([cs, rs])
                ir.add_edge(FlowEdge(prev, cs, "flow", callsite_line=line,
                                     callee=callee))
                ir.add_edge(FlowEdge(cs, ir.functions[callee].entry, "call",
                                     callsite_line=line, callee=callee))
                ir.add_edge(FlowEdge(ir.functions[callee].exit, rs, "return",
                                     callsite_line=line, callee=callee))
                prev = rs
            if continuations:
                for out in continuations:       # 保留原 true/false 标注
                    ir.add_edge(FlowEdge(prev, out.dst, out.kind,
                                         condition=out.condition))
            else:
                ir.add_edge(FlowEdge(prev, fn.exit, "flow"))
            fn.blocks.extend(chain_blocks)
    ir.metadata["callgraph_supplied"] = callgraph is not None
    ir.metadata["function_count"] = len(ir.functions)
    ir.metadata["block_count"] = len(ir.blocks)
    ir.metadata["edge_count"] = len(ir.edges)
    ir.metadata["unresolved_calls"] = unresolved
    ir.metadata["source_hash"] = _source_hash(root)
    return ir


def _resolve_via_callgraph(caller: str, short: str | None, ir: ProgramIR,
                           callgraph: dict[str, list[str]]) -> str | None:
    """按调用图边消歧:caller 的 callee 候选中 short 名匹配且唯一者胜出。

    PyCG 产物的 qname 可能带仓库前缀(如 ``vulnshop.app.a.handler``),用
    后缀匹配桥接到本 IR 的函数名;歧义或无命中返回 None(保持 unresolved)。
    """
    if not short:
        return None
    known = set(ir.functions)
    callees: set[str] = set()
    for key, targets in callgraph.items():
        if key != caller and not key.endswith("." + caller):
            continue
        for target in targets:
            tname = target.rsplit(".", 1)[-1]
            if tname != short:
                continue
            if target in known:
                callees.add(target)
            else:                       # 仓库前缀形态:target 以我们的 qname 结尾
                for fn in known:
                    if target == fn or target.endswith("." + fn):
                        callees.add(fn)
    return callees.pop() if len(callees) == 1 else None


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _resolve_callee(short: str | None, module: str, funcs, aliases) -> str | None:
    if not short:
        return None
    direct = (module, short)
    if direct in funcs:
        return f"{module}.{short}"
    candidates = [f"{m}.{n}" for (m, n) in funcs if n == short]
    if len(candidates) == 1:
        return candidates[0]
    aliased = aliases.get(short, [])
    return aliased[0] if len(aliased) == 1 else None
