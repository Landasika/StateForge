# goal_directed_eval 三臂对照(Phase 6)

预算=60 请求/靶,随机种子=7

## t_state_chain.py(目标 1 个)
| 臂 | reach | cost/target | requests |
|---|---|---|---|
| single_random | 0/1 | — | 60 |
| seq_random | 1/1 | 69-70:12 | 60 |
| goal_directed | 1/1 | 69-70:2 | 2 |

## t_state_gate.py(目标 1 个)
| 臂 | reach | cost/target | requests |
|---|---|---|---|
| single_random | 0/1 | — | 60 |
| seq_random | 0/1 | — | 60 |
| goal_directed | 1/1 | 69-70:2 | 2 |

## t_state_shop.py(目标 3 个)
| 臂 | reach | cost/target | requests |
|---|---|---|---|
| single_random | 0/3 | — | 60 |
| seq_random | 1/3 | 78-83:27 | 61 |
| goal_directed | 3/3 | 78-83:2;91-92:3;94-95:4 | 9 |
