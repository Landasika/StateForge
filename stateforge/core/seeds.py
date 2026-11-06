"""C10 sequence seed codec. Pure data contract; no execution dependencies."""

from __future__ import annotations

import json


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


