# M1 重建手册(新环境 /home/landasika,2026-09-17)

> 旧环境(/home/coder/icassp)不可用,本文件记录在新环境从零重建 M1 的完整步骤、差异与坑。
> 结果报告见 `m1-results-dsvw.md`。

## 0. 环境差异(旧 → 新)

| 项 | 旧环境 | 新环境 | 影响 |
|---|---|---|---|
| CPU / RAM | 2 核 / 7.8G | **8 核 / 15G** | 构建与实验更快 |
| /dev/kvm | ❌ | ❌(同样未开嵌套虚拟化) | 快照维持 L1 进程级方案 |
| sudo | ❌ | ✅ | lxml 可装,XXE/XPath case 解禁(旧环境 N/A) |
| github.com 直连 | ✅ | ❌(TLS 被断) | 走镜像 `https://gh-proxy.com/` 前缀 |
| pip/PyPI | 未装 | tuna 镜像直装 | ANOTA 解释器内 `ensurepip` + `pip install lxml` |

## 1. 系统依赖

```bash
sudo apt-get install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev \
    tk-dev uuid-dev libxml2-dev libxslt1-dev pkg-config libgdbm-dev libnss3-dev
```

## 2. ANOTA artifact 与解释器构建

```bash
# github 直连失败,走代理镜像下载 tarball
curl -sL -o anota.tar.gz "https://gh-proxy.com/https://codeload.github.com/ANOTA-Sanitizer/ANOTA/tar.gz/refs/heads/main"
tar xzf anota.tar.gz && mv ANOTA-main ANOTA
cd anota/ANOTA/ANOTA-interpreter
./configure --prefix=$HOME/icassp/anota/ANOTA-interpreter-install   # 故意不加 PGO/LTO,与旧环境可比
make -j8        # 约 4 分钟
```

自检(与旧环境结论一致):

| 测试 | 结果 |
|---|---|
| `./python anota_object_access_test.py`(WATCH/EXECUTION) | ✅ 全过 |
| `./python anota_syscall_test.py`(SYSCALL,仅日志) | ✅ 全过 |
| `./python anota_taint_test.py`(TAINT) | ❌ 同一 C 层 bug 复现:`SystemError: _PyEval_EvalFrameDefault returned a result with an exception set`(sink RuntimeError 传播中 traceback 格式化再次进入 taint 回调)。维持"绕过不修"决策,套件只用 WATCH/EXECUTION。 |

## 3. lxml(旧环境 N/A 项解禁)

```bash
P=$HOME/icassp/anota/ANOTA/ANOTA-interpreter/python
$P -m ensurepip --default-pip
$P -m pip install lxml -i https://pypi.tuna.tsinghua.edu.cn/simple   # lxml 6.1.3
```

## 4. 靶场与实验文件

- 靶场:`targets/dsvw/dsvw.py`(stamparm/DSVW,同样走 gh-proxy 镜像)+ 良性 fixture `static.txt`、`plugin.txt`
- `exp/dsvw_anota.py`:在 dsvw.py 原文上最小 diff 植入 10 条 `ANOTA_EXECUTION.BLOCK(..., msg="ANOTA_POLICY:<名>")` 守卫(7 条 core 与旧环境同名同义;`xml_no_dtd`/`xpath_syntax` 为 lxml 解禁新增;`fetch_whitelist` 为 SSRF 新增)
- `exp/harness_dsvw.py`:基线模式(长驻 subprocess)+ 完整进程重启基准 + 用例矩阵(20 条:11 攻击 / 9 良性;core 14 与旧环境对齐)
- `exp/snapshot_dsvw.py`:快照模式(父进程冻结在检查点 = DB init 完成 + listen socket 已绑定;每 testcase fork COW 子进程 `handle_request()` 一次后 `os._exit`)
- 结果:`exp/results/{baseline,snapshot}_results.json`

## 5. 坑与决策(本次新踩/复核)

1. **fork-per-request 必须用同步 server**:`http.server.HTTPServer.handle_request()`(非 ThreadingMixIn),子进程服务完一个请求 `os._exit(0)`,无在途线程问题 —— 旧环境风险 #5 的规范得到遵守。
2. **守卫语义方向**:SSRF 的攻击载荷打的是内网(`http://127.0.0.1:631`),第一版守卫写成"只许本机"反而放行攻击(FN);正确语义是"外部域名白名单"(`fetch_whitelist`)。注解语义 = 允许集的声明,不是攻击特征匹配。
3. **注解必须都在 try 覆盖区内**(旧环境风险 #4 复核成立):charset 守卫放在 try 顶部而非 finally,违规 → 500 traceback 进响应体,检测信道不需要额外 errorhandler。
4. **pickle"先执行后检出"再次实证**:快照模式日志里 `ping -c 2 127.0.0.1` 真实发出 2 个 ICMP 包(约 1s)后才命中 `pickle_dict_only`。EXECUTION 守卫对该类是检测器不是阻止器(旧环境风险 #2 复核成立)。
5. curl/URL 编码注意:payload 里的空格要编码为 `%20`(q() 全量编码),`+` 会被 DSVW 的 unquote 当字面量,导致 shell 命令变形。

## 6. 一键复跑

```bash
cd ~/icassp/exp
~/icassp/anota/ANOTA/ANOTA-interpreter/python harness_dsvw.py --reps=15     # 基线 + 重启基准
~/icassp/anota/ANOTA/ANOTA-interpreter/python snapshot_dsvw.py              # 快照 + 隔离探针
```
