"""vulnshop 运行时状态观测器(P0-B 靶侧适配 + PR-03 v2 结构化列)。

从靶的文件 SQLite(``fuzz_target._DB``,应用真源)只读直查持久状态,
产出 ``RUNTIME_VERIFIED`` 观测;不读代码轨迹——覆盖代理
(``reached_state_atoms`` 的 SSTG requires 词汇,如 ``Session.authed_as``)
与运行时事实(DB 行)在此显式分离,两组词汇互不冒充,阶段 2 调度器
再负责对齐。

谓词口径(本观测器声明,非通用推断;PR-03 增归属/标识两族):

=================  ==========================  ==============
表                  谓词(自带取值)             scope / entity
=================  ==========================  ==============
users               ``User.exists``             user / username
users               ``User.id=<uid>``           user / username
users               ``User.role=<role>``        user / username
orders              ``Order.exists``            resource / oid
orders              ``Order.status=<STATUS>``   resource / oid
orders              ``Order.user_id=<uid>``     resource / oid
coupons             ``Coupon.used``             resource / code
=================  ==========================  ==============

v2 可选列(契约 §2.3,全部 additive):
- ``subject`` 分解列;``observed_value`` 裸值;``value_kind`` ∈
  ENUM/NUMBER/BOOLEAN;``observed_at`` 缺省 -1(输入级观测,PR-04 才有
  逐请求序号)。
- ``User.id`` 与 ``Order.user_id`` 同为 NUMBER——``Order.user_id ==
  User.id`` 的归属关系验证(契约例 11)自此具备数据面。

可观测性声明(PR-03,属性级;契约 §5/R6):
- 逐属性精确声明上表七谓词;``Session.*`` 显式 NOT_OBSERVABLE
  (vulnshop Bearer 无状态 token,无服务端会话真源);
- 未声明属性(如 ``Order.owner``)→ UNPROBED——"Order.status 已声明
  不代表 Order.owner 自动可观测"(属性级边界,observability_of 语义)。

诚实边界:DB 里存在 user 行 ≠ 当前请求认证为该用户——认证主体关联
属绑定来源(BindingEnv provenance)职责,本观测器不产任何"已认证"
断言(契约 §6.6.4/PR-03 反例 8)。

调用时点契约:ExecutionContext.finish_run 在基线还原**之前**调用
observe(),故读到的是该输入执行结束时的真实终态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from stateforge.state.runtime_observer import (
    Evidence,
    NOT_OBSERVABLE,
    OBSERVABLE,
    SCOPE_RESOURCE,
    SCOPE_USER,
    StateKey,
    StateObservation,
    VALUE_KIND_BOOLEAN,
    VALUE_KIND_ENUM,
    VALUE_KIND_NUMBER,
)


class VulnshopStateObserver:
    """DB 只读直查 → verified 观测(靶场适配层,非通用算法)。

    每表一连接、finally 关闭:观测自身绝不持有跨输入连接(否则
    SnapshotLifecycle 的文件还原会被打开的句柄卡住)。
    """

    name = "vulnshop-db"

    # 属性级可观测性声明(PR-03;精确键,无家族通配回填)
    subject_families = {
        "User.exists": OBSERVABLE,
        "User.id": OBSERVABLE,
        "User.role": OBSERVABLE,
        "Order.exists": OBSERVABLE,
        "Order.status": OBSERVABLE,
        "Order.user_id": OBSERVABLE,
        "Coupon.used": OBSERVABLE,
        "Session.*": NOT_OBSERVABLE,
    }

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
                  value: bool, table: str, *, subject: str | None = None,
                  observed_value: object = None,
                  value_kind: str | None = None) -> StateObservation:
        return StateObservation(
            key=StateKey(predicate, entity, scope), value=value,
            evidence=Evidence.RUNTIME_VERIFIED, source=f"db:{table}",
            subject=subject, observed_value=observed_value,
            value_kind=value_kind)

    def observe(self) -> list[StateObservation]:
        out: list[StateObservation] = []
        for username, uid, role in self._rows(
                "SELECT username, id, role FROM users"):
            out.append(self._verified("User.exists", str(username),
                                      SCOPE_USER, True, "users",
                                      subject="User.exists",
                                      value_kind=VALUE_KIND_BOOLEAN))
            out.append(self._verified(f"User.id={uid}", str(username),
                                      SCOPE_USER, True, "users",
                                      subject="User.id",
                                      observed_value=uid,
                                      value_kind=VALUE_KIND_NUMBER))
            out.append(self._verified(f"User.role={role}", str(username),
                                      SCOPE_USER, True, "users",
                                      subject="User.role",
                                      observed_value=role,
                                      value_kind=VALUE_KIND_ENUM))
        for oid, uid, status in self._rows(
                "SELECT id, user_id, status FROM orders"):
            out.append(self._verified("Order.exists", str(oid),
                                      SCOPE_RESOURCE, True, "orders",
                                      subject="Order.exists",
                                      value_kind=VALUE_KIND_BOOLEAN))
            out.append(self._verified(f"Order.status={status}", str(oid),
                                      SCOPE_RESOURCE, True, "orders",
                                      subject="Order.status",
                                      observed_value=status,
                                      value_kind=VALUE_KIND_ENUM))
            out.append(self._verified(f"Order.user_id={uid}", str(oid),
                                      SCOPE_RESOURCE, True, "orders",
                                      subject="Order.user_id",
                                      observed_value=uid,
                                      value_kind=VALUE_KIND_NUMBER))
        for code, used in self._rows(
                "SELECT code, used FROM coupons"):
            out.append(self._verified("Coupon.used", str(code),
                                      SCOPE_RESOURCE, bool(used), "coupons",
                                      subject="Coupon.used",
                                      observed_value=bool(used),
                                      value_kind=VALUE_KIND_BOOLEAN))
        return out
