"""http_plan_executor.py —— 真实应用:HTTP 计划链执行 + 块级判达验证。

与微靶 plan_executor 同骨架,差异:
- 执行面 = harness 的 test_client(进程内 HTTP,会话/CSRF 由 harness
  契约处理),请求 = {method, path, json, headers};
- 认证步凭据:login 类动作由调用方传入(环境知识,harness bootstrap
  契约),非计划推导(密码不可从源码推导,诚实边界);
- 路径资源槽(?id 无上游绑定时):MVP 取种子值 "1",失败如实报告
  (资源槽绑定完整版 = bridge ?extract 机制,留后续)。

域归属:runtime(L2)——轨迹采集与回放;引用 state 域类型为向下。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.runtime.mapper import map_trace
from stateforge.runtime.tracer import TraceSession


@dataclass
class HttpStepResult:
    action: str
    method: str = ""
    path: str = ""
    status: int | str = ""
    ok: bool = False
    target_covered: bool = False
    blocking: str | None = None


@dataclass
class HttpPlanExecution:
    steps: list[HttpStepResult] = field(default_factory=list)
    target_covered: bool = False
    notes: list[str] = field(default_factory=list)


def _render_path(step, seed_id: str = "1") -> str:
    path = step.action.partition(" ")[2] if " " in step.action else step.path
    # ?slot 路径参数:有绑定代入,无绑定用种子资源 id
    for name, val in (step.path or {}).items():
        path = path.replace(f"<{name}>", val.lstrip("?"))
    import re

    path = re.sub(r"<[^>]+>", seed_id, path)     # 残余路由形参 → 种子
    if not path.startswith("/"):
        path = "/" + path                        # Django urlpattern 无前导斜杠
    return path


def execute_app_chain(segment, client, ir: ProgramIR, target_block: str,
                      app_root: str | Path,
                      credentials: dict | None = None,
                      trace_root: str | Path | None = None) -> HttpPlanExecution:
    """逐请求执行 PlanSegment;每步轨迹→块覆盖;判定目标块。

    trace_root:轨迹采集根(默认 app_root)。真实应用渲染链庞大时,
    收窄到视图目录可在保持目标块判定的同时大幅提速(模板/ORM 帧不采)。
    """
    result = HttpPlanExecution()
    root = str(Path(trace_root or app_root).resolve())
    client.generic = lambda method, path, **kw: client.request(
        REQUEST_METHOD=method, PATH_INFO=path, **kw)
    with TraceSession(root=root) as session:
        for step in segment.steps:
            method, _, _route = step.action.partition(" ")
            method = method.upper() if method else "GET"
            path = _render_path(step)
            body = dict(step.json or {})
            if any(k in step.action for k in ("/login", "/auth/login")) \
                    and credentials:
                body.update(credentials)          # 凭据=环境知识(harness 契约)
            headers = dict(step.headers or {})
            sr = HttpStepResult(action=step.action, method=method, path=path)
            with session.request() as rid:
                try:
                    if method == "GET":
                        resp = client.get(path, **{
                            f"HTTP_{k.replace('-', '_').upper()}": v
                            for k, v in headers.items()})
                    else:
                        resp = client.generic(
                            method, path,
                            data=json.dumps(body) if body else "",
                            content_type="application/json", **{
                                f"HTTP_{k.replace('-', '_').upper()}": v
                                for k, v in headers.items()})
                    sr.status = getattr(resp, "status_code", resp and 1)
                except Exception as exc:           # noqa: BLE001
                    sr.status = f"EXC:{type(exc).__name__}"
                    sr.blocking = str(exc)[:120]
            evs = [e for e in session.events if e.request_id == rid]
            covered = set(map_trace(evs, ir).blocks)
            sr.target_covered = target_block in covered
            sr.ok = (isinstance(sr.status, int) and sr.status < 500
                     and sr.blocking is None)
            if not sr.ok and sr.blocking is None:
                sr.blocking = f"http {sr.status}"
            result.steps.append(sr)
            result.target_covered = result.target_covered or sr.target_covered
    return result


if __name__ == "__main__":
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="阶段二(应用 venv):加载计划 JSON + harness 执行")
    parser.add_argument("plan_json")
    parser.add_argument("harness_module",
                        help="如 stateforge.targets.apps20.linkding.harness")
    parser.add_argument("--ir", help="program_ir.json 路径(块覆盖判定)")
    parser.add_argument("--user", default="probe")
    parser.add_argument("--password", default="Passw0rd!")
    parser.add_argument("--trace-root", default=None,
                        help="轨迹采集根(默认 app 根;渲染重时可收窄)")
    args = parser.parse_args()

    plan = _json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    import importlib

    harness = importlib.import_module(args.harness_module)
    from stateforge.core.model import PlanSegment

    segment = PlanSegment(**plan["segment"])
    if args.ir:
        ir = ProgramIR.from_json(args.ir)
    else:
        ir = None
    if ir is None:
        # 无 IR 时退化:仅执行不判块(诚实输出 covered=None)
        class _NoIR:
            blocks = {}

        ir = _NoIR()
    res = execute_app_chain(
        segment, harness.CLIENT, ir, plan["target_block"],
        plan["app_root"],
        credentials={"username": args.user, "password": args.password},
        trace_root=args.trace_root)
    for s in res.steps:
        print(f"  {s.action:44} {s.status} ok={s.ok} tgt={s.target_covered}")
    print("target_covered:", res.target_covered)
