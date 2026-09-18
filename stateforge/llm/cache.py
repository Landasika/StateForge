"""cache.py —— SF6-T1 磁盘缓存:sha256(候选事实+提示词版本) 为键。

同输入二次调用零 token;换提示词版本自动失效。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"


def _key(facts: dict | list, prompt_version: str) -> str:
    blob = json.dumps({"v": prompt_version, "facts": facts}, sort_keys=True,
                      ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def get(facts, prompt_version: str) -> str | None:
    p = CACHE_DIR / f"{_key(facts, prompt_version)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["text"]
    return None


def put(facts, prompt_version: str, text: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{_key(facts, prompt_version)}.json"
    p.write_text(json.dumps({"text": text, "prompt_version": prompt_version},
                            ensure_ascii=False), encoding="utf-8")


def stats() -> dict:
    if not CACHE_DIR.exists():
        return {"entries": 0}
    return {"entries": len(list(CACHE_DIR.glob("*.json")))}
