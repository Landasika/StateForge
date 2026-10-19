# StateForge 数据协议

**域间数据契约一览:生产者 → 消费者 | 格式 | 稳定性等级。** 本文与 `stateforge/ARCHITECTURE.md`(控制耦合)互补:分层管"谁能依赖谁",本文管"依赖时交换什么"。机器执行面在 `stateforge/tests/test_protocols.py`(pytest 即测):每个**序列化**契约由真实生产者代码路径构造最小样本,断言键集/类型;冻结级契约额外断言 round-trip 字节稳定。字段**禁止发明**——每节示例均摘自实际序列化代码;改动任一节引用的序列化代码,必须同步本节与对应测试(每节"执行面"行给出测试名,测试不存在即规范漂移)。

---

## 稳定性等级

| 等级 | 语义 | 变更纪律 |
|------|------|---------|
| **冻结** | 键集/类型/字节布局变化即破坏兼容:旧产物读不出、随仓缓存失效、跨进程产物报废 | 只加不改;确需破坏须版本化迁移并更新全部消费者 |
| **演进** | 只许加字段,不许改/删既有键;消费端对缺键有缺省兼容 | 加字段须给缺省值,旧消费者按 `.get(键, 缺省)` 读 |
| **内部** | 内存结构,不落盘,无序列化兼容承诺 | 重构自由,只需同步本文与消费者代码 |

---

## 契约索引

| # | 契约 | 生产者 → 消费者 | 格式 | 等级 | 落盘位置 |
|---|------|----------------|------|------|---------|
| C1 | ProgramIR JSON | cli analyze ← analysis.static.icfg → cli feedback / fuzzing.entry_driver / evaluation.four_arm / runtime.mapper | JSON | 冻结 | `<out>/program_ir.json`(+`summary.json`) |
| C2 | TraceEvent JSONL | fuzzing.atheris_runner.TraceJsonlWriter → runtime.io.load_events → merge_events → runtime.mapper | JSONL(一行一输入) | 冻结 | `trace.jsonl` |
| C3 | MappedTrace / Coverage | runtime.mapper.map_trace / runtime.coverage → fuzzing.feedback.*、cli feedback payload | 内存 | 内部 | —(仅 feedback.json 中投影) |
| C4 | StructuralFeedback / Observation | fuzzing.feedback.structural → entry_driver 采集器 | 内存 | 内部 | —(全局覆盖落盘归 C8) |
| C5 | MutationProposal JSONL | solving.proposal_trace.ProposalTraceRecorder → 离线审计聚合(评测域) | JSONL | 演进 | `proposal_trace.jsonl` |
| C6 | StateFact / StructuredPredicate JSON | state.extractor → state.index → state.annotation(5B LLM 输入);P3-M2 起 run_state_analysis(`transitions`)→ cli state | JSON-ready dict | 演进 | 由调用方决定(现状内存传递;cli state 落盘 `<out>/state.json`) |
| C7 | LLM cache JSON | solving.llm.cache.put(lifting label/relate)→ cache.get(缓存优先通道) | JSON 单行 | 冻结 | `stateforge/solving/llm/cache/<sha256>.json`(随仓) |
| C8 | corpus + global_state.json | fuzzing.corpus_manager / fuzzing.entry_driver → export_seed_dir(libFuzzer)、evaluation.experiment.epoch | bytes + JSONL + JSON | 演进 | `spool/seeds/<cid>`、`spool/meta.jsonl`、`global_state.json` |
| C9 | 实验记录 JSONL | fuzzing.recorder.ExperimentRecorder → evaluation.experiment.epoch | JSONL | 演进 | `<group>-e<N>-snapshots.jsonl`、`<group>-e<N>-candidates.jsonl` |
| C10 | 序列种子 JSON(steps) | `solving/state_seed_injector.inject_state_seeds`(P3-T3,经 `epoch.py::_inject_state_seeds_between_epochs`,policy="state")→ fuzzing.entry_driver 解码分派、targets.stateful.* 靶 | JSON | 演进 | corpus 种子文件(`lf_corpus/<cid>`、`state_spool/seeds/<cid>`) |

---

## C1 ProgramIR JSON(program_ir.json)—— 冻结

| | |
|---|---|
| 生产者 | `cli.analyze_cmd`(`cli.py:19-41`):`analysis.static.icfg.build_icfg`(`analysis/static/icfg.py:53`)构建 → `ProgramIR.to_json`(`core/ir.py:89-91`,`json.dumps(indent=2, ensure_ascii=False)`)经 `save_program_ir`(`core/serialization.py:12`)落盘;`summary.json` 由 `save_json`(`core/serialization.py:20`)落盘 |
| 消费者 | `cli.feedback_cmd`(`cli.py:45` `from_json`)、`fuzzing/entry_driver.py:69`、`evaluation/experiment/four_arm.py:96`、`runtime/mapper.map_trace`(块/边映射全以它为底图) |
| 格式 | JSON;顶层键序固定 `blocks / functions / edges / metadata` |
| 执行面 | `test_protocols.py::test_c1_program_ir_analyze_producer_golden_keys`、`test_c1_program_ir_roundtrip_byte_stable` |

键集(键序即 dataclass 字段序,`asdict` 保序):

- 顶层:`blocks` `functions` `edges` `metadata`(`core/ir.py:72-78`)
- `blocks[v]`(BasicBlock,`core/ir.py:17-26`):`id` `file` `module` `function` `start_line` `end_line` `kind` `statements` `line_numbers`
- `functions[k]`(FunctionIR,`core/ir.py:40-46`):`name` `file` `module` `entry` `exit` `blocks`
- `edges[i]`(FlowEdge,`core/ir.py:30-36`):`src` `dst` `kind` `condition` `callsite_line` `callee`
- `metadata` 由 `build_icfg` 写入(`analysis/static/icfg.py:56` + `:151-156`):`root` `kind`(="icfg") `version`(=1) `callgraph_supplied` `function_count` `block_count` `edge_count` `unresolved_calls` `source_hash`

最小示例(`feedback_cmd` 陈旧检测与 `entry_driver` 的 `metadata.root` 均读此文件;块 id 格式为 `文件::函数::行域::类型::序`):

```json
{
  "blocks": {
    "app.py::handler::1-1::entry::1": {"id": "app.py::handler::1-1::entry::1",
                       "file": "/…/app/app.py",
                       "module": "app", "function": "handler",
                       "start_line": 1, "end_line": 1, "kind": "entry",
                       "statements": [], "line_numbers": [1]}
  },
  "functions": {
    "app.handler": {"name": "app.handler", "file": "/…/app/app.py",
                     "module": "app",
                     "entry": "app.py::handler::1-1::entry::1",
                     "exit": "app.py::handler::1-1::exit::1",
                     "blocks": ["app.py::handler::1-1::entry::1"]}
  },
  "edges": [
    {"src": "app.py::handler::2-2::callsite::1", "dst": "app.py::first::5-5::entry::1",
     "kind": "call", "condition": null, "callsite_line": 2, "callee": "app.first"}
  ],
  "metadata": {"root": "/…/app", "kind": "icfg", "version": 1,
                "callgraph_supplied": false, "function_count": 3,
                "block_count": 9, "edge_count": 12, "unresolved_calls": [],
                "source_hash": "…"}
}
```

**冻结语义:** Phase-1 冻结口径——mapper/评测/子进程驱动三域共读同一底图;`edge_kinds` 集合(`flow/true/false/call/return`)被 `coverage`(`runtime/coverage.py:47`)硬编码。加键安全(消费端按键取),改/删即破坏。

`summary.json`(同级伴生产物,`save_json` indent=2)键序:`target` `functions` `blocks` `edges` `unresolved_calls` `edge_kinds`。

---

## C2 TraceEvent JSONL(trace.jsonl,一行一输入)—— 冻结

| | |
|---|---|
| 生产者 | `TraceJsonlWriter.write_input`(`fuzzing/atheris_runner.py:34-43`,`json.dumps(record, ensure_ascii=False)` 逐行 flush,抗 libFuzzer `_Exit`);同形产物:`evaluation/benchmark_baseline.py:86-88`(每行多 `route`/`method` 扩展键);旧格式:`TraceSession.to_json`(`runtime/tracer.py:97-98`,裸数组) |
| 消费者 | `runtime/io.load_events`(`runtime/io.py:11-31`;JSONL 分支 `:19-25` 一行一输入、`events` 展平)→ `merge_events`(`:34-55`,重编 request_id/index)→ `runtime/mapper.map_trace` |
| 格式 | JSONL;每行一个输入记录,记录内 `events` 为该输入的有序 TraceEvent 数组 |
| 执行面 | `test_protocols.py::test_c2_trace_jsonl_record_schema`、`test_c2_trace_jsonl_consumer_and_byte_stable`、`test_c2_trace_session_producer_legacy_json` |

键集:

- 输入记录(`fuzzing/atheris_runner.py:36-42`):`request_id` `data_sha256`(hex64) `data_length` `exception`(str|null) `events`
- `events[i]` = TraceEvent(`runtime/tracer.py:15-21`)键序:`request_id` `index` `file`(已解析绝对路径) `function` `line` `event_type`(`call/line/return/exception`)

最小示例:

```jsonl
{"request_id": 0, "data_sha256": "03d3…", "data_length": 3, "exception": null, "events": [{"request_id": 0, "index": 0, "file": "/a.py", "function": "handler", "line": 12, "event_type": "line"}]}
```

**冻结语义:** 这是 Atheris 进程被 `_Exit` 硬终止后唯一存活的动态证据;评测(`benchmark_baseline`、`epoch` 重建)与复现全靠它。`load_events` 对裸数组/单 dict 旧格式保留兼容读(`runtime/io.py:15-31`),但**写**只认本格式。

---

## C3 MappedTrace / Coverage —— 内部(内存)

| | |
|---|---|
| 生产者 | `runtime/mapper.map_trace`(`runtime/mapper.py:28`)、`runtime/coverage.coverage`(`runtime/coverage.py:46`) |
| 消费者 | `fuzzing/feedback/structural.StructuralFeedback.evidence`(`:60-64`)、`fuzzing/feedback/engine.compute_feedback`、cli `feedback_cmd` payload(`coverage.to_dict` 投影进 `feedback.json`) |
| 格式 | 内存 dataclass,无序列化 |
| 执行面 | `test_protocols.py::test_c3_mapped_trace_and_coverage_inmemory_shape` |

形状:

- `MappedTrace`(`runtime/mapper.py:13-25`)字段:`blocks: list[str]`(块 id 序) `edges: list[tuple[str,str]]` `block_hits: Counter` `edge_hits: Counter` `edge_kinds: dict[pair, set[str]]`(只含有静态边支撑的对) `unresolved: list[TraceEvent]` `block_requests: dict[str, set[int]]` `edge_requests: dict[pair, set[int]]`
- `Coverage.to_dict()`(`runtime/coverage.py:32-43`)键序:`blocks` `total_blocks` `block_ratio` `edges` `total_edges` `edge_ratio` `calls` `total_calls` `call_ratio`

**内部语义:** 不落盘;唯一对外投影是 C1 消费方 `feedback.json` 里的 `coverage` 子对象(上面九键)。重构字段只需同步 mapper 与 feedback 域。

---

## C4 StructuralFeedback / Observation —— 内部(内存)

| | |
|---|---|
| 生产者 | `StructuralFeedback.observe`(`fuzzing/feedback/structural.py:66-73`);前沿变体 `FrontierDirectedFeedback.observe`(`fuzzing/feedback/frontier_feedback.py:62-84`) |
| 消费者 | `fuzzing/entry_driver.py`(CandidateCollector/FrontierCollector 保留裁决) |
| 格式 | 内存 |
| 执行面 | `test_protocols.py::test_c4_structural_feedback_observation_shape` |

形状:

- `Observation`(`structural.py:19-25`)字段:`blocks: set[str]` `edges: set[pair]` `new_blocks: set[str]` `new_edges: set[pair]`;派生 `score = |N_B|+|N_E|`、`interesting = score > 0`
- 评分纪律:先对**执行前**全局快照算分,后 `update_global_coverage` 并入(`structural.py:52-56`)
- `FrontierObservation`(`frontier_feedback.py:23-33`):`new_blocks/new_edges/distance/nearest_frontier/proximity/score/interesting/reason/frontier_version/edge_frontier_count`

**内部语义:** 其全局覆盖的落盘形态是 C8 的 `global_state.json`;本节结构本身不落盘。

---

## C5 MutationProposal JSONL(proposal_trace)—— 演进

| | |
|---|---|
| 生产者 | `ProposalTraceRecorder.record`(`solving/proposal_trace.py:24-28`):行 = `FIELDS` 全键(缺省显式 `null`)+ `ts`;`json.dumps(row, ensure_ascii=False)` 逐条 flush |
| 消费者 | 离线审计/聚合(评测域;现状唯一消费者为回归测试 `tests/feasibility/test_llm_solve.py:207`) |
| 格式 | JSONL,一行一次 LLM 调用 |
| 执行面 | `test_protocols.py::test_c5_proposal_trace_jsonl_schema` |

键集(`solving/proposal_trace.py:13-17`,16 键含 `ts`):`trigger_reason` `frontier_id` `slice_hash` `deterministic_failure_reason` `llm_status` `proposal` `validation_status` `candidate_generated` `candidate_executed` `distance_before` `distance_after` `branch_satisfied` `frontier_crossed` `latency_seconds` `token_usage` `ts`

最小示例:

```jsonl
{"trigger_reason": "stall", "frontier_id": "u->v", "slice_hash": "abc", "deterministic_failure_reason": null, "llm_status": "PROPOSED", "proposal": null, "validation_status": "valid", "candidate_generated": null, "candidate_executed": null, "distance_before": 3, "distance_after": 0, "branch_satisfied": null, "frontier_crossed": true, "latency_seconds": 0.5, "token_usage": null, "ts": 1728380000.0}
```

**演进语义:** `record(**kw)` 走 `kw.get(k)`,加 `FIELDS` 字段即加键、旧行不破坏;`proposal` 列内嵌的是 `MutationProposal`(`solving/proposal.py:17-27`,五状态 `ProposalStatus`)的内存对象,其序列化形态由写入方决定。

---

## C6 StateFact / StructuredPredicate JSON(5A 输出)—— 演进

| | |
|---|---|
| 生产者 | `state.extractor.extract_state_facts`(`state/extractor.py:406`)→ `StateFactCandidate.to_dict`(`state/model.py:75-92`)、`StructuredPredicate.to_dict`(`state/model.py:49-52`) |
| 消费者 | `state/index.build_index` → `ObjectUsage.to_dict`(`state/index.py:23-34`)→ `state/annotation/annotator._build_prompt`(`state/annotation/annotator.py:18-58`,reads/writes/guards 字典族直接进 LLM 提示词) |
| 格式 | JSON-ready dict(现状内存传递;落盘形态由调用方决定) |
| 执行面 | `test_protocols.py::test_c6_state_fact_to_dict_schema`、`test_c6_structured_predicate_roundtrip` |

键集:

- StateFactCandidate(`state/model.py:76-92`,13 键):`candidate_id` `domain` `object_ref` `operation` `source_file` `function_name` `lineno` `block_id` `predicate` `structured_predicate` `written_value` `relevance` `evidence`
- 枚举取值:domain ∈ `SESSION/DATABASE/FILE`;operation ∈ `READ/WRITE/DELETE`;relevance ∈ `CONTROL/NON_CONTROL/UNKNOWN`(`state/model.py:14-29`)
- StructuredPredicate:`operator` `subject` `value`;operator ∈ `EQ/NE/EXISTS/NOT_EXISTS/IN/NOT_IN/TRUTHY/FALSY`(`state/model.py:32-40`)
- `candidate_id` 确定性:`SF-` + sha256(`domain:object_ref:op:file:line`)前 8 位(`state/extractor.py:29-33`)

最小示例(S1 夹具实测,`stateforge/fixtures/state_micro/S1_session_read`):

```json
{"candidate_id": "SF-849ff04c", "domain": "SESSION", "object_ref": "session.user_id",
 "operation": "READ", "source_file": "…/S1_session_read/app.py",
 "function_name": "handler", "lineno": 2, "block_id": null,
 "predicate": "session.get('user_id')",
 "structured_predicate": {"operator": "EXISTS", "subject": "session.user_id", "value": null},
 "written_value": null, "relevance": "CONTROL",
 "evidence": ["…/S1_session_read/app.py:2 session.get"]}
```

**演进语义:** 13 键为当前闭集;加键须给缺省。`from_dict` 仅 `StructuredPredicate` 提供(`state/model.py:54-56`),fact 本身无反序列化器——消费端只读 dict。

### P3-M2 扩展:run_state_analysis `transitions` 字段(state.json)—— 演进

`state.observer.run_state_analysis`(`state/observer.py:98`)的返回 dict 增 `transitions` 键(只加不改,既有键不动),随 `cli state <dir> --out state.json` 落盘:

- `transitions.static_candidates`:`StaticCandidate.to_dict`(`state/transition.py:100`)4 键 —— `function` `var` `op` `evidence`;op ∈ `set`/`update`(session 变异 × 所属函数)或 `guard:<OP>(<value>)`(守卫谓词,如 `guard:EQ(admin)`);evidence = `["<file>:<line>"]`。证据级——不声称运行时确认。
- `transitions.note`:动态边说明——动态迁移边不在静态产物中,由 `state.transition.TransitionRecorder.observe()`(`state/transition.py:55`)在 fuzz 期写入 `StateRegistry.edges`(去重键 `(from_key, to_key, trigger)`,evidence 固定 `["dynamic:state-diff"]`)。
- `TransitionEdge`(`state/transition.py:31`)4 键:`from_key` `to_key` `trigger` `evidence`;from/to 用 `AbstractState.canonical_key`(与节点去重键同词表),会话边界由 `TransitionRecorder.new_session()` 显式重置。

执行面:`tests/state/test_state_m2.py`。

---

## C7 LLM cache JSON(solving/llm/cache/*.json)—— 冻结

| | |
|---|---|
| 生产者 | `solving/llm/cache.put`(`solving/llm/cache.py:28-32`):单行 `json.dumps({"text", "prompt_version"}, ensure_ascii=False)`;文件名 = `sha256(json.dumps({"v": prompt_version, "facts": …}, sort_keys=True, ensure_ascii=False))`(`cache.py:15-18`)。`facts` 形态由 `lifting.py` 定义:label 列(`:75-79`)、relate 字典(`:181-193`),外层再包 `{"kind": …, "facts": …}` 作缓存键 |
| 消费者 | `cache.get`(`cache.py:21-25`)——`lifting.label`/`relate` 的缓存优先通道(`lifting.py:82`、`:196`);README「已知义务」(`README.md:219`) |
| 格式 | JSON 单行(无 indent) |
| 执行面 | `test_protocols.py::test_c7_llm_cache_schema_and_key_derivation`、`test_c7_llm_cache_shipped_files_obligation` |

最小示例(仓内实测,`prompt_version` 现为 `v1`,`lifting.py:25`):

```json
{"text": "[{\"var\": \"db\", \"label\": \"irrelevant\", …}]", "prompt_version": "v1"}
```

**冻结语义:** 随仓缓存即数据——键集或字节布局变化 = 全量缓存判失效,fresh clone 语义标注回归"全 irrelevant"。变更 `PROMPT_VERSION` 或夹具须重跑生成器并提交新缓存(README 已知义务;该目录禁 `git add -A`,按显式路径提交)。

---

## C8 corpus(spool)+ global_state.json —— 演进

| | |
|---|---|
| 生产者 | 语料:`CorpusManager.save`(`fuzzing/corpus_manager.py:57-77`)写 `spool/seeds/<cid>` 原始 bytes(`cid=sha256(data)[:16]`,`:53-55`)+ `spool/meta.jsonl` 一行 `asdict(CorpusEntryMeta)`(`:74-75`)。状态:`entry_driver._dump_state`(`fuzzing/entry_driver.py:82-90`)写 `global_state.json` |
| 消费者 | 语料:`_load_existing`(`:47-51`,只取 `input_id` 去重)、`entries`(`:79-84` 反序列化回 dataclass)、`export_seed_dir`(`:86-104`,→ 下一轮 libFuzzer 种子目录)、`epoch.py:93-98`(按 `interesting_reason` 挑 focused 语料)。状态:`entry_driver` 启动恢复(`:94-101`)、`epoch.py:89-90`(champions→focused 种子)、`epoch.py:119-127`(覆盖计数/停滞判定) |
| 格式 | bytes + JSONL + JSON |
| 执行面 | `test_protocols.py::test_c8_corpus_meta_jsonl_schema_and_roundtrip`、`test_c8_global_state_frontier_mode_roundtrip`、`test_c8_global_state_feedback_mode_shape` |

### C8.1 spool/meta.jsonl(CorpusEntryMeta,`fuzzing/corpus_manager.py:17-33`,14 键)

`input_id` `epoch` `discovered_blocks` `discovered_edges` `new_blocks` `new_edges` `structural_score` `saved_at` `distance` `nearest_frontier` `interesting_reason` `origin` `parent_seed_id` `llm_attempt_id`

```jsonl
{"input_id": "9f86d081884c7d65", "epoch": 1, "discovered_blocks": 5, "discovered_edges": 4, "new_blocks": 2, "new_edges": 1, "structural_score": 3, "saved_at": 1728380000.0, "distance": null, "nearest_frontier": null, "interesting_reason": "new_blocks", "origin": "LLM", "parent_seed_id": null, "llm_attempt_id": null}
```

**演进语义:** `distance` 起为 Phase-3.1 可选键,注释(`:26-33`)明写"缺省兼容旧 meta 行";`seeds/<cid>` 是原始字节(不是评分或哈希),保证下一轮可直接作 libFuzzer 种子回放。

### C8.2 global_state.json(跨 epoch 全局覆盖,`entry_driver.py:80-90`)

- **feedback/measure 模式**(`entry_driver.py:86-89`):`{"blocks": [块id…], "edges": [[u,v]…]}`
- **frontier 模式**:`FrontierDirectedFeedback.export_state`(`fuzzing/feedback/frontier_feedback.py:46-49`)= `FrontierTracker.state`(`fuzzing/feedback/frontier_tracker.py:61-64`)+ `portfolio`:

```json
{"covered_blocks": ["app.py::handler::1-1::entry::1"], "covered_edges": [["a", "b"]],
 "version": 3, "edge_frontiers": [["c", "d"]],
 "portfolio": {"champions": {"c->d": {"input_id": "9f86d081884c7d65", "distance": 2, "epoch": 1, "frontier_version": 2}},
                "replacement_count": 1}}
```

ChampionRecord 四键:`input_id` `distance` `epoch` `frontier_version`(`frontier_portfolio.py:18-22`)。

**演进语义:** 消费端全部 `.get(键, 缺省)`(`frontier_feedback.py:52-58`、`epoch.py:125-127`),加键安全;`version` 语义 = 覆盖增长单调递增,`load_state` 读入后 +1(`frontier_feedback.py:57`)。

**演进键 `state_evidence`(阶段 1 P0-B,仅 `--observe-state` 时写入):** 顶层可选键,生产者 = `entry_driver._dump_state`(经 `StateEvidenceLedger.to_payload`,`state/runtime_observer.py`),跨 epoch 结转经 `load_payload`。键集冻结于 v1:`evidence_version`(=1)、`observer`(观测器名;`""` = 靶无观测器,显式退化到代理模式)、`verified_atoms`(`StateKey.render` 串,词形 `Predicate{Entity@Scope}`)、`verified`(全量观测记录,行四键 `key/value/evidence/source`)、`proxy_atoms`(覆盖代理口径,scope=`unknown`,与 verified 分桶不互撞)。消费端 `epoch._verified_state_atoms` 只读 `verified_atoms`;**缺整块 = 未观测**,与空集"观测为零"显式区分。执行面:`tests/state/test_runtime_observer.py`、`tests/runtime/test_execution_context.py`、`tests/fuzzing/test_lifecycle_wiring.py`。

**C8 行级演进键(PR-03,2026-10-19):** `verified` 行可含可选键 `subject`("Order.user_id",分解列)、`observed_value`(裸观测值)、`value_kind`(ENUM/BOOLEAN/NUMBER/STRING/NULL/MISSING/UNKNOWN;NULL=字段为空是已观测,MISSING=实体不存在,UNKNOWN=未观测——三值互斥)、`observed_at`(输入内请求序号,-1=输入级观测)。仅 v2 观测器携带;旧读者按缺键忽略,旧行(四键)照读。vulnshop 观测器另声明属性级可观测性 `subject_families`(内存协议属性,不入 payload)。执行面:`tests/state/test_runtime_observer.py::test_observation_v2_optional_columns_roundtrip`。

---

## C9 实验记录 JSONL(snapshots/candidates)—— 演进

| | |
|---|---|
| 生产者 | `ExperimentRecorder.tick`(`fuzzing/recorder.py:45-82`;P3-T4 增可选 `state_atoms_reached`/`state_injections` 参数):candidates 行(`:60-64`)、snapshots 行(`:66-81`),均 `json.dumps(…, ensure_ascii=False)` 逐行 flush 抗 `_Exit`;epoch 汇总 `summary()`(`:84-101`)由 EpochController 落盘 |
| 消费者 | `evaluation/experiment/epoch._load_epoch_summary`(`epoch.py:42-58`,只按 `last.get(键, 缺省)` 读末行快照、按行数计 candidates) |
| 格式 | JSONL |
| 执行面 | `test_protocols.py::test_c9_recorder_jsonl_schema` |

键集:

- candidates 行(3 键):`input_id` `epoch` `score`
- snapshots 行(8 键):`group` `epoch` `t` `executions` `blocks` `edges` `exec_seconds` `feedback_seconds`
- `summary()`(11 键,内存 dict):`group` `epoch` `wall_seconds` `executions` `exec_seconds` `feedback_seconds` `feedback_overhead` `throughput_exec_s` `blocks` `edges` `candidates_saved`

P3-T4 演进(可选键,缺省不出现——上面键集与 pin 不变):`tick` 增可选
`state_atoms_reached`/`state_injections` 参数(state 臂轮间接缝的状态
记账:覆盖代理判达原子数 / 序列种子注入数),为进程内随采样记录预留。
二者非逐执行量、演进级:传入即粘滞(`recorder.py:54-58`),此后
snapshots 行与 `summary()` 持续携带同名键。注意(P3 终审口径修正):
`tick` 当前没有任何调用方传入状态参数——实际调用方只有
`fuzzing/candidates.py:55`、`fuzzing/frontier_collector.py:61`、
`fuzzing/entry_driver.py:121`,均不传;P3 的生效状态指标不走本键,而由
`evaluation/experiment/epoch.py` 的轮间边界路径承载:`_state_boundary`
读 `epochs/state_ledger.json` + `epochs/state_injections.jsonl` →
epoch 记录 `rec["state"]` → group_summary.json 的
`state_atoms_reached`/`state_atoms`/`state_injections_total`。

```jsonl
{"group": "x", "epoch": 0, "t": 2.01, "executions": 120, "blocks": 3, "edges": 4, "exec_seconds": 1.98, "feedback_seconds": 0.4}
{"input_id": "9f86d081884c7d65", "epoch": 0, "score": 2}
```

**演进语义:** 消费端全 `.get` 缺省(`epoch.py:52-57`),加键安全;文件名模式 `<group>-e<epoch>-{snapshots,candidates}.jsonl` 由构造器决定(`recorder.py:40-43`),消费端按同名拼路径。

---

## C10 序列种子 JSON(steps)—— 演进

| | |
|---|---|
| 生产者 | `solving/state_seed_injector.py::inject_state_seeds`(P3-T3 已接):M1/M2 状态分析 + op 分派归属 → 合成 SSTG(`analysis/builder.build_sstg`,builder 零改动)→ `plan_backward(reach_state)` → `BindingContext.render_step` → 单请求 dict 序列;轮间接缝 `epoch.py::_inject_state_seeds_between_epochs`(policy="state",R2 触发:未达深状态原子 且 上轮无新状态节点)。落盘复用 `encode_sequence_seed`(`fuzzing/entry_driver.py`,与消费端同一 builder),经 CorpusManager spool(`state_spool`)+ 直写 corpus;审计 `epochs/state_injections.jsonl`。**P4-T3 双路径(R7)**:靶目录可提出 AppModel(extract_flask 有 Flask 端点)→ 真路径 `solving/vulnshop_state_bridge.py`(确定性认证链完备化 + `build_sstg` + `plan_backward`,零 LLM;判达=写点覆盖代理,值匹配锚点优先/值盲锚点限 mutating 端点;无可规划原子 → `NO_PLAN` 审计不占预算);分派只看 AppModel 可用性,微靶路径行为不变 |
| 消费者 | `fuzzing/entry_driver.decode_sequence_seed` + `dispatch_seed`(libFuzzer handler 入口,三模式一致:`entry_driver.py` dispatch);`targets/stateful/t_state_*.py` 靶本体(整体 steps 重放语义,与驱动逐步分派等价);`targets/vulnshop/fuzz_target.py` 请求 dict 面(P4-T3:method/path 步 → Flask `test_request_context` 全栈,`?slot` 运行期绑定表 + `extract` 回填;legacy 字节面 = `adapters/flask.FlaskAdapter` 路由枚举,无免费认证) |
| 格式 | JSON 单行(`json.dumps({"steps": […]}, ensure_ascii=False, sort_keys=True)`);作为一条 libFuzzer 输入整体变异 |
| 执行面 | `test_protocols.py::test_c10_sequence_seed_consumer_pin`、`test_protocols.py::test_c10_producer_artifact_round_trip`、`tests/fuzzing/test_sequence_seeds.py` |

键集:

- 顶层:`steps`(唯一键,list,可为空)
- `steps[i]` = **单请求 dict**:微靶路径 = C-legacy 冻结契约 `{"query","form","json","header","cookie"}` 的子集;AppModel 路径(P4-T3 演进)在此之上加 `method`/`path`(HTTP 动词与路由,`<int:x>` 已代入或为 `?slot` 符号)与 `extract`(响应键 → `?slot` 回填);序列里前缀步素状态,末步=当前请求

最小示例(`encode_sequence_seed` 实际输出,sort_keys 键序):

```json
{"steps": [{"json": {"op": "login", "user": "alice"}}, {"json": {"op": "profile"}}]}
```

**演进语义与分派纪律(R1 冻结裁决):**

- 分派只看 `"steps"` 键存在且值为 list:是→按序逐步执行,每步经 `json.dumps` 走与单请求**同一** `target(data)` 执行面;否→**原字节透传**,legacy 单请求路径行为逐字节不变(四臂非回归门)。已知碰撞方向:legacy 单请求 JSON 若顶层恰含 `"steps": list` 会被误判为序列种子走逐步重放(判型只看顶层键);当前靶族与生产者均不产出该形状(单请求契约键集为 query/form/json/header/cookie)。
- 会话连续性 = 进程内持久:adapter 持有的 app/test_client、靶模块级会话态跨步存活;Atheris 仍是唯一执行引擎,整条序列是一次输入,序列变异即状态空间探索(涌现)。
- 跨输入策略:无逐输入重置——状态在 libFuzzer 输入间延续(进程内);需干净态的实验以 target 的 reset_state() 显式重置(见各靶 docstring)。
- 覆盖口径 = 序列全程并集:所有步在同一 handler 调用、同一 TraceSession 请求窗口内执行,coverage 计数自然累加(R3)。
- 畸形输入确定性:`steps: []` = 零执行;非 dict 步原样序列化喂靶(靶自己的错误路径消化);`steps` 非 list → 按 legacy 透传;单步异常由外层 handler 统一吞,不向引擎泄 crash。
- 加字段安全(演进):顶层可加新键(消费端只按 `steps` 分派);`steps[i]` 内沿用单请求契约的演进纪律。
- `?slot` 符号槽(P4-T3,AppModel 路径):生产者不代入未绑定槽(代入即 None 化),保持 `"?token"`/`"/orders/?oid/pay"` 字面落盘;消费端(靶模块绑定表)运行期代入并经 `extract` 回填——绑定是靶侧进程内状态,同 `@fresh` 一样跨步持久、跨输入延续。审计 `state_injections.jsonl` 的 AppModel 行带 `path: "appmodel"` 与 `steps: ["METHOD route",…]`(NO_PLAN 行 steps 为空)。

---

## 执行面

```bash
# 仅跑协议测试
python -m pytest stateforge/tests/test_protocols.py -v
```

`test_protocols.py` 对 C1/C2/C7(冻结)断言 round-trip 字节稳定;对 C5/C6/C8/C9/C10(演进)断言键集与缺省兼容;对 C3/C4(内部)断言 dataclass 形状;另以 `test_c0_protocols_md_sections_pinned` 锁定本文 C1–C10 小节存在性——文档删节而测试不删即红,两向同步。

---

## C11 relational_arm 消融记录 —— 演进

| | |
|---|---|
| 生产者 | `evaluation.experiment.relational_arm.run_ablation`(PR-07)→ `relational_arm.jsonl`(每臂一行)+ `relational_arm_summary.json` |
| 消费者 | 离线复核/论文口径(无 LLM 消融 A/B/C/D) |
| 格式 | JSONL;行键 `arm / requests_executed / inputs / wall_seconds / target_block_covered / goal_coverage{achieved,total,note} / instance_state_novelty / verified_transition_novelty / proxy_progress{atoms} / notes` |
| 执行面 | `tests/evaluation/test_relational_arm.py` |

**演进语义与纪律**：指标四桶分键、永不混计——`goal_coverage.achieved` 仅统计一致观测点完整 witness（无观测臂为 `null`+note，不伪造 0）；`instance_state_novelty`/`verified_transition_novelty` 为首见计数（无静态分母）；`proxy_progress` 独立分桶。公平字段（seed/budget/requests_executed/wall）每行必带。同种子确定性由 `random.Random(seed)` 保证。
