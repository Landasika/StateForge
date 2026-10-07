"""Phase-4.3 Session 访问分析——完整 READ/SET/UPDATE/DELETE/CLEAR."""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.state.semantic_model import (
    StateVariable, StateOp, StateAccess, StateSource)


def analyze_session_access(source_dir):
    accesses = []
    for py in sorted(Path(source_dir).rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            accesses.extend(_visit_function(fn, str(py)))
    return accesses


def _visit_function(fn, filename):
    result = []
    for node in ast.walk(fn):
        # session["key"] — ctx 区分 READ/SET
        if isinstance(node, ast.Subscript) and _is_session(node.value):
            key = _extract_key(node.slice)
            if key is None:
                continue
            var = StateVariable(StateSource.SESSION, key)
            if isinstance(node.ctx, ast.Load):
                result.append(StateAccess(
                    variable=var, operation=StateOp.READ,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))
            elif isinstance(node.ctx, ast.Store):
                result.append(StateAccess(
                    variable=var, operation=StateOp.SET,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno,
                    value_expr=_safe_unparse(node)))

        # session.get / session.pop / session.clear
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            obj = node.func.value
            method = node.func.attr
            if _is_session(obj) and method == "get" and node.args:
                key = _extract_key(node.args[0])
                if key:
                    result.append(StateAccess(
                        variable=StateVariable(StateSource.SESSION, key),
                        operation=StateOp.READ,
                        endpoint=None, function=fn.name,
                        filename=filename, lineno=node.lineno))
            if _is_session(obj) and method == "pop" and node.args:
                key = _extract_key(node.args[0])
                if key:
                    result.append(StateAccess(
                        variable=StateVariable(StateSource.SESSION, key),
                        operation=StateOp.DELETE,
                        endpoint=None, function=fn.name,
                        filename=filename, lineno=node.lineno))
            if _is_session(obj) and method == "clear":
                result.append(StateAccess(
                    variable=StateVariable(StateSource.SESSION, "*"),
                    operation=StateOp.CLEAR,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))

        # session["key"] += value
        if (isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Subscript)
                and _is_session(node.target.value)):
            key = _extract_key(node.target.slice)
            if key:
                result.append(StateAccess(
                    variable=StateVariable(StateSource.SESSION, key),
                    operation=StateOp.UPDATE,
                    endpoint=None, function=fn.name,
                    filename=filename, lineno=node.lineno))

        # del session["key"]
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and _is_session(target.value)):
                    key = _extract_key(target.slice)
                    if key:
                        result.append(StateAccess(
                            variable=StateVariable(StateSource.SESSION, key),
                            operation=StateOp.DELETE,
                            endpoint=None, function=fn.name,
                            filename=filename, lineno=node.lineno))
    return result


def _is_session(obj):
    return isinstance(obj, ast.Name) and obj.id == "session"


def _extract_key(node):
    if isinstance(node, ast.Constant):
        return str(node.value)
    return None


def _safe_unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return None
