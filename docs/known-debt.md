# 已知留档不修清单(known cosmetic debt)

来源=P5 终审(v1.0-release 冻结,原载 task-3 报告终版清单);逐条当日复核在树(HEAD a48b5ae,行号为当日实测)。均为观感/文档级,或经裁定保守性可接受——不修不影响验收主张,列此备查。

## 代码侧(P3/P4 挂账)

1. **PROTOCOLS 执行面锚宽松** — `stateforge/tests/test_protocols.py:72-79`:只钉 C1–C10 节头 + 三级词汇(冻结/演进/内部),「执行面→测试名」映射无机检,任一侧改名静默漂移。不修:补机检属增强而非缺陷,漂移由评审兜底。
2. **M2 嵌套守卫端点级归因** — `stateforge/analysis/patterns.py:356`:嵌套 If 守卫按 (kind,detail) 去重后归端点,不按外层分支条件归因。不修:端点级粒度对 M2 结论足够,分支级归因属精度增强。
3. **_EMPTY_STATE 浅拷贝(潜伏)** — `stateforge/evaluation/experiment/state_arm_report.py:59`(模板含可变 list `atoms`),:127/:224 `dict(_EMPTY_STATE)` 共享;当前所有键整键重赋故无害,原地变异即污染。不修:现用法整键重赋,无活跃缺陷,改 deepcopy 属防御性重构。
4. **「末 return=成功路径」启发式** — `stateforge/solving/vulnshop_state_bridge.py:95`(`_success_return_line`,docstring 自认过近似):函数解析不出回退端点行;多 return 非末成功形态会错锚。不修:vulnshop 靶内早退错误惯用法下恒正确,泛化形态不在靶面。
5. **值盲锚点过近似** — `stateforge/solving/vulnshop_state_bridge.py:36-39`:值盲写锚对多值变量可能超前判达(诚实记录)。不修:偏差方向保守(多判不漏判),且已在文档披露。
6. **campaign._manifest 死参 `out`** — `stateforge/evaluation/experiment/campaign.py:43`:签名收 `out` 函数体零引用;两调用点照传。不修:改签名须动两调用点,纯接口清理、零行为收益。
7. **JSONL 审计 helper 三处同形** — `epoch.py:193`(`_state_audit`)/ `epoch.py:199`(`_llm_audit`)/ `state_ablation.py:197`(`_audit`,均在 `stateforge/evaluation/experiment/`):同 append 形状跨三处。不修:向上复用违反分层,重复系设计。
8. **vulnshop DB 残留(pid 复用保守)** — `stateforge/targets/vulnshop/fuzz_target.py:49-79`:死 pid 清扫已覆盖 kill -9 / libFuzzer `_Exit`,但 pid 复用 → 误判存活 → 留残文件(:58-59,保守不删)。不修:误删他人活进程 DB 的风险大于留残,保守取舍正确。

## 文档侧(P5 T1/T2 挂账)

9. **ARTIFACT.md 措辞两项** — `ARTIFACT.md:49` 双「口径注」从句连排(不歧义,观感);`ARTIFACT.md:122` `grep -v` 按子串过滤 symlink 行(仓内唯一命中,文中已注)。不修:语义无歧义,改动仅观感。
10. **论文两版 :216 `\texttt` 尾随空格** — `papers/draft/main.tex:216`、`papers/draft/main-zh.tex:216`:`\textless authed\textgreater ` 在 `\texttt` 内多一空格(两版未编译,渲染无从验证)。不修:本机无 LaTeX 工具链,改动无法经编译验证。
