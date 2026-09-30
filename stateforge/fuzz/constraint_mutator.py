"""Phase-4.1B Level 6:ConstraintMutator——把 MutationHint 写回种子。

结构化种子 = {"query": {k: str}, "form": {k: str}, "json": {k: any}}。
原则(冻结):只改目标字段,其余逐字节不变;字段不存在 → FIELD_NOT_FOUND,
不新增字段(禁止顺手重建请求)。
"""

from __future__ import annotations

import json

from stateforge.feasibility.mutation_hint import MutationHint

_SECTION = {"QUERY": "query", "FORM": "form", "JSON": "json",
            "HEADER": "header", "COOKIE": "cookie"}


class MutationError(ValueError):
    pass


def serialize_for(kind: str, value) -> object:
    """冻结的不动点序列化:QUERY/FORM/HEADER/COOKIE 为字符串域
    (str(target) 经 strip/lower/upper/int/float 链后仍得 target 的
    固定点);JSON 保原生类型。"""
    return str(value) if kind in ("QUERY", "FORM", "HEADER", "COOKIE") else value


def apply_hint(seed: dict, hint: MutationHint) -> dict:
    """返回新种子(深拷贝后仅改目标字段)。"""
    import copy
    if hint.reason != "solved" or hint.target_value is None:
        raise MutationError(f"hint 未求解: {hint.reason}")
    section = _SECTION.get(hint.source_kind)
    if section is None or section not in seed:
        raise MutationError(f"种子缺少段: {hint.source_kind}")
    fields = seed[section]
    if hint.source_key not in fields:
        raise MutationError(
            f"FIELD_NOT_FOUND: {hint.source_kind}[{hint.source_key}]")
    out = copy.deepcopy(seed)
    out[section][hint.source_key] = serialize_for(hint.source_kind,
                                                  hint.target_value)
    return out


def seed_to_bytes(seed: dict) -> bytes:
    return json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
