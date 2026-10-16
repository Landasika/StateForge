"""vulnshop 运行时状态观测器(P0-B 靶侧适配,审计要求:适配层知识显式标记)。

从靶的文件 SQLite(``fuzz_target._DB``,应用真源)只读直查持久状态,
产出 ``RUNTIME_VERIFIED`` 观测;不读代码轨迹——覆盖代理
(``reached_state_atoms`` 的 SSTG requires 词汇,如 ``Session.authed_as``)
与运行时事实(DB 行)在此显式分离,两组词汇互不冒充,阶段 2 调度器
再负责对齐。

谓词口径(本观测器声明,非通用推断):

==================  ==========================  ==============
表                  谓词(自带取值)             scope / entity
==================  ==========================  ==============
users               ``User.exists``             user / username
users               ``User.role=<role>``        user / username
orders              ``Order.exists``            resource / oid
orders              ``Order.status=<STATUS>``   resource / oid
coupons             ``Coupon.used``             resource / code
==================  ==========================  ==============

诚实边界:vulnshop 无服务端会话存储(Bearer 无状态 token),故
``Session.*`` 谓词在本靶**运行时不可观测**——不观测、不伪造,保持
UNKNOWN(由台账缺位表达)。观测只反映 DB 真值(双极:role=admin 与
role=user 都是 verified 事实)。

调用时点契约:ExecutionContext.finish_run 在基线还原**之前**调用
observe(),故读到的是该输入执行结束时的真实终态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from stateforge.state.runtime_observer import (
    Evidence,
    SCOPE_RESOURCE,
    SCOPE_USER,
    StateKey,
    StateObservation,
)


class VulnshopStateObserver:
    """DB 只读直查 → verified 观测(靶场适配层,非通用算法)。

    每表一连接、finally 关闭:观测自身绝不持有跨输入连接(否则
    SnapshotLifecycle 的文件还原会被打开的句柄卡住)。
    """

    name = "vulnshop-db"

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _rows(self, sql: str) -> list[tuple]:
        """只读连接查询(URI mode=ro,防观测写状态;查完即关)。"""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def _verified(self, predicate: str, entity: str, scope: str,
                  value: bool, table: str) -> StateObservation:
        return StateObservation(
            key=StateKey(predicate, entity, scope), value=value,
            evidence=Evidence.RUNTIME_VERIFIED, source=f"db:{table}")

    def observe(self) -> list[StateObservation]:
        out: list[StateObservation] = []
        for username, role in self._rows(
                "SELECT username, role FROM users"):
            out.append(self._verified("User.exists", str(username),
                                      SCOPE_USER, True, "users"))
            out.append(self._verified(f"User.role={role}", str(username),
                                      SCOPE_USER, True, "users"))
        for oid, status in self._rows(
                "SELECT id, status FROM orders"):
            out.append(self._verified("Order.exists", str(oid),
                                      SCOPE_RESOURCE, True, "orders"))
            out.append(self._verified(f"Order.status={status}", str(oid),
                                      SCOPE_RESOURCE, True, "orders"))
        for code, used in self._rows(
                "SELECT code, used FROM coupons"):
            out.append(self._verified("Coupon.used", str(code),
                                      SCOPE_RESOURCE, bool(used), "coupons"))
        return out
