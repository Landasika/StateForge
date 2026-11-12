"""Deterministic mutator for directed campaigns.

Two mutation modes share one contract: same seed, same sequence of outputs.
Reproducibility is what experiments rest on, so no operator may reach for
module-level ``random`` state or the clock.

**Byte mode** (default) is structure-blind. It suits opaque payloads and is
the wrong choice for structured input: flipping bits inside a JSON document
usually destroys the syntax, the adapter then falls back to its default
request, and the budget is spent re-executing the same no-op.  Measured on
this repo's own seeds, 73.9% of byte mutations of a JSON request fail to
parse -- i.e. three quarters of the budget never reaches the target's logic.

**Structured mode** is enabled by passing a ``codec``: any object with
``decode(bytes) -> object | None`` and ``encode(object) -> bytes``.  A codec
returning ``None`` from ``decode`` declares "this input is not mine", and the
mutator transparently falls back to byte mode for that candidate, so a
campaign may mix structured and opaque seeds.  The operators walk generic
containers only (maps, sequences, scalars) and never look for a field name,
which keeps this layer free of target knowledge.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any, Protocol


class Codec(Protocol):
    """Turn bytes into a structured value and back.

    ``decode`` returning ``None`` means the input is not in this codec's
    format; the caller then treats the bytes opaquely.
    """

    def decode(self, data: bytes) -> Any | None: ...

    def encode(self, value: Any) -> bytes: ...


class JsonCodec:
    """The codec every JSON-speaking adapter can use unchanged."""

    def decode(self, data: bytes) -> Any | None:
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def encode(self, value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False).encode("utf-8")


class BasicMutator:
    def __init__(self, random_seed: int = 0, rng: random.Random | None = None,
                 codec: Codec | None = None):
        self.random_seed = random_seed
        self._rng = rng if rng is not None else random.Random(random_seed)
        self.codec = codec
        self.operators = (
            "bit_flip",
            "byte_replace",
            "byte_insert",
            "byte_delete",
            "range_overwrite",
            "splice",
        )
        # Listed separately so a caller can see which mode is actually active
        # rather than inferring it from whether ``codec`` was passed.
        self.structured_operators = (
            "leaf_replace",
            "leaf_toggle",
            "leaf_delete",
            "leaf_insert",
            "subtree_splice",
        )

    def mutate(self, data: bytes, *, max_size: int,
               other: bytes | None = None) -> bytes:
        if max_size <= 0:
            return b""
        base = bytes(data[:max_size])
        if self.codec is not None and base:
            structured = self._mutate_structured(base, max_size, other)
            if structured is not None:
                return structured[:max_size]
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

    # -- structured mode -------------------------------------------------
    #
    # Every operator edits a decoded value in place on a copy and encodes the
    # result.  The only shape assumption is "dict / list / scalar", which is
    # what any structured codec yields; nothing here knows what the keys mean.

    def _mutate_structured(self, base: bytes, max_size: int,
                           other: bytes | None) -> bytes | None:
        """Return encoded bytes, or ``None`` to fall back to byte mode."""
        value = self.codec.decode(base)
        if value is None:
            return None
        # A scalar root has no addressable slot; byte mode handles it better.
        if not isinstance(value, (dict, list)):
            return None
        donor = self.codec.decode(other) if other else None
        available = list(self.structured_operators)
        if donor is None:
            available.remove("subtree_splice")
        # An operator can legitimately find nothing to edit (empty container,
        # single-key map, no nested slot).  Retrying a different operator is
        # strictly better than falling back to byte mode, which would throw
        # away the guarantee this mode exists for: the output still parses.
        self._rng.shuffle(available)
        for operator in available:
            target = copy.deepcopy(value)
            if not getattr(self, f"_{operator}")(target, other):
                continue
            try:
                encoded = self.codec.encode(target)
            except (TypeError, ValueError):
                continue
            if encoded != base:
                return encoded
        return None

    def _slots(self, container: Any) -> list[tuple[Any, Any]]:
        """Addressable ``(container, key)`` pairs, depth-first."""
        out: list[tuple[Any, Any]] = []
        stack: list[Any] = [container]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in list(node.keys()):
                    out.append((node, key))
                    if isinstance(node[key], (dict, list)):
                        stack.append(node[key])
            elif isinstance(node, list):
                for index in range(len(node)):
                    out.append((node, index))
                    if isinstance(node[index], (dict, list)):
                        stack.append(node[index])
        return out

    def _leaf_replace(self, target: Any, other: bytes | None) -> bool:
        slots = self._slots(target)
        if not slots:
            return False
        container, key = self._rng.choice(slots)
        container[key] = self._random_scalar()
        return True

    def _leaf_toggle(self, target: Any, other: bytes | None) -> bool:
        """Flip a value only where a flip is meaningful, else replace."""
        slots = [slot for slot in self._slots(target)
                 if isinstance(slot[0][slot[1]], (bool, int))]
        if not slots:
            return self._leaf_replace(target, other)
        container, key = self._rng.choice(slots)
        current = container[key]
        if isinstance(current, bool):
            container[key] = not current
        else:
            container[key] = current + self._rng.choice((-1, 1))
        return True

    def _leaf_delete(self, target: Any, other: bytes | None) -> bool:
        # Top-level keys are the request's envelope: dropping one usually
        # makes the adapter reject the whole input and re-execute its default
        # request, which is a wasted execution rather than a novel path.  Only
        # nested payload is deleted; a flat document has nothing this operator
        # may safely remove, so it declines and another operator runs.
        nested = [slot for slot in self._slots(target) if slot[0] is not target]
        if not nested:
            return False
        container, key = self._rng.choice(nested)
        if isinstance(container, dict) and len(container) <= 1:
            return False
        if isinstance(container, list) and len(container) <= 1:
            return False
        del container[key]
        return True

    def _leaf_insert(self, target: Any, other: bytes | None) -> bool:
        slots = self._slots(target)
        containers = [slot for slot in slots
                      if isinstance(slot[0][slot[1]], (dict, list))]
        if containers:
            parent, key = self._rng.choice(containers)
            node = parent[key]
            if isinstance(node, dict):
                node[self._random_key(node)] = self._random_scalar()
            else:
                node.append(self._random_scalar())
            return True
        if isinstance(target, dict):
            target[self._random_key(target)] = self._random_scalar()
            return True
        if isinstance(target, list):
            target.append(self._random_scalar())
            return True
        return False

    def _subtree_splice(self, target: Any, other: bytes | None) -> bool:
        """Graft a subtree from a donor seed at a random slot."""
        donor = self.codec.decode(other) if other else None
        if donor is None:
            return False
        donor_slots = self._slots(donor)
        if not donor_slots:
            return False
        # ``_slots`` yields (container, key) pairs, and the container is often
        # nested -- indexing ``donor`` directly would raise on any key that
        # lives below the root.
        donor_container, donor_key = self._rng.choice(donor_slots)
        graft = copy.deepcopy(donor_container[donor_key])
        slots = self._slots(target)
        if slots:
            container, key = self._rng.choice(slots)
            container[key] = graft
            return True
        if isinstance(target, dict):
            target[self._random_key(target)] = graft
            return True
        return False

    def _random_scalar(self) -> Any:
        choice = self._rng.randrange(5)
        if choice == 0:
            return self._rng.choice((0, 1, -1, 7, 255, 65535, 2**31 - 1))
        if choice == 1:
            return self._rng.choice((True, False))
        if choice == 2:
            return ""
        if choice == 3:
            return bytes(self._rng.randrange(32, 127)
                         for _ in range(self._rng.randint(1, 6))).decode()
        return self._rng.choice(("../../etc/passwd", "%00", "1' OR '1'='1",
                                 "<script>", "A" * self._rng.randint(2, 16)))

    def _random_key(self, existing: dict) -> str:
        for _ in range(8):
            key = self._rng.choice(("id", "name", "q", "page", "limit", "value",
                                    "data", "key", "token", "path", "type"))
            if key not in existing:
                return key
        return f"k{self._rng.randrange(1 << 16)}"

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
