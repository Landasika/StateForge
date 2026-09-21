"""guided_explorer.py —— 定向探索循环:自动朝最近缺口发请求。

不用 Atheris;用 coverage_viz 采覆盖 + gap_analysis 找缺口 + 规划器构造输入。

循环:
  ① 发当前请求集
  ② 采覆盖(sys.settrace)
  ③ 找缺口(gap_analysis)
  ④ 对最近的缺口:如果端点没被调→构造调用;如果分支没走到→变异参数
  ⑤ 回到 ①,直到覆盖率不再提升

用法:
  python3 -m stateforge.fuzz.guided_explorer stateforge/targets/vulnshop
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 核心循环


def explore(target_dir: str, framework: str = "flask",
            max_rounds: int = 15, verbose: bool = True) -> dict:
    """定向探索:循环{发请求→采覆盖→找缺口→构造下一个请求}。"""
    from stateforge.analyzer.analyzer import analyze
    from stateforge.analyzer.cfg import build_cfg, attach_anchors, block_gates
    from stateforge.fuzz.coverage_viz import map_coverage_to_blocks

    model = analyze(Path(target_dir), framework)
    app = _load_app(target_dir, framework)
    client = app.test_client()

    # 状态跟踪
    all_covered_lines: set[tuple[str, int]] = set()
    all_requests_sent: list[dict] = []
    coverage_history: list[float] = []
    round_num = 0

    # 初始请求集:每个端点一次基础调用
    requests = _initial_requests(model)

    while round_num < max_rounds:
        round_num += 1
        if verbose:
            print(f"\n--- 第 {round_num} 轮 ({len(requests)} 个请求) ---")

        # ① 发请求 + 采覆盖
        round_covered = set()

        def tracer(frame, event, arg):
            if event == "line":
                round_covered.add((frame.f_code.co_filename, frame.f_lineno))
            return tracer

        sys.settrace(tracer)
        session_state = _execute_requests(client, requests)
        sys.settrace(None)

        # 累积覆盖
        root = str(Path(target_dir).resolve())
        round_proj = {(f, l) for f, l in round_covered if f.startswith(root)}
        new_lines = round_proj - all_covered_lines
        all_covered_lines |= round_proj
        all_requests_sent.extend(requests)

        # ② 计算块覆盖
        result = map_coverage_to_blocks(target_dir, framework, all_covered_lines)
        xfg = result["xfg"]
        covered_blocks = set(result["covered_blocks"].keys())
        total_blocks = result["total_blocks"]
        pct = 100 * len(covered_blocks) / max(total_blocks, 1)
        coverage_history.append(pct)

        if verbose:
            print(f"  覆盖: {len(covered_blocks)}/{total_blocks} = {pct:.0f}%"
                  f"  (+{len(new_lines)} 新行)")

        # ③ 停止条件:覆盖率不再提升
        if len(coverage_history) >= 3 and coverage_history[-1] == coverage_history[-2] == coverage_history[-3]:
            if verbose:
                print("  覆盖率连续 3 轮不变,停止")
            break

        # ④ 找缺口 + 构造下一轮请求
        next_requests = _plan_next_round(
            model, xfg, covered_blocks, session_state, verbose)
        if not next_requests:
            if verbose:
                print("  无更多可构造的请求,停止")
            break
        requests = next_requests

    return {
        "rounds": round_num,
        "coverage_pct": coverage_history[-1] if coverage_history else 0,
        "coverage_history": coverage_history,
        "total_requests": len(all_requests_sent),
        "covered_lines": len(all_covered_lines),
        "covered_blocks": len(covered_blocks),
        "total_blocks": total_blocks,
        "requests_sent": all_requests_sent,
    }


# ---------------------------------------------------------------- 请求执行


def _execute_requests(client, requests: list[dict]) -> dict:
    """发一组请求,返回会话状态(token、资源 ID 等)。"""
    state = {"token": None, "order_ids": [], "user_id": None}

    for req in requests:
        try:
            headers = {}
            if state.get("token"):
                headers["Authorization"] = f"Bearer {state['token']}"
            method = req.get("method", "GET")
            route = req.get("route", "/")
            for k, v in req.get("subs", {}).items():
                route = route.replace(k, str(v))

            if method in ("POST", "PUT") and req.get("json"):
                resp = client.open(route, method=method, json=req["json"], headers=headers)
            else:
                resp = client.open(route, method=method, headers=headers)

            # 提取状态
            if resp.status_code in (200, 201):
                try:
                    body = resp.get_json()
                    if isinstance(body, dict):
                        if "token" in body:
                            state["token"] = body["token"]
                        if "id" in body:
                            if "order" in route or "orders" in route:
                                state["order_ids"].append(body["id"])
                            elif "user" in route or "auth" in route:
                                state["user_id"] = body["id"]
                except Exception:
                    pass
        except Exception:
            pass

    return state


# ---------------------------------------------------------------- 下一轮规划


def _initial_requests(model) -> list[dict]:
    """初始请求:所有端点的基础调用(无参数,无 auth)。"""
    reqs = [{"method": "GET", "route": "/"}]
    for ep in model.endpoints:
        route = ep.route
        for p in ep.params:
            route = route.replace(f"<int:{p.name}>", "1").replace(f"<{p.name}>", "x")
        req = {"method": ep.methods[0], "route": route}
        if ep.methods[0] in ("POST", "PUT"):
            req["json"] = {}
        if req not in reqs:
            reqs.append(req)
    return reqs


def _plan_next_round(model, xfg, covered_blocks, session_state, verbose) -> list[dict]:
    """分析缺口,构造下一轮请求。"""
    from stateforge.fuzz.gap_analysis import find_gaps

    gaps = find_gaps(xfg, covered_blocks)
    if not gaps:
        return []

    requests = []
    # 确保有 auth(如果需要)
    if not session_state.get("token"):
        # 检查是否有端点需要 auth
        needs_auth = any(
            any(p.var == "Session.authed_as" for p in ep.preconditions)
            for ep in model.endpoints
        )
        if needs_auth:
            requests.extend([
                {"method": "POST", "route": "/auth/register",
                 "json": {"username": f"exp_{int(time.time())}", "password": "exp_pass"}},
                {"method": "POST", "route": "/auth/login",
                 "json": {"username": f"exp_{int(time.time())}", "password": "exp_pass"}},
            ])

    # 对每个可达缺口,构造到达请求
    for gap in gaps[:5]:  # 最多 5 个
        func = gap.get("func", "")
        ep = next((e for e in model.endpoints if e.func == func), None)
        if not ep:
            continue

        # 检查此函数是否有任何块被覆盖(是否已调用过)
        func_covered = any(
            xfg.nodes[n].get("covered", False)
            for n, d in xfg.nodes(data=True) if d.get("func") == func
        )

        route = ep.route
        for p in ep.params:
            route = route.replace(f"<int:{p.name}>", "1").replace(f"<{p.name}>", "x")

        if not func_covered:
            # 端点没被调用 → 构造基础调用
            req = {"method": ep.methods[0], "route": route}
            if ep.methods[0] in ("POST", "PUT"):
                req["json"] = _smart_params(ep, gap, session_state)
            requests.append(req)
            if verbose:
                print(f"  → 调用未覆盖端点: {ep.methods[0]} {ep.route}")
        else:
            # 端点被调了但没走到这个块 → 变异参数
            req = {"method": ep.methods[0], "route": route,
                   "json": _mutate_params(ep, gap)}
            requests.append(req)
            if verbose:
                lines = gap.get("lines", (0, 0))
                print(f"  → 变异参数触发分支: {ep.func} L{lines[0]}")

    return _dedup_requests(requests)


def _smart_params(ep, gap, session_state) -> dict:
    """根据缺口信息构造参数。"""
    params = {}
    for p in ep.params:
        if p.location == "json":
            if p.name in ("username",):
                params[p.name] = f"user_{int(time.time()) % 10000}"
            elif p.name in ("password",):
                params[p.name] = "pass123"
            elif p.name in ("items",):
                params[p.name] = [{"sku": "test", "price": 100, "qty": 1}]
            elif p.name in ("amount",):
                params[p.name] = 100
            elif p.name in ("coupon_code",):
                params[p.name] = "SAVE10"
            elif p.name in ("role",):
                params[p.name] = "admin"
            else:
                params[p.name] = "test"
    return params


def _mutate_params(ep, gap) -> dict:
    """根据缺口的分支条件变异参数。"""
    params = _smart_params(ep, gap, {})

    # 从 gap 的分支条件推导变异方向
    conditions = _extract_conditions(gap)
    for cond in conditions:
        if "coupon" in cond.lower():
            params["coupon_code"] = "SAVE10"
        elif "amount" in cond.lower() or "price" in cond.lower():
            if "not isinstance" in cond or "<= 0" in cond:
                params["amount"] = -1  # 或 price = -1
        elif "items" in cond.lower():
            if "not items" in cond or "not isinstance" in cond:
                params["items"] = []
        elif "status" in cond.lower():
            # 需要先做前置操作,这里只标记
            pass

    return params


def _extract_conditions(gap) -> list[str]:
    """从 gap 的路径中提取分支条件文本。"""
    import ast
    conds = []
    path = gap.get("path", [])
    xfg = gap.get("_xfg")  # 不太干净但可用
    return conds


def _dedup_requests(requests: list[dict]) -> list[dict]:
    """去重(同路由+同方法+同参数)。"""
    seen = set()
    out = []
    for r in requests:
        key = (r.get("method"), r.get("route"), json.dumps(r.get("json", {}), sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ---------------------------------------------------------------- 应用加载


def _load_app(app_root: str, framework: str):
    if framework == "flask":
        import importlib
        sys.path.insert(0, str(Path(app_root).parent))
        mod_name = Path(app_root).name
        for entry in ["app"]:
            try:
                mod = importlib.import_module(f"{mod_name}.{entry}")
                if hasattr(mod, "create_app"):
                    return mod.create_app(db_path="/tmp/guided_explore.db",
                                          guards=0, init_db=True)
                if hasattr(mod, "app"):
                    return mod.app
            except (ImportError, AttributeError):
                continue
        raise RuntimeError(f"无法加载 {app_root} 的 Flask app")
    raise RuntimeError(f"framework {framework} 暂不支持")


# ---------------------------------------------------------------- CLI


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "stateforge/targets/vulnshop"
    result = explore(target)
    print(f"\n{'='*60}")
    print(f"定向探索完成: {result['rounds']} 轮, "
          f"{result['total_requests']} 个请求, "
          f"覆盖率 {result['coverage_pct']:.0f}%")
    print(f"覆盖历史: {['%.0f%%' % c for c in result['coverage_history']]}")
