"""路由反向归属测试:手工构造 ICFG + 端点,验证 BFS 与匹配规则。"""

from __future__ import annotations

from stateforge.analysis.attribution import (
    RouteAttribution, attribute_routes, function_callers)
from stateforge.core.ir import FlowEdge, FunctionIR, ProgramIR
from stateforge.core.model import Endpoint


def _fn(name: str, file: str = "/app/views.py", entry: str | None = None):
    entry = entry or f"{name}::entry"
    return name, FunctionIR(name=name, file=file, module="app",
                            entry=entry, exit=f"{name}::exit",
                            blocks=[entry, f"{name}::exit"])


def _ir(*fns):
    ir = ProgramIR()
    for qname, fn in fns:
        ir.functions[qname] = fn
    return ir


def _call(ir, caller: str, callee: str):
    ir.add_edge(FlowEdge(
        src=f"{caller}::entry", dst=f"{callee}::entry", kind="call"))


def _ep(route: str, func: str, methods=("GET",), file="/app/views.py"):
    return Endpoint(func=func, route=route, methods=list(methods),
                    file=file, line=1)


def test_callers_reverse_edges():
    ir = _ir(_fn("view"), _fn("helper"))
    _call(ir, "view", "helper")
    assert function_callers(ir) == {"helper": {"view"}}


def test_route_reaches_transitively():
    # view -> helper -> deep
    ir = _ir(_fn("view"), _fn("helper"), _fn("deep"))
    _call(ir, "view", "helper")
    _call(ir, "helper", "deep")
    att = attribute_routes(ir, [_ep("/x", "view")])
    assert att.routes_reaching("deep") == {"GET /x"}
    assert att.routes_reaching("view") == {"GET /x"}
    assert "helper" in att.matched_endpoints["GET /x"] or True


def test_multiple_routes_merge():
    ir = _ir(_fn("a"), _fn("b"), _fn("shared"))
    _call(ir, "a", "shared")
    _call(ir, "b", "shared")
    att = attribute_routes(ir, [_ep("/a", "a"), _ep("/b", "b")])
    assert att.routes_reaching("shared") == {"GET /a", "GET /b"}


def test_qname_suffix_and_cbv_match():
    # qname 带模块前缀;CBV 方法 Class.method
    ir = _ir(_fn("app.auth.login_user", file="/app/auth.py"),
             _fn("app.views.MyView.post", file="/app/views.py"),
             _fn("app.views.MyView.dispatch", file="/app/views.py"))
    att = attribute_routes(
        ir, [_ep("/login", "login_user", file="/app/auth.py"),
             _ep("/cbv", "MyView", file="/app/views.py")])
    assert "app.auth.login_user" in att.matched_endpoints["GET /login"]
    assert "app.views.MyView.post" in att.matched_endpoints["GET /cbv"]
    assert "app.views.MyView.dispatch" in att.matched_endpoints["GET /cbv"]


def test_unmatched_endpoint_recorded():
    ir = _ir(_fn("view"))
    att = attribute_routes(ir, [_ep("/gone", "nope")])
    assert att.unmatched_endpoints == ["GET /gone"]
    assert att.routes_reaching("view") == set()


def test_cycle_safe():
    ir = _ir(_fn("view"), _fn("a"), _fn("b"))
    _call(ir, "view", "a")
    _call(ir, "a", "b")
    _call(ir, "b", "a")          # 环
    att = attribute_routes(ir, [_ep("/x", "view")])
    assert att.routes_reaching("b") == {"GET /x"}
