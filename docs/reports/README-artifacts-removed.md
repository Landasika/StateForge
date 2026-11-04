# 关于报告中 `artifacts/` 路径的说明

`docs/reports/` 下多份报告（SS-06、SS-07A~F、SESSION_FIRST_EXPERIMENT 等）
引用了形如 `artifacts/session_ss07f/...` 的产物路径。

**这些目录已于 2026-11-04 从仓库移除**，原因见下。报告中所有数字、结论、
判定与命令均未改动——它们记录的是当时的实测结果，仍然是有效的记述；
只是"去哪里找当时的原始产物"这一项不再成立。

## 移除了什么

| 目录 | 体积 | 内容 |
|------|------|------|
| `artifacts/session_ss07f` | 18 MB | SS-07F 覆盖补充与 heldout 冻结产物（含 ctfd 等应用的 `result.json`） |
| `artifacts/session_ss06` | 4.7 MB | SS-06 会话线原始 JSONL 与 per-run 产物 |
| `artifacts/session_ss07a/d/c` | 3.1 MB | 各轮 A/C/D 原始产物 |
| `artifacts/session_first` | 644 KB | session-first 实验产物 |
| `artifacts/m0` | 48 KB | M0 关系线样例产物 |
| 其余 session_* | ~60 KB | — |

合计约 **15.8 MB**（1279 个文件）。

## 为什么移除

仓库入库体积需要收缩到 10 MB 以内（主体代码优先）。这些产物的特点是：

- 体量大（占当时入库体积的 62%）；
- **可重新生成**——每份报告同时给出了生成命令与判定标准；
- 报告正文已经固化了结论与数字，产物只用于复核。

## 仍然保留的测量留证

`artifacts/measurement-summary/`（约 500 KB）保留了**汇总层**：

- `p4_formal/p4_report`、`p4_report.txt` —— 论文 evaluation 数字的直接来源；
- 各轮战役/消融的 `summary.json`；
- `goal_eval/` 报告。

并附有该目录自己的 README，说明"保留了什么 / 没保留什么 / 可复现性边界"。

## 如何重新生成

各报告的「产物」或「复现」小节给出了确切命令，例如 SS-07F：

```bash
python -m stateforge.evaluation.experiment.session_ablation \
  --heldout artifacts/session_ss07f/heldout_v2 ...
```

重跑需要注意：结果**可能与报告中的数字不同**（机器负载、依赖版本、随机
种子漂移）。引用数字时应以报告正文为准，不要把新跑的结果当成对历史的
"更正"。

## 判定标准未变

移除产物不影响任何验收标准。当前全量测试门禁仍是：

```
881 passed, 1 skipped, 26 deselected
```

`stateforge/tests/test_artifact_docs.py` 不读取 `artifacts/`，全部通过。
