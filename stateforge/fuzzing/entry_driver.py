"""Phase-2 fuzz 子进程入口:python -m stateforge.fuzzing.entry_driver ...

模式:off(裸执行计数)| measure(+settrace 全局覆盖,不筛选)| feedback(+结构新颖度筛选落盘)。
产物逐行 flush 抗 libFuzzer _Exit;epoch 汇总由 EpochController 事后从产物重建。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import atheris

from stateforge.core.ir import ProgramIR
from stateforge.runtime.tracer import TraceSession
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.fuzzing.feedback.structural import StructuralFeedback
from stateforge.fuzzing.candidates import CandidateCollector
from stateforge.fuzzing.corpus_manager import CorpusManager


def _load_target(path: str):
    spec = importlib.util.spec_from_file_location("sf_target", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sf_target"] = mod
    spec.loader.exec_module(mod)
    return mod.target


# ------------------------------------------------ C10 序列种子（P3-T2）分派


def decode_sequence_seed(data: bytes) -> list | None:
    """C10 消费端解码：bytes → 序列步列表；非序列种子 → None。

    序列种子 = {"steps": [<单请求 dict>, ...]}（R1 裁决：前缀步素状态，
    末步=当前请求）。分派只看 "steps" 键存在且值为 list——legacy 单请求
    契约（{"query","form","json","header","cookie"}）保持冻结，凡非序列
    输入一律由调用方原字节透传，行为逐字节不变（四臂非回归门）。
    """
    try:
        seed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(seed, dict) and isinstance(seed.get("steps"), list):
        return seed["steps"]
    return None


def encode_sequence_seed(steps: list) -> bytes:
    """C10 序列种子构造（steps → bytes）。T3 state 注入器落盘时必须
    复用同一 builder，保证与消费端解码逐字节互认。"""
    return json.dumps({"steps": list(steps)}, ensure_ascii=False,
                      sort_keys=True).encode("utf-8")


def execute_sequence(target, steps: list) -> None:
    """按序逐步执行：每步经序列化走与单请求完全相同的 target(data) 执行面。

    会话连续性 = 进程内持久：adapter 持有的 app/test_client、或靶模块的
    会话态跨调用存活，无需显式 cookie jar。空 steps = 零执行；非 dict 步
    原样序列化喂靶（由靶自己的错误路径消化）。单步异常不在此吞——与
    legacy 一致由外层 handler 统一吞掉（不向引擎泄 crash）。
    """
    for step in steps:
        target(json.dumps(step, ensure_ascii=False,
                          sort_keys=True).encode("utf-8"))


def dispatch_seed(target, data: bytes) -> None:
    """C10 分派入口：含 "steps" 的 JSON dict → 序列执行；否则原字节透传。"""
    steps = decode_sequence_seed(data)
    if steps is None:
        target(data)                          # legacy：逐字节不变
        return
    execute_sequence(target, steps)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")            # 目标 py 文件(含 target(data))
    parser.add_argument("ir")                # program_ir.json
    parser.add_argument("corpus_dir")        # 种子目录(libFuzzer 亦落盘于此)
    parser.add_argument("out_dir")           # recorder 产物目录
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--mode",
                        choices=["off", "measure", "feedback", "frontier"],
                        required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epoch", type=int, default=0)
    parser.add_argument("--group", default="x")
    parser.add_argument("--lifecycle", choices=["noop", "reset", "snapshot"],
                        default="noop",
                        help="P0-A 输入级状态隔离(noop=旧行为:输入间状态延续)")
    parser.add_argument("--observe-state", action="store_true",
                        help="P0-B 运行时状态观测(state_evidence 落 global_state)")
    parser.add_argument("--session-guided", action="store_true",
                        help="SS-03: 启动前用静态 Session 候选写入块做定向种子搜索(默认关)")
    parser.add_argument("--session-target-block", default=None,
                        help="SS-03: 目标基本块 BlockID(启用 --session-guided 时必填)")
    parser.add_argument("--session-request-pool", default=None,
                        help="SS-03: 独立单请求 JSON list[dict] 文件")
    parser.add_argument("--session-max-steps", type=int, default=3,
                        help="SS-03: 搜索序列最大请求步数(默认 3)")
    parser.add_argument("--session-max-execs", type=int, default=200,
                        help="SS-03: 定向搜索执行预算(默认 200)")
    parser.add_argument("--session-online", action="store_true",
                        help="SS-07C: Atheris 运行中消费 Session 反馈(默认关)")
    parser.add_argument("--session-online-exploration", type=float, default=0.15,
                        help="SS-07C: 在线调度探索比例(默认 0.15)")
    parser.add_argument("--session-seed", type=int, default=None,
                        help="SS-03: 定向搜索随机种子(默认取 --seed)")
    parser.add_argument("--session-contract", default=None,
                        help="SS-07F: 预注册的真实应用 Session 合约 JSON")
    parser.add_argument("--max-runs", type=int, default=None,
                        help="SS-06: 可选的最大 Atheris 输入次数;默认不限制")
    parser.add_argument("--cost-ledger", action="store_true",
                        help="SS-07A: 记录真实 target HTTP 请求、序列和 reset 成本")
    parser.add_argument("--max-target-http-requests", type=int, default=None,
                        help="SS-07A: 硬请求上限，超过则在 target 调用前停止")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--arm", default="")
    parser.add_argument("--target-block", default="")
    parser.add_argument("--cost-out", default=None,
                        help="SS-07A: 独立计量目录(默认与 out_dir 相同)")
    args = parser.parse_args()

    Path(args.corpus_dir).mkdir(parents=True, exist_ok=True)
    if args.session_guided and args.mode == "off":
        parser.error("--session-guided 仅支持 --mode feedback/frontier")
    if args.session_online:
        if args.mode not in ("feedback", "frontier"):
            parser.error("--session-online 仅支持 --mode feedback/frontier")
        if not args.session_target_block or not args.session_request_pool:
            parser.error("--session-online 需要 --session-target-block "
                         "与 --session-request-pool")
    target = _load_target(args.target)
    import sys as _sys
    _target_module = _sys.modules["sf_target"]
    ledger = None
    if args.cost_ledger:
        from stateforge.runtime.execution_costs import ExecutionCostLedger
        ledger = ExecutionCostLedger(
            out_dir=(args.cost_out or args.out_dir),
            run_id=args.run_id or f"driver-{args.seed}",
            arm=args.arm, seed=args.seed, app=str(Path(args.target).parent),
            target_block=args.target_block,
            request_limit=args.max_target_http_requests)
        target = ledger.wrap_target(target, phase="atheris")

    # P0-A/P0-B:执行上下文(生命周期 + 观测器)。默认全关 = 行为与旧版
    # 逐字节一致(NoopLifecycle + 无观测);显式开启才引入隔离/证据,
    # 旧实验臂不受影响。约定缺失(reset/snapshot 无钩子)在首个输入前
    # 即刻失败——配置错误不允许静默退化成 noop。
    from stateforge.runtime.execution_context import (
        ExecutionContext,
        LifecycleError,
        audit_lifecycle_failure,
        lifecycle_from_module,
        observer_from_module,
    )
    execution = None
    state_ledger = None
    if args.lifecycle != "noop" or args.observe_state:
        from stateforge.state.runtime_observer import StateEvidenceLedger
        lifecycle = lifecycle_from_module(_target_module, args.lifecycle)
        observer = (observer_from_module(_target_module)
                    if args.observe_state else None)
        execution = ExecutionContext(lifecycle, observer)
        if args.observe_state:
            # 无观测器 → 显式退化标记(observer=""),epoch 侧凭
            # verified_atoms 为空可辨"未观测",不与"观测为零"混淆
            state_ledger = StateEvidenceLedger(
                observer_name=getattr(observer, "name", ""))

    def dispatch(data: bytes) -> None:
        # C10 分派：序列种子按序多步执行；其余输入原字节透传给 target
        # （legacy 单请求路径行为逐字节不变——四臂非回归门）。
        dispatch_seed(target, data)

    def _make_recorder():
        return ExperimentRecorder(args.out_dir, args.group, args.epoch)

    online = None
    if args.mode == "off":
        import time

        recorder = _make_recorder()

        def handler(data: bytes) -> None:
            t0 = time.monotonic()
            if ledger is not None:
                ledger.begin_sequence("atheris-input")
            try:
                if execution is not None:          # P0-A:off 模式同样走生命周期
                    execution.begin_run()
                    try:
                        dispatch(data)
                    except Exception:              # noqa: BLE001
                        pass
                    execution.finish_run(data)
                else:
                    dispatch(data)
            except LifecycleError as exc:          # fail-closed:审计后跳过
                audit_lifecycle_failure(args.out_dir, {
                    "group": args.group, "epoch": args.epoch, "mode": "off",
                    "lifecycle": execution.lifecycle.name
                    if execution is not None else "noop",
                    "error": str(exc)})
            except Exception:                # noqa: BLE001
                pass
            finally:
                if ledger is not None:
                    ledger.end_sequence()
            recorder.tick(time.monotonic() - t0, 0.0, 0, 0, False, None, 0)
    else:
        import json as _json
        ir = ProgramIR.from_json(args.ir)
        root = ir.metadata.get("root") or str(Path(args.target).parent)
        if args.session_guided:
            if not args.session_target_block or not args.session_request_pool:
                parser.error("--session-guided 需要 --session-target-block "
                             "与 --session-request-pool")
            from stateforge.fuzzing.session_guided import (
                bootstrap_session_guided, load_request_pool)
            pool = load_request_pool(args.session_request_pool)
            bootstrap_session_guided(
                _target_module, target, ir,
                source_dir=Path(args.target).resolve().parent,
                target_block=args.session_target_block,
                request_pool=pool, corpus_dir=args.corpus_dir,
                out_dir=args.out_dir, max_steps=args.session_max_steps,
                max_execs=args.session_max_execs,
                seed=(args.session_seed if args.session_seed is not None
                      else args.seed),
                session_contract=args.session_contract)
        online = None
        if args.session_online:
            from stateforge.state.session_access import extract_session_accesses
            from stateforge.state.session_index import (build_session_index,
                                                        query_session_frontier)
            from stateforge.fuzzing.session_online import (
                scheduler_from_frontier)
            source = Path(args.target).resolve().parent
            accesses = extract_session_accesses(source, ir=ir)
            index = build_session_index(accesses)
            frontier = query_session_frontier(
                ir, index, args.session_target_block, accesses)
            from stateforge.fuzzing.session_guided import load_request_pool
            online_pool = load_request_pool(args.session_request_pool)
            online = scheduler_from_frontier(
                frontier, target_block=args.session_target_block,
                request_pool=online_pool,
                out_dir=Path(args.out_dir) / "online",
                run_id=args.run_id or f"online-{args.seed}",
                ledger=ledger, max_steps=args.session_max_steps,
                seed=args.seed,
                exploration_ratio=args.session_online_exploration)
        else:
            online = None
        session = TraceSession(root=root)
        session.__enter__()
        # 反馈对象按模式先建(frontier 需要 load_state 恢复前沿/冠军)
        if args.mode == "frontier":
            from stateforge.fuzzing.feedback.frontier_feedback import FrontierDirectedFeedback
            feedback = FrontierDirectedFeedback(ir)
        else:
            feedback = StructuralFeedback()
        # 全局覆盖跨 epoch 持久化(采样时落盘,容忍 ~1 个采样间隔的尾部丢失)
        state_path = Path(args.out_dir) / "global_state.json"

        def _dump_state():
            if args.mode == "frontier":
                payload = feedback.export_state()       # 含 version/best_distance
            else:
                payload = {
                    "blocks": sorted(feedback.global_blocks),
                    "edges": [list(e) for e in sorted(feedback.global_edges)],
                }
            if state_ledger is not None:                # P0-B:仅 --observe-state
                payload["state_evidence"] = state_ledger.to_payload()
            state_path.write_text(_json.dumps(payload), encoding="utf-8")

        def _online_after(data: bytes, start: int) -> None:
            if online is None:
                return
            from stateforge.runtime.mapper import map_trace
            mapped = map_trace(session.events[start:], ir)
            view = None
            probe = getattr(_target_module, "session_view", None)
            if callable(probe):
                try:
                    view = probe()
                except Exception:                 # noqa: BLE001
                    view = None
            online.observe_execution(
                mapped, view, sequence_id=f"atheris-{online.decisions}",
                data=data)

        def _online_mutator(data: bytes, max_size: int, seed: int) -> bytes:
            return online.mutate(data, max_size, seed, atheris)

        recorder = ExperimentRecorder(args.out_dir, args.group, args.epoch,
                                       on_sample=_dump_state)
        if state_path.exists():
            prior = _json.loads(state_path.read_text(encoding="utf-8"))
            if args.mode == "frontier":
                feedback.load_state(prior)              # 恢复覆盖+前沿版本+冠军
            else:
                feedback.update_global_coverage(
                    set(prior.get("blocks", [])),
                    {tuple(e) for e in prior.get("edges", [])})
            if state_ledger is not None:                # P0-B:证据跨 epoch 结转
                state_ledger.load_payload(prior.get("state_evidence", {}))
        if args.mode == "frontier":
            from stateforge.fuzzing.frontier_collector import FrontierCollector
            corpus = CorpusManager(Path(args.out_dir) / "spool")
            collector = FrontierCollector(ir, session, feedback, corpus,
                                          recorder, args.epoch,
                                          on_save=_dump_state,
                                          execution=execution,
                                          state_ledger=state_ledger)

            def handler(data: bytes) -> None:
                start = len(session.events)
                if ledger is not None:
                    ledger.begin_sequence("atheris-input")
                try:
                    collector.handle(dispatch, data)
                    _online_after(data, start)
                finally:
                    if ledger is not None:
                        ledger.end_sequence()
        else:
            corpus = (CorpusManager(Path(args.out_dir) / "spool")
                      if args.mode == "feedback" else None)
            collector = CandidateCollector(ir, session, feedback, corpus,
                                           recorder, args.epoch,
                                           on_save=_dump_state,
                                           execution=execution,
                                           state_ledger=state_ledger)

            def handler(data: bytes) -> None:
                start = len(session.events)
                if ledger is not None:
                    ledger.begin_sequence("atheris-input")
                try:
                    collector.handle(dispatch, data)
                    _online_after(data, start)
                finally:
                    if ledger is not None:
                        ledger.end_sequence()

    # 原生覆盖必须开(否则盲变异、语料不增长);用选择性插桩只打目标模块——
    # instrument_all 会把解释器全量模块插桩,启动 ~30s 且与 tracer 冲突风险高。
    from stateforge.fuzzing.instrumentation import Instrumenter
    instrumenter = Instrumenter()
    n_instrumented = sum(
        instrumenter.instrument_callable(obj, atheris)
        for obj in vars(_target_module).values()
        if callable(obj) and getattr(obj, "__module__", "") == "sf_target")
    argv = [sys.argv[0], args.corpus_dir,
            f"-max_total_time={int(args.seconds)}", f"-seed={args.seed}",
            "-print_final_stats=1"]
    if args.max_runs is not None:
        if args.max_runs <= 0:
            parser.error("--max-runs 必须 > 0")
        argv.append(f"-runs={args.max_runs}")
    if ledger is not None:
        ledger.snapshot()
    if online is not None:
        atheris.Setup(argv, handler, custom_mutator=_online_mutator)
    else:
        atheris.Setup(argv, handler)
    atheris.Fuzz()                            # _Exit 结束,产物靠逐行 flush 存活
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
