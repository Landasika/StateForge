# ARTIFACT.md — 评估者入口(Evaluator Entry Point)

StateForge artifact 说明:**仓库即 artifact**(NDSS artifact evaluation 惯例——clone + 安装 + 测试即验收;发布渠道为私有 Gitea,无 tarball)。本文是评估者入口:环境要求、一条命令安装、每张冻结表对应的复现命令与预期值、badge 映射、已知限制。完整叙事(架构图/阶段历史/全部数字口径)见 [README](README.md)。

---

## Overview / 概览

StateForge 是面向 Python Web 应用的**结构 + 状态双反馈模糊测试**系统:在 Atheris(变异引擎,唯一执行者)之上叠加多层反馈——静态执行图(ICFG)→ 运行时轨迹映射 → 覆盖前沿检测 → 分支约束求解 → LLM 语义推理 → 状态序列规划——当传统 fuzzing 在某道分支门前停滞时,主动生成能过 `if` 的输入写回语料库,由 Atheris 继续变异探索。项目按 P0 基线→P4 评测规范化五期交付(标签与验收数字见 README「冻结版本」节);本 artifact 对应 `main` 分支仓库本体,`./ci.sh` 端到端证明 fresh-clone 可复现(clone→venv→install→pytest→微基准)。系统全貌见 [README](README.md)。

## Requirements / 环境要求

| 项 | 要求 | 验证环境(v0.1 起,见 README 冻结节) |
|----|------|------|
| Python | ≥ 3.10 | 3.13.5(anaconda) |
| 操作系统 | Linux(Ubuntu 系) | Ubuntu,Linux 6.8 内核 |
| 编译器 | clang(仅备用) | atheris 无预编译 wheel 时 sdist 编译需要;验证环境实走 wheel,未触发 |
| 磁盘 | 数百 MB | 仓库本体 + venv;`anota/`(ANOTA 解释器,~GB)与 `external/`、`results/` 不属于本 artifact、不入 git |
| 网络 | 可达 PyPI | 安装期唯一外联。**公开 PyPI 即可,无需任何镜像配置**(开发机本地的 tuna 镜像设置与评估者无关) |

## Install / 安装

```bash
git clone <本仓库 Gitea 地址> stateforge-artifact
cd stateforge-artifact
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,test,fuzz]"      # 唯一依赖源:pyproject.toml
pytest                                 # 全量测试
```

预期:全量测试绿。计数随阶段增长,本文不硬编码——**以 README「冻结版本」节验收行与 pytest 汇总行为准**。26 个 `anota` 标记用例默认 deselect(ANOTA 解释器侧不在本 artifact 验收链内);少量 skipped 为 external/ 不入库按设计跳过。

一键验收(等价于上面四步 + 微基准):`./ci.sh`(或 `make verify`)。

## Reproducing the tables / 复现实验表

每小节对应一张冻结表;命令为真实 CLI 口径,预期 headline **引用 README「冻结版本」节的数字**。统计容差口径统一见[Badges 节](#badges--徽章映射):fuzzing 数字有随机性,复现判定看配对差方向与量级带,非逐位相等。

### 五臂 spot(four_arm)

v0.4-stateful-core 验收口径(2 runs × 3 epochs × 5s,E=状态臂;IR 由脚本自动对套件目录 analyze,无需手跑):

```bash
python -m stateforge.evaluation.experiment.four_arm \
    --suite stateful --out results/experiment/four_arm_spot \
    --runs 2 --epochs 3 --seconds 5
```

预期 headline:每靶打印 `B/C/D/E` 块均值与差值行——README v0.4 验收记录 **E−D=+16/+19 块(两状态靶)、状态原子判达 E 4/4 run vs D 0/4 run(同机制)**。口径注:非 session 靶(microplus 套件)上 E 落后 D,状态主张限定 stateful 靶族。

### 正式战役(campaign --groups D1,E)

v0.5-evaluation 10-run 配对战役口径(3×10s/seed0=7)。**ir 须为套件目录 analyze 出的 program_ir.json**——单文件 analyze 产 0 块 IR,战役会不报错地烧完全程;空/坏 IR 守卫在启动即 `parser.error`(退出码 2)点名 IR 路径并提示套件目录口径,挡住这一错路:

```bash
# 1) 套件目录 analyze(不是单文件)
python -m stateforge.cli analyze stateforge/targets/stateful \
    --out results/experiment/p4_formal/ir_chain_gate

# 2) 10-run 配对战役(t_state_chain;t_state_gate 同理换名;每靶约 10 分钟)
python -m stateforge.evaluation.experiment.campaign \
    stateforge/targets/stateful/t_state_chain.py \
    results/experiment/p4_formal/ir_chain_gate/program_ir.json \
    --out results/experiment/p4_formal/camp_t_state_chain \
    --runs 10 --epochs 3 --seconds 10 --groups D1,E

# 3) vulnshop(真实应用靶):目录口径同上
python -m stateforge.cli analyze stateforge/targets/vulnshop \
    --out results/experiment/p4_formal/ir_vulnshop
python -m stateforge.evaluation.experiment.campaign \
    stateforge/targets/vulnshop/fuzz_target.py \
    results/experiment/p4_formal/ir_vulnshop/program_ir.json \
    --out results/experiment/p4_formal/camp_vulnshop \
    --runs 10 --epochs 3 --seconds 10 --groups D1,E
```

预期 headline(README v0.5 验收):**E−D1 块增益 t_state_chain +20.2、t_state_gate +22.0、vulnshop +99.9(median 98,10/0/0 全胜,AUC +0.0967)**。

### 消融(state_ablation)

同 IR 口径(套件目录 analyze 的 program_ir.json),三臂 E / E-rand(随机序列)/ E-always(恒注入):

```bash
python -m stateforge.evaluation.experiment.state_ablation \
    stateforge/targets/stateful/t_state_chain.py \
    results/experiment/p4_formal/ir_chain_gate/program_ir.json \
    --out results/experiment/p4_formal/abl_t_state_chain \
    --runs 10 --epochs 3 --seconds 10
```

预期 headline(README v0.5 验收):规划器贡献 **E−E-rand +2.4/+5.5/+65.6**(t_state_chain/t_state_gate/vulnshop,真实应用上最大);R2 触发代价 **|E−E-always| ≤1.1**(≈零)。注:vulnshop 的消融三状态指标为未测(见已知限制)。

### 分析(p4_analysis)

战役/消融跑完后聚合为论文口径表(对产物只读):

```bash
python -m stateforge.evaluation.experiment.p4_analysis \
    results/experiment/p4_formal --out results/experiment/p4_formal/p4_report.json
```

预期:stdout 文本表 + `<out>.json`/`<out>.txt` 落盘,首行 `campaign dirs 3 | ablation dirs 3`;表值即 README v0.5 数字来源。

### SF-Micro 微基准(make demo)

```bash
make demo    # = python -m stateforge.evaluation.micro_benchmark
```

预期:静态 21/21(CFG/CALL/RETURN P&R)+ 动态 5/5(trace-accuracy)**全 1.00**(Phase-1 冻结真值)。

### CI 一键(ci.sh / make verify)

```bash
./ci.sh      # 或 make verify
```

预期:临时 clone → venv → install → pytest → micro_benchmark 全链绿,尾部打印三验收数字(pytest 汇总行 / micro 静态+动态)。这就是 Badge **Available** 的执行面。

## Badges / 徽章映射

| Badge | 含义 | 本 artifact 的执行面 |
|-------|------|--------------------|
| **Available** | artifact 可得、可安装、可跑测试 | fresh clone + `pip install -e ".[dev,test,fuzz]"` + pytest 全绿 = `./ci.sh` 全链绿 |
| **Functional** | 声称的功能可被评估者独立触发 | `make demo`(微基准)+ 五臂 spot(four_arm)+ 正式战役命令(campaign)在评估者环境全部可跑通,产物结构完整(curves.csv / summary.json / manifest.json) |
| **Reproduced** | 冻结表数字可复现 | 10-run 战役数字落在 README「冻结版本」节报道值的随机容差带内;判定口径 = **配对差方向**(E−D1 三靶全胜/多数胜)+ 量级带,非逐位相等 |

统计口径注:战役/消融数字含随机性(fuzzing 时间预算与调度噪声),同一 `--seed0 7` 下跨机器也非逐位可复现;**方向性结论(E > D1)与数量级差应稳定复现**。

## Known limitations / 已知限制

- **M1 session 词汇边界**:t_state_shop 状态原子零注入(5A DB 域守卫对 M1 会话词汇不可见)——状态主张限定 stateful 靶族 + vulnshop;非 session 靶(microplus)E 落后 D。
- **vulnshop 消融原子未测**:消融侧三状态指标(atoms/transitions/deep)仅微靶路径可测;vulnshop 的状态原子口径为 campaign 侧 AppModel 分派,两侧不可互推。
- **跨机器语料盐 = 靶绝对路径**:state 种子渲染的 BindingContext salt 取靶路径 sha256(`stateforge/solving/state_seed_injector.py`),语料种子字节跨机器不逐位一致;配对差结论不受影响(同机配对)。
- **LLM 臂需 provider 配置**:four_arm D 臂的 LLM 注入需 `STATEFORGE_LLM_API_KEY` 等 4 个环境变量(README「配置」节);未配置时自动降级规则标注、链路不阻塞,但 LLM 注入不会实际发生。E 臂(状态规划)零 LLM、无 provider 依赖。

## License

Proprietary — Research use only.(与 README 一致)
