"""Selective Atheris bytecode instrumentation.

Atheris coverage comes from ``instrument_func`` rewriting a function's
bytecode to call ``_trace_branch``.  Rewriting the already-rewritten code
double-instruments, so dedup keys (file, firstlineno, name) of both the
original and the instrumented code object are recorded in a registry.
``instrument_all`` is intentionally not offered: instrumenting ANOTA's own
internals conflicts with the interpreter (see old atheris_harness.py notes).
"""

from __future__ import annotations

from types import CodeType

_DEFAULT_REGISTRY: set[tuple[str, int, str]] = set()


def _code_key(code: CodeType) -> tuple[str, int, str]:
    return (code.co_filename, code.co_firstlineno, code.co_name)


class Instrumenter:
    def __init__(self, registry: set[tuple[str, int, str]] | None = None):
        # None -> 模块级共享 registry:跨实例防二次插桩(同进程多次 prepare)
        self._seen = _DEFAULT_REGISTRY if registry is None else registry

    def instrument_callable(self, func, atheris) -> bool:
        code = getattr(func, "__code__", None)
        if code is None or not callable(func):
            return False
        if _code_key(code) in self._seen:
            return False
        try:
            atheris.instrument_func(func)
        except Exception:
            return False          # 失败不登记,可重试
        self._seen.add(_code_key(code))
        rewritten = getattr(func, "__code__", None)
        if rewritten is not None:
            self._seen.add(_code_key(rewritten))
        return True

    def instrument_flask_app(self, app, atheris) -> int:
        count = 0
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            view = app.view_functions.get(rule.endpoint)
            if self.instrument_callable(view, atheris):
                count += 1
            for cell in getattr(view, "__closure__", None) or ():
                try:
                    inner = cell.cell_contents
                except ValueError:      # 空 cell
                    continue
                if self.instrument_callable(inner, atheris):
                    count += 1
        return count
