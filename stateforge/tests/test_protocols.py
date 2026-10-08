"""数据协议 schema 稳定性测试 —— PROTOCOLS.md 的机器执行面。

规范文见 stateforge/PROTOCOLS.md(每节"执行面"行指向本文件测试名)。规则:
- 每个**序列化**契约 ≥1 测试,经真实生产者代码路径构造最小样本
  (cli.analyze_cmd / TraceJsonlWriter / TraceSession / ProposalTraceRecorder /
  extract_state_facts / llm cache.put / CorpusManager.save /
  FrontierDirectedFeedback.export_state / ExperimentRecorder.tick /
  map_trace+coverage / StructuralFeedback),禁止手写 dict 冒充 schema;
- 冻结级(ProgramIR、TraceEvent JSONL、LLM cache)额外断言 dump→load→dump
  字节稳定或键集 golden;
- 演进级断言键集 + 消费端缺省兼容;内部级(MappedTrace/StructuralFeedback)
  只断言 dataclass/dict 形状。
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import re
from pathlib import Path

from stateforge.cli import analyze_cmd
from stateforge.core.ir import BasicBlock, FlowEdge
from stateforge.core.serialization import load_program_ir, save_program_ir
from stateforge.fuzzing.atheris_runner import TraceJsonlWriter
from stateforge.fuzzing.corpus_manager import CorpusEntryMeta, CorpusManager
from stateforge.fuzzing.feedback.frontier_feedback import FrontierDirectedFeedback
from stateforge.fuzzing.feedback.frontier_portfolio import ChampionRecord
from stateforge.fuzzing.feedback.structural import Observation, StructuralFeedback
from stateforge.fuzzing.recorder import ExperimentRecorder
from stateforge.analysis.static.icfg import build_icfg
from stateforge.runtime.coverage import coverage
from stateforge.runtime.io import load_events, merge_events
from stateforge.runtime.mapper import MappedTrace, map_trace
from stateforge.runtime.tracer import TraceEvent, TraceSession
from stateforge.solving.llm import cache as llm_cache
from stateforge.solving.llm.lifting import PROMPT_VERSION
from stateforge.solving.proposal import ProposalStatus
from stateforge.solving.proposal_trace import ProposalTraceRecorder
from stateforge.state.extractor import extract_state_facts
from stateforge.state.model import StructuredPredicate

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PROTOCOLS_MD = PACKAGE_DIR / "PROTOCOLS.md"
FIXTURE_S1 = PACKAGE_DIR / "fixtures" / "state_micro" / "S1_session_read"

APP_SRC = """\
def handler(u):
    a = first(u)
    return second(a)

def first(x):
    return x

def second(y):
    return y
"""


def _app(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text(APP_SRC, encoding="utf-8")
    return root


# --------------------------------------------------------------- C0 文档互锁


def test_c0_protocols_md_sections_pinned():
    """PROTOCOLS.md 规范文与执行面同在:C1-C10 小节与三级词汇缺一即漂移。"""
    text = PROTOCOLS_MD.read_text(encoding="utf-8")
    for i in range(1, 11):
        assert re.search(rf"^## C{i}\b", text, flags=re.M), (
            f"PROTOCOLS.md 缺 C{i} 小节")
    for grade in ("冻结", "演进", "内部"):
        assert grade in text


# ------------------------------------------------------- C1 ProgramIR(冻结)


def test_c1_program_ir_analyze_producer_golden_keys(tmp_path):
    """生产者 = cli analyze_cmd 全路径(build_icfg → to_json 落盘)。"""
    out = tmp_path / "out"
    assert analyze_cmd(str(_app(tmp_path)), str(out)) == 0
    ir = json.loads((out / "program_ir.json").read_text(encoding="utf-8"))
    assert list(ir.keys()) == ["blocks", "functions", "edges", "metadata"]
    assert set(ir["functions"]) == {"app.handler", "app.first", "app.second"}
    block = next(iter(ir["blocks"].values()))
    assert list(block.keys()) == ["id", "file", "module", "function",
                                  "start_line", "end_line", "kind",
                                  "statements", "line_numbers"]
    fn = ir["functions"]["app.handler"]
    assert list(fn.keys()) == ["name", "file", "module", "entry", "exit",
                               "blocks"]
    assert ir["edges"], "handler 调 first/second,call 边必须存在"
    edge = ir["edges"][0]
    assert list(edge.keys()) == ["src", "dst", "kind", "condition",
                                 "callsite_line", "callee"]
    assert set(ir["metadata"]) == {"root", "kind", "version",
                                   "callgraph_supplied", "function_count",
                                   "block_count", "edge_count",
                                   "unresolved_calls", "source_hash"}
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert list(summary.keys()) == ["target", "functions", "blocks", "edges",
                                    "unresolved_calls", "edge_kinds"]


def test_c1_program_ir_roundtrip_byte_stable(tmp_path):
    """冻结:dump → from_json → dump 字节稳定;消费端还原为真实 dataclass。"""
    out = tmp_path / "out"
    analyze_cmd(str(_app(tmp_path)), str(out))
    first = out / "program_ir.json"
    ir = load_program_ir(first)
    assert isinstance(ir.blocks["app.py::handler::1-1::entry::1"], BasicBlock)
    assert all(isinstance(e, FlowEdge) for e in ir.edges)
    second = tmp_path / "resaved.json"
    save_program_ir(ir, second)
    assert second.read_bytes() == first.read_bytes()


# ------------------------------------------------- C2 TraceEvent JSONL(冻结)


def test_c2_trace_jsonl_record_schema(tmp_path):
    """生产者 = TraceJsonlWriter(fuzz 子进程唯一的轨迹落盘通道)。"""
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path)
    writer.write_input(0, b"\x01ab", None,
                       [TraceEvent(0, 0, "/tmp/a.py", "handler", 12, "line")])
    writer.close()
    line = path.read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line)
    assert list(rec.keys()) == ["request_id", "data_sha256", "data_length",
                                "exception", "events"]
    assert rec["data_sha256"] == hashlib.sha256(b"\x01ab").hexdigest()
    assert rec["data_length"] == 3 and rec["exception"] is None
    assert list(rec["events"][0].keys()) == ["request_id", "index", "file",
                                             "function", "line", "event_type"]


def test_c2_trace_jsonl_consumer_and_byte_stable(tmp_path):
    """冻结:逐行 dump→parse→dump 字节稳定;消费端 load_events/merge_events。"""
    path = tmp_path / "trace.jsonl"
    writer = TraceJsonlWriter(path)
    writer.write_input(5, b"\x00", None,
                       [TraceEvent(5, 0, "/a.py", "f", 1, "call"),
                        TraceEvent(5, 1, "/a.py", "f", 2, "line")])
    writer.write_input(7, b"\x01", "ValueError",
                       [TraceEvent(7, 0, "/a.py", "g", 9, "line")])
    writer.close()
    for line in path.read_text(encoding="utf-8").splitlines():
        assert json.dumps(json.loads(line), ensure_ascii=False) == line
    events = load_events(path)
    assert [(e.request_id, e.function) for e in events] == \
        [(5, "f"), (5, "f"), (7, "g")]
    merged = merge_events([path])
    assert [e.request_id for e in merged] == [0, 0, 1]   # request_id 重编
    assert [e.index for e in merged] == [0, 1, 0]        # index 输入内重排


def test_c2_trace_session_producer_legacy_json(tmp_path):
    """生产者 = TraceSession(sys.settrace 真采集);to_json 旧裸数组仍可读回。"""
    mod_src = tmp_path / "proto_target.py"
    mod_src.write_text("def target_fn():\n    return 41\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("sf_proto_target", mod_src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with TraceSession(root=str(tmp_path)) as session:
        mod.target_fn()
    assert session.events, "root 内函数执行必须采到事件"
    assert {e.event_type for e in session.events} <= {"call", "line", "return"}
    assert all(e.file.startswith(str(tmp_path)) for e in session.events)
    path = tmp_path / "session_trace.json"
    session.to_json(path)
    loaded = load_events(path)
    assert [e.function for e in loaded] == [e.function for e in session.events]
    assert all(isinstance(e, TraceEvent) for e in loaded)


# ------------------------------------- C3 MappedTrace/Coverage(内部,内存)


def test_c3_mapped_trace_and_coverage_inmemory_shape(tmp_path):
    ir = build_icfg(_app(tmp_path))
    app_file = str(_app(tmp_path) / "app.py")
    events = [TraceEvent(0, 0, app_file, "handler", 2, "line")]
    trace = map_trace(events, ir)
    assert isinstance(trace, MappedTrace)
    assert set(vars(trace)) == {"blocks", "edges", "block_hits", "edge_hits",
                                "edge_kinds", "unresolved", "block_requests",
                                "edge_requests"}
    assert trace.unresolved == []
    assert sum(trace.block_hits.values()) >= 1
    cov = coverage(ir, trace).to_dict()
    assert list(cov.keys()) == ["blocks", "total_blocks", "block_ratio",
                                "edges", "total_edges", "edge_ratio",
                                "calls", "total_calls", "call_ratio"]


# ------------------------------------ C4 StructuralFeedback(内部,内存)


def test_c4_structural_feedback_observation_shape(tmp_path):
    ir = build_icfg(_app(tmp_path))
    trace = map_trace([TraceEvent(0, 0, str(_app(tmp_path) / "app.py"),
                                  "handler", 2, "line")], ir)
    sf = StructuralFeedback()
    obs = sf.observe(trace)
    assert isinstance(obs, Observation)
    assert set(vars(obs)) == {"blocks", "edges", "new_blocks", "new_edges"}
    assert obs.score == len(obs.new_blocks) + len(obs.new_edges)
    assert obs.interesting == (obs.score > 0)
    sf.update_global_coverage(obs.blocks, obs.edges)   # 先算分后并入
    assert sf.observe(trace).score == 0                # 二次无新颖


# ------------------------------------- C5 MutationProposal JSONL(演进)


def test_c5_proposal_trace_jsonl_schema(tmp_path):
    """生产者 = ProposalTraceRecorder.record;演进:缺省字段显式 null。"""
    path = tmp_path / "pt" / "proposal_trace.jsonl"
    rec = ProposalTraceRecorder(path)
    rec.record(trigger_reason="stall", frontier_id="u->v", slice_hash="abc",
               llm_status="PROPOSED", validation_status="valid",
               distance_before=3, distance_after=0, frontier_crossed=True,
               latency_seconds=0.5)
    rec.close()
    line = path.read_text(encoding="utf-8").splitlines()[0]
    row = json.loads(line)
    assert set(row) == set(ProposalTraceRecorder.FIELDS) | {"ts"}
    assert row["frontier_id"] == "u->v"
    assert row["distance_before"] == 3 and row["distance_after"] == 0
    assert row["deterministic_failure_reason"] is None    # 未提供 → 显式 null
    assert row["proposal"] is None
    assert json.dumps(row, ensure_ascii=False) == line    # 键序稳定可复算
    assert ProposalStatus("PROPOSED").value == "PROPOSED"  # 五状态枚举冻结值
    assert {s.value for s in ProposalStatus} == {          # 五态闭集
        "PROPOSED", "INSUFFICIENT_EVIDENCE", "STATE_REQUIRED",
        "UNSUPPORTED", "INVALID_OUTPUT"}


# ------------------------------------ C6 StateFact/StructuredPredicate(演进)


def test_c6_state_fact_to_dict_schema():
    """生产者 = extract_state_facts(真实 AST 提取,S1 夹具)。"""
    facts = extract_state_facts(FIXTURE_S1)
    reads = [f for f in facts
             if f.object_ref == "session.user_id"
             and f.operation.value == "READ"]
    assert reads, "S1 夹具应产出 session.user_id READ"
    d = reads[0].to_dict()
    assert list(d.keys()) == ["candidate_id", "domain", "object_ref",
                              "operation", "source_file", "function_name",
                              "lineno", "block_id", "predicate",
                              "structured_predicate", "written_value",
                              "relevance", "evidence"]
    assert d["candidate_id"].startswith("SF-")
    assert d["domain"] in {"SESSION", "DATABASE", "FILE"}
    assert d["operation"] in {"READ", "WRITE", "DELETE"}
    assert d["relevance"] in {"CONTROL", "NON_CONTROL", "UNKNOWN"}
    assert d["predicate"] == "session.get('user_id')"
    assert d["structured_predicate"] == {"operator": "EXISTS",
                                         "subject": "session.user_id",
                                         "value": None}
    assert isinstance(d["evidence"], list) and d["evidence"]


def test_c6_structured_predicate_roundtrip():
    facts = extract_state_facts(FIXTURE_S1)
    sp = next(f.structured_predicate for f in facts
              if f.structured_predicate is not None)
    assert StructuredPredicate.from_dict(sp.to_dict()) == sp


# ----------------------------------------------- C7 LLM cache JSON(冻结)


def test_c7_llm_cache_schema_and_key_derivation(tmp_path, monkeypatch):
    """生产者 = cache.put;文件名 = sha256({"v","facts"} sort_keys)。"""
    monkeypatch.setattr(llm_cache, "CACHE_DIR", tmp_path)
    facts = {"kind": "label",
             "facts": [{"var": "db", "ops": ["read"], "source": "flask",
                        "domain": ["open", "closed"],
                        "evidence": "app.py:3 cursor.execute"}]}
    llm_cache.put(facts, PROMPT_VERSION, '{"var": "db", "label": "irrelevant"}')
    expected = hashlib.sha256(json.dumps(
        {"v": PROMPT_VERSION, "facts": facts}, sort_keys=True,
        ensure_ascii=False).encode()).hexdigest()
    f = tmp_path / f"{expected}.json"
    assert f.exists()
    raw = f.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert list(payload.keys()) == ["text", "prompt_version"]
    assert payload["prompt_version"] == PROMPT_VERSION
    assert llm_cache.get(facts, PROMPT_VERSION) == payload["text"]  # 消费端命中
    assert llm_cache.get({"kind": "label", "facts": "other"},
                         PROMPT_VERSION) is None                    # 键变即失效
    assert json.dumps(payload, ensure_ascii=False) == raw           # 冻结:可复算


def test_c7_llm_cache_shipped_files_obligation():
    """README 已知义务:随仓缓存必须提交且全部符合冻结 schema。"""
    files = sorted(llm_cache.CACHE_DIR.glob("*.json"))
    assert files, "随仓缓存缺失:fresh clone 语义标注会回归全 irrelevant"
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        assert list(payload.keys()) == ["text", "prompt_version"]
        assert payload["prompt_version"] == PROMPT_VERSION
        assert isinstance(payload["text"], str) and payload["text"]


# ------------------------------- C8 corpus(meta.jsonl)+ global_state(演进)


def test_c8_corpus_meta_jsonl_schema_and_roundtrip(tmp_path):
    """生产者 = CorpusManager.save:原始字节落 seeds/<cid>,meta 一行一 asdict。"""
    data = b"POST /login HTTP/1.1\r\n\r\nuser=admin"
    mgr = CorpusManager(tmp_path / "spool")
    cid = mgr.save(data, epoch=1, new_blocks=2, new_edges=1,
                   discovered_blocks=5, discovered_edges=4,
                   origin="LLM", parent_seed_id="abc")
    assert cid == hashlib.sha256(data).hexdigest()[:16]
    assert (tmp_path / "spool" / "seeds" / cid).read_bytes() == data
    line = (tmp_path / "spool" / "meta.jsonl").read_text(
        encoding="utf-8").splitlines()[0]
    meta = json.loads(line)
    assert list(meta.keys()) == [f.name for f in dataclasses.fields(
        CorpusEntryMeta)]
    assert meta["input_id"] == cid and meta["origin"] == "LLM"
    assert mgr.save(data, 1, 0, 0, 0, 0) is None        # 内容哈希去重
    entries = CorpusManager(tmp_path / "spool").entries()  # 消费端读回
    assert entries[0].input_id == cid and entries[0].parent_seed_id == "abc"
    assert json.dumps(dataclasses.asdict(entries[0]),
                      ensure_ascii=False) == line        # 键序稳定可复算


def test_c8_global_state_frontier_mode_roundtrip(tmp_path):
    """frontier 模式生产者 = FrontierDirectedFeedback:commit 真流程 → export_state。"""
    app = _app(tmp_path)
    ir = build_icfg(app)
    fb = FrontierDirectedFeedback(ir)
    trace = map_trace([TraceEvent(0, 0, str(app / "app.py"),
                                  "handler", 1, "line"),
                       TraceEvent(0, 1, str(app / "app.py"),
                                  "handler", 2, "line")], ir)
    fb.commit(trace, kept=True)          # 真实评分后并入路径(novelty+tracker)
    assert fb.novelty.global_edges, "commit 后须有边证据入库"
    fb.portfolio.champions["u->v"] = ChampionRecord(
        input_id="9f86d081884c7d65", distance=2, epoch=1, frontier_version=2)
    state = fb.export_state()
    assert list(state.keys()) == ["covered_blocks", "covered_edges", "version",
                                  "edge_frontiers", "portfolio"]
    assert state["covered_blocks"] and isinstance(
        state["covered_edges"][0], list)          # pair 落盘为 list
    assert list(state["portfolio"].keys()) == ["champions",
                                               "replacement_count"]
    champ = state["portfolio"]["champions"]["u->v"]
    assert list(champ.keys()) == ["input_id", "distance", "epoch",
                                  "frontier_version"]
    # 消费端 = load_state(entry_driver 启动恢复同路径)
    fb2 = FrontierDirectedFeedback(ir)
    fb2.load_state(state)
    assert fb2.novelty.global_blocks == set(state["covered_blocks"])
    assert fb2.novelty.global_edges == {tuple(e) for e in state["covered_edges"]}
    assert fb2.tracker.version == state["version"] + 1   # :57 读入后 +1


def test_c8_global_state_feedback_mode_shape():
    """feedback 模式 payload:生产/消费表达式逐字取自 entry_driver.py:86-89/99-101
    (_dump_state 为 main 内闭包,无法整体调用——此处以真实 StructuralFeedback
    为状态源,复算两端表达式)。"""
    fb = StructuralFeedback()
    fb.update_global_coverage({"b1", "b2"}, {("b1", "b2")})
    payload = {"blocks": sorted(fb.global_blocks),                # :87
               "edges": [list(e) for e in sorted(fb.global_edges)]}  # :88
    raw = json.dumps(payload)
    fb2 = StructuralFeedback()
    prior = json.loads(raw)
    fb2.update_global_coverage(set(prior.get("blocks", [])),       # :100
                               {tuple(e) for e in prior.get("edges", [])})
    assert fb2.global_blocks == {"b1", "b2"}
    assert fb2.global_edges == {("b1", "b2")}


# --------------------------------------------- C9 实验记录 JSONL(演进)


def test_c9_recorder_jsonl_schema(tmp_path):
    """生产者 = ExperimentRecorder.tick(sample_interval=0 强制首 tick 采样)。"""
    rec = ExperimentRecorder(tmp_path, group="x", epoch=0, sample_interval=0.0)
    rec.tick(0.01, 0.02, blocks=3, edges=4, saved=True, cid="9f86d081884c7d65",
             score=2)
    rec.close()
    cand_line = (tmp_path / "x-e0-candidates.jsonl").read_text(
        encoding="utf-8").splitlines()[0]
    cand = json.loads(cand_line)
    assert list(cand.keys()) == ["input_id", "epoch", "score"]
    assert cand["input_id"] == "9f86d081884c7d65"
    snap_line = (tmp_path / "x-e0-snapshots.jsonl").read_text(
        encoding="utf-8").splitlines()[0]
    snap = json.loads(snap_line)
    assert list(snap.keys()) == ["group", "epoch", "t", "executions", "blocks",
                                 "edges", "exec_seconds", "feedback_seconds"]
    assert json.dumps(snap, ensure_ascii=False) == snap_line
    summary = rec.summary()      # epoch 汇总(EpochController 落盘前形态)
    assert set(summary.keys()) == {"group", "epoch", "wall_seconds",
                                   "executions", "exec_seconds",
                                   "feedback_seconds", "feedback_overhead",
                                   "throughput_exec_s", "blocks", "edges",
                                   "candidates_saved"}


# ------------------------------------------------- C10 序列种子 JSON(演进)


def test_c10_sequence_seed_consumer_pin():
    """C10 消费端钉扎(生产者=T3 state 注入器,待接):经驱动同款真实
    encode/decode/dispatch 路径 round-trip,钉 steps 形状与分派语义。

    诚实标注:当前仓内尚无 C10 生产者,本测试用驱动自带的
    encode_sequence_seed(T3 届时必须复用的同一 builder)构造样本,
    属消费侧 pin;生产者接入后应改走 T3 真实产出路径。
    """
    from stateforge.fuzzing.entry_driver import (decode_sequence_seed,
                                                 dispatch_seed,
                                                 encode_sequence_seed)

    steps = [{"json": {"op": "login", "user": "alice"}},
             {"json": {"op": "profile"}}]
    raw = encode_sequence_seed(steps)                  # 生产侧 builder(共享)
    payload = json.loads(raw)
    assert list(payload.keys()) == ["steps"]           # 顶层只含 steps
    assert isinstance(payload["steps"], list)
    assert all(isinstance(s, dict) for s in payload["steps"])
    assert all(set(s) <= {"query", "form", "json", "header", "cookie"}
               for s in payload["steps"])              # 步=单请求 dict(冻结词汇)

    parsed = decode_sequence_seed(raw)                 # 消费端解码(驱动同函数)
    assert parsed == steps
    assert decode_sequence_seed(b'{"json":{}}') is None    # 非序列→legacy 透传
    assert decode_sequence_seed(b'{"steps":5}') is None
    assert decode_sequence_seed(b"\x00\xff") is None

    seen = []                                          # 分派语义:按序逐步执行
    dispatch_seed(lambda d: seen.append(json.loads(d)), raw)
    assert seen == steps
