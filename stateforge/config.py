"""config.py —— STATEFORGE_LLM_* 环境变量统一入口(P1 收敛)。

唯一读取入口:RealLLMProvider 及后续 Phase 一律经
`from stateforge.config import llm_settings` 取值,不散读 os.environ。
解析顺序:显式 overrides > 环境变量 > 内置默认(默认值=原 RealLLMProvider 内置,逐字保留)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class LLMSettings:
    """LLM 调用四要素。api_key 为空串视为未配置(调用方走规则降级)。"""

    api_key: str | None
    base_url: str | None
    model: str | None
    timeout: float


_DEFAULTS = {
    "api_key": "",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4-flash",
}
_DEFAULT_TIMEOUT = 30
_ENV_VARS = {
    "api_key": "STATEFORGE_LLM_API_KEY",
    "base_url": "STATEFORGE_LLM_BASE_URL",
    "model": "STATEFORGE_LLM_MODEL",
}


def _pick(override: str | None, env_name: str, default: str) -> str:
    """单字段解析:override 非 None 直接胜出;否则读环境变量;最后内置默认。"""
    if override is not None:
        return override
    return os.environ.get(env_name, default)


def llm_settings(**overrides: str | float | None) -> LLMSettings:
    """解析 LLM 配置:显式 overrides > STATEFORGE_LLM_* 环境变量 > 内置默认。

    timeout:字符串值(含环境变量)按 int 收敛——与原 RealLLMProvider 语义一致,
    小数字符串同样抛 ValueError,不静默截断;数值 override 按 float 透传。
    base_url 统一去掉尾部 "/",调用方可直接拼接 API 路径。
    """
    raw_timeout = overrides.get("timeout")
    if raw_timeout is None:
        raw_timeout = os.environ.get("STATEFORGE_LLM_TIMEOUT", str(_DEFAULT_TIMEOUT))
    timeout = int(raw_timeout) if isinstance(raw_timeout, str) else float(raw_timeout)
    return LLMSettings(
        api_key=_pick(overrides.get("api_key"), _ENV_VARS["api_key"],
                      _DEFAULTS["api_key"]),
        base_url=_pick(overrides.get("base_url"), _ENV_VARS["base_url"],
                       _DEFAULTS["base_url"]).rstrip("/"),
        model=_pick(overrides.get("model"), _ENV_VARS["model"],
                    _DEFAULTS["model"]),
        timeout=timeout,
    )
