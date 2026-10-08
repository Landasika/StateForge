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
    args = parser.parse_args()

    Path(args.corpus_dir).mkdir(parents=True, exist_ok=True)
    target = _load_target(args.target)
    import sys as _sys
    _target_module = _sys.modules["sf_target"]

    def dispatch(data: bytes) -> None:
        # C10 分派：序列种子按序多步执行；其余输入原字节透传给 target
        # （legacy 单请求路径行为逐字节不变——四臂非回归门）。
        dispatch_seed(target, data)

    def _make_recorder():
        return ExperimentRecorder(args.out_dir, args.group, args.epoch)

    if args.mode == "off":
        import time

        recorder = _make_recorder()

        def handler(data: bytes) -> None:
            t0 = time.monotonic()
            try:
                dispatch(data)
            except Exception:                # noqa: BLE001
                pass
            recorder.tick(time.monotonic() - t0, 0.0, 0, 0, False, None, 0)
    else:
        import json as _json
        ir = ProgramIR.from_json(args.ir)
        root = ir.metadata.get("root") or str(Path(args.target).parent)
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
            state_path.write_text(_json.dumps(payload), encoding="utf-8")

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
        if args.mode == "frontier":
            from stateforge.fuzzing.frontier_collector import FrontierCollector
            corpus = CorpusManager(Path(args.out_dir) / "spool")
            collector = FrontierCollector(ir, session, feedback, corpus,
                                          recorder, args.epoch,
                                          on_save=_dump_state)

            def handler(data: bytes) -> None:
                collector.handle(dispatch, data)
        else:
            corpus = (CorpusManager(Path(args.out_dir) / "spool")
                      if args.mode == "feedback" else None)
            collector = CandidateCollector(ir, session, feedback, corpus,
                                           recorder, args.epoch,
                                           on_save=_dump_state)

            def handler(data: bytes) -> None:
                collector.handle(dispatch, data)

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
    atheris.Setup(argv, handler)
    atheris.Fuzz()                            # _Exit 结束,产物靠逐行 flush 存活
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
