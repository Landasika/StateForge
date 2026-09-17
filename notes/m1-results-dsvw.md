# M1 结果报告:DSVW 双模式(新环境复现,2026-09-17)

> 环境:Ubuntu 24.04 / 8 核 / 15G;ANOTA(CPython 3.10.13 fork, gcc 13.3);lxml 6.1.3。
> 重建步骤与差异:`env-rebuild-2026-09-17.md`。原始数据:`exp/results/*.json`。

## 1. ANOTA 自检

| 原语 | 结果 |
|---|---|
| ANOTA_WATCH / ANOTA_EXECUTION | ✅ 全过 |
| ANOTA_SYSCALL(仅日志模式) | ✅ 全过 |
| ANOTA_TAINT | ❌ 与旧环境同一 C 层 bug(§7 风险1),按既定决策绕过 |

## 2. 检测矩阵(20 testcase = core 14(与旧环境对齐)+ ext 6)

| 模式 | TP | FN | FP | TN | TP-MISMATCH |
|---|---|---|---|---|---|
| 基线(长驻服务) | 11 | 0 | 0 | 9 | 0 |
| **快照(fork-per-testcase)** | **11** | **0** | **0** | **9** | 0 |

- **core 14 子集两种模式均为 7/0/0/7,与旧环境第一次运行完全一致。**
- ext 6 为本环境新增:lxml 解禁后 `attack_xxe_local`、`attack_xxe_remote`、`attack_xpath_injection` 全部 TP(旧环境这 3 条 N/A),`attack_ssrf`(fetch_whitelist)TP。
- 判定信道:违规 → RuntimeError("ANOTA_POLICY:<名>")→ 500 traceback 进响应体,harness 按策略名归因(逐条命中矩阵见 JSON)。

## 3. 注解策略(10 条 @ 7 个 sink,全部 `ANOTA_EXECUTION.BLOCK` 守卫风格)

core(与旧环境同名同义):`pickle_dict_only` / `path_web_root` / `path_no_source` / `domain_syntax` / `include_local_root` / `tmpl_plain_text` / `charset_syntax`
新增:`xml_no_dtd` / `xpath_syntax`(lxml)/ `fetch_whitelist`(SSRF)

## 4. 快照开销与隔离性

| 指标 | 旧环境(2核/7.8G) | 新环境(8核/15G) |
|---|---|---|
| 完整进程重启(至可服务) | 54 ms | **64.7 ms**(median,15 reps;含解释器启动+DB init+bind) |
| fork | 0.37–1.13 ms | **0.258 ms**(median;min 0.198 / max 0.476) |
| reap(waitpid) | ≤1.12 ms | **0.588 ms**(median) |
| **恢复开销(fork+reap)** | 3.84 ms 起(含 serve) | **0.839 ms**(median,min 0.784) |
| serve(单请求往返)median | — | 2.09 ms(剔除 pickle 真实执行 ping 的 1067ms 离群值) |
| 父进程 RSS(全矩阵+DoS 探针后) | 22.7 → 22.8 MB | 28928 → 29056 kB(**+0.4%**) |
| 吞吐(含请求处理) | — | 17.4 testcase/s(受 pickle 真执行 1s 拖累) |

**对比:快照恢复开销 ≈ 0.84ms vs 完整重启 ≈ 65ms → 约 1/77。**

隔离性证据(快照模式):
1. 子进程写评论(`?comment=probe123`)后,下一子进程读评论列表 → **probe 不可见**(COW 写随子进程销毁)✅
2. 有界 DoS 探针(`?size=10000`,子进程内瞬时构建 ~100MB 字符串阵)打完,父进程 RSS 仅 +128kB ✅
3. 基线模式同一评论写入**会**对后续请求可见 —— 两种模式的状态语义差异被实验直接展示,正是 Atropos 语义价值所在。

## 5. 诚实备注

- `pickle_dict_only` 为**事后检出**:快照日志中攻击载荷的 `ping -c 2 127.0.0.1` 真实执行(2 个 ICMP 包,~1s)后才命中。EXECUTION 守卫对"loads 即 RCE"类是检测器不是阻止器(与旧环境结论一致;阻止需 WATCH 风格入口拦截,列为后续实验项)。
- 命中计数:单条响应的 traceback 会含策略名两次(harness 按去重策略名归因,不影响判定)。
- `benign_domain_lookup`(nslookup localhost)实测 ~20ms;若在无解析器环境会退化为"命令不存在"输出,仍为 TN,不影响矩阵。
- kvm 依旧缺失:Nyx/VM 级数字只能引 Atropos 文献,不可声称复现;本实验的对比叙事是 **VM 级 vs 进程级(fork COW)vs 逻辑级(DB 回滚)**。

## 6. 与创新点的衔接

- 创新 1(LLM 标注)gold standard:本套件 10 条策略 / 7 个 sink / 20 条用例即人工标注成本基线(见 `dsvw_anota.py` diff)。
- 创新 3(多深度检查点):快照父进程已验证可在任意检查点冻结;M4 将在"登录后/建资源后"打多个检查点。
- M2 下一站:VAmPI + OWASP Vulnerable Flask App(Flask errorhandler 检测信道)。
