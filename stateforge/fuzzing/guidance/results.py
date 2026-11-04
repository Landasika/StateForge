"""Campaign result records and on-disk evidence writer.

落盘契约(每次 campaign 一个目录):``config.json``(含环境与初始语料 id)、
``metrics.jsonl``(逐次执行一行)、``summary.json``(末次汇总)、
``corpus/``(保留种子)、``crashes/``(异常输入 + 元数据)、
``target_archive/``(已到目标但被裁剪的种子)。全部为追加或原子替换写,
中途中断不会留下半截 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any


def environment_info() -> dict[str, Any]:
    """记录环境指纹:换机器/换依赖后结果不可比,必须随结果一起留证。"""
    dependencies: dict[str, str] = {}
    for name in ("stateforge", "PyYAML", "atheris", "pytest", "flask"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "dependencies": dependencies,
    }


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class CampaignResult:
    target_block_id: str
    target_reached: bool
    executions: int
    executions_to_target: int | None
    wall_clock_to_target: float | None
    best_distance: int | None
    unique_covered_blocks: int
    unique_valid_icfg_edges: int
    retained_seeds: int
    unexpected_exceptions: int
    mapping_failures: int
    mapping_failure_rate: float
    termination_reason: str
    elapsed_seconds: float
    target_input_id: str | None = None
    output_dir: str | None = None
    archived_targets: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_basic_block": self.target_block_id,
            "target_reached": self.target_reached,
            "executions": self.executions,
            "executions_to_target": self.executions_to_target,
            "wall_clock_time_to_target_seconds": self.wall_clock_to_target,
            "best_distance": self.best_distance,
            "unique_covered_blocks": self.unique_covered_blocks,
            "unique_valid_icfg_edges": self.unique_valid_icfg_edges,
            "retained_seeds": self.retained_seeds,
            "unexpected_exceptions": self.unexpected_exceptions,
            "mapping_failures": self.mapping_failures,
            "mapping_failure_rate": self.mapping_failure_rate,
            "termination_reason": self.termination_reason,
            "elapsed_seconds": self.elapsed_seconds,
            "target_reaching_input_id": self.target_input_id,
            "output_dir": self.output_dir,
            "archived_target_seeds": self.archived_targets,
        }


class CampaignReporter:
    def __init__(self, output_dir: str | Path, config: Any,
                 target_block_id: str, source_hash: str | None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.corpus_dir = self.output_dir / "corpus"
        self.crashes_dir = self.output_dir / "crashes"
        self.target_archive_dir = self.output_dir / "target_archive"
        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.crashes_dir.mkdir(parents=True, exist_ok=True)
        self.target_archive_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.output_dir / "metrics.jsonl"
        self.config_path = self.output_dir / "config.json"
        self.summary_path = self.output_dir / "summary.json"
        self.target_input_path = self.output_dir / "target_reaching_input.bin"
        self._metrics = self.metrics_path.open("w", encoding="utf-8")
        self._config = config
        self._target_block_id = target_block_id
        self._source_hash = source_hash
        self._started = time.time()

    def start(self, initial_seed_ids: list[str],
              scheduler: dict[str, Any] | None = None,
              mutation: dict[str, Any] | None = None) -> None:
        if self.target_input_path.exists():
            self.target_input_path.unlink()
        for directory in (self.corpus_dir, self.crashes_dir,
                          self.target_archive_dir):
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink()
        payload = {
            "target_basic_block": self._target_block_id,
            "source_hash": self._source_hash,
            "started_at": self._started,
            "initial_corpus": initial_seed_ids,
            "config": self._config.to_dict(),
            "scheduler": scheduler,
            "mutation": mutation,
            "environment": environment_info(),
        }
        _write_json_atomic(self.config_path, payload)

    def record(self, record: dict[str, Any]) -> None:
        self._metrics.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._metrics.flush()

    def save_seed(self, seed: Any) -> Path:
        path = self.corpus_dir / f"{seed.seed_id}.bin"
        if not path.exists():
            path.write_bytes(seed.data)
        return path

    def save_archived_target(self, seed: Any) -> Path:
        path = self.target_archive_dir / f"{seed.seed_id}.bin"
        if not path.exists():
            path.write_bytes(seed.data)
        return path

    def save_crash(self, input_id: str, data: bytes, error: str | None) -> Path:
        path = self.crashes_dir / f"{input_id}.bin"
        if not path.exists():
            path.write_bytes(data)
        metadata = self.crashes_dir / f"{input_id}.json"
        _write_json_atomic(metadata, {"input_id": input_id, "error": error})
        return path

    def save_target_input(self, input_id: str, data: bytes) -> Path:
        # 只留第一条到达目标的输入:它是复现证据,后续命中不覆盖。
        if not self.target_input_path.exists():
            self.target_input_path.write_bytes(data)
        return self.target_input_path

    def write_summary(self, summary: dict[str, Any]) -> None:
        _write_json_atomic(self.summary_path, summary)

    def close(self) -> None:
        if not self._metrics.closed:
            self._metrics.flush()
            self._metrics.close()
