"""状态面图(State Plane Graph):由转移点 + 读位点构造的静态骨架。

节点(两类,与 SSTG 的 action/state 二分同形,但数据源是 sites.py
的实测事实而非几乎为空的 AppModel.preconditions):

- **slot 节点**:``链[key]``(不透明原子)。``链[*]`` 表示整库
  (clear/flush)。只读 slot(有读无写)也建节点,标
  ``written=False``——读得到写不到,说明生产者在扫描范围外
  (典型:框架写入、别处模块)。
- **action 节点**:函数(含类视图方法 ``Class.method``)。

边(两类,**概念不混**——见 sites.py 三分法):

- **produces**(实线):转移点 → slot。op ∈ write/pop/delete/
  setdefault/update/clear/flush;clear/flush 指 ``链[*]``。
- **reads**(虚线):读位点 → slot。**读 ≠ 门卫**:读可能发生在
  守卫已过之后。语义上的 requires(门卫)由守卫形状 + 动态
  确认派生,本图只给证据。

动态转移记录(转移点前后快照差)将来在此图上叠加为
slot-state → slot-state 边,即成真正的状态机;本图是它的骨架。

分层(L1.5 state):仅 stdlib + dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stateforge.state.sites import SiteScan, StoreSite


@dataclass(frozen=True)
class Slot:
    """slot 节点:一个不透明状态原子。"""

    id: str                    # "session[id]" / "request.session[*]"
    chain: str
    key: str | None
    written: bool = True       # False = 只读(生产者在范围外)


@dataclass(frozen=True)
class Produce:
    action: str                # 函数全名
    slot: str
    op: str
    file: str
    line: int


@dataclass(frozen=True)
class Read:
    action: str
    slot: str
    file: str
    line: int
    guard: bool = False        # 同函数存在提前退出守卫(读可能与门相关)


@dataclass
class StatePlaneGraph:
    slots: dict[str, Slot] = field(default_factory=dict)
    produces: list[Produce] = field(default_factory=list)
    reads: list[Read] = field(default_factory=list)

    @property
    def actions(self) -> set[str]:
        return {p.action for p in self.produces} | {r.action for r in self.reads}

    def producers_of(self, slot_id: str) -> list[Produce]:
        return [p for p in self.produces if p.slot == slot_id]

    def readers_of(self, slot_id: str) -> list[Read]:
        return [r for r in self.reads if r.slot == slot_id]

    def to_dict(self) -> dict:
        return {
            "slots": [s.id for s in self.slots.values()],
            "actions": sorted(self.actions),
            "produces": [p.__dict__ for p in self.produces],
            "reads": [r.__dict__ for r in self.reads],
        }

    def to_dot(self, title: str = "state plane") -> str:
        """渲染为 Graphviz DOT:action=box(实线写/虚线读),slot=ellipse。"""
        lines = [f'digraph "{title}" {{',
                 "  rankdir=LR;",
                 '  node [fontname="Helvetica"];']
        for a in sorted(self.actions):
            lines.append(f'  "{a}" [shape=box];')
        for s in self.slots.values():
            style = "" if s.written else ', style=dotted, color=gray'
            lines.append(f'  "{s.id}" [shape=ellipse{style}];')
        for p in self.produces:
            color = ', color=red' if p.op in ("clear", "flush") else ""
            lines.append(f'  "{p.action}" -> "{p.slot}" '
                         f'[label="{p.op}"{color}];')
        for r in self.reads:
            style = ', color=blue, fontcolor=blue' if r.guard else ""
            lines.append(f'  "{r.action}" -> "{r.slot}" '
                         f'[style=dashed, label="read"{style}];')
        lines.append("}")
        return "\n".join(lines)


def build_state_plane_graph(scan: SiteScan) -> StatePlaneGraph:
    """转移点(stores)+ 读位点(reads)→ 状态面图。

    join 规则:``链[key]`` 精确相等(key 已由常量折叠归一)。
    只读 slot 仅当**同链**已有写/清证据时才建(否则 ``row["x"]``
    这类普通下标读会灌入噪声)。
    """
    g = StatePlaneGraph()
    guarded_fns = {gd.function for gd in scan.guards}

    for s in scan.stores:                      # 转移点 → produces
        sid = s.slot
        if sid not in g.slots:
            g.slots[sid] = Slot(id=sid, chain=s.chain, key=s.key)
        g.produces.append(Produce(
            action=s.function, slot=sid, op=s.op,
            file=s.file, line=s.line))

    written_chains = {s.chain for s in g.slots.values()}
    for r in scan.reads:                       # 读位点 → reads
        if r.key is None:
            continue
        sid = f"{r.chain}[{r.key}]"
        if sid not in g.slots:
            if r.chain not in written_chains:
                continue                       # 无同链证据,不造 slot
            g.slots[sid] = Slot(id=sid, chain=r.chain, key=r.key,
                                written=False)
        g.reads.append(Read(
            action=r.function, slot=sid,
            file=r.file, line=r.line,
            guard=r.function in guarded_fns))
    return g
