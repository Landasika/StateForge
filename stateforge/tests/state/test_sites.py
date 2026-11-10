"""sites.py 通用位点提取器的形状级测试。

覆盖:守卫-提前退出形状、``not`` 极性、常量折叠、共享判定
(单函数局部字典被丢)、pop/flush 记录。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from stateforge.state.sites import extract_sites

SAMPLE = textwrap.dedent("""
    TOKEN_KEY = "tk"

    def view_a(request):
        if not request.user.is_authenticated:
            return None
        request.session[TOKEN_KEY] = "v"
        return request

    def view_b(request):
        request.session.pop("tmp")
        request.session.flush()

    def helper(d):
        d["local"] = 1
""")


def _run(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text(SAMPLE, encoding="utf-8")
    return extract_sites([tmp_path])


def test_guard_shape_and_polarity(tmp_path):
    scan = _run(tmp_path)
    assert len(scan.guards) == 1
    g = scan.guards[0]
    assert g.negated is True
    assert "request" in g.roots
    assert "is_authenticated" in g.expr


def test_shared_store_kept_and_local_dropped(tmp_path):
    scan = _run(tmp_path)
    # request.session 被 view_a/view_b 两个函数使用 → 共享,保留
    chains = {s.chain for s in scan.stores}
    assert "request.session" in chains
    # helper 的局部字典 d 仅单函数使用 → 丢弃
    assert "d" not in chains
    assert scan.dropped_unshared == 1


def test_constant_folding_of_key(tmp_path):
    scan = _run(tmp_path)
    writes = [s for s in scan.stores if s.op == "write"]
    assert len(writes) == 1
    assert writes[0].key == "tk"
    assert writes[0].key_status == "const"
    assert writes[0].slot == 'request.session[tk]'


def test_pop_and_flush_ops(tmp_path):
    scan = _run(tmp_path)
    ops = {(s.op, s.key) for s in scan.stores}
    assert ("pop", "tmp") in ops
    assert ("flush", None) in ops
    flush = next(s for s in scan.stores if s.op == "flush")
    assert flush.slot == "request.session[*]"


def test_function_attribution(tmp_path):
    scan = _run(tmp_path)
    by_fn = {s.function for s in scan.stores}
    assert "view_a" in by_fn and "view_b" in by_fn
    assert scan.guards[0].function == "view_a"


def test_dynamic_key_never_guessed(tmp_path):
    f = tmp_path / "dyn.py"
    f.write_text(textwrap.dedent("""
        def a(request):
            request.session[f"cart_{request.user.id}"] = 1

        def b(request):
            request.session[f"wish_{request.user.id}"] = 2
    """), encoding="utf-8")
    scan = extract_sites([tmp_path])
    dyn = [s for s in scan.stores if s.file.endswith("dyn.py")]
    assert dyn and all(s.key == "<dyn>" and s.key_status == "dyn" for s in dyn)


def test_is_entry_filters_request_local_dicts(tmp_path):
    """共享但请求内的字典(kwargs 类)被入口资格条件排除。"""
    f = tmp_path / "app2.py"
    f.write_text(textwrap.dedent("""
        def view_a(request):
            request.session["k"] = 1

        def view_b(request):
            request.session["k"] = 2

        def util_a(opts):
            opts["x"] = 1

        def util_b(opts):
            opts["y"] = 2
    """), encoding="utf-8")
    # 只有 view_a/view_b 是入口;opts 两个函数共享但不是入口参数
    scan = extract_sites(
        [tmp_path], is_entry=lambda fn, file: fn.startswith("view_"))
    chains = {s.chain for s in scan.stores if s.file.endswith("app2.py")}
    assert "request.session" in chains
    assert "opts" not in chains
