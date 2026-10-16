"""P0-B 运行时状态观测契约 —— 三值证据,防"代理冒充事实"。

背景(阶段 1 任务书):采集器(CandidateCollector/FrontierCollector)原先
只有代码轨迹反馈(覆盖代理),没有真实状态观测;``TraceSession.request()``
隔离的是**追踪事件的收集范围**,不代表应用数据库/会话/全局变量已恢复,
不能把 trace 隔离当作状态隔离。本模块定义二者之间的证据契约:

- ``StateKey = (Predicate, EntityBinding, Scope)``:状态真值的唯一键。
  状态真值 ≠ 状态新颖:连续两次登录都产 ``authenticated=True``,第二次
  不应是新状态;但同一谓词在不同用户/资源/权限上下文中成立,也不能简单
  合并——entity 与 scope 就是为此进键的。
- ``Evidence`` 三值:``RUNTIME_VERIFIED``(从真实应用状态独立确认,如
  DB 行/会话读回)/ ``CONTROL_FLOW_PROXY``(仅有控制流证据——守卫真
  分支块被覆盖,持久状态未经独立确认;``reached_state_atoms`` 口径)/
  ``UNKNOWN``(无法观测;亦以"无观测记录"表达,不虚构)。
- ``StateEvidenceLedger``:跨输入累积 verified 观测,渲染进
  ``global_state.json`` 的 ``state_evidence`` 块(仅 ``--observe-state``
  时存在);覆盖代理原子仍由注入器口径单独记账,epoch 台账分记
  ``proxy_reached_atoms`` / ``verified_reached_atoms``,互不冒充。

初版边界(任务书):不构造完整数据库状态哈希;只实现能从运行时可靠
观测的谓词,其余谓词保持 UNKNOWN。值翻转不记新证据(键值对首次出现
即新颖;值历史留给阶段 2 调度器)。

分层(L1.5 state):纯数据契约 + 协议,零 import 本包其他域
(ARCHITECTURE.md 分层,向上引用一律违规)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable


# ------------------------------------------------------------- 证据三值

class Evidence(str, Enum):
    RUNTIME_VERIFIED = "runtime_verified"      # 运行时从应用真源独立确认
    CONTROL_FLOW_PROXY = "control_flow_proxy"  # 仅守卫真分支覆盖,未确认持久状态
    UNKNOWN = "unknown"                        # 无法观测(显式标记,不虚构)


# ------------------------------------------------------------- 作用域词汇

SCOPE_SESSION = "session"    # 会话内(如 Session.authenticated)
SCOPE_USER = "user"          # 用户上下文(如 User.role=admin)
SCOPE_RESOURCE = "resource"  # 资源实例(如 Order.status=PAID 的订单 id)
SCOPE_GLOBAL = "global"      # 全局(无实体绑定)
SCOPE_UNKNOWN = "unknown"    # 代理推断无法分辨作用域时显式降级
SCOPES = frozenset({SCOPE_SESSION, SCOPE_USER, SCOPE_RESOURCE,
                    SCOPE_GLOBAL, SCOPE_UNKNOWN})


@dataclass(frozen=True)
class StateKey:
    """状态键 = (谓词, 实体绑定, 作用域)。

    谓词字符串自带极性/取值(如 ``"Order.status=PAID"``、
    ``"Session.authenticated=True"``);``value`` 字段表达该谓词的观测
    真值(verified-TRUE 与 verified-FALSE 都是事实,见 StateObservation)。
    """

    predicate: str
    entity: str = "*"
    scope: str = SCOPE_GLOBAL

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, str) or not self.predicate:
            raise ValueError("predicate 必须是非空字符串")
        if not isinstance(self.entity, str) or not self.entity:
            raise ValueError("entity 必须是非空字符串(全局用 '*')")
        if self.scope not in SCOPES:
            raise ValueError(f"scope 必须是 {sorted(SCOPES)} 之一: {self.scope!r}")

    def render(self) -> str:
        """稳定序列化(台账/JSONL 键):``Predicate{Entity@Scope}``。"""
        return f"{self.predicate}{{{self.entity}@{self.scope}}}"


@dataclass(frozen=True)
class StateObservation:
    """一条状态观测:键 + 真值 + 证据等级 + 来源。

    ``(key, value)`` 一起构成事实单元:verified-TRUE 是"确认成立",
    verified-FALSE 是"确认不成立"(DB 直读可给双极);CONTROL_FLOW_PROXY
    只有"走过真分支"的控制流证据。UNKNOWN 观测只允许出现在枚举式
    producer(如阶段 2 调度器对谓词全集打标)里,台账不累积。
    """

    key: StateKey
    value: bool
    evidence: Evidence
    source: str = ""

    def render(self) -> str:
        """``Key=Value``(证据在台账侧按等级分桶,不入渲染键)。"""
        return f"{self.key.render()}={self.value}"

    def to_dict(self) -> dict:
        return {"key": self.key.render(), "value": self.value,
                "evidence": self.evidence.value, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "StateObservation":
        pred, entity, scope = _parse_rendered(d["key"])
        return cls(key=StateKey(pred, entity, scope), value=bool(d["value"]),
                   evidence=Evidence(d["evidence"]), source=d.get("source", ""))


def _parse_rendered(rendered: str) -> tuple[str, str, str]:
    """``Predicate{Entity@Scope}`` → 三元组(与 render 互逆)。

    scope 是封闭词汇(不含 @),entity 开放(可能含 @,如邮箱用户名),
    故从右侧切分;解析失败即抛错——证据解析宁拒勿猜。
    """
    if not (rendered.endswith("}") and "{" in rendered and "@" in rendered):
        raise ValueError(f"非 StateKey.render 形态: {rendered!r}")
    pred, sep, rest = rendered.partition("{")
    entity, at, scope = rest[:-1].rpartition("@")     # rest 去尾 '}'
    if not (sep and at and pred and entity and scope):
        raise ValueError(f"非 StateKey.render 形态: {rendered!r}")
    return pred, entity, scope


# ------------------------------------------------------------- 观测器协议

@runtime_checkable
class RuntimeObserver(Protocol):
    """靶侧观测器:从应用真源(DB/会话/全局变量)读运行时状态。

    约定:在 ExecutionContext.finish_run 内、状态还原**之前**调用,
    故 observe() 读到的是该输入执行结束时的真实终态;观测器自身的
    异常不 poison 执行(证据缺失 ≠ 状态污染),由上下文计数审计。
    """

    name: str

    def observe(self) -> list[StateObservation]: ...


class NullObserver:
    """无观测:显式"零事实"(不是零状态)——防代理推断冒充运行时证据。"""

    name = "null"

    def observe(self) -> list[StateObservation]:
        return []


# ------------------------------------------------------------- 代理适配

def control_flow_proxy_observation(atom: str,
                                   source: str = "reached_state_atoms"
                                   ) -> StateObservation:
    """覆盖代理原子 → CONTROL_FLOW_PROXY 观测(阶段 2 调度器统一消费用)。

    代理口径无法分辨实体绑定与作用域,显式降级 ``entity="*"`` /
    ``scope="unknown"``,不得与 RUNTIME_VERIFIED 混桶。
    """
    return StateObservation(key=StateKey(predicate=atom, entity="*",
                                         scope=SCOPE_UNKNOWN),
                            value=True, evidence=Evidence.CONTROL_FLOW_PROXY,
                            source=source)


# ------------------------------------------------------------- 证据台账

@dataclass
class StateEvidenceLedger:
    """跨输入的 verified 证据累积 + payload 渲染(global_state 消费)。

    新颖度口径:``(key.render(), value)`` 首次出现即新颖(同键值翻转到
    未见过的极性也是新证据;同键同值重复不记)。proxy 原子可经
    ``record_proxy`` 入独立桶,渲染时与 verified 分开落键。
    """

    observer_name: str = ""
    _verified: dict = field(default_factory=dict)   # (render, value) -> obs
    _proxy: dict = field(default_factory=dict)      # render -> obs

    def record(self, observations: Iterable[StateObservation]
               ) -> list[StateObservation]:
        """记入一批观测,返回其中**新出现**的 verified 事实(新颖度)。"""
        novel: list[StateObservation] = []
        for obs in observations or ():
            if obs.evidence is not Evidence.RUNTIME_VERIFIED:
                continue                      # 代理/UNKNOWN 不入事实桶
            dedup = (obs.key.render(), obs.value)
            if dedup not in self._verified:
                self._verified[dedup] = obs
                novel.append(obs)
        return novel

    def record_proxy(self, atoms: Iterable[str],
                     source: str = "reached_state_atoms") -> list[StateObservation]:
        """代理原子入独立桶(与 verified 分键渲染,不冒充)。"""
        novel: list[StateObservation] = []
        for atom in atoms or ():
            obs = control_flow_proxy_observation(atom, source)
            if obs.key.render() not in self._proxy:
                self._proxy[obs.key.render()] = obs
                novel.append(obs)
        return novel

    @property
    def verified_keys(self) -> set[str]:
        return {render for render, _value in self._verified}

    @property
    def verified_observations(self) -> list[StateObservation]:
        return list(self._verified.values())

    @property
    def proxy_atoms(self) -> set[str]:
        return set(self._proxy)

    def to_payload(self) -> dict:
        """global_state.json 的 state_evidence 块(演进级,键集冻结于 v1)。"""
        return {
            "evidence_version": 1,
            "observer": self.observer_name,
            "verified_atoms": sorted(self.verified_keys),
            "verified": [obs.to_dict() for obs in
                         sorted(self.verified_observations,
                                key=lambda o: (o.key.render(), o.value))],
            "proxy_atoms": sorted(self.proxy_atoms),
        }

    def load_payload(self, payload: dict) -> None:
        """跨 epoch 恢复(启动时读 prior global_state;缺失键 = 空,不冒充)。"""
        self.observer_name = payload.get("observer", self.observer_name)
        for d in payload.get("verified", []):
            try:
                self.record([StateObservation.from_dict(d)])
            except (KeyError, ValueError):
                continue                     # 坏行跳过:证据宁缺毋假
