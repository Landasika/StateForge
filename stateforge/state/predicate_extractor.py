"""Phase-4.3 Predicate Extractor - Guard to StatePredicate via provenance."""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.state.semantic_model import (
    StateVariable, StatePredicate, PredicateOp, StateSource)
from stateforge.state.state_provenance import SessionProvenance

_OPS = {
    ast.Eq: PredicateOp.EQ, ast.NotEq: PredicateOp.NE,
    ast.Gt: PredicateOp.GT, ast.GtE: PredicateOp.GE,
    ast.Lt: PredicateOp.LT, ast.LtE: PredicateOp.LE,
    ast.In: PredicateOp.IN, ast.NotIn: PredicateOp.NOT_IN,
    ast.Is: PredicateOp.EQ, ast.IsNot: PredicateOp.NE,
}


def extract_predicates(source_dir):
    predicates = []
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
            predicates.extend(_extract_from_fn(fn))
    return predicates


def _extract_from_fn(fn):
    prov = SessionProvenance()
    prov.analyze(fn)
    result = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        test = getattr(node, "test", None)
        if test is None:
            continue
        pred = _resolve_condition(test, prov)
        if pred:
            result.append(pred)
    return result


def _resolve_condition(test, prov):
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        return _resolve_compare(test, prov)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _resolve_condition(test.operand, prov)
        if inner is None:
            return None
        return StatePredicate(inner.variable, _negate(inner.op), inner.value)
    if isinstance(test, ast.Name):
        sv = prov.lookup(test.id)
        if sv is not None:
            return StatePredicate(sv, PredicateOp.TRUTHY)
    return None


def _resolve_compare(node, prov):
    left = node.left
    right = node.comparators[0]
    sv = None
    if isinstance(left, ast.Name):
        sv = prov.lookup(left.id)
    if sv is None:
        return None

    if isinstance(right, ast.Constant):
        val = right.value
    else:
        return None

    py_op = type(node.ops[0])
    pred_op = _OPS.get(py_op)
    if pred_op is None:
        return None

    if val is None and py_op in (ast.Eq, ast.Is):
        pred_op = PredicateOp.IS_NONE
    elif val is None and py_op in (ast.NotEq, ast.IsNot):
        pred_op = PredicateOp.IS_NOT_NONE

    return StatePredicate(sv, pred_op, val)


def _negate(op):
    neg = {
        PredicateOp.EQ: PredicateOp.NE, PredicateOp.NE: PredicateOp.EQ,
        PredicateOp.GT: PredicateOp.LE, PredicateOp.LE: PredicateOp.GT,
        PredicateOp.GE: PredicateOp.LT, PredicateOp.LT: PredicateOp.GE,
        PredicateOp.IS_NONE: PredicateOp.IS_NOT_NONE,
        PredicateOp.IS_NOT_NONE: PredicateOp.IS_NONE,
        PredicateOp.IN: PredicateOp.NOT_IN,
        PredicateOp.NOT_IN: PredicateOp.IN,
        PredicateOp.TRUTHY: PredicateOp.FALSY,
        PredicateOp.FALSY: PredicateOp.TRUTHY,
    }
    return neg.get(op, op)
