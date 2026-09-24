"""Phase-2 fuzz 子进程入口:python -m stateforge.fuzz.entry_driver ...

模式:off(裸执行计数)| measure(+settrace 全局覆盖,不筛选)| feedback(+结构新颖度筛选落盘)。
产物逐行 flush 抗 libFuzzer _Exit;epoch 汇总由 EpochController 事后从产物重建。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import atheris

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.tracer import TraceSession
from stateforge.experiment.recorder import ExperimentRecorder
from stateforge.feedback.structural import StructuralFeedback
from stateforge.fuzz.candidates import CandidateCollector
from stateforge.fuzz.corpus_manager import CorpusManager


def _load_target(path: str):
    spec = importlib.util.spec_from_file_location("sf_target", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sf_target"] = mod
    spec.loader.exec_module(mod)
    return mod.target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")            # 目标 py 文件(含 target(data))
    parser.add_argument("ir")                # program_ir.json
    parser.add_argument("corpus_dir")        # 种子目录(libFuzzer 亦落盘于此)
    parser.add_argument("out_dir")           # recorder 产物目录
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--mode", choices=["off", "measure", "feedback"],
                        required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epoch", type=int, default=0)
    parser.add_argument("--group", default="x")
    args = parser.parse_args()

    Path(args.corpus_dir).mkdir(parents=True, exist_ok=True)
    target = _load_target(args.target)
    import sys as _sys
    _target_module = _sys.modules["sf_target"]

    def _make_recorder():
        return ExperimentRecorder(args.out_dir, args.group, args.epoch)

    if args.mode == "off":
        import time

        recorder = _make_recorder()

        def handler(data: bytes) -> None:
            t0 = time.monotonic()
            try:
                target(data)
            except Exception:                # noqa: BLE001
                pass
            recorder.tick(time.monotonic() - t0, 0.0, 0, 0, False, None, 0)
    else:
        import json as _json
        ir = ProgramIR.from_json(args.ir)
        root = ir.metadata.get("root") or str(Path(args.target).parent)
        session = TraceSession(root=root)
        session.__enter__()
        feedback = StructuralFeedback()
        # 全局覆盖跨 epoch 持久化(采样时落盘,容忍 ~1 个采样间隔的尾部丢失)
        state_path = Path(args.out_dir) / "global_state.json"

        def _dump_state():
            state_path.write_text(_json.dumps({
                "blocks": sorted(feedback.global_blocks),
                "edges": [list(e) for e in sorted(feedback.global_edges)],
            }), encoding="utf-8")

        recorder = ExperimentRecorder(args.out_dir, args.group, args.epoch,
                                       on_sample=_dump_state)
        if state_path.exists():
            prior = _json.loads(state_path.read_text(encoding="utf-8"))
            feedback.update_global_coverage(
                set(prior.get("blocks", [])),
                {tuple(e) for e in prior.get("edges", [])})
        corpus = (CorpusManager(Path(args.out_dir) / "spool")
                  if args.mode == "feedback" else None)
        collector = CandidateCollector(ir, session, feedback, corpus,
                                       recorder, args.epoch)

        def handler(data: bytes) -> None:
            collector.handle(target, data)

    # 原生覆盖必须开(否则盲变异、语料不增长);用选择性插桩只打目标模块——
    # instrument_all 会把解释器全量模块插桩,启动 ~30s 且与 tracer 冲突风险高。
    from stateforge.fuzz.instrumentation import Instrumenter
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
