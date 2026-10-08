"""config.py —— LLM 环境变量统一入口(P1 收敛 + P2 legacy 族并入)。

唯一读取入口:RealLLMProvider 及后续 Phase 一律经
`from stateforge.config import llm_settings` 取值,legacy 双协议 Provider
(provider.py)经 `legacy_provider_settings` 取值,不散读 os.environ。
解析顺序:显式 overrides > 环境变量 > 内置默认(默认值=原实现内置,逐字保留)。
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


# ---------------------------------------------------------------- legacy 双协议族

@dataclass
class LegacyProviderSettings:
    """legacy 双协议 Provider(provider.py)的环境族快照。

    覆盖 ANTHROPIC_* + OPENAI_API_KEY + SF_LLM_{PROVIDER,BASE_URL,MODEL,
    MAX_CALLS,SSL_VERIFY}。默认值逐字对齐 provider.py 原内联语义,含其两个
    特性:model 同一变量(SF_LLM_MODEL)按分支各取缺省(claude-sonnet-5 /
    glm-4.6),空串环境值原样透传(仅"未配置"落缺省);openai_base_url 无
    缺省(未配置保持 None,由调用方保留原 AttributeError 崩溃路径)。
    """

    provider: str | None              # SF_LLM_PROVIDER ∈ {anthropic, openai-compatible}
    anthropic_api_key: str | None     # ANTHROPIC_API_KEY(x-api-key 直连)
    anthropic_auth_token: str | None  # ANTHROPIC_AUTH_TOKEN(Bearer;代理形态)
    anthropic_base_url: str           # ANTHROPIC_BASE_URL,缺省 https://api.anthropic.com,去尾 /
    openai_api_key: str | None        # OPENAI_API_KEY
    openai_base_url: str | None       # SF_LLM_BASE_URL(无缺省;去尾 / 由调用方做)
    anthropic_model: str              # SF_LLM_MODEL,anthropic 分支缺省 claude-sonnet-5
    openai_model: str                 # SF_LLM_MODEL,openai-compatible 分支缺省 glm-4.6
    max_calls: int                    # SF_LLM_MAX_CALLS,int 收敛,缺省 64
    ssl_verify: bool                  # SF_LLM_SSL_VERIFY,仅 "false"(忽略大小写)为 False


_LEGACY_ENV_VARS = {
    "provider": "SF_LLM_PROVIDER",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_auth_token": "ANTHROPIC_AUTH_TOKEN",
    "anthropic_base_url": "ANTHROPIC_BASE_URL",
    "openai_api_key": "OPENAI_API_KEY",
    "openai_base_url": "SF_LLM_BASE_URL",
}
_LEGACY_MODEL_ENV = "SF_LLM_MODEL"


def _pick_raw(override: str | None, env_name: str) -> str | None:
    """单字段解析(无默认变体):override 非 None 直接胜出;否则读环境变量,未配置为 None。"""
    if override is not None:
        return override
    return os.environ.get(env_name)


def legacy_provider_settings(
        **overrides: str | int | bool | None) -> LegacyProviderSettings:
    """解析 legacy Provider 配置:显式 overrides > 环境变量 > 内置默认。

    默认逐字对齐原 provider.py:str|None 族未配置即 None(无该凭据);
    anthropic_base_url 保留原 `值 or 缺省` 语义(override/环境值为空串同样落
    缺省)并去尾 "/";anthropic_model / openai_model 是同一 SF_LLM_MODEL 的
    两个分支视图,缺省各为 claude-sonnet-5 / glm-4.6,空串环境值原样透传;
    max_calls 按 int 收敛(小数字符串同样抛 ValueError,不静默截断),缺省 64;
    ssl_verify 取自 SF_LLM_SSL_VERIFY,仅 "false"(忽略大小写)为 False,缺省 True。
    """
    raw_max_calls = overrides.get("max_calls")
    if raw_max_calls is None:
        raw_max_calls = os.environ.get("SF_LLM_MAX_CALLS", "64")
    raw_verify = overrides.get("ssl_verify")
    if raw_verify is None:
        raw_verify = os.environ.get("SF_LLM_SSL_VERIFY", "true")
    return LegacyProviderSettings(
        provider=_pick_raw(overrides.get("provider"),
                           _LEGACY_ENV_VARS["provider"]),
        anthropic_api_key=_pick_raw(overrides.get("anthropic_api_key"),
                                    _LEGACY_ENV_VARS["anthropic_api_key"]),
        anthropic_auth_token=_pick_raw(overrides.get("anthropic_auth_token"),
                                       _LEGACY_ENV_VARS["anthropic_auth_token"]),
        anthropic_base_url=(_pick_raw(overrides.get("anthropic_base_url"),
                                      _LEGACY_ENV_VARS["anthropic_base_url"])
                            or "https://api.anthropic.com").rstrip("/"),
        openai_api_key=_pick_raw(overrides.get("openai_api_key"),
                                 _LEGACY_ENV_VARS["openai_api_key"]),
        openai_base_url=_pick_raw(overrides.get("openai_base_url"),
                                  _LEGACY_ENV_VARS["openai_base_url"]),
        anthropic_model=_pick(overrides.get("anthropic_model"),
                              _LEGACY_MODEL_ENV, "claude-sonnet-5"),
        openai_model=_pick(overrides.get("openai_model"),
                           _LEGACY_MODEL_ENV, "glm-4.6"),
        max_calls=int(raw_max_calls),
        ssl_verify=(raw_verify.lower() != "false" if isinstance(raw_verify, str)
                    else bool(raw_verify)),
    )
