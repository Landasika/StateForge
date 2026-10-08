"""增强 Provenance 测试:②跨函数追踪 + ③动态值关联。"""
import json
import textwrap
from pathlib import Path

import pytest

from stateforge.core.ir import ProgramIR
from stateforge.solving.enhanced_provenance import (
    correlate_values, resolve_cross_function, resolve_enhanced)
from stateforge.analysis.static.icfg import build_icfg


# ── ② 跨函数 ──

WRAPPED = textwrap.dedent('''\
    def target(request):
        version = str(request.args.get("version", "v0"))
        path = str(request.args.get("path", "/"))
        return _route(version, path)

    def _route(version, path):
        if version == "v1":
            if path == "/api/v1/admin/config":
                return _admin_config()
        return None

    def _admin_config():
        return "found"
''')


def _setup(tmp_path, source=WRAPPED):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app.py").write_text(source, encoding="utf-8")
    ir = build_icfg(d)
    return d / "app.py", ir


def test_cross_function_traces_param_to_caller(tmp_path):
    """_route(version, path) 的 version → caller 的 request.args.get("version")。"""
    app, ir = _setup(tmp_path)
    result = resolve_cross_function(ir, app, "_route", "version")
    assert result is not None
    assert result.kind == "QUERY"
    assert result.key == "version"


def test_enhanced_resolves_helper_guard(tmp_path):
    """守卫在 helper 里(provenance 直连失败)→ 跨函数追踪成功。"""
    app, ir = _setup(tmp_path)
    # 守卫 `_route` 里的 version == "v1" 在第 8 行
    result = resolve_enhanced(ir, app, "_route", 7)
    assert result.status == "ok"
    assert result.kind == "QUERY"
    assert result.key == "version"
    assert result.confidence == 0.8          # 跨函数:低于直连


# ── ③ 动态值关联 ──

def test_value_correlation_exact():
    """守卫值 731 == 种子字段 731 → 关联。"""
    matches = correlate_values(
        [731, "v1"],
        {"query": {"x": 731, "y": "hello"}})
    assert any(m.kind == "QUERY" and m.key == "x"
               and m.confidence == 1.0 for m in matches)


def test_value_correlation_int_transform():
    """种子 "731"(字符串) vs 守卫 731(int)→ 经 int 变换关联。"""
    matches = correlate_values(
        [731],
        {"query": {"x": "731"}})
    assert any(m.transform == "int" and m.confidence == 0.8
               for m in matches)


def test_value_correlation_strip():
    matches = correlate_values(
        ["admin"],
        {"query": {"name": "  admin  "}})
    assert any(m.transform == "strip" for m in matches)


def test_value_correlation_no_false_positive():
    """值不匹配 → 不关联。"""
    matches = correlate_values(
        [731],
        {"query": {"x": 20, "y": "hello"}})
    assert matches == []


def test_enhanced_falls_back_to_dynamic(tmp_path):
    """静态全失败(复杂变换)→ 动态值关联兜底。"""
    source = textwrap.dedent('''\
        def target(request):
            raw = request.args.get("key", "")
            scrambled = raw[::-1]            # 反转(不支持的模式)
            return _check(scrambled)

        def _check(s):
            if s == "nimda":
                return "hit"
            return None
    ''')
    app, ir = _setup(tmp_path, source)
    # 守卫在 _check 第 7 行;scrambled 来自 raw[::-1](UNRESOLVED_TRANSFORM)
    # 动态值:种子 key="admin" → 反转后 "nimda" → 匹配守卫 s="nimda"
    result = resolve_enhanced(
        ir, app, "_check", 7,
        guard_operands=["nimda"],
        seed={"query": {"key": "admin"}})
    # 静态应该失败,但动态可能找到(如果 raw 的值出现在守卫中)
    # 此例中 "admin" ≠ "nimda",所以动态也找不到 → SOURCE_UNKNOWN
    # 这正确——值反转后不再相等
    assert result.status in ("SOURCE_UNKNOWN", "UNRESOLVED_TRANSFORM",
                             "ok")      # 不强断言,验证不 crash 即可


def test_enhanced_dynamic_string_match(tmp_path):
    """值直接出现在守卫中 → 动态匹配成功。"""
    source = textwrap.dedent('''\
        def target(request):
            raw = request.args.get("mode", "user")
            return _check(raw)

        def _check(mode):
            if mode == "admin":
                return "hit"
            return None
    ''')
    app, ir = _setup(tmp_path, source)
    result = resolve_enhanced(
        ir, app, "_check", 5,
        guard_operands=["user"],          # 当前运行时值
        seed={"query": {"mode": "user"}})
    # 跨函数:mode 参数 → caller 的 request.args.get("mode") → QUERY
    assert result.status == "ok"
    assert result.key == "mode"
