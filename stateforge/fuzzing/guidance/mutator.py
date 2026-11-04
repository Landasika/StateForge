"""Deterministic byte-level mutator for directed campaigns.

只做与结构无关的字节算符:目标是"在固定预算下稳定复现",不是变异质量本身。
所有随机都走注入的 ``random.Random``,同种子必得同序列——实验可复现性
依赖这条,不要在算符里引入 ``random`` 模块级状态或时间。
"""

from __future__ import annotations

import random


class BasicMutator:
    def __init__(self, random_seed: int = 0, rng: random.Random | None = None):
        self.random_seed = random_seed
        self._rng = rng if rng is not None else random.Random(random_seed)
        self.operators = (
            "bit_flip",
            "byte_replace",
            "byte_insert",
            "byte_delete",
            "range_overwrite",
            "splice",
        )

    def mutate(self, data: bytes, *, max_size: int,
               other: bytes | None = None) -> bytes:
        if max_size <= 0:
            return b""
        base = bytes(data[:max_size])
        if not base:
            return self._empty_case(max_size, other)
        available = list(self.operators)
        if not other:
            available.remove("splice")
        operator = self._rng.choice(available)
        result = getattr(self, f"_{operator}")(base, max_size, other)
        result = result[:max_size]
        # 保证"变异真的发生了":否则同一父种会反复产出同一个子种,
        # 预算被空转消耗,且距离不会有任何变化。
        if result == base:
            if len(result) < max_size:
                result = result + bytes([self._rng.randrange(256)])
            elif result:
                mutable = bytearray(result)
                mutable[-1] ^= 1 << self._rng.randrange(8)
                result = bytes(mutable)
        return result

    def _empty_case(self, max_size: int, other: bytes | None) -> bytes:
        if other and self._rng.random() < 0.5:
            candidate = bytes(other[:max_size])
            if candidate:
                return candidate
        length = self._rng.randint(1, min(4, max_size))
        return bytes(self._rng.randrange(256) for _ in range(length))

    def _bit_flip(self, data: bytes, max_size: int,
                  other: bytes | None) -> bytes:
        mutable = bytearray(data)
        index = self._rng.randrange(len(mutable))
        mutable[index] ^= 1 << self._rng.randrange(8)
        return bytes(mutable)

    def _byte_replace(self, data: bytes, max_size: int,
                      other: bytes | None) -> bytes:
        mutable = bytearray(data)
        index = self._rng.randrange(len(mutable))
        mutable[index] = (mutable[index] + self._rng.randrange(1, 256)) & 0xFF
        return bytes(mutable)

    def _byte_insert(self, data: bytes, max_size: int,
                     other: bytes | None) -> bytes:
        if len(data) >= max_size:
            return self._byte_replace(data, max_size, other)
        mutable = bytearray(data)
        index = self._rng.randrange(len(mutable) + 1)
        mutable.insert(index, self._rng.randrange(256))
        return bytes(mutable)

    def _byte_delete(self, data: bytes, max_size: int,
                     other: bytes | None) -> bytes:
        mutable = bytearray(data)
        index = self._rng.randrange(len(mutable))
        del mutable[index]
        return bytes(mutable)

    def _range_overwrite(self, data: bytes, max_size: int,
                         other: bytes | None) -> bytes:
        mutable = bytearray(data)
        start = self._rng.randrange(len(mutable))
        limit = min(8, len(mutable) - start)
        length = self._rng.randint(1, limit)
        for offset in range(length):
            index = start + offset
            mutable[index] = (mutable[index]
                              + self._rng.randrange(1, 256)) & 0xFF
        if self._rng.random() < 0.25 and len(mutable) < max_size:
            extra = self._rng.randint(1, min(4, max_size - len(mutable)))
            mutable[start + length:start + length] = bytes(
                self._rng.randrange(256) for _ in range(extra))
        return bytes(mutable)

    def _splice(self, data: bytes, max_size: int,
                other: bytes | None) -> bytes:
        if not other:
            return self._byte_replace(data, max_size, other)
        donor = bytes(other[:max_size])
        left = self._rng.randrange(len(data) + 1)
        right = self._rng.randrange(len(donor) + 1)
        candidate = data[:left] + donor[right:]
        if not candidate:
            return self._empty_case(max_size, other)
        return candidate[:max_size]
