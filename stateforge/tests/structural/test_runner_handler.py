"""Input handler: monotonic request ids, exception capture, no cross-seed edges."""
import json
from pathlib import Path

from stateforge.core.ir import ProgramIR
from stateforge.dynamic.mapper import map_trace
from stateforge.dynamic.tracer import TraceSession
from stateforge.fuzz.atheris_runner import TraceJsonlWriter, build_input_handler
from stateforge.static.icfg import build_icfg


class FakeAdapter:
    """First input runs outer_a, second runs outer_b — would fabricate an
    a->b edge under the old per-input request-id-reset behaviour."""

    def __init__(self, ns):
        self.ns = ns
        self.n = 0

    def prepare(self, atheris):
        pass

    def execute(self, data: bytes):
        self.n += 1
        (self.ns["outer_a"] if self.n == 1 else self.ns["outer_b"])(data)


SOURCE = (
    "def outer_a(d):\n"
    "    return leaf_a(d)\n"
    "def leaf_a(d):\n"
    "    return len(d)\n"
    "def outer_b(d):\n"
    "    return leaf_b(d)\n"
    "def leaf_b(d):\n"
    "    return len(d)\n"
)


def _per_request_edge_union(ir, session):
    union = set()
    for rid in sorted({e.request_id for e in session.events}):
        union |= {tuple(p) for p in map_trace(
            [e for e in session.events if e.request_id == rid], ir).edges}
    return union


def test_request_ids_monotonic_and_no_cross_seed_edges(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text(SOURCE, encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)
    ir: ProgramIR = build_icfg(tmp_path)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(FakeAdapter(ns), session, writer)
    with session:                       # run_atheris 中由 with session: 安装 hook
        handler(b"\x00")
        handler(b"\x01")

    rids = sorted({e.request_id for e in session.events})
    assert rids == [1, 2]        # 单调不重置;0 被 session.__enter__ 占用(run_one 语义)
    whole = {tuple(p) for p in map_trace(session.events, ir).edges}
    assert whole == _per_request_edge_union(ir, session)   # 无跨种子伪造边


def test_handler_captures_exception_and_writes_line(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f(d):\n    raise ValueError('x')\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)

    class BoomAdapter:
        def prepare(self, atheris):
            pass

        def execute(self, data: bytes):
            ns["f"](data)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(BoomAdapter(), session, writer)
    handler(b"\x05")                                   # 不得抛出
    rec = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert rec["exception"] == "ValueError: x"


def test_sampling_every_n_skips_lines(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def f(d):\n    return len(d)\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(source.read_text(), str(source), "exec"), ns)

    class SimpleAdapter:
        def prepare(self, atheris):
            pass

        def execute(self, data: bytes):
            ns["f"](data)

    session = TraceSession(tmp_path)
    writer = TraceJsonlWriter(tmp_path / "t.jsonl")
    handler = build_input_handler(SimpleAdapter(), session, writer, every_n=2)
    with session:               # rid 1 被 __enter__ 占用;输入 rid=2、rid=3
        handler(b"\x00")        # rid 2 → 2%2==0 → 写
        handler(b"\x01")        # rid 3 → 跳过
    lines = (tmp_path / "t.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["request_id"] == 2
