"""Build a project-level interprocedural CFG (ICFG)."""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.core.ir import ProgramIR, FlowEdge
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
                cfg = build_function_cfg(node, str(path), module)
                if not include_all_functions and callgraph and qname not in callgraph:
                    continue
                ir.functions[qname] = cfg.function
                ir.blocks.update(cfg.blocks)
                for edge in cfg.edges:
                    ir.add_edge(edge)

    # Resolve calls from real AST call sites.  A call edge is anchored at the
    # block containing the Call node; an explicit return edge goes to every
    # normal successor of that callsite block.
    unresolved: list[dict[str, object]] = []
    for qname, fn in list(ir.functions.items()):
        module, _, fname = qname.rpartition(".")
        node = ast_functions.get((module, fname))
        if node is None:
            continue
        for block_id in fn.blocks:
            block = ir.blocks[block_id]
            lines = set(block.line_numbers)
            calls = [n for n in _iter_owned_calls(node)
                     if getattr(n, "lineno", 0) in lines]
            for call in calls:
                callee_short = _callee_name(call)
                callee = _resolve_callee(callee_short, module, ast_functions, aliases)
                if callee is None or callee not in ir.functions or callee == qname:
                    unresolved.append({
                        "caller": qname,
                        "callee": callee_short,
                        "line": getattr(call, "lineno", block.start_line),
                    })
                    continue
                target = ir.functions[callee]
                line = getattr(call, "lineno", block.start_line)
                ir.add_edge(FlowEdge(block_id, target.entry, "call", callsite_line=line,
                                     callee=callee))
                continuations = [out.dst for out in ir.outgoing(block_id)
                                 if out.kind in ("flow", "true", "false")
                                 and out.dst != target.entry]
                # A call inside ``return helper()`` has no CFG successor in
                # the caller.  Its natural continuation is the caller exit.
                if not continuations:
                    continuations = [fn.exit]
                for continuation in continuations:
                    ir.add_edge(FlowEdge(target.exit, continuation, "return",
                                         callsite_line=line, callee=callee))
    ir.metadata["callgraph_supplied"] = callgraph is not None
    ir.metadata["function_count"] = len(ir.functions)
    ir.metadata["block_count"] = len(ir.blocks)
    ir.metadata["edge_count"] = len(ir.edges)
    ir.metadata["unresolved_calls"] = unresolved
    return ir


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
