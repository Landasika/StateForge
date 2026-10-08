"""Phase-2 模块 2:候选语料管理——内容哈希去重 + 原始字节持久化。

顾问约束:候选必须真实可回放——保存的是原始 bytes(不是评分或哈希),
下一轮直接作为 libFuzzer 种子目录输入;元数据另存 JSONL。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CorpusEntryMeta:
    input_id: str            # sha256 前 16 位
    epoch: int
    discovered_blocks: int
    discovered_edges: int
    new_blocks: int
    new_edges: int
    structural_score: int
    saved_at: float
    # Phase-3.1(可选;缺省兼容旧 meta 行)
    distance: int | None = None
    nearest_frontier: str | None = None
    interesting_reason: str = "new_structure"
    # Phase-4.2-F:lineage(区分 LLM direct gain vs descendant gain)
    origin: str = "NATIVE"            # NATIVE / LLM
    parent_seed_id: str | None = None
    llm_attempt_id: str | None = None


class CorpusManager:
    """spool 目录落盘新种子;内容哈希去重;导出下一轮种子目录。"""

    def __init__(self, spool_dir: str | Path):
        self.spool = Path(spool_dir)
        self.seeds_dir = self.spool / "seeds"
        self.meta_path = self.spool / "meta.jsonl"
        self.seeds_dir.mkdir(parents=True, exist_ok=True)
        self._seen_hashes: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        if self.meta_path.exists():
            for line in self.meta_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._seen_hashes.add(json.loads(line)["input_id"])

    @staticmethod
    def content_id(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:16]

    def save(self, data: bytes, epoch: int, new_blocks: int, new_edges: int,
             discovered_blocks: int, discovered_edges: int,
             distance: int | None = None, nearest_frontier: str | None = None,
             interesting_reason: str = "new_structure",
             origin: str = "NATIVE", parent_seed_id: str | None = None,
             llm_attempt_id: str | None = None) -> str | None:
        """有结构新颖性且未见过才保存;返回 input_id 或 None。"""
        cid = self.content_id(data)
        if cid in self._seen_hashes:
            return None
        self._seen_hashes.add(cid)
        score = new_blocks + new_edges
        (self.seeds_dir / cid).write_bytes(data)
        meta = CorpusEntryMeta(cid, epoch, discovered_blocks, discovered_edges,
                               new_blocks, new_edges, score, time.time(),
                               distance, nearest_frontier, interesting_reason,
                               origin, parent_seed_id, llm_attempt_id)
        with self.meta_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(meta), ensure_ascii=False) + "\n")
            fh.flush()
        return cid

    def entries(self) -> list[CorpusEntryMeta]:
        if not self.meta_path.exists():
            return []
        return [CorpusEntryMeta(**json.loads(line))
                for line in self.meta_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def export_seed_dir(self, out_dir: str | Path, extra: list[Path] | None = None
                        ) -> int:
        """把全部 SF 种子(+可选附加目录,如 libFuzzer 自落盘语料)拷成下一轮
        种子目录;返回种子数。C 组下一轮 = SF 种子 ∪ libFuzzer 结转。"""
        import shutil
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        count = 0
        for seed in self.seeds_dir.iterdir():
            shutil.copy2(seed, out / seed.name)
            count += 1
        for src_dir in extra or []:
            if not Path(src_dir).exists():
                continue
            for seed in Path(src_dir).iterdir():
                if seed.is_file() and not (out / seed.name).exists():
                    shutil.copy2(seed, out / seed.name)
                    count += 1
        return count
