# 测量汇总留证（原 `results/` 的摘要层）

本目录是从本地 `results/` 实验输出中**挑出的汇总层**（46 个文件、~224KB），
用于在删除原始轨迹目录后，仍能追溯到论文与 README 里引用的数字。

## 保留了什么

- `experiment/p4_formal/p4_report`、`p4_report.txt` —— 论文 evaluation 章节
  数字的直接来源（`p4_analysis` 的输出抄录）。
- 各轮战役/消融的 `summary.json` —— 每次 run 的最终指标与臂配置。
- `goal_eval/` —— goal 求值报告与消融摘要。
- 少量 `*.md` / `*.csv` / `*.txt` 报告。

## 没保留什么

原始 `results/` 里的逐次执行轨迹（`metrics.jsonl`）、种子二进制（`state_spool/seeds/`）、
逐运行中间产物**未保留**——它们占 150MB 的绝大部分，且可由命令重新生成。

## 可复现性边界

保留了汇总数字，**不等于**保留了可重算的原始数据。若要重跑：

```bash
# 五臂 spot
python -m stateforge.evaluation.experiment.four_arm --out results/experiment --runs 3 --epochs 3 --seconds 8

# 正式战役（论文口径，10-run 配对）
# 目标与参数见 ARTIFACT.md「正式战役(campaign --groups D1,E)」一节
python -m stateforge.evaluation.experiment.campaign ... --runs 10 --epochs 3 --seconds 10 --groups D1,E

# 汇总重算
python -m stateforge.evaluation.experiment.p4_analysis --root results/experiment/p4_formal --out ...
```

注意：重跑结果与历史数字**可能不同**（机器负载、依赖版本、随机种子漂移），
故引用论文数字时应以本目录的 `p4_report*` 为准，而不是以新跑的结果覆盖。

## 与 git 的关系

原始 `results/` 一直被 `.gitignore` 排除（`git ls-files results` = 0），
因此它从未进入版本历史；本目录则是**入库**的，作为长期留证。
