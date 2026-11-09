"""Build a project-level interprocedural CFG (ICFG)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from stateforge.core.ir import ProgramIR, FlowEdge, BasicBlock
from stateforge.analysis.static.cfg import build_function_cfg

_SKIP = {"tests", "__pycache__", "migrations", "node_modules", ".git", "golden",
         ".venv", "venv", ".tox", ".eggs", "site-packages"}

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
        # Exclude descendants of the selected root, not its ancestors. An
        # explicitly selected application can itself live in site-packages.
        if not any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
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
    function_nodes = {}
    block_nodes = {}
    for path in _files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        module = _module_name(root, path)
        for node, class_path in _walk_with_class(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # F2(封版):方法按类限定命名(module.Class.method)——同名方法
                # 不合并;对它们的裸名/属性名调用解析不出→unresolved(不造假边)
                short = ".".join(class_path + [node.name]) if class_path else node.name
                qname = f"{module}.{short}"
                ast_functions[(module, short)] = node
                aliases.setdefault(short, []).append(qname)
                rel_id = str(path.relative_to(root))
                cfg = build_function_cfg(node, str(path), module, id_file=rel_id)
                if not include_all_functions and callgraph and qname not in callgraph:
                    continue
                function_nodes[qname] = (module, node)
                block_nodes.update(cfg.ast_nodes)
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
        module, node = function_nodes[qname]
        fname = node.name
        # Associate calls with their owning AST block, including multiline
        # expressions; statement line ranges cannot establish AST ownership.
        for host in list(fn.blocks):
            roots = block_nodes.get(host, [])
            calls = []
            for expression in roots:
                for call in _owned_expression_calls(expression):
                    callee_short = _callee_name(call)
                    callee = _resolve_callee(callee_short, module, ast_functions, aliases)
                    if callee is None and callee_short and callgraph is not None:
                        callee = _resolve_via_callgraph(qname, callee_short, ir, callgraph)
                    if callee is None or callee not in ir.functions:
                        unresolved.append({"caller": qname, "callee": callee_short,
                                           "line": call.lineno})
                    else:
                        calls.append((call, callee))
            if not calls:
                continue
            calls.sort(key=lambda item: (item[0].lineno, item[0].col_offset))
            file = ir.blocks[host].file
            id_file = str(Path(file).relative_to(root))
            sites = {}
            for n, (call, callee) in enumerate(calls, start=1):
                line = call.lineno
                cs = f"{id_file}::{fname}::{line}-{line}::callsite::{n}"
                rs = f"{id_file}::{fname}::{line}-{line}::returnsite::{n}"
                # Distinct blocks can contain separate same-line suites.  Keep
                # IDs paired while avoiding accidental cross-host replacement.
                while cs in ir.blocks:
                    n += len(calls)
                    cs = f"{id_file}::{fname}::{line}-{line}::callsite::{n}"
                    rs = f"{id_file}::{fname}::{line}-{line}::returnsite::{n}"
                ir.blocks[cs] = BasicBlock(cs, file, module, fname, line, line,
                                           "callsite", [f"<call> {callee}"], [line])
                ir.blocks[rs] = BasicBlock(rs, file, module, fname, line, line,
                                           "returnsite", [f"<ret> {callee}"], [line])
                fn.blocks.extend([cs, rs])
                sites[call] = (cs, rs, callee)
                ir.add_edge(FlowEdge(cs, ir.functions[callee].entry, "call",
                                     callsite_line=line, callee=callee))
                ir.add_edge(FlowEdge(ir.functions[callee].exit, rs, "return",
                                     callsite_line=line, callee=callee))
            succs = [out for out in ir.outgoing(host)
                     if out.kind in ("flow", "true", "false")]
            for out in succs:
                # A with header is also observed during cleanup. Re-evaluating
                # its context expression then would be a false mandatory call;
                # retain direct cleanup continuations conservatively.
                if ir.blocks[host].kind != "with":
                    ir.remove_edge(host, out.dst, out.kind)
            flow = _ExpressionCalls(ir, sites)
            ports = {(host, "flow")}
            truth = (ports, ports)
            for expression in roots:
                truth = flow.build(expression, ports)
                ports = flow.merge(*truth)
            for out in succs or [FlowEdge(host, fn.exit)]:
                exits = (truth[0] if out.kind == "true" else truth[1]
                         if out.kind == "false" else ports)
                for src, kind in sorted(exits):
                    ir.add_edge(FlowEdge(src, out.dst,
                                         out.kind if out.kind != "flow" else kind,
                                         condition=out.condition))
    ir.metadata["callgraph_supplied"] = callgraph is not None
    ir.metadata["function_count"] = len(ir.functions)
    ir.metadata["block_count"] = len(ir.blocks)
    ir.metadata["edge_count"] = len(ir.edges)
    ir.metadata["unresolved_calls"] = unresolved
    ir.metadata["source_hash"] = _source_hash(root)
    return ir


def _owned_expression_calls(node):
    if isinstance(node, _OWNED_STOP):
        return
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _owned_expression_calls(child)


class _ExpressionCalls:
    """Connect observable call sites without inventing untraceable gate nodes.

    Ports retain the outcome of a short-circuit decision until the next call
    or CFG continuation. Unknown non-call truth values conservatively allow
    both outcomes; mutually exclusive IfExp arms never become a serial chain.
    """

    def __init__(self, ir, sites):
        self.ir, self.sites = ir, sites

    @staticmethod
    def merge(*groups):
        ports = set().union(*groups)
        # If both outcomes reach the same continuation, it is unconditional.
        for src, _ in list(ports):
            if (src, "flow") in ports or ({(src, "true"), (src, "false")} <= ports):
                ports.difference_update({(src, "true"), (src, "false")})
                ports.add((src, "flow"))
        return ports

    @staticmethod
    def tag(ports, kind):
        return {(src, kind) for src, _ in ports}

    def build(self, node, incoming):
        if not incoming:
            return set(), set()
        if isinstance(node, _OWNED_STOP):
            return incoming, incoming
        if isinstance(node, ast.BoolOp):
            active = incoming
            skipped = set()
            is_and = isinstance(node.op, ast.And)
            for value in node.values[:-1]:
                yes, no = self.build(value, active)
                skipped |= self.tag(no if is_and else yes,
                                    "false" if is_and else "true")
                active = self.tag(yes if is_and else no,
                                  "true" if is_and else "false")
            yes, no = self.build(node.values[-1], active)
            return ((self.tag(yes, "true"), skipped | self.tag(no, "false"))
                    if is_and else
                    (skipped | self.tag(yes, "true"), self.tag(no, "false")))
        if isinstance(node, ast.IfExp):
            yes, no = self.build(node.test, incoming)
            body_yes, body_no = self.build(node.body, self.tag(yes, "true"))
            else_yes, else_no = self.build(node.orelse, self.tag(no, "false"))
            return self.merge(body_yes, else_yes), self.merge(body_no, else_no)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yes, no = self.build(node.operand, incoming)
            return no, yes
        ports = incoming
        # AST field order matches ordinary expression evaluation (callee and
        # arguments before the call itself). Definitions are ownership stops.
        for child in ast.iter_child_nodes(node):
            ports = self.merge(*self.build(child, ports))
        if node in self.sites:
            cs, rs, callee = self.sites[node]
            for src, kind in sorted(ports):
                self.ir.add_edge(FlowEdge(src, cs, kind,
                                         callsite_line=node.lineno, callee=callee))
            ports = {(rs, "flow")}
        if isinstance(node, ast.Constant):
            return (ports, set()) if bool(node.value) else (set(), ports)
        return ports, ports


def _walk_with_class(tree: ast.Module):
    """Yield (node, class_path);class_path = 直接外层 ClassDef 名链(仅直接)。"""
    stack = [(node, []) for node in ast.iter_child_nodes(tree)]
    while stack:
        node, path = stack.pop()
        yield node, path
        child_path = path + [node.name] if isinstance(node, ast.ClassDef) else path
        stack.extend((child, child_path)
                     for child in ast.iter_child_nodes(node))


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
