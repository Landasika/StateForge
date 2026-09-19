"""idioms.py —— SF1-T4 惯用法字典(纯数据;新增框架只加数据不动 matcher)。

加载 idioms.yaml;表驱动 patterns 的可分派知识:
  row_entities   行变量名 → 实体(vulnshop 的 order/coupon + VAmPI 的 user/book)
  authn_calls    认证链函数名(产/需 Session.authed_as)
  single_use     单次性属性名模式
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

_DEFAULT_YAML = """
row_entities:
  order: Order
  coupon: Coupon
  user: User
  book: Book
  newBook: Book
  target: User
authn_calls:
  produce: [make_token, encode_auth_token, jwt.encode]
  require: [current_user, token_validator, decode_auth_token, require_user]
single_use_attrs: [used, consumed, redeemed, redeemed_at]
"""

_cache: dict | None = None


@functools.lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    global _cache
    if path is None:
        data = yaml.safe_load(_DEFAULT_YAML)
    else:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    _cache = data
    return data


def row_entity(var: str) -> str | None:
    return load().get("row_entities", {}).get(var)


def single_use_attrs() -> list[str]:
    return load().get("single_use_attrs", [])
