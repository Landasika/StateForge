# 联合消融对照(2026-10-15,零代码修改实验轮)

环境:无 LLM API key(Provider=None;缓存仅 vulnshop 适用)→ 四臂中
D 臂 LLM 注入降级为定向反馈实效(v0.4 口径注一致),如实记录。
口径:均为 spot 小样本(goal 三臂=确定性 60 请求预算;four_arm 与
ablation=2 runs × 3 epochs × 5s,种子 7),非正式战役数字。

## 组 A:goal_directed 三臂(确定性构造 vs 盲搜)

| 靶 | single_random | seq_random | goal_directed |
|---|---|---|---|
| t_state_chain | 0/1 | 1/1(cost 12) | **1/1(cost 2)** |
| t_state_gate | 0/1 | 0/1 | **1/1(cost 2)** |
| t_state_shop | 0/3 | 1/3(cost 27) | **3/3(cost 2/3/4)** |

goal 臂 5/5 判达,成本 2-4 请求/目标;随机臂合计 2/5、唯一命中成本
高一个量级;gate(谓词门)为随机不可达(值集中性无谓词泄露)。

## 组 B:four_arm 四臂消融(stateful 套件,Atheris 战役)

| 靶 | B(结构) | C(定向) | D(+LLM,降级) | E(状态) | E−D |
|---|---|---|---|---|---|
| t_state_chain | 17 | 17 | 17 | **36** | **+19.0** |
| t_state_gate | 17 | 17 | 17 | **36** | **+19.0** |

E 臂(旧状态注入器)显著增益;D 无 key 降级 = C(D−C=0),与 v0.4
留档一致;复现 v0.4 spot 口径(+16/+19 量级)。

## 组 C:state_ablation 三臂(E / E-rand / E-always)

| 靶 | E−E-rand(规划器贡献) | E−E-always(R2 触发代价) |
|---|---|---|
| t_state_chain | +0.0 blocks(spot 噪声;v0.5 正式口径 +2.4) | −0.5(≈零) |
| t_state_gate | **+6.0 blocks +1.0 atoms** | 0.0(≈零) |

## 读法(三组拼起来)

1. **机制层(组 A)**:goal 定向构造在状态门深块上 reach 5/5、成本
   个位数;两个随机对照合计 2/5——静态引导的增益来源确证。
2. **引擎层(组 B)**:同一套件上旧 E 臂(规划注入+Atheris 变异)块
   数 +19/靶,证明注入种子被引擎放大。
3. **组件层(组 C)**:E 的增益主要来自规划器(gate 上 +6)而触发
   代价≈零(E-always 不亏)。
4. **LLM 维度**:本环境无 key,D 臂全程降级——LLM 通道在组 B/C
   均未参与(诚实边界;v0.4 留档同口径)。

## 待讨论(需改代码,未动)

- goal_directed 作为新臂接入 four_arm/campaign 正式口径(新臂 F?)
- goal 链与 E 臂注入器合流(goal 产的 C10 种子喂 state_spool)
- LLM key 配置后 D 臂重跑(以及 20 靶 LLM 标注缓存生成)

产物:results/goal_eval/ | results/experiment/four_arm_ablation_joint/
| results/experiment/ablation_joint_{chain,gate}/ + 本报告
