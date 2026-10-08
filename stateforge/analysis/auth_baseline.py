"""auth_baseline.py —— 已废弃(2026-09-21 白名单移除)。

原实现用路由/实体关键词启发补 Session.authed_as——已删。
auth 链的正式通道 = LLM relate() 的 auth_chains 输出。
此文件保留为空壳以避免 import 断裂;后续可删。
"""

from __future__ import annotations

from stateforge.core.model import AppModel


def apply_auth_baseline(model: AppModel) -> int:
    """no-op:auth 链由 LLM relate() 提供(或缓存)。"""
    return 0
