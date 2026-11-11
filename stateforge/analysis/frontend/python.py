"""Python frontend: Python source -> CFG/ICFG materialised on a ProgramIR.

Responsibilities that are language specific live here:

* discovering and parsing ``.py`` files;
* building per-function control flow (:mod:`stateforge.analysis.static.cfg`);
* turning Python expression structure (short-circuit ``and``/``or``, conditional
  expressions, ownership of nested definitions) into explicit call chains;
* reporting :class:`~stateforge.analysis.facts.CallSiteFact` facts.

Call-target resolution is *not* here.  It is delegated to the language-neutral
:class:`~stateforge.analysis.solver.callgraph.NameCallSolver`, so adding another
language means writing another frontend, not another resolver.

The scan covers the application tree.  A dependency slice can be added on
request (``build_python_icfg(..., deps=True)``): the installed modules the
application can actually reach, discovered by :mod:`stateforge.analysis.
frontend.pyslice`.  Without it the IR stops at the application boundary, which
is what coverage wants but not what route attribution needs — a Django view
resolved from a URLconf may be defined in ``site-packages``.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Iterable

from stateforge.analysis.facts import (
    EXPR,
    MODULE,
    NONE,
    SELF,
    TYPE_ANNOTATION,
    TYPE_ASSIGNMENT,
    UNKNOWN,
    CallSiteFact,
    ClassFact,
    FunctionFact,
    ImportFact,
    ProgramFacts,
    TypeFact,
)
from stateforge.analysis.frontend.pyslice import Slice, slice_modules
from stateforge.analysis.solver.callgraph import NameCallSolver
from stateforge.analysis.static.cfg import build_function_cfg
from stateforge.core.ir import BasicBlock, FlowEdge, ProgramIR

_SKIP = {"tests", "__pycache__", "migrations", "node_modules", ".git", "golden",
         ".venv", "venv", ".tox", ".eggs", "site-packages"}

_OWNED_STOP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Upper bound on installed modules in one dependency slice.  A library whose
#: ``__init__`` re-exports its whole API can pull in a large closure; the cap
#: keeps a single build finite and is reported in ``metadata["deps"]``.
DEPS_MAX_MODULES = 4000


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
    """Dotted module path as an import statement would spell it.

    The name starts at the outermost directory below *root* that is a package.
    Without that, a ``src/`` layout yields ``src.pkg.mod`` while every import in
    the program says ``pkg.mod``, so no import ever matches a declaration.
    Roots with no packages at all (test fixtures) keep the plain relative path.
    """
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    for index in range(max(len(parts) - 1, 0)):
        if (root.joinpath(*parts[: index + 1]) / "__init__.py").is_file():
            parts = parts[index:]
            break
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _files(root: Path):
    for path in sorted(root.rglob("*.py")):
        # Exclude descendants of the selected root, not its ancestors. An
        # explicitly selected application can itself live in site-packages.
        if not any(part in _SKIP for part in path.relative_to(root).parts[:-1]):
            yield path


def _app_sources(root: Path) -> dict[str, Path]:
    """Dotted module name -> file, for the application tree under *root*."""
    out: dict[str, Path] = {}
    for path in _files(root):
        out.setdefault(_module_name(root, path), path)
    return out


def _dependency_slice(root: Path, extra_roots: Iterable[Path | str] = ()
                      ) -> Slice:
    """Installed modules reachable from the application under *root*."""
    return slice_modules(root, _app_sources(root), extra_roots,
                         max_files=DEPS_MAX_MODULES)


def _load_deps(root: Path, deps: bool | Iterable[Path | str]) -> Slice:
    """Resolve the ``deps`` argument into a slice (empty when disabled)."""
    if deps is False or deps is None:
        return Slice(modules={}, roots=())
    if deps is True:
        return _dependency_slice(root)
    if isinstance(deps, (str, Path)):
        return _dependency_slice(root, [deps])
    return _dependency_slice(root, list(deps))


def _owning_root(path: Path, slice_info: Slice) -> Path:
    """The directory *path*'s relative identity is taken against.

    For an installed module that is the ``site-packages`` directory it was
    found in, so its block IDs read ``wagtail/admin/views/workflows.py::…``
    rather than repeating the venv path.
    """
    for site in slice_info.roots:
        try:
            Path(path).relative_to(site)
        except ValueError:
            continue
        return site
    return Path(path).parent


def _relative_id(base: Path, path: Path) -> str:
    """Block-ID file component: relative when possible, absolute otherwise."""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


def _source_hash(root: Path, extra_files: Iterable[Path] = ()) -> str:
    """P1-7:目标源指纹(path:size:mtime),缓存失效判据。

    *extra_files* extends the fingerprint to the dependency slice: when the
    IR contains installed code, a changed library is a changed input.
    """
    parts = []
    for path in [*_files(root), *sorted(set(extra_files))]:
        st = path.stat()
        parts.append(f"{path}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _annotation_text(node: ast.expr | None) -> str | None:
    """Best-effort textual annotation; never fails a build over syntax."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:                     # pragma: no cover - defensive only
        return None


def _annotation_type_name(node: ast.expr | None) -> str | None:
    """The simple type name an annotation denotes, when that is unambiguous.

    Only plain names and dotted names qualify.  Subscripts are skipped on
    purpose: ``list[User]`` does not mean "this value is a User", and treating
    it as one would create method edges against the wrong receiver.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _base_name(node: ast.expr) -> str:
    """Base-class name as written, for local-hierarchy lookups."""
    name = _annotation_type_name(node)
    if name is not None:
        return name
    return _annotation_text(node) or ""


def _owned_nodes(fn_node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Every node owned by *fn_node*, never descending into nested definitions."""
    stack = list(fn_node.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _OWNED_STOP):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _package_of(module: str) -> str:
    """The package a module lives in (``a.b.c`` -> ``a.b``; ``a.b.__init__`` -> ``a.b``)."""
    parts = module.split(".")
    if parts and parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _resolve_relative(module: str, level: int, target: str | None) -> str | None:
    """Absolute module path for a relative ``ImportFrom``."""
    if level <= 0:
        return target
    parts = module.split(".")
    if parts and parts[-1] == "__init__":
        base = parts[:-1]
        drop = level - 1
    else:
        base = parts[:-1]
        drop = level
    if drop > len(base):
        return None
    resolved = base[: len(base) - drop]
    if target:
        resolved = resolved + target.split(".")
    return ".".join([p for p in resolved if p])


def _import_facts(module: str, tree: ast.Module) -> list[ImportFact]:
    """Bindings an import statement creates, as ``local -> target module``."""
    out: list[ImportFact] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                out.append(ImportFact(scope=module, local=local, target=target))
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_relative(module, node.level or 0, node.module)
            if resolved is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                # ``from a.b import c`` binds ``c`` to the qualified path
                # ``a.b.c``: it may be a function there, or a submodule that
                # the call site then dereferences as ``c.d()``.
                target = f"{resolved}.{alias.name}" if resolved else alias.name
                out.append(ImportFact(scope=module, local=alias.asname or alias.name,
                                      target=target))
    return out


def _constructor_assignments(fn_node) -> list[tuple[str, str]]:
    """``x = ClassName(...)`` bindings that are safe to treat as a type.

    Only single-assignment symbols qualify: rebinding would make a
    flow-insensitive fact wrong, and a wrong type manufactures a wrong call
    edge, which is worse than leaving the call unresolved.
    """
    counts: dict[str, int] = {}
    candidate: dict[str, str] = {}
    for node in _owned_nodes(fn_node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        counts[target.id] = counts.get(target.id, 0) + 1
        if isinstance(node.value, ast.Call):
            callee = _callee_name(node.value)
            if callee:
                candidate[target.id] = callee
    return [(name, cls) for name, cls in candidate.items() if counts.get(name) == 1]


def _receiver_kind(call: ast.Call) -> tuple[str, str | None]:
    """Classify the syntactic receiver of a call (not a type judgement)."""
    func = call.func
    if isinstance(func, ast.Name):
        return NONE, func.id
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls"):
                return SELF, base.id
            return MODULE, base.id
        return EXPR, None
    return UNKNOWN, None


def build_python_icfg(app_root: str | Path,
                      callgraph: dict[str, list[str]] | None = None,
                      include_all_functions: bool = True,
                      dynamic: dict[tuple[str, int], str] | None = None,
                      deps: bool | Iterable[Path | str] = False) -> ProgramIR:
    """Build the ICFG for the Python sources under *app_root*.

    ``deps`` adds the dependency slice: ``True`` discovers the virtualenvs
    under the root, a path (or list of paths) names them explicitly — either a
    virtualenv directory or a ``site-packages`` directory.  Sliced files keep
    relative block IDs against their own root, so an ID never embeds a path
    that only exists on this machine.
    """
    root = Path(app_root).resolve()
    ir = ProgramIR(metadata={"root": str(root), "kind": "icfg", "version": 1})
    slice_info = _load_deps(root, deps)
    # Every file to parse, paired with the root its relative paths are taken
    # against: the application against *root*, installed code against its own
    # site-packages.  Application files come first and win name collisions —
    # an application module shadows the installed module of the same name, as
    # ``sys.path`` ordering makes it at run time.
    app_files = [(p, root) for p in _files(root)]
    shadowed = {_module_name(root, p) for p, _ in app_files}
    sources: list[tuple[Path, Path]] = list(app_files)
    for name in sorted(slice_info.modules):
        if name in shadowed:
            continue
        path = slice_info.modules[name]
        sources.append((path, _owning_root(path, slice_info)))
    facts = ProgramFacts()
    function_nodes = {}
    block_nodes = {}
    file_bases: dict[str, Path] = {}
    for path, base in sources:
        file_bases[str(path)] = base
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        module = _module_name(base, path)
        for fact in _import_facts(module, tree):
            facts.add_import(fact)
        for node, class_path in _walk_with_class(tree):
            if isinstance(node, ast.ClassDef):
                facts.add_class(ClassFact(
                    qname=".".join([module, *class_path, node.name]),
                    module=module, name=node.name,
                    bases=tuple(_base_name(b) for b in node.bases),
                    methods=tuple(n.name for n in node.body
                                  if isinstance(n, (ast.FunctionDef,
                                                    ast.AsyncFunctionDef))),
                ))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # F2(封版):方法按类限定命名(module.Class.method)——同名方法
                # 不合并;对它们的裸名/属性名调用解析不出→unresolved(不造假边)
                short = ".".join(class_path + [node.name]) if class_path else node.name
                qname = f"{module}.{short}"
                facts.add_function(FunctionFact(
                    qname=qname, module=module, name=node.name,
                    class_path=tuple(class_path), file=str(path),
                    start_line=getattr(node, "lineno", None),
                    end_line=getattr(node, "end_lineno", None),
                    params=tuple(a.arg for a in node.args.args),
                    return_type=_annotation_text(node.returns),
                ))
                # Parameter annotations are the cheapest real type evidence in
                # Python: record them so ``u.method()`` can find the class.
                every_arg = (list(getattr(node.args, "posonlyargs", []))
                             + list(node.args.args)
                             + list(node.args.kwonlyargs))
                for arg in every_arg:
                    type_name = _annotation_type_name(arg.annotation)
                    if type_name:
                        facts.add_type_fact(TypeFact(
                            scope=qname, symbol=arg.arg,
                            type_name=type_name, source=TYPE_ANNOTATION))
                # ``x = ClassName(...)`` is the other cheap, safe type source.
                for symbol, type_name in _constructor_assignments(node):
                    facts.add_type_fact(TypeFact(
                        scope=qname, symbol=symbol,
                        type_name=type_name, source=TYPE_ASSIGNMENT))
                rel_id = _relative_id(base, path)
                cfg = build_function_cfg(node, str(path), module, id_file=rel_id)
                if not include_all_functions and callgraph and qname not in callgraph:
                    continue
                function_nodes[qname] = (module, node)
                block_nodes.update(cfg.ast_nodes)
                ir.functions[qname] = cfg.function
                ir.blocks.update(cfg.blocks)
                for edge in cfg.edges:
                    ir.add_edge(edge)

    # Resolution is delegated to the language-neutral solver: this frontend
    # only reports what the syntax shows (spec P1-2).
    solver = NameCallSolver(facts, known=set(ir.functions), callgraph=callgraph,
                            dynamic=dynamic)
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
                    receiver, receiver_symbol = _receiver_kind(call)
                    resolution = solver.resolve(CallSiteFact(
                        caller=qname, callee_name=callee_short, line=call.lineno,
                        column=call.col_offset, host_block=host,
                        receiver=receiver, receiver_symbol=receiver_symbol))
                    if not solver.is_usable(resolution):
                        unresolved.append({"caller": qname, "callee": callee_short,
                                           "line": call.lineno})
                    else:
                        calls.append((call, resolution.callee))
            if not calls:
                continue
            calls.sort(key=lambda item: (item[0].lineno, item[0].col_offset))
            file = ir.blocks[host].file
            id_file = _relative_id(file_bases.get(file, root), Path(file))
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
    # Removals during the flow rewrite are deferred for speed; fold them into
    # the edge list before anyone reads it, so counts and serialization match
    # what the index reports.
    ir.compact()
    ir.metadata["callgraph_supplied"] = callgraph is not None
    ir.metadata["function_count"] = len(ir.functions)
    ir.metadata["block_count"] = len(ir.blocks)
    ir.metadata["edge_count"] = len(ir.edges)
    ir.metadata["unresolved_calls"] = unresolved
    ir.metadata["source_hash"] = _source_hash(root, slice_info.modules.values())
    if deps:
        ir.metadata["deps"] = {
            "module_count": len(slice_info.modules),
            "roots": [str(r) for r in slice_info.roots],
            "truncated": slice_info.truncated,
        }
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


def _callee_name(call: ast.Call) -> str | None:
    """Syntactic callee name only — whether it *resolves* is the solver's job."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None
